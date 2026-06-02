"""ScansMixin — scan run lifecycle tracking.

Owns the scan_runs table: CRUD, status transitions, cooldown checks, log tail.
"""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from pathlib import Path
from typing import Any, get_args

from filigree.db_base import DBMixinProtocol, _begin_immediate, _now_iso
from filigree.types.core import ScanRunStatus
from filigree.types.files import ScanRunDict, ScanRunStatusDict

logger = logging.getLogger(__name__)

SCAN_COOLDOWN_SECONDS = 30
VALID_SCAN_RUN_STATUSES: frozenset[str] = frozenset(get_args(ScanRunStatus))

# Valid transitions: from_status -> set of valid to_statuses
_VALID_TRANSITIONS: dict[ScanRunStatus, set[ScanRunStatus]] = {
    "pending": {"running", "failed"},
    "running": {"completed", "failed", "timeout"},
}

# Terminal states are exactly those that never appear as a transition source —
# derived from _VALID_TRANSITIONS so the two can't drift. Today: completed,
# failed, timeout. Consumers deciding "benign already-finished vs actionable
# transition failure" must use this, not a hand-written tuple.
TERMINAL_SCAN_RUN_STATUSES: frozenset[str] = VALID_SCAN_RUN_STATUSES - frozenset(_VALID_TRANSITIONS)


