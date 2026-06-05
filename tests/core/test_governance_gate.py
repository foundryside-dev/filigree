"""Tests for the transport-neutral closure-gate policy (B5, DECISION 1/2).

``evaluate_closure_gate`` decides whether a close may proceed:

- governance OFF (LEGIS_URL unset) → PROCEED, no DB read, no network.
- governed = the issue has >=1 entity-association with a non-null
  ``signature`` (DECISION 1A). Only governed issues consult Legis.
- governed + Legis disabled/unreachable → UNAVAILABLE (fail closed,
  DECISION 2). Integrity failure → INTEGRITY_FAILURE. 200 → PROCEED.
"""

from __future__ import annotations

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
    return [{"clarion_entity_id": "sei:a", "signature": "deadbeef", "signoff_seq": 1}]


def _ungoverned_rows() -> list[dict[str, object]]:
    return [{"clarion_entity_id": "sei:a", "signature": None, "signoff_seq": None}]


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
