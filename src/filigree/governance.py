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

import logging
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Protocol

from filigree import legis_client
from filigree.legis_client import LegisGateResult, LegisGateStatus
from filigree.registry import RegistryUnavailableError, RegistryVersionMismatchError, is_loomweave_backend_unreachable
from filigree.types.core import LineageEvent, make_issue_id

logger = logging.getLogger(__name__)


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
    # The RED-1 drift check could not consult Loomweave for this issue (whole-
    # backend outage / version mismatch / batch caller already knew it was
    # down); the binding's freshness was UNKNOWN. Advisory only: it lets a batch
    # caller thread ``loomweave_known_down`` and bound a down Loomweave to one
    # probe per batch. Never affects ``allowed`` — Loomweave is enrich-only.
    loomweave_unavailable: bool = False
    # The orphan rename-hint fallback (``GET /identity/lineage/{sei}``) hit a
    # connectivity-class failure AFTER the primary by-SEI channel answered, so
    # ``lineage_hints`` may be incomplete. Kept SEPARATE from
    # ``loomweave_unavailable`` on purpose: the primary answer milliseconds
    # earlier is direct evidence Loomweave is up, and a batch caller reads
    # ``loomweave_unavailable`` as known-down — folding this in would skip every
    # later issue's drift probe and let a drifted binding auto-close. Purely
    # informational; never affects ``allowed``.
    lineage_unavailable: bool = False
    # Rename hints for governed bindings whose SEI Loomweave reported orphaned
    # (``alive:false``) during the RED-1 drift check, keyed by the bound entity
    # id: the SEI's latest lineage event, so an agent can re-bind to
    # ``new_locator`` instead of hitting a dead end (filigree-4e13d133f7). On a
    # non-PROCEED verdict the same hint is also appended to ``reason`` (the only
    # channel every close surface renders). Advisory only: never affects
    # ``allowed`` or the outcome — an orphaned binding stays freshness UNKNOWN.
    lineage_hints: dict[str, LineageEvent] = field(default_factory=dict)

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


def _row_entity_id(row: Any) -> str:
    """The opaque entity id a binding row points at (forward or by-entity row)."""
    return str(row.get("loomweave_entity_id") or row.get("entity_id") or "")


@dataclass(frozen=True)
class _DriftCheck:
    """Result of :func:`_evaluate_current_drift`.

    ``decision`` is a ``STALE`` verdict (current code drifted) or ``None`` (no
    known drift — proceed to the Legis gate). ``loomweave_unavailable`` is True
    only when the PRIMARY probe proved the backend down: a connectivity-class
    or retried-out gateway-5xx ``RegistryUnavailableError``
    (:func:`is_loomweave_backend_unreachable`), a version mismatch, or a
    caller-supplied known-down; it is False on the no-resolver path (local
    mode — no network happened), on per-entity ``unresolved`` degrades, and on
    a ``RegistryUnavailableError`` Loomweave answered with (a deterministic
    4xx / plain 500 / auth / malformed body — per-issue UNKNOWN only).
    ``lineage_unavailable`` relays the resolver's advisory of the same name
    (the enrich-only rename-hint fallback failed after the primary channel
    answered) and is deliberately NOT folded into ``loomweave_unavailable`` —
    see :attr:`GateDecision.lineage_unavailable`. ``lineage_hints`` carries the
    registry's rename hint for each UNKNOWN (orphaned) binding that has one —
    see :attr:`GateDecision.lineage_hints`; empty when Loomweave was not
    consulted or reported no lineage.
    """

    decision: GateDecision | None
    loomweave_unavailable: bool
    lineage_hints: dict[str, LineageEvent] = field(default_factory=dict)
    lineage_unavailable: bool = False


def _format_lineage_hints(hints: dict[str, LineageEvent]) -> str:
    """Render rename hints for a human/agent-readable message suffix.

    ``<entity> -> <new_locator> (<event>)`` when the latest event names a new
    locator, else ``<entity>: <event>`` (e.g. a ``died`` event with no target).
    """
    parts: list[str] = []
    for entity_id, hint in hints.items():
        new_locator = hint.get("new_locator")
        event = hint.get("event", "")
        parts.append(f"{entity_id} -> {new_locator} ({event})" if new_locator else f"{entity_id}: {event}")
    return "rename lineage: " + ", ".join(parts)


