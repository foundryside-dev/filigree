"""Cross-surface parity for the 2.0 error envelope.

tests/test_error_envelope_contract.py pins the envelope *shape* on each
surface individually. This module pins the *code* each surface emits for
the same logical bad input — dashboard, MCP, and CLI must agree or a
client pattern-matching on ``code`` will behave inconsistently.

Scope (from the 2026-04-23 continuation prompt):

- Seven bed-down cases that round-tripped through Stages 1 + 2a and drove
  the bed-down commits (11cfb80, dc3917e). Each is one test class with
  three surface invocations plus a parity assertion.
- The ``POST /api/v1/scan-results`` envelope, which is dashboard-only but
  is Stage 2B's highest-risk Clarion-facing hop. Pinning the error
  envelope shape here is the pre-release gate in lieu of a Clarion
  staging environment.

Each parity test creates three isolated per-surface projects (no shared
state) because we are asserting envelope shape for a class of input, not
state sync. ``initialized_project`` (tests/conftest.py) provides the CLI
path; inline setup wires the dashboard and MCP surfaces for the same
test.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable, Generator
from pathlib import Path
from typing import Any, cast

import pytest
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

import filigree.dashboard as dash_module
import filigree.mcp_server as mcp_module
from filigree.cli import cli
from filigree.core import DB_FILENAME, FILIGREE_DIR_NAME, SUMMARY_FILENAME, FiligreeDB, write_config
from filigree.dashboard import create_app
from filigree.mcp_tools.issues import (
    _handle_claim_issue,
    _handle_close_issue,
    _handle_create_issue,
    _handle_get_issue,
    _handle_update_issue,
)
from filigree.types.api import ErrorCode

_VALID_CODES: frozenset[str] = frozenset(e.value for e in ErrorCode)


# ---------------------------------------------------------------------------
# Envelope extraction + shape guard
# ---------------------------------------------------------------------------


def _assert_flat_envelope(payload: Any, *, surface: str) -> None:
    """Assert ``payload`` conforms to the 2.0 flat error envelope.

    Deliberately duplicates the helper in tests/test_error_envelope_contract.py
    rather than importing it across the tests/ tree — that file has a
    dashboard-specific fixture and imports would pull unrelated collection
    into util/.
    """
    assert isinstance(payload, dict), f"[{surface}] payload is not a dict: {payload!r}"
    assert "error" in payload, f"[{surface}] missing 'error': {payload!r}"
    assert "code" in payload, f"[{surface}] missing 'code': {payload!r}"
    assert isinstance(payload["error"], str), f"[{surface}] 'error' is not a string: {payload!r}"
    assert payload["error"], f"[{surface}] empty 'error': {payload!r}"
    code = payload["code"]
    assert isinstance(code, str), f"[{surface}] 'code' is not a string: {code!r}"
    assert code in _VALID_CODES, f"[{surface}] 'code' {code!r} not in ErrorCode"
    assert "message" not in payload, f"[{surface}] legacy 'message' key: {payload!r}"
    assert "message_detail" not in payload, f"[{surface}] legacy 'message_detail' key: {payload!r}"


def _mcp_envelope(result: list[Any]) -> dict[str, Any]:
    """Parse an MCP tool handler result; fail clearly if the response is not
    a JSON error envelope."""
    assert result, "MCP handler returned empty content list"
    text = result[0].text
    payload = json.loads(text)
    assert isinstance(payload, dict), f"MCP payload not a dict: {payload!r}"
    return payload


def _cli_envelope(result: Any) -> dict[str, Any]:
    """Extract the JSON envelope from a CliRunner result.

    Parses the full output as JSON — ``filigree <cmd> --json`` on error
    paths emits a single JSON object (possibly multi-line via indent=2),
    and on success also uses multi-line JSON. If a stray non-JSON prefix
    exists (e.g. a Click usage error printed before JSON), slice from the
    first ``{`` to the last ``}``.
    """
    assert result.exit_code != 0, f"CLI expected non-zero exit for error scenario; got 0 with output: {result.output!r}"
    out = result.output
    try:
        return cast(dict[str, Any], json.loads(out))
    except json.JSONDecodeError:
        start = out.find("{")
        end = out.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise AssertionError(f"CLI output is not JSON and has no {{...}} span: {out!r}") from None
        return cast(dict[str, Any], json.loads(out[start : end + 1]))


# ---------------------------------------------------------------------------
# Per-surface harness fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def dashboard_surface(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[AsyncClient]:
    """Isolated dashboard surface for parity tests.

    Mounts a fresh FiligreeDB at the module global ``dash_module._db`` and
    yields an AsyncClient pointed at the app. Cleanup restores the
    previous global so concurrent tests don't interfere.
    """
    tmp = tmp_path_factory.mktemp("parity-dash")
    filigree_dir = tmp / FILIGREE_DIR_NAME
    filigree_dir.mkdir()
    write_config(filigree_dir, {"prefix": "dash", "version": 1})
    (filigree_dir / SUMMARY_FILENAME).write_text("# parity\n")
    db = FiligreeDB(filigree_dir / DB_FILENAME, prefix="dash", check_same_thread=False)
    db.initialize()

    original_db = dash_module._db
    original_store = dash_module._project_store
    dash_module._db = db
    dash_module._project_store = None
    app = create_app(server_mode=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    dash_module._db = original_db
    dash_module._project_store = original_store
    db.close()


@pytest.fixture
def mcp_surface(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> FiligreeDB:
    """Isolated MCP surface for parity tests.

    Patches ``mcp_server.db`` and ``mcp_server._filigree_dir`` so the MCP
    tool handlers see a fresh project. Returns the DB so tests can seed
    issues before invoking handlers.
    """
    tmp = tmp_path_factory.mktemp("parity-mcp")
    filigree_dir = tmp / FILIGREE_DIR_NAME
    filigree_dir.mkdir()
    write_config(filigree_dir, {"prefix": "mcp", "version": 1})
    (filigree_dir / SUMMARY_FILENAME).write_text("# parity\n")
    db = FiligreeDB(filigree_dir / DB_FILENAME, prefix="mcp")
    db.initialize()
    monkeypatch.setattr(mcp_module, "db", db)
    monkeypatch.setattr(mcp_module, "_filigree_dir", filigree_dir)
    return db


@pytest.fixture
def cli_surface(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[Callable[..., Any], None, None]:
    """Isolated CLI surface for parity tests.

    Returns an invoker that takes a callable(runner, project_root) → result
    so the test body controls both the pre-seed (via runner.invoke) and
    the error trigger. Each call creates a fresh project.
    """
    tmp = tmp_path_factory.mktemp("parity-cli")
    original_cwd = os.getcwd()
    os.chdir(str(tmp))
    try:
        runner = CliRunner()
        init = runner.invoke(cli, ["init", "--prefix", "cli"])
        assert init.exit_code == 0, init.output
    except Exception:
        os.chdir(original_cwd)
        raise

    def invoker(action: Callable[[CliRunner, Path], Any]) -> Any:
        # Caller provides a function that runs one or more cli invocations
        # in the seeded project and returns the final (error) result.
        return action(runner, tmp)

    yield invoker
    os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# Scenario 1: unknown issue_id on get → NOT_FOUND
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestUnknownIdGetParity:
    async def test_parity(
        self,
        dashboard_surface: AsyncClient,
        mcp_surface: FiligreeDB,
        cli_surface: Callable[..., Any],
    ) -> None:
        missing = "dash-ffffffffff"

        dash_resp = await dashboard_surface.get(f"/api/issue/{missing}")
        dash_env = dash_resp.json()
        _assert_flat_envelope(dash_env, surface="dashboard")
        assert dash_resp.status_code == 404

        mcp_env = _mcp_envelope(await _handle_get_issue({"issue_id": missing}))
        _assert_flat_envelope(mcp_env, surface="mcp")

        def cli_action(runner: CliRunner, _: Path) -> Any:
            return runner.invoke(cli, ["show", missing, "--json"])

        cli_env = _cli_envelope(cli_surface(cli_action))
        _assert_flat_envelope(cli_env, surface="cli")

        # Parity: all three agree on NOT_FOUND
        assert dash_env["code"] == mcp_env["code"] == cli_env["code"] == ErrorCode.NOT_FOUND, (
            f"dashboard={dash_env['code']} mcp={mcp_env['code']} cli={cli_env['code']}"
        )


# ---------------------------------------------------------------------------
# Scenario 2: out-of-range priority on create → VALIDATION
#
# D+M parity holds today. CLI parity is broken because `--priority` uses
# click.IntRange(0, 4) in cli_commands/issues.py:23 — Click intercepts the
# value at parse time and emits its own stderr usage error with exit 2,
# bypassing the 2.0 JSON envelope entirely. Fixing this is Stage 2B scope:
# either replace IntRange with a callback that honours --json, or add a
# consistent "Click error → envelope" shim at the top-level group.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPriorityOutOfRangeCreateParity:
    async def test_dashboard_mcp_parity(
        self,
        dashboard_surface: AsyncClient,
        mcp_surface: FiligreeDB,
    ) -> None:
        dash_resp = await dashboard_surface.post("/api/issues", json={"title": "Bad", "priority": 99})
        dash_env = dash_resp.json()
        _assert_flat_envelope(dash_env, surface="dashboard")
        assert dash_resp.status_code == 400

        mcp_env = _mcp_envelope(await _handle_create_issue({"title": "Bad", "priority": 99}))
        _assert_flat_envelope(mcp_env, surface="mcp")

        assert dash_env["code"] == mcp_env["code"] == ErrorCode.VALIDATION, f"dashboard={dash_env['code']} mcp={mcp_env['code']}"

    async def test_cli_emits_envelope(self, cli_surface: Callable[..., Any]) -> None:
        # Was strict-xfail before Stage 2B task 2b.3a. The --priority option
        # now routes through _validate_priority_range callback
        # (cli_commands/issues.py) which emits the 2.0 envelope when
        # json_flag_in_argv() is true.
        def cli_action(runner: CliRunner, _: Path) -> Any:
            return runner.invoke(cli, ["create", "Bad", "--priority", "99", "--json"])

        cli_env = _cli_envelope(cli_surface(cli_action))
        _assert_flat_envelope(cli_env, surface="cli")
        assert cli_env["code"] == ErrorCode.VALIDATION


# ---------------------------------------------------------------------------
# Scenario 3: unknown issue type on create → VALIDATION
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestUnknownTypeCreateParity:
    async def test_parity(
        self,
        dashboard_surface: AsyncClient,
        mcp_surface: FiligreeDB,
        cli_surface: Callable[..., Any],
    ) -> None:
        bogus = "definitely-not-a-type"

        dash_resp = await dashboard_surface.post("/api/issues", json={"title": "Bad", "type": bogus})
        dash_env = dash_resp.json()
        _assert_flat_envelope(dash_env, surface="dashboard")

        mcp_env = _mcp_envelope(await _handle_create_issue({"title": "Bad", "type": bogus}))
        _assert_flat_envelope(mcp_env, surface="mcp")

        def cli_action(runner: CliRunner, _: Path) -> Any:
            return runner.invoke(cli, ["create", "Bad", "--type", bogus, "--json"])

        cli_env = _cli_envelope(cli_surface(cli_action))
        _assert_flat_envelope(cli_env, surface="cli")

        assert dash_env["code"] == mcp_env["code"] == cli_env["code"] == ErrorCode.VALIDATION, (
            f"dashboard={dash_env['code']} mcp={mcp_env['code']} cli={cli_env['code']}"
        )


# ---------------------------------------------------------------------------
# Scenario 4: blank/whitespace actor on update → VALIDATION
#
# Same pattern as Scenario 2 — CLI's `--actor` is a top-level group option
# with a Click-layer validator that emits stderr usage errors, not the
# envelope. D+M parity holds.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBlankActorUpdateParity:
    async def test_dashboard_mcp_parity(
        self,
        dashboard_surface: AsyncClient,
        mcp_surface: FiligreeDB,
    ) -> None:
        # Each surface needs a valid issue to target first.
        dash_create = await dashboard_surface.post("/api/issues", json={"title": "Target"})
        assert dash_create.status_code == 201
        dash_id = dash_create.json()["id"]
        dash_resp = await dashboard_surface.patch(f"/api/issue/{dash_id}", json={"actor": "   ", "title": "x"})
        dash_env = dash_resp.json()
        _assert_flat_envelope(dash_env, surface="dashboard")
        assert dash_resp.status_code == 400

        mcp_issue = mcp_surface.create_issue("Target")
        mcp_env = _mcp_envelope(await _handle_update_issue({"issue_id": mcp_issue.id, "title": "x", "actor": "   "}))
        _assert_flat_envelope(mcp_env, surface="mcp")

        assert dash_env["code"] == mcp_env["code"] == ErrorCode.VALIDATION, f"dashboard={dash_env['code']} mcp={mcp_env['code']}"

    async def test_cli_emits_envelope(self, cli_surface: Callable[..., Any]) -> None:
        # Was strict-xfail before Stage 2B task 2b.3b. The cli group
        # callback in cli.py now sniffs ``ctx.args`` for --json when
        # sanitize_actor fails and emits the 2.0 envelope instead of
        # raising click.BadParameter.
        def cli_action(runner: CliRunner, _: Path) -> Any:
            create = runner.invoke(cli, ["create", "Target", "--json"])
            assert create.exit_code == 0, create.output
            issue_id = json.loads(create.output)["issue_id"]
            return runner.invoke(cli, ["--actor", "   ", "update", issue_id, "--title", "x", "--json"])

        cli_env = _cli_envelope(cli_surface(cli_action))
        _assert_flat_envelope(cli_env, surface="cli")
        assert cli_env["code"] == ErrorCode.VALIDATION


# ---------------------------------------------------------------------------
# Scenario 5: blank/whitespace assignee on claim → VALIDATION
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBlankAssigneeClaimParity:
    async def test_parity(
        self,
        dashboard_surface: AsyncClient,
        mcp_surface: FiligreeDB,
        cli_surface: Callable[..., Any],
    ) -> None:
        dash_create = await dashboard_surface.post("/api/issues", json={"title": "Claimable"})
        assert dash_create.status_code == 201
        dash_id = dash_create.json()["id"]
        dash_resp = await dashboard_surface.post(f"/api/issue/{dash_id}/claim", json={"assignee": "   "})
        dash_env = dash_resp.json()
        _assert_flat_envelope(dash_env, surface="dashboard")
        assert dash_resp.status_code == 400

        mcp_issue = mcp_surface.create_issue("Claimable")
        mcp_env = _mcp_envelope(await _handle_claim_issue({"issue_id": mcp_issue.id, "assignee": "   "}))
        _assert_flat_envelope(mcp_env, surface="mcp")

        def cli_action(runner: CliRunner, _: Path) -> Any:
            create = runner.invoke(cli, ["create", "Claimable", "--json"])
            assert create.exit_code == 0, create.output
            issue_id = json.loads(create.output)["issue_id"]
            return runner.invoke(cli, ["claim", issue_id, "--assignee", "   ", "--json"])

        cli_env = _cli_envelope(cli_surface(cli_action))
        _assert_flat_envelope(cli_env, surface="cli")

        assert dash_env["code"] == mcp_env["code"] == cli_env["code"] == ErrorCode.VALIDATION, (
            f"dashboard={dash_env['code']} mcp={mcp_env['code']} cli={cli_env['code']}"
        )


# ---------------------------------------------------------------------------
# Scenario 6: invalid status transition on update → INVALID_TRANSITION
#
# tests/test_error_envelope_contract.py::TestTransitionErrorSurfaceContract
# already pins this per-surface. Kept here as a true cross-surface equality
# assertion — if the three surfaces ever drift on TransitionError handling,
# this test catches it in one place.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestInvalidTransitionParity:
    async def test_parity(
        self,
        dashboard_surface: AsyncClient,
        mcp_surface: FiligreeDB,
        cli_surface: Callable[..., Any],
    ) -> None:
        dash_create = await dashboard_surface.post("/api/issues", json={"title": "Probe", "type": "bug"})
        assert dash_create.status_code == 201
        dash_id = dash_create.json()["id"]
        dash_resp = await dashboard_surface.patch(f"/api/issue/{dash_id}", json={"status": "nonexistent_state"})
        dash_env = dash_resp.json()
        _assert_flat_envelope(dash_env, surface="dashboard")

        mcp_issue = mcp_surface.create_issue("Probe", type="bug")
        mcp_env = _mcp_envelope(await _handle_update_issue({"issue_id": mcp_issue.id, "status": "nonexistent_state"}))
        _assert_flat_envelope(mcp_env, surface="mcp")

        def cli_action(runner: CliRunner, _: Path) -> Any:
            create = runner.invoke(cli, ["create", "Probe", "--type", "bug", "--json"])
            assert create.exit_code == 0, create.output
            issue_id = json.loads(create.output)["issue_id"]
            return runner.invoke(cli, ["update", issue_id, "--status", "nonexistent_state", "--json"])

        cli_env = _cli_envelope(cli_surface(cli_action))
        _assert_flat_envelope(cli_env, surface="cli")

        assert dash_env["code"] == mcp_env["code"] == cli_env["code"] == ErrorCode.INVALID_TRANSITION, (
            f"dashboard={dash_env['code']} mcp={mcp_env['code']} cli={cli_env['code']}"
        )


# ---------------------------------------------------------------------------
# Scenario 6b: claim on closed issue -> VALIDATION
#
# claim_issue has two logical ValueError branches below the data layer:
# assignee CAS failure (retryable CONFLICT) and status/category mismatch
# (caller picked an unclaimable issue). Pin the latter across surfaces.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestClosedClaimValidationParity:
    async def test_parity(
        self,
        dashboard_surface: AsyncClient,
        mcp_surface: FiligreeDB,
        cli_surface: Callable[..., Any],
    ) -> None:
        dash_create = await dashboard_surface.post("/api/issues", json={"title": "Closed claim"})
        assert dash_create.status_code == 201
        dash_id = dash_create.json()["id"]
        first = await dashboard_surface.post(f"/api/issue/{dash_id}/close", json={})
        assert first.status_code == 200
        dash_resp = await dashboard_surface.post(f"/api/issue/{dash_id}/claim", json={"assignee": "agent-1"})
        dash_env = dash_resp.json()
        _assert_flat_envelope(dash_env, surface="dashboard")
        assert dash_resp.status_code == 400

        mcp_issue = mcp_surface.create_issue("Closed claim")
        mcp_surface.close_issue(mcp_issue.id)
        mcp_env = _mcp_envelope(await _handle_claim_issue({"issue_id": mcp_issue.id, "assignee": "agent-1"}))
        _assert_flat_envelope(mcp_env, surface="mcp")

        def cli_action(runner: CliRunner, _: Path) -> Any:
            create = runner.invoke(cli, ["create", "Closed claim", "--json"])
            assert create.exit_code == 0, create.output
            issue_id = json.loads(create.output)["issue_id"]
            close = runner.invoke(cli, ["close", issue_id, "--json"])
            assert close.exit_code == 0, close.output
            return runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-1", "--json"])

        cli_env = _cli_envelope(cli_surface(cli_action))
        _assert_flat_envelope(cli_env, surface="cli")

        assert dash_env["code"] == mcp_env["code"] == cli_env["code"] == ErrorCode.VALIDATION, (
            f"dashboard={dash_env['code']} mcp={mcp_env['code']} cli={cli_env['code']}"
        )


# ---------------------------------------------------------------------------
# Scenario 7: already-closed issue on close → CONFLICT or INVALID_TRANSITION
#
# close_issue on an already-closed issue raises ValueError; classify_value_error
# lands on a specific code. D+M parity holds (both emit the flat envelope and
# agree on the code). CLI parity is broken because `filigree close <id> --json`
# always emits a batch-shape wrapper ({closed, unblocked, errors:[{id,error,code}]})
# even when given a single id — the per-item envelope is correct but the top
# level is not the 2.0 flat ErrorResponse. Stage 2B scope to unify: either
# emit flat envelope for N=1 close, or decide CLI close is always batch-shaped
# (in which case the parity test shifts to the per-item envelope).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAlreadyClosedParity:
    async def test_dashboard_mcp_parity(
        self,
        dashboard_surface: AsyncClient,
        mcp_surface: FiligreeDB,
    ) -> None:
        dash_create = await dashboard_surface.post("/api/issues", json={"title": "C"})
        dash_id = dash_create.json()["id"]
        first = await dashboard_surface.post(f"/api/issue/{dash_id}/close", json={})
        assert first.status_code == 200, first.text
        dash_resp = await dashboard_surface.post(f"/api/issue/{dash_id}/close", json={})
        dash_env = dash_resp.json()
        _assert_flat_envelope(dash_env, surface="dashboard")

        mcp_issue = mcp_surface.create_issue("C")
        mcp_surface.close_issue(mcp_issue.id)
        mcp_env = _mcp_envelope(await _handle_close_issue({"issue_id": mcp_issue.id}))
        _assert_flat_envelope(mcp_env, surface="mcp")

        assert dash_env["code"] == mcp_env["code"], f"dashboard={dash_env['code']} mcp={mcp_env['code']}"
        # Whichever code wins, it must be one of the reasonable candidates —
        # catches silent drift to VALIDATION/IO.
        assert dash_env["code"] in {ErrorCode.CONFLICT, ErrorCode.INVALID_TRANSITION}

    async def test_cli_emits_flat_envelope(self, cli_surface: Callable[..., Any]) -> None:
        # Was strict-xfail before Stage 2B task 2b.3c. `filigree close <id>
        # --json` now emits the flat envelope when len(issue_ids)==1 and
        # the close fails; N≥2 close calls keep the batch-shape wrapper.
        def cli_action(runner: CliRunner, _: Path) -> Any:
            create = runner.invoke(cli, ["create", "C", "--json"])
            issue_id = json.loads(create.output)["issue_id"]
            close_once = runner.invoke(cli, ["close", issue_id])
            assert close_once.exit_code == 0, close_once.output
            return runner.invoke(cli, ["close", issue_id, "--json"])

        cli_env = _cli_envelope(cli_surface(cli_action))
        _assert_flat_envelope(cli_env, surface="cli")


# ---------------------------------------------------------------------------
# Scenario 8: batch_update with mixed validity
#
# Two parity claims pulled apart here:
#
#   (a) Per-item envelope parity — both surfaces emit {id, error, code} per
#       failed item, with `code` an ErrorCode member. This is what Stage 2a's
#       bed-down round 2 landed (dc3917e). PASSES.
#
#   (b) Container-key parity — the surfaces disagree on the outer key name:
#       dashboard returns {"updated", "errors"}, MCP returns {"updated",
#       "failed"}. This is exactly the 2B wire-contract unification work
#       (BatchResponse[_T] in types/api.py). STRICT XFAIL.
#
# The per-item parity is the meaningful pre-release gate; the container-key
# xfail marks the 2B task that removes this divergence.
# ---------------------------------------------------------------------------


def _dashboard_batch_failed(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Dashboard's container key is ``errors``; normalise for test bodies."""
    failed = body.get("errors")
    assert isinstance(failed, list), f"dashboard batch missing 'errors' list: {body!r}"
    return failed


