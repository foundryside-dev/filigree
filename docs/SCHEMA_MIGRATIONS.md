# Schema Migrations

Filigree uses a lightweight, hand-rolled migration framework built on SQLite's `PRAGMA user_version`. No external dependencies (no Alembic, no SQLAlchemy).

## Architecture

```
src/filigree/
  db_schema.py     # SCHEMA_SQL constant + CURRENT_SCHEMA_VERSION
  migrations.py    # Migration registry, runner, and SQLite helpers
tests/
  core/test_schema.py  # Framework tests + per-migration test templates
```

**How it works:**

1. Every filigree database stores its schema version in `PRAGMA user_version`
2. `FiligreeDB.initialize()` checks this version on startup:
   - `user_version == 0` (fresh DB): runs `SCHEMA_SQL` and stamps `CURRENT_SCHEMA_VERSION`
   - `user_version < CURRENT_SCHEMA_VERSION` (outdated DB): runs pending migrations
   - `user_version == CURRENT_SCHEMA_VERSION`: no-op
3. Each migration is a Python function that receives a `sqlite3.Connection`
4. Migrations run one at a time, each committed and version-bumped individually
5. If a migration fails, it rolls back and the database stays at the last successful version

## Release Schema Registry

Use this table to answer "what `PRAGMA user_version` ships in this release?"
without grepping source. The source of truth remains
`src/filigree/db_schema.py::CURRENT_SCHEMA_VERSION`.

| Release | Ships `user_version` | Notes |
|---------|----------------------|-------|
| 3.0.0 | 27 | Migration 26 to 27 (signature-bypass fix): nullable `entity_associations.signed_content_hash` (`TEXT`) records the content_hash the current `signature` was cut over (the Legis HMAC binds content_hash). Backfilled `= content_hash_at_attach WHERE signature IS NOT NULL` — sound because the pre-fix re-attach unconditionally NULLed `signature` on any signatureless refresh, so a row that still holds a signature has not drifted since signing. The closure gate now (a) classifies governed-ness by `signature IS NOT NULL` (not truthiness; a blank signature is normalised to `NULL` at the data layer), and (b) fails closed as `STALE` when `signed_content_hash != content_hash_at_attach` (the sign-off has drifted) without a network call. The re-attach UPSERT is now sticky: `signature`/`signoff_seq`/`signed_content_hash` update only on a write that carries a signature (only Legis signs), so a routine signatureless drift refresh no longer revokes governance. **Scope boundary:** this detects CONTENT drift, not IDENTITY drift — the 25→26 rebrand rewrote `entity_id` (changing the HMAC input) while leaving content_hash untouched, so those rows backfill as content-fresh, the gate consults Legis, and Legis resolves the identity drift by re-signing in lockstep. Additive + idempotent; round-tripped through `export`/`import`. Migration 25 to 26 (Loomweave/Weft rebrand data pass): rename `entity_associations.clarion_entity_id` -> `loomweave_entity_id` and rewrite stored `clarion:eid:` SEI prefixes -> `loomweave:eid:` and finding rule-id prefixes `CLA-` -> `LMWV-` in place (suffixes preserved), across the binding, the F5 deletion tombstone `entity_ids` array, and the audit `events` log. Stored Legis `signature`s become stale-pending-reissue (the HMAC was cut over the old entity_id); Filigree never verifies, Legis re-signs in lockstep. Migration 24 to 25 (B1, Legis governed-sign-off binding): nullable `entity_associations.signature` (`TEXT`) and `signoff_seq` (`INTEGER`). Opaque HMAC + sequence Legis sends when binding a cleared governed sign-off; Filigree stores them verbatim and never verifies them. Additive + idempotent; no backfill (`NULL` = no key configured / non-governed binding); round-tripped through `export`/`import`. Migration 23 to 24 (ADR-012, transport-bound actor identity): nullable `verified_*` column on the 5 runtime event-bearing tables (`events.verified_actor`, `file_events.verified_actor`, `annotation_events.verified_actor`, `comments.verified_author`, `observations.verified_actor`). Additive + idempotent; no backfill (`NULL` = no transport proof); the `events` dedup unique index is **not** extended |
| 2.3.0 | 23 | Migration 22 to 23: `entity_associations.entity_kind` caller-supplied metadata; public projections expose canonical `entity_id` with `clarion_entity_id` compatibility alias |
| 2.1.1 | 21 | Migration 20 to 21: `deleted_issues.entity_ids`, surfaced as `affected_entities` on the `issue_deleted` deletion-signal record (F5 amplifier) |
| 2.1.0 | 20 | Migrations 14 to 20: entity associations (v15), event sequencing (v16), file registry metadata (v17), `application_id` stamp (v18), scan-finding fingerprints (v19), and the `deleted_issues` tombstone (v20) |
| 2.0.0 to 2.0.3 | 14 | Loom/API generation and 2.0 surface releases |
| 1.6.0 to 1.6.1 | 8 | Autodiscovery MCP install line |

