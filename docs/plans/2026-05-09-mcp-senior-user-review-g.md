# MCP Senior-User Friction Review — review-g (2026-05-09)

Reviewer: agent driving the live `mcp__filigree__*` surface against
`/home/john/filigree/.filigree/`. Scratch artefacts labelled
`mcp-review-scratch` + `cluster:mcp-review-g`; promoted-from-scratch issues
also captured. Cleanup is performed after this report is written.

This run is independent of (and parallel to) another agent driving the same
prompt. To avoid stepping on its findings I exercised the surface with a
focus on **what's still broken after the review-d/e/f fixes landed** and on
**under-driven tools** (plan tree, annotation lifecycle, power tools).

## 1. Executive summary

The MCP surface is mostly in good shape — the headline fixes from
review-d/e/f landed (`undo_last` of `close_issue` is one call, `release_claim`
no longer strands a wip issue, `close_issue` no longer bypasses workflow,
`heartbeat_work` no longer requires an explicit actor, `get_blocked` now
includes wip issues, `source_finding_id` is populated on auto-observations).
The two findings that hurt most this run are: **(a)** *silent parameter
ignore* — at least three tools (`get_ready`, `annotate_file`, `export_jsonl`)
silently accept and discard unknown properties because their JSONSchemas
don't set `additionalProperties: false`, so an agent passing `label=...` to
`export_jsonl` exports the whole 4243-record DB instead of the 4 rows it
asked for; and **(b)** the much-discussed *write-path enforcement asymmetry*
is now resolved — but only for the opt-in path. Pass `expected_assignee` and
`update_issue`/`add_comment`/`add_label`/`close_issue` all return clean
`CONFLICT`. Forget to pass it (i.e., write the same code you wrote yesterday)
and any actor still rewrites any issue.

## 2. Coverage

I drove these workflows end-to-end:

- **A. Discovery / pickup** — `get_mcp_status`, `session_context`,
  `get_summary`, `get_schema`, `get_ready` (with several spurious filter
  params), `get_stats`, `get_critical_path`, `get_metrics`,
  `get_workflow_statuses`, `list_packs`, `list_types`, `get_template`,
  `get_workflow_guide`, `get_label_taxonomy`, `get_stale_claims`.
- **B. Triage / find work** — `search_issues` (hyphen + bracket queries),
  `list_issues` (label filter), `list_observations` (file_id filter),
  `list_findings`, `list_files`.
- **C. Claim → progress → finish** — `start_work`, `claim_issue` (as a
  different actor), `heartbeat_work` (no actor — verifies review-d/e fix),
  `add_dependency` (incl. `get_blocked` includes-wip verification),
  `release_claim` (verifies review-d/e strand fix), `update_issue`
  (`triage→closed` workflow rejection — verifies review-d fix), `close_issue`
  (workflow rejection — same), **write-asymmetry probes both *with* and
  *without* `expected_assignee`**, `undo_last` of `close_issue` (verifies
  review-f F2 single-event fix), `get_valid_transitions`, `get_issue_events`.
- **D. Plan tools** (under-driven in prior reviews) — `create_plan` (with
  same-phase int deps and cross-phase `"p.s"` notation), `add_plan_step`,
  `move_plan_step`, `label_plan_tree`, `label_subtree`,
  `retarget_plan_dependency`, `get_plan`.
- **E. Annotations + finding lifecycle** (under-driven in prior reviews) —
  `annotate_file` (incl. parameter-name probes), `link_annotation`
  (with relationship enum), `supersede_annotation`, `promote_annotation`,
  `carry_forward_annotation`, `report_finding`, `update_finding`,
  `promote_finding`.
- **F. Power tools** — `compact_events` (dry-run), `get_changes` (with
  cursor), `reload_templates`, `export_jsonl` (with three path variants).

I did not drive: `trigger_scan*`, `preview_scan`, `get_scan_status`,
`reclaim_issue`, `import_jsonl`, `restart_dashboard`, `unlink_annotation`,
`update_annotation`, `resolve_annotation`, `dismiss_observation` (all
exercised in prior reviews).

## 3. Per-workflow walkthrough