def _mcp_batch_failed(body: dict[str, Any]) -> list[dict[str, Any]]:
    """MCP's container key is ``failed``; normalise for test bodies."""
    failed = body.get("failed")
    assert isinstance(failed, list), f"mcp batch missing 'failed' list: {body!r}"
    return failed


@pytest.mark.asyncio
class TestBatchMixedValidityParity:
    async def test_per_item_envelope_parity(
        self,
        dashboard_surface: AsyncClient,
        mcp_surface: FiligreeDB,
    ) -> None:
        """Each failed item is {id, error, code} with a valid ErrorCode."""
        dash_create = await dashboard_surface.post("/api/issues", json={"title": "Real"})
        dash_real = dash_create.json()["id"]
        dash_missing = "dash-ffffffffff"
        dash_resp = await dashboard_surface.post(
            "/api/batch/update",
            json={"issue_ids": [dash_real, dash_missing], "priority": 1},
        )
        assert dash_resp.status_code == 200, dash_resp.text
        dash_body = dash_resp.json()
        # Container-key pin: dashboard uses "errors" pre-2b.1. When 2b.1 lands
        # and unifies both surfaces to "failed", flip this and the mcp pin
        # below together. Without this inline assertion, a silent drift that
        # emits both keys ({"errors": [], "failed": [...]}) would satisfy the
        # helper (which only checks "errors" is a list) and pass.
        assert "errors" in dash_body, f"dashboard batch must keep 'errors' container key pre-2b.1: keys={sorted(dash_body.keys())!r}"
        dash_failed = _dashboard_batch_failed(dash_body)
        assert len(dash_failed) == 1
        dash_item = dash_failed[0]
        assert set(dash_item.keys()) >= {"id", "error", "code"}, f"dashboard per-item missing keys: {dash_item!r}"
        assert dash_item["code"] in _VALID_CODES

        mcp_real = mcp_surface.create_issue("Real").id
        mcp_missing = "mcp-ffffffffff"
        from filigree.mcp_tools.issues import _handle_batch_update

        # MCP batch_update uses "issue_ids" matching the loom HTTP /api/loom/batch/update
        # vocabulary (Phase D1 alignment). This test focuses on envelope shape only.
        mcp_body = _mcp_envelope(await _handle_batch_update({"issue_ids": [mcp_real, mcp_missing], "priority": 1}))
        # Container-key pin: MCP uses "failed" pre-2b.1. Paired with the
        # dashboard pin above — both flip together when 2b.1 unifies.
        assert "failed" in mcp_body, f"mcp batch must keep 'failed' container key: keys={sorted(mcp_body.keys())!r}"
        mcp_failed = _mcp_batch_failed(mcp_body)
        assert len(mcp_failed) == 1, f"MCP batch failed list: {mcp_failed!r}"
        mcp_item = mcp_failed[0]
        assert set(mcp_item.keys()) >= {"id", "error", "code"}, f"mcp per-item missing keys: {mcp_item!r}"
        assert mcp_item["code"] in _VALID_CODES

        # Parity: both surfaces agree per-item that the missing id is NOT_FOUND.
        assert dash_item["code"] == mcp_item["code"] == ErrorCode.NOT_FOUND, f"dashboard={dash_item['code']!r} mcp={mcp_item['code']!r}"

    async def test_classic_container_keys_frozen(
        self,
        dashboard_surface: AsyncClient,
    ) -> None:
        """Classic dashboard ``/api/batch/update`` returns the frozen
        ``{updated, errors}`` envelope per ADR-002 §8 (classic 1.x is
        contract-frozen for the 1.x lifetime). This positive-shape pin
        replaces the divergence-flagging strict-xfail
        ``test_container_key_parity``: instead of asserting parity that
        cannot exist (the two surfaces speak different generations on
        purpose), we pin each surface's wire shape independently. Drift
        in the classic shape — which would constitute a breaking change
        to a frozen contract — fails this test.
        """
        dash_create = await dashboard_surface.post("/api/issues", json={"title": "Real"})
        dash_real = dash_create.json()["id"]
        dash_resp = await dashboard_surface.post(
            "/api/batch/update",
            json={"issue_ids": [dash_real, "dash-ffffffffff"], "priority": 1},
        )
        assert dash_resp.status_code == 200, dash_resp.text
        dash_body = dash_resp.json()
        dash_keys = set(dash_body.keys())

        # Classic frozen shape: {updated, errors}. ADR-002 §8.
        assert "updated" in dash_keys, f"classic dashboard missing 'updated': {dash_keys!r}"
        assert "errors" in dash_keys, f"classic dashboard missing 'errors': {dash_keys!r}"
        # And MUST NOT carry the unified-envelope keys (that's the loom
        # surface's job; leakage here would mean the frozen contract
        # changed).
        assert "succeeded" not in dash_keys, f"classic dashboard leaked loom 'succeeded': {dash_keys!r}"
        assert "failed" not in dash_keys, f"classic dashboard leaked loom 'failed': {dash_keys!r}"

    async def test_loom_container_keys_unified(
        self,
        dashboard_surface: AsyncClient,
        mcp_surface: FiligreeDB,
    ) -> None:
        """Loom-side positive-shape pin: ``/api/loom/batch/update`` and
        MCP ``batch_update`` both expose ``{succeeded, failed}``
        (``BatchResponse[SlimIssueLoom]`` on the dashboard side,
        ``BatchResponse[SlimIssue]`` on the MCP side after Phase D1).
        What matters is that BOTH publish ``succeeded`` and ``failed``
        and that NEITHER publishes the classic-only
        ``updated``/``errors`` keys. Paired with
        ``test_classic_container_keys_frozen`` to codify ADR-002's
        "classic is frozen, loom is the new wire shape" stance.
        """
        dash_create = await dashboard_surface.post("/api/issues", json={"title": "Real"})
        dash_real = dash_create.json()["id"]
        dash_resp = await dashboard_surface.post(
            "/api/loom/batch/update",
            json={"issue_ids": [dash_real, "dash-ffffffffff"], "priority": 1},
        )
        assert dash_resp.status_code == 200, dash_resp.text
        dash_body = dash_resp.json()
        dash_keys = set(dash_body.keys())

        mcp_real = mcp_surface.create_issue("Real").id
        from filigree.mcp_tools.issues import _handle_batch_update

        mcp_body = _mcp_envelope(await _handle_batch_update({"issue_ids": [mcp_real, "mcp-ffffffffff"], "priority": 1}))
        mcp_keys = set(mcp_body.keys())

        # Both surfaces publish the unified container keys.
        assert "succeeded" in dash_keys, f"loom dashboard missing 'succeeded': {dash_keys!r}"
        assert "failed" in dash_keys, f"loom dashboard missing 'failed': {dash_keys!r}"
        assert "succeeded" in mcp_keys, f"mcp missing 'succeeded': {mcp_keys!r}"
        assert "failed" in mcp_keys, f"mcp missing 'failed': {mcp_keys!r}"
        # And neither emits the classic-only keys (which would indicate drift).
        assert "updated" not in dash_keys, f"loom dashboard leaked classic 'updated': {dash_keys!r}"
        assert "errors" not in dash_keys, f"loom dashboard leaked classic 'errors': {dash_keys!r}"
        assert "updated" not in mcp_keys, f"mcp emitted unexpected 'updated': {mcp_keys!r}"
        assert "errors" not in mcp_keys, f"mcp emitted unexpected 'errors': {mcp_keys!r}"


