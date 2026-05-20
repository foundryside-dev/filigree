"""Tests for ScansMixin — scan run lifecycle tracking."""

from __future__ import annotations

from pathlib import Path

import pytest

from filigree.core import FiligreeDB


class TestCreateScanRun:
    def test_create_returns_dict(self, db: FiligreeDB) -> None:
        run = db.create_scan_run(
            scan_run_id="test-run-1",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["src/main.py"],
            file_ids=["f-1"],
        )
        assert run["id"] == "test-run-1"
        assert run["scanner_name"] == "codex"
        assert run["status"] == "pending"
        assert run["file_paths"] == ["src/main.py"]

    def test_create_duplicate_raises(self, db: FiligreeDB) -> None:
        db.create_scan_run(
            scan_run_id="test-run-1",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["src/main.py"],
            file_ids=["f-1"],
        )
        with pytest.raises(ValueError, match="already exists"):
            db.create_scan_run(
                scan_run_id="test-run-1",
                scanner_name="codex",
                scan_source="codex",
                file_paths=["src/main.py"],
                file_ids=["f-1"],
            )

    def test_create_rejects_log_path_outside_project(self, db: FiligreeDB, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="log_path must be project-relative"):
            db.create_scan_run(
                scan_run_id="test-run-1",
                scanner_name="codex",
                scan_source="codex",
                file_paths=["src/main.py"],
                file_ids=["f-1"],
                log_path=str(tmp_path / "outside.log"),
            )


class TestUpdateScanRunStatus:
    def test_transition_pending_to_running(self, db: FiligreeDB) -> None:
        db.create_scan_run(
            scan_run_id="run-1",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["src/main.py"],
            file_ids=["f-1"],
            pid=1234,
        )
        db.update_scan_run_status("run-1", "running")
        run = db.get_scan_run("run-1")
        assert run["status"] == "running"

    def test_transition_running_to_completed(self, db: FiligreeDB) -> None:
        db.create_scan_run(
            scan_run_id="run-1",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["src/main.py"],
            file_ids=["f-1"],
        )
        db.update_scan_run_status("run-1", "running")
        db.update_scan_run_status("run-1", "completed", exit_code=0, findings_count=5)
        run = db.get_scan_run("run-1")
        assert run["status"] == "completed"
        assert run["exit_code"] == 0
        assert run["findings_count"] == 5
        assert run["completed_at"] is not None

    def test_transition_running_to_failed(self, db: FiligreeDB) -> None:
        db.create_scan_run(
            scan_run_id="run-1",
            scanner_name="codex",
            scan_source="codex",
            file_paths=[],
            file_ids=[],
        )
        db.update_scan_run_status("run-1", "running")
        db.update_scan_run_status("run-1", "failed", error_message="crash")
        run = db.get_scan_run("run-1")
        assert run["status"] == "failed"
        assert run["error_message"] == "crash"

    def test_invalid_transition_raises(self, db: FiligreeDB) -> None:
        db.create_scan_run(
            scan_run_id="run-1",
            scanner_name="codex",
            scan_source="codex",
            file_paths=[],
            file_ids=[],
        )
        with pytest.raises(ValueError, match="Invalid transition"):
            db.update_scan_run_status("run-1", "completed")

    def test_not_found_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(KeyError):
            db.update_scan_run_status("no-such-run", "running")


class TestGetScanRun:
    def test_get_returns_dict(self, db: FiligreeDB) -> None:
        db.create_scan_run(
            scan_run_id="run-1",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["a.py", "b.py"],
            file_ids=["f-1", "f-2"],
        )
        run = db.get_scan_run("run-1")
        assert run["id"] == "run-1"
        assert run["file_paths"] == ["a.py", "b.py"]

    def test_get_not_found_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(KeyError):
            db.get_scan_run("no-such-run")


