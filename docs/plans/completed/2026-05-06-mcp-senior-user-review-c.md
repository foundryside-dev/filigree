# Filigree MCP — Senior-User Friction Review (Reviewer C)

Date: 2026-05-07
Reviewer: review-c (Claude Opus 4.7, 1M)
Filigree version: project DB schema v10 (dashboard install is older — see Open Questions)
Scope: live drive of `mcp__filigree__*` against the working repo's `.filigree/` DB.
Scratch label used: `mcp-review-scratch-c` (synthetic issues `filigree-fdd3a35dda`, `filigree-468e183c3e`, `filigree-3609bf269e` left closed; finding `filigree-sf-409dcab9fd` left dismissed).

---

## 1. Executive summary

The MCP surface is rich, well-documented, and largely usable — for an agent following the happy path. **Two structural issues degrade real workflows**: (a) `start_work`, advertised in CLAUDE.md as the canonical claim-and-start tool for 2.0, does not work for the most common bug-pickup path because it ignores graph reachability; and (b) the write-side response envelope is fragmented into at least five distinct shapes (`undo_last`, `promote_observation`, `add_comment`, `report_finding`, normal writes) with `id` vs `issue_id` drift, forcing per-tool special-casing. Soft-enforcement transition warnings exist in the event log but never surface in `data_warnings`, so an agent has no in-band way to learn it skipped a recommended field.

---

## 2. Per-workflow walkthrough

### Workflow A — Session start, find work

**Calls:** `get_summary`, `get_stats`, `get_ready`, `get_blocked`, `list_issues(status_category=wip)`, `search_issues`.

What worked:
- `get_summary` is a great single-call orientation: vitals, ready list with parent-epic context inlined ("(MCP agent-systems effectiveness review (2026-05-07))"), in-progress, blocked, epic progress, and a stale-observation prompt at the bottom. This is the right kind of "hello, agent" surface.
- `get_ready` returns slim items with `priority asc` ordering. Combined with `start_next_work`, it's a fast pickup loop.
- `get_blocked` returned a clean empty `items[]` envelope — no inconsistency vs. populated lists.
- Error code surface is consistent (`{error, code, ...}` with stable enum) — easy to switch on.

What hurt:
- **Stat counts disagree quietly.** `get_stats` shows `by_status: {open: 4, planning: 1, proposed: 3, confirmed: 1, ...}` but `by_category.open: 9`. Both are correct, but `open` is overloaded — it's both a *literal status name* (used by tasks) and a *category name* (covering planning, proposed, confirmed, …). An agent skimming `by_status.open: 4` and `ready_count: 9` will momentarily think they disagree. A single reading "open category statuses by name" would be less ambiguous.
- `get_ready` slim payload omits parent epic. The same data is rendered nicely in `get_summary` ("(parent epic title)") but the structured `get_ready` doesn't carry it. Picking work programmatically therefore needs a follow-up `get_issue` per item to reconstruct the same context the human-readable summary already has.

### Workflow B — Claim and start work

**Calls:** `start_work` (×3 attempts), `update_issue`, `claim_next`, `claim_issue`, `release_claim`, `get_valid_transitions`, `get_issue_events`.

What worked:
- `claim_next` returned `selection_reason: "Highest-priority P3, ready issue (no blockers)"` — a small touch that lets an agent log *why* it picked an issue. Excellent.
- The INVALID_TRANSITION error response inlines `valid_transitions[]`. Recovery is one call away, not two.
- Conflict on double-claim names the current assignee in the message: `"Cannot claim ...: already assigned to 'review-c'"`. An agent that race-claimed itself can detect that.

What hurt — the structural issue:
- **`start_work` does not work for fresh bugs.** Bug initial state is `triage` (open). I tried:
  ```
  start_work(filigree-fdd3a35dda, assignee=review-c)
  → "start_work ambiguous for type 'bug': multiple wip-category targets
     available (['fixing', 'verifying']). Specify target_status explicitly."
  ```
  OK — pass it explicitly:
  ```
  start_work(... target_status="fixing")
  → "Transition 'triage' -> 'fixing' is not allowed for type 'bug'."
  ```
  The first error doesn't admit that no wip target is reachable from `triage` at all; the second doesn't acknowledge that `start_work` was the caller. The actual canonical flow turns out to be three calls:
  1. `update_issue(status=confirmed)` to clear triage.
  2. `start_work(target_status=fixing)` to claim+advance — **still** must specify `target_status` because from `confirmed` the tool sees both `fixing` and `verifying` as wip even though only `fixing` is reachable.
  3. Continue with comments, fields, etc.

  CLAUDE.md says "`start_work`/`start_next_work` — atomically claim and transition to in-progress (the usual way to pick up work in 2.0)". For tasks (open→in_progress, single wip status), it works. For bugs — the project's most common type by 5× — it doesn't. The fix is graph-aware: `start_work` should consider only wip statuses *reachable from the current state*, and surface "no wip target reachable from `triage` for bug" when none exist.

