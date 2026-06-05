# Transport-bound Actor Identity (v24 slice) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind each Filigree write's claimed `actor`/`author` to a transport-verified identity (the OS user the process runs as), recorded in a new nullable `verified_*` column on every runtime event-bearing table, surfaced on read, with a non-blocking mismatch warning.

**Architecture:** Schema v24 adds a sibling `verified_*` column next to each claimed-actor column (no rename, no backfill, `NULL` = no transport proof). `FiligreeDB` carries a session-level `self._verified_actor` set once at the entry point (CLI `get_db()`, MCP-stdio `_init_db`); every runtime insert site stamps it; mismatch detection happens once per call at the entry point and never blocks.

**Tech Stack:** Python 3.13, SQLite (via `sqlite3`), Click (CLI), MCP SDK (stdio server), mypy + ruff + pytest gate.

**Branch:** `release/3.0.0` — commits land directly on the branch (this is a 3.0.0 breaking-bundle keystone change). Do NOT create or switch branches.

**ADR:** extends ADR-012 (Actor Identity Threat Model).

---

## Decisions locked before implementation (read these first)

These resolve gaps and conflicts found between the spec and codebase reality. They are binding for this plan.

1. **Migration naming.** The spec wrote `_migrate_v23_to_v24`; the actual codebase convention has **no leading underscore**: `migrate_v23_to_v24`, registered in `MIGRATIONS` under integer key `23`. `CURRENT_SCHEMA_VERSION` goes 23 → 24.

2. **`db_base.py` is in scope (spec §9 omitted it).** Every insert site reads `self._verified_actor` through `DBMixinProtocol` (`db_base.py:268`). mypy will fail unless `_verified_actor: str | None` is declared on that protocol. This is Task 3.

3. **`db_meta.py:add_comment` (line ~58) is a runtime stamp site (spec §7 omitted it).** Spec §7 named only `db_observations.py:840` for comments — the secondary observation-link path. `add_comment` is the *primary* user comment writer and MUST stamp `verified_author`. This is Task 5.

4. **Import restores stored verified attribution (not NULL).** `export_jsonl` uses `SELECT *` (`db_meta.py:873-884`), so `verified_*` columns automatically flow into exports. The matching `import_jsonl` insert sites for **events, comments, file_events** therefore restore the stored value via `record.get("verified_*")` — mirroring the existing `migration_orphaned_at` preservation precedent (`db_meta.py:1376`). Setting them NULL would destroy verified attribution that existed at original write time (an audit-integrity regression). This is Task 7. The discriminator is *"am I recording a new event now, or restoring a recorded one?"* — restoring → preserve; new system write → NULL.

5. **System/cascade writers stay NULL.** `finding_issue_cascade.py:record_reconciliation_debt_comment` (writes via a bare `conn`, system actor, no transport proof) and `migrations.py`/`migrate.py` leave the column NULL. No transport identity exists for these.

6. **MCP mismatch surface — build the envelope `warnings` list now (user-confirmed).** Spec §6 says MCP "add it to the response envelope `warnings` list." That list does not exist today — every MCP tool handler returns `list[TextContent]` with its own JSON envelope. **Decision (confirmed with user): build it.** Rather than edit every handler, `call_tool` post-processes the handler's `list[TextContent]` result: parse the first text element's JSON, and if it is a dict, add/extend a top-level `warnings` array (a shared `_inject_warnings` helper). Bare-string and non-dict responses are left untouched. The same helper serves any future warning producer. This applies to both MCP-stdio and MCP-HTTP call paths (both go through `call_tool`); the *resolver* that sets `_verified_actor` is wired for stdio in this slice (HTTP peer identity remains the out-of-scope ticket, so HTTP requests simply carry `_verified_actor = None` → no mismatch warning until that ticket lands).

7. **Tombstone synthetic records carry `verified_actor=None`.** `_deleted_issue_changes` (`db_events.py:274`) constructs `EventRecordWithTitle` by hand; once the TypedDict gains the field, this construction must add `verified_actor=None` (a hard-deleted issue has no verified actor) or mypy fails. This is Task 4.

8. **`borrow_for_worker_thread` needs no new code.** It clones via `copy.copy(self)` (`core.py:1654`), which shallow-copies `self._verified_actor` for free. A propagation test is still written (Task 3) to lock the behavior against future refactors.

9. **Mismatch warning suppresses placeholder-default claims (user-confirmed).** As specified (§6: "both non-empty and differ → warn"), the warning would fire on essentially *every* command: `--actor` defaults to `"cli"` (`cli.py:79`) while verified resolves to the OS user → mismatch every time; and intended agent usage (CLAUDE.md tells agents to pass e.g. `--actor clarion-bot`) also differs from the OS user every command — the whole premise of ADR-012. **Decision (confirmed with user): suppress placeholder defaults** — the warning fires only when the claim is a *real, distinct* identity, never for the framework's auto-default placeholders. Implemented in `actor_mismatch_warning(claimed, verified)` via a `_PLACEHOLDER_ACTORS` set (`{"cli", "mcp"}`): a claimed value in that set is treated as "no genuine claim" → returns `None`. **Recording both values in the DB (the real audit feature) is unaffected — every write still stamps `verified_*` regardless of the warning.** Only the warning surface is scoped.

---

## File Structure

**Create:**
- `src/filigree/actor_identity.py` — OS-user resolver (`resolve_os_actor`) + mismatch-warning builder (`actor_mismatch_warning`). One responsibility: transport identity resolution. No DB, no I/O beyond `pwd`/`os`.
- `tests/core/test_actor_identity.py` — unit tests for the resolver + warning builder.
- `tests/core/test_verified_actor.py` — stamping, read-path, propagation, import round-trip tests.

**Modify:**
- `src/filigree/db_schema.py` — add `verified_*` to 5 `CREATE TABLE`s in `SCHEMA_SQL`; bump `CURRENT_SCHEMA_VERSION` 23→24. (`SCHEMA_V1_SQL` untouched — it is a frozen migration-test fixture.)
- `src/filigree/migrations.py` — add `migrate_v23_to_v24`; register key `23`.
- `src/filigree/db_base.py` — add `_verified_actor` to `DBMixinProtocol`.
- `src/filigree/core.py` — `FiligreeDB.__init__` param + `self._verified_actor` + `set_verified_actor`.
- `src/filigree/db_events.py` — stamp `_record_event`; project column in the 2 event-record builders + tombstone.
- `src/filigree/types/events.py` — `verified_actor` on `EventRecord`.
- `src/filigree/types/planning.py` — `verified_author` on `CommentRecord`.
- `src/filigree/types/core.py` — `verified_actor` on `ObservationDict`.
- `src/filigree/db_meta.py` — stamp `add_comment`; project `verified_author` in `get_comments`/`get_comment`; preserve on import (event/comment/file_event stages).
- `src/filigree/db_observations.py` — stamp `observe` INSERT + return dict; stamp observation-link comment.
- `src/filigree/db_files.py` — stamp 2 `file_events` inserts.
- `src/filigree/db_annotations.py` — stamp `_record_annotation_event`.
- `src/filigree/cli_common.py` — set verified actor in `get_db()`.
- `src/filigree/cli.py` — mismatch check in the `cli()` group callback.
- `src/filigree/mcp_server.py` — set verified actor in `_init_db`; mismatch log in `call_tool`.
- `tests/core/test_schema.py` — migration + fresh-schema + idempotent tests.
- `docs/SCHEMA_MIGRATIONS.md`, `CHANGELOG.md`, `docs/adr/ADR-012-*.md` (or wherever ADR-012 lives).

---

## Pre-flight (run once before Task 1)

- [ ] **Confirm starting state is green and on the right branch**

Run:
```bash
cd /home/john/filigree
git branch --show-current   # expect: release/3.0.0
uv run pytest tests/core/test_schema.py -q
```
Expected: branch is `release/3.0.0`; schema tests pass. If the branch differs, STOP and ask the user — do not switch branches.

---

## Task 1: Schema v24 — migration + CREATE-TABLE columns + version bump