### A. Discovery / pickup

`get_mcp_status` returned `{"status": "ok", "schema_compatible": true,
installed=12, db=12}` — no `schema_mismatch` theatre this run. Headline
discovery loop (`session_context` → `get_summary` → `get_ready
--include_context`) is the right shape.

Two pieces of friction were intrinsic to the surface, not to the data:

- `get_ready` declares only `include_context` in its inputSchema; passing
  `type=task`, `priority_min=3`, `limit=2`, or even `garbage_param=...`
  returns the same full unfiltered list every time. **The schema does not
  set `additionalProperties: false`.** See finding G1.
- `get_stale_claims` returned an `archived` issue with `closed_at` set —
  see finding G3.

### B. Triage / find work

`search_issues` no longer chokes on hyphens or bracket-prefixed tokens —
`mcp-review-g` and `[mcp-review-g]` both returned my four scratch issues
correctly. The `Scratch task` query, however, still returns archived rows
from prior reviews (`status: "archived"` mixed with my live `open` rows)
because `search_issues` has no status / category filter — see finding G4
(carried forward from review-d/e).

`list_observations` with `file_id` filter returned cleanly. `list_findings`
default-pagination envelope (`{items, has_more, next_offset?}`) is consistent
with sibling list tools.

### C. Claim → progress → finish — the verification run

#### Things that ARE fixed (regression-free)

- **`undo_last` of `close_issue` is one call** (review-f F2). I closed
  scratch task B with `reason="..."`, then called `undo_last` once. Result:
  `status: "open"`, `closed_at: null`, `fields: {}` — all in a single undo.
  The audit log shows close was written as a single
  `event_type: "status_changed"` event with the close-reason embedded as
  the event `comment` rather than a sibling `fields_changed` event.

- **`heartbeat_work` no longer requires an explicit `actor`** (review-d/e).
  `heartbeat_work(issue_id=...)` from the assignee succeeded; the literal
  `'mcp'` default is gone.

- **`release_claim` does not strand wip issues** (review-d/e). I called
  `release_claim` on an `in_progress` task; the issue rolled back to
  `status: "open"` with `assignee: ""`, returning to the `get_ready` queue.

- **`get_blocked` includes wip issues** (review-d/e). After
  `add_dependency(from=in_progress_task, to=open_task)`, `get_blocked`
  returned the in-progress task with the right `blocked_by`.

- **`close_issue` does not bypass workflow** (review-d). Calling
  `close_issue` on a bug in `triage` returned the same
  `INVALID_TRANSITION` envelope as `update_issue --status=closed`, with
  `valid_transitions` and `hint` inline.

#### Write-path enforcement — fix landed, but opt-in

This is the finding most worth re-reading. Calls **without**
`expected_assignee` against an issue held by another actor still succeed
silently:

```json
// task B held by other-agent-h; I'm mcp-review-g
update_issue(B, title="...HIJACKED...", actor="mcp-review-g")
→ assignee: "other-agent-h", changed_fields: ["title"]      // succeeded
add_comment(B, ..., actor="mcp-review-g")                    // succeeded
add_label(B, "tech-debt", actor="mcp-review-g")              // succeeded
close_issue(B, reason=..., actor="mcp-review-g")             // succeeded
```

But the **same calls with `expected_assignee="mcp-review-g"`** all return
clean CONFLICTs:

```json
update_issue(B, ..., expected_assignee="mcp-review-g", actor="mcp-review-g")
→ {"error": "Cannot operate on filigree-1d2bc22a94: assigned to
   'other-agent-h' (expected 'mcp-review-g')", "code": "CONFLICT"}
// add_comment, add_label, close_issue all return the same shape.
```

So the fix landed exactly as review-d's suggested resolution implied
("mirror reclaim_issue's check"). What's still wrong is that the **default**
is unsafe — an agent that copies CLI/automation patterns from yesterday
keeps the old observable behaviour. See finding G2.

### D. Plan tools

`create_plan` is excellent. Same-phase int deps (`[0, 1]`) and cross-phase
`"p.s"` notation (`"0.2"`) both resolve correctly into populated
`blocks`/`blocked_by` arrays in the response. `add_plan_step` inherits
labels from the phase.

