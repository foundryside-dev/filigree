"""Migrate from .beads SQLite database to filigree.

One-time migration. Maps beads schema -> filigree schema, preserving IDs,
dependencies, events, labels, and comments. Domain-specific beads columns
(design, acceptance_criteria, estimated_minutes, etc.) go into the JSON
fields bag.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from filigree.core import FiligreeDB


def _safe_priority(raw: int | float | str | None) -> int:
    """Coerce a raw priority value to a valid int in 0..4, defaulting to 2."""
    if raw is None:
        return 2
    try:
        return max(0, min(4, int(raw)))
    except (TypeError, ValueError):
        return 2


def _is_parseable_iso(raw: str) -> bool:
    """True when ``raw`` is a valid ISO-8601 timestamp string."""
    try:
        datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return False
    return True


def _safe_timestamp(raw: object, fallback: str | None = None) -> str:
    """Return a valid ISO-8601 timestamp string.

    When ``raw`` parses as ISO-8601, return it unchanged. Otherwise return
    ``fallback`` if it is also a parseable ISO-8601 string. As a last resort,
    synthesize ``datetime.now(UTC).isoformat()`` — but note that is non-
    deterministic across runs, which breaks downstream dedup keys (events,
    comments). Callers migrating re-runnable data MUST pass a stable
    ``fallback`` (typically the owning issue's already-normalized timestamp).
    """
    if raw and isinstance(raw, str) and raw.strip():
        try:
            datetime.fromisoformat(raw)
            return raw
        except ValueError:
            pass
    if fallback is not None:
        try:
            datetime.fromisoformat(fallback)
            return fallback
        except (TypeError, ValueError):
            pass
    return datetime.now(UTC).isoformat()


# Beads columns that map to the filigree `fields` JSON bag.
# Everything not in the core schema gets stuffed into fields.
FIELDS_COLUMNS = [
    "design",
    "acceptance_criteria",
    "estimated_minutes",
    "close_reason",
    "external_ref",
    "mol_type",
    "work_type",
    "quality_score",
    "source_system",
    "event_kind",
    "actor",
    "target",
    "payload",
    "source_repo",
    "await_type",
    "await_id",
    "role_type",
    "rig",
    "spec_id",
    "wisp_type",
    "sender",
]


def _is_missing_table_error(e: sqlite3.OperationalError) -> bool:
    """Check if a SQLite OperationalError is due to a missing table."""
    return "no such table" in str(e).lower()


def migrate_from_beads(beads_db_path: str | Path, tracker: FiligreeDB) -> int:
    """Migrate all non-deleted issues from beads to filigree. Returns count.

    The migration is atomic: if any step fails, all changes are rolled back.
    """
    beads_conn = sqlite3.connect(str(beads_db_path))
    try:
        beads_conn.row_factory = sqlite3.Row

        # -- Migrate issues (two-pass to avoid FK ordering issues)
        rows = beads_conn.execute("SELECT * FROM issues WHERE deleted_at IS NULL").fetchall()
        migrated_ids = {row["id"] for row in rows}

        # Pass 1: build issue data and insert WITHOUT parent_id to avoid FK ordering
        parent_map: dict[str, str] = {}  # id -> parent_id for pass 2
        inserted_ids: set[str] = set()  # track actually inserted rows for safe pass 2
        # Owning-issue timestamps drive deterministic fallbacks for events/comments
        # whose source created_at is blank/invalid — keeps dedup keys stable across re-runs.
        issue_fallback_ts: dict[str, str] = {}
        count = 0
        for row in rows:
            # Build fields bag from beads-specific columns
            fields: dict[str, object] = {}
            for col in FIELDS_COLUMNS:
                try:
                    val = row[col]
                except IndexError:
                    val = None
                if val is not None and val != "":
                    fields[col] = val

            # Also preserve beads metadata if present
            if row["metadata"] and row["metadata"] != "null":
                try:
                    meta = json.loads(row["metadata"])
                    if meta:
                        fields["_beads_metadata"] = meta
                except (json.JSONDecodeError, TypeError):
                    pass

            # Map beads status -> filigree status
            status = row["status"]
            if status not in ("open", "in_progress", "closed"):
                status = "open"  # Default unknown statuses to open

            # Map priority (beads uses 0-4 same as us)
            priority = _safe_priority(row["priority"])

            # Map type
            issue_type = row["issue_type"] or "task"

            # Map parent relationships: prefer parent_id, fall back to parent_epic
            parent_id: str | None = None
            with contextlib.suppress(IndexError, KeyError):
                parent_id = row["parent_id"] or None
            if parent_id is None:
                with contextlib.suppress(IndexError, KeyError):
                    parent_id = row["parent_epic"] or None
            # Only keep parent_id if the referenced issue was also migrated
            if parent_id and parent_id not in migrated_ids:
                parent_id = None

            # Defer parent_id to pass 2 so insert order doesn't matter
            if parent_id:
                parent_map[row["id"]] = parent_id

            created_at_iso = _safe_timestamp(row["created_at"])
            updated_at_iso = _safe_timestamp(row["updated_at"], fallback=created_at_iso)
            # Deterministic fallback for child rows (events, comments) — keeps
            # dedup keys stable across re-migrations when source ts is blank/bad.
            issue_fallback_ts[row["id"]] = updated_at_iso

            # Non-done issues must not carry a closed_at (filigree clears it on
            # reopen — beads doesn't, so a stale value can leak through).
            # Done issues need a parseable ISO timestamp for downstream
            # analytics/archival; fall back to updated_at when source is bad.
            raw_closed = row["closed_at"]
            closed_at_iso: str | None
            if status == "closed":
                closed_at_iso = (
                    raw_closed if isinstance(raw_closed, str) and raw_closed.strip() and _is_parseable_iso(raw_closed) else updated_at_iso
                )
            else:
                closed_at_iso = None

            issue_data = {
                "id": row["id"],
                "title": row["title"] or "(untitled)",
                "status": status,
                "priority": priority,
                "type": issue_type,
                "parent_id": None,
                "assignee": row["assignee"] or "",
                "created_at": created_at_iso,
                "updated_at": updated_at_iso,
                "closed_at": closed_at_iso,
                "description": row["description"] or "",
                "notes": row["notes"] or "",
                "fields": fields,
            }

            tracker.bulk_insert_issue(issue_data, validate=False)
            if tracker.conn.execute("SELECT changes()").fetchone()[0] > 0:
                count += 1
                inserted_ids.add(row["id"])

        # Pass 2: set parent_id only for rows actually inserted in this run
        # (avoids overwriting hierarchy changes made after a previous migration)
        for issue_id, pid in parent_map.items():
            if issue_id in inserted_ids:
                tracker.conn.execute("UPDATE issues SET parent_id = ? WHERE id = ?", (pid, issue_id))

        # -- Migrate dependencies (only where both sides were migrated)
        deps = beads_conn.execute("SELECT * FROM dependencies").fetchall()
        for dep in deps:
            if dep["issue_id"] in migrated_ids and dep["depends_on_id"] in migrated_ids:
                tracker.bulk_insert_dependency(
                    dep["issue_id"],
                    dep["depends_on_id"],
                    dep["type"] or "blocks",
                    validate=False,
                )

        # -- Migrate events (only for migrated issues)
        try:
            events = beads_conn.execute(
                "SELECT issue_id, event_type, actor, old_value, new_value, comment, created_at FROM events"
            ).fetchall()
            for evt in events:
                if evt["issue_id"] in migrated_ids:
                    tracker.bulk_insert_event(
                        {
                            "issue_id": evt["issue_id"],
                            "event_type": evt["event_type"] or "unknown",
                            "actor": evt["actor"] or "beads",
                            "old_value": evt["old_value"],
                            "new_value": evt["new_value"],
                            "comment": evt["comment"] or "",
                            "created_at": _safe_timestamp(
                                evt["created_at"],
                                fallback=issue_fallback_ts.get(evt["issue_id"]),
                            ),
                        }
                    )
        except sqlite3.OperationalError as e:
            if not _is_missing_table_error(e):
                raise

        # -- Migrate labels (only for migrated issues)
        try:
            labels = beads_conn.execute("SELECT issue_id, label FROM labels").fetchall()
            for lbl in labels:
                if lbl["issue_id"] in migrated_ids:
                    tracker.conn.execute(
                        "INSERT OR IGNORE INTO labels (issue_id, label) VALUES (?, ?)",
                        (lbl["issue_id"], lbl["label"]),
                    )
        except sqlite3.OperationalError as e:
            if not _is_missing_table_error(e):
                raise

        # -- Migrate comments (only for migrated issues, with dedup)
        try:
            comments = beads_conn.execute("SELECT issue_id, author, text, created_at FROM comments").fetchall()
            for cmt in comments:
                if cmt["issue_id"] in migrated_ids:
                    cmt_ts = _safe_timestamp(
                        cmt["created_at"],
                        fallback=issue_fallback_ts.get(cmt["issue_id"]),
                    )
                    cmt_author = cmt["author"] or ""
                    tracker.conn.execute(
                        "INSERT INTO comments (issue_id, author, text, created_at) "
                        "SELECT ?, ?, ?, ? "
                        "WHERE NOT EXISTS ("
                        "  SELECT 1 FROM comments "
                        "  WHERE issue_id = ? AND text = ? AND author = ? AND created_at = ?"
                        ")",
                        (
                            cmt["issue_id"],
                            cmt_author,
                            cmt["text"],
                            cmt_ts,
                            cmt["issue_id"],
                            cmt["text"],
                            cmt_author,
                            cmt_ts,
                        ),
                    )
        except sqlite3.OperationalError as e:
            if not _is_missing_table_error(e):
                raise

        tracker.bulk_commit()
        return count
    except BaseException:
        # Roll back any uncommitted writes on the target DB
        with contextlib.suppress(Exception):
            tracker.conn.rollback()
        raise
    finally:
        beads_conn.close()
