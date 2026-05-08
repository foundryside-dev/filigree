# Filigree MCP Senior-User Friction Review

**Date:** 2026-05-06
**Reviewer:** Codex, acting as a senior agent user
**Branch:** `2.0-project-management-extension`
**Method:** Live use of `mcp__filigree__*` tools against this repo's working `.filigree/` database, with scratch issues labeled `mcp-review-scratch` / `cluster:mcp-review` and cleaned up via MCP where possible. I read `CLAUDE.md`, `AGENTS.md`, the 2.0 design deltas banner, and `src/filigree/types/api.py` first, then used source only to confirm the schema-mismatch path that could not be safely triggered against the live DB.

Scores below use 5 = agent-friendly, 1 = painful.

## 1. Executive summary

The MCP surface is close to being genuinely agent-native, especially around `get_summary`, `start_next_work`, transition hints, batch envelopes, and direct finding/observation capture. The most important friction is that "ready" still includes assigned work, so a cold agent can select a supposedly ready issue and immediately hit `CONFLICT`; the second major problem is that the 2.0 `issue_id` rename is incomplete in planning, observation promotion, critical-path, undo, and dependency responses. The remaining pain is mostly workflow composition: no MCP `session_context` equivalent by name, no create-plan-from-file path, no bulk remove labels, scan triggering is risky/opaque, and catch-up/change feeds are too noisy for multi-agent sessions.

## 2. Per-workflow walkthrough

### Workflow A - Cold start

**Scorecard:** Discoverability 3, naming 3, composability 4, error UX 4, response shapes 3, defaults 2, cognitive load 3, gaps 3, surprises 2.

**What I did:** `get_summary` -> `get_ready` -> `get_stats` -> `list_issues(status_category="wip")` -> `get_critical_path` -> `get_issue(..., include_files=true, include_transitions=true)` -> `get_comments` / `get_issue_events` / `get_issue_files` -> scratch `create_issue` -> `start_next_work` -> `add_comment` -> `batch_close`.

**What worked:** `get_summary` is a good one-call orientation surface. It included ready work, in-progress work, recent activity, and a stale-observations nudge: `STALE OBSERVATIONS: 15 observation(s) older than 48 hours`. `get_issue(include_transitions=true)` also did the right thing by folding workflow hints into the issue detail. For `filigree-1c7b2776a5`, it showed `missing_fields: ["root_cause"]` and `ready: false` for `fixing`, so an agent knows why the obvious next transition is blocked.

**What hurt:** `get_ready` returned `filigree-1c7b2776a5` as the top P1 item:

```json
mcp__filigree__.get_ready({}) -> {
  "items": [
    {"issue_id": "filigree-1c7b2776a5", "status": "confirmed", "priority": 1, "type": "bug"}
  ],
  "has_more": false
}
```

But `get_issue` for that same issue showed `"assignee": "claude-debug"`, and trying to start it failed:

```json
mcp__filigree__.start_work({
  "issue_id": "filigree-1c7b2776a5",
  "assignee": "mcp-review-agent"
}) -> {
  "error": "Cannot claim filigree-1c7b2776a5: already assigned to 'claude-debug'",
  "code": "CONFLICT"
}
```

That makes the cold-start path actively misleading: "highest-leverage ready work" is not actually claimable. The happy-path composed operation did work on a scratch task:

```json
mcp__filigree__.start_next_work({
  "assignee": "mcp-review-agent",
  "priority_min": 0,
  "priority_max": 0,
  "type": "task"
}) -> {
  "issue_id": "filigree-743d3b9abf",
  "status": "in_progress",
  "assignee": "mcp-review-agent",
  "is_ready": false
}
```

Closing with audit trail worked through `batch_close(response_detail="full")`, which set `fields.close_reason` and returned full post-close records. The single-issue `close_issue` call was blocked by the execution guard in this environment, so I did not treat that cancellation as a Filigree MCP response.

### Workflow B - Triage and grooming

**Scorecard:** Discoverability 3, naming 2, composability 3, error UX 4, response shapes 3, defaults 3, cognitive load 3, gaps 2, surprises 3.

