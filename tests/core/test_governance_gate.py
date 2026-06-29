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


# --- RED-1: current-code-vs-attach drift (Filigree owns the comparison) --------
# The snapshot-STALE check above only catches a re-attach that advanced
# content_hash_at_attach past the signed snapshot. It cannot catch the bound CODE
# drifting while nobody re-attaches: then content_hash_at_attach stays frozen at
# (and equal to) signed_content_hash, _signed_row_is_stale is False, and the
# close was waved through. The gate now resolves each governed binding's CURRENT
# content_hash via the Loomweave registry consumer and fails closed as STALE on a
# mismatch. The resolution is enrich-only: a Loomweave outage degrades to a
# discriminated freshness UNKNOWN and never hard-blocks the close.


class _FakeRegistry:
    """Mirrors ``registry.resolve_entity_content_hashes``: returns the current
    content_hash for ids it knows, lists the rest as ``unresolved`` (the orphan /
    not_found / invalid degrade), or raises ``RegistryUnavailableError`` to
    simulate a whole-backend Loomweave outage."""

    def __init__(self, hashes: dict[str, str], *, raise_unavailable: bool = False) -> None:
        self._hashes = hashes
        self._raise_unavailable = raise_unavailable
        self.calls: list[list[str]] = []

    def resolve_entity_content_hashes(self, entity_ids: list[str]) -> dict[str, object]:
        self.calls.append(list(entity_ids))
        if self._raise_unavailable:
            from filigree.registry import RegistryUnavailableError

            raise RegistryUnavailableError("loomweave down", url="http://legis.test", cause_kind="network")
        resolved = {eid: self._hashes[eid] for eid in entity_ids if eid in self._hashes}
        unresolved = [eid for eid in entity_ids if eid not in self._hashes]
        return {"resolved": resolved, "unresolved": unresolved}


class _FakeDBWithRegistry(_FakeDB):
    """``_FakeDB`` plus a ``.registry`` exposing the entity-hash resolver."""

    def __init__(self, rows: list[dict[str, object]], registry: object) -> None:
        super().__init__(rows)
        self.registry = registry


def _governed_rows_attached_at(entity_id: str, attach_hash: str) -> list[dict[str, object]]:
    # Sign-off snapshot is FRESH (signed == attach): the v27 snapshot check does
    # NOT fire, so any STALE verdict here is the new current-code drift check.
    return [
        {
            "loomweave_entity_id": entity_id,
            "signature": "deadbeef",
            "signoff_seq": 1,
            "content_hash_at_attach": attach_hash,
            "signed_content_hash": attach_hash,
        }
    ]


def test_current_code_drift_fails_closed_as_stale_without_legis(monkeypatch: pytest.MonkeyPatch) -> None:
    """(a) Current code moved on (h1 at attach, registry reports h2) -> STALE,
    no Legis call. Uses an SEI-form entity id to prove SEI bindings ARE checked
    (not silently degraded)."""
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    entity_id = "loomweave:eid:00000000000000000000000000000001"
    registry = _FakeRegistry({entity_id: "h2"})
    db = _FakeDBWithRegistry(_governed_rows_attached_at(entity_id, "h1"), registry)
    spy: list[str] = []
    monkeypatch.setattr(governance, "check_closure_gate", lambda iid: spy.append(iid))
    decision = governance.evaluate_closure_gate(db, "test-1")
    assert decision.outcome is GateOutcome.STALE
    assert "drifted since attach" in decision.reason
    assert registry.calls == [[entity_id]]  # current hash was resolved
    assert spy == []  # fail closed locally, no Legis consultation


def test_current_code_match_proceeds_to_legis(monkeypatch: pytest.MonkeyPatch) -> None:
    """(b) Current content still matches the attach snapshot -> no drift block;
    the close proceeds through the normal Legis gate (ALLOWED -> PROCEED)."""
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
    entity_id = "py:func:mod::f"
    registry = _FakeRegistry({entity_id: "h1"})
    db = _FakeDBWithRegistry(_governed_rows_attached_at(entity_id, "h1"), registry)
    decision = governance.evaluate_closure_gate(db, "test-1")
    assert decision.outcome is GateOutcome.PROCEED
    assert registry.calls == [[entity_id]]


def test_loomweave_unavailable_degrades_to_unknown_not_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """(c) Loomweave unreachable -> discriminated UNKNOWN, the drift check does
    NOT hard-block: the close still proceeds through the Legis gate (enrich-only,
    core close not load-bearing on Loomweave)."""
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
    entity_id = "py:func:mod::f"
    registry = _FakeRegistry({entity_id: "h2"}, raise_unavailable=True)
    db = _FakeDBWithRegistry(_governed_rows_attached_at(entity_id, "h1"), registry)
    decision = governance.evaluate_closure_gate(db, "test-1")
    assert decision.outcome is GateOutcome.PROCEED  # NOT STALE, NOT blocked
    assert registry.calls == [[entity_id]]  # drift resolution was attempted


def test_entity_unresolved_degrades_to_unknown_not_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loomweave reachable but the entity is orphaned/not_found (absent from
    ``resolved``) -> UNKNOWN, not a block: proceeds to the Legis gate. Distinct
    from a drift (which we DO know about) and from an outage."""
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
    entity_id = "py:func:mod::gone"
    registry = _FakeRegistry({})  # entity not in resolved -> unresolved
    db = _FakeDBWithRegistry(_governed_rows_attached_at(entity_id, "h1"), registry)
    decision = governance.evaluate_closure_gate(db, "test-1")
    assert decision.outcome is GateOutcome.PROCEED
    assert registry.calls == [[entity_id]]


def test_ungoverned_close_never_resolves_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """(d) Ungoverned close is unchanged: no signature -> PROCEED before any
    drift resolution; the registry is never consulted."""
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    registry = _FakeRegistry({"py:func:mod::f": "h2"})
    db = _FakeDBWithRegistry(_ungoverned_rows(), registry)
    spy: list[str] = []
    monkeypatch.setattr(governance, "check_closure_gate", lambda iid: spy.append(iid))
    decision = governance.evaluate_closure_gate(db, "test-1")
    assert decision.outcome is GateOutcome.PROCEED
    assert registry.calls == []  # ungoverned short-circuit precedes drift resolution
    assert spy == []


def test_drift_wins_over_unknown_when_mixed(monkeypatch: pytest.MonkeyPatch) -> None:
    """One binding drifted + one unresolvable -> STALE: a known drift on any
    governed binding fails the close closed regardless of an UNKNOWN sibling."""
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    rows = _governed_rows_attached_at("py:func:mod::f", "h1") + _governed_rows_attached_at("py:func:mod::g", "h1")
    registry = _FakeRegistry({"py:func:mod::f": "h2"})  # f drifted, g unresolved
    db = _FakeDBWithRegistry(rows, registry)
    spy: list[str] = []
    monkeypatch.setattr(governance, "check_closure_gate", lambda iid: spy.append(iid))
    decision = governance.evaluate_closure_gate(db, "test-1")
    assert decision.outcome is GateOutcome.STALE
    assert spy == []


def test_no_registry_attribute_degrades_to_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A db with no ``.registry`` (local mode / bare fake) cannot resolve drift ->
    UNKNOWN, proceeds to Legis. Pins that the new check is a no-op for the
    registry-less _FakeDB the rest of this module relies on."""
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
    db = _FakeDB(_governed_rows_attached_at("py:func:mod::f", "h1"))
    decision = governance.evaluate_closure_gate(db, "test-1")
    assert decision.outcome is GateOutcome.PROCEED
