# Filigree MCP Senior-User Friction Review (run d)

**Date:** 2026-05-08 (filed under 2026-05-06 series)
**Reviewer:** Claude Opus, acting as senior-user agent
**Branch:** `2.0-project-management-extension`
**MCP schema:** v11 (matched, `get_mcp_status: ok`)
**Method:** Live use of `mcp__filigree__*` against this repo's `.filigree/`. Scratch issues labelled `cluster:mcp-review-d` / `mcp-review-scratch`; cleanup via `batch_close` + `archive_closed` at the end. Workflows A–F driven end-to-end.

---

## 1. Executive summary

The MCP surface is genuinely usable for an agent who knows the contract — `start_next_work`, `create_plan`, `annotate_file` (with full provenance), `get_label_taxonomy`, and the `INVALID_TRANSITION` error envelope (with `valid_transitions` and `hint`/`reopen_available`) all do exactly what an agent needs. The most painful gap is **write-path enforcement asymmetry**: claim ownership is strictly checked by `heartbeat_work`/`release_claim`/`reclaim_issue` but completely ignored by `update_issue`/`batch_update`/`add_comment`/`add_label`/`close_issue`, so a non-claimant can rename, transition, or even close a held issue with no conflict, while the rightful holder gets `CONFLICT` if they forget to pass `actor` to a heartbeat. The second-most painful gap is the **observation/finding shadow queue**: `report_finding` still spawns a parallel observation with no `source_finding_id`, and that observation outlives `dismiss_finding`/`promote_finding`, becoming triage debt that the prior reviews thought was fixed.

---

## 2. Per-workflow walkthrough

### Workflow A — Cold start (session_context → ready → claim)

**What I did:** `get_mcp_status` → `session_context` → `get_summary` → `get_ready(include_context=true)` → `get_stats` → `get_critical_path` → `list_types` → `get_schema` → `get_template feature` → `create_issue` (3 scratch) → `start_work` → `claim_issue` (as a different actor) → `start_next_work(priority_min=3, priority_max=3, type=task)`.

**What worked:**
- `get_mcp_status` was instant and explicit: `installed_schema_version: 11, database_schema_version: 11, schema_compatible: true`. A v8/v11 mismatch elsewhere in this host (the local `~/.local/bin/filigree` dashboard) is invisible from MCP, which is correct.
- `get_summary` markdown showed Epic Progress bar `[████░░░░] 3/6` — glanceable.
- `get_ready(include_context=true)` returned `parent_issue_id` and `parent_title`, which is exactly the orientation an agent wants before claiming.
- `start_next_work` happy path: one call, claim + transition + 48h lease, returns the full transitioned issue.
- `start_next_work` with no matches returned `{status: "empty", reason: "No ready issues matching filters"}` rather than an error or empty issue body — clean signalling.

**What hurt:**
- **Output-shape mix.** `get_summary` is markdown; `get_ready`, `get_stats`, `get_critical_path`, `list_types` are JSON. An agent doing programmatic orientation has to either parse markdown or call two tools.
- **Status-name fragmentation.** `get_stats.status_name_counts` shows `closed: 313, completed: 16, done: 2, not_a_bug: 1`. The `status_category_counts` collapses these to `done: 332`, but any tool that filters by literal status (`list_issues(status=...)`) has to know all four. `list_types` confirmed 11 types with completely different terminal vocabularies.
- **Naming inconsistency: `parent_id` vs `parent_issue_id`.** `get_issue` returns `parent_id`. `get_ready`/`list_issues` return `parent_issue_id`. Same field, two names.

  ```text
  mcp__filigree__get_issue → "parent_id": "filigree-ed2ccaf10d"
  mcp__filigree__get_ready → "parent_issue_id": "filigree-ed2ccaf10d"
  ```

### Workflow B — Triage and grooming (observations, labels, deps, search)

**What I did:** `list_observations` → `batch_dismiss_observations` (4 prior-review residue + 1 bogus ID for error-shape) → `observe` → `promote_observation` → `add_label P0` (rejected) → `add_label review:needed` → `add_label review:done` → `list_labels(namespace=cluster)` → `search_issues` (multiple queries) → `list_issues(label=cluster:mcp-review-d)` → `get_blocked` → `remove_label does-not-exist`.