- **Failed `start_work` attempts pollute the event log.** Each call emits `claimed` + `released` events with millisecond-apart timestamps:
  ```
  2278 claimed   review-c
  2279 released  review-c
  2280 claimed   review-c
  2281 released  review-c
  2285 claimed   review-c
  2286 released  review-c
  ```
  The schema doc says "On transition failure the claim is rolled back" — the rollback works, but it doesn't suppress the audit trail. For a flow where a confused agent retries 3–4 times to find the right `target_status`, the issue's history accumulates noise that makes legitimate claim events harder to read.

- **`release_claim` is non-idempotent.** Calling release on an unassigned issue returns `{error: "...no assignee set", code: CONFLICT}`. Defensible if you want to detect double-release, but the symmetric `claim_issue` self-claim is also a CONFLICT, so an agent that wants "release if held" needs a get-then-release dance. A `force` or `if_held` parameter would help.

### Workflow C — Update, comment, label, transition

**Calls:** `update_issue` (status changes), `add_comment`, `get_comments`, `add_label`, `validate_issue`, `get_valid_transitions`, `get_issue_events`.

What worked:
- `validate_issue` is genuinely useful — it told me "Field 'root_cause' is recommended at state 'fixing' for type 'bug' but is not populated" and "Transition to 'verifying' requires: fix_verification". Calling this before close is the right reflex.
- `get_comments` returned the list with stable shape `{items[], has_more}`.