**What I did:** `list_observations`, `get_label_taxonomy`, `list_labels`, `list_issues(label="cluster:mcp-review")`, `get_blocked`, `get_critical_path`, `batch_update(response_detail="full")`, `batch_add_label(response_detail="full")`, `remove_label`, `observe`, `dismiss_observation`, `promote_observation`, `remove_dependency`.

**What worked:** `get_label_taxonomy` is genuinely useful. It explains auto labels, virtual labels, suggested manual namespaces, and bare labels without making the agent read docs. `batch_update(..., response_detail="full")` and `batch_add_label(..., response_detail="full")` returned full issue records and `failed: []`, which is exactly what an agent wants after changing priorities or labels in bulk:

```json
mcp__filigree__.batch_update({
  "issue_ids": ["filigree-179f3c8e65", "filigree-8e7ee8bb38"],
  "priority": 1,
  "response_detail": "full"
}) -> {
  "succeeded": [
    {"issue_id": "filigree-179f3c8e65", "priority": 1, "...": "..."},
    {"issue_id": "filigree-8e7ee8bb38", "priority": 1, "...": "..."}
  ],
  "failed": []
}
```

**What hurt:** There is `batch_add_label`, but no `batch_remove_label`; removing the review label I had just added required two separate `remove_label` calls. `get_blocked` used `issue_id`, but `get_critical_path` immediately switched back to `id`:

```json
mcp__filigree__.get_critical_path({}) -> {
  "path": [
    {"id": "filigree-179f3c8e65", "title": "MCP review scratch: dependency blocker"},
    {"id": "filigree-3023a4101a", "title": "MCP review scratch: blocked downstream"}
  ],
  "length": 2
}
```

Dependency mutation has the same half-renamed shape:

```json
mcp__filigree__.remove_dependency({
  "from_issue_id": "filigree-3023a4101a",
  "to_issue_id": "filigree-179f3c8e65"
}) -> {
  "status": "removed",
  "from_id": "filigree-3023a4101a",
  "to_id": "filigree-179f3c8e65"
}
```

Observation promotion had the sharpest response-shape surprise. The tool description says it creates an issue with the `from-observation` label, but the immediate response was nested and used `id`:

```json
mcp__filigree__.promote_observation({
  "observation_id": "filigree-obs-74884efc7e",
  "type": "task"
}) -> {
  "issue": {
    "id": "filigree-fe2e11738c",
    "labels": [],
    "fields": {"source_observation_id": "filigree-obs-74884efc7e"}
  }
}
```

A later `list_issues(label="from-observation")` showed the label was actually present, so the response is both stale and shaped differently from `create_issue`.

### Workflow C - Multi-agent coordination

**Scorecard:** Discoverability 4, naming 3, composability 3, error UX 4, response shapes 3, defaults 2, cognitive load 3, gaps 3, surprises 2.

**What I did:** Scratch `create_issue`, simultaneous `claim_issue` calls for `agent-alpha` and `agent-bravo`, `add_comment` handoff, `release_claim`, and `get_changes(since=...)`.

**What worked:** The loser in a claim race gets an actionable error:

```json
mcp__filigree__.claim_issue({
  "issue_id": "filigree-86f669f82b",
  "assignee": "agent-bravo"
}) -> {
  "error": "Cannot claim filigree-86f669f82b: already assigned to 'agent-alpha'",
  "code": "CONFLICT"
}
```

`add_comment` is a lightweight handoff channel, and `release_claim` cleanly cleared the assignee:

```json
mcp__filigree__.release_claim({"issue_id": "filigree-86f669f82b"}) -> {
  "issue_id": "filigree-86f669f82b",
  "status": "open",
  "assignee": "",
  "is_ready": true
}
```

`get_changes(since=...)` is useful for resumption because it includes `event_type`, `actor`, `old_value`, `new_value`, timestamp, and `issue_title`.

**What hurt:** A claimed issue still reports `"is_ready": true` after `claim_issue`, and the broader `get_ready` behavior already showed that assigned issues can appear as ready. That undermines the core coordination contract: "ready" should not mean "will conflict if you touch it." `get_changes` also became noisy under real multi-agent conditions: my catch-up call included many concurrent `reviewer-b` scratch events unrelated to my workflow. There is no actor filter, label filter, or `next_since` cursor, so an agent has to hold local state and manually separate "my workflow" from "ambient project churn."

