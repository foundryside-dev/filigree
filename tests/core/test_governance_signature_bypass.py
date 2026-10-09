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

WHAT THESE TESTS ASSERT -- the *governed classification invariant*: "after a
benign re-attach (or a refused removal), a governed issue is still classified
governed." Legis is retired (Task 0.2, M-7), so a governed close no longer
consults it: it PROCEEDs carrying a ``governance_provider_archived`` warning,
while an ungoverned close PROCEEDs without one, and a drifted sign-off fails
closed as STALE. The warning is therefore the observable marker of "still
governed": a bypass that un-governs the issue would silently drop it. (The
original assertion -- "Legis BLOCK stops the close" -- cannot be expressed now
that Legis is never asked.) See
docs/superpowers/specs/2026-06-06-signature-bypass-resolution.md.

The *drift* case (governed h1 -> signatureless re-attach at a NEW hash h2 -> must
fail closed even if Legis would ALLOW the stale binding) was resolved as
fail-closed-on-drift (GateOutcome.STALE, schema v27) and is pinned directly in
tests/core/test_governance_gate.py (the _FakeDB drift cases and the real-DB
end-to-end ``test_real_db_signatureless_reattach_drifts_to_stale``).

These tests PASS post-fix and remain a guard against regression.
"""

from __future__ import annotations

import pytest

from filigree import governance
from filigree.core import FiligreeDB
from filigree.db_entity_associations import GovernedAssociationRemovalError
from filigree.governance import GateOutcome
from tests._fakes.legis_retired import governance_on as _governance_on

_ENTITY = "sei:func:auth.verify_token"


def _is_governed(decision: governance.GateDecision) -> bool:
    """A governed issue that clears the local checks carries the archived-provider warning."""
    return governance.GOVERNANCE_PROVIDER_ARCHIVED_WARNING in decision.warnings


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


# --- sanity anchors: prove the classification marker, so the bypass reds aren't vacuous ---


def test_governed_proceeds_with_archived_warning(db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    _governance_on(monkeypatch)
    issue_id = _govern(db)
    decision = governance.evaluate_closure_gate(db, issue_id)
    assert decision.outcome is GateOutcome.PROCEED
    assert _is_governed(decision), "a governed close must carry the archived-provider warning"


def test_ungoverned_proceeds_without_warning(db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    _governance_on(monkeypatch)
    issue = db.create_issue("ungoverned", priority=2)
    decision = governance.evaluate_closure_gate(db, issue.id)
    assert decision.outcome is GateOutcome.PROCEED
    assert not _is_governed(decision)


# --- the bypass: a benign re-attach must not silently un-gate the close --------


def test_signatureless_mcp_reattach_keeps_issue_gated(db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduces the MCP path: the handler re-attaches to refresh the drifted
    content hash and passes no ``signature``. Before the fix the UPSERT clobbered
    the stored signature to NULL and the issue read ungoverned. Now the signature
    is preserved, so the drifted sign-off fails closed as STALE.
    """
    _governance_on(monkeypatch)
    issue_id = _govern(db)

    # Byte-for-byte as mcp_tools/entities.py issues it: no signature kwarg.
    db.add_entity_association(issue_id, _ENTITY, content_hash="hash-v2", actor="agent")

    decision = governance.evaluate_closure_gate(db, issue_id)
    assert decision.outcome is GateOutcome.STALE, (
        f"BYPASS: signatureless re-attach un-governed the issue -- a routine drift "
        f"refresh silently revoked governance (outcome={decision.outcome})."
    )


def test_empty_string_http_reattach_keeps_issue_gated(db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduces the HTTP path: the route accepts an empty-string signature
    (non-null, so governed per DECISION 1A), stores it verbatim, and the
    truthiness predicate then read it as ungoverned. Same content hash, so this
    isolates the classification bug from drift.
    """
    _governance_on(monkeypatch)
    issue_id = _govern(db)

    db.add_entity_association(issue_id, _ENTITY, content_hash="hash-v1", actor="dashboard", signature="")

    decision = governance.evaluate_closure_gate(db, issue_id)
    assert _is_governed(decision), (
        f"BYPASS: a non-null empty-string signature read as ungoverned, contradicting "
        f"DECISION 1A ('non-null signature') (outcome={decision.outcome})."
    )


# --- the removal vector: deleting the signed row must not un-gate the close ----
#
# The write-clobber (above) and blank-signature vectors were closed in v27, but
# DELETE is an independent way to drop the signed row. An ungated removal of a
# governed issue's only signed association flips it ungoverned, so the gate
# short-circuits to a plain PROCEED (the same ``if not signed_rows`` path the
# clobber exploited). The data layer must refuse the removal; these tests assert
# the issue stays classified governed plus that the guard does not over-block the
# ungoverned cases.


def test_signatureless_removal_of_signed_assoc_is_refused(db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduces the removal vector: deleting a governed issue's only signed
    association would un-govern it. The data layer refuses the removal, so the
    signed row survives and the issue stays governed.
    """
    _governance_on(monkeypatch)
    issue_id = _govern(db)

    with pytest.raises(GovernedAssociationRemovalError):
        db.remove_entity_association(issue_id, _ENTITY, actor="agent")

    decision = governance.evaluate_closure_gate(db, issue_id)
    assert _is_governed(decision), f"BYPASS: removing the signed association un-governed the issue (outcome={decision.outcome})."


def test_removal_guard_holds_even_when_legis_unconfigured(db: FiligreeDB) -> None:
    """The guard keys on the durable signature, NOT on governance being live:
    unsetting LEGIS_URL must not become a back door to delete the sign-off.
    """
    issue = db.create_issue("Harden token verification", priority=1)
    db.add_entity_association(issue.id, _ENTITY, content_hash="hash-v1", actor="legis", signature="sig", signoff_seq=1)
    with pytest.raises(GovernedAssociationRemovalError):
        db.remove_entity_association(issue.id, _ENTITY, actor="agent")


# --- the guard must NOT over-block the ungoverned cases ------------------------


def test_removal_of_unsigned_assoc_still_works(db: FiligreeDB) -> None:
    """An ungoverned (signatureless) binding is freely removable, idempotently."""
    issue = db.create_issue("ungoverned", priority=2)
    db.add_entity_association(issue.id, _ENTITY, content_hash="h1", actor="agent")
    assert db.remove_entity_association(issue.id, _ENTITY, actor="agent") is True
    # Idempotent no-op on the now-missing row.
    assert db.remove_entity_association(issue.id, _ENTITY, actor="agent") is False


def test_removal_of_missing_row_is_idempotent(db: FiligreeDB) -> None:
    issue = db.create_issue("empty", priority=2)
    assert db.remove_entity_association(issue.id, "sei:never:attached", actor="agent") is False


def test_unsigned_sibling_removable_while_signed_binding_keeps_issue_governed(db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-row guard: on a governed issue with a signed + an unsigned binding,
    removing the unsigned sibling succeeds and the issue stays governed via the
    signed binding; the signed binding itself stays un-removable.
    """
    _governance_on(monkeypatch)
    issue_id = _govern(db)  # signed binding on _ENTITY
    sibling = "sei:func:auth.refresh_token"
    db.add_entity_association(issue_id, sibling, content_hash="hash-v1", actor="agent")

    assert db.remove_entity_association(issue_id, sibling, actor="agent") is True
    decision = governance.evaluate_closure_gate(db, issue_id)
    assert _is_governed(decision), "the surviving signed binding must keep the issue governed"
    with pytest.raises(GovernedAssociationRemovalError):
        db.remove_entity_association(issue_id, _ENTITY, actor="agent")
