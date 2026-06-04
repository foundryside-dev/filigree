# Filigree — the road to first-class (roadmap & final form)

**Date:** 2026-06-01  
**Status:** Living reference (roadmap; companion to the Loom goal-state case study)  
**Scope:** Filigree's **final form** as a first-class, enterprise-capable issue/workflow
authority — and the staged path to it — given the Loom operating model and invariants
settled across the 2026-06-01 design sessions. Sibling to
`2026-06-01-loom-goal-state-case-study.md` (the suite umbrella) and
`2026-06-01-loom-stable-entity-identity-conformance.md` (the SEI keystone).

> **The thesis filter governs every line of this roadmap.** Filigree is done and
> frozen at v2.3.0 — "done" means the published wire surface is stable, not that
> the tool stops improving. Near-term work must be non-breaking (additive or
> internal) within the freeze. Breaking-wire items and deprecated-alias removals
> fence to next-major. The governed-lifecycle combo is an **opt-in layer** legis
> adds from outside; Filigree provides the substrate and never builds governance
> natively. A solo project pays nothing for layers it does not switch on.

---

## 0. The final form, in one sentence

> Filigree becomes the **best agent-operated issue and workflow authority in
> existence** — proven-sound, richly introspectable, agent-programmable via its
> open workflow-pack grammar — **and** a first-class Loom citizen: SEI-conformant,
> freshness-honest on both axes, the open-work layer of every dossier call, and
> the substrate the governed lifecycle runs on when `legis` exists.

"First-class" has **two co-equal halves.** The tracker-quality bar comes first;
the suite-integration bar comes second. The first half is mostly Filigree's to
finish alone; the second is largely sibling-gated.

---

## 1. Half 1 — the tracker itself (the foundation; mostly Filigree-autonomous)

Filigree is already a first-class tracker. Half 1 is the internal hardening that
keeps it the best-in-class agent coordination platform *without touching the frozen
surface*.

### 1.1 Agent-platform performance & concurrency

The highest-priority un-gated item is **per-thread SQLite connection pooling**
(`filigree-d4237f486f`, P1) — eliminating the serialisation bottleneck that
prevents parallel scan-result ingestion. This is the foundation for any multi-agent
scenario where findings and issue mutations arrive concurrently. Gated on nothing;
owns it alone.

### 1.2 Session/run checkpoints

Structured checkpoints (beyond free-form actor strings) let agent orchestrators
resume mid-session work reliably and give `legis`'s future custody model a
well-defined handoff seam. (`filigree-c2009921cf`, P2.) Non-breaking addition.

### 1.3 Agent-facing consumer surface

The agent-systems effectiveness review (`filigree-18bd3b8c98`, P2) covers
workflow correctness, attribution accuracy, stats, deletion semantics, and surface
consolidation — the housekeeping that keeps MCP consumer behavior predictable.
Additive/internal; no wire break.

### 1.4 The workflow-pack system: Filigree's agent-programmable extension plane

The workflow-pack / template system is the most important structural fact in Half
1, and the one most easily overlooked in a backlog list. It is Filigree's
**agent-programmable extension plane**: agents define new issue types, state
machines, required fields, and transition rules — the builtins are preloaded
defaults in an open grammar. This is the same seam shape the goal-state doc names
for Wardline's extensible trust grammar, Clarion's Transport, and elspeth's plugin
architecture. The pack system *already ships* and is the reason an agent (not a
human configuring a form) can adapt Filigree to any project topology. Future work
here (project-declared entity schemas, `filigree-8ed24e9999`) extends this seam
further without a wire break.

### 1.5 Surface polish (non-breaking; near-term)

- `TransitionMode` enum replacing the backward boolean (`filigree-9b4bb6e52e`)
- `safe_message` parity for claim/transition errors (`filigree-d25e75cebf`)
- `migrate-registry --to local` rollback path (`filigree-73d27aed7b`)
- Auth-posture reconciliation with Clarion (`filigree-30cd35bcb9`)
- Suppressing the benign "status not updated" warning for unknown `scan_run_id`
  (`filigree-c0dbf8e5a4`)

