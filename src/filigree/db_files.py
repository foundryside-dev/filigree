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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, get_args

from filigree.db_base import DBMixinProtocol, _escape_like_chars, _now_iso, _safe_json_loads
from filigree.models import FileRecord, ScanFinding
from filigree.registry import (
    BatchQuery,
    BatchResolution,
    RegistryBriefingBlockedError,
    RegistryFileNotFoundError,
    RegistryResolutionError,
    resolve_files_batch_via_loop,
)
from filigree.types.core import AssocType, FindingStatus, Severity
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
        """Build a ScanFinding from a database row."""
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
            issue_id=row["issue_id"],
            seen_count=row["seen_count"] or 1,
            created_by=row["created_by"] or "",
            updated_by=row["updated_by"] or "",
            first_seen=row["first_seen"],
            updated_at=row["updated_at"],
            last_seen_at=row["last_seen_at"],
            metadata=self._parse_metadata(row["metadata"], f"scan_finding:{row['file_id']}"),
        )

    def _is_local_registry_fallback_row(self, registry_backend: str) -> bool:
        # ``registry_backend`` and ``allow_local_fallback`` are always set on
        # ``FiligreeDB.__init__`` before any DB call reaches a mixin method;
        # attribute access is safe without a default.
        return self.registry_backend == "clarion" and bool(self.allow_local_fallback) and registry_backend == "local"

    def _record_registry_fallback_event(self, file_id: str, *, actor: str, now: str) -> None:
        self.conn.execute(
            "INSERT INTO file_events "
            "(file_id, event_type, field, old_value, new_value, actor, created_at) "
            "VALUES (?, 'registry_local_fallback', 'registry_backend', 'clarion', 'local', ?, ?)",
            (file_id, actor, now),
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
            )
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
            self.conn.commit()
        except sqlite3.IntegrityError:
            self.conn.rollback()
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
                )
            raise
        except Exception:
            self.conn.rollback()
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
        try:
            self.conn.execute(
                f"UPDATE file_records SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            for field, old_val, new_val in changes:
                self.conn.execute(
                    "INSERT INTO file_events "
                    "(file_id, event_type, field, old_value, new_value, actor, created_at) "
                    "VALUES (?, 'file_metadata_update', ?, ?, ?, ?, ?)",
                    (existing["id"], field, old_val, new_val, actor, now),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
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
        _sev_cols = " ".join(
            f"(SELECT COUNT(*) FROM scan_findings sf WHERE sf.file_id = fr.id AND {_open} AND sf.severity='{s}') AS cnt_{s},"
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
            f"{_sev_cols} "
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
        """Clear or clamp line ranges that cannot exist in the target file."""
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
                f["line_start"] = None
                if line_end is not None:
                    f["line_end"] = None
                warnings.append(
                    f"Finding {rule_id!r} at {path}: line_start {line_start} exceeds file length "
                    f"({path} has {line_count} {line_label}); line attribution cleared."
                )
                continue
            if line_end is not None and line_end > line_count:
                f["line_end"] = line_count if line_count > 0 else None
                warnings.append(
                    f"Finding {rule_id!r} at {path}: line_end {line_end} exceeds file length "
                    f"({path} has {line_count} {line_label}); line_end clamped."
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
                    msg = (
                        f"Existing scan file {path!r} resolves to registry id {resolved['file_id']!r}, "
                        f"but stored file id is {file_id!r}; run migrate-registry before ingesting scan results"
                    )
                    raise ValueError(msg)
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

        CONTRACT-1 (Clarion 1.0): unfamiliar paths are batched into a single
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
            msg = messages.get(first) or f"Clarion registry refuses briefing-blocked file at {first!r} (batch resolve)"
            raise RegistryBriefingBlockedError(msg, status_code=403, url="")
        if batch["not_found"]:
            first = batch["not_found"][0]
            msg = messages.get(first) or f"Clarion registry could not resolve file at {first!r} (batch resolve)"
            raise RegistryFileNotFoundError(msg, status_code=404, url="")
        if batch["errors"]:
            err = batch["errors"][0]
            msg = f"Clarion registry rejected file {err['requested_path']!r}: {err['code']} {err['message']}"
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
        create_observations: bool,
        observation_actor: str = "",
        actor: str = "",
    ) -> None:
        """Upsert a single finding (dedup on file_id + scan_source + rule_id + line_start)."""
        severity = f.get("severity", "info")
        path = f["path"]
        rule_id = f.get("rule_id", "")
        line_start = f.get("line_start")
        dedup_line = line_start if line_start is not None else -1

        suggestion = f.get("suggestion", "")
        if len(suggestion) > 10_000:
            logger.warning(
                "Suggestion truncated for %s (rule_id=%s): %d chars → 10000",
                path,
                rule_id,
                len(suggestion),
            )
            suggestion = suggestion[:10_000] + "\n[truncated]"

        existing_finding = self.conn.execute(
            "SELECT id, seen_count, scan_run_id, issue_id FROM scan_findings "
            "WHERE file_id = ? AND scan_source = ? AND rule_id = ? AND coalesce(line_start, -1) = ?",
            (file_id, scan_source, rule_id, dedup_line),
        ).fetchone()

        if existing_finding is not None:
            self._update_existing_finding(
                existing_finding=existing_finding,
                f=f,
                severity=severity,
                suggestion=suggestion,
                scan_run_id=scan_run_id,
                now=now,
                stats=stats,
                actor=actor,
            )
            seen_finding_ids.setdefault(file_id, []).append(existing_finding["id"])
        else:
            finding_id = self._generate_unique_id("scan_findings", "sf")
            self.conn.execute(
                "INSERT INTO scan_findings "
                "(id, file_id, scan_source, rule_id, severity, status, message, "
                "suggestion, scan_run_id, "
                "line_start, line_end, created_by, updated_by, first_seen, updated_at, last_seen_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                obs_summary = scan_finding_observation_summary(
                    scan_source,
                    path,
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
                        file_path=path,
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
        severity: str,
        suggestion: str,
        scan_run_id: str,
        now: str,
        stats: ScanIngestResult,
        actor: str,
    ) -> None:
        """Update an already-existing finding with new scan data."""
        existing_run_id = existing_finding["scan_run_id"] or ""
        run_id_update = existing_run_id
        if scan_run_id and not existing_run_id:  # first-attribution-wins
            run_id_update = scan_run_id

        self.conn.execute(
            "UPDATE scan_findings SET message = ?, severity = ?, line_end = ?, "
            "suggestion = ?, scan_run_id = ?, metadata = ?, "
            "seen_count = seen_count + 1, updated_at = ?, last_seen_at = ?, "
            "updated_by = ?, "
            "status = CASE WHEN status IN ('fixed', 'unseen_in_latest') THEN 'open' ELSE status END "
            "WHERE id = ?",
            (
                f.get("message", ""),
                severity,
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
    ) -> None:
        """Mark findings not in current batch as unseen_in_latest."""
        terminal = tuple(TERMINAL_FINDING_STATUSES)
        terminal_ph = ",".join("?" * len(terminal))
        for fid, fids in seen_finding_ids.items():
            placeholders = ",".join("?" * len(fids))
            conn.execute(
                f"UPDATE scan_findings SET status = 'unseen_in_latest', updated_at = ?, updated_by = ? "
                f"WHERE file_id = ? AND scan_source = ? "
                f"AND status NOT IN ({terminal_ph}) "
                f"AND id NOT IN ({placeholders})",
                [now, actor, fid, scan_source, *terminal, *fids],
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
    ) -> ScanIngestResult:
        """Ingest scan results: create/update file records and findings.

        Each finding dict must have at minimum: path, rule_id, message.
        Optional: severity (default: 'info'), language, line_start, line_end, suggestion, metadata.

        When *mark_unseen* is ``True``, findings in the same (file, scan_source)
        that are NOT in this batch are set to ``unseen_in_latest`` status.
        Only findings with a non-terminal status are affected (``fixed`` and
        ``false_positive`` are left alone).

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
        if mark_unseen and not findings:
            raise ValueError(
                "mark_unseen=True requires at least one finding; an empty batch cannot identify which (file, scan_source) pairs to sweep"
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

        seen_finding_ids: dict[str, list[str]] = {}
        counted_file_ids: set[str] = set()
        file_resolutions = self._pre_resolve_scan_file_records(findings, actor=actor)

        try:
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
                    create_observations=create_observations,
                    observation_actor=observation_actor,
                    actor=actor,
                )

            if mark_unseen:
                self._mark_unseen_findings(
                    self.conn,
                    scan_source=scan_source,
                    seen_finding_ids=seen_finding_ids,
                    now=now,
                    actor=actor,
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

            self.conn.commit()
        except Exception:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise

        if scan_run_id and complete_scan_run:
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
                    is_terminal = row is not None and row["status"] in ("completed", "failed")
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

        return stats

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

        try:
            self.conn.execute(
                f"UPDATE scan_findings SET {', '.join(updates)} WHERE id = ? AND file_id = ?",
                params,
            )

            if normalized_issue_id:
                self.conn.execute(
                    "INSERT OR IGNORE INTO file_associations (file_id, issue_id, assoc_type, actor, created_at) "
                    "VALUES (?, ?, 'bug_in', ?, ?)",
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

            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
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

    def clean_stale_findings(
        self,
        *,
        days: int = 30,
        scan_source: str | None = None,
        actor: str = "",
    ) -> CleanStaleResult:
        """Move ``unseen_in_latest`` findings older than *days* to ``fixed``.

        Only affects findings whose ``last_seen_at`` (or ``updated_at`` as
        fallback) is older than the cutoff.  Returns stats about what changed.
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
        try:
            fixed_rows = self.conn.execute(
                f"SELECT id FROM scan_findings WHERE {where}",
                params,
            ).fetchall()
            cursor = self.conn.execute(
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
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"findings_fixed": cursor.rowcount}

    @staticmethod
    def _severity_bucket_sql(open_filter: str) -> str:
        """Build ``SUM(CASE WHEN severity=... AND <open_filter> ...)`` columns for all severities."""
        parts = " ".join(
            f"SUM(CASE WHEN severity='{s}' AND {open_filter} THEN 1 ELSE 0 END) AS {s}," for s in ("critical", "high", "medium", "low")
        )
        return f"{parts} SUM(CASE WHEN severity='info' AND {open_filter} THEN 1 ELSE 0 END) AS info"

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

        clauses = ["file_id = ?"]
        params: list[Any] = [file_id]

        if severity is not None:
            clauses.append("severity = ?")
            params.append(severity)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)

        where = " AND ".join(clauses)
        order_clause = f"{self._SEVERITY_ORDER_SQL} ASC, updated_at DESC" if sort == "severity" else "updated_at DESC"
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
            f"SELECT * FROM scan_findings WHERE {where} ORDER BY {order_clause} LIMIT ? OFFSET ?",
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

        total: int = self.conn.execute(
            f"SELECT COUNT(*) FROM scan_findings WHERE {where}",
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

    def get_finding(self, finding_id: str) -> ScanFindingDict:
        """Get a single finding by ID.  Raises *KeyError* if not found."""
        row = self.conn.execute(
            "SELECT * FROM scan_findings WHERE id = ?",
            (finding_id,),
        ).fetchone()
        if row is None:
            msg = f"Finding not found: {finding_id}"
            raise KeyError(msg)
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
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Project-wide finding query with optional filters.

        Returns ``{"findings": [...], "total": N, "limit": ..., "offset": ...}``.
        """
        if severity is not None and severity not in VALID_SEVERITIES:
            valid = ", ".join(sorted(VALID_SEVERITIES))
            raise ValueError(f'Invalid severity filter "{severity}". Must be one of: {valid}')
        if status is not None and status not in VALID_FINDING_STATUSES:
            valid = ", ".join(sorted(VALID_FINDING_STATUSES))
            raise ValueError(f'Invalid status filter "{status}". Must be one of: {valid}')
        # All filters are simple equality on identically-named columns.
        filters = {
            "severity": severity,
            "status": status,
            "scan_source": scan_source,
            "scan_run_id": scan_run_id,
            "file_id": file_id,
            "issue_id": issue_id,
        }
        clauses: list[str] = []
        params: list[Any] = []
        for col, val in filters.items():
            if val is not None:
                clauses.append(f"{col} = ?")
                params.append(val)

        where = " AND ".join(clauses) if clauses else "1=1"

        total: int = self.conn.execute(
            f"SELECT COUNT(*) FROM scan_findings WHERE {where}",
            params,
        ).fetchone()[0]

        rows = self.conn.execute(
            f"SELECT * FROM scan_findings WHERE {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
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
    ) -> dict[str, Any]:
        """Promote a finding directly to a tracked issue.

        The older ``promote_finding_to_observation`` helper remains available
        for explicit scratchpad triage. This method backs public
        ``promote_finding`` surfaces where agents expect a real work item.

        ``labels`` lets the caller carry session-cluster context onto the
        promoted issue. The ``from-finding`` label is always added in
        addition. Senior-user MCP review run e P2.12.
        """
        actor = _validate_string(actor, "actor")
        labels = _validate_optional_string_list(labels, "labels")
        finding = self.get_finding(finding_id)
        if priority is None:
            priority = self._SEVERITY_TO_PRIORITY.get(finding["severity"], 3)

        warnings: list[str] = []
        linked_issue_id = finding.get("issue_id")
        if linked_issue_id:
            try:
                issue = self.get_issue(str(linked_issue_id))
            except KeyError:
                warnings.append(f"Finding {finding_id} referenced missing issue {linked_issue_id}; creating a new issue")
            else:
                warnings.append(f"Finding {finding_id} already linked to issue {issue.id} (returning existing)")
                return {"issue": issue, "warnings": warnings}

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
            return {"issue": issue, "warnings": warnings}

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

        result: dict[str, Any] = {"issue": self.get_issue(issue.id)}
        if warnings:
            result["warnings"] = warnings
        return result

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
        row = self.conn.execute(
            f"SELECT COUNT(*) AS total_findings, "
            f"SUM(CASE WHEN {_open} THEN 1 ELSE 0 END) AS open_findings, "
            f"{_sev} "
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
        }

    def get_global_findings_stats(self) -> GlobalFindingsStats:
        """Get project-wide severity-bucketed findings stats."""
        _open = self._OPEN_FINDINGS_FILTER
        _sev = self._severity_bucket_sql(_open)
        row = self.conn.execute(
            f"SELECT COUNT(*) AS total_findings, "
            f"SUM(CASE WHEN {_open} THEN 1 ELSE 0 END) AS open_findings, "
            f"COUNT(DISTINCT CASE WHEN {_open} THEN file_id END) AS files_with_findings, "
            f"{_sev} "
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
        }

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
