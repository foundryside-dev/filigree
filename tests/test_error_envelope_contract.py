"""Cross-surface contract for the 2.0 flat error envelope (review finding #12).

test_error_format.py pins the shape only for the dashboard surface.
This module parameterises the same structural contract across all three
emit surfaces — dashboard (FastAPI), MCP (in-process tool handler), and
CLI (--json flag) — so a regression on any surface lights up red.

Shape invariants (from ErrorResponse TypedDict, types/api.py):
- Exactly 2 or 3 top-level keys: {error, code, details?}.
- ``error`` is a non-empty string (never a nested dict — old shape).
- ``code`` is one of the 11 ErrorCode member values (uppercase).
- No legacy keys: no ``message``, no ``message_detail``, no lowercase code.
"""

from __future__ import annotations

import importlib.resources
import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

import filigree.dashboard as dash
from filigree.cli import cli
from filigree.core import (
    CONF_FILENAME,
    DB_FILENAME,
    FILIGREE_DIR_NAME,
    SUMMARY_FILENAME,
    FiligreeDB,
    write_config,
)
from filigree.dashboard import create_app
from filigree.mcp_tools.issues import _handle_get_issue, _handle_update_issue
from filigree.registry import EXPECTED_CLARION_API_VERSION
from filigree.types.api import ErrorCode
from tests._fakes.clarion_http import clarion_stub

_VALID_CODES: frozenset[str] = frozenset(e.value for e in ErrorCode)


def _assert_flat_envelope(payload: dict, *, surface: str) -> None:
    """Assert ``payload`` conforms to the 2.0 flat error envelope.

    Shared helper so every surface asserts the same invariants.
    """
    assert isinstance(payload, dict), f"[{surface}] payload is not a dict: {payload!r}"

    # Required keys
    assert "error" in payload, f"[{surface}] missing 'error' key: {payload!r}"
    assert "code" in payload, f"[{surface}] missing 'code' key: {payload!r}"

    # error is a string, not a nested dict (old shape)
    assert isinstance(payload["error"], str), f"[{surface}] 'error' is not a string — old nested shape detected: {payload!r}"
    assert payload["error"], f"[{surface}] 'error' is empty: {payload!r}"

    # code is a valid ErrorCode member value
    code = payload["code"]
    assert isinstance(code, str), f"[{surface}] 'code' is not a string: {code!r}"
    assert code in _VALID_CODES, f"[{surface}] 'code' = {code!r} is not in ErrorCode"

    # No legacy fields at the top level
    assert "message" not in payload, f"[{surface}] legacy 'message' key present: {payload!r}"
    assert "message_detail" not in payload, f"[{surface}] legacy 'message_detail' key present: {payload!r}"

    # Allowed keys: error, code, optional details. Anything else is either
    # a per-surface carve-out (batch results with succeeded/failed) or a
    # violation.
    allowed_top_level = {"error", "code", "details"}
    extra = set(payload.keys()) - allowed_top_level
    # Allow transition-error hints (valid_transitions, hint) for now since
    # TransitionError carries them as top-level optional fields. Anything
    # else is a violation.
    extra -= {"valid_transitions", "hint"}
    assert not extra, f"[{surface}] unexpected top-level keys {extra!r} — extras should be in 'details': {payload!r}"


# ---------------------------------------------------------------------------
# Dashboard surface
# ---------------------------------------------------------------------------


@pytest.fixture
def dashboard_client(filigree_project: Path) -> TestClient:
    filigree_dir = filigree_project / FILIGREE_DIR_NAME
    db = FiligreeDB.from_filigree_dir(filigree_dir, check_same_thread=False)

    original_db = dash._db
    original_store = dash._project_store
    dash._db = db
    dash._project_store = None
    app = create_app(server_mode=False)
    client = TestClient(app, raise_server_exceptions=False)
    yield client
    dash._db = original_db
    dash._project_store = original_store
    db.close()


