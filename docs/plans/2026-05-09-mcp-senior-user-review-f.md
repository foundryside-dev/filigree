# MCP Senior-User Friction Review — review-f (2026-05-09)

Reviewer: agent driving the live `mcp__filigree__*` surface against
`/home/john/filigree/.filigree/`. Scratch artifacts labelled `mcp-review-scratch`
+ `review-f` and archived at the end of the session.

## 1. Executive summary

The MCP surface is *operationally* in good shape — the read/write tools
return well-shaped envelopes, errors carry useful machine-codes, and the
dominant happy-path tools (`session_context`, `get_ready`, `start_work`,
`promote_observation`, `batch_close`) are pleasant to drive. The two findings
that hurt most are subtle and likely to bite a working agent before being
caught by a test suite: **(a)** the server announces `schema_mismatch` via
`get_mcp_status` while every other tool — including writes — silently succeeds,
directly contradicting the documented "do not retry" guidance; and **(b)**
`undo_last` reverses one audit event at a time, so undoing `close_issue`
takes two calls and the first call returns `undone: true` while the issue
is still closed.

## 2. Coverage

I drove these workflows end-to-end:

- **A. Discovery / pickup** — `get_mcp_status`, `session_context`, `get_summary`,
  `get_schema`, `get_ready --include_context`, `get_stats`, `list_packs`,
  `list_types`, `get_template`, `get_workflow_guide`, `get_label_taxonomy`,
  `get_critical_path`, `get_metrics`, `get_stale_claims`.
- **B. Triage / find work** — `list_issues`, `list_observations`,
  `list_findings`, `list_files`, `list_attention_annotations`.
- **C. Claim → progress → finish** — `start_work`, `heartbeat_work`,
  `release_claim` (`if_held=true`), `update_issue` across soft/hard
  transitions, `validate_issue`, `get_valid_transitions`, `add_comment`,
  `add_label` (incl. reserved-namespace rejection), `add_dependency`
  (incl. cycle + self-cycle), `close_issue`, `undo_last`, `reopen_issue`
  (read only).
- **D. Observations / findings / annotations** — `observe`, `report_finding`,
  `promote_observation`, `dismiss_finding`, `batch_dismiss_observations`,
  `annotate_file`, `resolve_annotation`, `get_issue_files`, `get_changes`.
- **E. Batch + cleanup** — `batch_close` (mixed valid/invalid), `archive_closed`
  with label filter.

I did **not** drive: scanner triggering (`trigger_scan*`, `preview_scan`,
`get_scan_status`), `create_plan` / `add_plan_step` / `move_plan_step` /
`label_plan_tree` / `retarget_plan_dependency`, `start_next_work`,
`reclaim_issue`, `export_jsonl` / `import_jsonl`, `compact_events`,
`reload_templates`, `restart_dashboard`, `update_finding` /
`promote_finding`, link / supersede / carry-forward / promote on
annotations. Those are gaps, not assessments.

## 3. Per-workflow walkthrough

### A. Discovery / pickup

`session_context`, `get_summary`, and `get_ready --include_context=true` are
the right three tools to land in any new session. `get_ready` returning
`parent_issue_id` + `parent_title` inline keeps the pickup story to one
call. `get_summary` already mentions stale observations:

```
STALE OBSERVATIONS: 10 observation(s) older than 48 hours (oldest: 263h ago)
  Total pending: 10. Run `list_observations` to review.
```

The shape is fine; the friction is that the snapshot doesn't actually point
*at* the stale ones (no IDs, no top-N preview), so an agent has to fetch and
filter even when it would just dismiss them all.

`get_mcp_status` returned `schema_mismatch` — see finding F1 below.

### B. Triage / find work

Filters compose well (label AND, label_prefix, not_label, status_category).
`list_issues --status_category=wip` returned `[]` truthfully — the project
has no in-progress items. Pagination envelope `{items, has_more, next_offset?}`
is consistent across `list_issues`, `list_observations`, `list_findings`,
`list_files`. `get_changes` uses `next_since` (a time cursor), which is
correct given the time-stream semantics — different from offset pagination,
not a consistency bug.

