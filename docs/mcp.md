# MCP Server Reference

Filigree exposes an MCP (Model Context Protocol) server so AI agents interact natively without parsing CLI output. The server provides 113 tools, 1 resource, and 1 prompt.

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
| `get_issue` | Full issue details with deps, labels, children, ready status |
| `list_issues` | Filter by status, type, priority, parent, assignee |
| `create_issue` | Create with type, priority, deps, labels, fields |
| `update_issue` | Update status, priority, title, assignee, fields |
| `close_issue` | Close with optional reason |
| `reopen_issue` | Reopen a closed issue to the last non-done status before closure |
| `undo_last` | Undo most recent reversible action |

#### Relationship naming

MCP 2.0 public issue payloads use `issue_id` for the issue primary key and
`parent_issue_id` for hierarchy links. Full issue payloads also include
`parent_id` as a compatibility alias with the same value; new callers should
read `parent_issue_id`. Dependency edges use directional names:
`from_issue_id` is the issue that is blocked, and `to_issue_id` is the issue
that blocks it.

#### `get_issue`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `include_transitions` | boolean | no | Include valid next states in response |

#### `list_issues`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `status` | string | no | Filter by exact status name |
| `status_category` | enum | no | Filter by category: `open`, `wip`, `done` |
| `type` | string | no | Filter by issue type |
| `priority` | 0-4 | no | Filter by priority |
| `parent_issue_id` | string | no | Filter by parent issue ID |
| `limit` | integer | no | Max results (default 100) |
| `offset` | integer | no | Skip first N results |

#### `create_issue`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `title` | string | yes | Issue title |
| `type` | string | no | Issue type (default: `task`) |
| `priority` | 0-4 | no | Priority (default: 2) |
| `description` | string | no | Issue description |
| `notes` | string | no | Additional notes |
| `labels` | string[] | no | Labels to attach during creation (no separate `add_label` call needed) |
| `deps` | string[] | no | Dependency issue IDs |
| `parent_issue_id` | string | no | Parent issue ID |
| `fields` | object | no | Custom fields from template schema |
| `actor` | string | no | Agent identity for audit trail |

#### `update_issue`

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

#### `close_issue`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `reason` | string | no | Close reason |
| `fields` | object | no | Extra fields to set while closing (for enforced workflows) |
| `actor` | string | no | Agent identity for audit trail |
| `expected_assignee` | string | no | Override expected holder for coordinator writes |

When an issue has active `critical=true` annotations linked with
`relationship="must_consider"`, `close_issue` still closes the issue but returns
an `annotation_warnings` array. Each warning contains the `annotation_id`,
file anchor, computed `anchor_state`, and suggested follow-up tools.

#### `reopen_issue`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `actor` | string | no | Agent identity for audit trail |

#### `undo_last`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `actor` | string | no | Agent identity for audit trail |

### Ready and Blocked

| Tool | Description |
|------|-------------|
| `get_ready` | Unassigned open-category issues with no blockers, sorted by priority |
| `get_blocked` | Blocked issues with their blocker lists, optionally hydrated with blocker context |
| `get_critical_path` | Longest dependency chain |

`get_ready` returns slim five-key issue items by default. Pass
`include_context=true` to add `parent_issue_id` and `parent_title` to each item.
`get_blocked` returns blocker IDs by default. Pass `include_blockers=true` to
add slim blocker records under `blockers[]` while preserving `blocked_by`.
`get_critical_path` takes no required parameters.

#### `get_ready`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `include_context` | boolean | no | Include parent issue ID/title on each ready item |

#### `get_blocked`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `include_blockers` | boolean | no | Include slim blocker records under `blockers[]` |

### Dependencies

| Tool | Description |
|------|-------------|
| `add_dependency` | Add blocker: `from_issue_id` depends on `to_issue_id` |
| `remove_dependency` | Remove blocker relationship |

