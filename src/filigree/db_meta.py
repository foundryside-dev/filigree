"""MetaMixin — comments, labels, stats, bulk operations, and export/import.

All methods access ``self.conn``, ``self.get_issue()``, etc. via
Python's MRO when composed into ``FiligreeDB``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, ClassVar

from filigree.db_base import DBMixinProtocol, _in_immediate_tx, _normalize_iso_to_utc, _now_iso, _retry_busy
from filigree.db_files import VALID_FINDING_STATUSES, VALID_SEVERITIES, _normalize_project_relative_scan_path
from filigree.db_issues import _check_expected_assignee, _validate_fields_payload, _validate_priority_value
from filigree.db_observations import _expires_iso
from filigree.types.planning import CommentRecord, StatsResult

logger = logging.getLogger(__name__)

_VALID_FILE_REGISTRY_BACKENDS = frozenset({"local", "clarion"})


class MetaMixin(DBMixinProtocol):
    """Comments, labels, stats, bulk operations, and export/import.

    Declares ``DBMixinProtocol`` as a base for type-safe access to shared
    attributes. The Protocol provides method stubs for static analysis;
    actual implementations are provided by ``FiligreeDB`` at composition
    time via MRO.
    """

    # -- Comments ------------------------------------------------------------

    @_retry_busy()
    @_in_immediate_tx("add_comment")
    def add_comment(
        self,
        issue_id: str,
        text: str,
        *,
        author: str = "",
        expected_assignee: str | None = None,
        _skip_begin: bool = False,
    ) -> int:
        if not text or not text.strip():
            msg = "Comment text cannot be empty"
            raise ValueError(msg)
        self._check_id_prefix(issue_id)
        row = self.conn.execute("SELECT assignee FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None:
            msg = f"Issue not found: {issue_id}"
            raise KeyError(msg)
        _check_expected_assignee(issue_id, expected_assignee, row["assignee"] or "", actor=author)
        now = _now_iso()
        cursor = self.conn.execute(
            "INSERT INTO comments (issue_id, author, verified_author, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (issue_id, author, self._verified_actor, text, now),
        )
        rowid = cursor.lastrowid
        if rowid is None:  # pragma: no cover — INSERT always sets lastrowid
            msg = "INSERT did not produce a lastrowid"
            raise RuntimeError(msg)
        return rowid

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

    # -- Labels --------------------------------------------------------------

    @_retry_busy()
    @_in_immediate_tx("add_label")
    def add_label(
        self,
        issue_id: str,
        label: str,
        *,
        actor: str = "",
        expected_assignee: str | None = None,
        _skip_begin: bool = False,
    ) -> tuple[bool, str, list[str]]:
        """Add label to issue. Returns (added, canonical_label, replaced_labels).

        ``canonical_label`` is the stored form after normalization (strip, etc.);
        callers rendering output should use it rather than the raw argument.

        ``replaced_labels`` is the list of labels that were silently removed
        because of mutual-exclusivity rules in the same namespace (currently
        ``review:``). When non-empty, surfaces should report the displacement
        in their response so callers can audit silent label changes
        (filigree-cb980eee0d, P2.7 senior-user MCP review).

        ``expected_assignee`` is a claim-aware precondition. When omitted and
        ``actor`` is present, held issues default the expected holder to actor
        (ADR-008).
        """
        self._check_id_prefix(issue_id)
        row = self.conn.execute("SELECT assignee FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None:
            msg = f"Issue not found: {issue_id}"
            raise KeyError(msg)
        _check_expected_assignee(issue_id, expected_assignee, row["assignee"] or "", actor=actor)
        normalized = self._validate_label_name(label)
        replaced: list[str] = []
        # Idempotency for review:* — the mutual-exclusivity DELETE below would
        # otherwise turn a no-op re-add into delete-then-reinsert and falsely
        # report (True, ...). Detect and short-circuit when the existing review
        # set is exactly {normalized}.
        if normalized.startswith("review:"):
            existing_review = {
                row["label"]
                for row in self.conn.execute(
                    "SELECT label FROM labels WHERE issue_id = ? AND label LIKE 'review:%'",
                    (issue_id,),
                ).fetchall()
            }
            if existing_review == {normalized}:
                return False, normalized, []
            # Capture the set of labels we're about to displace so callers can
            # report the silent removal (P2.7).
            replaced = sorted(lbl for lbl in existing_review if lbl != normalized)
        # Mutual exclusivity for review: namespace
        if normalized.startswith("review:"):
            self.conn.execute(
                "DELETE FROM labels WHERE issue_id = ? AND label LIKE 'review:%'",
                (issue_id,),
            )
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO labels (issue_id, label) VALUES (?, ?)",
            (issue_id, normalized),
        )
        return cursor.rowcount > 0, normalized, replaced

    @_retry_busy()
    @_in_immediate_tx("remove_label")
    def remove_label(
        self,
        issue_id: str,
        label: str,
        *,
        actor: str = "",
        expected_assignee: str | None = None,
        _skip_begin: bool = False,
    ) -> tuple[bool, str]:
        """Remove label from issue. Returns (removed, canonical_label).

        ``expected_assignee`` is a claim-aware precondition. When omitted and
        ``actor`` is present, held issues default the expected holder to actor
        (ADR-008).
        """
        self._check_id_prefix(issue_id)
        row = self.conn.execute("SELECT assignee FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None:
            msg = f"Issue not found: {issue_id}"
            raise KeyError(msg)
        _check_expected_assignee(issue_id, expected_assignee, row["assignee"] or "", actor=actor)
        normalized = self._validate_label_name(label, allow_priority_like=True)
        cursor = self.conn.execute(
            "DELETE FROM labels WHERE issue_id = ? AND label = ?",
            (issue_id, normalized),
        )
        return cursor.rowcount > 0, normalized

    def list_labels(
        self,
        *,
        namespace: str | None = None,
        top: int = 10,
    ) -> dict[str, Any]:
        """Return all distinct labels grouped by namespace with counts.

        Includes virtual namespaces with computed counts.
        Sorted alphabetically within each namespace.
        """
        from filigree.db_workflow import WorkflowMixin

        auto_ns = WorkflowMixin.RESERVED_NAMESPACES_AUTO
        virtual_ns = WorkflowMixin.RESERVED_NAMESPACES_VIRTUAL

        rows = self.conn.execute("SELECT label, COUNT(*) as cnt FROM labels GROUP BY label ORDER BY label").fetchall()

        namespaces: dict[str, dict[str, Any]] = {}
        for row in rows:
            lbl = row["label"]
            cnt = row["cnt"]
            ns = lbl.split(":", 1)[0] if ":" in lbl else "_bare"

            if namespace is not None and ns != namespace:
                continue

            if ns not in namespaces:
                ns_lower = ns.casefold() if ns != "_bare" else "_bare"
                if ns_lower in auto_ns:
                    label_type, writable = "auto", False
                elif ns_lower in virtual_ns:
                    label_type, writable = "virtual", False
                else:
                    label_type, writable = "manual", True
                namespaces[ns] = {"type": label_type, "writable": writable, "labels": []}

            namespaces[ns]["labels"].append({"label": lbl, "count": cnt})

        # Add virtual namespaces with computed counts
        if namespace is None or namespace == "age":
            age_labels = self._compute_virtual_age_counts()
            namespaces.setdefault("age", {"type": "virtual", "writable": False, "labels": age_labels})

        if namespace is None or namespace == "has":
            has_labels = self._compute_virtual_has_counts()
            namespaces.setdefault("has", {"type": "virtual", "writable": False, "labels": has_labels})

        # Truncate after virtual namespaces are added so the per-namespace
        # cap applies uniformly. top=0 stays unlimited. Report the pre-cap
        # total so clients can tell when a namespace has hidden tail entries.
        for ns_data in namespaces.values():
            total_before_cap = len(ns_data["labels"])
            ns_data["total"] = total_before_cap
            ns_data["truncated"] = top > 0 and total_before_cap > top
            if top > 0:
                ns_data["labels"] = ns_data["labels"][:top]

        total = sum(len(ns["labels"]) for ns in namespaces.values())
        return {"namespaces": namespaces, "total_in_result": total}

    def _compute_virtual_age_counts(self) -> list[dict[str, Any]]:
        from filigree.db_base import AGE_BUCKETS

        results = []
        for name, (low, high) in sorted(AGE_BUCKETS.items()):
            cnt = self.conn.execute(
                "SELECT COUNT(*) as cnt FROM issues "
                "WHERE datetime(created_at) <= datetime('now', ?) "
                "AND datetime(created_at) > datetime('now', ?)",
                (f"-{low} days", f"-{high} days"),
            ).fetchone()["cnt"]
            results.append({"label": f"age:{name}", "count": cnt})
        return results

    def _compute_virtual_has_counts(self) -> list[dict[str, Any]]:
        # filigree-b55aa3191f: type-aware blocker-done predicate so a blocker
        # whose state name collides across categories (e.g. incident.resolved
        # vs debt_item.resolved) is classified per type. With
        # include_archived=True the predicate is always SQL-valid even when no
        # done-category states are registered (matches archived-only).
        blocker_done_sql, blocker_done_params = self._category_predicate_sql(
            "done", type_col="b.type", status_col="b.status", include_archived=True
        )
        counts = []
        cnt = self.conn.execute(
            f"SELECT COUNT(DISTINCT i.id) as cnt FROM issues i "
            f"JOIN dependencies d ON d.issue_id = i.id "
            f"JOIN issues b ON d.depends_on_id = b.id "
            f"WHERE NOT ({blocker_done_sql})",
            blocker_done_params,
        ).fetchone()["cnt"]
        counts.append({"label": "has:blockers", "count": cnt})
        cnt = self.conn.execute("SELECT COUNT(DISTINCT parent_id) as cnt FROM issues WHERE parent_id IS NOT NULL").fetchone()["cnt"]
        counts.append({"label": "has:children", "count": cnt})
        cnt = self.conn.execute(
            "SELECT COUNT(DISTINCT issue_id) as cnt FROM scan_findings "
            "WHERE issue_id IS NOT NULL AND status NOT IN ('fixed', 'false_positive')"
        ).fetchone()["cnt"]
        counts.append({"label": "has:findings", "count": cnt})
        cnt = self.conn.execute("SELECT COUNT(DISTINCT issue_id) as cnt FROM file_associations").fetchone()["cnt"]
        counts.append({"label": "has:files", "count": cnt})
        cnt = self.conn.execute("SELECT COUNT(DISTINCT issue_id) as cnt FROM comments").fetchone()["cnt"]
        counts.append({"label": "has:comments", "count": cnt})
        return counts

    def get_label_taxonomy(self) -> dict[str, Any]:
        """Return the full label vocabulary with descriptions and writability."""
        return {
            "auto": {
                "area": {"description": "Component area from file paths", "writable": False, "example": "area:mcp"},
                "severity": {
                    "description": "Highest active finding severity",
                    "writable": False,
                    "values": ["critical", "high", "medium", "low", "info"],
                },
                "scanner": {"description": "Scan source that produced findings", "writable": False, "example": "scanner:ruff"},
                "pack": {
                    "description": "Workflow pack the issue type belongs to",
                    "writable": False,
                    "values": ["core", "planning", "release", "requirements"],
                },
            },
            "virtual": {
                "age": {
                    "description": "Issue age bucket",
                    "writable": False,
                    "values": ["fresh", "recent", "aging", "stale", "ancient"],
                },
                "has": {
                    "description": "Existence predicates",
                    "writable": False,
                    "values": ["blockers", "children", "findings", "files", "comments"],
                },
            },
            "manual_suggested": {
                "cluster": {
                    "description": "Root cause pattern for bugs",
                    "writable": True,
                    "examples": ["broad-except", "race-condition", "null-check", "type-coercion", "resource-leak"],
                },
                "effort": {"description": "T-shirt sizing", "writable": True, "values": ["xs", "s", "m", "l", "xl"]},
                "source": {"description": "How the issue was discovered", "writable": True, "examples": ["scanner", "review", "agent"]},
                "agent": {"description": "Agent instance attribution (manual)", "writable": True, "examples": ["claude-1", "claude-2"]},
                "release": {"description": "Release version targeting", "writable": True, "examples": ["v1.3.0", "v1.4.0"]},
                "changelog": {
                    "description": "Changelog category",
                    "writable": True,
                    "values": ["added", "changed", "fixed", "removed", "deprecated"],
                },
                "wait": {
                    "description": "External blocker type",
                    "writable": True,
                    "examples": ["design", "upstream", "vendor", "decision"],
                },
                "breaking": {
                    "description": "Breaking change marker",
                    "writable": True,
                    "examples": ["api", "schema", "config"],
                },
                "review": {
                    "description": "Review workflow state (mutually exclusive)",
                    "writable": True,
                    "mutually_exclusive": True,
                    "values": ["needed", "done", "rework"],
                },
            },
            "bare_labels": {
                "description": "Common labels without namespace prefix",
                "writable": True,
                "suggested": ["tech-debt", "regression", "security", "perf", "cherry-pick", "hotfix", "flaky-test", "wontfix"],
                "reserved": {
                    "patterns": ["P[0-4]", "priority:*"],
                    "writable": False,
                    "reason": (
                        "Use the numeric priority field as the canonical priority signal; "
                        "priority-like labels are rejected to prevent drift."
                    ),
                },
                "discouraged": {
                    "pattern": "P[0-4]",
                    "writable": False,
                    "reason": "Deprecated alias for bare priority labels; these labels are now rejected. Use the priority field instead.",
                },
            },
        }

    # -- Stats ---------------------------------------------------------------

    def get_stats(self) -> StatsResult:
        by_status = {}
        for row in self.conn.execute("SELECT status, COUNT(*) as cnt FROM issues GROUP BY status").fetchall():
            by_status[row["status"]] = row["cnt"]

        by_type = {}
        for row in self.conn.execute("SELECT type, COUNT(*) as cnt FROM issues GROUP BY type").fetchall():
            by_type[row["type"]] = row["cnt"]

        preds = self._resolve_open_blocker_predicates()
        if preds is None:
            ready_count = 0
            blocked_count = 0
        else:
            (open_sql, open_params), (blocker_done_sql, blocker_done_params) = preds
            ready_count = self.conn.execute(
                f"SELECT COUNT(*) as cnt FROM issues i "
                f"WHERE {open_sql} "
                f"AND (i.assignee = '' OR i.assignee IS NULL) "
                f"AND NOT EXISTS ("
                f"  SELECT 1 FROM dependencies d "
                f"  JOIN issues blocker ON d.depends_on_id = blocker.id "
                f"  WHERE d.issue_id = i.id AND NOT ({blocker_done_sql})"
                f")",
                [*open_params, *blocker_done_params],
            ).fetchone()["cnt"]
            blocked_count = self.conn.execute(
                f"SELECT COUNT(DISTINCT i.id) as cnt FROM issues i "
                f"JOIN dependencies d ON d.issue_id = i.id "
                f"JOIN issues blocker ON d.depends_on_id = blocker.id "
                f"WHERE {open_sql} AND NOT ({blocker_done_sql})",
                [*open_params, *blocker_done_params],
            ).fetchone()["cnt"]

        dep_count = self.conn.execute("SELECT COUNT(*) as cnt FROM dependencies").fetchone()["cnt"]

        # Category-level counts (open/wip/done) via template-aware resolution
        by_category: dict[str, int] = {"open": 0, "wip": 0, "done": 0}
        for row in self.conn.execute("SELECT type, status, COUNT(*) as cnt FROM issues GROUP BY type, status").fetchall():
            cat = self._resolve_status_category(row["type"], row["status"])
            by_category[cat] = by_category.get(cat, 0) + row["cnt"]

        return {
            "by_status": by_status,
            "by_category": by_category,
            # DEPRECATED (filigree-17694d2db8): exact duplicates of by_status /
            # by_category. Retained as compatibility aliases on a wire surface
            # (MCP get_stats / get_summary, HTTP StatsWithPrefix) per ADR-009 §7.
            # Remove in the next major; consumers should read by_status /
            # by_category.
            "status_name_counts": dict(by_status),
            "status_category_counts": dict(by_category),
            "by_type": by_type,
            "ready_count": ready_count,
            "blocked_count": blocked_count,
            "total_dependencies": dep_count,
        }

    # -- Bulk import (for migration) -----------------------------------------

    def _validate_bulk_issue_data(
        self,
        issue_data: dict[str, Any],
        *,
        validate_parent_exists: bool = True,
        validate_prefix: bool = True,
    ) -> None:
        issue_id = issue_data["id"]
        if not isinstance(issue_id, str) or not issue_id.strip():
            msg = "Issue id cannot be empty"
            raise ValueError(msg)
        if validate_prefix:
            self._check_id_prefix(issue_id)

        title = issue_data["title"]
        if not isinstance(title, str) or not title.strip():
            msg = "Title cannot be empty"
            raise ValueError(msg)

        priority = issue_data.get("priority", 2)
        _validate_priority_value(priority)

        issue_type = issue_data.get("type", "task")
        if not isinstance(issue_type, str) or self.templates.get_type(issue_type) is None:
            valid_types = [t.type for t in self.templates.list_types()]
            msg = f"Unknown type '{issue_type}'. Valid types: {', '.join(valid_types)}"
            raise ValueError(msg)

        status = issue_data.get("status", self.templates.get_initial_state(issue_type))
        if not isinstance(status, str) or not status.strip():
            msg = "Invalid status"
            raise ValueError(msg)
        self._validate_status(status, issue_type)

        parent_id = issue_data.get("parent_id")
        if parent_id is not None and not isinstance(parent_id, str):
            msg = "parent_id must be a string"
            raise TypeError(msg)
        if parent_id and validate_prefix:
            self._check_id_prefix(parent_id)
        if validate_parent_exists:
            self._validate_parent_id(parent_id)

        assignee = issue_data.get("assignee", "")
        if not isinstance(assignee, str):
            msg = "assignee must be a string"
            raise TypeError(msg)

        fields = issue_data.get("fields")
        _validate_fields_payload(fields)
        field_values = {} if fields is None else fields
        if field_values:
            pattern_errors = self._validate_field_values(issue_type, field_values)
            if pattern_errors:
                msg = "Field validation failed: " + "; ".join(pattern_errors)
                raise ValueError(msg)
            self._check_field_uniqueness(issue_type, field_values, exclude_id=issue_id)

    def _validate_dependency_edge(
        self,
        issue_id: str,
        depends_on_id: str,
        *,
        validate_existence: bool = True,
        validate_prefix: bool = True,
    ) -> None:
        if not isinstance(issue_id, str) or not isinstance(depends_on_id, str):
            msg = "dependency issue IDs must be strings"
            raise TypeError(msg)
        if validate_prefix:
            self._check_id_prefix(issue_id)
            self._check_id_prefix(depends_on_id)
        if validate_existence:
            self.get_issue(issue_id)
            self.get_issue(depends_on_id)
        if issue_id == depends_on_id:
            msg = f"Cannot add self-dependency: {issue_id}"
            raise ValueError(msg)
        if self._would_create_cycle(issue_id, depends_on_id):
            msg = f"Dependency {issue_id} -> {depends_on_id} would create a cycle"
            raise ValueError(msg)

    def bulk_insert_issue(self, issue_data: dict[str, Any], *, validate: bool = True) -> bool:
        """Insert a pre-formed issue dict directly. For migration use only.

        Returns True if the row was inserted, False if skipped (duplicate).
        """
        fields = issue_data.get("fields", {})
        if validate:
            self._validate_bulk_issue_data(issue_data)
            if fields is None:
                fields = {}
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO issues "
            "(id, title, status, priority, type, parent_id, assignee, "
            "created_at, updated_at, closed_at, description, notes, fields) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                issue_data["id"],
                issue_data["title"],
                issue_data.get("status", "open"),
                issue_data.get("priority", 2),
                issue_data.get("type", "task"),
                issue_data.get("parent_id"),
                issue_data.get("assignee", ""),
                issue_data.get("created_at", _now_iso()),
                issue_data.get("updated_at", _now_iso()),
                issue_data.get("closed_at"),
                issue_data.get("description", ""),
                issue_data.get("notes", ""),
                json.dumps(fields),
            ),
        )
        inserted = cursor.rowcount > 0
        if not inserted:
            logger.debug("bulk_insert_issue: skipped duplicate id=%s", issue_data.get("id"))
        return inserted

    def bulk_insert_dependency(self, issue_id: str, depends_on_id: str, dep_type: str = "blocks", *, validate: bool = True) -> bool:
        """Insert a dependency. Returns True if inserted, False if skipped (duplicate)."""
        if validate:
            self._validate_dependency_edge(issue_id, depends_on_id)
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO dependencies (issue_id, depends_on_id, type, created_at) VALUES (?, ?, ?, ?)",
            (issue_id, depends_on_id, dep_type, _now_iso()),
        )
        inserted = cursor.rowcount > 0
        if not inserted:
            logger.debug("bulk_insert_dependency: skipped duplicate %s -> %s", issue_id, depends_on_id)
        return inserted

    def bulk_insert_event(self, event_data: dict[str, Any]) -> bool:
        """Insert an event. Returns True if inserted, False if skipped (duplicate).

        v16: ``event_seq`` is forwarded from ``event_data`` when present so
        JSONL round-trips preserve per-issue sequence numbers; legacy
        callers that omit it default to 0 (matching the migration's
        backfill of pre-v16 rows). ``INSERT OR IGNORE`` is retained on
        purpose for this path — imports re-applied against the same DB
        should be idempotent on the full composite key, not raise.
        """
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO events (issue_id, event_type, actor, old_value, new_value, comment, created_at, event_seq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_data["issue_id"],
                event_data["event_type"],
                event_data.get("actor", ""),
                event_data.get("old_value"),
                event_data.get("new_value"),
                event_data.get("comment", ""),
                event_data.get("created_at", _now_iso()),
                int(event_data.get("event_seq", 0)),
            ),
        )
        inserted = cursor.rowcount > 0
        if not inserted:
            logger.debug("bulk_insert_event: skipped duplicate for issue=%s", event_data.get("issue_id"))
        return inserted

    def bulk_commit(self) -> None:
        self.conn.commit()

    # -- Export / Import (JSONL) -----------------------------------------------

    @staticmethod
    def _issue_fields_json(fields: Any) -> str:
        if isinstance(fields, str):
            # Validate that string is actually valid JSON before passing through
            try:
                obj = json.loads(fields)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Invalid JSON fields string, replacing with empty: %r", fields[:200] if fields else fields)
                return "{}"
            # Ensure the decoded value is a dict (not a bare string/list/number)
            if not isinstance(obj, dict):
                logger.warning("Fields JSON is not a dict, replacing with empty: %r", fields[:80])
                return "{}"
            return fields
        return json.dumps(fields or {})

    @classmethod
    def _is_future_release_record(cls, record: dict[str, Any]) -> bool:
        if record.get("type") != "release":
            return False
        try:
            fields = json.loads(cls._issue_fields_json(record.get("fields", "{}")))
        except (TypeError, json.JSONDecodeError):
            return False
        return isinstance(fields, dict) and fields.get("version") == "Future"

    def _is_reconcilable_seeded_future(self, issue_id: str) -> bool:
        row = self.conn.execute(
            "SELECT id, title, assignee, description, notes, fields FROM issues WHERE id = ?",
            (issue_id,),
        ).fetchone()
        if row is None:
            return False
        if row["title"] != "Future" or row["assignee"] != "" or row["description"] != "" or row["notes"] != "":
            return False
        try:
            fields = json.loads(row["fields"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(fields, dict) or fields.get("version") != "Future":
            return False

        blockers = [
            ("SELECT 1 FROM issues WHERE parent_id = ? LIMIT 1", (issue_id,)),
            ("SELECT 1 FROM dependencies WHERE issue_id = ? OR depends_on_id = ? LIMIT 1", (issue_id, issue_id)),
            ("SELECT 1 FROM labels WHERE issue_id = ? LIMIT 1", (issue_id,)),
            ("SELECT 1 FROM comments WHERE issue_id = ? LIMIT 1", (issue_id,)),
            ("SELECT 1 FROM events WHERE issue_id = ? LIMIT 1", (issue_id,)),
            ("SELECT 1 FROM file_associations WHERE issue_id = ? LIMIT 1", (issue_id,)),
            ("SELECT 1 FROM scan_findings WHERE issue_id = ? LIMIT 1", (issue_id,)),
            ("SELECT 1 FROM annotation_links WHERE target_type = 'issue' AND target_id = ? LIMIT 1", (issue_id,)),
            (
                "SELECT 1 FROM annotation_closeout_acknowledgements "
                "WHERE target_type = 'issue' AND (target_id = ? OR carried_to_target_id = ?) LIMIT 1",
                (issue_id, issue_id),
            ),
        ]
        return not any(self.conn.execute(query, params).fetchone() is not None for query, params in blockers)

    def _reconcile_future_release_import(self, issues: list[dict[str, Any]]) -> None:
        imported_future_ids = [record["id"] for record in issues if self._is_future_release_record(record)]
        if not imported_future_ids:
            return
        if len(imported_future_ids) > 1:
            msg = "Import file contains multiple Future release issues"
            raise ValueError(msg)

        imported_id = imported_future_ids[0]
        existing_ids = [
            row["id"]
            for row in self.conn.execute(
                "SELECT id FROM issues WHERE type = 'release' AND json_valid(fields) AND json_extract(fields, '$.version') = 'Future'"
            ).fetchall()
        ]
        conflicting_ids = [issue_id for issue_id in existing_ids if issue_id != imported_id]
        if not conflicting_ids:
            return
        if len(conflicting_ids) == 1 and self._is_reconcilable_seeded_future(conflicting_ids[0]):
            self.conn.execute("DELETE FROM issues WHERE id = ?", (conflicting_ids[0],))
            return

        msg = "Cannot import Future release: tracker already contains a different Future release"
        raise ValueError(msg)

    @staticmethod
    def _json_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value or {})

    @staticmethod
    def _json_list_text(value: Any) -> str:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return "[]"
            return value if isinstance(parsed, list) else "[]"
        if isinstance(value, list):
            return json.dumps(value)
        return "[]"

    def _resolve_imported_file_id(
        self,
        record: dict[str, Any],
        *,
        merge: bool,
        conflict: str,
        file_id_map: dict[str, str],
    ) -> int:
        src_id = record["id"]
        path = _normalize_project_relative_scan_path(
            record["path"],
            field_name=f"file_record {src_id!r} path",
        )
        registry_backend = record.get("registry_backend", "local") or "local"
        if not isinstance(registry_backend, str) or registry_backend not in _VALID_FILE_REGISTRY_BACKENDS:
            msg = f"Invalid registry_backend {registry_backend!r} for imported file_record {src_id!r}"
            raise ValueError(msg)
        content_hash = record.get("content_hash", "") or ""
        if not isinstance(content_hash, str):
            msg = f"Invalid content_hash for imported file_record {src_id!r}: expected string"
            raise ValueError(msg)
        existing_by_id = self.conn.execute("SELECT id, path FROM file_records WHERE id = ?", (src_id,)).fetchone()
        existing_by_path = self.conn.execute("SELECT id, path FROM file_records WHERE path = ?", (path,)).fetchone()

        if existing_by_id is not None and existing_by_id["path"] != path:
            msg = f"Import conflict for file id {src_id}: existing path {existing_by_id['path']!r} != imported path {path!r}"
            raise ValueError(msg)

        if existing_by_path is not None:
            if merge:
                file_id_map[src_id] = existing_by_path["id"]
                return 0
            msg = f"Import conflict for file {path!r}"
            raise sqlite3.IntegrityError(msg)

        if existing_by_id is not None:
            if merge:
                file_id_map[src_id] = existing_by_id["id"]
                return 0
            msg = f"Import conflict for file id {src_id!r}"
            raise sqlite3.IntegrityError(msg)

        cursor = self.conn.execute(
            f"INSERT {conflict} INTO file_records "
            "(id, path, language, file_type, content_hash, registry_backend, first_seen, updated_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                src_id,
                path,
                record.get("language", ""),
                record.get("file_type", ""),
                content_hash,
                registry_backend,
                _normalize_iso_to_utc(record.get("first_seen")) or _now_iso(),
                _normalize_iso_to_utc(record.get("updated_at")) or _now_iso(),
                self._json_text(record.get("metadata", {})),
            ),
        )
        if cursor.rowcount > 0:
            file_id_map[src_id] = src_id
            return cursor.rowcount

        if not merge:
            msg = f"Import conflict for file {path!r}"
            raise sqlite3.IntegrityError(msg)

        if existing_by_path is not None:
            file_id_map[src_id] = existing_by_path["id"]
            return 0

        if existing_by_id is not None:
            file_id_map[src_id] = existing_by_id["id"]
            return 0

        msg = f"Could not reconcile imported file record for path {path!r}"
        raise sqlite3.IntegrityError(msg)

    @staticmethod
    def _remap_file_id(source_file_id: str, file_id_map: dict[str, str]) -> str:
        try:
            return file_id_map[source_file_id]
        except KeyError as exc:
            msg = f"Import references unknown file_id {source_file_id!r}"
            raise ValueError(msg) from exc

    def _remap_file_id_list_text(self, value: Any, file_id_map: dict[str, str]) -> str:
        file_ids = json.loads(self._json_list_text(value))
        remapped = [file_id_map.get(file_id, file_id) if isinstance(file_id, str) else file_id for file_id in file_ids]
        return json.dumps(remapped)

    def _resolve_imported_scan_finding_id(self, record: dict[str, Any], *, file_id: str) -> str:
        rec_id = record["id"]
        existing = self.conn.execute("SELECT id FROM scan_findings WHERE id = ?", (rec_id,)).fetchone()
        if existing is not None:
            return str(existing["id"])
        existing = self.conn.execute(
            "SELECT id FROM scan_findings "
            "WHERE file_id = ? AND scan_source = ? AND rule_id = ? "
            "AND COALESCE(line_start, -1) = COALESCE(?, -1)",
            (file_id, record.get("scan_source", ""), record.get("rule_id", ""), record.get("line_start")),
        ).fetchone()
        if existing is not None:
            return str(existing["id"])
        msg = f"Could not reconcile imported scan_finding {rec_id!r}"
        raise sqlite3.IntegrityError(msg)

    def _validate_imported_closeout_acknowledgement(self, *, index: int, record: dict[str, Any]) -> None:
        target_type = record.get("target_type", "issue")
        if target_type != "issue":
            msg = f"closeout acknowledgement record #{index} has invalid target_type {target_type!r}"
            raise ValueError(msg)

        target_id = record["target_id"]
        if self.conn.execute("SELECT 1 FROM issues WHERE id = ?", (target_id,)).fetchone() is None:
            msg = f"closeout acknowledgement record #{index} has missing issue target: {target_id}"
            raise ValueError(msg)

        # ``carried_to_target_id`` is a provenance breadcrumb, not a live ref: the
        # only live-operation reader (get_annotation_closeout_warnings' lookup)
        # tolerates a non-match, and delete_issue intentionally leaves it dangling
        # when the carry destination is deleted (same rationale as
        # observations.source_issue_id). Requiring it to reference a live issue
        # here made a normal delete-then-export/import round-trip raise. Project
        # isolation is still enforced by the prefix check in
        # _assert_import_ids_match_prefix; only the existence requirement is dropped.

        source_link = self.conn.execute(
            """
            SELECT 1
            FROM annotation_links
            WHERE annotation_id = ?
              AND target_type = 'issue'
              AND target_id = ?
              AND relationship = 'must_consider'
            LIMIT 1
            """,
            (record["annotation_id"], target_id),
        ).fetchone()
        if source_link is None:
            msg = f"closeout acknowledgement record #{index} has no must_consider issue link for target {target_id}"
            raise ValueError(msg)

    def _validate_imported_annotation_link_target(
        self,
        *,
        index: int,
        target_type: str,
        target_id: str,
    ) -> None:
        if target_type == "issue":
            table = "issues"
            description = "issue target"
        elif target_type == "file":
            table = "file_records"
            description = "file target"
        elif target_type == "finding":
            table = "scan_findings"
            description = "finding target"
        elif target_type == "observation":
            table = "observations"
            description = "observation target"
        else:
            msg = f"annotation_link record #{index} has invalid target_type {target_type!r}"
            raise ValueError(msg)
        row = self.conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (target_id,)).fetchone()
        if row is None:
            msg = f"annotation_link record #{index} has missing {description}: {target_id}"
            raise ValueError(msg)

    # Table export definitions: (record_type_tag, SQL query)
    _EXPORT_TABLES: ClassVar[list[tuple[str, str]]] = [
        ("issue", "SELECT * FROM issues ORDER BY created_at"),
        ("file_record", "SELECT * FROM file_records ORDER BY path"),
        ("scan_run", "SELECT * FROM scan_runs ORDER BY started_at, id"),
        ("scan_finding", "SELECT * FROM scan_findings ORDER BY first_seen, file_id, scan_source, rule_id"),
        ("dependency", "SELECT * FROM dependencies ORDER BY issue_id"),
        ("label", "SELECT * FROM labels ORDER BY issue_id"),
        ("comment", "SELECT * FROM comments ORDER BY created_at"),
        ("event", "SELECT * FROM events ORDER BY created_at"),
        ("file_association", "SELECT * FROM file_associations ORDER BY created_at, file_id, issue_id"),
        ("entity_association", "SELECT * FROM entity_associations ORDER BY attached_at, issue_id, clarion_entity_id"),
        ("file_event", "SELECT * FROM file_events ORDER BY created_at, file_id"),
        ("observation", "SELECT * FROM observations ORDER BY created_at"),
        ("dismissed_observation", "SELECT * FROM dismissed_observations ORDER BY dismissed_at"),
        ("observation_link", "SELECT * FROM observation_links ORDER BY linked_at, id"),
        ("annotation", "SELECT * FROM annotations ORDER BY created_at, id"),
        ("annotation_provenance", "SELECT * FROM annotation_provenance ORDER BY annotation_id"),
        ("annotation_link", "SELECT * FROM annotation_links ORDER BY created_at, id"),
        ("annotation_event", "SELECT * FROM annotation_events ORDER BY created_at, id"),
        (
            "annotation_closeout_acknowledgement",
            "SELECT * FROM annotation_closeout_acknowledgements ORDER BY acknowledged_at, id",
        ),
    ]

    def export_jsonl(self, output_path: str | Path) -> int:
        """Export full project data to JSONL.

        Each line is a JSON object with a "_type" field indicating the record type.
        Returns the total number of records written.
        """
        count = 0
        with Path(output_path).open("w") as f:
            for type_tag, query in self._EXPORT_TABLES:
                for row in self.conn.execute(query).fetchall():
                    record = dict(row)
                    record["_type"] = type_tag
                    f.write(json.dumps(record, default=str) + "\n")
                    count += 1
        return count

    def _assert_import_ids_match_prefix(
        self,
        *,
        issues: list[dict[str, Any]],
        dependencies: list[dict[str, Any]],
        labels: list[dict[str, Any]],
        comments: list[dict[str, Any]],
        events: list[dict[str, Any]],
        file_associations: list[dict[str, Any]],
        entity_associations: list[dict[str, Any]],
        scan_findings: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        observation_links: list[dict[str, Any]],
        annotation_links: list[dict[str, Any]],
        annotation_events: list[dict[str, Any]],
        annotation_closeout_acknowledgements: list[dict[str, Any]],
    ) -> None:
        """Raise WrongProjectError if any imported record references an
        issue ID whose prefix doesn't match this DB.

        Rationale: ``import_jsonl`` inserts rows by ``INSERT OR (IGNORE|ABORT)``
        directly into SQLite, bypassing the per-method ``_check_id_prefix``
        guard. Without this preflight, a cross-prefix JSONL would silently
        install rows that every subsequent mutation path rejects.
        """
        from filigree.core import WrongProjectError

        foreign: set[str] = set()

        def check(issue_id: Any) -> None:
            if not isinstance(issue_id, str) or not issue_id:
                return
            try:
                self._check_id_prefix(issue_id)
            except WrongProjectError:
                foreign.add(issue_id)

        for rec in issues:
            check(rec.get("id"))
            check(rec.get("parent_id"))
        for rec in dependencies:
            check(rec.get("issue_id"))
            check(rec.get("depends_on_id"))
        for rec in labels:
            check(rec.get("issue_id"))
        for rec in comments:
            check(rec.get("issue_id"))
        for rec in events:
            check(rec.get("issue_id"))
        for rec in file_associations:
            check(rec.get("issue_id"))
        for rec in entity_associations:
            check(rec.get("issue_id"))
        for rec in scan_findings:
            check(rec.get("issue_id"))
        for rec in observations:
            check(rec.get("source_issue_id"))
        for rec in observation_links:
            check(rec.get("issue_id"))
            check(rec.get("source_issue_id"))
        for rec in annotation_links:
            if rec.get("target_type") == "issue":
                check(rec.get("target_id"))
        for rec in annotation_events:
            if rec.get("target_type") == "issue":
                check(rec.get("target_id"))
        for rec in annotation_closeout_acknowledgements:
            if rec.get("target_type", "issue") == "issue":
                check(rec.get("target_id"))
                check(rec.get("carried_to_target_id"))

        if not foreign:
            return
        sample = sorted(foreign)[:5]
        extra = f" (+{len(foreign) - len(sample)} more)" if len(foreign) > len(sample) else ""
        msg = (
            f"import_jsonl: {len(foreign)} record(s) reference issue ID(s) "
            f"from a foreign project — this DB's prefix is {self.prefix!r}. "
            f"Examples: {sample}{extra}. Re-export the source data or pass "
            f"allow_foreign_ids=True to keep the original IDs (which will "
            f"then be readable but not mutable through the prefix-guarded "
            f"write methods)."
        )
        raise WrongProjectError(msg)

    def import_jsonl(
        self,
        input_path: str | Path,
        *,
        merge: bool = False,
        allow_foreign_ids: bool = False,
    ) -> dict[str, Any]:
        """Import full project data from a JSONL file.

        Args:
            input_path: Path to JSONL file
            merge: If True, skip existing records (OR IGNORE). If False, raise on conflict.
            allow_foreign_ids: If True, permit imported issue IDs whose prefix
                does not match this DB's prefix. The default (False) rejects
                cross-prefix imports fast, because later write paths enforce
                the prefix guard and the imported rows would otherwise be
                readable-but-unwritable. Migration tools that deliberately
                preserve source IDs may opt in.

        Returns dict with ``count`` (records inserted) and ``skipped_types``
        (mapping of unknown _type values to their occurrence counts, empty if none).

        Raises:
            WrongProjectError: if *allow_foreign_ids* is False and any
                imported record carries an issue ID that does not belong to
                this project. No rows are inserted before the check.
        """
        count = 0
        skipped_types: dict[str, int] = {}
        conflict = "OR IGNORE" if merge else "OR ABORT"
        issues: list[dict[str, Any]] = []
        file_records: list[dict[str, Any]] = []
        scan_runs: list[dict[str, Any]] = []
        scan_findings: list[dict[str, Any]] = []
        dependencies: list[dict[str, Any]] = []
        labels: list[dict[str, Any]] = []
        comments: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        file_associations: list[dict[str, Any]] = []
        entity_associations: list[dict[str, Any]] = []
        file_events: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        dismissed_observations: list[dict[str, Any]] = []
        observation_links: list[dict[str, Any]] = []
        annotations: list[dict[str, Any]] = []
        annotation_provenance: list[dict[str, Any]] = []
        annotation_links: list[dict[str, Any]] = []
        annotation_events: list[dict[str, Any]] = []
        annotation_closeout_acknowledgements: list[dict[str, Any]] = []

        buckets: dict[str, list[dict[str, Any]]] = {
            "issue": issues,
            "file_record": file_records,
            "scan_run": scan_runs,
            "scan_finding": scan_findings,
            "dependency": dependencies,
            "label": labels,
            "comment": comments,
            "event": events,
            "file_association": file_associations,
            "entity_association": entity_associations,
            "file_event": file_events,
            "observation": observations,
            "dismissed_observation": dismissed_observations,
            "observation_link": observation_links,
            "annotation": annotations,
            "annotation_provenance": annotation_provenance,
            "annotation_link": annotation_links,
            "annotation_event": annotation_events,
            "annotation_closeout_acknowledgement": annotation_closeout_acknowledgements,
        }

        with Path(input_path).open() as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("import_jsonl: corrupt JSON at line %d: %r", line_num, line[:200])
                    skipped_types["<corrupt_json>"] = skipped_types.get("<corrupt_json>", 0) + 1
                    continue
                record_type = record.pop("_type", None)

                bucket = buckets.get(record_type)
                if bucket is not None:
                    bucket.append(record)
                else:
                    key = record_type or "<missing>"
                    skipped_types[key] = skipped_types.get(key, 0) + 1

        if not allow_foreign_ids:
            self._assert_import_ids_match_prefix(
                issues=issues,
                dependencies=dependencies,
                labels=labels,
                comments=comments,
                events=events,
                file_associations=file_associations,
                entity_associations=entity_associations,
                scan_findings=scan_findings,
                observations=observations,
                observation_links=observation_links,
                annotation_links=annotation_links,
                annotation_events=annotation_events,
                annotation_closeout_acknowledgements=annotation_closeout_acknowledgements,
            )

        inserted_issue_ids: set[str] = set()
        parent_map: dict[str, str] = {}
        file_id_map: dict[str, str] = {}
        finding_id_map: dict[str, str] = {}
        _import_stage = "setup"
        _import_index = 0
        try:
            self._reconcile_future_release_import(issues)
            _import_stage = "issue"

            for _import_index, record in enumerate(issues):
                parent_id = record.get("parent_id")
                fields = self._issue_fields_json(record.get("fields", "{}"))
                issue_data = {**record, "fields": json.loads(fields)}
                self._validate_bulk_issue_data(
                    issue_data,
                    validate_parent_exists=False,
                    validate_prefix=not allow_foreign_ids,
                )
                # Normalize timestamps at the import boundary so SQLite TEXT
                # comparisons (used by archive_closed for closed_at, etc.) work
                # chronologically regardless of the source's offset. Internal
                # write paths already emit canonical UTC. (filigree-20911dfe6d)
                cursor = self.conn.execute(
                    f"INSERT {conflict} INTO issues "
                    "(id, title, status, priority, type, parent_id, assignee, "
                    "created_at, updated_at, closed_at, description, notes, fields) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record["id"],
                        record["title"],
                        record.get("status", "open"),
                        record.get("priority", 2),
                        record.get("type", "task"),
                        None,
                        record.get("assignee", ""),
                        _normalize_iso_to_utc(record.get("created_at")) or _now_iso(),
                        _normalize_iso_to_utc(record.get("updated_at")) or _now_iso(),
                        _normalize_iso_to_utc(record.get("closed_at")),
                        record.get("description", ""),
                        record.get("notes", ""),
                        fields,
                    ),
                )
                count += cursor.rowcount
                if cursor.rowcount > 0:
                    inserted_issue_ids.add(record["id"])
                    if parent_id:
                        parent_map[record["id"]] = parent_id

            for issue_id, parent_id in parent_map.items():
                if issue_id in inserted_issue_ids:
                    # Validate parent_id references an existing issue (in DB or just imported)
                    if parent_id not in inserted_issue_ids:
                        exists = self.conn.execute("SELECT 1 FROM issues WHERE id = ?", (parent_id,)).fetchone()
                        if not exists:
                            msg = f"import_jsonl: parent_id {parent_id!r} for issue {issue_id!r} references non-existent issue"
                            raise ValueError(msg)
                    if parent_id == issue_id:
                        msg = f"import_jsonl: issue {issue_id!r} cannot be its own parent"
                        raise ValueError(msg)
                    if self._would_create_parent_cycle(issue_id, parent_id):
                        msg = f"import_jsonl: parent_id {parent_id!r} for issue {issue_id!r} would create a circular parent chain"
                        raise ValueError(msg)
                    self.conn.execute("UPDATE issues SET parent_id = ? WHERE id = ?", (parent_id, issue_id))

            _import_stage = "file_record"
            for _import_index, record in enumerate(file_records):
                count += self._resolve_imported_file_id(record, merge=merge, conflict=conflict, file_id_map=file_id_map)

            _import_stage = "scan_run"
            for _import_index, record in enumerate(scan_runs):
                rec_id = record.get("id", "?")
                status = record.get("status", "pending")
                if status not in {"pending", "running", "completed", "failed", "timeout"}:
                    msg = f"Invalid scan_run status {status!r} for {rec_id!r}"
                    raise ValueError(msg)
                cursor = self.conn.execute(
                    f"INSERT {conflict} INTO scan_runs "
                    "(id, scanner_name, scan_source, status, file_paths, file_ids, "
                    "pid, api_url, log_path, started_at, updated_at, completed_at, "
                    "exit_code, findings_count, error_message) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record["id"],
                        record["scanner_name"],
                        record.get("scan_source", ""),
                        status,
                        record.get("file_paths", "[]"),
                        self._remap_file_id_list_text(record.get("file_ids", "[]"), file_id_map),
                        record.get("pid"),
                        record.get("api_url", ""),
                        record.get("log_path", ""),
                        _normalize_iso_to_utc(record.get("started_at")) or _now_iso(),
                        _normalize_iso_to_utc(record.get("updated_at")) or _now_iso(),
                        _normalize_iso_to_utc(record.get("completed_at")),
                        record.get("exit_code"),
                        record.get("findings_count", 0),
                        record.get("error_message", ""),
                    ),
                )
                count += cursor.rowcount

            _import_stage = "scan_finding"
            for _import_index, record in enumerate(scan_findings):
                file_id = self._remap_file_id(record["file_id"], file_id_map)
                severity = record.get("severity", "info")
                finding_status = record.get("status", "open")
                rec_id = record.get("id", "?")
                if severity not in VALID_SEVERITIES:
                    msg = f"Invalid severity {severity!r} in scan_finding {rec_id}, expected one of {sorted(VALID_SEVERITIES)}"
                    raise ValueError(msg)
                if finding_status not in VALID_FINDING_STATUSES:
                    valid = sorted(VALID_FINDING_STATUSES)
                    msg = f"Invalid finding status {finding_status!r} in scan_finding {rec_id}, expected one of {valid}"
                    raise ValueError(msg)
                cursor = self.conn.execute(
                    f"INSERT {conflict} INTO scan_findings "
                    "(id, file_id, issue_id, scan_source, rule_id, severity, status, message, suggestion, scan_run_id, "
                    "line_start, line_end, seen_count, first_seen, updated_at, last_seen_at, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record["id"],
                        file_id,
                        record.get("issue_id"),
                        record.get("scan_source", ""),
                        record.get("rule_id", ""),
                        severity,
                        finding_status,
                        record.get("message", ""),
                        record.get("suggestion", ""),
                        record.get("scan_run_id", ""),
                        record.get("line_start"),
                        record.get("line_end"),
                        record.get("seen_count", 1),
                        _normalize_iso_to_utc(record.get("first_seen")) or _now_iso(),
                        _normalize_iso_to_utc(record.get("updated_at")) or _now_iso(),
                        _normalize_iso_to_utc(record.get("last_seen_at")),
                        self._json_text(record.get("metadata", {})),
                    ),
                )
                count += cursor.rowcount
                if cursor.rowcount > 0:
                    finding_id_map[record["id"]] = record["id"]
                elif merge:
                    finding_id_map[record["id"]] = self._resolve_imported_scan_finding_id(record, file_id=file_id)

            _import_stage = "dependency"
            for _import_index, record in enumerate(dependencies):
                self._validate_dependency_edge(
                    record["issue_id"],
                    record["depends_on_id"],
                    validate_existence=False,
                    validate_prefix=not allow_foreign_ids,
                )
                cursor = self.conn.execute(
                    f"INSERT {conflict} INTO dependencies (issue_id, depends_on_id, type, created_at) VALUES (?, ?, ?, ?)",
                    (
                        record["issue_id"],
                        record["depends_on_id"],
                        record.get("type", "blocks"),
                        _normalize_iso_to_utc(record.get("created_at")) or _now_iso(),
                    ),
                )
                count += cursor.rowcount

            _import_stage = "label"
            for _import_index, record in enumerate(labels):
                raw_label = record["label"]
                try:
                    validated = self._validate_label_name(raw_label)
                except ValueError as exc:
                    # Mirrors normal-write enforcement: reserved auto/virtual
                    # namespaces (age:, has:) and reserved type names cannot be
                    # imported as physical rows — they would shadow computed
                    # virtual namespaces in list_labels(). Skip and account.
                    logger.warning(
                        "import_jsonl: skipping invalid label %r for %s: %s",
                        raw_label,
                        record.get("issue_id", "<missing>"),
                        exc,
                    )
                    skipped_types["<invalid_label>"] = skipped_types.get("<invalid_label>", 0) + 1
                    continue
                cursor = self.conn.execute(
                    f"INSERT {conflict} INTO labels (issue_id, label) VALUES (?, ?)",
                    (record["issue_id"], validated),
                )
                count += cursor.rowcount

            _import_stage = "comment"
            for _import_index, record in enumerate(comments):
                if merge:
                    created = _normalize_iso_to_utc(record.get("created_at")) or _now_iso()
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
                else:
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
                count += cursor.rowcount

            _import_stage = "event"
            for _import_index, record in enumerate(events):
                # events.created_at is the column read by get_events_since,
                # which compares lexicographically. Normalize on import so
                # rows from sources with non-UTC offsets sort correctly.
                # (filigree-20911dfe6d)
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
                count += cursor.rowcount

            _import_stage = "file_association"
            for _import_index, record in enumerate(file_associations):
                file_id = self._remap_file_id(record["file_id"], file_id_map)
                cursor = self.conn.execute(
                    f"INSERT {conflict} INTO file_associations (file_id, issue_id, assoc_type, created_at) VALUES (?, ?, ?, ?)",
                    (
                        file_id,
                        record["issue_id"],
                        record.get("assoc_type", "bug_in"),
                        _normalize_iso_to_utc(record.get("created_at")) or _now_iso(),
                    ),
                )
                count += cursor.rowcount

            _import_stage = "entity_association"
            for _import_index, record in enumerate(entity_associations):
                cursor = self.conn.execute(
                    f"INSERT {conflict} INTO entity_associations "
                    "(issue_id, clarion_entity_id, content_hash_at_attach, attached_at, attached_by, migration_orphaned_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        record["issue_id"],
                        record["clarion_entity_id"],
                        record["content_hash_at_attach"],
                        _normalize_iso_to_utc(record.get("attached_at")) or _now_iso(),
                        record.get("attached_by", ""),
                        # SEI-backfill ORPHAN marker (v22). NULL = healthy; preserved
                        # verbatim across a backup/restore so an unreviewed orphan
                        # survives. Absent in pre-v22 exports → NULL.
                        record.get("migration_orphaned_at"),
                    ),
                )
                count += cursor.rowcount

            _import_stage = "file_event"
            for _import_index, record in enumerate(file_events):
                file_id = self._remap_file_id(record["file_id"], file_id_map)
                if merge:
                    created = _normalize_iso_to_utc(record.get("created_at")) or _now_iso()
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
                else:
                    cursor = self.conn.execute(
                        "INSERT INTO file_events "
                        "(file_id, event_type, field, old_value, new_value, verified_actor, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
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
                count += cursor.rowcount

            _import_stage = "observation"
            for _import_index, record in enumerate(observations):
                obs_file_id: str | None = record.get("file_id")
                if obs_file_id and obs_file_id in file_id_map:
                    obs_file_id = file_id_map[obs_file_id]
                source_finding_id = record.get("source_finding_id", "")
                if source_finding_id in finding_id_map:
                    source_finding_id = finding_id_map[source_finding_id]
                # Default expires_at to _expires_iso() (now + TTL) when missing.
                # _now_iso() would make every imported observation already expired
                # and swept on the next read.
                cursor = self.conn.execute(
                    f"INSERT {conflict} INTO observations "
                    "(id, summary, detail, file_id, file_path, line, source_issue_id, "
                    "source_finding_id, priority, actor, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record["id"],
                        record["summary"],
                        record.get("detail", ""),
                        obs_file_id,
                        record.get("file_path", ""),
                        record.get("line"),
                        record.get("source_issue_id", ""),
                        source_finding_id,
                        record.get("priority", 3),
                        record.get("actor", ""),
                        _normalize_iso_to_utc(record.get("created_at")) or _now_iso(),
                        _normalize_iso_to_utc(record.get("expires_at")) or _expires_iso(),
                    ),
                )
                count += cursor.rowcount

            _import_stage = "dismissed_observation"
            for _import_index, record in enumerate(dismissed_observations):
                obs_id = record["obs_id"]
                summary = record["summary"]
                actor_val = record.get("actor", "")
                reason = record.get("reason", "")
                dismissed_at = _normalize_iso_to_utc(record.get("dismissed_at")) or _now_iso()
                # dismissed_observations has no unique content constraint (only
                # an auto-increment PK), so OR IGNORE won't deduplicate on
                # content.  In merge mode, skip rows that already exist.
                if merge:
                    exists = self.conn.execute(
                        "SELECT 1 FROM dismissed_observations WHERE obs_id = ? AND summary = ? AND dismissed_at = ?",
                        (obs_id, summary, dismissed_at),
                    ).fetchone()
                    if exists:
                        continue
                cursor = self.conn.execute(
                    f"INSERT {conflict} INTO dismissed_observations (obs_id, summary, actor, reason, dismissed_at) VALUES (?, ?, ?, ?, ?)",
                    (obs_id, summary, actor_val, reason, dismissed_at),
                )
                count += cursor.rowcount

            _import_stage = "observation_link"
            for _import_index, record in enumerate(observation_links):
                link_file_id: str | None = record.get("file_id")
                if link_file_id:
                    link_file_id = self._remap_file_id(link_file_id, file_id_map)
                source_finding_id = record.get("source_finding_id", "")
                if source_finding_id in finding_id_map:
                    source_finding_id = finding_id_map[source_finding_id]
                if merge:
                    linked_at = _normalize_iso_to_utc(record.get("linked_at")) or _now_iso()
                    exists = self.conn.execute(
                        "SELECT 1 FROM observation_links WHERE obs_id = ? AND issue_id = ? AND disposition = ?",
                        (record["obs_id"], record["issue_id"], record.get("disposition", "evidence")),
                    ).fetchone()
                    if exists:
                        continue
                else:
                    linked_at = _normalize_iso_to_utc(record.get("linked_at")) or _now_iso()
                cursor = self.conn.execute(
                    f"INSERT {conflict} INTO observation_links "
                    "(obs_id, issue_id, disposition, summary, detail, file_id, file_path, line, "
                    "source_issue_id, source_finding_id, priority, observation_actor, actor, reason, linked_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record["obs_id"],
                        record["issue_id"],
                        record.get("disposition", "evidence"),
                        record.get("summary", ""),
                        record.get("detail", ""),
                        link_file_id,
                        record.get("file_path", ""),
                        record.get("line"),
                        record.get("source_issue_id", ""),
                        source_finding_id,
                        record.get("priority", 3),
                        record.get("observation_actor", ""),
                        record.get("actor", ""),
                        record.get("reason", ""),
                        linked_at,
                    ),
                )
                count += cursor.rowcount

            _import_stage = "annotation"
            for _import_index, record in enumerate(annotations):
                ann_file_id: str | None = record.get("file_id")
                if ann_file_id:
                    ann_file_id = self._remap_file_id(ann_file_id, file_id_map)
                cursor = self.conn.execute(
                    f"INSERT {conflict} INTO annotations "
                    "(id, file_id, file_path, line_start, line_end, anchor_snippet, note, context_summary, "
                    "intent, critical, status, actor, session_ref, created_at, updated_at, resolved_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record["id"],
                        ann_file_id,
                        record["file_path"],
                        record.get("line_start"),
                        record.get("line_end"),
                        record.get("anchor_snippet", ""),
                        record["note"],
                        record.get("context_summary", ""),
                        record.get("intent", "breadcrumb"),
                        int(bool(record.get("critical", 0))),
                        record.get("status", "active"),
                        record.get("actor", ""),
                        record.get("session_ref", ""),
                        _normalize_iso_to_utc(record.get("created_at")) or _now_iso(),
                        _normalize_iso_to_utc(record.get("updated_at")) or _now_iso(),
                        _normalize_iso_to_utc(record.get("resolved_at")),
                    ),
                )
                count += cursor.rowcount

            _import_stage = "annotation_provenance"
            for _import_index, record in enumerate(annotation_provenance):
                cursor = self.conn.execute(
                    f"INSERT {conflict} INTO annotation_provenance "
                    "(annotation_id, commit_ref, branch, repo_root, worktree_root, git_state, worktree_dirty, "
                    "file_checksum, file_size, file_mtime, dirty_diff_hash, dirty_diff_summary, file_diff, "
                    "worktree_diff_summary, anchor_context_before, anchor_context_after, provenance_trust_level, "
                    "provenance_flags, provenance_warnings) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record["annotation_id"],
                        record.get("commit_ref", ""),
                        record.get("branch", ""),
                        record.get("repo_root", ""),
                        record.get("worktree_root", ""),
                        record.get("git_state", ""),
                        int(bool(record.get("worktree_dirty", 0))),
                        record.get("file_checksum", ""),
                        record.get("file_size", 0),
                        record.get("file_mtime", ""),
                        record.get("dirty_diff_hash", ""),
                        record.get("dirty_diff_summary", ""),
                        record.get("file_diff", ""),
                        record.get("worktree_diff_summary", ""),
                        record.get("anchor_context_before", ""),
                        record.get("anchor_context_after", ""),
                        record.get("provenance_trust_level", "minimal"),
                        self._json_list_text(record.get("provenance_flags", [])),
                        self._json_list_text(record.get("provenance_warnings", [])),
                    ),
                )
                count += cursor.rowcount

            _import_stage = "annotation_link"
            for _import_index, record in enumerate(annotation_links):
                target_id = record["target_id"]
                if record.get("target_type") == "file":
                    target_id = self._remap_file_id(target_id, file_id_map)
                elif record.get("target_type") == "finding" and target_id in finding_id_map:
                    target_id = finding_id_map[target_id]
                self._validate_imported_annotation_link_target(
                    index=_import_index,
                    target_type=record["target_type"],
                    target_id=target_id,
                )
                cursor = self.conn.execute(
                    f"INSERT {conflict} INTO annotation_links "
                    "(id, annotation_id, target_type, target_id, relationship, actor, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        record["id"],
                        record["annotation_id"],
                        record["target_type"],
                        target_id,
                        record["relationship"],
                        record.get("actor", ""),
                        _normalize_iso_to_utc(record.get("created_at")) or _now_iso(),
                    ),
                )
                count += cursor.rowcount

            _import_stage = "annotation_event"
            for _import_index, record in enumerate(annotation_events):
                cursor = self.conn.execute(
                    f"INSERT {conflict} INTO annotation_events "
                    "(id, annotation_id, event_type, actor, reason, old_value, new_value, target_type, target_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record["id"],
                        record["annotation_id"],
                        record["event_type"],
                        record.get("actor", ""),
                        record.get("reason", ""),
                        record.get("old_value"),
                        record.get("new_value"),
                        record.get("target_type", ""),
                        record.get("target_id", ""),
                        _normalize_iso_to_utc(record.get("created_at")) or _now_iso(),
                    ),
                )
                count += cursor.rowcount

            _import_stage = "annotation_closeout_acknowledgement"
            for _import_index, record in enumerate(annotation_closeout_acknowledgements):
                self._validate_imported_closeout_acknowledgement(index=_import_index, record=record)
                cursor = self.conn.execute(
                    f"INSERT {conflict} INTO annotation_closeout_acknowledgements "
                    "(annotation_id, target_type, target_id, carried_to_target_id, actor, reason, acknowledged_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        record["annotation_id"],
                        record.get("target_type", "issue"),
                        record["target_id"],
                        record.get("carried_to_target_id", ""),
                        record.get("actor", ""),
                        record.get("reason", ""),
                        _normalize_iso_to_utc(record.get("acknowledged_at")) or _now_iso(),
                    ),
                )
                count += cursor.rowcount
        except KeyError as exc:
            self.conn.rollback()
            msg = f"Missing required field {exc} in {_import_stage} record #{_import_index}"
            raise ValueError(msg) from exc
        except Exception:
            logger.error("import_jsonl failed during stage %r record #%d", _import_stage, _import_index, exc_info=True)
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

        if skipped_types:
            for rtype, rcount in skipped_types.items():
                logger.warning("import_jsonl: skipped %d record(s) with unknown type %r", rcount, rtype)

        return {"count": count, "skipped_types": dict(skipped_types)}