**What worked:**
- `get_label_taxonomy` is genuinely well-designed — `auto`/`virtual`/`manual_suggested`/`bare_labels` partitions, with `reserved` patterns (`P[0-4]`) and reasons. Saved a doc lookup.
- `add_label P0` rejected with the correct fix in the error message:
  ```json
  { "error": "Label 'P0' conflicts with the priority field; set the numeric priority field or filter with --priority instead of using P0-P4 or priority:* labels.", "code": "VALIDATION" }
  ```
- `batch_dismiss_observations` with mixed valid + bogus IDs returned partial success cleanly; `failed[]` shape is `{id, error, code}`.
- `promote_observation` returned a flat `PublicIssue` with `issue_id` (not `id`, not `{issue:...}`) — the prior reviews' envelope complaint is fixed.
- `remove_label` on a non-existent label returned `label_result: "not_found"` and a full issue record — graceful.

**What hurt:**
- **`search_issues` silently chokes on hyphens and short tokens.**
  ```json
  mcp__filigree__search_issues({"query": "mcp-review-d"}) → { "items": [], "has_more": false }
  mcp__filigree__search_issues({"query": "Scratch start_work"}) → { "items": [3 hits including "[mcp-review-d] Scratch task A"] }
  mcp__filigree__search_issues({"query": "mcp-review-d Scratch"}) → { "items": [], "has_more": false }
  ```
  An agent searching for its own work prefixed with `[mcp-review-d]` finds nothing. The FTS tokenisation (hyphens-as-separators, single-letter elision) is not documented in the tool description.
- **Mutual exclusivity for `review:` is enforced silently.** Taxonomy says `mutually_exclusive: true`. Adding `review:done` after `review:needed` removed `review:needed` from the labels list. The response showed `label_result: "added"`, no `data_warnings`, no `replaced_label`:
  ```json
  // before: labels = [..., "review:needed"]
  add_label("review:done") → label_result: "added", labels = [..., "review:done"]
  // review:needed silently removed; nothing in response signals the displacement.
  ```
- **`get_blocked` excludes wip issues.** Scratch task A was `in_progress`, `blocked_by: ["filigree-6177ac67a3"]` per `get_issue`, but `get_blocked` returned `items: []`. An agent asking "what's actually stuck right now" misses the in-progress dead-end case.
- **`list_observations` has no priority / actor / age / `source_issue_id` filter** — only `file_id`/`file_path`. Already on the roadmap as `filigree-b0af8a661b`, but worth re-flagging: 14 pending observations in this DB include 4 synthetic from prior reviews, mingled with real findings. Triage required title-substring scanning by hand.

### Workflow C — Multi-agent coordination (claims, leases, reclaim, conflicts)

**What I did:** `start_work` (mine) → `claim_issue` as `other-agent-x` → `start_work` on it (CONFLICT) → `heartbeat_work` (explicit actor) → `heartbeat_work` (wrong expected_assignee) → `heartbeat_work` (no actor) → `reclaim_issue` (correct expected) → `reclaim_issue` (wrong expected) → `release_claim(if_held=true)` → `claim_issue` (wip handoff) → `batch_update(status=closed)` mixed with bogus ID → `reopen_issue` → `update_issue` from a different actor.

**What worked:**
- `start_work` + heartbeat happy path: 48h lease set; second `heartbeat_work(lease_hours=2)` correctly shifted `claim_expires_at` to +2h.
- `start_work` on already-claimed issue: clean CONFLICT with the holder named.
  ```json
  { "error": "Cannot claim filigree-45e59b63e9: already assigned to 'other-agent-x'", "code": "CONFLICT" }
  ```
- `reclaim_issue(expected_assignee="other-agent-x")` succeeded; `reclaim_issue(expected_assignee="phantom-agent")` returned a clean CONFLICT with both current and expected named.
- `claim_issue` on an unassigned wip-status issue worked (handoff pickup), as the docstring promises.
- `release_claim(if_held=true)` is correctly idempotent: the second call on the same now-unassigned issue would no-op; the first cleared assignee.

