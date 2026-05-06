# MCP Server Reference

Filigree exposes an MCP (Model Context Protocol) server so AI agents interact natively without parsing CLI output. The server provides 71 tools, 1 resource, and 1 prompt.

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
| `reopen_issue` | Reopen a closed issue to its initial status |
| `undo_last` | Undo most recent reversible action |

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

#### `close_issue`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `reason` | string | no | Close reason |
| `fields` | object | no | Extra fields to set while closing (for enforced workflows) |
| `actor` | string | no | Agent identity for audit trail |

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
| `get_ready` | Unblocked issues sorted by priority |
| `get_blocked` | Blocked issues with their blocker lists |
| `get_critical_path` | Longest dependency chain |

These tools take no required parameters.

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

#### `get_comments`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |

#### `add_label` / `remove_label`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |
| `label` | string | yes | Label name |

### Search

| Tool | Description |
|------|-------------|
| `search_issues` | Search by title and description (FTS5) |
| `get_summary` | Pre-computed project summary (same as `context.md`) |
| `get_stats` | Project statistics: counts by status, type, ready/blocked |

#### `search_issues`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | string | yes | Search query |
| `limit` | integer | no | Max results (default 100) |
| `offset` | integer | no | Skip first N results |

### Planning

| Tool | Description |
|------|-------------|
| `get_plan` | Milestone plan tree with progress |
| `create_plan` | Create milestone/phase/step hierarchy in one call |

#### `get_plan`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `milestone_id` | string | yes | Milestone issue ID |

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
| `release_claim` | Release back to open |

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

### Batch Operations

| Tool | Description |
|------|-------------|
| `batch_update` | Update multiple issues with the same changes |
| `batch_close` | Close multiple with per-item error reporting |
| `batch_add_label` | Add the same label to multiple issues |
| `batch_add_comment` | Add the same comment to multiple issues |
| `batch_dismiss_observations` | Dismiss multiple observations at once |
| `batch_update_findings` | Update status on multiple scan findings |

All batch tools return the unified `BatchResponse` envelope (`{succeeded, failed, newly_unblocked?}`) and accept an optional `response_detail: "slim" | "full"` (default `"slim"`). In `"slim"` mode `succeeded` is a list of compact records (`SlimIssue` for issue ops, IDs for label/comment/observation/finding ops); in `"full"` mode each batch tool upgrades `succeeded` to the full record type:

| Tool | Slim `succeeded[i]` | Full `succeeded[i]` |
|------|---------------------|---------------------|
| `batch_update`, `batch_close` | `SlimIssue` | `IssueDict` |
| `batch_add_label`, `batch_add_comment` | `issue_id: str` | `IssueDict` |
| `batch_dismiss_observations` | `observation_id: str` | `ObservationDict` (snapshot pre-dismissal) |
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

#### `batch_close`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_ids` | string[] | yes | Issue IDs |
| `reason` | string | no | Close reason |
| `response_detail` | `"slim" \| "full"` | no | Default `"slim"` |
| `actor` | string | no | Agent identity for audit trail |

#### `batch_add_label`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_ids` | string[] | yes | Issue IDs |
| `label` | string | yes | Label to add |
| `response_detail` | `"slim" \| "full"` | no | Default `"slim"` |
| `actor` | string | no | Agent identity for audit trail |

#### `batch_add_comment`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_ids` | string[] | yes | Issue IDs |
| `text` | string | yes | Comment text |
| `response_detail` | `"slim" \| "full"` | no | Default `"slim"` |
| `actor` | string | no | Agent identity for audit trail |

### Templates and Workflow

| Tool | Description |
|------|-------------|
| `list_types` | All registered types with pack info |
| `get_type_info` | Full workflow definition for a type |
| `get_template` | Field schema for an issue type |
| `get_valid_transitions` | Valid next states with readiness indicators |
| `validate_issue` | Validate against template (warnings for missing fields) |
| `list_packs` | Enabled workflow packs |
| `get_workflow_guide` | Pack documentation |
| `get_workflow_statuses` | Statuses by category (open/wip/done) |
| `get_schema` | Entity ID prefixes and accepted tool families |
| `get_mcp_status` | Read-only MCP server/schema compatibility diagnostic |
| `explain_status` | Status transitions and required fields |
| `reload_templates` | Refresh templates from disk |

#### `get_type_info`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `type` | string | yes | Issue type name |

#### `get_template`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `type` | string | yes | Issue type name |

#### `get_valid_transitions`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `issue_id` | string | yes | Issue ID |

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

No parameters. Returns connector health fields including `status`, `db_initialized`, `schema_compatible`, `installed_schema_version`, `database_schema_version`, `code`, `error`, and `guidance`. This tool is safe to call in warm-but-degraded `SCHEMA_MISMATCH` mode.

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

#### `register_file`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | yes | Project-relative file path |
| `language` | string | no | Optional language hint |
| `file_type` | string | no | Optional file type tag |
| `metadata` | object | no | Optional metadata map |

#### `delete_file_record`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file_id` | string | yes | File ID |
| `force` | boolean | no | Cascade associations and open findings (default false) |

### Scanning

| Tool | Description |
|------|-------------|
| `list_scanners` | List registered scanners |
| `trigger_scan` | Trigger async file scan (single file) |
| `trigger_scan_batch` | Trigger a scanner across multiple files in one call |
| `get_scan_status` | Live status + log tail for a `scan_run_id` |
| `preview_scan` | Preview the command a scan would execute, without spawning a process |
| `report_finding` | Report a single agent-discovered finding (no scanner config needed) |

#### `list_scanners`

No parameters. Returns scanners registered in `.filigree/scanners/*.toml` in the unified list envelope: `{items: [{name, description, file_types, ...}], has_more: bool}`.

#### `trigger_scan`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `scanner` | string | yes | Scanner name (from list_scanners) |
| `file_path` | string | yes | File path to scan (relative to project root) |
| `api_url` | string | no | Dashboard URL (default `http://localhost:8377`, localhost only) |

Response: `{status, scanner, file_path, file_id, scan_run_id, pid, message}`

#### `trigger_scan_batch`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `scanner` | string | yes | Scanner name |
| `file_paths` | string[] | yes | File paths to scan (relative to project root) |
| `api_url` | string | no | Dashboard URL where the scanner POSTs results (localhost only) |

Spawns one scanner process per file and returns per-file `scan_run_id`s plus a `batch_id` for correlation. Same 30s rate-limit applies per scanner+file.

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

The agent-shortcut path: report a finding without standing up a scanner config. Auto-registers the file if needed.

**Workflow:**
1. `list_scanners` — discover available scanners
2. `trigger_scan` or `trigger_scan_batch` — fire-and-forget, get `scan_run_id`(s)
3. `get_scan_status` — poll for completion / tail logs
4. Check results via `list_findings` / `get_finding` or `GET /api/loom/files/{file_id}/findings`

**Rate limiting:** Repeated triggers for the same scanner+file are rejected within a 30s cooldown window.

**Important:** Results are POSTed to the dashboard API. Ensure the dashboard is running at the target `api_url` before triggering scans — if unreachable, results are silently lost.

**Scanner registration:** Add TOML files to `.filigree/scanners/`. See `scripts/scanners/*.toml.example` for templates.

For end-to-end issue/file/finding workflows (including dashboard UI and troubleshooting), see [File Traceability Playbook](file-traceability.md).
