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