### Workflow D - Scan, findings, and files

**Scorecard:** Discoverability 3, naming 2, composability 3, error UX 3, response shapes 3, defaults 2, cognitive load 3, gaps 3, surprises 2.

**What I did:** `list_scanners`, `preview_scan`, attempted `trigger_scan`, `report_finding`, `get_finding`, `list_findings`, `get_file`, `get_file_timeline`, `batch_update_findings(response_detail="full")`, `promote_finding`, and `dismiss_finding`.

**What worked:** `report_finding` is excellent for agent scratch work. One call auto-registered the file if needed and returned a direct finding ID:

```json
mcp__filigree__.report_finding({
  "file_path": "src/filigree/mcp_tools/issues.py",
  "rule_id": "mcp-review-scratch-direct-report",
  "severity": "low"
}) -> {
  "status": "created",
  "findings_created": 1,
  "file_created": false,
  "finding_id": "filigree-sf-648fe06426"
}
```

`batch_update_findings(response_detail="full")` returned full updated finding records and `failed: []`, and `dismiss_finding` preserved a `metadata.dismiss_reason`.

**What hurt:** `list_scanners` reported only one scanner, `claude-code`. `preview_scan` showed the scan would run:

```json
{
  "command_string": "python scripts/claude_bug_hunt.py --root /home/john/filigree --file src/filigree/types/api.py --max-files 1 --api-url http://localhost:8377 --scan-run-id preview-dry-run",
  "valid": true
}
```

The live `trigger_scan` call was rejected by the execution guard because it would spawn an external scanner workflow that may transmit repository contents. That is not a Filigree error, but it is still real agent friction: `list_scanners` does not expose risk level, external process behavior, expected data egress, or whether a safe local/no-op scanner exists.

The finding/file ID vocabulary also remains inconsistent. `get_finding` returns top-level `"id"` rather than `finding_id`; `get_file` wraps the primary record in `"file": {"id": ...}`; `get_file_timeline` returns timeline item `"id"` and `"source_id"`. `promote_finding` was also surprising: it promotes a finding to an observation, not to an issue:

```json
mcp__filigree__.promote_finding({"finding_id": "filigree-sf-648fe06426"}) -> {
  "id": "filigree-obs-a385f5ade4",
  "summary": "[agent] Synthetic low-severity finding ...",
  "file_id": "filigree-f-73fde6e29f"
}
```

That may be the intended staged lifecycle, but it conflicts with the guidance phrase "promote finding" and with an agent's expectation that a promoted scan finding becomes tracked work.

### Workflow E - Planning

**Scorecard:** Discoverability 2, naming 1, composability 2, error UX 3, response shapes 2, defaults 3, cognitive load 2, gaps 2, surprises 2.

**What I did:** `create_plan` with a nested milestone/phase/step object, `get_plan`, `create_issue(type="step", parent_issue_id=...)` to add a step mid-flight, then `remove_dependency` and `add_dependency` to retarget dependencies.

**What worked:** The nested `create_plan` call did create a milestone, phases, steps, and dependencies in one round trip. Same-phase integer deps and cross-phase `"0.1"` deps resolved into the persisted graph, and `get_plan` recomputed totals and readiness after I added a mid-flight step.

**What hurt:** The requested "from a JSON file" workflow is not available in MCP. The tool schema accepts nested arguments:

```json
mcp__filigree__.create_plan({
  "milestone": {"title": "MCP review scratch plan live"},
  "phases": [{"title": "...", "steps": [{"title": "..."}]}]
})
```

There is no `file_path`, `plan_json`, import, or validation-only mode for an agent that has been handed a plan file. The response also uses `id` everywhere:

```json
{
  "milestone": {"id": "filigree-320ac41fc8", "children": ["filigree-bbb9105982", "..."]},
  "phases": [{"phase": {"id": "filigree-bbb9105982"}, "steps": [{"id": "filigree-3d1c499afc"}]}]
}
```

Mid-flight edits required generic issue/dependency tools. Adding a step was `create_issue(type="step", parent_issue_id=...)`; retargeting required three dependency calls. There is no `add_plan_step`, `move_plan_step`, `retarget_step_dependency`, `label_plan_tree`, or plan diff/validate tool, so the agent has to keep the plan topology in its head.

