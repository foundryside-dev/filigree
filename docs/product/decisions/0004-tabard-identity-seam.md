# PDR-0004 — Filigree↔Tabard actor-identity seam: build for, not with

Date: 2026-06-16   Status: accepted   Author: claude-filigree   Owner sign-off: PARTIAL — Filigree-side build is in-grant; the hub-ratified handle, doctrine §2/§6 amendment, and Tabard admission are owner/hub-reserved (escalated, not decided here)
Supersedes: —   Related: PDR-0003, PRD-0001, filigree-81d3971467, filigree-c2009921cf, tabard/docs/product/ (vision, roadmap, hub-note 2026-06-16), weft-560f243c95

## Context
Tabard (candidate, incubating at /home/john/tabard) is standing up as the Weft
actor-identity authority: the cryptographic counterpart to SEI — SEI keys *what*
changed, Tabard keys *who*. Its founding reframe: durable identity belongs to the
**task**; an agent inherits it **by assignment** (dons the tabard); Tabard
**attests** that binding (the "Seal"), offline-verifiable, enrich-only, never
gating. Owner direction: Filigree builds "**for but not with** Tabard" — build
everything assuming a future identity provider hands an agent a usable, eventually-
attestable identifier, without coupling to Tabard's unfinished crypto.

Critically, the layered model is Body / Tabard / Seal, and **Filigree already owns
the Tabard layer**: `work_claim --assignee` / `work_start` = donning,
`close`/`release` = doffing (Tabard hub note #4). So Filigree is not a passive
consumer — it is the source of the assignment binding Tabard attests, and the
first consumer that can ship the coordination win without the Seal existing.

## Options considered
1. **Solve agent identity locally in Filigree** (finish filigree-81d3971467 as a
    full transport-bound scheme) — pro: no external dependency; con: forks a rival
    identity authority, fragments the suite, violates "be the adult in the room"
    and the hub's one-blessed-seam rule. Rejected.
2. **Wait for Tabard before building anything identity-shaped** — pro: no rework;
    con: Tabard is a go/no-go spike that may NO-GO; blocks the board and the
    enrich-only interface on an unproven crypto bet. Rejected.
3. **Build the Filigree-side seam now against an enrich-only interface; consume
    Tabard when/if it lands ("for, not with")** — pro: ships value today, de-risks
    Tabard by proving Body+Tabard coordination in production, degrades cleanly if
    Tabard NO-GOes; con: must respect the hub-blessed handle and not lock a
    dialect. CHOSEN.

## The call
Filigree builds the **consumer-side seam now**, for-not-with Tabard:

1. **Own the Tabard layer precisely.** Define the durable principal unit as **the
    claim on a ticket** — `work_claim` mints it, the lease is its lifespan,
    `close`/`release` doffs it. This is the concrete, already-built thing Tabard's
    spike can attest, and Filigree's contribution to its decisive open question
    (assignment-as-principal vs intra-fleet forgeability).
2. **Altitude (hub note #5): principal = claim (ticket grain); continuity = the
    enclosing epic / `cluster:` label as launch-context-style addressing, never
    the principal.** One principal grain, desk as a continuity attribute — resolves
    #5 without a two-level principal, using `parent_issue_id` + label namespaces
    that already exist.
3. **Build `verified_actor` as an enrich-only interface** — an additive field +
    assurance-tier metadata (T0 free-text / T1 launch-bound / T2 signed). NEVER
    load-bearing: absence degrades to free-text with `identity_stable:false`;
    `attest()` success is never a precondition for any write/claim (Tabard's
    mandatory F4-prevention clause). T2 is the slot Tabard fills offline later.
    This closes the cross-host half of **filigree-81d3971467**, which is reframed
    from "Filigree builds transport-bound identity" to "Filigree is the Tabard
    consumer adapter" (its local launch-bound T1 half remains a Legis-style
    capability).
4. **The broadcast board (PRD-0001) is the first/lowest-stakes consumer** — it
    ships the Body+Tabard coordination win (collision-by-comparison on the
    structured Body key) decoupled from the Seal. Correct Tabard's roadmap note
    that the board is "blocked on" the handle: it is NOT blocked; it is the pilot
    that de-risks the handle and the enrich-only degrade before the crypto exists.

## Rationale
Building for-not-with lets Filigree ship real multi-agent coordination now while
staying the adult in the room: it consumes the identity authority rather than
forking one, and it feeds the hardest part of Tabard's spike (a real assignment
primitive to attest) instead of theorising. Enrich-only + degrade-to-free-text
means a Tabard NO-GO costs Filigree nothing load-bearing — the worst case is the
board keeps running on Body-tier coordination, which is already useful.

## Escalations (NOT decided here — owner/hub-reserved)
- The **canonical handle field-set** is a hub-blessed seam (weft-560f243c95);
  Filigree consumes it and must NOT lock a third dialect. Loop the hub before any
  handle lock in PRD-0001.
- The doctrine **§2/§6 amendment** (singular "identity authority" → code-vs-actor
  re-scope) and **Tabard admission (§7)** are owner-filed; Filigree neither files
  nor presumes them.

## Reversal trigger
Revisit if Tabard NO-GOes the certification model (then `verified_actor` stays at
T0/T1 permanently and the board runs on Body-tier coordination — no Filigree
rework needed, which is the point), OR if the hub ratifies a canonical handle
whose field-set the board's distinct-actor metric cannot consume (then re-shape
PRD-0001's metric). Metric-bound via the board's adoption reading (PRD-0001).
