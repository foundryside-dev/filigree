# Vision — Filigree

## Purpose
Filigree exists to make AI coding agents *first-class operators* of their own
work-tracking. Traditional issue trackers are human-first: agents scrape CLI
output, parse API responses, and lose state between sessions. Filigree inverts
that — a local-first, SQLite-backed tracker where agents orient from a
pre-computed `context.md`, claim work with optimistic locking, and resume cold
across sessions without re-reading history. The change in the world it makes:
an agent can run a full unit of work — orient → claim → build → close — through
a native tool surface, alone or alongside other agents, without dead-ends.

## Who it serves
- **Primary:** AI coding agents (Claude Code, Codex, and peers) operating a
  repository's work-state natively over MCP — including multi-agent fleets that
  must claim, hand off, and recover without colliding.
- **Secondary:** the human developer driving those agents, via the CLI
  (`--json`, `--actor`) and the web dashboard (localhost:8377), and sibling
  tools in the **Weft federation** consuming Filigree's work-state contracts.
- **Explicitly not:** teams wanting a hosted, multi-tenant, human-first project
  management SaaS (Jira/Linear class), or anyone needing to store secure,
  regulated, confidential, or business-sensitive data — Filigree provides no
  encryption, sandboxing, or hardening beyond filesystem permissions.

## Anti-goals (what it refuses to be)
- **A cloud product.** No accounts, no multi-tenant hosting, no telemetry
  phone-home. Local-first is the identity, not a phase.
- **A secure data store.** It will not grow encryption/sandboxing to court
  regulated workloads; the README security boundary is a deliberate refusal.
- **A human-first PM suite.** Burndown-chart / sprint-ceremony surface area for
  human teams is declined; the agent is the primary user, the human the driver.
- **The federation hub.** Filigree owns its own surface and contracts; it does
  not absorb sibling-tool responsibilities (scanning is Wardline, code
  archaeology is Loomweave, git/CI governance is Legis). It composes, it does
  not annex.

## Authority grant
Granted by: John Morrissey (@qacona)     Last reviewed: 2026-06-16
Review cadence: monthly, or on any vision change

Autonomous within strategy — the agent MAY, without asking:
  prioritize the backlog, write PRDs, dispatch delivery, accept against
  criteria, reprioritize, kill a failing bet per metrics.md.

Escalate BEFORE acting — the agent MUST get owner sign-off for:
  changing this vision/strategy/grant, public release or announcement
  (PyPI publish, GitHub release, version tag of a shipped artifact),
  deprecating a feature users depend on, pricing/commercial change,
  data deletion, anything touching an external party.
  (Taxonomy + rationale: product-ownership-operating-model.md.)
