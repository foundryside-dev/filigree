# MCP Server Reference

Filigree exposes an MCP (Model Context Protocol) server so AI agents interact natively without parsing CLI output. The server provides 116 tools, 1 resource, and 1 prompt.

!!! note "3.0.0 tool names"
    The tool names below are the subsystem-namespaced `<entity>_<verb>` names
    (`issue_get`, `finding_list`, `work_start`, …). 3.0.0 **removed** the legacy
    flat aliases that 2.3.0 still resolved — a call to a removed name now returns
    the `NOT_FOUND` envelope. See
    the [3.0.0 consumer migration guide](MIGRATION-3.0.md#1-mcp-tool-name-namespacing)
    for the full old→new table.

## Contents

- [Setup](#setup)
- [Resource](#resource)
- [Prompt](#prompt)
- [Tools](#tools)
  - [Core Operations](#core-operations)
  - [Ready and Blocked](#ready-and-blocked)
  - [Dependencies](#dependencies)
  - [Comments and Labels](#comments-and-labels)
  - [Search](#search)
  - [Planning](#planning)
  - [Claiming](#claiming)
  - [Batch Operations](#batch-operations)
  - [Templates and Workflow](#templates-and-workflow)
  - [Analytics](#analytics)
  - [Data Management](#data-management)
  - [Files and Traceability](#files-and-traceability)
  - [Agent Context Notes](#agent-context-notes)
  - [Scanning](#scanning)

## Setup

The simplest path:

```bash
filigree install --claude-code    # Writes .mcp.json with folder-based autodiscovery
filigree install --codex          # Writes ~/.codex/config.toml with folder-based autodiscovery
filigree install --mode=server    # Configure streamable HTTP MCP for daemon mode
```

For Claude Code and Codex in stdio mode, Filigree now always uses runtime
project discovery. Their config must not pin `--project`, and Codex's global
config must not pin a daemon URL, because those forms can send one workspace's
writes to another workspace's database.

Or manually add to `.mcp.json`:

```json
{
  "mcpServers": {
    "filigree": {
      "type": "stdio",
      "command": "filigree-mcp",
      "args": []
    }
  }
}
```

The MCP server is included in the base install — no extra needed.

## Resource

### `filigree://context`

Auto-generated project summary containing:

- Project vitals (prefix, issue counts, schema version)
- Ready work queue (unblocked issues sorted by priority)
- Blocked issues with their blockers
- Recent activity

Regenerated on every mutation. Agents read this at session start for instant orientation.

## Prompt

### `filigree-workflow`

Workflow guide with optional live project context. Agents use this to understand how to interact with filigree — available types, status workflows, transition rules.

## Tools

### Core Operations

| Tool | Description |
|------|-------------|
| `issue_get` | Full issue details with deps, labels, children, ready status |
| `issue_list` | Filter by status, type, priority, parent, assignee |
| `issue_create` | Create with type, priority, deps, labels, fields |
| `issue_update` | Update status, priority, title, assignee, fields |
| `issue_close` | Close with optional reason |
| `issue_delete` | Hard-delete an issue + dependents (irreversible); writes a tombstone surfaced as `issue_deleted` on `/changes`. Refuses non-terminal/parented/depended-on issues unless `force` |
| `issue_reopen` | Reopen a closed issue to the last non-done status before closure |
| `admin_undo_last` | Undo most recent reversible action |

#### Relationship naming

MCP 2.0 public issue payloads use `issue_id` for the issue primary key and
`parent_issue_id` for hierarchy links. Full issue payloads also include
`parent_id` as a compatibility alias with the same value; new callers should
read `parent_issue_id`. Dependency edges use directional names:
`from_issue_id` is the issue that is blocked, and `to_issue_id` is the issue
that blocks it.

#### `issue_get`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `include_transitions` | boolean | no | Include valid next states in response |

#### `issue_list`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `status` | string | no | Filter by exact status name |
| `status_category` | enum | no | Filter by category: `open`, `wip`, `done` |
| `type` | string | no | Filter by issue type |
| `priority` | 0-4 | no | Filter by priority |
| `parent_issue_id` | string | no | Filter by parent issue ID |
| `limit` | integer | no | Max results (default 100) |
| `offset` | integer | no | Skip first N results |

#### `issue_create`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `title` | string | yes | Issue title |
| `type` | string | no | Issue type (default: `task`) |
| `priority` | 0-4 | no | Priority (default: 2) |
| `description` | string | no | Issue description |
| `notes` | string | no | Additional notes |
| `labels` | string[] | no | Labels to attach during creation (no separate `label_add` call needed) |
| `deps` | string[] | no | Dependency issue IDs |
| `parent_issue_id` | string | no | Parent issue ID |
| `fields` | object | no | Custom fields from template schema |
| `actor` | string | no | Agent identity for audit trail |

#### `issue_update`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `status` | string | no | New status |
| `priority` | 0-4 | no | New priority |
| `title` | string | no | New title |
| `description` | string | no | New description |
| `notes` | string | no | New notes |
| `assignee` | string | no | New assignee |
| `parent_issue_id` | string | no | New parent (empty string to clear) |
| `fields` | object | no | Fields to merge into existing |
| `actor` | string | no | Agent identity for audit trail |
| `expected_assignee` | string | no | Override expected holder for coordinator writes |

Soft workflow enforcement does not block the update. When a status change skips
recommended fields, the returned issue includes the advisory in
`data_warnings[]`; the same message is recorded once as a `transition_warning`
event.

Claim-aware write safety is on by default when `actor` is present: if the issue
is held, the observed assignee must match `actor`. Coordinator flows that
intentionally edit another actor's held issue can pass `expected_assignee` with
the observed holder; mismatches return `CONFLICT` and name both holders.

#### `issue_close`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `reason` | string | no | Close reason |
| `fields` | object | no | Extra fields to set while closing (for enforced workflows) |
| `actor` | string | no | Agent identity for audit trail |
| `expected_assignee` | string | no | Override expected holder for coordinator writes |
| `force` | boolean | no | Use the declared reverse/escape edge for cleanup closes |

`force=true` validates against template `reverse_transitions` and emits
`transition_forced`; normal close validation remains forward-only.

When an issue has active `critical=true` annotations linked with
`relationship="must_consider"`, `issue_close` still closes the issue but returns
an `annotation_warnings` array. Each warning contains the `annotation_id`,
file anchor, computed `anchor_state`, and suggested follow-up tools.

#### `issue_reopen`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `actor` | string | no | Agent identity for audit trail |

#### `admin_undo_last`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `actor` | string | no | Agent identity for audit trail |

### Ready and Blocked

| Tool | Description |
|------|-------------|
| `work_ready` | Unassigned open-category issues with no blockers, sorted by priority |
| `work_blocked` | Blocked issues with their blocker lists, optionally hydrated with blocker context |
| `dependency_critical_path` | Longest dependency chain |

`work_ready` returns the slim issue shape plus a `startable` flag on each item.
`startable` is `true` when the issue can be transitioned into a working state in
one hop (what `work_start` does by default); it is `false` for issues that are
*ready* but not directly *startable* — notably `triage` bugs, which must walk
`triage → confirmed → fixing`. Non-startable items also carry `next_action`, the
intermediate status to move through first (e.g. `"confirmed"`). Pass
`include_context=true` to additionally add `parent_issue_id` and `parent_title`.
`work_blocked` returns blocker IDs by default. Pass `include_blockers=true` to
add slim blocker records under `blockers[]` while preserving `blocked_by`.
`dependency_critical_path` takes no required parameters.

#### `work_ready`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `include_context` | boolean | no | Include parent issue ID/title on each ready item |

#### `work_blocked`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `include_blockers` | boolean | no | Include slim blocker records under `blockers[]` |

### Dependencies

| Tool | Description |
|------|-------------|
| `dependency_add` | Add blocker: `from_issue_id` depends on `to_issue_id` |
| `dependency_remove` | Remove blocker relationship |

#### `dependency_add` / `dependency_remove`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `from_issue_id` | string | yes | Issue that is blocked |
| `to_issue_id` | string | yes | Issue that blocks |
| `actor` | string | no | Agent identity for audit trail |

### Comments and Labels

| Tool | Description |
|------|-------------|
| `comment_add` | Add comment to an issue |
| `comment_list` | Get all comments on an issue |
| `label_add` | Add label to an issue |
| `label_remove` | Remove label from an issue |

#### `comment_add`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `text` | string | yes | Comment text |
| `actor` | string | no | Used as comment author |
| `expected_assignee` | string | no | Override expected holder for coordinator writes |

Returns the updated `PublicIssue`, preserving top-level `comment_id` for
compatibility and adding `comment: {comment_id, author, text, created_at}` so
callers can confirm the exact inserted comment without a follow-up read.

#### `comment_list`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |

#### `label_add` / `label_remove`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `label` | string | yes | Label name |
| `actor` | string | no | Agent identity for claim-aware write safety |
| `expected_assignee` | string | no | Override expected holder for coordinator writes |

### Search

| Tool | Description |
|------|-------------|
| `issue_search` | Search by title and description (FTS5) |
| `summary_get` | Pre-computed project summary (same as `context.md`) |
| `stats_get` | Project statistics with explicit status-name and status-category count maps |

#### `issue_search`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | string | yes | Search query |
| `limit` | integer | no | Max results (default 100) |
| `offset` | integer | no | Skip first N results |

#### `stats_get`

Returns `by_status` (counts by literal workflow status name such as `open` or
`in_progress`) and `by_category` (template categories `open`/`wip`/`done`),
plus `by_type`, `ready_count`, `blocked_count`, and `total_dependencies`. The
deprecated `status_name_counts` / `status_category_counts` maps (exact
duplicates of `by_status` / `by_category`) were **removed in 3.0.0**
(filigree-e4181ae767). Read `by_status` / `by_category`.

### Planning

| Tool | Description |
|------|-------------|
| `plan_get` | Milestone plan tree with progress |
| `plan_create` | Create milestone/phase/step hierarchy in one call |
| `plan_step_add` | Add a step to an existing phase |
| `plan_dependency_retarget` | Swap one step dependency for another |
| `plan_step_move` | Move an existing step to another phase |
| `plan_label_tree` | Apply a label to a milestone subtree |

#### `plan_get`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `milestone_id` | string | yes | Milestone issue ID |
| `response_detail` | enum | no | `slim` (default) for compact issue records, `full` for full issue payloads |

Returns the plan tree with progress fields. Slim responses keep milestone,
phase, and step records compact; full responses include full issue payloads
with descriptions, fields, labels, blockers, and timestamps.

Plan-editing operations preserve dependency edges. `plan_step_move` returns a
`warnings[]` entry when active dependencies are carried forward across the move;
use `plan_dependency_retarget` when a moved step's blockers should change.

#### `plan_create`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `milestone` | object | yes | `{title, description?, priority?}` |
| `phases` | array | yes | Array of `{title, description?, priority?, steps}` |
| `actor` | string | no | Agent identity for audit trail |

Step deps within a phase use integer indices. Cross-phase deps use `"phase_idx.step_idx"` format.

### Claiming

| Tool | Description |
|------|-------------|
| `work_start` | Atomically claim and transition an issue into work (single-hop; `advance` walks multi-hop types) |
| `work_start_next` | Claim highest-priority ready issue and transition it into work (skips non-startable candidates) |
| `work_claim` | Claim only, with optimistic locking |
| `work_claim_next` | Claim highest-priority ready issue only |
| `work_release` | Release a claim, optionally idempotently with `if_held` |
| `work_release_mine` | Bulk-release every live claim held by one actor |
| `work_heartbeat` | Refresh claim liveness for active work |
| `work_stale_list` | List assigned work with expired leases or old legacy assignments |
| `work_reclaim` | Transfer a stale claim when the expected holder still owns it |

#### `work_start`

A `triage` bug (and any type with no single-hop wip target) is *ready* but not
directly *startable*: without `advance`, `work_start` returns `INVALID_TRANSITION`
naming the intermediate status to move through first.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `assignee` | string | yes | Who is starting work |
| `target_status` | string | no | Working status override |
| `advance` | boolean | no | Walk soft transitions to the nearest wip state (e.g. `triage → confirmed → fixing`) when no single-hop wip target exists. Missing required fields surface as warnings, not blocks; hard edges are never auto-walked. Ignored when `target_status` is given. Default `false`. |
| `actor` | string | no | Agent identity (defaults to assignee) |

#### `work_start_next`

Candidates that are ready but not single-hop startable (e.g. `triage` bugs) are
skipped. Pass `advance=true` to make them startable via the multi-hop soft walk.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `assignee` | string | yes | Who is starting work |
| `type` | string | no | Filter by issue type |
| `priority_min` | 0-4 | no | Minimum priority |
| `priority_max` | 0-4 | no | Maximum priority |
| `target_status` | string | no | Working status override |
| `advance` | boolean | no | Walk soft transitions to wip so multi-hop types (e.g. `triage` bugs) become startable instead of skipped. Default `false`. |
| `actor` | string | no | Agent identity (defaults to assignee) |

#### `work_claim`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `assignee` | string | yes | Who is claiming |
| `actor` | string | no | Agent identity (defaults to assignee) |

#### `work_claim_next`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `assignee` | string | yes | Who is claiming |
| `type` | string | no | Filter by issue type |
| `priority_min` | 0-4 | no | Minimum priority |
| `priority_max` | 0-4 | no | Maximum priority |
| `actor` | string | no | Agent identity (defaults to assignee) |

#### `work_release`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `actor` | string | no | Agent identity for audit trail |
| `if_held` | boolean | no | Idempotent release-if-held mode; unassigned issues are returned unchanged, but held-by-other mismatches return `CONFLICT` |
| `expected_assignee` | string | no | Only release when the current assignee matches this value; defaults to `actor` in `if_held` mode |
| `reason` | string | no | Audit reason recorded on the release event |

#### `work_release_mine`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `actor` | string | yes | Agent identity whose live claims should be released |
| `label` | string | no | Restrict to issues carrying this exact label |
| `label_prefix` | string | no | Restrict to issues with a label starting with this prefix |
| `dry_run` | boolean | no | Return the issues that would be released without changing them |
| `revert_status` | boolean | no | Revert wip-category issues to an open predecessor (default true) |
| `reason` | string | no | Audit reason recorded on each release event |
| `response_detail` | enum | no | `slim` or `full` |

#### `work_heartbeat`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `actor` | string | no | Agent identity for audit trail and default holder check |
| `expected_assignee` | string | no | Only heartbeat when the current assignee matches this value |
| `lease_hours` | integer | no | Lease duration from this heartbeat (default 48) |

#### `work_stale_list`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `stale_after_hours` | integer | no | Age threshold for legacy assignments without explicit lease metadata (default 48) |
| `expires_within_hours` | integer | no | Also include active explicit leases expiring within this many hours |

Returns a `ListResponse[IssueDict]` containing assigned, non-done issues whose
`claim_expires_at` is in the past, plus legacy assigned rows older than the
threshold. Pass `expires_within_hours` to also surface active leases that are
close enough to expiry for proactive heartbeating.

#### `work_reclaim`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `assignee` | string | yes | New assignee |
| `expected_assignee` | string | yes | Current assignee expected by the caller |
| `reason` | string | yes | Why the claim is being reclaimed |
| `actor` | string | no | Agent identity for audit trail |
| `lease_hours` | integer | no | Lease duration for the new assignee (default 48) |

### Batch Operations

| Tool | Description |
|------|-------------|
| `issue_batch_update` | Update multiple issues with the same changes |
| `issue_batch_close` | Close multiple with per-item error reporting |
| `label_batch_add` | Add the same label to multiple issues |
| `comment_batch_add` | Add the same comment to multiple issues |
| `observation_batch_dismiss` | Dismiss multiple observations at once |
| `observation_batch_link` | Link multiple observations to one issue with a shared disposition |
| `observation_batch_promote` | Promote multiple observations to separate issues |
| `finding_batch_update` | Update status on multiple scan findings |

All batch tools return the unified `BatchResponse` envelope (`{succeeded, failed, newly_unblocked?}`) and accept an optional `response_detail: "slim" | "full"` (default `"slim"`). In `"slim"` mode `succeeded` is a list of compact records (`SlimIssue` for issue ops, IDs for label/comment/observation/finding ops); in `"full"` mode each batch tool upgrades `succeeded` to the full record type:

| Tool | Slim `succeeded[i]` | Full `succeeded[i]` |
|------|---------------------|---------------------|
| `issue_batch_update`, `issue_batch_close` | `SlimIssue` | `IssueDict` |
| `label_batch_add`, `comment_batch_add` | `issue_id: str` | `IssueDict` |
| `observation_batch_dismiss` | `observation_id: str` | `ObservationDict` (snapshot pre-dismissal) |
| `observation_batch_link` | `ObservationLink` | `ObservationLink` |
| `observation_batch_promote` | `SlimIssue` | `PublicIssue` |
| `finding_batch_update` | `finding_id: str` | `ScanFindingDict` |

#### `issue_batch_update`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_ids` | string[] | yes | Issue IDs |
| `status` | string | no | New status |
| `priority` | 0-4 | no | New priority |
| `assignee` | string | no | New assignee |
| `fields` | object | no | Fields to merge |
| `response_detail` | `"slim" \| "full"` | no | Default `"slim"` |
| `actor` | string | no | Agent identity for audit trail |
| `expected_assignee` | string | no | Override expected holder for coordinator writes |

#### `issue_batch_close`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_ids` | string[] | yes | Issue IDs |
| `reason` | string | no | Close reason |
| `response_detail` | `"slim" \| "full"` | no | Default `"slim"` |
| `actor` | string | no | Agent identity for audit trail |
| `expected_assignee` | string | no | Override expected holder for coordinator writes |

#### `label_batch_add`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_ids` | string[] | yes | Issue IDs |
| `label` | string | yes | Label to add |
| `response_detail` | `"slim" \| "full"` | no | Default `"slim"` |
| `actor` | string | no | Agent identity for audit trail |
| `expected_assignee` | string | no | Override expected holder for coordinator writes |

#### `comment_batch_add`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_ids` | string[] | yes | Issue IDs |
| `text` | string | yes | Comment text |
| `response_detail` | `"slim" \| "full"` | no | Default `"slim"` |
| `actor` | string | no | Agent identity for audit trail |
| `expected_assignee` | string | no | Override expected holder for coordinator writes |

### Templates and Workflow

| Tool | Description |
|------|-------------|
| `type_list` | All registered types with pack info |
| `template_get` | Canonical full workflow definition for a type |
| `type_get` | Compatibility alias for `template_get` |
| `workflow_transition_list` | Valid next states with readiness indicators |
| `issue_validate` | Validate against template (warnings for missing fields) |
| `pack_list` | Enabled workflow packs |
| `workflow_guide_get` | Pack documentation |
| `workflow_status_list` | Statuses by category (open/wip/done) |
| `schema_get` | Entity ID prefixes and accepted tool families |
| `mcp_status_get` | Read-only MCP server/schema compatibility diagnostic |
| `workflow_status_explain` | Status transitions and required fields |
| `admin_reload_templates` | Refresh templates from disk |

`schema_get.entity_id_prefixes.*.accepted_by_tools` is derived from the live MCP
tool registry. The docs headline tool count is pinned by tests against the same
registry so new tools cannot silently drift from the published reference.

See [Workflow Templates](workflows.md#runtime-semantics-contract) for the
runtime contract behind these tools: initial states, status categories,
hard/soft transition enforcement, `data_warnings[]`, close/reopen target
selection, and claim handoff behavior.

#### `type_get`

Compatibility alias for `template_get`; returns the same canonical workflow definition.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `type` | string | yes | Issue type name |

#### `template_get`

Canonical workflow-discovery tool for issue types. Returns pack, states,
forward transitions, reverse transitions, initial state, and fields schema.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `type` | string | yes | Issue type name |

#### `workflow_transition_list`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |

Returns `ListResponse[TransitionDetail]` (`{items, has_more}`), with
`has_more=false` because transition sets are finite and unpaginated.

#### `issue_validate`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |

#### `workflow_guide_get`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `pack` | string | yes | Pack name (e.g., `core`, `planning`) |

#### `workflow_status_explain`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `type` | string | yes | Issue type name |
| `status` | string | yes | Status name |

#### `mcp_status_get`

No parameters. Returns connector health fields including `status`, `db_initialized`, `schema_compatible`, `installed_schema_version`, `database_schema_version`, `code`, `error`, `guidance`, `filigree_dir`, `runtime`, and `actor_verification`. The `runtime` object identifies the executing Python binary, resolved binary path, MCP entrypoint, module file, package root, detected venv root, and install context (`venv`, `uv_tool`, or `system_or_unknown`). The `actor_verification` object (`{verified, verified_actor, deferral, note}`) reports the ADR-012 actor-verification posture for this transport: MCP-stdio stamps the OS identity (`verified=true`); MCP-HTTP cannot vouch for the caller, so the `actor` argument is a self-asserted claim and `verified_actor`/`verified_author` are NULL (`verified=false`) — transport-bound identity is deferred to `filigree-81d3971467`. This tool is safe to call in warm-but-degraded `SCHEMA_MISMATCH` mode.

### Analytics

| Tool | Description |
|------|-------------|
| `metrics_get` | Cycle time, lead time, throughput |
| `change_list` | Events since a timestamp |
| `issue_event_list` | Event history for one issue |

#### `metrics_get`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `days` | integer | no | Lookback window (default 30) |

#### `change_list`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `since` | ISO timestamp | yes | Get events after this time |
| `limit` | integer | no | Max events (default 100) |

#### `issue_event_list`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `limit` | integer | no | Max events (default 50) |

### Data Management

| Tool | Description |
|------|-------------|
| `admin_export_jsonl` | Export all data to JSONL |
| `admin_import_jsonl` | Import from JSONL |
| `admin_archive_closed` | Archive old closed issues |
| `admin_compact_events` | Compact event history |
| `reconciliation_debt_list` | List issues carrying reconciliation debt (governed cascade closes the Legis gate deferred) |

#### `reconciliation_debt_list`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `limit` | integer | no | Max results (default 50) |
| `offset` | integer | no | Skip first N results |

#### End-of-session cleanup

Use one session-unique label, such as `cluster:<session-id>`, on scratch issues
and temporary review work so cleanup can be scoped without sweeping another
agent's artifacts.

1. Finish, hand off, or comment on the active issue before cleanup; task-scope
   defects should become tracked work, not expiring observations.
2. Preview and release live claims with `work_release_mine(actor=..., label=...,
   dry_run=true)`, then repeat with `dry_run=false` and a `reason` once the
   preview is right. Use `label_prefix` only when the prefix is unique enough
   for the session. A claim held by another actor is a `CONFLICT`, not a
   release-if-held no-op; investigate it before retrying as a coordinator.
3. List pending notes with `observation_list(actor=...)`, then use
   `observation_promote_to_issue`, `observation_batch_link`, or
   `observation_batch_dismiss` so observations are either tracked, attached as
   evidence, or intentionally dropped.
4. Review scan scratch with `finding_list`; use `finding_promote`,
   `finding_dismiss`, or `finding_batch_update` before deleting file records.
5. Remove synthetic file records with `file_delete`. Prefer the default
   refusal mode first; use `force=true` only after associated issues/findings
   are handled.
6. Archive closed scratch with `admin_archive_closed(days_old=0, label=...)` after the
   label scope is confirmed. `admin_compact_events` is a separate storage-maintenance
   step for already archived issues.

#### `admin_export_jsonl`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `output_path` | string | yes | File path for JSONL output |

#### `admin_import_jsonl`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `input_path` | string | yes | File path to read JSONL from |
| `merge` | boolean | no | Skip existing records (default false) |

#### `admin_archive_closed`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `days_old` | integer | no | Archive issues closed more than N days ago (default 30) |
| `actor` | string | no | Agent identity for audit trail |
| `label` | string | no | Only archive closed issues currently carrying this label |

#### `admin_compact_events`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `keep_recent` | integer | no | Keep N most recent events per archived issue (default 50) |

### Files and Traceability

| Tool | Description |
|------|-------------|
| `file_list` | List tracked files with filtering, sorting, and pagination |
| `file_get` | Get file detail + associations + findings summary |
| `file_timeline_get` | Get merged file timeline events |
| `issue_file_list` | List files associated with an issue |
| `file_association_add` | Associate file and issue (`bug_in`, `task_for`, `scan_finding`, `mentioned_in`) |
| `file_register` | Register/get file record by project-relative path |
| `file_delete` | Delete a file record, refusing associations/open findings unless forced |

#### `file_list`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `limit` | integer | no | Max results (default 100, max 10000) |
| `offset` | integer | no | Skip first N results |
| `language` | string | no | Filter by language |
| `path_prefix` | string | no | Filter by path substring |
| `min_findings` | integer | no | Minimum open findings count |
| `has_severity` | enum | no | Require at least one open finding at severity |
| `scan_source` | string | no | Filter by finding source |
| `sort` | enum | no | `updated_at`, `first_seen`, `path`, `language` |
| `direction` | enum | no | `asc`/`desc` |

#### `file_get`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file_id` | string | yes | File ID |

Response includes: `file`, `associations`, `recent_findings`, `summary`.

#### `file_timeline_get`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file_id` | string | yes | File ID |
| `limit` | integer | no | Max events (default 50) |
| `offset` | integer | no | Skip first N events |
| `event_type` | enum | no | `finding`, `association`, `file_metadata_update`, `issue_event` |
| `include_issue_events` | boolean | no | Merge events from issues currently associated with the file |

#### `issue_file_list`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |

#### `file_association_add`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file_id` | string | yes | File ID |
| `issue_id` | string | yes | Issue ID |
| `assoc_type` | enum | yes | `bug_in`, `task_for`, `scan_finding`, `mentioned_in` |
| `actor` | string | no | Actor identity recorded on the association |

#### `file_register`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | yes | Project-relative file path |
| `language` | string | no | Optional language hint |
| `file_type` | string | no | Optional file type tag |
| `metadata` | object | no | Optional metadata map |
| `actor` | string | no | Actor identity recorded on the file record or metadata event |

#### `file_delete`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file_id` | string | yes | File ID |
| `force` | boolean | no | Cascade associations and open findings (default false) |
| `actor` | string | no | Actor identity echoed in the deletion result |

Finding records include `created_by` and `updated_by`. Finding timeline events
include the relevant actor; `finding_update`, `finding_batch_update`, and
`finding_dismiss` accept `actor` for triage attribution.
`finding_dismiss` defaults to `status="false_positive"` and accepts
`false_positive`, `fixed`, `unseen_in_latest`, or `acknowledged`. A `reason`
is stored on the finding metadata as `dismiss_reason`. File summaries and safe
file deletion treat `fixed` and `false_positive` as terminal; stale
`unseen_in_latest` findings become `fixed` through `clean_stale_findings`.

### Cross-Product Entity Associations

Bind a Filigree issue to an opaque entity identifier from a sibling
product (notably Loomweave — see ADR-029). Filigree never parses the
entity-ID grammar; the binding stores opaque strings so the federation
enrich-only rule is preserved.

| Tool | Description |
|------|-------------|
| `entity_association_add` | Attach an opaque external entity to a Filigree issue (idempotent on the composite key — re-attach refreshes the hash, preserves original actor) |
| `entity_association_remove` | Remove the binding identified by `(issue_id, entity_id)` |
| `entity_association_list` | Return the entity bindings attached to an issue (raw rows; drift comparison is the consumer's job per ADR-029 §"Decision 3") |
| `entity_association_list_by_entity` | Reverse lookup: return every issue in this project bound to a given opaque external entity ID |
| `finding_promote_and_attach_entity` | Promote a scan finding to an issue and attach an opaque external entity binding |

#### `entity_association_add`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Filigree issue ID |
| `entity_id` | string | yes | Opaque external entity ID; may be a `loomweave:eid:...` SEI or a legacy locator; not parsed |
| `content_hash` | string | yes | Snapshot of the caller's current content hash for drift detection at query time |
| `entity_kind` / `external_entity_kind` | string | no | Caller-supplied kind metadata; never inferred from `entity_id` |
| `actor` | string | no | Actor identity recorded as `attached_by` on first attach |

#### `entity_association_remove`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Filigree issue ID |
| `entity_id` | string | yes | Opaque external entity ID |

#### `entity_association_list`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Filigree issue ID |

#### `entity_association_list_by_entity`

Project isolation is by DB file — every row in this query already
belongs to the project hosting this database, so no project filter
is required (or accepted).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `entity_id` | string | yes | Opaque external entity ID; not parsed |
| `current_content_hash` | string | no | Caller-supplied current hash; response `freshness_status` is `fresh`, `stale`, or `unknown` |

### Agent Context Notes

Observations and annotations are both agent-facing context capture tools:
observations are ephemeral triage notes, while annotations are durable
file-anchored notes with provenance and drift detection.

#### Observations

| Tool | Description |
|------|-------------|
| `observation_create` | Record a quick scratchpad note, optionally anchored to a file |
| `observation_list` | List active observations with file filters and pagination |
| `observation_dismiss` | Dismiss one observation with audit trail |
| `observation_link` | Link one observation to an existing issue as `evidence`, `duplicate`, `superseded`, or `related` |
| `observation_promote` | Promote one observation to a tracked issue |
| `observation_batch_dismiss` | Dismiss multiple observations in one call |
| `observation_batch_link` | Link multiple observations to one existing issue |
| `observation_batch_promote` | Promote multiple observations in one call |
| `observation_promote_to_issue` | Promote multiple observations into one issue with all source IDs preserved |

#### Annotations

Annotations are durable, project-shared file notes with provenance. They are
not issues, comments, findings, or observations. Every annotation is anchored to
a file, can link to issues/files/findings/observations, and returns computed
anchor drift separately from lifecycle `status`.

List tools return `{items, has_more, next_offset?}`. `response_detail` defaults
to `summary`; pass `full` to include provenance, links, and audit events.

| Tool | Description |
|------|-------------|
| `annotation_create` | Create a file annotation and capture checksum/git/diff provenance |
| `annotation_list` | Filter annotations by file, link target, actor, intent, status, or anchor state |
| `annotation_get` | Get one annotation with full provenance, links, and audit events |
| `annotation_update` | Update note/context/intent/critical/status |
| `annotation_resolve` | Resolve an annotation with audit trail |
| `annotation_supersede` | Supersede one annotation with another |
| `annotation_promote` | Create an issue or observation and add a `promoted_to` link |
| `annotation_carry_forward` | Add a `must_consider` link to a new issue and acknowledge an existing source warning |
| `annotation_link` / `annotation_unlink` | Manage typed target links |
| `file_annotation_list` | List annotations for a file |
| `issue_annotation_list` | List annotations linked to an issue or epic |
| `annotation_attention_list` | List active critical `must_consider` annotations |

##### `annotation_create`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file_path` | string | yes | Project-relative file path |
| `note` | string | yes | Durable note text |
| `line_start` / `line_end` | integer | no | 1-based line range |
| `context_summary` | string | no | What the agent was doing |
| `intent` | enum | no | `explanation`, `warning`, `breadcrumb`, `hypothesis`, `decision`, `handoff`, `gotcha` |
| `critical` | boolean | no | Elevate surfacing and closeout warnings |
| `links` | array | no | `{target_type, target_id, relationship}` entries |
| `actor` | string | no | Agent identity |
| `session_ref` | string | no | Optional opaque run/session reference |

V1 link targets are `issue`, `file`, `finding`, and `observation`.
Relationships are `relevant_to`, `must_consider`, `evidence_for`, `explains`,
`created_from`, and `promoted_to`.

`annotation_carry_forward` requires the annotation to already be linked to
`from_target_id` as `must_consider`; otherwise it returns a `VALIDATION` error
instead of acknowledging an unrelated issue.

### Scanning

| Tool | Description |
|------|-------------|
| `scanner_list` | List registered scanners |
| `scanner_available_list` | List bundled scanners that can be enabled |
| `scanner_enable` | Enable a bundled scanner registration |
| `scanner_disable` | Disable a scanner registration |
| `prompt_pack_list` | List bundled scanner review-focus prompt packs |
| `scan_trigger` | Trigger async file scan (single file) |
| `scan_trigger_batch` | Trigger a scanner across multiple files in one call |
| `scan_status_get` | Live status + log tail for a `scan_run_id` |
| `scan_preview` | Preview the command a scan would execute, without spawning a process |
| `finding_report` | Report a single agent-discovered finding, with explicit opt-in paired observation creation |

#### `scanner_list`

No parameters. Returns scanners registered in `.filigree/scanners/*.toml` in
the unified list envelope:
`{items: [{name, description, file_types, accepts_prompt, prompt_pack_aware, prompt_packs_endpoint, applicable_prompts, bundled_name, bundled_match, managed, sandbox_class, sandbox_summary, ...}], has_more: bool}`.
If the list is empty, call `scanner_available_list` to see bundled scanners
that can be enabled.

#### `scanner_available_list`

No parameters. Returns bundled scanners that can be enabled in the current
project, including `command_available`, `command_path`, `enabled`,
`language_focus`, `applicable_prompts`, and the managed TOML `path`.

#### `scanner_enable`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `scanner` | string | yes | Bundled scanner name, e.g. `codex` or `claude` |
| `force` | boolean | no | Replace an existing custom or stale bundled TOML |

Writes the managed `.filigree/scanners/<scanner>.toml` registration for a
bundled scanner. Refuses to overwrite custom TOML unless `force=true`. If the
packaged runner command is not on `PATH`, the response includes
`command_available=false` and a warning with the `uv tool install --upgrade
filigree` remediation.

#### `scanner_disable`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `scanner` | string | yes | Scanner name |
| `force` | boolean | no | Remove a custom TOML that uses a bundled scanner name |

Removes a scanner registration. Custom non-bundled scanner names can be removed
without `force`; bundled scanner names with custom content require `force=true`.

#### `prompt_pack_list`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `language` | string | no | Optional scanner language focus, e.g. `python`; returns language-agnostic packs plus packs for that focus |

Returns bundled scanner prompt packs in the unified list envelope:
`{items: [{name, description, instructions, components, when_to_use, audience, language, expected_relative_cost, prompt_pack_scope}], has_more: bool}`.
Prompt packs are advisory review-focus hints; they do not restrict scanner file
access or reported findings. Some packs are language-specific; prefer a
scanner's `applicable_prompts` field, or call `prompt_pack_list` with the
scanner's `language_focus`, when selecting a pack.

#### `scan_trigger`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `scanner` | string | yes | Scanner name (from scanner_list) |
| `file_path` | string | yes | File path to scan (relative to project root) |
| `prompt` | enum | no | Bundled prompt pack (default `bug-hunt`; see `prompt_pack_list`; advisory only; requires `accepts_prompt=true` / `prompt_pack_aware=true` for non-default packs) |
| `api_url` | string | no | Dashboard URL override (localhost only). Defaults to the active local Filigree dashboard. |

Response: `{status, scanner, file_path, file_id, scan_run_id, pid, api_url, api_url_source, sandbox_class, risk_summary, prompt_pack_scope, file_summary, message}`.
`file_summary` is the file's current severity-bucketed findings posture (`{total_findings, open_findings, critical, high, medium, low, info}`) — a posture echo so a "triggered" response is not a vacuous run-state-only green. At trigger time it is the pre-scan posture; poll `scan_status_get` for the updated breakdown once results are ingested.
If the scanner name is a bundled scanner that is not enabled in this project,
the `NOT_FOUND` error includes `details.bundled=true`, `enable_with:
"scanner_enable"`, `cli_enable_command`, and a hint pointing at
`scanner_available_list` / `scanner_enable`.

#### `scan_trigger_batch`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `scanner` | string | yes | Scanner name |
| `file_paths` | string[] | yes | File paths to scan (relative to project root) |
| `prompt` | enum | no | Bundled prompt pack (default `bug-hunt`; see `prompt_pack_list`; advisory only; requires `accepts_prompt=true` / `prompt_pack_aware=true` for non-default packs) |
| `api_url` | string | no | Dashboard URL override (localhost only). Defaults to the active local Filigree dashboard. |

Spawns one scanner process per file and returns per-file `scan_run_id`s plus a
`batch_id` for correlation. The response also echoes `api_url`,
`api_url_source`, and scanner risk/sandbox metadata. Each `per_file` entry
carries a `file_summary` posture echo (severity-bucketed findings for that file).
Same 30s rate-limit applies per scanner+file.

#### `scan_status_get`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `scan_run_id` | string | yes | Scan run ID returned by `scan_trigger` / `scan_trigger_batch` |
| `log_lines` | integer | no | Tail size (1–500, default 50) |

Returns scan status with a live PID check and a tail of the scanner's log, plus
a `file_summary` posture echo — the severity-bucketed findings for the run's
target file(s), reflecting post-ingest state once results are POSTed back.

#### `scan_preview`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `scanner` | string | yes | Scanner name |
| `file_path` | string | yes | File path (relative to project root) |
| `prompt` | enum | no | Bundled prompt pack (default `bug-hunt`; see `prompt_pack_list`; advisory only) |

Returns the exact command that *would* be executed, without spawning anything. Useful for debugging scanner config.

#### `finding_report`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file_path` | string | yes | Project-relative file path (auto-registered if not tracked) |
| `rule_id` | string | yes | Finding identifier / title (e.g. `unused-import`, `sql-injection`) |
| `message` | string | yes | Detailed description |
| `severity` | enum | no | One of the registered severities (default `info`) |
| `line_start` | integer | no | Start line (≥ 1) |
| `line_end` | integer | no | End line (≥ 1) |
| `category` | string | no | Optional grouping category |
| `actor` | string | no | Agent identity for paired observation attribution |
| `create_observation` | boolean | no | Create a linked triage observation (default `false`) |
| `response_detail` | enum | no | `slim` (default) or `full` |

The agent-shortcut path: report a finding without standing up a scanner config.
Auto-registers the file if needed. By default the response is a slim single
finding result with no batch counters. Pass `create_observation=true` to also
create a linked triage observation; full responses then include
`observations_created`, `observations_failed`, `observation_ids`, and
`observation_id` when one was created.

**Workflow:**
1. `scanner_list` — discover registered scanners
2. If none are registered, `scanner_available_list` then `scanner_enable`
3. `prompt_pack_list` — choose an advisory review lens, if needed
4. `scan_trigger` or `scan_trigger_batch` — fire-and-forget, get `scan_run_id`(s)
5. `scan_status_get` — poll for completion / tail logs
6. Check results via `finding_list` / `finding_get` or `GET /api/weft/files/{file_id}/findings`

**Rate limiting:** Repeated triggers for the same scanner+file are rejected within a 30s cooldown window.

**Important:** Results are POSTed to the dashboard API at `/api/scan-results`, the living alias for the recommended Weft generation. Without an explicit `api_url`, scanners use the active local dashboard: ethereal mode reads `.filigree/ephemeral.port`, server mode reads the configured daemon port, and the legacy `http://localhost:8377` default is only used when no active ethereal port has been recorded. Ensure the target is reachable before triggering scans — if unreachable, results are silently lost.

External scanner producers should include a globally unique, non-empty
`scan_run_id` in scan-results POSTs when they want `GET /api/scan-runs`
history. An omitted or empty `scan_run_id` is accepted for fire-and-forget
findings, but those findings are intentionally excluded from scan-run history.

Filigree does not parse SARIF on the scan-results endpoint. Wardline/SARIF
adapters must map SARIF `partialFingerprints` or `fingerprints` into each
posted finding's `fingerprint` field before POSTing. Filigree preserves that
`finding.fingerprint` through readback, promote-by-fingerprint, dedup, stale
cleanup, and reopen-on-regress lifecycle transitions.

**Scanner registration:** Use `scanner_available_list`, `scanner_enable`, and `scanner_disable` from MCP, or `filigree scanner available`, `filigree scanner enable <name>`, and `filigree scanner disable <name>` from the CLI. Bundled scanners call installed `filigree-scanner-*` entrypoints, so projects do not need copied runner scripts. Custom scanners can still be added as TOML files under `.filigree/scanners/`. Custom scanners that declare `{prompt}` in their args template are expected to honor that prompt value themselves.

**Prompt packs:** Use `prompt_pack_list` or `filigree scanner prompts` to list bundled review lenses. Agents can pass `prompt` to `scan_preview`, `scan_trigger`, or `scan_trigger_batch` to focus review without embedding long scanner instructions in their own prompt. Bundled packs include `security`, `pytorch`, `quality-engineering`, `solution-architecture`, `systems-thinking`, `system-interactions`, `python-engineering`, `css`, `javascript`, `typescript`, `react`, `rust`, `go`, `terraform`, `sql`, `comprehensive`, and `major-refactor`. Pack records include `language`, `expected_relative_cost`, `instructions`, and `prompt_pack_scope`; scanner records include `applicable_prompts` so agents do not need to infer language fit from names. The prompt pack only nudges model focus; file access is governed by the scanner CLI sandbox.

For end-to-end issue/file/finding workflows (including dashboard UI and troubleshooting), see [File Traceability Playbook](file-traceability.md).