### Workflow F - Recovery and edge cases

**Scorecard:** Discoverability 3, naming 2, composability 3, error UX 4, response shapes 2, defaults 4, cognitive load 3, gaps 3, surprises 3.

**What I did:** Invalid status transition, claim a closed issue, empty batch input, missing issue in a batch, `undo_last`, and schema-mismatch investigation.

**What worked:** The invalid transition response is the model to preserve:

```json
mcp__filigree__.update_issue({
  "issue_id": "filigree-8e7ee8bb38",
  "status": "fixing"
}) -> {
  "error": "Invalid status 'fixing' for type 'task'. Valid states: open, in_progress, closed",
  "code": "INVALID_TRANSITION",
  "valid_transitions": [
    {"to": "in_progress", "category": "wip", "ready": true},
    {"to": "closed", "category": "done", "ready": true}
  ],
  "hint": "Use get_valid_transitions to see allowed state changes"
}
```

Empty dynamic batches are correctly valid:

```json
mcp__filigree__.batch_update({"issue_ids": [], "priority": 2}) -> {
  "succeeded": [],
  "failed": []
}
```

Missing IDs inside a batch also use the promised envelope:

```json
{
  "succeeded": [],
  "failed": [{"id": "filigree-doesnotexist", "error": "Not found: filigree-doesnotexist", "code": "NOT_FOUND"}]
}
```

`undo_last` did undo the intended priority change.

**What hurt:** Claiming a closed issue returns `CONFLICT`, not `INVALID_TRANSITION`:

```json
mcp__filigree__.claim_issue({
  "issue_id": "filigree-467b05ad8d",
  "assignee": "mcp-review-agent"
}) -> {
  "error": "Cannot claim filigree-467b05ad8d: status is 'closed', expected open-category state",
  "code": "CONFLICT"
}
```

That is understandable as "claim conflict", but it means similar status errors branch differently across tools. `undo_last` also returns a nested issue with `"id"`:

```json
mcp__filigree__.undo_last({"issue_id": "filigree-a77878def9"}) -> {
  "undone": true,
  "event_type": "priority_changed",
  "issue": {"id": "filigree-a77878def9", "priority": 4}
}
```

I did not attempt to corrupt or bump the live DB schema to trigger warm-but-degraded MCP. Source confirms the intended path: `mcp_server.call_tool` checks `_schema_mismatch` and returns `ErrorResponse(code=SCHEMA_MISMATCH)`, but an agent cannot safely verify that path from inside the normal MCP surface.

## 3. Findings

### P1

1. **P1 - `get_ready` includes assigned issues that cannot be started.** Evidence: `get_ready({})` returned P1 `filigree-1c7b2776a5`; `get_issue` showed `"assignee": "claude-debug"`; `start_work` returned `{"code": "CONFLICT", "error": "already assigned to 'claude-debug'"}`. Why it matters: a cold agent following the official workflow picks the top item, then immediately fails and has to second-guess the ready queue. Suggested resolution: exclude assigned issues from `get_ready` / `start_next_work` by default, or split them into `ready_unassigned` and `needs_reclaim` with an opt-in `include_assigned=true`.

2. **P1 - The 2.0 `issue_id` rename is incomplete across major workflows.** Evidence: `get_critical_path` returns path nodes with `id`; `create_plan` and `get_plan` return milestone/phase/step `id`; `promote_observation` returns `{"issue": {"id": ...}}`; `undo_last` returns `{"issue": {"id": ...}}`; dependency mutations accept `from_issue_id` / `to_issue_id` but return `from_id` / `to_id`. Why it matters: agents have to memorize response-specific key names and will write brittle handoffs or follow-up calls. Suggested resolution: define an entity-key rule and apply it everywhere; for issues, prefer `issue_id` in all issue records, including nested plan and undo records.

3. **P1 - `promote_observation` returns a stale, wrapped issue snapshot.** Evidence: `promote_observation(...) -> {"issue": {"id": "filigree-fe2e11738c", "labels": []}}`, while a later `list_issues(label="from-observation")` showed the promoted issue did have `from-observation`. Why it matters: the agent cannot trust the result it just received, and the response shape differs from `create_issue`. Suggested resolution: re-read after attaching labels and return a flat `PublicIssue` with `issue_id`.

