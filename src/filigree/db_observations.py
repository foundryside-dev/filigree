"""Mixin for observation (agent scratchpad) operations.

Observations are lightweight, disposable candidates — not issues.
They live in their own table and are promoted to issues or dismissed.

Includes:
- 14-day TTL with piggyback sweep on reads (in savepoint)
- Dismissal audit trail via dismissed_observations table
- Promotion to issue (creates the issue in one transaction, then cleans
  up the observation in a second transaction; if cleanup fails, the
  orphaned observation is swept on TTL expiry)
- Age stats for session context prompting
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from filigree.db_base import DBMixinProtocol, _begin_immediate, _escape_like, _now_iso
from filigree.db_files import _is_project_relative_scan_path, _normalize_scan_path
from filigree.finding_issue_cascade import record_reconciliation_debt_comment
from filigree.types.api import BatchFailure, ErrorCode
from filigree.types.core import (
    BatchDismissResult,
    ISOTimestamp,
    ObservationDict,
    ObservationLinkDict,
    ObservationStatsDict,
    PromoteObservationResult,
)

logger = logging.getLogger(__name__)

DEFAULT_TTL_DAYS = 14
STALE_THRESHOLD_HOURS = 48
MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807

_LIST_OBSERVATIONS_SORT_COLUMNS = frozenset({"priority", "created_at", "expires_at"})
_LIST_OBSERVATIONS_DIRECTIONS = frozenset({"asc", "desc"})
VALID_OBSERVATION_LINK_DISPOSITIONS = frozenset({"evidence", "duplicate", "superseded", "related"})


def _require_string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string, got {type(value).__name__}")
    return value


def _require_optional_string(value: object, field_name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string or null, got {type(value).__name__}")
    return value


def _require_string_list(value: object, field_name: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{field_name} must be a list of strings")
    return value


def _alive_clause(sweep: bool, now_iso: str) -> tuple[str, str, tuple[str, ...]]:
    """Return (AND-fragment, WHERE-fragment, params) to filter expired observations.

    When sweep=True (expired rows already deleted), returns empty filters.
    When sweep=False, provides both ``AND expires_at > ?`` (for appending to
    existing WHERE) and ``WHERE expires_at > ?`` (for standalone queries).
    """
    if sweep:
        return "", "", ()
    return " AND expires_at > ?", " WHERE expires_at > ?", (now_iso,)


DISMISSED_AUDIT_TRAIL_CAP = 10_000
OBSERVATION_SWEEP_FAILURE_ALERT_THRESHOLD = 3


def _record_promotion_reconciliation_debt(conn: Any, issue_id: str, warnings: list[str]) -> None:
    for warning in warnings:
        if not warning.startswith("Failed"):
            continue
        record_reconciliation_debt_comment(conn, issue_id, f"Observation promotion follow-up failed: {warning}")


def _expires_iso(ttl_days: int = DEFAULT_TTL_DAYS) -> str:
    """Compute expiry timestamp using same isoformat() as _now_iso for consistent text comparison."""
    return (datetime.now(UTC) + timedelta(days=ttl_days)).isoformat()


def _public_observation_link(row: dict[str, Any]) -> ObservationLinkDict:
    return cast(
        ObservationLinkDict,
        {
            "id": row["id"],
            "obs_id": row["obs_id"],
            "observation_id": row["obs_id"],
            "issue_id": row["issue_id"],
            "disposition": row["disposition"],
            "summary": row["summary"],
            "detail": row["detail"],
            "file_id": row["file_id"],
            "file_path": row["file_path"],
            "line": row["line"],
            "source_issue_id": row["source_issue_id"],
            "source_finding_id": row["source_finding_id"],
            "priority": row["priority"],
            "observation_actor": row["observation_actor"],
            "actor": row["actor"],
            "reason": row["reason"],
            "linked_at": row["linked_at"],
        },
    )


def _observation_location(obs: dict[str, Any]) -> str:
    if not obs.get("file_path"):
        return ""
    loc = str(obs["file_path"])
    if obs.get("line") is not None:
        loc += f":{obs['line']}"
    return loc


def _observation_evidence_block(obs: dict[str, Any]) -> str:
    lines = [f"- {obs['summary']}"]
    if obs.get("detail"):
        lines.append(f"  Detail: {obs['detail']}")
    location = _observation_location(obs)
    if location:
        lines.append(f"  Location: `{location}`")
    if obs.get("source_issue_id"):
        lines.append(f"  Observed while working on: {obs['source_issue_id']}")
    if obs.get("source_finding_id"):
        lines.append(f"  Source finding: {obs['source_finding_id']}")
    lines.append(f"  Observation ID: {obs['id']}")
    return "\n".join(lines)


class ObservationsMixin(DBMixinProtocol):
    """Observation CRUD — agent scratchpad for things noticed in passing.

    Declares ``DBMixinProtocol`` as a base for type-safe access to shared
    attributes. The Protocol provides method stubs for static analysis;
    actual implementations are provided by ``FiligreeDB`` at composition
    time via MRO.
    """

    def _record_observation_sweep_success(self, timestamp: str) -> None:
        self._observation_sweep_consecutive_failures = 0
        self._observation_sweep_last_success_at = timestamp

    def _record_observation_sweep_failure(self) -> int:
        failures = int(getattr(self, "_observation_sweep_consecutive_failures", 0)) + 1
        self._observation_sweep_consecutive_failures = failures
        return failures

    def _observation_sweep_health(self) -> dict[str, Any]:
        return {
            "sweep_consecutive_failures": int(getattr(self, "_observation_sweep_consecutive_failures", 0)),
            "last_successful_sweep_at": getattr(self, "_observation_sweep_last_success_at", None),
        }

    def _sweep_expired_observations(self) -> tuple[int, bool]:
        """Delete expired observations in a savepoint (piggyback cleanup).

        All expired observations are logged to dismissed_observations and deleted.
        Also prunes the dismissed_observations audit trail to DISMISSED_AUDIT_TRAIL_CAP entries.
        Uses a savepoint so it doesn't commit or interfere with in-flight transactions.

        Returns:
            (deleted_row_count, succeeded). ``succeeded=False`` indicates that the
            sweep was rolled back after a transient error, so expired rows may
            still be present — callers must apply ``WHERE expires_at > ?`` to
            avoid returning them as live results.
        """
        now = _now_iso()
        self.conn.execute("SAVEPOINT sweep_obs")
        try:
            self.conn.execute(
                "INSERT INTO dismissed_observations (obs_id, summary, actor, reason, dismissed_at) "
                "SELECT id, summary, 'system', 'expired (TTL)', ? FROM observations WHERE expires_at <= ?",
                (now, now),
            )
            cursor = self.conn.execute("DELETE FROM observations WHERE expires_at <= ?", (now,))
            # Prune dismissed_observations audit trail to prevent unbounded growth.
            # Keep the most recent DISMISSED_AUDIT_TRAIL_CAP entries. Only enforced
            # during sweep (not dismiss/promote) — acceptable for v1 scope. Without
            # an index on dismissed_at, this is O(N log N) for large tables.
            self.conn.execute(
                "DELETE FROM dismissed_observations WHERE id NOT IN "
                "(SELECT id FROM dismissed_observations ORDER BY dismissed_at DESC LIMIT ?)",
                (DISMISSED_AUDIT_TRAIL_CAP,),
            )
            self.conn.execute("RELEASE SAVEPOINT sweep_obs")
            self._record_observation_sweep_success(now)
            if cursor.rowcount > 0:
                logger.debug("Swept %d expired observations", cursor.rowcount)
            return cursor.rowcount, True
        except sqlite3.IntegrityError:
            logger.error("Observation sweep hit an integrity violation, rolled back", exc_info=True)
            try:
                self.conn.execute("ROLLBACK TO SAVEPOINT sweep_obs")
            finally:
                try:
                    self.conn.execute("RELEASE SAVEPOINT sweep_obs")
                except sqlite3.Error:
                    logger.warning("Failed to release savepoint after sweep rollback", exc_info=True)
            raise
        except sqlite3.OperationalError:
            # Suppress transient errors (locked, busy) — sweep is best-effort.
            # Let ProgrammingError and InterfaceError propagate; those indicate code bugs.
            failures = self._record_observation_sweep_failure()
            logger.warning("Observation sweep failed, rolled back", extra={"consecutive_failures": failures}, exc_info=True)
            if failures >= OBSERVATION_SWEEP_FAILURE_ALERT_THRESHOLD:
                logger.error("Observation sweep has failed %d consecutive times", failures)
            try:
                self.conn.execute("ROLLBACK TO SAVEPOINT sweep_obs")
            finally:
                try:
                    self.conn.execute("RELEASE SAVEPOINT sweep_obs")
                except sqlite3.Error:
                    logger.warning("Failed to release savepoint after sweep rollback", exc_info=True)
            return 0, False  # Sweep is best-effort — don't block reads, but signal failure

    def create_observation(
        self,
        summary: str,
        *,
        detail: str = "",
        file_id: str = "",
        file_path: str = "",
        line: int | None = None,
        source_issue_id: str = "",
        source_finding_id: str = "",
        priority: int = 3,
        actor: str = "",
        auto_commit: bool = True,
    ) -> ObservationDict:
        """Create an observation (agent scratchpad note).

        If an observation with the same (summary, file_path, line) already
        exists and is still alive, returns the existing observation unchanged.
        If the duplicate has expired, replaces it by deleting the old and
        inserting the new in one transaction (so no gap exists between delete
        and insert where a concurrent caller could claim the dedup slot).

        When *auto_commit* is ``False``, the caller is responsible for
        committing (or rolling back) the connection.  Use this when
        ``create_observation`` is called inside an outer transaction to
        avoid committing partial work.
        """
        summary = _require_string(summary, "summary")
        file_id = _require_string(file_id, "file_id")
        file_path = _require_string(file_path, "file_path")
        for field_name, value in (
            ("detail", detail),
            ("source_issue_id", source_issue_id),
            ("source_finding_id", source_finding_id),
            ("actor", actor),
        ):
            _require_string(value, field_name)
        if not summary or not summary.strip():
            raise ValueError("Observation summary cannot be empty")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError(f"priority must be an integer, got {type(priority).__name__}")
        if not (0 <= priority <= 4):
            raise ValueError(f"priority must be between 0 and 4, got {priority}")
        if line is not None:
            if isinstance(line, bool) or not isinstance(line, int):
                raise ValueError(f"line must be an integer or null, got {type(line).__name__}")
            if line < 0:
                raise ValueError(f"line must be >= 0, got {line}")
            if line > MAX_SQLITE_INTEGER:
                raise ValueError(f"line must be <= {MAX_SQLITE_INTEGER}, got {line}")

        linked_file_id: str | None = file_id or None
        # Track whether THIS call created a new file_record so we can compensate
        # by deleting it if the later observation INSERT fails — otherwise the
        # file_record is orphaned (register_file commits independently).
        created_file_id: str | None = None
        if file_path:
            file_path = _normalize_scan_path(file_path)
            if not _is_project_relative_scan_path(file_path):
                raise ValueError("file_path must be project-relative")
        elif linked_file_id:
            row = self.conn.execute("SELECT path FROM file_records WHERE id = ?", (linked_file_id,)).fetchone()
            if row is not None:
                file_path = row["path"] or ""

        now = _now_iso()
        summary_stripped = summary.strip()
        line_cmp = line if line is not None else -1

        # Check for existing duplicate (dedup key: summary + file_path + line).
        # Live duplicates are a no-op and must not require registry/file refresh.
        # If the duplicate is expired, delete it so re-creation succeeds —
        # otherwise the SELECT match would cause us to skip the new INSERT.
        existing = self.conn.execute(
            "SELECT * FROM observations WHERE summary = ? AND file_path = ? AND coalesce(line, -1) = ?",
            (summary_stripped, file_path, line_cmp),
        ).fetchone()
        if existing and existing["expires_at"] > now:
            # Live duplicate — return existing row
            return cast(ObservationDict, dict(existing))

        if file_path and linked_file_id is None:
            if auto_commit:
                # Standalone call — register_file commits, which is fine.
                existing_fr = self.conn.execute("SELECT id FROM file_records WHERE path = ?", (file_path,)).fetchone()
                fr = self.register_file(file_path)
                linked_file_id = fr.id
                if existing_fr is None:
                    created_file_id = linked_file_id
            else:
                # Inside an outer transaction — register_file would commit
                # prematurely.  Look up the file_id without side effects;
                # the caller is responsible for ensuring the file exists.
                row = self.conn.execute("SELECT id FROM file_records WHERE path = ?", (file_path,)).fetchone()
                linked_file_id = row["id"] if row else None

        savepoint_name = "create_observation_mutation"
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

        if not auto_commit:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN")
            self.conn.execute(f"SAVEPOINT {savepoint_name}")
            savepoint_active = True

        obs_id = self._generate_unique_id("observations", "obs")
        expires = _expires_iso()
        try:
            if existing:
                # Expired duplicate — delete and fall through to insert.
                # Both operations share a single transaction to avoid a TOCTOU
                # window where a concurrent caller could insert the same dedup key
                # between the delete-commit and the insert-commit.
                self.conn.execute(
                    "INSERT INTO dismissed_observations (obs_id, summary, actor, reason, dismissed_at) "
                    "VALUES (?, ?, 'system', 'expired (replaced)', ?)",
                    (existing["id"], existing["summary"], now),
                )
                self.conn.execute("DELETE FROM observations WHERE id = ?", (existing["id"],))
                # No commit here — fall through to INSERT below so both
                # the deletion and insertion are committed atomically.

            self.conn.execute(
                "INSERT INTO observations (id, summary, detail, file_id, file_path, line, "
                "source_issue_id, source_finding_id, priority, actor, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    obs_id,
                    summary_stripped,
                    detail,
                    linked_file_id,
                    file_path,
                    line,
                    source_issue_id,
                    source_finding_id,
                    priority,
                    actor,
                    now,
                    expires,
                ),
            )
            if auto_commit:
                self.conn.commit()
            else:
                _release_savepoint()
        except sqlite3.IntegrityError as e:
            # Concurrent racer won the dedup slot between our SELECT and INSERT.
            # The pre-insert SELECT cannot serialize writers under DEFERRED isolation,
            # and we cannot wrap the whole function in BEGIN IMMEDIATE because callers
            # invoke us with auto_commit=False inside their own transactions. So
            # absorb the dedup IntegrityError, roll back our partial work, re-SELECT
            # the live duplicate, and return it — preserving the documented contract
            # that concurrent calls with the same dedup key all return the same row.
            if "idx_observations_dedup" not in str(e):
                # Some other integrity failure (e.g. file_records race) — surface it.
                if auto_commit:
                    self.conn.rollback()
                else:
                    _rollback_savepoint()
                raise
            if auto_commit:
                self.conn.rollback()
            else:
                _rollback_savepoint()
            winner = self.conn.execute(
                "SELECT * FROM observations WHERE summary = ? AND file_path = ? AND coalesce(line, -1) = ?",
                (summary_stripped, file_path, line_cmp),
            ).fetchone()
            if winner is not None:
                return cast(ObservationDict, dict(winner))
            # Race resolved by deletion (sweep / dismiss between IntegrityError and re-SELECT).
            # No live duplicate to return — re-raise so caller retries.
            raise
        except sqlite3.Error:
            if auto_commit:
                self.conn.rollback()
                # Compensate: delete the file_record we just created.  register_file
                # committed independently, so rolling back this transaction does not
                # remove it — and we'd leave an orphan row otherwise.
                if created_file_id is not None:
                    try:
                        self.conn.execute("DELETE FROM file_records WHERE id = ?", (created_file_id,))
                        self.conn.commit()
                    except sqlite3.Error:
                        self.conn.rollback()
                        logger.warning(
                            "Failed to compensate orphaned file_record %s after observation insert failure",
                            created_file_id,
                            exc_info=True,
                        )
            else:
                _rollback_savepoint()
            raise
        return {
            "id": obs_id,
            "summary": summary_stripped,
            "detail": detail,
            "file_id": linked_file_id,
            "file_path": file_path,
            "line": line,
            "source_issue_id": source_issue_id,
            "source_finding_id": source_finding_id,
            "priority": priority,
            "actor": actor,
            "created_at": now,
            "expires_at": ISOTimestamp(expires),
            # v23->v24 (ADR-012): create path does not stamp transport identity
            # yet (later tasks own that); surface None so the create-return shape
            # matches the SELECT * read paths.
            "verified_actor": None,
        }

    def list_observations(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        file_path: str = "",
        file_id: str = "",
        actor: str = "",
        source_issue_id: str = "",
        priority_min: int | None = None,
        priority_max: int | None = None,
        older_than_hours: int | None = None,
        sort_by: str = "priority",
        direction: str = "asc",
        sweep: bool = True,
    ) -> list[ObservationDict]:
        """List pending observations with optional filtering.

        Sweeps expired observations first when ``sweep=True`` (best-effort, in
        savepoint).  If the sweep is disabled or fails (transient DB error,
        rolled back), the read falls back to ``WHERE expires_at > ?`` so
        expired rows are still excluded from results without mutating storage.

        Filters:
          - ``file_path``: substring match (LIKE)
          - ``file_id``: exact FK match (more precise than path LIKE)
          - ``actor``: exact actor string match (e.g. ``"mcp-review-h"``)
          - ``source_issue_id``: exact issue this observation was noticed during
          - ``priority_min`` / ``priority_max``: inclusive bounds (0..4)
          - ``older_than_hours``: only observations created more than N hours ago

        Sort:
          - ``sort_by``: one of ``"priority"`` (default), ``"created_at"``, ``"expires_at"``
          - ``direction``: ``"asc"`` (default) or ``"desc"``

        Sort fields and direction are whitelisted before interpolation so the
        SQL can't be poisoned by caller input.
        """
        sort_by = _require_string(sort_by, "sort_by")
        direction = _require_string(direction, "direction")
        if sort_by not in _LIST_OBSERVATIONS_SORT_COLUMNS:
            msg = f"sort_by must be one of {sorted(_LIST_OBSERVATIONS_SORT_COLUMNS)}, got {sort_by!r}"
            raise ValueError(msg)
        if direction not in _LIST_OBSERVATIONS_DIRECTIONS:
            msg = f"direction must be one of {sorted(_LIST_OBSERVATIONS_DIRECTIONS)}, got {direction!r}"
            raise ValueError(msg)
        if isinstance(limit, bool) or not isinstance(limit, int):
            msg = f"limit must be an integer, got {type(limit).__name__}"
            raise ValueError(msg)
        if not (1 <= limit <= MAX_SQLITE_INTEGER):
            msg = f"limit must be between 1 and {MAX_SQLITE_INTEGER}, got {limit}"
            raise ValueError(msg)
        if isinstance(offset, bool) or not isinstance(offset, int):
            msg = f"offset must be an integer, got {type(offset).__name__}"
            raise ValueError(msg)
        if not (0 <= offset <= MAX_SQLITE_INTEGER):
            msg = f"offset must be between 0 and {MAX_SQLITE_INTEGER}, got {offset}"
            raise ValueError(msg)
        for field_name, value in (
            ("file_path", file_path),
            ("file_id", file_id),
            ("actor", actor),
            ("source_issue_id", source_issue_id),
        ):
            _require_string(value, field_name)
        for int_field_name, int_value in (
            ("priority_min", priority_min),
            ("priority_max", priority_max),
            ("older_than_hours", older_than_hours),
        ):
            if int_value is not None and (isinstance(int_value, bool) or not isinstance(int_value, int)):
                msg = f"{int_field_name} must be an integer, got {type(int_value).__name__}"
                raise ValueError(msg)
        if priority_min is not None and not (0 <= priority_min <= 4):
            msg = f"priority_min must be between 0 and 4, got {priority_min}"
            raise ValueError(msg)
        if priority_max is not None and not (0 <= priority_max <= 4):
            msg = f"priority_max must be between 0 and 4, got {priority_max}"
            raise ValueError(msg)
        if priority_min is not None and priority_max is not None and priority_min > priority_max:
            msg = f"priority_min must be <= priority_max, got {priority_min} > {priority_max}"
            raise ValueError(msg)
        if older_than_hours is not None and older_than_hours < 0:
            msg = f"older_than_hours must be >= 0, got {older_than_hours}"
            raise ValueError(msg)

        swept_ok = False
        if sweep:
            _, swept_ok = self._sweep_expired_observations()
        now_iso = _now_iso()

        clauses: list[str] = []
        params: list[Any] = []
        if file_id:
            clauses.append("file_id = ?")
            params.append(file_id)
        elif file_path:
            normalized = _normalize_scan_path(file_path) or file_path
            clauses.append("file_path LIKE ? ESCAPE '\\'")
            params.append(_escape_like(normalized))
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        if source_issue_id:
            clauses.append("source_issue_id = ?")
            params.append(source_issue_id)
        if priority_min is not None:
            clauses.append("priority >= ?")
            params.append(priority_min)
        if priority_max is not None:
            clauses.append("priority <= ?")
            params.append(priority_max)
        if older_than_hours is not None:
            try:
                cutoff = (datetime.now(UTC) - timedelta(hours=older_than_hours)).isoformat()
            except OverflowError as exc:
                msg = f"older_than_hours is too large, got {older_than_hours}"
                raise ValueError(msg) from exc
            clauses.append("created_at <= ?")
            params.append(cutoff)
        if not swept_ok:
            clauses.append("expires_at > ?")
            params.append(now_iso)

        where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        # sort_by / direction whitelisted above — safe to interpolate.
        order_sql = f" ORDER BY {sort_by} {direction.upper()}, created_at ASC"
        rows = self.conn.execute(
            f"SELECT * FROM observations{where_sql}{order_sql} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [cast(ObservationDict, dict(row)) for row in rows]

    def get_observations_by_ids(self, obs_ids: list[str]) -> list[ObservationDict]:
        """Return observation records for a list of IDs, in input order.

        Used by ``batch_dismiss_observations`` callers that want full
        records returned before dismissal (response_detail='full').
        Missing IDs are silently skipped — pair with the not_found list
        from ``batch_dismiss_observations`` to identify them. Does not
        sweep expired observations.
        """
        if not obs_ids:
            return []
        unique_ids = list(dict.fromkeys(obs_ids))
        placeholders = ",".join("?" for _ in unique_ids)
        rows = self.conn.execute(
            f"SELECT * FROM observations WHERE id IN ({placeholders})",
            unique_ids,
        ).fetchall()
        by_id = {row["id"]: cast(ObservationDict, dict(row)) for row in rows}
        return [by_id[oid] for oid in unique_ids if oid in by_id]

    def observation_count(self) -> int:
        """Return total observation count WITHOUT sweeping expired rows.

        This is intentionally a raw count. It may include expired observations
        that have not yet been cleaned up. Use ``list_observations()`` for a
        sweep-then-read pattern, or call ``_sweep_expired_observations()``
        explicitly if an accurate count is needed.
        """
        row = self.conn.execute("SELECT COUNT(*) FROM observations").fetchone()
        return int(row[0])

    def observation_stats(self, *, sweep: bool = True) -> ObservationStatsDict:
        """Return observation count + age stats for session context prompting.

        Args:
            sweep: If True (default), sweep expired observations first.
                   Pass False when calling from read-only context paths
                   (summary generation, MCP prompt) to avoid write side effects.
                   When False — or when the sweep itself fails — expired rows
                   are excluded via WHERE filter to keep counts consistent with
                   what list_observations returns.
        """
        swept_ok = True
        if sweep:
            _, swept_ok = self._sweep_expired_observations()

        now = datetime.now(UTC)
        now_iso = now.isoformat()
        # Apply expires-filter whenever the sweep did not succeed OR when caller
        # opted out of sweeping — in both cases expired rows may still exist.
        alive_frag, alive_where, alive_params = _alive_clause(sweep and swept_ok, now_iso)
        count = self.conn.execute(f"SELECT COUNT(*) FROM observations{alive_where}", alive_params).fetchone()[0]
        sweep_health = self._observation_sweep_health()
        if count == 0:
            return {
                "count": 0,
                "stale_count": 0,
                "oldest_hours": 0,
                "expiring_soon_count": 0,
                "sweep_consecutive_failures": cast(int, sweep_health["sweep_consecutive_failures"]),
                "last_successful_sweep_at": cast(ISOTimestamp | None, sweep_health["last_successful_sweep_at"]),
            }

        stale_cutoff = (now - timedelta(hours=STALE_THRESHOLD_HOURS)).isoformat()
        expiring_cutoff = (now + timedelta(hours=24)).isoformat()

        stale = self.conn.execute(
            f"SELECT COUNT(*) FROM observations WHERE created_at <= ?{alive_frag}",
            (stale_cutoff, *alive_params),
        ).fetchone()[0]

        expiring = self.conn.execute(
            f"SELECT COUNT(*) FROM observations WHERE expires_at <= ?{alive_frag}",
            (expiring_cutoff, *alive_params),
        ).fetchone()[0]

        oldest_row = self.conn.execute(f"SELECT MIN(created_at) FROM observations{alive_where}", alive_params).fetchone()
        oldest_hours: float | None = 0.0
        if oldest_row and oldest_row[0]:
            try:
                oldest_dt = datetime.fromisoformat(oldest_row[0])
                oldest_hours = (now - oldest_dt).total_seconds() / 3600
            except (ValueError, TypeError):
                logger.warning("Corrupt created_at in observations: %r", oldest_row[0])
                oldest_hours = None  # Unknown age, not zero

        return {
            "count": count,
            "stale_count": stale,
            "oldest_hours": round(oldest_hours, 1) if oldest_hours is not None else None,
            "expiring_soon_count": expiring,
            "sweep_consecutive_failures": cast(int, sweep_health["sweep_consecutive_failures"]),
            "last_successful_sweep_at": cast(ISOTimestamp | None, sweep_health["last_successful_sweep_at"]),
        }

    def dismiss_observation(
        self,
        obs_id: str,
        *,
        actor: str = "",
        reason: str = "",
    ) -> None:
        _require_string(actor, "actor")
        _require_string(reason, "reason")
        # Serialize the SELECT/INSERT/DELETE so two concurrent dismissals don't
        # both write audit rows for the same row. Without BEGIN IMMEDIATE the
        # losing racer's stale pre-read still produces an audit insert and a
        # no-op delete that silently reports success — masking the fact that
        # the row was already gone. Contract change: concurrent second dismiss
        # now correctly raises ``ValueError("Observation not found")``.
        _begin_immediate(self.conn, "dismiss_observation")
        try:
            row = self.conn.execute("SELECT id, summary FROM observations WHERE id = ?", (obs_id,)).fetchone()
            if row is None:
                self.conn.rollback()
                raise ValueError(f"Observation not found: {obs_id}")
            now = _now_iso()
            self.conn.execute(
                "INSERT INTO dismissed_observations (obs_id, summary, actor, reason, dismissed_at) VALUES (?, ?, ?, ?, ?)",
                (obs_id, row["summary"], actor, reason, now),
            )
            self.conn.execute("DELETE FROM observations WHERE id = ?", (obs_id,))
            self.conn.commit()
        except Exception:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise

    def batch_dismiss_observations(
        self,
        obs_ids: list[str],
        *,
        actor: str = "",
        reason: str = "",
    ) -> BatchDismissResult:
        if not isinstance(obs_ids, list) or not all(isinstance(obs_id, str) for obs_id in obs_ids):
            msg = "obs_ids must be a list of strings"
            raise TypeError(msg)
        _require_string(actor, "actor")
        _require_string(reason, "reason")
        if not obs_ids:
            return {"dismissed": 0, "not_found": []}
        # Deduplicate in Python to avoid relying on SQL IN dedup behavior
        unique_ids = list(dict.fromkeys(obs_ids))
        now = _now_iso()
        placeholders = ",".join("?" for _ in unique_ids)
        # Same TOCTOU as single dismiss — under concurrent batch calls the
        # found_ids/not_found computation must match the rows we actually
        # delete, otherwise the audit table inflates and ``not_found`` lies.
        # Hold a writer lock across the SELECT and the INSERT/DELETE.
        _begin_immediate(self.conn, "batch_dismiss_observations")
        try:
            # Find which IDs actually exist (under writer lock)
            found_rows = self.conn.execute(
                f"SELECT id FROM observations WHERE id IN ({placeholders})",
                unique_ids,
            ).fetchall()
            found_ids = {row["id"] for row in found_rows}
            not_found = [oid for oid in unique_ids if oid not in found_ids]

            self.conn.execute(
                f"INSERT INTO dismissed_observations (obs_id, summary, actor, reason, dismissed_at) "
                f"SELECT id, summary, ?, ?, ? FROM observations WHERE id IN ({placeholders})",
                [actor, reason, now, *unique_ids],
            )
            cursor = self.conn.execute(
                f"DELETE FROM observations WHERE id IN ({placeholders})",
                unique_ids,
            )
            self.conn.commit()
        except Exception:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise
        return {"dismissed": cursor.rowcount, "not_found": not_found}

    def link_observation_to_issue(
        self,
        obs_id: str,
        issue_id: str,
        *,
        disposition: str = "evidence",
        reason: str = "",
        actor: str = "",
    ) -> ObservationLinkDict:
        """Link a live observation to an existing issue and remove it from the queue.

        The observation row is deleted, but its full evidence snapshot is kept
        in ``observation_links`` and the dismissal audit trail records the
        structured disposition.
        """
        obs_id = _require_string(obs_id, "obs_id")
        issue_id = _require_string(issue_id, "issue_id")
        disposition = _require_string(disposition, "disposition")
        _require_string(actor, "actor")
        _require_string(reason, "reason")
        if disposition not in VALID_OBSERVATION_LINK_DISPOSITIONS:
            msg = f"disposition must be one of {sorted(VALID_OBSERVATION_LINK_DISPOSITIONS)}, got {disposition!r}"
            raise ValueError(msg)
        # Validate the target issue before mutating the observation queue.
        self.get_issue(issue_id)

        _begin_immediate(self.conn, "link_observation_to_issue")
        try:
            row = self.conn.execute("SELECT * FROM observations WHERE id = ?", (obs_id,)).fetchone()
            if row is None:
                self.conn.rollback()
                raise ValueError(f"Observation not found: {obs_id}")
            obs = dict(row)
            if obs["expires_at"] <= _now_iso():
                self.conn.rollback()
                raise ValueError(f"Observation {obs_id} has expired and cannot be linked")

            now = _now_iso()
            cursor = self.conn.execute(
                "INSERT INTO observation_links "
                "(obs_id, issue_id, disposition, summary, detail, file_id, file_path, line, "
                "source_issue_id, source_finding_id, priority, observation_actor, actor, reason, linked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    obs_id,
                    issue_id,
                    disposition,
                    obs["summary"],
                    obs["detail"],
                    obs["file_id"],
                    obs["file_path"],
                    obs["line"],
                    obs["source_issue_id"],
                    obs["source_finding_id"],
                    obs["priority"],
                    obs["actor"],
                    actor,
                    reason,
                    now,
                ),
            )
            audit_reason = f"linked:{disposition}:{issue_id}"
            if reason:
                audit_reason += f": {reason}"
            self.conn.execute(
                "INSERT INTO dismissed_observations (obs_id, summary, actor, reason, dismissed_at) VALUES (?, ?, ?, ?, ?)",
                (obs_id, obs["summary"], actor or obs["actor"], audit_reason, now),
            )
            comment = f"Linked observation {obs_id} as {disposition}: {obs['summary']}"
            if reason:
                comment += f"\nReason: {reason}"
            location = _observation_location(obs)
            if location:
                comment += f"\nLocation: `{location}`"
            if obs.get("detail"):
                comment += f"\n\n{obs['detail']}"
            self.conn.execute(
                "INSERT INTO comments (issue_id, author, verified_author, text, created_at) VALUES (?, ?, ?, ?, ?)",
                (issue_id, actor, self._verified_actor, comment, now),
            )
            self.conn.execute("DELETE FROM observations WHERE id = ?", (obs_id,))
            self.conn.commit()
        except Exception:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise

        link_id = cursor.lastrowid
        if link_id is None:  # pragma: no cover - INSERT always sets lastrowid
            msg = "INSERT did not produce a lastrowid"
            raise RuntimeError(msg)
        stored = self.conn.execute("SELECT * FROM observation_links WHERE id = ?", (link_id,)).fetchone()
        return _public_observation_link(dict(stored))

    def batch_link_observations_to_issue(
        self,
        obs_ids: list[str],
        issue_id: str,
        *,
        disposition: str = "evidence",
        reason: str = "",
        actor: str = "",
    ) -> tuple[list[ObservationLinkDict], list[BatchFailure]]:
        """Link multiple observations to an existing issue with per-item errors."""
        if not isinstance(obs_ids, list) or not all(isinstance(obs_id, str) for obs_id in obs_ids):
            msg = "obs_ids must be a list of strings"
            raise TypeError(msg)
        self.get_issue(issue_id)

        linked: list[ObservationLinkDict] = []
        errors: list[BatchFailure] = []
        for obs_id in dict.fromkeys(obs_ids):
            try:
                linked.append(
                    self.link_observation_to_issue(
                        obs_id,
                        issue_id,
                        disposition=disposition,
                        reason=reason,
                        actor=actor,
                    )
                )
            except ValueError as e:
                msg = str(e)
                err_code = ErrorCode.NOT_FOUND if "not found" in msg.lower() else ErrorCode.VALIDATION
                errors.append(BatchFailure(id=obs_id, error=msg, code=err_code))
        return linked, errors

    def promote_observations_to_issue(
        self,
        obs_ids: list[str],
        *,
        issue_type: str = "task",
        priority: int | None = None,
        title: str | None = None,
        extra_description: str = "",
        actor: str = "",
        labels: list[str] | None = None,
    ) -> PromoteObservationResult:
        """Promote several observations into one issue, preserving all IDs."""
        if not isinstance(obs_ids, list) or not obs_ids or not all(isinstance(obs_id, str) for obs_id in obs_ids):
            msg = "obs_ids must be a non-empty list of strings"
            raise TypeError(msg)
        title = _require_optional_string(title, "title")
        extra_description = _require_string(extra_description, "extra_description")
        actor = _require_string(actor, "actor")
        labels = _require_string_list(labels, "labels")
        unique_ids = list(dict.fromkeys(obs_ids))
        source_ids_json = json.dumps(unique_ids, separators=(",", ":"))

        now = _now_iso()
        warnings: list[str] = []
        _begin_immediate(self.conn, "promote_observations_to_issue")
        try:
            existing_issue_row = self.conn.execute(
                "SELECT id FROM issues WHERE json_valid(fields) AND json_extract(fields, '$.source_observation_ids') = json(?)",
                (source_ids_json,),
            ).fetchone()
            if existing_issue_row is not None:
                self.conn.commit()
                existing_issue = self.get_issue(existing_issue_row["id"])
                msg = f"Observations {unique_ids} were already promoted to issue {existing_issue.id} (returning existing)"
                logger.info(msg)
                warnings.append(msg)

                cleanup_placeholders = ",".join("?" for _ in unique_ids)
                cleanup_rows = self.conn.execute(
                    f"SELECT * FROM observations WHERE id IN ({cleanup_placeholders})",
                    unique_ids,
                ).fetchall()
                cleanup_by_id = {row["id"]: dict(row) for row in cleanup_rows}
                for obs_id in unique_ids:
                    obs = cleanup_by_id.get(obs_id)
                    if obs is None:
                        continue
                    try:
                        self.link_observation_to_issue(
                            obs_id,
                            existing_issue.id,
                            disposition="evidence",
                            reason="promoted into merged issue",
                            actor=actor or obs["actor"],
                        )
                    except (sqlite3.Error, ValueError) as exc:
                        cleanup_msg = (
                            f"Failed to clean up linked observation {obs_id} after finding existing issue {existing_issue.id}: {exc}"
                        )
                        logger.warning(cleanup_msg, exc_info=True)
                        warnings.append(cleanup_msg)

                _record_promotion_reconciliation_debt(self.conn, existing_issue.id, warnings)
                idem_result = cast(PromoteObservationResult, {"issue": existing_issue, "warnings": warnings})
                return idem_result

            placeholders = ",".join("?" for _ in unique_ids)
            rows = self.conn.execute(f"SELECT * FROM observations WHERE id IN ({placeholders})", unique_ids).fetchall()
            by_id = {row["id"]: dict(row) for row in rows}
            missing = [obs_id for obs_id in unique_ids if obs_id not in by_id]
            if missing:
                self.conn.rollback()
                raise ValueError(f"Observation not found: {missing[0]}")
            observations = [by_id[obs_id] for obs_id in unique_ids]
            expired = [obs["id"] for obs in observations if obs["expires_at"] <= now]
            if expired:
                self.conn.rollback()
                raise ValueError(f"Observation {expired[0]} has expired and cannot be promoted")

            issue_title = title or observations[0]["summary"]
            issue_priority = priority if priority is not None else min(int(obs["priority"]) for obs in observations)
            desc_parts: list[str] = []
            if extra_description:
                desc_parts.append(extra_description)
            desc_parts.append("Promoted observations:\n" + "\n".join(_observation_evidence_block(obs) for obs in observations))
            description = "\n\n".join(desc_parts)

            carry_labels = ["from-observation", *list(dict.fromkeys(labels or []))]
            issue = self.create_issue(
                issue_title,
                type=issue_type,
                priority=issue_priority,
                description=description,
                actor=actor or observations[0]["actor"],
                fields={"source_observation_ids": unique_ids},
                labels=carry_labels,
                _skip_begin=True,
            )
            self.conn.commit()
        except Exception:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise

        for obs in observations:
            try:
                self.link_observation_to_issue(
                    obs["id"],
                    issue.id,
                    disposition="evidence",
                    reason="promoted into merged issue",
                    actor=actor or obs["actor"],
                )
            except (sqlite3.Error, ValueError) as exc:
                msg = f"Failed to clean up linked observation {obs['id']} after creating issue {issue.id}: {exc}"
                logger.warning(msg, exc_info=True)
                warnings.append(msg)

        _record_promotion_reconciliation_debt(self.conn, issue.id, warnings)
        refreshed = self.get_issue(issue.id)
        result = cast(PromoteObservationResult, {"issue": refreshed})
        if warnings:
            result["warnings"] = warnings
        return result

    def batch_promote_observations(
        self,
        obs_ids: list[str],
        *,
        issue_type: str = "task",
        priority: int | None = None,
        actor: str = "",
    ) -> tuple[list[PromoteObservationResult], list[BatchFailure]]:
        """Promote multiple observations with per-item error reporting."""
        if not isinstance(obs_ids, list) or not all(isinstance(obs_id, str) for obs_id in obs_ids):
            msg = "obs_ids must be a list of strings"
            raise TypeError(msg)

        promoted: list[PromoteObservationResult] = []
        errors: list[BatchFailure] = []
        for obs_id in dict.fromkeys(obs_ids):
            try:
                promoted.append(
                    self.promote_observation(
                        obs_id,
                        issue_type=issue_type,
                        priority=priority,
                        actor=actor,
                    )
                )
            except ValueError as e:
                msg = str(e)
                err_code = ErrorCode.NOT_FOUND if "not found" in msg.lower() else ErrorCode.VALIDATION
                errors.append(BatchFailure(id=obs_id, error=msg, code=err_code))
        return promoted, errors

    def promote_observation(
        self,
        obs_id: str,
        *,
        issue_type: str = "task",
        priority: int | None = None,
        title: str | None = None,
        extra_description: str = "",
        actor: str = "",
        labels: list[str] | None = None,
    ) -> PromoteObservationResult:
        """Promote an observation to a tracked issue.

        ``labels`` lets the caller carry session-cluster context (e.g.
        ``["cluster:mcp-review-e"]``) onto the promoted issue. The
        ``from-observation`` label is always added in addition.
        Senior-user MCP review run e P2.12.
        """
        title = _require_optional_string(title, "title")
        extra_description = _require_string(extra_description, "extra_description")
        actor = _require_string(actor, "actor")
        labels = _require_string_list(labels, "labels")
        # Idempotency check: if a prior promote already created an issue for this
        # obs_id (recorded in issue.fields.source_observation_id), return that
        # issue instead of creating a duplicate.  Handles the retry case where
        # the observation delete failed after the issue was committed.
        #
        # The check + create_issue is wrapped in BEGIN IMMEDIATE so two
        # concurrent promoters cannot both pass the check and both insert an
        # issue (mirrors the cooldown-check pattern in db_scans.py).
        # ``json_valid(fields)`` skips rows whose fields JSON is corrupt —
        # without it, one malformed row anywhere in the issues table makes
        # ``json_extract`` raise OperationalError and breaks every promote.
        warnings: list[str] = []
        _begin_immediate(self.conn, "promote_observation")
        try:
            existing_issue_row = self.conn.execute(
                "SELECT id FROM issues WHERE json_valid(fields) AND json_extract(fields, '$.source_observation_id') = ?",
                (obs_id,),
            ).fetchone()
            if existing_issue_row is not None:
                # Best-effort cleanup of lingering observation from prior failed promote,
                # including a missing promotion audit row when the prior cleanup
                # and audit both failed after the issue had already committed.
                # Lives inside the BEGIN IMMEDIATE so a single commit covers both reads
                # and the cleanup write atomically.
                lingering_obs = self.conn.execute("SELECT summary, actor FROM observations WHERE id = ?", (obs_id,)).fetchone()
                if lingering_obs is not None:
                    self.conn.execute("DELETE FROM observations WHERE id = ?", (obs_id,))
                    audit_exists = self.conn.execute(
                        "SELECT 1 FROM dismissed_observations WHERE obs_id = ? AND reason = 'promoted' LIMIT 1",
                        (obs_id,),
                    ).fetchone()
                    if audit_exists is None:
                        self.conn.execute(
                            "INSERT INTO dismissed_observations (obs_id, summary, actor, reason, dismissed_at) "
                            "VALUES (?, ?, ?, 'promoted', ?)",
                            (obs_id, lingering_obs["summary"], actor or lingering_obs["actor"], _now_iso()),
                        )
                self.conn.commit()
                existing_issue = self.get_issue(existing_issue_row["id"])
                msg = f"Observation {obs_id} was already promoted to issue {existing_issue.id} (returning existing)"
                logger.info(msg)
                warnings.append(msg)
                idem_result = cast(PromoteObservationResult, {"issue": existing_issue, "warnings": warnings})
                return idem_result

            # 1. Read observation (don't delete yet)
            row = self.conn.execute("SELECT * FROM observations WHERE id = ?", (obs_id,)).fetchone()
            if row is None:
                self.conn.rollback()
                raise ValueError(f"Observation not found: {obs_id}")
            obs = dict(row)

            # Reject expired observations — consistent with TTL enforcement elsewhere.
            if obs["expires_at"] <= _now_iso():
                self.conn.rollback()
                raise ValueError(f"Observation {obs_id} has expired and cannot be promoted")

            # 2. Build issue fields
            issue_title = title or obs["summary"]
            desc_parts = []
            if extra_description:
                desc_parts.append(extra_description)
            if obs["detail"]:
                desc_parts.append(obs["detail"])
            if obs["file_path"]:
                loc = f"`{obs['file_path']}`"
                if obs["line"] is not None:
                    loc += f":{obs['line']}"
                desc_parts.append(f"Observed in: {loc}")
            if obs.get("source_issue_id"):
                desc_parts.append(f"Observed while working on: {obs['source_issue_id']}")
            description = "\n\n".join(desc_parts)

            # 3. Create issue first — if this fails, observation is untouched.
            #    The source_observation_id field is the durable idempotency key:
            #    retries after a cleanup failure see the existing issue and return
            #    it instead of creating a duplicate.
            #
            #    Under 2.1.0 §2.1, ``create_issue`` is wrapped in
            #    ``@_in_immediate_tx``; pass ``_skip_begin=True`` so it runs
            #    inside the outer IMMEDIATE we just opened. Then commit
            #    explicitly to release the writer lock before the observation
            #    cleanup runs in its own transaction — preserves the prior
            #    invariant that a DELETE failure must not roll back the
            #    committed issue.
            issue = self.create_issue(
                issue_title,
                type=issue_type,
                priority=priority if priority is not None else obs["priority"],
                description=description,
                actor=actor or obs["actor"],
                fields={"source_observation_id": obs_id},
                _skip_begin=True,
            )
            self.conn.commit()
        except Exception:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise

        # 4. Issue created successfully — now clean up the observation.
        #    Delete the observation FIRST (prevents double-promotion on retry),
        #    then write the audit trail (nice-to-have).  If only the audit trail
        #    fails, the observation is already gone so retries get "not found".
        now = _now_iso()
        try:
            cursor = self.conn.execute("DELETE FROM observations WHERE id = ?", (obs_id,))
            self.conn.commit()
            if cursor.rowcount == 0:
                # Observation was swept by a concurrent TTL cleanup between our
                # SELECT and this DELETE.  The issue is already created, so this
                # is harmless — but surface a warning so callers know.
                msg = f"Observation {obs_id} was already swept before cleanup (issue {issue.id} created successfully)"
                logger.info(msg)
                warnings.append(msg)
        except sqlite3.Error:
            self.conn.rollback()
            msg = f"Failed to delete observation {obs_id} after promotion (issue {issue.id} created — retry may create duplicates)"
            logger.warning(msg, exc_info=True)
            warnings.append(msg)

        # 4b. Audit trail — best-effort, separate transaction.
        try:
            self.conn.execute(
                "INSERT INTO dismissed_observations (obs_id, summary, actor, reason, dismissed_at) VALUES (?, ?, ?, 'promoted', ?)",
                (obs_id, obs["summary"], actor or obs["actor"], now),
            )
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()
            msg = f"Failed to record promotion audit trail for observation {obs_id}"
            logger.warning(msg, exc_info=True)
            warnings.append(msg)

        # 5. Enrichments (non-critical — failure should not undo the promotion)
        carry_labels = ["from-observation", *list(dict.fromkeys(labels or []))]
        for lbl in carry_labels:
            try:
                self.add_label(issue.id, lbl)
            except (sqlite3.Error, ValueError) as exc:
                msg = f"Failed to add label {lbl!r} to promoted issue {issue.id}: {exc}"
                logger.warning(msg, exc_info=True)
                warnings.append(msg)

        file_id = obs.get("file_id")
        try:
            if file_id:
                self.add_file_association(file_id, issue.id, "mentioned_in")
        except (sqlite3.Error, ValueError) as exc:
            msg = f"Failed to add file association for promoted observation {obs_id}: {exc}"
            logger.warning(msg, exc_info=True)
            warnings.append(msg)

        result = cast(PromoteObservationResult, {"issue": issue})
        if warnings:
            _record_promotion_reconciliation_debt(self.conn, issue.id, warnings)
            result["warnings"] = warnings
        return result