**What hurt:**
- **`heartbeat_work` with no `actor` defaults to `'mcp'` as the expected holder.**
  ```json
  mcp__filigree__heartbeat_work({"issue_id": "filigree-725be04601"})
  → { "error": "Cannot heartbeat filigree-725be04601: assigned to 'mcp-review-d' (expected 'mcp')", "code": "CONFLICT" }
  ```
  An agent that forgets to pass `actor` to a heartbeat fails to keep its own lease alive — silently letting the lease expire. The docstring says "By default actor is treated as the expected current holder", which suggests "the assignee", not the literal string `'mcp'`.
- **Write-path enforcement asymmetry — major.** Heartbeat / release / reclaim strictly check claim ownership. `update_issue` / `batch_update` / `add_comment` / `add_label` / `close_issue` do not. I closed scratch task A (held by me) using a `batch_update(status=closed)` issued with `actor=mcp-review-d` (no explicit expected_assignee), and the same call would succeed with any actor:
  ```json
  batch_update({issue_ids:[A,B,C,bogus], status:"closed"}) → succeeded:[A,B,C], failed:[bogus]
  // No CONFLICT on A even though A had assignee=mcp-review-d and someone else could have invoked.
  ```
  Multi-agent handoff guarantees become "best-effort" outside of the heartbeat path. The natural fix is to have all write tools accept an optional `expected_assignee` and check it when present, mirroring `reclaim_issue`.
- **`release_claim` on a wip issue strands it.** `task` template has `open → in_progress → closed`; no `in_progress → open` transition. After `release_claim`, the issue is `in_progress` with no assignee:
  ```json
  release_claim(filigree-6177ac67a3) → status: "in_progress", assignee: "", is_ready: false
  get_valid_transitions(filigree-6177ac67a3) → [{ "to": "closed", ... }]   // only forward
  ```
  Such an issue is invisible to `get_ready` (wip category), invisible to `get_blocked` (wip category), invisible to `get_summary`. The only path back is `claim_issue` by an agent who somehow already knows the issue ID. There is no "needs-new-owner" surface.
- **`update_issue` / `close_issue` accept any actor, but template-side enforcement differs.**
  ```json
  update_issue(scratch_bug_in_triage, status="closed") → INVALID_TRANSITION (triage→closed not allowed)
  close_issue(scratch_bug_in_triage) → status: "closed"   // bypassed the workflow!
  ```
  Two tools, same target state, opposite enforcement. The bug template expects `triage → confirmed → fixing → verifying → closed`; `close_issue` shortcuts past all of that.

### Workflow D — Scans, findings, files, annotations

**What I did:** `list_scanners` → `list_files(path_prefix=src/filigree/mcp_tools/)` → `list_findings(status=open)` → `report_finding` (×2) → `list_observations(file_id=...)` → `promote_finding` → `dismiss_finding` → `get_finding` → `preview_scan` → `annotate_file(critical=true, intent=handoff)` → `list_attention_annotations` → `list_findings(severity=low, status=false_positive, limit=3)` → `get_scan_status(scan-doesnotexist)`.

**What worked:**
- `list_scanners` returns risk metadata an agent actually needs: `safe_preview_only`, `requires_approval`, `may_send_contents`, `risk_summary`, `requires_dashboard`. Cautious-by-default.
- `preview_scan` returned the exact command, `valid: true`, and the same risk metadata before any process spawn.
- `annotate_file` returns full **provenance**: `commit_ref`, `branch`, `file_checksum`, `file_size`, `file_mtime`, `anchor_match_confidence`, `worktree_diff_summary`, `provenance_flags: ["dirty_worktree"]`. This is the most agent-friendly tool in the surface.
- `list_attention_annotations` filters to active critical annotations cleanly.
- `list_findings` pagination envelope is clean: `{items, has_more, next_offset?}`.
- `get_scan_status` for a bogus ID returns a clean NOT_FOUND.
- `dismiss_finding` records `metadata.dismiss_reason` for audit.

