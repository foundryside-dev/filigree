# Architecture

Filigree's internal architecture: directory layout, database schema, source modules, and key design decisions.

## Contents

- [`.filigree/` Directory](#filigree-directory)
- [Source Layout](#source-layout)
- [Database Schema](#database-schema)
- [Key Design Decisions](#key-design-decisions)

## `.filigree/` Directory

Every filigree project has a `.filigree/` directory at its root, discovered by walking up the filesystem (like `.git/`):

```
.filigree/
  config.json    # Project config: prefix, version, mode, enabled_packs
  filigree.db    # SQLite database (WAL mode)
  context.md     # Auto-generated project summary, refreshed on every mutation
  scanners/      # Scanner registry (*.toml), optional
  ephemeral.pid  # Runtime PID metadata in ethereal mode, optional
  templates/     # Project-local workflow template overrides (optional)
  packs/         # Installed workflow packs (optional)
```

### `config.json`

```json
{
  "prefix": "myproj",
  "version": 1,
  "mode": "ethereal",
  "enabled_packs": ["core", "planning"]
}
```

- **prefix** — used in issue IDs (`{prefix}-{10hex}`, e.g., `myproj-a3f9b2e1c0`)
- **version** — config format version
- **mode** — installation mode (`ethereal` or `server`)
- **enabled_packs** — which workflow packs are active

## Source Layout

```
src/filigree/
  __init__.py        # Package version and metadata
  core.py            # FiligreeDB class, SQLite schema, Issue dataclass
  cli.py             # Click CLI (all commands)
  mcp_server.py      # MCP server facade (globals, resources, prompts, dispatch)
  mcp_tools/         # Domain-grouped MCP tool modules
    common.py        # Pure helpers: _text, pagination, validation
    issues.py        # 12 tools — CRUD, search, claim, batch
    planning.py      # 7 tools — deps, ready/blocked, plans, critical path
    files.py         # 8 tools — file tracking, scanners, scan triggering
    workflow.py      # 10 tools — templates, types, packs, transitions
    meta.py          # 16 tools — comments, labels, stats, export/import
  templates.py       # Workflow template engine (registry, validation, transitions)
  templates_data.py  # Built-in template definitions (24 types across 9 packs)
  summary.py         # context.md generator
  analytics.py       # Flow metrics (cycle time, lead time, throughput)
  install.py         # MCP config, CLAUDE.md injection, doctor checks
  hooks.py           # Session hooks (context + dashboard bootstrap)
  server.py          # Persistent daemon config and lifecycle helpers
  ephemeral.py       # Ethereal-mode PID and process lifecycle helpers
  scanners.py        # Scanner registry and command validation
  migrate.py         # Beads-to-filigree migration
  migrations.py      # Schema migration framework (registry, runner, SQLite helpers)
  dashboard.py       # FastAPI web dashboard
  logging.py         # Logging configuration
```

### Module Responsibilities

**`core.py`** — the heart of filigree. Contains the `FiligreeDB` class wrapping all SQLite operations, the `Issue` dataclass, schema DDL, and migration functions. Both `cli.py` and `mcp_server.py` import from here — no business logic is duplicated.

**`templates.py`** — the workflow engine. Defines `TemplateRegistry` (lazy-loaded singleton), frozen dataclasses for `StateDefinition`, `TransitionDefinition`, `FieldSchema`, and `TypeTemplate`. Handles transition validation, field requirement checking, and state category mapping.

**`templates_data.py`** — pure data, no logic. Contains all built-in pack definitions as nested dicts. Separated from `templates.py` so template definitions are readable and editable without touching engine code.

**`summary.py`** — generates `context.md` after every mutation. Queries the database for vitals, ready queue, blocked items, and recent activity, then writes a markdown file agents can read at session start.

**`analytics.py`** — computes flow metrics from the event stream: cycle time (start to close), lead time (create to close), throughput (issues closed per period).

**`migrations.py`** — schema migration framework. Defines the `MigrationFn` protocol and a `MIGRATIONS` registry mapping version numbers to migration functions. The `apply_pending_migrations()` runner reads the current schema version via `PRAGMA user_version`, applies each pending migration in its own transaction (with rollback on failure), and bumps the version after each success. Includes SQLite helpers for common DDL operations that work around ALTER TABLE limitations: `add_column`, `add_index`, `drop_index`, `rename_column`, and `rebuild_table` (the 12-step pattern for changes ALTER TABLE cannot express). Also provides migration templates as starting points for new migrations.

## Database Schema

SQLite with WAL mode. Schema version 4.

### Tables

#### `issues`

```sql
CREATE TABLE issues (
    id          TEXT PRIMARY KEY,       -- {prefix}-{10hex}
    title       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    priority    INTEGER NOT NULL DEFAULT 2,  -- 0-4
    type        TEXT NOT NULL DEFAULT 'task',
    parent_id   TEXT REFERENCES issues(id) ON DELETE SET NULL,
    assignee    TEXT DEFAULT '',
    created_at  TEXT NOT NULL,          -- ISO 8601
    updated_at  TEXT NOT NULL,
    closed_at   TEXT,
    description TEXT DEFAULT '',
    notes       TEXT DEFAULT '',
    fields      TEXT DEFAULT '{}'       -- JSON custom fields
);
```

Indexed on `status`, `type`, `parent_id`, `priority`, and a composite index on `(status, priority, created_at)` for efficient ready-queue queries.

#### `dependencies`

```sql
CREATE TABLE dependencies (
    issue_id       TEXT NOT NULL REFERENCES issues(id),
    depends_on_id  TEXT NOT NULL REFERENCES issues(id),
    type           TEXT NOT NULL DEFAULT 'blocks',
    created_at     TEXT NOT NULL,
    PRIMARY KEY (issue_id, depends_on_id)
);
```

Indexed on `depends_on_id` (reverse lookup for "what does this issue block?") and a composite index on `(issue_id, depends_on_id)`.

#### `events`

```sql
CREATE TABLE events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id   TEXT NOT NULL REFERENCES issues(id),
    event_type TEXT NOT NULL,
    actor      TEXT DEFAULT '',
    old_value  TEXT,
    new_value  TEXT,
    comment    TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
```

Indexed on `issue_id`, `created_at`, and a composite index on `(issue_id, created_at DESC)` for efficient per-issue history queries. Powers event history, undo, session resumption, and analytics. Per [ADR-003](https://github.com/tachyon-beep/filigree/blob/main/docs/architecture/decisions/ADR-003-operational-durability-not-audit-proofing.md), these records are durable for operational utility rather than audit-proof evidence.

#### `comments`

```sql
CREATE TABLE comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id   TEXT NOT NULL REFERENCES issues(id),
    author     TEXT DEFAULT '',
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

Indexed on `(issue_id, created_at)` for chronological comment retrieval per issue.

#### `labels`

```sql
CREATE TABLE labels (
    issue_id TEXT NOT NULL REFERENCES issues(id),
    label    TEXT NOT NULL,
    PRIMARY KEY (issue_id, label)
);
```

#### `type_templates`

```sql
CREATE TABLE type_templates (
    type          TEXT PRIMARY KEY,
    pack          TEXT NOT NULL DEFAULT 'core',
    definition    TEXT NOT NULL,       -- JSON workflow definition
    is_builtin    BOOLEAN NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
```

#### `packs`

```sql
CREATE TABLE packs (
    name          TEXT PRIMARY KEY,
    version       TEXT NOT NULL,
    definition    TEXT NOT NULL,       -- JSON pack definition
    is_builtin    BOOLEAN NOT NULL DEFAULT 0,
    enabled       BOOLEAN NOT NULL DEFAULT 1
);
```

### FTS5 Full-Text Search

```sql
CREATE VIRTUAL TABLE issues_fts USING fts5(
    title, description, content='issues', content_rowid='rowid'
);
```

Kept in sync via triggers on INSERT, UPDATE, and DELETE. Used by `search_issues` for fast full-text search.

## Key Design Decisions

### Convention-Based Discovery

Filigree finds the project root by walking up the filesystem looking for `.filigree/`, the same way git finds `.git/`. This means:

- No environment variables to set
- No config files to parse
- No server URLs to resolve in default `ethereal` mode
- Works from any subdirectory of the project

### Template-Driven Validation

Workflows are defined as data (state machines with transitions and field requirements), not code. This means:

- New types can be added without code changes
- Packs can be installed from disk
- Validation rules are introspectable (agents can query valid transitions)

### Category Abstraction

Every state maps to one of three categories: `open`, `wip`, `done`. This allows:

- Cross-type queries (`list --status=open` finds bugs in `triage`, features in `proposed`, tasks in `open`)
- Universal ready-queue logic (any `open`-category issue with no blockers is "ready")
- Consistent progress metrics regardless of type-specific state names

### Event Sourcing

Every mutation creates an event record. This enables:

- **Operational history** — who did what and when, per-issue or globally
- **Undo** — reverse the most recent reversible action
- **Session resumption** — agents catch up via `get_changes --since`
- **Analytics** — cycle time, lead time, and throughput computed from events
- **Archival** — old events can be compacted without losing issue state

See [ADR-003](https://github.com/tachyon-beep/filigree/blob/main/docs/architecture/decisions/ADR-003-operational-durability-not-audit-proofing.md):
Filigree records are durable working memory, not audit-proof evidence.

### Batch Optimizations

Batch operations (`batch_update`, `batch_close`) execute in a single transaction to eliminate N+1 query patterns. Per-item errors are reported without aborting the entire batch.

### Atomic Claiming

`claim_issue` uses optimistic locking — it checks the current assignee and sets the new one in a single atomic operation. If another agent claimed the issue between the check and the update, the operation fails. This prevents double-work in multi-agent scenarios without requiring external locks.

### Lazy-Loaded Template Registry

The `TemplateRegistry` is loaded lazily on first access and cached for the session. This avoids startup overhead when templates aren't needed (e.g., simple CRUD operations) while ensuring templates are always available for validation.

### Pre-Computed Context

Rather than having agents query for project state at session start, `context.md` is regenerated on every mutation. This inverts the cost: writes are slightly slower, but reads (which happen at every agent session start) are instant.