### 1.6 Fenced to next-major (breaking-wire items)

These are real work, but they are **breaking wire changes** and belong after the
freeze relaxes or in a new named generation:

- MCP tool namespace rationalization (`filigree-7771610917`) — the tool is
  explicitly titled "breaking wire change." It waits for the next generation
  milestone.
- Removal of deprecated `status_name_counts` / `status_category_counts` alias keys
  (`filigree-e4181ae767`) — tagged P4 "next major" for exactly this reason.

---

## 2. Half 2 — first-class Loom citizen (the layers; mostly gated)

### 2.1 SEI conformance *(gated on Clarion shipping SEI)*

**The trap stated plainly.** Filigree stores `clarion_entity_id` as an opaque
string and deliberately does not parse Clarion's entity-id grammar (see
`types/core.py:make_entity_id`). This makes Filigree *able* to conform to SEI with
zero breaking wire change — but that is the start of the work, not the end of it
(SEI spec §0.1). Filigree is conformant only once:

1. The one-time **locator→SEI value migration** (SEI spec §7) has run — every
   existing `entity_associations.clarion_entity_id` row has its stored locator
   string replaced with the corresponding SEI. The column name, the wire shape,
   and the storage mechanism are **unchanged**; only the *value format* changes.
   Opacity is what makes this safe across a frozen surface: consumers who honour
   the opacity contract (never parsing or pattern-matching the string) see no
   break; only consumers who had silently depended on the three-segment locator
   grammar are affected — and that was already forbidden.
2. The **§8 conformance oracle** passes — Filigree participates in the shared
   fixture-based test suite that demonstrates conformance, never merely asserts it.

The **two-axis status model** (SEI spec §2.1) is the other side of conformance.
Filigree's `content_hash_at_attach` already provides the **content axis** cleanly:
FRESH means the entity body is unchanged since attach; STALE means it has changed —
re-verify the association. The **identity axis** (ALIVE / ORPHANED) now lives in
Clarion's `resolve_sei` — Filigree exposes the stored SEI on its wire surface and
lets Clarion answer the identity question. Together these give every binding a
clean, no-false-green truth: "same entity + unchanged code" requires both axes;
neither infers the other. Filigree's no-false-green contribution is that it never
serves an association without the stored `content_hash_at_attach` that lets the
caller inspect freshness, and it never silently re-points an association.

