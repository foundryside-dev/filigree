# Filigree MCP — Senior-User Friction Review (pass B)

**Date:** 2026-05-06
**Reviewer:** Claude Opus 4.7 (1M), instance "reviewer-b"
**Branch:** `2.0-project-management-extension`
**Method:** Live drive of the `mcp__filigree__*` surface against the working repo's `.filigree/` DB. All findings cite tool calls actually made during the review (events visible in `get_changes since=2026-05-05T23:44:00Z`). Scratch issues created and closed; one promoted-observation issue (`filigree-efe52b6db8`) and one synthetic file record (`filigree-f-91d4863eb4`) intentionally left in place — see Findings 12 and 16.

> Companion review running in parallel: `2026-05-06-mcp-senior-user-review.md` (other reviewer agent). Independent findings; some overlap expected.

---

## 1. Executive summary

The 2.0 surface is mostly there — `get_summary`, `start_work`, `BatchResponse`, the `INVALID_TRANSITION` error envelope, and the agent scratchpad (observations) are genuinely good agent-shaped tooling. The two things that hurt are (a) the `id` → `issue_id` rename is incomplete in roughly six tools (most painfully in `promote_observation` and the entire `get_plan` tree), and (b) the multi-agent handoff workflow has no atomic primitive — once an agent `release_claim`s a `wip` issue, no successor can pick it back up via `start_work` or `claim_issue`, only via a non-atomic `update_issue(assignee=...)`. Lower-severity friction clusters around inconsistent mutation envelopes (five different shapes) and missing scaffolding for stale-work detection (no sort on `list_issues`, assigned-but-abandoned issues keep resurfacing in `get_ready`).

---

## 2. Per-workflow walkthrough

### Workflow A — Cold start

**What I did:**
`get_summary` → `get_ready` → `get_stats` → `get_critical_path` → `get_issue(filigree-1c7b2776a5, include_transitions=true)` → `get_comments` / `get_issue_events` / `get_issue_files` → `create_issue` (scratch) → `start_work` → `add_comment` → `close_issue`.

**Worked:**
- `get_summary` is a one-shot orientation tool. It already calls out stale observations at the bottom, which steered me to Workflow B before I'd even asked. Great default.
- `get_issue(include_transitions=true)` returns `valid_transitions[]` with `missing_fields` and `ready: bool` per transition — I knew immediately that "fixing" required `root_cause` without a second call. This is the surface at its best.
- `start_work` is exactly the atomic primitive an agent wants. One call, claimed and transitioned, returns the new shape.

**Hurt:**
- `get_ready`'s description says "open, no blockers" but the returned items include `confirmed`, `triage`, `planning` — i.e. the *open category*, not the literal `open` status. Easy to misread as "filter is broken" when first encountered.
- `get_critical_path` returned `{path: [], length: 0}` because there are no dependencies in the project. Empty results with no explanatory note force the agent to figure out *why* it's empty.
- The P1 "release_claim TOCTOU" issue surfaces in `get_ready` despite being claimed (`assignee="claude-debug"`) and having a "Fix staged" comment from 2026-04-18. An agent who picks the highest-priority ready item gets a stale, half-done piece of work — exactly the failure mode `start_work` was supposed to prevent. See Finding 12.
- `add_comment` returns `{status: "ok", comment_id}` while every other mutation returns the full record. Inconsistent envelope (Finding 7).

### Workflow B — Triage and grooming

**What I did:**
`list_observations` → `get_label_taxonomy` → `list_labels` → `get_blocked` → `list_issues(label="age:recent", status_category="open")` → `list_issues(label="P1")` → `list_issues(label_prefix="cluster:", status_category="open")` → `batch_add_label` (3 issues) → `batch_update(priority=2)` → `batch_update(status=closed)` (mixed-validity, includes a bad ID) → `batch_dismiss_observations(response_detail="full")` → `promote_observation` → `get_metrics`.

**Worked:**
- `get_label_taxonomy` is excellent — auto/virtual/manual_suggested/bare_labels with reserved namespaces. An agent can derive the project's vocabulary without reading docs.
- `batch_add_label`, `batch_update`, and `batch_dismiss_observations` all conform to the `BatchResponse[T]` envelope cleanly. Mixed-validity input (3 valid + 1 bogus ID) partitioned correctly into `succeeded[]`/`failed[{id, error, code: NOT_FOUND}]`.
- `response_detail="full"` on `batch_dismiss_observations` returned the pre-dismissal snapshots — exactly as advertised, useful for audit.
- Label-prefix filter (`label_prefix="cluster:"`) works.
- Priority filter `--label=P1` returns even closed issues by default; `status_category="open"` is the natural counter.

