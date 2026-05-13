# CLI Reference

Most data commands support `--json` for machine-readable output (`--json` is supported by every issue/observation/file/finding/scanner/planning command but not by setup/diagnostic commands like `install`, `doctor`, and `session-context`, which produce human-only output). The global `--actor` flag sets identity for the audit trail (default: `cli`).

## Contents

- [Setup](#setup)
- [Automation and Server](#automation-and-server)
- [Creating and Updating](#creating-and-updating)
- [Listing and Search](#listing-and-search)
- [Dependencies](#dependencies)
- [Comments and Labels](#comments-and-labels)
- [Atomic Claiming](#atomic-claiming)
- [Batch Operations](#batch-operations)
- [Planning](#planning)
- [Workflow Templates](#workflow-templates)
- [Analytics and Events](#analytics-and-events)
- [Agent Context Notes](#agent-context-notes)
- [Files and Findings](#files-and-findings)
- [Scanners](#scanners)
- [Data Management](#data-management)

## Verb-Noun Aliases (Phase E3)

Every short-form CLI command has a permanent verb-noun alias that matches the corresponding MCP tool name. Both forms appear in `--help` and produce identical output.

| Short form | Verb-noun alias |
|---|---|
| `ready` | `get-ready` |
| `blocked` | `get-blocked` |
| `plan` | `get-plan` |
| `changes` | `get-changes` |
| `critical-path` | `get-critical-path` |
| `transitions` | `get-valid-transitions` |
| `validate` | `validate-issue` |
| `guide` | `get-workflow-guide` |
| `workflow-statuses` | `get-workflow-statuses` |
| `type-info` | `get-type-info` |
| `types` | `list-types` |
| `packs` | `list-packs` |
| `labels` | `list-labels` |
| `taxonomy` | `get-label-taxonomy` |
| `update` | `update-issue` |
| `show` | `get-issue` |
| `list` | `list-issues` |
| `release` | `release-claim` |
| `stale-claims` | `get-stale-claims` |
| `reclaim` | `reclaim-issue` |
| `events` | `get-issue-events` |
| `undo` | `undo-last` |

The short forms are stable — no deprecation cycle.

```bash
filigree --actor bot-1 create "Title"   # Set actor identity
filigree list --json                    # JSON output
filigree --version                      # Show version
```

## Setup

```bash
filigree init                              # Create .filigree/ in current directory
filigree init --prefix=myproject           # Custom ID prefix
filigree init --mode=server                # Initialize in persistent server mode
filigree install                           # Install everything: MCP, instructions, .gitignore
filigree install --claude-code             # Claude Code MCP server only
filigree install --codex                   # OpenAI Codex MCP server only (~/.codex/config.toml, autodiscovery)
filigree install --claude-md               # Inject instructions into CLAUDE.md only
filigree install --agents-md               # Inject instructions into AGENTS.md only
filigree install --gitignore               # Add .filigree/ to .gitignore only
filigree install --hooks                   # Install Claude Code hooks only
filigree install --skills                  # Install Claude Code skills only
filigree install --codex-skills            # Install Codex skills only
filigree install --mode=server             # Switch MCP/hook configuration to server mode
filigree doctor                            # Health check
filigree doctor --fix                      # Auto-fix what's possible
filigree doctor --verbose                  # Show all checks including passed
```

### `init`

Initialize `.filigree/` in the current directory.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--prefix` | string | directory name | ID prefix for issues |
| `--mode` | `ethereal`/`server` | `ethereal` | Installation mode |

### `install`

Install filigree into the current project. With no flags, installs everything: MCP servers, instructions, gitignore, hooks, and skills. With specific flags, installs only the selected components.

| Parameter | Type | Description |
|-----------|------|-------------|
| `--claude-code` | flag | Install MCP config for Claude Code only |
| `--codex` | flag | Install MCP config for Codex only (runtime folder autodiscovery) |
| `--claude-md` | flag | Inject instructions into CLAUDE.md only |
| `--agents-md` | flag | Inject instructions into AGENTS.md only |
| `--gitignore` | flag | Add `.filigree/` to .gitignore only |
| `--hooks` | flag | Install Claude Code hooks only |
| `--skills` | flag | Install Claude Code skills only |
| `--codex-skills` | flag | Install Codex skills only |
| `--mode` | `ethereal`/`server` | Installation mode (`preserve existing`, else `ethereal`) |

### `doctor`

Run health checks on the filigree installation.

| Parameter | Type | Description |
|-----------|------|-------------|
| `--fix` | flag | Auto-fix what's possible |
| `--verbose` | flag | Show all checks including passed |

## Automation and Server

```bash
filigree session-context                    # Print project snapshot for session bootstrap
filigree ensure-dashboard                   # Ensure dashboard process is running/reachable
filigree ensure-dashboard --port 8378       # Override server-mode dashboard port
filigree clean-stale-findings --days 30     # Mark stale unseen findings as fixed
filigree clean-stale-findings --scan-source claude
filigree server start                       # Start daemon
filigree server status                      # Check daemon status
filigree server register .                  # Register current project with daemon
filigree server unregister .                # Unregister project from daemon
filigree server stop                        # Stop daemon
```

### `session-context`

Output a session bootstrap snapshot (ready work, in-progress work, critical path, stats).

### `ensure-dashboard`

Ensure the dashboard is running. In `ethereal` mode this starts/attaches to the project-local process; in `server` mode it checks daemon connectivity.

| Parameter | Type | Description |
|-----------|------|-------------|
| `--port` | integer | Optional dashboard port override (server mode) |

### `clean-stale-findings`

Move stale `unseen_in_latest` findings to `fixed`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `--days` | integer | Mark as fixed if unseen for more than N days (default 30) |
| `--scan-source` | string | Restrict to one scan source |

### `server`

Manage the persistent filigree daemon.

Subcommands:
- `filigree server start [--port N]`
- `filigree server stop`
- `filigree server status`
- `filigree server register [PATH]`
- `filigree server unregister [PATH]`

## Creating and Updating

```bash
filigree create "Fix login bug" --type=bug --priority=1
filigree create "Add search" --type=feature -d "Full-text search" -l backend -l search
filigree update <id> --status=in_progress
filigree update <id> --priority=0 --assignee=alice
filigree update <id> --field severity=high --field component=auth
filigree close <id>
filigree close <id1> <id2> <id3>                # Close multiple at once
filigree close <id> --reason="Fixed in commit abc123"
filigree reopen <id>
filigree reopen <id1> <id2>                     # Reopen multiple at once
filigree undo <id>                          # Undo last reversible action
```

`update --json` returns the full issue projection. Soft workflow enforcement
does not block status changes; missing recommended fields are returned in
`data_warnings[]` and recorded once as `transition_warning` events.
Claim-aware writes use the global `--actor` as the expected holder for assigned
issues. Editing another actor's held issue fails with `CONFLICT` unless the
command has an explicit coordinator override such as `--expected-assignee`.

### `create`

Create a new issue.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `title` | string | required | Issue title (positional) |
| `--type` | string | `task` | Issue type (use `filigree types` to see options) |
| `--priority` | 0-4 | `2` | Priority level (0=critical, 4=backlog) |
| `-d`, `--description` | string | `""` | Issue description |
| `-l`, `--label` | string | — | Label (repeatable) |
| `--parent` | string | — | Parent issue ID |
| `--dep` | string | — | Dependency issue ID (repeatable) |
| `--field` | key=value | — | Custom field (repeatable) |
| `--notes` | string | `""` | Additional notes |

### `update`

Update an existing issue.

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Issue ID (positional) |
| `--status` | string | New status (use `filigree transitions <id>`) |
| `--priority` | 0-4 | New priority |
| `--assignee` | string | New assignee |
| `--title` | string | New title |
| `--description` | string | New description |
| `--notes` | string | New notes |
| `--field` | key=value | Custom field (repeatable) |
| `--design` | string | New design field (shorthand for `--field design=...`) |
| `--parent` | string | New parent issue ID (empty string to clear) |
| `--expected-assignee` | string | Expected current holder for coordinator writes |

### `close`

Close one or more issues. Accepts multiple IDs.

| Parameter | Type | Description |
|-----------|------|-------------|
| `ids` | string... | One or more issue IDs (positional, variadic) |
| `--reason` | string | Close reason |
| `--expected-assignee` | string | Expected current holder for coordinator writes |

When using `--json`, the output includes `succeeded`, `failed`, and
`newly_unblocked`. Closed issues with active critical `must_consider`
annotations include an `annotation_warnings` array. Plain-text close prints the
same warning after the close; V1 warnings are advisory and do not block closure.

### `reopen`

Reopen one or more closed issues, returning each to the last non-done status
before closure. Reopen clears `closed_at` and stale close-only fields such as
`close_reason`. Accepts multiple IDs.

| Parameter | Type | Description |
|-----------|------|-------------|
| `ids` | string... | One or more issue IDs (positional, variadic) |

### `undo`

Undo the most recent reversible action on an issue. Covers status, title, priority, assignee, description, notes, claims, and dependency changes.

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Issue ID (positional) |

## Listing and Search

```bash
filigree ready                              # Unblocked issues, sorted by priority
filigree list --status=open                 # All open-category issues
filigree list --status=in_progress          # All work-in-progress
filigree list --type=bug --priority=0       # Filter by type and priority
filigree list --assignee=bot-1              # Filter by assignee
filigree show <id>                          # Full issue details
filigree search "auth"                      # Search by title/description
filigree blocked                            # Issues waiting on blockers
filigree critical-path                      # Longest dependency chain
```

### `ready`

Show issues that are ready to work on — open, no blockers, sorted by priority.
JSON output stays slim by default. Pass `--include-context` with `--json` to
add `parent_issue_id` and `parent_title` to each item.

| Parameter | Type | Description |
|-----------|------|-------------|
| `--json` | flag | Output `{items, has_more}` |
| `--include-context` | flag | Add parent issue ID/title to JSON items |

### `list`

List issues with optional filters.

| Parameter | Type | Description |
|-----------|------|-------------|
| `--status` | string | Filter by status (or status category: `open`, `in_progress`, `closed`) |
| `--type` | string | Filter by issue type |
| `--priority` | 0-4 | Filter by priority |
| `--assignee` | string | Filter by assignee |
| `--label` | string | Filter by label |
| `--parent` | string | Filter by parent issue ID |
| `--limit` | integer | Max results (default 100) |
| `--offset` | integer | Skip first N results |

### `show`

Show full details for an issue including deps, labels, children, and ready status. File associations are omitted by default (Phase E5); pass `--with-files` to include them.

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Issue ID (positional) |
| `--with-files` | flag | Include file associations (default: off) |

### `search`

Search issues by title and description (uses FTS5 full-text search).

| Parameter | Type | Description |
|-----------|------|-------------|
| `query` | string | Search query (positional) |
| `--limit` | integer | Max results (default 100) |
| `--offset` | integer | Skip first N results |

### `blocked`

Show all blocked issues with their blocker ID lists. JSON output stays slim by
default; pass `--include-blockers` with `--json` to add slim blocker records
under `blockers[]`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `--json` | flag | Output `{items, has_more}` |
| `--include-blockers` | flag | Add blocker issue ID/title/status/priority/type records to JSON items |

### `critical-path`

Show the longest dependency chain among open issues. Useful for identifying what to prioritize.

## Dependencies

```bash
filigree add-dep <issue> <depends-on>       # issue is blocked by depends-on
filigree remove-dep <issue> <depends-on>
filigree blocked                            # Show all blocked issues
```

### `add-dep`

Add a dependency: the first issue depends on (is blocked by) the second.

| Parameter | Type | Description |
|-----------|------|-------------|
| `issue` | string | Issue that is blocked (positional) |
| `depends-on` | string | Issue that blocks (positional) |

### `remove-dep`

Remove a dependency between two issues.

| Parameter | Type | Description |
|-----------|------|-------------|
| `issue` | string | Issue that was blocked (positional) |
| `depends-on` | string | Issue that was blocking (positional) |

## Comments and Labels

```bash
filigree add-comment <id> "Found the root cause"
filigree get-comments <id>
filigree add-label backend <id>
filigree remove-label <id> backend
```

### `add-comment`

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Issue ID (positional) |
| `text` | string | Comment text (positional) |
| `--expected-assignee` | string | Expected current holder for coordinator writes |

### `get-comments`

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Issue ID (positional) |

### `add-label`

**Breaking change in 2.0 E6:** arg order is now `<label> <issue_id>`, matching `batch-add-label`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `label` | string | Label name (positional, first) |
| `id` | string | Issue ID (positional, second) |
| `--expected-assignee` | string | Expected current holder for coordinator writes |

### `remove-label`

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Issue ID (positional) |
| `label` | string | Label name (positional) |
| `--expected-assignee` | string | Expected current holder for coordinator writes |

## Atomic Claiming

Prevents double-work when multiple agents are active. Claiming uses optimistic locking — if another agent already claimed the issue, the operation fails.

```bash
filigree claim <id> --assignee agent-1          # Claim specific issue
filigree claim-next --assignee agent-1          # Claim highest-priority ready issue
filigree claim-next --assignee agent-1 --type=bug --priority-max=1
filigree --actor agent-1 heartbeat-work <id>    # Refresh claim liveness for current holder
filigree stale-claims                           # List abandoned or expired claims
filigree reclaim <id> --assignee agent-2 --expected-assignee agent-1 --reason "missed heartbeat"
filigree release <id> --reason "handoff"        # Release without changing status
filigree --actor agent-1 release-my-claims --label cluster:review-2026-05-14 --dry-run
filigree start-work <id> --assignee agent-1     # Claim + transition to wip in one call
filigree start-next-work --assignee agent-1     # Claim + transition highest-priority ready
```

### `claim`

Atomically claim an issue. Does NOT change status — use `update` to advance through the workflow after claiming.

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Issue ID (positional) |
| `--assignee` | string | Who is claiming (required) |

### `claim-next`

Claim the highest-priority ready issue.

| Parameter | Type | Description |
|-----------|------|-------------|
| `--assignee` | string | Who is claiming (required) |
| `--type` | string | Filter by issue type |
| `--priority-min` | 0-4 | Minimum priority (0=critical) |
| `--priority-max` | 0-4 | Maximum priority |

### `release`

Release a claimed issue by clearing its assignee without changing status. By default this is strict: releasing an
unassigned issue returns a conflict. Use `--if-held` for idempotent cleanup flows; it no-ops when the issue is
already unassigned and only clears a live claim held by `--expected-assignee`, or by the global `--actor` when no
expected assignee is provided.

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Issue ID (positional) |
| `--if-held` | flag | Idempotent release-if-held mode |
| `--expected-assignee` | string | Expected current assignee for `--if-held` coordinator flows |
| `--reason` | string | Audit reason recorded on the release event |

### `release-my-claims`

Bulk-release every live claim held by the global `--actor`. Done-category
issues are skipped because their assignee is audit history, not a live claim.
Use `--dry-run` first, and scope with a session-unique label when cleaning up
scratch or review work.

| Parameter | Type | Description |
|-----------|------|-------------|
| `--label` | string | Restrict to issues carrying this exact label |
| `--label-prefix` | string | Restrict to issues with a label starting with this prefix |
| `--dry-run` | flag | List issues that would be released without changing them |
| `--no-revert-status` | flag | Do not revert wip-category issues back to an open predecessor |
| `--reason` | string | Audit reason recorded on each release event |

### `heartbeat-work`

Refresh claim liveness for a claimed issue. By default the global `--actor` is
treated as the expected holder; coordinators can pass `--expected-assignee`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Issue ID (positional) |
| `--expected-assignee` | string | Expected current assignee |
| `--lease-hours` | integer | Lease duration from this heartbeat (default 48) |

### `stale-claims`

List assigned, non-done issues whose explicit claim lease has expired, plus
legacy assigned issues older than the threshold. Pass `--expires-within-hours`
to also include active leases that are close enough to expiry for proactive
heartbeating.

| Parameter | Type | Description |
|-----------|------|-------------|
| `--stale-after-hours` | integer | Legacy assignment age threshold (default 48) |
| `--expires-within-hours` | integer | Include active explicit leases expiring within this many hours |

### `reclaim`

Safely transfer a claim only if the current assignee still matches the expected
holder. The reason is required and is recorded on the reclaim event.

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Issue ID (positional) |
| `--assignee` | string | New assignee (required) |
| `--expected-assignee` | string | Current assignee expected by the caller (required) |
| `--reason` | string | Why the claim is being reclaimed (required) |
| `--lease-hours` | integer | Lease duration for the new assignee (default 48) |

### `start-work`

Atomically claim an issue AND transition it to its working status in a single call. Backs `FiligreeDB.start_work` with compensating-action rollback — if the transition fails, the claim is released. Returns the full updated issue dict.

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Issue ID (positional) |
| `--assignee` | string | Who is claiming (required) |
| `--target-status` | string | Target wip status (default: unique reachable wip target) |
| `--actor` | string | Audit trail actor (default: assignee) |

### `start-next-work`

Claim AND transition the highest-priority ready issue. Returns `{status: "empty", reason: ...}` when no matching issue exists.

| Parameter | Type | Description |
|-----------|------|-------------|
| `--assignee` | string | Who is claiming (required) |
| `--type` | string | Filter by issue type |
| `--priority-min` | 0-4 | Minimum priority filter |
| `--priority-max` | 0-4 | Maximum priority filter |
| `--target-status` | string | Target wip status (default: unique reachable wip target) |
| `--actor` | string | Audit trail actor (default: assignee) |

## Batch Operations

```bash
filigree batch-update <id1> <id2> --priority=0     # Update multiple issues
filigree batch-close <id1> <id2> --reason="Sprint complete"
filigree batch-add-label security <id1> <id2>      # Add same label to many issues
filigree batch-add-comment "triage complete" <id1> <id2>
```

### `batch-update`

Update multiple issues with the same changes.

| Parameter | Type | Description |
|-----------|------|-------------|
| `ids` | string... | Issue IDs (positional, multiple) |
| `--status` | string | New status |
| `--priority` | 0-4 | New priority |
| `--assignee` | string | New assignee |
| `--field` | key=value | Custom field (repeatable) |
| `--expected-assignee` | string | Expected current holder for coordinator writes |

### `batch-close`

Close multiple issues.

| Parameter | Type | Description |
|-----------|------|-------------|
| `ids` | string... | Issue IDs (positional, multiple) |
| `--reason` | string | Close reason |
| `--expected-assignee` | string | Expected current holder for coordinator writes |

### `batch-add-label`

Add the same label to multiple issues.

| Parameter | Type | Description |
|-----------|------|-------------|
| `label` | string | Label name (positional) |
| `ids` | string... | Issue IDs (positional, multiple) |
| `--expected-assignee` | string | Expected current holder for coordinator writes |

### `batch-add-comment`

Add the same comment to multiple issues.

| Parameter | Type | Description |
|-----------|------|-------------|
| `text` | string | Comment text (positional) |
| `ids` | string... | Issue IDs (positional, multiple) |
| `--expected-assignee` | string | Expected current holder for coordinator writes |

## Planning

```bash
filigree create-plan --file plan.json       # Create from JSON file
cat plan.json | filigree create-plan        # Create from stdin
filigree plan <milestone-id>                # Show plan tree with progress
```

### `create-plan`

Create a full milestone/phase/step hierarchy from JSON. Reads from `--file` if provided, otherwise reads from stdin.

| Parameter | Type | Description |
|-----------|------|-------------|
| `--file` | path | JSON plan file (optional; reads from stdin if omitted) |

Plan JSON structure:

```json
{
  "milestone": {"title": "v1.0 Release"},
  "phases": [
    {
      "title": "Core Implementation",
      "steps": [
        {"title": "Define schema"},
        {"title": "Build CLI", "deps": [0]}
      ]
    }
  ]
}
```

Step deps use indices: integer for same-phase, `"phase_idx.step_idx"` for cross-phase.

### `plan`

Show plan tree with progress for a milestone.

| Parameter | Type | Description |
|-----------|------|-------------|
| `milestone-id` | string | Milestone issue ID (positional) |

## Workflow Templates

```bash
filigree types                              # List all types with status flows
filigree get-template <type>                # Canonical full workflow definition
filigree type-info <type>                   # Compatibility alias for get-template
filigree transitions <id>                   # Valid next statuses for an issue
filigree validate <id>                      # Validate against template
filigree packs                              # List enabled packs
filigree guide <pack>                       # Workflow guide for a pack
filigree explain-status <type> <status>     # Explain a specific status
filigree workflow-statuses                  # All statuses grouped by category
filigree templates                          # List available templates
filigree templates --type=bug               # Show specific template fields
filigree templates reload                   # Reload templates from disk
```

See [Workflow Templates](workflows.md) for details on types, packs, and status workflows.

### `types`

List all registered issue types with their pack and status flow.

### `type-info`

Compatibility alias for `get-template`. Shows the same full workflow definition for an issue type:
pack, statuses, transitions, fields, and enforcement rules.

| Parameter | Type | Description |
|-----------|------|-------------|
| `type` | string | Issue type name (positional) |

### `transitions`

Show valid next statuses for an issue, with readiness indicators and missing field warnings.

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Issue ID (positional) |

### `validate`

Validate an issue against its type template. Returns warnings for missing recommended fields.

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Issue ID (positional) |

### `packs`

List all enabled workflow packs with their types and metadata.

### `guide`

Display the workflow guide for a pack, including status diagram, tips, and common mistakes.

| Parameter | Type | Description |
|-----------|------|-------------|
| `pack` | string | Pack name (positional) |

### `explain-status`

Explain a status within a type's workflow: its category, inbound/outbound transitions, and fields required at that status.

| Parameter | Type | Description |
|-----------|------|-------------|
| `type` | string | Issue type name (positional) |
| `status` | string | Status name (positional) |

### `workflow-statuses`

Show all workflow statuses grouped by category (open, wip, done) from enabled templates.

## Analytics and Events

```bash
filigree stats                              # Counts by status name/category, type, ready/blocked
filigree metrics --days=30                  # Cycle time, lead time, throughput
filigree changes --since 2026-01-01T00:00   # Events since timestamp
filigree changes --since 2026-01-01T00:00 --actor agent-1 --label cluster:review
filigree events <id>                        # Event history for one issue
```

### `stats`

Project statistics: counts by literal status name, template status category,
type, ready, and blocked. JSON includes explicit `status_name_counts` and
`status_category_counts` maps; `by_status` and `by_category` remain for
compatibility.

### `metrics`

Flow metrics for retrospectives and velocity tracking.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--days` | integer | 30 | Lookback window in days |

### `changes`

Events since a timestamp. Used for session resumption.

| Parameter | Type | Description |
|-----------|------|-------------|
| `--since` | ISO timestamp | Start timestamp (required) |
| `--limit` | integer | Max events (default 100) |
| `--after-event-id` | integer | Resume inside a same-timestamp event group |
| `--actor` | string | Only include events written by this actor |
| `--issue-id` | string | Only include events for this issue |
| `--label` | string | Only include events for issues currently carrying this label |
| `--type` | string | Only include events of this event type |
| `--include-heartbeats` | flag | Include heartbeat events; excluded by default |

### `events`

Event history for a specific issue, newest first.

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Issue ID (positional) |
| `--limit` | integer | Max events (default 50) |

## Agent Context Notes

Observations and annotations are both agent-facing context capture tools:
observations are ephemeral scratchpad notes, while annotations are durable
file-anchored notes with provenance and drift detection.

### Observations

Agent scratchpad — fire-and-forget notes that expire after 14 days. Use `list-observations` with `--label=from-observation` after promoting to find resulting issues.

```bash
filigree observe "Possible auth race" --file-path src/auth.py --line 42
filigree list-observations
filigree list-observations --file-path src/auth.py
filigree dismiss-observation <obs-id> --reason "Already fixed"
filigree link-observation <obs-id> <issue-id> --disposition duplicate --reason "Same root cause"
filigree promote-observation <obs-id> --type bug --priority 1
filigree batch-dismiss-observations <id1> <id2> --reason "Stale"
filigree batch-link-observations <issue-id> <id1> <id2> --disposition evidence
filigree promote-observations-to-issue <id1> <id2> --type bug --title "Merged issue"
```

#### `observe`

Record a quick observation note.

| Parameter | Type | Description |
|-----------|------|-------------|
| `summary` | string | Observation summary (positional) |
| `--detail` | string | Extended detail |
| `--file-path` | string | Anchor to source file path |
| `--line` | integer | Line number anchor |
| `--source-issue-id` | string | Link to a related issue |
| `--priority` | 0-4 | Observation priority |

#### `list-observations`

List observations with optional filters. Output: `ListResponse[T]` (`{items, has_more}`).

| Parameter | Type | Description |
|-----------|------|-------------|
| `--limit` | integer | Max results (default 50) |
| `--offset` | integer | Skip first N results |
| `--file-path` | string | Filter by file path |
| `--file-id` | string | Filter by file record ID |
| `--actor` | string | Filter by exact actor |
| `--source-issue-id` | string | Filter by source issue |
| `--priority-min` / `--priority-max` | 0-4 | Filter by priority range |
| `--older-than-hours` | integer | Filter to older observations |

#### `dismiss-observation`

Dismiss an observation (will not generate an issue).

| Parameter | Type | Description |
|-----------|------|-------------|
| `observation-id` | string | Observation ID (positional) |
| `--reason` | string | Dismissal reason |

#### `link-observation`

Link an observation to an existing issue, preserve its evidence snapshot in
`observation_links`, and remove it from the pending queue.

| Parameter | Type | Description |
|-----------|------|-------------|
| `observation-id` | string | Observation ID (positional) |
| `issue-id` | string | Existing issue ID (positional) |
| `--disposition` | enum | `evidence`, `duplicate`, `superseded`, or `related` |
| `--reason` | string | Link reason |

#### `promote-observation`

Promote an observation to a tracked issue.

| Parameter | Type | Description |
|-----------|------|-------------|
| `observation-id` | string | Observation ID (positional) |
| `--type` | string | Issue type for the new issue |
| `--priority` | 0-4 | Priority for the new issue |
| `--title` | string | Override title (default: observation summary) |
| `--description` | string | Override description |

#### `batch-dismiss-observations`

Dismiss multiple observations in one call.

| Parameter | Type | Description |
|-----------|------|-------------|
| `observation-ids` | string... | Observation IDs (positional, multiple) |
| `--reason` | string | Dismissal reason |

#### `batch-link-observations`

Link multiple observations to one existing issue with a shared disposition.

| Parameter | Type | Description |
|-----------|------|-------------|
| `issue-id` | string | Existing issue ID (positional) |
| `observation-ids` | string... | Observation IDs (positional, multiple) |
| `--disposition` | enum | `evidence`, `duplicate`, `superseded`, or `related` |
| `--reason` | string | Link reason |

#### `promote-observations-to-issue`

Promote multiple observations into one issue. The created issue stores all
source IDs in `fields.source_observation_ids` and each observation is linked
as durable evidence.

| Parameter | Type | Description |
|-----------|------|-------------|
| `observation-ids` | string... | Observation IDs (positional, multiple) |
| `--type` | string | Issue type for the new issue |
| `--priority` | 0-4 | Priority override |
| `--title` | string | Override title |
| `--description` | string | Extra description to prepend |

## Files and Findings

Track source files and code-health findings from automated scanners.

```bash
filigree list-files
filigree list-files --language python --min-findings 1
filigree get-file <file-id>
filigree get-file-timeline <file-id>
filigree get-issue-files <issue-id>
filigree add-file-association <file-id> <issue-id> <assoc-type>
filigree register-file src/auth.py --language python
filigree delete-file-record <file-id> --force
filigree list-findings --status open
filigree get-finding <finding-id>
filigree update-finding <finding-id> --status fixed
filigree promote-finding <finding-id> --priority 1
filigree dismiss-finding <finding-id> --reason "False positive"
filigree batch-update-findings <id1> <id2> --status fixed
```

### `list-files`

List tracked files. Output: `ListResponse[T]` (`{items, has_more}`).

| Parameter | Type | Description |
|-----------|------|-------------|
| `--language` | string | Filter by language |
| `--path-prefix` | string | Filter by file path prefix |
| `--min-findings` | integer | Min finding count filter |
| `--has-severity` | string | Filter by finding severity |
| `--scan-source` | string | Filter by scanner name |
| `--sort` | string | Sort field |
| `--direction` | `asc`/`desc` | Sort direction |
| `--limit` | integer | Max results |
| `--offset` | integer | Skip first N results |

### `get-file`

Get details for a single tracked file.

| Parameter | Type | Description |
|-----------|------|-------------|
| `file-id` | string | File record ID (positional) |

### `get-file-timeline`

Get the event timeline for a tracked file.

| Parameter | Type | Description |
|-----------|------|-------------|
| `file-id` | string | File record ID (positional) |
| `--event-type` | string | Filter by event type (`finding`, `association`, `file_metadata_update`, `issue_event`) |
| `--include-issue-events` | flag | Merge events from issues currently associated with the file |
| `--limit` | integer | Max results |
| `--offset` | integer | Skip first N results |

### `get-issue-files`

List files associated with an issue.

| Parameter | Type | Description |
|-----------|------|-------------|
| `issue-id` | string | Issue ID (positional) |

### `add-file-association`

Associate a tracked file with an issue.

| Parameter | Type | Description |
|-----------|------|-------------|
| `file-id` | string | File record ID (positional) |
| `issue-id` | string | Issue ID (positional) |
| `assoc-type` | string | Association type (positional) |

Records the global `--actor` on the association.

### `register-file`

Register a source file in the file inventory.

| Parameter | Type | Description |
|-----------|------|-------------|
| `path` | string | File path (positional) |
| `--language` | string | Language override |
| `--file-type` | string | File type classification |
| `--metadata` | JSON string | Extra metadata |

Records the global `--actor` in `created_by`/`updated_by`; metadata update
timeline events include the same actor.

### `delete-file-record`

Delete a tracked file record. By default, this refuses records that still have issue associations or open findings. Use `--force` to cascade file associations and findings.

| Parameter | Type | Description |
|-----------|------|-------------|
| `file-id` | string | File record ID (positional) |
| `--force` | flag | Cascade associations and open findings |

JSON output echoes the global `--actor` that performed the deletion.

### `list-findings`

List code-health findings. Output: `ListResponse[T]` (`{items, has_more}`).

| Parameter | Type | Description |
|-----------|------|-------------|
| `--file-id` | string | Filter by file |
| `--status` | string | Filter by status |
| `--severity` | string | Filter by severity |
| `--scan-source` | string | Filter by scanner |
| `--limit` | integer | Max results |
| `--offset` | integer | Skip first N results |

### `get-finding`

Get a single finding by ID.

| Parameter | Type | Description |
|-----------|------|-------------|
| `finding-id` | string | Finding ID (positional) |

### `update-finding`

Update a finding's status or linked issue.

| Parameter | Type | Description |
|-----------|------|-------------|
| `finding-id` | string | Finding ID (positional) |
| `--status` | string | New status |
| `--issue-id` | string | Link to issue |

Records the global `--actor` in the finding's `updated_by` field.

### `promote-finding`

Promote a finding to an observation.

| Parameter | Type | Description |
|-----------|------|-------------|
| `finding-id` | string | Finding ID (positional) |
| `--priority` | 0-4 | Priority for the created observation |

### `dismiss-finding`

Dismiss a finding (marks as not worth tracking).

| Parameter | Type | Description |
|-----------|------|-------------|
| `finding-id` | string | Finding ID (positional) |
| `--reason` | string | Dismissal reason |

Records the global `--actor` in the finding's `updated_by` field.

### `batch-update-findings`

Update multiple findings in one call.

| Parameter | Type | Description |
|-----------|------|-------------|
| `finding-ids` | string... | Finding IDs (positional, multiple) |
| `--status` | string | New status (required) |

Records the global `--actor` in each updated finding's `updated_by` field.

### Annotations

Annotations are durable, project-shared file notes with checksum/git/diff
provenance and computed anchor drift. They are file anchored and can link to
issues, files, findings, and observations.

```bash
filigree annotate-file src/auth.py "Keep this invariant in mind" --line 42 --intent warning --critical
filigree annotate-file src/auth.py "Context for phase 2" --link issue:filigree-abc123:must_consider
filigree list-annotations --file src/auth.py --json
filigree get-annotation <annotation-id> --json
filigree resolve-annotation <annotation-id> --reason "Handled"
filigree carry-forward-annotation <annotation-id> --from <old-issue> --to <new-issue> --reason "Still applies"
```

JSON list output uses `{items, has_more, next_offset?}`. `--detail summary` is
the default for list commands; `--detail full` includes provenance, links, and
audit events.

`carry-forward-annotation` requires the annotation to already be linked to
`--from` as `must_consider`; otherwise it fails instead of acknowledging an
unrelated issue.

#### `annotate-file`

| Parameter | Type | Description |
|-----------|------|-------------|
| `file-path` | string | Project-relative file path (positional) |
| `note` | string | Annotation note (positional) |
| `--line` / `--line-end` | integer | 1-based anchor range |
| `--context-summary` | string | What the agent was doing |
| `--intent` | enum | `explanation`, `warning`, `breadcrumb`, `hypothesis`, `decision`, `handoff`, `gotcha` |
| `--critical` | flag | Elevate surfacing and closeout warnings |
| `--link` | string | `target_type:target_id:relationship` |
| `--session-ref` | string | Optional opaque session/run reference |

#### `list-annotations`

| Parameter | Type | Description |
|-----------|------|-------------|
| `--file` / `--file-id` | string | Filter by file |
| `--issue-id` | string | Filter by linked issue |
| `--target-type` / `--target-id` | string | Filter by any V1 link target |
| `--relationship` | string | Filter by link relationship |
| `--intent` / `--status` / `--critical` | mixed | Filter by annotation fields |
| `--anchor-state` | enum | Filter by computed drift state |
| `--detail` | `summary`/`full` | Response detail |
| `--limit` / `--offset` | integer | Pagination |

## Scanners

Trigger and monitor automated code scanners.

```bash
filigree list-scanners
filigree trigger-scan <scanner> <file-path>
filigree trigger-scan-batch <scanner> <file1> <file2>
filigree get-scan-status <scan-run-id>
filigree preview-scan <scanner> <file-path>
filigree report-finding --file finding.json
filigree report-finding --file finding.json --create-observation
cat finding.json | filigree report-finding   # Read from stdin
```

### `list-scanners`

List configured scanners from the `scanners/` directory.

### `trigger-scan`

Trigger a single-file scan.

| Parameter | Type | Description |
|-----------|------|-------------|
| `scanner` | string | Scanner name (positional) |
| `file-path` | string | File to scan (positional) |
| `--api-url` | string | Dashboard URL override |

### `trigger-scan-batch`

Trigger a scanner on multiple files.

| Parameter | Type | Description |
|-----------|------|-------------|
| `scanner` | string | Scanner name (positional) |
| `file-paths` | string... | Files to scan (positional, multiple) |
| `--api-url` | string | Dashboard URL override |

### `get-scan-status`

Check the status of a scan run.

| Parameter | Type | Description |
|-----------|------|-------------|
| `scan-run-id` | string | Scan run ID (positional) |
| `--log-lines` | integer | Number of log lines to include |

### `preview-scan`

Preview the shell command a scanner would run (without executing it).

| Parameter | Type | Description |
|-----------|------|-------------|
| `scanner` | string | Scanner name (positional) |
| `file-path` | string | File to preview (positional) |

### `report-finding`

Ingest a finding in loom-shape JSON format. Reads from stdin by default; `--file` overrides. This agent-shortcut path writes a single finding by default. Use `--create-observation` to also create a linked triage observation; full JSON output then includes `observations_created`, `observations_failed`, `observation_ids`, and `observation_id` when one was created.

| Parameter | Type | Description |
|-----------|------|-------------|
| `--file` | path | JSON finding file (optional; reads from stdin if omitted) |
| `--api-url` | string | Dashboard URL override |
| `--create-observation` | flag | Also create a linked triage observation |

## Data Management

```bash
filigree export backup.jsonl                # Export all data
filigree import backup.jsonl --merge        # Import (skip existing)
filigree archive --days=30                  # Archive old closed issues
filigree archive --days=0 --label=scratch   # Archive closed scratch/review fixtures only
filigree compact --keep=50                  # Compact event history
filigree migrate --from-beads              # Migrate from beads tracker
filigree clean-stale-findings --days=30     # Move stale unseen findings to fixed
filigree dashboard --port=8377              # Launch web UI
filigree dashboard --no-browser            # Launch without opening browser
filigree dashboard --server-mode            # Launch in multi-project daemon mode
```

### End-of-session cleanup

Use one session-unique label, such as `cluster:<session-id>`, on scratch issues
and temporary review work. Generic labels like `scratch` are useful for search,
but they are too broad for final cleanup by themselves.

1. Finish, hand off, or comment on active task-scope work first. Defects found
   inside the current task should become tracked issues or be fixed before the
   task closes, not hidden in expiring observations.
2. Preview live claim cleanup:
   `filigree --actor <agent> release-my-claims --label <session-label> --dry-run`.
   If the preview is correct, repeat without `--dry-run` and include `--reason`.
3. Triage observations with `list-observations --actor <agent>`, then choose
   `promote-observations-to-issue`, `batch-link-observations`, or
   `batch-dismiss-observations` so each pending note is tracked, attached as
   evidence, or intentionally dropped.
4. Triage scan scratch with `list-findings`, `promote-finding`,
   `dismiss-finding`, `batch-update-findings`, or `clean-stale-findings`.
5. Remove temporary file records with `delete-file-record`. Run it without
   `--force` first; only force after associations and open findings are handled.
6. Archive closed scratch with
   `filigree archive --days=0 --label <session-label> --json`. Confirm the
   label is session-unique before archiving, because archive scopes by label,
   not by actor.

### `export`

Export all project data (issues, deps, labels, comments, events) to JSONL.

| Parameter | Type | Description |
|-----------|------|-------------|
| `output` | path | Output file path (positional) |

### `import`

Import project data from JSONL.

| Parameter | Type | Description |
|-----------|------|-------------|
| `input` | path | Input file path (positional) |
| `--merge` | flag | Skip existing records instead of failing |

### `archive`

Archive old closed issues to reduce active issue count.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--days` | integer | 30 | Archive issues closed more than N days ago |
| `--label` | string | none | Only archive closed issues currently carrying this label |

### `compact`

Remove old events for archived issues.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--keep` | integer | 50 | Keep N most recent events per archived issue |

### `migrate`

Migrate issues from another system. Currently supports migrating from the beads issue tracker.

| Parameter | Type | Description |
|-----------|------|-------------|
| `--from-beads` | flag | Migrate from `.beads` database |
| `--beads-db` | path | Path to beads.db (default: `.beads/beads.db`) |

### `dashboard`

Launch the web dashboard.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--port` | integer | 8377 | Port to listen on |
| `--no-browser` | flag | — | Don't auto-open browser |
| `--server-mode` | flag | — | Multi-project server mode (reads server config) |

## Dashboard

```bash
filigree dashboard                    # Opens browser at localhost:8377
filigree dashboard --port 9000        # Custom port
filigree dashboard --no-browser       # Skip auto-open
filigree dashboard --server-mode      # Multi-project server mode
```

### `dashboard`

Launch an interactive web dashboard at `http://localhost:8377`. Features:

| View | Description |
|------|-------------|
| **Kanban** | Three-column board (open/wip/done) with cluster mode grouping by epic |
| **Graph** | Graph v2 dependency map with focus/path exploration and time windows |
| **Files** | File inventory with findings, associations, and timeline drilldown |
| **Health** | Code Health overview (hotspots, severity mix, scan summaries) |
| **Metrics** | Throughput, cycle time, lead time with agent workload chart |
| **Activity** | Chronological event feed across all issues |
| **Workflow** | State machine visualization for any issue type |

**Interactive features:** Inline status transitions, priority/assignee changes, comments, issue creation, claim/release, dependency management, batch operations, keyboard navigation (`?` for shortcuts), filter presets, dark/light theme toggle, auto-refresh with change highlighting.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--port` | int | 8377 | Port to serve on |
| `--no-browser` | flag | false | Don't auto-open browser |
| `--server-mode` | flag | false | Start dashboard in multi-project daemon mode |

Default dashboard mode connects to `.filigree/` in the current directory (`ethereal` mode). In `--server-mode`, the dashboard serves registered projects through the daemon. All write operations record `"dashboard"` as the actor for audit trail.
