# Filigree Planning-Pack Deprecation Plan

**Status:** Draft (2026-05-17)

> **Federation status (added 2026-06-05).** This plan depends on Shuttle and on
> the `loom://`/`shuttle://` URI scheme, both of whose federation status is now
> tracked authoritatively at the Loom hub. Per `~/loom/uri-scheme.md`: the
> `loom://` scheme is **formally closed** (superseded by SEI,
> `~/loom/sei-standard.md`), and the `shuttle://` milestone-reference convenience
> is **unresolved and low-stakes** because **Shuttle is a roadmap thought-bubble
> with no repo** (`~/loom/doctrine.md`, `~/loom/members/shuttle.md`) — so this
> deprecation has no concrete migration target today. The plan is retained as a
> design record; do not execute its `shuttle://`-dependent migration until a
> change-execution authority actually exists.

**Scope:** Sequencing and migration plan for retiring filigree's `planning` pack types when Shuttle assumes ownership of planning
**Sibling documents:**
- `2026-05-17-shuttle-design.md` — Shuttle architecture (the new home for planning)
- `2026-05-17-loom-uri-spec.md` — federation URI scheme (required for cross-component refs after migration)

---

## 1. Context

The Shuttle component (sibling doc) will own planning and execution in
the Loom federation. Filigree's existing `planning` pack overlaps
substantially with what Shuttle will provide, and continuing to ship
both produces two competing planning surfaces — one of the precise
failure modes Shuttle is meant to resolve.

This document specifies how filigree's `planning` pack is wound down
without breaking existing projects, dependent packs, or live data.

---

## 2. Current filigree pack landscape

Verified against `src/filigree/templates_data.py` as of 2026-05-17:

### 2.1 The `planning` pack

```
pack: planning
requires_packs: [core]
types (5):
  - milestone
  - phase
  - step
  - work_package
  - deliverable
relationships (4):
  - milestone_contains_phase   (phase.parent_id → milestone)
  - phase_contains_step        (step.parent_id → phase)
  - work_package_in_milestone  (work_package.parent_id → milestone)
  - deliverable_for_package    (deliverable depends_on work_package)
```

### 2.2 Packs that depend on `planning`

Verified against `src/filigree/templates_data.py`:

```
pack: release   (templates_data.py:1531)
requires_packs: [core, planning]
cross_pack_relationships:
  - release_delivers_milestone   (release → planning.milestone)
  - release_item_from_task       (release_item → core.{task,bug,feature})

pack: roadmap   (templates_data.py:823)
requires_packs: [core, planning]
cross_pack_relationships:
  - milestone_delivers_objective (planning.milestone → roadmap.objective)
```

Only `release_delivers_milestone` and `milestone_delivers_objective`
cross *into* planning — both reference **`milestone` specifically**, not
the other planning types. `release_item_from_task` is listed for
inventory completeness; it crosses release → core and is unaffected by
the planning rename. The work_package, deliverable, and step types
have no cross-pack consumers.

### 2.3 Live data in the filigree dogfood

As of 2026-05-17 the filigree project's own `.filigree/filigree.db`
contains the following planning-pack rows (verified by direct query of
the issues table at design time):

| Type | Total | Archived | Completed | Other |
|---|---|---|---|---|
| `milestone` | 6 | 3 | 3 | 0 |
| `phase` | 12 | 6 | 6 | 0 |
| `step` | 27 | 15 | 12 | 0 |
| `work_package` | 0 | 0 | 0 | 0 |
| `deliverable` | 0 | 0 | 0 | 0 |

All rows in the dogfood DB are in terminal states (archived or
completed) — no `open` or `in_progress` planning-pack work exists. This
is the easy case for migration; other projects using filigree may have
live (non-terminal) rows in any of the 5 types, and their migration
runs against a non-trivial workload. The migration tool must handle
both shapes.

**Caveat for cross-project parameterization.** Counts above are
specific to the filigree dogfood. Tests and acceptance checks in §8
that assert exact counts apply only to the dogfood sandbox; checks
that need to run against arbitrary other projects must be
parameterized over the per-project inventory the tool reports in its
`--dry-run` output.

---

## 3. The constraint

Naively deprecating the planning pack breaks five things, **silently**
in the current filigree implementation (none of these surface as a
loud load error today; the failure mode is orphaned relations and
blank type panels):

