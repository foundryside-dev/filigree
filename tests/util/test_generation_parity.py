"""Parity-per-generation tests against published contract fixtures.

Phase B4 of the 2.0 federation work package. Complements
``test_cross_surface_parity.py`` (which pins cross-*surface* agreement
on the envelope) by pinning cross-*generation* agreement against the
fixtures published under ``tests/fixtures/contracts/<generation>/``.

Shape-reference, not byte-equality, per ``docs/federation/contracts.md``:
a response matches a fixture example if status codes agree, the body's
JSON type agrees at every level, non-empty dicts in the fixture have
the exact key set (empty dicts mean "any keys" — a soft check used
for open-ended payloads such as ``IssueDict.fields``), non-empty lists
have at least one element with matching shape, scalars have the
declared Python type, and ``ErrorCode`` values match exactly (enum
closure).

Phase C scope (cumulative): scan-results (C1, with living-surface
alias), batch update/close (C2), single-issue CRUD (C3 — get,
create, patch, close, reopen, claim, release, claim-next, comments,
dependencies). Living-surface aliases land per-endpoint; each
decision is recorded in ``docs/federation/contracts.md``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

import filigree.dashboard as dash_module
from filigree.core import DB_FILENAME, FILIGREE_DIR_NAME, SUMMARY_FILENAME, FiligreeDB, write_config
from filigree.dashboard import create_app
from filigree.types.api import ErrorCode

_VALID_ERROR_CODES: frozenset[str] = frozenset(e.value for e in ErrorCode)

# Fixtures root is relative to the filigree repo root. pytest runs from repo
# root so this is a direct Path; tests/conftest.py does not change cwd.
_FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "contracts"


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


def _load_contract_fixture(generation: str, endpoint_name: str) -> dict[str, Any]:
    path = _FIXTURE_ROOT / generation / f"{endpoint_name}.json"
    assert path.exists(), f"contract fixture not found: {path}"
    return json.loads(path.read_text())  # type: ignore[no-any-return]


def _examples_for(generation: str, endpoint_name: str) -> list[dict[str, Any]]:
    return list(_load_contract_fixture(generation, endpoint_name)["examples"])


# ---------------------------------------------------------------------------
# Shape assertion
# ---------------------------------------------------------------------------


def _assert_shape_matches(actual: Any, expected: Any, path: str = "$") -> None:
    """Assert ``actual`` has the same *shape* as ``expected``.

    Shape rules (see docs/federation/contracts.md):
    - Same JSON type at each level (dict/list/scalar).
    - Dicts: if ``expected`` is empty, ``actual`` only needs to be a
      dict (any key set allowed) — used for open-ended payloads such
      as ``IssueDict.fields`` whose keys are caller-supplied. If
      ``expected`` is non-empty, the key set must match exactly and
      values recurse.
    - Lists: if ``expected`` is empty, ``actual`` may be any list; if
      ``expected`` is non-empty, every element of ``actual`` is checked
      against the first element of ``expected``.
    - Scalars: same Python type (``bool`` is not ``int`` for the type
      check; ``int`` and ``float`` are distinct).
    """
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected dict, got {type(actual).__name__}"
        if expected:
            assert set(actual.keys()) == set(expected.keys()), (
                f"{path}: key-set mismatch; missing={set(expected.keys()) - set(actual.keys())} "
                f"extra={set(actual.keys()) - set(expected.keys())}"
            )
            for key, sub_expected in expected.items():
                _assert_shape_matches(actual[key], sub_expected, f"{path}.{key}")
        # Empty expected dict is a soft check: any dict shape allowed.
        # Mirrors the empty-list rule above.
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: expected list, got {type(actual).__name__}"
        if expected:
            assert actual, f"{path}: expected non-empty list, got empty"
            for index, item in enumerate(actual):
                _assert_shape_matches(item, expected[0], f"{path}[{index}]")
        # Empty expected is a soft check: actual may be any list length.
        # Tests that need exact length pin it in the test body.
    else:
        # Scalars — strict type match (bool vs int distinguished).
        assert type(actual) is type(expected), (
            f"{path}: type mismatch — expected {type(expected).__name__} ({expected!r}), got {type(actual).__name__} ({actual!r})"
        )


def _assert_structural_equivalence(a: Any, b: Any, path: str = "$") -> None:
    """Assert two live responses have the same structural shape.

    Used for living-surface equivalence (same body → two HTTP paths) where
    side effects between the two calls (e.g. scan-results dedup on the
    second invocation) make list lengths legitimately diverge. Equivalence
    here is: same JSON type at each level, same dict key sets, same scalar
    types, every list element type matches if both lists are non-empty. Unlike
    ``_assert_shape_matches``, list emptiness is not checked — both being
    lists is enough.
    """
    if isinstance(a, dict):
        assert isinstance(b, dict), f"{path}: a is dict, b is {type(b).__name__}"
        assert set(a.keys()) == set(b.keys()), (
            f"{path}: key-set mismatch; only-in-a={set(a.keys()) - set(b.keys())} only-in-b={set(b.keys()) - set(a.keys())}"
        )
        for key in a:
            _assert_structural_equivalence(a[key], b[key], f"{path}.{key}")
    elif isinstance(a, list):
        assert isinstance(b, list), f"{path}: a is list, b is {type(b).__name__}"
        if a and b:
            for index, item in enumerate(a):
                _assert_structural_equivalence(item, b[0], f"{path}[{index}]")
            for index, item in enumerate(b):
                _assert_structural_equivalence(a[0], item, f"{path}[{index}]")
    else:
        assert type(a) is type(b), f"{path}: type mismatch — a={type(a).__name__} ({a!r}), b={type(b).__name__} ({b!r})"


def test_shape_match_checks_later_list_items() -> None:
    actual = [{"id": "ok"}, {"id": 123}]
    expected = [{"id": "sample"}]

    with pytest.raises(AssertionError, match=r"\$\[1\]\.id"):
        _assert_shape_matches(actual, expected)


def test_structural_equivalence_checks_later_list_items() -> None:
    actual = [{"id": "ok"}, {"id": 123}]
    expected = [{"id": "sample"}]

    with pytest.raises(AssertionError, match=r"\$\[1\]\.id"):
        _assert_structural_equivalence(actual, expected)


def _assert_error_envelope(
    body: Any,
    *,
    expected_code: str,
    path: str = "$",
    expected_body: dict[str, Any] | None = None,
) -> None:
    """Assert ``body`` is a flat 2.0 error envelope with the expected code.

    Used for the 400/404/409 cases in fixtures — more strict than shape
    matching because error envelope correctness is the federation-era
    contract that consumers branch on.
    """
    assert isinstance(body, dict), f"{path}: expected dict, got {type(body).__name__}"
    assert "error" in body, f"{path}: missing 'error'"
    assert "code" in body, f"{path}: missing 'code'"
    assert isinstance(body["error"], str), f"{path}: 'error' not a string"
    assert body["error"], f"{path}: 'error' is empty"
    assert body["code"] == expected_code, f"{path}: code={body['code']!r} expected {expected_code!r}"
    assert body["code"] in _VALID_ERROR_CODES, f"{path}: code {body['code']!r} not a known ErrorCode member"
    if "details" in body:
        assert isinstance(body["details"], dict), f"{path}: 'details' must be an object when present"
        if body["code"] == ErrorCode.CONFLICT:
            assert set(body["details"]) == {"issue_id", "observed", "expected"}, (
                f"{path}: CONFLICT details must expose issue_id/observed/expected; got {set(body['details'])}"
            )
            assert all(isinstance(body["details"][key], str) for key in ("issue_id", "observed", "expected")), (
                f"{path}: CONFLICT details values must all be strings"
            )
    if expected_body and "details" in expected_body:
        assert "details" in body, f"{path}: fixture expects details but response omitted them"
        _assert_shape_matches(body["details"], expected_body["details"], path=f"{path}.details")


# ---------------------------------------------------------------------------
# Dashboard surface (isolated per-test, same pattern as test_cross_surface_parity)
# ---------------------------------------------------------------------------


@pytest.fixture
async def dashboard_surface(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[AsyncClient]:
    tmp = tmp_path_factory.mktemp("gen-parity")
    filigree_dir = tmp / FILIGREE_DIR_NAME
    filigree_dir.mkdir()
    write_config(filigree_dir, {"prefix": "genp", "version": 1})
    (filigree_dir / SUMMARY_FILENAME).write_text("# generation parity\n")
    db = FiligreeDB(filigree_dir / DB_FILENAME, prefix="genp", check_same_thread=False)
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


# ---------------------------------------------------------------------------
# Classic generation — scan-results
# ---------------------------------------------------------------------------


_CLASSIC_SCAN_RESULTS_EXAMPLES = _examples_for("classic", "scan-results")


@pytest.mark.asyncio
class TestClassicGenerationParityScanResults:
    """Each example in tests/fixtures/contracts/classic/scan-results.json is
    replayed against the live dashboard; status code + body shape must
    match the fixture declaration. See docs/federation/contracts.md for
    the pinning discipline.
    """

    @pytest.mark.parametrize(
        "example",
        _CLASSIC_SCAN_RESULTS_EXAMPLES,
        ids=[e["name"] for e in _CLASSIC_SCAN_RESULTS_EXAMPLES],
    )
    async def test_example_matches_fixture(
        self,
        dashboard_surface: AsyncClient,
        example: dict[str, Any],
    ) -> None:
        req = example["request"]
        expected_resp = example["response"]
        resp = await dashboard_surface.request(req["method"], req["path"], json=req["body"])
        assert resp.status_code == expected_resp["status"], (
            f"{example['name']}: status {resp.status_code} != fixture {expected_resp['status']}; body={resp.text!r}"
        )
        body = resp.json()
        if expected_resp["status"] >= 400:
            _assert_error_envelope(
                body,
                expected_code=expected_resp["body"]["code"],
                path=example["name"],
                expected_body=expected_resp["body"],
            )
        else:
            _assert_shape_matches(body, expected_resp["body"], path=example["name"])


# ---------------------------------------------------------------------------
# Loom generation — scan-results (skip-placeholders until Phase C1)
# ---------------------------------------------------------------------------


_LOOM_SCAN_RESULTS_EXAMPLES = _examples_for("loom", "scan-results")


@pytest.mark.asyncio
class TestLoomGenerationParityScanResults:
    """Loom scan-results parity. Mounted in Phase C1; each example in
    ``tests/fixtures/contracts/loom/scan-results.json`` is replayed
    against the live dashboard.
    """

    @pytest.mark.parametrize(
        "example",
        _LOOM_SCAN_RESULTS_EXAMPLES,
        ids=[e["name"] for e in _LOOM_SCAN_RESULTS_EXAMPLES],
    )
    async def test_example_matches_fixture(
        self,
        dashboard_surface: AsyncClient,
        example: dict[str, Any],
    ) -> None:
        req = example["request"]
        expected_resp = example["response"]
        resp = await dashboard_surface.request(req["method"], req["path"], json=req["body"])
        assert resp.status_code == expected_resp["status"], (
            f"{example['name']}: status {resp.status_code} != fixture {expected_resp['status']}; body={resp.text!r}"
        )
        body = resp.json()
        if expected_resp["status"] >= 400:
            _assert_error_envelope(
                body,
                expected_code=expected_resp["body"]["code"],
                path=example["name"],
                expected_body=expected_resp["body"],
            )
        else:
            _assert_shape_matches(body, expected_resp["body"], path=example["name"])


# ---------------------------------------------------------------------------
# Living-surface equivalence — scan-results
# ---------------------------------------------------------------------------


_LIVING_SURFACE_EQUIV_EXAMPLES = [e for e in _LOOM_SCAN_RESULTS_EXAMPLES if e["response"]["status"] < 400]


@pytest.mark.asyncio
class TestLivingSurfaceEquivalenceScanResults:
    """Pin that ``/api/scan-results`` (living-surface alias) and
    ``/api/loom/scan-results`` return the same response shape for the
    same request body. Phase C1 living-surface decision per the loom
    fixture — see ``docs/federation/contracts.md``.

    Equivalence is *structural* between the two live responses, not
    against the fixture (the fixture is already pinned by
    ``TestLoomGenerationParityScanResults``). The shared-state caveat:
    scan-results is state-mutating, so the second call sees the dedup
    result of the first; ``succeeded`` length and ``stats`` counters
    legitimately differ between the two calls even though the wire shape
    is identical. ``_assert_structural_equivalence`` ignores list lengths
    and scalar values, checking only keys and types at every level —
    which is exactly the contract the alias preserves. Error cases are
    excluded: error envelopes are pinned per-generation by the parity
    tests above.
    """

    @pytest.mark.parametrize(
        "example",
        _LIVING_SURFACE_EQUIV_EXAMPLES,
        ids=[e["name"] for e in _LIVING_SURFACE_EQUIV_EXAMPLES],
    )
    async def test_living_matches_loom(
        self,
        dashboard_surface: AsyncClient,
        example: dict[str, Any],
    ) -> None:
        body = example["request"]["body"]
        loom_resp = await dashboard_surface.post("/api/loom/scan-results", json=body)
        living_resp = await dashboard_surface.post("/api/scan-results", json=body)
        assert loom_resp.status_code == living_resp.status_code, (
            f"{example['name']}: loom={loom_resp.status_code} living={living_resp.status_code}"
        )
        _assert_structural_equivalence(living_resp.json(), loom_resp.json(), path=example["name"])


# ---------------------------------------------------------------------------
# Loom generation — batch/update (Phase C2)
# ---------------------------------------------------------------------------


_LOOM_BATCH_UPDATE_EXAMPLES = _examples_for("loom", "batch-update")


_SLIM_ISSUE_LOOM_KEYS = frozenset({"issue_id", "title", "status", "priority", "type"})


def _assert_slim_issue_loom(item: Any, path: str = "$") -> None:
    """Assert ``item`` is a SlimIssueLoom (5 keys, all the expected types)."""
    assert isinstance(item, dict), f"{path}: expected dict, got {type(item).__name__}"
    assert set(item.keys()) == _SLIM_ISSUE_LOOM_KEYS, (
        f"{path}: SlimIssueLoom key-set mismatch; "
        f"missing={_SLIM_ISSUE_LOOM_KEYS - set(item.keys())} "
        f"extra={set(item.keys()) - _SLIM_ISSUE_LOOM_KEYS}"
    )
    assert isinstance(item["issue_id"], str), f"{path}: issue_id must be str"
    assert isinstance(item["title"], str), f"{path}: title must be str"
    assert isinstance(item["status"], str), f"{path}: status must be str"
    assert isinstance(item["priority"], int), f"{path}: priority must be int"
    assert not isinstance(item["priority"], bool), f"{path}: priority must be int (not bool)"
    assert isinstance(item["type"], str), f"{path}: type must be str"


@pytest.mark.asyncio
class TestLoomGenerationParityBatchUpdate:
    """Loom batch/update parity. Phase C2 mounts
    ``POST /api/loom/batch/update`` returning ``BatchResponse[SlimIssueLoom]``.

    Fixture replay covers error envelopes and the all-missing 200 case
    (which pins ``BatchFailure`` shape on ``failed[0]``). The populated-
    success path needs a real seeded issue id, so the
    ``test_succeeded_populated_shape`` method covers it directly.
    """

    @pytest.mark.parametrize(
        "example",
        _LOOM_BATCH_UPDATE_EXAMPLES,
        ids=[e["name"] for e in _LOOM_BATCH_UPDATE_EXAMPLES],
    )
    async def test_example_matches_fixture(
        self,
        dashboard_surface: AsyncClient,
        example: dict[str, Any],
    ) -> None:
        req = example["request"]
        expected_resp = example["response"]
        resp = await dashboard_surface.request(req["method"], req["path"], json=req["body"])
        assert resp.status_code == expected_resp["status"], (
            f"{example['name']}: status {resp.status_code} != fixture {expected_resp['status']}; body={resp.text!r}"
        )
        body = resp.json()
        if expected_resp["status"] >= 400:
            _assert_error_envelope(
                body,
                expected_code=expected_resp["body"]["code"],
                path=example["name"],
                expected_body=expected_resp["body"],
            )
        else:
            _assert_shape_matches(body, expected_resp["body"], path=example["name"])

    async def test_succeeded_populated_shape(self, dashboard_surface: AsyncClient) -> None:
        """Pin ``succeeded[0]`` as a SlimIssueLoom against a real update.

        Fixture replay can't populate ``succeeded`` (the fixture body uses
        non-existent ids), so this test seeds a real issue and asserts
        the loom slim shape end-to-end.
        """
        create = await dashboard_surface.post("/api/issues", json={"title": "C2 batch update seed"})
        assert create.status_code in (200, 201), create.text
        issue_id = create.json()["id"]
        resp = await dashboard_surface.post(
            "/api/loom/batch/update",
            json={"issue_ids": [issue_id], "priority": 1},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body.keys()) == {"succeeded", "failed"}, body
        assert isinstance(body["failed"], list)
        assert body["failed"] == []
        assert isinstance(body["succeeded"], list)
        assert len(body["succeeded"]) == 1
        _assert_slim_issue_loom(body["succeeded"][0], path="succeeded[0]")
        assert body["succeeded"][0]["issue_id"] == issue_id
        assert body["succeeded"][0]["priority"] == 1

    async def test_response_detail_full_succeeded_shape(self, dashboard_surface: AsyncClient) -> None:
        """``?response_detail=full`` upgrades ``succeeded[0]`` from a slim
        5-key projection to a full ``IssueLoom`` (23 keys). Pins the C5
        shape contract for federation consumers that opt in.
        """
        create = await dashboard_surface.post("/api/issues", json={"title": "C5 batch update full seed"})
        assert create.status_code in (200, 201), create.text
        issue_id = create.json()["id"]
        resp = await dashboard_surface.post(
            "/api/loom/batch/update?response_detail=full",
            json={"issue_ids": [issue_id], "priority": 1},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body.keys()) == {"succeeded", "failed"}, body
        assert body["failed"] == []
        assert len(body["succeeded"]) == 1
        _assert_issue_loom_shape(body["succeeded"][0], path="succeeded[0]")
        assert body["succeeded"][0]["issue_id"] == issue_id
        assert body["succeeded"][0]["priority"] == 1

    async def test_response_detail_default_is_slim(self, dashboard_surface: AsyncClient) -> None:
        """No ``?response_detail`` param → succeeded[] items are
        ``SlimIssueLoom`` (default). Pins the backwards-compat guarantee.
        """
        create = await dashboard_surface.post("/api/issues", json={"title": "C5 default slim"})
        assert create.status_code in (200, 201), create.text
        issue_id = create.json()["id"]
        resp = await dashboard_surface.post(
            "/api/loom/batch/update",
            json={"issue_ids": [issue_id], "priority": 1},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["succeeded"]) == 1
        _assert_slim_issue_loom(body["succeeded"][0], path="succeeded[0]")


# ---------------------------------------------------------------------------
# Loom generation — batch/close (Phase C2)
# ---------------------------------------------------------------------------


_LOOM_BATCH_CLOSE_EXAMPLES = _examples_for("loom", "batch-close")


@pytest.mark.asyncio
class TestLoomGenerationParityBatchClose:
    """Loom batch/close parity. Phase C2 mounts
    ``POST /api/loom/batch/close`` returning ``BatchCloseResponseLoom``
    (BatchResponse[SlimIssueLoom] plus optional ``newly_unblocked``).

    Fixture replay covers errors + all-missing. The seeded test methods
    pin the populated-success shape and the ``newly_unblocked``
    omitted-when-empty rule.
    """

    @pytest.mark.parametrize(
        "example",
        _LOOM_BATCH_CLOSE_EXAMPLES,
        ids=[e["name"] for e in _LOOM_BATCH_CLOSE_EXAMPLES],
    )
    async def test_example_matches_fixture(
        self,
        dashboard_surface: AsyncClient,
        example: dict[str, Any],
    ) -> None:
        req = example["request"]
        expected_resp = example["response"]
        resp = await dashboard_surface.request(req["method"], req["path"], json=req["body"])
        assert resp.status_code == expected_resp["status"], (
            f"{example['name']}: status {resp.status_code} != fixture {expected_resp['status']}; body={resp.text!r}"
        )
        body = resp.json()
        if expected_resp["status"] >= 400:
            _assert_error_envelope(
                body,
                expected_code=expected_resp["body"]["code"],
                path=example["name"],
                expected_body=expected_resp["body"],
            )
        else:
            _assert_shape_matches(body, expected_resp["body"], path=example["name"])

    async def test_succeeded_populated_shape(self, dashboard_surface: AsyncClient) -> None:
        """Close one isolated (no dependents) issue. ``succeeded[0]`` is a
        ``SlimIssueLoom``; ``newly_unblocked`` MUST be omitted because no
        issue was waiting on this one (per the BatchResponse §C2 rule).
        """
        create = await dashboard_surface.post("/api/issues", json={"title": "C2 batch close seed"})
        assert create.status_code in (200, 201), create.text
        issue_id = create.json()["id"]
        resp = await dashboard_surface.post(
            "/api/loom/batch/close",
            json={"issue_ids": [issue_id], "reason": "C2 test"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "newly_unblocked" not in body, f"newly_unblocked must be omitted when empty: {body!r}"
        assert set(body.keys()) == {"succeeded", "failed"}
        assert body["failed"] == []
        assert len(body["succeeded"]) == 1
        _assert_slim_issue_loom(body["succeeded"][0], path="succeeded[0]")
        assert body["succeeded"][0]["issue_id"] == issue_id

    async def test_newly_unblocked_populated_shape(self, dashboard_surface: AsyncClient) -> None:
        """Wire up a dependency (B blocked by A), close A, assert
        ``newly_unblocked`` is present and contains B as a SlimIssueLoom.
        """
        a_resp = await dashboard_surface.post("/api/issues", json={"title": "blocker"})
        b_resp = await dashboard_surface.post("/api/issues", json={"title": "blocked"})
        assert a_resp.status_code in (200, 201), a_resp.text
        assert b_resp.status_code in (200, 201), b_resp.text
        a_id = a_resp.json()["id"]
        b_id = b_resp.json()["id"]
        dep = await dashboard_surface.post(f"/api/issue/{b_id}/dependencies", json={"depends_on": a_id})
        assert dep.status_code in (200, 201), dep.text

        resp = await dashboard_surface.post(
            "/api/loom/batch/close",
            json={"issue_ids": [a_id], "reason": "unblock"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body.keys()) == {"succeeded", "failed", "newly_unblocked"}, body
        assert len(body["succeeded"]) == 1
        assert body["succeeded"][0]["issue_id"] == a_id
        assert isinstance(body["newly_unblocked"], list)
        assert len(body["newly_unblocked"]) == 1
        unblocked = body["newly_unblocked"][0]
        _assert_slim_issue_loom(unblocked, path="newly_unblocked[0]")
        assert unblocked["issue_id"] == b_id

    async def test_force_bypasses_transition_check(self, dashboard_surface: AsyncClient) -> None:
        """``force=true`` on /api/loom/batch/close bypasses the template
        transition validator — matches CLI ``--force`` and MCP ``force``.
        Phase in 'pending' cannot reach 'completed' without it.

        2.1.0 §1.1: HTTP ``force=true`` is gated behind
        ``--allow-http-force-close`` (default-off). The test toggles the
        module flag the CLI option sets so the legacy parity assertion
        still pins behaviour-under-opt-in.
        """
        import filigree.dashboard as dash_module

        create = await dashboard_surface.post(
            "/api/issues",
            json={"title": "C2 batch close force seed", "type": "phase"},
        )
        assert create.status_code in (200, 201), create.text
        issue_id = create.json()["id"]
        # Without force → INVALID_TRANSITION per item.
        resp = await dashboard_surface.post(
            "/api/loom/batch/close",
            json={"issue_ids": [issue_id], "reason": "no-force"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["succeeded"] == []
        assert len(body["failed"]) == 1
        assert body["failed"][0]["code"] == "INVALID_TRANSITION"
        # With force AND --allow-http-force-close opt-in → lands in the
        # type's default done state.
        prev = dash_module._allow_http_force_close
        dash_module._allow_http_force_close = True
        try:
            resp = await dashboard_surface.post(
                "/api/loom/batch/close",
                json={"issue_ids": [issue_id], "reason": "force", "force": True},
            )
        finally:
            dash_module._allow_http_force_close = prev
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["failed"] == []
        assert len(body["succeeded"]) == 1
        assert body["succeeded"][0]["status"] == "completed"

    async def test_response_detail_full_succeeded_shape(self, dashboard_surface: AsyncClient) -> None:
        """``?response_detail=full`` on batch/close upgrades succeeded[0]
        to a full ``IssueLoom``. Single isolated close, no
        newly_unblocked path exercised here.
        """
        create = await dashboard_surface.post("/api/issues", json={"title": "C5 batch close full seed"})
        assert create.status_code in (200, 201), create.text
        issue_id = create.json()["id"]
        resp = await dashboard_surface.post(
            "/api/loom/batch/close?response_detail=full",
            json={"issue_ids": [issue_id], "reason": "C5 full"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "newly_unblocked" not in body, "no dependents → newly_unblocked omitted"
        assert len(body["succeeded"]) == 1
        _assert_issue_loom_shape(body["succeeded"][0], path="succeeded[0]")
        assert body["succeeded"][0]["status"] == "closed"
        assert body["succeeded"][0]["issue_id"] == issue_id

    async def test_newly_unblocked_stays_slim_under_response_detail_full(
        self,
        dashboard_surface: AsyncClient,
    ) -> None:
        """Even with ``?response_detail=full``, ``newly_unblocked[]``
        items stay ``SlimIssueLoom`` per the locked C5 decision (see
        docs/federation/contracts.md). Set up B blocked by A, close A
        with ``?response_detail=full``, and assert succeeded[0] is full
        IssueLoom while newly_unblocked[0] is slim.
        """
        a_resp = await dashboard_surface.post("/api/issues", json={"title": "blocker C5"})
        b_resp = await dashboard_surface.post("/api/issues", json={"title": "blocked C5"})
        assert a_resp.status_code in (200, 201), a_resp.text
        assert b_resp.status_code in (200, 201), b_resp.text
        a_id = a_resp.json()["id"]
        b_id = b_resp.json()["id"]
        dep = await dashboard_surface.post(f"/api/issue/{b_id}/dependencies", json={"depends_on": a_id})
        assert dep.status_code in (200, 201), dep.text

        resp = await dashboard_surface.post(
            "/api/loom/batch/close?response_detail=full",
            json={"issue_ids": [a_id], "reason": "C5 unblock"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body.keys()) == {"succeeded", "failed", "newly_unblocked"}, body
        assert len(body["succeeded"]) == 1
        _assert_issue_loom_shape(body["succeeded"][0], path="succeeded[0]")
        assert body["succeeded"][0]["issue_id"] == a_id

        assert isinstance(body["newly_unblocked"], list)
        assert len(body["newly_unblocked"]) == 1
        unblocked = body["newly_unblocked"][0]
        # Locked C5 rule: newly_unblocked stays slim regardless of response_detail.
        _assert_slim_issue_loom(unblocked, path="newly_unblocked[0]")
        assert unblocked["issue_id"] == b_id


# ---------------------------------------------------------------------------
# Loom generation — single-issue CRUD (Phase C3)
# ---------------------------------------------------------------------------


_LOOM_ISSUE_FIXTURE_SLUGS: list[str] = [
    "issues-get",
    "issues-create",
    "issues-patch",
    "issues-close",
    "issues-reopen",
    "issues-claim",
    "issues-release",
    "issues-claim-next",
    "issues-comments",
    "issues-dep-add",
    "issues-dep-remove",
]


_LOOM_ISSUE_EXAMPLES: list[tuple[str, dict[str, Any]]] = [
    (slug, ex) for slug in _LOOM_ISSUE_FIXTURE_SLUGS for ex in _examples_for("loom", slug)
]


_ISSUE_LOOM_KEYS: frozenset[str] = frozenset(
    {
        "issue_id",
        "title",
        "status",
        "status_category",
        "priority",
        "type",
        "parent_id",
        "assignee",
        "claimed_at",
        "last_heartbeat_at",
        "claim_expires_at",
        "created_at",
        "updated_at",
        "closed_at",
        "description",
        "notes",
        "fields",
        "labels",
        "blocks",
        "blocked_by",
        "is_ready",
        "children",
        "data_warnings",
    }
)


def _assert_issue_loom_shape(body: Any, *, path: str = "$") -> None:
    """Assert ``body`` is an ``IssueLoom`` (23 keys, types match the TypedDict).

    Allows extra keys to support the ``WithFiles`` / ``WithUnblocked`` subtypes
    (the test bodies that hit those paths assert the extra keys themselves).
    """
    assert isinstance(body, dict), f"{path}: expected dict, got {type(body).__name__}"
    missing = _ISSUE_LOOM_KEYS - set(body.keys())
    assert not missing, f"{path}: IssueLoom missing keys {missing}; got {sorted(body.keys())}"
    assert isinstance(body["issue_id"], str), f"{path}: issue_id must be str"
    assert "id" not in body, f"{path}: classic 'id' leaked into loom shape"
    assert isinstance(body["title"], str), f"{path}: title must be str"
    assert isinstance(body["status"], str), f"{path}: status must be str"
    assert isinstance(body["status_category"], str), f"{path}: status_category must be str"
    assert isinstance(body["priority"], int), f"{path}: priority must be int"
    assert not isinstance(body["priority"], bool), f"{path}: priority must be int (not bool)"
    assert isinstance(body["type"], str), f"{path}: type must be str"
    assert body["parent_id"] is None or isinstance(body["parent_id"], str), f"{path}: parent_id must be str|None"
    assert isinstance(body["assignee"], str), f"{path}: assignee must be str"
    assert body["claimed_at"] is None or isinstance(body["claimed_at"], str), f"{path}: claimed_at must be str|None"
    assert body["last_heartbeat_at"] is None or isinstance(body["last_heartbeat_at"], str), f"{path}: last_heartbeat_at must be str|None"
    assert body["claim_expires_at"] is None or isinstance(body["claim_expires_at"], str), f"{path}: claim_expires_at must be str|None"
    assert isinstance(body["fields"], dict), f"{path}: fields must be dict"
    for list_key in ("labels", "blocks", "blocked_by", "children", "data_warnings"):
        assert isinstance(body[list_key], list), f"{path}: {list_key} must be list"
    assert isinstance(body["is_ready"], bool), f"{path}: is_ready must be bool"


@pytest.mark.asyncio
class TestLoomGenerationParityIssues:
    """Phase C3 single-issue CRUD parity. Each fixture in
    ``tests/fixtures/contracts/loom/issues-*.json`` is replayed against
    the live dashboard; status code + body shape (or error envelope)
    must match the fixture declaration.

    Fixture coverage is intentionally narrow — most are 404/400 cases
    that exercise the path/validation surface without seeded state.
    The populated-success ``IssueLoom`` shape and the
    ``newly_unblocked`` cascade are pinned by
    ``test_full_lifecycle_pins_issue_loom_shape`` below, which seeds
    real issues and exercises the entire surface in one test.
    """

    @pytest.mark.parametrize(
        ("slug", "example"),
        _LOOM_ISSUE_EXAMPLES,
        ids=[f"{s}::{e['name']}" for s, e in _LOOM_ISSUE_EXAMPLES],
    )
    async def test_example_matches_fixture(
        self,
        dashboard_surface: AsyncClient,
        slug: str,
        example: dict[str, Any],
    ) -> None:
        req = example["request"]
        expected_resp = example["response"]
        kwargs: dict[str, Any] = {}
        if req.get("body") is not None:
            kwargs["json"] = req["body"]
        resp = await dashboard_surface.request(req["method"], req["path"], **kwargs)
        assert resp.status_code == expected_resp["status"], (
            f"{slug}::{example['name']}: status {resp.status_code} != fixture {expected_resp['status']}; body={resp.text!r}"
        )
        body = resp.json()
        if expected_resp["status"] >= 400:
            _assert_error_envelope(
                body,
                expected_code=expected_resp["body"]["code"],
                path=f"{slug}::{example['name']}",
                expected_body=expected_resp["body"],
            )
        else:
            _assert_shape_matches(body, expected_resp["body"], path=f"{slug}::{example['name']}")

    async def test_full_lifecycle_pins_issue_loom_shape(
        self,
        dashboard_surface: AsyncClient,
    ) -> None:
        """Seed two issues + a dependency, then exercise every loom CRUD
        endpoint that returns an ``IssueLoom`` and assert the shape pin
        on each response. Covers what fixture replay can't seed.
        """
        # Create
        c_resp = await dashboard_surface.post("/api/loom/issues", json={"title": "blocker", "priority": 2})
        assert c_resp.status_code == 201, c_resp.text
        a_body = c_resp.json()
        _assert_issue_loom_shape(a_body, path="create")
        a_id = a_body["issue_id"]

        c2 = await dashboard_surface.post("/api/loom/issues", json={"title": "blocked", "priority": 1})
        assert c2.status_code == 201, c2.text
        b_id = c2.json()["issue_id"]

        # GET (default include_files=False)
        g = await dashboard_surface.get(f"/api/loom/issues/{a_id}")
        assert g.status_code == 200
        _assert_issue_loom_shape(g.json(), path="get")
        assert "files" not in g.json(), "include_files=false must omit files"

        # GET with include_files=true
        gf = await dashboard_surface.get(f"/api/loom/issues/{a_id}?include_files=true")
        assert gf.status_code == 200
        gf_body = gf.json()
        _assert_issue_loom_shape(gf_body, path="get_with_files")
        assert "files" in gf_body
        assert isinstance(gf_body["files"], list)

        # PATCH
        p = await dashboard_surface.patch(f"/api/loom/issues/{a_id}", json={"priority": 0})
        assert p.status_code == 200, p.text
        _assert_issue_loom_shape(p.json(), path="patch")
        assert p.json()["priority"] == 0

        # CLAIM
        cl = await dashboard_surface.post(f"/api/loom/issues/{a_id}/claim", json={"assignee": "tester"})
        assert cl.status_code == 200, cl.text
        _assert_issue_loom_shape(cl.json(), path="claim")
        assert cl.json()["assignee"] == "tester"

        # RELEASE
        rl = await dashboard_surface.post(f"/api/loom/issues/{a_id}/release", json={})
        assert rl.status_code == 200, rl.text
        _assert_issue_loom_shape(rl.json(), path="release")

        # COMMENT (CommentRecordLoom shape)
        cm = await dashboard_surface.post(
            f"/api/loom/issues/{a_id}/comments",
            json={"text": "lifecycle test"},
        )
        assert cm.status_code == 201, cm.text
        cm_body = cm.json()
        assert set(cm_body.keys()) == {"comment_id", "author", "text", "created_at"}, cm_body
        assert isinstance(cm_body["comment_id"], int)
        assert "id" not in cm_body, "classic 'id' leaked into CommentRecordLoom"

        # DEPENDENCY ADD (b depends on a)
        da = await dashboard_surface.post(
            f"/api/loom/issues/{b_id}/dependencies",
            json={"depends_on": a_id},
        )
        assert da.status_code == 200, da.text
        assert da.json() == {"added": True}

        # CLOSE (with newly_unblocked: a's close unblocks b)
        cl2 = await dashboard_surface.post(f"/api/loom/issues/{a_id}/close", json={"reason": "done"})
        assert cl2.status_code == 200, cl2.text
        cl2_body = cl2.json()
        _assert_issue_loom_shape(cl2_body, path="close")
        assert cl2_body["status"] == "closed"
        assert "newly_unblocked" in cl2_body, "newly_unblocked must be present when an issue was unblocked"
        assert isinstance(cl2_body["newly_unblocked"], list)
        assert len(cl2_body["newly_unblocked"]) == 1
        _assert_slim_issue_loom(cl2_body["newly_unblocked"][0], path="close.newly_unblocked[0]")
        assert cl2_body["newly_unblocked"][0]["issue_id"] == b_id

        # REOPEN (no newly_unblocked since reopen is the inverse)
        ro = await dashboard_surface.post(f"/api/loom/issues/{a_id}/reopen", json={})
        assert ro.status_code == 200, ro.text
        _assert_issue_loom_shape(ro.json(), path="reopen")
        assert ro.json()["status"] == "open"

        # CLAIM-NEXT (a is now ready again after reopen+release; expect IssueLoom)
        cn = await dashboard_surface.post("/api/loom/claim-next", json={"assignee": "tester2"})
        assert cn.status_code == 200, cn.text
        _assert_issue_loom_shape(cn.json(), path="claim_next")
        assert cn.json()["assignee"] == "tester2"

        # DEPENDENCY REMOVE (b → a) — both issues exist, edge exists, so
        # removed=true and issue_found=true (the loom-only field).
        dr = await dashboard_surface.delete(f"/api/loom/issues/{b_id}/dependencies/{a_id}")
        assert dr.status_code == 200, dr.text
        assert dr.json() == {"removed": True, "issue_found": True}


# ---------------------------------------------------------------------------
# Loom generation — list endpoints (Phase C4)
# ---------------------------------------------------------------------------


_LOOM_LIST_FIXTURE_SLUGS: list[str] = [
    "blocked",
    "issues",
    "ready",
    "search",
    "files",
    "findings",
    "observations",
    "scanners",
    "packs",
    "types",
    "changes",
    "issue-comments",
    "issue-events",
    "issue-files",
]


_LOOM_LIST_EXAMPLES: list[tuple[str, dict[str, Any]]] = [
    (slug, ex) for slug in _LOOM_LIST_FIXTURE_SLUGS for ex in _examples_for("loom", slug)
]


_LIST_RESPONSE_REQUIRED_KEYS: frozenset[str] = frozenset({"items", "has_more"})


def _assert_list_response_shape(body: Any, *, path: str = "$") -> None:
    """Assert ``body`` is a ``ListResponse[T]`` envelope.

    Pins the unified envelope: ``items`` is a list, ``has_more`` is a
    bool, ``next_offset`` is present iff ``has_more`` is True. Item
    shape is checked separately by the per-endpoint helpers.
    """
    assert isinstance(body, dict), f"{path}: expected dict, got {type(body).__name__}"
    missing = _LIST_RESPONSE_REQUIRED_KEYS - set(body.keys())
    assert not missing, f"{path}: ListResponse missing keys {missing}; got {sorted(body.keys())}"
    assert isinstance(body["items"], list), f"{path}: items must be list"
    assert isinstance(body["has_more"], bool), f"{path}: has_more must be bool"
    if body["has_more"]:
        assert "next_offset" in body, f"{path}: next_offset must be present when has_more is True"
        assert isinstance(body["next_offset"], int), f"{path}: next_offset must be int"
        assert not isinstance(body["next_offset"], bool), f"{path}: next_offset must be int (not bool)"
    else:
        assert "next_offset" not in body, f"{path}: next_offset must be omitted when has_more is False"


_BLOCKED_ISSUE_LOOM_KEYS: frozenset[str] = frozenset({"issue_id", "title", "status", "priority", "type", "blocked_by"})


def _assert_blocked_issue_loom(item: Any, *, path: str = "$") -> None:
    """Assert ``item`` is a BlockedIssueLoom (SlimIssueLoom + blocked_by)."""
    assert isinstance(item, dict), f"{path}: expected dict, got {type(item).__name__}"
    assert set(item.keys()) == _BLOCKED_ISSUE_LOOM_KEYS, (
        f"{path}: BlockedIssueLoom key-set mismatch; "
        f"missing={_BLOCKED_ISSUE_LOOM_KEYS - set(item.keys())} "
        f"extra={set(item.keys()) - _BLOCKED_ISSUE_LOOM_KEYS}"
    )
    assert isinstance(item["issue_id"], str), f"{path}: issue_id must be str"
    assert "id" not in item, f"{path}: classic 'id' leaked into BlockedIssueLoom"
    assert isinstance(item["blocked_by"], list), f"{path}: blocked_by must be list"
    for blocker in item["blocked_by"]:
        assert isinstance(blocker, str), f"{path}: blocked_by entries must be str"


@pytest.mark.asyncio
class TestLoomGenerationParityLists:
    """Phase C4 list-endpoint parity. Each fixture in
    ``tests/fixtures/contracts/loom/<list-endpoint>.json`` is replayed
    against the live dashboard; status code + body shape (or error
    envelope) must match the fixture declaration.

    Fixtures pin the empty/error cases. Populated-success shape pins
    live in seeded methods below — fixture replay can't seed real rows
    that satisfy item-shape assertions.
    """

    @pytest.mark.parametrize(
        ("slug", "example"),
        _LOOM_LIST_EXAMPLES,
        ids=[f"{s}::{e['name']}" for s, e in _LOOM_LIST_EXAMPLES],
    )
    async def test_example_matches_fixture(
        self,
        dashboard_surface: AsyncClient,
        slug: str,
        example: dict[str, Any],
    ) -> None:
        req = example["request"]
        expected_resp = example["response"]
        kwargs: dict[str, Any] = {}
        if req.get("body") is not None:
            kwargs["json"] = req["body"]
        resp = await dashboard_surface.request(req["method"], req["path"], **kwargs)
        assert resp.status_code == expected_resp["status"], (
            f"{slug}::{example['name']}: status {resp.status_code} != fixture {expected_resp['status']}; body={resp.text!r}"
        )
        body = resp.json()
        if expected_resp["status"] >= 400:
            _assert_error_envelope(
                body,
                expected_code=expected_resp["body"]["code"],
                path=f"{slug}::{example['name']}",
                expected_body=expected_resp["body"],
            )
        else:
            _assert_shape_matches(body, expected_resp["body"], path=f"{slug}::{example['name']}")
            _assert_list_response_shape(body, path=f"{slug}::{example['name']}")

    async def test_blocked_populated_shape(self, dashboard_surface: AsyncClient) -> None:
        """Seed A blocking B, then assert ``GET /api/loom/blocked`` returns
        B as a ``BlockedIssueLoom`` with ``blocked_by=[A]``.
        """
        a_resp = await dashboard_surface.post("/api/issues", json={"title": "blocker"})
        b_resp = await dashboard_surface.post("/api/issues", json={"title": "blocked"})
        assert a_resp.status_code in (200, 201), a_resp.text
        assert b_resp.status_code in (200, 201), b_resp.text
        a_id = a_resp.json()["id"]
        b_id = b_resp.json()["id"]
        dep = await dashboard_surface.post(f"/api/issue/{b_id}/dependencies", json={"depends_on": a_id})
        assert dep.status_code in (200, 201), dep.text

        resp = await dashboard_surface.get("/api/loom/blocked")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        _assert_list_response_shape(body, path="blocked")
        # Find B in the items (auto-seeded "Future" release won't be blocked).
        blocked_ids = [item["issue_id"] for item in body["items"]]
        assert b_id in blocked_ids, f"expected {b_id} in blocked items, got {blocked_ids}"
        b_item = next(item for item in body["items"] if item["issue_id"] == b_id)
        _assert_blocked_issue_loom(b_item, path="blocked.items[B]")
        assert b_item["blocked_by"] == [a_id]

    async def test_issues_pagination_and_shape(self, dashboard_surface: AsyncClient) -> None:
        """Seed three issues, then exercise ``GET /api/loom/issues`` with
        a ``limit=1`` page boundary so the overfetch-by-1 has_more
        detection actually triggers. Asserts ``IssueLoom`` shape on the
        item plus ``next_offset`` semantics on subsequent pages.
        """
        for n in range(3):
            r = await dashboard_surface.post("/api/issues", json={"title": f"page-test-{n}"})
            assert r.status_code in (200, 201), r.text

        resp = await dashboard_surface.get("/api/loom/issues?limit=1&offset=0")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        _assert_list_response_shape(body, path="issues.page1")
        assert body["has_more"] is True
        assert body["next_offset"] == 1
        assert len(body["items"]) == 1
        _assert_issue_loom_shape(body["items"][0], path="issues.page1.items[0]")

    async def test_search_drops_total(self, dashboard_surface: AsyncClient) -> None:
        """Pin that the search response wraps in ListResponse and does
        NOT carry the classic 'total' field.
        """
        await dashboard_surface.post("/api/issues", json={"title": "needle in haystack"})
        resp = await dashboard_surface.get("/api/loom/search?q=needle")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        _assert_list_response_shape(body, path="search")
        assert "total" not in body, "loom search must drop classic 'total' field"
        assert "results" not in body, "loom search must use 'items' not 'results'"
        assert len(body["items"]) >= 1
        _assert_issue_loom_shape(body["items"][0], path="search.items[0]")

    async def test_files_findings_observations_populated(self, dashboard_surface: AsyncClient) -> None:
        """One seeded scan-results ingest populates files, findings, and
        observations in a single shot — exercise the three list endpoints
        and pin the loom item shape on each.
        """
        # Ingest a scan with one finding + create_observations=true so we
        # populate three tables in one POST.
        ingest = await dashboard_surface.post(
            "/api/loom/scan-results",
            json={
                "scan_source": "C4-test-scan",
                "create_observations": True,
                "findings": [
                    {
                        "path": "src/example.py",
                        "rule_id": "C4-rule",
                        "message": "C4 seeded finding",
                        "severity": "high",
                        "line_start": 10,
                        "line_end": 12,
                    }
                ],
            },
        )
        assert ingest.status_code == 200, ingest.text

        # Files
        files_resp = await dashboard_surface.get("/api/loom/files")
        assert files_resp.status_code == 200, files_resp.text
        files_body = files_resp.json()
        _assert_list_response_shape(files_body, path="files")
        assert len(files_body["items"]) >= 1
        f0 = files_body["items"][0]
        assert "file_id" in f0, f"FileRecordLoom must use file_id, got keys: {sorted(f0.keys())}"
        assert "id" not in f0, "classic 'id' leaked into FileRecordLoom"
        assert isinstance(f0["file_id"], str)
        assert isinstance(f0["path"], str)
        assert isinstance(f0["summary"], dict)
        assert isinstance(f0["associations_count"], int)
        assert isinstance(f0["observation_count"], int)

        # Findings
        findings_resp = await dashboard_surface.get("/api/loom/findings")
        assert findings_resp.status_code == 200, findings_resp.text
        findings_body = findings_resp.json()
        _assert_list_response_shape(findings_body, path="findings")
        assert len(findings_body["items"]) >= 1
        sf0 = findings_body["items"][0]
        assert "finding_id" in sf0, f"ScanFindingLoom must use finding_id, got keys: {sorted(sf0.keys())}"
        assert "id" not in sf0, "classic 'id' leaked into ScanFindingLoom"
        assert isinstance(sf0["finding_id"], str)
        assert sf0["severity"] == "high"
        assert sf0["rule_id"] == "C4-rule"

        # Observations
        obs_resp = await dashboard_surface.get("/api/loom/observations")
        assert obs_resp.status_code == 200, obs_resp.text
        obs_body = obs_resp.json()
        _assert_list_response_shape(obs_body, path="observations")
        assert len(obs_body["items"]) >= 1
        ob0 = obs_body["items"][0]
        assert "observation_id" in ob0, f"ObservationLoom must use observation_id, got keys: {sorted(ob0.keys())}"
        assert "id" not in ob0, "classic 'id' leaked into ObservationLoom"
        assert isinstance(ob0["observation_id"], str)
        # Pin that loom drops MCP's 'stats' sibling.
        assert "stats" not in obs_body, "loom observations must drop MCP's 'stats' field"

    async def test_per_issue_endpoints_populated(self, dashboard_surface: AsyncClient) -> None:
        """Seed an issue + comment + event, then hit the per-issue
        list endpoints and pin item shapes."""
        c_resp = await dashboard_surface.post("/api/issues", json={"title": "C4 per-issue test"})
        assert c_resp.status_code in (200, 201), c_resp.text
        issue_id = c_resp.json()["id"]
        cm_resp = await dashboard_surface.post(
            f"/api/loom/issues/{issue_id}/comments",
            json={"text": "hello"},
        )
        assert cm_resp.status_code == 201, cm_resp.text

        # Comments — CommentRecordLoom item.
        comments_resp = await dashboard_surface.get(f"/api/loom/issues/{issue_id}/comments")
        assert comments_resp.status_code == 200, comments_resp.text
        cb = comments_resp.json()
        _assert_list_response_shape(cb, path="issue-comments")
        assert len(cb["items"]) == 1
        c0 = cb["items"][0]
        assert set(c0.keys()) == {"comment_id", "author", "text", "created_at"}, c0
        assert isinstance(c0["comment_id"], int)
        assert "id" not in c0, "classic 'id' leaked into CommentRecordLoom"

        # Events — IssueEventLoom item. Issue creation generated at least one event.
        events_resp = await dashboard_surface.get(f"/api/loom/issues/{issue_id}/events")
        assert events_resp.status_code == 200, events_resp.text
        eb = events_resp.json()
        _assert_list_response_shape(eb, path="issue-events")
        assert len(eb["items"]) >= 1
        e0 = eb["items"][0]
        assert "event_id" in e0, f"IssueEventLoom must use event_id, got keys: {sorted(e0.keys())}"
        assert "id" not in e0, "classic 'id' leaked into IssueEventLoom"
        assert isinstance(e0["event_id"], int)
        assert e0["issue_id"] == issue_id

        # Files (empty list since no associations created).
        files_resp = await dashboard_surface.get(f"/api/loom/issues/{issue_id}/files")
        assert files_resp.status_code == 200, files_resp.text
        fb = files_resp.json()
        _assert_list_response_shape(fb, path="issue-files")
        assert fb["items"] == []