**Files:**
- Modify: `src/filigree/db_schema.py` (5 `CREATE TABLE`s in `SCHEMA_SQL`; `CURRENT_SCHEMA_VERSION`)
- Modify: `src/filigree/migrations.py` (new `migrate_v23_to_v24`; register key `23`)
- Test: `tests/core/test_schema.py`

- [ ] **Step 1: Write the failing migration test**

Append to the migration test class in `tests/core/test_schema.py` (the class that contains `test_migration_v22_to_v23_adds_entity_kind_column`). Use the existing helpers `_make_db`, `_get_table_columns`, `_get_schema_version`, `SCHEMA_SQL`, `apply_pending_migrations`.

```python
    def test_migration_v23_to_v24_adds_verified_actor_columns(self, tmp_path: Path) -> None:
        conn = _make_db(tmp_path)
        conn.executescript(SCHEMA_SQL)
        # Simulate a true v23 DB: drop the new columns and stamp the prior version.
        conn.execute("ALTER TABLE events DROP COLUMN verified_actor")
        conn.execute("ALTER TABLE file_events DROP COLUMN verified_actor")
        conn.execute("ALTER TABLE annotation_events DROP COLUMN verified_actor")
        conn.execute("ALTER TABLE comments DROP COLUMN verified_author")
        conn.execute("ALTER TABLE observations DROP COLUMN verified_actor")
        conn.execute("PRAGMA user_version = 23")
        conn.execute(
            "INSERT INTO issues (id, title, created_at, updated_at) VALUES ('iss-1', 't', ?, ?)",
            ("2026-05-01T00:00:00+00:00", "2026-05-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, created_at) VALUES ('iss-1', 'created', 'x', ?)",
            ("2026-05-01T00:00:00+00:00",),
        )
        conn.commit()
        assert "verified_actor" not in _get_table_columns(conn, "events")

        applied = apply_pending_migrations(conn, 24)

        assert applied == 1
        assert _get_schema_version(conn) == 24
        assert "verified_actor" in _get_table_columns(conn, "events")
        assert "verified_actor" in _get_table_columns(conn, "file_events")
        assert "verified_actor" in _get_table_columns(conn, "annotation_events")
        assert "verified_author" in _get_table_columns(conn, "comments")
        assert "verified_actor" in _get_table_columns(conn, "observations")
        # Pre-existing row reads NULL (no backfill).
        row = conn.execute("SELECT verified_actor FROM events WHERE issue_id = 'iss-1'").fetchone()
        assert row["verified_actor"] is None
        conn.close()

    def test_migration_v23_to_v24_idempotent(self, tmp_path: Path) -> None:
        from filigree.migrations import migrate_v23_to_v24

        conn = _make_db(tmp_path)
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        # Columns already present (fresh schema) — re-running add_column is a no-op.
        migrate_v23_to_v24(conn)
        assert "verified_actor" in _get_table_columns(conn, "events")
        assert "verified_author" in _get_table_columns(conn, "comments")
        conn.close()

    def test_fresh_schema_has_v24_verified_columns(self, tmp_path: Path) -> None:
        conn = _make_db(tmp_path)
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        assert "verified_actor" in _get_table_columns(conn, "events")
        assert "verified_actor" in _get_table_columns(conn, "file_events")
        assert "verified_actor" in _get_table_columns(conn, "annotation_events")
        assert "verified_author" in _get_table_columns(conn, "comments")
        assert "verified_actor" in _get_table_columns(conn, "observations")
        conn.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/core/test_schema.py -k "v23_to_v24 or v24_verified" -v`
Expected: FAIL — `migrate_v23_to_v24` does not exist / `apply_pending_migrations(conn, 24)` raises "No migration registered for v23 → v24" / columns absent from `SCHEMA_SQL`.

- [ ] **Step 3: Add the columns to `SCHEMA_SQL` (fresh DBs)**

In `src/filigree/db_schema.py`, edit the 5 `CREATE TABLE` blocks inside `SCHEMA_SQL` (the first occurrence of each — lines ~50, ~75, ~228, ~243, ~370; NOT the `SCHEMA_V1_SQL` copies at lines ~505+).

`events` — change the `event_seq` line to add a trailing column:
```sql
    event_seq  INTEGER NOT NULL DEFAULT 0,
    verified_actor TEXT
);
```

`comments`:
```sql
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    verified_author TEXT
);
```

`file_events`:
```sql
    actor       TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    verified_actor TEXT
);
```

`observations`:
```sql
    created_at        TEXT NOT NULL,
    expires_at        TEXT NOT NULL,
    verified_actor    TEXT
);
```

`annotation_events`:
```sql
    target_id     TEXT DEFAULT '',
    created_at    TEXT NOT NULL,
    verified_actor TEXT
);
```

- [ ] **Step 4: Bump the schema version**

In `src/filigree/db_schema.py`:
```python
CURRENT_SCHEMA_VERSION = 24
```

- [ ] **Step 5: Add the migration function and register it**

In `src/filigree/migrations.py`, add after `migrate_v22_to_v23` (before the `MIGRATIONS` dict):
```python
def migrate_v23_to_v24(conn: sqlite3.Connection) -> None:
    """v23 -> v24: Add nullable ``verified_*`` transport-bound actor columns (ADR-012).

    The ``actor``/``author`` string on a write is an unauthenticated *claim*. This
    adds a sibling column on every runtime event-bearing table holding the
    identity the transport *verified* (the OS user the writing process ran as), or
    NULL when no transport proof exists. NULL is the default for all existing rows
    (no backfill) and for every unverified or system-written row. The claimed
    column is unchanged; the ``events`` dedup index is NOT extended — verified_actor
    is attribution metadata, not part of event identity. Nullable (``default=None``
    adds no DEFAULT clause); existing rows read NULL. Idempotent: ``add_column``
    no-ops if the column already exists.
    """
    add_column(conn, "events", "verified_actor", "TEXT", default=None)
    add_column(conn, "file_events", "verified_actor", "TEXT", default=None)
    add_column(conn, "annotation_events", "verified_actor", "TEXT", default=None)
    add_column(conn, "comments", "verified_author", "TEXT", default=None)
    add_column(conn, "observations", "verified_actor", "TEXT", default=None)
```

Then add to the `MIGRATIONS` dict (after the `22: migrate_v22_to_v23,` line):
```python
    23: migrate_v23_to_v24,
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/core/test_schema.py -v`
Expected: PASS (all migration tests, including the three new ones).

- [ ] **Step 7: Commit**

```bash
git add src/filigree/db_schema.py src/filigree/migrations.py tests/core/test_schema.py
git commit -m "feat(schema): v24 — add nullable verified_* actor columns

Adds verified_actor/verified_author to events, file_events,
annotation_events, comments, observations. Nullable, no backfill;
events dedup index unchanged. ADR-012.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `actor_identity.py` — OS-user resolver + mismatch-warning builder

**Files:**
- Create: `src/filigree/actor_identity.py`
- Test: `tests/core/test_actor_identity.py`

- [ ] **Step 1: Write the failing test**

Create `tests/core/test_actor_identity.py`:
```python
"""Tests for transport-bound actor identity resolution (ADR-012, schema v24)."""

from __future__ import annotations

import builtins

from filigree.actor_identity import actor_mismatch_warning, resolve_os_actor


def test_resolve_os_actor_returns_str_on_posix() -> None:
    # On the POSIX CI/dev host this resolves to the running user's name.
    result = resolve_os_actor()
    assert result is None or isinstance(result, str)
    assert result != ""  # never an empty string — None or a real name


