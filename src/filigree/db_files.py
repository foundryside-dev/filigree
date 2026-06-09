"""FilesMixin — file records, scan findings, associations, and timeline.

Extracted from core.py as part of the module architecture split.
All methods access ``self.conn``, ``self.get_issue()``, etc. via
Python's MRO when composed into ``FiligreeDB``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, get_args

from filigree.db_base import (
    DBMixinProtocol,
    _escape_like_chars,
    _in_immediate_tx,
    _now_iso,
    _retry_busy,
    _safe_json_loads,
)
from filigree.db_scans import TERMINAL_SCAN_RUN_STATUSES
from filigree.finding_issue_cascade import (
    FINDING_CASCADE_MARKER,
    FindingIssueCascadeService,
)
from filigree.models import FileRecord, Issue, ScanFinding
from filigree.registry import (
    BatchQuery,
    BatchResolution,
    RegistryBriefingBlockedError,
    RegistryFileNotFoundError,
    RegistryResolutionError,
    resolve_files_batch_via_loop,
)
from filigree.types.core import (
    AssocType,
    FindingStatus,
    Severity,
    make_content_hash,
    make_issue_id,
    make_loomweave_entity_id,
)
from filigree.types.files import ScanIngestResult

if TYPE_CHECKING:
    from filigree.registry import ResolvedFile
    from filigree.types.core import ObservationDict, PaginatedResult, ScanFindingDict
    from filigree.types.files import (
        CleanStaleResult,
        DeleteFileRecordResult,
        EnrichedFileItem,
        FileAssociation,
        FileDetail,
        FileHotspot,
        FindingsSummary,
        GlobalFindingsStats,
        IssueFileAssociation,
        ScanRunRecord,
        TimelineEntry,
    )

logger = logging.getLogger(__name__)

INGESTED_FILE_ID_KEY = "_filigree_ingested_file_id"

# ---------------------------------------------------------------------------
# Constants for file-domain validation
# ---------------------------------------------------------------------------

VALID_SEVERITIES: frozenset[str] = frozenset(get_args(Severity))
VALID_FINDING_STATUSES: frozenset[str] = frozenset(get_args(FindingStatus))
TERMINAL_FINDING_STATUSES: frozenset[str] = frozenset({"fixed", "false_positive"})
# Safety: these values are interpolated into SQL string literals below.
# Verify none contain characters that could break the SQL.
if not all(s.isalpha() or s.replace("_", "").isalpha() for s in TERMINAL_FINDING_STATUSES):
    raise ValueError(f"TERMINAL_FINDING_STATUSES values must be simple identifiers, got: {TERMINAL_FINDING_STATUSES}")
VALID_ASSOC_TYPES: frozenset[str] = frozenset(get_args(AssocType))
# FIL-2/X-5: the nested wardline axes ``finding_list`` can filter on. Mirror
# wardline's own ``scan where:{kind, suppression}`` enums so the consumer's
# grammar matches the producer's. ``kind``/``suppression_state`` live under
# ``metadata.wardline.*`` (lifted onto the read surface in ``_build_scan_finding``).
VALID_WARDLINE_FINDING_KINDS: frozenset[str] = frozenset({"defect", "fact", "classification", "metric", "suggestion"})
# ``active`` is the unsuppressed population (no wardline suppression verdict);
# ``baselined``/``waived``/``judged`` are wardline's repo-controlled suppression
# annotations; ``all`` is the explicit no-filter sentinel (identical to omitting
# the filter — returns every row). The agent-facing surfaces (MCP/CLI) default
# to ``active`` and pass ``all`` to opt back in to suppressed rows
# (filigree-2bdb878bd2); the core query keeps its all-inclusive default so
# internal callers are unaffected.
VALID_SUPPRESSION_FILTERS: frozenset[str] = frozenset({"all", "active", "baselined", "waived", "judged"})


def _wardline_suppressed_sql(alias: str = "") -> str:
    """SQL predicate that is true when a finding carries a wardline suppression
    verdict (``metadata.wardline.suppression_state`` is present).

    Shared verbatim by ``unbridged_finding_stats`` (its ``suppressed`` count)
    and the ``finding_list`` ``suppression`` filter so the active/suppressed
    *classification* is computed identically on both surfaces and cannot drift.
    (This shares the predicate, not the base population: ``unbridged_finding_stats``
    additionally restricts to open + un-bridged findings, so its ``actionable``
    count is a subset of ``finding_list(suppression="active")`` — the latter
    applies no open/unbridged filter of its own.) ``json_valid`` guards a single
    corrupt-metadata row from raising ``OperationalError``; an absent/corrupt
    verdict reads as un-suppressed (matches the promote-guard's "absent =>
    active" contract). ``alias`` is an optional table qualifier (e.g. ``"sf"``)
    for joined queries.
    """
    col = f"{alias}.metadata" if alias else "metadata"
    return f"(json_valid({col}) AND json_extract({col}, '$.wardline.suppression_state') IS NOT NULL)"


def _wardline_field_eq_sql(field: str, alias: str = "") -> str:
    """SQL predicate matching a nested ``metadata.wardline.<field>`` against a
    bound ``?`` parameter, guarded by ``json_valid``. ``field`` is always a
    fixed literal from this module's call sites (never user input)."""
    col = f"{alias}.metadata" if alias else "metadata"
    return f"(json_valid({col}) AND json_extract({col}, '$.wardline.{field}') = ?)"


def _validate_string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string, got {type(value).__name__}")
    return value


def _validate_optional_string_list(value: object, field_name: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{field_name} must be a list of strings")
    return value


_LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".css": "css",
    ".cxx": "cpp",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".jsx": "javascript",
    ".kt": "kotlin",
    ".lua": "lua",
    ".md": "markdown",
    ".mdown": "markdown",
    ".markdown": "markdown",
    ".php": "php",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".scala": "scala",
    ".scss": "scss",
    ".sh": "shell",
    ".sql": "sql",
    ".swift": "swift",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
}


def _normalize_scan_path(path: str) -> str:
    """Normalize scanner-provided paths for stable file identity."""
    normalized = os.path.normpath(path.replace("\\", "/"))
    return "" if normalized == "." else normalized


def _is_project_relative_scan_path(path: str) -> bool:
    if Path(path).is_absolute():
        return False
    if len(path) >= 3 and path[1:3] == ":/":
        return False
    return path != ".." and not path.startswith("../")


def _normalize_project_relative_scan_path(path: object, *, field_name: str) -> str:
    if not isinstance(path, str):
        raise ValueError(f"{field_name} must be a string, got {type(path).__name__}")
    normalized = _normalize_scan_path(path)
    if not normalized:
        raise ValueError(f"{field_name} cannot be empty after normalization")
    if not _is_project_relative_scan_path(normalized):
        raise ValueError(f"{field_name} must be project-relative")
    return normalized


def _normalize_registry_canonical_path(path: object, *, requested_path: str) -> str:
    return _normalize_project_relative_scan_path(
        path,
        field_name=f"Registry canonical_path for {requested_path!r}",
    )


def _normalize_file_path_prefix(path_prefix: str) -> str:
    raw_prefix = path_prefix.replace("\\", "/")
    normalized = _normalize_scan_path(raw_prefix)
    if not normalized:
        return ""
    if not _is_project_relative_scan_path(normalized):
        raise ValueError("path_prefix must be project-relative")
    if raw_prefix.endswith("/") and not normalized.endswith("/"):
        normalized += "/"
    return normalized


def _validate_file_metadata(metadata: object | None) -> dict[str, Any] | None:
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        msg = "metadata must be a JSON object"
        raise ValueError(msg)
    return metadata


def _infer_language_from_path(path: str) -> str:
    """Infer a conservative language name from a path extension."""
    _root, ext = os.path.splitext(path.casefold())
    return _LANGUAGE_BY_EXTENSION.get(ext, "")


def scan_finding_observation_summary(scan_source: str, path: str, line_start: int | None, message: str) -> str:
    """Return the observation summary used for scanner-created findings."""
    first_line = message.strip().splitlines()[0] if message.strip() else "Scanner finding"
    line_label = line_start if line_start is not None else "?"
    return f"[{scan_source}] {path}:{line_label} -- {first_line}"


class FilesMixin(DBMixinProtocol):
    """File records, scan findings, associations, and timeline.

    Inherits ``DBMixinProtocol`` for type-safe access to shared attributes.
    Actual implementations provided by ``FiligreeDB`` at composition time via MRO.
    """

    # SQL fragment for filtering open (non-terminal) findings — derived from TERMINAL_FINDING_STATUSES.
    _OPEN_FINDINGS_FILTER = "status NOT IN ({})".format(", ".join(f"'{s}'" for s in sorted(TERMINAL_FINDING_STATUSES)))
    _OPEN_FINDINGS_FILTER_SF = "sf.status NOT IN ({})".format(", ".join(f"'{s}'" for s in sorted(TERMINAL_FINDING_STATUSES)))

    # Severity ordering for SQL sort: lower number = more severe.
    _SEVERITY_ORDER_SQL = (
        "CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 WHEN 'info' THEN 4 ELSE 5 END"
    )

    _VALID_FILE_SORTS = frozenset({"updated_at", "first_seen", "path", "language"})
    _VALID_FINDING_SORTS = frozenset({"updated_at", "severity"})

    # N6 (weft-c815d5e77d): finding read paths LEFT JOIN the linked issue so the
    # finding carries the issue's status + ``close_reason`` resolution. The
    # ``scan_findings`` table is aliased ``sf`` and ``issues`` ``i``; any WHERE /
    # ORDER BY column that exists on BOTH tables (``status``, ``updated_at``,
    # ``id``) MUST be qualified with ``sf.`` to stay unambiguous after the join.
    _FINDING_ISSUE_JOIN = "FROM scan_findings sf LEFT JOIN issues i ON sf.issue_id = i.id"
    _FINDING_SELECT_COLS = "sf.*, i.status AS issue_status, json_extract(i.fields, '$.close_reason') AS issue_resolution"

    # -- Build helpers -------------------------------------------------------

    @staticmethod
    def _parse_metadata(raw: str | None, context_id: str) -> dict[str, Any]:
        """Parse a JSON metadata column.

        Returns a ``_ParsedJson`` (dict subclass) — corrupt input yields an
        empty dict with ``_filigree_corrupt=True`` instead of an in-band
        sentinel key, so user metadata named ``_metadata_error`` round-trips
        unchanged (filigree-7ea6b80f3b).
        """
        return _safe_json_loads(raw, context_id)

    def _build_file_record(self, row: sqlite3.Row) -> FileRecord:
        """Build a FileRecord from a database row."""
        return FileRecord(
            id=row["id"],
            path=row["path"],
            language=row["language"] or "",
            file_type=row["file_type"] or "",
            content_hash=row["content_hash"] or "",
            registry_backend=row["registry_backend"] or "local",
            created_by=row["created_by"] or "",
            updated_by=row["updated_by"] or "",
            first_seen=row["first_seen"],
            updated_at=row["updated_at"],
            metadata=self._parse_metadata(row["metadata"], f"file_record:{row['id']}"),
        )

    def _build_scan_finding(self, row: sqlite3.Row) -> ScanFinding:
        """Build a ScanFinding from a database row.

        The optional ``issue_status`` / ``issue_resolution`` columns are present
        only when the caller's query LEFT JOINs ``issues`` (N6). Bare
        ``SELECT * FROM scan_findings`` callers omit them, so read them
        defensively and default to ``None`` (an unjoined read carries no linked
        issue status).
        """
        row_keys = row.keys()
        metadata = self._parse_metadata(row["metadata"], f"scan_finding:{row['file_id']}")
        # Lift wardline's suppression verdict out of the metadata blob onto the
        # read surface (mirrors the N6 issue_status lift). Guard every level: the
        # wardline namespace and its suppression_state are both optional, and a
        # corrupt metadata parse yields a non-dict sentinel.
        wardline_meta = metadata.get("wardline") if isinstance(metadata, dict) else None
        suppression_state = wardline_meta.get("suppression_state") if isinstance(wardline_meta, dict) else None
        return ScanFinding(
            id=row["id"],
            file_id=row["file_id"],
            severity=row["severity"],
            status=row["status"],
            scan_source=row["scan_source"] or "",
            rule_id=row["rule_id"] or "",
            message=row["message"] or "",
            suggestion=row["suggestion"] or "",
            scan_run_id=row["scan_run_id"] or "",
            line_start=row["line_start"],
            line_end=row["line_end"],
            fingerprint=row["fingerprint"] or "",
            issue_id=row["issue_id"],
            seen_count=row["seen_count"] or 1,
            created_by=row["created_by"] or "",
            updated_by=row["updated_by"] or "",
            first_seen=row["first_seen"],
            updated_at=row["updated_at"],
            last_seen_at=row["last_seen_at"],
            issue_status=row["issue_status"] if "issue_status" in row_keys else None,
            issue_resolution=row["issue_resolution"] if "issue_resolution" in row_keys else None,
            suppression_state=suppression_state,
            metadata=metadata,
        )

    def _is_local_registry_fallback_row(self, registry_backend: str) -> bool:
        # ``registry_backend`` and ``allow_local_fallback`` are always set on
        # ``FiligreeDB.__init__`` before any DB call reaches a mixin method;
        # attribute access is safe without a default.
        return self.registry_backend == "loomweave" and bool(self.allow_local_fallback) and registry_backend == "local"

    def _record_registry_fallback_event(self, file_id: str, *, actor: str, now: str) -> None:
        self.conn.execute(
            "INSERT INTO file_events "
            "(file_id, event_type, field, old_value, new_value, actor, verified_actor, created_at) "
            "VALUES (?, 'registry_local_fallback', 'registry_backend', 'loomweave', 'local', ?, ?, ?)",
            (file_id, actor, self._verified_actor, now),
        )

    # -- File registration ---------------------------------------------------

    def register_file(
        self,
        path: str,
        *,
        language: str = "",
        file_type: str = "",
        metadata: dict[str, Any] | None = None,
        actor: str = "",
        _commit: bool = True,
    ) -> FileRecord:
        """Register a file or update it if already registered (upsert by path).

        Path is normalized via ``_normalize_scan_path`` to ensure consistent
        identity regardless of caller (MCP tool, scan ingestion, etc.).

        Returns the FileRecord (created or updated).

        Implementation note: a registry that canonicalises the requested path
        (case-fold, whitespace, slash normalisation) may resolve a fresh-looking
        call to the storage path of an already-registered row. The previous
        version of this method handled that by recursing back into
        ``register_file`` with the canonical path; this flat variant calls
        :meth:`_update_existing_file_record` directly so there is exactly one
        update code path (mirroring ``_upsert_file_record``'s pattern).
        """
        path = _normalize_project_relative_scan_path(path, field_name="File path")
        metadata = _validate_file_metadata(metadata)
        now = _now_iso()
        existing = self.conn.execute("SELECT * FROM file_records WHERE path = ?", (path,)).fetchone()
        inferred_language = _infer_language_from_path(path)

        if existing is not None:
            return self._update_existing_file_record(
                existing,
                path=path,
                language=language,
                inferred_language=inferred_language,
                file_type=file_type,
                metadata=metadata,
                actor=actor,
                now=now,
                _commit=_commit,
            )

        stored_language = language or inferred_language
        resolved = self.registry.resolve_file(
            path,
            language=stored_language,
            actor=actor,
        )
        file_id = resolved["file_id"]
        stored_path = _normalize_registry_canonical_path(resolved["canonical_path"], requested_path=path)
        stored_language = resolved["language"] or stored_language
        content_hash = resolved["content_hash"]
        registry_backend = resolved["registry_backend"]
        storage_existing = self.conn.execute(
            "SELECT * FROM file_records WHERE path = ? OR id = ?",
            (stored_path, file_id),
        ).fetchone()
        if storage_existing is not None:
            return self._update_existing_file_record(
                storage_existing,
                path=storage_existing["path"],
                language=language,
                inferred_language=_infer_language_from_path(storage_existing["path"]),
                file_type=file_type,
                metadata=metadata,
                actor=actor,
                now=now,
                _commit=_commit,
            )
        # When the caller owns the transaction (``_commit=False``) a failure here
        # must undo only our own INSERT — a full ``rollback()`` would discard the
        # caller's prior uncommitted work. Bracket the INSERT in a savepoint and
        # roll back to it instead (mirrors ``create_observation``).
        savepoint_name = "register_file_insert"
        savepoint_active = False

        def _rollback_savepoint() -> None:
            nonlocal savepoint_active
            if not savepoint_active:
                return
            try:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
            finally:
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
                savepoint_active = False

        def _release_savepoint() -> None:
            nonlocal savepoint_active
            if savepoint_active:
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
                savepoint_active = False

        if not _commit:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN")
            self.conn.execute(f"SAVEPOINT {savepoint_name}")
            savepoint_active = True
        try:
            self.conn.execute(
                "INSERT INTO file_records "
                "(id, path, language, file_type, content_hash, registry_backend, created_by, updated_by, first_seen, updated_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    file_id,
                    stored_path,
                    stored_language,
                    file_type,
                    content_hash,
                    registry_backend,
                    actor,
                    actor,
                    now,
                    now,
                    json.dumps(metadata or {}),
                ),
            )
            if self._is_local_registry_fallback_row(registry_backend):
                self._record_registry_fallback_event(file_id, actor=actor, now=now)
            if _commit:
                self.conn.commit()
            else:
                _release_savepoint()
        except sqlite3.IntegrityError:
            # Undo our failed INSERT: a full rollback when we own the transaction,
            # otherwise only to our savepoint so the caller's prior writes survive.
            # The INSERT raised because the conflicting row is visible in our read
            # snapshot, so the recovery requery finds it under both rollback modes
            # — retry the collision as an update.
            if _commit:
                self.conn.rollback()
            else:
                _rollback_savepoint()
            storage_existing = self.conn.execute(
                "SELECT * FROM file_records WHERE path IN (?, ?) OR id = ?",
                (path, stored_path, file_id),
            ).fetchone()
            if storage_existing is not None:
                return self._update_existing_file_record(
                    storage_existing,
                    path=storage_existing["path"],
                    language=language,
                    inferred_language=_infer_language_from_path(storage_existing["path"]),
                    file_type=file_type,
                    metadata=metadata,
                    actor=actor,
                    now=now,
                    _commit=_commit,
                )
            raise
        except Exception:
            if _commit:
                self.conn.rollback()
            else:
                _rollback_savepoint()
            raise
        return self.get_file(file_id)

    def _update_existing_file_record(
        self,
        existing: sqlite3.Row,
        *,
        path: str,
        language: str,
        inferred_language: str,
        file_type: str,
        metadata: dict[str, Any] | None,
        actor: str,
        now: str,
        _commit: bool = True,
    ) -> FileRecord:
        """Update an already-stored ``file_records`` row from a register call.

        Centralises the diff-detect/emit-events/UPDATE path so both the
        same-path-match branch and the canonical-collision recovery branch
        of :meth:`register_file` go through the same code (mirrors the flat
        ``update_existing_file`` pattern in :meth:`_upsert_file_record`).
        """
        updates: list[str] = []
        params: list[Any] = []
        changes: list[tuple[str, str, str]] = []  # (field, old, new)
        current_language = existing["language"] or ""
        next_language = language or (inferred_language if not current_language else "")
        if self.registry.is_displaced():
            resolved = self.registry.resolve_file(
                path,
                language=next_language or current_language,
                actor=actor,
            )
            if resolved["file_id"] != existing["id"]:
                msg = (
                    f"Existing file {path!r} resolves to registry id {resolved['file_id']!r}, "
                    f"but stored file id is {existing['id']!r}; run migrate-registry before re-registering"
                )
                raise ValueError(msg)
            for field, next_value in (
                ("content_hash", resolved["content_hash"]),
                ("registry_backend", resolved["registry_backend"]),
            ):
                current_value = existing[field] or ""
                if next_value != current_value:
                    updates.append(f"{field} = ?")
                    params.append(next_value)
                    changes.append((field, current_value, next_value))
        if next_language and next_language != current_language:
            updates.append("language = ?")
            params.append(next_language)
            changes.append(("language", current_language, next_language))
        if file_type and file_type != (existing["file_type"] or ""):
            updates.append("file_type = ?")
            params.append(file_type)
            changes.append(("file_type", existing["file_type"] or "", file_type))
        # ``is not None`` (not truthy) so ``metadata={}`` can explicitly clear
        # existing metadata; ``metadata=None`` means "leave unchanged".
        if metadata is not None:
            old_meta_raw = existing["metadata"] or "{}"
            try:
                old_meta_parsed = json.loads(old_meta_raw)
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Corrupt metadata for file %s (id=%s), treating as empty",
                    existing["path"],
                    existing["id"],
                )
                old_meta_parsed = {}
            if old_meta_parsed != metadata:
                new_meta = json.dumps(metadata)
                updates.append("metadata = ?")
                params.append(new_meta)
                changes.append(("metadata", old_meta_raw, new_meta))
        if not updates:
            return self.get_file(existing["id"])
        updates.append("updated_at = ?")
        params.append(now)
        updates.append("updated_by = ?")
        params.append(actor)
        params.append(existing["id"])
        # When the caller owns the transaction (``_commit=False``) a failure here
        # must undo only our own writes — a full ``rollback()`` would discard the
        # caller's prior uncommitted work. Bracket our writes in a savepoint and
        # roll back to it instead (mirrors ``create_observation``).
        savepoint_name = "update_file_record"
        savepoint_active = False

        def _rollback_savepoint() -> None:
            nonlocal savepoint_active
            if not savepoint_active:
                return
            try:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
            finally:
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
                savepoint_active = False

        def _release_savepoint() -> None:
            nonlocal savepoint_active
            if savepoint_active:
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
                savepoint_active = False

        if not _commit:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN")
            self.conn.execute(f"SAVEPOINT {savepoint_name}")
            savepoint_active = True
        try:
            self.conn.execute(
                f"UPDATE file_records SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            for field, old_val, new_val in changes:
                self.conn.execute(
                    "INSERT INTO file_events "
                    "(file_id, event_type, field, old_value, new_value, actor, verified_actor, created_at) "
                    "VALUES (?, 'file_metadata_update', ?, ?, ?, ?, ?, ?)",
                    (existing["id"], field, old_val, new_val, actor, self._verified_actor, now),
                )
            if _commit:
                self.conn.commit()
            else:
                _release_savepoint()
        except Exception:
            if _commit:
                self.conn.rollback()
            else:
                _rollback_savepoint()
            raise
        return self.get_file(existing["id"])

    def get_file(self, file_id: str) -> FileRecord:
        """Get a file record by ID. Raises KeyError if not found."""
        row = self.conn.execute("SELECT * FROM file_records WHERE id = ?", (file_id,)).fetchone()
        if row is None:
            raise KeyError(file_id)
        return self._build_file_record(row)

    def get_file_by_path(self, path: str) -> FileRecord | None:
        """Get a file record by path. Returns None if not found."""
        path = _normalize_scan_path(path)
        row = self.conn.execute("SELECT * FROM file_records WHERE path = ?", (path,)).fetchone()
        if row is None:
            return None
        return self._build_file_record(row)

    def delete_file_record(self, file_id: str, *, force: bool = False, actor: str = "") -> DeleteFileRecordResult:
        """Delete a file record and file-domain dependent rows.

        Refuses by default when the file still has issue associations or
        non-terminal findings. Terminal findings, metadata events, and
        observation links are cleanup residue and can be removed/unlinked
        without ``force``.
        """
        self.get_file(file_id)  # raises KeyError if not found
        if not isinstance(force, bool):
            msg = "force must be a boolean"
            raise ValueError(msg)

        counts = self.conn.execute(
            f"""
            SELECT
              (SELECT COUNT(*) FROM file_associations WHERE file_id = ?) AS associations,
              (SELECT COUNT(*) FROM scan_findings WHERE file_id = ? AND {self._OPEN_FINDINGS_FILTER}) AS open_findings
            """,
            (file_id, file_id),
        ).fetchone()
        associations = int(counts["associations"])
        open_findings = int(counts["open_findings"])
        if not force and (associations or open_findings):
            blockers: list[str] = []
            if associations:
                blockers.append(f"{associations} association{'s' if associations != 1 else ''}")
            if open_findings:
                blockers.append(f"{open_findings} open finding{'s' if open_findings != 1 else ''}")
            msg = f"Cannot delete file record {file_id}: " + " and ".join(blockers) + "; pass force=True to cascade."
            raise ValueError(msg)

        try:
            finding_ids = [
                row["id"]
                for row in self.conn.execute(
                    "SELECT id FROM scan_findings WHERE file_id = ?",
                    (file_id,),
                ).fetchall()
            ]
            annotation_links = self.conn.execute(
                "DELETE FROM annotation_links WHERE target_type = 'file' AND target_id = ?",
                (file_id,),
            ).rowcount
            if finding_ids:
                placeholders = ", ".join("?" for _ in finding_ids)
                annotation_links += self.conn.execute(
                    f"DELETE FROM annotation_links WHERE target_type = 'finding' AND target_id IN ({placeholders})",
                    finding_ids,
                ).rowcount
            observations = self.conn.execute(
                "UPDATE observations SET file_id = NULL WHERE file_id = ?",
                (file_id,),
            ).rowcount
            file_events = self.conn.execute("DELETE FROM file_events WHERE file_id = ?", (file_id,)).rowcount
            deleted_associations = self.conn.execute("DELETE FROM file_associations WHERE file_id = ?", (file_id,)).rowcount
            deleted_findings = self.conn.execute("DELETE FROM scan_findings WHERE file_id = ?", (file_id,)).rowcount
            deleted_files = self.conn.execute("DELETE FROM file_records WHERE id = ?", (file_id,)).rowcount
            if deleted_files != 1:
                msg = f"File not found: {file_id}"
                raise KeyError(msg)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        return {
            "status": "deleted",
            "file_id": file_id,
            "deleted_findings": deleted_findings,
            "deleted_associations": deleted_associations,
            "deleted_file_events": file_events,
            "deleted_annotation_links": annotation_links,
            "unlinked_observations": observations,
            "actor": actor,
        }

    def list_files(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        language: str | None = None,
        path_prefix: str | None = None,
        sort: str = "updated_at",
    ) -> list[FileRecord]:
        """List file records with optional filtering and sorting."""
        clauses: list[str] = []
        params: list[Any] = []

        if language is not None:
            clauses.append("language = ?")
            params.append(language)
        if path_prefix is not None:
            clauses.append("path LIKE ? ESCAPE '\\'")
            params.append(f"{_escape_like_chars(_normalize_file_path_prefix(path_prefix))}%")

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        if sort not in self._VALID_FILE_SORTS:
            valid = ", ".join(sorted(self._VALID_FILE_SORTS))
            raise ValueError(f'Invalid sort field "{sort}". Must be one of: {valid}')
        order = "ASC" if sort == "path" else "DESC"

        rows = self.conn.execute(
            f"SELECT * FROM file_records{where} ORDER BY {sort} {order} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [self._build_file_record(r) for r in rows]

    def list_files_paginated(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        language: str | None = None,
        path_prefix: str | None = None,
        min_findings: int | None = None,
        has_severity: str | None = None,
        scan_source: str | None = None,
        sort: str = "updated_at",
        direction: str | None = None,
    ) -> PaginatedResult[EnrichedFileItem]:
        """List file records with pagination metadata.

        Returns ``{results, total, limit, offset, has_more}``.

        When *min_findings* is provided, only files with at least that many
        open findings are returned (uses a correlated subquery).

        When *has_severity* is provided (e.g. ``"critical"``), only files
        with at least one open finding of that severity are returned.
        """
        # Use "fr" alias throughout so the same WHERE works in both the COUNT
        # and enriched queries without string replacement.
        clauses: list[str] = []
        params: list[Any] = []

        if language is not None:
            clauses.append("fr.language = ?")
            params.append(language)
        if path_prefix is not None:
            clauses.append("fr.path LIKE ? ESCAPE '\\'")
            params.append(f"{_escape_like_chars(_normalize_file_path_prefix(path_prefix))}%")
        if min_findings is not None and min_findings > 0:
            clauses.append(f"(SELECT COUNT(*) FROM scan_findings sf WHERE sf.file_id = fr.id AND {self._OPEN_FINDINGS_FILTER_SF}) >= ?")
            params.append(min_findings)
        if has_severity is not None:
            if has_severity not in VALID_SEVERITIES:
                valid = ", ".join(sorted(VALID_SEVERITIES))
                raise ValueError(f'Invalid severity filter "{has_severity}". Must be one of: {valid}')
            clauses.append(
                "(SELECT COUNT(*) FROM scan_findings sf"
                " WHERE sf.file_id = fr.id"
                f" AND {self._OPEN_FINDINGS_FILTER_SF}"
                " AND sf.severity = ?) > 0"
            )
            params.append(has_severity)
        if scan_source:
            clauses.append("EXISTS (SELECT 1 FROM scan_findings sf WHERE sf.file_id = fr.id AND sf.scan_source = ?)")
            params.append(scan_source)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        total: int = self.conn.execute(
            f"SELECT COUNT(*) FROM file_records fr{where}",
            params,
        ).fetchone()[0]

        if sort not in self._VALID_FILE_SORTS:
            valid = ", ".join(sorted(self._VALID_FILE_SORTS))
            raise ValueError(f'Invalid sort field "{sort}". Must be one of: {valid}')
        default_order = "ASC" if sort == "path" else "DESC"
        if direction is None:
            order = default_order
        else:
            order = direction.upper() if isinstance(direction, str) else ""
            if order not in ("ASC", "DESC"):
                raise ValueError(f'Invalid direction "{direction}". Must be "asc" or "desc".')

        _open = self._OPEN_FINDINGS_FILTER_SF
        _supp = _wardline_suppressed_sql("sf")
        _sev_cols = " ".join(
            f"(SELECT COUNT(*) FROM scan_findings sf WHERE sf.file_id = fr.id AND {_open} AND sf.severity='{s}') AS cnt_{s},"
            for s in ("critical", "high", "medium", "low", "info")
        )
        _supp_cols = " ".join(
            f"(SELECT COUNT(*) FROM scan_findings sf WHERE sf.file_id = fr.id AND {_open} AND sf.severity='{s}' AND {_supp}) AS supp_{s},"
            for s in ("critical", "high", "medium", "low", "info")
        )
        enriched_sql = (
            f"SELECT fr.*, "
            f"(SELECT COUNT(*) FROM scan_findings sf"
            f" WHERE sf.file_id = fr.id AND {_open}"
            f") AS open_findings, "
            f"(SELECT COUNT(*) FROM scan_findings sf"
            f" WHERE sf.file_id = fr.id"
            f") AS total_findings, "
            f"{_sev_cols} {_supp_cols} "
            f"(SELECT COUNT(*) FROM file_associations fa"
            f" WHERE fa.file_id = fr.id"
            f") AS associations_count, "
            f"(SELECT COUNT(*) FROM observations o"
            f" WHERE o.file_id = fr.id AND o.expires_at > ?"
            f") AS observation_count"
            f" FROM file_records fr{where}"
            f" ORDER BY {sort} {order}"
            f" LIMIT ? OFFSET ?"
        )
        now_iso = _now_iso()
        rows = self.conn.execute(enriched_sql, [now_iso, *params, limit, offset]).fetchall()

        results: list[EnrichedFileItem] = []
        for r in rows:
            d: dict[str, Any] = dict(self._build_file_record(r).to_dict())
            d["summary"] = {
                "total_findings": r["total_findings"],
                "open_findings": r["open_findings"],
                "critical": r["cnt_critical"],
                "high": r["cnt_high"],
                "medium": r["cnt_medium"],
                "low": r["cnt_low"],
                "info": r["cnt_info"],
                "suppressed": {
                    "critical": r["supp_critical"],
                    "high": r["supp_high"],
                    "medium": r["supp_medium"],
                    "low": r["supp_low"],
                    "info": r["supp_info"],
                },
            }
            d["associations_count"] = r["associations_count"]
            d["observation_count"] = r["observation_count"]
            results.append(d)  # type: ignore[arg-type]  # dict built incrementally
        return {
            "results": results,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": (offset + limit) < total,
        }

    # -- Scan ingestion ------------------------------------------------------

    @staticmethod
    def _require_str(f: dict[str, Any], key: str, idx: int, *, non_empty: bool = False) -> str:
        """Validate that finding[key] exists and is a string. Raises ValueError on failure."""
        if key not in f:
            raise ValueError(f"findings[{idx}] is missing required key '{key}'")
        val = f[key]
        if not isinstance(val, str):
            raise ValueError(f"findings[{idx}] {key} must be a string, got {type(val).__name__}")
        if non_empty and not val.strip():
            raise ValueError(f"findings[{idx}] {key} must be a non-empty string")
        return val

    @staticmethod
    def _validate_scan_findings(findings: list[dict[str, Any]], scan_source: str) -> list[str]:
        """Validate and normalize all findings upfront before any writes.

        Mutates findings in-place (normalizes paths and severities).
        Returns a list of warning messages for unknown severities.
        """
        _req = FilesMixin._require_str
        warnings: list[str] = []
        for i, f in enumerate(findings):
            if not isinstance(f, dict):
                raise ValueError(f"findings[{i}] must be a dict, got {type(f).__name__}")
            _req(f, "path", i, non_empty=True)
            f["path"] = _normalize_scan_path(f["path"])
            if not f["path"]:
                raise ValueError(f"findings[{i}] path is empty after normalization")
            if not _is_project_relative_scan_path(f["path"]):
                raise ValueError(f"findings[{i}] path must be project-relative")
            _req(f, "rule_id", i, non_empty=True)
            _req(f, "message", i, non_empty=True)
            severity = f.get("severity", "info")
            if not isinstance(severity, str):
                raise ValueError(f"findings[{i}] severity must be a string, got {type(severity).__name__}")
            for ln_field in ("line_start", "line_end"):
                ln_val = f.get(ln_field)
                if ln_val is not None and (isinstance(ln_val, bool) or not isinstance(ln_val, int)):
                    raise ValueError(f"findings[{i}] {ln_field} must be an integer or null, got {type(ln_val).__name__}")
                # Scanner line numbers are 1-based; NULL remains the only
                # representation for "line unknown".
                if isinstance(ln_val, int) and not isinstance(ln_val, bool) and ln_val < 1:
                    raise ValueError(f"findings[{i}] {ln_field} must be >= 1, got {ln_val}")
            line_start = f.get("line_start")
            line_end = f.get("line_end")
            if isinstance(line_start, int) and isinstance(line_end, int) and line_end < line_start:
                raise ValueError(f"findings[{i}] line_end must be >= line_start, got {line_end} < {line_start}")
            if "suggestion" in f:
                suggestion = f["suggestion"]
                if not isinstance(suggestion, str):
                    raise ValueError(f"findings[{i}] suggestion must be a string, got {type(suggestion).__name__}")
            if "fingerprint" in f:
                fingerprint = f["fingerprint"]
                if fingerprint is not None and not isinstance(fingerprint, str):
                    # A non-string fingerprint would bind under the column's TEXT
                    # affinity and silently break cross-run dedup; reject upfront.
                    raise ValueError(f"findings[{i}] fingerprint must be a string, got {type(fingerprint).__name__}")
            if "language" in f:
                language = f["language"]
                if language is None:
                    f["language"] = ""
                elif not isinstance(language, str):
                    raise ValueError(f"findings[{i}] language must be a string, got {type(language).__name__}")
            metadata = f.get("metadata")
            if metadata is not None and not isinstance(metadata, dict):
                raise ValueError(f"findings[{i}] metadata must be a JSON object or null, got {type(metadata).__name__}")
            # Normalize severity
            normalized = severity.strip().lower()
            if normalized in VALID_SEVERITIES:
                f["severity"] = normalized
            else:
                path = f["path"]
                rule_id = f.get("rule_id", "")
                warn_msg = f"Unknown severity {severity!r} for finding at {path} (rule_id={rule_id!r}), mapped to 'info'"
                warnings.append(warn_msg)
                logger.warning(
                    "Severity fallback: %r → 'info' for %s (rule_id=%s, scan_source=%s)",
                    severity,
                    path,
                    rule_id,
                    scan_source,
                )
                f["severity"] = "info"
        return warnings

    @staticmethod
    def _count_file_lines(path: Path) -> int | None:
        try:
            with path.open("rb") as handle:
                return sum(1 for _ in handle)
        except OSError as exc:
            logger.debug("Could not count lines for %s: %s", path, exc, exc_info=True)
            return None

    def _normalize_line_attribution_for_existing_files(self, findings: list[dict[str, Any]]) -> list[str]:
        """Reject line ranges that cannot exist in an already-present target file."""
        if self.project_root is None:
            return []

        root = self.project_root.resolve()
        line_count_cache: dict[Path, int | None] = {}
        warnings: list[str] = []
        for f in findings:
            path = f["path"]
            try:
                target = (root / path).resolve()
                target.relative_to(root)
            except (OSError, ValueError):
                continue
            if not target.is_file():
                continue
            if target not in line_count_cache:
                line_count_cache[target] = self._count_file_lines(target)
            line_count = line_count_cache[target]
            if line_count is None:
                continue

            line_label = "line" if line_count == 1 else "lines"
            rule_id = f.get("rule_id", "")
            line_start = f.get("line_start")
            line_end = f.get("line_end")
            if line_start is not None and line_start > line_count:
                raise ValueError(
                    f"Finding {rule_id!r} at {path}: line_start {line_start} exceeds file length ({path} has {line_count} {line_label})"
                )
            if line_end is not None and line_end > line_count:
                raise ValueError(
                    f"Finding {rule_id!r} at {path}: line_end {line_end} exceeds file length ({path} has {line_count} {line_label})"
                )
        return warnings

    def _upsert_file_record(
        self,
        *,
        path: str,
        language: str,
        infer_language: bool,
        now: str,
        stats: ScanIngestResult,
        counted_file_ids: set[str],
        actor: str,
        resolved_file: ResolvedFile | None = None,
    ) -> str:
        """Create or update a file record, returning its id."""
        inferred_language = _infer_language_from_path(path) if infer_language else ""

        def should_count_file(file_id: str) -> bool:
            if file_id in counted_file_ids:
                return False
            counted_file_ids.add(file_id)
            return True

        def update_existing_file(existing_file: sqlite3.Row, resolved: ResolvedFile | None = None) -> str:
            file_id: str = existing_file["id"]
            update_parts = ["updated_at = ?", "updated_by = ?"]
            update_params: list[Any] = [now, actor]
            current_language = existing_file["language"] or ""
            next_language = language or (inferred_language if not current_language else "")
            if resolved is not None:
                _normalize_registry_canonical_path(resolved["canonical_path"], requested_path=path)
                if resolved["file_id"] != file_id:
                    # A concurrent ingest committed this path under a different
                    # id between our pre-resolve and this write. A local-backend
                    # resolution mints a fresh, arbitrary id per resolve
                    # (LocalRegistry.resolve_file) — covering both pure-local and
                    # Loomweave-fallback rows — so the already-committed row is
                    # authoritative and we adopt its id rather than raising. Only
                    # a stable-id (Loomweave) mismatch is genuine registry drift,
                    # for which migrate-registry is the right remedy.
                    if resolved["registry_backend"] != "local":
                        msg = (
                            f"Existing scan file {path!r} resolves to registry id {resolved['file_id']!r}, "
                            f"but stored file id is {file_id!r}; run migrate-registry before ingesting scan results"
                        )
                        raise ValueError(msg)
                else:
                    # Ids match: sync registry-owned columns from the resolution.
                    # Skipped on a local id-adopt above so we never clobber a
                    # winner's content_hash / backend with the loser's empties.
                    for field, next_value in (
                        ("content_hash", resolved["content_hash"]),
                        ("registry_backend", resolved["registry_backend"]),
                    ):
                        current_value = existing_file[field] or ""
                        if next_value != current_value:
                            update_parts.append(f"{field} = ?")
                            update_params.append(next_value)
            if next_language:
                update_parts.append("language = ?")
                update_params.append(next_language)
            update_params.append(file_id)
            self.conn.execute(
                f"UPDATE file_records SET {', '.join(update_parts)} WHERE id = ?",
                update_params,
            )
            if should_count_file(file_id):
                stats["files_updated"] += 1
            return file_id

        existing_file = self.conn.execute(
            "SELECT id, path, language, content_hash, registry_backend FROM file_records WHERE path = ?",
            (path,),
        ).fetchone()
        if existing_file is not None:
            file_id = update_existing_file(existing_file, resolved_file)
        else:
            stored_language = language or inferred_language
            resolved = resolved_file or self.registry.resolve_file(path, language=stored_language, actor=actor)
            file_id = resolved["file_id"]
            stored_path = _normalize_registry_canonical_path(resolved["canonical_path"], requested_path=path)
            stored_language = resolved["language"] or stored_language
            content_hash = resolved["content_hash"]
            registry_backend = resolved["registry_backend"]
            storage_existing = self.conn.execute(
                "SELECT id, path, language, content_hash, registry_backend FROM file_records WHERE path = ? OR id = ?",
                (stored_path, file_id),
            ).fetchone()
            if storage_existing is not None:
                file_id = update_existing_file(storage_existing, resolved)
            else:
                try:
                    self.conn.execute(
                        "INSERT INTO file_records "
                        "(id, path, language, content_hash, registry_backend, created_by, updated_by, first_seen, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (file_id, stored_path, stored_language, content_hash, registry_backend, actor, actor, now, now),
                    )
                except sqlite3.IntegrityError:
                    storage_existing = self.conn.execute(
                        "SELECT id, path, language, content_hash, registry_backend FROM file_records WHERE path IN (?, ?) OR id = ?",
                        (path, stored_path, file_id),
                    ).fetchone()
                    if storage_existing is None:
                        raise
                    file_id = update_existing_file(storage_existing, resolved)
                else:
                    if self._is_local_registry_fallback_row(registry_backend):
                        self._record_registry_fallback_event(file_id, actor=actor, now=now)
                    if should_count_file(file_id):
                        stats["files_created"] += 1
        return file_id

    def _pre_resolve_scan_file_records(self, findings: list[dict[str, Any]], *, actor: str) -> dict[str, ResolvedFile]:
        """Resolve new scan file identities before the write transaction opens.

        CONTRACT-1 (Loomweave 1.0): unfamiliar paths are batched into a single
        ``resolve_files_batch`` call (chunked at 256 by the protocol). One HTTP
        round-trip per chunk replaces the prior N-round-trip per-finding loop.
        Briefing-blocked / not_found / structured-error per-item failures are
        promoted back to the existing per-finding raise behaviour so the
        scan-results POST keeps its fail-closed semantics.
        """
        # Deduplicate unfamiliar paths and capture the language to send.
        seen_paths: set[str] = set()
        queries: list[BatchQuery] = []
        refresh_existing = self.registry.is_displaced()
        for f in findings:
            path = f["path"]
            if path in seen_paths:
                continue
            existing_file = self.conn.execute("SELECT 1 FROM file_records WHERE path = ?", (path,)).fetchone()
            if existing_file is not None and not refresh_existing:
                seen_paths.add(path)
                continue
            inferred_language = _infer_language_from_path(path) if "language" not in f else ""
            stored_language = f.get("language", "") or inferred_language
            queries.append(BatchQuery(path=path, language=stored_language))
            seen_paths.add(path)

        if not queries:
            return {}

        # Use the registry's native batch when available; fall back to
        # looping ``resolve_file`` for registries that only implement the
        # single-item API (test fakes predating CONTRACT-1).
        batch_method = getattr(self.registry, "resolve_files_batch", None)
        batch: BatchResolution
        if batch_method is not None:
            batch = batch_method(queries, actor=actor)
        else:
            batch = resolve_files_batch_via_loop(self.registry, queries, actor=actor)
        # Promote per-item failures to the same exceptions the per-finding
        # loop used to raise (preserves caller / dashboard error mapping).
        # Use ``batch["messages"]`` to preserve the original registry-side
        # exception text when the loop-fallback adapter populated it (wire
        # batch responses leave it empty, so we fall back to a derived msg).
        messages = batch.get("messages", {})
        if batch["briefing_blocked"]:
            first = batch["briefing_blocked"][0]
            msg = messages.get(first) or f"Loomweave registry refuses briefing-blocked file at {first!r} (batch resolve)"
            raise RegistryBriefingBlockedError(msg, status_code=403, url="")
        if batch["not_found"]:
            first = batch["not_found"][0]
            msg = messages.get(first) or f"Loomweave registry could not resolve file at {first!r} (batch resolve)"
            raise RegistryFileNotFoundError(msg, status_code=404, url="")
        if batch["errors"]:
            err = batch["errors"][0]
            msg = f"Loomweave registry rejected file {err['requested_path']!r}: {err['code']} {err['message']}"
            raise RegistryResolutionError(msg, status_code=400, url="")
        return batch["resolved"]

    def _upsert_finding(
        self,
        *,
        f: dict[str, Any],
        file_id: str,
        scan_source: str,
        scan_run_id: str,
        now: str,
        stats: ScanIngestResult,
        seen_finding_ids: dict[str, list[str]],
        regressed_issue_ids: set[str],
        create_observations: bool,
        observation_actor: str = "",
        actor: str = "",
    ) -> None:
        """Upsert a single finding (dedup on file_id + scan_source + rule_id + line_start).

        ``regressed_issue_ids`` collects the linked issue ids of findings whose
        stored status was ``fixed``/``unseen_in_latest`` and that re-appear in
        this batch (``_update_existing_finding`` flips them back to ``open``).
        The caller reopens those issues post-commit (the finding→issue cascade).
        """
        severity = f.get("severity", "info")
        path = f["path"]
        rule_id = f.get("rule_id", "")
        line_start = f.get("line_start")
        dedup_line = line_start if line_start is not None else -1
        fingerprint = f.get("fingerprint") or ""

        suggestion = f.get("suggestion", "")
        if len(suggestion) > 10_000:
            logger.warning(
                "Suggestion truncated for %s (rule_id=%s): %d chars → 10000",
                path,
                rule_id,
                len(suggestion),
            )
            suggestion = suggestion[:10_000] + "\n[truncated]"

        if fingerprint:
            # Scanner-supplied fingerprint is the cross-run identity (Weft §3.B):
            # it follows the finding across line moves, so identity is keyed on
            # (scan_source, fingerprint) alone, not file/rule/line.
            existing_finding = self.conn.execute(
                "SELECT id, seen_count, scan_run_id, issue_id, status FROM scan_findings WHERE scan_source = ? AND fingerprint = ?",
                (scan_source, fingerprint),
            ).fetchone()
        else:
            # Legacy heuristic — scoped to fingerprint-less rows so a re-scan
            # without a fingerprint never collides with a fingerprint-bearing
            # row that happens to share the same site (matches the partial index).
            existing_finding = self.conn.execute(
                "SELECT id, seen_count, scan_run_id, issue_id, status FROM scan_findings "
                "WHERE file_id = ? AND scan_source = ? AND rule_id = ? "
                "AND coalesce(line_start, -1) = ? AND fingerprint = ''",
                (file_id, scan_source, rule_id, dedup_line),
            ).fetchone()

        if existing_finding is not None:
            # Capture the pre-update status before _update_existing_finding flips
            # fixed/unseen_in_latest → open, so the caller can reopen the linked
            # issue post-commit (finding→issue regress cascade).
            prior_status = existing_finding["status"]
            linked_issue_id = existing_finding["issue_id"]
            self._update_existing_finding(
                existing_finding=existing_finding,
                f=f,
                file_id=file_id,
                severity=severity,
                suggestion=suggestion,
                scan_run_id=scan_run_id,
                now=now,
                stats=stats,
                actor=actor,
            )
            seen_finding_ids.setdefault(file_id, []).append(existing_finding["id"])
            if linked_issue_id and prior_status in ("fixed", "unseen_in_latest"):
                regressed_issue_ids.add(str(linked_issue_id))
        else:
            finding_id = self._generate_unique_id("scan_findings", "sf")
            self.conn.execute(
                "INSERT INTO scan_findings "
                "(id, file_id, scan_source, rule_id, severity, status, message, "
                "suggestion, scan_run_id, "
                "line_start, line_end, fingerprint, created_by, updated_by, first_seen, updated_at, last_seen_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    finding_id,
                    file_id,
                    scan_source,
                    rule_id,
                    severity,
                    f.get("message", ""),
                    suggestion,
                    scan_run_id,
                    line_start,
                    f.get("line_end"),
                    fingerprint,
                    actor,
                    actor,
                    now,
                    now,
                    now,
                    json.dumps(f.get("metadata") or {}),
                ),
            )
            stats["findings_created"] += 1
            stats["new_finding_ids"].append(finding_id)
            seen_finding_ids.setdefault(file_id, []).append(finding_id)
            if create_observations:
                stored_file = self.conn.execute("SELECT path FROM file_records WHERE id = ?", (file_id,)).fetchone()
                observation_path = stored_file["path"] if stored_file is not None else path
                obs_summary = scan_finding_observation_summary(
                    scan_source,
                    observation_path,
                    f.get("line_start"),
                    f.get("message", ""),
                )
                obs_detail = f.get("message", "")
                if f.get("suggestion"):
                    obs_detail += f"\n\nSuggested fix:\n{f['suggestion']}"
                try:
                    self.create_observation(
                        obs_summary,
                        detail=obs_detail,
                        file_id=file_id,
                        file_path=observation_path,
                        line=f.get("line_start"),
                        # Link the observation back to the finding so
                        # dismiss_finding / promote_finding can cascade-clean
                        # the scratchpad note (filigree-cb980eee0d, P1.2).
                        source_finding_id=finding_id,
                        priority=self._SEVERITY_TO_PRIORITY.get(f.get("severity", "info"), 3),
                        actor=observation_actor or f"scanner:{scan_source}",
                        auto_commit=False,
                    )
                    stats["observations_created"] += 1
                except (sqlite3.Error, ValueError) as obs_exc:
                    logger.warning(
                        "Failed to create observation for finding %s in %s: %s",
                        finding_id,
                        path,
                        obs_exc,
                    )
                    stats["observations_failed"] += 1
                    msg = f"Observation failed for {finding_id}: {obs_exc}"
                    if msg not in stats["warnings"]:
                        stats["warnings"].append(msg)

    def _update_existing_finding(
        self,
        *,
        existing_finding: Any,
        f: dict[str, Any],
        file_id: str,
        severity: str,
        suggestion: str,
        scan_run_id: str,
        now: str,
        stats: ScanIngestResult,
        actor: str,
    ) -> None:
        """Update an already-existing finding with new scan data.

        ``file_id`` and ``line_start`` are refreshed to the current scan's
        position. For legacy (fingerprint-less) dedup these are identical to the
        stored values by construction (they are part of the dedup key), so the
        write is a no-op; for fingerprint dedup the finding's location follows it
        across line/file moves while keeping its cross-run identity.
        """
        existing_run_id = existing_finding["scan_run_id"] or ""
        run_id_update = existing_run_id
        if scan_run_id and not existing_run_id:  # first-attribution-wins
            run_id_update = scan_run_id

        self.conn.execute(
            "UPDATE scan_findings SET message = ?, severity = ?, file_id = ?, "
            "line_start = ?, line_end = ?, "
            "suggestion = ?, scan_run_id = ?, metadata = ?, "
            "seen_count = seen_count + 1, updated_at = ?, last_seen_at = ?, "
            "updated_by = ?, "
            "status = CASE WHEN status IN ('fixed', 'unseen_in_latest') THEN 'open' ELSE status END "
            "WHERE id = ?",
            (
                f.get("message", ""),
                severity,
                file_id,
                f.get("line_start"),
                f.get("line_end"),
                suggestion,
                run_id_update,
                json.dumps(f.get("metadata") or {}),
                now,
                now,
                actor,
                existing_finding["id"],
            ),
        )
        stats["findings_updated"] += 1

    @staticmethod
    def _mark_unseen_findings(
        conn: Any,
        *,
        scan_source: str,
        seen_finding_ids: dict[str, list[str]],
        now: str,
        actor: str,
        resolved: set[tuple[str, str]],
    ) -> None:
        """Mark findings not in current batch as unseen_in_latest.

        Captures into *resolved* the ``(finding_id, issue_id)`` pairs whose
        status genuinely transitions open/new → ``unseen_in_latest``
        (issue-linked only), so the caller runs the close-on-fixed cascade
        post-commit. A file with an empty seen-set — a clean file folded in via
        ``scanned_paths`` — sweeps ALL its non-terminal findings; the
        ``id NOT IN`` clause is dropped in that case to avoid the
        ``id NOT IN ()`` SQLite syntax error.
        """
        terminal = tuple(TERMINAL_FINDING_STATUSES)
        terminal_ph = ",".join("?" * len(terminal))
        for fid, fids in seen_finding_ids.items():
            not_in_clause = ""
            extra_params: list[Any] = []
            if fids:
                placeholders = ",".join("?" * len(fids))
                not_in_clause = f" AND id NOT IN ({placeholders})"
                extra_params = list(fids)
            # Capture genuine open/new → unseen transitions on issue-linked
            # findings BEFORE the UPDATE rewrites their status. The
            # ``status != 'unseen_in_latest'`` guard keeps an already-unseen
            # finding from re-firing the close cascade every batch (idempotency).
            for row in conn.execute(
                f"SELECT id, issue_id FROM scan_findings "
                f"WHERE file_id = ? AND scan_source = ? AND issue_id IS NOT NULL "
                f"AND status NOT IN ({terminal_ph}) "
                f"AND status != 'unseen_in_latest'"
                f"{not_in_clause}",
                [fid, scan_source, *terminal, *extra_params],
            ).fetchall():
                resolved.add((row["id"], str(row["issue_id"])))
            conn.execute(
                f"UPDATE scan_findings SET status = 'unseen_in_latest', updated_at = ?, updated_by = ? "
                f"WHERE file_id = ? AND scan_source = ? "
                f"AND status NOT IN ({terminal_ph})"
                f"{not_in_clause}",
                [now, actor, fid, scan_source, *terminal, *extra_params],
            )

    def process_scan_results(
        self,
        *,
        scan_source: str,
        findings: list[dict[str, Any]],
        scan_run_id: str = "",
        mark_unseen: bool = False,
        create_observations: bool = False,
        complete_scan_run: bool = True,
        observation_actor: str = "",
        scanned_paths: Sequence[str] = (),
    ) -> ScanIngestResult:
        """Ingest scan results: create/update file records and findings.

        Each finding dict must have at minimum: path, rule_id, message.
        Optional: severity (default: 'info'), language, line_start, line_end, suggestion, metadata.
        Optional ``fingerprint``: a stable per-finding hash supplied by the scanner. When
        non-empty it becomes the finding's cross-run identity (keyed with scan_source),
        so seen_count/lifecycle track it across line moves instead of the
        (file_id, scan_source, rule_id, line_start) heuristic; absent → legacy heuristic.

        When *mark_unseen* is ``True``, findings in the same (file, scan_source)
        that are NOT in this batch are set to ``unseen_in_latest`` status.
        Only findings with a non-terminal status are affected (``fixed`` and
        ``false_positive`` are left alone).

        *scanned_paths* is the authoritative set of files the scanner visited
        this run, including clean files with zero findings. The sweep above only
        visits files present in *findings*; *scanned_paths* widens it so a file
        whose last/only finding was fixed (and is therefore absent from this
        batch) is still swept — its prior findings flip to ``unseen_in_latest``,
        which the close-on-fixed cascade then resolves. Unknown clean paths (no
        prior file record) are skipped, not created. Only consulted when
        *mark_unseen* is ``True``.

        When *create_observations* is ``True``, each new finding is promoted to
        an observation for triage tracking. Pass *observation_actor* to set the
        observation's ``actor`` field — required for ``report_finding`` callers
        that want to attribute the finding to a specific agent rather than the
        default ``scanner:{scan_source}`` (F3 — review-h). Empty string falls
        back to the default.

        When *complete_scan_run* is ``False`` and a *scan_run_id* is provided,
        the scan run status is NOT transitioned to ``completed``.  Use this for
        batch scans where multiple callers share one scan_run_id — the
        orchestrator should send a final call with ``complete_scan_run=True``
        after all workers finish.

        Returns summary stats including ``new_finding_ids``.
        """
        if not isinstance(scan_source, str) or not scan_source.strip():
            raise ValueError("scan_source must be a non-empty string")
        if not isinstance(scan_run_id, str):
            raise ValueError(f"scan_run_id must be a string, got {type(scan_run_id).__name__}")
        if mark_unseen and not findings and not scanned_paths:
            raise ValueError(
                "mark_unseen=True requires at least one finding or scanned path; "
                "without either, an empty batch cannot identify which (file, scan_source) pairs to sweep"
            )
        if scan_run_id:
            scan_run = self.conn.execute("SELECT scan_source FROM scan_runs WHERE id = ?", (scan_run_id,)).fetchone()
            if scan_run is not None and scan_run["scan_source"] != scan_source:
                msg = (
                    f"scan_source mismatch for scan_run_id {scan_run_id!r}: "
                    f"existing run uses {scan_run['scan_source']!r}, got {scan_source!r}"
                )
                raise ValueError(msg)

        warnings = self._validate_scan_findings(findings, scan_source)
        warnings.extend(self._normalize_line_attribution_for_existing_files(findings))

        now = _now_iso()
        actor = observation_actor or f"scanner:{scan_source}"
        stats = ScanIngestResult(
            files_created=0,
            files_updated=0,
            findings_created=0,
            findings_updated=0,
            new_finding_ids=[],
            observations_created=0,
            observations_failed=0,
            warnings=warnings,
        )
        regressed_issue_ids: set[str] = set()
        # (finding_id, issue_id) pairs whose finding genuinely transitioned to
        # ``unseen_in_latest`` in the sweep below — the close-on-fixed cascade
        # resolves these post-commit, symmetric to regressed_issue_ids/reopen.
        resolved: set[tuple[str, str]] = set()

        # CONTRACT-E / c9196e5: resolve unfamiliar paths (the Loomweave HTTP round
        # trip) BEFORE the writer lock so concurrent ingests overlap. The write
        # window below then runs under its own BEGIN IMMEDIATE + busy-retry, the
        # same transaction discipline every other write surface uses; scan-run
        # completion afterwards is a separate transaction.
        file_resolutions = self._pre_resolve_scan_file_records(findings, actor=actor)

        self._ingest_resolved_findings(
            findings=findings,
            scan_source=scan_source,
            scan_run_id=scan_run_id,
            mark_unseen=mark_unseen,
            create_observations=create_observations,
            observation_actor=observation_actor,
            file_resolutions=file_resolutions,
            now=now,
            actor=actor,
            stats=stats,
            regressed_issue_ids=regressed_issue_ids,
            resolved=resolved,
            scanned_paths=scanned_paths,
        )

        # Post-commit finding→issue cascade: reopen issues whose linked finding
        # just regressed to ``open``. Runs OUTSIDE the ingest transaction (each
        # reopen owns its own BEGIN IMMEDIATE) and is best-effort — a transition
        # that the issue's workflow forbids must not fail the whole scan ingest.
        # A failure appends to ``stats["warnings"]``, which IS surfaced on the
        # wire (the classic envelope is a passthrough of this dict and the loom
        # adapter lifts ``warnings`` to the top level), and is now also logged
        # per-failure below so a systemic "every cascade is failing" is visible
        # in operator logs. The ``logger.info`` further down fires only for
        # SUCCESSFUL reopens.
        warnings_before = len(stats["warnings"])
        reopened_issue_ids = [
            issue_id
            for issue_id in sorted(regressed_issue_ids)
            if self._reopen_issue_for_regressed_finding(issue_id, warnings=stats["warnings"])
        ]
        for warning in stats["warnings"][warnings_before:]:
            logger.warning("finding→issue reopen cascade: %s", warning)
        if reopened_issue_ids:
            logger.info(
                "finding→issue cascade: reopened %d issue(s) on regress (scan_source=%r): %s",
                len(reopened_issue_ids),
                scan_source,
                ", ".join(reopened_issue_ids),
            )

        # Post-commit close cascade, symmetric to the reopen above: close issues
        # whose linked finding just resolved (flipped to ``unseen_in_latest`` in
        # the sweep). Same best-effort discipline — each close owns its own
        # BEGIN IMMEDIATE, the ``== "done"`` guard preserves terminal human
        # decisions, and a forbidden transition rides out in ``warnings`` (the
        # wire) plus a per-failure log line rather than failing the ingest.
        #
        # Skip any issue that ALSO had a finding regress in this same batch: a
        # regress means an active defect, so reopen wins over close. The
        # sibling-open guard in ``_close_issue_for_fixed_finding_tx`` is the
        # authoritative backstop (it re-reads sibling state under the writer
        # lock); this exclusion just avoids a doomed close attempt and the
        # reconciliation-debt churn it would log.
        warnings_before_close = len(stats["warnings"])
        close_candidates = [(finding_id, issue_id) for finding_id, issue_id in sorted(resolved) if issue_id not in regressed_issue_ids]
        closed_issue_ids = self._finding_issue_cascade_service().close_resolved_findings(close_candidates, warnings=stats["warnings"])
        for warning in stats["warnings"][warnings_before_close:]:
            logger.warning("finding→issue close cascade: %s", warning)
        if closed_issue_ids:
            logger.info(
                "finding→issue cascade: closed %d issue(s) on fix (scan_source=%r): %s",
                len(closed_issue_ids),
                scan_source,
                ", ".join(closed_issue_ids),
            )

        if scan_run_id and complete_scan_run:
            # §F6 tolerate-unknown: an enrich-only producer (e.g. Loomweave
            # `loomweave analyze`) POSTs findings under a scan_run_id Filigree
            # never created, so there is no scan_runs row to mark completed.
            # That is the normal path, not an error — skip the completion
            # attempt silently rather than emit a benign "status not updated"
            # warning on every such POST (which would train consumers to ignore
            # warnings[] entirely). Only a run that EXISTS but cannot be
            # transitioned is a real advisory worth surfacing.
            run_exists = self.conn.execute("SELECT 1 FROM scan_runs WHERE id = ?", (scan_run_id,)).fetchone() is not None
            if run_exists:
                self._complete_scan_run_with_warning(scan_run_id, stats)
            else:
                # Silent skip is correct ONLY while the invariant "scan_runs rows
                # are never deleted" holds — a missing row means tolerate-unknown
                # (enrich-only producer), never a pruned-away real run. If a
                # future retention/prune path starts deleting scan_runs rows, this
                # skip would silently swallow a legitimate completion; the debug
                # breadcrumb makes that case traceable instead of invisible.
                logger.debug(
                    "Skipping scan-run completion for %r: no scan_runs row (tolerate-unknown; assumes scan_runs rows are never deleted)",
                    scan_run_id,
                )

        return stats

    @_retry_busy()
    @_in_immediate_tx("process_scan_results")
    def _ingest_resolved_findings(
        self,
        *,
        findings: list[dict[str, Any]],
        scan_source: str,
        scan_run_id: str,
        mark_unseen: bool,
        create_observations: bool,
        observation_actor: str,
        file_resolutions: dict[str, ResolvedFile],
        now: str,
        actor: str,
        stats: ScanIngestResult,
        regressed_issue_ids: set[str],
        resolved: set[tuple[str, str]],
        scanned_paths: Sequence[str] = (),
    ) -> None:
        """Write window for :meth:`process_scan_results`.

        Upserts each file + finding, sweeps unseen findings, and bumps the
        scan-run ``findings_count`` — all inside the single ``BEGIN IMMEDIATE``
        the ``@_in_immediate_tx`` decorator owns (commit on success, rollback on
        error). ``@_retry_busy`` re-runs the whole method after a rolled-back
        transient SQLITE_BUSY, so the write-counter fields of ``stats`` are reset
        on entry to keep a retry from double-counting; ``warnings`` (computed
        before the writer lock) is deliberately preserved.
        """
        stats["files_created"] = 0
        stats["files_updated"] = 0
        stats["findings_created"] = 0
        stats["findings_updated"] = 0
        stats["observations_created"] = 0
        stats["observations_failed"] = 0
        stats["new_finding_ids"] = []
        # Reset on every entry so a @_retry_busy re-run after a rolled-back
        # transient SQLITE_BUSY does not double-accumulate regressed/resolved issues.
        regressed_issue_ids.clear()
        resolved.clear()

        seen_finding_ids: dict[str, list[str]] = {}
        counted_file_ids: set[str] = set()

        for f in findings:
            file_id = self._upsert_file_record(
                path=f["path"],
                language=f.get("language", ""),
                infer_language="language" not in f,
                now=now,
                stats=stats,
                counted_file_ids=counted_file_ids,
                actor=actor,
                resolved_file=file_resolutions.get(f["path"]),
            )
            f[INGESTED_FILE_ID_KEY] = file_id
            self._upsert_finding(
                f=f,
                file_id=file_id,
                scan_source=scan_source,
                scan_run_id=scan_run_id,
                now=now,
                stats=stats,
                seen_finding_ids=seen_finding_ids,
                regressed_issue_ids=regressed_issue_ids,
                create_observations=create_observations,
                observation_actor=observation_actor,
                actor=actor,
            )

        if mark_unseen:
            # Widen the sweep to the union of files-with-findings and scanned_paths.
            # A clean file (scanned, zero findings this batch) gets an empty
            # seen-set so all its prior non-terminal findings flip to unseen —
            # this is how close-on-fixed fires when the last/only finding in a
            # file is fixed. Resolve clean paths by lookup, not upsert: a path
            # with no prior file record can hold no findings, so skip it.
            for path in scanned_paths:
                record = self.get_file_by_path(path)
                if record is None:
                    continue
                seen_finding_ids.setdefault(record.id, [])
            self._mark_unseen_findings(
                self.conn,
                scan_source=scan_source,
                seen_finding_ids=seen_finding_ids,
                now=now,
                actor=actor,
                resolved=resolved,
            )

        # Accumulate findings_count on the scan_run row per batch.
        # Counting via SELECT ... WHERE scan_run_id = ? would undercount
        # because scan_findings.scan_run_id is first-attribution-wins
        # (see _update_existing_finding), so a re-scan that only re-sees
        # existing findings would report 0. Incrementing here handles both
        # the single-call case AND multi-batch case (the orchestrator's
        # final complete_scan_run=True call may have empty findings).
        run_observed_delta = stats["findings_created"] + stats["findings_updated"]
        if scan_run_id and run_observed_delta:
            self.conn.execute(
                "UPDATE scan_runs SET findings_count = findings_count + ? WHERE id = ?",
                (run_observed_delta, scan_run_id),
            )

    def _complete_scan_run_with_warning(self, scan_run_id: str, stats: ScanIngestResult) -> None:
        """Mark an existing scan run completed, downgrading failures to a warning.

        The caller has already confirmed a ``scan_runs`` row exists, so a
        failure here is a real (non-tolerate-unknown) condition: either the run
        is already terminal (benign, logged at INFO) or a genuine transition
        failure (logged at WARNING). Either way findings were already ingested,
        so the failure is surfaced in ``stats['warnings']`` rather than raised.
        """
        try:
            self.update_scan_run_status(
                scan_run_id,
                "completed",
            )
        except (KeyError, ValueError, sqlite3.Error) as exc:
            # Check if the scan run is already in a terminal state by
            # querying directly, rather than relying on error message text.
            try:
                row = self.conn.execute("SELECT status FROM scan_runs WHERE id = ?", (scan_run_id,)).fetchone()
                is_terminal = row is not None and row["status"] in TERMINAL_SCAN_RUN_STATUSES
            except sqlite3.Error:
                is_terminal = False
            if is_terminal:
                logger.info(
                    "Scan run %r already in terminal state, skipping completion: %s",
                    scan_run_id,
                    exc,
                )
            else:
                logger.warning(
                    "Failed to mark scan run %r as completed (findings were ingested successfully): %s",
                    scan_run_id,
                    exc,
                )
            stats["warnings"].append(f"Scan run {scan_run_id} status not updated to 'completed': {exc}")

    def get_scan_runs(self, *, limit: int = 10) -> list[ScanRunRecord]:
        """Query scan run history from the union of scan_runs and scan_findings.

        Returns a list of scan run summaries, ordered by most recent activity.
        Runs are sourced from both `scan_runs` (lifecycle table -- preserves
        clean runs with zero findings) and `scan_findings.scan_run_id`
        (legacy/orphan ingestion paths that never created a scan_runs row).
        Empty scan_run_ids are excluded from both sides.
        """
        rows = self.conn.execute(
            """
            WITH all_runs AS (
                SELECT id AS scan_run_id, scan_source FROM scan_runs WHERE id != ''
                UNION
                SELECT scan_run_id, scan_source FROM scan_findings WHERE scan_run_id != ''
            )
            SELECT
                ar.scan_run_id AS scan_run_id,
                ar.scan_source AS scan_source,
                coalesce(sr.started_at, MIN(sf.first_seen)) AS started_at,
                coalesce(sr.completed_at, sr.updated_at, MAX(sf.updated_at)) AS completed_at,
                COUNT(sf.id) AS total_findings,
                COUNT(DISTINCT sf.file_id) AS files_scanned
            FROM all_runs ar
            LEFT JOIN scan_runs sr
                ON sr.id = ar.scan_run_id AND sr.scan_source = ar.scan_source
            LEFT JOIN scan_findings sf
                ON sf.scan_run_id = ar.scan_run_id AND sf.scan_source = ar.scan_source
            GROUP BY ar.scan_run_id, ar.scan_source
            ORDER BY completed_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "scan_run_id": row["scan_run_id"],
                "scan_source": row["scan_source"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "total_findings": row["total_findings"],
                "files_scanned": row["files_scanned"],
            }
            for row in rows
        ]

    @_retry_busy()
    @_in_immediate_tx("update_finding")
    def update_finding(
        self,
        finding_id: str,
        *,
        file_id: str | None = None,
        status: FindingStatus | None = None,
        issue_id: str | None = None,
        dismiss_reason: str | None = None,
        actor: str = "",
    ) -> ScanFindingDict:
        """Update finding status and/or linked issue.

        *file_id* is optional — when omitted, it is looked up from the
        finding record.  This allows callers that only have a finding ID
        (e.g. MCP tool handlers) to update findings without knowing
        which file they belong to.
        """
        if file_id is not None:
            row = self.conn.execute(
                "SELECT id, file_id FROM scan_findings WHERE id = ? AND file_id = ?",
                (finding_id, file_id),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT id, file_id FROM scan_findings WHERE id = ?",
                (finding_id,),
            ).fetchone()
        if row is None:
            msg = f"Finding not found: {finding_id}"
            raise KeyError(msg)
        file_id = row["file_id"]

        updates: list[str] = []
        params: list[Any] = []

        if status is not None:
            if not isinstance(status, str):
                msg = "status must be a string"
                raise ValueError(msg)
            if status not in VALID_FINDING_STATUSES:
                valid = ", ".join(sorted(VALID_FINDING_STATUSES))
                msg = f'Invalid finding status "{status}". Must be one of: {valid}'
                raise ValueError(msg)
            updates.append("status = ?")
            params.append(status)

        normalized_issue_id: str | None = None
        if issue_id is not None:
            if not isinstance(issue_id, str):
                msg = "issue_id must be a string when provided"
                raise ValueError(msg)
            normalized_issue_id = issue_id.strip()
            if not normalized_issue_id:
                msg = "issue_id cannot be empty when provided"
                raise ValueError(msg)
            issue = self.conn.execute("SELECT id FROM issues WHERE id = ?", (normalized_issue_id,)).fetchone()
            if issue is None:
                msg = f'Issue not found: "{normalized_issue_id}". Verify the issue exists before linking.'
                raise ValueError(msg)
            updates.append("issue_id = ?")
            params.append(normalized_issue_id)

        if dismiss_reason is not None:
            if status is None:
                msg = "dismiss_reason requires status to also be provided"
                raise ValueError(msg)
            old_meta_raw = self.conn.execute("SELECT metadata FROM scan_findings WHERE id = ?", (finding_id,)).fetchone()
            # Use _safe_json_loads so corrupt JSON or non-dict top-level values
            # (e.g. legacy rows containing JSON arrays) reset to {} instead of
            # crashing with TypeError on the dict assignment below.
            old_meta = _safe_json_loads(
                old_meta_raw["metadata"] if old_meta_raw else None,
                f"scan_finding:{finding_id}",
            )
            old_meta["dismiss_reason"] = dismiss_reason
            updates.append("metadata = ?")
            params.append(json.dumps(old_meta))

        if not updates:
            msg = "At least one of status or issue_id must be provided"
            raise ValueError(msg)

        now = _now_iso()
        updates.append("updated_at = ?")
        params.append(now)
        updates.append("updated_by = ?")
        params.append(actor)
        params.extend([finding_id, file_id])

        # Writer lock + commit/rollback are owned by @_in_immediate_tx;
        # @_retry_busy recovers transient SQLITE_BUSY by re-running the method.
        self.conn.execute(
            f"UPDATE scan_findings SET {', '.join(updates)} WHERE id = ? AND file_id = ?",
            params,
        )

        if normalized_issue_id:
            self.conn.execute(
                "INSERT OR IGNORE INTO file_associations (file_id, issue_id, assoc_type, actor, created_at) VALUES (?, ?, 'bug_in', ?, ?)",
                (file_id, normalized_issue_id, actor, now),
            )

        # Cascade-dismiss any observation linked to this finding when the
        # finding reaches a terminal lifecycle state (dismissed, fixed,
        # promoted to issue) so the agent triage queue doesn't accumulate
        # zombie scratchpad notes for findings that have already been
        # triaged elsewhere. (filigree-cb980eee0d, P1.2.) Non-terminal
        # statuses such as 'open', 'acknowledged', and 'unseen_in_latest'
        # leave the observation alive for continued triage.
        should_cascade = (status is not None and status in TERMINAL_FINDING_STATUSES) or normalized_issue_id is not None
        if should_cascade:
            self._cascade_dismiss_observations_for_finding(
                finding_id,
                actor=actor or "system",
                reason=dismiss_reason
                or (f"finding promoted to {normalized_issue_id}" if normalized_issue_id else f"finding marked {status}"),
                now=now,
            )

        updated = self.conn.execute("SELECT * FROM scan_findings WHERE id = ?", (finding_id,)).fetchone()
        if updated is None:
            msg = f"Finding not found after update: {finding_id}"
            raise KeyError(msg)
        return self._build_scan_finding(updated).to_dict()

    def _cascade_dismiss_observations_for_finding(
        self,
        finding_id: str,
        *,
        actor: str,
        reason: str,
        now: str,
    ) -> None:
        """Dismiss observations linked to ``finding_id`` via source_finding_id.

        Inserts dismissal audit rows and deletes the live observations in a
        single statement pair, matching the contract of
        ``dismiss_observation``. Caller commits as part of the surrounding
        transaction. (filigree-cb980eee0d, P1.2.)
        """
        rows = self.conn.execute(
            "SELECT id, summary FROM observations WHERE source_finding_id = ?",
            (finding_id,),
        ).fetchall()
        if not rows:
            return
        for row in rows:
            self.conn.execute(
                "INSERT INTO dismissed_observations (obs_id, summary, actor, reason, dismissed_at) VALUES (?, ?, ?, ?, ?)",
                (row["id"], row["summary"], actor, reason, now),
            )
        self.conn.execute(
            "DELETE FROM observations WHERE source_finding_id = ?",
            (finding_id,),
        )

    def _finding_issue_cascade_service(self) -> FindingIssueCascadeService:
        return FindingIssueCascadeService(self)

    def _close_issue_for_fixed_finding(self, finding_id: str, issue_id: str, *, warnings: list[str]) -> bool:
        """Close an issue whose linked finding just went ``fixed`` (best-effort).

        Returns True iff this call closed the issue.
        """
        return self._finding_issue_cascade_service().close_fixed_finding(finding_id, issue_id, warnings=warnings)

    @_retry_busy()
    @_in_immediate_tx("close_issue_for_fixed_finding")
    def _close_issue_for_fixed_finding_tx(self, finding_id: str, issue_id: str) -> bool:
        """Atomically verify the finding is still resolved before closing its issue.

        Resolved means ``fixed`` (the age-gated clean-stale sweep) or
        ``unseen_in_latest`` (the eager scan-ingest sweep). Both callers commit
        before their best-effort issue cascades; this transaction closes the
        post-commit race: if a later ingest reopened the same finding to
        ``open`` after the sweep, the status check observes ``open`` under the
        writer lock and skips the close.
        """
        finding = self.conn.execute(
            "SELECT status FROM scan_findings WHERE id = ? AND issue_id = ?",
            (finding_id, issue_id),
        ).fetchone()
        if finding is None or finding["status"] not in ("fixed", "unseen_in_latest"):
            return False

        # Sibling-open guard: an issue may link more than one finding (a second
        # finding can be attached via ``update_finding(..., issue_id=...)``; see
        # ``get_issue_findings``). Resolving ONE of them does not mean the issue
        # is fixed — if any other linked finding is still an active defect
        # (non-terminal and not itself resolved-as-unseen), leave the issue open.
        # This also closes the same-batch reopen-then-close race: a sibling that
        # regressed to ``open`` in this ingest now blocks the close under the
        # writer lock.
        resolved_statuses = (*TERMINAL_FINDING_STATUSES, "unseen_in_latest")
        resolved_ph = ",".join("?" * len(resolved_statuses))
        sibling_open = self.conn.execute(
            f"SELECT 1 FROM scan_findings WHERE issue_id = ? AND id != ? AND status NOT IN ({resolved_ph}) LIMIT 1",
            (issue_id, finding_id, *resolved_statuses),
        ).fetchone()
        if sibling_open is not None:
            return False  # another linked finding is still an active defect

        issue = self.get_issue(issue_id)
        if self._resolve_status_category(issue.type, issue.status) == "done":
            return False  # already terminal (human or a prior cascade) — leave it
        # force=True uses the template's declared escape edge: a freshly
        # promoted bug sits at ``triage``, from which the normal workflow has no
        # single-hop edge to a done state. The cascade is exactly the
        # "intentionally leaves the normal workflow" case force=True exists for.
        self.close_issue(
            issue_id,
            reason="linked scan finding resolved (finding→issue cascade)",
            actor=FINDING_CASCADE_MARKER,
            force=True,
            _skip_begin=True,
        )
        return True

    def _issue_last_closed_by_cascade(self, issue: Issue) -> bool:
        """True iff the most recent transition *into* a done state was the cascade.

        Derived from the ``status_changed`` event history rather than a stored
        field, so a human reopen + reclose (which leaves no sticky marker to
        clear) is always honoured: the most recent into-done event would then
        carry the human's actor, not the cascade's.
        """
        return self._finding_issue_cascade_service().issue_last_closed_by_cascade(issue)

    def _reopen_issue_for_regressed_finding(self, issue_id: str, *, warnings: list[str]) -> bool:
        """Reopen a cascade-closed issue whose linked finding regressed (best-effort).

        Only reopens issues the cascade closed — gated on the most recent
        into-done transition being the cascade actor (see
        :meth:`_issue_last_closed_by_cascade`) — so a human's terminal decision
        is never overturned. Runs in its own transaction; call AFTER the ingest
        transaction commits.
        """
        return self._finding_issue_cascade_service().reopen_regressed_finding(issue_id, warnings=warnings)

    @_retry_busy()
    @_in_immediate_tx("clean_stale_findings")
    def _sweep_stale_findings_to_fixed(
        self,
        *,
        days: int,
        scan_source: str | None,
        actor: str,
    ) -> list[tuple[str, str | None]]:
        """Transaction body for :meth:`clean_stale_findings`.

        Moves ``unseen_in_latest`` findings older than *days* to ``fixed`` and
        dismisses their linked observations, all in one ``BEGIN IMMEDIATE``.
        Returns ``(finding_id, issue_id)`` for each fixed finding so the caller
        can cascade-close the linked issues post-commit.

        Writer lock + commit/rollback are owned by ``@_in_immediate_tx``;
        ``@_retry_busy`` recovers transient SQLITE_BUSY by re-running the method
        (the SELECT/UPDATE/cascade set is idempotent after a rollback).
        """
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()

        clauses = [
            "status = 'unseen_in_latest'",
            "coalesce(last_seen_at, updated_at) < ?",
        ]
        params: list[Any] = [cutoff]

        if scan_source is not None:
            clauses.append("scan_source = ?")
            params.append(scan_source)

        now = _now_iso()
        where = " AND ".join(clauses)
        fixed_rows = self.conn.execute(
            f"SELECT id, issue_id FROM scan_findings WHERE {where}",
            params,
        ).fetchall()
        self.conn.execute(
            f"UPDATE scan_findings SET status = 'fixed', updated_at = ?, updated_by = ? WHERE {where}",
            [now, actor, *params],
        )
        for row in fixed_rows:
            self._cascade_dismiss_observations_for_finding(
                row["id"],
                actor=actor or "system",
                reason="stale finding cleanup marked finding fixed",
                now=now,
            )
        return [(row["id"], row["issue_id"]) for row in fixed_rows]

    def clean_stale_findings(
        self,
        *,
        days: int = 30,
        scan_source: str | None = None,
        actor: str = "",
    ) -> CleanStaleResult:
        """Move ``unseen_in_latest`` findings older than *days* to ``fixed``.

        Only affects findings whose ``last_seen_at`` (or ``updated_at`` as
        fallback) is older than the cutoff. After the sweep commits, any fixed
        finding linked to a still-open issue cascade-closes that issue (the
        finding→issue cascade); each close runs in its own transaction and is
        best-effort, so a forbidden workflow transition is logged rather than
        failing the sweep.
        """
        fixed = self._sweep_stale_findings_to_fixed(days=days, scan_source=scan_source, actor=actor)

        warnings: list[str] = []
        valid = [(finding_id, str(issue_id)) for finding_id, issue_id in fixed if issue_id]
        closed_issue_ids = self._finding_issue_cascade_service().close_resolved_findings(valid, warnings=warnings)
        for warning in warnings:
            logger.warning("clean_stale_findings cascade: %s", warning)

        return {"findings_fixed": len(fixed), "closed_issue_ids": closed_issue_ids, "warnings": warnings}

    @staticmethod
    def _severity_bucket_sql(open_filter: str) -> str:
        """Build ``SUM(CASE WHEN severity=... AND <open_filter> ...)`` columns for all severities."""
        parts = " ".join(
            f"SUM(CASE WHEN severity='{s}' AND {open_filter} THEN 1 ELSE 0 END) AS {s}," for s in ("critical", "high", "medium", "low")
        )
        return f"{parts} SUM(CASE WHEN severity='info' AND {open_filter} THEN 1 ELSE 0 END) AS info"

    @staticmethod
    def _suppressed_severity_bucket_sql(open_filter: str) -> str:
        """Build ``SUM(...) AS supp_<severity>`` columns counting the wardline-suppressed
        subset of each open severity bucket.

        Parallel to :meth:`_severity_bucket_sql` but gated additionally on
        :func:`_wardline_suppressed_sql`, so the rollup reports how many of the
        open findings in each severity bucket are baselined/waived/judged
        (accepted) rather than actionable. The shared classifier keeps this
        identical to the row-level ``finding_list`` suppression filter — they
        cannot drift. Columns are aliased ``supp_<severity>``; the ``FROM
        scan_findings`` callers have no table alias, so ``severity`` and
        ``metadata`` are referenced bare.
        """
        supp = _wardline_suppressed_sql()
        parts = " ".join(
            f"SUM(CASE WHEN severity='{s}' AND {open_filter} AND {supp} THEN 1 ELSE 0 END) AS supp_{s},"
            for s in ("critical", "high", "medium", "low")
        )
        return f"{parts} SUM(CASE WHEN severity='info' AND {open_filter} AND {supp} THEN 1 ELSE 0 END) AS supp_info"

    def _findings_where(
        self,
        file_id: str,
        *,
        severity: Severity | None = None,
        status: FindingStatus | None = None,
        sort: str = "updated_at",
    ) -> tuple[str, list[Any], str]:
        """Build WHERE clause, params, and ORDER clause for findings queries.

        Returns ``(where, params, order_clause)`` — shared by
        ``get_findings`` and ``get_findings_paginated``.
        """
        if sort not in self._VALID_FINDING_SORTS:
            valid = ", ".join(sorted(self._VALID_FINDING_SORTS))
            raise ValueError(f'Invalid sort field "{sort}". Must be one of: {valid}')

        # ``sf.`` qualifiers: the shared WHERE/ORDER feed queries that LEFT JOIN
        # ``issues`` (N6), where ``status``/``updated_at`` are ambiguous. The
        # count path aliases ``scan_findings sf`` too (no join) so these resolve.
        clauses = ["sf.file_id = ?"]
        params: list[Any] = [file_id]

        if severity is not None:
            clauses.append("sf.severity = ?")
            params.append(severity)
        if status is not None:
            clauses.append("sf.status = ?")
            params.append(status)

        where = " AND ".join(clauses)
        order_clause = f"{self._SEVERITY_ORDER_SQL} ASC, sf.updated_at DESC" if sort == "severity" else "sf.updated_at DESC"
        return where, params, order_clause

    def get_findings(
        self,
        file_id: str,
        *,
        severity: Severity | None = None,
        status: FindingStatus | None = None,
        sort: str = "updated_at",
        limit: int = 100,
        offset: int = 0,
    ) -> list[ScanFinding]:
        """Get scan findings for a file with optional filters."""
        where, params, order_clause = self._findings_where(file_id, severity=severity, status=status, sort=sort)
        rows = self.conn.execute(
            f"SELECT {self._FINDING_SELECT_COLS} {self._FINDING_ISSUE_JOIN} WHERE {where} ORDER BY {order_clause} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [self._build_scan_finding(r) for r in rows]

    def get_findings_paginated(
        self,
        file_id: str,
        *,
        severity: Severity | None = None,
        status: FindingStatus | None = None,
        sort: str = "updated_at",
        limit: int = 100,
        offset: int = 0,
    ) -> PaginatedResult[ScanFindingDict]:
        """Get scan findings with pagination metadata.

        Returns ``{results, total, limit, offset, has_more}``.
        """
        where, params, _order = self._findings_where(file_id, severity=severity, status=status, sort=sort)

        # ``where`` carries ``sf.`` qualifiers (N6), so alias the table here too.
        # The COUNT needs no join — the filters are all finding-level.
        total: int = self.conn.execute(
            f"SELECT COUNT(*) FROM scan_findings sf WHERE {where}",
            params,
        ).fetchone()[0]

        findings = self.get_findings(file_id, severity=severity, status=status, sort=sort, limit=limit, offset=offset)
        results: list[ScanFindingDict] = [f.to_dict() for f in findings]
        return {
            "results": results,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": (offset + limit) < total,
        }

    # ------------------------------------------------------------------
    # Finding triage methods
    # ------------------------------------------------------------------

    _SEVERITY_TO_PRIORITY: ClassVar[dict[str, int]] = {
        "critical": 0,
        "high": 1,
        "medium": 2,
        "low": 3,
        "info": 3,
    }
    _FINDING_SEVERITY_TO_BUG_SEVERITY: ClassVar[dict[str, str]] = {
        "critical": "critical",
        "high": "major",
        "medium": "major",
        "low": "minor",
        "info": "cosmetic",
    }

    def get_finding(self, finding_id: str) -> ScanFindingDict:
        """Get a single finding by ID.  Raises *KeyError* if not found."""
        row = self.conn.execute(
            f"SELECT {self._FINDING_SELECT_COLS} {self._FINDING_ISSUE_JOIN} WHERE sf.id = ?",
            (finding_id,),
        ).fetchone()
        if row is None:
            msg = f"Finding not found: {finding_id}"
            raise KeyError(msg)
        return self._build_scan_finding(row).to_dict()

    def find_finding_by_fingerprint(self, scan_source: str, fingerprint: str) -> ScanFindingDict | None:
        """Resolve a finding by its ``(scan_source, fingerprint)`` identity.

        Returns the finding dict, or ``None`` when no fingerprint-bearing
        finding matches. Uses the unique partial index
        ``idx_scan_findings_fingerprint (scan_source, fingerprint)
        WHERE fingerprint <> ''``. An empty *fingerprint* never matches (those
        rows are excluded from the index) and returns ``None``; callers that
        want to reject a blank fingerprint should validate before calling.
        """
        if not fingerprint:
            return None
        row = self.conn.execute(
            "SELECT * FROM scan_findings WHERE scan_source = ? AND fingerprint = ?",
            (scan_source, fingerprint),
        ).fetchone()
        if row is None:
            return None
        return self._build_scan_finding(row).to_dict()

    def list_findings_global(
        self,
        *,
        severity: str | None = None,
        status: str | None = None,
        scan_source: str | None = None,
        scan_run_id: str | None = None,
        file_id: str | None = None,
        issue_id: str | None = None,
        fingerprint: str | None = None,
        rule_id: str | None = None,
        kind: str | None = None,
        qualname: str | None = None,
        suppression: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Project-wide finding query with optional filters.

        Beyond the flat column filters, ``kind``/``qualname``/``suppression``
        reach into the nested ``metadata.wardline.*`` axes (FIL-2/X-5) so an
        agent can answer "show me the real un-suppressed defects"
        (``kind="defect", suppression="active"``) server-side rather than
        pulling every finding and filtering nested metadata client-side.
        ``suppression="active"`` selects the un-suppressed population, classified
        by the same shared predicate ``unbridged_finding_stats`` uses to split
        ``actionable`` vs ``suppressed`` (so the classification cannot drift).
        Note this query applies no open/unbridged base filter, so its total is a
        *superset* of that ``actionable`` count unless the caller also passes
        ``status``/``issue_id``; ``baselined`` / ``waived`` / ``judged`` select
        that specific wardline verdict. ``suppression="all"`` (and the default
        ``None``) apply no suppression clause at all — every row is returned.
        The agent-facing surfaces (MCP ``finding_list`` / CLI ``list-findings``)
        default to ``active`` and pass ``all`` to opt back in (filigree-2bdb878bd2);
        this primitive keeps its all-inclusive default so internal callers (e.g.
        ``scanner_reporting`` re-finding a just-ingested row) never silently lose rows.

        Returns ``{"findings": [...], "total": N, "limit": ..., "offset": ...}``.
        """
        if severity is not None and severity not in VALID_SEVERITIES:
            valid = ", ".join(sorted(VALID_SEVERITIES))
            raise ValueError(f'Invalid severity filter "{severity}". Must be one of: {valid}')
        if status is not None and status not in VALID_FINDING_STATUSES:
            valid = ", ".join(sorted(VALID_FINDING_STATUSES))
            raise ValueError(f'Invalid status filter "{status}". Must be one of: {valid}')
        if kind is not None and kind not in VALID_WARDLINE_FINDING_KINDS:
            valid = ", ".join(sorted(VALID_WARDLINE_FINDING_KINDS))
            raise ValueError(f'Invalid kind filter "{kind}". Must be one of: {valid}')
        if suppression is not None and suppression not in VALID_SUPPRESSION_FILTERS:
            valid = ", ".join(sorted(VALID_SUPPRESSION_FILTERS))
            raise ValueError(f'Invalid suppression filter "{suppression}". Must be one of: {valid}')
        # Flat filters are simple equality on identically-named columns.
        filters = {
            "severity": severity,
            "status": status,
            "scan_source": scan_source,
            "scan_run_id": scan_run_id,
            "file_id": file_id,
            "issue_id": issue_id,
            "fingerprint": fingerprint,
            "rule_id": rule_id,
        }
        # ``sf.`` qualifiers: the rows query LEFT JOINs ``issues`` (N6), where the
        # ``status`` filter would otherwise be ambiguous (both tables have it).
        # The COUNT aliases ``scan_findings sf`` for the same qualified WHERE.
        clauses: list[str] = []
        params: list[Any] = []
        for col, val in filters.items():
            if val is not None:
                clauses.append(f"sf.{col} = ?")
                params.append(val)

        # Nested wardline metadata axes (FIL-2/X-5). These clauses and their
        # params join the SAME ``clauses``/``params`` lists the COUNT and the
        # rows query share, so the two cannot count different populations.
        if kind is not None:
            clauses.append(_wardline_field_eq_sql("kind", "sf"))
            params.append(kind)
        if qualname is not None:
            clauses.append(_wardline_field_eq_sql("qualname", "sf"))
            params.append(qualname)
        if suppression == "active":
            clauses.append(f"NOT {_wardline_suppressed_sql('sf')}")
        elif suppression not in (None, "all"):
            # ``all`` is the explicit no-filter sentinel — like ``None`` it adds
            # no clause, so it must not fall through to an equality on
            # ``suppression_state = 'all'`` (which would match nothing).
            clauses.append(_wardline_field_eq_sql("suppression_state", "sf"))
            params.append(suppression)

        where = " AND ".join(clauses) if clauses else "1=1"

        total: int = self.conn.execute(
            f"SELECT COUNT(*) FROM scan_findings sf WHERE {where}",
            params,
        ).fetchone()[0]

        rows = self.conn.execute(
            f"SELECT {self._FINDING_SELECT_COLS} {self._FINDING_ISSUE_JOIN} WHERE {where} ORDER BY sf.updated_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()

        findings = [self._build_scan_finding(r).to_dict() for r in rows]
        return {"findings": findings, "total": total, "limit": limit, "offset": offset}

    def promote_finding_to_observation(
        self,
        finding_id: str,
        *,
        priority: int | None = None,
        actor: str = "",
    ) -> ObservationDict:
        """Promote a finding to an observation.

        Creates an observation note from the finding's data.  Priority
        is inferred from severity if not provided explicitly.
        """
        finding = self.get_finding(finding_id)
        if priority is None:
            priority = self._SEVERITY_TO_PRIORITY.get(finding["severity"], 3)

        file_path = self._file_path_for_finding(finding["file_id"])
        if not file_path:
            logger.warning(
                "Promoting finding %s without file context (file_id=%s not found)",
                finding_id,
                finding["file_id"],
            )

        summary = f"[{finding['scan_source']}] {finding['message']}"
        detail = f"rule: {finding['rule_id']}, severity: {finding['severity']}"
        if not file_path:
            detail += f"\n\nNote: file record for file_id={finding['file_id']} was not found."
        return self.create_observation(
            summary,
            detail=detail,
            file_path=file_path,
            line=finding.get("line_start"),
            priority=priority,
            actor=actor,
        )

    def promote_finding_to_issue(
        self,
        finding_id: str,
        *,
        priority: int | None = None,
        actor: str = "",
        labels: list[str] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Promote a finding directly to a tracked issue.

        The older ``promote_finding_to_observation`` helper remains available
        for explicit scratchpad triage. This method backs public
        ``promote_finding`` surfaces where agents expect a real work item.

        ``labels`` lets the caller carry session-cluster context onto the
        promoted issue. The ``from-finding`` label is always added in
        addition. Senior-user MCP review run e P2.12.

        Suppression guard (weft-171fc22a50): wardline stamps suppression
        provenance onto a finding's metadata as
        ``metadata.wardline.suppression_state`` (``"baselined"`` |
        ``"waived"`` | ``"judged"``); the key is *absent* for an active
        finding. A suppressed finding is an already-accepted/suppressed defect,
        not active work — creating a *new* issue from it would manufacture false
        work. So we refuse by default (``ValueError`` → a clean VALIDATION coded
        error at the MCP/HTTP surfaces) and require an explicit ``force=True``
        override, which is recorded as a warning on the result. The guard runs
        *after* the idempotency early-returns, so re-promoting a finding that is
        already linked to an issue still returns that issue (``created=False``)
        even if it later acquired a ``suppression_state`` — that path
        manufactures no new work and so is never blocked.
        """
        actor = _validate_string(actor, "actor")
        labels = _validate_optional_string_list(labels, "labels")
        finding = self.get_finding(finding_id)
        if priority is None:
            priority = self._SEVERITY_TO_PRIORITY.get(finding["severity"], 3)

        warnings: list[str] = []

        # Idempotency first: re-promoting a finding that already links an issue
        # returns that existing issue (created=False) and manufactures no new
        # work, so it must NOT be blocked by the suppression guard below — the
        # promote-by-fingerprint surfaces are designed to be called repeatedly,
        # and a finding can acquire a suppression_state on a later rescan after
        # it was already promoted while active. The guard only protects against
        # creating a *new* issue from an accepted/suppressed defect.
        linked_issue_id = finding.get("issue_id")
        if linked_issue_id:
            try:
                issue = self.get_issue(str(linked_issue_id))
            except KeyError:
                warnings.append(f"Finding {finding_id} referenced missing issue {linked_issue_id}; creating a new issue")
            else:
                # N6 (b): a finding linked to an already-closed issue (e.g. a
                # human dismissal as ``not_a_bug``) returns that issue rather than
                # minting new work — the prior triage stands. Name the done state
                # so the caller does not read the result as fresh, open work.
                if self._resolve_status_category(issue.type, issue.status) == "done":
                    warnings.append(
                        f"Finding {finding_id} already linked to issue {issue.id}, which is closed as "
                        f"'{issue.status}'; returning the existing (dismissed) issue rather than minting "
                        "new work — its prior triage stands."
                    )
                else:
                    warnings.append(f"Finding {finding_id} already linked to issue {issue.id} (returning existing)")
                return {"issue": issue, "created": False, "warnings": warnings}

        existing_issue_row = self.conn.execute(
            "SELECT id FROM issues WHERE json_valid(fields) AND json_extract(fields, '$.source_finding_id') = ?",
            (finding_id,),
        ).fetchone()
        if existing_issue_row is not None:
            issue = self.get_issue(existing_issue_row["id"])
            try:
                self.update_finding(finding_id, issue_id=issue.id, actor=actor)
            except (KeyError, ValueError, sqlite3.Error):
                warnings.append(f"Finding {finding_id} was already promoted to issue {issue.id}, but relinking failed")
            warnings.append(f"Finding {finding_id} was already promoted to issue {issue.id} (returning existing)")
            return {"issue": issue, "created": False, "warnings": warnings}

        # Suppression guard (weft-171fc22a50): only reached when we are about to
        # create a NEW issue. A suppressed finding (baselined/waived/judged) is
        # an already-accepted defect, not active work — refuse by default and
        # require an explicit force override (recorded as a warning).
        # ``metadata.wardline`` is external scanner payload that filigree stores
        # verbatim without shape-validating nested values, so it may be a
        # non-dict (off-contract) value. Guard its type: a non-dict/absent
        # ``wardline`` node falls through to ``None`` (treat-as-active), matching
        # the "absent => active" contract.
        wardline_meta = (finding.get("metadata") or {}).get("wardline")
        suppression_state = wardline_meta.get("suppression_state") if isinstance(wardline_meta, dict) else None
        if suppression_state:
            if not force:
                msg = (
                    f"Finding {finding_id} is {suppression_state} — an accepted/suppressed defect, "
                    "not active work; promoting it generates false work. Pass force=true to promote anyway."
                )
                raise ValueError(msg)
            warnings.append(f"Promoted {suppression_state} finding {finding_id} via force override.")

        file_path = self._file_path_for_finding(finding["file_id"])
        if not file_path:
            logger.warning(
                "Promoting finding %s without file context (file_id=%s not found)",
                finding_id,
                finding["file_id"],
            )

        title = f"[{finding['scan_source']}] {finding['message']}"
        description_parts = [
            f"Scan source: {finding['scan_source']}",
            f"Rule: {finding['rule_id']}",
            f"Severity: {finding['severity']}",
        ]
        if file_path:
            location = f"`{file_path}`"
            if finding.get("line_start") is not None:
                location += f":{finding['line_start']}"
            description_parts.append(f"Finding location: {location}")
        else:
            description_parts.append(f"Finding file record was missing: {finding['file_id']}")
        if finding.get("suggestion"):
            description_parts.append(f"Suggestion: {finding['suggestion']}")

        # Carry caller-supplied session-cluster labels alongside the
        # canonical ``from-finding`` marker. dict.fromkeys preserves order
        # while de-duplicating if the caller redundantly passed
        # "from-finding".
        carry_labels = list(dict.fromkeys(["from-finding", *(labels or [])]))
        issue = self.create_issue(
            title,
            type="bug",
            priority=priority,
            description="\n\n".join(description_parts),
            fields={
                "severity": self._FINDING_SEVERITY_TO_BUG_SEVERITY.get(finding["severity"], "minor"),
                "source_finding_id": finding_id,
                "scan_source": finding["scan_source"],
                "rule_id": finding["rule_id"],
            },
            labels=carry_labels,
            actor=actor,
        )
        try:
            self.update_finding(finding_id, issue_id=issue.id, actor=actor)
        except (KeyError, ValueError, sqlite3.Error):
            warnings.append(f"Created issue {issue.id}, but linking finding {finding_id} failed")

        result: dict[str, Any] = {"issue": self.get_issue(issue.id), "created": True}
        if warnings:
            result["warnings"] = warnings
        return result

    def promote_finding_and_attach_entity(
        self,
        finding_id: str,
        entity_id: str,
        content_hash: str,
        *,
        priority: int | None = None,
        actor: str = "",
        labels: list[str] | None = None,
        entity_kind: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Promote a finding to an issue and attach an opaque entity binding.

        This composes the existing idempotent primitives so retrying the same
        request returns the existing issue and refreshes the association hash.
        Public HTTP routes run this on a private worker-thread connection.

        **Partial-state contract.** This is deliberately *not* a single
        transaction — ``promote_finding_to_issue`` commits the issue before
        ``add_entity_association`` runs. If the attach step raises, the issue
        is already promoted and committed, with no association attached. This
        is safe because both primitives are idempotent and converge on retry:
        ``promote_finding_to_issue`` reuses the existing issue via the
        finding's ``source_finding_id`` lookup, and ``add_entity_association``
        upserts on ``(issue_id, entity_id)`` and refreshes the stored content
        hash. Re-issuing the same request after a mid-operation failure
        therefore returns the existing issue with the association now present
        (see ``test_promote_and_attach_retry_converges_after_attach_failure``).
        """
        result = self.promote_finding_to_issue(finding_id, priority=priority, actor=actor, labels=labels, force=force)
        issue = result["issue"]
        association = self.add_entity_association(
            make_issue_id(issue.id),
            make_loomweave_entity_id(entity_id),
            make_content_hash(content_hash),
            actor=actor,
            entity_kind=entity_kind,
        )
        payload: dict[str, Any] = {
            "issue": self.get_issue(issue.id),
            "created": result["created"],
            "association": association,
        }
        if result.get("warnings"):
            payload["warnings"] = result["warnings"]
        return payload

    def _file_path_for_finding(self, file_id: str) -> str:
        """Look up the file path for a file_id, returning empty string if not found."""
        row = self.conn.execute("SELECT path FROM file_records WHERE id = ?", (file_id,)).fetchone()
        if row is None:
            logger.warning("File record not found for file_id=%s during finding promotion", file_id)
            return ""
        return str(row["path"])

    def get_file_findings_summary(self, file_id: str) -> FindingsSummary:
        """Get a severity-bucketed summary of findings for a file."""
        _open = self._OPEN_FINDINGS_FILTER
        _sev = self._severity_bucket_sql(_open)
        _supp = self._suppressed_severity_bucket_sql(_open)
        row = self.conn.execute(
            f"SELECT COUNT(*) AS total_findings, "
            f"SUM(CASE WHEN {_open} THEN 1 ELSE 0 END) AS open_findings, "
            f"{_sev}, {_supp} "
            f"FROM scan_findings WHERE file_id = ?",
            (file_id,),
        ).fetchone()
        return {
            "total_findings": row["total_findings"],
            "open_findings": row["open_findings"] or 0,
            "critical": row["critical"] or 0,
            "high": row["high"] or 0,
            "medium": row["medium"] or 0,
            "low": row["low"] or 0,
            "info": row["info"] or 0,
            "suppressed": {
                "critical": row["supp_critical"] or 0,
                "high": row["supp_high"] or 0,
                "medium": row["supp_medium"] or 0,
                "low": row["supp_low"] or 0,
                "info": row["supp_info"] or 0,
            },
        }

    def get_files_findings_summary(self, file_ids: list[str]) -> FindingsSummary:
        """Severity-bucketed summary aggregated across *file_ids* in one query.

        The multi-file analogue of :meth:`get_file_findings_summary`; a single
        ``IN (...)`` aggregate so a scan-run posture echo over N files stays one
        round-trip. An empty list returns the all-zero summary (no ``IN ()``).
        """
        if not file_ids:
            return {
                "total_findings": 0,
                "open_findings": 0,
                "critical": 0,
                "high": 0,
                "medium": 0,
                "low": 0,
                "info": 0,
                "suppressed": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            }
        _open = self._OPEN_FINDINGS_FILTER
        _sev = self._severity_bucket_sql(_open)
        _supp = self._suppressed_severity_bucket_sql(_open)
        placeholders = ", ".join("?" for _ in file_ids)
        row = self.conn.execute(
            f"SELECT COUNT(*) AS total_findings, "
            f"SUM(CASE WHEN {_open} THEN 1 ELSE 0 END) AS open_findings, "
            f"{_sev}, {_supp} "
            f"FROM scan_findings WHERE file_id IN ({placeholders})",
            list(file_ids),
        ).fetchone()
        return {
            "total_findings": row["total_findings"],
            "open_findings": row["open_findings"] or 0,
            "critical": row["critical"] or 0,
            "high": row["high"] or 0,
            "medium": row["medium"] or 0,
            "low": row["low"] or 0,
            "info": row["info"] or 0,
            "suppressed": {
                "critical": row["supp_critical"] or 0,
                "high": row["supp_high"] or 0,
                "medium": row["supp_medium"] or 0,
                "low": row["supp_low"] or 0,
                "info": row["supp_info"] or 0,
            },
        }

    def get_global_findings_stats(self) -> GlobalFindingsStats:
        """Get project-wide severity-bucketed findings stats."""
        _open = self._OPEN_FINDINGS_FILTER
        _sev = self._severity_bucket_sql(_open)
        _supp = self._suppressed_severity_bucket_sql(_open)
        row = self.conn.execute(
            f"SELECT COUNT(*) AS total_findings, "
            f"SUM(CASE WHEN {_open} THEN 1 ELSE 0 END) AS open_findings, "
            f"COUNT(DISTINCT CASE WHEN {_open} THEN file_id END) AS files_with_findings, "
            f"{_sev}, {_supp} "
            f"FROM scan_findings",
        ).fetchone()
        return {
            "total_findings": row["total_findings"],
            "open_findings": row["open_findings"] or 0,
            "files_with_findings": row["files_with_findings"],
            "critical": row["critical"] or 0,
            "high": row["high"] or 0,
            "medium": row["medium"] or 0,
            "low": row["low"] or 0,
            "info": row["info"] or 0,
            "suppressed": {
                "critical": row["supp_critical"] or 0,
                "high": row["supp_high"] or 0,
                "medium": row["supp_medium"] or 0,
                "low": row["supp_low"] or 0,
                "info": row["supp_info"] or 0,
            },
        }

    def unbridged_finding_stats(self) -> dict[str, int]:
        """Count OPEN analyzer findings not yet bridged to an issue (F2).

        "Un-bridged" = a non-terminal (``_OPEN_FINDINGS_FILTER``) scan finding
        with ``issue_id IS NULL`` — i.e. a live defect that has not been promoted
        to a tracked issue. Returns ``{"total", "actionable", "suppressed"}``,
        splitting the un-bridged set by the wardline ``suppression_state`` that
        survives the emit: a baselined/waived/judged finding is an already-accepted
        defect, *not* actionable work, so it must not read as a unit of work in
        session orientation. Off-contract / absent metadata counts as actionable
        (matches the promote-guard's "absent => active" contract). Terminal
        findings (fixed/false_positive) and already-bridged ones are excluded —
        only hidden work is counted.
        """
        # Guard json_extract with json_valid: a single corrupt metadata row would
        # otherwise raise OperationalError and break session context entirely.
        # Shared verbatim with the ``finding_list`` ``suppression`` filter so the
        # active/suppressed classification is computed identically on both
        # surfaces (this restricts to open + un-bridged below, so ``actionable``
        # is a subset of an unfiltered ``finding_list(suppression="active")``).
        suppressed = _wardline_suppressed_sql()
        row = self.conn.execute(
            f"SELECT "
            f"SUM(CASE WHEN {suppressed} THEN 1 ELSE 0 END) AS suppressed, "
            f"SUM(CASE WHEN NOT {suppressed} THEN 1 ELSE 0 END) AS actionable "
            f"FROM scan_findings "
            f"WHERE issue_id IS NULL AND {self._OPEN_FINDINGS_FILTER}"
        ).fetchone()
        actionable = row["actionable"] or 0
        suppressed_count = row["suppressed"] or 0
        return {"total": actionable + suppressed_count, "actionable": actionable, "suppressed": suppressed_count}

    def get_file_detail(self, file_id: str) -> FileDetail:
        """Get a structured file detail response with separated data layers."""
        f = self.get_file(file_id)
        associations = self.get_file_associations(file_id)
        recent = self.get_findings(file_id, limit=10)
        summary = self.get_file_findings_summary(file_id)
        # Observation count (no sweep, but filter expired — read-only path).
        # Guarded for pre-v7 DBs where observations table may not exist.
        has_obs_table = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'").fetchone()
        if has_obs_table:
            obs_count = self.conn.execute(
                "SELECT COUNT(*) FROM observations WHERE file_id = ? AND expires_at > ?",
                (file_id, _now_iso()),
            ).fetchone()[0]
        else:
            obs_count = 0
        return {
            "file": f.to_dict(),
            "associations": associations,
            "recent_findings": [r.to_dict() for r in recent],
            "summary": summary,
            "observation_count": obs_count,
        }

    # -- File associations ---------------------------------------------------

    def add_file_association(
        self,
        file_id: str,
        issue_id: str,
        assoc_type: AssocType,
        *,
        actor: str = "",
    ) -> None:
        """Link a file to an issue. Idempotent (duplicates ignored)."""
        if assoc_type not in VALID_ASSOC_TYPES:
            msg = f'Invalid assoc_type "{assoc_type}". Must be one of: {", ".join(sorted(VALID_ASSOC_TYPES))}'
            raise ValueError(msg)
        # Validate issue exists before creating the association
        row = self.conn.execute("SELECT 1 FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None:
            msg = f'Issue not found: "{issue_id}". Verify the issue exists before creating an association.'
            raise ValueError(msg)
        now = _now_iso()
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO file_associations (file_id, issue_id, assoc_type, actor, created_at) VALUES (?, ?, ?, ?, ?)",
                (file_id, issue_id, assoc_type, actor, now),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def get_file_associations(self, file_id: str) -> list[FileAssociation]:
        """Get all issue associations for a file."""
        rows = self.conn.execute(
            "SELECT fa.*, i.title as issue_title, i.status as issue_status "
            "FROM file_associations fa "
            "LEFT JOIN issues i ON fa.issue_id = i.id "
            "WHERE fa.file_id = ? ORDER BY fa.created_at DESC",
            (file_id,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "file_id": r["file_id"],
                "issue_id": r["issue_id"],
                "assoc_type": r["assoc_type"],
                "actor": r["actor"] or "",
                "created_at": r["created_at"],
                "issue_title": r["issue_title"],
                "issue_status": r["issue_status"],
            }
            for r in rows
        ]

    def get_issue_files(self, issue_id: str) -> list[IssueFileAssociation]:
        """Get all files associated with an issue (issue -> files direction)."""
        rows = self.conn.execute(
            "SELECT fa.*, fr.path as file_path, fr.language as file_language "
            "FROM file_associations fa "
            "JOIN file_records fr ON fa.file_id = fr.id "
            "WHERE fa.issue_id = ? ORDER BY fa.created_at DESC",
            (issue_id,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "file_id": r["file_id"],
                "issue_id": r["issue_id"],
                "assoc_type": r["assoc_type"],
                "actor": r["actor"] or "",
                "created_at": r["created_at"],
                "file_path": r["file_path"],
                "file_language": r["file_language"],
            }
            for r in rows
        ]

    def get_issue_findings(self, issue_id: str) -> list[ScanFinding]:
        """Get all scan findings related to an issue."""
        rows = self.conn.execute(
            "SELECT sf.* FROM scan_findings sf WHERE sf.issue_id = ? "
            "UNION "
            "SELECT sf.* FROM scan_findings sf "
            "JOIN file_associations fa ON sf.file_id = fa.file_id "
            "WHERE fa.issue_id = ?",
            (issue_id, issue_id),
        ).fetchall()
        return [self._build_scan_finding(r) for r in rows]

    def get_file_hotspots(self, *, limit: int = 10) -> list[FileHotspot]:
        """Get files ranked by weighted finding severity score."""
        rows = self.conn.execute(
            f"""
            SELECT
                fr.id, fr.path, fr.language,
                SUM(CASE WHEN sf.severity = 'critical' THEN 1 ELSE 0 END) as cnt_critical,
                SUM(CASE WHEN sf.severity = 'high' THEN 1 ELSE 0 END) as cnt_high,
                SUM(CASE WHEN sf.severity = 'medium' THEN 1 ELSE 0 END) as cnt_medium,
                SUM(CASE WHEN sf.severity = 'low' THEN 1 ELSE 0 END) as cnt_low,
                SUM(CASE WHEN sf.severity = 'info' THEN 1 ELSE 0 END) as cnt_info,
                SUM(
                    CASE sf.severity
                        WHEN 'critical' THEN 10
                        WHEN 'high' THEN 5
                        WHEN 'medium' THEN 2
                        WHEN 'low' THEN 1
                        ELSE 0
                    END
                ) as score
            FROM file_records fr
            JOIN scan_findings sf ON sf.file_id = fr.id
            WHERE {self._OPEN_FINDINGS_FILTER_SF}
            GROUP BY fr.id
            HAVING score > 0
            ORDER BY score DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        return [
            {
                "file": {"id": r["id"], "path": r["path"], "language": r["language"]},
                "score": r["score"],
                "findings_breakdown": {
                    "critical": r["cnt_critical"],
                    "high": r["cnt_high"],
                    "medium": r["cnt_medium"],
                    "low": r["cnt_low"],
                    "info": r["cnt_info"],
                },
            }
            for r in rows
        ]

    # -- File Timeline -------------------------------------------------------

    @staticmethod
    def _timeline_cte(*, include_issue_events: bool) -> str:
        issue_events_sql = (
            """
        UNION ALL
        SELECT 'issue_event' AS type, e.created_at AS timestamp,
               CAST(e.id AS TEXT) AS source_id,
               json_object('issue_id', e.issue_id,
                           'issue_title', COALESCE(i.title, ''),
                           'event_type', e.event_type,
                           'actor', e.actor,
                           'old_value', e.old_value,
                           'new_value', e.new_value,
                           'comment', e.comment) AS data_json
        FROM events e
        JOIN (SELECT DISTINCT issue_id FROM file_associations WHERE file_id = ?) fa ON fa.issue_id = e.issue_id
        LEFT JOIN issues i ON e.issue_id = i.id
            """
            if include_issue_events
            else ""
        )
        return f"""
    WITH timeline AS (
        SELECT 'finding_created' AS type, first_seen AS timestamp,
               id AS source_id,
               json_object('scan_source', scan_source, 'rule_id', rule_id,
                           'severity', severity, 'message', message,
                           'actor', created_by) AS data_json
        FROM scan_findings WHERE file_id = ?
        UNION ALL
        SELECT 'finding_updated' AS type, updated_at AS timestamp,
               id AS source_id,
               json_object('scan_source', scan_source, 'rule_id', rule_id,
                           'severity', severity, 'status', status,
                           'actor', updated_by) AS data_json
        FROM scan_findings WHERE file_id = ? AND updated_at != first_seen
        UNION ALL
        SELECT 'association_created' AS type, fa.created_at AS timestamp,
               CAST(fa.id AS TEXT) AS source_id,
               json_object('issue_id', fa.issue_id,
                           'issue_title', COALESCE(i.title, ''),
                           'assoc_type', fa.assoc_type,
                           'actor', fa.actor) AS data_json
        FROM file_associations fa
        LEFT JOIN issues i ON fa.issue_id = i.id
        WHERE fa.file_id = ?
        UNION ALL
        SELECT 'file_metadata_update' AS type, created_at AS timestamp,
               CAST(id AS TEXT) AS source_id,
               json_object('field', field, 'old_value', old_value,
                           'new_value', new_value,
                           'actor', actor) AS data_json
        FROM file_events WHERE file_id = ?
        {issue_events_sql}
    )
    """

    _TIMELINE_TYPE_FILTERS: ClassVar[dict[str, str]] = {
        "finding": "WHERE type IN ('finding_created', 'finding_updated')",
        "association": "WHERE type = 'association_created'",
        "file_metadata_update": "WHERE type = 'file_metadata_update'",
        "issue_event": "WHERE type = 'issue_event'",
    }

    def get_file_timeline(
        self,
        file_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        event_type: str | None = None,
        include_issue_events: bool = False,
    ) -> PaginatedResult[TimelineEntry]:
        """Build a merged timeline of events for a file.

        Assembles entries from scan findings and file associations, sorted
        newest-first.  Each entry carries a deterministic ``id`` derived from
        ``sha256(type + timestamp + source_id)[:12]`` so clients can
        cache/deduplicate without server coordination.

        Pagination is pushed to SQL via UNION ALL + ORDER BY + LIMIT/OFFSET
        so only the requested page is materialized in Python.
        """
        self.get_file(file_id)  # validate existence
        if not isinstance(include_issue_events, bool):
            msg = "include_issue_events must be a boolean"
            raise ValueError(msg)

        if event_type is not None and event_type not in self._TIMELINE_TYPE_FILTERS:
            valid_types = tuple(self._TIMELINE_TYPE_FILTERS)
            raise ValueError(f'Invalid event_type "{event_type}". Must be one of: {", ".join(valid_types)}')

        include_issue_events = include_issue_events or event_type == "issue_event"
        type_filter = self._TIMELINE_TYPE_FILTERS[event_type] if event_type else ""
        base_params: list[Any] = [file_id, file_id, file_id, file_id]
        if include_issue_events:
            base_params.append(file_id)
        timeline_cte = self._timeline_cte(include_issue_events=include_issue_events)

        total_row = self.conn.execute(
            f"{timeline_cte} SELECT COUNT(*) FROM timeline {type_filter}",
            base_params,
        ).fetchone()
        total: int = total_row[0]

        rows = self.conn.execute(
            f"{timeline_cte} SELECT type, timestamp, source_id, data_json "
            f"FROM timeline {type_filter} "
            f"ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            [*base_params, limit, offset],
        ).fetchall()

        entries: list[TimelineEntry] = []
        for r in rows:
            raw = f"{r['type']}:{r['timestamp']}:{r['source_id']}"
            entries.append(
                {
                    "id": hashlib.sha256(raw.encode()).hexdigest()[:12],
                    "type": r["type"],
                    "timestamp": r["timestamp"],
                    "source_id": r["source_id"],
                    "data": _safe_json_loads(r["data_json"], f"timeline:{r['source_id']}"),
                }
            )

        return {
            "results": entries,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < total,
        }