class TestCooldownCheck:
    def test_no_recent_run_allows_trigger(self, db: FiligreeDB) -> None:
        assert db.check_scan_cooldown("codex", "src/main.py") is None

    def test_running_scan_blocks(self, db: FiligreeDB) -> None:
        db.create_scan_run(
            scan_run_id="run-1",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["src/main.py"],
            file_ids=["f-1"],
        )
        db.update_scan_run_status("run-1", "running")
        result = db.check_scan_cooldown("codex", "src/main.py")
        assert result is not None  # returns blocking run info

    def test_failed_scan_does_not_block(self, db: FiligreeDB) -> None:
        db.create_scan_run(
            scan_run_id="run-1",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["src/main.py"],
            file_ids=["f-1"],
        )
        db.update_scan_run_status("run-1", "running")
        db.update_scan_run_status("run-1", "failed")
        assert db.check_scan_cooldown("codex", "src/main.py") is None

    def test_reserve_rejects_caller_transaction_without_rollback(self, db: FiligreeDB) -> None:
        """reserve_scan_run must not silently roll back unrelated caller writes."""
        db.conn.execute(
            "INSERT INTO file_records (id, path, first_seen, updated_at) VALUES (?, ?, ?, ?)",
            ("pending-file", "src/pending.py", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )

        with pytest.raises(RuntimeError, match="nested transaction not supported"):
            db.reserve_scan_run(
                scan_run_id="nested-run",
                scanner_name="codex",
                scan_source="codex",
                file_path="src/main.py",
                file_id="f-1",
            )

        assert db.conn.in_transaction
        assert db.conn.execute("SELECT id FROM file_records WHERE id = 'pending-file'").fetchone() is not None
        db.conn.rollback()
        assert db.conn.execute("SELECT id FROM file_records WHERE id = 'pending-file'").fetchone() is None
        with pytest.raises(KeyError):
            db.get_scan_run("nested-run")


class TestGetScanStatus:
    def test_returns_status_with_process_info(self, db: FiligreeDB) -> None:
        db.create_scan_run(
            scan_run_id="run-1",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["src/main.py"],
            file_ids=["f-1"],
        )
        status = db.get_scan_status("run-1")
        assert status["id"] == "run-1"
        assert status["process_alive"] is False
        assert isinstance(status["log_tail"], list)

    def test_not_found_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(KeyError):
            db.get_scan_status("no-such-run")

    def test_batch_scan_warns_about_partial_pid_monitoring(self, db: FiligreeDB) -> None:
        """Batch scans (multiple file_paths) include a data_warnings note."""
        db.create_scan_run(
            scan_run_id="batch-1",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["a.py", "b.py", "c.py"],
            file_ids=["f-1", "f-2", "f-3"],
            pid=99999,
        )
        status = db.get_scan_status("batch-1")
        assert any("remaining 2 file(s)" in w for w in status["data_warnings"])

    def test_log_tail_resolves_from_project_root(self, tmp_path: Path) -> None:
        """Log paths resolve against ``project_root`` — not the DB's parent.parent.

        Regression for filigree-daefeda81d: custom ``.filigree.conf`` DB
        locations made ``db_path.parent.parent`` land on the wrong directory,
        so ``log_tail`` was always empty for those installs.
        """
        # Mimic a v2.0 install that put the DB off to the side.
        project_root = tmp_path / "proj"
        project_root.mkdir()
        db_dir = tmp_path / "storage" / "db"
        db_dir.mkdir(parents=True)
        scan_log = project_root / ".filigree" / "scans" / "my-run.log"
        scan_log.parent.mkdir(parents=True)
        scan_log.write_text("line one\nline two\nline three\n")

        d = FiligreeDB(db_dir / "track.db", prefix="test", project_root=project_root)
        d.initialize()
        try:
            d.create_scan_run(
                scan_run_id="my-run",
                scanner_name="codex",
                scan_source="codex",
                file_paths=["a.py"],
                file_ids=["f-1"],
                log_path=".filigree/scans/my-run.log",
            )
            status = d.get_scan_status("my-run")
        finally:
            d.close()

        assert status["log_tail"] == ["line one", "line two", "line three"]

    @pytest.mark.parametrize("stored_path", ["absolute", "traversal"])
    def test_invalid_persisted_log_path_is_not_read(self, tmp_path: Path, stored_path: str) -> None:
        project_root = tmp_path / "proj"
        project_root.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("do not leak\n")
        log_path = str(secret) if stored_path == "absolute" else "../secret.txt"
        db_dir = tmp_path / "storage" / "db"
        db_dir.mkdir(parents=True)
        d = FiligreeDB(db_dir / "track.db", prefix="test", project_root=project_root)
        d.initialize()
        try:
            d.create_scan_run(
                scan_run_id="bad-log",
                scanner_name="codex",
                scan_source="codex",
                file_paths=["a.py"],
                file_ids=["f-1"],
            )
            d.conn.execute("UPDATE scan_runs SET log_path = ? WHERE id = ?", (log_path, "bad-log"))
            d.conn.commit()

            status = d.get_scan_status("bad-log")
        finally:
            d.close()

        assert status["log_tail"] == []
        assert any("log_path" in warning and "outside project root" in warning for warning in status["data_warnings"])

    def test_log_tail_streams_without_reading_full_file(self, db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        project_root = db.project_root if db.project_root is not None else db.db_path.parent.parent
        log_path = project_root / "scan.log"
        log_path.write_text("\n".join(f"line {i}" for i in range(10)) + "\n")
        db.create_scan_run(
            scan_run_id="stream-log",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["src/main.py"],
            file_ids=["f-1"],
            log_path="scan.log",
        )

        def fail_read_text(self: Path, *args: object, **kwargs: object) -> str:
            if self == log_path:
                raise AssertionError("get_scan_status must stream log tails instead of read_text()")
            return original_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

        original_read_text = Path.read_text
        monkeypatch.setattr(Path, "read_text", fail_read_text)

        status = db.get_scan_status("stream-log", log_lines=3)

        assert status["log_tail"] == ["line 7", "line 8", "line 9"]

    def test_set_spawn_info_rejects_log_path_outside_project(self, db: FiligreeDB, tmp_path: Path) -> None:
        db.create_scan_run(
            scan_run_id="spawn-log",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["src/main.py"],
            file_ids=["f-1"],
        )

        with pytest.raises(ValueError, match="log_path must be project-relative"):
            db.set_scan_run_spawn_info("spawn-log", pid=123, log_path=str(tmp_path / "outside.log"))

    def test_dead_pid_already_completed_race(self, db: FiligreeDB) -> None:
        """When another codepath completes the run before dead-PID auto-fail, re-read succeeds."""
        from unittest.mock import patch

        db.create_scan_run(
            scan_run_id="race-1",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["a.py"],
            file_ids=["f-1"],
            pid=99999,
        )
        db.update_scan_run_status("race-1", "running")
        # Simulate: another codepath already completed the run
        db.update_scan_run_status("race-1", "completed", findings_count=3)
        # os.kill will raise ProcessLookupError (PID doesn't exist),
        # then auto-fail will fail with "Invalid transition" since it's already completed.
        # The method should re-read and return the completed status.
        with patch("os.kill", side_effect=ProcessLookupError):
            status = db.get_scan_status("race-1")
        assert status["status"] == "completed"
        assert status["process_alive"] is False


class TestCorruptScanRunJson:
    """Corrupt JSON in scan_runs is handled gracefully with data_warnings."""

    def test_corrupt_file_paths_returns_empty_with_warning(self, db: FiligreeDB) -> None:
        db.create_scan_run(
            scan_run_id="run-corrupt",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["a.py"],
            file_ids=["f-1"],
        )
        # Corrupt the file_paths JSON directly
        db.conn.execute(
            "UPDATE scan_runs SET file_paths = 'not-valid-json' WHERE id = ?",
            ("run-corrupt",),
        )
        db.conn.commit()
        run = db.get_scan_run("run-corrupt")
        assert run["file_paths"] == []
        assert any("file_paths" in w for w in run["data_warnings"])

    def test_corrupt_file_ids_returns_empty_with_warning(self, db: FiligreeDB) -> None:
        db.create_scan_run(
            scan_run_id="run-corrupt-ids",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["a.py"],
            file_ids=["f-1"],
        )
        db.conn.execute(
            "UPDATE scan_runs SET file_ids = '{broken' WHERE id = ?",
            ("run-corrupt-ids",),
        )
        db.conn.commit()
        run = db.get_scan_run("run-corrupt-ids")
        assert run["file_ids"] == []
        assert any("file_ids" in w for w in run["data_warnings"])

    def test_valid_json_has_no_warnings(self, db: FiligreeDB) -> None:
        db.create_scan_run(
            scan_run_id="run-ok",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["a.py"],
            file_ids=["f-1"],
        )
        run = db.get_scan_run("run-ok")
        assert run["data_warnings"] == []


class TestNonListJsonInScanRun:
    """Valid JSON that is not a list should be treated as corrupt."""

    def test_dict_json_returns_empty_with_warning(self, db: FiligreeDB) -> None:
        db.create_scan_run(
            scan_run_id="run-dict",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["a.py"],
            file_ids=["f-1"],
        )
        db.conn.execute(
            'UPDATE scan_runs SET file_paths = \'{"not": "a list"}\' WHERE id = ?',
            ("run-dict",),
        )
        db.conn.commit()
        run = db.get_scan_run("run-dict")
        assert run["file_paths"] == []
        assert any("expected list" in w for w in run["data_warnings"])

    def test_int_json_returns_empty_with_warning(self, db: FiligreeDB) -> None:
        db.create_scan_run(
            scan_run_id="run-int",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["a.py"],
            file_ids=["f-1"],
        )
        db.conn.execute(
            "UPDATE scan_runs SET file_ids = '42' WHERE id = ?",
            ("run-int",),
        )
        db.conn.commit()
        run = db.get_scan_run("run-int")
        assert run["file_ids"] == []
        assert any("expected list" in w for w in run["data_warnings"])


class TestScanRunTimeout:
    """The running -> timeout transition is valid."""

    def test_transition_running_to_timeout(self, db: FiligreeDB) -> None:
        db.create_scan_run(
            scan_run_id="run-t",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["x.py"],
            file_ids=["f-1"],
        )
        db.update_scan_run_status("run-t", "running")
        db.update_scan_run_status("run-t", "timeout", error_message="Exceeded 300s limit")
        run = db.get_scan_run("run-t")
        assert run["status"] == "timeout"
        assert run["completed_at"] is not None
        assert run["error_message"] == "Exceeded 300s limit"


class TestCooldownMultiFile:
    """json_each cooldown correctly matches individual files in arrays."""

    def test_cooldown_matches_specific_file_in_batch(self, db: FiligreeDB) -> None:
        db.create_scan_run(
            scan_run_id="batch-cd",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["src/main.py", "src/other.py", "src/utils.py"],
            file_ids=["f-1", "f-2", "f-3"],
        )
        db.update_scan_run_status("batch-cd", "running")
        # Should block for a file that's in the array
        assert db.check_scan_cooldown("codex", "src/other.py") is not None
        # Should not block for a file not in the array
        assert db.check_scan_cooldown("codex", "src/different.py") is None

    def test_cooldown_no_prefix_false_positive(self, db: FiligreeDB) -> None:
        """json_each matches exactly, not as prefix like LIKE would."""
        db.create_scan_run(
            scan_run_id="batch-prefix",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["src/main.py"],
            file_ids=["f-1"],
        )
        db.update_scan_run_status("batch-prefix", "running")
        # "src/main.py.bak" should NOT match "src/main.py"
        assert db.check_scan_cooldown("codex", "src/main.py.bak") is None


class TestCooldownTimestampFormat:
    """Regression: cooldown comparison must use ISO format matching _now_iso()."""

    def test_cooldown_expires_after_threshold(self, db: FiligreeDB) -> None:
        """A completed scan should stop blocking after SCAN_COOLDOWN_SECONDS.

        Before the fix, _now_iso()'s 'T' separator sorted after SQLite
        datetime()'s space separator, making every same-day scan appear
        newer than the cutoff — permanent rate-limiting until date change.
        """
        from filigree.db_scans import SCAN_COOLDOWN_SECONDS

        db.create_scan_run(
            scan_run_id="ts-fmt-1",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["src/main.py"],
            file_ids=["f-1"],
        )
        db.update_scan_run_status("ts-fmt-1", "running")
        db.update_scan_run_status("ts-fmt-1", "completed")

        # Immediately after completion, cooldown should block
        assert db.check_scan_cooldown("codex", "src/main.py") is not None

        # Backdate updated_at to exceed cooldown
        db.conn.execute(
            "UPDATE scan_runs SET updated_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', ?) WHERE id = ?",
            (f"-{SCAN_COOLDOWN_SECONDS + 5} seconds", "ts-fmt-1"),
        )
        db.conn.commit()

        # After cooldown expires, trigger should be allowed
        assert db.check_scan_cooldown("codex", "src/main.py") is None


class TestCooldownActiveRunsBlockRegardlessOfAge:
    """Regression for filigree-254e1299a3: pending/running rows are a singleton
    lock, not a debounce — they must block duplicate triggers regardless of how
    long the run has been active. Only `completed` rows use the timestamp cutoff.
    """

    def test_running_scan_blocks_after_cooldown_window(self, db: FiligreeDB) -> None:
        """A scanner running longer than SCAN_COOLDOWN_SECONDS must still block
        a duplicate trigger — the cooldown is only relevant for the debounce
        window after a `completed` run, not for active singletons."""
        from filigree.db_scans import SCAN_COOLDOWN_SECONDS

        db.create_scan_run(
            scan_run_id="long-running",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["src/main.py"],
            file_ids=["f-1"],
        )
        db.update_scan_run_status("long-running", "running")
        # Backdate updated_at past the cutoff to simulate a long-running scanner.
        db.conn.execute(
            "UPDATE scan_runs SET updated_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', ?) WHERE id = ?",
            (f"-{SCAN_COOLDOWN_SECONDS + 5} seconds", "long-running"),
        )
        db.conn.commit()
        # Active run must still block, even though its updated_at is past cutoff.
        blocking = db.check_scan_cooldown("codex", "src/main.py")
        assert blocking is not None
        assert blocking["id"] == "long-running"

    def test_pending_scan_blocks_after_cooldown_window(self, db: FiligreeDB) -> None:
        """Same invariant for a `pending` reservation that hasn't transitioned yet."""
        from filigree.db_scans import SCAN_COOLDOWN_SECONDS

        db.create_scan_run(
            scan_run_id="long-pending",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["src/main.py"],
            file_ids=["f-1"],
        )
        # Stays in 'pending' state (initial status from create_scan_run).
        db.conn.execute(
            "UPDATE scan_runs SET updated_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', ?) WHERE id = ?",
            (f"-{SCAN_COOLDOWN_SECONDS + 5} seconds", "long-pending"),
        )
        db.conn.commit()
        blocking = db.check_scan_cooldown("codex", "src/main.py")
        assert blocking is not None
        assert blocking["id"] == "long-pending"

    def test_completed_scan_still_obeys_timestamp_cutoff(self, db: FiligreeDB) -> None:
        """Sanity check: the fix to active runs must not break the debounce
        window for completed runs."""
        from filigree.db_scans import SCAN_COOLDOWN_SECONDS

        db.create_scan_run(
            scan_run_id="old-completed",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["src/main.py"],
            file_ids=["f-1"],
        )
        db.update_scan_run_status("old-completed", "running")
        db.update_scan_run_status("old-completed", "completed")
        db.conn.execute(
            "UPDATE scan_runs SET updated_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', ?) WHERE id = ?",
            (f"-{SCAN_COOLDOWN_SECONDS + 5} seconds", "old-completed"),
        )
        db.conn.commit()
        # Completed + past cutoff = no block (debounce expired).
        assert db.check_scan_cooldown("codex", "src/main.py") is None


class TestUpdateScanRunStatusCompareAndSwap:
    """Regression for filigree-c835e730fb: status transitions must guard against
    stale reads. Two writers that both observe `running` must not both succeed
    in transitioning to a terminal state — the second one must raise rather
    than silently overwrite."""

    def test_concurrent_transition_does_not_overwrite_terminal_state(self, db: FiligreeDB) -> None:
        """Simulate a TOCTOU race: caller reads `running`, validates, but
        before its UPDATE commits, another writer transitions the row to
        `failed`. The CAS UPDATE must match zero rows and raise, leaving
        the prior terminal state intact."""
        db.create_scan_run(
            scan_run_id="cas-test",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["src/main.py"],
            file_ids=["f-1"],
        )
        db.update_scan_run_status("cas-test", "running")

        # Inject a stale snapshot: the first call inside update_scan_run_status
        # returns 'running', but the row is concurrently mutated to 'failed'
        # before the UPDATE statement runs.
        real_get = db.get_scan_run
        injected = [False]

        def stale_then_real(run_id: str):
            if not injected[0]:
                injected[0] = True
                # Simulate the "other writer" committing a terminal state
                # between our read and our update.
                db.conn.execute(
                    "UPDATE scan_runs SET status = 'failed', "
                    "completed_at = '2026-05-03T10:00:00+00:00', "
                    "error_message = 'died' WHERE id = ?",
                    (run_id,),
                )
                db.conn.commit()
                # Return the pre-mutation snapshot the caller would have seen.
                snapshot = real_get(run_id)
                snapshot["status"] = "running"
                return snapshot
            return real_get(run_id)

        db.get_scan_run = stale_then_real  # type: ignore[method-assign]
        try:
            with pytest.raises(ValueError, match=r"stale|concurrent|changed"):
                db.update_scan_run_status("cas-test", "completed", findings_count=10)
        finally:
            db.get_scan_run = real_get  # type: ignore[method-assign]

        # The terminal state set by the "other writer" must survive.
        final = real_get("cas-test")
        assert final["status"] == "failed"
        assert final["error_message"] == "died"


class TestProcessScanResultsCompleteScanRun:
    """Regression: complete_scan_run=False prevents premature batch completion."""

    def test_complete_scan_run_false_does_not_transition(self, db: FiligreeDB) -> None:
        db.create_scan_run(
            scan_run_id="batch-no-complete",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["a.py", "b.py"],
            file_ids=["f-1", "f-2"],
        )
        db.update_scan_run_status("batch-no-complete", "running")

        # Ingest findings for first file but don't complete
        db.process_scan_results(
            scan_source="codex",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "medium", "message": "test"}],
            scan_run_id="batch-no-complete",
            complete_scan_run=False,
        )
        run = db.get_scan_run("batch-no-complete")
        assert run["status"] == "running"  # NOT completed

    def test_complete_scan_run_true_transitions(self, db: FiligreeDB) -> None:
        db.create_scan_run(
            scan_run_id="batch-complete",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["a.py"],
            file_ids=["f-1"],
        )
        db.update_scan_run_status("batch-complete", "running")

        # Ingest with default (complete_scan_run=True)
        db.process_scan_results(
            scan_source="codex",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "medium", "message": "test"}],
            scan_run_id="batch-complete",
        )
        run = db.get_scan_run("batch-complete")
        assert run["status"] == "completed"

    def test_empty_findings_with_complete_marks_done(self, db: FiligreeDB) -> None:
        """Clean scans (zero findings) can still complete the run."""
        db.create_scan_run(
            scan_run_id="clean-run",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["a.py"],
            file_ids=["f-1"],
        )
        db.update_scan_run_status("clean-run", "running")

        db.process_scan_results(
            scan_source="codex",
            findings=[],
            scan_run_id="clean-run",
            complete_scan_run=True,
        )
        run = db.get_scan_run("clean-run")
        assert run["status"] == "completed"

    def test_completion_race_already_failed_warns(self, db: FiligreeDB) -> None:
        """When scan run is already failed (e.g. dead PID), completion logs info, not crash."""
        db.create_scan_run(
            scan_run_id="race-complete",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["a.py"],
            file_ids=["f-1"],
        )
        db.update_scan_run_status("race-complete", "running")
        db.update_scan_run_status("race-complete", "failed", error_message="PID died")

        # Ingest findings — completion will fail (failed→completed invalid)
        # but findings should still be ingested and a warning surfaced
        result = db.process_scan_results(
            scan_source="codex",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "medium", "message": "test"}],
            scan_run_id="race-complete",
        )
        assert result["findings_created"] == 1
        assert any("race-complete" in w for w in result["warnings"])
        # Run should still be failed
        run = db.get_scan_run("race-complete")
        assert run["status"] == "failed"


