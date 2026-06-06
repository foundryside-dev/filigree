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
  offline. A 500 (tampered ledger) is ``INTEGRITY_FAILURE``. With
  ``LEGIS_URL`` unset, governance is OFF entirely and every close proceeds
  ("invisible until wanted").
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


def evaluate_closure_gate(db: _AssocReader, issue_id: str) -> GateDecision:
    """Decide whether *issue_id* may be closed.

    Short-circuits to ``PROCEED`` when governance is off, and again for
    ungoverned issues — only a governed issue triggers a network call.
    """
    if not legis_client.is_configured():
        return _PROCEED
    rows = db.list_entity_associations(make_issue_id(str(issue_id)))
    if not any(row.get("signature") for row in rows):
        return _PROCEED  # ungoverned — no network call (DECISION 1A)
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
    # NOT_ENABLED or UNREACHABLE for a governed issue → fail closed (DECISION 2).
    return GateDecision(GateOutcome.UNAVAILABLE, result.reason or "Governance backend unavailable")