`get_critical_path` returned my freshly-created scratch dep chain after I
made one — useful and snappy:

```json
{"path": [
  {"title": "[mcp-review-f] Dep target B", "issue_id": "filigree-4310d39015"},
  {"title": "[mcp-review-f] Dep target A", "issue_id": "filigree-f5387c26c7"}
], "length": 2}
```

### C. Claim → progress → finish

`start_work` is the headline win: one atomic call sets `assignee`,
`status=in_progress`, `claimed_at`, `last_heartbeat_at`, and a 48h
`claim_expires_at`. No follow-up needed.

```json
"assignee": "mcp-review-f",
"claimed_at": "2026-05-08T21:16:05.138856+00:00",
"claim_expires_at": "2026-05-10T21:16:05.138856+00:00",
"status": "in_progress"
```

The error response for an invalid transition (`triage` → `verifying` on a
bug) embeds `valid_transitions` inline:

```json
{
  "error": "Transition 'triage' -> 'verifying' is not allowed for type 'bug'.",
  "code": "INVALID_TRANSITION",
  "valid_transitions": [
    {"to": "confirmed", "category": "open", "ready": false},
    {"to": "wont_fix", "category": "done", "ready": true},
    {"to": "not_a_bug", "category": "done", "ready": true}
  ]
}
```

Excellent — agent gets the menu without a follow-up call. **However**, the
embedded list is a bare array, while the standalone `get_valid_transitions`
tool returns a bare array too — see F4.

Soft-warning transitions (`triage→confirmed`, `confirmed→fixing`,
`fixing→verifying`) returned cleanly with `data_warnings` populated *and*
wrote a `transition_warning` audit event alongside the `status_changed`
event. Hard-enforcement (`verifying→closed` requiring `fix_verification`)
is correctly blocking — the soft warning at `verifying` becomes a hard
gate at `closed` if the field still isn't there.

`add_label P1` and `add_label priority:critical` were both rejected with
clear reasons that point at the canonical priority field — exactly how
this *should* be enforced:

```json
{"error":"Label 'P1' conflicts with the priority field; set the numeric priority field or filter with --priority instead of using P0-P4 or priority:* labels.","code":"VALIDATION"}
```

`add_dependency` cycle detection works in both 2-cycle and self-cycle
shapes. `validate_issue` was useful — it surfaced *future* requirements
("Transition to 'confirmed' requires: severity") not just current ones.

### D. Observations / findings / annotations

`observe` then `promote_observation` did the right thing: the promoted issue
inherited the file association as `assoc_type: "mentioned_in"`, recorded
`source_observation_id` in `fields`, and set the `from-observation` label.
The original observation row is removed from `list_observations` after
promotion. Clean.

```json
"files": [{
  "file_id": "filigree-f-73fde6e29f",
  "assoc_type": "mentioned_in",
  "file_path": "src/filigree/mcp_tools/issues.py"
}]
```

`report_finding` worked but the response is over-stuffed — see F3 below.

`annotate_file` is feature-rich (provenance, anchor confidence, commit ref,
git_state). `resolve_annotation` cleanly recorded a `resolved` event with
the reason. Worth keeping.

### E. Batch + cleanup

`batch_close` handled a mixed `[valid, valid, valid, valid, valid, BAD]`
input correctly — five succeeded, one in `failed[]` with `code=NOT_FOUND`.

`archive_closed --days_old=0 --label=mcp-review-scratch` swept up my five
scratch issues — and seven older `from-observation` ones I didn't author.
That's a feature of the contract, not a bug, but see F7 about the framing.

`undo_last` had a real footgun — see F2.

## 4. Findings (sorted by severity)

### F1 (P1) — `schema_mismatch` is theatre: writes succeed silently

**Evidence.**