class TestCreateObservationsIntegration:
    """process_scan_results with create_observations=True creates observations."""

    def test_observations_created_for_new_findings(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py")
        result = db.process_scan_results(
            scan_source="test-scanner",
            findings=[
                {"path": "src/main.py", "rule_id": "R1", "severity": "high", "message": "Bug found"},
                {"path": "src/main.py", "rule_id": "R2", "severity": "low", "message": "Style issue"},
            ],
            create_observations=True,
        )
        assert result["observations_created"] == 2
        obs_list = db.list_observations()
        assert len(obs_list) == 2
        summaries = {o["summary"] for o in obs_list}
        assert any("Bug found" in s for s in summaries)
        assert any("Style issue" in s for s in summaries)

    def test_observations_not_created_when_false(self, db: FiligreeDB) -> None:
        db.register_file("src/main.py")
        result = db.process_scan_results(
            scan_source="test-scanner",
            findings=[{"path": "src/main.py", "rule_id": "R1", "severity": "medium", "message": "test"}],
            create_observations=False,
        )
        assert result["observations_created"] == 0
        assert len(db.list_observations()) == 0

    def test_findings_survive_observation_failure(self, db: FiligreeDB) -> None:
        """If observation creation fails, the finding itself is still ingested."""
        db.register_file("src/main.py")
        # First call creates both finding and observation
        db.process_scan_results(
            scan_source="test-scanner",
            findings=[{"path": "src/main.py", "rule_id": "R1", "severity": "medium", "message": "dup test"}],
            create_observations=True,
        )
        # Second call with same finding — finding is updated, observation deduped (not an error)
        result = db.process_scan_results(
            scan_source="test-scanner",
            findings=[{"path": "src/main.py", "rule_id": "R1", "severity": "medium", "message": "dup test"}],
            create_observations=True,
        )
        # Finding should be updated regardless
        assert result["findings_updated"] == 1


class TestObservationFailureWarning:
    """Observation creation failures are surfaced in stats warnings."""

    def test_observation_failure_adds_warning(self, db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        db.register_file("src/main.py")

        def failing_create_observation(*args: object, **kwargs: object) -> None:
            raise ValueError("forced observation failure")

        monkeypatch.setattr(db, "create_observation", failing_create_observation)
        result = db.process_scan_results(
            scan_source="test-scanner",
            findings=[
                {"path": "src/main.py", "rule_id": "R1", "severity": "medium", "message": "msg1"},
                {"path": "src/main.py", "rule_id": "R2", "severity": "high", "message": "msg2"},
            ],
            create_observations=True,
        )
        assert result["observations_created"] == 0
        assert result["observations_failed"] == 2
        assert result["findings_created"] == 2
        # Each distinct failure gets its own warning message
        obs_warnings = [w for w in result["warnings"] if "Observation failed" in w]
        assert len(obs_warnings) == 2


class TestScanIngestClusterRegressions:
    """Regressions for the scan ingest correctness cluster (f71df69ee2, 784b698cf4, 23c676fd5a)."""

    def test_mark_unseen_with_empty_findings_is_rejected(self, db: FiligreeDB) -> None:
        """mark_unseen=True + empty findings is ambiguous and must be rejected (f71df69ee2)."""
        # Seed a prior finding that should NOT be silently left open.
        db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "medium", "message": "m"}],
        )
        with pytest.raises(ValueError, match="mark_unseen"):
            db.process_scan_results(
                scan_source="ruff",
                findings=[],
                mark_unseen=True,
            )

    def test_mark_unseen_false_still_allows_empty_findings(self, db: FiligreeDB) -> None:
        """Empty findings without mark_unseen remains valid (clean scan)."""
        result = db.process_scan_results(scan_source="ruff", findings=[], mark_unseen=False)
        assert result["findings_created"] == 0

    def test_batch_completion_counts_all_findings_not_last_delta(self, db: FiligreeDB) -> None:
        """findings_count on completion reflects all findings for the run, not the final call's delta (784b698cf4)."""
        db.create_scan_run(
            scan_run_id="batch-count",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["a.py", "b.py"],
            file_ids=["f-1", "f-2"],
        )
        db.update_scan_run_status("batch-count", "running")

        db.process_scan_results(
            scan_source="codex",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "medium", "message": "m1"}],
            scan_run_id="batch-count",
            complete_scan_run=False,
        )
        db.process_scan_results(
            scan_source="codex",
            findings=[{"path": "b.py", "rule_id": "R2", "severity": "medium", "message": "m2"}],
            scan_run_id="batch-count",
            complete_scan_run=False,
        )
        # Final orchestrator call has no new findings but must complete the run
        db.process_scan_results(
            scan_source="codex",
            findings=[],
            scan_run_id="batch-count",
            complete_scan_run=True,
        )
        run = db.get_scan_run("batch-count")
        assert run["status"] == "completed"
        assert run["findings_count"] == 2, f"expected 2 total findings, got {run['findings_count']}"

    def test_multiple_findings_for_new_file_count_file_created_once(self, db: FiligreeDB) -> None:
        """File-level scan ingest stats count unique files, not findings."""
        result = db.process_scan_results(
            scan_source="codex",
            findings=[
                {"path": "a.py", "rule_id": "R1", "severity": "medium", "message": "m1"},
                {"path": "a.py", "rule_id": "R2", "severity": "high", "message": "m2"},
            ],
        )

        assert result["files_created"] == 1
        assert result["files_updated"] == 0
        assert result["findings_created"] == 2

    def test_cooldown_check_tolerates_malformed_file_paths(self, db: FiligreeDB) -> None:
        """One corrupt scan_runs.file_paths row must not block cooldown checks for other files (23c676fd5a)."""
        db.create_scan_run(
            scan_run_id="corrupt-cd",
            scanner_name="codex",
            scan_source="codex",
            file_paths=["x.py"],
            file_ids=["f-1"],
        )
        db.conn.execute(
            "UPDATE scan_runs SET file_paths = 'not-valid-json' WHERE id = ?",
            ("corrupt-cd",),
        )
        db.conn.commit()
        # Must not raise OperationalError; corrupt row is simply skipped
        assert db.check_scan_cooldown("codex", "anything.py") is None


class TestObservationAutoCommit:
    """Regression: create_observation with auto_commit=False defers commit."""

    def test_auto_commit_false_does_not_commit(self, db: FiligreeDB) -> None:
        """Observations created with auto_commit=False can be rolled back."""
        db.create_observation(
            "test obs",
            file_path="src/main.py",
            auto_commit=False,
        )
        # Roll back — observation should disappear
        db.conn.rollback()
        obs_list = db.list_observations()
        assert len(obs_list) == 0

    def test_auto_commit_true_persists(self, db: FiligreeDB) -> None:
        """Default auto_commit=True commits immediately."""
        db.create_observation(
            "test obs persisted",
            file_path="src/main.py",
        )
        # Even after rollback attempt, observation persists (already committed)
        db.conn.rollback()
        obs_list = db.list_observations()
        assert len(obs_list) == 1
