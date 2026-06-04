# ADR-002: API Generations and the Federation-Component Posture

**Status**: Accepted
**Date**: 2026-04-24
**Deciders**: John (project lead)
**Context**: Filigree 2.0 direction; Loom federation participation (Clarion, Wardline, Shuttle); first external consumer (Clarion) approaching integration at WP2/9.

> **Roster pointer (added 2026-06-05).** This ADR's references to the Loom
> roster as "Clarion, Wardline, Shuttle, filigree" are **stale** and are
> retained here only as the historical context of the 2026-04-24 decision. The
> **authoritative federation roster and posture now live at `~/loom/doctrine.md`**:
> 5 realized members (Clarion, Filigree, Wardline, Legis, Charter); **Shuttle is
> a roadmap thought-bubble with no repo, not a fourth member**. Where this ADR
> names "Clarion / Wardline / Shuttle" as the peer set, read the loom doctrine
> for the current membership. **The decision below — Filigree's own named HTTP
> generations (`classic`, `loom`) and their lifecycle — is Filigree-owned and
> unchanged by this note.**

## Summary

Filigree 2.0 reframes the product from "standalone issue tracker with an HTTP API" to "standalone issue tracker **plus** a loosely-coupled component of the Loom federation." This release introduces **named API generations** at the HTTP surface — `classic` (historical, at `/api/*` — mostly un-prefixed, with one `/api/v1/` outlier, `POST /api/v1/scan-results`) and `loom` (new, at `/api/loom/*`) — with **lifecycles decoupled from filigree's code-version cadence**. MCP and CLI reflect the living / current-recommended surface only; HTTP is where pinned generations live. The federation posture is **cooperation, not mandate**: every `loom`-generation endpoint must be fully functional in the absence of other federation components.

## Context

### What 2.0 actually changes

Filigree 1.x was a product in its own right — an issue tracker with an HTTP API, a CLI, and an MCP surface. Integration with other tools was possible but incidental.

Filigree 2.0 is:

1. **Still that same product**, running fine standalone, for users who want an issue tracker and nothing more.
2. **Additionally, the work-state component of the Loom federation** — the place where Clarion ingests findings, where Wardline emits SARIF-translated findings, where Shuttle publishes work state.

The code-level changes in 2.0 (unified `BatchResponse`/`ListResponse` envelopes, closed `ErrorCode` enum, `issue_id` vocabulary, composed `start_work` operations) are mostly *additive* against the existing HTTP surface if we land them as a new generation rather than as in-place breaks. The meaningful change is not mechanical — it is **how we think about the product's boundaries and obligations**. That reframing is what justifies the major version bump.

### Why "the meaning changed" justifies a major bump

Semver is conventionally read as "breaking code changes." The more honest reading is "breaking assumptions about what this thing *is*." 1.x users assumed filigree was a standalone tool. 2.0 users inherit a product that actively participates in a federation. That's a category shift even if the wire-level compatibility stays high. Reserving major bumps only for mechanical breaks understates what actually changed in 2.0 and overstates what would change in a hypothetical 3.0 with identical mechanical delta.

### The consumer situation

Clarion is the first external consumer. As of 2026-04-24 Clarion is at WP2 of 9, with:

- **Zero code-level hits** on Filigree's MCP or batch/list API (Rust client not yet written).
- **~10 ADRs and design documents** referencing specific Filigree API shapes (`POST /api/v1/scan-results`, `metadata.clarion.*` nesting, dedup semantics from `mark_unseen` / `run_id`).
- **Explicit ADR-017 request** for a published contract fixture that Clarion's CI can pin against.
- **Explicit acknowledgment** that "Filigree does not treat `/api/v1/…` as a strong stability contract; it breaks and tags" — a characterization they have planned around, not one they prefer.

Clarion is therefore in the rare position of being an *influencing* consumer rather than a *constraining* one: our design leverage over their integration is wide (until ~WP4-5), and their asks (stable contracts, published fixtures) are reasonable, cheap, and point toward good design practice.

### What break-and-bundle was solving, and failing to solve

The original 2026-04-18 design (`docs/plans/2026-04-18-2.0-unified-surface-design.md`) chose a single-surface clean-cut: hard-break every renamed key, every envelope shape, every error code, in one release. The 2026-04-23 rebaseline (`docs/plans/2026-04-23-2.0-stage-2b-rebaseline.md`) switched to "bundled 2.0 PR, no interim push" because Clarion has no staging environment and a per-stage break cadence would have sprayed breaks onto production.

