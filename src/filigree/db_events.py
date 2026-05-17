"""EventsMixin — event recording, undo, archive, and compaction.

Extracted from core.py as part of the module architecture split.
All methods access ``self.conn``, ``self.get_issue()``, etc. via
Python's MRO when composed into ``FiligreeDB``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from filigree.db_base import DBMixinProtocol, _now_iso
from filigree.types.events import EventRecord, EventRecordWithTitle, EventType, UndoResult

_UNDO_CLAIM_LEASE_HOURS = 48


def _undo_claim_expiry(now: str) -> str:
    return (datetime.fromisoformat(str(now)) + timedelta(hours=_UNDO_CLAIM_LEASE_HOURS)).isoformat()


# ---------------------------------------------------------------------------
# Undo constants (moved from core.py — only used by undo_last)
# ---------------------------------------------------------------------------

_REVERSIBLE_EVENTS = frozenset(
    {
        "status_changed",
        "title_changed",
        "priority_changed",
        "assignee_changed",
        "claimed",
        "dependency_added",
        "dependency_removed",
        "description_changed",
        "notes_changed",
        "fields_changed",
        "parent_changed",
    }
)


class EventsMixin(DBMixinProtocol):
    """Event recording, undo, archive, and compaction methods.

    Inherits ``DBMixinProtocol`` for type-safe access to shared attributes
    (``self.conn``, ``self.get_issue()``, etc.). Actual implementations
    provided by ``FiligreeDB`` at composition time via MRO.
    """

    # -- Build helpers (replace cast() at SQL boundary) -----------------------

    @staticmethod
    def _build_event_record(row: sqlite3.Row) -> EventRecord:
        """Build an EventRecord from a database row with explicit key mapping."""
        return EventRecord(
            id=row["id"],
            issue_id=row["issue_id"],
            event_type=row["event_type"],
            actor=row["actor"],
            old_value=row["old_value"],
            new_value=row["new_value"],
            comment=row["comment"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _build_event_record_with_title(row: sqlite3.Row) -> EventRecordWithTitle:
        """Build an EventRecordWithTitle from a joined database row."""
        return EventRecordWithTitle(
            id=row["id"],
            issue_id=row["issue_id"],
            event_type=row["event_type"],
            actor=row["actor"],
            old_value=row["old_value"],
            new_value=row["new_value"],
            comment=row["comment"],
            created_at=row["created_at"],
            issue_title=row["issue_title"],
        )

    # -- Events (private) ----------------------------------------------------

    def _record_event(
        self,
        issue_id: str,
        event_type: EventType,
        *,
        actor: str = "",
        old_value: str | None = None,
        new_value: str | None = None,
        comment: str = "",
    ) -> None:
        # 2.1.0 §0.2: plain INSERT (not INSERT OR IGNORE) so true-duplicate
        # collisions bubble up to the caller's transaction for rollback.
        # ``event_seq`` is computed inline as the next per-issue monotonic
        # value via COALESCE+MAX subquery — multi-process-safe (no
        # in-memory counter), survives crashes, and runs atomically inside
        # the caller's open transaction. Same-second emissions (heartbeat
        # bursts, batch ops sharing _now_iso()) get distinct sequence
        # numbers and stop silently colliding on the dedup index.
        self.conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, old_value, new_value, comment, created_at, event_seq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT MAX(event_seq) FROM events WHERE issue_id = ?), -1) + 1)",
            (issue_id, event_type, actor, old_value, new_value, comment, _now_iso(), issue_id),
        )

    def get_recent_events(self, limit: int = 20) -> list[EventRecordWithTitle]:
        rows = self.conn.execute(
            "SELECT e.*, i.title as issue_title FROM events e "
            "JOIN issues i ON e.issue_id = i.id "
            "ORDER BY e.created_at DESC, e.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._build_event_record_with_title(r) for r in rows]

    def get_events_since(
        self,
        since: str,
        *,
        after_event_id: int | None = None,
        limit: int = 100,
        actor: str | None = None,
        issue_id: str | None = None,
        label: str | None = None,
        event_type: str | None = None,
        exclude_types: list[str] | None = None,
    ) -> list[EventRecordWithTitle]:
        """Get events since a given ISO timestamp, ordered chronologically.

        ``exclude_types`` filters out specific event types from the result
        (e.g. ``["heartbeat"]``); takes precedence over an inclusive
        ``event_type`` filter only when no overlap exists. The catch-up MCP
        path defaults to excluding ``heartbeat`` so liveness pings don't
        dominate session-resumption feeds (filigree-cb980eee0d, P2.11).
        """
        if after_event_id is None:
            clauses = ["e.created_at > ?"]
            params: list[object] = [since]
        else:
            clauses = ["(e.created_at > ? OR (e.created_at = ? AND e.id > ?))"]
            params = [since, since, after_event_id]
        if actor is not None:
            clauses.append("e.actor = ?")
            params.append(actor)
        if issue_id is not None:
            clauses.append("e.issue_id = ?")
            params.append(issue_id)
        if label is not None:
            clauses.append("EXISTS (SELECT 1 FROM labels l WHERE l.issue_id = e.issue_id AND l.label = ?)")
            params.append(label)
        if event_type is not None:
            clauses.append("e.event_type = ?")
            params.append(event_type)
        if exclude_types:
            placeholders = ",".join("?" for _ in exclude_types)
            clauses.append(f"e.event_type NOT IN ({placeholders})")
            params.extend(exclude_types)
        params.append(limit)

        where_sql = " AND ".join(clauses)
        rows = self.conn.execute(
            "SELECT e.*, i.title as issue_title FROM events e "
            "JOIN issues i ON e.issue_id = i.id "
            f"WHERE {where_sql} "
            "ORDER BY e.created_at ASC, e.id ASC LIMIT ?",
            params,
        ).fetchall()
        return [self._build_event_record_with_title(r) for r in rows]

    def get_issue_events(self, issue_id: str, *, limit: int = 50, offset: int = 0) -> list[EventRecord]:
        """Get events for a specific issue, newest first."""
        self.get_issue(issue_id)  # raises KeyError if not found
        rows = self.conn.execute(
            "SELECT * FROM events WHERE issue_id = ? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (issue_id, limit, offset),
        ).fetchall()
        return [self._build_event_record(r) for r in rows]

    def undo_last(self, issue_id: str, *, actor: str = "") -> UndoResult:
        """Undo the most recent reversible event for an issue.

        Returns dict with 'undone' bool and details. Only reverses the single
        most recent reversible event — 'undone' events are not themselves
        undoable, preventing undo chains.
        """
        current = self.get_issue(issue_id)
        now = _now_iso()

        # Acquire SQLite's write lock before the candidate SELECT so the
        # read-check-write sequence is atomic. Without this, two connections
        # can both pass the NOT EXISTS check and both write 'undone' markers
        # for the same target event (filigree-f38d4e2874). The try/finally
        # below releases the lock on every exit path, including the
        # ``undone=False`` early returns inside the match block.
        opened_txn = False
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
            opened_txn = True

        try:
            # Find the most recent reversible event that has not already been
            # covered by an 'undone' marker. Filtering already-undone events at
            # SELECT time (not after) lets undo fall back to earlier reversible
            # history when the newest reversible event has been undone already.
            rev_ph = ",".join("?" * len(_REVERSIBLE_EVENTS))
            row = self.conn.execute(
                f"SELECT * FROM events e WHERE e.issue_id = ? AND e.event_type IN ({rev_ph}) "
                f"AND NOT EXISTS ("
                f"  SELECT 1 FROM events u WHERE u.issue_id = e.issue_id "
                f"  AND u.event_type = 'undone' AND u.new_value = CAST(e.id AS TEXT)"
                f") "
                f"ORDER BY e.created_at DESC, e.id DESC LIMIT 1",
                (issue_id, *_REVERSIBLE_EVENTS),
            ).fetchone()

            if row is None:
                return {"undone": False, "reason": "No reversible events to undo"}

            event_type = row["event_type"]
            event_id = row["id"]

            match event_type:
                case "status_changed":
                    old_status = row["old_value"]
                    if old_status is None:
                        return {"undone": False, "reason": "Cannot undo: event has no old_value"}
                    # Direct SQL update — bypasses transition validation for undo
                    self.conn.execute(
                        "UPDATE issues SET status = ?, updated_at = ? WHERE id = ?",
                        (old_status, now, issue_id),
                    )
                    # Maintain closed_at consistency with the restored status
                    old_cat = self._resolve_status_category(current.type, old_status)
                    if old_cat == "done":
                        # Restoring to a done state — set closed_at
                        self.conn.execute(
                            "UPDATE issues SET closed_at = ? WHERE id = ?",
                            (now, issue_id),
                        )
                    else:
                        # Restoring to a non-done state — clear closed_at and any
                        # close-only fields (e.g. ``close_reason``). Mirrors
                        # ``reopen_issue``'s contract so undo and reopen converge
                        # on the same shape, and lets the F2 close-with-reason
                        # composite event reverse fully in a single undo call.
                        from filigree.db_issues import _REOPEN_CLEAR_FIELDS

                        self.conn.execute(
                            "UPDATE issues SET closed_at = NULL WHERE id = ?",
                            (issue_id,),
                        )
                        existing_fields = current.fields or {}
                        cleaned_fields = {k: v for k, v in existing_fields.items() if k not in _REOPEN_CLEAR_FIELDS}
                        if cleaned_fields != existing_fields:
                            self.conn.execute(
                                "UPDATE issues SET fields = ? WHERE id = ?",
                                (json.dumps(cleaned_fields), issue_id),
                            )

                case "title_changed":
                    if row["old_value"] is None:
                        return {"undone": False, "reason": "Cannot undo: event has no old_value"}
                    self.conn.execute(
                        "UPDATE issues SET title = ?, updated_at = ? WHERE id = ?",
                        (row["old_value"], now, issue_id),
                    )

                case "priority_changed":
                    if row["old_value"] is None:
                        return {"undone": False, "reason": "Cannot undo: event has no old_value"}
                    try:
                        old_priority = int(row["old_value"])
                    except (ValueError, TypeError):
                        return {"undone": False, "reason": f"Cannot undo: old_value {row['old_value']!r} is not a valid priority"}
                    self.conn.execute(
                        "UPDATE issues SET priority = ?, updated_at = ? WHERE id = ?",
                        (old_priority, now, issue_id),
                    )

                case "assignee_changed":
                    old_assignee = row["old_value"] or ""
                    if old_assignee:
                        self.conn.execute(
                            "UPDATE issues SET assignee = ?, claimed_at = ?, last_heartbeat_at = ?, "
                            "claim_expires_at = ?, updated_at = ? WHERE id = ?",
                            (old_assignee, now, now, _undo_claim_expiry(now), now, issue_id),
                        )
                    else:
                        self.conn.execute(
                            "UPDATE issues SET assignee = '', claimed_at = NULL, last_heartbeat_at = NULL, "
                            "claim_expires_at = NULL, updated_at = ? WHERE id = ?",
                            (now, issue_id),
                        )

                case "claimed":
                    # Restore: revert to the assignee before the claim (usually '' but
                    # preserves prior assignee if the claim re-assigned from another agent)
                    old_assignee = row["old_value"] if row["old_value"] is not None else ""
                    if old_assignee:
                        self.conn.execute(
                            "UPDATE issues SET assignee = ?, claimed_at = ?, last_heartbeat_at = ?, "
                            "claim_expires_at = ?, updated_at = ? WHERE id = ?",
                            (old_assignee, now, now, _undo_claim_expiry(now), now, issue_id),
                        )
                    else:
                        self.conn.execute(
                            "UPDATE issues SET assignee = '', claimed_at = NULL, last_heartbeat_at = NULL, "
                            "claim_expires_at = NULL, updated_at = ? WHERE id = ?",
                            (now, issue_id),
                        )

                case "dependency_added":
                    # Event: issue_id=from_id, new_value="type:depends_on_id"
                    # rsplit so a dep_type that itself contains ':' still rounds-trips.
                    if row["new_value"] is None:
                        return {"undone": False, "reason": "Cannot undo: event has no new_value"}
                    dep_target = row["new_value"].rsplit(":", 1)[-1] if ":" in row["new_value"] else row["new_value"]
                    self.conn.execute(
                        "DELETE FROM dependencies WHERE issue_id = ? AND depends_on_id = ?",
                        (issue_id, dep_target),
                    )

                case "dependency_removed":
                    # Event: issue_id=from_id, old_value="dep_type:depends_on_id" or legacy "depends_on_id"
                    # rsplit because dep_type may contain ':'; issue IDs do not.
                    if row["old_value"] is None:
                        return {"undone": False, "reason": "Cannot undo: event has no old_value"}
                    old_val = row["old_value"]
                    if ":" in old_val:
                        dep_type, dep_target = old_val.rsplit(":", 1)
                    else:
                        dep_type, dep_target = "blocks", old_val
                    # Check for cycles before re-inserting (inline DFS
                    # to avoid cross-mixin call that mypy can't resolve)
                    adj: dict[str, list[str]] = {}
                    for dep_row in self.conn.execute("SELECT issue_id, depends_on_id FROM dependencies").fetchall():
                        adj.setdefault(dep_row["issue_id"], []).append(dep_row["depends_on_id"])
                    visited: set[str] = set()
                    queue = [dep_target]
                    would_cycle = False
                    while queue:
                        cur = queue.pop()
                        if cur == issue_id:
                            would_cycle = True
                            break
                        if cur in visited:
                            continue
                        visited.add(cur)
                        queue.extend(adj.get(cur, ()))
                    if would_cycle:
                        return {"undone": False, "reason": "Cannot undo: restoring dependency would create a cycle"}
                    self.conn.execute(
                        "INSERT OR IGNORE INTO dependencies (issue_id, depends_on_id, type, created_at) VALUES (?, ?, ?, ?)",
                        (issue_id, dep_target, dep_type, now),
                    )

                case "description_changed":
                    self.conn.execute(
                        "UPDATE issues SET description = ?, updated_at = ? WHERE id = ?",
                        (row["old_value"] or "", now, issue_id),
                    )

                case "notes_changed":
                    self.conn.execute(
                        "UPDATE issues SET notes = ?, updated_at = ? WHERE id = ?",
                        (row["old_value"] or "", now, issue_id),
                    )

                case "fields_changed":
                    # filigree-cb4cd68f80: require old_value to be present and
                    # to decode to a JSON object. Treating NULL as ``{}`` or
                    # accepting list/string JSON would silently violate the
                    # ``issues.fields`` invariant (always a JSON object).
                    raw_fields = row["old_value"]
                    if raw_fields is None:
                        return {"undone": False, "reason": "Cannot undo: event has no old_value"}
                    try:
                        parsed_fields = json.loads(raw_fields)
                    except (json.JSONDecodeError, TypeError):
                        return {"undone": False, "reason": "Cannot undo: stored fields JSON is corrupt"}
                    if not isinstance(parsed_fields, dict):
                        return {"undone": False, "reason": "Cannot undo: stored fields is not a JSON object"}
                    self.conn.execute(
                        "UPDATE issues SET fields = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(parsed_fields), now, issue_id),
                    )

                case "parent_changed":
                    # Event: old_value is the previous parent_id, "" or None for root.
                    # Restore: empty/None → NULL; non-empty → verify the issue still
                    # exists, otherwise refuse rather than dangling-pointer the FK.
                    old_parent = row["old_value"]
                    if old_parent in (None, ""):
                        self.conn.execute(
                            "UPDATE issues SET parent_id = NULL, updated_at = ? WHERE id = ?",
                            (now, issue_id),
                        )
                    else:
                        exists = self.conn.execute("SELECT 1 FROM issues WHERE id = ?", (old_parent,)).fetchone()
                        if exists is None:
                            return {
                                "undone": False,
                                "reason": f"Cannot undo: prior parent {old_parent!r} no longer exists",
                            }
                        # filigree-0a8c3d38d7: re-run the same cycle check
                        # update_issue() enforces. Interleaved reparents can
                        # make the previously-valid parent into an ancestor
                        # of this issue, so restoring it would form a loop.
                        if self._would_create_parent_cycle(issue_id, old_parent):
                            return {
                                "undone": False,
                                "reason": f"Cannot undo: restoring parent {old_parent!r} would create a circular parent chain",
                            }
                        self.conn.execute(
                            "UPDATE issues SET parent_id = ?, updated_at = ? WHERE id = ?",
                            (old_parent, now, issue_id),
                        )

            # Record the undo event
            self._record_event(
                issue_id,
                "undone",
                actor=actor,
                old_value=event_type,
                new_value=str(event_id),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            # Release the write lock on any early-return path that left the
            # transaction we opened uncommitted (e.g. row is None, or a
            # ``undone=False`` branch inside the match block).
            if opened_txn and self.conn.in_transaction:
                self.conn.rollback()

        return {
            "undone": True,
            "event_type": event_type,
            "event_id": event_id,
            "issue": self.get_issue(issue_id).to_dict(),
        }

    # -- Archival / Compaction ------------------------------------------------

    def archive_closed(self, *, days_old: int = 30, actor: str = "", label: str | None = None) -> list[str]:
        """Archive done-category issues older than `days_old` days.

        When ``label`` is provided, only issues currently carrying that label
        are archived. This gives review/test cleanup a scoped maintenance path
        without changing the default project-wide archive behavior.

        Sets their status to 'archived' (preserving closed_at).
        Returns list of archived issue IDs.
        """
        from datetime import timedelta

        if days_old < 0:
            msg = f"days_old must be >= 0, got {days_old}"
            raise ValueError(msg)

        normalized_label = self._validate_label_name(label, allow_priority_like=True) if label is not None else None

        cutoff_dt = datetime.now(UTC) - timedelta(days=days_old)
        cutoff = cutoff_dt.isoformat()

        # Match (type, status) pairs so a state name shared across types in
        # different categories is classified per type (filigree-b55aa3191f).
        # Archive_closed selects done-category rows only — not the synthetic
        # 'archived' status, which would re-archive already-archived rows.
        done_sql, done_params = self._category_predicate_sql("done", type_col="type", status_col="status")
        if not done_params:
            # No done-category states registered; nothing to archive.
            return []
        clauses = [f"({done_sql})", "closed_at < ?", "closed_at IS NOT NULL"]
        params: list[object] = [*done_params, cutoff]
        if normalized_label is not None:
            clauses.append("EXISTS (SELECT 1 FROM labels l WHERE l.issue_id = issues.id AND l.label = ?)")
            params.append(normalized_label)
        where_sql = " AND ".join(clauses)
        rows = self.conn.execute(
            f"SELECT id FROM issues WHERE {where_sql}",
            params,
        ).fetchall()

        archived_ids = [r["id"] for r in rows]
        if not archived_ids:
            return []

        now = _now_iso()
        try:
            for issue_id in archived_ids:
                self.conn.execute(
                    "UPDATE issues SET status = 'archived', updated_at = ? WHERE id = ?",
                    (now, issue_id),
                )
                self._record_event(issue_id, "archived", actor=actor)

            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return archived_ids

    def compact_events(self, *, keep_recent: int = 50, actor: str = "") -> int:
        """Remove old events for archived issues, keeping only the most recent ones.

        Returns the number of events deleted.
        """
        if keep_recent < 0:
            msg = f"keep_recent must be >= 0, got {keep_recent}"
            raise ValueError(msg)
        archived = self.conn.execute("SELECT id FROM issues WHERE status = 'archived'").fetchall()
        if not archived:
            return 0

        total_deleted = 0
        try:
            for row in archived:
                issue_id = row["id"]
                event_count = self.conn.execute("SELECT COUNT(*) as cnt FROM events WHERE issue_id = ?", (issue_id,)).fetchone()["cnt"]

                if event_count <= keep_recent:
                    continue

                cursor = self.conn.execute(
                    "DELETE FROM events WHERE id IN (SELECT id FROM events WHERE issue_id = ? ORDER BY created_at ASC, id ASC LIMIT ?)",
                    (issue_id, event_count - keep_recent),
                )
                total_deleted += cursor.rowcount

            if total_deleted > 0:
                self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        return total_deleted

    def vacuum(self) -> None:
        """Run VACUUM to reclaim space after compaction."""
        self.conn.execute("VACUUM")

    def analyze(self) -> None:
        """Update query planner statistics."""
        self.conn.execute("ANALYZE")
