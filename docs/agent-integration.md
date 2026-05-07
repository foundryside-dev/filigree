# Agent Integration

Filigree is built for AI coding agents. This guide covers how foreground agents, background subagents, and multi-agent teams interact with filigree 2.0.

## Foreground Agents

Foreground agents (Claude Code, Codex) use the **MCP server** directly — see the [MCP Server Reference](mcp.md) for the current live tool count and full read/write surface.

```bash
filigree install --claude-code    # Set up MCP for Claude Code
filigree install --codex          # Set up MCP for Codex via runtime autodiscovery
```

Once installed, agents call MCP tools like `get_ready`, `start_work`, and `close_issue` natively. See [MCP Server Reference](mcp.md) for the full tool list.

## Background Subagents

Background subagents use the **CLI with `--json`** for structured output:

```bash
filigree --actor sub-agent-3 start-next-work --assignee sub-agent-3 --json
filigree --actor sub-agent-3 close <issue-id> --json
```

The `--json` flag returns machine-readable responses in the unified 2.0 envelopes (see "Response Shapes" below). The `--actor` flag sets the identity in the audit trail so you can track which agent performed each action.

## The Agent Workflow Loop

The recommended pattern for agents working with filigree 2.0:

1. **Orient** — read `filigree://context` resource for project state
2. **Find work** — `get_ready` to find unblocked work sorted by priority
3. **Start** — `start_work` (specific issue) or `start_next_work` (highest-priority ready) atomically claims and transitions to the issue type's reachable working status in one step
4. **Work** — do the task, `add_comment` to log progress
5. **Close** — `close_issue` when done (response includes newly-unblocked items)
6. **Repeat** — loop back to step 2

The atomic primitives `claim_issue` / `claim_next` still exist for niche use (reserve without transitioning), but `start_work` / `start_next_work` are the usual path in 2.0.

## Response Shapes

All MCP tools and CLI `--json` output use the unified 2.0 envelopes:

- **Batch ops** return `{succeeded: [...], failed: [{id, error, code}, ...], newly_unblocked?: [...]}`. `failed` is always present (empty list if none); `newly_unblocked` is omitted when the op cannot unblock. Pass `response_detail="full"` (MCP) or `--detail=full` (CLI) to get full records back instead of slim summaries.
- **List ops** return `{items: [...], has_more: bool, next_offset?: int}`. `has_more` is always present; `next_offset` appears only when there is a next page.
- **Errors** return `{error: str, code: ErrorCode, details?: dict}` where `code` is one of: `VALIDATION`, `NOT_FOUND`, `CONFLICT`, `INVALID_TRANSITION`, `PERMISSION`, `NOT_INITIALIZED`, `IO`, `INVALID_API_URL`, `STOP_FAILED`, `SCHEMA_MISMATCH`, `INTERNAL`.

The issue ID is always exposed as `issue_id` (in MCP inputs, response payloads, and CLI JSON). Status is always `status`; "state" was retired as a user-facing word in 2.0.

## Schema-Mismatch (Warm-but-Degraded MCP)

When the installed `filigree` is older than the project's database, the MCP server still launches in warm-but-degraded mode. Most tool calls return an `ErrorResponse` with `code: SCHEMA_MISMATCH` and upgrade guidance; `get_mcp_status` remains available as a safe read-only diagnostic. Surface that message to the user — do not retry. The fix is `uv tool install --upgrade filigree` (or whatever installed it).

## Session Resumption

When an agent resumes after downtime, it can catch up on what happened:

```bash
filigree changes --since 2026-02-14T10:00:00 --json
```

Via MCP:

```
get_changes(since="2026-02-14T10:00:00")
```

Returns all events since the timestamp — status changes, new issues, closed items, dependency changes. The agent can reconstruct what happened while it was offline and adjust its plan accordingly.

## Multi-Agent Coordination

### Atomic Start

When multiple agents are active, **`start_work` prevents double-work**. It uses optimistic locking on `assignee` *and* atomically advances the status — both land or neither does. If another agent already claimed the issue, the operation fails with `code: CONFLICT` rather than silently overwriting.