1. **`requires_packs` is declared but not enforced at runtime.**
   `templates.py:898` reads `enabled_packs` and skips disabled built-in
   packs without checking that any other loaded pack's `requires_packs`
   are satisfied. If `planning` is removed and `release` / `roadmap`
   remain enabled, both packs load anyway. Their cross-pack
   relationships (`release_delivers_milestone`,
   `milestone_delivers_objective`) end up referencing a `milestone`
   type that no longer has a template registered. **The failure mode
   is silent orphan-relations and broken UI rendering, not a loud load
   error.** Any phrasing in earlier drafts that implied "pack
   load fails" was empirically wrong.

2. **Default `enabled_packs` is hardcoded in three sites in `core.py`:**

   | Site | Current value |
   |---|---|
   | `core.py:337` (`ProjectConfig` defaults) | `["core", "planning", "release"]` |
   | `core.py:465` (`default_enabled` set) | `{"core", "planning", "release"}` |
   | `core.py:531` (`enabled_packs` fallback) | `["core", "planning", "release"]` |

   Renaming the pack without rewriting these defaults means new
   `filigree init` runs continue to enable a pack called `planning`
   that no longer exists; templates load partially and the dashboard
   renders blank type panels.

3. **Existing project configurations carry `enabled_packs: [..., "planning", ...]`** in
   their `.filigree.conf`. Even after a code rewrite of the defaults,
   existing projects keep their stale config value until rewritten.

4. **Existing data** of types milestone / phase / step / work_package /
   deliverable in any filigree-using project becomes orphaned —
   readable but with no template to validate against.

5. **Cross-pack relationship targets reference `milestone` by name.**
   Dropping the type silently breaks `release_delivers_milestone` and
   `milestone_delivers_objective`. Retaining `milestone` under a new
   pack name (Option C, §4.3) preserves the relationship targets only
   if the `to_types` strings in `templates_data.py` are also updated
   *and* loaded-DB internal template snapshots are reconciled — see §6.3.

---

## 4. Three deprecation options

### 4.1 Option A — Full removal

Drop the planning pack entirely. Migrate all 5 types' data into Shuttle.
Rewrite release and roadmap packs to either (a) reference Shuttle
objects via Loom URI, or (b) lose their milestone-coupling.

**Pros:** clean. **Cons:** maximally disruptive; release and roadmap
behavior changes; cross-pack relationships need cross-component
reformulation.

### 4.2 Option B — Coexistence (do nothing)

Keep the planning pack indefinitely. Document Shuttle as the recommended
planning surface for new projects; existing filigree planning data
stays where it is.

**Pros:** zero migration risk. **Cons:** two planning surfaces forever;
new projects face the "which do I use" question every time; Shuttle's
value proposition is undermined.

### 4.3 Option C — Narrow retention (recommended)

Retain `milestone` in filigree under a renamed pack scoped to
*release-coordination*. Deprecate the other 4 planning types (phase,
step, work_package, deliverable). Existing data in the deprecated
types either migrates to Shuttle or stays as legacy data marked
read-only.

**Pros:** preserves release/roadmap cross-pack relationships without
rewriting them; preserves filigree's identity as the ticketing +
coordination component (milestone = ship-tracking, not work-planning);
minimizes the migration surface. **Cons:** filigree retains a planning-
shaped artifact (milestone), which is a small responsibility-envelope
blur.

**This document recommends Option C** and specifies it below.

---

## 5. Option C in detail

### 5.1 Pack restructuring

Rename `planning` pack to `coordination`. Reduce its types to one:

```
pack: coordination
requires_packs: [core]
types (1):
  - milestone   (unchanged schema)
relationships:
  - (none — phase_contains_step, work_package_in_milestone,
     deliverable_for_package removed with their types)
```

Update dependent packs:

```
pack: release
requires_packs: [core, coordination]
cross_pack_relationships:
  - release_delivers_milestone   (release → coordination.milestone)

pack: roadmap
requires_packs: [core, coordination]
cross_pack_relationships:
  - milestone_delivers_objective (coordination.milestone → roadmap.objective)
```

`coordination` is a more honest name for what filigree owns post-Shuttle:
release tracking, milestone coordination, ship dates, gates. It is *not*
the work hierarchy.

