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
    # Fresh governed binding: the signed snapshot still matches the current content.
    return [
        {
            "loomweave_entity_id": "sei:a",
            "signature": "deadbeef",
            "signoff_seq": 1,
            "content_hash_at_attach": "h1",
            "signed_content_hash": "h1",
        }
    ]


def _ungoverned_rows() -> list[dict[str, object]]:
    return [
        {
            "loomweave_entity_id": "sei:a",
            "signature": None,
            "signoff_seq": None,
            "content_hash_at_attach": "h1",
            "signed_content_hash": None,
        }
    ]


def _stale_governed_rows() -> list[dict[str, object]]:
    # Drifted sign-off: signed over h1, but the content has since advanced to h2.
    return [
        {
            "loomweave_entity_id": "sei:a",
            "signature": "deadbeef",
            "signoff_seq": 1,
            "content_hash_at_attach": "h2",
            "signed_content_hash": "h1",
        }
    ]


def _legacy_governed_rows() -> list[dict[str, object]]:
    # Pre-v27 / backfill-absent governed row: no recorded snapshot -> read as fresh.
    return [
        {
            "loomweave_entity_id": "sei:a",
            "signature": "deadbeef",
            "signoff_seq": 1,
            "content_hash_at_attach": "h1",
            "signed_content_hash": None,
        }
    ]


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


def test_governed_invalid_response_is_contract_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A contract-violating 2xx (Legis answered, but the body broke the wire
    contract) maps to CONTRACT_VIOLATION, not UNAVAILABLE: it fails closed for
    this issue but — unlike UNAVAILABLE — never trips the batch short-circuit."""
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.INVALID_RESPONSE, reason="2xx no allowed=true"))
    decision = governance.evaluate_closure_gate(_FakeDB(_governed_rows()), "test-1")
    assert decision.outcome is GateOutcome.CONTRACT_VIOLATION
    assert not decision.allowed
    assert "2xx no allowed=true" in decision.reason


# --- v27 drift: a governed sign-off whose bound content has moved on ----------
# The Legis signature is an HMAC over the content snapshot recorded in
# signed_content_hash. When it no longer matches content_hash_at_attach the
# sign-off has drifted; the gate fails closed as STALE with NO network call
# (the issue-id-only gate call cannot convey the drift to Legis).


def test_governed_stale_fails_closed_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    db = _FakeDB(_stale_governed_rows())
    spy: list[str] = []
    monkeypatch.setattr(governance, "check_closure_gate", lambda iid: spy.append(iid))
    decision = governance.evaluate_closure_gate(db, "test-1")
    assert decision.outcome is GateOutcome.STALE
    assert db.calls == ["test-1"]  # governed-ness + freshness were read
    assert spy == []  # but Legis was NOT consulted — fail closed locally


def test_governed_legacy_null_snapshot_reads_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """A governed row with no recorded snapshot (pre-v27 / backfill-absent) is
    treated as fresh and consults Legis — the compatibility shim."""
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
    decision = governance.evaluate_closure_gate(_FakeDB(_legacy_governed_rows()), "test-1")
    assert decision.outcome is GateOutcome.PROCEED


# --- legis_known_down batch short-circuit ordering (I4c) ----------------------
# ``legis_known_down`` suppresses the per-issue Legis round-trip once an earlier
# issue in a batch proved Legis unreachable. It must apply ONLY where a network
# call would otherwise happen — AFTER the governance-off, ungoverned, and STALE
# short-circuits. The STALE-before-known_down ordering is load-bearing: hoisting
# the known_down short-circuit above the stale check would mask tamper (a drifted
# sign-off) as a transient retry, turning a fail-closed STALE into a recoverable
# UNAVAILABLE. These pin that ordering against such a refactor.


def test_governed_stale_with_legis_known_down_still_reports_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    db = _FakeDB(_stale_governed_rows())
    spy: list[str] = []
    monkeypatch.setattr(governance, "check_closure_gate", lambda iid: spy.append(iid))
    # Even with Legis already known down in this batch, a drifted sign-off must
    # fail closed as STALE — NOT be downgraded to a transient UNAVAILABLE.
    decision = governance.evaluate_closure_gate(db, "test-1", legis_known_down=True)
    assert decision.outcome is GateOutcome.STALE
    assert spy == []  # no network call either way


def test_governed_nonstale_with_legis_known_down_is_unavailable_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    # The complement: a fresh governed issue with Legis known down fails closed as
    # UNAVAILABLE and skips the round-trip. Proves known_down is honoured at all,
    # so the STALE test above isn't passing merely because known_down is ignored.
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    db = _FakeDB(_governed_rows())
    spy: list[str] = []
    monkeypatch.setattr(governance, "check_closure_gate", lambda iid: spy.append(iid))
    decision = governance.evaluate_closure_gate(db, "test-1", legis_known_down=True)
    assert decision.outcome is GateOutcome.UNAVAILABLE
    assert spy == []  # round-trip suppressed by the batch-level known-down flag


def test_ungoverned_with_legis_known_down_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    # An ungoverned issue never touches Legis, so the known-down flag must not
    # defer it (gate-level analogue of the batch cascade regression test).
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    db = _FakeDB(_ungoverned_rows())
    spy: list[str] = []
    monkeypatch.setattr(governance, "check_closure_gate", lambda iid: spy.append(iid))
    decision = governance.evaluate_closure_gate(db, "test-1", legis_known_down=True)
    assert decision.outcome is GateOutcome.PROCEED
    assert spy == []


def test_any_stale_signed_association_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An issue with one fresh + one stale signed association fails closed:
    a drifted sign-off on any governed binding compromises the close."""
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    rows = _governed_rows() + _stale_governed_rows()
    spy: list[str] = []
    monkeypatch.setattr(governance, "check_closure_gate", lambda iid: spy.append(iid))
    decision = governance.evaluate_closure_gate(_FakeDB(rows), "test-1")
    assert decision.outcome is GateOutcome.STALE
    assert spy == []


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