# ---------------------------------------------------------------------------
# Scenario 9: POST /api/v1/scan-results envelope pin (Stage 2B gate).
#
# This route is dashboard-only — there is no Clarion staging environment,
# so the parity-test module doubles as the pre-release contract for the
# Clarion-facing ingest endpoint. Any 2B shape change that breaks these
# invariants must update the tests and the design doc together.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestScanResultsEnvelope:
    async def test_missing_findings_validation(self, dashboard_surface: AsyncClient) -> None:
        resp = await dashboard_surface.post(
            "/api/v1/scan-results",
            json={"scan_source": "parity-test"},
        )
        assert resp.status_code == 400
        payload = resp.json()
        _assert_flat_envelope(payload, surface="scan-results")
        assert payload["code"] == ErrorCode.VALIDATION

    async def test_findings_wrong_type_validation(self, dashboard_surface: AsyncClient) -> None:
        resp = await dashboard_surface.post(
            "/api/v1/scan-results",
            json={"scan_source": "parity-test", "findings": "not-a-list"},
        )
        assert resp.status_code == 400
        payload = resp.json()
        _assert_flat_envelope(payload, surface="scan-results")
        assert payload["code"] == ErrorCode.VALIDATION

    async def test_missing_scan_source_validation(self, dashboard_surface: AsyncClient) -> None:
        resp = await dashboard_surface.post(
            "/api/v1/scan-results",
            json={"findings": []},
        )
        assert resp.status_code == 400
        payload = resp.json()
        _assert_flat_envelope(payload, surface="scan-results")
        assert payload["code"] == ErrorCode.VALIDATION

    async def test_invalid_json_body_validation(self, dashboard_surface: AsyncClient) -> None:
        resp = await dashboard_surface.post(
            "/api/v1/scan-results",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        payload = resp.json()
        _assert_flat_envelope(payload, surface="scan-results")

    async def test_success_shape_empty_findings(self, dashboard_surface: AsyncClient) -> None:
        """Stage 2B task 2b.-1: pin the 200 success shape.

        Error paths are pinned by the four tests above. This pins the
        200 shape — the dict returned by ``db.process_scan_results``
        (``ScanIngestResult`` in src/filigree/types/files.py). A 2B task
        that changes this shape must update the test in the same commit
        and list the breakage in CHANGELOG [Unreleased] ### Changed.
        This is the Clarion-facing gate; there is no staging env.
        """
        resp = await dashboard_surface.post(
            "/api/v1/scan-results",
            json={"scan_source": "parity-success-pin", "findings": []},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body, dict), f"scan-results success body is not a dict: {body!r}"

        # Exact key set from ScanIngestResult (types/files.py:138). If you
        # see this assertion fail, either the test is wrong or the route's
        # return shape changed — in which case Clarion consumers break.
        expected_keys = {
            "files_created",
            "files_updated",
            "findings_created",
            "findings_updated",
            "new_finding_ids",
            "observations_created",
            "observations_failed",
            "warnings",
        }
        assert set(body.keys()) == expected_keys, (
            f"scan-results success-shape drift: missing={expected_keys - set(body.keys())} extra={set(body.keys()) - expected_keys}"
        )

        # Value-type invariants.
        for int_key in (
            "files_created",
            "files_updated",
            "findings_created",
            "findings_updated",
            "observations_created",
            "observations_failed",
        ):
            assert isinstance(body[int_key], int), f"{int_key} is not int: {body[int_key]!r}"
        assert isinstance(body["new_finding_ids"], list), body["new_finding_ids"]
        assert isinstance(body["warnings"], list), body["warnings"]

        # Empty-findings semantics: no files, no findings, no observations.
        assert body["files_created"] == 0
        assert body["files_updated"] == 0
        assert body["findings_created"] == 0
        assert body["findings_updated"] == 0
        assert body["new_finding_ids"] == []
        assert body["observations_created"] == 0
        assert body["observations_failed"] == 0


# ---------------------------------------------------------------------------
# Phase E — CLI↔MCP↔HTTP parity battery
#
# Three scenarios verifying that the new E2 CLI commands and the E4
# start-work command emit the same envelope shapes as the MCP tools and
# (where applicable) the loom HTTP endpoints. Each test asserts:
#   (a) the envelope keys are correct for the surface,
#   (b) the shapes agree across surfaces,
#   (c) error envelopes use a valid ErrorCode.
#
# Only surfaces where the command exists are compared:
#   list-observations: CLI ↔ MCP  (no loom HTTP route)
#   list-files:        CLI ↔ loom HTTP  (no MCP list-files wrapper)
#   start-work:        CLI ↔ MCP  (no HTTP composed-op route)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestListObservationsEnvelopeParity:
    """CLI ``list-observations --json`` and MCP ``list_observations`` agree on
    ``ListResponse[T]`` shape: ``{items, has_more}`` with no legacy siblings."""

    async def test_cli_mcp_envelope_parity(
        self,
        mcp_surface: FiligreeDB,
        cli_surface: Callable[..., Any],
    ) -> None:
        from filigree.mcp_tools.observations import _handle_list_observations

        # MCP surface: empty project → zero items.
        mcp_body = _mcp_envelope(await _handle_list_observations({}))
        assert "items" in mcp_body, f"mcp list-observations missing 'items': {mcp_body!r}"
        assert "has_more" in mcp_body, f"mcp list-observations missing 'has_more': {mcp_body!r}"
        assert isinstance(mcp_body["items"], list)
        assert isinstance(mcp_body["has_more"], bool)
        # No legacy siblings.
        for legacy_key in ("observations", "total", "stats", "errors"):
            assert legacy_key not in mcp_body, f"mcp emitted legacy key '{legacy_key}': {mcp_body!r}"

        # CLI surface: empty project → zero items; exit 0.
        def cli_action(runner: CliRunner, _: Path) -> Any:
            return runner.invoke(cli, ["list-observations", "--json"])

        cli_result = cli_surface(cli_action)
        assert cli_result.exit_code == 0, cli_result.output
        cli_body = json.loads(cli_result.output)
        assert "items" in cli_body, f"cli list-observations missing 'items': {cli_body!r}"
        assert "has_more" in cli_body, f"cli list-observations missing 'has_more': {cli_body!r}"
        assert isinstance(cli_body["items"], list)
        assert isinstance(cli_body["has_more"], bool)
        for legacy_key in ("observations", "total", "stats", "errors"):
            assert legacy_key not in cli_body, f"cli emitted legacy key '{legacy_key}': {cli_body!r}"

        # Shape parity.
        assert set(mcp_body.keys()) == set(cli_body.keys()), (
            f"list-observations key set mismatch: mcp={set(mcp_body.keys())} cli={set(cli_body.keys())}"
        )

    async def test_error_envelope_parity_not_found(
        self,
        mcp_surface: FiligreeDB,
        cli_surface: Callable[..., Any],
    ) -> None:
        """Both surfaces emit a valid error envelope for a missing observation_id."""
        from filigree.mcp_tools.observations import _handle_dismiss_observation

        # MCP: dismiss a non-existent observation (ID format does not require a DB prefix
        # — observation IDs are UUIDs, not project-prefixed).
        mcp_body = _mcp_envelope(await _handle_dismiss_observation({"observation_id": "00000000-0000-0000-0000-000000000000"}))
        _assert_flat_envelope(mcp_body, surface="mcp")
        assert mcp_body["code"] == ErrorCode.NOT_FOUND

        # CLI: dismiss a non-existent observation → NOT_FOUND.
        def cli_action(runner: CliRunner, _: Path) -> Any:
            return runner.invoke(cli, ["dismiss-observation", "00000000-0000-0000-0000-000000000000", "--json"])

        cli_result = cli_surface(cli_action)
        cli_body = _cli_envelope(cli_result)
        _assert_flat_envelope(cli_body, surface="cli")
        assert cli_body["code"] == ErrorCode.NOT_FOUND
        assert mcp_body["code"] == cli_body["code"]


@pytest.mark.asyncio
class TestListFilesEnvelopeParity:
    """CLI ``list-files --json`` and loom HTTP ``GET /api/loom/files`` agree on
    ``ListResponse[T]`` shape: ``{items, has_more}`` with no legacy siblings."""

    async def test_cli_loom_http_envelope_parity(
        self,
        dashboard_surface: AsyncClient,
        cli_surface: Callable[..., Any],
    ) -> None:
        # Loom HTTP: empty project → zero items.
        resp = await dashboard_surface.get("/api/loom/files")
        assert resp.status_code == 200, resp.text
        http_body = resp.json()
        assert "items" in http_body, f"loom-http list-files missing 'items': {http_body!r}"
        assert "has_more" in http_body, f"loom-http list-files missing 'has_more': {http_body!r}"
        assert isinstance(http_body["items"], list)
        assert isinstance(http_body["has_more"], bool)
        for legacy_key in ("results", "total", "limit", "offset", "errors"):
            assert legacy_key not in http_body, f"loom-http emitted legacy key '{legacy_key}': {http_body!r}"

        # CLI surface: empty project → zero items; exit 0.
        def cli_action(runner: CliRunner, _: Path) -> Any:
            return runner.invoke(cli, ["list-files", "--json"])

        cli_result = cli_surface(cli_action)
        assert cli_result.exit_code == 0, cli_result.output
        cli_body = json.loads(cli_result.output)
        assert "items" in cli_body, f"cli list-files missing 'items': {cli_body!r}"
        assert "has_more" in cli_body, f"cli list-files missing 'has_more': {cli_body!r}"
        for legacy_key in ("results", "total", "limit", "offset", "errors"):
            assert legacy_key not in cli_body, f"cli emitted legacy key '{legacy_key}': {cli_body!r}"

        # Shape parity: both have the same top-level keys.
        assert set(http_body.keys()) == set(cli_body.keys()), (
            f"list-files key set mismatch: loom-http={set(http_body.keys())} cli={set(cli_body.keys())}"
        )


@pytest.mark.asyncio
class TestStartWorkEnvelopeParity:
    """CLI ``start-work --json`` and MCP ``start_work`` agree:
    success path emits a full public issue; NOT_FOUND error paths agree on code."""

    async def test_error_envelope_parity(
        self,
        mcp_surface: FiligreeDB,
        cli_surface: Callable[..., Any],
    ) -> None:
        """Both surfaces emit NOT_FOUND for an unknown issue_id."""
        from filigree.mcp_tools.issues import _handle_start_work

        # Use prefix-matching IDs to avoid WrongProjectError (which lands as CONFLICT).
        mcp_missing = "mcp-ffffffffff"
        cli_missing = "cli-ffffffffff"

        # MCP: start-work on missing mcp-prefixed issue → NOT_FOUND.
        mcp_body = _mcp_envelope(await _handle_start_work({"issue_id": mcp_missing, "assignee": "bot"}))
        _assert_flat_envelope(mcp_body, surface="mcp")
        assert mcp_body["code"] == ErrorCode.NOT_FOUND

        # CLI: start-work on missing cli-prefixed issue → NOT_FOUND.
        def cli_action(runner: CliRunner, _: Path) -> Any:
            return runner.invoke(cli, ["start-work", cli_missing, "--assignee", "bot", "--json"])

        cli_result = cli_surface(cli_action)
        cli_body = _cli_envelope(cli_result)
        _assert_flat_envelope(cli_body, surface="cli")
        assert cli_body["code"] == ErrorCode.NOT_FOUND
        assert mcp_body["code"] == cli_body["code"]

    async def test_success_shape_parity(
        self,
        mcp_surface: FiligreeDB,
        cli_surface: Callable[..., Any],
    ) -> None:
        """Both surfaces return a public issue with the same structural keys on success."""
        from filigree.mcp_tools.issues import _handle_start_work

        # MCP: seed an issue and start-work on it.
        mcp_issue = mcp_surface.create_issue("MCP start-work target", type="task")
        mcp_body = _mcp_envelope(await _handle_start_work({"issue_id": mcp_issue.id, "assignee": "bot"}))
        # On success, MCP returns a full public issue (not an error envelope).
        assert "issue_id" in mcp_body, f"mcp start-work success missing 'issue_id': {mcp_body!r}"
        assert "id" not in mcp_body, f"mcp start-work success leaked internal 'id': {mcp_body!r}"
        assert "status" in mcp_body, f"mcp start-work success missing 'status': {mcp_body!r}"
        assert "assignee" in mcp_body, f"mcp start-work success missing 'assignee': {mcp_body!r}"

        # CLI: seed an independent issue and start-work on it.
        def cli_action(runner: CliRunner, _: Path) -> Any:
            create = runner.invoke(cli, ["create", "CLI start-work target", "--type", "task", "--json"])
            assert create.exit_code == 0, create.output
            issue_id = json.loads(create.output)["issue_id"]
            return runner.invoke(cli, ["start-work", issue_id, "--assignee", "bot", "--json"])

        cli_result = cli_surface(cli_action)
        assert cli_result.exit_code == 0, cli_result.output
        cli_body = json.loads(cli_result.output)
        assert "issue_id" in cli_body, f"cli start-work success missing 'issue_id': {cli_body!r}"
        assert "id" not in cli_body, f"cli start-work success leaked internal 'id': {cli_body!r}"
        assert "status" in cli_body, f"cli start-work success missing 'status': {cli_body!r}"
        assert "assignee" in cli_body, f"cli start-work success missing 'assignee': {cli_body!r}"

        # Structural parity: both surfaces agree on the same top-level key set.
        mcp_keys = set(mcp_body.keys())
        cli_keys = set(cli_body.keys())
        assert mcp_keys == cli_keys, f"start-work success key set mismatch: mcp={mcp_keys} cli={cli_keys}"