def test_resolve_os_actor_returns_none_when_pwd_unavailable(monkeypatch) -> None:
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pwd":
            raise ModuleNotFoundError("No module named 'pwd'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert resolve_os_actor() is None  # does not raise


def test_mismatch_warning_none_when_equal() -> None:
    assert actor_mismatch_warning("alice", "alice") is None


def test_mismatch_warning_none_when_either_empty() -> None:
    assert actor_mismatch_warning("alice", None) is None
    assert actor_mismatch_warning("alice", "") is None
    assert actor_mismatch_warning(None, "alice") is None
    assert actor_mismatch_warning("", "alice") is None


def test_mismatch_warning_emitted_when_both_present_and_differ() -> None:
    warning = actor_mismatch_warning("agent-x", "alice")
    assert warning == {"code": "ACTOR_MISMATCH", "claimed": "agent-x", "verified": "alice"}


def test_mismatch_warning_suppressed_for_placeholder_default_claims() -> None:
    # Framework auto-defaults are not genuine claims — no warning even though
    # "cli"/"mcp" differ from the verified OS user.
    assert actor_mismatch_warning("cli", "alice") is None
    assert actor_mismatch_warning("mcp", "alice") is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/core/test_actor_identity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'filigree.actor_identity'`.

- [ ] **Step 3: Create the module**

Create `src/filigree/actor_identity.py`:
```python
"""Transport-bound actor identity resolution (ADR-012, schema v24).

The ``actor`` string on a Filigree write is an unauthenticated *claim*, not a
proof. This module resolves a best-effort *verified* identity from the
transport (the OS user the process runs as) and builds the structured warning
emitted when the claimed and verified identities disagree. Resolution never
raises and never blocks a write: a missing or unresolvable identity yields
``None`` and the write proceeds with ``verified_actor = NULL``.
"""

from __future__ import annotations

from typing import TypedDict


def resolve_os_actor() -> str | None:
    """Best-effort OS-user identity, or ``None`` on any failure.

    Uses ``pwd.getpwuid(os.geteuid())`` on POSIX. Windows has no ``pwd``
    module, so the import fails and we return ``None`` (verified_actor stays
    NULL — no crash, per the cross-platform contract).
    """
    try:
        import os
        import pwd

        return pwd.getpwuid(os.geteuid()).pw_name or None
    except Exception:
        return None


class ActorMismatchWarning(TypedDict):
    """Structured warning emitted when claimed actor != verified actor."""

    code: str
    claimed: str
    verified: str


# Framework auto-default actor strings. A claim equal to one of these is NOT a
# genuine identity assertion (it is what Click/MCP fill in when the caller
# supplied nothing), so a difference from the verified OS user is expected and
# must not produce a warning. The DB still records the value verbatim; only the
# warning surface is suppressed. (ADR-012 decision 9.)
_PLACEHOLDER_ACTORS = frozenset({"cli", "mcp"})


def actor_mismatch_warning(claimed: str | None, verified: str | None) -> ActorMismatchWarning | None:
    """Return a structured warning when claimed and verified identities differ.

    Returns ``None`` (no warning) unless BOTH values are non-empty, differ, and
    the claim is a *genuine* identity (not a framework placeholder default). A
    missing/empty/placeholder claimed value, or an empty verified value, is an
    unverified surface rather than a conflict. Never raises, never blocks a write.
    """
    if claimed and verified and claimed != verified and claimed not in _PLACEHOLDER_ACTORS:
        return {"code": "ACTOR_MISMATCH", "claimed": claimed, "verified": verified}
    return None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/core/test_actor_identity.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/filigree/actor_identity.py tests/core/test_actor_identity.py
git commit -m "feat: add actor_identity resolver + mismatch-warning builder (ADR-012)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Session-level identity plumbing (constructor + setter + protocol + propagation)

**Files:**
- Modify: `src/filigree/core.py` (`FiligreeDB.__init__`, new `set_verified_actor`)
- Modify: `src/filigree/db_base.py` (`DBMixinProtocol`)
- Test: `tests/core/test_verified_actor.py`

- [ ] **Step 1: Write the failing test**

Create `tests/core/test_verified_actor.py`. (Use the existing fresh-DB construction helper. The conventional way to stand up a `FiligreeDB` in this suite is `FiligreeDB.from_filigree_dir` after `filigree init`; if a project test fixture like `tmp_db` exists in `tests/conftest.py` or `tests/core/conftest.py`, prefer it. The code below uses a minimal direct construction against a temp DB path — adjust the constructor call to match the suite's existing helper if one is present.)

```python
"""Tests for transport-bound verified-actor plumbing (ADR-012, schema v24)."""

from __future__ import annotations

from pathlib import Path

import pytest

from filigree.core import FiligreeDB


@pytest.fixture
def db(tmp_path: Path) -> FiligreeDB:
    filigree_dir = tmp_path / ".filigree"
    filigree_dir.mkdir()
    database = FiligreeDB.from_filigree_dir(filigree_dir)
    return database


def test_constructor_defaults_verified_actor_to_none(db: FiligreeDB) -> None:
    assert db._verified_actor is None


def test_set_verified_actor_updates_field(db: FiligreeDB) -> None:
    db.set_verified_actor("alice")
    assert db._verified_actor == "alice"
    db.set_verified_actor(None)
    assert db._verified_actor is None


def test_borrow_for_worker_thread_propagates_verified_actor(db: FiligreeDB) -> None:
    db.set_verified_actor("alice")
    with db.borrow_for_worker_thread() as clone:
        assert clone._verified_actor == "alice"
```

> If `FiligreeDB.from_filigree_dir` requires additional setup (templates, registry), copy the construction idiom from an existing core test such as `tests/core/test_crud.py`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/core/test_verified_actor.py -v`
Expected: FAIL — `AttributeError: 'FiligreeDB' object has no attribute '_verified_actor'`.

- [ ] **Step 3: Add the field + setter to `FiligreeDB`**

In `src/filigree/core.py`, add a parameter to `__init__` (the signature at line ~981). Add after `skip_clarion_capability_probe: bool = False,`:
```python
        skip_clarion_capability_probe: bool = False,
        verified_actor: str | None = None,
```

Then in the `__init__` body, alongside the other `self.*` assignments (e.g. just after `self._check_same_thread = check_same_thread` at line ~1016), add:
```python
        # ADR-012 (schema v24): the transport-verified identity for this session,
        # set once at the entry point (CLI get_db / MCP _init_db). None = no
        # transport proof; every runtime insert stamps this into verified_*.
        # ``borrow_for_worker_thread`` propagates it for free via copy.copy.
        self._verified_actor: str | None = verified_actor
```

Add the setter method to the `FiligreeDB` class (place it near `borrow_for_worker_thread`, e.g. just before it at line ~1614):
```python
    def set_verified_actor(self, value: str | None) -> None:
        """Set the transport-verified identity for this session.

        Entry points (CLI ``get_db``, MCP ``_init_db``) construct the DB before
        resolving identity, then call this. Every subsequent runtime write
        stamps ``value`` into its ``verified_*`` column. ``None`` (the default)
        leaves writes unverified (``verified_* = NULL``).
        """
        self._verified_actor = value
```

- [ ] **Step 4: Declare the attribute on `DBMixinProtocol`**

In `src/filigree/db_base.py`, in the `DBMixinProtocol` shared-attributes block (after `_conn: sqlite3.Connection | None` at line ~293), add:
```python
    # ADR-012 (schema v24): transport-verified session identity. Mixins read
    # this directly when stamping verified_* columns. Set on FiligreeDB.__init__
    # (defaults to None); never None-guarded at insert sites.
    _verified_actor: str | None
```

- [ ] **Step 5: Run the test + mypy to verify**

Run: `uv run pytest tests/core/test_verified_actor.py -v && uv run mypy src/filigree/core.py src/filigree/db_base.py`
Expected: PASS; mypy clean.

- [ ] **Step 6: Commit**

```bash
git add src/filigree/core.py src/filigree/db_base.py tests/core/test_verified_actor.py
git commit -m "feat: session-level verified_actor plumbing on FiligreeDB (ADR-012)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Stamp `events` + project `verified_actor` on the read path

**Files:**
- Modify: `src/filigree/db_events.py` (`_record_event`, `_build_event_record`, `_build_event_record_with_title`, `_deleted_issue_changes`)
- Modify: `src/filigree/types/events.py` (`EventRecord`)
- Test: `tests/core/test_verified_actor.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_verified_actor.py`:
```python
def _create_issue(db: FiligreeDB) -> str:
    issue = db.create_issue(title="t", actor="agent-x")
    return issue.id


def test_event_stamps_verified_actor_when_set(db: FiligreeDB) -> None:
    db.set_verified_actor("alice")
    issue_id = _create_issue(db)
    row = db.conn.execute(
        "SELECT verified_actor FROM events WHERE issue_id = ? AND event_type = 'created'",
        (issue_id,),
    ).fetchone()
    assert row["verified_actor"] == "alice"


def test_event_verified_actor_null_when_unset(db: FiligreeDB) -> None:
    # No set_verified_actor call — unverified surface.
    issue_id = _create_issue(db)
    row = db.conn.execute(
        "SELECT verified_actor FROM events WHERE issue_id = ? AND event_type = 'created'",
        (issue_id,),
    ).fetchone()
    assert row["verified_actor"] is None


def test_event_record_read_path_exposes_verified_actor(db: FiligreeDB) -> None:
    db.set_verified_actor("alice")
    issue_id = _create_issue(db)
    events = db.get_issue_events(issue_id)
    created = next(e for e in events if e["event_type"] == "created")
    assert created["verified_actor"] == "alice"
```

> Adjust `db.create_issue(...)` to the actual create signature if it differs (check `tests/core/test_crud.py`). The point is: any write that records an event.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/core/test_verified_actor.py -k "event" -v`
Expected: FAIL — `verified_actor` not in the INSERT (stored NULL even when set) / `KeyError: 'verified_actor'` from the read builder.

- [ ] **Step 3: Stamp the INSERT in `_record_event`**

In `src/filigree/db_events.py`, replace the `_record_event` INSERT (lines ~98-102):
```python
        self.conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, verified_actor, old_value, new_value, comment, created_at, event_seq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT MAX(event_seq) FROM events WHERE issue_id = ?), -1) + 1)",
            (issue_id, event_type, actor, self._verified_actor, old_value, new_value, comment, _now_iso(), issue_id),
        )
