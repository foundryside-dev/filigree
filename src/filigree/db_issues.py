"""IssuesMixin — issue CRUD, batch operations, search, and claiming.

Extracted from core.py as part of the module architecture split.
All methods access ``self.conn``, ``self.get_issue()``, etc. via
Python's MRO when composed into ``FiligreeDB``.
"""

from __future__ import annotations

import json
import logging
import re as _re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from filigree.db_base import (
    AGE_BUCKETS,
    DBMixinProtocol,
    _escape_like,
    _escape_like_chars,
    _in_immediate_tx,
    _now_iso,
    _retry_busy,
    _safe_json_loads,
)
from filigree.models import Issue
from filigree.templates import TransitionOption, TransitionResult, validate_field_pattern
from filigree.types.api import BatchFailure, ClaimConflictError, ErrorCode, InvalidTransitionError, TransitionHint, classify_value_error
from filigree.types.core import StatusCategory

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_REOPEN_CLEAR_FIELDS = frozenset({"close_reason"})
DEFAULT_CLAIM_LEASE_HOURS = 48


class _StartCandidateUnclaimableError(Exception):
    """Internal sentinel: ``_start_work_locked``'s claim phase failed.

    Wraps the original claim-conflict or vanished-row error from ``claim_issue`` so
    the ``start_next_work`` iterator can distinguish "try a different
    candidate" from a user-supplied error in the transition phase (which
    propagates unchanged). Outside the iteration loop, ``start_work``
    unwraps the sentinel and re-raises the original exception to preserve
    its composed start-work public API contract.
    """


class _ClaimCandidateVanishedError(Exception):
    """Internal sentinel for a candidate deleted between discovery and claim."""


_CLAIM_STATUS_MISMATCH_MARKER = "expected open-category state or wip-category handoff state"


def _is_claim_status_mismatch(exc: ValueError) -> bool:
    """Return True when claim_issue reports a stale/non-claimable candidate."""
    return _CLAIM_STATUS_MISMATCH_MARKER in str(exc)


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


def _transition_hints(options: list[TransitionOption]) -> list[TransitionHint]:
    """Return the compact structured transition hints used in error envelopes."""
    return [{"to": t.to, "category": t.category, "ready": t.ready} for t in options]


def _log_transition_enrichment_failure(issue_id: str, exc: Exception) -> None:
    """Log a best-effort transition-hint enrichment failure at the right level."""
    if isinstance(exc, KeyError):
        logger.debug("Issue %s disappeared while enriching invalid-transition error", issue_id, exc_info=True)
        return
    logger.warning("failed to enrich invalid-transition error for %s", issue_id, exc_info=True)


def _fields_for_reopen(fields: dict[str, Any]) -> dict[str, Any]:
    """Drop stale terminal-only fields when returning an issue to live work."""
    return {key: value for key, value in fields.items() if key not in _REOPEN_CLEAR_FIELDS}


def _claim_expiry(now: str, lease_hours: int = DEFAULT_CLAIM_LEASE_HOURS) -> str:
    """Return the expiry timestamp for a claim heartbeat."""
    return (datetime.fromisoformat(str(now)) + timedelta(hours=lease_hours)).isoformat()


def _parse_issue_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _validate_lease_hours(value: int, *, name: str = "lease_hours") -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        msg = f"{name} must be a positive integer"
        raise ValueError(msg)


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


def _check_expected_assignee(
    issue_id: str,
    expected_assignee: str | None,
    observed: str,
    *,
    actor: str = "",
) -> None:
    """Verify ``observed`` matches ``expected_assignee`` for a write-path precondition.

    When ``expected_assignee`` is omitted but ``actor`` is present and the issue
    is currently held, the actor becomes the expected holder by default
    (ADR-008). Actorless writes to held issues remain permissive for local/manual
    workflows. When set or derived, raises ``ClaimConflictError`` carrying the
    observed and expected assignee fields so the MCP / CLI / dashboard surfaces
    can render a structured ``CONFLICT`` envelope without message parsing.

    The compare normalises whitespace via ``_normalize_assignee`` so callers
    don't have to think about leading/trailing whitespace differences.

    See filigree-cb980eee0d (senior-user MCP review run d, P1.1) for the
    motivation: claim ownership was strictly enforced by heartbeat /
    reclaim / release_claim but completely ignored by update_issue /
    batch_update / close_issue / add_comment / add_label, so a non-claimant
    could overwrite a held issue silently. This helper closes that gap by
    letting all five write tools opt in to the same check.
    """
    current = (observed or "").strip()
    if expected_assignee is None:
        if not actor.strip() or not current:
            return
        expected_assignee = actor
    if expected_assignee is None:
        return
    expected = _normalize_assignee(expected_assignee)
    if current != expected:
        raise ClaimConflictError(issue_id, observed=current, expected=expected)


def _sanitize_fts_query(query: str) -> str:
    """Sanitize a search query for FTS5 MATCH syntax.

    Strips non-alphanumeric chars (keeping ``*`` and ``"``), quotes each token,
    and joins with AND for prefix matching.
    """
    sanitized = _re.sub(r'[^\w\s*"]', "", query)
    tokens = [t.replace('"', "") for t in sanitized.strip().split()]
    tokens = [t for t in tokens if t]
    return " AND ".join(f'"{t}"*' for t in tokens) if tokens else ""


# Punctuation that signals "agent self-tag" / "literal substring" intent.
# Other punctuation (e.g. @, #, $) is benign — strip-and-FTS is fine; only
# hyphens and bracket-like delimiters wreck cluster prefixes that agents
# rely on for self-discovery (``[mcp-review-e]``, ``cluster-foo``, etc.).
_FTS_LITERAL_HINT_RE = _re.compile(r"[-\[\](){}]")


def _is_fts_unavailable_error(exc: sqlite3.OperationalError) -> bool:
    """Return True when an FTS query failed because FTS5 is unavailable.

    Python's sqlite3 exposes both missing FTS tables and missing FTS5 modules
    as SQLITE_ERROR, so use the error code when SQLite provides it. Synthetic
    or older-driver OperationalErrors may not carry ``sqlite_errorcode``; keep
    a narrow compatibility fallback for the historical messages.
    """
    message = str(exc).lower()
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        if (code & 0xFF) != sqlite3.SQLITE_ERROR:
            return False
        return "no such table: issues_fts" in message or "no such module: fts" in message
    return "no such table: issues_fts" in message or "no such module: fts" in message