#### `add_dependency` / `remove_dependency`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `from_issue_id` | string | yes | Issue that is blocked |
| `to_issue_id` | string | yes | Issue that blocks |
| `actor` | string | no | Agent identity for audit trail |

### Comments and Labels

| Tool | Description |
|------|-------------|
| `add_comment` | Add comment to an issue |
| `get_comments` | Get all comments on an issue |
| `add_label` | Add label to an issue |
| `remove_label` | Remove label from an issue |

#### `add_comment`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `text` | string | yes | Comment text |
| `actor` | string | no | Used as comment author |
| `expected_assignee` | string | no | Override expected holder for coordinator writes |

Returns the updated `PublicIssue`, preserving top-level `comment_id` for
compatibility and adding `comment: {comment_id, author, text, created_at}` so
callers can confirm the exact inserted comment without a follow-up read.

#### `get_comments`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |

#### `add_label` / `remove_label`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `label` | string | yes | Label name |
| `actor` | string | no | Agent identity for claim-aware write safety |
| `expected_assignee` | string | no | Override expected holder for coordinator writes |

### Search

| Tool | Description |
|------|-------------|
| `search_issues` | Search by title and description (FTS5) |
| `get_summary` | Pre-computed project summary (same as `context.md`) |
| `get_stats` | Project statistics with explicit status-name and status-category count maps |

#### `search_issues`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | string | yes | Search query |
| `limit` | integer | no | Max results (default 100) |
| `offset` | integer | no | Skip first N results |

#### `get_stats`

Returns both legacy count maps and explicit aliases:
`status_name_counts` contains literal workflow status names such as `open` or
`in_progress`; `status_category_counts` contains template categories
`open`/`wip`/`done`. `by_status` and `by_category` remain for compatibility.

### Planning

| Tool | Description |
|------|-------------|
| `get_plan` | Milestone plan tree with progress |
| `create_plan` | Create milestone/phase/step hierarchy in one call |
| `add_plan_step` | Add a step to an existing phase |
| `retarget_plan_dependency` | Swap one step dependency for another |
| `move_plan_step` | Move an existing step to another phase |
| `label_plan_tree` | Apply a label to a milestone subtree |

#### `get_plan`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `milestone_id` | string | yes | Milestone issue ID |
| `response_detail` | enum | no | `slim` (default) for compact issue records, `full` for full issue payloads |

Returns the plan tree with progress fields. Slim responses keep milestone,
phase, and step records compact; full responses include full issue payloads
with descriptions, fields, labels, blockers, and timestamps.

Plan-editing operations preserve dependency edges. `move_plan_step` returns a
`warnings[]` entry when active dependencies are carried forward across the move;
use `retarget_plan_dependency` when a moved step's blockers should change.

#### `create_plan`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `milestone` | object | yes | `{title, description?, priority?}` |
| `phases` | array | yes | Array of `{title, description?, priority?, steps}` |
| `actor` | string | no | Agent identity for audit trail |

Step deps within a phase use integer indices. Cross-phase deps use `"phase_idx.step_idx"` format.

### Claiming

| Tool | Description |
|------|-------------|
| `start_work` | Atomically claim and transition an issue into work |
| `start_next_work` | Claim highest-priority ready issue and transition it into work |
| `claim_issue` | Claim only, with optimistic locking |
| `claim_next` | Claim highest-priority ready issue only |
| `release_claim` | Release a claim, optionally idempotently with `if_held` |
| `release_my_claims` | Bulk-release every live claim held by one actor |
| `heartbeat_work` | Refresh claim liveness for active work |
| `get_stale_claims` | List assigned work with expired leases or old legacy assignments |
| `reclaim_issue` | Transfer a stale claim when the expected holder still owns it |

#### `start_work`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `assignee` | string | yes | Who is starting work |
| `target_status` | string | no | Working status override |
| `actor` | string | no | Agent identity (defaults to assignee) |

