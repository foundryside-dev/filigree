# Shuttle — Planning and Execution in the Loom Federation

**Status:** Draft design v2 (2026-05-17)
**Scope:** Architecture and schema for *Shuttle*, the Loom federation's planning-and-execution component
**Federation peers (via Loom API):** Filigree (ticketing + messaging), Clarion (review + audit), Wardline (security + semantics)
**Sibling documents:**
- `2026-05-17-loom-uri-spec.md` — Loom URI scheme (must ship before Shuttle Batch 3)
- `2026-05-17-filigree-planning-deprecation.md` — filigree planning-pack deprecation plan

---

## 1. Context and motivation

The Loom federation splits four concerns across four standalone components.
Each component owns its own data, its own storage, its own MCP/CLI/HTTP
surfaces, and its own concurrency model. They communicate through the **Loom
API surface** — a shared cross-component contract for references, events,
and queries. No component "sits on" another; Loom is the federation glue,
not a substrate.

The four components and their responsibilities:

- **Shuttle** — *planning and execution.* Owns plans, stages, steps, the
  hand-off-to-subagent flow, the TDD/spike/decision/integration shapes,
  the prose containers that hold design narrative.
- **Filigree** — *ticketing and messaging.* Owns issues (bugs, features,
  tasks), observations, annotations, comments, the recording side of
  work. Filigree tickets can be *attached to* Shuttle objects via Loom
  for informational reasons.
- **Clarion** — *review and audit.* Consumes Shuttle's `ready_for_review`
  events, owns the review workflows, surfaces audit queries.
- **Wardline** — *security and semantics.* Owns named gates that Shuttle's
  `pre_checks` / `post_checks` may reference; owns "actor cannot
  self-approve their own change"-style preconditions.

The motivation for Shuttle as its own component: agentic workflows in
this codebase currently plan in flat files (`docs/plans/*.md`). The
filigree team itself writes 40+ flat plans alongside filigree, including
a 5,760-line unified-surface plan, because filigree's ticketing-shaped
surfaces don't match the planning workflow's actual needs (prose-bearing
containers, typed step shapes, navigable plan rendering, prep-for-step
affordances, cross-step synthesis at fleet scale). Trying to bolt those
onto filigree would distort filigree's role; building them as Shuttle, a
federation peer, keeps each component's responsibility envelope clean.

> **Roster/status pointer (added 2026-06-05).** The four-component framing in
> this document is stale and Shuttle-centric. The **authoritative federation
> roster and axiom now live at `~/loom/doctrine.md`**: 5 realized members
> (Clarion, Filigree, Wardline, Legis, Charter), and **Shuttle is a roadmap
> thought-bubble with no repo** — this very design was never built (see
> `~/loom/members/shuttle.md`). Read this as a historical design sketch, not
> current membership.

**Federation reality check.** As of 2026-05-17:

- Filigree's `/api/loom/*` surface exists (issues, files, analytics,
  releases routers), and the federation posture is formally defined in
  ADR-002.
- Clarion exists as an active sibling project with documented integration
  work (references in `docs/federation/contracts.md`).
- **Wardline is named-only.** No code, no contracts, no endpoints exist
  today. The design references Wardline as a planned peer; gate
  definitions that this design says "live in Wardline" do not have a
  home yet. Pre/post checks that reference Wardline gate names degrade
  to opaque string identifiers until Wardline ships.
- **Shuttle does not exist.** This document is its founding artifact.
- **Loom is not a running service.** It is the federation API
  convention. The `/api/loom/*` HTTP namespace is filigree's
  contribution; each component exposes its own. There is no Loom broker.
  Cross-component traffic is direct HTTP between component dashboards,
  routed by URI scheme (see sibling doc `2026-05-17-loom-uri-spec.md`).

This design is the convergent output of four agent perspectives plus a
formal architectural review panel. The design carries dissent forward —
Section 11 records what Shuttle does *not* fix, with explicit canary
scenarios for when to revisit the bets.

---

## 2. The shape of the design

**Principle: prose-bearing containers with a small typed surface where
shape is obvious.** Every Shuttle planning tier (plan / stage / step) has
an unbounded markdown `prose` field; on top of that, a handful of typed
fields capture the parts of planning content that benefit from being
structured (verification commands, pre/post checks, target files,
outcomes, supersession). The agent's default move is "dump thoughts into
prose"; they promote a paragraph to a typed field only when its shape is
unambiguous.

This collapses the cognitive overhead that would otherwise apply to every
recorded thought. The taxonomy tax becomes "which tier of the tree does
this thought belong to," which is real but materially smaller than
"which of N verbs records this thought."

Step-level work is further organized by a small set of *shapes* — TDD,
spike, decision, integration, and a free-text fallback. A shape is a
step-template that gates which typed fields apply, based on whether the
agent's *first move* is structurally different (write a failing test;
name a question; commit a choice; reconcile a boundary). Shapes that
don't change the first move stay as labels, not templates.

**Design boundary: Shuttle owns forward planning, not reasoning history.**
A plan stored in Shuttle and rendered via `show_plan` is a *graph* of
prose blocks. The flat plans Shuttle replaces are *narratives* readable
top-to-bottom as arguments. Shuttle is materially worse than a flat file
at preserving the argument structure that some plans (ADRs,
retrospectives, forensic post-mortems) depend on. `export_plan` (Section
5.4) writes a markdown projection but the projection is one-way: re-import
loses authorship history, supersession chains, and outcome enumerations.
For reasoning-history use cases, flat ADRs and post-mortem documents
remain the right surface; Shuttle stays out of their way.

---

## 3. Shuttle's standalone surface

Shuttle is a complete component. It has its own data store, its own
MCP/CLI/HTTP servers, its own concurrency model. The Loom federation API
is how Shuttle is *reachable from* the rest of the federation, not how
it *is built*.

### 3.1 Shuttle's data and storage

Shuttle owns:

- Its own SQLite store with WAL mode, co-resident with the project repo
  at `.shuttle/shuttle.db`. The Loom registry consumed by this Shuttle
  process is the **per-project** `.loom/registry.json` co-resident with
  the same project (sibling doc §5.1.1). When one Shuttle process serves
  multiple projects, each project's view is held independently keyed on
  the URI authority slot; cross-project resolution falls through to the
  calling project's registry per §5.1.1.
- Its own optimistic-concurrency model: per-row `expected_assignee`
  preconditions, atomic claim+transition verbs, append-only event log.
  These are *Shuttle's implementations*, but the **patterns are deliberately
  identical to filigree's** so that a future `loom-core` shared library
  can extract both implementations into a common surface without breaking
  callers. See Section 12 for the explicit pre-Batch-1 task to extract
  filigree's claim/heartbeat/event-log primitives into a shared package.
- Its own type system: `plan`, `stage`, `step`, with workflows defined
  inside Shuttle's own templates module.
- Its own annotations and observations *for plan-anchored content*.
  File-anchored marginalia stays in filigree (3.4).
- Its own event log with Shuttle-specific event kinds (Section 4.5).

#### Append-only is operational, not absolute