def _with_lineage_hints(decision: GateDecision, hints: dict[str, LineageEvent]) -> GateDecision:
    """Stamp *hints* on a copy of *decision* (``_PROCEED`` is a shared singleton).

    A non-PROCEED reason gains the rendered suffix so the re-bind target reaches
    the agent through the one channel every close surface prints; a PROCEED
    keeps ``reason`` empty (it is the "why not allowed" channel) and carries the
    hint as data only. The outcome is never changed.
    """
    if not hints:
        return decision
    reason = decision.reason
    if decision.outcome is not GateOutcome.PROCEED:
        suffix = _format_lineage_hints(hints)
        reason = f"{reason}; {suffix}" if reason else suffix
    return replace(decision, reason=reason, lineage_hints=dict(hints))


def _evaluate_current_drift(db: Any, issue_id: str, signed_rows: list[Any], *, loomweave_known_down: bool = False) -> _DriftCheck:
    """Compare each governed binding's CURRENT content against its attach snapshot.

    RED-1: the sign-off-snapshot staleness check (``_signed_row_is_stale``) only
    catches a *re-attach* that advanced ``content_hash_at_attach`` past the signed
    snapshot. It cannot catch the bound CODE drifting while nobody re-attaches —
    then ``content_hash_at_attach`` stays frozen at (and equal to) the signed
    snapshot, so the close was waved through despite the code having moved on.

    Filigree owns this comparison (ADR-029 Decision 3; hub ruling 2026-06-29):
    resolve each governed binding's CURRENT ``content_hash`` from the Loomweave
    registry consumer and compare it to ``content_hash_at_attach``.

    Returns a :class:`_DriftCheck` whose ``decision`` is a ``STALE``
    :class:`GateDecision` if ANY governed binding's current content differs from
    its attach snapshot; otherwise ``None`` (no current-code drift — the caller
    proceeds to the existing Legis gate). The check is **enrich-only**: when
    Loomweave is unreachable / unsupported (no registry, no resolver surface,
    availability error) or an individual entity is unresolvable (orphaned /
    not_found / invalid), the binding's freshness is a discriminated UNKNOWN —
    logged, never silently treated as fresh, and never a hard block. The core
    close must not become load-bearing on Loomweave.

    An orphaned SEI is still UNKNOWN, but when the resolver carries a
    ``lineage_hints`` entry for it (the SEI's latest Loomweave lineage event —
    a ``NotRequired`` key, absent from legacy producers) the hint is relayed:
    on the ``entity_unresolved`` log record (``lineage_hints`` extra + a
    ``rename lineage: <sei> -> <new_locator>`` suffix) and on the returned
    check's ``lineage_hints`` for the decision (filigree-4e13d133f7).

    ``loomweave_known_down`` (batch callers): skip the resolver call because an
    earlier issue in the same sweep already proved Loomweave down — freshness is
    UNKNOWN for this issue without re-incurring the resolver's retry budget. The
    check's ``loomweave_unavailable`` reports True for that case and for a
    whole-backend failure raised by the resolver, so the caller can set the flag
    for the rest of its batch. Whole-backend means input-independent: a
    ``RegistryUnavailableError`` that proves the backend down
    (:func:`is_loomweave_backend_unreachable` — connectivity-class, the request
    got no answer; or a retried-out gateway 502/503/504, a proxy answering for
    a dead upstream) or a version mismatch. A ``RegistryUnavailableError``
    Loomweave ANSWERED with (a deterministic 4xx such as 413 for THIS issue's
    oversize locator, a plain 500, an auth refusal, a malformed body) degrades
    only this issue to UNKNOWN and leaves the flag False — otherwise one
    issue's bad input would skip every later issue's probe and let a drifted
    binding auto-close. The resolver's advisory ``lineage_unavailable`` (the
    enrich-only rename-hint fallback failed AFTER the primary channel answered)
    is relayed on the check's own ``lineage_unavailable`` and never counted
    toward the batch bound — a successful primary read is direct evidence
    Loomweave is up.
    """
    resolver = getattr(getattr(db, "registry", None), "resolve_entity_content_hashes", None)
    if resolver is None:
        # Local-mode / injected fake / pre-surface fallback registry: cannot
        # determine current-code drift. Flag UNKNOWN, do not block.
        logger.info(
            "closure-gate drift check: no Loomweave resolver; entity freshness UNKNOWN (enrich-only, close not blocked on this axis)",
            extra={"issue_id": issue_id, "freshness": "unknown", "reason": "no_resolver"},
        )
        return _DriftCheck(None, False)

    if loomweave_known_down:
        # An earlier issue in this batch already proved Loomweave down — do not
        # re-incur the resolver's deadline/retry budget. Freshness UNKNOWN, and
        # (unlike Legis) the issue still proceeds to its own Legis verdict.
        logger.info(
            "closure-gate drift check: Loomweave unavailable earlier in this batch; entity freshness UNKNOWN "
            "(enrich-only, close not blocked on this axis)",
            extra={"issue_id": issue_id, "freshness": "unknown", "reason": "registry_unavailable_earlier_in_batch"},
        )
        return _DriftCheck(None, True)

    entity_ids = [_row_entity_id(row) for row in signed_rows if _row_entity_id(row)]
    try:
        resolution = resolver(entity_ids)
    except RegistryVersionMismatchError as exc:
        logger.warning(
            "closure-gate drift check: Loomweave unavailable; entity freshness UNKNOWN (enrich-only, close not blocked on this axis)",
            extra={"issue_id": issue_id, "freshness": "unknown", "reason": "registry_unavailable", "detail": str(exc)},
        )
        return _DriftCheck(None, True)
    except RegistryUnavailableError as exc:
        backend_down = is_loomweave_backend_unreachable(exc)
        if backend_down:
            logger.warning(
                "closure-gate drift check: Loomweave unavailable; entity freshness UNKNOWN (enrich-only, close not blocked on this axis)",
                extra={
                    "issue_id": issue_id,
                    "freshness": "unknown",
                    "reason": "registry_unavailable",
                    "cause_kind": exc.cause_kind,
                    "status_code": exc.status_code,
                    "detail": str(exc),
                },
            )
        else:
            logger.warning(
                "closure-gate drift check: Loomweave rejected this issue's resolve request; entity freshness UNKNOWN "
                "(enrich-only, close not blocked on this axis; Loomweave answered, so later issues in a batch are still probed)",
                extra={
                    "issue_id": issue_id,
                    "freshness": "unknown",
                    "reason": "registry_request_rejected",
                    "cause_kind": exc.cause_kind,
                    "status_code": exc.status_code,
                    "detail": str(exc),
                },
            )
        return _DriftCheck(None, backend_down)

    # The orphan rename-hint fallback hit a connectivity-class failure (advisory
    # key; absent from legacy producers). The primary channels answered, so the
    # comparison below stands and — crucially — this is NOT a known-down signal
    # for the batch: it rides on the check's separate ``lineage_unavailable``.
    lineage_unavailable = resolution.get("lineage_unavailable") is True
    if lineage_unavailable:
        logger.info(
            "closure-gate drift check: Loomweave lineage route unreachable (rename hints unavailable; advisory only)",
            extra={"issue_id": issue_id, "reason": "lineage_unavailable"},
        )
    resolved: dict[str, str] = dict(resolution.get("resolved", {}))
    all_hints: dict[str, LineageEvent] = dict(resolution.get("lineage_hints") or {})
    drifted: list[str] = []
    unknown: list[str] = []
    for row in signed_rows:
        entity_id = _row_entity_id(row)
        if not entity_id:
            continue
        current = resolved.get(entity_id)
        attach = row.get("content_hash_at_attach")
        if current is None:
            unknown.append(entity_id)
        elif attach is not None and current != attach:
            drifted.append(entity_id)
    lineage_hints = {eid: all_hints[eid] for eid in unknown if eid in all_hints}
    if unknown:
        message = "closure-gate drift check: entity content unresolvable; freshness UNKNOWN (enrich-only, close not blocked on this axis)"
        if lineage_hints:
            message = f"{message}; {_format_lineage_hints(lineage_hints)}"
        logger.warning(
            message,
            extra={
                "issue_id": issue_id,
                "freshness": "unknown",
                "reason": "entity_unresolved",
                "entity_ids": unknown,
                "lineage_hints": lineage_hints,
            },
        )
    if drifted:
        return _DriftCheck(
            GateDecision(
                GateOutcome.STALE,
                "entity content drifted since attach (current code no longer matches content at attach); awaiting re-attest",
            ),
            False,
            lineage_hints,
            lineage_unavailable=lineage_unavailable,
        )
    return _DriftCheck(None, False, lineage_hints, lineage_unavailable=lineage_unavailable)