### P2

4. **P2 - Claimed work still looks ready.** Evidence: `claim_issue` on `filigree-86f669f82b` returned `"assignee": "agent-alpha"` and `"is_ready": true`; the real P1 assigned issue also appeared in `get_ready`. Why it matters: multi-agent sessions need "ready" to mean claimable, not merely open-category and unblocked. Suggested resolution: make readiness account for assignee, and expose a separate stale-claim/reclaim queue.

5. **P2 - MCP has no `session_context` tool matching the canonical startup instruction.** Evidence: `CLAUDE.md` / `AGENTS.md` require `filigree session-context`; MCP exposes `get_summary` instead. Why it matters: the primary surface cannot directly perform the documented session-start ritual, so agents either use CLI or guess that `get_summary` is equivalent. Suggested resolution: add `session_context` as an alias/tool, or update canonical guidance to name `get_summary` for MCP.

6. **P2 - `create_plan` cannot consume a JSON file through MCP.** Evidence: tool schema accepts nested `milestone` / `phases` objects but no file path; the user workflow "Create a milestone/phase/step plan from a JSON file" cannot be done via MCP. Why it matters: agents are often handed artifacts on disk, and MCP is the preferred interface. Suggested resolution: add `create_plan_from_file(path)` or `validate_plan_json` plus `create_plan`.

7. **P2 - Planning edits require low-level graph surgery.** Evidence: adding a step mid-flight required `create_issue(type="step", parent_issue_id=...)`; retargeting required `remove_dependency` plus two `add_dependency` calls; there is no plan-specific edit operation. Why it matters: the agent must hold the plan tree and dependency semantics manually, increasing the chance of wrong deps. Suggested resolution: add plan-native tools such as `add_plan_step`, `retarget_plan_dependency`, and `label_plan_tree`.

8. **P2 - Findings promotion semantics conflict with agent expectations.** Evidence: `promote_finding({"finding_id": "filigree-sf-648fe06426"})` returned an observation ID (`filigree-obs-a385f5ade4`), not an issue ID. Why it matters: guidance and common language imply "promote" means "track this as work"; turning it into another expiring scratchpad item adds an unexpected triage hop. Suggested resolution: either rename to `promote_finding_to_observation` and add `promote_finding_to_issue`, or make `promote_finding` create tracked work directly.

9. **P2 - Scanner triggering does not expose risk/egress metadata.** Evidence: `list_scanners` returned only `{"name": "claude-code", "description": "Per-file bug hunt using Claude Code CLI"}`; `preview_scan` showed an external CLI command; `trigger_scan` was blocked by the execution guard as possible repository-content egress. Why it matters: agents need to know before triggering whether a scanner is local, external, long-running, networked, or requires approval. Suggested resolution: include scanner metadata such as `execution_mode`, `may_send_contents`, `requires_dashboard`, `estimated_cost`, and `safe_preview_only`.

10. **P2 - No bulk remove-label operation.** Evidence: `batch_add_label` exists and worked, but removing the same label from two issues required two `remove_label` calls. Why it matters: triage and grooming commonly add and remove labels over a cluster; one-sided bulk support makes cleanup and retargeting tedious. Suggested resolution: add `batch_remove_label` with the same `response_detail` behavior as `batch_add_label`.

11. **P2 - `get_changes` is too noisy for multi-agent catch-up.** Evidence: `get_changes(since="2026-05-05T23:49:45+00:00")` returned my coordination events mixed with many unrelated `reviewer-b` scratch events. Why it matters: a resumed agent wants "what changed relevant to me/my issue/my labels", not a raw project event firehose. Suggested resolution: add filters for `actor`, `issue_id`, `label`, `type`, and a response `next_since` cursor.

12. **P2 - Similar status failures use different error codes.** Evidence: invalid `update_issue(status="fixing")` returned `INVALID_TRANSITION` with transition hints; `claim_issue` on a closed issue returned `CONFLICT` for "status is 'closed', expected open-category state." Why it matters: agents branch on `code`; status-shape errors should not sometimes mean retryable conflict and sometimes invalid transition. Suggested resolution: use `INVALID_TRANSITION` for status/category violations and reserve `CONFLICT` for assignee compare-and-swap conflicts.