Following filigree's pattern, Shuttle's event log is **operationally
append-only** — INSERT-only in normal use — but a `compact_events()`
maintenance verb may eventually delete oldest events per object (matching
filigree's `db_events.py:501-523`). For audit-trail purposes, the
event log should be considered authoritative *up to the compaction
horizon*, not forever. The horizon is configurable; default
"never compact." When compaction runs, a compaction event is itself
recorded so the gap is observable.

### 3.2 Shuttle's surfaces

- **MCP server** — exposes Shuttle's verbs (Section 5) as MCP tools.
  Independent process from filigree-mcp.
- **CLI** — `shuttle` command. Independent binary from `filigree`.
- **HTTP dashboard** — read/write dashboard for plans/stages/steps,
  navigable plan renderer, prose-block history viewer.
- **Loom API surface** — `/api/loom/shuttle/*` endpoints conforming to
  the federation contract (sibling doc `2026-05-17-loom-uri-spec.md`).
  This is how *other components* reach Shuttle. Agents and humans use
  MCP/CLI/dashboard.

### 3.3 Cross-component integration via Loom

Shuttle objects can carry *attachments* — typed references to objects
in sibling components, identified by Loom URIs. Attachments are
informational: they enrich a Shuttle step's context without making it
depend on the sibling component for execution.

Attachment kinds (extensible via Loom registration):

| Attachment kind | Source component | When |
|---|---|---|
| `filigree_issue` | Filigree | "this step is addressing these bug/feature/task tickets" |
| `filigree_observation` | Filigree | "these incidental discoveries touch this step" |
| `filigree_annotation` | Filigree | "the rationale for this step lives at this code anchor" |
| `clarion_review` | Clarion | "this step has been reviewed in Clarion" |
| `wardline_gate` | Wardline | "this step's pre/post checks reference Wardline-defined gates" |

#### Schema — polymorphic parent via check-constrained pair

The attachments table is shape-neutral but `parent_id` must not directly
foreign-key a single table (plan/stage/step are separate tables in
Shuttle's schema). The relation uses a check-constrained
`(parent_kind, parent_id)` pair, with application-level integrity:

```
shuttle_attachments (
  id              text primary key,
  parent_kind     text not null check (parent_kind in ('plan','stage','step')),
  parent_id       text not null,
  loom_uri        text not null,
  attachment_kind text not null,
  attached_at     timestamp not null,
  attached_by     text not null,
  note            text
)
-- Composite index for fast "all attachments for this object"
create index ix_attachments_parent on shuttle_attachments(parent_kind, parent_id)
-- Idempotency: same URI cannot be attached twice to same parent
create unique index ux_attachments_parent_uri on shuttle_attachments(parent_kind, parent_id, loom_uri)
```

The same `(parent_kind, parent_id)` pair is used for `prose_blocks`
(Section 4.3). Application-level integrity (verify parent exists before
INSERT, refuse cross-kind reparenting) is enforced in Shuttle's write
verbs.

#### Authorization on Loom attachment writes

`POST /api/loom/shuttle/steps/{id}/attach` (Section 5.6) accepts attachment
registrations from federation peers. Authorization model:

- Each component holds a *component identity* token issued at federation
  registration time. Loom-side caller authentication uses this token in an
  `X-Loom-Component` header.
- Shuttle's Loom router validates the token, identifies the calling
  component, and accepts attachments whose `attachment_kind` matches that
  component's declared kinds. (A `clarion_review` attachment must come
  from a token registered as Clarion.)
- Cross-component impersonation (Clarion posting a `filigree_issue`
  attachment) is refused with HTTP 403.
- Attachment writes are also rate-limited per component identity.

The Loom URI spec (sibling doc) covers the identity/registration model in
detail. This design assumes that surface exists by Batch 3.

### 3.4 What Shuttle does NOT do

- **Bug / feature / task tracking.** Stays in filigree.
- **Code review state machines.** Lives in Clarion. Shuttle emits a
  `ready_for_review` event; Clarion runs the review.
- **Security or semantic gate definitions.** Live in Wardline. *Note:
  Wardline does not exist yet.* Until Wardline ships, named gates
  referenced in Shuttle's `pre_checks` / `post_checks` are opaque
  strings — they document intent but cannot be evaluated. **Strings
  must use the prefix convention defined in §4.1.1** so that the
  `wardline:` subspace is cleanly delimited from `local:` /
  `external:` checks that can be evaluated today. This is acceptable
  for Batches 1-2 (which are pre-Loom-integration anyway); Batch 3
  should not block on Wardline if it isn't ready, but the acceptance
  criteria for "production-ready Shuttle" include "Wardline gates
  resolvable" and "no `wardline:` prefix accumulates unparseable
  strings."
- **File-anchored marginalia.** Code annotations stay in filigree.
  Shuttle's plan-anchored prose is for design narrative; filigree's
  file-anchored annotations are for code marginalia; they link
  bidirectionally via Loom.

---

## 4. Schema additions

### 4.1 Step typed fields

All fields are optional at the *storage* level. Which fields are
**required for close** depends on `step.kind` (Section 6). Validation
happens at `close_step` time, not at `create_step` — agents accumulate
content into the step incrementally and the close call confirms the
shape was satisfied.

Free-text steps use only `prose`, `context`, `warnings`, `outcome`,
`related_issues`, `related_observations`.

| Field | Type | Applies to (kind) | Required at close? | Purpose |
|---|---|---|---|---|
| `prose` | markdown text | all | no (may be empty) | The container. Default destination for thought. |
| `context` | markdown text | all | no | One-paragraph broader context — where this fits. |
| `warnings` | `list[str]` | all | no | Gotchas the executing agent should know. |
| `outcome` | enum: `satisfied / partial / failed / abandoned` | all | **yes** | Verdict on conclusion. |
| `related_issues` | `list[loom_uri]` | all | no | Attached sibling issues — projection over `shuttle_attachments`. |
| `related_observations` | `list[loom_uri]` | all | no | Attached sibling observations — projection over `shuttle_attachments`. |
| `target_files` | `list[path]` | tdd, integration, decision | no | Files this step expects to touch. |
| `pre_checks` | `list[str]` | tdd, integration | no | Conditions verified before starting. Each string MUST carry a namespace prefix per §4.1.1 (e.g., `wardline:tier3-trust-boundary`, `local:tests-green`). |
| `post_checks` | `list[str]` | tdd, integration | no | Conditions verified after. Same prefix discipline as `pre_checks`. |
| `test_targets` | `list[str]` | tdd | **yes when kind=tdd** | Test files / pytest node-ids — RED anchors. |
| `failing_test_command` | `str` | tdd | **yes when kind=tdd** | The runnable that must fail before, pass after. |
| `question` | `str` | spike | **yes when kind=spike** | The named gap. One sentence. |
| `time_box` | duration | spike | no | Informational budget (see "Time-box enforcement" below). |
| `findings` | markdown text | spike | **yes when kind=spike** | Conclusion. |
| `followup_steps` | relation: `list[step_id]` | spike | no | Steps spawned from spike. |
| `alternatives` | `list[str]` | decision | **yes when kind=decision** | Options considered. |
| `chosen` | `str` | decision | **yes when kind=decision** | The committed option. |
| `rejected_because` | `dict[str, str]` | decision | **yes when kind=decision** | option → reason. Forces rejected-options asymmetry. |
| `scope` | `str` | decision | no | Predicate over modules/files the decision governs. |
| `merges` | relation: `list[step_id]` | integration | **yes when kind=integration** | Streams being joined. |
| `contract_ref` | `str` | integration | no | Schema/API spec/protocol doc honored. |
| `conflict_surface` | `list[path]` | integration | no | Files multiple streams touch. |

#### Kind-gated enforcement

`close_step` runs a validator that checks the **required-at-close**
columns above. Closes that violate the requirements raise
`SHAPE_VALIDATION` and refuse to close. The validator is a single function
keyed on `step.kind`; it is unit-tested per shape (Section 9 testing
strategy).

#### 4.1.1 Pre/post-check prefix discipline

`pre_checks` and `post_checks` are `list[str]` and Wardline (the gate
evaluator) does not exist yet. Without discipline, stored strings
accumulate in arbitrary shapes for the 6-12 months until Wardline
ships, and retroactive interpretation becomes the problem Wardline
was meant to solve.

**Rule (enforced from Batch 3 forward):** every check string MUST
begin with one of the registered namespace prefixes below, followed by
a colon and an identifier. The write path on `update_step` /
`append_check` validates the prefix; unknown prefixes are refused
with `INVALID_CHECK_PREFIX`. Stored strings without a prefix are
permitted only on rows created before Batch 3 (legacy data is
read-only against this rule).

| Prefix | Meaning | Evaluator | When stored as opaque |
|---|---|---|---|
| `wardline:` | Wardline-registered named gate. | Wardline (post-ship). | Yes — until Wardline ships, the string is documented intent. The `wardline_gates` attachment surface (§3.3, §5.2) reports `status="unavailable"`. |
| `local:` | Project-local convention; evaluated by the agent or a project-level script. Format `local:<slug>`. | Agent / project script. | No — locally evaluated from day one. |
| `external:` | Third-party system reference (CI run name, external scanner). Format `external:<system>:<identifier>`. | Operator / external lookup. | No — operator-evaluable from day one. |
| `loom:` | Reserved for federation-level check kinds (e.g., `loom:peer-resolves`); reserved but unused in v1. | TBD | Yes — refused at write until populated. |

The prefix list is **closed by design** for v1; new prefixes go through
a spec amendment, not ad-hoc adoption. This bounds the namespace before
accumulation; when Wardline ships, the `wardline:` subspace is
already cleanly delimited and Wardline does not have to retroactively
interpret arbitrary strings.

The `force=True` parameter (§4.6) bypasses the prefix check at
`close_step` (logged via `force_used`), so cleanup flows are not
blocked; the rule is a quality gate, not an integrity invariant.

#### step.kind mutability

`step.kind` is **mutable while status ∈ {open, in_progress}**, and
**frozen once the step closes** (any status in the done category). The
write verb `change_step_kind(step_id, new_kind)` re-validates: changing
to a kind whose required fields are unsatisfied is allowed at the
mutation step (the agent is signaling intent to fill them), but the
subsequent `close_step` enforces them.

The change is logged as an event (`kind_changed: old → new`) so
downstream queries can detect kind churn.

#### Time-box enforcement (spike only)

`time_box` is **informational**, not auto-enforced. No background daemon
closes overrun spikes. The agent (and the operator UI) can compute "this
spike has run past its time_box" and surface a soft prompt to conclude;
the actual close is still an agent action with the required `findings`
field filled. Section 11.4 lists "automated time-box enforcement" as a
canary trigger.

#### `followup_steps` vs `parent_spike_id`

The architectural reviewer flagged that `followup_steps` on a spike is
a retrospective link populated after the fact, when child steps are
created. We adopt a hybrid: child steps spawned from a spike carry a
`parent_spike_id` field (lightweight relation column on the child); the
spike's `followup_steps` is a *derived* read populated on
`prepare_step` and `show_plan` by reverse-querying. No duplicate write
path; the child knows its parent, the parent reads its children.

### 4.2 Supersession relations

First-class relations on plan, stage, step:

- `supersedes: list[shuttle_uri]` — objects this one replaces.
- `superseded_by: shuttle_uri` — set when this object is replaced
  (derived; materialized for fast query).

Supersession is **only valid within the same kind** (a stage cannot
supersede a step). Constraint enforced at the supersede verb.

Supersession does not transition the superseded object's status. The
agent doing the superseding may close the prior object manually or
leave it open with `superseded_by` set (for partial supersession —
Section 10.2).

### 4.3 prose_blocks table — corrected design

The `prose` field on plan/stage/step is **rendered from** an append-only
`prose_blocks` table. The renderer is the authoritative read path.

```
prose_blocks (
  id              text primary key,             -- ULID, time-sortable
  parent_kind     text not null check (parent_kind in ('plan','stage','step')),
  parent_id       text not null,
  seq             integer not null,             -- monotonic per (parent_kind, parent_id)
  author          text not null,
  created_at      timestamp not null,
  body            text not null,                -- markdown
  supersedes_block_id text references prose_blocks(id),
  idempotency_key text                          -- caller-supplied retry safety
)
create index ix_blocks_parent on prose_blocks(parent_kind, parent_id, seq)
create unique index ux_blocks_idempotency on prose_blocks(parent_kind, parent_id, author, idempotency_key) where idempotency_key is not null
```

#### Ordering — monotonic counter, not wall clock

Block order within a `(parent_kind, parent_id)` is determined by `seq`,
a per-parent monotonic counter assigned at INSERT time inside the
transaction. Wall-clock `created_at` is recorded for display and audit
but is **not** the ordering key. This eliminates clock-skew bugs across
multi-machine fleets.

The `seq` is allocated via:
```
INSERT ... seq = (SELECT coalesce(max(seq), 0) + 1 FROM prose_blocks
                  WHERE parent_kind = ? AND parent_id = ?)
```
Inside a `BEGIN IMMEDIATE` transaction, this is race-free.

#### Idempotency — explicit key, not implicit window

Callers supply `idempotency_key` for retry safety. The unique index on
`(parent_kind, parent_id, author, idempotency_key)` makes retries
idempotent without time windows. Callers that omit the key (e.g.
interactive dashboard edits) accept the duplicate risk; programmatic
callers should always supply.

#### Cache column — non-authoritative read cache

Each parent object carries a `prose` text column that is a *cache* of
the rendered concatenation. Properties of the cache:

- Written through on every `append_prose` within the same transaction
  as the block insert.
- **Not authoritative.** All Loom-API reads of prose content
  re-derive from `prose_blocks`. The cache is for fast single-object
  reads in the MCP/CLI dashboard surfaces only.
- Carries a `prose_cache_version` integer that increments on every write.
  This lets readers detect stale cache without re-deriving.
- A `shuttle rebuild-prose-cache <parent_id>` maintenance verb
  recomputes from blocks and overwrites the cache. The verb is
  idempotent and safe to run while writers are active.

The renderer (used by both the cache-write path and the on-demand
`show_plan` renderer) is a single function. Drift between paths is
impossible by construction.

#### Concurrent supersession — explicit tip check

`append_prose(..., supersedes=<block_id>, expected_tip=<block_id>)` —
the supersession is rejected with `STALE_SUPERSESSION` if `expected_tip`
is not the current tip of the supersession chain (the block currently
unreplaced for that anchor). This prevents mutual-supersession loops
where A supersedes B's block and B supersedes A's replacement
concurrently.

If `expected_tip` is omitted, the supersession proceeds without the
check — the caller is taking responsibility for any race. The dashboard
UI always supplies it; agentic callers should as well.

#### Migration from Batch 1's mutable `prose` column

**Batch 1 ships with a mutable `prose` column only** — no `prose_blocks`
table. Batch 2 introduces the table and migrates existing content:

```
-- For each plan/stage/step with non-empty prose:
INSERT INTO prose_blocks (id, parent_kind, parent_id, seq, author, created_at, body)
SELECT ulid(), 'step', id, 1, '__migration__', updated_at, prose FROM steps WHERE prose IS NOT NULL
-- ...repeat for plan, stage.
```

This is the **expensive direction** (architectural reviewer's correction):
mutable → append-only loses authorship history per block. We pay this
cost once at Batch 1 → Batch 2; we do not pay it twice. Migration script
ships with Batch 2; backfilled blocks have `author='__migration__'` and
the original `updated_at` as `created_at`.

### 4.4 step.outcome enum

A required field on close (per Section 4.1's "required at close" gate).
Distinguishes how a step concluded:

- `satisfied` — work completed as planned.
- `partial` — work concluded with caveats.
- `failed` — work attempted, did not produce intended outcome.
- `abandoned` — work stopped without an attempted completion.

This is the single typed field that recovers Query Q3 ("what attempts
failed for reasons that would also doom my current approach") from
impossible to tractable: filter the prose corpus by `outcome=failed`
first, then FTS or embedding search across `findings` / `prose`.

### 4.5 TDD cycle event kinds

Shuttle event kinds:

- `red_committed` — failing test exists, run, confirmed failing.
- `green_committed` — implementation passes the previously-red test.
- `refactor_committed` — refactor pass with tests still passing.
- `post_checks_passed` — `post_checks` ran clean.
- `ready_for_review` — step transitioned to await Clarion-side review.
- `kind_changed` — `step.kind` mutated; payload includes old/new.
- `compaction_run` — `compact_events` ran on this object (Section 3.1).

#### Idempotency

`record_event(step_id, kind, idempotency_key=None)` uses `INSERT OR
IGNORE` with a unique index on `(step_id, kind, idempotency_key)` for
the case where the caller supplies a key. For null keys, duplicates are
allowed; callers needing dedup must supply a key.

This matches filigree's pattern at `db_events.py:97`.

#### Ordering enforcement (TDD lifecycle)

Out-of-order TDD events corrupt downstream queries (`green_committed`
without prior `red_committed` is meaningless). The write path enforces
a **soft ordering check**:

- `green_committed` requires a prior `red_committed` on the same step.
- `post_checks_passed` requires a prior `green_committed`.
- Violation returns `OUT_OF_ORDER_EVENT` with the missing prerequisite
  named.
- A `force` parameter (see Section 4.6) bypasses the check; bypass is
  logged in the event payload.

`ready_for_review` and `kind_changed` have no prerequisites and
record unconditionally.

### 4.6 The `force` parameter

`force=True` is the documented escape hatch for cleanup flows that
intentionally bypass shape or ordering checks. Available on:

- `close_step(step_id, outcome=..., force=True)` — bypass kind-gated
  shape validation (lets a `kind=tdd` step close without
  `failing_test_command`).
- `record_event(step_id, kind=..., force=True)` — bypass TDD ordering.
- `change_step_kind(step_id, new_kind, force=True)` — change kind on a
  closed step (otherwise refused).

Every `force=True` invocation records:
- An event of kind `force_used` with the verb name and reason in the
  payload.
- The actor.
- The bypassed check.

`force` is **not authorized at the API layer** — any actor with write
access can use it. The audit trail is the deterrent. Wardline (when it
ships) may add an actor-allowlist precondition; until then, the event
log is what makes misuse visible.

---

## 5. New verbs and surfaces

All verbs are Shuttle's own — exposed on Shuttle's MCP server, CLI, and
HTTP dashboard. The Loom API surfaces (`/api/loom/shuttle/*`) expose a
subset for federation peers.

### 5.1 `append_prose(parent_kind, parent_id, body, supersedes=None, expected_tip=None, idempotency_key=None)`

Writes a new block to `prose_blocks`. Allocates a monotonic `seq`.
Optionally enforces tip-check via `expected_tip` (returns
`STALE_SUPERSESSION` if check fails). Idempotent on
`(parent_kind, parent_id, author, idempotency_key)` when key is supplied.
Triggers write-through of the parent's prose cache column.

### 5.2 `prepare_step(step_id) → StepPrepBrief`

The hand-off-to-subagent affordance. One Shuttle call returns
everything the receiving agent needs to self-assess readiness.
Internally fans out a single batched Loom query (`POST
/api/loom/multi-fetch`) to resolve all attachments. **Partial failure
is surfaced, not hidden.**

```
StepPrepBrief = {
  step: { ...all typed fields },
  parent_stage: { prose, supersedes_chain },
  prior_siblings: [
    { id, prose_summary, outcome, key_warnings, post_check_failures }
  ],
  attachments: {
    filigree_issues: [
      {
        loom_uri,
        status: "resolved" | "unavailable" | "not_found" | "timeout",
        # When status="resolved":
        title, issue_status, priority, summary,
        # When status != "resolved":
        error: { message, retryable: bool }
      }
    ],
    filigree_observations: [...],
    filigree_annotations: [
      { file, line, intent, body, critical, status }
    ],
    clarion_reviews: [...],
    wardline_gates: [
      { name, status }  # status="unavailable" until Wardline ships
    ]
  },
  blocking_dependencies: [ { step_id, status } ],
  readiness: {
    pre_checks_unverified: [...],
    failing_test_command_unset: bool,
    target_files_missing: [...],
    critical_annotations_unresolved: [...],
    blocking_deps_open: [...],
    # Loom degraded mode signals:
    federation_peers_unavailable: [...]    # which peer(s) returned non-resolved attachments
  }
}
```

**Failure semantics for the batched Loom call:**

- Each attachment fetch is independent. Per-attachment status is one of
  `resolved | unavailable | not_found | timeout` — the subagent always
  knows which attachments returned partial data.
- `prepare_step` itself does not fail if some peer is down; it returns a
  brief with degraded attachments and the `federation_peers_unavailable`
  list populated.
- `prepare_step` fails (raises) only if Shuttle's own data store cannot
  be read (corruption, lock contention) — i.e., the inability to assemble
  the brief at all.

Subagent reads `readiness` and decides. **Shuttle does not refuse
`start_work` based on readiness**, but `readiness.federation_peers_unavailable`
is the explicit signal for "your context is degraded, decide whether to
proceed anyway."

### 5.3 `show_plan(plan_id, options?) → markdown`

Renders the plan tree as one navigable nested prose document. Options:

- `--history` — include superseded prose blocks with strikethrough.
- `--hide-tombstones` — filter out steps with `outcome=abandoned` (default:
  show under `## Historical context` per Section 10.3).

Re-derives prose from `prose_blocks`, not from the cache. Uses the same
renderer as the cache write path; drift is impossible by construction.

### 5.4 `export_plan(plan_id, path)`

Writes `show_plan` output to a markdown file at `path`. Used at PR time
to produce a flat-file projection of the plan for review, grep, and
archival.

**One-way export.** Re-importing from a markdown projection loses
authorship history, supersession chains, and outcome enumerations. The
export is not a backup or round-trippable serialization.

### 5.5 `start_work` response extension

Shuttle's `start_work` / `start_next_work` responses include a `context`
field:

```
context: {
  parent_stage_prose: markdown,           # full prose of parent stage
  diff_since_my_last_touch: markdown,     # see scope below
  unread_decisions: [step_id, ...],       # capped at 20; see size bound
  attachment_summary: [...]               # one-line summary per attached object
}
```

#### Diff scope — step-level, not stage-level

`diff_since_my_last_touch` is **blocks added to the claimed step's prose**
since the claimant's last event on this step. For a first-time claimant
on a step with no prior events from them, the diff is the full prose
(everything is "since their last touch — which was never").

Stage-level prose changes are not surfaced here (they would dwarf the
relevant signal). Stage-level synthesis is what `parent_stage_prose`
covers, in full.

#### Unread-decisions size bound

`unread_decisions` is capped at 20 most-recent unread decisions. Beyond
that, the payload returns a `more_unread: int` count and the agent can
`list_decisions(stage_id, claimed_by=me)` to retrieve the rest. This
bounds the response size at fleet scale.

"Read" is recorded per-claimant per-decision via a lightweight
`decision_reads` table: `(decision_step_id, reader, read_at)`. Reads are
upserted by `prepare_step` and `start_work`.

### 5.6 Loom API surface (subset)

Endpoints exposed via Loom for federation peers:

- `GET /api/loom/shuttle/plans/{id}` — plan summary + stage list.
- `GET /api/loom/shuttle/steps/{id}` — step content (slimmed; prose from
  blocks, not cache).
- `GET /api/loom/shuttle/steps/{id}/attachments` — attached Loom URIs.
- `POST /api/loom/shuttle/steps/{id}/attach` — sibling components
  register attachments; authenticated and rate-limited per component
  identity (Section 3.3).
- `GET /api/loom/shuttle/events?since=<timestamp_or_event_id>` — event
  stream for listeners. Required for Clarion replay (Section 10.4).
- `POST /api/loom/multi-fetch` — batched resolution for `prepare_step`;
  authenticated.

Full reads use Shuttle's native HTTP/MCP/CLI; the Loom surface is for
cross-component traffic only.

---

## 6. Step shapes

### 6.1 The five shapes

`step.kind ∈ {tdd, spike, decision, integration, free_text}`. Default
`free_text`. Each shape gates required-at-close fields (Section 4.1).

#### tdd

- **Reach when:** implementing a behavior with a test-first loop.
- **Required at close:** `test_targets`, `failing_test_command`, `outcome`.
- **First move:** write the failing test.
- **Lifecycle markers:** `red_committed` → `green_committed` →
  (optional `refactor_committed`) → `post_checks_passed` → close.
- **Ordering enforced** at `record_event` time (Section 4.5).

#### spike

- **Reach when:** the agent doesn't know enough to plan; learning by
  doing.
- **Required at close:** `question`, `findings`, `outcome`.
- **First move:** name the question.
- **Discard contract:** spike code is read, summarized, re-implemented
  as a TDD step. Children carry `parent_spike_id` (Section 4.1).
- **Time-box is informational** — no auto-enforcement (Section 4.1).

#### decision

- **Reach when:** a viable choice exists among 2+ alternatives and the
  choice will be questioned later.
- **Required at close:** `alternatives`, `chosen`, `rejected_because`,
  `outcome`.
- **First move:** list the alternatives.
- **Why it's a shape:** `rejected_because` forces the asymmetry prose
  chronically omits.

#### integration

- **Reach when:** two or more previously-independent workstreams must
  land together, or work crosses a contract boundary.
- **Required at close:** `merges`, `outcome`.
- **First move:** name the streams being joined.

#### free_text

- **Reach when:** no other shape fits.
- **Required at close:** `outcome` only.
- **First move:** describe what you're doing.

### 6.2 Held for later (canaries — Section 11)

These were considered and deferred to specific trigger conditions:

- `refactor` — `invariant`, `invariant_check`. Trigger: "show me
  refactors where invariant X was claimed and later broke" gets asked
  more than once in a month.
- `bug-fix` — `repro`, `expected`, `root_cause`, `regression_test`.
  Trigger: post-mortem discipline degrades because TDD-with-label
  isn't being followed.
- `review` — *not a shape.* Lives in Clarion (Section 10.4).

### 6.3 Labels

Non-coercive, no schema cost: `kind:research`, `kind:refactor`,
`kind:bug-fix`, `kind:documentation`, `kind:cleanup`, `kind:integration`.

---

## 7. The hand-off-to-subagent workflow

The defining Shuttle interaction. Same as v1; degraded-mode handling
made explicit.

```
parent_agent:
  spawn_subagent(step_id="shuttle-step-9a3f...")

subagent (cold start):
  brief = shuttle.prepare_step("shuttle-step-9a3f...")

  if brief.readiness.federation_peers_unavailable:
    # Decide whether to proceed with degraded context.
    # Brief still contains everything Shuttle natively knows;
    # missing pieces are the attachments from down peers.
    log_degraded_context(brief.readiness.federation_peers_unavailable)

  if brief.readiness.is_clean:
    shuttle.start_work(step_id, assignee="claude-B")
  else:
    # Inspect what's missing; most input is already in the payload.
    # If a prior sibling's full prose needed: shuttle.get_step(prior_sibling.id)
    shuttle.start_work(step_id, assignee="claude-B")

  # If kind=tdd:
  run brief.step.failing_test_command       # confirm RED
  shuttle.record_event(step_id, kind="red_committed", idempotency_key=...)
  write_impl(brief.step.target_files)
  run brief.step.failing_test_command       # confirm GREEN
  shuttle.record_event(step_id, kind="green_committed", idempotency_key=...)
  run brief.step.post_checks
  shuttle.record_event(step_id, kind="post_checks_passed", idempotency_key=...)
  shuttle.close_step(step_id, outcome="satisfied")
  shuttle.record_event(step_id, kind="ready_for_review", idempotency_key=...)
  # Clarion's listener picks this up; if Clarion is down, see Section 10.4.
```

---

## 8. Testing strategy

**Test framework:** pytest. **Test layout:** Shuttle adopts a
**by-surface** organization to mirror filigree's actual convention:

```
tests/
  ├── core/         # core DB logic — claim, supersession, event log
  ├── api/          # HTTP API tests (dashboard + Loom endpoints)
  ├── cli/          # CLI verb tests
  ├── mcp/          # MCP server tests
  ├── migrations/   # migration tests (Batch 1 → Batch 2 prose blocks)
  ├── workflows/    # full lifecycle / multi-step scenarios
  ├── contract/     # Loom contract tests against pinned fixtures (§8.5)
  ├── fixtures/     # shared test fixtures
  └── conftest.py
```

This matches filigree's tree
(`tests/{api,cli,core,migrations,workflows,...}`) — explicitly by
surface, not by abstract layer. Earlier drafts described
`tests/{unit,integration,contract}/` as "mirroring filigree" — that
was wrong; filigree's `tests/unit/` is a small two-file directory and
the bulk of the suite is surface-organized. The §8.1-8.3 tables
below have been retargeted accordingly.

### 8.1 Core tests (`tests/core/`)

| Surface | Test path | Key cases |
|---|---|---|
| Kind-gated close validator | `tests/core/test_close_validator.py` | each kind × each required-at-close field missing; force=True bypass; outcome=None refused |
| `append_prose` ordering | `tests/core/test_prose_blocks.py` | monotonic `seq` under concurrent inserts (BEGIN IMMEDIATE); idempotency_key collision; cache write-through visible in same txn |
| Supersession tip-check | `tests/core/test_supersession.py` | tip match accepts; tip stale returns STALE_SUPERSESSION; expected_tip=None accepts unchecked |
| Renderer equivalence | `tests/core/test_renderer.py` | render-from-blocks == render-from-cache for every multi-block fixture; superseded blocks hidden by default, shown with `--history`; forked supersession chain rendered deterministically |
| Event ordering enforcement | `tests/core/test_event_ordering.py` | green without red refused; post_checks without green refused; force=True bypass records force_used event |
| `record_event` idempotency | `tests/core/test_record_event.py` | duplicate (step, kind, key) returns existing; null key allows duplicates |
| Kind mutation | `tests/core/test_change_kind.py` | mutation allowed while open; refused on closed (unless force); kind_changed event recorded |
| Attachment polymorphic FK | `tests/core/test_attachments.py` | parent_kind constraint enforced; parent existence verified at write; cross-kind reparent refused |
| `force` audit | `tests/core/test_force_audit.py` | every force=True path emits force_used event with verb, actor, bypassed check |
| Check prefix validation (§4.1.1) | `tests/core/test_check_prefixes.py` | `wardline:`/`local:`/`external:` accepted; unprefixed refused with `INVALID_CHECK_PREFIX`; legacy rows read-only past the rule |

### 8.2 Workflow tests (`tests/workflows/`)

| Scenario | Test path | Validates |
|---|---|---|
| Full TDD step lifecycle | `tests/workflows/test_tdd_lifecycle.py` | create → red → green → post_checks → close with outcome=satisfied; events in order |
| Spike with followups | `tests/workflows/test_spike.py` | spike close → spawn TDD steps with parent_spike_id → followup_steps derived read |
| `prepare_step` happy path | `tests/workflows/test_prepare_step.py` | brief contains step, parent stage prose, prior siblings, all attachments resolved |
| `prepare_step` partial Loom failure | `tests/workflows/test_prepare_step_degraded.py` | filigree up, Clarion down → brief returns per-attachment status; federation_peers_unavailable populated |
| Migration Batch 1 → Batch 2 | `tests/migrations/test_prose_migration.py` | existing prose column → prose_blocks with author=__migration__; rebuild_cache reproduces |
| Concurrent supersession | `tests/workflows/test_concurrent_supersession.py` | two writers racing on same anchor with expected_tip → one succeeds, one STALE_SUPERSESSION |
| `show_plan` round-trip | `tests/workflows/test_show_plan.py` | export_plan → markdown matches show_plan output exactly; --history surfaces superseded blocks |

### 8.3 Contract tests (cross-component)

**Required before Batch 3 ships.** Shuttle exposes a Loom contract that
Clarion consumes; if either side drifts, the federation breaks.

| Contract | Test path | Validates |
|---|---|---|
| `GET /api/loom/shuttle/events?since=` schema | `tests/contract/test_loom_events.py` | event schema validated against the Loom spec fixture; Clarion can replay from any since-anchor |
| `POST /api/loom/shuttle/steps/{id}/attach` | `tests/contract/test_loom_attach.py` | component-identity auth enforced; attachment_kind mismatch refused; rate limit enforced |
| `POST /api/loom/multi-fetch` | `tests/contract/test_loom_multi_fetch.py` | per-attachment status correct; partial failure returns degraded brief, not exception |

The Loom URI spec doc (`2026-05-17-loom-uri-spec.md`) publishes the
canonical fixtures; both Shuttle's contract tests and Clarion's
consumer tests load the same fixtures.

### 8.4 Coverage targets

- Unit: 100% on validator, renderer, ordering enforcement. ≥85% overall.
- Integration: every happy-path lifecycle + every degraded-mode path
  named above.
- Contract: complete coverage of Loom-exposed endpoints.

### 8.5 CI gates

```
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/shuttle/
uv run pytest --tb=short
# Contract tests against fixtures pinned via pyproject.toml [tool.loom-fixtures]
# (sibling URI spec §10.4.2); CI verifies the pinned SHA before running
uv run python scripts/verify-loom-fixtures.py
uv run pytest tests/contract/ --tb=short
```

The `verify-loom-fixtures.py` script reads `[tool.loom-fixtures]
pinned_sha` from `pyproject.toml` and asserts the local
`tests/contract/loom-fixtures/` submodule (or sparse-download cache)
matches; otherwise CI fails with a clear "fixture SHA drift" error.
This is the concrete mechanism behind the "fixtures published" gate;
the gate is not abstract and not an external coordination point.

Matches filigree's `release/2.0.2` CI pipeline shape.

---

## 9. Observability

**Reality check.** Filigree today does not use OpenTelemetry and does
not expose a Prometheus `/metrics` endpoint. Shuttle's observability
stack as described below is **new with Shuttle**, not extracted from
filigree, and is therefore a green-field decision rather than a "match
filigree" decision. The stack is chosen because OTel + Prometheus is
the prevailing convention for Python services at Shuttle's scale and
because the loom-core extraction (Batch 0, §12) is the natural future
home for observability primitives that filigree could later adopt.
Until filigree adopts loom-core observability, expect Shuttle's
dashboards to look unlike filigree's, and do not present this as a
shared convention.

### 9.1 Logging

- **Structured logs** (JSON) at INFO/WARN/ERROR levels via Python's
  `logging` module configured in Shuttle's main entrypoints. Filigree
  uses Python `logging` similarly; this is the one observability
  surface where there is genuine convention alignment.
- **Required log points:**
  - Every write verb logs at INFO: verb name, actor, parent_id, outcome
    code.
  - Every `force=True` invocation logs at WARN with the bypassed check.
  - Every Loom call (outbound from `prepare_step`'s multi-fetch) logs at
    INFO start/end with peer, attachment count, status counts;
    timeouts/errors at WARN.
  - Every `STALE_SUPERSESSION`, `OUT_OF_ORDER_EVENT`,
    `SHAPE_VALIDATION` error logged at WARN with the affected step_id.
  - SQLite `BUSY`/`LOCKED` retries logged at WARN; persistent contention
    at ERROR.

### 9.2 Metrics

Exposed via a `/metrics` Prometheus-compatible endpoint on the Shuttle
dashboard.

| Metric | Type | Labels | Purpose |
|---|---|---|---|
| `shuttle_writes_total` | counter | verb, outcome | Write throughput; outcome ∈ ok/refused/forced |
| `shuttle_event_kind_total` | counter | kind | Event-kind frequency; canary 11.4 reads this |
| `shuttle_force_used_total` | counter | verb, bypass_check | force= usage; alerts on rate |
| `shuttle_prepare_step_latency_seconds` | histogram | result | Brief assembly time; result ∈ ok/degraded/error |
| `shuttle_loom_call_total` | counter | peer, result | Outbound Loom calls; result ∈ resolved/unavailable/not_found/timeout |
| `shuttle_sqlite_busy_total` | counter | verb | SQLite contention canary (Section 11.6) |
| `shuttle_attachments_unresolved_total` | counter | peer | Persistent unavailability of a peer |
| `loom_registry_divergence_total` | counter | peer | Outbound `X-Loom-Registry-Version` mismatched what we expected for `peer` (sibling URI spec §5.1.2) |
| `shuttle_tdd_event_omission_ratio` | gauge | — | Background analysis of closed `kind=tdd` steps: fraction closed with missing or out-of-sequence TDD events. Computed off the write path because §4.5 ordering enforcement prevents the underlying mistake from manifesting as a write. See canary 11.4. |

### 9.3 Traces

OpenTelemetry-compatible spans on the Shuttle MCP server and HTTP
dashboard. Each verb invocation is a span; outbound Loom calls are child
spans. Trace IDs propagate across the Loom API via standard headers.

### 9.4 Error surfacing to agents

Per-error error codes that callers can switch on (matching filigree's
pattern):

- `SHAPE_VALIDATION` — kind-gated close validator rejected.
- `OUT_OF_ORDER_EVENT` — TDD ordering violated.
- `STALE_SUPERSESSION` — tip check failed.
- `KIND_FROZEN` — kind change refused on closed step.
- `INVALID_CHECK_PREFIX` — `pre_checks`/`post_checks` string lacks a registered namespace prefix (§4.1.1).
- `LOOM_DEGRADED` — non-fatal, embedded in `prepare_step` response
  payload (not an exception).
- Plus filigree-standard codes: `VALIDATION`, `NOT_FOUND`, `CONFLICT`,
  `INVALID_TRANSITION`, `PERMISSION`, `IO`, `INTERNAL`.

---

## 10. What Shuttle does NOT solve

The adversarial-skeptic perspective moved from "category error" to
"viable for forward planning, still wrong for reasoning history." Shuttle
inherits the surviving concerns.

### 10.1 Atomic git-editability — survives intact

Plan edits live in Shuttle's SQLite store. Code edits live in git. The
two cannot land in one reviewable diff. The earlier draft proposed
"check `.shuttle/` into git" as a workaround. **This option is dropped.**
Binary SQLite files produce unreadable diffs, unresolvable merge
conflicts, and noise on every read (WAL mode rewrites internal
statistics). The `export_plan` path (Section 5.4) is the load-bearing
escape hatch: commit the exported markdown projection alongside code at
PR time as the reviewable narrative. Export is one-way (Section 5.4).

### 10.2 Partial supersession — small but real

The `supersedes` relation answers "is it superseded." The prose still
has to explain "in what way." Current call: prose explains; `supersedes`
points; readers do the work. A typed `supersession_kind` field is held
as a potential future addition.

### 10.3 Orphan rationale

Decision rationale anchored to an artifact that no longer exists lives
in a tombstone: keep the closed step under `label:tombstone` with
`outcome=abandoned` and prose intact. `show_plan` renders tombstones
under a folded `## Historical context` section by default;
`--hide-tombstones` filters them.

### 10.4 The `ready_for_review` orphan problem

Shuttle ships before Clarion. When `ready_for_review` fires with no
Clarion listener, the step is in limbo. Mitigation, sequenced:

- **Event log catch-up is the contract.** `GET
  /api/loom/shuttle/events?since=<anchor>` lets Clarion replay all
  missed events on first connect or after downtime. Clarion persists
  its last-seen anchor.
- **TTL escalation.** If a step has emitted `ready_for_review` and no
  follow-up event from Clarion lands within a configurable TTL
  (default: 7 days), Shuttle emits a `review_overdue` event observable
  via the `/metrics` endpoint and the dashboard's stale-review panel.
- **No auto-status change.** Shuttle does not auto-close or re-transition
  steps based on review absence; the operator decides what to do.

Until Clarion exists, the orphan path is the *default* path. The
dashboard's stale-review panel and the `review_overdue` metric make
the invisible visible.

### 10.5 PII / secrets in prose

Append-only `prose_blocks` are permanently readable via `--history`.
**There is no automatic redaction.** If sensitive content lands in a
block (a credential pasted by an agent, a personal name in a
forensic narrative), it stays unless explicitly redacted.

Mitigations:
- **Operator responsibility.** Documented in operator-facing docs:
  "do not paste secrets into prose; if you do, you must redact."
- **`shuttle redact-block <block_id>` maintenance verb.** Writes a
  successor block with the same `supersedes_block_id`, body=
  `"[REDACTED]"`, and an event of kind `block_redacted` with the
  reason. The original block is *not* deleted (preserves audit
  trail of "something was here") but is hidden in all renders
  including `--history`.
- **Loom-side filtering.** `GET /api/loom/shuttle/steps/{id}` slims
  the response to exclude block bodies if the caller's component
  identity lacks read-prose permission (a future Wardline-defined
  gate). Until Wardline ships, this is a no-op.

### 10.6 Verb conflation

`close_step(outcome=satisfied)` is fine for satisfied work but quietly
conflates "attempt concluded successfully" with "intent satisfied"
when they differ. The greenfield primitives split (separate
Attempt/Intent objects) would distinguish; Shuttle does not. Acceptable
for v1 unless downstream queries demand the distinction (Section 11.5
canary).

### 10.7 Narrative vs graph shape mismatch

Restated from Section 2: Shuttle is materially worse than a flat file
for plans where the argument structure carries most of the information.
`export_plan` is one-way. ADRs, post-mortems, and forensic narratives
should remain flat files; Shuttle does not absorb them.

### 10.8 Single-node SQLite scale ceiling

Shuttle's SQLite store serializes writes. At fleet scale (50+
concurrent agents), write contention is foreseeable. Backup and
recovery follow filigree's pattern: WAL mode, periodic
`VACUUM`, file-level snapshots. No live replication; no read replicas;
no horizontal scale. Canary 11.6 names the trigger for revisiting this.

---

## 11. Canary scenarios

The deferred items have specific trigger conditions.

### 11.1 Assertion retraction cascade — Q5 canary

> Enumerate the N decisions that cite a retracted assertion in <1s.

**Trigger:** recurs and bites twice in a month. **Response:** add an
`assertion` Shuttle object kind and a `cites: list[assertion_uri]`
relation on `decision` steps.

### 11.2 Refactor invariant query

> Show me refactors where invariant X was claimed and later broke.

**Trigger:** asked more than once in a month. **Response:** ship the
`refactor` step shape; key on `invariant_preserved: bool` events.

### 11.3 Container-choice fragmentation

> Cross-cutting concerns scattered across multiple stages' prose
> because the agent couldn't pick a tier.

**Trigger:** cross-references rise as a fraction of total writes;
`prepare_step` reveals duplication. **Response:** add a `plan_note`
kind that floats above stages.

### 11.4 TDD event omission

> Agents skip `record_event(red_committed)`, making downstream queries
> unreliable.

The write-path ordering check (§4.5) refuses `green_committed` without
a prior `red_committed`, so the omission cannot be observed by counting
order-violations at write time — the write path *prevents* them. The
canary must run as a **background analysis over closed TDD steps**, not
a write-path counter:

```
For each step with kind=tdd and status in {done category}:
  events = events_for(step.id)
  red    = any(e.kind == 'red_committed' for e in events)
  green  = any(e.kind == 'green_committed' for e in events)
  closed_no_events = (not red and not green)
  green_no_red     = (green and not red)   -- only possible with force=True
  → metric: omission_ratio = (closed_no_events + green_no_red) / total_closed_tdd_steps
```

The `shuttle_tdd_event_omission_ratio` gauge (§9.2) is computed
periodically by a background job (not on the write path) and exposes
this ratio over a rolling 30-day window of closed TDD steps.

**Trigger:** omission_ratio exceeds 0.20. **Response:** git-hook
integration that auto-emits events based on commit-message convention,
plus a CI lint that fails on closing a `kind=tdd` step without the
expected event chain (deliberately gated on opt-in to avoid breaking
the `force=True` escape hatch).

### 11.5 Verb-conflation pressure

> Downstream queries can't distinguish "step satisfied" from "parent
> intent satisfied."

**Trigger:** asked more than twice in a month. **Response:** introduce
the Intent/Attempt primitive split per the greenfield design; expensive
schema migration.

### 11.6 SQLite write contention

> Fleet operation slows; SQLITE_BUSY rate climbs.

**Trigger:** `shuttle_sqlite_busy_total` rate exceeds 1/min sustained
over an hour, OR p95 write latency exceeds 200ms. **Response:** evaluate
WAL tuning / connection pooling / sharding by plan, in that order.

### 11.7 Time-box drift

> Spike steps routinely overrun their `time_box` without an explicit
> close.

**Trigger:** >25% of closed spikes overshot `time_box` by >2× across a
30-day window (measurable via the event log's
`step_closed.payload.time_box` vs. wall-clock duration). **Response:**
add automated time-box enforcement (background reaper closes overrun
spikes with `outcome=time_expired`).

---

## 12. Batch 0: extract loom-core

Before Shuttle Batch 1 begins, extract filigree's mature primitives
into a shared `loom-core` (or `filigree-primitives`) library. This
is **Batch 0** of the Shuttle sequence — it is named "Batch 0" rather
than "pre-Batch-1 prerequisite" so the workstream has a single,
unambiguous label across sections 12 and 13.

Primitives to extract:

- `expected_assignee` precondition pattern.
- `claim_issue` / `start_work` / `start_next_work` CAS verbs.
- Heartbeat / lease / stale-claim reaper.
- Append-only event log with `INSERT OR IGNORE` dedup.
- Optimistic-concurrency error code envelope (`VALIDATION`,
  `CONFLICT`, etc.).
- Migration framework (`migrations.py` shape).

Filigree refactors to import from the shared library; Shuttle imports
from day one. This avoids the divergence anti-pattern the
architectural reviewer named: "independent reimplementations of the
same primitives generate divergence debt proportional to the time
before the shared library is actually written."

### 12.1 Scope is not just file moves

The mechanical-extraction framing in earlier drafts understated the
risk. `db_issues.py` alone is >1000 LOC wired into `FiligreeDB` via a
mixin MRO chain; the primitives carry implicit dependencies on
filigree's schema names, error envelopes, and migration framework.
Batch 0 has two distinguishable phases:

| Phase | Work | Risk |
|---|---|---|
| 0a | Design the loom-core generic interface — what's parameterized (table names, ID schemes), what's fixed (CAS semantics, error codes). Write the interface as a `loom-core` package with type stubs but no implementation. | Medium. Wrong abstractions force a Batch 1 rewrite. |
| 0b | Move filigree's implementation behind the interface; filigree imports `loom-core` instead of running its own copy. CI green throughout. Then write Shuttle's first stub against the same interface. | Low. Mechanical once 0a is right. |

### 12.2 Acceptance gate

Batch 0 ships when:

- `loom-core` package exists with a stable v0.x interface and unit
  tests independent of filigree.
- Filigree's full CI pipeline (`uv run ruff`, `mypy`, `pytest`) is
  green at every commit during the extraction — no "broken main"
  windows. This is the load-bearing acceptance criterion; without it,
  the extraction stalls filigree's own ongoing work.
- Shuttle's empty skeleton can import `loom-core.claim` and
  `loom-core.events` and exercise the primitives against an in-memory
  SQLite DB. (No Shuttle business logic yet — just proof the import
  surface works.)

### 12.3 Revised estimate

**Scope:** 3-5 weeks (was ~2-3, revised upward to reflect the 0a
interface-design phase the earlier draft elided). **Risk:** medium for
0a, low for 0b. The primitives are well-tested in filigree, but the
shape of a clean generic surface has not been designed.

---

## 13. Sequencing

### Batch 0 — loom-core extraction

Per Section 12. 3-5 weeks (0a interface design + 0b mechanical
extraction). Filigree continues to function and stays green on CI
throughout; Shuttle import targets land at end of 0b.

### Batch 1 — Shuttle skeleton (solo-agent useful)

New package. SQLite store. Plan/stage/step types using `loom-core`
primitives. CRUD verbs + CLI + MCP server + minimal dashboard.
`prose` as mutable column (no blocks yet). No step shapes (everything
free-text). `show_plan` renders the tree.

**Value delivered:** solo-agent or small-team planning replacing flat
plan files. **Estimated effort:** 3-4 weeks.

### Batch 2 — prose-as-blocks + step shapes (fleet-ready storage)

Add `prose_blocks` table + `append_prose` + non-authoritative cache +
migration script (Section 4.3). Add `step.kind` enum + typed fields.
Add `step.outcome` required at close. Migration from Batch 1 mutable
column described in Section 4.3.

**Value delivered:** concurrent prose editing safe; typed step shapes
available. **Estimated effort:** 3-4 weeks.

### Batch 3 — prep + Loom integration (fleet useful)

**Prerequisite:** Loom URI spec (`2026-05-17-loom-uri-spec.md`)
published and ratified. **Prerequisite:** filigree planning-pack
deprecation plan (`2026-05-17-filigree-planning-deprecation.md`)
**ratified** — design approved, migration tool reviewed and merged in
draft form behind a feature flag. Batch 3 does **not** wait on the
deprecation plan's execution (Phase 1-4 of that doc), which is
explicitly sequenced *after* Batch 3 ships (sibling doc §6). Treating
"ratified" rather than "executing" as the prerequisite removes the
mutual wait the two docs would otherwise have on each other.

Batch 3 acceptance criteria include: the planning-pack deprecation's
migration tool successfully exercises Shuttle's Batch 3 MCP surface
end-to-end against a dogfood-DB sandbox (per sibling doc §8.1). This
is the migration-target-readiness check; it does not require Phase 1
(announce & freeze) to have begun.

`shuttle_attachments` table. `prepare_step`. `start_work` response
extension. Loom API surface for federation peers
(`/api/loom/shuttle/*`). TDD event kinds + ordering enforcement.
Contract tests against Loom fixtures.

**Value delivered:** fleet-scale agentic coordination. **Estimated
effort:** 3-4 weeks.

### Total

~12-17 weeks including Batch 0 (revised: Batch 0 is 3-5, Batches 1-3
are 3-4 each). The first user-deployable increment is **Batch 1**
(solo-agent value). Fleet-scale value lands in **Batch 3**.
Stakeholders should not treat Batches 1-2 as fleet-ready.

---

## 14. Provenance

Convergent output of four agent perspectives (constructive,
adversarial, multi-agent fleet, greenfield primitives-up) plus a formal
four-reviewer architectural panel. Full provenance is in the conversation
history; the design itself stands on its merits.
