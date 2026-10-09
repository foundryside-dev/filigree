"""HTTP route tests for the Legis closure-gate (B5).

Covers all four HTTP close surfaces — classic single, weft single, classic
batch, weft batch. Legis is retired (M-7): a governed close with fresh bindings
PROCEEDs with a ``governance_provider_archived`` warning and a
``governance_warning`` event, and the network is never touched (``governance_on``
makes any Legis access fail the test). A governed issue whose sign-off has
drifted still fails closed as STALE (409). An issue is made *governed* by
attaching an entity-association with a non-null signature (the B1 column).
"""

from __future__ import annotations

import asyncio
import time

import pytest
from httpx import AsyncClient

from filigree import commit_reachability, governance, legis_client
from filigree.types.api import ErrorCode
from tests._fakes.legis_retired import ARCHIVED_WARNING, governance_on
from tests.conftest import PopulatedDB


def _make_governed(dashboard_db: PopulatedDB, issue_id: str) -> None:
    dashboard_db.db.add_entity_association(issue_id, "sei:gov", content_hash="h", actor="legis", signature="sig", signoff_seq=1)


def _make_stale(dashboard_db: PopulatedDB, issue_id: str) -> None:
    """Drift the sign-off: a signatureless re-attach advances content past the signed snapshot."""
    dashboard_db.db.add_entity_association(issue_id, "sei:gov", content_hash="h-drifted", actor="agent")


def _warning_events(dashboard_db: PopulatedDB, issue_id: str) -> list[str]:
    return [e["new_value"] or "" for e in dashboard_db.db.get_issue_events(issue_id) if e["event_type"] == "governance_warning"]