def _query_uses_literal_substring(query: str) -> bool:
    """Return True when ``query`` should bypass FTS in favour of a LIKE substring.

    The FTS5 tokeniser splits on hyphens and brackets, so a query like
    ``[mcp-review-e]`` is decomposed into single-letter tokens that FTS
    drops. When the raw query contains any of those literal-intent
    delimiters, this returns True so the caller can run a LIKE
    substring search on the raw query instead. Other punctuation (``@``,
    ``#``, ``$`` etc.) is left to the legacy strip-and-FTS path so the
    pre-existing ``"notification @#$%"`` behaviour is preserved.
    Senior-user MCP review run e P2.6.
    """
    return bool(query.strip()) and bool(_FTS_LITERAL_HINT_RE.search(query))


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
            sql = "SELECT id FROM issues WHERE type = ? AND json_valid(fields) AND json_extract(fields, ?) = ?"
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

    @_retry_busy()
    @_in_immediate_tx("create_issue")
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
        _skip_begin: bool = False,
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
        if parent_id:
            self._check_id_prefix(parent_id)
        if deps:
            for dep_id in deps:
                self._check_id_prefix(dep_id)
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
        claimed_at = now if assignee else None
        last_heartbeat_at = now if assignee else None
        claim_expires_at = _claim_expiry(now) if assignee else None
        fields = fields or {}

        # Determine initial state from template
        initial_state = self.templates.get_initial_state(type)

        self.conn.execute(
            "INSERT INTO issues (id, title, status, priority, type, parent_id, assignee, "
            "claimed_at, last_heartbeat_at, claim_expires_at, created_at, updated_at, description, notes, fields) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                issue_id,
                title,
                initial_state,
                priority,
                type,
                parent_id,
                assignee,
                claimed_at,
                last_heartbeat_at,
                claim_expires_at,
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
                    claimed_at=row["claimed_at"],
                    last_heartbeat_at=row["last_heartbeat_at"],
                    claim_expires_at=row["claim_expires_at"],
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

    @_retry_busy()
    @_in_immediate_tx("update_issue")
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
        expected_assignee: str | None = None,
        force_overwrite_corrupt: bool = False,
        backward: bool = False,
        _skip_begin: bool = False,
    ) -> Issue:
        """Update issue fields, workflow status, assignment, and parent links.

        The write runs in an IMMEDIATE transaction and applies a compare-and-swap
        guard when the issue is already assigned, so a concurrent reassignment
        cannot be silently overwritten. Status changes are validated against the
        issue type's workflow template; pass ``backward=True`` only for declared
        reverse/escape transitions such as force-close, reopen, or release.

        Args:
            issue_id: Issue identifier to mutate. The id prefix must belong to
                this project for write operations.
            title: Replacement title. Blank titles are rejected.
            status: Target workflow state. When it differs from the current
                state, template transition and field-gate validation run before
                any write.
            priority: Replacement priority value.
            assignee: Replacement assignee. Non-empty values refresh claim
                timestamps; empty values clear claim metadata.
            description: Replacement description.
            notes: Replacement notes.
            parent_id: Replacement parent id, or ``""`` to clear the parent.
            fields: Field delta to merge into the existing field object.
            actor: Audit identity. If ``expected_assignee`` is omitted and the
                issue is held, this also acts as the expected holder.
            expected_assignee: Optional compare-and-swap holder precondition.
                Use this when a caller has already observed ownership and wants
                stale ownership writes to fail with ``ClaimConflictError``.
            force_overwrite_corrupt: When the stored ``fields`` JSON is corrupt,
                refuse merges by default. Set this to replace the corrupt value
                entirely and record a ``corrupt_fields_overwritten`` event.
            backward: Validate a status change against declared reverse/escape
                transitions and audit the shortcut with ``transition_forced``.

        Returns:
            The freshly loaded issue. Soft transition data warnings are also
            copied onto ``Issue.data_warnings`` for callers that need to surface
            non-blocking template warnings.

        Raises:
            KeyError: The issue or requested parent issue does not exist.
            WrongProjectError: The write targets an id from another project.
            TypeError: ``fields`` is provided but is not a dictionary.
            ValueError: Input validation fails, including invalid status,
                priority, title, parent cycle, field pattern, uniqueness, or a
                corrupt-field merge without ``force_overwrite_corrupt``.
            ClaimConflictError: The observed or expected assignee no longer
                matches at validation or write time.
            InvalidTransitionError: The requested transition is not declared or
                is blocked by required fields. ``valid_transitions`` is included
                when template context is available.
        """
        self._check_id_prefix(issue_id)
        current = self.get_issue(issue_id)
        # Claim-aware precondition: explicit expected_assignee still behaves
        # like a compare-and-swap guard, while ADR-008 defaults the expected
        # holder to actor when actor is present and the issue is held.
        _check_expected_assignee(issue_id, expected_assignee, current.assignee, actor=actor)
        # 2.1.0 §0.1: capture the observed assignee at SELECT time. When
        # non-empty the WHERE clause below adds ``AND assignee = ?`` so a
        # concurrent reassignment between this read and the write below
        # closes the race instead of silently overwriting the new claimant's
        # state. Matches the pattern already used by claim_issue:1080,
        # heartbeat_work:1332, reclaim_issue:1438.
        _observed_assignee = current.assignee or ""
        now = _now_iso()

        # --- Validate all inputs BEFORE any writes to prevent partial commits ---
        if fields is not None and not isinstance(fields, dict):
            msg = "fields must be a dict"
            raise TypeError(msg)
        corrupt_fields_raw: Any | None = None
        if fields is not None and getattr(current.fields, "_filigree_corrupt", False):
            if not force_overwrite_corrupt:
                msg = "Refusing to merge fields: current value is corrupt; pass force_overwrite_corrupt=True to overwrite"
                raise ValueError(msg)
            raw_row = self.conn.execute("SELECT fields FROM issues WHERE id = ?", (issue_id,)).fetchone()
            corrupt_fields_raw = raw_row["fields"] if raw_row is not None else None
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

            # Atomic transition-with-fields: validate merged fields against target state.
            # ``backward=True`` routes through the declared reverse/escape edge
            # table, preserving auditability without the old skip-check bypass.
            merged_fields = {**current.fields}
            if fields is not None:
                merged_fields.update(fields)

            tpl = self.templates.get_type(current.type)
            if tpl is not None:
                _transition_result = self.templates.validate_transition(
                    current.type,
                    current.status,
                    status,
                    merged_fields,
                    backward=backward,
                )
                if not _transition_result.allowed:
                    valid_transitions = _transition_hints(self.templates.get_valid_transitions(current.type, current.status, merged_fields))
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
                    raise InvalidTransitionError(
                        current.type,
                        current.status,
                        to_state=status,
                        valid_transitions=valid_transitions,
                        message=msg,
                    )

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

        # Detect "close-with-reason-only": a status transition into a done-category
        # status whose fields delta consists entirely of close-only fields
        # (currently just ``close_reason``). When detected we collapse the close
        # into a single ``status_changed`` event carrying the reason in its
        # ``comment`` column, and skip the redundant ``fields_changed`` event so
        # ``undo_last`` reverses the close in one call instead of two. The fields
        # column is still written; consumers reading ``issue.fields.close_reason``
        # see no change.
        # Senior-user MCP review-f finding F2.
        _close_reason_only = False
        _close_reason_comment = ""
        if status is not None and status != current.status and fields is not None:
            _delta_keys = {k for k, v in fields.items() if current.fields.get(k) != v}
            if _delta_keys and _delta_keys.issubset(_REOPEN_CLEAR_FIELDS):
                _target_cat = self.templates.get_category(current.type, status) or self._infer_status_category(current.type, status)
                if _target_cat == "done":
                    _close_reason_only = True
                    _close_reason_comment = str(fields.get("close_reason", ""))

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

            # 2.1.0 §4.1: when the declared backward/escape workflow lane is used
            # the audit trail records a ``transition_forced`` event
            # alongside the ``status_changed`` so reviewers can find every
            # workflow shortcut. Sequenced before the status_changed event so
            # the chain is causally ordered when read top-to-bottom.
            if backward:
                self._record_event(
                    issue_id,
                    "transition_forced",
                    actor=actor,
                    old_value=current.status,
                    new_value=status,
                )

            self._record_event(
                issue_id,
                "status_changed",
                actor=actor,
                old_value=current.status,
                new_value=status,
                comment=_close_reason_comment,
            )
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
            if assignee:
                updates.extend(["claimed_at = ?", "last_heartbeat_at = ?", "claim_expires_at = ?"])
                params.extend([now, now, _claim_expiry(now)])
            else:
                updates.extend(["claimed_at = NULL", "last_heartbeat_at = NULL", "claim_expires_at = NULL"])

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
            merged = dict(fields) if corrupt_fields_raw is not None else {**current.fields, **fields}
            if corrupt_fields_raw is not None or merged != current.fields:
                if corrupt_fields_raw is not None:
                    self._record_event(
                        issue_id,
                        "corrupt_fields_overwritten",
                        actor=actor,
                        old_value=corrupt_fields_raw,
                        new_value=json.dumps(merged),
                    )
                elif not _close_reason_only:
                    # Skip the event for the close-with-reason-only path —
                    # the reason is already audit-trailed on the status_changed
                    # event's ``comment``. The fields column update still runs
                    # so ``issue.fields.close_reason`` remains readable.
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
            # §0.1 CAS guard: when an assignee was observed at SELECT time,
            # add ``AND assignee = ?`` so a concurrent reassignment fails
            # the UPDATE atomically. On rowcount==0 we re-read to
            # distinguish "row vanished" from "reassigned" and raise
            # ClaimConflictError (typed CONFLICT, not silent VALIDATION).
            where = "WHERE id = ?"
            if _observed_assignee:
                where += " AND assignee = ?"
                params.append(_observed_assignee)
            sql = f"UPDATE issues SET {', '.join(updates)} {where}"
            cursor = self.conn.execute(sql, params)
            if _observed_assignee and cursor.rowcount == 0:
                row = self.conn.execute("SELECT assignee FROM issues WHERE id = ?", (issue_id,)).fetchone()
                if row is None:
                    msg = f"Issue not found: {issue_id}"
                    raise KeyError(msg)
                new_assignee = row["assignee"] or ""
                msg = f"Cannot update {issue_id}: reassigned to '{new_assignee}' (expected '{_observed_assignee}')"
                raise ClaimConflictError(
                    issue_id,
                    observed=new_assignee,
                    expected=_observed_assignee,
                    message=msg,
                )

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
        expected_assignee: str | None = None,
        force: bool = False,
    ) -> Issue:
        """Close an issue.

        Routes through ``update_issue`` so the same template transition
        validator enforces ``triage → closed`` (and similar shortcuts)
        consistently across both close paths. Pass ``force=True`` to use
        the template's declared reverse/escape edge — this is the documented
        cleanup lane for flows that intentionally leave the normal workflow.

        When ``status`` is omitted, the close target defaults to the first
        done-category state for the type. If that default is not reachable
        from the current status, ``update_issue`` raises INVALID_TRANSITION
        and the caller must either pass ``status=`` explicitly to pick a
        done-category target, walk the workflow forward to a state from
        which the default is reachable, or pass ``force=True`` to use the
        declared escape edge. The close path never silently picks a done
        state on the caller's behalf — that hid intent (a feature in
        ``building`` that's actually shipped should not become ``deferred``
        just because that's the only reachable done-state).
        """
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
            # Default to first done-category state. If it isn't reachable
            # from current.status, update_issue's transition validator
            # raises INVALID_TRANSITION below and the caller must pick
            # explicitly or use force=True.
            _first_done = self.templates.get_first_state_of_category(current.type, "done")
            done_status = _first_done if _first_done is not None else "closed"

        # Merge close_reason into fields for the update call. Transition
        # validation (including hard-enforcement field gates) is delegated
        # to update_issue so close_issue and update_issue enforce the same
        # contract for the same target state — closing a bug from 'triage'
        # without a defined transition raises INVALID_TRANSITION just
        # like update_issue(status=closed) does, unless force=True.
        update_fields: dict[str, Any] = {}
        if fields:
            update_fields.update(fields)
        if reason:
            update_fields["close_reason"] = reason

        use_reverse_transition = force
        return self.update_issue(
            issue_id,
            status=done_status,
            fields=update_fields or None,
            actor=actor,
            expected_assignee=expected_assignee,
            backward=use_reverse_transition,
        )

    @_retry_busy()
    @_in_immediate_tx("reopen_issue")
    def reopen_issue(self, issue_id: str, *, actor: str = "") -> Issue:
        """Reopen a closed issue to the last non-done status before closure.

        The target comes from the most recent ``status_changed`` event whose
        old status is non-done and whose new status is done. If no such event is
        available, the issue type's initial state is used. Reopen routes through
        ``update_issue(backward=True)`` so the reverse/escape transition must be
        declared and any invalid transition carries normal ``valid_transitions``
        context. After the transition it clears ``closed_at`` and stale
        close-only fields such as ``close_reason``.

        Args:
            issue_id: Closed issue to reopen. The id prefix must belong to this
                project for write operations.
            actor: Audit identity recorded on transition, fields, and reopened
                events.

        Raises:
            KeyError: The issue does not exist.
            WrongProjectError: The write targets an id from another project.
            ValueError: The issue is not currently in a done-category state.
            InvalidTransitionError: The reverse transition to the computed
                reopen target is not declared or is blocked by field gates.
        """
        self._check_id_prefix(issue_id)
        current = self.get_issue(issue_id)
        if self._resolve_status_category(current.type, current.status) != "done":
            msg = f"Cannot reopen {issue_id}: status '{current.status}' is not in a done-category state"
            raise ValueError(msg)

        reopen_status = self._reopen_target_status(current)
        result = self.update_issue(
            issue_id,
            status=reopen_status,
            actor=actor,
            backward=True,
            _skip_begin=True,
        )
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
            result = self.get_issue(issue_id)
        self._record_event(issue_id, "reopened", actor=actor, old_value=current.status, new_value=reopen_status)
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

    @_retry_busy()
    @_in_immediate_tx("claim_issue")
    def claim_issue(self, issue_id: str, *, assignee: str, actor: str = "", _skip_begin: bool = False) -> Issue:
        """Atomically claim an open/wip-category issue with optimistic locking.

        Sets assignee only — does NOT change status. Agent uses update_issue
        to advance through the workflow after claiming. Wip-category issues can
        be claimed when they have been released and are currently unassigned,
        which preserves an atomic multi-agent handoff path after release_claim().

        Uses a single atomic UPDATE with WHERE guard to prevent race conditions
        where two agents try to claim the same issue concurrently.

        Composed callers (``start_work``, ``_claim_next_with_prior``) pass the
        decorator's ``_skip_begin=True`` so this method runs inside the outer
        IMMEDIATE transaction.
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
        now = _now_iso()
        claim_expires_at = _claim_expiry(now)
        cursor = self.conn.execute(
            f"UPDATE issues SET assignee = ?, claimed_at = COALESCE(claimed_at, ?), "
            f"last_heartbeat_at = ?, claim_expires_at = ?, updated_at = ? "
            f"WHERE id = ? AND status IN ({status_ph}) "
            f"AND (assignee = '' OR assignee IS NULL OR assignee = ?)",
            [assignee, now, now, claim_expires_at, now, issue_id, *claimable_states, assignee],
        )

        if cursor.rowcount == 0:
            # Figure out why it failed: wrong status or already claimed?
            current = self.conn.execute("SELECT status, assignee FROM issues WHERE id = ?", (issue_id,)).fetchone()
            if current is None:
                msg = f"Issue not found: {issue_id}"
                raise KeyError(msg)
            if current["assignee"] and current["assignee"] != assignee:
                msg = f"Cannot claim {issue_id}: already assigned to '{current['assignee']}'"
                raise ClaimConflictError(issue_id, observed=current["assignee"], expected=assignee, message=msg)
            msg = f"Cannot claim {issue_id}: status is '{current['status']}', expected open-category state or wip-category handoff state"
            raise ValueError(msg)

        self._record_event(issue_id, "claimed", actor=actor, old_value=old_assignee, new_value=assignee)
        return self.get_issue(issue_id)

    @_retry_busy()
    @_in_immediate_tx("release_claim")
    def release_claim(
        self,
        issue_id: str,
        *,
        actor: str = "",
        if_held: bool = False,
        expected_assignee: str | None = None,
        reason: str = "",
        revert_status: bool = True,
    ) -> Issue:
        """Release a claimed issue by clearing its assignee.

        Uses compare-and-swap on the observed assignee so a concurrent
        reassignment between read and UPDATE cannot be silently erased.

        When the issue is in a wip-category status, the call also reverts
        the status to the template-defined open-category predecessor so the
        issue rejoins ``get_ready`` discovery instead of being orphaned in
        wip with no assignee (filigree-cb980eee0d, P1.3 senior-user MCP
        review). Set ``revert_status=False`` to opt out and keep the
        legacy "release without status change" behaviour. The reverse
        target is resolved via ``templates.get_release_target``: prefer
        the open predecessor whose forward transition targets the current
        wip status; fall back to the template's initial_state when no
        direct predecessor exists. Types with no open-category state get
        no reverse target and stay in wip.

        When ``if_held`` is true, the call is idempotent only for already
        unassigned issues: those are returned unchanged. Claimed issues are
        released only when the observed assignee matches ``expected_assignee``;
        if no expected assignee is provided, ``actor`` is used as the expected
        holder. A claimed-by-someone-else mismatch raises
        ``ClaimConflictError`` rather than silently no-oping, so cleanup
        scripts cannot hide ownership surprises.

        Args:
            issue_id: Claimed issue to release. The id prefix must belong to
                this project for write operations.
            actor: Audit identity. Also becomes the expected holder for
                ``if_held=True`` when ``expected_assignee`` is omitted.
            if_held: Make already-unassigned issues idempotent no-ops, while
                still rejecting claims held by another assignee.
            expected_assignee: Optional expected holder for ``if_held=True``.
                A mismatch raises ``ClaimConflictError``.
            reason: Audit comment recorded on the ``released`` event.
            revert_status: When true, move wip-category issues back through the
                declared reverse/escape transition to the open predecessor, or
                to the template initial state when no direct predecessor exists.

        Raises:
            KeyError: The issue does not exist.
            WrongProjectError: The write targets an id from another project.
            ValueError: ``if_held`` is not boolean, the expected holder is blank,
                the issue is unassigned and ``if_held`` is false, or the claim
                was concurrently released.
            ClaimConflictError: The issue is held by someone other than the
                expected holder, or it is reassigned between read and write.
            InvalidTransitionError: The reverse status transition selected by
                ``revert_status`` is not declared or is blocked by field gates.
                ``valid_transitions`` is attached when template context is
                available.
        """
        if not isinstance(if_held, bool):
            msg = "if_held must be a boolean"
            raise ValueError(msg)
        expected_holder: str | None = None
        if if_held:
            expected_holder = _normalize_assignee(actor if expected_assignee is None else expected_assignee)
            if not expected_holder:
                msg = "expected_assignee or actor is required when if_held=True"
                raise ValueError(msg)
        self._check_id_prefix(issue_id)
        row = self.conn.execute("SELECT type, status, assignee, fields FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None:
            msg = f"Issue not found: {issue_id}"
            raise KeyError(msg)
        observed = row["assignee"] or ""
        if not observed:
            if if_held:
                return self.get_issue(issue_id)
            msg = f"Cannot release {issue_id}: no assignee set"
            raise ValueError(msg)
        if if_held and observed != expected_holder:
            msg = f"Cannot release {issue_id}: assigned to '{observed}' (expected '{expected_holder}')"
            raise ClaimConflictError(issue_id, observed=observed, expected=expected_holder or "", message=msg)

        target: str | None = None
        if revert_status:
            target = self.templates.get_release_target(row["type"], row["status"])
            if target == row["status"]:
                target = None
            if target is not None:
                fields = _safe_fields_json(row["fields"], issue_id)
                valid_transitions = _transition_hints(self.templates.get_valid_transitions(row["type"], row["status"], fields))
                try:
                    result = self.templates.validate_transition(
                        row["type"],
                        row["status"],
                        target,
                        fields,
                        backward=True,
                    )
                except InvalidTransitionError as exc:
                    if exc.valid_transitions is None:
                        raise exc.with_valid_transitions(valid_transitions) from exc
                    raise
                if not result.allowed:
                    if result.missing_fields:
                        missing_str = ", ".join(result.missing_fields)
                        msg = (
                            f"Cannot transition '{row['status']}' -> '{target}' for type "
                            f"'{row['type']}': missing required fields: {missing_str}"
                        )
                    else:
                        msg = (
                            f"Transition '{row['status']}' -> '{target}' is not allowed for type "
                            f"'{row['type']}'. Use get_valid_transitions() to see allowed transitions."
                        )
                    raise InvalidTransitionError(
                        row["type"],
                        row["status"],
                        to_state=target,
                        backward=True,
                        valid_transitions=valid_transitions,
                        message=msg,
                    )

        now = _now_iso()
        updates = [
            "assignee = ''",
            "claimed_at = NULL",
            "last_heartbeat_at = NULL",
            "claim_expires_at = NULL",
        ]
        params: list[Any] = []
        if target is not None:
            updates.extend(["status = ?", "closed_at = NULL"])
            params.append(target)
        updates.append("updated_at = ?")
        params.append(now)
        params.extend([issue_id, observed])
        cursor = self.conn.execute(
            f"UPDATE issues SET {', '.join(updates)} WHERE id = ? AND assignee = ?",
            params,
        )

        if cursor.rowcount == 0:
            current = self.conn.execute("SELECT assignee FROM issues WHERE id = ?", (issue_id,)).fetchone()
            if current is None:
                msg = f"Issue not found: {issue_id}"
                raise KeyError(msg)
            new_assignee = current["assignee"] or ""
            if not new_assignee:
                if if_held:
                    return self.get_issue(issue_id)
                msg = f"Cannot release {issue_id}: already released"
                raise ValueError(msg)
            if if_held:
                msg = f"Cannot release {issue_id}: assigned to '{new_assignee}' (expected '{expected_holder}')"
                raise ClaimConflictError(issue_id, observed=new_assignee, expected=expected_holder or "", message=msg)
            msg = f"Cannot release {issue_id}: reassigned to '{new_assignee}' (expected '{observed}')"
            raise ClaimConflictError(issue_id, observed=new_assignee, expected=observed, message=msg)

        self._record_event(issue_id, "released", actor=actor, old_value=observed, comment=reason.strip())
        if target is not None:
            self._record_event(issue_id, "transition_forced", actor=actor, old_value=row["status"], new_value=target)
            self._record_event(issue_id, "status_changed", actor=actor, old_value=row["status"], new_value=target)
        return self.get_issue(issue_id)

    def release_my_claims(
        self,
        *,
        actor: str,
        label: str | None = None,
        label_prefix: str | None = None,
        dry_run: bool = False,
        revert_status: bool = True,
        reason: str = "",
    ) -> tuple[list[Issue], list[BatchFailure]]:
        """Bulk-release every live claim held by ``actor``.

        Discovers all issues whose ``assignee == actor`` (optionally narrowed
        by ``label`` and/or ``label_prefix``) and releases each via
        ``release_claim(if_held=True)``. Done-category issues are skipped —
        a closed issue still carries assignee for audit but isn't an active
        claim, and releasing it would clobber the audit signal that ``X
        closed this``.

        Designed for end-of-session cleanup: a reviewer can tag their scratch
        with a cluster label (``cluster:mcp-review-h``) and call
        ``release_my_claims(actor="mcp-review-h", label_prefix="cluster:")``
        to drop everything they're holding in one shot. Senior-user MCP
        review run h F4.

        Args:
            actor: The agent identity whose claims should be released. Required.
            label: Restrict to issues carrying this exact label.
            label_prefix: Restrict to issues with a label starting with this
                prefix (must include trailing colon, e.g. ``"cluster:"``).
            dry_run: If True, return the set of issues that *would* be released
                without making any changes. Each item lands in ``succeeded[]``
                with its current state.
            revert_status: Forwarded to ``release_claim`` per-item; True (default)
                reverts wip-category issues to their open predecessor.
            reason: Audit reason recorded on each ``released`` event.

        Returns:
            ``(released, failures)`` — released is the list of issues whose
            claim was actually cleared (or would be in dry_run mode); failures
            is the list of per-issue errors (e.g. a concurrent reassignment
            that broke the compare-and-swap).
        """
        if not actor or not actor.strip():
            msg = "actor is required for release_my_claims"
            raise ValueError(msg)
        if label_prefix is not None and not label_prefix.endswith(":"):
            msg = f"label_prefix must include a trailing colon (got {label_prefix!r})"
            raise ValueError(msg)
        normalized_actor = actor.strip()

        # Discover candidates. assignee filter is exact match; label / label_prefix
        # are passed through to list_issues' own filter plumbing.
        candidates = self.list_issues(
            assignee=normalized_actor,
            label=label,
            label_prefix=label_prefix,
            limit=10_000_000,
        )
        # Exclude done-category — those aren't live claims, they're audit
        # trail. Releasing them would erase the "X closed this" signal.
        live = [issue for issue in candidates if self._resolve_status_category(issue.type, issue.status) != "done"]

        from filigree.core import WrongProjectError

        released: list[Issue] = []
        failures: list[BatchFailure] = []
        for issue in live:
            if dry_run:
                released.append(issue)
                continue
            try:
                result = self.release_claim(
                    issue.id,
                    actor=normalized_actor,
                    if_held=True,
                    revert_status=revert_status,
                    reason=reason,
                )
                released.append(result)
            except WrongProjectError:
                raise
            except (ValueError, KeyError) as exc:
                msg = str(exc)
                if isinstance(exc, KeyError):
                    code = ErrorCode.NOT_FOUND
                elif isinstance(exc, ClaimConflictError):
                    code = ErrorCode.CONFLICT
                else:
                    code = classify_value_error(msg)
                failures.append(BatchFailure(id=issue.id, error=msg, code=code))
        return released, failures

    @_retry_busy()
    @_in_immediate_tx("heartbeat_work")
    def heartbeat_work(
        self,
        issue_id: str,
        *,
        actor: str = "",
        expected_assignee: str | None = None,
        lease_hours: int = DEFAULT_CLAIM_LEASE_HOURS,
    ) -> Issue:
        """Refresh liveness metadata for a claimed, non-done issue.

        Updates ``last_heartbeat_at``, ``claim_expires_at``, and ``updated_at``
        only if the assignee observed before the write still owns the issue.

        Args:
            issue_id: Claimed issue to heartbeat. The id prefix must belong to
                this project for write operations.
            actor: Audit identity. If ``expected_assignee`` is omitted, this is
                also accepted as the expected holder.
            expected_assignee: Optional explicit holder precondition.
            lease_hours: Number of hours from now until the refreshed claim
                expires.

        Raises:
            KeyError: The issue does not exist.
            WrongProjectError: The write targets an id from another project.
            ValueError: The lease value is invalid, the issue is unassigned,
                the expected holder is blank, or the issue is already done.
            ClaimConflictError: The issue is held by someone other than the
                expected holder, or it is reassigned between read and write.
        """
        _validate_lease_hours(lease_hours)
        self._check_id_prefix(issue_id)
        row = self.conn.execute("SELECT type, status, assignee FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None:
            msg = f"Issue not found: {issue_id}"
            raise KeyError(msg)
        observed = row["assignee"] or ""
        if not observed:
            msg = f"Cannot heartbeat {issue_id}: no assignee set"
            raise ValueError(msg)
        expected_holder = ""
        if expected_assignee is not None or actor:
            expected_holder = _normalize_assignee(actor if expected_assignee is None else expected_assignee)
            if not expected_holder:
                msg = "expected_assignee or actor is required"
                raise ValueError(msg)
        if expected_holder and observed != expected_holder:
            msg = f"Cannot heartbeat {issue_id}: assigned to '{observed}' (expected '{expected_holder}')"
            raise ClaimConflictError(issue_id, observed=observed, expected=expected_holder, message=msg)
        if self._resolve_status_category(row["type"], row["status"]) == "done":
            msg = f"Cannot heartbeat {issue_id}: status is '{row['status']}'"
            raise ValueError(msg)

        now = _now_iso()
        claim_expires_at = _claim_expiry(now, lease_hours)
        cursor = self.conn.execute(
            "UPDATE issues SET last_heartbeat_at = ?, claim_expires_at = ?, updated_at = ? WHERE id = ? AND assignee = ?",
            (now, claim_expires_at, now, issue_id, observed),
        )
        if cursor.rowcount == 0:
            current = self.conn.execute("SELECT assignee FROM issues WHERE id = ?", (issue_id,)).fetchone()
            if current is None:
                msg = f"Issue not found: {issue_id}"
                raise KeyError(msg)
            new_assignee = current["assignee"] or ""
            msg = f"Cannot heartbeat {issue_id}: reassigned to '{new_assignee}' (expected '{observed}')"
            raise ClaimConflictError(issue_id, observed=new_assignee, expected=observed, message=msg)
        self._record_event(
            issue_id,
            "heartbeat",
            actor=actor,
            old_value=observed,
            new_value=claim_expires_at,
        )
        return self.get_issue(issue_id)

    def get_stale_claims(
        self,
        *,
        stale_after_hours: int = DEFAULT_CLAIM_LEASE_HOURS,
        expires_within_hours: int | None = None,
    ) -> list[Issue]:
        """Return assigned, non-done issues whose ownership appears abandoned or near expiry.

        Modern rows with parseable ``claim_expires_at`` have their expiry
        check pushed into the WHERE clause so a polling agent that runs
        ``get_stale_claims`` every N seconds does not scan every assigned
        row in Python (2.1.0 §2.3). The legacy fallback — heartbeat /
        claimed / updated timestamp against ``stale_after_hours`` — still
        runs in Python for rows whose ``claim_expires_at`` is NULL or
        malformed.
        """
        _validate_lease_hours(stale_after_hours)
        if expires_within_hours is not None:
            _validate_lease_hours(expires_within_hours, name="expires_within_hours")
        now = datetime.now(UTC)
        cutoff = now - timedelta(hours=stale_after_hours)
        # SQL cutoff: matches expired-now and (if requested) near-expiry rows.
        expiry_cutoff_iso = (now + timedelta(hours=expires_within_hours or 0)).isoformat()

        pred_sql, pred_params = self._category_predicate_sql("done", type_col="i.type", status_col="i.status")
        rows = self.conn.execute(
            "SELECT i.id, i.claim_expires_at, i.last_heartbeat_at, i.claimed_at, i.updated_at "
            "FROM issues i "
            "WHERE COALESCE(i.assignee, '') != '' "
            f"AND NOT ({pred_sql}) "
            "AND ("
            "  i.claim_expires_at IS NULL"
            "  OR datetime(i.claim_expires_at) IS NULL"
            "  OR datetime(i.claim_expires_at) <= datetime(?)"
            ") "
            "ORDER BY i.priority ASC, i.created_at ASC, i.id ASC",
            [*pred_params, expiry_cutoff_iso],
        ).fetchall()

        stale_ids: list[str] = []
        for row in rows:
            # Modern parseable rows: the SQL filter already says this row is
            # stale-or-near-expiry, so include unconditionally. Malformed
            # non-NULL expiry text is intentionally left for the legacy
            # timestamp fallback below.
            if _parse_issue_timestamp(row["claim_expires_at"]) is not None:
                stale_ids.append(row["id"])
                continue

            # Legacy fallback for rows predating the claim_expires_at column,
            # plus malformed non-NULL expiry text.
            basis = (
                _parse_issue_timestamp(row["last_heartbeat_at"])
                or _parse_issue_timestamp(row["claimed_at"])
                or _parse_issue_timestamp(row["updated_at"])
            )
            if basis is None or basis <= cutoff:
                stale_ids.append(row["id"])

        return self._build_issues_batch(stale_ids)

    @_retry_busy()
    @_in_immediate_tx("reclaim_issue")
    def reclaim_issue(
        self,
        issue_id: str,
        *,
        assignee: str,
        expected_assignee: str,
        reason: str,
        actor: str = "",
        lease_hours: int = DEFAULT_CLAIM_LEASE_HOURS,
    ) -> Issue:
        """Atomically transfer a stale claim to a new assignee.

        The transfer succeeds only when ``expected_assignee`` still matches the
        observed holder at write time. On success it sets ``assignee``,
        ``claimed_at``, ``last_heartbeat_at``, ``claim_expires_at``, and
        ``updated_at`` together and records a ``reclaimed`` event.

        Args:
            issue_id: Claimed issue to reclaim. The id prefix must belong to
                this project for write operations.
            assignee: New holder for the claim. Blank values are rejected.
            expected_assignee: Holder that the caller believes currently owns
                the issue. This is the compare-and-swap precondition.
            reason: Non-empty audit reason recorded on the ``reclaimed`` event.
            actor: Audit identity performing the reclaim.
            lease_hours: Number of hours from now until the new claim expires.

        Raises:
            KeyError: The issue does not exist.
            WrongProjectError: The write targets an id from another project.
            ValueError: ``assignee``, ``expected_assignee``, ``reason``, or
                ``lease_hours`` is invalid, or the issue is already done.
            ClaimConflictError: The current holder does not match
                ``expected_assignee`` at validation or write time.
        """
        _validate_lease_hours(lease_hours)
        assignee = _normalize_assignee(assignee)
        expected_assignee = _normalize_assignee(expected_assignee)
        if not assignee:
            msg = "Assignee cannot be empty"
            raise ValueError(msg)
        if not expected_assignee:
            msg = "expected_assignee cannot be empty"
            raise ValueError(msg)
        reason = reason.strip()
        if not reason:
            msg = "reason cannot be empty"
            raise ValueError(msg)
        self._check_id_prefix(issue_id)
        row = self.conn.execute("SELECT type, status, assignee FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None:
            msg = f"Issue not found: {issue_id}"
            raise KeyError(msg)
        observed = row["assignee"] or ""
        if observed != expected_assignee:
            msg = f"Cannot reclaim {issue_id}: assigned to '{observed}' (expected '{expected_assignee}')"
            raise ClaimConflictError(issue_id, observed=observed, expected=expected_assignee, message=msg)
        if self._resolve_status_category(row["type"], row["status"]) == "done":
            msg = f"Cannot reclaim {issue_id}: status is '{row['status']}'"
            raise ValueError(msg)

        now = _now_iso()
        claim_expires_at = _claim_expiry(now, lease_hours)
        cursor = self.conn.execute(
            "UPDATE issues SET assignee = ?, claimed_at = ?, last_heartbeat_at = ?, "
            "claim_expires_at = ?, updated_at = ? WHERE id = ? AND assignee = ?",
            (assignee, now, now, claim_expires_at, now, issue_id, expected_assignee),
        )
        if cursor.rowcount == 0:
            current = self.conn.execute("SELECT assignee FROM issues WHERE id = ?", (issue_id,)).fetchone()
            if current is None:
                msg = f"Issue not found: {issue_id}"
                raise KeyError(msg)
            new_assignee = current["assignee"] or ""
            msg = f"Cannot reclaim {issue_id}: reassigned to '{new_assignee}' (expected '{expected_assignee}')"
            raise ClaimConflictError(issue_id, observed=new_assignee, expected=expected_assignee, message=msg)
        self._record_event(
            issue_id,
            "reclaimed",
            actor=actor,
            old_value=expected_assignee,
            new_value=assignee,
            comment=reason,
        )
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
        _skip_begin: bool = False,
    ) -> tuple[Issue, str] | None:
        """Internal: claim_next that also returns the candidate's prior assignee.

        Returns ``(claimed_issue, prior_assignee)`` so composed callers
        (start_next_work) can distinguish a freshly-acquired claim from a
        same-assignee re-claim and decide whether a compensating release is
        appropriate. ``prior_assignee`` is read transactionally just before
        ``claim_issue`` so a concurrent reassignment landing between the read
        and the UPDATE will surface as the same race ``claim_issue`` already
        handles (skip and continue).

        ``_skip_begin`` is forwarded to the inner ``claim_issue`` decorator
        stack: composed callers (``start_next_work``) own the outer IMMEDIATE
        transaction and pass ``True`` so the inner claim does not commit
        independently.
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
                if row is None:
                    raise _ClaimCandidateVanishedError(issue.id)
                prior_assignee = row["assignee"] or ""
                try:
                    claimed = self.claim_issue(issue.id, assignee=assignee, actor=actor or assignee, _skip_begin=_skip_begin)
                except ClaimConflictError:
                    raise
                except KeyError as exc:
                    raise _ClaimCandidateVanishedError(issue.id) from exc
                except ValueError as exc:
                    if _is_claim_status_mismatch(exc):
                        raise _ClaimCandidateVanishedError(issue.id) from exc
                    raise
            except (ClaimConflictError, _ClaimCandidateVanishedError) as exc:
                skipped += 1
                logger.debug("claim_next: skipping %s: %s", issue.id, exc)
                continue  # Claim race or deleted issue
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

        ``target_status`` resolution runs lock-free in this public wrapper;
        the writer lock is acquired only inside ``_start_work_locked``, which
        composes ``claim_issue`` + ``update_issue`` under one
        ``BEGIN IMMEDIATE``. The held-writer window is limited to claim UPDATE
        + status UPDATE + event INSERTs + COMMIT; template lookup happens
        before the transaction opens.

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
        self._check_id_prefix(issue_id)
        if target_status is None:
            target_status = self._resolve_start_target(issue_id)
        try:
            return self._start_work_locked(
                issue_id,
                assignee=assignee,
                target_status=target_status,
                actor=actor,
            )
        except _StartCandidateUnclaimableError as exc:
            # Public API contract: surface the underlying claim error.
            raise exc.__cause__ from None  # type: ignore[misc]

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

        Candidate discovery (``get_ready``) and per-candidate
        ``target_status`` resolution run lock-free; only the per-candidate
        claim+transition composite acquires a writer lock via
        ``_start_work_locked``. On a per-candidate race (claim conflict,
        status mismatch, deleted issue), the iteration continues to the next
        candidate without holding any lock.

        Returns ``None`` if no ready issue matches the filters.

        Tie-break ordering inherits from ``claim_next``: priority asc,
        created_at asc, issue_id asc.
        """
        if not assignee or not assignee.strip():
            msg = "Assignee cannot be empty"
            raise ValueError(msg)
        actor = actor or assignee

        # Discover candidates outside any writer transaction.
        ready = self.get_ready()

        skipped = 0
        first_explicit_transition_error: ValueError | None = None
        for issue in ready:
            if type_filter is not None and issue.type != type_filter:
                continue
            if priority_min is not None and issue.priority < priority_min:
                continue
            if priority_max is not None and issue.priority > priority_max:
                continue

            # Resolve target_status per-candidate, lock-free. Template
            # errors (no template, AmbiguousTransitionError, no reachable
            # wip status) propagate — they signal a programmer error or
            # workflow mismatch that retrying a different candidate
            # cannot fix.
            if target_status is None:
                tpl = self.templates.get_type(issue.type)
                if tpl is None:
                    from filigree.types.api import InvalidTransitionError

                    raise InvalidTransitionError(issue.type, issue.status)
                this_target = tpl.reachable_working_status(issue.status)
            else:
                this_target = target_status

            try:
                return self._start_work_locked(
                    issue.id,
                    assignee=assignee,
                    target_status=this_target,
                    actor=actor,
                )
            except _StartCandidateUnclaimableError as exc:
                # Race / status mismatch / deleted — try next candidate.
                skipped += 1
                logger.debug("start_next_work: skipping %s: %s", issue.id, exc.__cause__)
                if (
                    target_status is not None
                    and first_explicit_transition_error is None
                    and isinstance(exc.__cause__, ValueError)
                    and classify_value_error(str(exc.__cause__)) == ErrorCode.INVALID_TRANSITION
                ):
                    first_explicit_transition_error = exc.__cause__
                continue

        if first_explicit_transition_error is not None:
            raise first_explicit_transition_error
        if skipped:
            logger.warning("start_next_work: all %d candidate(s) failed to claim for '%s'", skipped, assignee)
        return None

    def _resolve_start_target(self, issue_id: str) -> str:
        """Resolve the default wip-target status for ``issue_id`` lock-free.

        Reads the issue's type and current status, then asks the template
        for the unique reachable wip-category status. Surfaces
        ``InvalidTransitionError`` / ``AmbiguousTransitionError`` for
        callers that did not pass an explicit ``target_status``.
        """
        row = self.conn.execute("SELECT type, status FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None:
            msg = f"Issue not found: {issue_id}"
            raise KeyError(msg)
        tpl = self.templates.get_type(row["type"])
        if tpl is None:
            from filigree.types.api import InvalidTransitionError

            raise InvalidTransitionError(row["type"], row["status"])
        return tpl.reachable_working_status(row["status"])

    @_retry_busy()
    @_in_immediate_tx("start_work")
    def _start_work_locked(
        self,
        issue_id: str,
        *,
        assignee: str,
        target_status: str,
        actor: str,
    ) -> Issue:
        """Private critical section for ``start_work`` / ``start_next_work``.

        The ``@_in_immediate_tx`` decorator wraps a tight claim+update
        composite with no template lookups or candidate discovery, so the
        writer lock is held only across the SQL writes. On exception the
        decorator rolls back both the claim and its audit event.

        Claim-phase failures (race, status mismatch, deleted issue) and
        transition-class failures are repackaged as
        ``_StartCandidateUnclaimableError`` so the ``start_next_work``
        iterator can try another candidate. ``start_work`` unwraps the
        sentinel to preserve its public error contract; ``start_next_work``
        re-raises an explicit target-status transition error only after no
        compatible candidate succeeds.
        """
        try:
            self.claim_issue(issue_id, assignee=assignee, actor=actor, _skip_begin=True)
        except (ClaimConflictError, KeyError) as exc:
            raise _StartCandidateUnclaimableError(issue_id) from exc
        except ValueError as exc:
            if _is_claim_status_mismatch(exc):
                raise _StartCandidateUnclaimableError(issue_id) from exc
            raise
        try:
            return self.update_issue(issue_id, status=target_status, actor=actor, _skip_begin=True)
        except InvalidTransitionError as exc:
            raise _StartCandidateUnclaimableError(issue_id) from exc
        except ValueError as exc:
            if classify_value_error(str(exc)) == ErrorCode.INVALID_TRANSITION:
                raise _StartCandidateUnclaimableError(issue_id) from exc
            raise

    def _batch_with_transition_errors(
        self,
        issue_ids: list[str],
        action: Callable[[str], Issue],
    ) -> tuple[list[Issue], list[BatchFailure]]:
        """Run *action(issue_id)* per item with transition-enriched error handling.

        ``WrongProjectError`` aborts the whole batch envelope-level rather
        than producing N per-item validation failures (2.1.0 §0.4). The
        foreign-prefix surface is structurally distinct from "this issue
        is missing in our DB" — silently masking it as N per-item errors
        is exactly the silent foreign-DB-mutation surface `core.py`'s
        anchor discovery was hardened against. Pre-flighting every id
        through ``_check_id_prefix`` before any per-item work commits
        ensures the abort fires before partial state lands; the helper
        already raises ``WrongProjectError`` on a foreign prefix.
        """
        from filigree.core import WrongProjectError

        _validate_string_list(issue_ids, "issue_ids")
        for issue_id in issue_ids:
            self._check_id_prefix(issue_id)
        results: list[Issue] = []
        errors: list[BatchFailure] = []
        for issue_id in issue_ids:
            try:
                results.append(action(issue_id))
            except WrongProjectError:
                raise
            except KeyError:
                errors.append(BatchFailure(id=issue_id, error=f"Not found: {issue_id}", code=ErrorCode.NOT_FOUND))
            except ValueError as e:
                msg = str(e)
                if isinstance(e, ClaimConflictError):
                    code = ErrorCode.CONFLICT
                elif isinstance(e, InvalidTransitionError):
                    code = ErrorCode.INVALID_TRANSITION
                else:
                    code = classify_value_error(msg)
                err = BatchFailure(id=issue_id, error=str(e), code=code)
                if isinstance(e, InvalidTransitionError) and e.valid_transitions is not None:
                    err["valid_transitions"] = e.valid_transitions
                elif code == ErrorCode.INVALID_TRANSITION:
                    try:
                        transitions = self.get_valid_transitions(issue_id)
                        err["valid_transitions"] = _transition_hints(transitions)
                    except Exception as exc:
                        _log_transition_enrichment_failure(issue_id, exc)
                errors.append(err)
        return results, errors

    def batch_close(
        self,
        issue_ids: list[str],
        *,
        reason: str = "",
        actor: str = "",
        expected_assignee: str | None = None,
        force: bool = False,
    ) -> tuple[list[Issue], list[BatchFailure]]:
        """Close multiple issues with per-item error handling. Returns (closed, errors).

        ``expected_assignee`` is applied to every issue in the batch as a
        single shared precondition. When omitted and ``actor`` is present,
        held issues default the expected holder to actor (ADR-008).

        ``force=True`` uses the template reverse/escape transition on every
        item — same escape hatch as ``close_issue(force=True)``. Use only
        for cleanup flows that intentionally leave the normal workflow.
        Senior-user MCP review run e P1.3.
        """
        return self._batch_with_transition_errors(
            issue_ids,
            lambda iid: self.close_issue(
                iid,
                reason=reason,
                actor=actor,
                expected_assignee=expected_assignee,
                force=force,
            ),
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
        expected_assignee: str | None = None,
    ) -> tuple[list[Issue], list[BatchFailure]]:
        """Update multiple issues with the same changes. Returns (updated, errors).

        ``expected_assignee`` is applied to every issue in the batch as a
        single shared precondition. When omitted and ``actor`` is present,
        held issues default the expected holder to actor (ADR-008).
        """
        return self._batch_with_transition_errors(
            issue_ids,
            lambda iid: self.update_issue(
                iid,
                status=status,
                priority=priority,
                assignee=assignee,
                fields=fields,
                actor=actor,
                expected_assignee=expected_assignee,
            ),
        )

    def batch_add_label(
        self,
        issue_ids: list[str],
        *,
        label: str,
        actor: str = "",
        expected_assignee: str | None = None,
    ) -> tuple[list[dict[str, str]], list[BatchFailure]]:
        """Add the same label to multiple issues. Returns (labeled, errors).

        ``expected_assignee`` is applied per-item. When omitted and ``actor``
        is present, held issues default the expected holder to actor (ADR-008).
        ``WrongProjectError`` aborts the whole batch envelope-level (2.1.0 §0.4).
        """
        from filigree.core import WrongProjectError

        _validate_string_list(issue_ids, "issue_ids")
        if not isinstance(label, str):
            msg = "label must be a string"
            raise TypeError(msg)
        for issue_id in issue_ids:
            self._check_id_prefix(issue_id)

        results: list[dict[str, str]] = []
        errors: list[BatchFailure] = []
        for issue_id in issue_ids:
            try:
                self.get_issue(issue_id)
                added, _canonical, _replaced = self.add_label(
                    issue_id,
                    label,
                    actor=actor,
                    expected_assignee=expected_assignee,
                )
                results.append({"id": issue_id, "status": "added" if added else "already_exists"})
            except WrongProjectError:
                raise
            except KeyError:
                errors.append(BatchFailure(id=issue_id, error=f"Not found: {issue_id}", code=ErrorCode.NOT_FOUND))
            except ValueError as e:
                code = ErrorCode.CONFLICT if isinstance(e, ClaimConflictError) else classify_value_error(str(e))
                errors.append(BatchFailure(id=issue_id, error=str(e), code=code))
        return results, errors

    def batch_remove_label(
        self,
        issue_ids: list[str],
        *,
        label: str,
        actor: str = "",
        expected_assignee: str | None = None,
    ) -> tuple[list[dict[str, str]], list[BatchFailure]]:
        """Remove the same label from multiple issues. Returns (removed, errors).

        ``expected_assignee`` is applied per-item. When omitted and ``actor``
        is present, held issues default the expected holder to actor (ADR-008).
        ``WrongProjectError`` aborts the whole batch envelope-level (2.1.0 §0.4).
        """
        from filigree.core import WrongProjectError

        _validate_string_list(issue_ids, "issue_ids")
        if not isinstance(label, str):
            msg = "label must be a string"
            raise TypeError(msg)
        for issue_id in issue_ids:
            self._check_id_prefix(issue_id)

        results: list[dict[str, str]] = []
        errors: list[BatchFailure] = []
        for issue_id in issue_ids:
            try:
                self.get_issue(issue_id)
                removed, _canonical = self.remove_label(
                    issue_id,
                    label,
                    actor=actor,
                    expected_assignee=expected_assignee,
                )
                results.append({"id": issue_id, "status": "removed" if removed else "not_found"})
            except WrongProjectError:
                raise
            except KeyError:
                errors.append(BatchFailure(id=issue_id, error=f"Not found: {issue_id}", code=ErrorCode.NOT_FOUND))
            except ValueError as e:
                code = ErrorCode.CONFLICT if isinstance(e, ClaimConflictError) else classify_value_error(str(e))
                errors.append(BatchFailure(id=issue_id, error=str(e), code=code))
        return results, errors

    def batch_add_comment(
        self,
        issue_ids: list[str],
        *,
        text: str,
        author: str = "",
        expected_assignee: str | None = None,
    ) -> tuple[list[dict[str, str | int]], list[BatchFailure]]:
        """Add the same comment to multiple issues. Returns (commented, errors).

        ``expected_assignee`` is applied per-item. When omitted and ``author``
        is present, held issues default the expected holder to author (ADR-008).
        ``WrongProjectError`` aborts the whole batch envelope-level (2.1.0 §0.4).
        """
        from filigree.core import WrongProjectError

        _validate_string_list(issue_ids, "issue_ids")
        if not isinstance(text, str):
            msg = "text must be a string"
            raise TypeError(msg)
        if not isinstance(author, str):
            msg = "author must be a string"
            raise TypeError(msg)
        for issue_id in issue_ids:
            self._check_id_prefix(issue_id)

        results: list[dict[str, str | int]] = []
        errors: list[BatchFailure] = []
        for issue_id in issue_ids:
            try:
                self.get_issue(issue_id)
                comment_id = self.add_comment(issue_id, text, author=author, expected_assignee=expected_assignee)
                results.append({"id": issue_id, "comment_id": comment_id})
            except WrongProjectError:
                raise
            except KeyError:
                errors.append(BatchFailure(id=issue_id, error=f"Not found: {issue_id}", code=ErrorCode.NOT_FOUND))
            except ValueError as e:
                code = ErrorCode.CONFLICT if isinstance(e, ClaimConflictError) else classify_value_error(str(e))
                errors.append(BatchFailure(id=issue_id, error=str(e), code=code))
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
        fts_query = "" if _query_uses_literal_substring(query) else _sanitize_fts_query(query)
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
            if not _is_fts_unavailable_error(exc):
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

    def search_issues(
        self,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
        status_category: StatusCategory | None = None,
    ) -> list[Issue]:
        """Search issues by title/description using FTS5, falling back to LIKE.

        When ``query`` contains punctuation that FTS5 would tokenise away
        (hyphens, brackets, etc.), this falls back to a LIKE substring
        match on the raw query so agents can find self-tagged work
        prefixed with ``[cluster-foo]`` or ``mcp-review-e``. Pure
        word-token queries continue to use FTS5 for ranked relevance.
        Senior-user MCP review run e P2.6.

        ``status_category`` (``"open"`` / ``"wip"`` / ``"done"``) optionally
        restricts the result set so agents searching for live work don't
        get archived results back. Senior-user MCP review run e P2.7.
        """
        category_sql = ""
        category_params: list[str] = []
        if status_category is not None:
            if status_category not in ("open", "wip", "done"):
                msg = f"Invalid status_category: {status_category!r}. Valid: open, wip, done."
                raise ValueError(msg)
            category_sql, category_params = self._category_predicate_sql(
                status_category,
                type_col="i.type",
                status_col="i.status",
                include_archived=status_category == "done",
            )

        use_like_substring = _query_uses_literal_substring(query)
        fts_query = "" if use_like_substring else _sanitize_fts_query(query)

        rows: list[Any]
        if not fts_query:
            pattern = _escape_like(query)
            where = "(i.title LIKE ? ESCAPE '\\' OR i.description LIKE ? ESCAPE '\\')"
            params: list[Any] = [pattern, pattern]
            if category_sql:
                where = f"{where} AND ({category_sql})"
                params.extend(category_params)
            params.extend([limit, offset])
            rows = self.conn.execute(
                f"SELECT i.id, i.type, i.status FROM issues i WHERE {where} ORDER BY priority, created_at LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        else:
            try:
                where = "issues_fts MATCH ?"
                params = [fts_query]
                if category_sql:
                    where = f"{where} AND ({category_sql})"
                    params.extend(category_params)
                params.extend([limit, offset])
                rows = self.conn.execute(
                    "SELECT i.id, i.type, i.status FROM issues i "
                    "JOIN issues_fts ON issues_fts.rowid = i.rowid "
                    f"WHERE {where} "
                    "ORDER BY issues_fts.rank LIMIT ? OFFSET ?",
                    params,
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if not _is_fts_unavailable_error(exc):
                    raise
                logging.getLogger(__name__).warning(
                    "FTS5 search unavailable (%s); falling back to LIKE. Performance may be degraded. Run 'filigree doctor' to check.",
                    exc,
                )
                pattern = _escape_like(query)
                where = "(i.title LIKE ? ESCAPE '\\' OR i.description LIKE ? ESCAPE '\\')"
                params = [pattern, pattern]
                if category_sql:
                    where = f"{where} AND ({category_sql})"
                    params.extend(category_params)
                params.extend([limit, offset])
                rows = self.conn.execute(
                    f"SELECT i.id, i.type, i.status FROM issues i WHERE {where} ORDER BY priority, created_at LIMIT ? OFFSET ?",
                    params,
                ).fetchall()

        return self._build_issues_batch([r["id"] for r in rows])
