"""Tests for observation CRUD operations."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from filigree.core import FiligreeDB
from filigree.db_base import _now_iso
from filigree.registry import RegistryUnavailableError, ResolvedFile

from .._db_factory import make_db
from .._fakes.registry import FixedRegistry


class _CanonicalThenUnavailableRegistry:
    def __init__(self) -> None:
        self.calls = 0

    def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
        self.calls += 1
        if self.calls > 1:
            raise RegistryUnavailableError("registry down", path=path, cause_kind="network")
        return {
            "file_id": "clarion:file:canonical",
            "content_hash": "hash:canonical",
            "canonical_path": "src/canonical.py",
            "language": language,
            "registry_backend": "clarion",
        }

    def is_displaced(self) -> bool:
        return True


class TestCreateObservation:
    def test_create_observation_file_path_uses_registry_resolved_file_id(self, tmp_path: Path) -> None:
        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            registry=FixedRegistry(file_id="core:file:obs123@src/observed.py"),
        )
        try:
            db.initialize()

            obs = db.create_observation("file-level bug", file_path="src/observed.py")

            assert obs["file_id"] == "core:file:obs123@src/observed.py"
            assert db.get_file("core:file:obs123@src/observed.py").path == "src/observed.py"
        finally:
            db.close()

    def test_create_minimal(self, db: FiligreeDB) -> None:
        obs = db.create_observation("Something looks wrong here")
        assert obs["id"].startswith("test-")
        assert obs["summary"] == "Something looks wrong here"
        assert obs["priority"] == 3
        assert obs["expires_at"] > obs["created_at"]  # 14 days in future

    def test_create_with_all_fields(self, db: FiligreeDB) -> None:
        obs = db.create_observation(
            "Possible null deref",
            detail="Line 42 dereferences result without checking for None",
            file_path="src/core.py",
            line=42,
            priority=1,
            actor="claude",
        )
        assert obs["summary"] == "Possible null deref"
        assert obs["detail"].startswith("Line 42")
        assert obs["file_path"] == "src/core.py"
        assert obs["line"] == 42
        assert obs["priority"] == 1
        assert obs["actor"] == "claude"

    def test_create_empty_summary_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="summary"):
            db.create_observation("")

    def test_create_non_string_summary_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="summary must be a string"):
            db.create_observation(123)  # type: ignore[arg-type]

    @pytest.mark.parametrize("field", ["detail", "source_issue_id", "source_finding_id", "actor"])
    def test_create_non_string_text_fields_raise(self, db: FiligreeDB, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            db.create_observation("bad text field", **{field: 123})  # type: ignore[arg-type]

    def test_create_with_source_issue_id(self, db: FiligreeDB) -> None:
        obs = db.create_observation("odd behavior", source_issue_id="test-abc123")
        assert obs["source_issue_id"] == "test-abc123"

    def test_create_duplicate_is_ignored(self, db: FiligreeDB) -> None:
        """Dedup index silently drops exact duplicates (same summary+file+line)."""
        result1 = db.create_observation("bug here", file_path="src/foo.py", line=10)
        result2 = db.create_observation("bug here", file_path="src/foo.py", line=10)
        assert db.observation_count() == 1
        assert result2["id"] == result1["id"]  # Returns existing record

    def test_create_duplicate_no_line_is_ignored(self, db: FiligreeDB) -> None:
        """Most common case: file-level observation without a specific line."""
        result1 = db.create_observation("file-level bug", file_path="src/foo.py")
        result2 = db.create_observation("file-level bug", file_path="src/foo.py")
        assert db.observation_count() == 1
        assert result2["id"] == result1["id"]

    def test_live_duplicate_returns_before_registry_refresh(self, tmp_path: Path) -> None:
        registry = _CanonicalThenUnavailableRegistry()
        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            registry=registry,
            registry_backend="clarion",
        )
        try:
            db.initialize()
            result1 = db.create_observation("file-level bug", file_path="src/foo.py")

            result2 = db.create_observation("file-level bug", file_path="src/foo.py")

            assert result2["id"] == result1["id"]
            assert registry.calls == 1
        finally:
            db.close()

    def test_create_duplicate_replaces_expired(self, db: FiligreeDB) -> None:
        """An expired duplicate is swept and replaced with a fresh observation."""
        obs1 = db.create_observation("stale finding", file_path="src/bar.py", line=5)
        original_id = obs1["id"]
        # Expire the observation
        db.conn.execute(
            "UPDATE observations SET expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (original_id,),
        )
        db.conn.commit()

        # Re-creating the same observation should succeed with a new ID
        obs2 = db.create_observation("stale finding", file_path="src/bar.py", line=5)
        assert obs2["id"] != original_id
        assert db.observation_count() == 1

        # Original should be in audit trail
        row = db.conn.execute("SELECT * FROM dismissed_observations WHERE obs_id = ?", (original_id,)).fetchone()
        assert row is not None
        assert row["reason"] == "expired (replaced)"

    def test_create_duplicate_replaces_expired_atomically(self, db: FiligreeDB) -> None:
        """Delete-expired + insert happen in one transaction — no intermediate commit."""
        obs1 = db.create_observation("atomic check", file_path="src/x.py", line=1)
        # Expire the observation
        db.conn.execute(
            "UPDATE observations SET expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (obs1["id"],),
        )
        db.conn.commit()

        # After replacement, exactly 1 observation should exist
        obs2 = db.create_observation("atomic check", file_path="src/x.py", line=1)
        assert obs2["id"] != obs1["id"]
        assert db.observation_count() == 1
        # Audit trail has the expired original
        row = db.conn.execute("SELECT * FROM dismissed_observations WHERE obs_id = ?", (obs1["id"],)).fetchone()
        assert row is not None
        assert row["reason"] == "expired (replaced)"

    def test_create_rollback_preserves_expired_duplicate_on_insert_failure(self, db: FiligreeDB) -> None:
        """If INSERT fails after expired-duplicate deletion, rollback restores the original."""
        obs1 = db.create_observation("will survive", file_path="src/rollback.py", line=1)
        original_id = obs1["id"]
        # Expire the observation
        db.conn.execute(
            "UPDATE observations SET expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (original_id,),
        )
        db.conn.commit()

        real_conn = db._conn

        def _fail_on_insert(real: Any, sql: str, params: Any = ()) -> Any:
            if "INSERT INTO observations" in sql and "dismissed_observations" not in sql:
                raise sqlite3.OperationalError("disk I/O error on insert")
            return real.execute(sql, params)

        db._conn = _InterceptingConn(real_conn, _fail_on_insert)  # type: ignore[assignment]
        try:
            with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
                db.create_observation("will survive", file_path="src/rollback.py", line=1)
        finally:
            db._conn = real_conn

        # Original expired observation should be restored by rollback
        row = db.conn.execute("SELECT id FROM observations WHERE id = ?", (original_id,)).fetchone()
        assert row is not None, "Rollback should have preserved the expired observation"

    def test_create_auto_commit_false_rolls_back_expired_cleanup_on_insert_failure(self, db: FiligreeDB) -> None:
        """An outer transaction commit must not preserve failed replacement cleanup."""
        obs1 = db.create_observation("outer survives", file_path="src/outer.py", line=1)
        original_id = obs1["id"]
        db.conn.execute(
            "UPDATE observations SET expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (original_id,),
        )
        db.conn.commit()

        real_conn = db._conn

        def _fail_on_insert(real: Any, sql: str, params: Any = ()) -> Any:
            if "INSERT INTO observations" in sql and "dismissed_observations" not in sql:
                raise sqlite3.OperationalError("disk I/O error on insert")
            return real.execute(sql, params)

        db._conn = _InterceptingConn(real_conn, _fail_on_insert)  # type: ignore[assignment]
        try:
            with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
                db.create_observation("outer survives", file_path="src/outer.py", line=1, auto_commit=False)
        finally:
            db._conn = real_conn

        # Simulate callers like process_scan_results catching observation errors
        # and committing the rest of their outer transaction.
        db.conn.commit()

        row = db.conn.execute("SELECT id FROM observations WHERE id = ?", (original_id,)).fetchone()
        assert row is not None, "Savepoint rollback should preserve the expired observation"
        audit = db.conn.execute("SELECT id FROM dismissed_observations WHERE obs_id = ?", (original_id,)).fetchone()
        assert audit is None

    def test_create_different_summary_same_location_allowed(self, db: FiligreeDB) -> None:
        db.create_observation("null deref", file_path="src/foo.py", line=10)
        db.create_observation("type error", file_path="src/foo.py", line=10)
        assert db.observation_count() == 2

    def test_create_priority_boundary_zero(self, db: FiligreeDB) -> None:
        obs = db.create_observation("critical thing", priority=0)
        assert obs["priority"] == 0

    def test_create_priority_boundary_four(self, db: FiligreeDB) -> None:
        obs = db.create_observation("backlog thing", priority=4)
        assert obs["priority"] == 4

    def test_create_priority_out_of_range_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="priority"):
            db.create_observation("bad priority", priority=5)

    def test_create_negative_priority_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="priority"):
            db.create_observation("bad priority", priority=-1)

    @pytest.mark.parametrize("priority", [True, "high"])
    def test_create_non_integer_priority_raises(self, db: FiligreeDB, priority: object) -> None:
        with pytest.raises(ValueError, match="priority must be an integer"):
            db.create_observation("bad priority", priority=priority)  # type: ignore[arg-type]

    def test_create_negative_line_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="line"):
            db.create_observation("bad line", line=-1)

    def test_create_bool_line_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="line must be an integer"):
            db.create_observation("bad line", line=True)  # type: ignore[arg-type]

    def test_create_huge_line_raises_before_sqlite_binding(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="line must be <="):
            db.create_observation("line overflow", line=9_223_372_036_854_775_808)

    def test_create_whitespace_only_summary_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="summary"):
            db.create_observation("   ")

    def test_create_with_line_zero(self, db: FiligreeDB) -> None:
        obs = db.create_observation("bug at top", file_path="src/foo.py", line=0)
        assert obs["line"] == 0

    def test_create_with_file_auto_registers(self, db: FiligreeDB) -> None:
        obs = db.create_observation("bug here", file_path="src/main.py")
        assert obs["file_id"] is not None
        f = db.get_file(obs["file_id"])
        assert f.path == "src/main.py"

    def test_create_observation_rejects_non_string_file_path(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="file_path must be a string"):
            db.create_observation("bad path", file_path=123)  # type: ignore[arg-type]

    @pytest.mark.parametrize("file_path", [0, False, None], ids=["zero", "false", "none"])
    def test_create_observation_rejects_falsey_non_string_file_path(self, db: FiligreeDB, file_path: object) -> None:
        with pytest.raises(ValueError, match="file_path must be a string"):
            db.create_observation("bad falsey path", file_path=file_path)  # type: ignore[arg-type]

    @pytest.mark.parametrize("auto_commit", [True, False])
    def test_create_observation_rejects_non_project_relative_file_path(self, db: FiligreeDB, auto_commit: bool) -> None:
        with pytest.raises(ValueError, match="project-relative"):
            db.create_observation("bad path", file_path="../outside.py", auto_commit=auto_commit)

    def test_create_observation_rejects_absolute_file_path(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="project-relative"):
            db.create_observation("rooted path", file_path="/etc/passwd")

    def test_create_observation_insert_failure_removes_newly_created_file_record(self, db: FiligreeDB) -> None:
        """filigree-8b285ec210: orphaned file_record must not linger when obs insert fails."""
        real_conn = db._conn

        def _fail_on_obs_insert(real: Any, sql: str, params: Any = ()) -> Any:
            if "INSERT INTO observations" in sql and "dismissed_observations" not in sql:
                raise sqlite3.OperationalError("disk I/O error on insert")
            return real.execute(sql, params)

        db._conn = _InterceptingConn(real_conn, _fail_on_obs_insert)  # type: ignore[assignment]
        try:
            with pytest.raises(sqlite3.OperationalError):
                db.create_observation("will fail", file_path="src/new/file_orphan.py")
        finally:
            db._conn = real_conn

        # No observation persisted
        assert db.observation_count() == 0
        # file_record must also be gone — would otherwise be orphaned
        row = db.conn.execute("SELECT id FROM file_records WHERE path = ?", ("src/new/file_orphan.py",)).fetchone()
        assert row is None, "file_record should have been compensated when observation insert failed"

    def test_create_observation_insert_failure_keeps_preexisting_file_record(self, db: FiligreeDB) -> None:
        """If the file_record already existed, a failed observation insert must NOT delete it."""
        # Pre-register the file
        pre = db.register_file("src/existing/keep.py")

        real_conn = db._conn

        def _fail_on_obs_insert(real: Any, sql: str, params: Any = ()) -> Any:
            if "INSERT INTO observations" in sql and "dismissed_observations" not in sql:
                raise sqlite3.OperationalError("disk I/O error on insert")
            return real.execute(sql, params)

        db._conn = _InterceptingConn(real_conn, _fail_on_obs_insert)  # type: ignore[assignment]
        try:
            with pytest.raises(sqlite3.OperationalError):
                db.create_observation("will fail", file_path="src/existing/keep.py")
        finally:
            db._conn = real_conn

        # Pre-existing file_record must still be there
        row = db.conn.execute("SELECT id FROM file_records WHERE path = ?", ("src/existing/keep.py",)).fetchone()
        assert row is not None, "pre-existing file_record must be preserved"
        assert row["id"] == pre.id


class TestListObservations:
    def test_list_empty(self, db: FiligreeDB) -> None:
        assert db.list_observations() == []

    def test_list_returns_all(self, db: FiligreeDB) -> None:
        db.create_observation("First")
        db.create_observation("Second")
        assert len(db.list_observations()) == 2

    def test_list_ordered_by_priority_then_created(self, db: FiligreeDB) -> None:
        db.create_observation("Low priority", priority=3)
        db.create_observation("High priority", priority=1)
        result = db.list_observations()
        assert result[0]["priority"] == 1
        assert result[1]["priority"] == 3

    def test_list_with_limit(self, db: FiligreeDB) -> None:
        for i in range(5):
            db.create_observation(f"Obs {i}")
        assert len(db.list_observations(limit=2)) == 2

    @pytest.mark.parametrize("limit", [0, -1, True], ids=["zero", "negative", "bool"])
    def test_list_invalid_limit_raises(self, db: FiligreeDB, limit: object) -> None:
        with pytest.raises(ValueError, match="limit"):
            db.list_observations(limit=limit)  # type: ignore[arg-type]

    def test_list_with_offset(self, db: FiligreeDB) -> None:
        for i in range(5):
            db.create_observation(f"Obs {i}", priority=i % 5)
        all_obs = db.list_observations()
        offset_obs = db.list_observations(offset=2)
        assert len(offset_obs) == 3
        assert offset_obs[0]["id"] == all_obs[2]["id"]

    def test_list_with_limit_and_offset(self, db: FiligreeDB) -> None:
        for i in range(5):
            db.create_observation(f"Obs {i}", priority=i % 5)
        all_obs = db.list_observations()
        page = db.list_observations(limit=2, offset=1)
        assert len(page) == 2
        assert page[0]["id"] == all_obs[1]["id"]
        assert page[1]["id"] == all_obs[2]["id"]

    def test_list_offset_beyond_results(self, db: FiligreeDB) -> None:
        db.create_observation("Only one")
        assert db.list_observations(offset=10) == []

    @pytest.mark.parametrize("offset", [-1, True], ids=["negative", "bool"])
    def test_list_invalid_offset_raises(self, db: FiligreeDB, offset: object) -> None:
        with pytest.raises(ValueError, match="offset"):
            db.list_observations(offset=offset)  # type: ignore[arg-type]

    def test_list_filter_by_file_path(self, db: FiligreeDB) -> None:
        db.create_observation("api bug", file_path="src/api/routes.py")
        db.create_observation("core bug", file_path="src/core.py")
        result = db.list_observations(file_path="src/api")
        assert len(result) == 1
        assert result[0]["summary"] == "api bug"

    def test_list_filter_by_file_id(self, db: FiligreeDB) -> None:
        """Direct FK query by file_id — more precise than path LIKE."""
        obs1 = db.create_observation("api bug", file_path="src/api/routes.py")
        db.create_observation("core bug", file_path="src/core.py")
        result = db.list_observations(file_id=obs1["file_id"])
        assert len(result) == 1
        assert result[0]["summary"] == "api bug"

    def test_list_filter_by_file_id_no_results(self, db: FiligreeDB) -> None:
        db.create_observation("bug", file_path="src/core.py")
        result = db.list_observations(file_id="nonexistent-file-id")
        assert len(result) == 0

    def test_list_filter_file_path_with_special_chars(self, db: FiligreeDB) -> None:
        """LIKE wildcards in file_path are escaped so % and _ are literal."""
        db.create_observation("special", file_path="src/100%_done.py")
        db.create_observation("other", file_path="src/normal.py")
        result = db.list_observations(file_path="100%_done")
        assert len(result) == 1
        assert result[0]["summary"] == "special"

    def test_list_filter_file_path_with_backslash(self, db: FiligreeDB) -> None:
        """Backslash in file_path is treated as literal, not LIKE escape."""
        db.create_observation("windows path bug", file_path="src\\module\\file.py")
        db.create_observation("unrelated", file_path="src/other.py")
        result = db.list_observations(file_path="src\\module")
        assert len(result) == 1
        assert result[0]["summary"] == "windows path bug"

    def test_list_filter_by_actor(self, db: FiligreeDB) -> None:
        db.create_observation("alpha note", actor="alpha")
        db.create_observation("beta note", actor="beta")
        db.create_observation("alpha note 2", actor="alpha")
        result = db.list_observations(actor="alpha")
        summaries = sorted(r["summary"] for r in result)
        assert summaries == ["alpha note", "alpha note 2"]

    def test_list_filter_by_source_issue_id(self, db: FiligreeDB) -> None:
        db.create_observation("from issue", source_issue_id="test-issue-1")
        db.create_observation("unrelated", source_issue_id="test-issue-2")
        result = db.list_observations(source_issue_id="test-issue-1")
        assert len(result) == 1
        assert result[0]["summary"] == "from issue"

    def test_list_filter_by_priority_range(self, db: FiligreeDB) -> None:
        db.create_observation("p0", priority=0)
        db.create_observation("p2", priority=2)
        db.create_observation("p4", priority=4)
        result = db.list_observations(priority_min=1, priority_max=3)
        assert len(result) == 1
        assert result[0]["summary"] == "p2"

    def test_list_inverted_priority_range_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="priority_min"):
            db.list_observations(priority_min=3, priority_max=1)

    def test_list_filter_older_than_hours(self, db: FiligreeDB) -> None:
        old = db.create_observation("ancient")
        db.create_observation("fresh")
        # Backdate the first observation by 100 hours
        from datetime import UTC, datetime, timedelta

        old_dt = (datetime.now(UTC) - timedelta(hours=100)).isoformat()
        db.conn.execute("UPDATE observations SET created_at = ? WHERE id = ?", (old_dt, old["id"]))
        db.conn.commit()
        result = db.list_observations(older_than_hours=48)
        assert len(result) == 1
        assert result[0]["summary"] == "ancient"

    def test_list_sort_by_created_at_desc(self, db: FiligreeDB) -> None:
        from datetime import UTC, datetime, timedelta

        first = db.create_observation("first")
        db.create_observation("second")
        # Backdate the first so created_at ordering is deterministic
        backdated = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        db.conn.execute("UPDATE observations SET created_at = ? WHERE id = ?", (backdated, first["id"]))
        db.conn.commit()
        result = db.list_observations(sort_by="created_at", direction="desc")
        assert [r["summary"] for r in result[:2]] == ["second", "first"]

    def test_list_invalid_sort_by_raises(self, db: FiligreeDB) -> None:
        import pytest

        with pytest.raises(ValueError, match="sort_by must be"):
            db.list_observations(sort_by="evil; DROP TABLE observations")

    def test_list_invalid_direction_raises(self, db: FiligreeDB) -> None:
        import pytest

        with pytest.raises(ValueError, match="direction must be"):
            db.list_observations(direction="sideways")

    def test_list_actor_and_priority_compose(self, db: FiligreeDB) -> None:
        db.create_observation("alpha-p1", actor="alpha", priority=1)
        db.create_observation("alpha-p3", actor="alpha", priority=3)
        db.create_observation("beta-p1", actor="beta", priority=1)
        result = db.list_observations(actor="alpha", priority_max=1)
        assert len(result) == 1
        assert result[0]["summary"] == "alpha-p1"

    def test_list_rejects_non_string_file_path_filter(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="file_path must be a string"):
            db.list_observations(file_path=123)  # type: ignore[arg-type]

    @pytest.mark.parametrize("field", ["file_id", "actor", "source_issue_id", "sort_by", "direction"])
    def test_list_rejects_non_string_text_filters(self, db: FiligreeDB, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            db.list_observations(**{field: 123})  # type: ignore[arg-type]

    @pytest.mark.parametrize("field", ["priority_min", "priority_max", "older_than_hours"])
    def test_list_rejects_bool_numeric_filters(self, db: FiligreeDB, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            db.list_observations(**{field: True})  # type: ignore[arg-type]

    def test_list_sweeps_expired(self, db: FiligreeDB) -> None:
        """Expired observations are auto-removed on list and logged to audit trail."""
        obs = db.create_observation("Will expire")
        # Manually set expires_at to the past
        db.conn.execute(
            "UPDATE observations SET expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (obs["id"],),
        )
        db.conn.commit()
        result = db.list_observations()
        assert len(result) == 0
        # Verify audit trail
        row = db.conn.execute("SELECT * FROM dismissed_observations WHERE obs_id = ?", (obs["id"],)).fetchone()
        assert row is not None
        assert row["reason"] == "expired (TTL)"
        assert row["actor"] == "system"


class _InterceptingConn:
    """Thin wrapper around a sqlite3.Connection that intercepts execute calls."""

    def __init__(self, real: Any, intercept: Any) -> None:
        self._real = real
        self._intercept = intercept

    def execute(self, sql: str, params: Any = ()) -> Any:
        return self._intercept(self._real, sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class TestSweepExceptionSuppression:
    """Verify _sweep_expired_observations suppresses transient errors but propagates code bugs."""

    def test_sweep_catches_operational_error(self, db: FiligreeDB) -> None:
        """OperationalError during sweep is caught — list_observations still returns results."""
        import sqlite3

        db.create_observation("survives sweep failure")

        real_conn = db._conn

        def _fail_on_delete(real: Any, sql: str, params: Any = ()) -> Any:
            if "DELETE FROM observations WHERE expires_at" in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return real.execute(sql, params)

        db._conn = _InterceptingConn(real_conn, _fail_on_delete)  # type: ignore[assignment]
        try:
            result = db.list_observations()
        finally:
            db._conn = real_conn
        assert len(result) == 1

    def test_sweep_failure_does_not_surface_expired_rows(self, db: FiligreeDB) -> None:
        """filigree-6b05db86a3: when sweep fails, list_observations must still hide expired rows."""
        import sqlite3

        live = db.create_observation("live one")
        expired = db.create_observation("expired one")
        db.conn.execute(
            "UPDATE observations SET expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (expired["id"],),
        )
        db.conn.commit()

        real_conn = db._conn

        def _fail_on_sweep_delete(real: Any, sql: str, params: Any = ()) -> Any:
            # Fail the sweep's DELETE, not any other DELETE
            if "DELETE FROM observations WHERE expires_at" in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return real.execute(sql, params)

        db._conn = _InterceptingConn(real_conn, _fail_on_sweep_delete)  # type: ignore[assignment]
        try:
            result = db.list_observations()
            stats = db.observation_stats(sweep=True)
        finally:
            db._conn = real_conn

        ids = {o["id"] for o in result}
        assert live["id"] in ids
        assert expired["id"] not in ids, "expired row must not be returned as live when sweep failed"
        # Stats count must match the visible rows (not include the expired row)
        assert stats["count"] == 1

    def test_repeated_sweep_failures_are_visible_in_stats(self, db: FiligreeDB) -> None:
        db.create_observation("survives repeated sweep failure")
        real_conn = db._conn

        def _fail_on_sweep_delete(real: Any, sql: str, params: Any = ()) -> Any:
            if "DELETE FROM observations WHERE expires_at" in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return real.execute(sql, params)

        db._conn = _InterceptingConn(real_conn, _fail_on_sweep_delete)  # type: ignore[assignment]
        try:
            db.list_observations()
            db.list_observations()
        finally:
            db._conn = real_conn

        stats = db.observation_stats(sweep=False)
        assert stats["sweep_consecutive_failures"] == 2
        assert stats["last_successful_sweep_at"] is None

    def test_successful_sweep_resets_failure_count(self, db: FiligreeDB) -> None:
        db.create_observation("survives transient sweep failure")
        real_conn = db._conn

        def _fail_on_sweep_delete(real: Any, sql: str, params: Any = ()) -> Any:
            if "DELETE FROM observations WHERE expires_at" in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return real.execute(sql, params)

        db._conn = _InterceptingConn(real_conn, _fail_on_sweep_delete)  # type: ignore[assignment]
        try:
            db.list_observations()
        finally:
            db._conn = real_conn

        db.list_observations()

        stats = db.observation_stats(sweep=False)
        assert stats["sweep_consecutive_failures"] == 0
        assert stats["last_successful_sweep_at"] is not None

    def test_sweep_propagates_integrity_error(self, db: FiligreeDB) -> None:
        """IntegrityError during sweep indicates a real invariant violation, not a transient lock."""
        import sqlite3

        db.create_observation("will survive sweep failure")

        real_conn = db._conn

        def _fail_with_integrity(real: Any, sql: str, params: Any = ()) -> Any:
            if "DELETE FROM observations WHERE expires_at" in sql:
                raise sqlite3.IntegrityError("UNIQUE constraint failed")
            return real.execute(sql, params)

        db._conn = _InterceptingConn(real_conn, _fail_with_integrity)  # type: ignore[assignment]
        try:
            with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
                db.list_observations()
        finally:
            db._conn = real_conn

    def test_sweep_propagates_programming_error(self, db: FiligreeDB) -> None:
        """ProgrammingError during sweep propagates — it indicates a code bug, not transient failure."""
        import sqlite3

        db.create_observation("will survive sweep failure")

        real_conn = db._conn

        def _fail_with_programming(real: Any, sql: str, params: Any = ()) -> Any:
            if "DELETE FROM observations WHERE expires_at" in sql:
                raise sqlite3.ProgrammingError("SQL logic error")
            return real.execute(sql, params)

        db._conn = _InterceptingConn(real_conn, _fail_with_programming)  # type: ignore[assignment]
        try:
            with pytest.raises(sqlite3.ProgrammingError, match="SQL logic error"):
                db.list_observations()
        finally:
            db._conn = real_conn

    def test_sweep_propagates_non_sqlite_error(self, db: FiligreeDB) -> None:
        """Non-sqlite3.Error (e.g. RuntimeError) propagates through sweep."""
        db.create_observation("irrelevant")

        real_conn = db._conn

        def _fail_with_runtime(real: Any, sql: str, params: Any = ()) -> Any:
            if "DELETE FROM observations WHERE expires_at" in sql:
                raise RuntimeError("thread safety violation")
            return real.execute(sql, params)

        db._conn = _InterceptingConn(real_conn, _fail_with_runtime)  # type: ignore[assignment]
        try:
            with pytest.raises(RuntimeError, match="thread safety"):
                db.list_observations()
        finally:
            db._conn = real_conn


class TestSweepAuditTrailPruning:
    """Verify DISMISSED_AUDIT_TRAIL_CAP is enforced during sweep."""

    def test_sweep_prunes_dismissed_observations_to_cap(self, db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        import filigree.db_observations as obs_mod

        monkeypatch.setattr(obs_mod, "DISMISSED_AUDIT_TRAIL_CAP", 3)

        # Create and expire 5 observations so sweep moves them to dismissed_observations
        for i in range(5):
            obs = db.create_observation(f"obs-{i}", file_path=f"src/file{i}.py")
            db.conn.execute(
                "UPDATE observations SET expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
                (obs["id"],),
            )
        db.conn.commit()

        # Trigger sweep via list_observations
        db.list_observations()

        count = db.conn.execute("SELECT COUNT(*) FROM dismissed_observations").fetchone()[0]
        assert count == 3, f"Audit trail should be pruned to cap (3), got {count}"


class TestDismissObservation:
    def test_dismiss_deletes_and_logs(self, db: FiligreeDB) -> None:
        obs = db.create_observation("To dismiss")
        db.dismiss_observation(obs["id"], actor="tester", reason="not a real bug")
        assert db.list_observations() == []
        # Check audit trail
        row = db.conn.execute("SELECT * FROM dismissed_observations WHERE obs_id = ?", (obs["id"],)).fetchone()
        assert row is not None
        assert row["summary"] == "To dismiss"
        assert row["actor"] == "tester"
        assert row["reason"] == "not a real bug"

    def test_dismiss_nonexistent_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="not found"):
            db.dismiss_observation("nope-123")

    @pytest.mark.parametrize("field", ["actor", "reason"])
    def test_dismiss_rejects_non_string_audit_fields(self, db: FiligreeDB, field: str) -> None:
        obs = db.create_observation("To dismiss")
        with pytest.raises(ValueError, match=field):
            db.dismiss_observation(obs["id"], **{field: 123})  # type: ignore[arg-type]

    def test_batch_dismiss(self, db: FiligreeDB) -> None:
        o1 = db.create_observation("One")
        o2 = db.create_observation("Two")
        db.create_observation("Three")
        result = db.batch_dismiss_observations([o1["id"], o2["id"]])
        assert result["dismissed"] == 2
        assert result["not_found"] == []
        remaining = db.list_observations()
        assert len(remaining) == 1
        assert remaining[0]["summary"] == "Three"
        # Both logged in audit trail
        count = db.conn.execute("SELECT COUNT(*) FROM dismissed_observations").fetchone()[0]
        assert count == 2

    def test_batch_dismiss_empty_list(self, db: FiligreeDB) -> None:
        result = db.batch_dismiss_observations([])
        assert result == {"dismissed": 0, "not_found": []}

    def test_batch_dismiss_rejects_non_list_ids(self, db: FiligreeDB) -> None:
        with pytest.raises(TypeError, match="obs_ids must be a list of strings"):
            db.batch_dismiss_observations("obs-123")  # type: ignore[arg-type]

    def test_batch_dismiss_rejects_non_string_id_items(self, db: FiligreeDB) -> None:
        with pytest.raises(TypeError, match="obs_ids must be a list of strings"):
            db.batch_dismiss_observations(["obs-123", 123])  # type: ignore[list-item]

    @pytest.mark.parametrize("field", ["actor", "reason"])
    def test_batch_dismiss_rejects_non_string_audit_fields(self, db: FiligreeDB, field: str) -> None:
        obs = db.create_observation("To dismiss")
        with pytest.raises(ValueError, match=field):
            db.batch_dismiss_observations([obs["id"]], **{field: 123})  # type: ignore[arg-type]

    def test_batch_dismiss_duplicate_ids(self, db: FiligreeDB) -> None:
        o1 = db.create_observation("Only one")
        result = db.batch_dismiss_observations([o1["id"], o1["id"]])
        assert result["dismissed"] == 1
        assert result["not_found"] == []
        assert db.observation_count() == 0
        # Audit trail should have exactly one entry (SQL IN deduplicates)
        count = db.conn.execute("SELECT COUNT(*) FROM dismissed_observations WHERE obs_id = ?", (o1["id"],)).fetchone()[0]
        assert count == 1

    def test_batch_dismiss_partial_invalid_ids_reports_not_found(self, db: FiligreeDB) -> None:
        """Non-existent IDs reported in not_found; dismissed count reflects actual deletes."""
        o1 = db.create_observation("Real one")
        result = db.batch_dismiss_observations([o1["id"], "does-not-exist"])
        assert result["dismissed"] == 1
        assert result["not_found"] == ["does-not-exist"]
        assert db.observation_count() == 0


class TestPromoteObservation:
    def test_promote_creates_issue_and_deletes_observation(self, db: FiligreeDB) -> None:
        obs = db.create_observation(
            "Null pointer risk",
            detail="result.data used without check",
            file_path="src/api.py",
            line=99,
            priority=2,
        )
        result = db.promote_observation(obs["id"], issue_type="bug")
        issue = result["issue"]
        assert issue.title == "Null pointer risk"
        assert "result.data used without check" in issue.description
        assert issue.priority == 2
        assert issue.type == "bug"
        assert db.list_observations() == []

    def test_promote_adds_from_observation_label(self, db: FiligreeDB) -> None:
        obs = db.create_observation("bug")
        result = db.promote_observation(obs["id"])
        labels = db.conn.execute("SELECT label FROM labels WHERE issue_id = ?", (result["issue"].id,)).fetchall()
        assert any(row["label"] == "from-observation" for row in labels)

    def test_promote_carries_caller_supplied_labels(self, db: FiligreeDB) -> None:
        """Senior-user MCP review run e P2.12: caller labels travel with the promote.

        Agents tag their session work with ``cluster:foo``. Without the
        labels parameter, that context was lost at promote and the
        promoted issue carried only ``from-observation``. Caller labels
        now carry through, with ``from-observation`` always added.
        """
        obs = db.create_observation("noted code smell")
        result = db.promote_observation(obs["id"], labels=["cluster:mcp-review-e", "review:needed"])
        labels = {row["label"] for row in db.conn.execute("SELECT label FROM labels WHERE issue_id = ?", (result["issue"].id,)).fetchall()}
        assert "from-observation" in labels
        assert "cluster:mcp-review-e" in labels
        assert "review:needed" in labels

    @pytest.mark.parametrize("field", ["title", "extra_description", "actor"])
    def test_promote_rejects_non_string_text_fields(self, db: FiligreeDB, field: str) -> None:
        obs = db.create_observation("bad promote input")
        with pytest.raises(ValueError, match=field):
            db.promote_observation(obs["id"], **{field: 123})  # type: ignore[arg-type]

    def test_promote_rejects_non_list_labels(self, db: FiligreeDB) -> None:
        obs = db.create_observation("bad promote labels")
        with pytest.raises(TypeError, match="labels must be a list of strings"):
            db.promote_observation(obs["id"], labels="cluster:test")  # type: ignore[arg-type]

    def test_promote_rejects_non_string_label_items(self, db: FiligreeDB) -> None:
        obs = db.create_observation("bad promote labels")
        with pytest.raises(TypeError, match="labels must be a list of strings"):
            db.promote_observation(obs["id"], labels=["cluster:test", 123])  # type: ignore[list-item]

    def test_promote_with_file_creates_association(self, db: FiligreeDB) -> None:
        obs = db.create_observation("bug", file_path="src/core.py")
        result = db.promote_observation(obs["id"])
        files = db.get_issue_files(result["issue"].id)
        assert len(files) >= 1

    def test_promote_logs_to_dismissed_observations(self, db: FiligreeDB) -> None:
        """Promoted observations are logged to audit trail with reason='promoted'."""
        obs = db.create_observation("will promote")
        db.promote_observation(obs["id"])
        row = db.conn.execute("SELECT * FROM dismissed_observations WHERE obs_id = ?", (obs["id"],)).fetchone()
        assert row is not None
        assert row["reason"] == "promoted"

    def test_promote_nonexistent_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="not found"):
            db.promote_observation("nope-123")

    def test_promote_is_atomic_no_double_promote(self, db: FiligreeDB) -> None:
        """Second promote of same observation is idempotent — returns the original issue."""
        obs = db.create_observation("Once only")
        result1 = db.promote_observation(obs["id"])
        # Second promote finds the existing issue via source_observation_id and returns it.
        result2 = db.promote_observation(obs["id"])
        assert result2["issue"].id == result1["issue"].id
        assert "warnings" in result2
        assert any("already promoted" in w for w in result2["warnings"])

    def test_promote_with_line_zero_includes_location(self, db: FiligreeDB) -> None:
        """line=0 is valid and must appear in the promoted issue description."""
        obs = db.create_observation("top of file", file_path="src/main.py", line=0)
        result = db.promote_observation(obs["id"])
        assert ":0" in result["issue"].description

    def test_promote_with_source_issue_id_in_description(self, db: FiligreeDB) -> None:
        """source_issue_id appears in the promoted issue description."""
        obs = db.create_observation("side note", source_issue_id="test-abc")
        result = db.promote_observation(obs["id"])
        assert "test-abc" in result["issue"].description

    def test_promote_rejects_caller_transaction_without_rollback(self, db: FiligreeDB) -> None:
        """promote_observation must not silently roll back unrelated caller writes."""
        obs = db.create_observation("nested transaction risk")
        db.conn.execute(
            "INSERT INTO file_records (id, path, first_seen, updated_at) VALUES (?, ?, ?, ?)",
            ("pending-file", "src/pending.py", _now_iso(), _now_iso()),
        )

        with pytest.raises(RuntimeError, match="nested transaction not supported"):
            db.promote_observation(obs["id"])

        assert db.conn.in_transaction
        assert db.conn.execute("SELECT id FROM file_records WHERE id = 'pending-file'").fetchone() is not None
        db.conn.rollback()
        assert db.conn.execute("SELECT id FROM file_records WHERE id = 'pending-file'").fetchone() is None
        assert db.conn.execute("SELECT id FROM observations WHERE id = ?", (obs["id"],)).fetchone() is not None

    def test_promote_preserves_observation_on_create_issue_failure(self, db: FiligreeDB) -> None:
        """If create_issue raises, the observation is NOT deleted — no data loss."""
        from unittest.mock import patch

        obs = db.create_observation("will fail promote")
        with patch.object(db, "create_issue", side_effect=RuntimeError("boom")), pytest.raises(RuntimeError, match="boom"):
            db.promote_observation(obs["id"])
        # Observation must still exist
        assert db.observation_count() == 1
        remaining = db.list_observations()
        assert remaining[0]["id"] == obs["id"]
        # No audit trail entry — nothing was dismissed
        row = db.conn.execute("SELECT * FROM dismissed_observations WHERE obs_id = ?", (obs["id"],)).fetchone()
        assert row is None

    def test_promote_label_failure_still_creates_issue(self, db: FiligreeDB) -> None:
        """Enrichment failures are best-effort: issue is still returned even if
        add_label raises."""
        from unittest.mock import patch

        count_before = db.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
        obs = db.create_observation("will partially fail")
        with patch.object(db, "add_label", side_effect=sqlite3.OperationalError("label boom")):
            result = db.promote_observation(obs["id"])
        # Issue was created and returned successfully
        assert result["issue"] is not None
        assert db.observation_count() == 0
        count_after = db.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
        assert count_after == count_before + 1

    def test_promote_label_failure_returns_warnings(self, db: FiligreeDB) -> None:
        """Enrichment failures surface in warnings list."""
        from unittest.mock import patch

        obs = db.create_observation("will warn")
        with patch.object(db, "add_label", side_effect=sqlite3.OperationalError("label boom")):
            result = db.promote_observation(obs["id"])
        assert "warnings" in result
        assert any("label" in w for w in result["warnings"])

    def test_promote_cleanup_failure_returns_warnings(self, db: FiligreeDB) -> None:
        """If observation delete fails, warning is surfaced."""
        import sqlite3

        obs = db.create_observation("cleanup will fail")
        real_conn = db._conn

        def _fail_on_obs_delete(real: Any, sql: str, params: Any = ()) -> Any:
            if "DELETE FROM observations WHERE id" in sql and "expires_at" not in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return real.execute(sql, params)

        db._conn = _InterceptingConn(real_conn, _fail_on_obs_delete)  # type: ignore[assignment]
        try:
            result = db.promote_observation(obs["id"])
        finally:
            db._conn = real_conn
        assert result["issue"] is not None
        assert "warnings" in result
        assert any("delete observation" in w.lower() for w in result["warnings"])

    def test_promote_audit_trail_failure_returns_warnings(self, db: FiligreeDB) -> None:
        """If audit trail insert fails but observation delete succeeds, warning is surfaced."""
        import sqlite3

        obs = db.create_observation("audit trail will fail")
        real_conn = db._conn

        def _fail_on_dismissed_insert(real: Any, sql: str, params: Any = ()) -> Any:
            if "INSERT INTO dismissed_observations" in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return real.execute(sql, params)

        db._conn = _InterceptingConn(real_conn, _fail_on_dismissed_insert)  # type: ignore[assignment]
        try:
            result = db.promote_observation(obs["id"])
        finally:
            db._conn = real_conn
        assert result["issue"] is not None
        # Observation should be deleted (preventing double-promotion)
        assert db.observation_count() == 0
        assert "warnings" in result
        assert any("audit trail" in w.lower() for w in result["warnings"])

    def test_promote_cleanup_failure_prevents_double_promotion(self, db: FiligreeDB) -> None:
        """If observation delete fails, a second promote attempt must NOT create a duplicate issue.

        Idempotency is provided by fields.source_observation_id on the created issue.
        The second promote finds the existing issue and returns it, cleaning up the
        lingering observation as a best-effort side effect.
        """
        import sqlite3

        obs = db.create_observation("will survive first promote")
        real_conn = db._conn

        def _fail_on_obs_delete(real: Any, sql: str, params: Any = ()) -> Any:
            if "DELETE FROM observations WHERE id" in sql and "expires_at" not in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return real.execute(sql, params)

        # First promote — delete fails, issue is created, observation lingers
        issues_before = db.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
        db._conn = _InterceptingConn(real_conn, _fail_on_obs_delete)  # type: ignore[assignment]
        try:
            result1 = db.promote_observation(obs["id"])
        finally:
            db._conn = real_conn
        first_issue = result1["issue"]
        assert first_issue is not None
        # Observation still exists because delete failed
        assert db.observation_count() == 1
        # Exactly one new issue
        issues_after_first = db.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
        assert issues_after_first == issues_before + 1

        # Second promote — should return the SAME issue (idempotent), not create a new one
        result2 = db.promote_observation(obs["id"])
        assert result2["issue"].id == first_issue.id, "Retry must return the same issue, not a duplicate"
        # Still exactly one issue from this promotion
        issues_after_second = db.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
        assert issues_after_second == issues_before + 1
        # Observation has been cleaned up by the idempotent retry
        assert db.observation_count() == 0
        # Warning surfaced to caller
        assert "warnings" in result2
        assert any("already promoted" in w for w in result2["warnings"])

        # Third promote — source_observation_id lookup still finds the issue,
        # so it remains idempotent (returns the same issue).
        result3 = db.promote_observation(obs["id"])
        assert result3["issue"].id == first_issue.id

    def test_promote_idempotent_retry_records_missing_audit_trail(self, db: FiligreeDB) -> None:
        """If first cleanup and audit both fail, retry must preserve the dismissal audit trail."""
        obs = db.create_observation("audit retry after cleanup failure")
        real_conn = db._conn

        def _fail_cleanup_and_audit(real: Any, sql: str, params: Any = ()) -> Any:
            if "DELETE FROM observations WHERE id" in sql and "expires_at" not in sql:
                raise sqlite3.OperationalError("delete failed")
            if "INSERT INTO dismissed_observations" in sql:
                raise sqlite3.OperationalError("audit failed")
            return real.execute(sql, params)

        db._conn = _InterceptingConn(real_conn, _fail_cleanup_and_audit)  # type: ignore[assignment]
        try:
            first = db.promote_observation(obs["id"])
        finally:
            db._conn = real_conn

        assert "warnings" in first
        assert db.observation_count() == 1
        assert db.conn.execute("SELECT COUNT(*) FROM dismissed_observations WHERE obs_id = ?", (obs["id"],)).fetchone()[0] == 0

        retry = db.promote_observation(obs["id"])

        assert retry["issue"].id == first["issue"].id
        assert db.observation_count() == 0
        audit = db.conn.execute("SELECT reason FROM dismissed_observations WHERE obs_id = ?", (obs["id"],)).fetchone()
        assert audit is not None
        assert audit["reason"] == "promoted"

    def test_promote_warns_when_observation_already_swept(self, db: FiligreeDB) -> None:
        """If a concurrent sweep deletes the observation between SELECT and DELETE,
        promote succeeds but returns a warning about the race."""
        obs = db.create_observation("will be swept mid-promote")
        real_conn = db._conn

        def _sweep_on_delete(real: Any, sql: str, params: Any = ()) -> Any:
            """Simulate concurrent sweep: delete the obs right before the cleanup DELETE."""
            if "DELETE FROM observations WHERE id" in sql and "expires_at" not in sql:
                # Simulate the sweep deleting this row first
                real.execute("DELETE FROM observations WHERE id = ?", (obs["id"],))
            return real.execute(sql, params)

        db._conn = _InterceptingConn(real_conn, _sweep_on_delete)  # type: ignore[assignment]
        try:
            result = db.promote_observation(obs["id"])
        finally:
            db._conn = real_conn
        # Issue was created
        assert result["issue"] is not None
        # Warning about the race
        assert "warnings" in result
        assert any("already swept" in w for w in result["warnings"])

    def test_promote_no_warnings_on_success(self, db: FiligreeDB) -> None:
        """No warnings key when everything succeeds."""
        obs = db.create_observation("all good")
        result = db.promote_observation(obs["id"])
        assert "warnings" not in result

    def test_link_observation_to_issue_preserves_evidence_and_clears_queue(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Existing tracked work")
        obs = db.create_observation(
            "Same root cause",
            detail="The duplicate evidence lives here",
            file_path="src/api.py",
            line=12,
            priority=1,
            actor="reviewer-a",
        )

        link = db.link_observation_to_issue(
            obs["id"],
            issue.id,
            disposition="duplicate",
            reason="covered by existing issue",
            actor="triager",
        )

        assert link["observation_id"] == obs["id"]
        assert link["issue_id"] == issue.id
        assert link["disposition"] == "duplicate"
        assert db.list_observations() == []
        stored = db.conn.execute("SELECT * FROM observation_links WHERE obs_id = ?", (obs["id"],)).fetchone()
        assert stored is not None
        assert stored["summary"] == "Same root cause"
        assert stored["detail"] == "The duplicate evidence lives here"
        assert stored["file_path"] == "src/api.py"
        assert stored["line"] == 12
        audit = db.conn.execute("SELECT * FROM dismissed_observations WHERE obs_id = ?", (obs["id"],)).fetchone()
        assert audit is not None
        assert audit["reason"] == f"linked:duplicate:{issue.id}: covered by existing issue"

    @pytest.mark.parametrize("field", ["actor", "reason"])
    def test_link_observation_rejects_non_string_audit_fields(self, db: FiligreeDB, field: str) -> None:
        issue = db.create_issue("Existing tracked work")
        obs = db.create_observation("Attach me")

        with pytest.raises(ValueError, match=field):
            db.link_observation_to_issue(obs["id"], issue.id, **{field: 123})  # type: ignore[arg-type]

    def test_batch_link_observations_reports_per_item_failures(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Existing tracked work")
        obs = db.create_observation("Attach me")

        linked, failed = db.batch_link_observations_to_issue(
            [obs["id"], "obs-missing"],
            issue.id,
            disposition="evidence",
            actor="triager",
        )

        assert [item["observation_id"] for item in linked] == [obs["id"]]
        assert len(failed) == 1
        assert failed[0]["id"] == "obs-missing"
        assert failed[0]["code"] == "NOT_FOUND"

    def test_promote_many_observations_to_one_issue_preserves_all_source_ids(self, db: FiligreeDB) -> None:
        obs1 = db.create_observation("First signal", detail="one", priority=3)
        obs2 = db.create_observation("Second signal", detail="two", file_path="src/core.py", line=8, priority=1)

        result = db.promote_observations_to_issue(
            [obs1["id"], obs2["id"]],
            issue_type="bug",
            title="Merged observation issue",
            actor="triager",
        )

        issue = result["issue"]
        assert issue.title == "Merged observation issue"
        assert issue.type == "bug"
        assert issue.priority == 1
        assert issue.fields["source_observation_ids"] == [obs1["id"], obs2["id"]]
        assert "First signal" in issue.description
        assert "Second signal" in issue.description
        assert "src/core.py:8" in issue.description
        assert db.list_observations() == []
        linked = db.conn.execute("SELECT obs_id FROM observation_links WHERE issue_id = ? ORDER BY id", (issue.id,)).fetchall()
        assert [row["obs_id"] for row in linked] == [obs1["id"], obs2["id"]]

    def test_promote_many_cleanup_failure_prevents_duplicate_issue(
        self, db: FiligreeDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        obs1 = db.create_observation("First retry signal", detail="one", priority=3)
        obs2 = db.create_observation("Second retry signal", detail="two", priority=1)
        real_link = db.link_observation_to_issue

        def fail_link_cleanup(*args: Any, **kwargs: Any) -> Any:
            raise sqlite3.OperationalError("disk I/O error")

        issues_before = db.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
        monkeypatch.setattr(db, "link_observation_to_issue", fail_link_cleanup)
        result1 = db.promote_observations_to_issue(
            [obs1["id"], obs2["id"]],
            title="Merged retry issue",
            actor="triager",
        )
        first_issue = result1["issue"]
        assert "warnings" in result1
        assert db.observation_count() == 2
        assert db.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == issues_before + 1

        monkeypatch.setattr(db, "link_observation_to_issue", real_link)
        result2 = db.promote_observations_to_issue(
            [obs1["id"], obs2["id"]],
            title="Merged retry issue",
            actor="triager",
        )

        assert result2["issue"].id == first_issue.id
        assert db.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == issues_before + 1
        assert db.observation_count() == 0
        assert "warnings" in result2
        assert any("already promoted" in warning for warning in result2["warnings"])

    @pytest.mark.parametrize("field", ["title", "extra_description", "actor"])
    def test_promote_many_rejects_non_string_text_fields(self, db: FiligreeDB, field: str) -> None:
        obs = db.create_observation("bad promote input")
        with pytest.raises(ValueError, match=field):
            db.promote_observations_to_issue([obs["id"]], **{field: 123})  # type: ignore[arg-type]

    def test_promote_many_rejects_non_list_labels(self, db: FiligreeDB) -> None:
        obs = db.create_observation("bad promote labels")
        with pytest.raises(TypeError, match="labels must be a list of strings"):
            db.promote_observations_to_issue([obs["id"]], labels="cluster:test")  # type: ignore[arg-type]

    def test_promote_many_rejects_non_string_label_items(self, db: FiligreeDB) -> None:
        obs = db.create_observation("bad promote labels")
        with pytest.raises(TypeError, match="labels must be a list of strings"):
            db.promote_observations_to_issue([obs["id"]], labels=["cluster:test", 123])  # type: ignore[list-item]

    def test_promote_expired_observation_raises(self, db: FiligreeDB) -> None:
        """Promoting an expired observation should fail, not create a stale issue."""
        obs = db.create_observation("stale finding")
        db.conn.execute(
            "UPDATE observations SET expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (obs["id"],),
        )
        db.conn.commit()
        with pytest.raises(ValueError, match="expired"):
            db.promote_observation(obs["id"])

    def test_concurrent_promote_creates_only_one_issue(self, db: FiligreeDB) -> None:
        """Two threads racing to promote the same observation must produce one issue.

        Regression for filigree-58aa8fb4ac (TOCTOU at db_observations.py:413-416):
        the idempotency SELECT + create_issue must be serialized via BEGIN
        IMMEDIATE so concurrent callers can't both pass the check.
        """
        import threading

        obs = db.create_observation("racey finding")
        obs_id = obs["id"]
        db_path = db.db_path
        # Release fixture's connection so peer connections can take the writer lock.
        db.close()

        peer_a = FiligreeDB(db_path, prefix="test", check_same_thread=False)
        peer_b = FiligreeDB(db_path, prefix="test", check_same_thread=False)
        try:
            barrier = threading.Barrier(2)
            results: list[str] = []
            errors: list[BaseException] = []

            def run(peer: FiligreeDB) -> None:
                try:
                    barrier.wait()
                    r = peer.promote_observation(obs_id)
                    results.append(r["issue"].id)
                except BaseException as e:
                    errors.append(e)

            t1 = threading.Thread(target=run, args=(peer_a,))
            t2 = threading.Thread(target=run, args=(peer_b,))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            assert errors == [], f"Unexpected errors: {errors}"
            assert len(results) == 2
            assert results[0] == results[1], f"Both threads must converge on the same issue id; got {results}"

            audit = FiligreeDB(db_path, prefix="test", check_same_thread=False)
            try:
                rows = audit.conn.execute(
                    "SELECT id FROM issues WHERE json_extract(fields, '$.source_observation_id') = ?",
                    (obs_id,),
                ).fetchall()
            finally:
                audit.close()
            assert len(rows) == 1, f"Expected exactly one issue per observation, got {[r['id'] for r in rows]}"
        finally:
            peer_a.close()
            peer_b.close()

    def test_promote_tolerates_corrupt_fields_on_unrelated_issue(self, db: FiligreeDB) -> None:
        """Corrupt fields JSON on an unrelated issue must not block promotion.

        Regression for filigree-9bb842088d: the idempotency lookup uses
        json_extract over the full issues table, which raises OperationalError
        on malformed rows. The query must guard with json_valid(fields) so
        corrupt rows are skipped (matches the _safe_fields_json convention).
        """
        bystander = db.create_issue("bystander")
        db.conn.execute("UPDATE issues SET fields = '{bad json' WHERE id = ?", (bystander.id,))
        db.conn.commit()

        obs = db.create_observation("valid promotion path")
        result = db.promote_observation(obs["id"])
        assert result["issue"].id != bystander.id
        # Issue's own source_observation_id round-trips intact
        assert result["issue"].fields.get("source_observation_id") == obs["id"]


class TestObservationStats:
    def test_count_empty(self, db: FiligreeDB) -> None:
        assert db.observation_count() == 0

    def test_count_matches(self, db: FiligreeDB) -> None:
        db.create_observation("One")
        db.create_observation("Two")
        assert db.observation_count() == 2

    def test_observation_age_stats(self, db: FiligreeDB) -> None:
        db.create_observation("Fresh")
        stats = db.observation_stats()
        assert stats["count"] == 1
        assert stats["stale_count"] == 0
        assert stats["oldest_hours"] >= 0

    def test_stale_detection(self, db: FiligreeDB) -> None:
        obs = db.create_observation("Old one")
        # Backdate to 3 days ago
        db.conn.execute(
            "UPDATE observations SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (obs["id"],),
        )
        db.conn.commit()
        stats = db.observation_stats()
        assert stats["stale_count"] == 1

    def test_stats_corrupt_created_at_returns_none_oldest_hours(self, db: FiligreeDB) -> None:
        """Corrupt created_at returns oldest_hours=None (unknown), not 0.0."""
        obs = db.create_observation("has corrupt timestamp")
        db.conn.execute(
            "UPDATE observations SET created_at = 'not-a-date' WHERE id = ?",
            (obs["id"],),
        )
        db.conn.commit()
        stats = db.observation_stats()
        assert stats["oldest_hours"] is None
        assert stats["count"] == 1


class TestObservationStatsNoSweep:
    """Verify observation_stats(sweep=False) excludes expired rows via WHERE filter."""

    def test_stats_sweep_false_excludes_expired(self, db: FiligreeDB) -> None:
        db.create_observation("active obs")
        expired = db.create_observation("expired obs", file_path="src/old.py")
        # Backdate expiry to the past
        db.conn.execute(
            "UPDATE observations SET expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (expired["id"],),
        )
        db.conn.commit()

        stats = db.observation_stats(sweep=False)
        assert stats["count"] == 1, "sweep=False should exclude expired rows"
        # Verify the expired row is still in the database (no sweep happened)
        raw_count = db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        assert raw_count == 2, "Expired row should still exist in DB"

    def test_stats_sweep_false_stale_excludes_expired(self, db: FiligreeDB) -> None:
        """Stale count should also exclude expired rows when sweep=False."""
        stale = db.create_observation("stale but alive")
        expired_stale = db.create_observation("stale and expired", file_path="src/x.py")
        # Backdate both to 3 days ago (stale), but only expire one
        db.conn.execute(
            "UPDATE observations SET created_at = '2020-01-01T00:00:00+00:00' WHERE id IN (?, ?)",
            (stale["id"], expired_stale["id"]),
        )
        db.conn.execute(
            "UPDATE observations SET expires_at = '2020-01-02T00:00:00+00:00' WHERE id = ?",
            (expired_stale["id"],),
        )
        db.conn.commit()

        stats = db.observation_stats(sweep=False)
        assert stats["stale_count"] == 1, "Only non-expired stale obs should be counted"

    def test_stats_sweep_false_empty_when_all_expired(self, db: FiligreeDB) -> None:
        obs = db.create_observation("will expire")
        db.conn.execute(
            "UPDATE observations SET expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (obs["id"],),
        )
        db.conn.commit()

        stats = db.observation_stats(sweep=False)
        assert stats["count"] == 0
        assert stats["stale_count"] == 0
        assert stats["expiring_soon_count"] == 0


class TestObservationCountDocumentation:
    """Verify that observation_count() does NOT sweep (known asymmetry with list_observations)."""

    def test_count_includes_expired(self, db: FiligreeDB) -> None:
        obs = db.create_observation("Will expire")
        db.conn.execute(
            "UPDATE observations SET expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (obs["id"],),
        )
        db.conn.commit()
        # count() returns 1 (no sweep), but list_observations() returns 0 (sweeps)
        assert db.observation_count() == 1
        assert db.list_observations() == []
        assert db.observation_count() == 0  # Sweep has now run


class TestFileDetailObservations:
    """Verify get_file_detail() includes observation_count."""

    def test_file_detail_no_observations(self, db: FiligreeDB) -> None:
        fr = db.register_file("src/clean.py")
        detail = db.get_file_detail(fr.id)
        assert detail["observation_count"] == 0

    def test_file_detail_with_observations(self, db: FiligreeDB) -> None:
        db.create_observation("bug 1", file_path="src/buggy.py")
        db.create_observation("bug 2", file_path="src/buggy.py")
        db.create_observation("unrelated", file_path="src/other.py")
        obs = db.list_observations(file_path="src/buggy.py")
        file_id = obs[0]["file_id"]
        detail = db.get_file_detail(file_id)
        assert detail["observation_count"] == 2

    def test_list_files_excludes_expired_observations(self, db: FiligreeDB) -> None:
        """list_files_paginated should exclude expired observations from the count."""
        db.create_observation("active obs", file_path="src/listed.py")
        obs2 = db.create_observation("will expire", file_path="src/listed.py", line=1)
        db.conn.execute(
            "UPDATE observations SET expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (obs2["id"],),
        )
        db.conn.commit()
        result = db.list_files_paginated(path_prefix="src/listed")
        assert len(result["results"]) == 1
        assert result["results"][0]["observation_count"] == 1

    def test_file_detail_excludes_expired_observations(self, db: FiligreeDB) -> None:
        """get_file_detail should exclude expired observations from the count."""
        db.create_observation("active obs", file_path="src/temp.py")
        obs2 = db.create_observation("will expire", file_path="src/temp.py", line=1)
        db.conn.execute(
            "UPDATE observations SET expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (obs2["id"],),
        )
        db.conn.commit()
        detail = db.get_file_detail(obs2["file_id"])
        # Expired observations should not be counted
        assert detail["observation_count"] == 1


# ── M13: observation_stats sweep=False oldest_hours ────────────────────


class TestObservationStatsNoSweepOldest:
    """Verify sweep=False correctly filters the oldest_hours query."""

    def test_oldest_hours_excludes_expired(self, db: FiligreeDB) -> None:
        """oldest_hours should reflect only non-expired observations when sweep=False."""
        # Create a very old observation that is expired
        old_expired = db.create_observation("ancient expired")
        db.conn.execute(
            "UPDATE observations SET created_at = '2020-01-01T00:00:00+00:00', expires_at = '2020-02-01T00:00:00+00:00' WHERE id = ?",
            (old_expired["id"],),
        )
        # Create a recent observation that is alive
        db.create_observation("recent alive")
        db.conn.commit()

        stats = db.observation_stats(sweep=False)
        assert stats["count"] == 1
        # oldest_hours should reflect the recent obs, not the ancient expired one
        assert stats["oldest_hours"] < 24  # created just now

    def test_expiring_soon_excludes_already_expired(self, db: FiligreeDB) -> None:
        """expiring_soon_count should not include already-expired observations."""
        from datetime import UTC, datetime, timedelta

        # Create an obs that expires in 12 hours (expiring soon, alive)
        soon = db.create_observation("expires soon")
        soon_expiry = (datetime.now(UTC) + timedelta(hours=12)).isoformat()
        db.conn.execute(
            "UPDATE observations SET expires_at = ? WHERE id = ?",
            (soon_expiry, soon["id"]),
        )
        # Create an obs that already expired (should NOT count)
        expired = db.create_observation("already expired")
        db.conn.execute(
            "UPDATE observations SET expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (expired["id"],),
        )
        db.conn.commit()

        stats = db.observation_stats(sweep=False)
        assert stats["expiring_soon_count"] == 1  # only the soon-expiring one


# ── promote_observation title/priority overrides ───────────────────────


class TestPromoteObservationOverrides:
    """filigree-ae4d760c75: promote_observation title and priority override params."""

    def test_custom_title_overrides_summary(self, db: FiligreeDB) -> None:
        obs = db.create_observation("Original summary", priority=2)
        result = db.promote_observation(obs["id"], title="Custom title")
        assert result["issue"].title == "Custom title"

    def test_custom_priority_overrides_observation_priority(self, db: FiligreeDB) -> None:
        obs = db.create_observation("Something", priority=3)
        result = db.promote_observation(obs["id"], priority=0)
        assert result["issue"].priority == 0

    def test_empty_string_title_falls_back_to_summary(self, db: FiligreeDB) -> None:
        obs = db.create_observation("Fallback summary", priority=2)
        result = db.promote_observation(obs["id"], title="")
        assert result["issue"].title == "Fallback summary"


# ── promote_observation file_association failure path ──────────────────


class TestPromoteFileAssociationFailure:
    """filigree-671ce16125: file_association failure during promote."""

    def test_file_association_failure_still_creates_issue(self, db: FiligreeDB) -> None:
        """If add_file_association raises, the issue is still created and returned."""
        from unittest.mock import patch

        count_before = db.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
        obs = db.create_observation("bug with file", file_path="src/buggy.py")
        with patch.object(db, "add_file_association", side_effect=sqlite3.OperationalError("assoc boom")):
            result = db.promote_observation(obs["id"])
        assert result["issue"] is not None
        assert db.observation_count() == 0
        count_after = db.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
        assert count_after == count_before + 1

    def test_file_association_failure_returns_warning(self, db: FiligreeDB) -> None:
        """File association failure surfaces in warnings list."""
        from unittest.mock import patch

        obs = db.create_observation("bug with file", file_path="src/warn.py")
        with patch.object(db, "add_file_association", side_effect=sqlite3.OperationalError("assoc boom")):
            result = db.promote_observation(obs["id"])
        assert "warnings" in result
        assert any("file association" in w.lower() for w in result["warnings"])


# ── JSONL export/import roundtrip for observations ────────────────────


class TestObservationJsonlRoundtrip:
    """filigree-770b3f3f75: JSONL roundtrip preserves observation data."""

    def test_roundtrip_preserves_observations(self, db: FiligreeDB, tmp_path: Any) -> None:
        """Create observations + dismiss one, export, reimport into fresh DB, verify."""
        db.create_observation("Active obs", priority=1, file_path="src/a.py", line=10)
        obs2 = db.create_observation("Will dismiss", priority=2)
        db.create_observation("Another active", detail="Some detail", priority=0)
        db.dismiss_observation(obs2["id"], reason="not relevant", actor="tester")

        out = tmp_path / "obs-roundtrip.jsonl"
        export_count = db.export_jsonl(out)
        assert export_count > 0

        fresh = FiligreeDB(tmp_path / "fresh-obs.db", prefix="test")
        fresh.initialize()
        result = fresh.import_jsonl(out, merge=True)
        assert result["count"] > 0

        # Active observations should be present
        imported_obs = fresh.list_observations()
        summaries = {o["summary"] for o in imported_obs}
        assert "Active obs" in summaries
        assert "Another active" in summaries
        assert "Will dismiss" not in summaries  # was dismissed

        # Verify dismissed_observations audit trail was imported
        dismissed = fresh.conn.execute("SELECT * FROM dismissed_observations WHERE obs_id = ?", (obs2["id"],)).fetchone()
        assert dismissed is not None
        assert dismissed["reason"] == "not relevant"

        # Verify field integrity on imported observation
        active = next(o for o in imported_obs if o["summary"] == "Active obs")
        assert active["priority"] == 1
        assert active["file_path"] == "src/a.py"
        assert active["line"] == 10

        fresh.close()

    def test_import_missing_expires_at_defaults_to_future_not_now(self, db: FiligreeDB, tmp_path: Any) -> None:
        """filigree-13fccfce44: imported observations without expires_at must not be instantly expired.

        Regression: missing expires_at used to default to _now_iso(), which made
        every such observation sweep-eligible on the next read.
        """
        import json

        out = tmp_path / "no-expires.jsonl"
        # Hand-craft a JSONL file with an observation that lacks expires_at
        record = {
            "_type": "observation",
            "id": "test-obs-noexp",
            "summary": "legacy import",
            "created_at": _now_iso(),
        }
        with out.open("w") as f:
            f.write(json.dumps(record) + "\n")

        fresh = FiligreeDB(tmp_path / "fresh-noexp.db", prefix="test")
        fresh.initialize()
        result = fresh.import_jsonl(out, merge=False)
        assert result["count"] >= 1

        # Row must be present with a future expires_at
        row = fresh.conn.execute("SELECT id, expires_at FROM observations WHERE id = 'test-obs-noexp'").fetchone()
        assert row is not None, "observation should have been imported"
        assert row["expires_at"] > _now_iso(), "expires_at should default to future, not now"

        # And list_observations should return it (not sweep it)
        assert any(o["id"] == "test-obs-noexp" for o in fresh.list_observations())
        fresh.close()

    def test_merge_import_dismissed_observations_idempotent(self, db: FiligreeDB, tmp_path: Any) -> None:
        """Importing the same JSONL twice with merge=True must not duplicate dismissed_observations rows."""
        obs = db.create_observation("Will dismiss for merge test")
        db.dismiss_observation(obs["id"], reason="testing merge", actor="tester")

        out = tmp_path / "merge-dismissed.jsonl"
        db.export_jsonl(out)

        fresh = FiligreeDB(tmp_path / "merge-dismissed.db", prefix="test")
        fresh.initialize()

        # Import once
        fresh.import_jsonl(out, merge=True)
        count1 = fresh.conn.execute("SELECT COUNT(*) FROM dismissed_observations").fetchone()[0]
        assert count1 >= 1

        # Import again — should NOT duplicate dismissed_observation rows
        fresh.import_jsonl(out, merge=True)
        count2 = fresh.conn.execute("SELECT COUNT(*) FROM dismissed_observations").fetchone()[0]
        assert count2 == count1, f"Expected {count1} dismissed rows after re-import, got {count2} (duplicated)"

        fresh.close()


class TestAuditTrailCap:
    """Verify dismissed_observations is pruned to DISMISSED_AUDIT_TRAIL_CAP during sweep."""

    def test_sweep_prunes_audit_trail_to_cap(self, db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        """When dismissed_observations exceeds the cap, sweep keeps the most recent entries."""
        import filigree.db_observations as obs_mod

        cap = 5
        monkeypatch.setattr(obs_mod, "DISMISSED_AUDIT_TRAIL_CAP", cap)
        overflow = 3
        now = datetime.now(UTC)

        for i in range(cap + overflow):
            ts = (now - timedelta(hours=cap + overflow - i)).isoformat()
            db.conn.execute(
                "INSERT INTO dismissed_observations (obs_id, summary, actor, reason, dismissed_at) VALUES (?, ?, 'test', 'bulk', ?)",
                (f"bulk-obs-{i}", f"Obs {i}", ts),
            )
        db.conn.commit()

        row = db.conn.execute("SELECT COUNT(*) FROM dismissed_observations").fetchone()
        assert row[0] == cap + overflow

        # Create and expire an observation to trigger sweep
        obs = db.create_observation("trigger sweep")
        db.conn.execute("UPDATE observations SET expires_at = ? WHERE id = ?", ("2000-01-01T00:00:00", obs["id"]))
        db.conn.commit()

        db._sweep_expired_observations()

        row = db.conn.execute("SELECT COUNT(*) FROM dismissed_observations").fetchone()
        assert row[0] <= cap

        # Verify the most recent entries were kept (not the oldest)
        oldest = db.conn.execute("SELECT dismissed_at FROM dismissed_observations ORDER BY dismissed_at ASC LIMIT 1").fetchone()
        newest = db.conn.execute("SELECT dismissed_at FROM dismissed_observations ORDER BY dismissed_at DESC LIMIT 1").fetchone()
        assert oldest is not None
        assert newest is not None
        # The oldest surviving entry should be newer than the very first entry we inserted
        first_ts = (now - timedelta(hours=cap + overflow)).isoformat()
        assert oldest[0] > first_ts


def _shared_db_path(tmp_path: Path) -> Path:
    # Initialize the schema once via an isolated connection, then hand the
    # path to worker threads. Each worker opens its own FiligreeDB instance
    # — no shared connection state.
    seed = make_db(tmp_path)
    path = seed.db_path
    seed.close()
    return path


def _run_one_create_race(db_path: Path, n_threads: int) -> tuple[list[str], list[Exception]]:
    import threading

    from filigree.core import FiligreeDB

    wipe = FiligreeDB(db_path, prefix="test")
    wipe.initialize()
    wipe.conn.execute("DELETE FROM observations")
    wipe.conn.commit()
    wipe.close()

    errors: list[Exception] = []
    ids: list[str] = []
    barrier = threading.Barrier(n_threads)

    def worker() -> None:
        d: FiligreeDB | None = None
        try:
            d = FiligreeDB(db_path, prefix="test")
            d.initialize()
            barrier.wait(timeout=5)
            obs = d.create_observation("dedup-race")
            ids.append(obs["id"])
        except Exception as e:
            errors.append(e)
        finally:
            if d is not None:
                d.close()

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return ids, errors


def _run_one_dismiss_race(db_path: Path, n_threads: int) -> tuple[str, list[str], list[Exception]]:
    import threading

    from filigree.core import FiligreeDB

    seed = FiligreeDB(db_path, prefix="test")
    seed.initialize()
    seed.conn.execute("DELETE FROM observations")
    seed.conn.execute("DELETE FROM dismissed_observations")
    seed.conn.commit()
    obs = seed.create_observation("dismiss-race")
    obs_id = obs["id"]
    seed.close()

    successes: list[str] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(n_threads)

    def worker(name: str) -> None:
        d: FiligreeDB | None = None
        try:
            d = FiligreeDB(db_path, prefix="test")
            d.initialize()
            barrier.wait(timeout=5)
            d.dismiss_observation(obs_id, actor=name)
            successes.append(name)
        except ValueError:
            # Expected: races that lose see "Observation not found".
            pass
        except Exception as e:
            errors.append(e)
        finally:
            if d is not None:
                d.close()

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return obs_id, successes, errors


class TestObservationConcurrencyRegressions:
    """Regression tests for filigree-248ca4ff3f and filigree-fd77d71a49.

    Concurrency races are inherently flaky — each scenario is run in a loop
    so a regression that flips the outcome 10% of the time still trips here.
    Threads use separate FiligreeDB instances over the same on-disk DB to
    exercise the WAL-mode path (mirrors MCP/CLI behavior).
    """

    def test_concurrent_create_returns_existing_under_dedup_race(self, tmp_path: Path) -> None:
        db_path = _shared_db_path(tmp_path)
        n_threads = 8
        for _ in range(20):
            ids, errors = _run_one_create_race(db_path, n_threads)
            assert errors == [], f"unexpected errors: {errors!r}"
            assert len(ids) == n_threads
            assert len(set(ids)) == 1, f"racers should share one id, got {set(ids)!r}"

    def test_concurrent_dismiss_writes_one_audit_row(self, tmp_path: Path) -> None:
        from filigree.core import FiligreeDB

        db_path = _shared_db_path(tmp_path)
        n_threads = 5
        for _ in range(20):
            obs_id, successes, errors = _run_one_dismiss_race(db_path, n_threads)
            assert errors == [], f"unexpected errors: {errors!r}"
            assert len(successes) == 1, f"exactly one dismiss should win, got {successes!r}"

            check = FiligreeDB(db_path, prefix="test")
            check.initialize()
            audit_count = check.conn.execute("SELECT COUNT(*) FROM dismissed_observations WHERE obs_id = ?", (obs_id,)).fetchone()[0]
            remaining = check.conn.execute("SELECT COUNT(*) FROM observations WHERE id = ?", (obs_id,)).fetchone()[0]
            check.close()
            assert audit_count == 1, f"expected 1 audit row, got {audit_count}"
            assert remaining == 0
