"""Tests for the transport-neutral closure-gate policy (B5, DECISION 1/2).

``evaluate_closure_gate`` decides whether a close may proceed:

- governance OFF (LEGIS_URL unset) → PROCEED, no DB read, no network.
- governed = the issue has >=1 entity-association with a non-null
  ``signature`` (DECISION 1A). Only governed issues consult Legis.
- governed + Legis disabled/unreachable → UNAVAILABLE (fail closed,
  DECISION 2). Integrity failure → INTEGRITY_FAILURE. 200 → PROCEED.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from filigree import governance, legis_client
from filigree.governance import GateOutcome
from filigree.legis_client import LegisGateResult, LegisGateStatus


class _FakeDB:
    """Minimal stand-in exposing only what the gate reads."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.calls: list[str] = []

    def list_entity_associations(self, issue_id: object) -> list[dict[str, object]]:
        self.calls.append(str(issue_id))
        return self._rows


def _governed_rows() -> list[dict[str, object]]:
    return [{"loomweave_entity_id": "sei:a", "signature": "deadbeef", "signoff_seq": 1}]


def _ungoverned_rows() -> list[dict[str, object]]:
    return [{"loomweave_entity_id": "sei:a", "signature": None, "signoff_seq": None}]


def test_governance_off_proceeds_without_reading_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(legis_client.LEGIS_URL_ENV, raising=False)
    db = _FakeDB(_governed_rows())
    spy: list[str] = []
    monkeypatch.setattr(governance, "check_closure_gate", lambda iid: spy.append(iid))
    decision = governance.evaluate_closure_gate(db, "test-1")
    assert decision.outcome is GateOutcome.PROCEED
    assert db.calls == []  # no DB read when governance is off
    assert spy == []  # no network call


def test_ungoverned_proceeds_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    db = _FakeDB(_ungoverned_rows())
    spy: list[str] = []
    monkeypatch.setattr(governance, "check_closure_gate", lambda iid: spy.append(iid))
    decision = governance.evaluate_closure_gate(db, "test-1")
    assert decision.outcome is GateOutcome.PROCEED
    assert db.calls == ["test-1"]  # governed-ness was checked
    assert spy == []  # but no network call


def _patch_gate(monkeypatch: pytest.MonkeyPatch, result: LegisGateResult) -> None:
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    monkeypatch.setattr(governance, "check_closure_gate", lambda iid: result)


def test_governed_allowed_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
    decision = governance.evaluate_closure_gate(_FakeDB(_governed_rows()), "test-1")
    assert decision.outcome is GateOutcome.PROCEED


def test_governed_blocked_blocks_with_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.BLOCKED, reason="no verified binding"))
    decision = governance.evaluate_closure_gate(_FakeDB(_governed_rows()), "test-1")
    assert decision.outcome is GateOutcome.BLOCKED
    assert "no verified binding" in decision.reason


def test_governed_not_enabled_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.NOT_ENABLED))
    decision = governance.evaluate_closure_gate(_FakeDB(_governed_rows()), "test-1")
    assert decision.outcome is GateOutcome.UNAVAILABLE


def test_governed_unreachable_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.UNREACHABLE))
    decision = governance.evaluate_closure_gate(_FakeDB(_governed_rows()), "test-1")
    assert decision.outcome is GateOutcome.UNAVAILABLE


def test_governed_integrity_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.INTEGRITY_FAILURE, reason="tampered"))
    decision = governance.evaluate_closure_gate(_FakeDB(_governed_rows()), "test-1")
    assert decision.outcome is GateOutcome.INTEGRITY_FAILURE


# --- C1: evaluate_status_change_gate ------------------------------------
# ``update_issue``/``batch_update`` reach the same data-layer close as
# ``close_issue`` (close_issue delegates to update_issue), so a status write
# that lands in a done-category state must consult the same gate. The gate
# makes no network call unless the write is a *real* close of a governed
# issue: a closing transition (target done, current not done) on a governed
# issue with governance configured.