#### `start_next_work`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `assignee` | string | yes | Who is starting work |
| `type` | string | no | Filter by issue type |
| `priority_min` | 0-4 | no | Minimum priority |
| `priority_max` | 0-4 | no | Maximum priority |
| `target_status` | string | no | Working status override |
| `actor` | string | no | Agent identity (defaults to assignee) |

#### `claim_issue`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `assignee` | string | yes | Who is claiming |
| `actor` | string | no | Agent identity (defaults to assignee) |

#### `claim_next`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `assignee` | string | yes | Who is claiming |
| `type` | string | no | Filter by issue type |
| `priority_min` | 0-4 | no | Minimum priority |
| `priority_max` | 0-4 | no | Maximum priority |
| `actor` | string | no | Agent identity (defaults to assignee) |

#### `release_claim`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `actor` | string | no | Agent identity for audit trail |
| `if_held` | boolean | no | Idempotent release-if-held mode; unassigned issues are returned unchanged |
| `expected_assignee` | string | no | Only release when the current assignee matches this value; defaults to `actor` in `if_held` mode |
| `reason` | string | no | Audit reason recorded on the release event |

#### `release_my_claims`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `actor` | string | yes | Agent identity whose live claims should be released |
| `label` | string | no | Restrict to issues carrying this exact label |
| `label_prefix` | string | no | Restrict to issues with a label starting with this prefix |
| `dry_run` | boolean | no | Return the issues that would be released without changing them |
| `revert_status` | boolean | no | Revert wip-category issues to an open predecessor (default true) |
| `reason` | string | no | Audit reason recorded on each release event |
| `response_detail` | enum | no | `slim` or `full` |

#### `heartbeat_work`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `actor` | string | no | Agent identity for audit trail and default holder check |
| `expected_assignee` | string | no | Only heartbeat when the current assignee matches this value |
| `lease_hours` | integer | no | Lease duration from this heartbeat (default 48) |

#### `get_stale_claims`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `stale_after_hours` | integer | no | Age threshold for legacy assignments without explicit lease metadata (default 48) |
| `expires_within_hours` | integer | no | Also include active explicit leases expiring within this many hours |

Returns a `ListResponse[IssueDict]` containing assigned, non-done issues whose
`claim_expires_at` is in the past, plus legacy assigned rows older than the
threshold. Pass `expires_within_hours` to also surface active leases that are
close enough to expiry for proactive heartbeating.

#### `reclaim_issue`

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
| `batch_update` | Update multiple issues with the same changes |
| `batch_close` | Close multiple with per-item error reporting |
| `batch_add_label` | Add the same label to multiple issues |
| `batch_add_comment` | Add the same comment to multiple issues |
| `batch_dismiss_observations` | Dismiss multiple observations at once |
| `batch_link_observations` | Link multiple observations to one issue with a shared disposition |
| `batch_promote_observations` | Promote multiple observations to separate issues |
| `batch_update_findings` | Update status on multiple scan findings |

All batch tools return the unified `BatchResponse` envelope (`{succeeded, failed, newly_unblocked?}`) and accept an optional `response_detail: "slim" | "full"` (default `"slim"`). In `"slim"` mode `succeeded` is a list of compact records (`SlimIssue` for issue ops, IDs for label/comment/observation/finding ops); in `"full"` mode each batch tool upgrades `succeeded` to the full record type:

| Tool | Slim `succeeded[i]` | Full `succeeded[i]` |
|------|---------------------|---------------------|
| `batch_update`, `batch_close` | `SlimIssue` | `IssueDict` |
| `batch_add_label`, `batch_add_comment` | `issue_id: str` | `IssueDict` |
| `batch_dismiss_observations` | `observation_id: str` | `ObservationDict` (snapshot pre-dismissal) |
| `batch_link_observations` | `ObservationLink` | `ObservationLink` |
| `batch_promote_observations` | `SlimIssue` | `PublicIssue` |
| `batch_update_findings` | `finding_id: str` | `ScanFindingDict` |