Neither solves the underlying problem: **Clarion has no way to pin against a specific contract across a filigree upgrade**. Break-and-bundle only defers that problem to the next major. The rebaseline's own parity module, Task 2b.-1's success-shape pin, and Clarion's ADR-017 fixture request all point at the same missing primitive: *a stable, addressable target to pin against*.

Named generations provide that primitive. `/api/loom/scan-results` is a stable, addressable target that Clarion can pin against. When we need to evolve, we introduce a new generation at a new path — we do not mutate `loom`. Clarion pins and upgrades on their own clock.

## Decision

### 1. Named API generations at the HTTP surface

Filigree's HTTP surface exposes **named API generations**, each with a stable routing identity:

- **classic** — the pre-federation filigree HTTP API as it existed through the 1.x series. URL layout: the currently-existing `/api/*` surface, with one `/api/v1/` outlier (`POST /api/v1/scan-results`). Classic is not uniformly under a `/v1/` prefix; that was the break-and-bundle plan's aspiration, not the historical reality. Frozen: no new operations, no shape changes, no URL moves. Continues to be fully supported.
- **loom** — new in 2.0, at `/api/loom/*`. Represents filigree's participation in the Loom federation. Uses the unified envelope shapes (`BatchResponse[T]`, `ListResponse[T]`), the closed `ErrorCode` enum, the `issue_id` vocabulary, and the composed operations (`start_work`, etc.).

### 2. Living surface alongside generations

A **living surface** at `/api/*` (no generation prefix) aliases the current recommended generation. For loom endpoints that have no classic counterpart (e.g. new composed operations like `/api/loom/issues/{id}/start-work`), the living-surface alias lands at `/api/<path>` and routes to the loom handler. For endpoints where classic and loom *both* exist, `/api/<path>` continues to serve classic (because classic is frozen and its URLs are already there) until a per-endpoint living-surface decision is made. See the 2.0 work package Phase B3 amendment for the deferral rationale and Phase C for the per-endpoint resolution.

The living surface is explicitly **non-stability**: it is for callers who want "whatever filigree's current standard interface is" — prototypes, local dev tools, hooks, scripts that are updated alongside filigree itself. Production integrations across version boundaries must pin to a named generation.

Documentation for each living-surface endpoint declares, explicitly: *"Equivalent to `/api/<generation>/<path>` as of <date>."* The declaration is per-endpoint, kept current.

### 3. API generation lifecycle decoupled from filigree code version

A named generation's lifecycle does **not** track filigree's major/minor/patch cadence:

- A generation is **introduced** when we decide it is ready, at a release of our choosing. Generations are not tied to major-version bumps.
- A generation is **retired** only via a new ADR with explicit justification. Retirement is never automatic, never a side-effect of a version bump.
- A generation stays **frozen** for its entire lifetime: shape additions are allowed only if they preserve wire compatibility for existing consumers (new optional fields in responses, new optional request parameters with safe defaults). Anything that changes existing behavior requires a new generation.

Filigree 3.0 might or might not introduce a new generation; the code-version bump does not imply one. Conversely, filigree may introduce a new generation mid-2.x if a genuinely-new era emerges before 3.0.

### 4. Naming rule: thematic, reflecting era or capability

Generations are named thematically — reflecting what they *represent*, not which count they are:

- `classic` — the pre-federation era.
- `loom` — the federation-era generation, named for the federation it participates in.

Future generations follow the same rule. If (hypothetically) a generation emerges around a specific capability — entity resolution, graph queries, streaming — the name reflects that: `loom-entities`, `loom-graph`, `loom-stream`, or an entirely new era name if the shift is foundational enough.

Numeric sequencing (`v1`, `v2`, `v3`) is rejected: it tethers the API to the code version by visual convention and forces every break to imply progression. Numeric-scoped naming (`loom/v1`, `loom/v2`) is held as a fallback if thematic names get hard to pick (never yet), and is tolerable because it still escapes the code-version tether.

### 5. MCP and CLI reflect the living surface only

MCP tool names and CLI command names do not carry generation markers. They evolve forward with each release:

- `mcp__filigree__batch_update` always emits the current recommended shape. When we introduce a new generation, the MCP tool's response shape moves with it.
- `filigree batch-update` (CLI) likewise.
- There is no `mcp__filigree__batch_update_classic` or `filigree --api=v1 batch-update`. Callers who need pinned stability use HTTP.

Rationale:

