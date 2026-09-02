"""Tests for the transport-neutral closure-gate policy (B5, DECISION 1/2).

``evaluate_closure_gate`` decides whether a close may proceed:

- governance OFF (LEGIS_URL unset) → PROCEED, no DB read, no network.
- governed = the issue has >=1 entity-association with a non-null
  ``signature`` (DECISION 1A). Only governed issues consult Legis.
- governed + Legis disabled/unreachable → UNAVAILABLE (fail closed,
  DECISION 2). Integrity failure → INTEGRITY_FAILURE. 200 → PROCEED.
"""

from __future__ import annotations

import logging
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

    def __init__(
        self,
        hashes: dict[str, str],
        *,
        raise_unavailable: bool = False,
        lineage_hints: dict[str, dict[str, object]] | None = None,
        lineage_unavailable: bool = False,
    ) -> None:
        self._hashes = hashes
        self._raise_unavailable = raise_unavailable
        # ``lineage_unavailable`` is the registry's NotRequired advisory flag: the
        # orphan rename-hint fallback hit a connectivity-class failure.
        self._lineage_unavailable = lineage_unavailable
        # ``lineage_hints`` is a NotRequired key on EntityHashResolution: a
        # legacy producer (None here) omits it entirely rather than sending {}.
        self._lineage_hints = lineage_hints
        self.calls: list[list[str]] = []

    def resolve_entity_content_hashes(self, entity_ids: list[str]) -> dict[str, object]:
        self.calls.append(list(entity_ids))
        if self._raise_unavailable:
            from filigree.registry import RegistryUnavailableError

            raise RegistryUnavailableError("loomweave down", url="http://legis.test", cause_kind="network")
        resolved = {eid: self._hashes[eid] for eid in entity_ids if eid in self._hashes}
        unresolved = [eid for eid in entity_ids if eid not in self._hashes]
        result: dict[str, object] = {"resolved": resolved, "unresolved": unresolved}
        if self._lineage_hints is not None:
            result["lineage_hints"] = {eid: self._lineage_hints[eid] for eid in unresolved if eid in self._lineage_hints}
        if self._lineage_unavailable:
            result["lineage_unavailable"] = True
        return result


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


# --- rename-lineage hint on orphaned bindings (filigree-4e13d133f7) -----------
# An orphaned (alive:false) SEI degrades to UNKNOWN above — a dead end for the
# agent. When the registry can name the SEI's latest lineage event, the gate
# relays it: on the decision (``lineage_hints`` + a reason suffix naming the
# re-bind target on any non-PROCEED verdict) and on the ``entity_unresolved``
# log record. Enrich-only: the outcome never changes because of a hint.

_ORPHAN_SEI = "loomweave:eid:0000000000000000000000000000dead"
_RENAMED_EVENT: dict[str, object] = {
    "event": "locator_changed",
    "old_locator": "py:func:mod::f",
    "new_locator": "py:func:mod::g",
    "run_id": "run-2",
    "recorded_at": "2026-02-01T00:00:00Z",
}


def _unresolved_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "filigree.governance" and getattr(r, "reason", None) == "entity_unresolved"]


