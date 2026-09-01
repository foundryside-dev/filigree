# Metrics — Filigree             Last read: 2026-06-16

> Targets below are BOOTSTRAP PLACEHOLDERS. Each carries a number and a date so
> it is falsifiable, but the numbers are inferred, not owner-set. Replace
> BASELINE/TARGET with real readings before using any of these to accept a bet
> or fire a PDR reversal trigger. Experiment/instrumentation/kill logic lives in
> product-metrics-and-experimentation.md.

## North-star
Agent work-loop completion: an agent can go claim → work → close without hitting
a dead-end (INVALID_TRANSITION trap, schema mismatch, or a forced manual CLI
fallback the MCP surface should have covered).

| Metric | Target (falsifiable) | Current | Read on | Trend |
|--------|----------------------|---------|---------|-------|
| Agent work-loop completion rate (claim→close, no dead-end) | ≥ 95% by 2026-09-30 | BASELINE unset | 2026-06-16 | — |

## Input metrics (the levers that move the north-star)
| Metric | Target | Current | Read on |
|--------|--------|---------|---------|
| Dogfood defects per session that block a work loop | ≤ 1 by 2026-09-30 | BASELINE unset (recent dogfood rounds found multi-bug bundles) | 2026-06-16 |
| MCP/CLI verb-grammar parity gaps (a verb reachable one surface, not the other) | 0 by 2026-09-30 | open: ≥1 (filigree-4c73f6cf22) | 2026-06-16 |
| Agent-facing Toolkit-DX defects (F1–F7 epic) open | 0 by 2026-09-30 | epic filigree-18bd3b8c98 open | 2026-06-16 |
| Broadcast-board adoption — distinct agent actors posting per 4-week window (PDR-0003 / PRD-0001) | ≥ 5 within 28 days of MVP release | BASELINE 0 (not shipped) | 2026-06-16 |

## Guardrails (must NOT degrade)
| Metric | Floor / ceiling | Current | Read on |
|--------|-----------------|---------|---------|
| Schema-mismatch incidents hitting a live agent | ≤ 0 unhandled / release | 1 observed this session (installed v27 vs DB v28; handled — dogfood artifact) | 2026-06-16 |
| Federation-contract regressions (sibling tool breaks on a Filigree change) | 0 / release | BASELINE unset | 2026-06-16 |
| Broadcast-board signal-to-noise — reflexive-ack ratio (PRD-0001): replies that add no coordination value ÷ total posts | ≤ 0.3 over the adoption window | BASELINE 0 (not shipped) | 2026-06-16 |
| CI gate (ruff + format + mypy + pytest) | green on every merge to release/main | green (last: 095bfd0) | 2026-06-16 |
| Agent-reported defects per release | ≤ TARGET (owner to set) | BASELINE unset | 2026-06-16 |
