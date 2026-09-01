# PDR-0003 — Three-goal frame; agent broadcast board is the goal-#3 lead bet

Date: 2026-06-16   Status: accepted   Author: claude-filigree   Owner sign-off: yes (owner-directed strategy this session)
Supersedes: —   Related: PDR-0002, vision.md, roadmap.md (Now), metrics.md (north-star)

## Context
Owner set the standing strategic frame for Filigree's current era: (1) be the
mature, popular "adult in the room" of the Weft suite; (2) maintain and harden
the interface surface to be the SEI backbone; (3) add cool, lightweight,
agent-value features without bloating the suite. 3.0.0 is ready but replaces the
ad-hoc API with SEI, so its merge must be a SYNCHRONISED federation push — an
owner-gated, coordinated release (escalate-first per the grant). While that
coordination lands, goal #3 is the productive lane. Owner's first #3 candidate:
an agent broadcast board — a transitory bulletin where any agent posts a message
and other agents in the project see recent posts via a session hook. Owner reads
it as a sleeper hit in the mold of observations.

Canonical motivating example (owner): an agent posts "I was editing <module> and
detected another agent working in the repo — backing off until we can
deconflict." This is the load-bearing use case: transient deconfliction signal
between agents, which fits the Weft suite's deconfliction-first design. The board
is the lightweight channel that signal needs and that issues/observations are the
wrong shape for.

## Options considered
1. **Scoped/addressed messaging** (per-recipient, threads, read-state) — pro:
    powerful; con: bloats toward a chat platform, violates goal #1 (don't bloat)
    and the vision anti-goal (not a human-first messaging suite). Rejected by
    owner mid-shaping.
2. **Super-lightweight broadcast feed** — a project-scoped board; post a
    message; a ~30-minute time window; the SessionStart hook surfaces "recent
    broadcasts (last 30 min)" and flags "another agent broadcast a message!".
    Reply = just post another broadcast. Pro: recombines two patterns Filigree
    already ships (observations' TTL mixin + the hook context block); genuinely
    lightweight; cheap to kill. CHOSEN.
3. **Do nothing in #3; wait on the 3.0.0 push** — pro: zero risk; con: wastes
    the coordination window and forgoes a possible sleeper hit.

## The call
Adopt the three-goal frame. Build the **agent broadcast board (Option 2)** as
the lead Now bet for the goal-#3 lane this session. PDR-0002's Now bets persist
as the goal-#1 (Toolkit DX maturity) and goal-#2 (federation/SEI tail) lanes;
this does not supersede them, it adds the #3 lane that is actually buildable
while the SEI merge is owner-gated. The 3.0.0 synchronised push stays
escalate-first — the agent does not trigger it.

## Rationale
The board is the smallest real version of the owner's idea and reuses proven
seams, so it honours goal #3 (lightweight, no bloat) by construction. It serves
the north-star (agent work-loop completion) through multi-agent coordination:
fewer dropped handoffs when agents can leave each other a transient note. Like
observations, its success is whether agents actually use it — so it is cheap to
validate and cheap to kill, which is exactly the risk profile a sleeper-hit bet
should have.

## Anti-bloat guardrail (goal #1, load-bearing)
The board MUST stay a thin coordination primitive. Hard constraints: a default
30-minute window with a hard cap; a message-body size cap; no attachments, no
rich formatting, no per-recipient addressing, no read-receipts, no notification
infrastructure. If the design starts growing threading depth, mentions, or
push-delivery, STOP — that is the chat-platform anti-goal, not this bet.

## Relevance-gated response (load-bearing UX constraint)
The delivery (hook context block + tool/CLI descriptions) MUST brief receiving
agents that broadcasts are FYI and a reply is OPTIONAL — respond only if it is
relevant to do so. This is not cosmetic: without it, every agent feels obliged
to answer every broadcast, the board fills with reflexive acks, and it collapses
into the chat-platform anti-goal. "Read it; reply only if it matters to your
work" is the contract, and it is part of the feature, not a doc afterthought.

## Reversal trigger
Within 4 weeks of the MVP landing in real (dogfood or external) use: if fewer
than 3 broadcasts are posted, or fewer than 2 distinct agent actors ever post,
the sleeper-hit thesis is FALSIFIED — freeze the primitive and do not build the
dashboard/polish phase. Conversely, sustained use (≥ 5 distinct actors posting
over the window) confirms the bet and promotes the polish phase. Metric-bound;
counts are a trivial table query. Kill/keep logic:
product-metrics-and-experimentation.md.
