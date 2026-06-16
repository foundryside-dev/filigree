"""MCP-layer tests for the Legis closure-gate (B5).

The MCP ``close_issue`` and ``batch_close`` tools must consult the same gate
as the HTTP routes — agents close primarily over MCP, so an ungated MCP path
would be a silent bypass. The Legis client is faked; no live Legis.
"""

from __future__ import annotations

import pytest

from filigree import governance, legis_client
from filigree.core import FiligreeDB
from filigree.legis_client import LegisGateResult, LegisGateStatus
from filigree.mcp_server import call_tool  # type: ignore[attr-defined]
from filigree.types.api import ErrorCode
from tests.mcp._helpers import _parse

pytestmark = pytest.mark.asyncio


def _make_governed(db: FiligreeDB, issue_id: str) -> None:
    db.add_entity_association(issue_id, "sei:gov", content_hash="h", actor="legis", signature="sig", signoff_seq=1)


def _patch_gate(monkeypatch: pytest.MonkeyPatch, result: LegisGateResult) -> list[str]:
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    calls: list[str] = []

    def _fake(issue_id: str) -> LegisGateResult:
        calls.append(issue_id)
        return result

    monkeypatch.setattr(governance, "check_closure_gate", _fake)
    return calls


async def test_mcp_close_governed_blocked(mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    issue = mcp_db.create_issue("Governed", priority=2)
    _make_governed(mcp_db, issue.id)
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.BLOCKED, reason="no verified binding"))
    result = _parse(await call_tool("issue_close", {"issue_id": issue.id, "actor": "agent"}))
    assert result["code"] == ErrorCode.CONFLICT
    assert "no verified binding" in result["error"]


async def test_mcp_close_governed_allowed(mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    issue = mcp_db.create_issue("Governed", priority=2)
    _make_governed(mcp_db, issue.id)
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
    result = _parse(await call_tool("issue_close", {"issue_id": issue.id, "actor": "agent"}))
    assert result.get("code") != ErrorCode.CONFLICT
    assert result["issue_id"] == issue.id


async def test_mcp_close_ungoverned_does_not_call_gate(mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    issue = mcp_db.create_issue("Ungoverned", priority=2)
    calls = _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.BLOCKED))
    result = _parse(await call_tool("issue_close", {"issue_id": issue.id, "actor": "agent"}))
    assert result["issue_id"] == issue.id
    assert calls == []


async def test_mcp_close_integrity_failure(mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    issue = mcp_db.create_issue("Governed", priority=2)
    _make_governed(mcp_db, issue.id)
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.INTEGRITY_FAILURE, reason="tampered"))
    result = _parse(await call_tool("issue_close", {"issue_id": issue.id, "actor": "agent"}))
    assert result["code"] == ErrorCode.INTERNAL


async def test_mcp_batch_close_reports_blocked(mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    gov = mcp_db.create_issue("Governed", priority=2)
    ungov = mcp_db.create_issue("Ungoverned", priority=2)
    _make_governed(mcp_db, gov.id)
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.BLOCKED, reason="blocked"))
    result = _parse(await call_tool("issue_batch_close", {"issue_ids": [gov.id, ungov.id], "actor": "agent"}))
    succeeded_ids = {i["issue_id"] for i in result["succeeded"]}
    failed_ids = {e["id"] for e in result["failed"]}
    assert ungov.id in succeeded_ids
    assert gov.id in failed_ids


