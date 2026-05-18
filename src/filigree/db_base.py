"""Shared utilities, types, and Protocol for DB mixins."""

from __future__ import annotations

import functools
import inspect
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ParamSpec, Protocol, TypeVar, cast

from filigree.models import FileRecord, Issue
from filigree.types.core import AssocType, ISOTimestamp, ScanRunStatus, StatusCategory
from filigree.types.events import EventType

if TYPE_CHECKING:
    from filigree.templates import TemplateRegistry, TransitionOption
    from filigree.types.api import BatchFailure
    from filigree.types.core import ObservationDict, ObservationLinkDict, ScanFindingDict
    from filigree.types.files import ScanRunDict
    from filigree.types.planning import CommentRecord

logger = logging.getLogger(__name__)
_SQLITE_TRANSIENT_LOCK_CODES = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}

# Shared internal API — used by DB mixins across modules.
__all__ = [
    "AGE_BUCKETS",
    "DBMixinProtocol",
    "StatusCategory",
    "_begin_immediate",
    "_escape_like",
    "_escape_like_chars",
    "_in_immediate_tx",
    "_normalize_iso_to_utc",
    "_now_iso",
    "_retry_busy",
    "_safe_json_loads",
]

_P = ParamSpec("_P")
_R = TypeVar("_R")

# Virtual label dispatch — explicit allowlist, no prefix matching
AGE_BUCKETS: dict[str, tuple[int, int]] = {
    "fresh": (0, 7),
    "recent": (7, 30),
    "aging": (30, 90),
    "stale": (90, 180),
    "ancient": (180, 999999),
}


def _now_iso() -> ISOTimestamp:
    return ISOTimestamp(datetime.now(UTC).isoformat())


def _begin_immediate(conn: sqlite3.Connection, operation: str) -> None:
    """Start a serialized writer transaction without discarding caller work."""
    if conn.in_transaction:
        raise RuntimeError(f"{operation}: nested transaction not supported; commit or roll back the active transaction first")
    conn.execute("BEGIN IMMEDIATE")