**What hurt:**
- **`report_finding` silently spawns an unlinked observation.** Same anti-pattern that `filigree-42e0aa3c89` (Agent systems finding 4) was meant to fix:
  ```json
  report_finding({...}) → { finding_id: "filigree-sf-3b0ccbf633", observation_ids: ["filigree-obs-e2b67726e5"], ... }
  list_observations(file_id=...) → [{
    summary: "[agent] src/filigree/mcp_tools/issues.py:10 -- Synthetic finding ...",
    source_issue_id: "",   // empty — no source_finding_id field exists
    actor: "scanner:agent"
  }, ...]
  ```
  The observation has no structural link back to the finding (no `source_finding_id` field). After `dismiss_finding(filigree-sf-3b0ccbf633)`, the observation `filigree-obs-e2b67726e5` is still pending. After `promote_finding(filigree-sf-bdfda71c66)`, the observation `filigree-obs-06d735f474` is still pending. So every closed/promoted finding leaves a 14-day zombie observation that looks like fresh work.
- **`dismiss_finding` only writes `false_positive`.** The `update_finding` enum has `acknowledged`, `fixed`, `unseen_in_latest`, `false_positive`, `open`. There is no path through `dismiss_finding` to record "won't fix here" or "duplicate" — the natural verb forces the wrong status name.

### Workflow E — Planning (`create_plan`, `add_plan_step`, `label_plan_tree`)

**What I did:** `create_plan` (milestone + 2 phases × 2 steps with `[0]` same-phase and `"0.1"` cross-phase deps) → `get_plan` → `add_plan_step(deps=[issue_id])` → `label_plan_tree`.