**Hurt:**
- **No sort order on `list_issues`.** To find stale issues I'd have to either eyeball or burst-call. The age virtual-labels (`age:fresh`, `age:recent`, `age:aging`, `age:stale`, `age:ancient`) are buckets, not a sort. In this DB all 246 issues fall into `fresh`/`recent`; both `aging` and `stale` are 0. So "stale" means nothing operationally.
- **Duplicate priority vocabulary.** `list_labels` shows `P1: 1, P2: 9, P3: 6` text labels alongside the numeric `priority` field. Same concept, two namespaces, no taxonomy guard.
- **`_bare` namespace name.** `list_labels` returns `namespace: "_bare"` for non-prefixed labels. Internal sentinel leaking into the public response.
- **`promote_observation`** returned `{issue: {id: ..., labels: []}}`. Two breakage modes in one response: wraps in `issue:` (every other mutation returns the issue dict directly), uses `id` instead of `issue_id`, and the snapshot is *stale* — the new issue actually has the `from-observation` label (verified by an immediate `get_issue`), but the response says `labels: []`. See Finding 1.
- **No `batch_promote_observations`.** I had 15 stale observations and could only triage them one at a time.
- **`list_labels` truncates by namespace at top=10 by default.** The `_bare` namespace alone has 25+ labels in this project. Easy to miss tail vocabulary.
- **`get_critical_path`** can't help when there are no dependencies, and there are no dependencies in this project. So the recommended grooming flow ("identify the critical path and unblock it") has nothing to identify on a flat backlog.

### Workflow C — Multi-agent coordination

**What I did:**
`create_issue` (scratch) → `start_work(agent-alpha)` → `start_work(agent-bravo)` (loser) → `claim_issue(agent-charlie)` (loser) → `add_comment` → `release_claim` → `start_work(agent-bravo)` (refused) → `claim_issue(agent-bravo)` (refused) → `update_issue(assignee=agent-bravo)` (worked) → `get_changes(since=...)`.

**Worked:**
- The CONFLICT error from `start_work` and `claim_issue` is actionable: `{error: "Cannot claim ...: already assigned to 'agent-alpha'", code: "CONFLICT"}`. An agent can branch on `code === "CONFLICT"`.
- The INVALID_TRANSITION error from `start_work` on a `wip` issue is **the platinum standard** for error UX:
  ```json
  {"error": "...", "code": "INVALID_TRANSITION",
   "valid_transitions": [{"to": "closed", "category": "done", "ready": true}],
   "hint": "Use get_valid_transitions to see allowed state changes"}
  ```
  Embedded next-step data plus a tool hint. Other tools should adopt this shape.
- `get_changes(since)` returned a clean event list with `actor`, `event_type`, `old_value/new_value`, `issue_title`. Resumption-friendly.

**Hurt:**
- **The handoff is broken.** After `agent-alpha` `release_claim`s, the issue is `status=in_progress, assignee=""`. `agent-bravo` running `start_work` gets `INVALID_TRANSITION`. `agent-bravo` running `claim_issue` gets `CONFLICT` ("status is 'in_progress', expected open-category state"). The only path that works is `update_issue(assignee=agent-bravo)`, which is a plain UPDATE without compare-and-swap — it has the same TOCTOU problem `claim_issue` exists to avoid. See Finding 2.
- **Same condition, two error codes.** `claim_issue` returns `CONFLICT` and `start_work` returns `INVALID_TRANSITION` for the identical underlying state ("issue not in open-category"). An agent can't reliably branch on `code`. See Finding 3.
- **`is_ready: false` on a released wip issue.** So `start_next_work` won't pick it up either — discoverability is also broken; the next agent has to know the ID out-of-band.
- **No cursor on `get_changes`.** I have to track the latest event's `created_at` myself for the next poll. A `next_since` echo in the response would be friendlier.
- **No "is the assignee still alive?" affordance.** A claim made by a long-dead agent (e.g. `claude-debug` on `filigree-1c7b2776a5`, now ~20 days old) blocks reclaim, with no built-in TTL or liveness signal.

### Workflow D — Scan / findings / files

**What I did:**
`list_scanners` → `list_findings` → `list_files(sort=updated_at, direction=desc)` → `preview_scan` → `report_finding` (ad-hoc) → `get_file` → `list_findings(file_id=...)` → `get_file_timeline` → `get_finding` → `dismiss_finding`.