### 5.2 Data migration paths per type

| Type | Live count (dogfood) | Migration target | Notes |
|---|---|---|---|
| `milestone` | 2 | **stays in filigree** (renamed pack) | No data move. The milestone retains its id, relationships, fields. |
| `phase` | unknown — varies by project | **migrate to Shuttle as `stage`** | 1:1 schema map. parent_id (formerly to milestone) becomes a Shuttle plan; the source milestone gets a `shuttle://...` attachment linking to it. |
| `step` | unknown | **migrate to Shuttle as `step` (kind=free_text)** | Existing filigree-step schema fields (target_files, verification, implementation_notes, done_definition, sequence) map to Shuttle's step fields. `kind` defaults to `free_text`; agents can promote later. |
| `work_package` | unknown | **migrate to Shuttle as `stage` (separate flavor)** OR **keep as filigree custom type** | Decision: **migrate to Shuttle `stage`**. Work packages are stage-shaped (assignable bundles); the deliverable relation (below) carries the differentiation. |
| `deliverable` | unknown | **migrate to filigree as a new `release` pack type** | Deliverables track concrete outputs (code, docs, artifacts) and are release-coordination artifacts, not work-planning. They fit better next to `release` than next to Shuttle's work tree. |

### 5.3 Relationship migration

| Old relationship | New form |
|---|---|
| `milestone_contains_phase` (planning) | Removed. Phases become Shuttle stages whose parent is a Shuttle plan, not a filigree milestone. Linkage via Loom attachment (`coordination://milestone/{id}` attached to the Shuttle plan). |
| `phase_contains_step` (planning) | Becomes Shuttle's stage→step parent relation (intra-Shuttle, not cross-component). |
| `work_package_in_milestone` (planning) | Becomes Loom attachment (Shuttle stage attached to filigree milestone). |
| `deliverable_for_package` (planning) | Becomes Loom attachment (filigree-release-pack deliverable attached to Shuttle stage). |
| `release_delivers_milestone` (release pack, cross-pack) | Unchanged; still references `coordination.milestone`. |
| `milestone_delivers_objective` (roadmap pack, cross-pack) | Unchanged; still references `coordination.milestone`. |

---

## 6. Sequencing

Migration cannot run before Shuttle exists. Shuttle Batch 3 cannot ship
before the Loom URI scheme is published. The order is fixed:

```
Loom URI spec (sibling doc) — ratified
  ↓
Shuttle Batch 0 (loom-core extraction) — shipped
  ↓
Shuttle Batch 1 (skeleton, solo-agent useful) — shipped
  ↓
Shuttle Batch 2 (prose-blocks + step shapes) — shipped
  ↓
Shuttle Batch 3 (Loom integration) — shipped, MCP surface stable
  ↓
THIS PLAN — filigree planning-pack deprecation begins (Phase 1 announce)
```

The dependency between this plan and Shuttle is **one-way**: this plan
consumes Shuttle's Batch 3 MCP surface as a fully shipped capability.
Shuttle does not consume any artifact from this plan. The Shuttle
design doc (sibling `2026-05-17-shuttle-design.md` §13) prerequisites
Batch 3 on the planning-pack deprecation having been **ratified**
(design approved, migration tool reviewed) — *not* on this plan being
executed or in progress. Ratification before Shuttle Batch 3 ships is
how Shuttle's acceptance criteria can include "migration target
readiness" without creating a circular wait.

This plan's Phase 1-4 execution runs *after* Shuttle Batch 3 ships
because:

- Migration targets (Shuttle stages, steps) must exist to receive data.
- Loom attachments (the new replacement for cross-relationships) need
  the Loom URI scheme operational.
- The migration tool itself uses Shuttle's MCP surface to create the new
  objects.

### 6.1 Phases of the deprecation

**Phase 1 — Announce & freeze (Week 0).** Filigree release-notes
announce planning-pack deprecation. New filigree installations get a
soft warning when creating phase/step/work_package/deliverable types.
Existing projects unaffected.

**Phase 2 — Migration tool ships (Week 2-3).** A new filigree CLI verb
ships:

```
filigree migrate-to-shuttle [--dry-run] [--types phase,step,work_package,deliverable]
```