class ScansMixin(DBMixinProtocol):
    """Scan run lifecycle — create, update status, check cooldown, read logs."""

    def create_scan_run(
        self,
        *,
        scan_run_id: str,
        scanner_name: str,
        scan_source: str,
        file_paths: list[str],
        file_ids: list[str],
        pid: int | None = None,
        api_url: str = "",
        log_path: str = "",
    ) -> ScanRunDict:
        self._validate_scan_log_path(log_path)
        now = _now_iso()
        existing = self.conn.execute("SELECT id FROM scan_runs WHERE id = ?", (scan_run_id,)).fetchone()
        if existing:
            logger.warning("Duplicate scan_run_id %r rejected", scan_run_id)
            raise ValueError(f"Scan run {scan_run_id!r} already exists")
        self.conn.execute(
            "INSERT INTO scan_runs "
            "(id, scanner_name, scan_source, status, file_paths, file_ids, "
            "pid, api_url, log_path, started_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)",
            (
                scan_run_id,
                scanner_name,
                scan_source,
                json.dumps(file_paths),
                json.dumps(file_ids),
                pid,
                api_url,
                log_path,
                now,
                now,
            ),
        )
        self.conn.commit()
        return self.get_scan_run(scan_run_id)

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
    ) -> tuple[ScanRunDict | None, ScanRunDict | None]:
        """Atomically check cooldown and insert a pending scan_run row.

        Returns ``(created_run, None)`` on success, or ``(None, blocking_run)``
        if a cooldown-active run already exists for ``(scanner_name, file_path)``.

        Uses ``BEGIN IMMEDIATE`` so the cooldown read + insert is serialized
        across concurrent callers — without that, two concurrent
        ``trigger_scan`` invocations can both pass the cooldown check and
        both spawn a scanner for the same file.
        """
        self._validate_scan_log_path(log_path)
        _begin_immediate(self.conn, "reserve_scan_run")
        try:
            blocking = self.check_scan_cooldown(scanner_name, file_path)
            if blocking is not None:
                self.conn.rollback()
                return None, blocking
            existing = self.conn.execute("SELECT id FROM scan_runs WHERE id = ?", (scan_run_id,)).fetchone()
            if existing:
                self.conn.rollback()
                msg = f"Scan run {scan_run_id!r} already exists"
                raise ValueError(msg)
            now = _now_iso()
            self.conn.execute(
                "INSERT INTO scan_runs "
                "(id, scanner_name, scan_source, status, file_paths, file_ids, "
                "pid, api_url, log_path, started_at, updated_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?, NULL, ?, ?, ?, ?)",
                (
                    scan_run_id,
                    scanner_name,
                    scan_source,
                    json.dumps([file_path]),
                    json.dumps([file_id]),
                    api_url,
                    log_path,
                    now,
                    now,
                ),
            )
            self.conn.commit()
        except Exception:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise
        return self.get_scan_run(scan_run_id), None

    def set_scan_run_spawn_info(
        self,
        scan_run_id: str,
        *,
        pid: int,
        log_path: str,
    ) -> None:
        """Backfill ``pid`` and ``log_path`` onto a reserved (pending) run.

        Used by the trigger handlers after they spawn the scanner process —
        the row exists from :meth:`reserve_scan_run`, this fills in the
        process-specific fields.
        """
        self._validate_scan_log_path(log_path)
        now = _now_iso()
        self.conn.execute(
            "UPDATE scan_runs SET pid = ?, log_path = ?, updated_at = ? WHERE id = ?",
            (pid, log_path, now, scan_run_id),
        )
        self.conn.commit()

    def get_scan_run(self, scan_run_id: str) -> ScanRunDict:
        row = self.conn.execute("SELECT * FROM scan_runs WHERE id = ?", (scan_run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Scan run not found: {scan_run_id!r}")
        return self._build_scan_run_dict(row)

    def update_scan_run_status(
        self,
        scan_run_id: str,
        status: ScanRunStatus,
        *,
        exit_code: int | None = None,
        findings_count: int | None = None,
        error_message: str | None = None,
    ) -> ScanRunDict:
        if status not in VALID_SCAN_RUN_STATUSES:
            raise ValueError(f"Invalid scan run status {status!r}. Must be one of: {sorted(VALID_SCAN_RUN_STATUSES)}")
        current = self.get_scan_run(scan_run_id)
        current_status = current["status"]
        valid_next = _VALID_TRANSITIONS.get(current_status, set())
        if status not in valid_next:
            logger.warning(
                "Invalid scan_run transition %r -> %r for %s",
                current_status,
                status,
                scan_run_id,
            )
            raise ValueError(f"Invalid transition: {current_status!r} -> {status!r}. Valid: {sorted(valid_next)}")
        now = _now_iso()
        # Column names are hardcoded — no injection risk from dynamic SQL
        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, now]
        if status in ("completed", "failed", "timeout"):
            updates.append("completed_at = ?")
            params.append(now)
        if exit_code is not None:
            updates.append("exit_code = ?")
            params.append(exit_code)
        if findings_count is not None:
            updates.append("findings_count = ?")
            params.append(findings_count)
        if error_message is not None:
            updates.append("error_message = ?")
            params.append(error_message)
        # Compare-and-swap on the previously-observed status. Without this
        # guard, two writers that both see `running` (e.g. dead-PID auto-fail
        # in get_scan_status and result-ingestion completion in db_files) can
        # both pass python validation and the second commit would silently
        # overwrite the first writer's terminal state.
        params.extend([scan_run_id, current_status])
        cursor = self.conn.execute(
            f"UPDATE scan_runs SET {', '.join(updates)} WHERE id = ? AND status = ?",
            params,
        )
        if cursor.rowcount == 0:
            self.conn.rollback()
            actual = self.get_scan_run(scan_run_id)
            logger.warning(
                "Stale scan_run transition for %s: expected %r, found %r",
                scan_run_id,
                current_status,
                actual["status"],
            )
            raise ValueError(
                f"Stale transition for {scan_run_id!r}: expected status {current_status!r}, "
                f"found {actual['status']!r} (concurrent writer changed the row)"
            )
        self.conn.commit()
        return self.get_scan_run(scan_run_id)

    def check_scan_cooldown(self, scanner_name: str, file_path: str) -> ScanRunDict | None:
        """Check if a prior scan blocks triggering.

        Returns the blocking scan run dict, or ``None`` if trigger is allowed.
        Two distinct invariants apply here:

        - ``pending`` and ``running`` rows are a singleton lock on the active
          run for ``(scanner_name, file_path)``. They block regardless of age:
          ``updated_at`` is only refreshed on status transitions, so a
          long-running scan would otherwise fall outside the cutoff and a
          duplicate trigger could spawn a parallel process.
        - ``completed`` rows act as a debounce window — they block for
          ``SCAN_COOLDOWN_SECONDS`` after completion, then trigger is allowed.
        """
        row = self.conn.execute(
            "SELECT sr.* FROM scan_runs sr "
            "WHERE sr.scanner_name = ? "
            "AND json_valid(sr.file_paths) "
            "AND EXISTS ("
            "  SELECT 1 FROM json_each(sr.file_paths) je WHERE je.value = ?"
            ") "
            "AND ("
            "  sr.status IN ('pending', 'running') "
            "  OR ("
            "    sr.status = 'completed' "
            "    AND sr.updated_at >= strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', ?) "
            "  )"
            ") "
            "ORDER BY sr.updated_at DESC LIMIT 1",
            (scanner_name, file_path, f"-{SCAN_COOLDOWN_SECONDS} seconds"),
        ).fetchone()
        if row is None:
            return None
        return self._build_scan_run_dict(row)

    def get_scan_status(self, scan_run_id: str, *, log_lines: int = 50) -> ScanRunStatusDict:
        """Get scan run with live PID check and log tail.

        When a running scan's process is detected as dead, the status is
        auto-transitioned to ``'failed'`` so the DB does not stay stale.
        """
        run = self.get_scan_run(scan_run_id)
        process_alive = False
        if run["pid"] is not None and run["status"] == "running":
            try:
                os.kill(run["pid"], 0)
                process_alive = True
            except PermissionError:
                # EPERM means process exists but is owned by another user
                process_alive = True
            except ProcessLookupError:
                logger.info(
                    "Scan run %s: process %d appears dead, transitioning to failed",
                    scan_run_id,
                    run["pid"],
                )
                try:
                    run = self.update_scan_run_status(
                        scan_run_id,
                        "failed",
                        error_message=f"Process {run['pid']} died without updating status",
                    )
                except (KeyError, ValueError) as exc:
                    logger.warning(
                        "Could not auto-fail scan run %s: %s",
                        scan_run_id,
                        exc,
                    )
                    # Re-read: another codepath may have completed it concurrently.
                    run = self.get_scan_run(scan_run_id)
                    if run["status"] == "running":
                        run["data_warnings"].append(
                            f"Process {run['pid']} appears dead but status is still 'running' (auto-fail transition failed: {exc})"
                        )
        log_tail: list[str] = []
        if run["log_path"]:
            log_path, warning = self._resolve_scan_log_path(run["log_path"])
            if warning:
                run["data_warnings"].append(warning)
            elif log_path is not None and log_path.is_file():
                try:
                    log_tail = self._read_log_tail(log_path, log_lines)
                except OSError as exc:
                    logger.warning("Could not read log file %s: %s", run["log_path"], exc)
        result = ScanRunStatusDict(
            **run,
            process_alive=process_alive,
            log_tail=log_tail,
        )
        if len(run["file_paths"]) > 1:
            result["data_warnings"].append(
                f"Batch scan: only PID {run['pid']} is monitored; "
                f"status for the remaining {len(run['file_paths']) - 1} file(s) is not tracked individually"
            )
        return result

    def _scan_project_root(self) -> Path:
        """Return the project root used for resolving scan log paths."""
        # Prefer the explicit ``project_root`` set by from_filigree_dir/from_conf.
        # Fall back to ``db_path.parent.parent`` only for legacy direct-path
        # construction in the classic ``.filigree/filigree.db`` layout.
        return self.project_root if self.project_root is not None else self.db_path.parent.parent

    def _validate_scan_log_path(self, log_path: str) -> None:
        """Reject caller-supplied scan log paths that would escape the project."""
        if not log_path:
            return
        _, warning = self._resolve_scan_log_path(log_path)
        if warning:
            raise ValueError(f"log_path must be project-relative and stay inside the project root: {log_path!r}")

    def _resolve_scan_log_path(self, log_path: str) -> tuple[Path | None, str | None]:
        """Resolve a persisted scan log path without trusting stored data."""
        raw = Path(log_path)
        if raw.is_absolute():
            return None, f"Ignored invalid log_path outside project root: {log_path}"
        project_root = self._scan_project_root().resolve(strict=False)
        resolved = (project_root / raw).resolve(strict=False)
        try:
            resolved.relative_to(project_root)
        except ValueError:
            return None, f"Ignored invalid log_path outside project root: {log_path}"
        return resolved, None

    @staticmethod
    def _read_log_tail(log_path: Path, log_lines: int) -> list[str]:
        """Stream a bounded line tail without loading the full log into memory."""
        tail: deque[str] = deque(maxlen=log_lines)
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                tail.append(line.rstrip("\r\n"))
        return list(tail)

    @staticmethod
    def _safe_json_list(raw: str | None, field: str, run_id: str) -> tuple[list[str], str | None]:
        """Parse a JSON list column, returning ``([], warning)`` on corrupt data."""
        try:
            parsed = json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            logger.warning("Corrupt %s JSON in scan_run %s", field, run_id)
            return [], f"Corrupt {field} JSON — defaulted to empty list"
        if not isinstance(parsed, list):
            logger.warning("Corrupt %s JSON in scan_run %s: expected list, got %s", field, run_id, type(parsed).__name__)
            return [], f"Corrupt {field} JSON — expected list, got {type(parsed).__name__}"
        return parsed, None

    def _build_scan_run_dict(self, row: Any) -> ScanRunDict:
        warnings: list[str] = []
        file_paths, w1 = self._safe_json_list(row["file_paths"], "file_paths", row["id"])
        if w1:
            warnings.append(w1)
        file_ids, w2 = self._safe_json_list(row["file_ids"], "file_ids", row["id"])
        if w2:
            warnings.append(w2)
        return ScanRunDict(
            id=row["id"],
            scanner_name=row["scanner_name"],
            scan_source=row["scan_source"],
            status=row["status"],
            file_paths=file_paths,
            file_ids=file_ids,
            pid=row["pid"],
            api_url=row["api_url"] or "",
            log_path=row["log_path"] or "",
            started_at=row["started_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            exit_code=row["exit_code"],
            findings_count=row["findings_count"] or 0,
            error_message=row["error_message"] or "",
            data_warnings=warnings,
        )
