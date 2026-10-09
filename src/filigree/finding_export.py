"""Export-and-drop of stored Wardline telemetry findings (Stage 0, Task 0.5c).

Stage 0 stopped Wardline engine telemetry (the non-defect kinds
``fact`` / ``classification`` / ``metric`` / ``suggestion`` and ``<engine>``
pseudo-path rows) from entering the tracker. This module disposes of the rows
a 3.x ingest already stored. It is operator-invoked through
``filigree finding export`` and never runs implicitly.

Selection
---------
A ``scan_findings`` row is selected when its stored ``metadata.wardline.kind``
is a KNOWN non-defect kind (the ``CASE``-guarded
``_wardline_non_defect_side_sql``) or its file record is the ``<engine>``
pseudo-path (which the 0.5a ingest rejects whatever its kind — it is never a
file with work on it). FIL-1: on a real path, a row whose kind is missing,
corrupt, ``'{}'``, ``NULL`` or unknown is defect-side and is never selected.
Every status is selected, including ``unseen_in_latest`` rows a 3.3 sweep left.

Safety properties
-----------------
- **Export before delete, structurally.** :func:`delete_exported_findings`
  takes the :class:`FindingExport` that :func:`export_telemetry_findings`
  returns only after the JSONL, its sha256 sidecar and their directory entry
  were ``fsync``-ed in this process; a pre-existing archive file is never
  accepted as proof of export.
- **Delete exactly what was exported.** The delete runs over the exported id
  snapshot (re-checking the selection predicate under the writer lock), never
  over a fresh query, so a row stored after the export is not dropped
  unexported.
- **One writer transaction.** Findings and the file records they leave
  unreferenced are dropped in a single ``BEGIN IMMEDIATE``; nothing is written
  to the ``issues`` or ``events`` tables.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from filigree.db_base import _begin_immediate
from filigree.db_files import WARDLINE_ENGINE_PSEUDO_PATH, _wardline_non_defect_side_sql

if TYPE_CHECKING:
    from filigree.core import FiligreeDB

#: Default archive location, relative to the project root.
DEFAULT_EXPORT_RELPATH = Path("archive") / "telemetry-3x.jsonl"

# Comfortably under SQLite's historical 999 host-parameter limit.
_ID_CHUNK = 500

_SELECTION = (
    f"FROM scan_findings sf JOIN file_records fr ON fr.id = sf.file_id WHERE ({_wardline_non_defect_side_sql('sf')} OR fr.path = ?)"
)


class ExportTargetExistsError(FileExistsError):
    """The archive (or its sidecar) already exists and ``force`` was not given."""


@dataclass(frozen=True)
class FindingExport:
    """Proof that an export was written and fsynced in this process."""

    out: Path
    sidecar: Path
    sha256: str
    finding_ids: tuple[str, ...]


def sidecar_path(out: Path) -> Path:
    """The sha256sum-format sidecar for *out* (``<out>.sha256``)."""
    return out.with_name(out.name + ".sha256")


def count_telemetry_findings(db: FiligreeDB) -> int:
    """Number of stored rows the export would select (writes nothing)."""
    return int(db.conn.execute(f"SELECT COUNT(*) {_SELECTION}", (WARDLINE_ENGINE_PSEUDO_PATH,)).fetchone()[0])


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _iter_export_lines(conn: sqlite3.Connection) -> Iterator[tuple[str, bytes]]:
    """Yield ``(finding_id, jsonl_line)`` for every selected row, ordered by id.

    One statement, so the rows are a single consistent snapshot. Each line
    nests the raw stored ``scan_findings`` row and its ``file_records`` row
    (both tables share column names such as ``id`` and ``metadata``, so a flat
    merge would clobber); ``metadata`` stays the stored text, never re-parsed.
    """
    finding_cols = _columns(conn, "scan_findings")
    file_cols = _columns(conn, "file_records")
    select = ", ".join(
        [f'sf."{c}" AS "f{i}"' for i, c in enumerate(finding_cols)] + [f'fr."{c}" AS "r{i}"' for i, c in enumerate(file_cols)]
    )
    cursor = conn.execute(f"SELECT {select} {_SELECTION} ORDER BY sf.id", (WARDLINE_ENGINE_PSEUDO_PATH,))
    for row in cursor:
        finding = {c: row[f"f{i}"] for i, c in enumerate(finding_cols)}
        file_record = {c: row[f"r{i}"] for i, c in enumerate(file_cols)}
        line = json.dumps({"finding": finding, "file": file_record}, sort_keys=True) + "\n"
        yield str(finding["id"]), line.encode("utf-8")


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_durably(target: Path, chunks: Iterator[bytes]) -> None:
    """Write *chunks* to a temp file beside *target*, fsync it, then rename over *target*."""
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as handle:
            for chunk in chunks:
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def export_telemetry_findings(db: FiligreeDB, out: Path, *, force: bool = False) -> FindingExport:
    """Write every selected row to *out* (JSONL) plus a ``<out>.sha256`` sidecar.

    Refuses (:class:`ExportTargetExistsError`) when either file exists unless
    *force*. Creates the parent directory. Both files and the directory entry
    are fsynced before this returns; any I/O failure raises and leaves no
    partially-written archive in place.
    """
    sidecar = sidecar_path(out)
    if not force:
        for target in (out, sidecar):
            if target.exists():
                raise ExportTargetExistsError(f"{target} already exists; pass --force to overwrite it")
    created_dir = not out.parent.exists()
    out.parent.mkdir(parents=True, exist_ok=True)

    hasher = hashlib.sha256()
    finding_ids: list[str] = []

    def _lines() -> Iterator[bytes]:
        for finding_id, line in _iter_export_lines(db.conn):
            finding_ids.append(finding_id)
            hasher.update(line)
            yield line

    _write_durably(out, _lines())
    digest = hasher.hexdigest()
    _write_durably(sidecar, iter([f"{digest}  {out.name}\n".encode()]))
    _fsync_dir(out.parent)
    if created_dir:
        _fsync_dir(out.parent.parent)
    return FindingExport(out=out, sidecar=sidecar, sha256=digest, finding_ids=tuple(finding_ids))


def _chunks(ids: Sequence[str]) -> Iterator[Sequence[str]]:
    for start in range(0, len(ids), _ID_CHUNK):
        yield ids[start : start + _ID_CHUNK]


def _placeholders(ids: Sequence[str]) -> str:
    return ", ".join("?" for _ in ids)


def delete_exported_findings(db: FiligreeDB, export: FindingExport) -> tuple[int, int]:
    """Drop the exported findings and the file records they leave unreferenced.

    Runs in one ``BEGIN IMMEDIATE``. Each exported id is re-checked against the
    selection predicate under the writer lock, so a row whose classification
    changed since the export is kept. A file record is dropped only when no
    ``scan_findings`` and no ``file_associations`` row references it any
    more; its file-domain residue is cleaned exactly as ``delete_file_record``
    does (``file_events`` and file/finding ``annotation_links`` deleted,
    observation / annotation back-references set to NULL — the annotation
    keeps its ``file_path``). Returns ``(deleted_findings, deleted_file_records)``.
    """
    conn = db.conn
    _begin_immediate(conn, "finding export --delete")
    try:
        deleted_findings = 0
        candidate_file_ids: set[str] = set()
        for chunk in _chunks(export.finding_ids):
            rows = conn.execute(
                f"SELECT sf.id, sf.file_id {_SELECTION} AND sf.id IN ({_placeholders(chunk)})",
                (WARDLINE_ENGINE_PSEUDO_PATH, *chunk),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if not ids:
                continue
            candidate_file_ids.update(row["file_id"] for row in rows)
            conn.execute(
                f"DELETE FROM annotation_links WHERE target_type = 'finding' AND target_id IN ({_placeholders(ids)})",
                ids,
            )
            deleted_findings += conn.execute(f"DELETE FROM scan_findings WHERE id IN ({_placeholders(ids)})", ids).rowcount

        orphan_file_ids = [
            file_id
            for file_id in sorted(candidate_file_ids)
            if conn.execute(
                "SELECT NOT EXISTS (SELECT 1 FROM scan_findings WHERE file_id = ?) "
                "AND NOT EXISTS (SELECT 1 FROM file_associations WHERE file_id = ?)",
                (file_id, file_id),
            ).fetchone()[0]
        ]
        deleted_files = 0
        for chunk in _chunks(orphan_file_ids):
            ph = _placeholders(chunk)
            conn.execute(f"DELETE FROM annotation_links WHERE target_type = 'file' AND target_id IN ({ph})", chunk)
            conn.execute(f"UPDATE observations SET file_id = NULL WHERE file_id IN ({ph})", chunk)
            conn.execute(f"UPDATE observation_links SET file_id = NULL WHERE file_id IN ({ph})", chunk)
            conn.execute(f"UPDATE annotations SET file_id = NULL WHERE file_id IN ({ph})", chunk)
            conn.execute(f"DELETE FROM file_events WHERE file_id IN ({ph})", chunk)
            deleted_files += conn.execute(f"DELETE FROM file_records WHERE id IN ({ph})", chunk).rowcount
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return deleted_findings, deleted_files


def run_finding_export(db: FiligreeDB, out: Path, *, delete: bool = False, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """Count (``dry_run``), export, or export-then-delete; return the counts payload.

    ``{"selected", "exported", "deleted", "deleted_file_records", "out",
    "sha256", "dry_run"}``. ``dry_run`` writes nothing at all (not even the
    archive directory) and cannot be combined with ``delete``.
    """
    if dry_run and delete:
        raise ValueError("dry_run and delete are mutually exclusive")
    result: dict[str, Any] = {
        "selected": 0,
        "exported": 0,
        "deleted": 0,
        "deleted_file_records": 0,
        "out": str(out),
        "sha256": None,
        "dry_run": dry_run,
    }
    if dry_run:
        result["selected"] = count_telemetry_findings(db)
        return result
    export = export_telemetry_findings(db, out, force=force)
    result["selected"] = result["exported"] = len(export.finding_ids)
    result["sha256"] = export.sha256
    if delete:
        result["deleted"], result["deleted_file_records"] = delete_exported_findings(db, export)
    return result