```

- [ ] **Step 4: Project the column in both record builders**

In `_build_event_record` (line ~44), add the field to the returned `EventRecord`:
```python
        return EventRecord(
            id=row["id"],
            issue_id=row["issue_id"],
            event_type=row["event_type"],
            actor=row["actor"],
            verified_actor=row["verified_actor"],
            old_value=row["old_value"],
            new_value=row["new_value"],
            comment=row["comment"],
            created_at=row["created_at"],
        )
```

In `_build_event_record_with_title` (line ~58), add the same field:
```python
        return EventRecordWithTitle(
            id=row["id"],
            issue_id=row["issue_id"],
            event_type=row["event_type"],
            actor=row["actor"],
            verified_actor=row["verified_actor"],
            old_value=row["old_value"],
            new_value=row["new_value"],
            comment=row["comment"],
            created_at=row["created_at"],
            issue_title=row["issue_title"],
        )
```

- [ ] **Step 5: Add `verified_actor=None` to the tombstone synthetic record**

In `_deleted_issue_changes` (the `EventRecordWithTitle(...)` construction at line ~275), add the field (a hard-deleted issue has no verified actor):
```python
            records.append(
                EventRecordWithTitle(
                    id=synthetic_id,
                    issue_id=r["issue_id"],
                    event_type="issue_deleted",
                    actor=r["deleted_by"] or "",
                    verified_actor=None,
                    old_value=None,
                    new_value=None,
                    comment="",
                    created_at=r["deleted_at"],
                    issue_title=r["title"] or "",
                    affected_entities=affected_entities,
                )
            )
```

- [ ] **Step 6: Add the field to the `EventRecord` TypedDict**

In `src/filigree/types/events.py`, add to `EventRecord` (line ~104, after `actor: str`):
```python
    id: int
    issue_id: str
    event_type: EventType
    actor: str
    verified_actor: str | None
    old_value: str | None
    new_value: str | None
    comment: str
    created_at: ISOTimestamp
```
(`EventRecordWithTitle` inherits `EventRecord`, so it gets the field automatically.)

- [ ] **Step 7: Run the tests + mypy to verify they pass**

Run: `uv run pytest tests/core/test_verified_actor.py -k "event" -v && uv run mypy src/filigree/db_events.py src/filigree/types/events.py`
Expected: PASS; mypy clean.

- [ ] **Step 8: Commit**

```bash
git add src/filigree/db_events.py src/filigree/types/events.py tests/core/test_verified_actor.py
git commit -m "feat: stamp verified_actor on events + expose on read path (ADR-012)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Stamp `comments` (primary + observation-link) + project `verified_author`

**Files:**
- Modify: `src/filigree/db_meta.py` (`add_comment` INSERT; `get_comments`/`get_comment` projection)
- Modify: `src/filigree/db_observations.py` (observation-link comment INSERT, line ~840)
- Modify: `src/filigree/types/planning.py` (`CommentRecord`)
- Test: `tests/core/test_verified_actor.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_verified_actor.py`:
```python
def test_add_comment_stamps_verified_author(db: FiligreeDB) -> None:
    db.set_verified_actor("alice")
    issue_id = _create_issue(db)
    db.add_comment(issue_id, "hello", author="agent-x")
    row = db.conn.execute(
        "SELECT verified_author FROM comments WHERE issue_id = ?", (issue_id,)
    ).fetchone()
    assert row["verified_author"] == "alice"


def test_get_comments_exposes_verified_author(db: FiligreeDB) -> None:
    db.set_verified_actor("alice")
    issue_id = _create_issue(db)
    db.add_comment(issue_id, "hello", author="agent-x")
    comments = db.get_comments(issue_id)
    assert comments[0]["verified_author"] == "alice"


def test_comment_verified_author_null_when_unset(db: FiligreeDB) -> None:
    issue_id = _create_issue(db)
    db.add_comment(issue_id, "hello", author="agent-x")
    comments = db.get_comments(issue_id)
    assert comments[0]["verified_author"] is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/core/test_verified_actor.py -k "comment or author" -v`
Expected: FAIL — `verified_author` not stamped / `KeyError: 'verified_author'`.

- [ ] **Step 3: Stamp `add_comment`**

In `src/filigree/db_meta.py`, replace the `add_comment` INSERT (line ~58-61):
```python
        cursor = self.conn.execute(
            "INSERT INTO comments (issue_id, author, verified_author, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (issue_id, author, self._verified_actor, text, now),
        )
```

- [ ] **Step 4: Project `verified_author` in `get_comments` and `get_comment`**

In `src/filigree/db_meta.py`, replace `get_comments` (line ~68-73):
```python
    def get_comments(self, issue_id: str) -> list[CommentRecord]:
        rows = self.conn.execute(
            "SELECT id, author, verified_author, text, created_at FROM comments WHERE issue_id = ? ORDER BY created_at",
            (issue_id,),
        ).fetchall()
        return [
            CommentRecord(
                id=r["id"],
                author=r["author"],
                verified_author=r["verified_author"],
                text=r["text"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
```

Replace `get_comment` (line ~75-83):
```python
    def get_comment(self, comment_id: int) -> CommentRecord:
        row = self.conn.execute(
            "SELECT id, author, verified_author, text, created_at FROM comments WHERE id = ?",
            (comment_id,),
        ).fetchone()
        if row is None:
            msg = f"Comment not found: {comment_id}"
            raise KeyError(msg)
        return CommentRecord(
            id=row["id"],
            author=row["author"],
            verified_author=row["verified_author"],
            text=row["text"],
            created_at=row["created_at"],
        )
```

- [ ] **Step 5: Stamp the observation-link comment**

In `src/filigree/db_observations.py`, replace the comment INSERT (line ~839-842):
```python
            self.conn.execute(
                "INSERT INTO comments (issue_id, author, verified_author, text, created_at) VALUES (?, ?, ?, ?, ?)",
                (issue_id, actor, self._verified_actor, comment, now),
            )
```

- [ ] **Step 6: Add the field to `CommentRecord`**

In `src/filigree/types/planning.py`, add to `CommentRecord` (line ~139, after `author: str`):
```python
class CommentRecord(TypedDict):
    """Row from the comments table returned by ``get_comments()``."""

    id: int
    author: str
    verified_author: str | None
    text: str
    created_at: ISOTimestamp
```

- [ ] **Step 7: Run the tests + mypy to verify they pass**

Run: `uv run pytest tests/core/test_verified_actor.py -k "comment or author" -v && uv run mypy src/filigree/db_meta.py src/filigree/db_observations.py src/filigree/types/planning.py`
Expected: PASS; mypy clean.

- [ ] **Step 8: Commit**

