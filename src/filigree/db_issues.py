"""IssuesMixin — issue CRUD, batch operations, search, and claiming.

Extracted from core.py as part of the module architecture split.
All methods access ``self.conn``, ``self.get_issue()``, etc. via
Python's MRO when composed into ``FiligreeDB``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re as _re
import sqlite3
import uuid
from typing import TYPE_CHECKING, Any, cast

from filigree.db_base import AGE_BUCKETS, DBMixinProtocol, _escape_like, _escape_like_chars, _now_iso, _safe_json_loads
from filigree.models import Issue
from filigree.templates import TransitionResult, validate_field_pattern
from filigree.types.api import BatchFailure, ErrorCode, classify_value_error

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_REOPEN_CLEAR_FIELDS = frozenset({"close_reason"})

_LIST_ISSUE_SORT_COLUMNS = {
    "created_at": "i.created_at",
    "updated_at": "i.updated_at",
    "priority": "i.priority",
}


def _escape_like_prefix(value: str) -> str:
    """Escape LIKE wildcard characters for prefix matching (no wrapping %)."""
    return _escape_like_chars(value)


def _validate_priority_value(priority: Any) -> None:
    """Reject non-int (including bool) and out-of-range priorities before any write.

    The Issue model's ``__post_init__`` validates type and range during hydration
    (models.py: ``isinstance(self.priority, int)`` and 0..4). Without this
    pre-write guard, float priorities pass the previous range-only check and are
    INSERTed before hydration raises — leaving a row with
    ``typeof(priority) = 'real'`` durably committed. ``bool`` slips past
    ``isinstance(_, int)`` because Python's ``bool`` is an ``int`` subclass and
    ``0 <= True <= 4`` is ``0 <= 1 <= 4``, so it must be rejected explicitly first.
    """
    if isinstance(priority, bool) or not isinstance(priority, int) or not (0 <= priority <= 4):
        msg = f"Priority must be an integer between 0 and 4, got {priority!r}"
        raise ValueError(msg)


def _transition_data_warnings(result: TransitionResult) -> list[str]:
    """Return the canonical soft-transition warnings for response + audit."""
    warnings: list[str] = []
    for warning in result.warnings:
        if warning not in warnings:
            warnings.append(warning)
    if not warnings and result.enforcement == "soft" and result.missing_fields:
        warnings.append(f"Missing recommended fields: {', '.join(result.missing_fields)}")
    return warnings


def _fields_for_reopen(fields: dict[str, Any]) -> dict[str, Any]:
    """Drop stale terminal-only fields when returning an issue to live work."""
    return {key: value for key, value in fields.items() if key not in _REOPEN_CLEAR_FIELDS}


def _list_issue_order_by(sort_by: str, direction: str) -> str:
    if not isinstance(sort_by, str) or sort_by not in _LIST_ISSUE_SORT_COLUMNS:
        valid = ", ".join(sorted(_LIST_ISSUE_SORT_COLUMNS))
        raise ValueError(f"sort_by must be one of: {valid}")
    if not isinstance(direction, str) or direction.lower() not in {"asc", "desc"}:
        raise ValueError("direction must be 'asc' or 'desc'")

    order_direction = direction.upper()
    order_by = [f"{_LIST_ISSUE_SORT_COLUMNS[sort_by]} {order_direction}"]
    if sort_by != "priority":
        order_by.append("i.priority ASC")
    if sort_by != "created_at":
        order_by.append("i.created_at ASC")
    order_by.append("i.id ASC")
    return ", ".join(order_by)


def _resolve_virtual_label(
    label: str,
    *,
    negate: bool = False,
    blocker_done_predicate: tuple[str, list[Any]] | None = None,
) -> tuple[str, list[Any]] | None:
    """Resolve a virtual label to a SQL condition + params.

    Returns (sql_fragment, params) or None if not a virtual label.

    ``blocker_done_predicate`` is a ``(sql, params)`` fragment that
    matches a "done" blocker row aliased ``blocker`` (for ``has:blockers``).
    Build it at the call site via ``_category_predicate_sql("done",
    type_col="blocker.type", status_col="blocker.status",
    include_archived=True)``. When ``None``, falls back to a safe
    name-only predicate using ``blocker.status IN ('closed', 'archived')``;
    the typed form is preferred so state-name collisions across types
    (filigree-b55aa3191f) do not erroneously treat a wip blocker as done.
    """
    if label.startswith("age:"):
        value = label.split(":", 1)[1]
        bucket = AGE_BUCKETS.get(value)
        if bucket is None:
            valid = ", ".join(sorted(AGE_BUCKETS))
            raise ValueError(f"Unknown age bucket {value!r} in virtual label {label!r}. Valid values: {valid}")
        low, high = bucket
        if negate:
            return (
                "(datetime(i.created_at) > datetime('now', ?) OR datetime(i.created_at) <= datetime('now', ?))",
                [f"-{low} days", f"-{high} days"],
            )
        return (
            "datetime(i.created_at) <= datetime('now', ?) AND datetime(i.created_at) > datetime('now', ?)",
            [f"-{low} days", f"-{high} days"],
        )

    if label.startswith("has:"):
        value = label.split(":", 1)[1]
        exists_op = "NOT EXISTS" if negate else "EXISTS"
        if blocker_done_predicate is None:
            blocker_done_sql = "blocker.status IN ('closed', 'archived')"
            blocker_done_params: list[Any] = []
        else:
            blocker_done_sql, blocker_done_params = blocker_done_predicate
        subqueries: dict[str, tuple[str, list[Any]]] = {
            "blockers": (
                f"{exists_op} (SELECT 1 FROM dependencies d "
                "JOIN issues blocker ON d.depends_on_id = blocker.id "
                f"WHERE d.issue_id = i.id AND NOT ({blocker_done_sql}))",
                list(blocker_done_params),
            ),
            "children": (
                f"{exists_op} (SELECT 1 FROM issues child WHERE child.parent_id = i.id)",
                [],
            ),
            "findings": (
                f"{exists_op} (SELECT 1 FROM scan_findings sf WHERE sf.issue_id = i.id AND sf.status NOT IN ('fixed', 'false_positive'))",
                [],
            ),
            "files": (
                f"{exists_op} (SELECT 1 FROM file_associations fa WHERE fa.issue_id = i.id)",
                [],
            ),
            "comments": (
                f"{exists_op} (SELECT 1 FROM comments c WHERE c.issue_id = i.id)",
                [],
            ),
        }
        entry = subqueries.get(value)
        if entry is None:
            valid = ", ".join(sorted(subqueries))
            raise ValueError(f"Unknown has: value {value!r} in virtual label {label!r}. Valid values: {valid}")
        return entry

    return None  # Not a virtual label


def _safe_fields_json(raw: str | None, issue_id: str) -> dict[str, Any]:
    """Parse issue fields JSON.

    Returns a ``_ParsedJson`` (dict subclass). On corrupt JSON it returns
    an empty dict with ``_filigree_corrupt=True``; ``Issue.to_dict()`` reads
    that flag to derive ``data_warnings``.
    """
    return _safe_json_loads(raw, f"issue {issue_id} fields")


def _validate_string_list(value: object, name: str) -> None:
    """Raise TypeError if *value* is not a list of strings."""
    if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
        msg = f"{name} must be a list of strings"
        raise TypeError(msg)


def _normalize_assignee(value: object) -> str:
    """Strip whitespace from an assignee value; whitespace-only becomes ``""`` (unassigned).

    Enforces the storage invariant that ``assignee`` is either the empty string
    (unassigned) or a trimmed real identity — never whitespace-only. This keeps
    ``claim_issue``'s "already assigned" check (which treats any non-empty
    stored value as owned) from misfiring on a blank that looks empty.
    """
    if not isinstance(value, str):
        msg = "assignee must be a string"
        raise TypeError(msg)
    return value.strip()


def _sanitize_fts_query(query: str) -> str:
    """Sanitize a search query for FTS5 MATCH syntax.

    Strips non-alphanumeric chars (keeping ``*`` and ``"``), quotes each token,
    and joins with AND for prefix matching.
    """
    sanitized = _re.sub(r'[^\w\s*"]', "", query)
    tokens = [t.replace('"', "") for t in sanitized.strip().split()]
    tokens = [t for t in tokens if t]
    return " AND ".join(f'"{t}"*' for t in tokens) if tokens else ""


class IssuesMixin(DBMixinProtocol):
    """Issue CRUD, batch operations, search, and claiming.

    Inherits ``DBMixinProtocol`` for type-safe access to shared attributes.
    Actual implementations provided by ``FiligreeDB`` at composition time via MRO.
    """

    # -- Parent hierarchy invariants -----------------------------------------

    def _would_create_parent_cycle(self, child_id: str, proposed_parent_id: str) -> bool:
        """Return True if setting ``child_id``'s parent to ``proposed_parent_id``
        would create a circular parent chain. Walks the proposed parent's
        ancestor chain looking for the child. Shared by ``update_issue`` and
        ``EventsMixin.undo_last`` so both write paths enforce the same
        hierarchy invariant (filigree-0a8c3d38d7).
        """
        if not proposed_parent_id or proposed_parent_id == child_id:
            return proposed_parent_id == child_id
        ancestor: str | None = proposed_parent_id
        while ancestor is not None:
            row = self.conn.execute("SELECT parent_id FROM issues WHERE id = ?", (ancestor,)).fetchone()
            if row is None:
                return False
            ancestor = row["parent_id"]
            if ancestor == child_id:
                return True
        return False

    # -- Field validation ----------------------------------------------------

    def _validate_field_values(self, issue_type: str, fields: dict[str, Any]) -> list[str]:
        """Validate field values against their schema patterns.

        Returns a list of error messages for fields that don't match.
        """
        tpl = self.templates.get_type(issue_type)
        if tpl is None:
            return []
        schema_by_name = {f.name: f for f in tpl.fields_schema}
        errors: list[str] = []
        for name, value in fields.items():
            fs = schema_by_name.get(name)
            if fs is None:
                continue
            err = validate_field_pattern(fs, value)
            if err is not None:
                errors.append(err)
            if fs.type == "enum" and fs.options and value is not None:
                str_value = str(value)
                if str_value and str_value not in fs.options:
                    errors.append(f"Field '{fs.name}' value '{str_value}' is not a valid option. Valid options: {', '.join(fs.options)}")
        return errors

    def _check_field_uniqueness(self, issue_type: str, fields: dict[str, Any], *, exclude_id: str | None = None) -> None:
        """Raise ValueError if any unique field value conflicts with existing issues."""
        tpl = self.templates.get_type(issue_type)
        if tpl is None:
            return
        for fs in tpl.fields_schema:
            if not fs.unique:
                continue
            value = fields.get(fs.name)
            if value is None or (isinstance(value, str) and value.strip() == ""):
                continue
            sql = "SELECT id FROM issues WHERE type = ? AND json_extract(fields, ?) = ?"
            params: list[Any] = [issue_type, f"$.{fs.name}", value]
            if exclude_id is not None:
                sql += " AND id != ?"
                params.append(exclude_id)
            row = self.conn.execute(sql, params).fetchone()
            if row is not None:
                msg = f"Duplicate value '{value}' for unique field '{fs.name}' on type '{issue_type}' (conflicts with issue {row['id']})"
                raise ValueError(msg)

    # -- ID generation -------------------------------------------------------

    def _generate_unique_id(self, table: str, infix: str = "") -> str:
        """Generate a unique ID using O(1) EXISTS checks against the PK index.

        *table* is always a hardcoded literal at the call site (never user input).
        """
        sep = f"-{infix}-" if infix else "-"
        for _ in range(10):
            candidate = f"{self.prefix}{sep}{uuid.uuid4().hex[:10]}"
            if self.conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (candidate,)).fetchone() is None:
                return candidate
        # 10 consecutive collisions is near-impossible — likely a systemic bug
        logger.error(
            "10 consecutive ID collisions in table %s (prefix=%s). Possible corrupted DB, broken RNG, or wrong table.",
            table,
            self.prefix,
        )
        candidate = f"{self.prefix}{sep}{uuid.uuid4().hex[:16]}"
        if self.conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (candidate,)).fetchone() is not None:
            msg = f"ID generation failed: fallback ID also collided in table {table}"
            raise RuntimeError(msg)
        return candidate

    # -- Issue CRUD ----------------------------------------------------------

    def create_issue(
        self,
        title: str,
        *,
        type: str = "task",
        priority: int = 2,
        parent_id: str | None = None,
        assignee: str = "",
        description: str = "",
        notes: str = "",
        fields: dict[str, Any] | None = None,
        labels: list[str] | None = None,
        deps: list[str] | None = None,
        actor: str = "",
    ) -> Issue:
        if not title or not title.strip():
            msg = "Title cannot be empty"
            raise ValueError(msg)
        _validate_priority_value(priority)
        if fields is not None and not isinstance(fields, dict):
            msg = "fields must be a dict"
            raise TypeError(msg)
        if fields:
            for k in fields:
                if not k or not k.strip():
                    msg = "Field key cannot be empty"
                    raise ValueError(msg)
        # Validate container shape before iterating — a bare str would otherwise
        # be iterated character-by-character (see filigree-0b4fcb6d30).
        if labels is not None:
            _validate_string_list(labels, "labels")
        if deps is not None:
            _validate_string_list(deps, "deps")
        assignee = _normalize_assignee(assignee)
        if labels:
            labels = [self._validate_label_name(label) for label in labels]
        # Reject unknown types — don't silently fall back
        if self.templates.get_type(type) is None:
            valid_types = [t.type for t in self.templates.list_types()]
            msg = f"Unknown type '{type}'. Valid types: {', '.join(valid_types)}"
            raise ValueError(msg)

        self._validate_parent_id(parent_id)

        # Validate field patterns and uniqueness BEFORE any writes
        if fields:
            pattern_errors = self._validate_field_values(type, fields)
            if pattern_errors:
                msg = "Field validation failed: " + "; ".join(pattern_errors)
                raise ValueError(msg)
            self._check_field_uniqueness(type, fields)

        # Validate deps BEFORE any writes to prevent partial commits
        if deps:
            dep_ph = ",".join("?" * len(deps))
            found = {r["id"] for r in self.conn.execute(f"SELECT id FROM issues WHERE id IN ({dep_ph})", deps).fetchall()}
            missing = [d for d in deps if d not in found]
            if missing:
                msg = f"Invalid dependency IDs (not found): {', '.join(missing)}"
                raise ValueError(msg)

        issue_id = self._generate_unique_id("issues")
        now = _now_iso()
        fields = fields or {}

        # Determine initial state from template
        initial_state = self.templates.get_initial_state(type)

        try:
            self.conn.execute(
                "INSERT INTO issues (id, title, status, priority, type, parent_id, assignee, "
                "created_at, updated_at, description, notes, fields) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    issue_id,
                    title,
                    initial_state,
                    priority,
                    type,
                    parent_id,
                    assignee,
                    now,
                    now,
                    description,
                    notes,
                    json.dumps(fields),
                ),
            )

            self._record_event(issue_id, "created", actor=actor, new_value=title)

            if labels:
                labels = list(dict.fromkeys(labels))  # explicit dedup, preserve order
                for label in labels:
                    self.conn.execute(
                        "INSERT INTO labels (issue_id, label) VALUES (?, ?)",
                        (issue_id, label),
                    )

            if deps:
                for dep_id in deps:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO dependencies (issue_id, depends_on_id, type, created_at) VALUES (?, ?, 'blocks', ?)",
                        (issue_id, dep_id, now),
                    )

            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        return self.get_issue(issue_id)

    def get_issue(self, issue_id: str) -> Issue:
        # Reads do not enforce prefix-matching — cross-project lookups simply
        # return KeyError if not found. Writes do enforce; see update_issue,
        # close_issue, reopen_issue, claim_issue.
        return self._build_issue(issue_id)

    def _build_issue(self, issue_id: str) -> Issue:
        """Build a single Issue with all computed fields. Internal — caller must validate existence."""
        issues = self._build_issues_batch([issue_id])
        if not issues:
            msg = f"Issue not found: {issue_id}"
            raise KeyError(msg)
        return issues[0]

    def _build_issues_batch(self, issue_ids: list[str]) -> list[Issue]:
        """Build multiple Issues efficiently with batched queries (eliminates N+1)."""
        if not issue_ids:
            return []

        placeholders = ",".join("?" * len(issue_ids))

        # 1. Fetch all issue rows
        rows_by_id: dict[str, sqlite3.Row] = {}
        for r in self.conn.execute(f"SELECT * FROM issues WHERE id IN ({placeholders})", issue_ids).fetchall():
            rows_by_id[r["id"]] = r

        # 2. Batch fetch labels
        labels_by_id: dict[str, list[str]] = {iid: [] for iid in issue_ids}
        for r in self.conn.execute(f"SELECT issue_id, label FROM labels WHERE issue_id IN ({placeholders})", issue_ids).fetchall():
            labels_by_id[r["issue_id"]].append(r["label"])

        # 3. Batch fetch "blocks" — issues blocked BY these IDs (where depends_on_id = this issue)
        blocks_by_id: dict[str, list[str]] = {iid: [] for iid in issue_ids}
        for r in self.conn.execute(
            f"SELECT depends_on_id, issue_id FROM dependencies WHERE depends_on_id IN ({placeholders})",
            issue_ids,
        ).fetchall():
            blocks_by_id[r["depends_on_id"]].append(r["issue_id"])

        # 4. Batch fetch "blocked_by" — only open (non-done, non-archived) blockers.
        # Archived blockers must not appear here (filigree-42045dd065): archive_closed
        # writes status='archived' which is not a workflow done state.
        # filigree-b55aa3191f: match by (blocker.type, blocker.status) so a state
        # name shared across types in different categories (e.g.
        # incident.resolved=wip, debt_item.resolved=done) is classified per type.
        blocker_done_sql, blocker_done_params = self._category_predicate_sql(
            "done",
            type_col="blocker.type",
            status_col="blocker.status",
            include_archived=True,
        )
        blocked_by_id: dict[str, list[str]] = {iid: [] for iid in issue_ids}
        for r in self.conn.execute(
            f"SELECT d.issue_id, d.depends_on_id FROM dependencies d "
            f"JOIN issues blocker ON d.depends_on_id = blocker.id "
            f"WHERE d.issue_id IN ({placeholders}) AND NOT ({blocker_done_sql})",
            [*issue_ids, *blocker_done_params],
        ).fetchall():
            blocked_by_id[r["issue_id"]].append(r["depends_on_id"])

        # 5. Batch fetch children
        children_by_id: dict[str, list[str]] = {iid: [] for iid in issue_ids}
        for r in self.conn.execute(f"SELECT id, parent_id FROM issues WHERE parent_id IN ({placeholders})", issue_ids).fetchall():
            children_by_id[r["parent_id"]].append(r["id"])

        # 6. Batch compute open blocker counts — same blocker semantics as step 4.
        open_blockers_by_id: dict[str, int] = dict.fromkeys(issue_ids, 0)
        for r in self.conn.execute(
            f"SELECT d.issue_id, COUNT(*) as cnt FROM dependencies d "
            f"JOIN issues blocker ON d.depends_on_id = blocker.id "
            f"WHERE d.issue_id IN ({placeholders}) AND NOT ({blocker_done_sql}) "
            f"GROUP BY d.issue_id",
            [*issue_ids, *blocker_done_params],
        ).fetchall():
            open_blockers_by_id[r["issue_id"]] = r["cnt"]

        # Build Issue objects preserving input order
        result: list[Issue] = []
        for iid in issue_ids:
            row = rows_by_id.get(iid)
            if row is None:
                continue
            result.append(
                Issue(
                    id=row["id"],
                    title=row["title"],
                    status=row["status"],
                    priority=row["priority"],
                    type=row["type"],
                    parent_id=row["parent_id"],
                    assignee=row["assignee"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    closed_at=row["closed_at"],
                    description=row["description"],
                    notes=row["notes"],
                    fields=_safe_fields_json(row["fields"], iid),
                    labels=labels_by_id.get(iid, []),
                    blocks=blocks_by_id.get(iid, []),
                    blocked_by=blocked_by_id.get(iid, []),
                    is_ready=(
                        # filigree-b55aa3191f: resolve category per (type, status)
                        # rather than via a deduplicated open-state name set, so a
                        # state name shared across types in different categories
                        # is classified correctly.
                        self._resolve_status_category(row["type"], row["status"]) == "open"
                        and open_blockers_by_id.get(iid, 0) == 0
                        and not (row["assignee"] or "")
                    ),
                    children=children_by_id.get(iid, []),
                    status_category=self._resolve_status_category(row["type"], row["status"]),
                )
            )
        return result

    def update_issue(
        self,
        issue_id: str,
        *,
        title: str | None = None,
        status: str | None = None,
        priority: int | None = None,
        assignee: str | None = None,
        description: str | None = None,
        notes: str | None = None,
        parent_id: str | None = None,
        fields: dict[str, Any] | None = None,
        actor: str = "",
        _skip_transition_check: bool = False,
    ) -> Issue:
        self._check_id_prefix(issue_id)
        current = self.get_issue(issue_id)
        now = _now_iso()

        # --- Validate all inputs BEFORE any writes to prevent partial commits ---
        if fields is not None and not isinstance(fields, dict):
            msg = "fields must be a dict"
            raise TypeError(msg)
        if title is not None and not title.strip():
            # filigree-365dff403e: mirror create_issue's invariant on update.
            msg = "Title cannot be empty"
            raise ValueError(msg)
        if priority is not None:
            _validate_priority_value(priority)
        if assignee is not None:
            assignee = _normalize_assignee(assignee)

        if parent_id is not None and parent_id != "":
            if parent_id == issue_id:
                msg = f"Issue {issue_id} cannot be its own parent"
                raise ValueError(msg)
            self._validate_parent_id(parent_id)
            if self._would_create_parent_cycle(issue_id, parent_id):
                msg = f"Setting parent_id to '{parent_id}' would create a circular parent chain"
                raise ValueError(msg)

        # Cache transition validation result for reuse in write phase (warnings)
        _transition_result: TransitionResult | None = None
        _transition_warnings: list[str] = []
        if status is not None and status != current.status:
            self._validate_status(status, current.type)

            if not _skip_transition_check:
                # Atomic transition-with-fields: validate merged fields against target state
                merged_fields = {**current.fields}
                if fields is not None:
                    merged_fields.update(fields)

                tpl = self.templates.get_type(current.type)
                if tpl is not None:
                    _transition_result = self.templates.validate_transition(current.type, current.status, status, merged_fields)
                    if not _transition_result.allowed:
                        if _transition_result.missing_fields:
                            missing_str = ", ".join(_transition_result.missing_fields)
                            msg = (
                                f"Cannot transition '{current.status}' -> '{status}' for type "
                                f"'{current.type}': missing required fields: {missing_str}"
                            )
                        else:
                            msg = (
                                f"Transition '{current.status}' -> '{status}' is not allowed for type "
                                f"'{current.type}'. Use get_valid_transitions() to see allowed transitions."
                            )
                        raise ValueError(msg)

        # Validate field patterns and uniqueness for incoming fields
        if fields is not None:
            pattern_errors = self._validate_field_values(current.type, fields)
            if pattern_errors:
                msg = "Field validation failed: " + "; ".join(pattern_errors)
                raise ValueError(msg)
            self._check_field_uniqueness(current.type, fields, exclude_id=issue_id)

        # --- All validation passed — now record events and apply changes ---
        updates: list[str] = []
        params: list[Any] = []

        try:
            if title is not None and title != current.title:
                self._record_event(issue_id, "title_changed", actor=actor, old_value=current.title, new_value=title)
                updates.append("title = ?")
                params.append(title)

            if status is not None and status != current.status:
                # Record soft-enforcement warnings from cached validation result
                if _transition_result is not None:
                    _transition_warnings = _transition_data_warnings(_transition_result)
                    for warning in _transition_warnings:
                        self._record_event(
                            issue_id,
                            "transition_warning",
                            actor=actor,
                            old_value=current.status,
                            new_value=status,
                            comment=warning,
                        )

                self._record_event(issue_id, "status_changed", actor=actor, old_value=current.status, new_value=status)
                updates.append("status = ?")
                params.append(status)

                # Set closed_at when entering a done-category state
                status_cat = self.templates.get_category(current.type, status)
                is_done = (status_cat or self._infer_status_category(current.type, status)) == "done"

                if is_done:
                    updates.append("closed_at = ?")
                    params.append(now)
                else:
                    # Clear closed_at when leaving a done-category state
                    old_cat = self.templates.get_category(current.type, current.status)
                    if (old_cat or self._infer_status_category(current.type, current.status)) == "done":
                        updates.append("closed_at = NULL")

            if priority is not None and priority != current.priority:
                self._record_event(
                    issue_id,
                    "priority_changed",
                    actor=actor,
                    old_value=str(current.priority),
                    new_value=str(priority),
                )
                updates.append("priority = ?")
                params.append(priority)

            if assignee is not None and assignee != current.assignee:
                self._record_event(issue_id, "assignee_changed", actor=actor, old_value=current.assignee, new_value=assignee)
                updates.append("assignee = ?")
                params.append(assignee)

            if description is not None and description != current.description:
                self._record_event(
                    issue_id,
                    "description_changed",
                    actor=actor,
                    old_value=current.description,
                    new_value=description,
                )
                updates.append("description = ?")
                params.append(description)

            if notes is not None and notes != current.notes:
                self._record_event(
                    issue_id,
                    "notes_changed",
                    actor=actor,
                    old_value=current.notes,
                    new_value=notes,
                )
                updates.append("notes = ?")
                params.append(notes)

            if parent_id is not None:
                if parent_id == "":
                    # Clear parent
                    if current.parent_id is not None:
                        self._record_event(
                            issue_id,
                            "parent_changed",
                            actor=actor,
                            old_value=current.parent_id or "",
                            new_value="",
                        )
                        updates.append("parent_id = NULL")
                else:
                    if parent_id != current.parent_id:
                        self._record_event(
                            issue_id,
                            "parent_changed",
                            actor=actor,
                            old_value=current.parent_id or "",
                            new_value=parent_id,
                        )
                        updates.append("parent_id = ?")
                        params.append(parent_id)

            if fields is not None:
                # Merge into existing fields
                merged = {**current.fields, **fields}
                if merged != current.fields:
                    self._record_event(
                        issue_id,
                        "fields_changed",
                        actor=actor,
                        old_value=json.dumps(current.fields),
                        new_value=json.dumps(merged),
                    )
                    updates.append("fields = ?")
                    params.append(json.dumps(merged))

            if updates:
                updates.append("updated_at = ?")
                params.append(now)
                params.append(issue_id)
                sql = f"UPDATE issues SET {', '.join(updates)} WHERE id = ?"
                self.conn.execute(sql, params)
                self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        updated = self.get_issue(issue_id)
        if _transition_warnings:
            updated.data_warnings.extend(_transition_warnings)
        return updated

    def close_issue(
        self,
        issue_id: str,
        *,
        reason: str = "",
        actor: str = "",
        status: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> Issue:
        if fields is not None and not isinstance(fields, dict):
            msg = "fields must be a dict"
            raise TypeError(msg)
        self._check_id_prefix(issue_id)

        current = self.get_issue(issue_id)

        # Determine done state via template system
        if self._resolve_status_category(current.type, current.status) == "done":
            msg = f"Issue {issue_id} is already closed (status: '{current.status}', closed_at: {current.closed_at})"
            raise ValueError(msg)

        if status is not None:
            # Validate that the requested status is a done-category state
            target_category = self.templates.get_category(current.type, status)
            if target_category != "done":
                msg = f"Cannot close with status '{status}': it is not a done-category state for type '{current.type}'."
                raise ValueError(msg)
            done_status = status
        else:
            # Default to first done-category state
            _first_done = self.templates.get_first_state_of_category(current.type, "done")
            done_status = _first_done if _first_done is not None else "closed"

        # Enforce hard gates even though close_issue skips transition graph
        # validation. If a defined transition from current→done has hard
        # enforcement, the required fields must be satisfied.
        merged_fields = {**current.fields}
        if fields:
            merged_fields.update(fields)
        if reason:
            merged_fields["close_reason"] = reason
        result = self.templates.validate_transition(current.type, current.status, done_status, merged_fields)
        if not result.allowed and result.enforcement == "hard":
            missing_str = ", ".join(result.missing_fields)
            msg = f"Cannot close issue {issue_id}: hard-enforcement gate requires fields: {missing_str}"
            raise ValueError(msg)

        # Merge close_reason into fields for the update call
        update_fields: dict[str, Any] = {}
        if fields:
            update_fields.update(fields)
        if reason:
            update_fields["close_reason"] = reason

        return self.update_issue(
            issue_id,
            status=done_status,
            fields=update_fields or None,
            actor=actor,
            _skip_transition_check=True,
        )

    def reopen_issue(self, issue_id: str, *, actor: str = "") -> Issue:
        """Reopen a closed issue to the last non-done status before closure.

        Clears closed_at and stale close-only fields. Only works on issues in
        done-category states.
        """
        self._check_id_prefix(issue_id)
        current = self.get_issue(issue_id)
        if self._resolve_status_category(current.type, current.status) != "done":
            msg = f"Cannot reopen {issue_id}: status '{current.status}' is not in a done-category state"
            raise ValueError(msg)

        reopen_status = self._reopen_target_status(current)
        result = self.update_issue(issue_id, status=reopen_status, actor=actor, _skip_transition_check=True)
        reopen_fields = _fields_for_reopen(current.fields)
        if reopen_fields != current.fields:
            self._record_event(
                issue_id,
                "fields_changed",
                actor=actor,
                old_value=json.dumps(current.fields),
                new_value=json.dumps(reopen_fields),
            )
            self.conn.execute(
                "UPDATE issues SET fields = ?, updated_at = ? WHERE id = ?",
                (json.dumps(reopen_fields), _now_iso(), issue_id),
            )
            self.conn.commit()
            result = self.get_issue(issue_id)
        # Record "reopened" event after update_issue has committed the status
        # change and its own status_changed event.  The state change is already
        # durable, so a failure here must not propagate — callers would wrongly
        # assume the reopen failed and retry into a confusing error.
        try:
            self._record_event(issue_id, "reopened", actor=actor, old_value=current.status, new_value=reopen_status)
            self.conn.commit()
        except Exception:
            logger.warning("Failed to record reopened event for %s (status change succeeded)", issue_id, exc_info=True)
            with contextlib.suppress(sqlite3.Error):
                self.conn.rollback()
        return result

    def _reopen_target_status(self, issue: Issue) -> str:
        """Find the most recent non-done status that led into a done state."""
        rows = self.conn.execute(
            "SELECT old_value, new_value FROM events "
            "WHERE issue_id = ? AND event_type = 'status_changed' "
            "ORDER BY created_at DESC, id DESC",
            (issue.id,),
        ).fetchall()
        for row in rows:
            old_status = row["old_value"]
            new_status = row["new_value"]
            if not old_status or not new_status:
                continue
            try:
                self._validate_status(old_status, issue.type)
            except ValueError:
                continue
            if (
                self._resolve_status_category(issue.type, new_status) == "done"
                and self._resolve_status_category(issue.type, old_status) != "done"
            ):
                return cast(str, old_status)
        return self.templates.get_initial_state(issue.type)

    def claim_issue(self, issue_id: str, *, assignee: str, actor: str = "", _commit: bool = True) -> Issue:
        """Atomically claim an open/wip-category issue with optimistic locking.

        Sets assignee only — does NOT change status. Agent uses update_issue
        to advance through the workflow after claiming. Wip-category issues can
        be claimed when they have been released and are currently unassigned,
        which preserves an atomic multi-agent handoff path after release_claim().

        Uses a single atomic UPDATE with WHERE guard to prevent race conditions
        where two agents try to claim the same issue concurrently.
        """
        # filigree-694f7e9bf8: enforce the same trimmed-identity invariant as
        # create_issue/update_issue. Without normalization, claiming with
        # "  bob  " stores the padded form and a later canonical "bob" claim
        # falsely reports "already assigned to '  bob  '".
        assignee = _normalize_assignee(assignee)
        if not assignee:
            msg = "Assignee cannot be empty"
            raise ValueError(msg)
        self._check_id_prefix(issue_id)
        # Look up the issue type and current assignee so we know which states are
        # claimable and can record old_value for undo.
        row = self.conn.execute("SELECT type, assignee FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None:
            msg = f"Issue not found: {issue_id}"
            raise KeyError(msg)
        issue_type = row["type"]
        old_assignee = row["assignee"] or ""

        # Open states are claimable for new work. Wip states are claimable only
        # through the assignee CAS below, so released in-flight work can be
        # handed off atomically without changing status.
        claimable_states: list[str] = []
        tpl = self.templates.get_type(issue_type)
        if tpl is not None:
            claimable_states = [s.name for s in tpl.states if s.category in {"open", "wip"}]
        if not claimable_states:
            claimable_states = ["open"]

        # Atomic UPDATE: only succeeds if issue is unassigned OR already owned by this agent
        status_ph = ",".join("?" * len(claimable_states))
        try:
            cursor = self.conn.execute(
                f"UPDATE issues SET assignee = ?, updated_at = ? "
                f"WHERE id = ? AND status IN ({status_ph}) "
                f"AND (assignee = '' OR assignee IS NULL OR assignee = ?)",
                [assignee, _now_iso(), issue_id, *claimable_states, assignee],
            )

            if cursor.rowcount == 0:
                # Figure out why it failed: wrong status or already claimed?
                current = self.conn.execute("SELECT status, assignee FROM issues WHERE id = ?", (issue_id,)).fetchone()
                if current is None:
                    msg = f"Issue not found: {issue_id}"
                    raise KeyError(msg)
                if current["assignee"] and current["assignee"] != assignee:
                    msg = f"Cannot claim {issue_id}: already assigned to '{current['assignee']}'"
                    raise ValueError(msg)
                msg = (
                    f"Cannot claim {issue_id}: status is '{current['status']}', expected open-category state or wip-category handoff state"
                )
                raise ValueError(msg)

            self._record_event(issue_id, "claimed", actor=actor, old_value=old_assignee, new_value=assignee)
            if _commit:
                self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get_issue(issue_id)

    def release_claim(self, issue_id: str, *, actor: str = "") -> Issue:
        """Release a claimed issue by clearing its assignee.

        Does NOT change status. Uses compare-and-swap on the observed
        assignee so a concurrent reassignment between read and UPDATE
        cannot be silently erased.
        """
        self._check_id_prefix(issue_id)
        row = self.conn.execute("SELECT assignee FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None:
            msg = f"Issue not found: {issue_id}"
            raise KeyError(msg)
        observed = row["assignee"] or ""
        if not observed:
            msg = f"Cannot release {issue_id}: no assignee set"
            raise ValueError(msg)

        try:
            cursor = self.conn.execute(
                "UPDATE issues SET assignee = '', updated_at = ? WHERE id = ? AND assignee = ?",
                [_now_iso(), issue_id, observed],
            )

            if cursor.rowcount == 0:
                current = self.conn.execute("SELECT assignee FROM issues WHERE id = ?", (issue_id,)).fetchone()
                if current is None:
                    msg = f"Issue not found: {issue_id}"
                    raise KeyError(msg)
                new_assignee = current["assignee"] or ""
                if not new_assignee:
                    msg = f"Cannot release {issue_id}: already released"
                    raise ValueError(msg)
                msg = f"Cannot release {issue_id}: reassigned to '{new_assignee}' (expected '{observed}')"
                raise ValueError(msg)

            self._record_event(issue_id, "released", actor=actor, old_value=observed)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get_issue(issue_id)

    def claim_next(
        self,
        assignee: str,
        *,
        type_filter: str | None = None,
        priority_min: int | None = None,
        priority_max: int | None = None,
        actor: str = "",
    ) -> Issue | None:
        """Claim the highest-priority ready issue matching filters.

        Iterates ready issues sorted by priority and attempts claim_issue()
        on each until one succeeds (handles race conditions with retry).
        Returns None if no matching ready issues exist.
        """
        result = self._claim_next_with_prior(
            assignee,
            type_filter=type_filter,
            priority_min=priority_min,
            priority_max=priority_max,
            actor=actor,
        )
        return result[0] if result is not None else None

    def _claim_next_with_prior(
        self,
        assignee: str,
        *,
        type_filter: str | None = None,
        priority_min: int | None = None,
        priority_max: int | None = None,
        actor: str = "",
        _commit: bool = True,
    ) -> tuple[Issue, str] | None:
        """Internal: claim_next that also returns the candidate's prior assignee.

        Returns ``(claimed_issue, prior_assignee)`` so composed callers
        (start_next_work) can distinguish a freshly-acquired claim from a
        same-assignee re-claim and decide whether a compensating release is
        appropriate. ``prior_assignee`` is read transactionally just before
        ``claim_issue`` so a concurrent reassignment landing between the read
        and the UPDATE will surface as the same race ``claim_issue`` already
        handles (skip and continue).
        """
        if not assignee or not assignee.strip():
            msg = "Assignee cannot be empty"
            raise ValueError(msg)
        ready = self.get_ready()

        skipped = 0
        for issue in ready:
            if type_filter is not None and issue.type != type_filter:
                continue
            if priority_min is not None and issue.priority < priority_min:
                continue
            if priority_max is not None and issue.priority > priority_max:
                continue
            try:
                row = self.conn.execute("SELECT assignee FROM issues WHERE id = ?", (issue.id,)).fetchone()
                prior_assignee = (row["assignee"] if row is not None else "") or ""
                claimed = self.claim_issue(issue.id, assignee=assignee, actor=actor or assignee, _commit=_commit)
            except (ValueError, KeyError) as exc:
                skipped += 1
                logger.debug("claim_next: skipping %s: %s", issue.id, exc)
                continue  # Race condition, status mismatch, or deleted issue
            return claimed, prior_assignee
        if skipped:
            logger.warning("claim_next: all %d candidate(s) failed to claim for '%s'", skipped, assignee)
        return None

    def start_work(
        self,
        issue_id: str,
        *,
        assignee: str,
        target_status: str | None = None,
        actor: str = "",
    ) -> Issue:
        """Atomically claim an issue and transition it to a working status.

        Phase D6 composed operation. Performs ``claim_issue`` without
        committing, followed by ``update_issue(status=target_status)``. If the
        transition fails, rolling back that open transaction removes both the
        attempted claim and its audit event, so failed starts do not look like
        real claim/release handoffs.

        ``target_status`` defaults to the unique wip-category status reachable
        from the issue's current status. If the current status can transition
        to multiple wip statuses an ``AmbiguousTransitionError`` surfaces
        (caller must specify ``target_status`` explicitly); if zero,
        ``InvalidTransitionError``.

        Transaction rollback preserves the prior ownership state
        (filigree-31404d228f). ``claim_issue`` is idempotent for the same
        identity — if the issue was already owned by ``assignee`` before the
        call, a transition failure must leave the claim in place rather than
        wiping out an unrelated, pre-existing claim.
        """
        actor = actor or assignee

        issue = self.claim_issue(issue_id, assignee=assignee, actor=actor, _commit=False)

        def _rollback_claim() -> None:
            self._rollback_uncommitted_start_claim(issue_id)

        if target_status is None:
            tpl = self.templates.get_type(issue.type)
            if tpl is None:
                # Roll back the claim before surfacing the error.
                from filigree.types.api import InvalidTransitionError

                _rollback_claim()
                raise InvalidTransitionError(issue.type, issue.status)
            try:
                target_status = tpl.reachable_working_status(issue.status)
            except Exception:
                _rollback_claim()
                raise

        try:
            updated = self.update_issue(issue_id, status=target_status, actor=actor)
            if self.conn.in_transaction:
                self.conn.commit()
            return updated
        except Exception:
            _rollback_claim()
            raise

    def start_next_work(
        self,
        *,
        assignee: str,
        type_filter: str | None = None,
        priority_min: int | None = None,
        priority_max: int | None = None,
        target_status: str | None = None,
        actor: str = "",
    ) -> Issue | None:
        """Claim the highest-priority ready issue (filtered) and atomically
        transition it to a working status.

        Phase D6 composed operation: ``claim_next`` + atomic transition with
        compensating rollback (see ``start_work`` for the rollback contract).
        Returns ``None`` if no ready issue matches the filters.

        Rollback only releases the claim when *this* invocation acquired it.
        ``claim_next`` reuses an existing same-assignee claim (claim_issue is
        idempotent for the same identity), so an unconditional release on
        transition failure would wipe out a pre-existing, unrelated claim.
        Mirrors ``start_work``'s ownership-tracking contract.

        Tie-break ordering inherits from ``claim_next``: priority asc,
        created_at asc, issue_id asc.
        """
        actor = actor or assignee
        claimed_with_prior = self._claim_next_with_prior(
            assignee,
            type_filter=type_filter,
            priority_min=priority_min,
            priority_max=priority_max,
            actor=actor,
            _commit=False,
        )
        if claimed_with_prior is None:
            return None
        claimed, _prior_assignee = claimed_with_prior

        def _rollback_claim() -> None:
            self._rollback_uncommitted_start_claim(claimed.id)

        if target_status is None:
            tpl = self.templates.get_type(claimed.type)
            if tpl is None:
                from filigree.types.api import InvalidTransitionError

                _rollback_claim()
                raise InvalidTransitionError(claimed.type, claimed.status)
            try:
                target_status = tpl.reachable_working_status(claimed.status)
            except Exception:
                _rollback_claim()
                raise

        try:
            updated = self.update_issue(claimed.id, status=target_status, actor=actor)
            if self.conn.in_transaction:
                self.conn.commit()
            return updated
        except Exception:
            _rollback_claim()
            raise

    def _rollback_uncommitted_start_claim(self, issue_id: str) -> None:
        """Rollback an uncommitted claim acquired by start_work/start_next_work.

        The composed operation keeps claim and transition in one transaction.
        If transition selection or validation fails before update_issue commits,
        this removes the attempted claim and its event without recording a
        synthetic release.
        """
        if not self.conn.in_transaction:
            return
        try:
            self.conn.rollback()
        except sqlite3.Error:
            logger.warning("start_work rollback failed for uncommitted claim on %s", issue_id, exc_info=True)

    def _batch_with_transition_errors(
        self,
        issue_ids: list[str],
        action: Callable[[str], Issue],
    ) -> tuple[list[Issue], list[BatchFailure]]:
        """Run *action(issue_id)* per item with transition-enriched error handling."""
        _validate_string_list(issue_ids, "issue_ids")
        results: list[Issue] = []
        errors: list[BatchFailure] = []
        for issue_id in issue_ids:
            try:
                results.append(action(issue_id))
            except KeyError:
                errors.append(BatchFailure(id=issue_id, error=f"Not found: {issue_id}", code=ErrorCode.NOT_FOUND))
            except ValueError as e:
                code = classify_value_error(str(e))
                err = BatchFailure(id=issue_id, error=str(e), code=code)
                if code == ErrorCode.INVALID_TRANSITION:
                    try:
                        transitions = self.get_valid_transitions(issue_id)
                        err["valid_transitions"] = [{"to": t.to, "category": t.category} for t in transitions]
                    except KeyError:
                        logger.debug("batch: could not enrich error with transitions for %s", issue_id)
                errors.append(err)
        return results, errors

    def batch_close(
        self,
        issue_ids: list[str],
        *,
        reason: str = "",
        actor: str = "",
    ) -> tuple[list[Issue], list[BatchFailure]]:
        """Close multiple issues with per-item error handling. Returns (closed, errors)."""
        return self._batch_with_transition_errors(
            issue_ids,
            lambda iid: self.close_issue(iid, reason=reason, actor=actor),
        )

    def batch_update(
        self,
        issue_ids: list[str],
        *,
        status: str | None = None,
        priority: int | None = None,
        assignee: str | None = None,
        fields: dict[str, Any] | None = None,
        actor: str = "",
    ) -> tuple[list[Issue], list[BatchFailure]]:
        """Update multiple issues with the same changes. Returns (updated, errors)."""
        return self._batch_with_transition_errors(
            issue_ids,
            lambda iid: self.update_issue(
                iid,
                status=status,
                priority=priority,
                assignee=assignee,
                fields=fields,
                actor=actor,
            ),
        )

    def batch_add_label(
        self,
        issue_ids: list[str],
        *,
        label: str,
    ) -> tuple[list[dict[str, str]], list[BatchFailure]]:
        """Add the same label to multiple issues. Returns (labeled, errors)."""
        _validate_string_list(issue_ids, "issue_ids")
        if not isinstance(label, str):
            msg = "label must be a string"
            raise TypeError(msg)

        results: list[dict[str, str]] = []
        errors: list[BatchFailure] = []
        for issue_id in issue_ids:
            try:
                self.get_issue(issue_id)
                added, _canonical = self.add_label(issue_id, label)
                results.append({"id": issue_id, "status": "added" if added else "already_exists"})
            except KeyError:
                errors.append(BatchFailure(id=issue_id, error=f"Not found: {issue_id}", code=ErrorCode.NOT_FOUND))
            except ValueError as e:
                errors.append(BatchFailure(id=issue_id, error=str(e), code=ErrorCode.VALIDATION))
        return results, errors

    def batch_remove_label(
        self,
        issue_ids: list[str],
        *,
        label: str,
    ) -> tuple[list[dict[str, str]], list[BatchFailure]]:
        """Remove the same label from multiple issues. Returns (removed, errors)."""
        _validate_string_list(issue_ids, "issue_ids")
        if not isinstance(label, str):
            msg = "label must be a string"
            raise TypeError(msg)

        results: list[dict[str, str]] = []
        errors: list[BatchFailure] = []
        for issue_id in issue_ids:
            try:
                self.get_issue(issue_id)
                removed, _canonical = self.remove_label(issue_id, label)
                results.append({"id": issue_id, "status": "removed" if removed else "not_found"})
            except KeyError:
                errors.append(BatchFailure(id=issue_id, error=f"Not found: {issue_id}", code=ErrorCode.NOT_FOUND))
            except ValueError as e:
                errors.append(BatchFailure(id=issue_id, error=str(e), code=ErrorCode.VALIDATION))
        return results, errors

    def batch_add_comment(
        self,
        issue_ids: list[str],
        *,
        text: str,
        author: str = "",
    ) -> tuple[list[dict[str, str | int]], list[BatchFailure]]:
        """Add the same comment to multiple issues. Returns (commented, errors)."""
        _validate_string_list(issue_ids, "issue_ids")
        if not isinstance(text, str):
            msg = "text must be a string"
            raise TypeError(msg)
        if not isinstance(author, str):
            msg = "author must be a string"
            raise TypeError(msg)

        results: list[dict[str, str | int]] = []
        errors: list[BatchFailure] = []
        for issue_id in issue_ids:
            try:
                self.get_issue(issue_id)
                comment_id = self.add_comment(issue_id, text, author=author)
                results.append({"id": issue_id, "comment_id": comment_id})
            except KeyError:
                errors.append(BatchFailure(id=issue_id, error=f"Not found: {issue_id}", code=ErrorCode.NOT_FOUND))
            except ValueError as e:
                errors.append(BatchFailure(id=issue_id, error=str(e), code=ErrorCode.VALIDATION))
        return results, errors

    def list_issues(
        self,
        *,
        status: str | None = None,
        type: str | None = None,
        priority: int | None = None,
        parent_id: str | None = None,
        assignee: str | None = None,
        label: str | list[str] | None = None,
        label_prefix: str | None = None,
        not_label: str | None = None,
        sort_by: str = "priority",
        direction: str = "asc",
        limit: int = 100,
        offset: int = 0,
    ) -> list[Issue]:
        if limit < 0:
            raise ValueError(f"limit must be non-negative, got {limit}")
        if offset < 0:
            raise ValueError(f"offset must be non-negative, got {offset}")
        if label_prefix is not None and not label_prefix.endswith(":"):
            msg = f"label_prefix must include a trailing colon (got {label_prefix!r})"
            raise ValueError(msg)
        order_by = _list_issue_order_by(sort_by, direction)

        # Normalize label to list
        if isinstance(label, str):
            label = [label]

        conditions: list[str] = []
        params: list[Any] = []

        # Build the type-aware blocker-done predicate once for virtual
        # has:blockers. Blocker semantics (filigree-42045dd065): archived
        # blockers do not block dependents. (filigree-b55aa3191f): match by
        # ``(blocker.type, blocker.status)`` rather than status name alone, so
        # an ``incident.resolved`` (wip) is correctly seen as still-blocking.
        blocker_done_predicate = self._category_predicate_sql(
            "done",
            type_col="blocker.type",
            status_col="blocker.status",
            include_archived=True,
        )

        if status is not None:
            # Check if status is a category name (with aliases)
            category_aliases = {"in_progress": "wip", "closed": "done"}
            category_key = category_aliases.get(status, status)
            cat_pred: tuple[str, list[str]] | None = None
            if category_key in ("open", "wip", "done"):
                # filigree-b55aa3191f: compare (type, status) pairs so a state
                # name shared across types in different categories (e.g.
                # incident.resolved=wip vs debt_item.resolved=done) routes only
                # to the right type.
                pred_sql, pred_params = self._category_predicate_sql(category_key, type_col="i.type", status_col="i.status")
                if pred_params:
                    cat_pred = (pred_sql, pred_params)

            if cat_pred is not None:
                conditions.append(cat_pred[0])
                params.extend(cat_pred[1])
            else:
                # Literal state match (either not a category, or W7 empty guard)
                conditions.append("i.status = ?")
                params.append(status)
        if type is not None:
            conditions.append("i.type = ?")
            params.append(type)
        if priority is not None:
            conditions.append("i.priority = ?")
            params.append(priority)
        if parent_id is not None:
            conditions.append("i.parent_id = ?")
            params.append(parent_id)
        if assignee is not None:
            conditions.append("i.assignee = ?")
            params.append(assignee)

        # Label filters (array, AND logic)
        if label:
            for lbl in label:
                virtual = _resolve_virtual_label(lbl, negate=False, blocker_done_predicate=blocker_done_predicate)
                if virtual is not None:
                    sql_frag, vparams = virtual
                    conditions.append(f"({sql_frag})")
                    params.extend(vparams)
                else:
                    conditions.append("i.id IN (SELECT issue_id FROM labels WHERE label = ?)")
                    params.append(lbl)

        # Label prefix filter
        if label_prefix is not None:
            escaped = _escape_like_prefix(label_prefix)
            conditions.append("i.id IN (SELECT issue_id FROM labels WHERE label LIKE ? ESCAPE '\\')")
            params.append(escaped + "%")

        # Not-label filter
        if not_label is not None:
            if not_label.endswith(":"):
                # Prefix negation
                ns = not_label.rstrip(":")
                if ns in ("age", "has"):
                    msg = f"Cannot negate virtual namespace prefix {not_label!r} — use a specific value like {ns}:stale"
                    raise ValueError(msg)
                escaped = _escape_like_prefix(not_label)
                conditions.append("i.id NOT IN (SELECT issue_id FROM labels WHERE label LIKE ? ESCAPE '\\')")
                params.append(escaped + "%")
            else:
                virtual = _resolve_virtual_label(not_label, negate=True, blocker_done_predicate=blocker_done_predicate)
                if virtual is not None:
                    sql_frag, vparams = virtual
                    conditions.append(f"({sql_frag})")
                    params.extend(vparams)
                else:
                    conditions.append("i.id NOT IN (SELECT issue_id FROM labels WHERE label = ?)")
                    params.append(not_label)

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])
        rows = self.conn.execute(
            f"SELECT i.id FROM issues i{where} ORDER BY {order_by} LIMIT ? OFFSET ?",
            params,
        ).fetchall()

        return self._build_issues_batch([r["id"] for r in rows])

    def count_search_results(self, query: str) -> int:
        """Return the total number of issues matching a search query."""
        fts_query = _sanitize_fts_query(query)
        if not fts_query:
            pattern = _escape_like(query)
            row = self.conn.execute(
                "SELECT COUNT(*) AS cnt FROM issues WHERE title LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\'",
                (pattern, pattern),
            ).fetchone()
            return int(row["cnt"]) if row else 0
        try:
            row = self.conn.execute(
                "SELECT COUNT(*) AS cnt FROM issues i JOIN issues_fts ON issues_fts.rowid = i.rowid WHERE issues_fts MATCH ?",
                (fts_query,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc) and "no such module" not in str(exc):
                raise
            logger.warning(
                "FTS5 search unavailable (%s); falling back to LIKE. Performance may be degraded. Run 'filigree doctor' to check.",
                exc,
            )
            pattern = _escape_like(query)
            row = self.conn.execute(
                "SELECT COUNT(*) AS cnt FROM issues WHERE title LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\'",
                (pattern, pattern),
            ).fetchone()
        return int(row["cnt"]) if row else 0

    def search_issues(self, query: str, *, limit: int = 100, offset: int = 0) -> list[Issue]:
        """Search issues by title/description using FTS5, falling back to LIKE."""
        fts_query = _sanitize_fts_query(query)
        if not fts_query:
            pattern = _escape_like(query)
            rows = self.conn.execute(
                "SELECT id FROM issues WHERE title LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\' "
                "ORDER BY priority, created_at LIMIT ? OFFSET ?",
                (pattern, pattern, limit, offset),
            ).fetchall()
            return self._build_issues_batch([r["id"] for r in rows])
        try:
            rows = self.conn.execute(
                "SELECT i.id FROM issues i "
                "JOIN issues_fts ON issues_fts.rowid = i.rowid "
                "WHERE issues_fts MATCH ? "
                "ORDER BY issues_fts.rank LIMIT ? OFFSET ?",
                (fts_query, limit, offset),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc) and "no such module" not in str(exc):
                raise
            logging.getLogger(__name__).warning(
                "FTS5 search unavailable (%s); falling back to LIKE. Performance may be degraded. Run 'filigree doctor' to check.",
                exc,
            )
            pattern = _escape_like(query)
            rows = self.conn.execute(
                "SELECT id FROM issues WHERE title LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\' "
                "ORDER BY priority, created_at LIMIT ? OFFSET ?",
                (pattern, pattern, limit, offset),
            ).fetchall()
        return self._build_issues_batch([r["id"] for r in rows])