def _in_immediate_tx(operation: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Wrap a public write method in BEGIN IMMEDIATE + commit/rollback lifecycle.

    The decorator consumes a ``_skip_begin=False`` kwarg from the caller — it
    is NOT forwarded to the wrapped function. When ``_skip_begin=True``, the
    decorator is a pass-through: no BEGIN, no COMMIT, no ROLLBACK. Use this
    when the wrapped method is invoked inside an outer caller's transaction
    (e.g. ``start_work`` → ``claim_issue``); the outer owner remains
    responsible for tx lifecycle.

    Catches ``Exception`` (not ``BaseException``) so SystemExit /
    KeyboardInterrupt propagate without an interposed rollback round-trip
    (filigree-2.1.0 §2.1).
    """

    def decorate(fn: Callable[_P, _R]) -> Callable[_P, _R]:
        @functools.wraps(fn)
        def wrapper(self: Any, *args: Any, _skip_begin: bool = False, **kwargs: Any) -> _R:
            if _skip_begin:
                return fn(self, *args, **kwargs)
            _begin_immediate(self.conn, operation)
            try:
                result = fn(self, *args, **kwargs)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            return result

        # Expose ``_skip_begin`` in the wrapper's introspectable signature so
        # ``inspect.signature`` (which follows ``__wrapped__`` by default) and
        # the mixin-contract test see the kwarg the wrapper actually accepts.
        wrapper.__signature__ = _augment_signature_with_skip_begin(fn)  # type: ignore[attr-defined]
        return cast("Callable[_P, _R]", wrapper)

    return decorate


def _augment_signature_with_skip_begin(fn: Callable[..., Any]) -> inspect.Signature:
    """Return ``fn``'s signature with ``_skip_begin: bool = False`` appended.

    No-op if the parameter is already present.
    """
    sig = inspect.signature(fn)
    if "_skip_begin" in sig.parameters:
        return sig
    params = list(sig.parameters.values())
    params.append(
        inspect.Parameter(
            "_skip_begin",
            inspect.Parameter.KEYWORD_ONLY,
            default=False,
            annotation=bool,
        )
    )
    return sig.replace(parameters=params)


def _is_transient_sqlite_lock(exc: sqlite3.OperationalError) -> bool:
    """Return True for SQLITE_BUSY / SQLITE_LOCKED, including extended codes."""
    code = getattr(exc, "sqlite_errorcode", None)
    if not isinstance(code, int):
        return False
    return code in _SQLITE_TRANSIENT_LOCK_CODES or (code & 0xFF) in _SQLITE_TRANSIENT_LOCK_CODES


def _retry_busy(
    *,
    attempts: int = 3,
    base: float = 0.05,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Retry a wrapped op on transient ``SQLITE_BUSY`` / ``SQLITE_LOCKED``.

    Catches ``sqlite3.OperationalError`` carrying a transient SQLite lock
    error code, sleeps ``base * 2 ** attempt`` seconds, and retries up to
    ``attempts`` times. Other ``OperationalError`` subclasses propagate.
    After the budget is exhausted the original exception is re-raised so
    call sites surface a real lock failure rather than a synthetic
    RuntimeError.

    Pass-through when ``_skip_begin=True``: the inner call is inside an
    outer caller's open transaction, where retrying alone cannot recover
    the lost lock — the BUSY error must propagate to the outer retry
    decorator, which rolls back the whole composed op and retries from
    scratch (filigree-2.1.0 §2.1).

    The ``sleep`` parameter is injectable for tests that simulate
    contention without burning wall time.
    """

    def decorate(fn: Callable[_P, _R]) -> Callable[_P, _R]:
        @functools.wraps(fn)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> _R:
            if kwargs.get("_skip_begin"):
                return fn(self, *args, **kwargs)
            for attempt in range(attempts):
                try:
                    return fn(self, *args, **kwargs)
                except sqlite3.OperationalError as exc:
                    if not _is_transient_sqlite_lock(exc):
                        raise
                    if attempt == attempts - 1:
                        raise
                    sleep(base * (2**attempt))
            raise RuntimeError("unreachable")  # pragma: no cover

        return cast("Callable[_P, _R]", wrapper)

    return decorate


def _normalize_iso_to_utc(raw: object) -> str | None:
    """Canonicalize an ISO-8601 timestamp to ``+00:00`` UTC text.

    Returns ``None`` for ``None`` or empty input. Naive timestamps are
    treated as UTC (matching the convention of ``_now_iso``). A trailing
    ``Z`` is accepted. Unparseable input raises ``ValueError``.

    SQLite TEXT compares lexicographically: rows whose stored text uses a
    non-zero offset (e.g. ``+02:00``) miscompare against the canonical
    ``+00:00`` written by ``_now_iso``. The internal write paths always
    emit canonical text; the import boundary must too. (filigree-20911dfe6d)
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        msg = f"timestamp must be a string, got {type(raw).__name__}"
        raise ValueError(msg)
    if raw == "":
        return None
    candidate = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


class _ParsedJson(dict[str, Any]):
    """``dict`` subclass carrying an out-of-band JSON-parse-failure flag.

    Returned by :func:`_safe_json_loads`. The ``_filigree_corrupt`` attribute
    signals corruption without occupying a user-visible dict key, so a custom
    field or metadata entry that happens to be named ``_fields_error`` or
    ``_metadata_error`` is not falsely stripped by Issue / FileRecord /
    ScanFinding ``to_dict()``. Consumers duck-type the attribute via
    ``getattr(value, "_filigree_corrupt", False)`` (filigree-7ea6b80f3b).
    """

    _filigree_corrupt: bool = False


def _safe_json_loads(raw: str | bytes | None, context: str) -> _ParsedJson:
    """Parse JSON from a database column, returning an out-of-band corrupt flag.

    Used by DB mixins to handle corrupt JSON in issue fields, file metadata,
    and scan finding metadata. On failure — invalid JSON, undecodable bytes,
    or a non-dict top-level value — returns an empty ``_ParsedJson`` with
    ``_filigree_corrupt=True``. SQLite's flexible typing can hand back
    ``bytes`` for BLOB-typed JSON columns, so undecodable bytes
    (``UnicodeDecodeError``) are treated as corrupt rather than allowed to
    propagate (filigree-7ea6b80f3b).
    """
    if not raw:
        return _ParsedJson()
    try:
        result = json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        logger.warning("Corrupt JSON (%s): %r", context, str(raw)[:200])
        out = _ParsedJson()
        out._filigree_corrupt = True
        return out
    if not isinstance(result, dict):
        logger.warning("JSON (%s) parsed but is not a dict (got %s): %r", context, type(result).__name__, str(raw)[:200])
        out = _ParsedJson()
        out._filigree_corrupt = True
        return out
    return _ParsedJson(result)


def _escape_like_chars(value: str) -> str:
    """Escape LIKE wildcard characters (``%``, ``_``, ``\\``) without adding wrapping wildcards."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _escape_like(query: str) -> str:
    """Escape a string for SQL LIKE with backslash escape, wrapped in % wildcards."""
    return f"%{_escape_like_chars(query)}%"


class DBMixinProtocol(Protocol):
    """Shared attributes and methods that DB mixins access via self.

    Mixins inherit this Protocol so mypy can type-check cross-mixin calls
    without ``type: ignore`` on every call.  Individual mixins implement a
    *subset* of these methods; the full contract is satisfied by FiligreeDB
    at composition time via MRO.

    **Single source of truth** — do NOT redeclare these methods in per-mixin
    ``TYPE_CHECKING`` blocks.  Add new cross-mixin methods here instead.
    """

    # -- Shared attributes ---------------------------------------------------

    db_path: Path
    # Project root set by from_filigree_dir / from_conf.  ``None`` for legacy
    # direct-path construction — consumers that need it must fall back.
    project_root: Path | None
    prefix: str
    _conn: sqlite3.Connection | None
    _template_registry: TemplateRegistry | None
    _enabled_packs_override: list[str] | None

    @property
    def conn(self) -> sqlite3.Connection: ...

    # -- Core (FiligreeDB) ---------------------------------------------------

    def get_issue(self, issue_id: str) -> Issue: ...
    def _check_id_prefix(self, issue_id: str) -> None: ...

    # -- WorkflowMixin -------------------------------------------------------

    @property
    def templates(self) -> TemplateRegistry: ...

    def _validate_status(self, status: str, issue_type: str = "task") -> None: ...
    def _validate_parent_id(self, parent_id: str | None) -> None: ...
    def _validate_label_name(self, label: str, *, allow_priority_like: bool = False) -> str: ...
    def _get_states_for_category(self, category: str) -> list[str]: ...
    def _get_type_states_for_category(self, category: str) -> list[tuple[str, str]]: ...
    def _category_predicate_sql(
        self,
        category: str,
        *,
        type_col: str,
        status_col: str,
        include_archived: bool = False,
    ) -> tuple[str, list[str]]: ...
    def _blocker_done_states(self) -> list[str]: ...
    def _resolve_status_category(self, issue_type: str, status: str) -> StatusCategory: ...
    def get_valid_transitions(self, issue_id: str) -> list[TransitionOption]: ...

    @staticmethod
    def _infer_status_category(issue_type: str, status: str) -> StatusCategory: ...

    # -- EventsMixin ---------------------------------------------------------

    def _record_event(
        self,
        issue_id: str,
        event_type: EventType,
        *,
        actor: str = "",
        old_value: str | None = None,
        new_value: str | None = None,
        comment: str = "",
    ) -> None: ...

    # -- IssuesMixin ---------------------------------------------------------

    def _generate_unique_id(self, table: str, infix: str = "") -> str: ...
    def _build_issues_batch(self, issue_ids: list[str]) -> list[Issue]: ...
    def _would_create_parent_cycle(self, child_id: str, proposed_parent_id: str) -> bool: ...

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
    ) -> Issue: ...

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
    ) -> Issue: ...

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
    ) -> list[Issue]: ...

    # -- MetaMixin -----------------------------------------------------------

    def add_label(
        self,
        issue_id: str,
        label: str,
        *,
        actor: str = "",
        expected_assignee: str | None = None,
        _skip_begin: bool = False,
    ) -> tuple[bool, str, list[str]]: ...
    def remove_label(
        self,
        issue_id: str,
        label: str,
        *,
        actor: str = "",
        expected_assignee: str | None = None,
        _skip_begin: bool = False,
    ) -> tuple[bool, str]: ...
    def batch_remove_label(
        self, issue_ids: list[str], *, label: str, actor: str = "", expected_assignee: str | None = None
    ) -> tuple[list[dict[str, str]], list[BatchFailure]]: ...
    def add_comment(
        self,
        issue_id: str,
        text: str,
        *,
        author: str = "",
        expected_assignee: str | None = None,
        _skip_begin: bool = False,
    ) -> int: ...
    def get_comment(self, comment_id: int) -> CommentRecord: ...

    # -- PlanningMixin -------------------------------------------------------

    def get_ready(self) -> list[Issue]: ...
    def label_subtree(self, parent_id: str, *, label: str) -> tuple[list[dict[str, str]], list[BatchFailure]]: ...
    def _resolve_open_blocker_predicates(
        self,
    ) -> tuple[tuple[str, list[str]], tuple[str, list[str]]] | None: ...

    # -- FilesMixin ----------------------------------------------------------

    def register_file(
        self,
        path: str,
        *,
        language: str = "",
        file_type: str = "",
        metadata: dict[str, Any] | None = None,
        actor: str = "",
    ) -> FileRecord: ...

    def get_file(self, file_id: str) -> FileRecord: ...
    def add_file_association(self, file_id: str, issue_id: str, assoc_type: AssocType, *, actor: str = "") -> None: ...
    def get_finding(self, finding_id: str) -> ScanFindingDict: ...

    # -- ObservationsMixin ---------------------------------------------------

    def create_observation(
        self,
        summary: str,
        *,
        detail: str = "",
        file_path: str = "",
        line: int | None = None,
        source_issue_id: str = "",
        source_finding_id: str = "",
        priority: int = 3,
        actor: str = "",
        auto_commit: bool = True,
    ) -> ObservationDict: ...

    def link_observation_to_issue(
        self,
        obs_id: str,
        issue_id: str,
        *,
        disposition: str = "evidence",
        reason: str = "",
        actor: str = "",
    ) -> ObservationLinkDict: ...

    # -- ScansMixin ----------------------------------------------------------

    def update_scan_run_status(
        self,
        scan_run_id: str,
        status: ScanRunStatus,
        *,
        exit_code: int | None = None,
        findings_count: int | None = None,
        error_message: str | None = None,
    ) -> ScanRunDict: ...

    def reserve_scan_run(
        self,
        *,
        scan_run_id: str,
        scanner_name: str,
        scan_source: str,
        file_path: str,
        file_id: str,
        api_url: str = "",
        log_path: str = "",
    ) -> tuple[ScanRunDict | None, ScanRunDict | None]: ...

    def set_scan_run_spawn_info(
        self,
        scan_run_id: str,
        *,
        pid: int,
        log_path: str,
    ) -> None: ...