The tool:
- Reads existing planning-pack data from the filigree project's DB.
- Connects to the project's Shuttle instance (must be running locally).
- For each migrated object, creates the corresponding Shuttle object,
  attaches cross-references via Loom URIs, and marks the original
  filigree object as `migrated_to=shuttle://...` (a new field on the
  legacy planning types — see 6.2).
- Emits a migration manifest to `.filigree/migrations/2026-XX-XX-shuttle.json`
  for rollback.
- `--dry-run` reports what would migrate without writing.

**Phase 3 — Pack rename (Week 4-5).** Filigree ships a release that
renames `planning` → `coordination` and removes the deprecated types
from the pack. Existing project DBs retain the old type rows but with
no template; the dashboard renders them under a "Legacy planning types"
panel with the migration tool's CTA. New installations of the new
filigree version get only `milestone` in `coordination`.

Phase 3 ships as a single release with the following coordinated edits:

1. **Pack data — `src/filigree/templates_data.py`:**
   - Rename the `planning` pack entry to `coordination`; drop the
     `phase`, `step`, `work_package`, `deliverable` types and their
     intra-pack relationships.
   - Update `requires_packs` from `["core", "planning"]` to
     `["core", "coordination"]` on the `release` pack
     (`templates_data.py:1531`) and the `roadmap` pack
     (`templates_data.py:823`).
   - Update `cross_pack_relationships` target descriptions / docstrings
     on `release_delivers_milestone` and `milestone_delivers_objective`
     to reference `coordination.milestone`.

2. **Hardcoded defaults — `src/filigree/core.py`:**
   - `core.py:337` (`ProjectConfig` defaults) — rewrite
     `["core", "planning", "release"]` →
     `["core", "coordination", "release"]`.
   - `core.py:465` (`default_enabled` set) — same substitution.
   - `core.py:531` (`enabled_packs` fallback) — same substitution.
   - Add a contract test (`tests/core/test_default_packs.py`) that
     parses `core.py` defaults and asserts no remaining `"planning"`
     literals, so future drift fails CI rather than silently breaks.

3. **`requires_packs` enforcement — `src/filigree/templates.py`:**
   - At pack load time, after enabled packs are resolved, verify each
     loaded pack's `requires_packs` is satisfied; on miss, emit a
     `WARN` log and surface a `PackDependencyWarning` on the
     `doctor` output.
   - This is **new enforcement**, not just a rename — it turns the
     silent failure mode named in §3 into an observable one going
     forward. The implementation lives in this release because Phase 3
     is when the failure mode would otherwise fire for existing users.

4. **Project config migration — `.filigree.conf` rewrite:**
   - Filigree on startup detects `enabled_packs` containing
     `"planning"` against a templates manifest that no longer ships
     a `planning` pack. It rewrites the config in place to substitute
     `"coordination"`, leaves a `.filigree.conf.bak.YYYY-MM-DD`
     backup, and logs the migration at INFO.
   - The rewrite is idempotent: a config already containing
     `"coordination"` is left alone.
   - For projects that enabled `planning` *plus* a hand-crafted custom
     pack referencing `planning`, the rewrite refuses (config is left
     unchanged, error surfaced to operator). This is rare; the
     operator handles it manually.

5. **Schema-snapshot reconciliation — see §6.3.**

6. **Dashboard rendering:**
   - `Legacy planning types` panel surfaces any rows of the four
     dropped types and links to the migration tool.

**Phase 4 — Read-only legacy (indefinite).** Filigree retains the
ability to *read* legacy planning-type rows but refuses new writes.
This is permanent backward compatibility for projects that never
migrate.

### 6.2 Schema additions to support migration

```
-- On the legacy planning types (phase, step, work_package, deliverable):
ALTER TABLE issues ADD COLUMN migrated_to text;  -- Loom URI of the new Shuttle object
```

A non-null `migrated_to` value means:
- The row is read-only in filigree.
- The dashboard surfaces the link to the Shuttle target.
- The migration tool can be re-run to verify consistency.

### 6.3 Schema-snapshot reconciliation for existing DBs

The architectural review asked: when an existing project's
`filigree.db` was created against the old `planning` pack and has
`release`/`roadmap` enabled, does the cross-pack relationship
`release_delivers_milestone` survive the rename?