**Worked:**
- `list_files` exposes the cleanest `ListResponse` envelope I saw: `{items, has_more, next_offset}`.
- `report_finding` is genuinely zero-ceremony — auto-registered the file, returned `{status: "created", finding_id, file_created: true}`. This is what every "I noticed something" agent flow wants.
- `preview_scan` returned a usable command + `valid: true` flag. Easy to dry-run.
- `dismiss_finding` returned the full updated finding with `metadata.dismiss_reason` populated.

**Hurt:**
- **`get_file`, `get_finding` use `id` not `file_id`/`finding_id`.** `get_file` even wraps the record in `{file: {id: ..., ...}, associations: [...], ...}` — same wrap-and-rename pattern as `promote_observation`. Tools renamed for 2.0 only renamed the *input arg*, not the output field.
- **`get_file_timeline` is sparse.** It only shows `association_created` / `file_metadata_update` / `finding`-type events. Issues that were closed against this file (events on the issues themselves) don't merge in. The file→work cross-cut is weaker than I expected.
- **Four ID prefix conventions** to remember: `filigree-{hash}` (issue), `filigree-obs-{hash}` (observation), `filigree-sf-{hash}` (scan finding), `filigree-f-{hash}` (file). No central reference; I had to discover each one by calling tools.
- **`report_finding` returns `finding_id` at top level** (correct) but its `file_id` is *new* (it auto-created a file record I can't easily clean up — there's no `delete_file_record` primitive).

### Workflow E — Planning

**What I did:**
`create_plan` (1 milestone, 2 phases, 4 steps with mixed int and "p.s" deps) → `get_plan` → `create_issue(type=step, parent_issue_id=..., deps=[...])` (mid-flight) → `remove_dependency` → `add_dependency` (retarget).

**Worked:**
- `create_plan` works in one call; index-based deps with mixed int (same-phase) and "phase.step" string (cross-phase) resolve correctly to UUID `blocked_by` lists.
- Mid-flight extension via `create_issue(type=step, parent_issue_id=..., deps=[...])` slots in cleanly.
- `remove_dependency` + `add_dependency` make retargeting easy.

**Hurt:**
- **Every record in `create_plan`/`get_plan` uses `id`, not `issue_id`.** `milestone.id`, `phases[].phase.id`, `phases[].steps[].id`, `children: [<id>, ...]`. Inconsistent with `create_issue` (which returns `issue_id`).
- **Plan tool doesn't propagate parent labels** (or accept a `labels` parameter at all). I couldn't mark the entire scratch plan tree with `mcp-review-scratch` in one call; cleanup required collecting eight IDs by hand from the response.
- **`add_dependency`/`remove_dependency` accept `from_issue_id`/`to_issue_id`** as input but **return `from_id`/`to_id`** in the response. The rename is half-applied within a single tool.
- **Index-based deps are fragile.** They're resolved at create time, so the persisted graph is fine, but if I'm composing a plan programmatically and shuffle steps before submitting, a dep silently retargets. Lower-severity but a footgun.

### Workflow F — Recovery and edge cases

**What I did:**
`update_issue(closed→in_progress)` → `claim_issue` on a closed issue → `batch_close(ids=[])` → `add_label(issue_id="does-not-exist")` → `update_issue` (rename) → `add_label` → `undo_last`.

**Worked:**
- Closed→in_progress via `update_issue` returned the canonical INVALID_TRANSITION envelope (with `valid_transitions: []` and a hint).
- `add_label` on a missing ID returned `{error: "Issue not found: does-not-exist", code: "NOT_FOUND"}` — clean.
- Empty `batch_close(ids=[])` returned `{succeeded: [], failed: []}` — matches the spec, no error on trivially valid input.
- `undo_last` did the right thing: I changed the title, then added a label, then `undo_last` undid the *title change* (skipping past the non-undoable label add) and surfaced `{undone: true, event_type: "title_changed", event_id: 1891, issue: {...}}`. Reaching past non-undoable events to find the next undoable one is the right semantic.

**Hurt:**
- **`update_issue(closed→in_progress)` returns `valid_transitions: []`** with no hint about `reopen_issue`. The agent is left looking for a non-existent transition with no pointer to the actual recovery path. See Finding 5.
- **Same condition, two codes (again).** `update_issue(closed→...)` → `INVALID_TRANSITION`. `claim_issue` on closed → `CONFLICT`. Both for "issue is closed."
- **`undo_last` returns `{issue: {id: ..., ...}}`** — third tool with the wrap-and-`id` envelope (after `promote_observation` and `get_file`).
- **Schema-mismatch path is untestable from inside the surface** — would require uninstalling/downgrading the local tool. The CLAUDE.md guidance says "surface the message and don't retry," but agents can't validate that path themselves.

