# Roadmap — Filigree            Updated: 2026-06-16 (PDR-0004)

> Sequencing, WSJF / cost-of-delay, and dated forecasts are produced by
> /axiom-program-management. This file records bets as INTENT, not a delivery
> schedule. Do not compute WSJF here; hand the committed bet over for sequencing.

## Standing goals (owner frame, PDR-0003)
1. **Be the mature, popular "adult in the room"** of the Weft suite.
2. **Harden the interface surface to be the SEI backbone.**
3. **Add lightweight, agent-value features without bloating the suite.**
> 3.0.0 (ad-hoc API → SEI) is ready but its merge is a SYNCHRONISED federation
> push — owner-gated, escalate-first. The agent does NOT trigger it. Goal #3 is
> the productive lane while that coordination lands.

## Now  (committed, in-flight)
- **Agent broadcast board** (goal #3, PDR-0003) — a super-lightweight transitory
  bulletin: any agent posts a message; the SessionStart hook surfaces "recent
  broadcasts (last 30 min)" + "another agent broadcast a message!". Reply = post
  another broadcast. Reuses the observations TTL-mixin pattern + the hook context
  block. Anti-bloat guardrail in PDR-0003 is load-bearing. Canonical use case:
  transient deconfliction signal ("editing X, detected another agent — backing
  off"). · tracker: filigree-9927145adc (epic; T1–T5 filed) · metric: north-star
  (multi-agent handoff coordination)
- **Agent-systems DX — remove agent work-loop dead-ends** (PDR-0002, goal #1) —
  the maturity lane: fix agent-facing CLI+MCP defects — workflow correctness,
  `--actor` attribution, stats payload, deletion, doc/skill-pack drift, surface
  consolidation. NOT YET FALSIFIABLE — needs a success criterion via /write-prd.
  · tracker: filigree-18bd3b8c98 (epic) · metric: north-star
- **Federation / SEI tail — bounded guardrail-closure** (PDR-0002, goal #2) —
  finish the finding/scan contract seams 3.0.0 opened so the contract is not
  left half-open for sibling tools. Bounded: finish these, do not expand.
  · tracker: filigree-8f6a1599fb (X-4), filigree-af55859975 (X-5),
    filigree-b789da2a1e (X-6) · metric: federation-contract guardrail

## Next (shaped, decreasing certainty)
- **Deeper agent-systems primitives** — richer than defect-fixes: session/run
  checkpoints beyond free-form actor strings (filigree-c2009921cf), session
  evidence bundle export for agent runs (filigree-6549e739de), and the broader
  agent-systems effectiveness review (filigree-ed2ccaf10d). Build after the
  dead-ends are removed. · metric: north-star
- **Tabard consumer adapter — `verified_actor` enrich-only** (PDR-0004, goal #2) —
  the Filigree side of the actor-identity seam, built "for, not with" Tabard:
  additive `verified_actor` + assurance-tier metadata (T0/T1/T2), never load-
  bearing, degrades to free-text. Filigree owns the Tabard layer (claim=donning;
  principal=claim-on-ticket). Reframes/closes the cross-host half of
  filigree-81d3971467. GATED on the Tabard go/no-go spike (GO) and the hub-blessed
  canonical handle (weft-560f243c95) — do not lock a dialect.
  · tracker: filigree-81d3971467 · metric: federation-contract guardrail

## Later (directional bets, no order, no dates)
- **Suite standards** — shared cross-tool contracts: complete nested
  `.gitignore` per dot-dir (filigree-4ed8152630), multi-owner managed-block
  contract (filigree-bcbd4d66f), shared `doctor --fix` summary contract
  (filigree-9fbbca77ac).
- **Transport-bound actor identity verification** — close the gap where
  `--actor`/agent-id is asserted, not verified (filigree-81d3971467).
- **Migration & store residuals** — `migrate-registry --to local`
  (filigree-73d27aed7b), relocate backups/logs under the dot-dir, mark
  generated files as generated.
- **Project-declared entity schemas** — subsystems/ADRs/clusters as
  first-class declarable entities (filigree-8ed24e999).