**Filigree stores no per-row "loaded template snapshot."** Templates
are read from the packs module at startup and held in memory; rows in
the `issues` table store `type` (e.g., `milestone`) but not the pack
they came from. Cross-pack relationships are resolved at query time
against the in-memory template registry, not against stored joins.

**Consequence:**

- Phase 3's edits to `templates_data.py` and the in-memory registry are
  sufficient for `release_delivers_milestone` to keep resolving — the
  relationship's `to_types: ["milestone"]` string matches whatever pack
  currently owns the `milestone` type, and after Phase 3 that pack is
  `coordination`.
- No row-level DB migration is required for `milestone` rows or for
  rows in `release`/`roadmap`/`releases.release_item` that reference
  milestones. The `issue.type` column is unchanged; the `parent_id`
  / `depends_on` references are unchanged.
- The `migrated_to` ALTER (§6.2) is the only schema migration Phase 3
  ships, and it applies only to the four deprecated types.

**Verification test** (`tests/integration/test_pack_rename_preserves_release_refs.py`):
load a pre-rename DB snapshot (the dogfood DB or a fixture), run the
Phase 3 release on it, and assert:

- `release_delivers_milestone` queries return the same edge set as
  before the rename.
- `milestone_delivers_objective` queries return the same edge set.
- The 6 dogfood milestones (§2.3) are still queryable by type.

### 6.4 Rollback

Phase 2 (migration tool) is reversible:

```
filigree migrate-to-shuttle --rollback --manifest .filigree/migrations/2026-XX-XX-shuttle.json
```

The rollback:
- Reads the manifest (per-object before-state).
- Deletes the corresponding Shuttle objects (via Shuttle MCP).
- Clears `migrated_to` on filigree rows.
- Restores any prior status / fields if the rollback runs within a
  reversibility window (default 7 days; configurable).

Beyond the reversibility window, Shuttle objects may have accumulated
events / prose / attachments that the rollback cannot recreate.
Rollback after the window is refused with a clear message.

Phase 3 (pack rename) is not reversible — the pack-rename release
ships as a major version bump. Projects that need the legacy pack name
stay on the prior filigree version. This is documented as a hard
boundary.

---

## 7. Compatibility and bridges

### 7.1 What filigree retains

- The `milestone` type, in pack `coordination`.
- All `release` pack functionality unchanged.
- All `roadmap` pack functionality unchanged.
- All non-planning packs (`requirements`, `risk`, `incident`, `debt`,
  `spike`-the-filigree-type, others) unchanged.
- The `/api/loom/*` federation surface (issues, files, analytics,
  releases routers).

### 7.2 What moves to Shuttle

- Work hierarchy: phase → stage, step → step (with `kind=free_text`
  default), work_package → stage.
- Deliverable → moves to filigree's `release` pack as a new type
  (release-coordination artifact, not work-planning).

### 7.3 Cross-component reference

Where filigree previously held intra-pack parent-child links
(milestone→phase→step), the post-migration topology is:

- Filigree milestone ←Loom attachment← Shuttle plan
- Shuttle plan → Shuttle stage → Shuttle step (intra-Shuttle)
- Shuttle stage ←Loom attachment→ filigree release.deliverable (when
  produced)

Cross-component refs use the Loom URI scheme per
`2026-05-17-loom-uri-spec.md`.

---

## 8. Test strategy

| Surface | Test path | Validates |
|---|---|---|
| Migration tool dry-run | `tests/integration/test_migrate_dry_run.py` | manifest matches plan; no DB writes |
| Migration tool execution | `tests/integration/test_migrate_execute.py` | each type's data lands in Shuttle correctly; `migrated_to` populated; manifest written |
| Rollback within window | `tests/integration/test_migrate_rollback.py` | Shuttle objects deleted; filigree state restored to manifest's before-state |
| Rollback outside window | `tests/integration/test_migrate_rollback_refused.py` | clear error; suggests forward-fix path |
| Pack rename | `tests/integration/test_pack_rename.py` | new installation has `coordination` pack with only `milestone`; old data readable as legacy |
| Cross-pack relations preserved | `tests/integration/test_release_roadmap_after_rename.py` | release.release_delivers_milestone and roadmap.milestone_delivers_objective still resolve |
| Filigree-Shuttle round trip | `tests/integration/test_filigree_shuttle_roundtrip.py` | migrate phase → Shuttle stage; Loom attachment from filigree milestone resolves to Shuttle plan; reverse query works |

