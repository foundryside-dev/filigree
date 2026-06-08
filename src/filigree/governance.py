"""Transport-neutral Legis closure-gate policy (B5).

This module owns the *decision* — which issues are governed and what to do
when Legis cannot confirm a binding — while staying free of any transport
concern. Every close surface (HTTP routes, MCP tools, CLI) calls
:func:`evaluate_closure_gate` and renders the resulting :class:`GateDecision`
in its own idiom, so the gate cannot be bypassed by closing through a
different surface. The data layer is never involved in the network call.

DECISIONS (see the B5 design notes):

- **DECISION 1A — governed = signature present.** An issue is governed when
  it has >=1 entity-association carrying a non-null Legis ``signature`` (the
  B1 column). Only governed issues consult Legis; ungoverned closes make no
  network call.
- **DECISION 2 — fail-closed for governed.** When Legis is disabled (404)
  or unreachable (timeout/connection error), a *governed* close is blocked
  (``UNAVAILABLE``) so an operator cannot dodge the gate by taking Legis
  offline. A 500 (tampered ledger) is ``INTEGRITY_FAILURE``. A 2xx that
  violates the wire contract (no ``allowed=true``) is ``CONTRACT_VIOLATION``
  — a *per-issue* fail-closed verdict, NOT ``UNAVAILABLE``: Legis answered, so
  it is reachable, and one bad answer must not short-circuit a whole cascade
  batch. With ``LEGIS_URL`` unset, governance is OFF entirely and every close
  proceeds ("invisible until wanted").
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from filigree import legis_client
from filigree.legis_client import LegisGateResult, LegisGateStatus
from filigree.types.core import make_issue_id


class GateOutcome(Enum):
    """What the close surface should do with a gate decision."""

    PROCEED = "proceed"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"
    INTEGRITY_FAILURE = "integrity_failure"
    # v27: a governed binding whose Legis sign-off has drifted (the bound content
    # moved on since it was signed). Fails closed like BLOCKED, but is a *local*
    # per-issue verdict — distinct from UNAVAILABLE so it never short-circuits a
    # whole cascade batch the way a Legis-down verdict does.
    STALE = "stale"
    # Legis answered a governed close with a contract-violating 2xx (a body that
    # did not affirm allowed=true). Like STALE, this is a *per-issue* fail-closed
    # verdict, NOT a connectivity failure: Legis is reachable (it returned a 2xx),
    # so it must not short-circuit the rest of a cascade batch the way UNAVAILABLE
    # does. The next issue still gets its own (cheap, already-responding) gate call.
    CONTRACT_VIOLATION = "contract_violation"


@dataclass(frozen=True)
class GateDecision:
    """A transport-neutral closure-gate verdict."""

    outcome: GateOutcome
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome is GateOutcome.PROCEED


_PROCEED = GateDecision(GateOutcome.PROCEED)


class _AssocReader(Protocol):
    # Structural type for "something that can read an issue's entity
    # associations" — satisfied by FiligreeDB without importing it (avoids a
    # circular import). ``list[Any]`` keeps it compatible with the concrete
    # ``list[EntityAssociationRow]`` return; rows are accessed via ``.get``.
    def list_entity_associations(self, issue_id: Any) -> list[Any]: ...


class _StatusGateReader(_AssocReader, Protocol):
    # Adds the issue/template reads the status-change gate needs to tell a
    # *closing* status write (target done-category) from an ordinary one.
    # Both methods exist on FiligreeDB.
    def get_issue(self, issue_id: Any) -> Any: ...
    def _resolve_status_category(self, issue_type: str, status: str) -> Any: ...


def check_closure_gate(issue_id: str) -> LegisGateResult:
    """Indirection point over the Legis client (monkeypatched in tests)."""
    return legis_client.check_closure_gate(issue_id)


def _signed_row_is_stale(row: Any) -> bool:
    """A signed association is stale when the content it was signed over no
    longer matches the current attached content.

    The Legis signature is an HMAC bound to a content snapshot, recorded in
    ``signed_content_hash``. ``content_hash_at_attach`` advances on every
    re-attach; when they diverge, the sign-off vouches for content that has since
    drifted. ``signed_content_hash`` NULL = a legacy / backfilled row with no
    recorded snapshot → treated as fresh (the compatibility shim).
    """
    signed = row.get("signed_content_hash")
    if signed is None:
        return False
    return bool(signed != row.get("content_hash_at_attach"))


def evaluate_closure_gate(db: _AssocReader, issue_id: str, *, legis_known_down: bool = False) -> GateDecision:
    """Decide whether *issue_id* may be closed.

    Short-circuits to ``PROCEED`` when governance is off, and again for
    ungoverned issues — only a governed issue triggers a network call. A
    *governed* issue whose Legis sign-off has drifted (any signed binding's
    content moved on since it was signed) fails closed as ``STALE`` with no
    network call: Filigree cannot treat a sign-off over old content as covering
    new content, and the issue-id-only gate call cannot convey the drift to
    Legis — only a fresh Legis sign-off (a signed write) clears it (v27).

    ``legis_known_down`` lets a batch caller suppress the per-issue Legis
    round-trip once an earlier issue in the same sweep already proved Legis
    unreachable (bounding a down/slow Legis to one timeout per batch). It is
    applied **only** at the point a network call would otherwise happen — after
    the governance-off, ungoverned, and stale short-circuits — so an ungoverned
    or governance-off issue later in the batch still PROCEEDs and a stale one
    still reports ``STALE``. A governed, non-stale issue fails closed as
    ``UNAVAILABLE`` (DECISION 2) with no further network call.
    """
    if not legis_client.is_configured():
        return _PROCEED
    rows = db.list_entity_associations(make_issue_id(str(issue_id)))
    # Governed = >=1 association carries a non-null Legis signature (DECISION 1A).
    # ``is not None`` rather than truthiness so a blank signature cannot
    # masquerade as ungoverned (the data layer also normalises "" -> NULL).
    signed_rows = [row for row in rows if row.get("signature") is not None]
    if not signed_rows:
        return _PROCEED  # ungoverned — no network call (DECISION 1A)
    if any(_signed_row_is_stale(row) for row in signed_rows):
        # Fail closed locally — do NOT consult Legis (it is asked only issue_id
        # and would answer for the stale snapshot it last saw).
        return GateDecision(GateOutcome.STALE, "entity content drifted since the Legis sign-off; awaiting re-sign")
    if legis_known_down:
        # A governed, non-stale issue needs a Legis round-trip, but a prior issue
        # in this batch already proved Legis unreachable — fail closed without
        # re-incurring the timeout (DECISION 2).
        return GateDecision(GateOutcome.UNAVAILABLE, "Legis unreachable earlier in this batch")
    return _map_result(check_closure_gate(str(issue_id)))


def evaluate_status_change_gate(db: _StatusGateReader, issue_id: str, requested_status: str | None) -> GateDecision:
    """Decide whether a status *write* that would close *issue_id* may proceed.

    ``close_issue`` delegates to ``update_issue`` (same template validator,
    same data-layer write), so ``update_issue``/``batch_update`` can drive a
    governed issue into a done-category status. Those surfaces historically
    skipped the gate — an ungated close-equivalent. This mirrors
    :func:`evaluate_closure_gate` for them, and is the single decision every
    status-write surface (MCP/HTTP/loom/CLI, single and batch) routes through
    so the policy cannot drift per verb.

    Returns ``PROCEED`` — making no network call and (beyond governance-off)
    no governed-ness read — when the write is not a real close:

    - ``requested_status`` is ``None`` (no status change),
    - governance is off,
    - the target status is not a done-category state (not a close),
    - the issue is already in a done-category state (done→done shuffle), or
    - the issue or target status cannot be resolved (the write's own
      transition validator will reject it with INVALID_TRANSITION / NOT_FOUND
      — the gate must not mask or pre-empt that error).

    Otherwise it delegates to :func:`evaluate_closure_gate`, which applies the
    governed-ness short-circuit and the fail-closed Legis policy.
    """
    if requested_status is None or not legis_client.is_configured():
        return _PROCEED
    try:
        issue = db.get_issue(issue_id)
        if db._resolve_status_category(issue.type, issue.status) == "done":
            return _PROCEED  # already closed — not a close transition
        if db._resolve_status_category(issue.type, requested_status) != "done":
            return _PROCEED  # target is not a done-category state
    except (KeyError, ValueError):
        return _PROCEED  # unknown issue/status — let the write validator reject it
    return evaluate_closure_gate(db, issue_id)


def _map_result(result: LegisGateResult) -> GateDecision:
    status = result.status
    if status in (LegisGateStatus.ALLOWED, LegisGateStatus.NOT_CONFIGURED):
        return _PROCEED
    if status is LegisGateStatus.BLOCKED:
        return GateDecision(GateOutcome.BLOCKED, result.reason or "Closure blocked by Legis governance")
    if status is LegisGateStatus.INTEGRITY_FAILURE:
        return GateDecision(GateOutcome.INTEGRITY_FAILURE, result.reason or "Legis binding ledger integrity failure")
    if status is LegisGateStatus.INVALID_RESPONSE:
        # Legis answered, but the answer broke the wire contract. Per-issue
        # fail-closed (CONTRACT_VIOLATION), NOT UNAVAILABLE: Legis is reachable, so
        # this must not flip the batch's legis_known_down short-circuit and starve
        # the remaining issues of their own gate evaluation.
        return GateDecision(
            GateOutcome.CONTRACT_VIOLATION,
            result.reason or "Legis returned a contract-violating response",
        )
    # NOT_ENABLED or UNREACHABLE for a governed issue → fail closed (DECISION 2).
    return GateDecision(GateOutcome.UNAVAILABLE, result.reason or "Governance backend unavailable")