What hurt:
- **`data_warnings` never surfaces soft-enforcement misses.** I transitioned `triage→confirmed` without setting `severity` (required_at: confirmed). Response had `data_warnings: []`. But the audit log shows:
  ```
  2282 transition_warning   "Missing recommended fields for 'confirmed': severity"
  2283 transition_warning   "Missing recommended fields: severity"
  2284 status_changed       triage → confirmed
  ```
  The warning fires, gets logged twice (see below), and is silently dropped from the response. An agent can only learn it violated soft enforcement by re-fetching events. Either `data_warnings` should be populated here, or it should be deleted from the response shape (since it's currently dead).

- **Duplicate `transition_warning` events.** Events 2282 and 2283 are the same warning with different comment text ("Missing recommended fields for 'confirmed': severity" vs "Missing recommended fields: severity"). Two writers? Two layers of validation each emitting their own event? Either way, it doubles audit volume.

- **`add_comment` and `add_label` return status-shape responses.** `add_comment` → `{status, comment_id}`. `add_label` → `{status, issue_id, label}`. Compare with `update_issue` which returns the full issue. To know "what's the issue look like after I added that label", agents do a follow-up `get_issue`. Inconsistent with the rest of the write surface.

- **`add_label` accepts `P1` and `priority:1` without complaint.** Bare labels `P1`/`P2`/`P3` already coexist with the first-class `priority` field (0–4), and `priority:N` is just one more. Three potential sources of truth for the same fact. The label taxonomy doesn't reserve `priority:` or warn against `P*` bare labels. The fix is policy, not code, but it should be enforced — either reject `priority:`-namespaced labels with a "use the priority field" hint, or auto-sync them.

### Workflow D — Close, reopen, undo

**Calls:** `update_issue(status=closed)` (rejected), `update_issue(status=verifying)` (without then with `fix_verification`), `close_issue`, `reopen_issue`, `undo_last` (×3).

What worked:
- The hard-enforcement boundary (`verifying→closed` requires `fix_verification`) does block: `close_issue` only works when the issue is at `verifying` with the field set. Good.
- `close_issue`'s INVALID_TRANSITION error from `fixing` returned `valid_transitions: [{to: verifying, ...}]` inline. One-call recovery again.
- `undo_last` is more powerful than I expected — it walks back fields, status, claims one event at a time. Useful for "I just did three things and I want to back out the last one."

What hurt:
- **`reopen_issue` resets bug to `triage`, regardless of where it was before close.** A bug that went `triage→confirmed→fixing→verifying→closed`, on reopen, lands back at `triage` — but its `close_reason`, `fix_verification`, and `root_cause` (if set) all *remain populated*. The state machine and the field-set are out of sync. A more sensible default would be the last open-category state before close (`confirmed` here), or at least clearing the close-only fields when status is reset to initial.

- **`undo_last` returns a different envelope from every other write.** Compare:
  ```
  update_issue → {issue_id: "filigree-...", title, status, ...}        # flat
  close_issue  → {issue_id: "filigree-...", title, status, ...}        # flat
  undo_last    → {undone: true, event_type, event_id, issue: {id: "filigree-...", ...}}
  ```
  Two divergences in one response: wrapped under `issue`, *and* `id` instead of `issue_id`. An agent that pipelines `update→undo→update` cannot use a uniform `result.issue_id` accessor.

- **`promote_observation` has the same divergence as `undo_last`.** It returns `{issue: {id: "filigree-...", ...}}` — wrapped, with `id` not `issue_id`. Another agent has already filed this as `filigree-obs-e8d052394e`, confirming it's not just one-off drift.

- **`promote_observation` doesn't show the `from-observation` label in the immediate response.** Tool description says it creates the issue with that label. The promote response had `labels: []`. The label was actually applied — visible after the next read — but the immediate response misrepresents the state. Agents who trust the response will think the label is missing and add it again.

### Workflow E — Observations & scratchpad

**Calls:** `observe`, `list_observations`, `list_observations(file_path="mcp_tools")`, `dismiss_observation`, `promote_observation`.

What worked:
- The "fire and forget, expires in 14 days" model is exactly right for ambient agent notes. CLAUDE.md's framing ("Observations are ambient — fire off as you go") is reflected in the surface: `observe` is one call, takes a summary plus optional `file_path`/`line`/`detail`/`source_issue_id`.
- `list_observations(file_path="...")` substring-matches — `mcp_tools` returned 4 observations across `mcp_tools/issues.py`, `observations.py`, `workflow.py`. Useful triage filter.
- The session-start prompt ("STALE OBSERVATIONS: 9 older than 48h") is a great nudge. Agents tend to forget housekeeping; surfacing it on every session is the right design.

What hurt:
- **Three different shapes for the three observation lifecycle endpoints.**
  ```
  observe              → {id, summary, detail, ...}                    # full record
  promote_observation  → {issue: {id: "filigree-...", ...}}            # wrapped, id-not-issue_id
  dismiss_observation  → {status: "dismissed", observation_id}         # status-only
  ```
  The `observe` shape is fine. The other two are different from each other *and* from the issue write shapes. There's no consistent "what kind of envelope does a write return?" rule.

- **No way to attach an observation to multiple files.** `observe` takes one `file_path`. Pattern observations ("this same issue is in db_files.py and dashboard_routes/files.py") have to fire two `observe` calls or stuff both paths in the summary text.

- **`source_issue_id` is opaque.** Existing observations populate it (e.g., `filigree-obs-76b54a25ef` references `filigree-873962aa58` because a similar bug was found while working on that issue). But there's no MCP tool to *list observations attached to issue X*. `list_observations` filters by file_path/file_id only.

### Workflow F — Scans, findings, files, plans

**Calls:** `list_scanners`, `preview_scan`, `report_finding`, `list_findings`, `dismiss_finding`, `list_files`, `get_critical_path`, `get_metrics`, `list_packs`, `explain_status`, `get_workflow_guide`, `get_changes`.

What worked:
- `preview_scan` returns the full `command[]` and `command_string` — no need to dry-run a real scanner to learn what it would do. Helpful when troubleshooting scanner config.
- `report_finding` is the "no scanner config needed" path the description promises. One call attached a finding to a file with no setup.
- `get_workflow_guide(pack=core)` returned an actually useful state diagram, "tips", and "common_mistakes". The mistakes list ("Closing bugs without fix_verification …") matches what an agent should check pre-close.
- `explain_status` for `bug:verifying` enumerates inbound + outbound + required_fields cleanly. Better than re-deriving from `get_type_info`.
- `get_metrics(days=7)` returned `throughput: 207, avg_cycle_time_hours: 0.3` — a useful health pulse without writing any SQL.

What hurt:
- **Scanner ecosystem is sparse.** `list_scanners` returned a single scanner (`claude-code`). For a workflow that's structured around "scan/triage/promote findings", that's underwhelming. (May be a config gap, not a tool gap.)
- **`list_files` returns empty `language` on `.py` files.** All three files in the `mcp_tools/` filter had `language: ""`. Filtering by `language=python` is therefore useless against current data. Auto-detection isn't running, or it doesn't infer from extension.
- **`report_finding` returns yet another envelope:** `{status, findings_created, findings_updated, file_created, finding_id}`. Reasonable for an idempotent reporter, but it means *six* distinct write-response shapes across the surface (issue-flat, observation-full, comment-status, label-status, undo/promote-wrapped, finding-counts).
- **`get_template` and `get_type_info` are near-duplicates.** Both return `states[]`, `transitions[]`, `fields_schema[]`. Only difference: `get_type_info` also returns `pack`. Two tools doing the same thing is one too many — `get_type_info` should swallow `get_template` (or vice versa) and the other should be deprecated or refactored to a thin alias.
- **`get_critical_path` returned `{path: [], length: 0}` with no explanation.** With only 14 dependencies in the project (`get_stats`), there's no chain longer than trivial. But the tool gives no signal: "no path found because no dependencies span open issues" vs. "no open issues at all" vs. "tool stub". Empty array silence is the worst kind.

---

## 3. Findings (numbered, sorted by severity then workflow)

### P1 — workflow-blocking

**1. `start_work` does not handle bug pickup.** *(Workflow B)*
- Evidence: `start_work(filigree-fdd3a35dda, assignee=review-c)` → INVALID_TRANSITION "ambiguous wip targets ['fixing', 'verifying']". Then `start_work(... target_status=fixing)` → INVALID_TRANSITION "triage → fixing not allowed". Required workaround: `update_issue(status=confirmed)` then `start_work(target_status=fixing)`.
- Why it matters: bugs are 5× more common than tasks (`get_stats: 256 bug / 46 task`). CLAUDE.md elevates `start_work` to "the usual way to pick up work in 2.0". The flagship pickup tool fails on the flagship type.
- Suggested resolution: graph-aware target resolution — `start_work` should pick the unique wip-category status *reachable from the current state*, fall back to the unique wip status only if the current state has multiple reachable wip targets, and fail with a precise diagnostic when none are reachable. For bug at `triage`, the right error is "no wip target reachable from `triage`; advance to `confirmed` first" — not an ambiguity message that's true in the abstract but irrelevant in the concrete.

**2. `data_warnings` silently empty on soft-enforcement transitions.** *(Workflow C)*
- Evidence: `update_issue(filigree-fdd3a35dda, status=confirmed)` (severity required_at confirmed, not set) returned `data_warnings: []`. `get_issue_events` shows `transition_warning` events 2282 and 2283 covering the same call.
- Why it matters: soft enforcement is the project's main vehicle for "should-have" fields. Without an in-band signal, agents will continue to ship issues with missing severity, root_cause, fix_verification — the exact fields that downstream automation and humans rely on.
- Suggested resolution: populate `data_warnings[]` with the same payload that goes to `transition_warning` events. Same data, two destinations. While there: deduplicate the events.

### P2 — significant friction

**3. Write-response envelope is fragmented (5+ shapes).** *(Workflows C, D, E, F)*
- Evidence:
  - flat issue: `update_issue`, `close_issue`, `claim_issue`, `release_claim`, `reopen_issue`, `create_issue`
  - status-only: `add_comment` `{status, comment_id}`, `dismiss_observation` `{status, observation_id}`, `add_label` `{status, issue_id, label}`
  - wrapped + `id`: `promote_observation` `{issue: {id, ...}}`, `undo_last` `{undone, event_type, event_id, issue: {id, ...}}`
  - finding counts: `report_finding` `{status, findings_created, findings_updated, file_created, finding_id}`
  - finding flat: `dismiss_finding` (full finding at top level)
- Why it matters: any pipeline that operates on multiple write tools needs per-tool extraction. That's avoidable by-design friction. Already filed observation `filigree-obs-e8d052394e` notes the same pattern.
- Suggested resolution: pick one envelope shape for write ops in 2.x. Suggested: flat record at top level + a `meta` block for op-specific data. `undo_last` becomes `{...issue fields, meta: {undone: true, event_type, event_id}}`; `promote_observation` returns the new issue flat plus `meta: {source_observation_id}`.

**4. `id` vs `issue_id` field-name drift.** *(Workflows D, E)*
- Evidence: `undo_last.issue.id`, `promote_observation.issue.id`. Everywhere else: `issue_id`.
- Why it matters: subset of (3), but worth calling out as its own finding because it's the easier half to fix without breaking compatibility — keep the wrapping for now, just rename `id`→`issue_id` inside.
- Suggested resolution: rename. Or eliminate together with (3).

**5. `get_template` and `get_type_info` overlap.** *(Workflow F)*
- Evidence: same `states[]`, `transitions[]`, `fields_schema[]`. `get_type_info` adds `pack`. No other delta.
- Why it matters: tool-list bloat; agent has to guess which to call.
- Suggested resolution: keep `get_type_info` as canonical. Either delete `get_template`, or document it as a strict alias that returns the same payload (and route both through one implementation).

**6. `reopen_issue` resets state to initial regardless of prior progress.** *(Workflow D)*
- Evidence: bug at `closed` after going through full triage→confirmed→fixing→verifying chain. `reopen_issue` returned status `triage`. `fields.fix_verification` and `fields.close_reason` still populated — state reset, fields not.
- Why it matters: false-closure recovery is a real workflow. After `reopen` an agent expects to resume near where they were, not redo triage.
- Suggested resolution: reopen to the most recent open-category state (default), with optional `target_status` override. Either way, clear `close_reason` on reopen — it's by definition stale.

**7. `get_mcp_status` is documented but missing.** *(Workflow A)*
- Evidence: CLAUDE.md (in this repo, lines about "Schema-mismatch (warm-but-degraded MCP)") promises `get_mcp_status` "remains available as a safe read-only diagnostic". Tool is not exposed by the MCP server (`mcp__filigree__get_mcp_status` returns "No such tool available").
- Why it matters: when the schema-mismatch path *does* fire (and the dashboard already exhibits it in this session — see Open Questions), the documented diagnostic is unreachable.
- Suggested resolution: either expose the tool or remove the CLAUDE.md/AGENTS.md reference. Hidden contract.

### P3 — annoyances and rough edges

**8. Failed `start_work` attempts emit `claimed`+`released` audit pairs.** *(Workflow B)*
- Evidence: events 2278/2279, 2280/2281, 2285/2286 — six events for three failed attempts on one issue.
- Why it matters: noise in `get_issue_events` makes legitimate audit reads harder.
- Suggested resolution: roll back atomically without writing the failed claim event; or tag both events with a `rolled_back: true` flag so they can be filtered.

**9. Duplicate `transition_warning` events for one transition.** *(Workflow C)*
- Evidence: events 2282 ("Missing recommended fields for 'confirmed': severity") and 2283 ("Missing recommended fields: severity") at the same `created_at` for the same transition.
- Why it matters: doubles audit volume, makes pattern detection noisier.
- Suggested resolution: collapse to one event; investigate why two writers are firing.

**10. Bare `P1`/`P2`/`P3` labels coexist with first-class `priority` field.** *(Workflow C)*
- Evidence: `list_labels` shows `_bare: [P1: 1, P2: 9, P3: 6, ...]`. `add_label(... P1)` and `add_label(... priority:1)` both succeed silently.
- Why it matters: two — actually three — sources of truth for the same fact. Agents that filter by `--label=P1` get a different set than those filtering by `priority=1`.
- Suggested resolution: either reserve `P\d` and `priority:` namespaces (reject with a hint), or auto-sync writes to those labels into the priority field.

**11. `list_labels` truncates at 10 silently.** *(Workflow A)*
- Evidence: `_bare` namespace returned 10 labels alphabetically (P1..bug-hunt). No `total_in_namespace` count, no `truncated: true` flag.
- Why it matters: agent doesn't know labels were elided. Has to call again with `top=0` defensively.
- Suggested resolution: include `total` per namespace; or set a `truncated` flag when the cap was hit.

**12. `release_claim` non-idempotent.** *(Workflow B)*
- Evidence: `release_claim(filigree-468e183c3e)` second call → CONFLICT "no assignee set".
- Why it matters: forces a get-then-release dance for "unconditionally release if held".
- Suggested resolution: add an `if_held: true` or just make it idempotent (no-op when already released).

**13. `language` empty on tracked files.** *(Workflow F)*
- Evidence: `list_files(path_prefix=mcp_tools)` returned 3 `.py` files all with `language: ""`. `register_file` and the schema accept `language` but it's not auto-inferred.
- Why it matters: language filter on `list_files` is dead code in current data.
- Suggested resolution: infer from extension at register time as a fallback, even when scanner doesn't supply it. Or remove the `language` parameter from `list_files` until it's populated.

**14. `promote_observation` response misrepresents labels.** *(Workflow D)*
- Evidence: promote response had `labels: []`; the issue actually has `from-observation` label (visible on next `close_issue` response).
- Why it matters: agents that read the response shape will think the label is missing and re-add it (the operation is idempotent so no harm — but it's confused).
- Suggested resolution: include the auto-applied labels in the immediate response.

**15. `get_critical_path` empty result is silent.** *(Workflow F)*
- Evidence: `{path: [], length: 0}`. No diagnostic.
- Why it matters: an agent can't distinguish "no chain" from "no data" from "tool not implemented".
- Suggested resolution: include a `reason` or `status` (`"no_open_dependencies"`, `"no_open_issues"`, etc.) on empty results.

---

## 4. What works well

Don't refactor these away:

- **Inline `valid_transitions[]` on INVALID_TRANSITION errors.** Recovery is a single call, not two.
- **`claim_next.selection_reason`.** Lets an agent log its reasoning trail.
- **`get_summary` as the session-open page.** Vitals, ready+context, blocked, epic progress, stale-observation prompt — all in one call.
- **`validate_issue`.** The right pre-close reflex; warnings are concrete and actionable.
- **`get_workflow_guide` "tips" and "common_mistakes".** Actually-useful agent guidance baked into the type definition.
- **`get_label_taxonomy` as a writable-vs-virtual-vs-auto reference.** Discoverability done well.
- **Stale-observation nudge at session start.** Forces housekeeping into the agent's view.
- **2.0 list envelope (`{items[], has_more, next_offset?}`) is consistent everywhere it appears.** The list-side discipline is real; only writes are fragmented.
- **Stable error code enum** (`VALIDATION`, `NOT_FOUND`, `CONFLICT`, `INVALID_TRANSITION`, `PERMISSION`, `SCHEMA_MISMATCH`, …). Easy to switch on programmatically.

---

## 5. Open questions for the maintainer

1. **Is `data_warnings[]` intended as the soft-enforcement signaling channel, or is it for something else?** The field exists on most write responses but I never saw it populated. If it's the warning channel, finding (2) is straightforward. If it's for upstream/freshness warnings, then soft-enforcement needs its own field.

2. **What's the canonical write-response shape for 2.0?** The list-side has been disciplined to `items[]/has_more`. The write side has 5+ shapes. Is there a target shape that just hasn't been migrated to yet, or is the variation deliberate (e.g., `add_comment` deliberately doesn't return the full issue to keep payloads small)?

3. **Should `start_work` walk through soft-enforcement open states automatically?** For bug-at-triage the canonical "claim and start" is genuinely three steps. Is the multi-step intentional (agent must explicitly decide "yes, this is a real bug worth fixing" by transitioning triage→confirmed) or accidental (graph-blind ambiguity)? Different fixes depending on the answer.

4. **What's the policy for `P1`/`P2`/`P3` bare labels?** Are they a legacy artifact, deliberately allowed alongside `priority`, or actively-undesired? The label taxonomy doesn't speak to priority at all.

5. **Schema-mismatch in this very session.** The dashboard process exited at session start with `Database schema v10 is newer than this version of filigree (expects v8)`. The MCP server worked fine (these tool calls all succeeded). That's the "warm-but-degraded MCP" path — and yet `get_mcp_status` (the documented diagnostic for exactly this situation) is missing from the surface. Either the dashboard install is intentionally lagging, or the install on this host needs a bump and `get_mcp_status` would have flagged it. Worth a check.

6. **Scanner ecosystem.** Only one scanner registered (`claude-code`). The findings/files surface is built for richer scanner traffic. Is that a config gap on this host or an ecosystem gap project-wide?

---

## Appendix — Scratch artifacts left in the DB

Cleanup status: scratch issues are closed, scratch finding is dismissed. Observations recorded as part of the walkthrough either dismissed or promoted then closed.

- `filigree-fdd3a35dda` — bug, closed, label `mcp-review-scratch-c`
- `filigree-468e183c3e` — task, closed, label `mcp-review-scratch-c`
- `filigree-3609bf269e` — bug (promoted from observation), closed
- `filigree-sf-409dcab9fd` — finding, dismissed
- `filigree-obs-d9d9c12538` — observation, promoted then closed
- `filigree-obs-796b59b844` — observation, dismissed (duplicate of `filigree-obs-e8d052394e` filed by another agent)

If the scratch label `mcp-review-scratch-c` is a problem, all six artifacts are filterable by it (issue label) or by actor `review-c` (events). I did not find a one-call "purge by label" — full deletion isn't part of the MCP surface, presumably by design (audit log immutability).