### 8.1 Pre-deployment validation

Before any project runs `filigree migrate-to-shuttle` for real, the
team runs the tool against a copy of the dogfood `.filigree/filigree.db`
in a sandbox and verifies (against the §2.3 inventory: 6 milestones +
12 phases + 27 steps + 0 work_packages + 0 deliverables):

- All 6 milestones survive in place (Option C keeps milestone in the
  renamed `coordination` pack; they shouldn't move, but assert it).
- All 12 phases land as Shuttle stages with matching prose, status, and
  parent linkage via Loom attachments.
- All 27 steps land as Shuttle steps with `kind=free_text` default and
  the field mapping from §5.2.
- `work_package` and `deliverable` migration paths are *exercised* via
  fixtures even though the dogfood has no live rows — otherwise their
  code paths only run for the first project that has them, in
  production.
- The migration manifest is reproducible across two `--dry-run`
  invocations (byte-identical except for timestamps).
- Rollback returns the sandbox to bit-identical state vs. pre-migration
  snapshot.

The exact counts above are **dogfood-specific**. The same validation
suite parameterized by inventory should run against at least one
non-dogfood project sandbox before Phase 3 ships; the test asserts
"every type X has the same count post-migration as the `--dry-run`
manifest predicted," not specific numbers.

---

## 9. Risks

### 9.1 Shuttle isn't ready

If Shuttle's Batch 3 misses its target, this plan stalls. Filigree's
planning pack continues unchanged; no harm done. The migration tool
fails fast if it cannot connect to a Shuttle instance.

### 9.2 Loom URI scheme changes after migration

If the Loom URI grammar changes after migration data exists, stored
`migrated_to` values and Shuttle's attachment URIs may need rewrites.
Sibling doc `2026-05-17-loom-uri-spec.md` flags URI-scheme stability as
a one-way door; migration must wait for that stability.

### 9.3 Projects that never migrate

The Phase 4 read-only legacy mode is indefinite. Some projects may
never migrate. Filigree's CI must include a long-tail test that loads
a v1.x project DB (with planning pack types) and confirms it still
opens, reads, and displays legacy data.

### 9.4 The work_package decision

Section 5.2 routes `work_package` to Shuttle's `stage` type. An
alternative is to retain `work_package` in filigree's `coordination`
pack as a stage-shaped artifact attached to a milestone. The
recommended path moves it to Shuttle because work_packages are part of
the work hierarchy that Shuttle owns; their assignability and
deliverable-coupling are execution concerns. If post-Shuttle usage
reveals that work_packages are used for coordination more often than
execution, this can be re-evaluated as a canary.

### 9.5 Deliverable home

Section 5.2 routes `deliverable` to filigree's `release` pack as a new
type. The alternative is keeping it in Shuttle. The recommended path
keeps it filigree-side because deliverables are concrete outputs
(release artifacts, signed binaries, documents) whose lifecycle is
release-shaped, not work-shaped. Shuttle stages produce deliverables;
filigree tracks them as release artifacts.

---

## 10. Open questions

### 10.1 Should `work_package` retention be an opt-in flag?

Some projects use work_packages as the primary coordination unit
without phase/step decomposition. Forcing them to Shuttle removes a
working pattern. **Recommendation:** offer
`filigree migrate-to-shuttle --keep-work-packages`; the type stays in
filigree's coordination pack alongside milestone. Decide based on
project survey before Phase 3 ships.

### 10.2 Migration tool: filigree-side verb or Shuttle-side verb?

The verb makes filigree-side calls (read from filigree DB) and
Shuttle-side calls (create new Shuttle objects). It could live in
either binary. **Recommendation:** filigree-side. The migration is
filigree-initiated cleanup; the user runs `filigree migrate-to-shuttle`
once and is done. Shuttle remains agnostic about its data's prior
home.

### 10.3 Do we need a Shuttle-side import verb?

If filigree-to-Shuttle migration is the only entry path, a generic
`shuttle import` verb is overkill. **Recommendation:** no generic
import; the filigree migration tool calls Shuttle's standard
`create_plan` / `add_stage` / `add_step` verbs. Anyone wanting to
migrate from a non-filigree source writes their own bridge using the
same Shuttle MCP surface.