Two papercuts surfaced in this lane:

- **`move_plan_step` doesn't surface a cross-phase-dep warning.** I moved
  step B3 from phase Beta to phase Alpha; B3's `blocked_by` continues to
  include B2 (still in phase Beta). The response has `data_warnings: []`
  and a clean `move_result: "moved"`. Cross-phase deps may be intentional,
  but a phase-restructuring agent will silently end up with phantom
  dependencies it didn't model. (G7)

- **Naming inconsistency cluster.** `label_plan_tree` requires `label`
  (singular string); `create_plan` accepts `labels: [...]`.
  `retarget_plan_dependency` requires `step_id` + `old_depends_on_id` +
  `new_depends_on_id`; `add_dependency` uses `from_issue_id` +
  `to_issue_id`; the retarget *response* uses `from_issue_id` +
  `old_to_issue_id` + `to_issue_id` (a third variant). `label_subtree`
  requires `parent_id`; the same field on `get_issue` responses is
  `parent_id`, on `get_ready`/`list_issues` items it is
  `parent_issue_id`. Four siblings, four conventions for "the issue
  this issue depends on / belongs under." (G6)

### E. Annotations + finding lifecycle

`annotate_file` provenance block is still the gold standard
(`commit_ref`, `branch`, `file_checksum`, `file_size`, `file_mtime`,
`anchor_match_confidence`, `worktree_diff_summary`,
`provenance_flags: ["dirty_worktree"]`, `provenance_trust_level`).
`link_annotation` → `supersede_annotation` → `promote_annotation` is a
clean event chain — every transition writes a typed audit event with
populated `target_type`/`target_id`. `promote_annotation` adds the
`from-annotation` label automatically (symmetric with `from-observation`
and `from-finding` — nice).

`carry_forward_annotation` has a soft footgun: the `from_target_id`
parameter is required, but the call **does not validate** that the
annotation was ever linked to that target. I called
`carry_forward_annotation(annotation_id, from_target_id=X, to_target_id=Y)`
on an annotation that had **no** prior link to X; the call succeeded,
returned `acknowledged_target_id: X`, and wrote a `carried_forward` event
recording the acknowledgment. The "old target warning" was acknowledged
even though there was no warning to acknowledge. (G8)

`report_finding` redundancy from review-f F3 is still present:

```json
report_finding(...) → {
  finding_id: "filigree-sf-231b2c7ffb",
  observations_created: 1, observations_failed: 0,
  observation_ids: ["filigree-obs-0360f3bc6c"],
  observation_id:  "filigree-obs-0360f3bc6c"   // 4 fields, single finding
}
```

The auto-observation `source_finding_id` is now populated (review-d/e
fix landed cleanly) — but the auto-create itself remains, and is the
single biggest contributor to the "STALE OBSERVATIONS: 10 older than 48h"
banner in `get_summary`. (G5)

`update_finding` and `promote_finding` are clean. The promoted issue
gets `from-finding` label, `source_finding_id` in `fields`, and a
`description` autopopulated with rule_id / severity / location.

### F. Power tools

- `compact_events --dry_run=true` returned `{status: "ok",
  events_deleted: 0}` — dry-run respected. But it doesn't preview *what*
  would be touched (event types, age cutoff applied, issues affected).
  Agents doing safe-by-default cleanup can't see the radius before
  committing. (G9, P3)

- `get_changes --since=...` returns events with both `old_value` and
  `new_value`, plus the **current** `issue_title`. The latter is helpful
  — my second event shows `new_value: "[mcp-review-g] Scratch task B"`
  but `issue_title: "[mcp-review-g] HIJACKED title — write-asymmetry
  probe"` because I had renamed it after creation. Cursor pagination via
  `next_since` is consistent with the time-stream nature of the tool.