- **MCP is an agent-facing convenience layer.** Agents (LLMs, automation) adapt to shape changes on-the-fly; they do not benefit from pinned stability the way a compiled client does.
- **CLI is inherently living.** Interactive users and shell scripts accept shape evolution alongside filigree upgrades; pinning a CLI to a generation is an anti-pattern (and would force users to remember which command shape they're on).
- **The real stability boundary is HTTP.** Compiled clients in other languages, CI pipelines, contract-pinned consumers (Clarion, future federation members) all live at the HTTP surface. That is where generations pay for themselves.

### 6. Implementation model: shared handler, per-generation shape adapters

For each endpoint:

- **One internal handler** implements the behavior, operating on internal-shape data (Python domain objects / typed dicts using the target `issue_id` vocabulary).
- **Per-generation shape adapters** transform the internal result into the wire shape expected for each generation:
  - Classic adapter: renames `issue_id → id`, uses `{updated, errors, count}` batch wrapper, returns bare arrays for lists.
  - Loom adapter: passes `issue_id` through, uses `BatchResponse[T]` / `ListResponse[T]` envelopes.
- **Shape adapters are thin, data-only transformations** — no business logic. Lives in `src/filigree/generations/<name>/adapters.py`.

Benefits:

- Behavior fixes land once, visible in all generations immediately.
- Shape differences are isolated, auditable, testable per-generation.
- Adding a new generation is a matter of adding a new adapter directory, not rewriting handlers.

### 7. Coupling principle: loose cooperation, not mandate

Every `loom`-generation endpoint **must be fully functional in the absence of other federation components**. Filigree-the-standalone-product continues to work without Clarion, Wardline, or Shuttle. The `loom` generation adds capability for cooperating with them; it does not require their presence.

Operationalized:

- No `loom`-generation endpoint returns an error or reduced response if a federation peer is absent.
- No `loom`-generation endpoint requires configuration of federation peers to operate.
- Cross-product integrations (e.g., `registry_backend: clarion` per Clarion's ADR-014) are opt-in configuration, not defaults.
- CHANGELOG entries for `loom`-generation capabilities describe them as *enabling* cooperation, not *requiring* it.

This is the LSP / Unix-tool pattern: filigree is fully useful alone; the `loom` generation is the optional cooperation surface, analogous to how an editor is fully useful without an LSP server but gains capability when one is present.

### 8. Retirement policy

A named generation is supported indefinitely by default. Retirement requires all of:

1. A new ADR explicitly proposing retirement, naming the replacement generation and the migration path.
2. A formal deprecation announcement in CHANGELOG and documentation, at least 12 months before the retirement release.
3. A tool (CLI / docs) that tells consumers which generation they are using and whether it is scheduled for retirement.

Retirement is therefore a **deliberate engineering decision**, never a drift-by-default. This is what earns back the trust that Clarion's integration recon (`"Filigree does not treat /api/v1/… as a strong stability contract; it breaks and tags"`) is trying to work around.

## Consequences

### Committed

- **The `classic` generation (the `/api/*` surface, including the `/api/v1/scan-results` outlier) is a stability contract.** No more breaks at these URLs. Any 1.x caller on the classic paths continues to work through 2.0 and onward, unchanged.
- **The `loom` generation is the 2.0 introduction.** It is the new recommended target for integrations; `classic` remains fully supported but is not where new federation capabilities land.
- **Fixture publication discipline.** For each generation, we publish representative request/response fixtures in `tests/fixtures/contracts/<generation>/` and reference them in CHANGELOG. Clarion's ADR-017 ask becomes the default practice.
- **A new surface-inventory discipline.** Changes to MCP/CLI/HTTP must declare which generation (for HTTP) or "living" (for MCP/CLI) they target, in commit messages and CHANGELOG entries.

### Preserved optionality

- **Evolution cadence decoupled from code cadence.** We can ship filigree 2.3 with a new generation, or filigree 3.0 with no new generation, or both, without the names or the numbers constraining each other.
- **Cooperation shape evolution without forcing moves.** If Loom federation semantics evolve (e.g., entity resolution protocol), we introduce `loom-entities` or similar, leave `loom` pinned, and let Clarion / Wardline / Shuttle migrate on their own clock.
- **Graceful federation exit.** If (hypothetically) a peer leaves the federation or the federation reframes, filigree's classic surface and standalone operation are unaffected — the `loom` generation's coupling was always opt-in.

### Costs

- **Maintenance burden per generation.** Each generation has its own adapters, fixtures, and tests. Adding a third generation doubles that overhead; adding a fourth triples it. Retirement becomes the pressure-release valve, and retirement policy (§8) is therefore load-bearing.
- **Discipline required to keep the living surface synced.** If a living-surface endpoint drifts from its declared generation-of-the-day, callers get surprised. A CI gate (documented in the 2.0 work package) checks living-surface equivalence against the declared current generation.
- **Documentation complexity.** Each endpoint needs generation-specific docs. Mitigated by auto-generating from shape fixtures + adapter types where possible.

## Rejected alternatives

### A. Break-and-bundle at the single-surface API

This was the original 2026-04-18 design. Rejected because:

- It does not provide a pinnable contract to Clarion; it only delays the need for one to 3.0.
- It requires lockstep release coordination with every consumer on every major, which Clarion explicitly does not have staging to support.
- The rebaseline's own mitigations (parity module, success-shape pins, bundled PR, Clarion grep) are all workarounds for the missing primitive that named generations would provide.

### B. Numeric API versions (`/api/v2/*`, `/api/v3/*`)

Rejected because:

- Implicit tether to code versions. Callers and reviewers assume `v2` ships with 2.0 and expect `v3` with 3.0; the tether then becomes a pressure to break the API at every code major whether or not the API has earned it.
- Numeric versions communicate nothing about *what* each version is. Thematic names force us to identify the era or capability a generation represents, which is a useful design pressure.
- Numeric versions invite "latest is best" assumptions that break the loose-coupling commitment: callers on `v1` feel informally pressured to move to `v2`, even if `v1` remains fully supported.

### C. Dual-accept / polymorphic endpoint

*One endpoint accepts both `id` and `issue_id`, returns both `errors` and `failed`, etc.*

Rejected because:

- Callers cannot tell which shape they got back without caller-side versioning logic, which defeats the purpose.
- Polymorphic endpoints require server-side state-machine complexity (which shape to emit? which to parse?) for no gain over two clean endpoints.
- The original 2026-04-18 design rejected this as §Non-goal "No dual-accept compat shim"; that rejection stands. Named generations are explicitly *not* a dual-accept shim — they are two endpoints, each with one shape.

### D. Capability negotiation / in-protocol version handshake

Rejected because:

- Adds a handshake cost to every connection.
- Forces clients to implement version-negotiation logic even when they only care about one generation.
- The original 2026-04-18 design rejected this as §Non-goal "No capability negotiation"; that rejection stands.

### E. Date-anchored names (`/api/2026-Q2/*`)

Considered, rejected because:

- Date names communicate nothing about what the generation *is*; a reader has to look up "what was different in 2026-Q2?"
- Works for Stripe because Stripe breaks many small things often; filigree's generations will be fewer and more meaningful, so thematic names pay off.
- Kept in reserve as a fallback if thematic naming ever fails us.

### F. Scoped numeric (`/api/loom/v1/*`, `/api/loom/v2/*`)

Considered, kept as fallback. Rejected as the default because:

- Two levels of naming is more indirection than the current problem warrants.
- Named eras (`loom`, eventual successors) should be stable enough that in-era numeric breaks are rare; if they become frequent, that is itself a signal to rename the era.

## Implementation scope for 2.0

See the 2.0 federation work package (`docs/plans/2026-04-24-2.0-federation-work-package.md`) for the concrete task breakdown. Summary:

1. **Landing the `classic` generation as frozen** — no code change to the classic `/api/*` paths themselves; an audit + fixture-pinning exercise.
2. **Landing the `loom` generation** — new `/api/loom/*` routes, shape adapters, response types, contract fixtures.
3. **Living surface wiring** — `/api/*` unversioned routes alias `loom`.
4. **MCP/CLI forward-migration to the living shape** — `issue_ids`, `issue_id`, `BatchResponse`, `ListResponse`, `start_work`, etc.
5. **Documentation and CHANGELOG telling the federation-component story** — not a "what broke" story.

## References

- **Prior design**: `docs/plans/2026-04-18-2.0-unified-surface-design.md` (superseded in framing; shape decisions preserved)
- **Prior plan**: `docs/plans/2026-04-18-2.0-unified-surface-plan.md` (supersed by the work package below)
- **Rebaseline**: `docs/plans/2026-04-23-2.0-stage-2b-rebaseline.md` (superseded; shape pins and Clarion survey findings preserved)
- **Snapshot**: `docs/plans/2026-04-21-2.0-unified-surface-snapshot.md` (historical; still accurate for pre-2026-04-24 state)
- **2.0 work package**: `docs/plans/2026-04-24-2.0-federation-work-package.md`
- **Clarion ADRs consulted**: ADR-004 (finding-exchange-format), ADR-014 (filigree-registry-backend), ADR-015 (wardline-filigree-emission), ADR-016 (observation-transport), ADR-017 (severity-and-dedup) — all in `/home/john/clarion/docs/clarion/adr/`.
- **Loom doctrine**: `~/loom/doctrine.md` (authoritative federation framing, roster, and axiom — promoted here 2026-06-05 from the former `/home/john/clarion/docs/suite/loom.md`, which is now a pointer). Cross-product contract index: `~/loom/contracts-index.md`.
