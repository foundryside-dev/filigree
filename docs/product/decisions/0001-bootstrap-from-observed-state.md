# PDR-0001 — Bootstrap the product workspace from observed state

Date: 2026-06-16   Status: accepted   Author: claude-filigree   Owner sign-off: yes (grant + north-star confirmed via /own-product)
Supersedes: —   Related: roadmap.md (Now/Next), metrics.md (north-star), vision.md (grant)

## Context
No product workspace existed (`docs/product/` absent). Filigree is a mature
product — v3.0.0 shipped launch-ready, 1114 issues done, 30 open, 0 in-flight —
so there was real direction to infer rather than invent. The workspace had to be
seeded from observed reality (README, pyproject, git log, the filigree tracker)
to give the next session a cold-resumable picture.

## Options considered
1. **Bootstrap from observed state, confirm only the irreducibly-human bits
    (grant + north-star) with the owner** — pro: fast, honest, grounded in what
    the repo actually shows; con: inferred bets may need correction.
2. **Interrogate the owner for vision/strategy/metrics from scratch** — pro:
    authoritative; con: ignores cheap, honest signal already in the repo and
    tracker; slower and more error-prone than reading.
3. **Do nothing / chat without writing files** — pro: zero commitment; con:
    defeats the purpose — ownership is stateful, the workspace IS the memory.

## The call
Option 1. Drafted the five artifacts from observed reality. Two genuinely
human-only inputs were confirmed with the owner via AskUserQuestion before being
written as authoritative: the **authority grant** (standard taxonomy — autonomous
within strategy, escalate the irreversible/outward-facing) and the **north-star
orientation** (agent work-loop completion, not external adoption or federation
health). All metric targets are written as falsifiable BOOTSTRAP PLACEHOLDERS.

## Rationale
Observed direction is cheaper and more honest than interrogation, and the repo
gave a strong signal: every recent commit is Weft-federation contract-seam work,
while the largest open epics are agent-systems DX — which is what the confirmed
north-star measures. Recording both honestly (federation seams as the in-flight
Now; agent DX as a Next candidate to pull forward) surfaces the real tension for
the owner to resolve as a DECIDE act, rather than silently picking one.

## Reversal trigger
Revisit this entire bootstrap once the owner confirms vision and the Now/Next
split, OR if the first real metrics reading shows the chosen north-star
(agent work-loop completion) is not the dimension the owner actually optimizes
for. Replace placeholder targets with owner-set numbers before any are used to
accept a bet.
