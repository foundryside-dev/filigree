"""HTTP route tests for the Legis closure-gate (B5).

Covers all four HTTP close surfaces — classic single, loom single, classic
batch, loom batch. The Legis client is faked via
``filigree.governance.check_closure_gate``; no live Legis is contacted. An
issue is made *governed* by attaching an entity-association with a non-null
signature (the B1 column).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from filigree import governance, legis_client
from filigree.legis_client import LegisGateResult, LegisGateStatus
from filigree.types.api import ErrorCode
from tests.conftest import PopulatedDB


def _make_governed(dashboard_db: PopulatedDB, issue_id: str) -> None:
    dashboard_db.db.add_entity_association(issue_id, "sei:gov", content_hash="h", actor="legis", signature="sig", signoff_seq=1)


def _patch_gate(monkeypatch: pytest.MonkeyPatch, result: LegisGateResult) -> list[str]:
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    calls: list[str] = []

    def _fake(issue_id: str) -> LegisGateResult:
        calls.append(issue_id)
        return result

    monkeypatch.setattr(governance, "check_closure_gate", _fake)
    return calls


class TestClosureGateSingleClose:
    async def test_governed_blocked_returns_409(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue_id = dashboard_db.ids["a"]
        _make_governed(dashboard_db, issue_id)
        _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.BLOCKED, reason="no verified binding"))
        resp = await client.post(f"/api/issue/{issue_id}/close", json={"actor": "x"})
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["code"] == ErrorCode.CONFLICT
        assert "no verified binding" in body["error"]

    async def test_governed_allowed_closes(self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch) -> None:
        issue_id = dashboard_db.ids["a"]
        _make_governed(dashboard_db, issue_id)
        _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
        resp = await client.post(f"/api/issue/{issue_id}/close", json={"actor": "x"})
        assert resp.status_code == 200, resp.text

    async def test_ungoverned_closes_without_calling_gate(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue_id = dashboard_db.ids["a"]  # no signature attached → ungoverned
        calls = _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.BLOCKED))
        resp = await client.post(f"/api/issue/{issue_id}/close", json={"actor": "x"})
        assert resp.status_code == 200, resp.text
        assert calls == []  # no network call on the ungoverned path

    async def test_governed_not_enabled_fails_closed(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue_id = dashboard_db.ids["a"]
        _make_governed(dashboard_db, issue_id)
        _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.NOT_ENABLED))
        resp = await client.post(f"/api/issue/{issue_id}/close", json={"actor": "x"})
        assert resp.status_code == 409, resp.text

    async def test_governed_integrity_failure_returns_502(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue_id = dashboard_db.ids["a"]
        _make_governed(dashboard_db, issue_id)
        _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.INTEGRITY_FAILURE, reason="tampered"))
        resp = await client.post(f"/api/issue/{issue_id}/close", json={"actor": "x"})
        assert resp.status_code == 502, resp.text
        assert resp.json()["code"] == ErrorCode.INTERNAL

    async def test_loom_single_close_governed_blocked_returns_409(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue_id = dashboard_db.ids["a"]
        _make_governed(dashboard_db, issue_id)
        _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.BLOCKED, reason="blocked"))
        resp = await client.post(f"/api/weft/issues/{issue_id}/close", json={"actor": "x"})
        assert resp.status_code == 409, resp.text


class TestClosureGateBatchClose:
    async def test_classic_batch_reports_blocked_and_closes_rest(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gov = dashboard_db.ids["a"]
        ungov = dashboard_db.ids["b"]
        _make_governed(dashboard_db, gov)
        _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.BLOCKED, reason="blocked"))
        resp = await client.post("/api/batch/close", json={"issue_ids": [gov, ungov], "actor": "x"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        closed_ids = {i["id"] for i in body["closed"]}
        error_ids = {e["id"] for e in body["errors"]}
        assert ungov in closed_ids
        assert gov in error_ids
        assert next(e for e in body["errors"] if e["id"] == gov)["code"] == ErrorCode.CONFLICT

    async def test_loom_batch_reports_blocked_in_failed(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gov = dashboard_db.ids["a"]
        ungov = dashboard_db.ids["b"]
        _make_governed(dashboard_db, gov)
        _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.BLOCKED, reason="blocked"))
        resp = await client.post("/api/weft/batch/close", json={"issue_ids": [gov, ungov], "actor": "x"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        failed_ids = {e["id"] for e in body["failed"]}
        assert gov in failed_ids

    async def test_batch_foreign_prefix_aborts_with_400_under_governance_on(
        self, client: AsyncClient, dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with governance ON (the gate reads associations per-id), a
        foreign-prefix id still triggers the §0.4 envelope-level 400 abort —
        the gate's WrongProjectError flows through to batch_close, not a 500."""
        valid = dashboard_db.ids["a"]
        _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
        resp = await client.post("/api/batch/close", json={"issue_ids": ["other-1234567890", valid], "actor": "x"})
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == ErrorCode.VALIDATION
