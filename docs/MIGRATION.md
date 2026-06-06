# Migrating from Beads to Filigree

Step-by-step run sheet for migrating a project from [beads](https://github.com/steveyegge/beads) (`bd`) to filigree. Designed for both AI agents and humans.

For Filigree version upgrades, including the 2.0.x to 2.1.0 in-place upgrade,
see [UPGRADING.md](UPGRADING.md).

## Background

Beads is a git-backed issue tracker that stores data in `.beads/` directories using a Dolt-powered SQLite database. It syncs issue data through git commits, which can cause merge conflicts and data loss when branches diverge.

Filigree replaces this with a local-first SQLite database (`.filigree/`) that never touches git. Agent access is via MCP tools (71 available) or CLI with `--json` output.

Filigree's `migrate` command imports all beads issues, dependencies, events, labels, and comments in a single operation.

## Prerequisites

- Python 3.11+
- `pip` or `uv` available on PATH
- The target project has a `.beads/` directory with `beads.db` inside it

## Phase 1: Install Filigree

### Option A: From PyPI (recommended)

```bash
pip install "filigree[all]"
```

This installs the CLI (`filigree`), MCP server (`filigree-mcp`), and web dashboard (`filigree-dashboard`).

For a minimal install (CLI only, no MCP or dashboard):

```bash
pip install filigree                    # CLI + MCP server + web dashboard
```

### Option B: With uv

```bash
uv add filigree
```

### Option C: From source

```bash
git clone https://github.com/foundryside-dev/filigree.git
cd filigree
uv sync
```

### Verify installation

```bash
filigree --help
```

Expected: help text listing available commands. If `filigree` is not found, ensure your Python bin directory is on PATH.

## Phase 2: Initialize Filigree in the Target Project

```bash
cd /path/to/your/project
filigree init
```

Expected output:

```
Initialized filigree project with prefix 'your-project'
Created .filigree/config.json
Created .filigree/filigree.db
```

This creates the `.filigree/` directory. Issue IDs will use the format `{prefix}-{10hex}` where the prefix defaults to the directory name.

### Set up integrations

```bash
filigree install
```

This does three things:

1. Writes `.mcp.json` for Claude Code (MCP server config)
2. Injects filigree usage instructions into `CLAUDE.md`
3. Adds `.filigree/` to `.gitignore`

For specific targets only:

```bash
filigree install --claude-code    # Claude Code MCP only
filigree install --codex          # OpenAI Codex only (runtime folder autodiscovery)
filigree install --claude-md      # CLAUDE.md instructions only
filigree install --agents-md      # AGENTS.md instructions only
filigree install --gitignore      # .gitignore entry only
```

### Verify initialization

```bash
filigree stats
```

Expected: all counts at zero. The database is ready.

## Phase 3: Migrate Data from Beads

### 3a. Stop any running beads daemons

Beads runs background daemons that hold database locks. Stop them before migrating.

Check for running daemons:

```bash
cat ~/.beads/registry.json 2>/dev/null
```

If any entries list your project's workspace path, kill them:

```bash
# Replace PID with the actual process IDs from the registry
kill <PID>
```

Or if `bd` is still available:

```bash
bd shutdown 2>/dev/null
```

### 3b. Run the migration

From your project root (where `.beads/` lives):

```bash
filigree migrate --from-beads
```

This reads `.beads/beads.db` by default. To specify a different path:

```bash
filigree migrate --from-beads --beads-db /path/to/beads.db
```

Expected output:

```
Migrated N issues from beads
```

### What gets migrated

| Beads data | Filigree destination |
|------------|---------------------|
| Issues (non-deleted) | Issues table, preserving IDs |
| Status (open/in_progress/closed) | Status field (unknown statuses default to open) |
| Priority (0-4) | Priority field (same scale) |
| Issue type | Type field (defaults to "task") |
| Parent relationships | parent_id field |
| Dependencies | Dependencies table |
| Events | Events table |
| Labels | Labels table |
| Comments | Comments table (deduplicated) |
| Beads-specific columns (design, acceptance_criteria, estimated_minutes, etc.) | JSON `fields` bag |
| Metadata JSON | `fields._beads_metadata` |

### 3c. Verify the migration

```bash
filigree stats                    # Check issue counts match expectations
filigree list                     # Browse migrated issues
filigree ready                    # Confirm ready queue is populated
filigree show <any-issue-id>      # Spot-check a specific issue
```

**Verification gate:** Do not proceed to Phase 4 until you have confirmed:

- [ ] Issue count matches what beads had (minus deleted issues)
- [ ] At least one issue's details look correct when viewed with `show`
- [ ] Dependencies are intact (check a known blocked issue)

## Phase 4: Remove Beads from the Project

### 4a. Remove the .beads directory from the project

```bash
cd /path/to/your/project
rm -rf .beads/
```

### 4b. Remove .beads from git tracking (if committed)

If `.beads/` was tracked in git:

```bash
# Add to .gitignore (filigree install already handles .filigree/)
echo ".beads/" >> .gitignore

# Remove from git index without deleting local files (already deleted above)
git rm -r --cached .beads/ 2>/dev/null

# Commit the removal
git add .gitignore
git commit -m "Remove beads tracking data, migrated to filigree"
```

### 4c. Remove beads hooks from the project

If your project has a `.beads-hooks/` directory:

```bash
rm -rf .beads-hooks/
```

Check for beads references in existing git hooks:

```bash
grep -r "beads\|bd " .git/hooks/ 2>/dev/null
```

If any hooks reference beads, edit them to remove those lines.

## Phase 5: Remove Beads from the System (Global Cleanup)

This phase removes beads entirely from your machine. Only do this after all projects have been migrated.

### 5a. Remove beads CLI installations

Beads can be installed via multiple methods. Remove all that apply:

**Go binary:**

```bash
rm -f ~/.local/bin/bd
# Also check GOPATH:
rm -f "$(go env GOPATH 2>/dev/null)/bin/bd" 2>/dev/null
```

**npm global package:**

```bash
npm uninstall -g @beads/bd
```

**Homebrew (macOS):**

```bash
brew uninstall beads 2>/dev/null
```

**Verify removal:**

```bash
which bd
```

Expected: no output or "not found".

### 5b. Remove the beads Claude Code plugin

```bash
rm -rf ~/.claude/plugins/marketplaces/beads-marketplace/
rm -rf ~/.claude/plugins/cache/beads-marketplace/
```

Restart any active Claude Code sessions for this to take effect. The beads skills, commands, and hooks will no longer load.

### 5c. Remove the global beads home directory

```bash
rm -rf ~/.beads/
```

This removes the global database, registry, lock files, and metadata.

### 5d. Remove .beads directories from other projects

List all remaining beads project directories:

```bash
find ~ -maxdepth 3 -name ".beads" -type d 2>/dev/null
```

For each project listed, decide whether to:

- **Migrate first** (go back to Phase 2 for that project), or
- **Remove without migrating** (if the data is not needed)

```bash
rm -rf /path/to/project/.beads/
```

### 5e. Check for stale references

Look for remaining beads references in shell config:

```bash
grep -r "beads\|bd " ~/.bashrc ~/.zshrc ~/.profile ~/.bash_profile 2>/dev/null
```

Check for cron jobs or systemd services:

```bash
crontab -l 2>/dev/null | grep -i beads
```

Remove any aliases, PATH entries, or scheduled tasks that reference beads.

## Phase 6: Post-Migration Verification

Run a final check to confirm the migration is complete:

```bash
# Filigree is working
filigree stats
filigree ready

# Beads is gone
which bd                                            # Should return nothing
ls ~/.beads/ 2>/dev/null                            # Should fail
find ~ -maxdepth 3 -name ".beads" -type d 2>/dev/null  # Should return nothing

# MCP server responds (if installed with [mcp] or [all])
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"capabilities":{}}}' | filigree-mcp 2>/dev/null | head -c 200
```

## Quick Reference: Beads to Filigree Command Mapping

| Beads (`bd`) | Filigree | Notes |
|-------------|----------|-------|
| `bd init` | `filigree init` | |
| `bd ready` | `filigree ready` | |
| `bd create "Title" -p 1` | `filigree create "Title" --priority=1` | |
| `bd update <id> --claim` | `filigree claim <id> --assignee <name>` | Separate claim command |
| `bd update <id> --status in_progress` | `filigree update <id> --status=in_progress` | |
| `bd close <id>` | `filigree close <id>` | |
| `bd show <id>` | `filigree show <id>` | |
| `bd list` | `filigree list` | |
| `bd dep add <child> <parent>` | `filigree add-dep <child> <parent>` | |
| `bd dep remove <child> <parent>` | `filigree remove-dep <child> <parent>` | |
| `bd search "query"` | `filigree search "query"` | FTS5 search |
| `bd blocked` | `filigree blocked` | |
| `bd stats` | `filigree stats` | |
| `bd compact` | `filigree archive` | Different mechanism |
| `bd sync` | *(not needed)* | Filigree is local-only |
| `bd prime` | *(not needed)* | Context auto-regenerated on mutations |

## Filigree Features Not in Beads

After migration, these filigree features are available:

- **MCP tools** — 53 native tools for agent interaction (no CLI parsing)
- **Workflow templates** — 24 issue types with enforced state machines
- **Atomic claiming** — `filigree claim-next` with optimistic locking
- **Milestone planning** — `filigree create-plan` for milestone/phase/step hierarchies
- **Critical path** — `filigree critical-path` shows the longest dependency chain
- **Web dashboard** — `filigree dashboard` at localhost:8377
- **Session resumption** — `filigree changes --since <timestamp>` for event replay

## Troubleshooting

### "Beads DB not found"

The migration expects `.beads/beads.db` in the current directory. Use `--beads-db` to specify the full path:

```bash
filigree migrate --from-beads --beads-db ~/project/.beads/beads.db
```

### "database is locked"

A beads daemon is still running. Check `~/.beads/registry.json` for active PIDs and kill them (Phase 3a).

### Migration count is zero

The beads database may be empty or all issues may be soft-deleted (`deleted_at IS NOT NULL`). Check directly:

```bash
sqlite3 .beads/beads.db "SELECT count(*) FROM issues WHERE deleted_at IS NULL"
```

### Duplicate IDs on re-run

The migration is safe to re-run. Filigree uses `INSERT OR IGNORE` semantics — existing issues are skipped, not duplicated. Comments are deduplicated by content and author.