```bash
git add src/filigree/db_meta.py src/filigree/db_observations.py src/filigree/types/planning.py tests/core/test_verified_actor.py
git commit -m "feat: stamp verified_author on comments + expose on read (ADR-012)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Stamp `file_events`, `annotation_events`, `observations`

**Files:**
- Modify: `src/filigree/db_files.py` (`_record_registry_fallback_event` line ~286; file-metadata-update insert line ~566)
- Modify: `src/filigree/db_annotations.py` (`_record_annotation_event` line ~562)
- Modify: `src/filigree/db_observations.py` (`observe` INSERT line ~368 + return dict line ~439)
- Modify: `src/filigree/types/core.py` (`ObservationDict`)
- Test: `tests/core/test_verified_actor.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_verified_actor.py`:
```python
def test_observation_stamps_and_exposes_verified_actor(db: FiligreeDB) -> None:
    db.set_verified_actor("alice")
    obs = db.observe(summary="smell in foo.py", actor="agent-x")
    assert obs["verified_actor"] == "alice"
    # Read-back via list also carries it.
    listed = db.list_observations()
    assert listed[0]["verified_actor"] == "alice"


def test_observation_verified_actor_null_when_unset(db: FiligreeDB) -> None:
    obs = db.observe(summary="another smell", actor="agent-x")
    assert obs["verified_actor"] is None
```

> Adjust `db.observe(...)` to the real signature (check `tests/core/test_observations.py`). `file_events` and `annotation_events` stamping is covered structurally below; add a direct column-read assertion for those if the suite has an easy file-update / annotation-event entry point (e.g. `db.update_file(...)`, `db.create_annotation(...)` then an annotation mutation). If a convenient entry point is not obvious, assert at minimum that the stamped column exists and is NULL/value via a raw `db.conn.execute("SELECT verified_actor FROM file_events ...")` after the relevant operation.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/core/test_verified_actor.py -k "observation" -v`
Expected: FAIL — `KeyError: 'verified_actor'` on the returned dict / column stored NULL when set.

- [ ] **Step 3: Stamp `file_events` — registry-fallback event**

In `src/filigree/db_files.py`, replace `_record_registry_fallback_event`'s INSERT (line ~285-289):
```python
        self.conn.execute(
            "INSERT INTO file_events "
            "(file_id, event_type, field, old_value, new_value, actor, verified_actor, created_at) "
            "VALUES (?, 'registry_local_fallback', 'registry_backend', 'clarion', 'local', ?, ?, ?)",
            (file_id, actor, self._verified_actor, now),
        )
```

- [ ] **Step 4: Stamp `file_events` — file-metadata-update event**

In `src/filigree/db_files.py`, replace the metadata-update INSERT (line ~565-570):
```python
                self.conn.execute(
                    "INSERT INTO file_events "
                    "(file_id, event_type, field, old_value, new_value, actor, verified_actor, created_at) "
                    "VALUES (?, 'file_metadata_update', ?, ?, ?, ?, ?, ?)",
                    (existing["id"], field, old_val, new_val, actor, self._verified_actor, now),
                )
```

- [ ] **Step 5: Stamp `annotation_events`**

In `src/filigree/db_annotations.py`, replace the `_record_annotation_event` INSERT (line ~561-566):
```python
        self.conn.execute(
            "INSERT INTO annotation_events "
            "(id, annotation_id, event_type, actor, verified_actor, reason, old_value, new_value, target_type, target_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, annotation_id, event_type, actor, self._verified_actor, reason, old_text, new_text, target_type, target_id, now),
        )
```

- [ ] **Step 6: Stamp `observations` INSERT**

In `src/filigree/db_observations.py`, replace the `observe` INSERT (lines ~368-385). Add `verified_actor` to the column list (after `actor`) and `self._verified_actor` to the values tuple (after `actor`):
```python
            self.conn.execute(
                "INSERT INTO observations (id, summary, detail, file_id, file_path, line, "
                "source_issue_id, source_finding_id, priority, actor, verified_actor, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    obs_id,
                    summary_stripped,
                    detail,
                    linked_file_id,
                    file_path,
                    line,
                    source_issue_id,
                    source_finding_id,
                    priority,
                    actor,
                    self._verified_actor,
                    now,
                    expires,
                ),
            )
```

- [ ] **Step 7: Stamp the `observe` return dict**

In `src/filigree/db_observations.py`, the hand-built return dict (line ~439-452) must include the field so the value returned to the caller matches what was stored:
```python
        return {
            "id": obs_id,
            "summary": summary_stripped,
            "detail": detail,
            "file_id": linked_file_id,
            "file_path": file_path,
            "line": line,
            "source_issue_id": source_issue_id,
            "source_finding_id": source_finding_id,
            "priority": priority,
            "actor": actor,
            "verified_actor": self._verified_actor,
            "created_at": now,
            "expires_at": ISOTimestamp(expires),
        }
```

> The `list_observations` / `get_observations_by_ids` / dedup-winner read paths use `cast(ObservationDict, dict(row))` over `SELECT *`, so they pick up `verified_actor` automatically once the column exists and the TypedDict declares it — no query change needed there.

- [ ] **Step 8: Add the field to `ObservationDict`**

In `src/filigree/types/core.py`, add to `ObservationDict` (line ~221, after `actor: str`):
```python
    priority: int
    actor: str
    verified_actor: str | None
    created_at: ISOTimestamp
    expires_at: ISOTimestamp
```

- [ ] **Step 9: Run the tests + mypy to verify they pass**

Run: `uv run pytest tests/core/test_verified_actor.py -v && uv run mypy src/filigree/db_files.py src/filigree/db_annotations.py src/filigree/db_observations.py src/filigree/types/core.py`
Expected: PASS; mypy clean.

- [ ] **Step 10: Commit**

```bash
git add src/filigree/db_files.py src/filigree/db_annotations.py src/filigree/db_observations.py src/filigree/types/core.py tests/core/test_verified_actor.py
git commit -m "feat: stamp verified_actor on file_events, annotation_events, observations (ADR-012)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Import round-trip preservation + document system-NULL writers

**Files:**
- Modify: `src/filigree/db_meta.py` (`import_jsonl`: event stage line ~1330; comment stages lines ~1296 merge / ~1314 non-merge; file_event stages lines ~1387 merge / ~1410 non-merge)
- Modify: `src/filigree/finding_issue_cascade.py` (comment to document NULL is intentional — no code change)
- Test: `tests/core/test_verified_actor.py`

**Rationale (decision 4/5):** export uses `SELECT *`, so a backup carries `verified_*`. Import is *restoring a recorded audit trail*, so it must preserve the stored value (`record.get("verified_*")`), exactly as `migration_orphaned_at` is preserved at `db_meta.py:1376`. System/cascade writes (reconciliation debt, migrations) create *new* records with no transport proof and stay NULL.

- [ ] **Step 1: Write the failing test (export → import round-trip)**

Append to `tests/core/test_verified_actor.py`:
```python
def test_export_import_round_trips_verified_actor(tmp_path: Path) -> None:
    src_dir = tmp_path / "src" / ".filigree"
    src_dir.mkdir(parents=True)
    src = FiligreeDB.from_filigree_dir(src_dir)
    src.set_verified_actor("alice")
    issue = src.create_issue(title="t", actor="agent-x")
    src.add_comment(issue.id, "hello", author="agent-x")
    export_path = tmp_path / "dump.jsonl"
    src.export_jsonl(export_path)

    dst_dir = tmp_path / "dst" / ".filigree"
    dst_dir.mkdir(parents=True)
    dst = FiligreeDB.from_filigree_dir(dst_dir)
    # Import must NOT stamp the importer's identity; it restores the recorded one.
    dst.set_verified_actor("bob")
    dst.import_jsonl(export_path)

    ev = dst.conn.execute(
        "SELECT verified_actor FROM events WHERE issue_id = ? AND event_type = 'created'",
        (issue.id,),
    ).fetchone()
    assert ev["verified_actor"] == "alice"
    cm = dst.conn.execute(
        "SELECT verified_author FROM comments WHERE issue_id = ?", (issue.id,)
    ).fetchone()
    assert cm["verified_author"] == "alice"