class TestClosureGateSingleClose:
    async def test_governed_stale_returns_409(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue_id = dashboard_db.ids["a"]
        _make_governed(dashboard_db, issue_id)
        _make_stale(dashboard_db, issue_id)
        governance_on(monkeypatch)
        resp = await client.post(f"/api/issue/{issue_id}/close", json={"actor": "x"})
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["code"] == ErrorCode.CONFLICT
        assert "drifted" in body["error"]

    async def test_governed_closes_with_archived_warning_event(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue_id = dashboard_db.ids["a"]
        _make_governed(dashboard_db, issue_id)
        governance_on(monkeypatch)
        resp = await client.post(f"/api/issue/{issue_id}/close", json={"actor": "x"})
        assert resp.status_code == 200, resp.text
        assert _warning_events(dashboard_db, issue_id) == [ARCHIVED_WARNING]
        # Task 0.6: the archived-provider warning now rides on the response too.
        assert resp.json()["warnings"] == [ARCHIVED_WARNING]

    async def test_weft_single_close_governed_carries_archived_warning(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue_id = dashboard_db.ids["a"]
        _make_governed(dashboard_db, issue_id)
        governance_on(monkeypatch)
        resp = await client.post(f"/api/weft/issues/{issue_id}/close", json={"actor": "x"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["warnings"] == [ARCHIVED_WARNING]

    async def test_ungoverned_close_omits_warnings(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        resp = await client.post(f"/api/issue/{dashboard_db.ids['a']}/close", json={"actor": "x"})
        assert resp.status_code == 200, resp.text
        assert "warnings" not in resp.json()  # omitted when empty

    async def test_ungoverned_closes_without_calling_gate(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue_id = dashboard_db.ids["a"]  # no signature attached → ungoverned
        governance_on(monkeypatch)
        resp = await client.post(f"/api/issue/{issue_id}/close", json={"actor": "x"})
        assert resp.status_code == 200, resp.text
        assert _warning_events(dashboard_db, issue_id) == []  # ungoverned → no warning

    async def test_weft_single_close_governed_stale_returns_409(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue_id = dashboard_db.ids["a"]
        _make_governed(dashboard_db, issue_id)
        _make_stale(dashboard_db, issue_id)
        governance_on(monkeypatch)
        resp = await client.post(f"/api/weft/issues/{issue_id}/close", json={"actor": "x"})
        assert resp.status_code == 409, resp.text


class TestClosureGateBatchClose:
    async def test_classic_batch_reports_stale_and_closes_rest(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gov = dashboard_db.ids["a"]
        ungov = dashboard_db.ids["b"]
        _make_governed(dashboard_db, gov)
        _make_stale(dashboard_db, gov)
        governance_on(monkeypatch)
        resp = await client.post("/api/batch/close", json={"issue_ids": [gov, ungov], "actor": "x"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        closed_ids = {i["id"] for i in body["closed"]}
        error_ids = {e["id"] for e in body["errors"]}
        assert ungov in closed_ids
        assert gov in error_ids
        assert next(e for e in body["errors"] if e["id"] == gov)["code"] == ErrorCode.CONFLICT

    async def test_weft_batch_reports_stale_in_failed(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gov = dashboard_db.ids["a"]
        ungov = dashboard_db.ids["b"]
        _make_governed(dashboard_db, gov)
        _make_stale(dashboard_db, gov)
        governance_on(monkeypatch)
        resp = await client.post("/api/weft/batch/close", json={"issue_ids": [gov, ungov], "actor": "x"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        failed_ids = {e["id"] for e in body["failed"]}
        assert gov in failed_ids

    async def test_batch_gate_read_error_fails_closed_does_not_close_governed(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A gate-read error (a plain ValueError that is NOT WrongProjectError)
        must fail CLOSED: the issue is reported per-item and never routed into
        the close. Regression for the fail-open helper bug where a gate-read
        error appended the issue to the ALLOWED list."""
        gov = dashboard_db.ids["a"]
        ungov = dashboard_db.ids["b"]
        _make_governed(dashboard_db, gov)
        monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
        real_eval = governance.evaluate_closure_gate

        def _boom(db: object, issue_id: str) -> governance.GateDecision:
            if issue_id == gov:
                raise ValueError("synthetic gate-read failure")
            return real_eval(db, issue_id)

        monkeypatch.setattr(governance, "evaluate_closure_gate", _boom)
        resp = await client.post("/api/batch/close", json={"issue_ids": [gov, ungov], "actor": "x"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        closed_ids = {i["id"] for i in body["closed"]}
        error_ids = {e["id"] for e in body["errors"]}
        # fails closed: governed issue NOT closed, reported per-item
        assert gov not in closed_ids
        assert gov in error_ids
        assert next(e for e in body["errors"] if e["id"] == gov)["code"] == ErrorCode.VALIDATION
        assert dashboard_db.db.get_issue(gov).status != "closed"
        # the batch stays alive: the ungoverned issue still closes
        assert ungov in closed_ids

    async def test_batch_foreign_prefix_aborts_with_400_under_governance_on(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with governance ON (the gate reads associations per-id), a
        foreign-prefix id still triggers the §0.4 envelope-level 400 abort —
        the gate's WrongProjectError flows through to batch_close, not a 500."""
        valid = dashboard_db.ids["a"]
        governance_on(monkeypatch)
        resp = await client.post("/api/batch/close", json={"issue_ids": ["other-1234567890", valid], "actor": "x"})
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == ErrorCode.VALIDATION


class TestStatusChangeGate:
    """C1: ``update_issue``/``batch_update`` reach the same data-layer close as
    ``close_issue`` (open→closed is a valid task transition), so the update
    surfaces must consult the same gate. Covers classic + weft, single + batch."""

    async def test_classic_update_to_done_governed_stale_returns_409(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue_id = dashboard_db.ids["a"]
        _make_governed(dashboard_db, issue_id)
        _make_stale(dashboard_db, issue_id)
        governance_on(monkeypatch)
        resp = await client.patch(f"/api/issue/{issue_id}", json={"status": "closed", "actor": "x"})
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == ErrorCode.CONFLICT
        assert dashboard_db.db.get_issue(issue_id).status != "closed"

    async def test_classic_update_to_done_governed_closes_with_warning(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue_id = dashboard_db.ids["a"]
        _make_governed(dashboard_db, issue_id)
        governance_on(monkeypatch)
        resp = await client.patch(f"/api/issue/{issue_id}", json={"status": "closed", "actor": "x"})
        assert resp.status_code == 200, resp.text
        assert dashboard_db.db.get_issue(issue_id).status == "closed"
        assert _warning_events(dashboard_db, issue_id) == [ARCHIVED_WARNING]

    async def test_classic_update_to_non_done_does_not_call_gate(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue_id = dashboard_db.ids["a"]
        _make_governed(dashboard_db, issue_id)
        governance_on(monkeypatch)
        resp = await client.patch(f"/api/issue/{issue_id}", json={"status": "in_progress", "actor": "x"})
        assert resp.status_code == 200, resp.text
        assert _warning_events(dashboard_db, issue_id) == []  # non-closing status change is never gated

    async def test_classic_update_to_done_ungoverned_does_not_call_gate(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue_id = dashboard_db.ids["a"]  # no signature → ungoverned
        governance_on(monkeypatch)
        resp = await client.patch(f"/api/issue/{issue_id}", json={"status": "closed", "actor": "x"})
        assert resp.status_code == 200, resp.text
        assert _warning_events(dashboard_db, issue_id) == []

    async def test_weft_update_to_done_governed_stale_returns_409(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue_id = dashboard_db.ids["a"]
        _make_governed(dashboard_db, issue_id)
        _make_stale(dashboard_db, issue_id)
        governance_on(monkeypatch)
        resp = await client.patch(f"/api/weft/issues/{issue_id}", json={"status": "closed", "actor": "x"})
        assert resp.status_code == 409, resp.text
        assert dashboard_db.db.get_issue(issue_id).status != "closed"

    async def test_classic_batch_update_to_done_reports_stale_in_errors(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gov = dashboard_db.ids["a"]
        ungov = dashboard_db.ids["b"]
        _make_governed(dashboard_db, gov)
        _make_stale(dashboard_db, gov)
        governance_on(monkeypatch)
        resp = await client.post("/api/batch/update", json={"issue_ids": [gov, ungov], "status": "closed", "actor": "x"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        updated_ids = {i["id"] for i in body["updated"]}
        error_ids = {e["id"] for e in body["errors"]}
        assert ungov in updated_ids
        assert gov in error_ids
        assert dashboard_db.db.get_issue(gov).status != "closed"

    async def test_weft_batch_update_to_done_reports_stale_in_failed(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gov = dashboard_db.ids["a"]
        ungov = dashboard_db.ids["b"]
        _make_governed(dashboard_db, gov)
        _make_stale(dashboard_db, gov)
        governance_on(monkeypatch)
        resp = await client.post("/api/weft/batch/update", json={"issue_ids": [gov, ungov], "status": "closed", "actor": "x"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        failed_ids = {e["id"] for e in body["failed"]}
        assert gov in failed_ids
        assert dashboard_db.db.get_issue(gov).status != "closed"


class TestBatchCloseDoesNotWedge:
    """M-7 / HTTP F1: the gate used to make a synchronous 5 s Legis probe per
    governed issue from inside the async handler. Legis is archived: no probe."""

    async def test_batch_close_of_governed_issues_completes_under_one_second(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time

        # An unroutable LEGIS_URL: under the old gate each governed issue cost a
        # network attempt (5 s timeout when blackholed) on the event loop.
        monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://10.255.255.1")
        ids = [dashboard_db.db.create_issue(f"Governed {i}", priority=2).id for i in range(10)]
        for issue_id in ids:
            _make_governed(dashboard_db, issue_id)
        started = time.monotonic()
        resp = await client.post("/api/batch/close", json={"issue_ids": ids, "actor": "x"})
        elapsed = time.monotonic() - started
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert {i["id"] for i in body["closed"]} == set(ids)
        assert body["errors"] == []
        assert elapsed < 1.0, f"batch close of 10 governed issues took {elapsed:.2f}s"


def _stub_unreachable(monkeypatch: pytest.MonkeyPatch) -> str:
    """Make every reachability check report the anchor as unreachable; return the warning text."""
    result = commit_reachability.ReachabilityCheck(reachable=False, sha="abc1234", ref="origin/main")
    monkeypatch.setattr(commit_reachability, "check_commit_reachable", lambda *_a, **_k: result)
    assert result.warning is not None
    return result.warning


class TestCloseWarningsCombined:
    """Task 0.6: the governance warning and the reachability warning share one ``warnings[]``."""

    async def test_classic_close_carries_both_warnings(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue_id = dashboard_db.ids["a"]
        _make_governed(dashboard_db, issue_id)
        governance_on(monkeypatch)
        unreachable = _stub_unreachable(monkeypatch)
        resp = await client.post(f"/api/issue/{issue_id}/close", json={"actor": "x", "commit": "side@abc1234"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["warnings"] == [ARCHIVED_WARNING, unreachable]

    async def test_weft_close_carries_both_warnings(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue_id = dashboard_db.ids["a"]
        _make_governed(dashboard_db, issue_id)
        governance_on(monkeypatch)
        unreachable = _stub_unreachable(monkeypatch)
        resp = await client.post(f"/api/weft/issues/{issue_id}/close", json={"actor": "x", "commit": "side@abc1234"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["warnings"] == [ARCHIVED_WARNING, unreachable]


class TestCloseReachabilityOffLoop:
    """Fix round 1 (#1): the git check never blocks the daemon's event loop."""

    @pytest.mark.parametrize("path", ["/api/issue/{id}/close", "/api/weft/issues/{id}/close"])
    async def test_health_answers_while_close_waits_on_git(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch, path: str
    ) -> None:
        def _slow(*_a: object, **_k: object) -> commit_reachability.ReachabilityCheck:
            time.sleep(0.5)
            return commit_reachability.ReachabilityCheck(reachable=True, sha="abc1234", ref="origin/main")

        monkeypatch.setattr(commit_reachability, "check_commit_reachable", _slow)
        issue_id = dashboard_db.ids["a"]
        order: list[str] = []

        async def _close() -> None:
            resp = await client.post(path.format(id=issue_id), json={"actor": "x", "commit": "main@abc1234"})
            assert resp.status_code == 200, resp.text
            order.append("close")

        async def _health() -> None:
            await asyncio.sleep(0.05)
            resp = await client.get("/api/health")
            assert resp.status_code == 200, resp.text
            order.append("health")

        await asyncio.gather(_close(), _health())
        assert order == ["health", "close"]