def evaluate_closure_gate(
    db: _AssocReader,
    issue_id: str,
    *,
    legis_known_down: bool = False,
    loomweave_known_down: bool = False,
) -> GateDecision:
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

    ``loomweave_known_down`` is the same bound for the RED-1 drift probe: once
    an earlier issue in the batch proved Loomweave down, the per-issue resolver
    call (and its retry budget) is skipped. Unlike Legis it is **enrich-only**:
    the issue still gets its own Legis verdict — only the drift probe is skipped,
    freshness is UNKNOWN (logged), and the resulting decision reports
    ``loomweave_unavailable=True`` so the caller can keep the flag set. It is
    applied inside the drift helper at the resolver call — after the
    ungoverned and snapshot-STALE short-circuits, before the
    ``legis_known_down`` short-circuit — so a drifted sign-off is never masked.
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
    check = _evaluate_current_drift(db, str(issue_id), signed_rows, loomweave_known_down=loomweave_known_down)
    if check.decision is not None:
        # Current code has moved on since the binding was attached — fail closed
        # as STALE, like the sign-off-snapshot drift above. This runs BEFORE the
        # legis_known_down short-circuit (same load-bearing ordering as the
        # snapshot check): a drifted binding must report STALE, never be masked
        # as a transient UNAVAILABLE. A Loomweave outage does NOT reach here as a
        # block — it degrades to UNKNOWN inside the helper (enrich-only).
        # loomweave_known_down is likewise applied INSIDE the helper, at the
        # resolver call — after the ungoverned and snapshot-STALE short-circuits,
        # before the legis_known_down short-circuit. The advisory
        # ``lineage_unavailable`` still rides on a STALE verdict (the lineage
        # fallback can be unreachable while the drift comparison succeeded).
        stale = check.decision
        if check.lineage_unavailable:
            stale = replace(stale, lineage_unavailable=True)
        return _with_lineage_hints(stale, check.lineage_hints)
    if legis_known_down:
        # A governed, non-stale issue needs a Legis round-trip, but a prior issue
        # in this batch already proved Legis unreachable — fail closed without
        # re-incurring the timeout (DECISION 2).
        decision = GateDecision(GateOutcome.UNAVAILABLE, "Legis unreachable earlier in this batch")
    else:
        decision = _map_result(check_closure_gate(str(issue_id)))
    # Stamp the advisory flags / rename hints on a copy — ``_PROCEED`` is a
    # shared singleton.
    if check.loomweave_unavailable or check.lineage_unavailable:
        decision = replace(
            decision,
            loomweave_unavailable=decision.loomweave_unavailable or check.loomweave_unavailable,
            lineage_unavailable=decision.lineage_unavailable or check.lineage_unavailable,
        )
    return _with_lineage_hints(decision, check.lineage_hints)


def evaluate_status_change_gate(db: _StatusGateReader, issue_id: str, requested_status: str | None) -> GateDecision:
    """Decide whether a status *write* that would close *issue_id* may proceed.

    ``close_issue`` delegates to ``update_issue`` (same template validator,
    same data-layer write), so ``update_issue``/``batch_update`` can drive a
    governed issue into a done-category status. Those surfaces historically
    skipped the gate — an ungated close-equivalent. This mirrors
    :func:`evaluate_closure_gate` for them, and is the single decision every
    status-write surface (MCP/HTTP/weft/CLI, single and batch) routes through
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
