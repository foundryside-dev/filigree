"""Release-gate reproduction: governed->ungoverned bypass via the signature
field (PR #52 review finding, 3-of-4-agent convergence).

DECISION 1A (governance.py:12-14) defines governed as "an issue with >=1
entity-association carrying a *non-null* Legis signature." The exploitable
defect is that a benign re-attach of an already-governed association silently
drops that signature, flipping the issue ungoverned so the closure gate skips
Legis entirely:

  * MCP path  -- ``mcp_tools/entities.py`` re-attach passes no ``signature``;
    ``add_entity_association``'s UPSERT writes ``signature = excluded.signature``
    unconditionally (db_entity_associations.py:198), clobbering the stored
    signature back to NULL. Unlike the sibling ``entity_kind`` column, there is
    no preserve-on-absence CASE.
  * HTTP path -- the route accepts ``signature=""`` (entities.py:147 checks only
    ``isinstance(str)``); it is stored verbatim, and the gate's truthiness
    predicate (``if not any(row.get("signature") ...)``, governance.py:89) reads
    the non-null "" as ungoverned -- contradicting DECISION 1A.

These are one bypass (the clobber) plus one classification bug (truthiness vs.
``is not None``) that share the same UPSERT as their mechanism.

WHAT THESE TESTS ASSERT -- the *resolution-agnostic security invariant*:
"after a benign re-attach, a governed issue still cannot be closed when Legis
says BLOCK." We configure the Legis spy to BLOCK and assert the gate does NOT
return PROCEED. This holds under every candidate fix the governance owner might
pick -- preserve-and-consult (gate -> BLOCKED) and fail-closed-on-drift
(gate -> UNAVAILABLE, possibly without a network call) -- so the test does not
prejudge the design (see docs/superpowers/specs/2026-06-06-signature-bypass-resolution.md).

Deliberately NOT asserted: that the gate makes a network call, or that the
signature column stays populated -- both are mechanisms that a legitimate
fail-closed-locally resolution would violate. We assert only that the close is
not silently waved through.

The *drift* case (governed h1 -> signatureless re-attach at a NEW hash h2 -> must
fail closed even if Legis would ALLOW the stale binding) was resolved as
fail-closed-on-drift (GateOutcome.STALE, schema v27) and is pinned directly in
tests/core/test_governance_gate.py (the _FakeDB drift cases and the real-DB
end-to-end ``test_real_db_signatureless_reattach_drifts_to_stale``).

These tests now PASS post-fix; they assert the resolution-agnostic invariant
(BLOCKING Legis -> gate does not PROCEED) and remain a guard against regression.
"""

from __future__ import annotations

import pytest

from filigree import governance, legis_client
from filigree.core import FiligreeDB
from filigree.governance import GateOutcome
from filigree.legis_client import LegisGateResult, LegisGateStatus

_ENTITY = "sei:func:auth.verify_token"


def _spy_legis(monkeypatch: pytest.MonkeyPatch, status: LegisGateStatus) -> list[str]:
    """Configure governance and record every Legis consultation.

    ``check_closure_gate`` returns ``status`` and appends each consulted issue
    id to the returned list. A governed close consults Legis (list grows and the
    verdict applies); an *ungoverned* close short-circuits to PROCEED before the
    network (list stays empty, verdict ignored).
    """
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    calls: list[str] = []

    def _record(issue_id: str) -> LegisGateResult:
        calls.append(issue_id)
        return LegisGateResult(status, reason="no verified binding")

    monkeypatch.setattr(governance, "check_closure_gate", _record)
    return calls


def _govern(db: FiligreeDB, content_hash: str = "hash-v1") -> str:
    """Create an issue and attach a Legis-signed association; return its id."""
    issue = db.create_issue("Harden token verification", priority=1)
    db.add_entity_association(
        issue.id,
        _ENTITY,
        content_hash=content_hash,
        actor="legis",
        signature="deadbeefcafef00d",
        signoff_seq=1,
    )
    return issue.id


# --- sanity anchors: prove the spy wiring, so the bypass reds aren't vacuous ---


def test_governed_allowed_proceeds_and_consults(db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_legis(monkeypatch, LegisGateStatus.ALLOWED)
    issue_id = _govern(db)
    decision = governance.evaluate_closure_gate(db, issue_id)
    assert decision.outcome is GateOutcome.PROCEED
    assert calls == [issue_id], "a governed close must consult Legis"


def test_governed_blocked_does_not_proceed(db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    _spy_legis(monkeypatch, LegisGateStatus.BLOCKED)
    issue_id = _govern(db)
    decision = governance.evaluate_closure_gate(db, issue_id)
    assert decision.outcome is not GateOutcome.PROCEED, "Legis BLOCK must stop the close"


# --- the bypass: a benign re-attach must not silently un-gate the close --------


def test_signatureless_mcp_reattach_keeps_issue_gated(db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduces the MCP path: the handler re-attaches to refresh the drifted
    content hash and passes no ``signature``. Today the UPSERT clobbers the
    stored signature to NULL, the issue reads ungoverned, and the close proceeds
    even though Legis would BLOCK.
    """
    calls = _spy_legis(monkeypatch, LegisGateStatus.BLOCKED)
    issue_id = _govern(db)

    # Byte-for-byte as mcp_tools/entities.py issues it: no signature kwarg.
    db.add_entity_association(issue_id, _ENTITY, content_hash="hash-v2", actor="agent")

    decision = governance.evaluate_closure_gate(db, issue_id)
    assert decision.outcome is not GateOutcome.PROCEED, (
        f"BYPASS: signatureless re-attach un-governed the issue -- a governed "
        f"close was waved through despite Legis BLOCK (outcome={decision.outcome}, "
        f"consulted={bool(calls)}). A routine drift refresh silently revoked governance."
    )


def test_empty_string_http_reattach_keeps_issue_gated(db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduces the HTTP path: the route accepts an empty-string signature
    (non-null, so governed per DECISION 1A), stores it verbatim, and the
    truthiness predicate then reads it as ungoverned. Same content hash, so this
    isolates the classification bug from drift.
    """
    calls = _spy_legis(monkeypatch, LegisGateStatus.BLOCKED)
    issue_id = _govern(db)

    db.add_entity_association(issue_id, _ENTITY, content_hash="hash-v1", actor="dashboard", signature="")

    decision = governance.evaluate_closure_gate(db, issue_id)
    assert decision.outcome is not GateOutcome.PROCEED, (
        f"BYPASS: a non-null empty-string signature read as ungoverned, "
        f"contradicting DECISION 1A ('non-null signature'); the close was waved "
        f"through despite Legis BLOCK (outcome={decision.outcome}, consulted={bool(calls)})."
    )