```

> Adjust `import_jsonl` / `export_jsonl` call shapes to the real API (check `tests/cli/test_admin_commands.py` for the canonical round-trip idiom; the CLI verbs are `admin export-jsonl` / `admin import-jsonl`). If a v23 export fixture without `verified_*` keys is asserted elsewhere, the `record.get(...)` default of `None` keeps it loading cleanly — add an assertion that a record lacking the key imports as NULL.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/core/test_verified_actor.py -k "round_trip" -v`
Expected: FAIL — imported rows read NULL because the import INSERTs omit `verified_*`.

- [ ] **Step 3: Preserve `verified_actor` on event import**

In `src/filigree/db_meta.py`, the `event` import stage (line ~1330-1344), add the column + `record.get`:
```python
                cursor = self.conn.execute(
                    f"INSERT {conflict} INTO events "
                    "(issue_id, event_type, actor, verified_actor, old_value, new_value, comment, created_at, event_seq) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.get("issue_id", ""),
                        record.get("event_type", ""),
                        record.get("actor", ""),
                        record.get("verified_actor"),
                        record.get("old_value"),
                        record.get("new_value"),
                        record.get("comment", ""),
                        _normalize_iso_to_utc(record.get("created_at")) or _now_iso(),
                        int(record.get("event_seq", 0)),
                    ),
                )
```

- [ ] **Step 4: Preserve `verified_author` on comment import (both branches)**

In `src/filigree/db_meta.py`, the comment merge branch (line ~1296-1310):
```python
                    cursor = self.conn.execute(
                        "INSERT INTO comments (issue_id, author, verified_author, text, created_at) "
                        "SELECT ?, ?, ?, ?, ? "
                        "WHERE NOT EXISTS ("
                        "  SELECT 1 FROM comments WHERE issue_id = ? AND author = ? AND text = ? AND created_at = ?"
                        ")",
                        (
                            record.get("issue_id", ""),
                            record.get("author", ""),
                            record.get("verified_author"),
                            record.get("text", ""),
                            created,
                            record.get("issue_id", ""),
                            record.get("author", ""),
                            record.get("text", ""),
                            created,
                        ),
                    )
```

The comment non-merge branch (line ~1314-1322):
```python
                    cursor = self.conn.execute(
                        "INSERT INTO comments (issue_id, author, verified_author, text, created_at) VALUES (?, ?, ?, ?, ?)",
                        (
                            record.get("issue_id", ""),
                            record.get("author", ""),
                            record.get("verified_author"),
                            record.get("text", ""),
                            _normalize_iso_to_utc(record.get("created_at")) or _now_iso(),
                        ),
                    )
```

- [ ] **Step 5: Preserve `verified_actor` on file_event import (both branches)**

In `src/filigree/db_meta.py`, the file_event merge branch (line ~1387-1406):
```python
                    cursor = self.conn.execute(
                        "INSERT INTO file_events (file_id, event_type, field, old_value, new_value, verified_actor, created_at) "
                        "SELECT ?, ?, ?, ?, ?, ?, ? "
                        "WHERE NOT EXISTS ("
                        "  SELECT 1 FROM file_events "
                        "  WHERE file_id = ? AND event_type = ? AND field = ? AND old_value = ? AND new_value = ? AND created_at = ?"
                        ")",
                        (
                            file_id,
                            record.get("event_type", "file_metadata_update"),
                            record.get("field", ""),
                            record.get("old_value", ""),
                            record.get("new_value", ""),
                            record.get("verified_actor"),
                            created,
                            file_id,
                            record.get("event_type", "file_metadata_update"),
                            record.get("field", ""),
                            record.get("old_value", ""),
                            record.get("new_value", ""),
                            created,
                        ),
                    )
```

The file_event non-merge branch (line ~1410 onward — the `INSERT INTO file_events ... VALUES (?, ?, ?, ?, ?, ?)`):
```python
                    cursor = self.conn.execute(
                        "INSERT INTO file_events (file_id, event_type, field, old_value, new_value, verified_actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            file_id,
                            record.get("event_type", "file_metadata_update"),
                            record.get("field", ""),
                            record.get("old_value", ""),
                            record.get("new_value", ""),
                            record.get("verified_actor"),
                            _normalize_iso_to_utc(record.get("created_at")) or _now_iso(),
                        ),
                    )
```

> Note: the file_event import omits `actor` entirely (it always has, pre-existing behavior). Do not add `actor` here — only `verified_actor`, matching the scope of this change. If the existing non-merge branch's value tuple differs from the snippet above, preserve its exact existing columns and only insert `verified_actor` + its `record.get("verified_actor")` in the matching position.

- [ ] **Step 6: Document the system-NULL writer**

In `src/filigree/finding_issue_cascade.py`, add a clarifying comment above the `record_reconciliation_debt_comment` INSERT (line ~55) — no behavior change:
```python
        # ADR-012: reconciliation-debt is a system-authored cascade write with no
        # transport proof (bare conn, system actor). verified_author is left NULL
        # intentionally — this is a NEW record, not a restored one.
        conn.execute(
            "INSERT INTO comments (issue_id, author, text, created_at) VALUES (?, ?, ?, ?)",
            (issue_id, actor, f"{RECONCILIATION_DEBT_PREFIX} {text}", _now_iso()),
        )
```

- [ ] **Step 7: Run the tests + mypy to verify they pass**

Run: `uv run pytest tests/core/test_verified_actor.py -k "round_trip" -v && uv run mypy src/filigree/db_meta.py src/filigree/finding_issue_cascade.py`
Expected: PASS; mypy clean.

- [ ] **Step 8: Commit**

