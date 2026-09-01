# PDR-0002 — Agent-systems DX is the Now bet; federation tail is bounded guardrail-closure

Date: 2026-06-16   Status: accepted   Author: claude-filigree   Owner sign-off: n/a (reprioritization is within the standard grant)
Supersedes: —   Related: PDR-0001, roadmap.md (Now/Next), metrics.md (north-star), vision.md (grant)

## Context
Bootstrap (PDR-0001) recorded a tension instead of resolving it: observed build
direction is the Weft federation contract seams (every recent commit), but the
owner-confirmed north-star is agent work-loop completion, which the Agent-systems
DX epics move most directly. The owner asked to settle the Now-vs-Next ordering
so the session can move into DECIDE. Nothing is claimed in the tracker (wip = 0),
so this reorders intent, not in-progress work.

## Options considered
1. **Federation-first stays Now** — pro: momentum, partial completion, finishing
    avoids a half-open contract; con: it serves sibling tools (secondary users),
    not the primary user (agents running work loops); choosing it because it is
    in motion is the feature-factory trap — motion is not the metric.
2. **Agent-systems DX becomes Now; federation tail demoted to bounded
    guardrail-closure** — pro: it is the only open work that directly moves the
    confirmed north-star; the federation tail still gets finished, just not as
    the strategic bet; con: pivots away from in-flight momentum.
3. **Run both as co-equal Now bets** — pro: nobody loses; con: two strategic
    bets at once is how focus dies; "do both" is the feature factory's answer.

## The call
Option 2. **Agent-systems DX is the Now bet**, led by the Toolkit DX epic
(filigree-18bd3b8c98: agent-facing CLI+MCP defect fixes — workflow correctness,
--actor attribution, stats, deletion, doc drift). The **federation tail
(X-4 filigree-8f6a1599fb, X-5 filigree-af55859975, X-6 filigree-b789da2a1e) is
reclassified to bounded guardrail-closure**: finish it because a half-open
contract regresses sibling tools, but do not let it expand into the strategic
bet. The **deeper agent-systems primitives** (session/run checkpoints
filigree-c2009921cf, session evidence bundle export filigree-6549e739de) move to
Next — fix dead-ends first, build richer primitives after.

## Rationale
"What is being built" (federation) is not "what moves the metric" (north-star).
The whole point of confirming the north-star as agent work-loop completion is to
order Now by it. The federation seams advance a guardrail (no sibling-tool
regressions), which is a thing to protect, not the bet to chase. Removing
agent-loop dead-ends is the bet that, if it works, moves the number the owner
said they care about.

## Reversal trigger
Revisit if EITHER: (a) shaping the Toolkit DX epic into a PRD shows the F1–F7
defects do not actually block an agent work loop (i.e. they are cosmetic) — then
demote it and promote the agent-systems primitives or the federation finish; OR
(b) once the north-star is instrumented, the baseline agent work-loop completion
rate is already ≥ 95% — then DX is not worth a Now slot and federation/primitives
take it. Metric-bound; kill/keep logic in product-metrics-and-experimentation.md.
