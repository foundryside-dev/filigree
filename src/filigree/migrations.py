"""Schema migration framework for filigree.

Migrations are version-keyed functions that transform the database schema
from one version to the next. Each migration receives a raw sqlite3.Connection
and must be idempotent (safe to re-run, using IF NOT EXISTS / IF EXISTS).

The migration runner:
  1. Reads the current schema version via PRAGMA user_version
  2. Applies each pending migration in order
  3. Bumps user_version after each successful migration
  4. Wraps each migration in a transaction (rollback on failure)

Usage — adding a new migration:
  1. Increment CURRENT_SCHEMA_VERSION in db_schema.py
  2. Add a function here: def migrate_v<N>_to_v<N+1>(conn) -> None
  3. Register it in MIGRATIONS: N: migrate_v<N>_to_v<N+1>
  4. Update SCHEMA_SQL in db_schema.py to match the post-migration state
  5. Add a test in tests/core/test_schema.py

SQLite ALTER TABLE limitations (why helpers exist):
  - ADD COLUMN: supported (but only with constant defaults, no NOT NULL without DEFAULT)
  - DROP COLUMN: supported only in SQLite >= 3.35.0
  - RENAME COLUMN: supported since SQLite 3.25.0
  - ALTER column type/constraints: NOT supported — use rebuild_table()
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Migration function protocol
# ---------------------------------------------------------------------------


class MigrationFn(Protocol):
    """Protocol for migration functions."""

    def __call__(self, conn: sqlite3.Connection) -> None: ...


# ---------------------------------------------------------------------------
# Migration registry
#
# Keys are the version being migrated FROM (i.e., the current user_version).
# Values are functions that transform the schema to the next version.
#
# Example: {1: migrate_v1_to_v2} means "if user_version == 1, run this to get to 2"
# ---------------------------------------------------------------------------


def migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """v1 → v2: Add file records, scan findings, and file associations tables.

    Changes:
      - new table 'file_records' for tracking source code files
      - new table 'scan_findings' for security/code quality scan results
      - new table 'file_associations' for linking files to issues
      - indexes for efficient querying
    """
    conn.execute("""\
        CREATE TABLE IF NOT EXISTS file_records (
            id          TEXT PRIMARY KEY,
            path        TEXT NOT NULL UNIQUE,
            language    TEXT DEFAULT '',
            file_type   TEXT DEFAULT '',
            first_seen  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            metadata    TEXT DEFAULT '{}'
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_records_path ON file_records(path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_records_language ON file_records(language)")

    conn.execute("""\
        CREATE TABLE IF NOT EXISTS scan_findings (
            id            TEXT PRIMARY KEY,
            file_id       TEXT NOT NULL REFERENCES file_records(id),
            issue_id      TEXT REFERENCES issues(id) ON DELETE SET NULL,
            scan_source   TEXT NOT NULL DEFAULT '',
            rule_id       TEXT DEFAULT '',
            severity      TEXT NOT NULL DEFAULT 'info',
            status        TEXT NOT NULL DEFAULT 'open',
            message       TEXT DEFAULT '',
            line_start    INTEGER,
            line_end      INTEGER,
            seen_count    INTEGER DEFAULT 1,
            first_seen    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            last_seen_at  TEXT,
            metadata      TEXT DEFAULT '{}',
            CHECK (severity IN ('critical', 'high', 'medium', 'low', 'info')),
            CHECK (status IN ('open', 'acknowledged', 'fixed', 'false_positive', 'unseen_in_latest'))
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scan_findings_file ON scan_findings(file_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scan_findings_issue ON scan_findings(issue_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scan_findings_severity ON scan_findings(severity)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scan_findings_status ON scan_findings(status)")
    conn.execute("""\
        CREATE UNIQUE INDEX IF NOT EXISTS idx_scan_findings_dedup
          ON scan_findings(file_id, scan_source, rule_id, coalesce(line_start, -1))""")

    conn.execute("""\
        CREATE TABLE IF NOT EXISTS file_associations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id     TEXT NOT NULL REFERENCES file_records(id),
            issue_id    TEXT NOT NULL REFERENCES issues(id),
            assoc_type  TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            UNIQUE(file_id, issue_id, assoc_type),
            CHECK (assoc_type IN ('bug_in', 'task_for', 'scan_finding', 'mentioned_in'))
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_assoc_file ON file_associations(file_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_assoc_issue ON file_associations(issue_id)")


def migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """v2 → v3: Add scan_run_id, suggestion columns and scan_run_id index to scan_findings.

    Changes:
      - scan_findings: add 'suggestion' column (TEXT, default '')
      - scan_findings: add 'scan_run_id' column (TEXT, default '')
      - new index idx_scan_findings_run on scan_findings(scan_run_id)
    """
    add_column(conn, "scan_findings", "suggestion", "TEXT", "''")
    add_column(conn, "scan_findings", "scan_run_id", "TEXT", "''")
    add_index(conn, "idx_scan_findings_run", "scan_findings", ["scan_run_id"])


def migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """v3 → v4: Add file_events table for file metadata timeline events.

    Changes:
      - new table 'file_events' for tracking file metadata field changes
      - new index idx_file_events_file on file_events(file_id)
    """
    conn.execute("""\
        CREATE TABLE IF NOT EXISTS file_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id     TEXT NOT NULL REFERENCES file_records(id),
            event_type  TEXT NOT NULL DEFAULT 'file_metadata_update',
            field       TEXT NOT NULL,
            old_value   TEXT DEFAULT '',
            new_value   TEXT DEFAULT '',
            created_at  TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_events_file ON file_events(file_id)")


def _now_iso() -> str:
    """ISO timestamp for migration operations (Z-suffix, no microseconds — distinct from db_base._now_iso)."""
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    """v4 → v5: Normalize release version fields to vMAJOR.MINOR.PATCH or Future.

    Changes:
      - Normalizes non-compliant version field values on existing release issues
      - Adds migration comments documenting changes
      - Clears un-normalizable versions with explanatory comments
    """
    import json

    semver_strict = re.compile(r"^v\d+\.\d+\.\d+$")
    semver_loose = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?$")

    rows = conn.execute("SELECT id, title, fields FROM issues WHERE type = 'release'").fetchall()
    skipped_ids: list[str] = []

    for row in rows:
        try:
            fields = json.loads(row["fields"] or "{}")
        except (json.JSONDecodeError, TypeError):
            skipped_ids.append(row["id"])
            logger.warning(
                "migrate_v4_to_v5: skipping issue %s with corrupt fields JSON: %r",
                row["id"],
                (row["fields"] or "")[:200],
            )
            continue
        version = fields.get("version", "")
        if not version:
            continue

        # Already compliant
        if version == "Future" or semver_strict.match(version):
            continue

        # Try to normalize: "1.2.3" → "v1.2.3", "v1.2" → "v1.2.0"
        m = semver_loose.match(version.strip())
        if m:
            normalized = f"v{m.group(1)}.{m.group(2)}.{m.group(3) or '0'}"
            fields["version"] = normalized
            conn.execute(
                "UPDATE issues SET fields = ? WHERE id = ?",
                (json.dumps(fields), row["id"]),
            )
            now = _now_iso()
            conn.execute(
                "INSERT INTO comments (issue_id, author, text, created_at) VALUES (?, ?, ?, ?)",
                (row["id"], "migration", f"Version normalized: '{version}' → '{normalized}'", now),
            )
        else:
            # Un-normalizable — clear and comment
            original = version
            fields.pop("version", None)
            conn.execute(
                "UPDATE issues SET fields = ? WHERE id = ?",
                (json.dumps(fields), row["id"]),
            )
            now = _now_iso()
            comment_text = f"Version field cleared during migration (was: '{original}'). Set a valid vX.Y.Z version before freezing."
            conn.execute(
                "INSERT INTO comments (issue_id, author, text, created_at) VALUES (?, ?, ?, ?)",
                (row["id"], "migration", comment_text, now),
            )

    if skipped_ids:
        logger.error(
            "migrate_v4_to_v5: skipped %d issue(s) with corrupt fields JSON: %s",
            len(skipped_ids),
            skipped_ids,
        )


def _issues_parent_fk_has_set_null(conn: sqlite3.Connection) -> bool:
    """Return True when issues.parent_id has an ON DELETE SET NULL self-FK."""
    for row in conn.execute("PRAGMA foreign_key_list(issues)").fetchall():
        if row[2] == "issues" and row[3] == "parent_id" and row[6].upper() == "SET NULL":
            return True
    return False


def _recreate_issues_fts_triggers(conn: sqlite3.Connection) -> None:
    """Recreate issues FTS triggers after rebuilding the issues table."""
    has_fts = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'issues_fts'").fetchone()
    if has_fts is None:
        return

    conn.execute("DROP TRIGGER IF EXISTS issues_fts_insert")
    conn.execute("DROP TRIGGER IF EXISTS issues_fts_update")
    conn.execute("DROP TRIGGER IF EXISTS issues_fts_delete")
    conn.execute("""\
        CREATE TRIGGER issues_fts_insert AFTER INSERT ON issues BEGIN
            INSERT INTO issues_fts(rowid, title, description) VALUES (new.rowid, new.title, new.description);
        END""")
    conn.execute("""\
        CREATE TRIGGER issues_fts_update AFTER UPDATE OF title, description ON issues BEGIN
            INSERT INTO issues_fts(issues_fts, rowid, title, description)
                VALUES('delete', old.rowid, old.title, old.description);
            INSERT INTO issues_fts(rowid, title, description) VALUES (new.rowid, new.title, new.description);
        END""")
    conn.execute("""\
        CREATE TRIGGER issues_fts_delete AFTER DELETE ON issues BEGIN
            INSERT INTO issues_fts(issues_fts, rowid, title, description)
                VALUES('delete', old.rowid, old.title, old.description);
        END""")


def _rebuild_issues_fts_index(conn: sqlite3.Connection) -> None:
    """Resync the issues_fts external-content index after an issues table rebuild."""
    has_fts = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'issues_fts'").fetchone()
    if has_fts is None:
        return

    conn.execute("INSERT INTO issues_fts(issues_fts) VALUES ('rebuild')")


def migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
    """v5 → v6: Restore the parent self-FK with ON DELETE SET NULL.

    Historical filigree databases already used schema version 6 for this
    change. Some later code paths kept the v6 schema shape but regressed the
    reported current version to v5. This migration handles both states:
      - old v5 databases without the parent self-FK are rebuilt safely
      - already-correct databases simply get restamped to v6
    """
    conn.execute("UPDATE issues SET parent_id = NULL WHERE parent_id IS NOT NULL AND parent_id NOT IN (SELECT id FROM issues)")

    if _issues_parent_fk_has_set_null(conn):
        return

    rebuild_table(
        conn,
        "issues",
        """\
        CREATE TABLE issues (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'open',
            priority    INTEGER NOT NULL DEFAULT 2,
            type        TEXT NOT NULL DEFAULT 'task',
            parent_id   TEXT REFERENCES issues(id) ON DELETE SET NULL,
            assignee    TEXT DEFAULT '',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            closed_at   TEXT,
            description TEXT DEFAULT '',
            notes       TEXT DEFAULT '',
            fields      TEXT DEFAULT '{}',
            CHECK (priority BETWEEN 0 AND 4)
        )""",
    )
    add_index(conn, "idx_issues_status", "issues", ["status"])
    add_index(conn, "idx_issues_type", "issues", ["type"])
    add_index(conn, "idx_issues_parent", "issues", ["parent_id"])
    add_index(conn, "idx_issues_priority", "issues", ["priority"])
    add_index(conn, "idx_issues_status_priority", "issues", ["status", "priority", "created_at"])
    _recreate_issues_fts_triggers(conn)
    _rebuild_issues_fts_index(conn)


def migrate_v6_to_v7(conn: sqlite3.Connection) -> None:
    """v6 → v7: Add observations and dismissed_observations tables.

    Changes:
      - new table 'observations' for lightweight agent-reported observations
      - new table 'dismissed_observations' for dismissal audit trail
      - new indexes on observations(priority, created_at), observations(expires_at),
        and dedup index on (summary, file_path, line)

    Rollback: DROP TABLE IF EXISTS dismissed_observations;
              DROP TABLE IF EXISTS observations;
              PRAGMA user_version = 6;
    """
    conn.execute("""\
        CREATE TABLE IF NOT EXISTS observations (
            id              TEXT PRIMARY KEY,
            summary         TEXT NOT NULL,
            detail          TEXT DEFAULT '',
            file_id         TEXT REFERENCES file_records(id) ON DELETE SET NULL,
            file_path       TEXT DEFAULT '',
            line            INTEGER,
            source_issue_id TEXT DEFAULT '',
            priority        INTEGER DEFAULT 3 CHECK (priority BETWEEN 0 AND 4),
            actor           TEXT DEFAULT '',
            created_at      TEXT NOT NULL,
            expires_at      TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_observations_priority ON observations(priority, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_observations_expires ON observations(expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_observations_file_id ON observations(file_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_observations_dedup ON observations(summary, file_path, coalesce(line, -1))")
    conn.execute("""\
        CREATE TABLE IF NOT EXISTS dismissed_observations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            obs_id       TEXT NOT NULL,
            summary      TEXT NOT NULL,
            actor        TEXT DEFAULT '',
            reason       TEXT DEFAULT '',
            dismissed_at TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dismissed_obs_id ON dismissed_observations(obs_id)")


def migrate_v7_to_v8(conn: sqlite3.Connection) -> None:
    """v7 → v8: Add scan_runs table for scan lifecycle tracking.

    Changes:
      - new table 'scan_runs' for tracking scanner invocations and their status
      - new indexes on scan_runs(status) and scan_runs(scanner_name)

    Rollback: DROP TABLE IF EXISTS scan_runs;
              DROP INDEX IF EXISTS idx_scan_runs_status;
              DROP INDEX IF EXISTS idx_scan_runs_scanner;
              PRAGMA user_version = 7;
    """
    conn.execute("""\
        CREATE TABLE IF NOT EXISTS scan_runs (
            id            TEXT PRIMARY KEY,
            scanner_name  TEXT NOT NULL,
            scan_source   TEXT NOT NULL DEFAULT '',
            status        TEXT NOT NULL DEFAULT 'pending',
            file_paths    TEXT NOT NULL DEFAULT '[]',
            file_ids      TEXT NOT NULL DEFAULT '[]',
            pid           INTEGER,
            api_url       TEXT DEFAULT '',
            log_path      TEXT DEFAULT '',
            started_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            completed_at  TEXT,
            exit_code     INTEGER,
            findings_count INTEGER DEFAULT 0,
            error_message TEXT DEFAULT '',
            CHECK (status IN ('pending', 'running', 'completed', 'failed', 'timeout'))
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scan_runs_status ON scan_runs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scan_runs_scanner ON scan_runs(scanner_name)")


def migrate_v8_to_v9(conn: sqlite3.Connection) -> None:
    """v8 → v9: Add missing performance indexes for label and assignee queries.

    Changes:
      - new index idx_labels_label_issue on labels(label, issue_id) —
        covers label-first lookups (WHERE label = ?) and the taxonomy
        GROUP BY label ORDER BY label scan. The labels PK is
        (issue_id, label), which is left-prefix and cannot serve label-first
        queries.
      - new index idx_issues_assignee_priority on issues(assignee, priority,
        created_at) — satisfies `--assignee` filters and collapses the
        ORDER BY priority, created_at sort into an index range scan.

    Rollback: DROP INDEX IF EXISTS idx_labels_label_issue;
              DROP INDEX IF EXISTS idx_issues_assignee_priority;
              PRAGMA user_version = 8;
    """
    add_index(conn, "idx_labels_label_issue", "labels", ["label", "issue_id"])
    add_index(conn, "idx_issues_assignee_priority", "issues", ["assignee", "priority", "created_at"])


def _create_annotation_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""\
        CREATE TABLE IF NOT EXISTS annotations (
            id              TEXT PRIMARY KEY,
            file_id         TEXT REFERENCES file_records(id) ON DELETE SET NULL,
            file_path       TEXT NOT NULL,
            line_start      INTEGER,
            line_end        INTEGER,
            anchor_snippet  TEXT DEFAULT '',
            note            TEXT NOT NULL,
            context_summary TEXT DEFAULT '',
            intent          TEXT NOT NULL DEFAULT 'breadcrumb',
            critical        BOOLEAN NOT NULL DEFAULT 0,
            status          TEXT NOT NULL DEFAULT 'active',
            actor           TEXT DEFAULT '',
            session_ref     TEXT DEFAULT '',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            resolved_at     TEXT,
            CHECK (intent IN ('explanation', 'warning', 'breadcrumb', 'hypothesis', 'decision', 'handoff', 'gotcha')),
            CHECK (status IN ('active', 'resolved', 'superseded', 'promoted')),
            CHECK (line_start IS NULL OR line_start >= 1),
            CHECK (line_end IS NULL OR (line_start IS NOT NULL AND line_end >= line_start))
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_annotations_file ON annotations(file_id, status, critical, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_annotations_path ON annotations(file_path, status, critical, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_annotations_status ON annotations(status, critical, created_at)")

    conn.execute("""\
        CREATE TABLE IF NOT EXISTS annotation_provenance (
            annotation_id          TEXT PRIMARY KEY REFERENCES annotations(id) ON DELETE CASCADE,
            commit_ref            TEXT DEFAULT '',
            branch                TEXT DEFAULT '',
            repo_root             TEXT DEFAULT '',
            worktree_root         TEXT DEFAULT '',
            git_state             TEXT DEFAULT '',
            worktree_dirty        BOOLEAN NOT NULL DEFAULT 0,
            file_checksum         TEXT DEFAULT '',
            file_size             INTEGER DEFAULT 0,
            file_mtime            TEXT DEFAULT '',
            dirty_diff_hash       TEXT DEFAULT '',
            dirty_diff_summary    TEXT DEFAULT '',
            file_diff             TEXT DEFAULT '',
            worktree_diff_summary TEXT DEFAULT '',
            anchor_context_before TEXT DEFAULT '',
            anchor_context_after  TEXT DEFAULT '',
            provenance_trust_level TEXT NOT NULL DEFAULT 'minimal',
            provenance_flags      TEXT NOT NULL DEFAULT '[]',
            provenance_warnings   TEXT NOT NULL DEFAULT '[]',
            CHECK (provenance_trust_level IN ('complete', 'partial', 'minimal'))
        )""")

    conn.execute("""\
        CREATE TABLE IF NOT EXISTS annotation_links (
            id             TEXT PRIMARY KEY,
            annotation_id  TEXT NOT NULL REFERENCES annotations(id) ON DELETE CASCADE,
            target_type    TEXT NOT NULL,
            target_id      TEXT NOT NULL,
            relationship   TEXT NOT NULL,
            actor          TEXT DEFAULT '',
            created_at     TEXT NOT NULL,
            UNIQUE(annotation_id, target_type, target_id, relationship),
            CHECK (target_type IN ('issue', 'file', 'finding', 'observation')),
            CHECK (relationship IN ('relevant_to', 'must_consider', 'evidence_for', 'explains', 'created_from', 'promoted_to'))
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_annotation_links_annotation ON annotation_links(annotation_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_annotation_links_target ON annotation_links(target_type, target_id, relationship)")

    conn.execute("""\
        CREATE TABLE IF NOT EXISTS annotation_events (
            id            TEXT PRIMARY KEY,
            annotation_id TEXT NOT NULL REFERENCES annotations(id) ON DELETE CASCADE,
            event_type    TEXT NOT NULL,
            actor         TEXT DEFAULT '',
            reason        TEXT DEFAULT '',
            old_value     TEXT,
            new_value     TEXT,
            target_type   TEXT DEFAULT '',
            target_id     TEXT DEFAULT '',
            created_at    TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_annotation_events_annotation ON annotation_events(annotation_id, created_at)")

    conn.execute("""\
        CREATE TABLE IF NOT EXISTS annotation_closeout_acknowledgements (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            annotation_id         TEXT NOT NULL REFERENCES annotations(id) ON DELETE CASCADE,
            target_type           TEXT NOT NULL DEFAULT 'issue',
            target_id             TEXT NOT NULL,
            carried_to_target_id  TEXT DEFAULT '',
            actor                 TEXT DEFAULT '',
            reason                TEXT DEFAULT '',
            acknowledged_at       TEXT NOT NULL,
            UNIQUE(annotation_id, target_type, target_id),
            CHECK (target_type IN ('issue'))
        )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_annotation_closeout_ack_target ON annotation_closeout_acknowledgements(target_type, target_id)"
    )


def migrate_v9_to_v10(conn: sqlite3.Connection) -> None:
    """v9 -> v10: Add shared file annotation tables."""
    _create_annotation_tables(conn)


def migrate_v10_to_v11(conn: sqlite3.Connection) -> None:
    """v10 -> v11: Add issue claim lease metadata.

    Changes:
      - issues: add claimed_at, last_heartbeat_at, and claim_expires_at
      - issues: add index on claim_expires_at for stale-claim scans
    """
    add_column(conn, "issues", "claimed_at", "TEXT", "NULL")
    add_column(conn, "issues", "last_heartbeat_at", "TEXT", "NULL")
    add_column(conn, "issues", "claim_expires_at", "TEXT", "NULL")
    add_index(conn, "idx_issues_claim_expires_at", "issues", ["claim_expires_at"])


def migrate_v11_to_v12(conn: sqlite3.Connection) -> None:
    """v11 -> v12: Link observations to findings for cleanup cascade.

    The senior-user MCP review (run d, P1.2) flagged that
    ``report_finding`` auto-creates a parallel observation but neither
    ``dismiss_finding`` nor ``promote_finding`` cleans it up — every
    triaged finding leaves a 14-day zombie observation. The new
    ``source_finding_id`` column lets the finding-side lifecycle
    cascade-dismiss the linked observation. The default ``''`` matches
    legacy rows so existing reads stay valid; new finding-spawned
    observations populate it. (filigree-cb980eee0d)

    Changes:
      - observations: add source_finding_id (TEXT DEFAULT '')
      - observations: add index on source_finding_id for cascade scans
    """
    add_column(conn, "observations", "source_finding_id", "TEXT", "''")
    add_index(conn, "idx_observations_source_finding", "observations", ["source_finding_id"])


def migrate_v12_to_v13(conn: sqlite3.Connection) -> None:
    """v12 -> v13: Preserve structured observation triage links.

    Observations can now be linked to existing issues as evidence,
    duplicate, superseded, or related dispositions.  The live observation is
    cleared from the queue while a snapshot of its evidence remains durable in
    ``observation_links``.
    """
    conn.execute("""\
        CREATE TABLE IF NOT EXISTS observation_links (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            obs_id             TEXT NOT NULL,
            issue_id           TEXT NOT NULL REFERENCES issues(id),
            disposition        TEXT NOT NULL DEFAULT 'evidence',
            summary            TEXT NOT NULL,
            detail             TEXT DEFAULT '',
            file_id            TEXT REFERENCES file_records(id) ON DELETE SET NULL,
            file_path          TEXT DEFAULT '',
            line               INTEGER,
            source_issue_id    TEXT DEFAULT '',
            source_finding_id  TEXT DEFAULT '',
            priority           INTEGER DEFAULT 3 CHECK (priority BETWEEN 0 AND 4),
            observation_actor  TEXT DEFAULT '',
            actor              TEXT DEFAULT '',
            reason             TEXT DEFAULT '',
            linked_at          TEXT NOT NULL,
            CHECK (disposition IN ('evidence', 'duplicate', 'superseded', 'related'))
        )""")
    add_index(conn, "idx_observation_links_obs", "observation_links", ["obs_id"])
    add_index(conn, "idx_observation_links_issue", "observation_links", ["issue_id", "linked_at"])


def migrate_v13_to_v14(conn: sqlite3.Connection) -> None:
    """v13 -> v14: Persist actor attribution for file/finding writes."""
    add_column(conn, "file_records", "created_by", "TEXT", "''")
    add_column(conn, "file_records", "updated_by", "TEXT", "''")
    add_column(conn, "scan_findings", "created_by", "TEXT", "''")
    add_column(conn, "scan_findings", "updated_by", "TEXT", "''")
    add_column(conn, "file_associations", "actor", "TEXT", "''")
    add_column(conn, "file_events", "actor", "TEXT", "''")


def migrate_v15_to_v16(conn: sqlite3.Connection) -> None:
    """v15 -> v16: Add event_seq column + rebuild the unique event index (2.1.0 §0.2).

    Previously the events table's UNIQUE event index was
    ``(issue_id, event_type, actor, old_value, new_value, created_at)``.
    With ISO-second precision on ``created_at``, same-actor heartbeats in
    the same second collided and silently lost events under ``INSERT OR
    IGNORE`` — the silent-failure C3 surface from the 2.1.0 panel review.

    The new column ``event_seq INTEGER NOT NULL DEFAULT 0`` is a
    per-issue event ordering key. ``_record_event`` computes the next value inline under the
    caller-held writer transaction via
    ``COALESCE((SELECT MAX(event_seq) FROM events WHERE issue_id = ?),
    -1) + 1`` so bursts at the same second land distinct rows. Historical
    rows default to 0; since the old behaviour was to silently drop the
    collision, replaying history with all-zero ``event_seq`` is no worse
    than the pre-v16 state.
    """
    add_column(conn, "events", "event_seq", "INTEGER NOT NULL", "0")
    conn.execute("DROP INDEX IF EXISTS idx_events_dedup")
    conn.execute(
        "CREATE UNIQUE INDEX idx_events_dedup ON events("
        "issue_id, event_type, actor, "
        "coalesce(old_value, ''), coalesce(new_value, ''), "
        "created_at, event_seq)"
    )


def migrate_v14_to_v15(conn: sqlite3.Connection) -> None:
    """v14 -> v15: Add entity_associations table (ADR-029, Loomweave B.7).

    Binds Filigree issues to Loomweave entities via opaque string IDs.
    Filigree does not parse the ID grammar — the federation enrich-only
    rule (loom.md §5) requires that Loomweave's entity-ID format remain
    Loomweave's contract with itself.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_associations (
            issue_id                TEXT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
            clarion_entity_id       TEXT NOT NULL,
            content_hash_at_attach  TEXT NOT NULL,
            attached_at             TEXT NOT NULL,
            attached_by             TEXT NOT NULL,
            PRIMARY KEY (issue_id, clarion_entity_id)
        )
    """)
    add_index(conn, "ix_entity_assoc_entity", "entity_associations", ["clarion_entity_id"])


def migrate_v16_to_v17(conn: sqlite3.Connection) -> None:
    """v16 -> v17: Add ADR-014 file registry metadata columns."""
    add_column(conn, "file_records", "content_hash", "TEXT NOT NULL", "''")
    add_column(conn, "file_records", "registry_backend", "TEXT NOT NULL", "'local'")


def migrate_v17_to_v18(conn: sqlite3.Connection) -> None:
    """v17 -> v18: Stamp ``application_id`` on pre-app-id-aware filigree DBs.

    Pure metadata hop, no DDL. Every filigree DB created before the app-id-aware
    install lacks ``application_id`` (it was never written). v18 closes the gap
    by writing it during the normal migration path so subsequent opens can
    distinguish a filigree DB from a foreign SQLite file at the same path
    (catalog §6.8 errata).

    The PRAGMA write lands in the database header on page 1 and is journaled
    like any other page change, so it participates in the migration's
    transaction — rollback reverts ``application_id`` along with
    ``user_version``. Idempotent under re-run.
    """
    from filigree.core import FILIGREE_APPLICATION_ID

    conn.execute(f"PRAGMA application_id = {FILIGREE_APPLICATION_ID}")


def migrate_v18_to_v19(conn: sqlite3.Connection) -> None:
    """v18 -> v19: Add ``scan_findings.fingerprint`` + partition the dedup index (Weft §3.B).

    A scanner may supply a stable per-finding ``fingerprint`` as the finding's
    cross-run identity. When present it keys lifecycle/``seen_count`` instead of
    the ``(file_id, scan_source, rule_id, line_start)`` heuristic — which is
    itself unstable, since line attribution is clamped/cleared against file
    length on ingest. The column is generic (any scanner may use it); absent
    (empty string) preserves the legacy heuristic.

    The pre-v19 unique dedup index is **non-partial** and constrains every row,
    so it would reject two distinct-fingerprint findings at the same
    file/rule/line (e.g. two taint paths into one sink). It is rebuilt as a
    partial index over fingerprint-less rows, and a second partial unique index
    keys fingerprint-bearing rows on ``(scan_source, fingerprint)``. Because the
    column is ``NOT NULL DEFAULT ''``, the empty-string sentinel partitions all
    rows cleanly between the two indexes. Idempotent under re-run.
    """
    add_column(conn, "scan_findings", "fingerprint", "TEXT NOT NULL", "''")
    conn.execute("DROP INDEX IF EXISTS idx_scan_findings_dedup")
    conn.execute(
        "CREATE UNIQUE INDEX idx_scan_findings_dedup ON scan_findings("
        "file_id, scan_source, rule_id, coalesce(line_start, -1)) "
        "WHERE fingerprint = ''"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_scan_findings_fingerprint ON scan_findings(scan_source, fingerprint) WHERE fingerprint <> ''"
    )


def migrate_v19_to_v20(conn: sqlite3.Connection) -> None:
    """v19 -> v20: Add the ``deleted_issues`` tombstone table.

    A hard delete (``FiligreeDB.delete_issue``) removes the issue row and all
    of its child rows, including its ``events``. Because ``GET
    /api/weft/changes`` (``get_events_since``) INNER JOINs ``issues``, a
    hard-deleted issue is otherwise invisible to federation consumers — they
    keep a stale reference forever. ``delete_issue`` writes a tombstone row
    here in the same transaction, and the changes feed surfaces it as a
    synthetic ``issue_deleted`` change record cursored on ``deleted_at`` so
    incremental consumers see each deletion exactly once. Idempotent under
    re-run.

    ``seq`` is an AUTOINCREMENT key (not implicit rowid) so VACUUM never
    renumbers it and the synthetic change-feed event_id stays stable across
    ``filigree compact``. The DDL is byte-identical to the ``deleted_issues``
    block in ``SCHEMA_SQL`` so fresh-create and migrated schemas are textually
    the same.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS deleted_issues (\n"
        "    seq        INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "    issue_id   TEXT NOT NULL UNIQUE,\n"
        "    title      TEXT NOT NULL DEFAULT '',\n"
        "    type       TEXT NOT NULL DEFAULT '',\n"
        "    deleted_at TEXT NOT NULL,\n"
        "    deleted_by TEXT DEFAULT '',\n"
        "    reason     TEXT DEFAULT ''\n"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deleted_issues_deleted_at ON deleted_issues(deleted_at, seq)")


def migrate_v20_to_v21(conn: sqlite3.Connection) -> None:
    """v20 -> v21: Add ``deleted_issues.entity_ids`` (F5 entity-association amplifier).

    ``delete_issue`` cascades ``entity_associations`` (``ON DELETE CASCADE``), so a
    hard delete silently drops Filigree's side of every Loomweave entity binding. The
    v20 tombstone recorded only the issue identity, so the synthetic ``issue_deleted``
    change record could not name *which* bindings the cascade removed — a consumer
    that mirrors the reverse lookup (``list_associations_by_entity``) would keep
    returning the deleted issue and surface a user-facing phantom (filigree-f3bf56554c).

    This adds a JSON-array column holding the affected ``loomweave_entity_id``s,
    captured in ``delete_issue`` before the cascade and surfaced as
    ``affected_entities`` on the change record. The column is ``NOT NULL DEFAULT
    '[]'`` to match the fresh ``SCHEMA_SQL`` shape (SQLite permits a NOT NULL ADD
    COLUMN when a non-NULL DEFAULT is supplied). Tombstones written under v20 get
    ``'[]'`` — their cascaded bindings are already gone and unrecoverable, the
    documented limitation of upgrading after the fact. Idempotent: ``add_column``
    no-ops if the column already exists.
    """
    add_column(conn, "deleted_issues", "entity_ids", "TEXT NOT NULL", default="'[]'")


def migrate_v21_to_v22(conn: sqlite3.Connection) -> None:
    """v21 -> v22: Add ``entity_associations.migration_orphaned_at`` (SEI backfill).

    The locator→SEI value migration (ADR-038 §7, driven by ``filigree
    sei-backfill``) rewrites each opaque ``loomweave_entity_id`` from its locator to
    the resolved SEI *in place* — the column name, wire shape, and storage
    mechanism are unchanged. A locator that no longer resolves (Loomweave answers
    ``alive:false``) must be kept verbatim and flagged for human review, never
    silently dropped (the suite's no-false-green ethos).

    This adds the nullable marker column that flag carries: NULL is the healthy
    default; a non-NULL ISO timestamp records when the backfill found the row's
    locator unresolvable. It is additive metadata about the migration, not a
    change to the opaque id itself. Nullable (``default=None`` adds no DEFAULT
    clause); existing rows read back NULL. Idempotent: ``add_column`` no-ops if
    the column already exists.
    """
    add_column(conn, "entity_associations", "migration_orphaned_at", "TEXT", default=None)


def migrate_v22_to_v23(conn: sqlite3.Connection) -> None:
    """v22 -> v23: Add caller-supplied ``entity_associations.entity_kind``.

    Entity IDs are opaque external identifiers: they may be SEIs, legacy
    locators, or non-Loomweave IDs. Filigree must not parse kind out of the ID,
    but callers that already know the kind can now store it explicitly for UI
    and API presentation. Empty string means unknown/not supplied.
    """
    add_column(conn, "entity_associations", "entity_kind", "TEXT NOT NULL", default="''")


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


def migrate_v24_to_v25(conn: sqlite3.Connection) -> None:
    """v24 -> v25: Add nullable ``signature`` / ``signoff_seq`` columns to
    ``entity_associations`` (B1 — Legis governed-sign-off binding).

    When Legis clears a *governed* sign-off and binds an entity-association to
    an issue, it sends an opaque HMAC ``signature`` over
    ``{issue_id, entity_id, content_hash, signoff_seq}`` plus the
    ``signoff_seq``. Both are OPTIONAL on the wire — Legis omits them when no
    key is configured — so both columns are nullable (``default=None`` adds no
    DEFAULT clause); existing rows and non-governed bindings read NULL with no
    backfill. Filigree stores them verbatim and never verifies the signature
    (it has no key), exactly as it treats ``content_hash_at_attach``.
    Idempotent: ``add_column`` no-ops if the column already exists.
    """
    add_column(conn, "entity_associations", "signature", "TEXT", default=None)
    add_column(conn, "entity_associations", "signoff_seq", "INTEGER", default=None)


def migrate_v25_to_v26(conn: sqlite3.Connection) -> None:
    """v25 -> v26: Loomweave/Weft rebrand data pass.

    Renames the entity-association column ``clarion_entity_id`` ->
    ``loomweave_entity_id`` (SQLite's RENAME COLUMN rewrites the PRIMARY KEY
    reference) and rewrites EVERY stored ``clarion:eid:`` SEI prefix ->
    ``loomweave:eid:`` and finding rule-id prefix ``CLA-`` -> ``LMWV-`` in
    place. Suffixes are preserved. Idempotent under re-run (guards on the source
    name/prefix existing).

    The SEI value is stored in more than one place — every site is rewritten so
    a federation consumer (already on ``loomweave:eid:``) reconciling off any of
    them matches:
      - ``entity_associations.loomweave_entity_id`` — the binding itself.
      - ``deleted_issues.entity_ids`` — the F5 tombstone JSON array surfaced as
        an ``issue_deleted`` change record's ``affected_entities``; a stale
        prefix here reproduces the phantom-reverse-lookup bug (filigree-f3bf56554c).
      - ``events`` audit log — the entity-association add/remove/refresh events
        carry the SEI in ``new_value`` / ``old_value`` / ``comment``.

    NOTE (rule-id dedup): the ``CLA- -> LMWV-`` rewrite could in principle trip
    the ``scan_findings`` unique dedup index if a DB straddled the cutover and
    already held an ``LMWV-`` finding at the same (file, source, line). Filigree
    is the last federation member to rebrand, so a real Filigree DB holds only
    ``CLA-`` rows; if it ever collided the transaction rolls back cleanly (no
    corruption). The v19 fingerprint-partition index makes it lower-probability.

    NOTE (Legis): rewriting the SEI prefix changes the entity_id string the
    Legis HMAC was cut over, so every stored ``signature`` becomes
    stale-pending-reissue. Filigree never verifies the signature, so reads do
    not break; Legis re-signs in lockstep (see the rebrand epic, T0b).
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(entity_associations)").fetchall()}
    if "clarion_entity_id" in cols and "loomweave_entity_id" not in cols:
        rename_column(conn, "entity_associations", "clarion_entity_id", "loomweave_entity_id")
    # Re-affirm the entity index under the new column name (drop old, recreate).
    drop_index(conn, "ix_entity_assoc_entity")
    add_index(conn, "ix_entity_assoc_entity", "entity_associations", ["loomweave_entity_id"])
    # Rewrite stored SEI prefixes (clarion:eid: -> loomweave:eid:), suffix preserved.
    conn.execute(
        "UPDATE entity_associations SET loomweave_entity_id = "
        "'loomweave:eid:' || substr(loomweave_entity_id, length('clarion:eid:') + 1) "
        "WHERE loomweave_entity_id LIKE 'clarion:eid:%'"
    )
    # F5 deletion-tombstone affected-entity arrays (JSON text) carry the same SEI.
    conn.execute(
        "UPDATE deleted_issues SET entity_ids = replace(entity_ids, 'clarion:eid:', 'loomweave:eid:') "
        "WHERE entity_ids LIKE '%clarion:eid:%'"
    )
    # Entity-association audit events store the SEI verbatim in new_value/old_value/comment.
    conn.execute(
        "UPDATE events SET "
        "new_value = replace(new_value, 'clarion:eid:', 'loomweave:eid:'), "
        "old_value = replace(old_value, 'clarion:eid:', 'loomweave:eid:'), "
        "comment = replace(comment, 'clarion:eid:', 'loomweave:eid:') "
        "WHERE new_value LIKE '%clarion:eid:%' OR old_value LIKE '%clarion:eid:%' OR comment LIKE '%clarion:eid:%'"
    )
    # Rewrite stored finding rule-id prefixes (CLA- -> LMWV-), suffix preserved.
    conn.execute("UPDATE scan_findings SET rule_id = 'LMWV-' || substr(rule_id, length('CLA-') + 1) WHERE rule_id LIKE 'CLA-%'")


MIGRATIONS: dict[int, MigrationFn] = {
    1: migrate_v1_to_v2,
    2: migrate_v2_to_v3,
    3: migrate_v3_to_v4,
    4: migrate_v4_to_v5,
    5: migrate_v5_to_v6,
    6: migrate_v6_to_v7,
    7: migrate_v7_to_v8,
    8: migrate_v8_to_v9,
    9: migrate_v9_to_v10,
    10: migrate_v10_to_v11,
    11: migrate_v11_to_v12,
    12: migrate_v12_to_v13,
    13: migrate_v13_to_v14,
    14: migrate_v14_to_v15,
    15: migrate_v15_to_v16,
    16: migrate_v16_to_v17,
    17: migrate_v17_to_v18,
    18: migrate_v18_to_v19,
    19: migrate_v19_to_v20,
    20: migrate_v20_to_v21,
    21: migrate_v21_to_v22,
    22: migrate_v22_to_v23,
    23: migrate_v23_to_v24,
    24: migrate_v24_to_v25,
    25: migrate_v25_to_v26,
}


# ---------------------------------------------------------------------------
# Migration runner
# ---------------------------------------------------------------------------


class MigrationError(Exception):
    """Raised when a migration fails."""

    def __init__(self, from_version: int, to_version: int, cause: Exception) -> None:
        self.from_version = from_version
        self.to_version = to_version
        self.cause = cause
        super().__init__(f"Migration v{from_version} → v{to_version} failed: {cause}")


def apply_pending_migrations(conn: sqlite3.Connection, target_version: int) -> int:
    """Apply all pending migrations from current version up to target_version.

    Args:
        conn: Open SQLite connection (must have row_factory and PRAGMAs already set).
        target_version: The CURRENT_SCHEMA_VERSION from db_schema.py.

    Returns:
        Number of migrations applied (0 if already up to date).

    Raises:
        MigrationError: If any individual migration fails (DB rolled back to
            the last successful migration).
        ValueError: If current version > target (downgrade not supported).
    """
    if conn.in_transaction:
        msg = (
            "apply_pending_migrations() must not be called inside an existing transaction. "
            "Commit or roll back the current transaction first."
        )
        raise RuntimeError(msg)

    current: int = conn.execute("PRAGMA user_version").fetchone()[0]

    if current == target_version:
        return 0

    if current > target_version:
        msg = f"Database schema v{current} is newer than this version of filigree (expects v{target_version}). Downgrade is not supported."
        raise ValueError(msg)

    # Capture caller's FK setting so we can restore it in finally.
    original_fk: int = conn.execute("PRAGMA foreign_keys").fetchone()[0]

    applied = 0
    for version in range(current, target_version):
        migration = MIGRATIONS.get(version)
        if migration is None:
            msg = (
                f"No migration registered for v{version} → v{version + 1}. "
                f"Database is at v{version}, target is v{target_version}. "
                f"Register the migration in filigree.migrations.MIGRATIONS."
            )
            raise MigrationError(version, version + 1, KeyError(msg))

        logger.info("Applying migration v%d → v%d ...", version, version + 1)
        migration_failed = False
        try:
            # Disable FK enforcement so rebuild_table() can atomically
            # DROP + RENAME FK-referenced tables within the transaction.
            # This PRAGMA only takes effect outside a transaction.
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN IMMEDIATE")
            migration(conn)
            conn.execute(f"PRAGMA user_version = {version + 1}")
            # Validate FK integrity before committing — catches both
            # migration bugs and pre-existing violations.
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(f"Foreign key violations after migration: {violations}")
            conn.commit()
            applied += 1
            logger.info("Migration v%d → v%d complete.", version, version + 1)
        except Exception as exc:
            migration_failed = True
            conn.rollback()
            raise MigrationError(version, version + 1, exc) from exc
        finally:
            # Restore caller's original FK enforcement setting.
            # After commit/rollback the connection is in autocommit mode,
            # so this PRAGMA takes effect immediately.
            try:
                conn.execute(f"PRAGMA foreign_keys={'ON' if original_fk else 'OFF'}")
            except sqlite3.Error as exc:
                logger.error(
                    "foreign_key_restore_failed",
                    extra={
                        "from_version": version,
                        "to_version": version + 1,
                        "original_foreign_keys": original_fk,
                    },
                    exc_info=True,
                )
                if not migration_failed:
                    raise MigrationError(version, version + 1, exc) from exc

    return applied


# ---------------------------------------------------------------------------
# SQLite migration helpers
#
# These handle the quirks of SQLite's limited ALTER TABLE support.
# ---------------------------------------------------------------------------


def add_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    col_type: str = "TEXT",
    default: str | None = "''",
) -> None:
    """Add a column to a table (idempotent).

    Args:
        conn: SQLite connection.
        table: Table name.
        column: New column name.
        col_type: SQL type (TEXT, INTEGER, REAL, BLOB).
        default: DEFAULT value as a SQL literal (e.g., "''" or "0" or "NULL").
                 If None, no DEFAULT clause is added.

    Note: SQLite requires a DEFAULT for ADD COLUMN with NOT NULL.
    """
    # Check if column already exists (idempotent)
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column in existing:
        return

    default_clause = f" DEFAULT {default}" if default is not None else ""
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}{default_clause}")


def add_index(
    conn: sqlite3.Connection,
    index_name: str,
    table: str,
    columns: list[str],
    *,
    unique: bool = False,
) -> None:
    """Create an index (idempotent via IF NOT EXISTS).

    Args:
        conn: SQLite connection.
        index_name: Name for the index.
        table: Table to index.
        columns: Column names to include.
        unique: Whether to create a UNIQUE index.
    """
    unique_kw = "UNIQUE " if unique else ""
    cols = ", ".join(columns)
    conn.execute(f"CREATE {unique_kw}INDEX IF NOT EXISTS {index_name} ON {table}({cols})")


def drop_index(conn: sqlite3.Connection, index_name: str) -> None:
    """Drop an index (idempotent via IF EXISTS)."""
    conn.execute(f"DROP INDEX IF EXISTS {index_name}")


def rename_column(conn: sqlite3.Connection, table: str, old_name: str, new_name: str) -> None:
    """Rename a column (SQLite >= 3.25.0, idempotent).

    Checks column existence before attempting rename.
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if new_name in existing:
        return  # Already renamed
    if old_name not in existing:
        msg = f"Column {old_name!r} not found in table {table!r}"
        raise ValueError(msg)
    conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old_name} TO {new_name}")


def rebuild_table(
    conn: sqlite3.Connection,
    table: str,
    new_schema_sql: str,
    column_mapping: dict[str, str] | None = None,
) -> None:
    """Recreate a table with a new schema, preserving data.

    This is the "12-step" pattern for SQLite schema changes that ALTER TABLE
    can't handle (changing types, constraints, removing columns, etc.):
      1. Create new table with temp name
      2. Copy data from old table
      3. Drop old table
      4. Rename new table to original name

    Args:
        conn: SQLite connection.
        table: Existing table name.
        new_schema_sql: Full CREATE TABLE statement for the new schema.
                        Must use the table name directly (not a temp name —
                        this function handles the rename dance).
        column_mapping: Optional mapping of {new_col: old_col_or_expr}.
                        If None, copies all columns that exist in both schemas.
                        Use SQL expressions as values for transformations, e.g.:
                        {"priority": "CASE WHEN priority > 4 THEN 4 ELSE priority END"}

    Warning: This drops all indexes, triggers, and views that reference the table.
             Recreate them after calling this function.
    """
    temp_table = f"_filigree_migrate_{table}"

    # Create temp table with new schema (case-insensitive replace of table name)
    pattern = re.compile(
        rf"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{re.escape(table)}\b",
        re.IGNORECASE,
    )
    temp_schema = pattern.sub(f"CREATE TABLE {temp_table}", new_schema_sql, count=1)

    conn.execute(f"DROP TABLE IF EXISTS {temp_table}")  # Clean up any leftover from failed run
    conn.execute(temp_schema)

    if column_mapping is None:
        # Auto-detect shared columns
        old_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        new_cols = [row[1] for row in conn.execute(f"PRAGMA table_info({temp_table})").fetchall()]
        shared = [c for c in new_cols if c in old_cols]
        if not shared:
            conn.execute(f"DROP TABLE IF EXISTS {temp_table}")
            msg = f"No shared columns between old and new schema for table '{table}'"
            raise ValueError(msg)
        select_cols = ", ".join(shared)
        insert_cols = select_cols
    else:
        insert_cols = ", ".join(column_mapping.keys())
        select_cols = ", ".join(column_mapping.values())

    # S608: table/column names are from internal schema, not user input
    insert_sql = f"INSERT INTO {temp_table} ({insert_cols}) SELECT {select_cols} FROM {table}"  # noqa: S608
    conn.execute(insert_sql)

    # The caller (migration runner) is responsible for disabling FK
    # enforcement before starting the transaction, so DROP TABLE works
    # even for FK-referenced tables without breaking atomicity.
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {temp_table} RENAME TO {table}")


# ---------------------------------------------------------------------------
# Migration templates
#
# Copy one of these as a starting point for a new migration. Keep the
# docstring — it serves as the migration's changelog entry.
# ---------------------------------------------------------------------------


def _template_simple_migration(conn: sqlite3.Connection) -> None:
    """v1 → v2: <describe what changes and why>.

    Changes:
      - issues: add 'foo' column (TEXT, default '')
      - new index idx_issues_foo on issues(foo)
    """
    add_column(conn, "issues", "foo", "TEXT", "''")
    add_index(conn, "idx_issues_foo", "issues", ["foo"])


def _template_table_rebuild_migration(conn: sqlite3.Connection) -> None:
    """v2 → v3: <describe what changes and why>.

    Changes:
      - issues: change priority CHECK constraint from 0-4 to 0-5
      - issues: drop legacy 'foo' column

    Uses rebuild_table because ALTER TABLE can't modify constraints.
    """
    new_schema = """\
    CREATE TABLE issues (
        id          TEXT PRIMARY KEY,
        title       TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'open',
        priority    INTEGER NOT NULL DEFAULT 2,
        -- ... full table definition with new constraints ...
        CHECK (priority BETWEEN 0 AND 5)
    )"""

    rebuild_table(
        conn,
        "issues",
        new_schema,
        # Explicit column mapping — omit 'foo' to drop it, transform priority
        column_mapping={
            "id": "id",
            "title": "title",
            "status": "status",
            "priority": "MIN(priority, 5)",  # clamp old values to new range
        },
    )

    # Recreate indexes (rebuild_table drops them)
    add_index(conn, "idx_issues_status", "issues", ["status"])
    add_index(conn, "idx_issues_priority", "issues", ["priority"])


def _template_new_table_migration(conn: sqlite3.Connection) -> None:
    """v3 → v4: <describe what changes and why>.

    Changes:
      - new table 'attachments' for file references
      - new index on attachments(issue_id)
    """
    # Use execute() not executescript() — executescript implicitly commits,
    # breaking the migration runner's per-migration transaction guarantees.
    conn.execute("""\
        CREATE TABLE IF NOT EXISTS attachments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            issue_id   TEXT NOT NULL REFERENCES issues(id),
            filename   TEXT NOT NULL,
            mime_type  TEXT DEFAULT '',
            size_bytes INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_attachments_issue ON attachments(issue_id)")