async def test_mcp_batch_close_gate_read_error_fails_closed(mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    """A gate-read error (plain ValueError, NOT WrongProjectError) must fail
    CLOSED: the governed issue is reported in `failed` and never closed.
    Regression for the fail-open helper that appended such an issue to ALLOWED."""
    gov = mcp_db.create_issue("Governed", priority=2)
    ungov = mcp_db.create_issue("Ungoverned", priority=2)
    _make_governed(mcp_db, gov.id)
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    real_eval = governance.evaluate_closure_gate

    def _boom(tracker: object, issue_id: str) -> governance.GateDecision:
        if issue_id == gov.id:
            raise ValueError("synthetic gate-read failure")
        return real_eval(tracker, issue_id)

    monkeypatch.setattr(governance, "evaluate_closure_gate", _boom)
    result = _parse(await call_tool("issue_batch_close", {"issue_ids": [gov.id, ungov.id], "actor": "agent"}))
    succeeded_ids = {i["issue_id"] for i in result["succeeded"]}
    failed_ids = {e["id"] for e in result["failed"]}
    assert gov.id not in succeeded_ids
    assert gov.id in failed_ids
    assert next(e for e in result["failed"] if e["id"] == gov.id)["code"] == ErrorCode.VALIDATION
    assert mcp_db.get_issue(gov.id).status != "closed"
    assert ungov.id in succeeded_ids  # batch stays alive


async def test_mcp_batch_update_gate_read_error_fails_closed(mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same fail-closed contract for the batch_update status-change gate."""
    gov = mcp_db.create_issue("Governed", priority=2)
    ungov = mcp_db.create_issue("Ungoverned", priority=2)
    _make_governed(mcp_db, gov.id)
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    real_eval = governance.evaluate_status_change_gate

    def _boom(tracker: object, issue_id: str, requested_status: object) -> governance.GateDecision:
        if issue_id == gov.id:
            raise ValueError("synthetic gate-read failure")
        return real_eval(tracker, issue_id, requested_status)  # type: ignore[arg-type]

    monkeypatch.setattr(governance, "evaluate_status_change_gate", _boom)
    result = _parse(await call_tool("issue_batch_update", {"issue_ids": [gov.id, ungov.id], "status": "closed", "actor": "agent"}))
    succeeded_ids = {i["issue_id"] for i in result["succeeded"]}
    failed_ids = {e["id"] for e in result["failed"]}
    assert gov.id not in succeeded_ids
    assert gov.id in failed_ids
    assert next(e for e in result["failed"] if e["id"] == gov.id)["code"] == ErrorCode.VALIDATION
    assert mcp_db.get_issue(gov.id).status != "closed"
    assert ungov.id in succeeded_ids


async def test_mcp_batch_foreign_prefix_aborts_under_governance_on(mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    """With governance ON, a foreign-prefix id in the batch still triggers the
    envelope-level WrongProjectError abort (VALIDATION), not an unhandled crash."""
    valid = mcp_db.create_issue("Valid", priority=2)
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
    result = _parse(await call_tool("issue_batch_close", {"issue_ids": ["other-1234567890", valid.id], "actor": "agent"}))
    assert result["code"] == ErrorCode.VALIDATION


# --- C1: the gate must also cover update_issue / batch_update -----------------
# A governed issue can be driven into a done-category status via update_issue
# (open→closed is a valid task transition), which historically skipped the
# gate. The update surfaces must consult the same gate as close_issue.


async def test_mcp_update_to_done_governed_blocked(mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    issue = mcp_db.create_issue("Governed", priority=2)
    _make_governed(mcp_db, issue.id)
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.BLOCKED, reason="no verified binding"))
    result = _parse(await call_tool("issue_update", {"issue_id": issue.id, "status": "closed", "actor": "agent"}))
    assert result["code"] == ErrorCode.CONFLICT
    assert "no verified binding" in result["error"]
    # the close was actually refused, not merely reported
    assert mcp_db.get_issue(issue.id).status != "closed"


async def test_mcp_update_to_done_governed_allowed(mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    issue = mcp_db.create_issue("Governed", priority=2)
    _make_governed(mcp_db, issue.id)
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
    result = _parse(await call_tool("issue_update", {"issue_id": issue.id, "status": "closed", "actor": "agent"}))
    assert result.get("code") != ErrorCode.CONFLICT
    assert result["status"] == "closed"


async def test_mcp_update_to_non_done_does_not_call_gate(mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    issue = mcp_db.create_issue("Governed", priority=2)
    _make_governed(mcp_db, issue.id)
    calls = _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.BLOCKED))
    result = _parse(await call_tool("issue_update", {"issue_id": issue.id, "status": "in_progress", "actor": "agent"}))
    assert result["status"] == "in_progress"
    assert calls == []  # a non-closing status change is never gated


async def test_mcp_update_to_done_ungoverned_does_not_call_gate(mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    issue = mcp_db.create_issue("Ungoverned", priority=2)
    calls = _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.BLOCKED))
    result = _parse(await call_tool("issue_update", {"issue_id": issue.id, "status": "closed", "actor": "agent"}))
    assert result["status"] == "closed"
    assert calls == []


async def test_mcp_batch_update_to_done_reports_blocked(mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    gov = mcp_db.create_issue("Governed", priority=2)
    ungov = mcp_db.create_issue("Ungoverned", priority=2)
    _make_governed(mcp_db, gov.id)
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.BLOCKED, reason="blocked"))
    result = _parse(await call_tool("issue_batch_update", {"issue_ids": [gov.id, ungov.id], "status": "closed", "actor": "agent"}))
    succeeded_ids = {i["issue_id"] for i in result["succeeded"]}
    failed_ids = {e["id"] for e in result["failed"]}
    assert ungov.id in succeeded_ids
    assert gov.id in failed_ids
    assert mcp_db.get_issue(gov.id).status != "closed"