# --- v27 drift, end-to-end through the REAL FiligreeDB -------------------------
# The _FakeDB doubles above would pass even if signed_content_hash were never
# plumbed through the real SELECT/serializer (the legacy-NULL shim hides the
# gap). This test drives the actual add_entity_association UPSERT + read path so
# a plumbing regression that silently disables drift detection cannot hide.


def test_real_db_signatureless_reattach_drifts_to_stale(db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from filigree.core import FiligreeDB

    assert isinstance(db, FiligreeDB)
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    spy: list[str] = []

    def _record(issue_id: str) -> LegisGateResult:
        spy.append(issue_id)
        return LegisGateResult(LegisGateStatus.ALLOWED)

    monkeypatch.setattr(governance, "check_closure_gate", _record)

    issue = db.create_issue("Governed then drifted", priority=1)
    # Legis-signed binding at content h1 -> fresh, consults Legis.
    db.add_entity_association(issue.id, "sei:x", content_hash="h1", actor="legis", signature="sig1", signoff_seq=1)
    assert governance.evaluate_closure_gate(db, issue.id).outcome is GateOutcome.PROCEED
    assert spy == [issue.id]

    # Agent drift refresh (no signature) advances content to h2; the preserved
    # sign-off now covers stale content.
    spy.clear()
    db.add_entity_association(issue.id, "sei:x", content_hash="h2", actor="agent")
    decision = governance.evaluate_closure_gate(db, issue.id)
    assert decision.outcome is GateOutcome.STALE
    assert spy == []  # fail closed locally, no Legis call

    # Legis re-signs over the new content -> fresh again, consults Legis.
    db.add_entity_association(issue.id, "sei:x", content_hash="h2", actor="legis", signature="sig2", signoff_seq=2)
    assert governance.evaluate_closure_gate(db, issue.id).outcome is GateOutcome.PROCEED
    assert spy == [issue.id]