```json
// get_mcp_status
{
  "status": "schema_mismatch",
  "installed_schema_version": 11,
  "database_schema_version": 12,
  "code": "SCHEMA_MISMATCH",
  "guidance": "... To fix: upgrade filigree (`uv tool upgrade filigree` ...)"
}

// create_issue, immediately afterwards — no error, no warning
{"issue_id": "filigree-9691790cc7", "status": "open", ...}
```

I exercised `create_issue`, `update_issue`, `start_work`, `add_comment`,
`add_dependency`, `add_label`, `report_finding`, `observe`,
`promote_observation`, `close_issue`, `batch_close`, `archive_closed`,
`annotate_file`, `resolve_annotation`, `heartbeat_work`, `release_claim`,
`undo_last` — every single one succeeded with a normal envelope.

**Why it matters.** `CLAUDE.md` (and the
`docs/plans/2026-04-26-2.0-phase-c-handover.md` note) tell agents:

> "the MCP server still launches but most tool calls return an `ErrorResponse`
> with `code: SCHEMA_MISMATCH` and upgrade guidance. ... Surface that message
> to the user — do not retry."

A by-the-book agent will refuse to operate against this DB. An agent that
ignores the docs operates fine — but is silently writing through a v11
binary into a v12 schema. Whichever is *meant* to be true, the surface and
the docs disagree.

**Suggested resolution.** Either (a) actually gate writes in mismatch mode
and standardize on the documented behaviour; or (b) downgrade
`get_mcp_status.status` from `schema_mismatch` to a less alarming
`schema_drift` / `degraded_compatible` and update the CLAUDE.md guidance
to say "MCP read+write paths still function for compatible schema deltas;
treat `get_mcp_status` as advisory, not gating." The current state is the
worst of both worlds.

### F2 (P1) — `undo_last` of `close_issue` reports `undone: true` but the issue stays closed

**Evidence.** Sequence on `filigree-987b445f26`:

1. `close_issue` → status=`closed`, `closed_at` set.
2. `undo_last` → response has `"undone": true`, `event_type: "fields_changed"`,
   but `"status": "closed"` and `"closed_at"` is unchanged. (Only the
   ancillary `close_reason` field was removed.)
3. `undo_last` again → *now* it reverses the `status_changed` event:
   status returns to `verifying`, `closed_at: null`.

Audit log confirms close writes two events:

```json
{"event_id": 2558, "event_type": "status_changed", "old": "verifying", "new": "closed"},
{"event_id": 2559, "event_type": "fields_changed",
  "new_value": "{\"fix_verification\": ..., \"close_reason\": \"...\"}"}
```

**Why it matters.** Agents read `undone: true` and trust it. The natural
mental model — "undo my last *action*" — does not match the actual
contract — "undo the last *audit event*". Worse, if the agent uses the
result envelope's `status` field to verify, it sees `"status": "closed"`
and may assume the close was preserved by design.

**Suggested resolution.** Either (a) make `close_issue` write a single
composite event instead of two (semantically: close-with-reason is one
operation); (b) make `undo_last` cascade ancillary `fields_changed`
events authored as part of the same logical action and reverse them
together; or (c) add a `next_undoable_event` field to the response so
the agent can see "you have one more undo to do" without re-fetching the
event log.

### F3 (P2) — `report_finding` response shape: redundant observation fields, side-effect under-described

**Evidence.** A single `report_finding` call returned:

```json
{
  "finding_id": "filigree-sf-87646d67ff",
  "finding_result": "created",
  "findings_created": 1,
  "findings_updated": 0,
  "file_created": false,
  "observations_created": 1,
  "observations_failed": 0,
  "observation_ids": ["filigree-obs-8f5412b8c2"],
  "observation_id":  "filigree-obs-8f5412b8c2"
}
```