**Groundwork that can start now (shape-independent).** The oracle participation
scaffolding — a test harness in `tests/federation/` that can run against a
reference Clarion — is shape-independent groundwork that can be built before SEI
locks. The two-axis mapping documentation (what ORPHAN means from Clarion's
perspective vs. what STALE means from Filigree's) can also be written now. The
actual backfill *run* and the value migration wait for Clarion to ship SEI.

### 2.2 Dossier participation *(gated on Clarion SEI + Wardline dossier)*

Filigree's open-work layer of the dossier — "what issues are open on this entity?"
— is the information Wardline's one-call mastery read (`dossier(entity)`) needs
from Filigree. Today, the Wardline→Filigree binding can already deliver a finding
→ issue link; the Clarion+Filigree binding (issue bound to a live code entity)
orphans on rename/move. With SEI, `dossier(entity)` can return a durably-keyed
list of open Filigree issues as part of the complete envelope. Filigree's half
(serving associations by SEI) is already present via `list_associations_by_entity`;
the gate is Clarion SEI landing so the stored value is an SEI the dossier can key
on.

### 2.3 Governed issue lifecycle *(gated on legis existing)*

Filigree already owns the substrate: a rich verification state machine, soft
transitions, claims and heartbeat, dedup'd audit events, and a workflow-pack grammar
that can express any issue lifecycle. The **Filigree + legis** combination is legis
governing *those* transitions from outside — adding sign-offs, RTM linkage, custody
attestations, and graded enforcement modes (block+escalate or surface+override) as
an opt-in layer, without Filigree building governance natively. One judge, not two:
Filigree enforces its own workflow rules; legis enforces suite-level governance on
top of them.

The audit spine legis needs is Filigree's dedup'd events table — every mutation is
already an attributable, ordered event. Lineage on entity associations (the
`entity_association_added` / `entity_association_refreshed` / `entity_association_removed`
events) feeds the custody model for the code↔work binding.

---

## 3. Staging — by capability milestone and dependency gate

| # | Milestone | Gate | Filigree's position |
|---|---|---|---|
| 1 | **Connection pool + internal hardening** — P1 SQLite pool, session checkpoints, agent DX fixes, surface polish | none (autonomous) | owns it end-to-end |
| 2 | **Workflow-pack extension** — entity schema declarations, pack system deepening | none (autonomous) | owns it end-to-end; **highest-leverage un-gated item** |
| 3 | **SEI oracle scaffold** — federation test harness, two-axis mapping docs | none (shape-independent) | owns it; backfill *run* waits for Clarion |
| 4 | **SEI value migration** — locator→SEI backfill + oracle pass | Clarion ships SEI | thin value migration; the stored string changes; nothing else does |
| 5 | **Dossier participation** — issue associations as the dossier's open-work layer | Clarion SEI + Wardline dossier | association lookup already exists; keying on SEI is the only gate |
| 6 | **Governed lifecycle** — legis governs Filigree transitions opt-in | `legis` exists | substrate already ship-ready; no Filigree build work at that point |

**Honest gating picture.** Milestones 1–3 are Filigree's to finish alone.
Milestone 4 (the backfill) is a thin data-migration that Filigree owns to execute,
but Clarion must ship SEI first for the migration to have a target. Milestones 5–6
wait entirely on siblings; Filigree's half of each is already present. The
dependency is not "Filigree is incomplete" — it is "the suite needs to meet
Filigree where it is."

---

## 4. North Star — work-identity at full generality

The v1 SEI milestone and the governed-lifecycle combination are *sequencing toward*
the fullest form of the idea, not the ceiling of it.

The most general form is: **any work-tracking producer feeds the same store.**
Filigree today tracks code-level findings from Wardline and entity bindings from
Clarion. At full generality, any tool that produces a work item — a design review,
a governance attestation, a deployment gate — binds to the same durable entity
identity and flows into the same governed lifecycle. The issue↔entity binding
survives rename and move in any language a Clarion plugin can describe; the
governed lifecycle spans the whole suite and any producer that conforms.

Filigree's role at that horizon is what it is today, scaled: the **open-grammar
authority for work state**, whose workflow-pack system is the extensible seam every
producer writes to. The base stays weightless and self-installing; the extension
plane is agent-authored.

The guiding stance applies: under-reach is also a failure mode. A first-class
tracker nobody reaches for is not being used.

### 4.1 A note on the abandoned Loom-URI spec

`docs/plans/2026-05-17-loom-uri-spec.md` is now formally superseded by SEI (SEI
spec §0.2). The Loom URI effort was right about *what* (stable cross-tool identity)
and over-built in its *mechanism* (registry, multi-fetch, URI grammar), which is
why it never shipped. SEI is the product-grade form of the same idea. No
implementation work on Loom-URI is warranted; the spec doc is retained as a design
artifact (it documents what was learned) but carries a superseded status.

---

## 5. The throughline

Every item above is an **opt-in layer** or an internal hardening that preserves
the frozen surface. The base stays frozen and weightless; the agent drives; the
human supervises from the loop's edge. A project that uses Filigree standalone gets
the best agent-operated issue tracker available, installs itself, needs zero human
configuration, and pays nothing for any layer it does not switch on. A team that
needs identity-durable issue bindings switches on SEI conformance (a one-time
backfill). A team that needs governed issue lifecycle switches on legis. That is
enterprise/first-class on Filigree's terms.

---

## Appendix A — SEI conformance position (deliverable B)

### A.1 Confirmed obligations (SEI spec §5)

Filigree's obligations at SEI conformance:

1. **No code change to store SEIs.** The `entity_associations.clarion_entity_id`
   column is `TEXT NOT NULL`; Filigree stores whatever opaque string the caller
   provides; the column name and wire shape are unchanged. The stored *value*
   changes from a locator to an SEI — that is the entirety of the storage work.
2. **One-time locator→SEI backfill (§7).** Every existing association row must
   have its locator resolved to an SEI via Clarion's `resolve(locator)` endpoint
   and rewritten. Locators that no longer resolve (already-orphaned by past
   renames) are flagged ORPHAN for review — consistent with the no-false-green
   ethos, never silently dropped.
3. **Oracle pass (§8).** Filigree participates in the shared conformance test
   suite. "Conformant" is a proven fact, not a claim.
4. **Content axis.** `content_hash_at_attach` provides the content-axis FRESH/STALE
   signal; this is unchanged. The identity-axis ALIVE/ORPHANED is now Clarion's
   `resolve_sei`'s answer, surfaced via the stored SEI. Together they satisfy the
   no-false-green obligation on the two-axis model.

### A.2 Concrete emerging requirements (for SEI spec shape, before lock)

These are real Filigree-specific constraints that should be reflected in the SEI
spec or the backfill protocol before lock:

**REQ-F-01 — `affected_entities` wire format after migration (HIGHEST PRIORITY)**

F5's deletion signal — the `issue_deleted` record on `GET /api/loom/changes`
(schema v21) — surfaces the `affected_entities` array carrying `clarion_entity_id`
strings from the associations cascade. This array is on a **shipped wire contract**
that federation consumers (Clarion, Wardline) use to reconcile mirrored bindings
on deletion. After the suite-wide backfill, these strings will be SEIs — but the
spec does not currently state *when* this transition happens or whether consumers
must tolerate a mixed-format feed during the migration window.

The SEI spec should clarify: does the `affected_entities` payload switch to SEIs
atomically (i.e., only after all rows have been backfilled), or is a mixed feed
(some locators, some SEIs) a supported consumer obligation? Filigree cannot
inspect or pattern-match the strings to distinguish the two (opacity is the
discipline). This must be resolved before the backfill is run in production, because
it determines whether consumers need a migration window or can rely on an atomic
cutover.

**REQ-F-02 — `resolve(locator)` must reject SEI-shaped inputs cleanly**

The backfill (§7) rewrites `clarion_entity_id` values in place. Filigree owns
backfill progress-tracking (a rowid cursor or migration-state side table) and does
not need Clarion to add a generation marker. The cross-tool ask is narrower:
**`resolve(locator)` must return a clean error — not a mis-resolution — when handed
an already-migrated SEI string.** If a retry or a re-run ever passes an SEI-shaped
value back to `resolve`, the correct result is a documented "not a valid locator"
rejection, not a silent mis-key. Filigree cannot distinguish locators from SEIs by
inspection (opacity is the discipline); safe idempotency of the backfill depends
entirely on `resolve`'s input-validation contract.

The SEI spec should state explicitly whether `resolve(locator)` rejects SEI-format
inputs as invalid. If yes, retryable and resumable backfill is proven; if
unspecified, Filigree must treat a partially-run backfill as permanently incomplete
and cannot safely resume without an out-of-band state store.

### A.3 Acknowledgements

- **Loom-URI superseded.** ADR-029 entity-associations (the shipped mechanism)
  stands; only the *identity value* carried becomes an SEI. The Loom-URI scheme
  (`docs/plans/2026-05-17-loom-uri-spec.md`) is formally closed (SEI spec §0.2).
- **No grandfathering.** Filigree is frozen, but frozen ≠ grandfathered. The §8
  oracle pass is mandatory.
- **Shape-independent groundwork starts now.** Oracle scaffolding and two-axis
  documentation are not gated on Clarion shipping SEI; they are the prep work
  Filigree does while Clarion builds the authority side.
