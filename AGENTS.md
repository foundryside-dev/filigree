1. This project uses UV like millions of other projects. Use uv run rather than trying literally nothing and then saying its broken.

<!-- filigree:instructions:v2.0.0:131abd53 -->
## Filigree Issue Tracker

Use `filigree` for all task tracking in this project. Data lives in `.filigree/`.

Filigree is a component of the Loom federation. The HTTP `loom` generation at `/api/loom/*` is the stable contract; classic at `/api/v1/*` is frozen. See ADR-002.

### If you see a `ForeignDatabaseError`

Filigree refuses to open an ancestor project's database when it detects that
the current directory is inside a git repo with no local `.filigree.conf`.
The error message tells you exactly what to do (usually `filigree init` in
the current project, then restart MCP). Do not work around it by `cd`-ing
upward unless that was the actual intent.

### MCP Tools (Preferred)

When MCP is configured, prefer `mcp__filigree__*` tools over CLI commands — they're
faster and return structured data. Key tools:

- `get_ready` / `get_blocked` — find available work
- `get_issue` / `list_issues` / `search_issues` — read issues
- `create_issue` / `update_issue` / `close_issue` — manage issues
- `start_work` / `start_next_work` — atomically claim and transition to in-progress (the usual way to pick up work in 2.0)
- `claim_issue` / `claim_next` — atomic claim only, no transition (niche; prefer `start_work`)
- `add_comment` / `add_label` — metadata
- `list_labels` / `get_label_taxonomy` — discover labels and reserved namespaces
- `create_plan` / `get_plan` — milestone planning
- `get_stats` / `get_metrics` — project health
- `get_mcp_status` — read-only connector/schema compatibility diagnostic
- `get_valid_transitions` — workflow navigation
- `observe` / `list_observations` / `dismiss_observation` / `promote_observation` — agent scratchpad
- `trigger_scan` / `trigger_scan_batch` / `get_scan_status` / `preview_scan` / `list_scanners` — automated code scanning
- `get_finding` / `list_findings` / `update_finding` / `batch_update_findings` — scan finding triage
- `promote_finding` / `dismiss_finding` — finding lifecycle (promote to issue or dismiss)

Observations are fire-and-forget notes that expire after 14 days. Use `list_issues --label=from-observation` to find promoted observations.

**Observations are ambient.** While doing other work, use `observe` whenever you
notice something worth noting — a code smell, a potential bug, a missing test, a
design concern. Don't stop what you're doing; just fire off the observation and
carry on. They're ideal for "I don't have time to investigate this right now, but
I want to come back to it." Include `file_path` and `line` when relevant so the
observation is anchored to code. At session end, skim `list_observations` and
either `dismiss_observation` (not worth tracking) or `promote_observation`
(deserves an issue) for anything that's accumulated.

Fall back to CLI (`filigree <command>`) when MCP is unavailable.

### Response shapes (for `--json` and MCP)

Filigree 2.0 unifies response envelopes across MCP and CLI:

- **Batch ops** return `{succeeded: [...], failed: [{id, error, code}, ...], newly_unblocked?: [...]}`. `failed` is always present (empty list if none); `newly_unblocked` is omitted when the op cannot unblock. Pass `response_detail="full"` (MCP) or `--detail=full` (CLI) to get full records back instead of slim summaries.
- **List ops** return `{items: [...], has_more: bool, next_offset?: int}`. `has_more` is always present; `next_offset` appears only when there is a next page.
- **Errors** return `{error: str, code: ErrorCode, details?: dict}` where `code` is one of: `VALIDATION`, `NOT_FOUND`, `CONFLICT`, `INVALID_TRANSITION`, `PERMISSION`, `NOT_INITIALIZED`, `IO`, `INVALID_API_URL`, `STOP_FAILED`, `SCHEMA_MISMATCH`, `INTERNAL`.

### Schema-mismatch (warm-but-degraded MCP)

When the installed `filigree` is older than the project's database, the MCP server still launches but most tool calls return an `ErrorResponse` with `code: SCHEMA_MISMATCH` and upgrade guidance. `get_mcp_status` remains available as a safe read-only diagnostic. Surface that message to the user — do not retry. The fix is `uv tool install --upgrade filigree` (or whatever installed it).

### CLI Quick Reference