class TestDashboardSurface:
    def test_404_issue_not_found(self, dashboard_client: TestClient) -> None:
        resp = dashboard_client.get("/api/issue/definitely-does-not-exist")
        assert resp.status_code == 404
        _assert_flat_envelope(resp.json(), surface="dashboard")

    def test_400_invalid_json_body(self, dashboard_client: TestClient) -> None:
        resp = dashboard_client.post(
            "/api/issues",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        _assert_flat_envelope(resp.json(), surface="dashboard")

    def test_400_unknown_type_template(self, dashboard_client: TestClient) -> None:
        resp = dashboard_client.get("/api/type/definitely-not-a-real-type")
        assert resp.status_code == 400
        _assert_flat_envelope(resp.json(), surface="dashboard")


# ---------------------------------------------------------------------------
# MCP surface
# ---------------------------------------------------------------------------


@pytest.fixture
def envelope_mcp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FiligreeDB:
    """Minimal MCP project for envelope testing.

    Inlined rather than depending on tests/mcp/conftest.py's mcp_db fixture
    because this test module lives at tests/ root so the dashboard and CLI
    sub-tests can share the _assert_flat_envelope helper without import
    gymnastics.
    """
    filigree_dir = tmp_path / FILIGREE_DIR_NAME
    filigree_dir.mkdir()
    write_config(filigree_dir, {"prefix": "env", "version": 1})
    (filigree_dir / SUMMARY_FILENAME).write_text("# envelope-contract\n")
    db = FiligreeDB(filigree_dir / DB_FILENAME, prefix="env")
    db.initialize()

    import filigree.mcp_server as mcp_mod

    monkeypatch.setattr(mcp_mod, "db", db)
    monkeypatch.setattr(mcp_mod, "_filigree_dir", filigree_dir)
    return db


class TestMCPSurface:
    async def test_get_issue_not_found(self, envelope_mcp_db: FiligreeDB) -> None:
        """MCP NOT_FOUND emits the same envelope as dashboard + CLI."""
        import json as json_mod

        result = await _handle_get_issue({"issue_id": "env-ffffffffff"})
        # _text returns list[TextContent]; .text is the JSON-serialised payload.
        payload = json_mod.loads(result[0].text)
        _assert_flat_envelope(payload, surface="mcp")
        assert payload["code"] == ErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


class TestCLISurface:
    def test_show_missing_id_json(self, initialized_project: Path) -> None:
        """`filigree show <missing-id> --json` emits the 2.0 envelope."""
        import os

        original = os.getcwd()
        os.chdir(initialized_project)
        try:
            runner = CliRunner()
            result = runner.invoke(cli, ["show", "test-ffffffffff", "--json"])
            assert result.exit_code != 0
            payload = json.loads(result.output)
            _assert_flat_envelope(payload, surface="cli")
            assert payload["code"] == ErrorCode.NOT_FOUND
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TransitionError — regression-pin PR #1 (a762547) across every surface.
# Pre-fix, _build_transition_error emitted lowercase "invalid_transition"
# while the 2.0 contract requires the uppercase ErrorCode member. The three
# tests below catch a repeat regression regardless of which surface it hits.
# ---------------------------------------------------------------------------


class TestTransitionErrorSurfaceContract:
    def test_dashboard_invalid_transition_is_uppercase(self, dashboard_client: TestClient) -> None:
        """Dashboard PATCH /api/issue/{id} returns INVALID_TRANSITION uppercase."""
        # Seed an issue via the CLI-created DB path
        import filigree.dashboard as dash

        assert dash._db is not None
        issue = dash._db.create_issue("Transition contract probe", type="bug")

        resp = dashboard_client.patch(
            f"/api/issue/{issue.id}",
            json={"status": "nonexistent_state"},
        )
        assert resp.status_code == 409
        payload = resp.json()
        _assert_flat_envelope(payload, surface="dashboard")
        assert payload["code"] == ErrorCode.INVALID_TRANSITION

    async def test_mcp_invalid_transition_is_uppercase(self, envelope_mcp_db: FiligreeDB) -> None:
        """MCP update_issue emits INVALID_TRANSITION uppercase + valid_transitions hint."""
        import json as json_mod

        issue = envelope_mcp_db.create_issue("MCP transition probe", type="bug")
        result = await _handle_update_issue({"issue_id": issue.id, "status": "nonexistent_state"})
        payload = json_mod.loads(result[0].text)
        _assert_flat_envelope(payload, surface="mcp")
        assert payload["code"] == ErrorCode.INVALID_TRANSITION
        assert "valid_transitions" in payload

    def test_cli_invalid_transition_is_uppercase(self, initialized_project: Path) -> None:
        """`filigree update <id> --status=invalid --json` emits INVALID_TRANSITION uppercase."""
        import os

        original = os.getcwd()
        os.chdir(initialized_project)
        try:
            runner = CliRunner()
            # Seed an issue first
            create_result = runner.invoke(cli, ["create", "CLI transition probe", "--type", "bug", "--json"])
            assert create_result.exit_code == 0, create_result.output
            issue_id = json.loads(create_result.output)["issue_id"]

            # Attempt the invalid transition
            result = runner.invoke(cli, ["update", issue_id, "--status", "nonexistent_state", "--json"])
            assert result.exit_code != 0
            payload = json.loads(result.output)
            _assert_flat_envelope(payload, surface="cli")
            assert payload["code"] == ErrorCode.INVALID_TRANSITION
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# CLI startup-failure envelope (filigree-3741fc571b).
# ``cli_common.get_db()`` runs before any subcommand can render its own JSON
# body, so it must honour ``--json`` itself for project-discovery, schema-
# mismatch, and config/DB-open failures — otherwise every JSON-capable
# command leaks plain-text errors when invoked outside an initialised
# project or against a forward-mismatched DB.
# ---------------------------------------------------------------------------


class TestCLIStartupEnvelope:
    def test_not_initialized_emits_envelope(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`stats --json` outside any filigree project emits NOT_INITIALIZED."""
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["stats", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        _assert_flat_envelope(payload, surface="cli")
        assert payload["code"] == ErrorCode.NOT_INITIALIZED

    def test_schema_mismatch_emits_envelope(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`stats --json` against a v+1 DB emits SCHEMA_MISMATCH."""
        import sqlite3

        from filigree.db_schema import CURRENT_SCHEMA_VERSION

        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        write_config(filigree_dir, {"prefix": "proj", "version": 1})

        db_path = filigree_dir / DB_FILENAME
        d = FiligreeDB(db_path, prefix="proj")
        d.initialize()
        d.close()

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
            conn.commit()
        finally:
            conn.close()

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["stats", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        _assert_flat_envelope(payload, surface="cli")
        assert payload["code"] == ErrorCode.SCHEMA_MISMATCH

    def test_clarion_api_version_mismatch_emits_envelope(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`stats --json` maps Clarion API mismatch to the public registry code."""
        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        with clarion_stub(api_version=EXPECTED_CLARION_API_VERSION + 1) as (base_url, _state):
            write_config(
                filigree_dir,
                {
                    "prefix": "proj",
                    "version": 1,
                    "registry_backend": "clarion",
                    "clarion": {"base_url": base_url, "timeout_seconds": 1},
                },
            )

            monkeypatch.chdir(tmp_path)
            runner = CliRunner()
            result = runner.invoke(cli, ["stats", "--json"])

        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        _assert_flat_envelope(payload, surface="cli")
        assert payload["code"] == ErrorCode.CLARION_REGISTRY_VERSION_MISMATCH
        assert payload["details"] == {
            "cause": "clarion_registry_version_mismatch",
            "url": f"{base_url}/api/v1/_capabilities",
            "expected": EXPECTED_CLARION_API_VERSION,
            "advertised": EXPECTED_CLARION_API_VERSION + 1,
        }

    def test_corrupt_conf_emits_envelope(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`stats --json` against a malformed `.filigree.conf` emits VALIDATION."""
        # `.filigree.conf` exists (so discovery anchors here) but is invalid JSON.
        # `read_conf` raises ValueError → routed to VALIDATION envelope.
        (tmp_path / CONF_FILENAME).write_text("{not valid json")

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["stats", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        _assert_flat_envelope(payload, surface="cli")
        assert payload["code"] == ErrorCode.VALIDATION

    def test_human_readable_path_unchanged(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without --json, startup errors stay plain-text (no JSON regression)."""
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["stats"])
        assert result.exit_code == 1
        # Plain text — must not be a JSON envelope (no leading '{').
        assert not result.output.strip().startswith("{"), result.output
        assert "filigree" in result.output

    def test_double_dash_positional_does_not_trigger_envelope(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`--json` after Click's `--` terminator is positional, not the flag.

        Regression for filigree-df988a37fc: get_db's --json detection must
        ignore tokens after the option terminator, otherwise non-JSON
        invocations whose title happens to be the literal string `--json`
        receive a JSON envelope on startup failure.
        """
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        # `create -- --json` makes the issue title literally "--json" — there
        # is no real --json flag in the invocation.
        result = runner.invoke(cli, ["create", "--", "--json"])
        assert result.exit_code == 1
        assert not result.output.strip().startswith("{"), result.output

    def test_double_dash_as_option_value_still_detects_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When `--` is the value of a value-taking option, a trailing `--json` is the real flag.

        Regression for filigree-e2cbfb247b: the previous fix sliced raw_args
        at the first literal `--`, but Click can legally consume `--` as the
        value of a value-taking option (e.g. `--description --`), so the
        subsequent `--json` is the real JSON-mode flag and the startup
        failure must surface as the JSON envelope, not plain text.
        """
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        # `create T --description -- --json`: Click parses description="--",
        # as_json=True. get_db fails (no project). Expected: JSON envelope.
        result = runner.invoke(cli, ["create", "T", "--description", "--", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        _assert_flat_envelope(payload, surface="cli")
        assert payload["code"] == ErrorCode.NOT_INITIALIZED


# ---------------------------------------------------------------------------
# Group-level --actor validation envelope. Distinct from the startup-failure
# class above: this path fires from the *group* callback before any subcommand
# has parsed its own args, so `_wants_json()` cannot rely on `as_json` being
# present in any ctx.params yet and must fall back to parsing the raw argv.
# ---------------------------------------------------------------------------


class TestCLIActorValidationEnvelope:
    def test_double_dash_as_option_value_with_actor_validation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Group-level --actor failure must detect a real --json after a `--` consumed as an option value.

        Twin of ``test_double_dash_as_option_value_still_detects_json`` for
        the *group-callback* path: when ``--actor`` validation fails, Click
        has not yet parsed the subcommand's options, so ``_wants_json()``
        must reparse the raw invocation in a parser-aware way to know
        ``--description --`` consumed the `--` as a value and that the
        trailing ``--json`` is the real JSON-mode flag — otherwise the
        validation error leaks Click's plain-text usage output.
        """
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        # `--actor "   "` fails sanitize_actor in the group callback before
        # the `create` subcommand parses its options. Expected: JSON envelope.
        result = runner.invoke(cli, ["--actor", "   ", "create", "T", "--description", "--", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        _assert_flat_envelope(payload, surface="cli")
        assert payload["code"] == ErrorCode.VALIDATION
        assert "actor" in payload["error"].lower()

    def test_double_dash_positional_with_actor_validation_no_envelope(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Group-level --actor failure must NOT emit envelope when `--json` is positional after a real `--` terminator."""
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        # `create -- --json` makes the title literally "--json"; no real --json flag.
        result = runner.invoke(cli, ["--actor", "   ", "create", "--", "--json"])
        assert result.exit_code == 2, result.output
        assert not result.output.strip().startswith("{"), result.output
        assert "actor" in result.output.lower()

    def test_simple_actor_validation_with_real_json_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sanity check: the simple ``--actor '' create --json`` case still emits the envelope."""
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["--actor", "   ", "create", "T", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        _assert_flat_envelope(payload, surface="cli")
        assert payload["code"] == ErrorCode.VALIDATION


# Bundled agent-instruction surfaces must enumerate every current ErrorCode, so
# agents following installed/bundled guidance never branch on a stale, partial
# enum (filigree-adbdda2ee5). Loaded the same way the package ships them.
_INSTRUCTION_DOCS: list[str] = [
    "data/instructions.md",
    "skills/filigree-workflow/SKILL.md",
]


@pytest.mark.parametrize("doc", _INSTRUCTION_DOCS)
def test_bundled_instructions_enumerate_every_error_code(doc: str) -> None:
    text = (importlib.resources.files("filigree") / doc).read_text(encoding="utf-8")
    missing = sorted(code for code in _VALID_CODES if f"`{code}`" not in text)
    assert not missing, f"{doc} omits current ErrorCode value(s): {missing}"