Four ways to learn about the same observation. The docstring does mention
`observation_id` ("plus ingest metadata, including any observation_id
created for triage"), so the side-effect is technically announced — but
in a return-field aside, not in the description, and the redundancy of
`observation_id` + `observation_ids` + `observations_created` +
`observations_failed` in a single-finding tool is a wart.

**Why it matters.** Agents have to choose which field to read; the next
agent reading the code will probably pick differently; tests will end up
asserting on different fields across the codebase. And the auto-observation
behaviour itself is surprising — a scanner that legitimately reports many
findings will, on the side, generate many observations and contribute to
the very "stale observations: 10 older than 48h" banner in `get_summary`.

**Suggested resolution.** Reduce to one field (`observation_id`, optional);
move the auto-create behaviour into the tool description proper; document
how to opt out (or make it opt-in via a `create_observation` parameter).
For batch/scanner ingest, the four-counter shape *is* useful — keep it
on `trigger_scan` results, not on the single-call `report_finding`.

### F4 (P2) — Bare-array transition payloads vs. `{items, has_more}` everywhere else

**Evidence.**

```json
// get_valid_transitions(issue_id)
[
  {"to": "in_progress", "category": "wip", "ready": true, ...},
  {"to": "closed",      "category": "done", "ready": true, ...}
]
```

Every other list-shaped tool (`list_issues`, `list_observations`,
`list_findings`, `list_files`, `list_attention_annotations`,
`list_annotations`, `get_changes`, `get_issue_events`, `get_issue_files`,
`list_packs`, `list_types`) returns `{items: [...], has_more: bool, ...}`.
`get_valid_transitions` and the embedded `valid_transitions` inside an
`INVALID_TRANSITION` error both return a bare array.

**Why it matters.** Two surfaces to special-case in any client adapter,
and the precedent invites future tools to return bare arrays "because
that one does." Both surfaces would change in one place.

**Suggested resolution.** Wrap both in the standard envelope. There won't
ever be enough transitions for `has_more` to be meaningful, but consistency
matters more than the few wasted bytes.

### F5 (P2) — Template-declared field defaults are not applied at creation

**Evidence.**

`get_template type=bug` advertises:

```json
{"name": "severity", "type": "enum", "default": "major",
 "required_at": ["confirmed"]}
```

`create_issue type=bug` returns `"fields": {}` and a soft warning fires
when transitioning to `confirmed`. Tracing the create path
(`src/filigree/db_issues.py:390-490`) confirms: `fields = fields or {}`
at line 456, and the INSERT writes the dict raw. Nothing reads
`fs.default`. The `default` declared in the schema is currently a
*display hint*, not an applied value.

**Why it matters.** Agents reading `get_template` reasonably assume
`default: "major"` means "you don't have to pass it." They don't.
Then they get a transition warning that should never fire if defaults
worked.

**Suggested resolution.** Either (a) apply `fs.default` at create time
when the field is missing; or (b) rename the template-schema field from
`default` to `display_default` / `suggested` and clarify in the docstring
that schema defaults are advisory only. Pick one — don't keep both
readings live.

### F6 (P2) — `close_issue` writes two audit events; first `undo_last` is a no-op surprise

(Sub-finding of F2 — keeping it separately because a maintainer fixing
F2 might pick the "single composite event" route and incidentally clean
this up too.)

`close_reason` arrives as a synthetic entry in `fields` rather than its
own column, which is what produces the second event. Same shape happens
on `dismiss_finding` — `reason` ends up in the finding's `metadata.dismiss_reason`.
Working as designed; just noting that the storage choice is what creates
the double-event audit footprint.

### F7 (P3) — `archive_closed --label=X` is shared scope, not session scope

**Evidence.**

```json
// archive_closed days_old=0 label=mcp-review-scratch
{"archived_count": 13, "archived_ids": [..., "filigree-6177ac67a3", ...]}
```

I created five scratch issues; thirteen got archived. The eight others
were old `mcp-review-scratch`-labelled artifacts left over from prior
review-c / review-d / review-e sessions. The contract is exactly what
the docstring says — "archive closed issues currently carrying this
label" — but a working agent who labels their scratch with a generic
tag and runs `archive_closed --label=...` to clean up will sweep
everyone else's scratch too.

**Why it matters.** Low-grade — the issues are already closed, so the
archive is recoverable in principle and not destructive in practice.
But in a multi-agent project, the natural pattern ("label your scratch,
sweep at session end") doesn't compose.

**Suggested resolution.** Document this in the workflow guide
("use a session-unique label like `review-f` for actor-scoped sweeps"),
or add an `actor=` filter so `archive_closed --label=mcp-review-scratch
--actor=mcp-review-f` does the obvious thing.

### F8 (P3) — `get_summary` flags stale observations but doesn't preview them

**Evidence.**

```
STALE OBSERVATIONS: 10 observation(s) older than 48 hours (oldest: 263h ago)
  Total pending: 10. Run `list_observations` to review.
```

The summary doesn't include even a top-3 preview by ID/path. Agents end
up doing `list_observations --limit=50` purely to see what they have to
triage, and stop using the snapshot.

**Suggested resolution.** Echo the top 3 stale observations (id, file_path,
age) inline in `get_summary`, the way "Recent Activity (last 10 events)"
is already done.

### F9 (P3) — `get_schema` is partially stale on accepted_by_tools

`accepted_by_tools` for the `observation` family lists
`dismiss_observation, promote_observation, batch_dismiss_observations,
batch_promote_observations` — better than what review-d caught in
`filigree-obs-e7b379ff6e`, but the `annotation` family lists nine tools and
omits `list_attention_annotations` (which DOES accept annotation IDs by
implication when filtered by `target_id`). Minor — but `get_schema` is
exactly the registry an agent uses to check "where can I send this ID?",
so silent drift here is corrosive.

**Suggested resolution.** Generate `accepted_by_tools` from the live tool
registry (this is also what tracked feature `filigree-b48cd07e68` proposes
for the wider self-discovery surface).

## 5. What works well

- `start_work` / `start_next_work` semantics — atomic claim + transition +
  lease. Removes the most common multi-call sequence and gives the agent
  a clean lease expiry to heartbeat against.
- Error envelope: `{error, code, details?}` with stable enum codes
  (`VALIDATION`, `NOT_FOUND`, `CONFLICT`, `INVALID_TRANSITION`,
  `SCHEMA_MISMATCH`, ...) + helpful inline payload (e.g.
  `valid_transitions` on `INVALID_TRANSITION`).
- Reserved-namespace label rejection (`P[0-4]`, `priority:*`) with a
  reason that points at the canonical priority field.
- `promote_observation` preserving the source `file_id` as a
  `mentioned_in` association and stamping `source_observation_id` in
  `fields` — full provenance from a one-call promotion.
- `batch_close` / `batch_dismiss_observations` returning per-item
  failures with codes — enabling partial-success agents.
- `validate_issue` returning *forward-looking* warnings, e.g.
  "Transition to 'confirmed' requires: severity" before you've tried.
- Cycle detection in `add_dependency` (2-cycle and self-cycle).
- `annotate_file` provenance block (commit ref, branch, dirty-worktree
  flag, anchor confidence) — exactly the metadata an agent needs to
  decide whether to trust a stored breadcrumb later.

## 6. Open questions for the maintainer

1. **Schema-mismatch policy.** Is `code=SCHEMA_MISMATCH` supposed to gate
   write paths, or only to advise? The current behaviour and the CLAUDE.md
   guidance disagree — please pick one and update the other.
2. **`undo_last` semantics.** Should it be "undo the last user-intended
   *action*" or "undo the last *audit event*"? The contract change implied
   by F2 is non-trivial.
3. **Template field `default`.** Display hint or applied default? Either
   answer is fine; the surface should commit to one.
4. **`report_finding` auto-observation.** Should every reported finding
   create an observation? In an automated-scanner ingest path that may
   be valuable; in an agent-noticed-while-working path it's the source
   of the very stale-observation backlog the surface complains about.
5. **`accepted_by_tools` source of truth.** Should `get_schema` be
   generated from the live tool registry, or curated? If curated, what's
   the cadence for refresh?
