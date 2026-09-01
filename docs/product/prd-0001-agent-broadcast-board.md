# PRD-0001 — Agent broadcast board            Status: ready-for-planning
Decision: PDR-0003   Bet (roadmap.md): Now (goal #3)   Target metric (metrics.md): Broadcast-board adoption

## Problem
**Who:** AI coding agents working a Filigree project across sessions, or as a
parallel fleet, where more than one agent may touch the same repo.
**The problem (their pain):** agents have no *transient* channel to signal each
other. The canonical case is deconfliction — an agent realizes "I was editing
`<module>` and another agent is active in this repo; I should back off until we
deconflict" — but there is nowhere lightweight to say it. Issues and comments are
permanent artifacts and the wrong weight; observations are scoped to *incidental
defects*, not agent-to-agent coordination. So the signal is simply lost, and
agents collide or duplicate work.
**Desired outcome:** an agent can drop a short, expiring note that the next agent
to orient in that project reliably sees — and can act on or ignore — without
creating a durable artifact or a reply obligation.
**Why now:** 3.0.0's SEI cutover is gated on a synchronised, owner-approved
federation push, so the productive lane is goal #3 (lightweight agent-value
features). The board reuses two patterns Filigree already ships (the observations
TTL mixin + the SessionStart hook context block), so it is small, and it has a
sleeper-hit profile in the mold of observations.

## Success metric (the signal the bet paid off)
**Broadcast-board adoption** (metrics.md): distinct agent actors posting a
broadcast per 4-week window. BASELINE 0 (not shipped) → TARGET ≥ 5 distinct
actors within 28 days of MVP release. This is the falsification condition: a
coordination primitive that agents do not use has not paid off, exactly as the
observe analog would be judged.

## Acceptance criteria (falsifiable)
1. **SUCCESS — adoption.** ≥ 5 distinct agent actors post a broadcast within 28
   days of MVP release, measured by the board's own post/actor counts (T5).
   *Reject branch:* < 3 total posts **or** < 2 distinct actors at 28 days → bet
   falsified; freeze the primitive, do not build the polish phase, open a
   follow-up PDR (matches PDR-0003 reversal trigger).
2. **GUARDRAIL — signal-to-noise.** Reflexive-ack ratio (replies adding no
   coordination value ÷ total posts) stays ≤ 0.3 over the adoption window.
   *Reject branch:* > 0.3 → the relevance-gate failed; the board is generating
   noise — bet rejected even if (1) passes; revisit the delivery briefing.
3. **GUARDRAIL — CI green (metrics.md).** ruff + format + mypy + pytest stay
   green on every merge carrying this work.
   *Reject branch:* any gate red at merge → not shippable; criterion unmet.
4. **SCOPE — full agent surface at parity.** At MVP, post + list are reachable
   over **both** MCP and CLI with identical verb grammar, and the SessionStart
   hook surfaces recent broadcasts — not a subset of surfaces.
   *Reject branch:* any surface missing, or MCP/CLI grammar diverges → criterion
   unmet (this is the verb-grammar-mismatch defect class; do not reopen it).

## Non-goals (this bet)
- **Per-recipient addressing / direct messages.** Broadcast-only.
- **Threading, mentions, read-receipts, notifications, attachments, rich text.**
  All explicitly out — they are the chat-platform anti-goal (vision.md).
- **A new agent-identity system.** The board *consumes* an agent handle; it does
  not build identity. Unforgeable transport-bound identity is owned by
  filigree-81d3971467, not this bet.
- **Persistence beyond the window.** Messages are transient; no archive/history
  surface. (Audit-trail needs belong in issues/events, not here.)
- **Dashboard polish.** The read-only dashboard strip is Phase 1, gated on
  criterion (1) passing.

## Constraints & guardrails
- **Anti-bloat (PDR-0003, load-bearing):** 30-minute default window with a hard
  cap; message-body size cap; project-scoped to one board per DB.
- **Relevance-gated delivery (PDR-0003, load-bearing):** the hook block and the
  tool/CLI descriptions MUST brief receiving agents that a reply is OPTIONAL —
  read it, reply only if relevant to your work. This is the control that keeps
  criterion (2) satisfiable; it is part of the feature, not documentation.
- **Best-effort delivery:** a board error must never block session-context
  generation (mirror the observation-stats block's failure handling).
- **Agent handle quality:** distinct-actor counting (criterion 1) is only as good
  as the handle the board records — see the assumption below.

## Identity model (revised per Tabard hub note 2026-06-16; see PDR-0004)
The board consumes the **structured Body coordination key** — `model · ticket ·
hostname · pid · process_start_time (· nonce)` as **comparable fields, NOT
hashed** (hashing destroys the legibility collision-by-comparison needs). The
board IS the hub note's "mechanism C": deconfliction falls out of field
comparison — *same ticket + different body = a second body on your task →
contention/deconflict*; *different ticket = orthogonal → skip*. This ships the
Body+Tabard coordination win **with zero dependency on the Seal** (Tabard's
crypto). The opaque *certified principal* and the offline-verifiable seal are
Tabard's, not the board's — the board must never let its cheap legible handle
become the certified principal.

> GOVERNANCE (hub note #6, weft-560f243c95): the exact handle field-set is a
> hub-blessed cross-member seam. The board consumes the canonical dialect the hub
> ratifies — it does NOT lock a third dialect. This PRD's distinct-actor metric
> assumes the robust field-set; final shape defers to the hub.

## Open questions / assumptions
- **ASSUMPTION (headline): the structured Body key distinguishes cooperating
  agents.** Distinct-actor counting (criterion 1) compares Body keys, not a hash.
  If two genuinely different bodies collide on all fields, the count is invalid.
  Forgery-resistance is explicitly NOT attempted here — that is the Seal (Tabard,
  T2) and the launch-bound tier (filigree-81d3971467, T1), both out of scope.
- **OPEN:** the unassigned-agent edge — a body with no claim has a Body key but no
  Tabard (ticket). The board still signals at Body grain (two bodies, same repo).
  This is live input to the hub's pending ruling, not resolved here.
- **OPEN:** instrumentation for criterion (2)'s "reflexive-ack" — needs a cheap,
  honest definition of a no-value reply (likely heuristic, flagged as such).
- **ASSUMPTION:** the 30-minute window is right for deconfliction latency; tune
  if dogfooding shows agents miss signals between sessions.

## Handoff
- **Top item → /axiom-planning:** **T1 — `broadcasts` table + `db_broadcasts.py`
  mixin** (filigree-fbc9410ded). Everything depends on it; it is the
  codebase-validated plan's first unit.
- **Solution shape → /axiom-solution-architect:** the agent-handle composition
  and nonce-minting point (the headline assumption), the window-sweep approach,
  and the hook-block format. The PRD names the constraints; the design picks the
  shape.
- **Tracker:** epic filigree-9927145adc; children T1 filigree-fbc9410ded, T2
  filigree-0d0e64292e (MCP), T3 filigree-209926b6e4 (CLI), T4 filigree-c5a365a9be
  (hook), T5 filigree-601c5c8262 (instrumentation, fires the kill criterion).
- **Forecast:** the dated commitment comes from /axiom-program-management, not
  this PRD.