**What worked:**
- `create_plan` did all of: 1 milestone + 2 phases + 4 steps + 4 dependency edges (2 same-phase, 1 cross-phase) + label propagation to all 7 issues, in a single call. The `int` vs `"p.s"` dep notation is concise.
- `get_plan` returned per-phase `{total, completed, ready}` plus `progress_pct` — exactly the structure a plan-runner needs.
- `add_plan_step` accepts full issue IDs in `deps` (cleaner than `create_plan`'s mixed indices) and inherited the phase's labels.
- `label_plan_tree` returned `succeeded: [8 ids]` — the milestone + all phases + all steps including the `add_plan_step` addendum, post-creation.

**What hurt:**
- **`create_plan` dep syntax is heterogeneous.** Same-phase = `int` (0-indexed step). Cross-phase = `"p.s"` (string with two 0-indexed dots). Within one tool's parameter object both forms are mixed in the same `deps` array. Mostly fine but error-prone for agents that programmatically generate plans.
- **`undo_last` does not cover plan-creation events.** I created a plan, then did a title change, then `undo_last` (rolled back the title), then `undo_last` again returned `{undone: false, reason: "No reversible events to undo"}`. The plan creation, the `label_plan_tree`, and the `add_plan_step` are all post-hoc immutable. If an agent generates a wrong plan tree, there's no rollback — only batch_close + archive.

### Workflow F — Recovery and edge cases

**What I did:** `update_issue(title)` → `undo_last` → `undo_last` again → `validate_issue` on the promoted bug → `get_changes(since=...)` → `create_plan` with bad dep index → self-dep → cycle → `get_issue(bogus_id)` → `create_issue(type=no-such-type)` → `archive_closed(days_old=0, label=mcp-review-scratch)`.

**What worked:**
- `update_issue` on closed task returned the cleanest error in the surface:
  ```json
  { "error": "Transition 'closed' -> 'in_progress' is not allowed for type 'task'. Use get_valid_transitions() to see allowed transitions.",
    "code": "INVALID_TRANSITION", "valid_transitions": [], "reopen_available": true,
    "hint": "Use reopen_issue to return this closed issue to the last non-done status before closure" }
  ```
  Agent gets the diagnosis, the alternative tool name, and the semantic in one envelope.
- `validate_issue` cleanly distinguished `valid: true, warnings: [...], errors: []` — the typecheck-style separation an agent expects.
- `add_dependency` cycle detection: `Dependency X -> Y would create a cycle` (VALIDATION).
- `add_dependency` self-loop: `Cannot add self-dependency: X` (VALIDATION).
- `create_plan` with `deps: [99]` in a 1-step phase: `"Dep index out of range: step 99 in phase 0 (max=0)"` — actionable.
- `create_issue(type="no-such-type")` lists every valid type in the error string.
- `archive_closed(days_old=0, label="mcp-review-scratch")` swept up 17 closed issues — including 14 leftover from prior reviews and 3 of mine. Cleanup primitive works.

**What hurt:**
- **`get_changes` includes `heartbeat` events.** With 48h leases and aggressive heartbeating, the catch-up firehose is dominated by liveness pings. Filter is single-value: `type` accepts one event-type, not a list; `actor` and `issue_id` are single-valued too. To reconstruct a coherent multi-actor catch-up, an agent must paginate the firehose without filters and re-categorise client-side. Already partially flagged historically as `filigree-352002daba` (closed).
- **`undo_last` covers field changes but not label-tree, dep-add, or step-add.** The line "Covers status, title, priority, assignee, description, notes, claims, and dependency changes" is honest, but a label_plan_tree mistake has to be reversed by hand (8 batch_remove_label calls).
- **Empty-`status` shape in `start_next_work` differs from happy-path.** `{status: "empty", reason: "..."}` vs the flat `PublicIssue` shape. Agent must branch on the presence of `issue_id`.

---

## 3. Findings

### P1 — degrades real workflows

1. **P1 — Write-path enforcement asymmetry breaks claim contract.** `heartbeat_work`/`release_claim`/`reclaim_issue` strictly enforce claim ownership; `update_issue`/`batch_update`/`add_comment`/`add_label`/`close_issue` ignore it. A non-claimant can rename, transition, comment, or close a held issue without `CONFLICT`. **Evidence:** `batch_update({issue_ids:[claimed_by_me, ...], status:"closed"})` succeeded; the matching heartbeat from the same agent without explicit `actor` failed with `expected 'mcp'`. **Why it matters:** any multi-agent guarantee built on `claim_issue + heartbeat` can be silently overwritten by a sibling tool that doesn't check. **Resolution:** add an optional `expected_assignee` to write tools; when present, return CONFLICT just like reclaim. Document the default (no check) clearly.

2. **P1 — `report_finding` spawns an unlinked observation that outlives the finding.** An observation is auto-created with no `source_finding_id` field; only embedded in title text via the `[agent]` prefix. Closing or promoting the finding does not touch the observation. **Evidence:** `report_finding` → `observation_ids: ["filigree-obs-e2b67726e5"]`. After `dismiss_finding`, `list_observations` still shows the orphan. After `promote_finding`, the second observation also still exists. **Why it matters:** every agent finding becomes 14 days of triage debt; the observation queue accumulates duplicates of work already triaged via the finding queue. The bug `filigree-42e0aa3c89` was supposed to address this. **Resolution:** add `source_finding_id` to observations; on `dismiss_finding`/`promote_finding`, auto-dismiss the linked observation; or stop creating the observation by default and gate it behind an explicit `also_observe=true` flag.

3. **P1 — `release_claim` strands wip-category issues.** Releasing a claim while in `in_progress` clears `assignee` but leaves `status="in_progress"`. The `task` template has no backwards transition, so the issue becomes invisible to `get_ready`, `get_blocked`, `get_summary`, and the only handoff path is `claim_issue` by an agent who already knows the ID. **Evidence:** `release_claim(filigree-6177ac67a3)` → `status: "in_progress", assignee: "", is_ready: false`. **Why it matters:** the documented "release-for-handoff" pattern requires a discoverable surface for orphans. **Resolution:** either (a) auto-revert wip→open category when releasing the claim (with template-aware target picking), or (b) ship a `list_orphan_wip` / `list_handoff_pool` discovery tool, or (c) extend `get_ready` to include unassigned wip-category issues with a marker.

4. **P1 — `close_issue` bypasses workflow enforcement that `update_issue` enforces.** Same target state, opposite contract. **Evidence:** `update_issue(bug_in_triage, status="closed")` → `INVALID_TRANSITION` (correctly rejecting `triage → closed`). Immediately after, `close_issue(bug_in_triage)` succeeded with `status: "closed"`. **Why it matters:** templates are advisory or mandatory, and right now they're both. An agent reading the bug template sees `triage → confirmed → fixing → verifying → closed` but the surface allows shortcuts whenever the agent picks the right tool. **Resolution:** route `close_issue` through the same transition validator. If the goal is to allow "rage-close from anywhere," gate it behind `force=true` (the same shape as `delete_file_record(force=true)`).

### P2 — real friction, has workaround

5. **P2 — `heartbeat_work` defaults `actor` to `'mcp'`, not the assignee.** Empty-actor heartbeat from the rightful holder fails with `expected 'mcp'`. **Evidence:**
   ```json
   heartbeat_work({issue_id: "filigree-725be04601"})
   → "error": "Cannot heartbeat filigree-725be04601: assigned to 'mcp-review-d' (expected 'mcp')"
   ```
   **Resolution:** if `actor` is omitted, treat the call as actor-less (skip the holder check) and only set `expected_assignee` when the caller passes it. Or surface the inferred default in the docstring.

6. **P2 — `search_issues` silently tokenises on hyphens and drops short terms.** Query `mcp-review-d` returns `[]`, `Scratch start_work` returns 3 hits including the same titles. **Why it matters:** agents prefix their work for self-discovery (`[mcp-review-d]`), then can't find it. **Resolution:** document FTS tokenisation in the tool description, or switch to a substring/LIKE pre-filter.

7. **P2 — Mutual exclusivity for `review:` is enforced silently.** Adding `review:done` after `review:needed` removes the prior label with no `data_warnings`, no `replaced_label`, `label_result: "added"`. **Resolution:** when a mutually-exclusive sibling is displaced, return `label_result: "replaced"` and include `replaced_label: "review:needed"` (and/or a `data_warnings` entry).

8. **P2 — `get_blocked` excludes wip-category issues.** A wip task blocked by another open issue does not appear. **Evidence:** scratch task A in `in_progress`, `blocked_by: [filigree-6177ac67a3]`, `get_blocked` returned `items: []`. **Resolution:** include wip-category blocked issues, or add a `categories` filter; otherwise an agent doing "what's stuck" misses the in-progress dead-end case.

9. **P2 — `parent_id` vs `parent_issue_id` naming inconsistency.** `get_issue` uses `parent_id`; `get_ready`/`list_issues` use `parent_issue_id`. **Resolution:** pick one (almost certainly `parent_issue_id`, given the 2.0 rename) and migrate `get_issue`. Schema-mismatch warnings for older clients.

10. **P2 — `list_observations` has no priority/actor/age/source filters.** Already on the roadmap as `filigree-b0af8a661b`. Confirmed live: 14 pending observations from at least 4 actors; only `file_id`/`file_path` filters available. Triage required scanning summary text by hand to separate prior-review residue from real findings.

11. **P2 — `get_changes` firehose includes heartbeat events; filters are single-value.** Active leases dominate the catch-up feed. `type` filter accepts one event-type, `actor` is single-valued. Already partially flagged historically. **Resolution:** allow list values; add `event_categories` (e.g. `[lifecycle, content, claim]`); or default-exclude `heartbeat` from `get_changes` and surface it through a separate liveness tool.

### P3 — papercuts

12. **P3 — `get_summary` is markdown while peers are JSON.** Output-shape mix forces dual parsing. **Resolution:** add `format="json"` parameter or a sibling `get_summary_json`.

13. **P3 — `dismiss_finding` only writes `false_positive`.** No path to "won't fix in scope" / "duplicate" / "deferred". `update_finding` accepts the wider enum; the natural verb does not. **Resolution:** accept `status` parameter on `dismiss_finding` (default false_positive).

14. **P3 — Failed-batch entries use `id`; succeeded entries use the entity's primary key.** `failed: [{id, error, code}]` vs `succeeded: [{observation_id, ...}]` / `{issue_id, ...}` / `{finding_id, ...}`. Agents must remember different keys per side. **Resolution:** rename `failed[].id` to match the entity's primary key field, or normalize both sides to `entity_id`.

15. **P3 — `start_next_work` empty-result shape `{status, reason}` differs from happy-path flat issue.** Agent must branch on `issue_id` presence. **Resolution:** wrap successful results in the same envelope, e.g. `{status: "claimed", issue: {...}}` vs `{status: "empty", reason: ...}` — but acknowledge breaking change.

16. **P3 — `create_plan` dep syntax mixes `int` and `"p.s"` strings in the same array.** Works fine but error-prone for code generators. `add_plan_step` taking full issue IDs is cleaner. **Resolution:** allow full issue IDs in `create_plan` deps too, alongside the index sugar.

17. **P3 — `archive_closed(days_old=0)` is permissive.** Archived 17 issues including ones closed seconds earlier in this session. With the label filter it's the right tool, but `days_old=0` is a footgun for label-less invocations. **Resolution:** require a non-empty `label` filter when `days_old < 7`, or warn in the response with `data_warnings`.

18. **P3 — `undo_last` does not cover label-tree, dep-add, or step-add.** Documented honestly, but the cliff is steep — a `label_plan_tree` mistake takes manual `batch_remove_label` to reverse. **Resolution:** extend reversibility to label and structural events, OR provide a `revert_label_plan_tree(milestone_id, label)` companion.

---

## 4. What works well (don't refactor away)

- **`INVALID_TRANSITION` error envelope.** Returns `valid_transitions`, `hint`, and `reopen_available: true` for closed→wip attempts. Best diagnostic shape in the surface.
- **`create_plan`** as a single-call milestone+phases+steps+deps+labels primitive.
- **`annotate_file` provenance** — commit ref, branch, file checksum, anchor confidence, dirty-worktree flag. Agent-grade traceability.
- **`get_label_taxonomy`** — auto/virtual/manual_suggested/reserved with examples and reasons. Saves a doc lookup.
- **`get_schema`** — entity ID prefixes plus `accepted_by_tools` per family. Self-describing surface.
- **`list_scanners` + `preview_scan` risk metadata** — `safe_preview_only`, `may_send_contents`, `risk_summary`. Cautious-by-default.
- **`archive_closed(label=...)`** — the cleanup story works when the agent uses the label filter.
- **`start_next_work` happy path** — claim + transition + lease + heartbeat in one tool.
- **`get_summary` Epic Progress bar `[████░░░░] 3/6`** — glanceable for cold start.
- **`list_attention_annotations`** — surfaces active critical handoff annotations cleanly.
- **`{has_more, next_offset?}` pagination envelope** — used consistently across list tools.
- **`{succeeded: [...], failed: [...]}` batch envelope** with `failed[]` always present (empty when all-OK) — matches the published 2.0 contract.

## 5. Open questions for the maintainer

1. **Is the silent label displacement on `mutually_exclusive: true` namespaces deliberate?** If yes, it should be at least surfaced in `data_warnings` so agents can audit. If no, returning `label_result: "replaced"` plus `replaced_label: "..."` is the obvious shape.
2. **Should `report_finding` create a parallel observation by default?** Given the prior bug `filigree-42e0aa3c89` (closed), the fix may have been "link them better in the response" rather than "stop creating them." If the parallel observation is the intended UX, please add `source_finding_id` to the observation row and auto-dismiss on finding close/promote.
3. **What's the intended discovery path for unassigned wip issues after `release_claim`?** The current state (invisible to `get_ready`, `get_blocked`, `get_summary`) may be deliberate but it's the only place in the surface where "I orphaned work" doesn't surface anywhere.
4. **Should `update_issue`/`batch_update`/`close_issue`/`add_comment`/`add_label` accept an optional `expected_assignee`?** Symmetric with `reclaim_issue` and would close the write-path enforcement asymmetry without breaking anything.
5. **Is `close_issue` intended as a workflow shortcut, or a workflow-respecting tool?** If the former, gate behind `force=true`. If the latter, route through the same transition validator that `update_issue` uses.
6. **`from-finding` vs `from-observation` vs (future?) `from-annotation` label namespaces** — should there be one `source:promoted-from-...` family, or are these three deliberately separate?
7. **Should `get_changes` default-exclude `heartbeat` events for catch-up consumers?** Or split into `get_changes` (lifecycle) + `get_liveness` (heartbeats)?

---

**Cleanup state at end of run:** all `cluster:mcp-review-d` issues closed (11 via `batch_close`); `archive_closed(label=mcp-review-scratch)` swept 17 (mine + prior reviews); `batch_dismiss_observations` cleared 4 prior-review residue + 2 review-d auto-observations; `resolve_annotation` cleared the scratch annotation. Database returned to a clean state.