```bash
# Agent 1 starts successfully
filigree --actor agent-1 start-next-work --assignee agent-1
# Returns: {"issue_id": "proj-a3f9b2e1c0", "title": "Fix auth bug", "status": "in_progress", ...}

# Agent 2 tries the same issue — fails
filigree --actor agent-2 start-work proj-a3f9b2e1c0 --assignee agent-2
# Returns: {"error": "...", "code": "CONFLICT", "details": {"current_assignee": "agent-1"}}
# Exit code 4
```

Via MCP:

```
start_work(issue_id="...", assignee="agent-1")            # Claim + transition atomically
start_next_work(assignee="agent-1", priority_max=1)       # Highest-priority ready, with filters
claim_issue(issue_id="...", assignee="agent-2")           # Niche: reserve without transitioning
release_claim(issue_id="...")                             # Clear assignee without changing status
release_claim(issue_id="...", actor="agent-1", if_held=True)  # No-op unless agent-1 holds the claim
```

### Tie-Break Ordering

`start_next_work` (and the underlying `claim_next`) selects the next issue by:

1. `priority` ascending (0 = critical first)
2. `created_at` ascending (oldest first within a priority tier)
3. `issue_id` ascending (deterministic tie-break)

### Filtering by Type or Priority

Agents can specialise by claiming only certain types or priority ranges:

```bash
filigree --actor bug-fixer start-next-work --assignee bug-fixer --type=bug
filigree --actor critical-agent start-next-work --assignee critical-agent --priority-max=1
```

## Audit Trail

Every mutation records an **actor**. The `--actor` flag (CLI) or `actor` parameter (MCP) sets who performed the action:

```bash
filigree --actor agent-alpha create "Fix auth"
filigree --actor agent-beta close proj-a3f9b2e1c0
```

Via MCP, every write tool accepts an `actor` parameter:

```
create_issue(title="Fix auth", actor="agent-alpha")
close_issue(issue_id="proj-a3f9b2e1c0", actor="agent-beta")
```

Event history is queryable per-issue or globally:

```bash
filigree events <issue-id>                  # Per-issue history
filigree changes --since 2026-02-14T10:00   # Global event stream
```

## Pre-Computed Context

Filigree generates a `context.md` file on every mutation, stored at `.filigree/context.md`. This file contains:

- Project vitals (prefix, enabled packs, issue counts)
- Ready work queue (unblocked, sorted by priority)
- Blocked issues with their blocker details
- Recent activity

Agents read this via the `filigree://context` MCP resource or `get_summary` tool at session start. Because it's pre-computed, there's no query overhead — the agent gets instant orientation.

## Exit Codes (CLI)

Standardised in 2.0 so automated callers can branch on retryability:

| Code | Meaning |
|---|---|
| 0 | success (including empty results) |
| 1 | operational error (`PERMISSION`, `INVALID_TRANSITION`, `IO`, `NOT_FOUND`, `INVALID_API_URL`, `STOP_FAILED`) |
| 2 | usage / validation error (`VALIDATION`) |
| 3 | not initialized / schema mismatch (`NOT_INITIALIZED`, `SCHEMA_MISMATCH`) |
| 4 | contention / conflict (`CONFLICT`) — safe to retry |

## Example: Multi-Agent Setup

A typical multi-agent setup with filigree:

```
Team Lead (foreground, MCP)
├── Reads context.md at session start
├── Creates and prioritises issues
├── Monitors progress via get_stats
│
Worker Agent 1 (background, CLI --json)
├── start-next-work --assignee worker-1 --type=task
├── Works on claimed issue
├── close when done, start-next-work again
│
Worker Agent 2 (background, CLI --json)
├── start-next-work --assignee worker-2 --type=bug
├── Specialises in bug fixes
├── Uses add-comment to log investigation progress
```

Each agent uses a distinct `--actor` identity, starts work atomically, and the event stream provides full visibility into who did what and when.