- `export_jsonl` is the strongest leg of finding G1:

  ```json
  export_jsonl(output_path="docs/bugs/...", label="cluster:mcp-review-g")
  → {"status": "ok", "records": 4243, "path": "..."}
  ```

  4 issues actually carry that label. The `label` parameter was silently
  dropped because `export_jsonl`'s schema only declares `output_path`.
  4243 records (entire DB) were written to disk. The tool's docstring
  says "export all project data," so technically the dump is correct —
  but the agent's filter intent was silently overridden, and a multi-MB
  file landed where a small one was wanted.

  Nice safety on the path side: absolute paths are rejected with a clean
  `VALIDATION` error, and a missing parent directory returns a clean
  `IO` error. The hint text could mention "project-relative; parent must
  exist" but the failure modes are right.

## 4. Findings (sorted by severity)

### G1 (P1) — Silent parameter ignore: agents' filters are dropped without warning

**Evidence.** Four confirmed instances (the last surfaced *during cleanup*
of this very review's scratch — the bug bit me doing routine work):

1. `get_ready` accepts `type`, `priority_min`, `limit`, even
   `garbage_param=...` and returns the same unfiltered list. The
   inputSchema declares only `include_context`; sibling
   `start_next_work` declares all the filter params one might expect
   here.
2. `annotate_file` accepts `kind` and `summary` (both real-sounding
   parameter names; `kind` is what `report_finding` uses for `severity`
   class, `summary` is what `observe` uses for the body) — both silently
   dropped. The required fields are `intent` (enum) and `note`.
3. `export_jsonl(output_path=..., label="cluster:mcp-review-g")` exported
   4243 records (the whole DB) instead of the 4 the label scopes to.
4. `update_issue(issue_id=..., add_labels=["a", "b"])` returns
   `changed_fields: []` and the labels list is unchanged. `update_issue`
   does not accept `add_labels`; it accepts only `labels` (full
   replacement). An agent reaching for `add_labels` (the verb that
   matches `add_label` and `batch_add_label`) silently no-ops. I had
   to fall back to `add_label` to attach `cluster:mcp-review-g` to a
   promote-side-effect issue during teardown.

The MCP server isn't enforcing `additionalProperties: false`. This is a
**class** bug — almost certainly affects every tool whose JSONSchema
omits the constraint.

**Why it matters.** An agent who learned that `start_next_work` has
`priority_min` will pass `priority_min` to `get_ready` and act on
unfiltered results. An agent who tries to scope a backup to one cluster
gets a multi-MB whole-DB dump. There is no error to recover from — the
call succeeds and the result *looks* correct.

**Suggested resolution.** Either:
- (a) Set `additionalProperties: false` on every MCP inputSchema and let
  unknown args fail with `VALIDATION`. Conservative but breaks any
  forgiving client; or
- (b) Have the MCP envelope log/return a `data_warnings` entry like
  `"unknown_param: 'label' ignored for tool 'export_jsonl'"` whenever
  unknown args are stripped — keeps forgiving clients working but makes
  the drop *visible*.

### G2 (P2) — Write-path enforcement is opt-in, not default

**Evidence.** Without `expected_assignee`:

```json
// task B held by other-agent-h; I'm mcp-review-g
update_issue(B, title="HIJACKED", actor="mcp-review-g")  → succeeded
add_comment(B, ..., actor="mcp-review-g")                → succeeded
add_label(B, "tech-debt", actor="mcp-review-g")          → succeeded
close_issue(B, reason=..., actor="mcp-review-g")         → succeeded
```

With `expected_assignee="mcp-review-g"` (the wrong holder):

```json
update_issue(B, ..., expected_assignee="mcp-review-g", actor="mcp-review-g")
→ {"error": "Cannot operate on filigree-1d2bc22a94: assigned to
   'other-agent-h' (expected 'mcp-review-g')", "code": "CONFLICT"}
// add_comment, add_label, close_issue all return the same shape.
```

**Why it matters.** Review-d and review-e flagged the asymmetry as the
single biggest multi-agent footgun. The maintainer's fix is good — the
error message is excellent and four sibling tools converged on one
`Cannot operate on X: assigned to 'Y' (expected 'Z')` shape. **But the
default behaviour is unchanged**: an agent who doesn't know the new
parameter exists still silently overwrites another claimant's work.

**Suggested resolution.** Pick one of:
- (a) Make `expected_assignee` default to `actor` when both are passed
  and the issue has a non-empty assignee — that's the implicit user
  intent ("I'm acting as mcp-review-g and I expect to hold this");
- (b) Require explicit `expected_assignee=null` (or `force=true`) to
  override the check on a held issue, and CONFLICT by default;
- (c) Surface a soft `data_warnings` entry on every write to a held
  issue when `expected_assignee` is omitted ("issue is held by 'Y'; pass
  `expected_assignee` for claim-aware coordination") — preserves
  back-compat but stops the silent footgun.

The CLAUDE.md guidance for multi-agent coordination should also mention
`expected_assignee` explicitly — right now the workflow guide describes
`heartbeat_work`/`release_claim`/`reclaim_issue` as the claim-aware path
and is silent on the write-tool option.

### G3 (P2) — `get_stale_claims` includes archived/done issues

**Evidence.**

```json
get_stale_claims() → {
  "items": [{
    "issue_id": "filigree-ec07bee5e9",
    "status": "archived",
    "status_category": "done",
    "closed_at": "2026-05-08T19:55:03.544649+00:00",
    "assignee": "mcp-review-e",
    "claim_expires_at": "2026-05-08T21:52:45.671569+00:00",
    ...
  }]
}
```

This task was closed and archived by the prior review-e session. Its
`claim_expires_at` is in the past, so it's "stale" — but it's also
`done`, and an agent reading `get_stale_claims` to "find work to
reclaim" gets a stale tombstone.

**Why it matters.** Stale-claim discovery is the foundation for picking
up dead-agent work. A done issue showing up there is noise at best;
worse, a careless agent calling `reclaim_issue` and then `start_work`
will get an `INVALID_TRANSITION` only on the second step, after having
already taken ownership.

**Suggested resolution.** Filter `get_stale_claims` to
`status_category != "done"` (and probably `status != "archived"` even
within the wider category). If a "stale claim on a done issue" is
genuinely useful diagnostic state, expose it as `include_done=true` and
default to `false`.

### G4 (P2) — `search_issues` returns archived/done results, no status filter

**Evidence.** `search_issues(query="Scratch task")` returned 9 hits;
4 of them have `status: "archived"`. There is no `status` /
`status_category` filter on `search_issues`.

**Why it matters.** Reported in review-d and review-e. An agent doing
"find live work matching X" by FTS gets unrelated tombstones from prior
sessions, and has to post-filter every response. Combined with G3 above,
"old session debris leaks into discovery surfaces" is a small theme.

**Suggested resolution.** Add `status_category` (or
`include_archived=false` default) to `search_issues`. The existing
`status_category` enum on `list_issues` would compose well.

### G5 (P2) — `report_finding` redundancy + auto-observation backlog

(Re-flagging review-f F3.)

**Evidence.**

```json
report_finding(...) → {
  finding_id: "filigree-sf-231b2c7ffb",
  observations_created: 1, observations_failed: 0,
  observation_ids: ["filigree-obs-0360f3bc6c"],
  observation_id:  "filigree-obs-0360f3bc6c"
}
```

Four fields tracking a single auto-created observation. The
`source_finding_id` linkage is now properly populated (review-d/e fix
landed) — that part's good.

**Why it matters.** Single-finding tools shouldn't return aggregate
counters; agents pick different fields and tests assert differently
across the codebase. And the auto-create remains the dominant source
of the `STALE OBSERVATIONS: 10 older than 48h` banner that
`get_summary` shows on every cold start.

**Suggested resolution.** Reduce the response to one field
(`observation_id`, optional). Make the auto-observe behaviour either
opt-in (`create_observation=true`) or exempt from the stale-observation
banner. Keep the four-counter shape on `trigger_scan` results where
batch counts genuinely matter.

### G6 (P2) — Parameter-naming inconsistency cluster across plan/dep tools

**Evidence.** Four naming variants for "the issue another issue depends
on / belongs under":

| Surface | Param / field |
|---|---|
| `add_dependency` (params) | `from_issue_id`, `to_issue_id` |
| `retarget_plan_dependency` (params) | `step_id`, `old_depends_on_id`, `new_depends_on_id` |
| `retarget_plan_dependency` (response) | `from_issue_id`, `old_to_issue_id`, `to_issue_id` |
| `create_plan` / `add_plan_step` (deps array) | `["filigree-..."]` (bare ID) |
| `label_plan_tree` (params) | `label` (singular) |
| `create_plan` (params) | `labels: [...]` (plural) |
| `label_subtree` (params) | `parent_id` |
| `get_issue` (response) | `parent_id` |
| `get_ready` / `list_issues` (response) | `parent_issue_id` |

**Why it matters.** Each per-tool quirk is small; together they break
the agent's mental model of "how to refer to relationships in
filigree." The `parent_id` vs `parent_issue_id` split was already
flagged in review-d/e and is still mid-fix.

**Suggested resolution.** One sweep to standardize on one verb-pair
(`from_issue_id`/`to_issue_id` is the most common); deprecate
`old_depends_on_id` family aliases; pick `parent_issue_id` everywhere
(it's self-describing) and alias `parent_id` for one release.

### G7 (P3) — `move_plan_step` carries cross-phase deps silently

**Evidence.** I called
`move_plan_step(step_id=B3, phase_id=phase_Alpha)`. Response:

```json
{
  "issue_id": "filigree-e59ec53bf2",
  "title": "Step B3 — added later",
  "parent_id": "filigree-794d6e5d63",   // now phase Alpha
  "blocked_by": ["filigree-43d1959265"], // still B2 in phase Beta!
  "data_warnings": [],
  "move_result": "moved",
  "changed_fields": ["parent_id"]
}
```

**Why it matters.** When restructuring a plan, an agent expects "moved
to a different phase" to mean the step is now an Alpha-phase citizen.
A silent surviving cross-phase dep means the step won't unblock with
Alpha, and the agent has no signal that this is the case until they
hit `get_ready` and notice the step is still blocked.

**Suggested resolution.** Either (a) auto-strip cross-phase deps on
move (with `keep_cross_phase=true` to opt back in); or (b) populate
`data_warnings` with one entry per surviving cross-phase dep.

### G8 (P3) — `carry_forward_annotation` acknowledges unlinked targets

**Evidence.** I called `carry_forward_annotation(annotation_id=X,
from_target_id=Y, to_target_id=Z)` on an annotation that had **no**
prior link to Y. The call succeeded with
`acknowledged_target_id: "Y"` and a `carried_forward` event was
written.

**Why it matters.** Tool description: "Carry an active critical
annotation forward to another issue and acknowledge the old target
warning." If there's no link to acknowledge, the acknowledgement is
vacuous — but the audit trail makes it look like a real handoff
happened. An agent reading the event later thinks the warning was
genuinely transferred.

**Suggested resolution.** Validate that an active link to
`from_target_id` exists before "acknowledging" it; otherwise return
`VALIDATION` with a hint to use `link_annotation` first.

### G9 (P3) — `compact_events --dry_run` returns count but no preview

**Evidence.** `compact_events(dry_run=true)` returns
`{"status": "ok", "events_deleted": 0}`. No breakdown of what age
threshold applies, what event types are eligible, or which issues
would be touched.

**Why it matters.** Cleanup tools are the kind of thing agents call
with `dry_run=true` *because* they want a preview. A bare count
doesn't tell the agent whether running it for real would be a no-op
or a substantial mutation, except by trying.

**Suggested resolution.** Have `dry_run=true` return
`{events_deleted, by_event_type: {...}, by_age_bucket: {...},
oldest_to_be_deleted, newest_to_be_deleted}`.

### G10 (P3) — `get_schema.entity_id_prefixes.issue.accepted_by_tools` still missing batch tools

**Evidence.** Live result lists 23 issue-accepting tools but omits
`batch_update` and `batch_close`, both of which clearly accept arrays
of issue IDs and worked fine in this run. Same class of drift was
caught in review-d/e for the observation entity (where it has since
been fixed: `batch_promote_observations` is now listed).

**Why it matters.** `get_schema` is exactly the registry an agent uses
to build its tool dispatch table for "where can I send this ID?". A
silent omission means the agent never finds the batch path and falls
back to N single calls.

**Suggested resolution.** Generate `accepted_by_tools` from the live
tool registry — exactly what tracked feature `filigree-b48cd07e68`
proposes. Until that lands, manually add the two missing entries.

### G11 (P3) — `get_valid_transitions` still returns a bare array

(Re-flagging review-f F4.)

`get_valid_transitions(issue_id)` returns
`[{to, category, enforcement, ...}, ...]`. Every other list-shaped
tool returns `{items, has_more, next_offset?}`. The embedded
`valid_transitions` inside an `INVALID_TRANSITION` error also returns
a bare array, but with a *different* schema (3 keys vs 6). Three
shapes for the same data.

## 5. What works well

- **Five review-d/e/f findings verified fixed in this run** — `undo_last`
  composite event, `heartbeat_work` no-actor default, `release_claim`
  doesn't strand wip, `close_issue` doesn't bypass workflow, `get_blocked`
  includes wip. Plus `source_finding_id` populated on auto-observations.
  This is a high fix-rate.
- **`expected_assignee` write-coordination shape is excellent** — single
  consistent error string across `update_issue` / `add_comment` /
  `add_label` / `close_issue`, with the specific holder named. The fix
  landed exactly the way prior reviews suggested ("mirror reclaim_issue").
  The opt-in default is the only thing keeping G2 alive.
- **`promote_*` family is symmetric and well-shaped.**
  `promote_observation` adds `from-observation`, `promote_finding` adds
  `from-finding`, `promote_annotation` adds `from-annotation`. Each
  preserves source IDs in `fields` and writes a typed link/event chain.
  Clean provenance.
- **`annotate_file` provenance block** remains the gold standard
  (`commit_ref`, `branch`, `file_checksum`, `file_size`, `file_mtime`,
  `anchor_match_confidence`, `worktree_diff_summary`,
  `provenance_flags`, `provenance_trust_level`).
- **Strict enum validation with valid values surfaced** —
  `intent="followup"` returned
  `'followup' is not one of ['breadcrumb', 'decision', 'explanation',
  'gotcha', 'handoff', 'hypothesis', 'warning']`. Agents can recover
  without a docs trip.
- **`create_plan` cross-phase dep notation** (`"p.s"`) and same-phase
  int dep notation work correctly with one call setting up the entire
  blocked_by graph.
- **Path safety on `export_jsonl`** — absolute paths rejected with
  `VALIDATION`, missing parent directories with `IO`. Both clean error
  codes.

## 6. Open questions for the maintainer

1. **Silent parameter ignore: feature or bug?** The MCP spec is forgiving
   about unknown args at the protocol level; is the in-tool silent
   discard intentional, or should every inputSchema set
   `additionalProperties: false`? If it's intentional, can the dropped
   args at least surface in `data_warnings`?
2. **`expected_assignee` default policy.** Should the parameter default
   to `actor` when the issue has a non-empty assignee? Should the
   workflow guide push agents toward always passing it? The current
   "fixed but opt-in only" middle ground reproduces the review-d/e
   footgun for any agent that doesn't read the changelog.
3. **`carry_forward_annotation` semantics.** Should the
   `from_target_id` parameter be validated against the annotation's
   actual link set, or is "acknowledge whatever the agent says is the
   from-target" the contract? The current behaviour writes audit
   events for handoffs that didn't happen.
4. **`export_jsonl` filtering scope.** Should it accept
   `label`/`type`/`since` filters, or is "all data" the only operation?
   The current "label is silently ignored" outcome (G1) is the worst
   path; the answer needs to be either "supports it cleanly" or
   "rejects unknowns explicitly."
5. **Stale-observation backlog.** The `report_finding` auto-observe
   and the `STALE OBSERVATIONS: N older than 48h` banner are in
   tension: scanners create observations, the banner complains about
   them, the docs tell agents to triage them, but they're not the kind
   of observation the human wrote. Either auto-observations should be
   exempt from the banner's age window, or the banner should partition
   "agent-noticed" vs "scanner-emitted" so an agent doing the banner's
   bidding can clear it without false work.