class _StatusFakeDB(_FakeDB):
    """``_FakeDB`` plus the issue/template reads the status gate needs."""

    _CATEGORIES: ClassVar[dict[str, str]] = {"open": "open", "in_progress": "wip", "closed": "done"}

    def __init__(self, rows: list[dict[str, object]], *, status: str = "open") -> None:
        super().__init__(rows)
        self._status = status
        self.issue_reads = 0

    def get_issue(self, issue_id: object) -> object:
        self.issue_reads += 1
        status = self._status

        class _Issue:
            id = str(issue_id)
            type = "task"

        _Issue.status = status  # type: ignore[attr-defined]
        return _Issue()

    def _resolve_status_category(self, issue_type: str, status: str) -> str:
        return self._CATEGORIES[status]


def test_status_change_none_proceeds_without_any_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    db = _StatusFakeDB(_governed_rows())
    spy: list[str] = []
    monkeypatch.setattr(governance, "check_closure_gate", lambda iid: spy.append(iid))
    decision = governance.evaluate_status_change_gate(db, "test-1", None)
    assert decision.outcome is GateOutcome.PROCEED
    assert db.issue_reads == 0  # not a status write → no read
    assert spy == []


def test_status_change_governance_off_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(legis_client.LEGIS_URL_ENV, raising=False)
    db = _StatusFakeDB(_governed_rows())
    decision = governance.evaluate_status_change_gate(db, "test-1", "closed")
    assert decision.outcome is GateOutcome.PROCEED
    assert db.issue_reads == 0  # governance off → no read, no network


def test_status_change_to_non_done_proceeds_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    db = _StatusFakeDB(_governed_rows())
    spy: list[str] = []
    monkeypatch.setattr(governance, "check_closure_gate", lambda iid: spy.append(iid))
    decision = governance.evaluate_status_change_gate(db, "test-1", "in_progress")
    assert decision.outcome is GateOutcome.PROCEED
    assert spy == []  # target is not done → no gate consultation


def test_status_change_already_done_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    db = _StatusFakeDB(_governed_rows(), status="closed")
    spy: list[str] = []
    monkeypatch.setattr(governance, "check_closure_gate", lambda iid: spy.append(iid))
    decision = governance.evaluate_status_change_gate(db, "test-1", "closed")
    assert decision.outcome is GateOutcome.PROCEED
    assert spy == []  # done→done shuffle is not a close → no gate


def test_ungoverned_close_via_status_change_proceeds_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    db = _StatusFakeDB(_ungoverned_rows())
    spy: list[str] = []
    monkeypatch.setattr(governance, "check_closure_gate", lambda iid: spy.append(iid))
    decision = governance.evaluate_status_change_gate(db, "test-1", "closed")
    assert decision.outcome is GateOutcome.PROCEED
    assert spy == []  # governed-ness checked, but no network for ungoverned


def test_governed_close_via_status_change_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.BLOCKED, reason="no verified binding"))
    decision = governance.evaluate_status_change_gate(_StatusFakeDB(_governed_rows()), "test-1", "closed")
    assert decision.outcome is GateOutcome.BLOCKED
    assert "no verified binding" in decision.reason


def test_governed_close_via_status_change_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
    decision = governance.evaluate_status_change_gate(_StatusFakeDB(_governed_rows()), "test-1", "closed")
    assert decision.outcome is GateOutcome.PROCEED


def test_governed_close_via_status_change_unavailable_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.UNREACHABLE))
    decision = governance.evaluate_status_change_gate(_StatusFakeDB(_governed_rows()), "test-1", "closed")
    assert decision.outcome is GateOutcome.UNAVAILABLE


def test_status_change_unknown_status_proceeds_for_validator(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unresolvable target status is not gated — update_issue's transition
    validator rejects it with INVALID_TRANSITION; the gate must not mask that."""
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.BLOCKED))
    db = _StatusFakeDB(_governed_rows())
    decision = governance.evaluate_status_change_gate(db, "test-1", "bogus-status")
    assert decision.outcome is GateOutcome.PROCEED