For the operator upgrade path from 2.0.x to 2.1.0, see
[UPGRADING.md](UPGRADING.md#upgrading-from-20x-to-210); for 2.1.0 to 2.1.1, see
[UPGRADING.md](UPGRADING.md#upgrading-from-210-to-211).

## Adding a Migration

### Step 1: Snapshot the current schema

Before making any changes, copy the current `SCHEMA_SQL` from `db_schema.py` into your test file as `V<N>_SCHEMA_SQL`. This is your "before" snapshot for equivalence testing.

### Step 2: Update `SCHEMA_SQL` in `db_schema.py`

Modify `SCHEMA_SQL` to reflect the **final** state of the schema. This is what fresh databases will get. Keep it as the single source of truth.

### Step 3: Bump `CURRENT_SCHEMA_VERSION`

```python
# db_schema.py
CURRENT_SCHEMA_VERSION = 2  # was 1
```

### Step 4: Write the migration function in `migrations.py`

```python
def migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """v1 -> v2: Add 'source' column to issues table.

    Tracks where an issue was created from (cli, mcp, api, import).
    """
    add_column(conn, "issues", "source", "TEXT", "''")
    add_index(conn, "idx_issues_source", "issues", ["source"])
```

### Step 5: Register it

```python
MIGRATIONS: dict[int, MigrationFn] = {
    1: migrate_v1_to_v2,
}
```

### Step 6: Add tests in `test_migrations.py`

```python
class TestMigrateV1ToV2:
    V1_SCHEMA = """..."""  # Paste from step 1

    @pytest.fixture
    def v1_db(self, tmp_path: Path) -> sqlite3.Connection:
        conn = _make_db(tmp_path)
        conn.executescript(self.V1_SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "INSERT INTO issues (id, title, status, priority, type, created_at, updated_at) "
            "VALUES ('test-1', 'Issue 1', 'open', 2, 'task', '2026-01-01', '2026-01-01')"
        )
        conn.commit()
        return conn

    def test_migration_runs(self, v1_db):
        applied = apply_pending_migrations(v1_db, 2)
        assert applied == 1

    def test_data_preserved(self, v1_db):
        apply_pending_migrations(v1_db, 2)
        row = v1_db.execute("SELECT title FROM issues WHERE id = 'test-1'").fetchone()
        assert row[0] == "Issue 1"

    def test_new_column_has_default(self, v1_db):
        apply_pending_migrations(v1_db, 2)
        row = v1_db.execute("SELECT source FROM issues WHERE id = 'test-1'").fetchone()
        assert row[0] == ""

    def test_schema_matches_fresh(self, v1_db, tmp_path):
        apply_pending_migrations(v1_db, 2)
        fresh = _make_db(tmp_path, "fresh.db")
        fresh.executescript(SCHEMA_SQL)
        fresh.commit()
        for table in ["issues", "dependencies", "events", "comments", "labels"]:
            assert _get_table_columns(v1_db, table) == _get_table_columns(fresh, table)
        fresh.close()
```

### Step 7: Run tests

```bash
python -m pytest tests/core/test_schema.py -v
python -m pytest tests/ -x  # full suite
```

## SQLite Helper Reference

All helpers are idempotent (safe to call twice).

### `add_column(conn, table, column, col_type, default)`

Add a column to an existing table.

```python
add_column(conn, "issues", "source", "TEXT", "''")
add_column(conn, "issues", "weight", "INTEGER", "0")
add_column(conn, "issues", "metadata", "TEXT", None)  # nullable, no default
```

**SQLite limitation:** `ADD COLUMN` cannot add `NOT NULL` columns without a `DEFAULT`.

### `add_index(conn, index_name, table, columns, unique=False)`

Create an index.

```python
add_index(conn, "idx_issues_source", "issues", ["source"])
add_index(conn, "idx_issues_status_priority", "issues", ["status", "priority"])
add_index(conn, "idx_issues_code", "issues", ["code"], unique=True)
```

### `drop_index(conn, index_name)`

Remove an index.

```python
drop_index(conn, "idx_issues_old_column")
```

### `rename_column(conn, table, old_name, new_name)`

Rename a column (requires SQLite >= 3.25.0).

```python
rename_column(conn, "issues", "assignee", "owner")
```

### `rebuild_table(conn, table, new_schema_sql, column_mapping=None)`

Recreate a table with a different schema. This is the "12-step" pattern for changes that `ALTER TABLE` can't handle: modifying types, changing constraints, dropping columns on older SQLite.

```python
# Drop a column and change a constraint
rebuild_table(
    conn,
    "issues",
    "CREATE TABLE issues (id TEXT PRIMARY KEY, title TEXT NOT NULL, priority INTEGER CHECK(priority BETWEEN 0 AND 5))",
    column_mapping={
        "id": "id",
        "title": "title",
        "priority": "MIN(priority, 5)",  # clamp values to new range
    },
)

# Recreate indexes after rebuild (rebuild drops them)
add_index(conn, "idx_issues_priority", "issues", ["priority"])
```

**Warning:** `rebuild_table` drops all indexes, triggers, and views on the table. Recreate them explicitly after calling it.

If `column_mapping` is omitted, all columns present in both the old and new schemas are copied automatically.

## Common Migration Patterns

### Adding a column

The simplest case. Use `add_column`:

```python
def migrate_v1_to_v2(conn):
    add_column(conn, "issues", "source", "TEXT", "''")
```

### Adding a table

Use `executescript` with `IF NOT EXISTS`:

```python
def migrate_v2_to_v3(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            issue_id TEXT NOT NULL REFERENCES issues(id),
            filename TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_attachments_issue ON attachments(issue_id);
    """)
```

### Changing a constraint or column type

Requires `rebuild_table` because SQLite doesn't support `ALTER COLUMN`:

```python
def migrate_v3_to_v4(conn):
    rebuild_table(conn, "issues", NEW_ISSUES_SCHEMA, column_mapping={...})
    # Recreate all indexes on 'issues' table
    add_index(conn, "idx_issues_status", "issues", ["status"])
    # ...
```

### Backfilling data

Run after schema changes within the same migration:

```python
def migrate_v4_to_v5(conn):
    add_column(conn, "issues", "source", "TEXT", "'unknown'")
    # Backfill: issues created via MCP have actor starting with "mcp-"
    conn.execute("UPDATE issues SET source = 'mcp' WHERE id IN (SELECT DISTINCT issue_id FROM events WHERE actor LIKE 'mcp-%' AND event_type = 'created')")
```

## Design Decisions

**Why not Alembic?** Alembic requires SQLAlchemy. Filigree uses raw `sqlite3` with zero database dependencies. Adopting Alembic would mean maintaining SQLAlchemy model definitions alongside raw SQL queries — the worst of both worlds. The migration surface is also small: 7 tables, local-only databases, infrequent schema changes.

**Why per-step commits?** Each migration commits independently so that partial progress is preserved. If v2->v3 fails, the database stays at v2 (not rolled back to v1). This makes recovery straightforward: fix the migration and re-run.

**Why schema equivalence tests?** The most common migration bug is drift between `SCHEMA_SQL` (what fresh databases get) and the migration chain (what existing databases get). The equivalence test catches this by comparing column-by-column.

**Why idempotent helpers?** Migrations may be interrupted (crash, power loss). Idempotent operations (`IF NOT EXISTS`, column-existence checks) make re-running safe without needing to track sub-step progress.