```bash
# Finding work
filigree ready                              # Show issues ready to work (no blockers)
filigree list --status=open                 # All open issues
filigree list --status=in_progress          # Active work
filigree list --label=bug --priority=1      # Filter bugs by numeric priority
filigree list --label-prefix=cluster:       # Filter by label namespace prefix
filigree list --not-label=wontfix           # Exclude issues with label
filigree show <id>                          # Detailed issue view
filigree show <id> --with-files             # Include file associations (off by default)

# Creating & updating
filigree create "Title" --type=task --priority=2          # New issue
filigree update <id> --status=<status>                   # Update status (free-form; prefer `start-work` for open→in_progress)
filigree close <id>                                      # Mark complete
filigree close <id> --reason="explanation"               # Close with reason

# Dependencies
filigree add-dep <issue> <depends-on>       # Add dependency
filigree remove-dep <issue> <depends-on>    # Remove dependency
filigree blocked                            # Show blocked issues

# Comments & labels
filigree add-comment <id> "text"            # Add comment
filigree get-comments <id>                  # List comments
filigree add-label <label> <id>             # Add label
filigree remove-label <id> <label>          # Remove label
filigree labels                             # List all labels by namespace
filigree taxonomy                           # Show reserved namespaces and vocabulary

# Workflow templates
filigree types                              # List registered types with status flows
filigree type-info <type>                   # Full workflow definition for a type
filigree transitions <id>                   # Valid next statuses for an issue
filigree workflow-statuses                  # All statuses by category from enabled templates
filigree explain-status <type> <status>     # Explain a status's transitions and required fields
filigree packs                              # List enabled workflow packs
filigree validate <id>                      # Validate issue against template
filigree guide <pack>                       # Display workflow guide for a pack

# Atomic claiming
filigree claim <id> --assignee <name>            # Claim issue (optimistic lock)
filigree claim-next --assignee <name>            # Claim highest-priority ready issue
filigree start-work <id> --assignee <name>       # Claim + transition to in_progress
filigree start-next-work --assignee <name>       # Claim-next + transition to in_progress

# Batch operations
filigree batch-update <ids...> --priority=0      # Update multiple issues
filigree batch-close <ids...>                    # Close multiple with error reporting

# Planning
filigree create-plan --file plan.json            # Create milestone/phase/step hierarchy

# Event history
filigree changes --since 2026-01-01T00:00:00    # Events since timestamp
filigree events <id>                             # Event history for issue

# Observations (agent scratchpad)
filigree observe "note" --file=src/foo.py --line=42      # Fire-and-forget note
filigree list-observations                               # List active observations
filigree dismiss-observation <id>                        # Drop a single observation
filigree promote-observation <id>                        # Promote to a tracked issue
filigree batch-dismiss-observations <ids...>             # Drop several at once

# Files
filigree list-files                                      # List tracked file records
filigree get-file <file_id>                              # File detail with associations
filigree get-file-timeline <file_id>                     # Per-file event timeline
filigree register-file <path>                            # Register a file record
filigree add-file-association <file_id> <issue_id>       # Link file to issue

# Findings (scan-result triage)
filigree list-findings                                   # List scan findings
filigree get-finding <id>                                # Finding detail
filigree update-finding <id> --status=...                # Update finding status
filigree promote-finding <id>                            # Promote finding to issue
filigree dismiss-finding <id>                            # Dismiss finding
filigree batch-update-findings <ids...> --status=...     # Update many at once

# Scanners
filigree list-scanners                                   # Registered scanners
filigree trigger-scan <scanner>                          # Run a scanner
filigree trigger-scan-batch <scanners...>                # Run several scanners
filigree preview-scan <scanner>                          # Dry-run a scanner
filigree get-scan-status <scan_id>                       # Scan progress / results
filigree report-finding ...                              # Report a finding from a scanner

# Most data commands support --json; --actor is global on every command.
# (`install`, `doctor`, `session-context` etc. are human-output only.)
filigree --actor bot-1 create "Title"            # Specify actor identity
filigree list --json                             # Machine-readable output

# Project health
filigree stats                              # Project statistics
filigree search "query"                     # Search issues
filigree doctor                             # Health check
```

Every short-form CLI command (e.g. `ready`, `labels`, `update`) has a permanent
verb-noun alias matching the MCP tool name (`get-ready`, `list-labels`,
`update-issue`). Both forms are stable — pick whichever reads better.

### File Records & Scan Findings (API)

The dashboard exposes REST endpoints for file tracking and scan result ingestion.
Use `GET /api/files/_schema` for available endpoints and valid field values.

API generations: `loom` (`/api/loom/*`) is the stable 2.0 federation contract;
`classic` (`/api/v1/*`) is frozen but supported. The un-prefixed living surface
(`/api/<endpoint>`) aliases the recommended generation (`loom` as of 2.0). New
emitters should target `loom` or the living surface; `classic` exists for
existing integrations only. See ADR-002 and `docs/federation/contracts.md`.

Key endpoints:
- `GET /api/files/_schema` — Discovery: valid enums, endpoint catalog
- `POST /api/loom/scan-results` (or `/api/scan-results`) — Ingest scan results (loom envelope)
- `POST /api/v1/scan-results` — Same intake, classic frozen response shape
- `GET /api/loom/files` (or `/api/files`) — List tracked files with filtering and sorting
- `GET /api/loom/files/{file_id}` — File detail with associations and findings summary
- `GET /api/loom/files/{file_id}/findings` — Findings for a specific file

### Workflow
1. `filigree ready` to find available work
2. `filigree show <id>` to review details
3. `filigree transitions <id>` to see valid status transitions
4. `filigree start-work <issue-id> --assignee <name>` to atomically claim and transition to in-progress (or `filigree start-next-work --assignee <name>` to skip steps 1–3 and grab the highest-priority ready issue)
5. Do the work, commit code
6. `filigree close <id>` when done

### Session Start
When beginning a new session, run `filigree session-context` to load the project
snapshot (ready work, in-progress items, critical path). This provides the
context needed to pick up where the previous session left off.

### Priority Scale
- P0: Critical (drop everything)
- P1: High (do next)
- P2: Medium (default)
- P3: Low
- P4: Backlog
<!-- /filigree:instructions -->