def test_orphaned_sei_hint_surfaces_on_decision_and_log_without_blocking(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
    registry = _FakeRegistry({}, lineage_hints={_ORPHAN_SEI: _RENAMED_EVENT})
    db = _FakeDBWithRegistry(_governed_rows_attached_at(_ORPHAN_SEI, "h1"), registry)
    with caplog.at_level(logging.WARNING, logger="filigree.governance"):
        decision = governance.evaluate_closure_gate(db, "test-1")
    assert decision.outcome is GateOutcome.PROCEED  # still not a block
    assert decision.allowed
    assert decision.lineage_hints == {_ORPHAN_SEI: _RENAMED_EVENT}
    assert decision.loomweave_unavailable is False  # Loomweave answered
    records = _unresolved_records(caplog)
    assert len(records) == 1
    assert records[0].lineage_hints == {_ORPHAN_SEI: _RENAMED_EVENT}
    assert "py:func:mod::g" in records[0].getMessage()
    assert _ORPHAN_SEI in records[0].getMessage()


def test_legacy_resolution_without_lineage_key_has_no_hint(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
    registry = _FakeRegistry({})  # no lineage_hints key at all (pre-hint producer)
    db = _FakeDBWithRegistry(_governed_rows_attached_at(_ORPHAN_SEI, "h1"), registry)
    with caplog.at_level(logging.WARNING, logger="filigree.governance"):
        decision = governance.evaluate_closure_gate(db, "test-1")
    assert decision.outcome is GateOutcome.PROCEED
    assert decision.lineage_hints == {}
    records = _unresolved_records(caplog)
    assert len(records) == 1
    assert records[0].lineage_hints == {}
    assert "rename lineage" not in records[0].getMessage()


def test_hint_names_rebind_target_on_stale_reason_when_sibling_drifted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drifted sibling + orphaned SEI -> STALE (drift wins, unchanged) and the
    agent-visible reason names the orphan's re-bind target."""
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    rows = _governed_rows_attached_at("py:func:mod::x", "h1") + _governed_rows_attached_at(_ORPHAN_SEI, "h1")
    registry = _FakeRegistry({"py:func:mod::x": "h2"}, lineage_hints={_ORPHAN_SEI: _RENAMED_EVENT})
    db = _FakeDBWithRegistry(rows, registry)
    spy: list[str] = []
    monkeypatch.setattr(governance, "check_closure_gate", lambda iid: spy.append(iid))
    decision = governance.evaluate_closure_gate(db, "test-1")
    assert decision.outcome is GateOutcome.STALE
    assert "drifted since attach" in decision.reason
    assert f"rename lineage: {_ORPHAN_SEI} -> py:func:mod::g (locator_changed)" in decision.reason
    assert decision.lineage_hints == {_ORPHAN_SEI: _RENAMED_EVENT}
    assert spy == []


def test_hint_suffixes_legis_block_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Legis BLOCKED verdict on an issue with an orphaned binding carries the
    hint on the reason the agent sees, without altering the outcome."""
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.BLOCKED, reason="policy says no"))
    registry = _FakeRegistry({}, lineage_hints={_ORPHAN_SEI: _RENAMED_EVENT})
    db = _FakeDBWithRegistry(_governed_rows_attached_at(_ORPHAN_SEI, "h1"), registry)
    decision = governance.evaluate_closure_gate(db, "test-1")
    assert decision.outcome is GateOutcome.BLOCKED
    assert decision.reason.startswith("policy says no")
    assert f"rename lineage: {_ORPHAN_SEI} -> py:func:mod::g (locator_changed)" in decision.reason
    assert decision.lineage_hints == {_ORPHAN_SEI: _RENAMED_EVENT}


def test_hint_without_new_locator_names_the_event_only(monkeypatch: pytest.MonkeyPatch) -> None:
    died = {"event": "died", "old_locator": "py:func:mod::f", "new_locator": None, "run_id": "r", "recorded_at": "t"}
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.BLOCKED, reason="policy says no"))
    registry = _FakeRegistry({}, lineage_hints={_ORPHAN_SEI: died})
    db = _FakeDBWithRegistry(_governed_rows_attached_at(_ORPHAN_SEI, "h1"), registry)
    decision = governance.evaluate_closure_gate(db, "test-1")
    assert decision.outcome is GateOutcome.BLOCKED
    assert f"rename lineage: {_ORPHAN_SEI}: died" in decision.reason
    assert "->" not in decision.reason


def test_proceed_reason_stays_empty_with_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    # PROCEED carries the hint as data only; ``reason`` is the "why not allowed"
    # channel and must stay empty so close surfaces do not print a phantom error.
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
    registry = _FakeRegistry({}, lineage_hints={_ORPHAN_SEI: _RENAMED_EVENT})
    db = _FakeDBWithRegistry(_governed_rows_attached_at(_ORPHAN_SEI, "h1"), registry)
    decision = governance.evaluate_closure_gate(db, "test-1")
    assert decision.outcome is GateOutcome.PROCEED
    assert decision.reason == ""


def test_loomweave_known_down_skips_lineage_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """known-down short-circuits the resolver entirely: no lineage lookup, no hint."""
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
    registry = _FakeRegistry({}, lineage_hints={_ORPHAN_SEI: _RENAMED_EVENT})
    db = _FakeDBWithRegistry(_governed_rows_attached_at(_ORPHAN_SEI, "h1"), registry)
    decision = governance.evaluate_closure_gate(db, "test-1", loomweave_known_down=True)
    assert decision.outcome is GateOutcome.PROCEED
    assert registry.calls == []
    assert decision.lineage_hints == {}
    assert decision.loomweave_unavailable is True


# --- loomweave_known_down batch short-circuit (hub weft-aee5769607 item 1) ----
# The RED-1 drift check costs one Loomweave round-trip (with its own deadline /
# retry budget) per governed, non-stale issue. ``loomweave_known_down`` lets a
# batch caller skip that probe once an earlier issue already proved Loomweave
# down, and ``GateDecision.loomweave_unavailable`` is how the gate tells the
# caller that happened. Unlike ``legis_known_down`` this is ENRICH-ONLY: the
# issue still proceeds to its own Legis verdict — freshness is UNKNOWN, never a
# block. Ordering mirrors the Legis analogue: applied at the resolver call,
# after the ungoverned / snapshot-STALE short-circuits.


class _FakeRegistryRaising(_FakeRegistry):
    """``_FakeRegistry`` that raises an arbitrary exception on resolve."""

    def __init__(self, exc: Exception) -> None:
        super().__init__({})
        self._exc = exc

    def resolve_entity_content_hashes(self, entity_ids: list[str]) -> dict[str, object]:
        self.calls.append(list(entity_ids))
        raise self._exc


def _loomweave_outage_exceptions() -> list[Exception]:
    from filigree.registry import RegistryUnavailableError, RegistryVersionMismatchError

    return [
        RegistryUnavailableError("loomweave down", url="http://loomweave.invalid", cause_kind="network"),
        RegistryUnavailableError("loomweave hung", url="http://loomweave.invalid", cause_kind="timeout"),
        RegistryVersionMismatchError("api_version 99", url="http://loomweave.invalid", expected=1, advertised=99),
    ]


@pytest.mark.parametrize("exc", _loomweave_outage_exceptions(), ids=["network", "timeout", "version_mismatch"])
def test_loomweave_outage_flags_decision_loomweave_unavailable(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """(a) A whole-backend Loomweave failure during the drift probe — a
    connectivity-class ``RegistryUnavailableError`` (the request got no answer)
    or a version mismatch (input-independent) — still PROCEEDs (enrich-only) but
    stamps ``loomweave_unavailable=True`` so a batch caller can bound the outage
    to one probe."""
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
    entity_id = "py:func:mod::f"
    registry = _FakeRegistryRaising(exc)
    db = _FakeDBWithRegistry(_governed_rows_attached_at(entity_id, "h1"), registry)
    decision = governance.evaluate_closure_gate(db, "test-1")
    assert decision.outcome is GateOutcome.PROCEED
    assert decision.loomweave_unavailable is True
    assert registry.calls == [[entity_id]]  # the probe was attempted exactly once


@pytest.mark.parametrize("cause_kind", ["http_error", "auth", "invalid_response", "unknown"])
def test_non_connectivity_registry_failure_degrades_only_this_issue(monkeypatch: pytest.MonkeyPatch, cause_kind: str) -> None:
    """A ``RegistryUnavailableError`` Loomweave ANSWERED with — a deterministic
    4xx such as 413 for one issue's oversize locator, a 5xx, an auth refusal, a
    malformed body — is per-issue: freshness UNKNOWN for THIS issue (still
    PROCEEDs, enrich-only) but ``loomweave_unavailable`` stays False so a batch
    caller keeps probing its later issues instead of auto-closing a drifted one."""
    from filigree.registry import RegistryUnavailableError

    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
    entity_id = "py:func:mod::f"
    registry = _FakeRegistryRaising(RegistryUnavailableError("HTTP 413", url="http://loomweave.invalid", cause_kind=cause_kind))
    db = _FakeDBWithRegistry(_governed_rows_attached_at(entity_id, "h1"), registry)
    decision = governance.evaluate_closure_gate(db, "test-1")
    assert decision.outcome is GateOutcome.PROCEED
    assert decision.loomweave_unavailable is False
    assert registry.calls == [[entity_id]]


@pytest.mark.parametrize("status_code", [502, 503, 504])
def test_retried_out_gateway_5xx_flags_decision_loomweave_unavailable(monkeypatch: pytest.MonkeyPatch, status_code: int) -> None:
    """A gateway-class 5xx (502/503/504 — a proxy in front of a dead or
    overloaded Loomweave) that survived the resolver's retry budget is
    input-independent, so it counts toward the batch known-down bound like a
    network failure: every later governed issue would otherwise re-burn the full
    retry budget against the same dead upstream. Still PROCEEDs (enrich-only)."""
    from filigree.registry import RegistryUnavailableError

    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
    entity_id = "py:func:mod::f"
    exc = RegistryUnavailableError(f"HTTP {status_code}", url="http://loomweave.invalid", cause_kind="http_error", status_code=status_code)
    registry = _FakeRegistryRaising(exc)
    decision = governance.evaluate_closure_gate(_FakeDBWithRegistry(_governed_rows_attached_at(entity_id, "h1"), registry), "test-1")
    assert decision.outcome is GateOutcome.PROCEED
    assert decision.loomweave_unavailable is True
    assert registry.calls == [[entity_id]]


@pytest.mark.parametrize("status_code", [413, 422, 500])
def test_non_gateway_http_error_with_status_degrades_only_this_issue(monkeypatch: pytest.MonkeyPatch, status_code: int) -> None:
    """A deterministic 4xx, or a plain 500 (which a specific input can trigger),
    stays per-issue even though the exception now carries its status code."""
    from filigree.registry import RegistryUnavailableError

    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
    entity_id = "py:func:mod::f"
    exc = RegistryUnavailableError(f"HTTP {status_code}", url="http://loomweave.invalid", cause_kind="http_error", status_code=status_code)
    registry = _FakeRegistryRaising(exc)
    decision = governance.evaluate_closure_gate(_FakeDBWithRegistry(_governed_rows_attached_at(entity_id, "h1"), registry), "test-1")
    assert decision.outcome is GateOutcome.PROCEED
    assert decision.loomweave_unavailable is False


def test_lineage_connectivity_failure_is_advisory_and_never_flips_loomweave_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry's ``lineage_unavailable`` advisory (the orphan rename-hint
    fallback hit a connectivity-class failure) is surfaced on its own
    ``GateDecision.lineage_unavailable`` field and NEVER folded into
    ``loomweave_unavailable``: the primary by-SEI channel answered milliseconds
    earlier, which is direct evidence Loomweave is up, and a batch caller reads
    ``loomweave_unavailable`` as known-down — feeding it here would skip every
    later issue's drift probe and let a drifted binding auto-close. The outcome
    is untouched, whether PROCEED (fresh sibling) or STALE (drifted sibling)."""
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
    orphan = "loomweave:eid:0000000000000000000000000000beef"
    fresh = "py:func:mod::f"
    rows = _governed_rows_attached_at(fresh, "h1") + _governed_rows_attached_at(orphan, "h1")
    decision = governance.evaluate_closure_gate(_FakeDBWithRegistry(rows, _FakeRegistry({fresh: "h1"}, lineage_unavailable=True)), "test-1")
    assert decision.outcome is GateOutcome.PROCEED
    assert decision.lineage_unavailable is True
    assert decision.loomweave_unavailable is False
    stale = governance.evaluate_closure_gate(_FakeDBWithRegistry(rows, _FakeRegistry({fresh: "h2"}, lineage_unavailable=True)), "test-1")
    assert stale.outcome is GateOutcome.STALE
    assert stale.lineage_unavailable is True
    assert stale.loomweave_unavailable is False
    # A legacy producer without the key, or one that answered False, is unflagged on both.
    plain = governance.evaluate_closure_gate(_FakeDBWithRegistry(rows, _FakeRegistry({fresh: "h1"})), "test-1")
    assert plain.lineage_unavailable is False
    assert plain.loomweave_unavailable is False


def test_loomweave_known_down_skips_resolver_and_still_proceeds_to_legis(monkeypatch: pytest.MonkeyPatch) -> None:
    """(b) With Loomweave already known down in this batch, the resolver is NOT
    called, the decision reports ``loomweave_unavailable``, and the issue still
    gets its own Legis verdict (enrich-only: Loomweave-down never blocks)."""
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    spy: list[str] = []
    monkeypatch.setattr(governance, "check_closure_gate", lambda iid: spy.append(iid) or LegisGateResult(LegisGateStatus.ALLOWED))
    entity_id = "py:func:mod::f"
    registry = _FakeRegistry({entity_id: "h2"})  # would report drift if consulted
    db = _FakeDBWithRegistry(_governed_rows_attached_at(entity_id, "h1"), registry)
    decision = governance.evaluate_closure_gate(db, "test-1", loomweave_known_down=True)
    assert registry.calls == []  # probe suppressed by the batch-level known-down flag
    assert decision.outcome is GateOutcome.PROCEED
    assert decision.loomweave_unavailable is True
    assert spy == ["test-1"]  # Legis was still consulted


def test_healthy_loomweave_decision_not_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """(c) Normal resolution, a per-entity unresolved degrade, and a db with no
    ``.registry`` all leave ``loomweave_unavailable`` False — no whole-backend
    outage happened, so there is nothing for a batch caller to bound."""
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
    entity_id = "py:func:mod::f"
    healthy = _FakeDBWithRegistry(_governed_rows_attached_at(entity_id, "h1"), _FakeRegistry({entity_id: "h1"}))
    assert governance.evaluate_closure_gate(healthy, "test-1").loomweave_unavailable is False
    unresolved = _FakeDBWithRegistry(_governed_rows_attached_at(entity_id, "h1"), _FakeRegistry({}))
    assert governance.evaluate_closure_gate(unresolved, "test-1").loomweave_unavailable is False
    no_registry = _FakeDB(_governed_rows_attached_at(entity_id, "h1"))
    assert governance.evaluate_closure_gate(no_registry, "test-1").loomweave_unavailable is False
    # And a drifted (STALE) verdict from a healthy Loomweave is not flagged either.
    drifted = _FakeDBWithRegistry(_governed_rows_attached_at(entity_id, "h1"), _FakeRegistry({entity_id: "h2"}))
    stale = governance.evaluate_closure_gate(drifted, "test-1")
    assert stale.outcome is GateOutcome.STALE
    assert stale.loomweave_unavailable is False


def test_loomweave_known_down_does_not_mask_snapshot_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """(d) Ordering pin: the snapshot-STALE short-circuit runs BEFORE the
    known-down flag is consulted — a drifted sign-off still reports STALE with
    no resolver call and no Legis call."""
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    spy: list[str] = []
    monkeypatch.setattr(governance, "check_closure_gate", lambda iid: spy.append(iid))
    registry = _FakeRegistry({"sei:a": "h2"})
    db = _FakeDBWithRegistry(_stale_governed_rows(), registry)
    decision = governance.evaluate_closure_gate(db, "test-1", loomweave_known_down=True)
    assert decision.outcome is GateOutcome.STALE
    assert decision.loomweave_unavailable is False  # never reached the resolver
    assert registry.calls == []
    assert spy == []


def test_ungoverned_with_loomweave_known_down_proceeds_unflagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ungoverned short-circuit precedes the flag: PROCEED, not flagged, no calls."""
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    spy: list[str] = []
    monkeypatch.setattr(governance, "check_closure_gate", lambda iid: spy.append(iid))
    registry = _FakeRegistry({"sei:a": "h2"})
    db = _FakeDBWithRegistry(_ungoverned_rows(), registry)
    decision = governance.evaluate_closure_gate(db, "test-1", loomweave_known_down=True)
    assert decision.outcome is GateOutcome.PROCEED
    assert decision.loomweave_unavailable is False
    assert registry.calls == []
    assert spy == []


def test_both_known_down_is_unavailable_with_zero_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """(e) Loomweave AND Legis both known down: the Legis short-circuit still
    fails closed as UNAVAILABLE (DECISION 2), no probe of either backend, and
    the decision carries the Loomweave flag."""
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    spy: list[str] = []
    monkeypatch.setattr(governance, "check_closure_gate", lambda iid: spy.append(iid))
    entity_id = "py:func:mod::f"
    registry = _FakeRegistry({entity_id: "h1"})
    db = _FakeDBWithRegistry(_governed_rows_attached_at(entity_id, "h1"), registry)
    decision = governance.evaluate_closure_gate(db, "test-1", loomweave_known_down=True, legis_known_down=True)
    assert decision.outcome is GateOutcome.UNAVAILABLE
    assert decision.loomweave_unavailable is True
    assert registry.calls == []
    assert spy == []


def test_legis_known_down_semantics_unchanged_by_loomweave_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """``legis_known_down`` alone (Loomweave healthy) still runs the drift probe
    first and then fails closed as UNAVAILABLE — the drift-before-Legis ordering
    is untouched, and a drifted binding is reported STALE, not UNAVAILABLE."""
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    spy: list[str] = []
    monkeypatch.setattr(governance, "check_closure_gate", lambda iid: spy.append(iid))
    entity_id = "py:func:mod::f"
    fresh = _FakeRegistry({entity_id: "h1"})
    decision = governance.evaluate_closure_gate(
        _FakeDBWithRegistry(_governed_rows_attached_at(entity_id, "h1"), fresh), "test-1", legis_known_down=True
    )
    assert decision.outcome is GateOutcome.UNAVAILABLE
    assert decision.loomweave_unavailable is False
    assert fresh.calls == [[entity_id]]  # drift probe still ran
    drifted = _FakeRegistry({entity_id: "h2"})
    decision = governance.evaluate_closure_gate(
        _FakeDBWithRegistry(_governed_rows_attached_at(entity_id, "h1"), drifted), "test-1", legis_known_down=True
    )
    assert decision.outcome is GateOutcome.STALE
    assert spy == []