---

## 3. Findings

Severity: P0 = blocks workflows; P1 = seriously degrades a real flow; P2 = forces workarounds or extra calls; P3 = paper cut.

### P1

**1. `promote_observation` envelope is the worst-case 2.0-rename hole.**
*Workflow B.* The response is `{issue: {id: "filigree-efe52b6db8", labels: [], ...}}`. Three problems compound: (a) wraps in `issue:` while every other mutation returns the issue dict directly; (b) inner key is `id` not `issue_id`, breaking the 2.0 contract; (c) the snapshot is *stale* — `labels: []` is wrong (an immediate follow-up `get_issue` shows `labels: ["from-observation"]` was correctly added, the response just wasn't re-read post-side-effect). An agent that binds to the response payload will both miss the label and look up the wrong key. Resolution: align with `create_issue` shape — return a flat `PublicIssue` with `issue_id`, re-read after the label is attached.

**2. Multi-agent handoff has no atomic primitive.**
*Workflow C.* `release_claim` leaves the issue in `wip` with `assignee=""`. From there, `start_work` returns `INVALID_TRANSITION` (status not open-category) and `claim_issue` returns `CONFLICT` (same reason, different code). The only path that works is `update_issue(assignee=...)`, which is a plain UPDATE without compare-and-swap — *exactly* the TOCTOU class that the open P1 bug `filigree-1c7b2776a5` documents. Resolution: add a `take_over` / `reclaim` tool that does atomic CAS on `assignee` regardless of status category, OR have `release_claim` revert status to its open-category origin so the standard `start_work` flow resumes.

**3. Same condition surfaces under two different error codes.**
*Workflows C & F.* "Issue not in open-category" returns `INVALID_TRANSITION` from `start_work` and `update_issue`, but `CONFLICT` from `claim_issue`. An agent that branches on `code` to decide whether to retry vs abort can't write one rule. Resolution: pick one — `INVALID_TRANSITION` is more informative (it lets you embed `valid_transitions[]`).

### P2

**4. `id` vs `issue_id` rename is incomplete across ~6 tools.**
*Workflows B, D, E, F.* Affected: `promote_observation` (Finding 1), `get_finding` (top-level `id`), `get_file` (inner `file.id`), `undo_last` (`issue.id`), `create_plan` / `get_plan` (every milestone/phase/step uses `id` and `children: [<id>, ...]`), `add_dependency` / `remove_dependency` (returns `from_id`/`to_id` despite accepting `from_issue_id`/`to_issue_id`). The 2.0 design's §1a renamed `BatchFailure.item_id` → `BatchFailure.id` for wire compatibility, so there's an *intentional* mixing of `id` and `issue_id` in the codebase — that ambiguity probably enabled the rename to stop short. Resolution: audit every tool and pick a rule. Either "all primary keys named `id`" (drop the rename) or "all primary keys named `<entity>_id` everywhere they appear" (finish the rename). The current half-state forces agents to remember which-one-where.

**5. Closed→update returns `valid_transitions: []` with no `reopen_issue` hint.**
*Workflow F.* The error is technically correct (no transition exists from `closed` in the type's workflow), but the actual recovery primitive (`reopen_issue`) lives outside the transition graph. Resolution: when `valid_transitions: []` and the issue is closed, append `"hint": "use reopen_issue to return to initial state"`.

**6. No `sort_by` on `list_issues`.**
*Workflow B.* Triage workflows need "what's been ignored longest?" The age virtual-labels (`age:stale`, `age:ancient`) are 0-count in this DB, so the bucket vocabulary effectively doesn't work for "find stale." Resolution: add `sort_by={created_at, updated_at, priority}` + `direction={asc, desc}` (parity with `list_files`).

**7. Mutation responses come in five different shapes.**
*Workflows A, C, F.* Sample shapes encountered:
- `create_issue` / `update_issue` / `close_issue`: full `PublicIssue` dict (with `issue_id`).
- `update_issue` extra: adds `changed_fields: [...]` only this tool.
- `add_comment`: `{status: "ok", comment_id: <int>}`.
- `add_label`: `{status: "added", issue_id, label}`.
- `add_dependency` / `remove_dependency`: `{status: <verb>, from_id, to_id}`.
- `release_claim` / `claim_issue`: full issue dict.
- `promote_observation`: `{issue: {id: ..., ...}}`.
- `undo_last`: `{undone: true, event_type, event_id, issue: {id: ..., ...}}`.
- `report_finding`: `{status: "created", findings_created, file_created, finding_id}`.
- `dismiss_finding` / `update_finding`: full `Finding` dict (with `id`).

Resolution: normalize. Suggestion — every mutation returns either the full record (rename inner keys to `issue_id`/`finding_id`/etc) or the standard `BatchResponse` envelope when it's a multi-target op. Drop the `{status: "ok"}` ack-only shape; an agent never just wants confirmation, they want the post-mutation state.

**8. Plan tool can't tag the whole tree.**
*Workflow E.* `create_plan` accepts no `labels` parameter, doesn't propagate the milestone's labels to phases/steps, and offers no follow-up tool to label the whole subtree. For "this is a scratch plan" or "this is for v1.4.0" annotation, the agent must collect every ID from the response and call `batch_add_label`. Resolution: accept a `labels: [...]` param at the milestone level that propagates, or add a `label_subtree(parent_id, label)` tool.

### P3

**9. `list_labels` truncates by namespace at `top=10`.**
*Workflow B.* The `_bare` namespace has 25+ labels here; default response shows 10. Easy to think "that's the full set" without reading the schema. Resolution: default `top=0` (unlimited) for the bare namespace, or surface a `truncated: true` per-group flag.

**10. `_bare` namespace name is an internal sentinel.**
*Workflow B.* `list_labels` returns `namespace: "_bare"` for non-prefixed labels. Underscore prefix is a Python-ish convention, not a documented public name. Resolution: rename to `"unnamespaced"` or `null` in the public envelope.

**11. `get_changes` lacks a resumption cursor.**
*Workflow C.* No `next_since` or `last_event_at` echoed in the response; the agent has to track the latest `created_at` themselves. Small, but every other paginated tool gives back `next_offset`. Resolution: add `next_since` (the most-recent event's timestamp) to the envelope.

**12. Stale claimed-but-unfinished issues resurface in `get_ready` indefinitely.**
*Workflow A.* `filigree-1c7b2776a5` is P1, `assignee="claude-debug"` (a 19-day-old session), has a "Fix staged (not yet committed)" comment from 2026-04-18, and still appears in `get_summary`'s top "READY TO WORK" list at every cold start. New agents either re-pick it (and discover the half-done state) or skip past it (eroding trust in the priority ordering). Resolution: either (a) `get_ready` excludes assigned issues by default with a `include_assigned=true` opt-in, or (b) a "needs reclaim" virtual label distinguishes assigned-stale from genuinely-ready.

**13. `get_ready` description says "open" but means "open category."**
*Workflow A.* Items returned include statuses `confirmed`, `triage`, `planning`, `open`. Descriptions should say "open-category" to match the 2.0 vocabulary in `status_category`.

**14. Duplicate priority vocabulary (`P1`/`P2`/`P3` text labels alongside numeric `priority`).**
*Workflow B.* Same concept stored twice. `get_label_taxonomy` doesn't flag this as discouraged. Resolution: either auto-derive a `priority:N` virtual label from the numeric field (mirroring `age:fresh`), or add a taxonomy warning when manual `P\d` labels are used.

**15. `promote_observation` response missing `from-observation` label is a snapshot timing bug.**
*Workflow B.* The label is correctly applied (verified). The response just wasn't re-read after the label add. Tactical fix; can be rolled into Finding 1.

**16. No `delete_file_record` primitive.**
*Workflow D.* My synthetic `report_finding` against a fake file path created `filigree-f-91d4863eb4` (`src/filigree/mcp_tools/observations.py`). I dismissed the finding but the file record is permanent. For agent-driven scratch flows this leaks. Lower priority because file records are cheap, but unaddressable.

**17. Four ID prefix conventions with no central reference.**
*Workflows B, D.* `filigree-{hash}` (issue), `filigree-obs-{hash}` (observation), `filigree-sf-{hash}` (scan finding), `filigree-f-{hash}` (file). Resolution: add a `_schema` MCP tool (mirroring `GET /api/files/_schema`) that lists ID prefixes and their entities.

**18. `get_file_timeline` doesn't merge in events from associated issues.**
*Workflow D.* The "file → activity" cross-cut shows only file-local events. To see all the issue work that touched a file, I'd have to fan out via `get_issue_events` per associated issue. Resolution: add an `include_issue_events=true` opt-in.

**19. `batch_promote_observations` doesn't exist.**
*Workflow B.* I had 15 stale observations and could only promote one at a time. `batch_dismiss_observations` exists; the symmetric tool doesn't. Resolution: add it.

**20. `get_critical_path` returns `{path: [], length: 0}` with no explanatory note.**
*Workflow A.* When the dependency graph is flat (this DB has 0 dependencies), the empty result is confusable with "tool broken" or "no permission." Resolution: include `note: "no dependencies in project"` (or similar) when the graph is empty.

---

## 4. What works well

- **`get_summary`** — single call, full orientation including stale-observations callout. Proactively surfaces grooming work without being asked.
- **`get_issue(include_transitions=true)`** — `valid_transitions[]` with `missing_fields` and `ready: bool` is the platinum cold-read.
- **`start_work` happy path** — atomic claim+transition, single call, returns the new state. The 2.0 design's headline win delivers.
- **The `INVALID_TRANSITION` error envelope** (`{error, code, valid_transitions[], hint}`) is the model every error response should follow.
- **`BatchResponse[T]`** with `succeeded[]`/`failed[{id, error, code}]` partitioning is clean and predictable. Mixed-validity batches handled gracefully.
- **`response_detail="full"` on `batch_dismiss_observations`** returns pre-dismissal snapshots — exactly the audit-friendly behavior promised.
- **`report_finding`** — zero-ceremony "I noticed something" tool that auto-registers files. Removes the most common per-call ceremony.
- **`get_label_taxonomy`** — auto/virtual/manual_suggested/bare with reserved-namespace markers. An agent can derive vocabulary without reading docs.
- **`undo_last`** does the right thing — including reaching past non-undoable events to find the next reversible one.
- **`batch_close` routes per-type done-status correctly** — task→closed, step/phase/milestone→completed. Cross-type batches "just work."

---

## 5. Open questions for the maintainer

1. **What's the intended handoff flow?** If `release_claim` keeps the issue in `wip`, is the canonical successor flow `update_issue(assignee=...)` (non-atomic)? Or should `release_claim` revert status and `start_work` resume? Or should there be a new `take_over` primitive? See Finding 2.
2. **How far should the `id` → `issue_id` rename go?** The 2.0 design's `BatchFailure.id` decision keeps `id` legitimate in *some* contexts. Where exactly is the line between "wire field stays `id`" and "must be `<entity>_id`"? See Finding 4.
3. **Is `get_ready` showing assigned issues intentional?** If yes, what's the affordance for distinguishing "ready and unclaimed" from "ready but stale-claimed"? See Finding 12.
4. **Should `get_summary`'s "READY TO WORK" list match `get_ready`?** Right now both include `filigree-1c7b2776a5` (assigned). If yes, see Finding 12. If no, the two need to diverge.
5. **`P1`/`P2`/`P3` text labels — accident or intentional?** `get_label_taxonomy` doesn't surface them as discouraged. If the team uses them as a secondary priority signal (perhaps for changelog grouping?) the taxonomy should explain. If accidental, a one-time cleanup + a guard. See Finding 14.
6. **What's the scratch-data lifecycle?** No `delete_issue` (intentional — closed is the analogue). No `delete_file_record`, no `delete_observation` (only `dismiss`, which logs). Is the design "everything is immutable, including agent test data"? If yes, surface that explicitly so reviewers don't try to clean up.
7. **Schema-mismatch path validation.** The CLAUDE.md guidance for SCHEMA_MISMATCH is excellent but agents can't provoke or test the path. Worth a dedicated `__simulate_schema_mismatch` tool gated behind a debug flag, or a documented test fixture?

---

## Appendix: Cleanup state

Closed and accounted for:
- All `[scratch]` test issues (7 created by reviewer-b: `2556283584`, `467b05ad8d`, `2650559d71`, `9fd039f5ce`, `76f7c35bc6`, `e175b9625f`, plus the 8-item plan tree rooted at `b81190e3fb`).
- One synthetic finding (`filigree-sf-826632f496`) dismissed.

Left in place intentionally:
- `filigree-efe52b6db8` — promoted observation about MCP `start_next_work` error classification, real concern (Finding context).
- `filigree-f-91d4863eb4` — auto-registered file record from `report_finding`, no removal primitive (Finding 16).

Other reviewer agent's scratch issues (actor `mcp-review`: `179f3c8e65`, `8e7ee8bb38`, `3023a4101a`) left for that agent to clean up.