#### `batch_update`

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

#### `batch_close`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_ids` | string[] | yes | Issue IDs |
| `reason` | string | no | Close reason |
| `response_detail` | `"slim" \| "full"` | no | Default `"slim"` |
| `actor` | string | no | Agent identity for audit trail |
| `expected_assignee` | string | no | Override expected holder for coordinator writes |

#### `batch_add_label`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_ids` | string[] | yes | Issue IDs |
| `label` | string | yes | Label to add |
| `response_detail` | `"slim" \| "full"` | no | Default `"slim"` |
| `actor` | string | no | Agent identity for audit trail |
| `expected_assignee` | string | no | Override expected holder for coordinator writes |

#### `batch_add_comment`

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
| `list_types` | All registered types with pack info |
| `get_template` | Canonical full workflow definition for a type |
| `get_type_info` | Compatibility alias for `get_template` |
| `get_valid_transitions` | Valid next states with readiness indicators |
| `validate_issue` | Validate against template (warnings for missing fields) |
| `list_packs` | Enabled workflow packs |
| `get_workflow_guide` | Pack documentation |
| `get_workflow_statuses` | Statuses by category (open/wip/done) |
| `get_schema` | Entity ID prefixes and accepted tool families |
| `get_mcp_status` | Read-only MCP server/schema compatibility diagnostic |
| `explain_status` | Status transitions and required fields |
| `reload_templates` | Refresh templates from disk |

`get_schema.entity_id_prefixes.*.accepted_by_tools` is derived from the live MCP
tool registry. The docs headline tool count is pinned by tests against the same
registry so new tools cannot silently drift from the published reference.