### P3

13. **P3 - `get_critical_path` empty output lacks an explanatory note.** Evidence: before I created scratch deps, `get_critical_path({}) -> {"path": [], "length": 0}`. Why it matters: empty can mean no dependencies, no open issues, a filter mistake, or a tool failure. Suggested resolution: include `note: "no open dependency chains"` or counts used to compute the result.

14. **P3 - Mutation response shapes vary too much.** Evidence: `add_comment` returns `{status, comment_id}`; `remove_label` returns `{status, issue_id, label}`; `remove_dependency` returns `{status, from_id, to_id}`; `report_finding` returns `{status, findings_created, finding_id}`; `undo_last` returns `{undone, event_type, issue}`. Why it matters: the agent has to inspect each result shape before composing the next call. Suggested resolution: prefer post-mutation records, or wrap acknowledgements in a small set of typed result envelopes.

15. **P3 - List/file/finding primary keys use generic `id` rather than entity-qualified IDs.** Evidence: `get_finding` returns `"id": "filigree-sf-..."`; `get_file` returns `"file": {"id": "filigree-f-..."}`; `get_file_timeline` uses `"source_id"`. Why it matters: mixed entity IDs are easy to pass to the wrong tool. Suggested resolution: use `finding_id`, `file_id`, and `timeline_event_id`, or expose a schema tool documenting all ID prefixes.

16. **P3 - `get_file_timeline` does not include associated issue events.** Evidence: `get_file` showed an associated issue for `src/filigree/mcp_tools/issues.py`, but `get_file_timeline` only showed `finding_created` and `association_created`, not issue lifecycle events. Why it matters: file -> issues -> findings -> scan history requires extra fan-out calls. Suggested resolution: add `include_issue_events=true` to merge associated issue event summaries.

17. **P3 - Schema mismatch cannot be safely exercised through MCP.** Evidence: live tools had no way to simulate or inspect warm-but-degraded mode; source confirms `call_tool` short-circuits to `SCHEMA_MISMATCH` only if server startup has already detected a newer DB schema. Why it matters: agents cannot verify the recovery path without mutating DB metadata or restarting MCP. Suggested resolution: add a read-only diagnostic tool that reports MCP server DB-open status and schema compatibility.

18. **P3 - Scratch cleanup is possible but not deletion.** Evidence: `batch_close` cleaned up open scratch issues and plan items, and `dismiss_finding` / `dismiss_observation` cleaned scratch findings/observations; file records and closed scratch issues remain by design. Why it matters: review/testing sessions leave durable closed clutter. Suggested resolution: document that this is expected, or add an admin-only purge/archive tool scoped to closed scratch labels.

## 4. What works well

- `get_summary` is the right shape for orientation and should stay.
- `start_next_work` / `start_work` happy paths are the core 2.0 win: atomic claim plus transition in one call.
- `get_issue(include_transitions=true)` gives an agent enough workflow context without a second call.
- `BatchResponse` with `response_detail="full"` is very useful and worked for issue and finding batch operations.
- Invalid-transition errors with `valid_transitions` and `hint` are excellent.
- `report_finding` is a strong zero-ceremony agent note-to-finding primitive.
- `batch_close` cleanup was effective and returned `newly_unblocked` when relevant.

## 5. Open questions for the maintainer

1. Should "ready" mean "open-category and unblocked" or "claimable by a new agent right now"? The MCP surface currently chooses the former, but agent workflow needs the latter.
2. Is the long-term ID contract `issue_id` for issues only, or entity-qualified IDs everywhere (`file_id`, `finding_id`, `observation_id`)? The current half-state is the source of several findings.
3. Should `promote_finding` create an observation first by design, or should agents be able to promote a finding straight to an issue?
4. Should MCP intentionally omit file-backed operations like `session-context` and `create-plan --file`, or should every canonical agent workflow have a first-class MCP equivalent?
5. What scanner metadata is needed so agents can decide whether triggering a scanner is safe without discovering risk at execution time?
6. Is closed scratch/test data supposed to remain forever, or should Filigree provide a scoped cleanup/archive path for review fixtures?