```bash
git add src/filigree/db_meta.py src/filigree/finding_issue_cascade.py tests/core/test_verified_actor.py
git commit -m "feat: preserve verified_* across export/import; document system-NULL writers (ADR-012)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Entry points — resolve identity (CLI + MCP-stdio) + mismatch warning

**Files:**
- Modify: `src/filigree/cli_common.py` (`get_db` sets verified actor)
- Modify: `src/filigree/cli.py` (`cli()` group callback emits mismatch warning to stderr)
- Modify: `src/filigree/mcp_server.py` (`_init_db` sets verified actor; `call_tool` logs mismatch)
- Test: `tests/cli/test_verified_actor_cli.py`, `tests/mcp/test_verified_actor_mcp.py`

- [ ] **Step 1: Write the failing CLI test**

Create `tests/cli/test_verified_actor_cli.py`. Use the suite's existing CLI runner idiom (check `tests/cli/test_admin_commands.py` for the `CliRunner` + temp-project fixture pattern). Core assertions:
```python
"""CLI transport-bound actor identity tests (ADR-012, schema v24)."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from filigree.cli import cli


def _init_project(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(cli, ["init"], catch_exceptions=False)
    assert result.exit_code == 0


def test_cli_write_stamps_verified_actor(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("filigree.actor_identity.resolve_os_actor", lambda: "alice")
    runner = CliRunner()
    _init_project(runner, tmp_path)
    result = runner.invoke(cli, ["--actor", "alice", "create", "task", "t"], catch_exceptions=False)
    assert result.exit_code == 0

    from filigree.cli_common import get_db

    db = get_db()
    row = db.conn.execute("SELECT verified_actor FROM events WHERE event_type = 'created' LIMIT 1").fetchone()
    assert row["verified_actor"] == "alice"


def test_cli_mismatch_warns_on_stderr_but_does_not_block(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("filigree.actor_identity.resolve_os_actor", lambda: "alice")
    runner = CliRunner(mix_stderr=False)
    _init_project(runner, tmp_path)
    result = runner.invoke(cli, ["--actor", "agent-x", "create", "task", "t"], catch_exceptions=False)
    assert result.exit_code == 0  # never blocks
    assert "ACTOR_MISMATCH" in result.stderr


def test_cli_no_warning_for_placeholder_default_actor(tmp_path, monkeypatch) -> None:
    # The 'cli' default is a framework placeholder, not a genuine claim — quiet
    # even though it differs from the verified OS user (decision 9).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("filigree.actor_identity.resolve_os_actor", lambda: "alice")
    runner = CliRunner(mix_stderr=False)
    _init_project(runner, tmp_path)
    result = runner.invoke(cli, ["create", "task", "t"], catch_exceptions=False)  # no --actor → "cli"
    assert result.exit_code == 0
    assert "ACTOR_MISMATCH" not in result.stderr
```

> Adjust `init` / `create` verbs and argument order to the real CLI surface (check `tests/cli/`). The `resolve_os_actor` monkeypatch target must match the import site actually used by `get_db` / the `cli()` callback — patch where it is *looked up*, not where it is defined (see Step 3/4 for the import choice).

- [ ] **Step 2: Run the CLI test to verify it fails**

Run: `uv run pytest tests/cli/test_verified_actor_cli.py -v`
Expected: FAIL — `verified_actor` stored NULL (not set in `get_db`); no `ACTOR_MISMATCH` on stderr.

- [ ] **Step 3: Set verified actor in `get_db()`**

In `src/filigree/cli_common.py`, import the resolver at the top of the file (with the other imports):
```python
from filigree import actor_identity
```
Then in `get_db()`, set the verified actor on the constructed DB before returning. Replace the success arm (lines ~205-208):
```python
    try:
        if conf_path is not None:
            database = FiligreeDB.from_conf(conf_path)
        else:
            database = FiligreeDB.from_filigree_dir(project_root / FILIGREE_DIR_NAME)
        database.set_verified_actor(actor_identity.resolve_os_actor())
        return database
```

- [ ] **Step 4: Emit the mismatch warning in the `cli()` group callback**

In `src/filigree/cli.py`, the `cli()` group callback (line ~81) already has the sanitized claimed `actor`. After `ctx.obj["actor"] = cleaned` (line ~97), add the mismatch check (logs to stderr, never blocks):
```python
    ctx.obj["actor"] = cleaned

    # ADR-012: surface a non-blocking warning when the claimed --actor disagrees
    # with the transport-verified OS identity. Resolution and the warning never
    # raise and never block the command.
    from filigree import actor_identity

    _verified = actor_identity.resolve_os_actor()
    _warning = actor_identity.actor_mismatch_warning(cleaned, _verified)
    if _warning is not None:
        click.echo(
            f"warning: {_warning['code']} claimed={_warning['claimed']!r} verified={_warning['verified']!r}",
            err=True,
        )
```

> The CLI resolves `resolve_os_actor()` twice (once here, once in `get_db`); both are best-effort and cheap. If you prefer a single resolution, stash it on `ctx.obj["verified_actor"]` here and have `get_db` read it — but `get_db` has no `ctx` access, so the two-call form is simpler and acceptable.

- [ ] **Step 5: Run the CLI test to verify it passes**

Run: `uv run pytest tests/cli/test_verified_actor_cli.py -v`
Expected: PASS.

- [ ] **Step 6: Write the failing MCP-stdio test**

Create `tests/mcp/test_verified_actor_mcp.py`. Use the suite's MCP harness (check `tests/mcp/conftest.py` and `tests/mcp/test_tools.py` for how `_init_db` / `call_tool` are exercised). Core assertions:
```python
"""MCP-stdio transport-bound actor identity tests (ADR-012, schema v24)."""

from __future__ import annotations

import filigree.mcp_server as mcp_server


def test_init_db_sets_verified_actor(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("filigree.actor_identity.resolve_os_actor", lambda: "alice")
    # Stand up a project + call the server's DB-init entry point.
    # (Use the conftest helper that creates a project and calls mcp_server._init_db.)
    filigree_dir = _init_project_for_mcp(tmp_path)  # provided by tests/mcp/conftest.py
    mcp_server._init_db(filigree_dir, None)
    assert mcp_server.db is not None
    assert mcp_server.db._verified_actor == "alice"


async def test_call_tool_injects_actor_mismatch_warning(tmp_path, monkeypatch) -> None:
    import json

    monkeypatch.setattr("filigree.actor_identity.resolve_os_actor", lambda: "alice")
    filigree_dir = _init_project_for_mcp(tmp_path)
    mcp_server._init_db(filigree_dir, None)
    # A write tool with a genuine, distinct claimed actor → mismatch warning in
    # the envelope; the call still succeeds (never blocks).
    result = await mcp_server.call_tool("issue_create", {"type": "task", "title": "t", "actor": "agent-x"})
    payload = json.loads(result[0].text)
    assert "warnings" in payload
    assert any(w["code"] == "ACTOR_MISMATCH" for w in payload["warnings"])


async def test_call_tool_no_warning_for_placeholder_actor(tmp_path, monkeypatch) -> None:
    import json

    monkeypatch.setattr("filigree.actor_identity.resolve_os_actor", lambda: "alice")
    filigree_dir = _init_project_for_mcp(tmp_path)
    mcp_server._init_db(filigree_dir, None)
    result = await mcp_server.call_tool("issue_create", {"type": "task", "title": "t", "actor": "mcp"})
    payload = json.loads(result[0].text)
    assert "ACTOR_MISMATCH" not in result[0].text  # placeholder claim → no warning
```

> `_init_db`'s real signature is `_init_db(filigree_dir, conf_path)` (`mcp_server.py:1141`); confirm the exact parameter names/order in situ and match the conftest helper. If the conftest exposes a higher-level "boot the server against tmp project" fixture, use it instead of calling `_init_db` directly. The two `call_tool` tests are `async` — match the suite's async-test convention (`@pytest.mark.asyncio` or the configured `asyncio_mode`; check `tests/mcp/test_tools.py`). Use the canonical tool name and argument shape that suite already exercises (`issue_create` is the namespaced name; if the suite calls the legacy `create_issue`, use that — `call_tool` canonicalizes either).

- [ ] **Step 7: Run the MCP test to verify it fails**

Run: `uv run pytest tests/mcp/test_verified_actor_mcp.py -v`
Expected: FAIL — `mcp_server.db._verified_actor` is `None` (never set).

- [ ] **Step 8: Set verified actor in MCP `_init_db`**

In `src/filigree/mcp_server.py`, in `_init_db` (line ~1144-1148), after the successful construction, set the verified actor:
```python
    try:
        db = FiligreeDB.from_conf(conf_path) if conf_path is not None else FiligreeDB.from_filigree_dir(filigree_dir)
        from filigree.actor_identity import resolve_os_actor

        db.set_verified_actor(resolve_os_actor())
        _schema_mismatch = None
        _registry_startup_error = None
        _db_open_error = None
```

- [ ] **Step 9: Add the `_inject_warnings` envelope helper**

In `src/filigree/mcp_tools/common.py` (which already imports `json` and `TextContent`), add a generic warning-injection helper near `_text`:
```python
def _inject_warnings(result: list[TextContent], warnings: list[dict[str, Any]]) -> list[TextContent]:
    """Add a top-level ``warnings`` array to a tool's JSON envelope.

    Post-processing hook so warning producers (e.g. ADR-012 actor mismatch) need
    not touch every handler. Parses the first text element; if it is a JSON
    object, appends to (or creates) its ``warnings`` list. Bare-string and
    non-object responses are returned untouched. Never raises.
    """
    if not warnings or not result:
        return result
    first = result[0]
    if first.type != "text":
        return result
    try:
        payload = json.loads(first.text)
    except (json.JSONDecodeError, ValueError):
        return result  # bare-string response — leave untouched
    if not isinstance(payload, dict):
        return result
    existing = payload.get("warnings")
    payload["warnings"] = (existing if isinstance(existing, list) else []) + warnings
    return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str)), *result[1:]]
```
(Ensure `Any` is imported in that module — it almost certainly already is; if not, add `from typing import Any`.)

- [ ] **Step 10: Inject the mismatch warning in `call_tool`**

In `src/filigree/mcp_server.py`, in `call_tool`'s inner `_run` (line ~944), wrap the handler result so a mismatch warning is added to the envelope. Replace the `out = await handler(arguments)` line:
```python
    async def _run() -> list[TextContent]:
        try:
            out: list[TextContent] = await handler(arguments)
            # ADR-012: surface a non-blocking actor mismatch in the response
            # envelope's ``warnings`` list. Best-effort — never break a tool call.
            try:
                from filigree.actor_identity import actor_mismatch_warning
                from filigree.mcp_tools.common import _inject_warnings

                run_db = _request_db.get() or db
                if run_db is not None:
                    mismatch = actor_mismatch_warning(arguments.get("actor"), run_db._verified_actor)
                    if mismatch is not None:
                        out = _inject_warnings(out, [dict(mismatch)])
            except Exception:
                pass
            return out
        except Exception:
            if _logger:
                _logger.error("tool_error", extra={"tool": name, "args_data": arguments}, exc_info=True)
            raise
        finally:
            # Safety net: roll back any uncommitted transaction left by a
            # failed mutation. Re-resolve _get_db() in case the handler
            # switched the ContextVar-scoped DB.
            resolved = _request_db.get() or db
            if resolved is not None and resolved.conn.in_transaction:
                resolved.conn.rollback()
```

- [ ] **Step 11: Run the MCP + CLI tests + mypy to verify they pass**

Run: `uv run pytest tests/mcp/test_verified_actor_mcp.py tests/cli/test_verified_actor_cli.py -v && uv run mypy src/filigree/cli_common.py src/filigree/cli.py src/filigree/mcp_server.py src/filigree/mcp_tools/common.py`
Expected: PASS; mypy clean.

- [ ] **Step 12: Commit**

```bash
git add src/filigree/cli_common.py src/filigree/cli.py src/filigree/mcp_server.py src/filigree/mcp_tools/common.py tests/cli/test_verified_actor_cli.py tests/mcp/test_verified_actor_mcp.py
git commit -m "feat: resolve+stamp verified actor at CLI/MCP entry points; envelope mismatch warning (ADR-012)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Docs, CHANGELOG, ADR-012, and contract fixtures

**Files:**
- Modify: `docs/SCHEMA_MIGRATIONS.md`
- Modify: `CHANGELOG.md`
- Modify: ADR-012 source (find with the grep in Step 1)
- Regenerate/verify: contract fixtures under `tests/fixtures/contracts/`

- [ ] **Step 1: Locate the ADR and contract-fixture generators**

Run:
```bash
cd /home/john/filigree
grep -rl "ADR-012" docs/
ls tests/fixtures/contracts/classic tests/fixtures/contracts/loom
grep -rn "verified_actor\|verified_author" tests/fixtures/contracts/ || echo "no fixture hits yet"
grep -rn "regen\|generate" tests/util/test_type_contracts.py | head
```
Expected: the ADR-012 file path; the fixture layout; whether any fixture already mentions the new columns (it should not yet); how contract fixtures are regenerated (look for a `--regen`/`UPDATE_FIXTURES` env or a generator script referenced by the contract tests).

- [ ] **Step 2: Add a `## v24` entry to `docs/SCHEMA_MIGRATIONS.md`**

Match the format of the existing v23 entry. Content:
```markdown
## v24 — Transport-bound actor identity (ADR-012)

Adds a nullable `verified_*` column to every runtime event-bearing table:
`events.verified_actor`, `file_events.verified_actor`,
`annotation_events.verified_actor`, `comments.verified_author`,
`observations.verified_actor`.

The `actor`/`author` claimed value is unchanged. `verified_*` holds the
transport-verified identity (the OS user the writing process ran as) or `NULL`
when no transport proof exists — which is the value for every historical row
(no backfill), every unverified surface, and every system/cascade-authored
write. The `events` dedup unique index is **not** extended: `verified_actor`
is attribution metadata, not part of event identity.

Migration `migrate_v23_to_v24` is additive and idempotent. Backup/restore
(`export-jsonl` / `import-jsonl`) preserves `verified_*` verbatim.
```

- [ ] **Step 3: Add a CHANGELOG entry**

Add under the appropriate 3.0.0 section of `CHANGELOG.md` (match the existing heading; this is part of the 3.0.0 breaking bundle, so note the schema bump):
```markdown
### Added
- Transport-bound actor identity (ADR-012, schema v24): every runtime write now
  records a `verified_*` column alongside the claimed `actor`/`author`, holding
  the OS-user identity the process verifiably ran as (or `NULL` when no
  transport proof exists). A non-blocking `ACTOR_MISMATCH` warning is emitted
  when the claimed and verified identities disagree (CLI: stderr; MCP-stdio:
  structured log). No backfill; the `events` dedup index is unchanged.
```

- [ ] **Step 4: Extend ADR-012**

Append a "v24 increment" / "Implementation status" subsection to the ADR-012 file located in Step 1, recording: schema v24 columns, session-level `_verified_actor` plumbing, CLI + MCP-stdio resolvers, the record-both-and-warn (never block) conflict policy, and the explicit **out-of-scope** items (MCP-HTTP peer identity, dashboard auth, envelope `warnings` channel — decision 6).

- [ ] **Step 5: Regenerate contract fixtures (if generation changed their content)**

The bulk `export_jsonl` uses `SELECT *`, so any contract fixture derived from exported rows or from the TypedDict shapes now includes the new fields. Regenerate per the mechanism found in Step 1 (e.g. an env-gated regen, or a generator under `src/filigree/generations/`). Then:

Run: `uv run pytest tests/util/test_type_contracts.py tests/util/test_docs_contracts.py tests/test_error_envelope_contract.py -v`
Expected: PASS. If a contract test fails because a fixture is stale, regenerate it (do not hand-edit generated fixtures unless the test contract says to), confirm the diff only adds `verified_*`, and re-run.

- [ ] **Step 6: Commit**

```bash
git add docs/SCHEMA_MIGRATIONS.md CHANGELOG.md docs/ tests/fixtures/contracts/
git commit -m "docs: document v24 verified-actor schema (SCHEMA_MIGRATIONS, CHANGELOG, ADR-012); refresh contract fixtures

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Final verification (full gate before declaring done)

- [ ] **Step 1: Run the full CI pipeline**

Run:
```bash
cd /home/john/filigree
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/filigree/
uv run pytest --tb=short
```
Expected: all green. (No JS touched, so the biome gate does not apply.)

- [ ] **Step 2: Confirm the schema-version + migration round-trip end to end**

Run: `uv run pytest tests/core/test_schema.py tests/core/test_verified_actor.py tests/core/test_actor_identity.py tests/cli/test_verified_actor_cli.py tests/mcp/test_verified_actor_mcp.py -v`
Expected: all PASS.

- [ ] **Step 3: Spot-check no system writer regressed to a non-NULL stamp**

Run: `uv run pytest tests/core/test_observations.py tests/core/test_annotations.py tests/cli/test_admin_commands.py -q`
Expected: PASS (import/export, observation, annotation paths unaffected except for the additive column).

---

## Spec coverage map (self-review)

| Spec section | Covered by |
|---|---|
| §3 schema v24 — 5 tables, version bump, CREATE + migration, dedup index untouched | Task 1 |
| §3 `observation_actor` left as-is; `actor`→`verified_actor` pairing | Task 6 (only `observations.actor` paired; `observation_actor` on `observation_links` is out of scope, unchanged) |
| §4 session plumbing — `__init__`, setter, `borrow_for_worker_thread` propagation | Task 3 |
| §4 every runtime insert stamps `self._verified_actor` | Tasks 4, 5, 6 |
| §5 `actor_identity.py` resolver; CLI + MCP-stdio set it | Tasks 2, 8 |
| §5 Windows-safe resolver (`None`, no crash) | Task 2 |
| §6 mismatch warning at entry point, never blocks; shared helper; envelope `warnings` list | Tasks 2, 8 (CLI=stderr; MCP=envelope `warnings` via `_inject_warnings`, decision 6; placeholder claims suppressed, decision 9) |
| §6 read path — `EventRecord` + comment/observation projections; historical → null | Tasks 4, 5, 6 |
| §7 insert sites (events, file_events×2, annotation_events, observations, comments×2) | Tasks 4, 5, 6 |
| §7 db_meta/cascade per-site decision; migrations excluded | Task 7 (import = preserve; cascade/migrations = NULL) |
| §8 testing items 1–10 | Tasks 1 (1,2,10-schema), 3 (5), 4 (3,8), 6, 7, 8 (3,4,6), 9 (10-docs/fixtures); item 9 (resolver robustness) → Task 2 |
| §9 files touched | All tasks; **plus `db_base.py`** (decision 2, Task 3) |
| §10 non-goals (no HTTP/dashboard auth, no blocking, no backfill, no dedup-index change) | Honored throughout; decision 6 records the HTTP deferral explicitly |