See [Workflow Templates](workflows.md#runtime-semantics-contract) for the
runtime contract behind these tools: initial states, status categories,
hard/soft transition enforcement, `data_warnings[]`, close/reopen target
selection, and claim handoff behavior.

#### `get_type_info`

Compatibility alias for `get_template`; returns the same canonical workflow definition.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `type` | string | yes | Issue type name |

#### `get_template`

Canonical workflow-discovery tool for issue types. Returns pack, states,
transitions, initial state, and fields schema.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `type` | string | yes | Issue type name |

#### `get_valid_transitions`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |

Returns `ListResponse[TransitionDetail]` (`{items, has_more}`), with
`has_more=false` because transition sets are finite and unpaginated.

#### `validate_issue`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |

#### `get_workflow_guide`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `pack` | string | yes | Pack name (e.g., `core`, `planning`) |

#### `explain_status`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `type` | string | yes | Issue type name |
| `status` | string | yes | Status name |

#### `get_mcp_status`

No parameters. Returns connector health fields including `status`, `db_initialized`, `schema_compatible`, `installed_schema_version`, `database_schema_version`, `code`, `error`, `guidance`, `filigree_dir`, and `runtime`. The `runtime` object identifies the executing Python binary, resolved binary path, MCP entrypoint, module file, package root, detected venv root, and install context (`venv`, `uv_tool`, or `system_or_unknown`). This tool is safe to call in warm-but-degraded `SCHEMA_MISMATCH` mode.

### Analytics

| Tool | Description |
|------|-------------|
| `get_metrics` | Cycle time, lead time, throughput |
| `get_changes` | Events since a timestamp |
| `get_issue_events` | Event history for one issue |

#### `get_metrics`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `days` | integer | no | Lookback window (default 30) |

#### `get_changes`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `since` | ISO timestamp | yes | Get events after this time |
| `limit` | integer | no | Max events (default 100) |

#### `get_issue_events`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `limit` | integer | no | Max events (default 50) |

### Data Management

| Tool | Description |
|------|-------------|
| `export_jsonl` | Export all data to JSONL |
| `import_jsonl` | Import from JSONL |
| `archive_closed` | Archive old closed issues |
| `compact_events` | Compact event history |

#### End-of-session cleanup

Use one session-unique label, such as `cluster:<session-id>`, on scratch issues
and temporary review work so cleanup can be scoped without sweeping another
agent's artifacts.

1. Finish, hand off, or comment on the active issue before cleanup; task-scope
   defects should become tracked work, not expiring observations.
2. Preview and release live claims with `release_my_claims(actor=..., label=...,
   dry_run=true)`, then repeat with `dry_run=false` and a `reason` once the
   preview is right. Use `label_prefix` only when the prefix is unique enough
   for the session.
3. List pending notes with `list_observations(actor=...)`, then use
   `promote_observations_to_issue`, `batch_link_observations`, or
   `batch_dismiss_observations` so observations are either tracked, attached as
   evidence, or intentionally dropped.
4. Review scan scratch with `list_findings`; use `promote_finding`,
   `dismiss_finding`, or `batch_update_findings` before deleting file records.
5. Remove synthetic file records with `delete_file_record`. Prefer the default
   refusal mode first; use `force=true` only after associated issues/findings
   are handled.
6. Archive closed scratch with `archive_closed(days_old=0, label=...)` after the
   label scope is confirmed. `compact_events` is a separate storage-maintenance
   step for already archived issues.

#### `export_jsonl`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `output_path` | string | yes | File path for JSONL output |

#### `import_jsonl`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `input_path` | string | yes | File path to read JSONL from |
| `merge` | boolean | no | Skip existing records (default false) |

#### `archive_closed`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `days_old` | integer | no | Archive issues closed more than N days ago (default 30) |
| `actor` | string | no | Agent identity for audit trail |
| `label` | string | no | Only archive closed issues currently carrying this label |

#### `compact_events`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `keep_recent` | integer | no | Keep N most recent events per archived issue (default 50) |

### Files and Traceability

| Tool | Description |
|------|-------------|
| `list_files` | List tracked files with filtering, sorting, and pagination |
| `get_file` | Get file detail + associations + findings summary |
| `get_file_timeline` | Get merged file timeline events |
| `get_issue_files` | List files associated with an issue |
| `add_file_association` | Associate file and issue (`bug_in`, `task_for`, `scan_finding`, `mentioned_in`) |
| `register_file` | Register/get file record by project-relative path |
| `delete_file_record` | Delete a file record, refusing associations/open findings unless forced |

#### `list_files`

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

#### `get_file`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file_id` | string | yes | File ID |

Response includes: `file`, `associations`, `recent_findings`, `summary`.

#### `get_file_timeline`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file_id` | string | yes | File ID |
| `limit` | integer | no | Max events (default 50) |
| `offset` | integer | no | Skip first N events |
| `event_type` | enum | no | `finding`, `association`, `file_metadata_update`, `issue_event` |
| `include_issue_events` | boolean | no | Merge events from issues currently associated with the file |

#### `get_issue_files`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |

#### `add_file_association`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file_id` | string | yes | File ID |
| `issue_id` | string | yes | Issue ID |
| `assoc_type` | enum | yes | `bug_in`, `task_for`, `scan_finding`, `mentioned_in` |
| `actor` | string | no | Actor identity recorded on the association |

#### `register_file`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | yes | Project-relative file path |
| `language` | string | no | Optional language hint |
| `file_type` | string | no | Optional file type tag |
| `metadata` | object | no | Optional metadata map |
| `actor` | string | no | Actor identity recorded on the file record or metadata event |

#### `delete_file_record`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file_id` | string | yes | File ID |
| `force` | boolean | no | Cascade associations and open findings (default false) |
| `actor` | string | no | Actor identity echoed in the deletion result |

Finding records include `created_by` and `updated_by`. Finding timeline events
include the relevant actor; `update_finding`, `batch_update_findings`, and
`dismiss_finding` accept `actor` for triage attribution.
`dismiss_finding` defaults to `status="false_positive"` and accepts
`false_positive`, `fixed`, `unseen_in_latest`, or `acknowledged`. A `reason`
is stored on the finding metadata as `dismiss_reason`. File summaries and safe
file deletion treat `fixed` and `false_positive` as terminal; stale
`unseen_in_latest` findings become `fixed` through `clean_stale_findings`.

### Cross-Product Entity Associations

Bind a Filigree issue to an opaque entity identifier from a sibling
product (notably Clarion — see ADR-029). Filigree never parses the
entity-ID grammar; the binding stores opaque strings so the federation
enrich-only rule (`clarion/docs/suite/loom.md` §5) is preserved.

| Tool | Description |
|------|-------------|
| `add_entity_association` | Attach a Clarion entity to a Filigree issue (idempotent on the composite key — re-attach refreshes the hash, preserves original actor) |
| `remove_entity_association` | Remove the binding identified by `(issue_id, entity_id)` |
| `list_entity_associations` | Return the entity bindings attached to an issue (raw rows; drift comparison is the consumer's job per ADR-029 §"Decision 3") |
| `list_associations_by_entity` | Reverse lookup: return every issue in this project bound to a given Clarion entity (the surface Clarion's `issues_for` calls) |

#### `add_entity_association`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Filigree issue ID |
| `entity_id` | string | yes | Opaque Clarion entity ID; not parsed |
| `content_hash` | string | yes | Snapshot of Clarion's current content hash for drift detection at query time |
| `actor` | string | no | Actor identity recorded as `attached_by` on first attach |

#### `remove_entity_association`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Filigree issue ID |
| `entity_id` | string | yes | Clarion entity ID |

#### `list_entity_associations`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Filigree issue ID |

#### `list_associations_by_entity`

Project isolation is by DB file — every row in this query already
belongs to the project hosting this database, so no project filter
is required (or accepted).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `entity_id` | string | yes | Opaque Clarion entity ID; not parsed |

### Agent Context Notes

Observations and annotations are both agent-facing context capture tools:
observations are ephemeral triage notes, while annotations are durable
file-anchored notes with provenance and drift detection.

#### Observations

| Tool | Description |
|------|-------------|
| `observe` | Record a quick scratchpad note, optionally anchored to a file |
| `list_observations` | List active observations with file filters and pagination |
| `dismiss_observation` | Dismiss one observation with audit trail |
| `link_observation` | Link one observation to an existing issue as `evidence`, `duplicate`, `superseded`, or `related` |
| `promote_observation` | Promote one observation to a tracked issue |
| `batch_dismiss_observations` | Dismiss multiple observations in one call |
| `batch_link_observations` | Link multiple observations to one existing issue |
| `batch_promote_observations` | Promote multiple observations in one call |
| `promote_observations_to_issue` | Promote multiple observations into one issue with all source IDs preserved |

#### Annotations

Annotations are durable, project-shared file notes with provenance. They are
not issues, comments, findings, or observations. Every annotation is anchored to
a file, can link to issues/files/findings/observations, and returns computed
anchor drift separately from lifecycle `status`.

List tools return `{items, has_more, next_offset?}`. `response_detail` defaults
to `summary`; pass `full` to include provenance, links, and audit events.

| Tool | Description |
|------|-------------|
| `annotate_file` | Create a file annotation and capture checksum/git/diff provenance |
| `list_annotations` | Filter annotations by file, link target, actor, intent, status, or anchor state |
| `get_annotation` | Get one annotation with full provenance, links, and audit events |
| `update_annotation` | Update note/context/intent/critical/status |
| `resolve_annotation` | Resolve an annotation with audit trail |
| `supersede_annotation` | Supersede one annotation with another |
| `promote_annotation` | Create an issue or observation and add a `promoted_to` link |
| `carry_forward_annotation` | Add a `must_consider` link to a new issue and acknowledge an existing source warning |
| `link_annotation` / `unlink_annotation` | Manage typed target links |
| `get_file_annotations` | List annotations for a file |
| `get_issue_annotations` | List annotations linked to an issue or epic |
| `list_attention_annotations` | List active critical `must_consider` annotations |

##### `annotate_file`

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

`carry_forward_annotation` requires the annotation to already be linked to
`from_target_id` as `must_consider`; otherwise it returns a `VALIDATION` error
instead of acknowledging an unrelated issue.

### Scanning

| Tool | Description |
|------|-------------|
| `list_scanners` | List registered scanners |
| `list_available_scanners` | List bundled scanners that can be enabled |
| `enable_scanner` | Enable a bundled scanner registration |
| `disable_scanner` | Disable a scanner registration |
| `list_prompt_packs` | List bundled scanner review-focus prompt packs |
| `trigger_scan` | Trigger async file scan (single file) |
| `trigger_scan_batch` | Trigger a scanner across multiple files in one call |
| `get_scan_status` | Live status + log tail for a `scan_run_id` |
| `preview_scan` | Preview the command a scan would execute, without spawning a process |
| `report_finding` | Report a single agent-discovered finding, with explicit opt-in paired observation creation |

#### `list_scanners`

No parameters. Returns scanners registered in `.filigree/scanners/*.toml` in
the unified list envelope:
`{items: [{name, description, file_types, accepts_prompt, prompt_pack_aware, prompt_packs_endpoint, applicable_prompts, bundled_name, bundled_match, managed, sandbox_class, sandbox_summary, ...}], has_more: bool}`.
If the list is empty, call `list_available_scanners` to see bundled scanners
that can be enabled.

#### `list_available_scanners`

No parameters. Returns bundled scanners that can be enabled in the current
project, including `command_available`, `command_path`, `enabled`,
`language_focus`, `applicable_prompts`, and the managed TOML `path`.

#### `enable_scanner`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `scanner` | string | yes | Bundled scanner name, e.g. `codex` or `claude` |
| `force` | boolean | no | Replace an existing custom or stale bundled TOML |

Writes the managed `.filigree/scanners/<scanner>.toml` registration for a
bundled scanner. Refuses to overwrite custom TOML unless `force=true`. If the
packaged runner command is not on `PATH`, the response includes
`command_available=false` and a warning with the `uv tool install --upgrade
filigree` remediation.

#### `disable_scanner`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `scanner` | string | yes | Scanner name |
| `force` | boolean | no | Remove a custom TOML that uses a bundled scanner name |

Removes a scanner registration. Custom non-bundled scanner names can be removed
without `force`; bundled scanner names with custom content require `force=true`.

#### `list_prompt_packs`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `language` | string | no | Optional scanner language focus, e.g. `python`; returns language-agnostic packs plus packs for that focus |

Returns bundled scanner prompt packs in the unified list envelope:
`{items: [{name, description, instructions, components, when_to_use, audience, language, expected_relative_cost, prompt_pack_scope}], has_more: bool}`.
Prompt packs are advisory review-focus hints; they do not restrict scanner file
access or reported findings. Some packs are language-specific; prefer a
scanner's `applicable_prompts` field, or call `list_prompt_packs` with the
scanner's `language_focus`, when selecting a pack.

#### `trigger_scan`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `scanner` | string | yes | Scanner name (from list_scanners) |
| `file_path` | string | yes | File path to scan (relative to project root) |
| `prompt` | enum | no | Bundled prompt pack (default `bug-hunt`; see `list_prompt_packs`; advisory only; requires `accepts_prompt=true` / `prompt_pack_aware=true` for non-default packs) |
| `api_url` | string | no | Dashboard URL override (localhost only). Defaults to the active local Filigree dashboard. |

Response: `{status, scanner, file_path, file_id, scan_run_id, pid, api_url, api_url_source, sandbox_class, risk_summary, prompt_pack_scope, message}`.
If the scanner name is a bundled scanner that is not enabled in this project,
the `NOT_FOUND` error includes `details.bundled=true`, `enable_with:
"enable_scanner"`, `cli_enable_command`, and a hint pointing at
`list_available_scanners` / `enable_scanner`.

#### `trigger_scan_batch`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `scanner` | string | yes | Scanner name |
| `file_paths` | string[] | yes | File paths to scan (relative to project root) |
| `prompt` | enum | no | Bundled prompt pack (default `bug-hunt`; see `list_prompt_packs`; advisory only; requires `accepts_prompt=true` / `prompt_pack_aware=true` for non-default packs) |
| `api_url` | string | no | Dashboard URL override (localhost only). Defaults to the active local Filigree dashboard. |

Spawns one scanner process per file and returns per-file `scan_run_id`s plus a
`batch_id` for correlation. The response also echoes `api_url`,
`api_url_source`, and scanner risk/sandbox metadata. Same 30s rate-limit applies
per scanner+file.

#### `get_scan_status`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `scan_run_id` | string | yes | Scan run ID returned by `trigger_scan` / `trigger_scan_batch` |
| `log_lines` | integer | no | Tail size (1–500, default 50) |

Returns scan status with a live PID check and a tail of the scanner's log.

#### `preview_scan`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `scanner` | string | yes | Scanner name |
| `file_path` | string | yes | File path (relative to project root) |
| `prompt` | enum | no | Bundled prompt pack (default `bug-hunt`; see `list_prompt_packs`; advisory only) |

Returns the exact command that *would* be executed, without spawning anything. Useful for debugging scanner config.

#### `report_finding`

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
1. `list_scanners` — discover registered scanners
2. If none are registered, `list_available_scanners` then `enable_scanner`
3. `list_prompt_packs` — choose an advisory review lens, if needed
4. `trigger_scan` or `trigger_scan_batch` — fire-and-forget, get `scan_run_id`(s)
5. `get_scan_status` — poll for completion / tail logs
6. Check results via `list_findings` / `get_finding` or `GET /api/loom/files/{file_id}/findings`

**Rate limiting:** Repeated triggers for the same scanner+file are rejected within a 30s cooldown window.

**Important:** Results are POSTed to the dashboard API at `/api/scan-results`, the living alias for the recommended Loom generation. Without an explicit `api_url`, scanners use the active local dashboard: ethereal mode reads `.filigree/ephemeral.port`, server mode reads the configured daemon port, and the legacy `http://localhost:8377` default is only used when no active ethereal port has been recorded. Ensure the target is reachable before triggering scans — if unreachable, results are silently lost.

**Scanner registration:** Use `list_available_scanners`, `enable_scanner`, and `disable_scanner` from MCP, or `filigree scanner available`, `filigree scanner enable <name>`, and `filigree scanner disable <name>` from the CLI. Bundled scanners call installed `filigree-scanner-*` entrypoints, so projects do not need copied runner scripts. Custom scanners can still be added as TOML files under `.filigree/scanners/`. Custom scanners that declare `{prompt}` in their args template are expected to honor that prompt value themselves.

**Prompt packs:** Use `list_prompt_packs` or `filigree scanner prompts` to list bundled review lenses. Agents can pass `prompt` to `preview_scan`, `trigger_scan`, or `trigger_scan_batch` to focus review without embedding long scanner instructions in their own prompt. Bundled packs include `security`, `pytorch`, `quality-engineering`, `solution-architecture`, `systems-thinking`, `system-interactions`, `python-engineering`, `css`, `javascript`, `typescript`, `react`, `rust`, `go`, `terraform`, `sql`, `comprehensive`, and `major-refactor`. Pack records include `language`, `expected_relative_cost`, `instructions`, and `prompt_pack_scope`; scanner records include `applicable_prompts` so agents do not need to infer language fit from names. The prompt pack only nudges model focus; file access is governed by the scanner CLI sandbox.

For end-to-end issue/file/finding workflows (including dashboard UI and troubleshooting), see [File Traceability Playbook](file-traceability.md).
