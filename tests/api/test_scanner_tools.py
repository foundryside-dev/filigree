"""Tests for new scanner MCP tools — get_scan_status, preview_scan, trigger_scan_batch."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest

from filigree.core import DB_FILENAME, FILIGREE_DIR_NAME, SUMMARY_FILENAME, VALID_SEVERITIES, FiligreeDB, write_config
from filigree.db_scans import SCAN_COOLDOWN_SECONDS
from filigree.mcp_server import call_tool  # type: ignore[attr-defined]
from filigree.types.api import ErrorCode
from tests.mcp._helpers import _parse

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_run(
    db: FiligreeDB,
    *,
    run_id: str = "run-1",
    scanner: str = "codex",
    file_paths: list[str] | None = None,
    file_ids: list[str] | None = None,
    pid: int | None = None,
    log_path: str = "",
) -> None:
    db.create_scan_run(
        scan_run_id=run_id,
        scanner_name=scanner,
        scan_source=scanner,
        file_paths=file_paths or ["src/main.py"],
        file_ids=file_ids or ["f-1"],
        pid=pid,
        log_path=log_path,
    )


# ---------------------------------------------------------------------------
# TestGetScanStatus (original 2 methods + extensions)
# ---------------------------------------------------------------------------


class TestGetScanStatus:
    def test_returns_status_with_process_info(self, db: FiligreeDB) -> None:
        _create_run(db)
        status = db.get_scan_status("run-1")
        assert status["id"] == "run-1"
        assert status["process_alive"] is False
        assert isinstance(status["log_tail"], list)

    def test_not_found_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(KeyError):
            db.get_scan_status("no-such-run")

    def test_pending_run_has_no_process_alive(self, db: FiligreeDB) -> None:
        _create_run(db, pid=99999)
        # Status is 'pending', so process_alive check is skipped regardless of PID
        status = db.get_scan_status("run-1")
        assert status["status"] == "pending"
        assert status["process_alive"] is False

    def test_running_run_with_dead_pid_auto_fails(self, db: FiligreeDB) -> None:
        # Use a PID extremely unlikely to exist
        _create_run(db, pid=9999991)
        db.update_scan_run_status("run-1", "running")
        status = db.get_scan_status("run-1", reconcile=True)
        # ProcessLookupError → auto-transition to failed
        assert status["status"] == "failed"
        assert status["process_alive"] is False
        assert "died" in (status["error_message"] or "")

    def test_log_tail_reads_from_log_file(self, tmp_path: Path) -> None:
        # Use a packs-based DB so db_path is .filigree/filigree.db and
        # db_path.parent.parent (project root) == tmp_path — a stable anchor
        # for constructing log-path fixtures.
        from tests._db_factory import make_db

        project_db = make_db(tmp_path, packs=["core"])
        try:
            log_dir = tmp_path / ".filigree" / "scans"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "run-log.log"
            log_file.write_text("line1\nline2\nline3\n")

            # log_path is stored relative to project root (tmp_path)
            log_rel = str(log_file.relative_to(tmp_path))
            _create_run(project_db, log_path=log_rel)

            status = project_db.get_scan_status("run-1")
            assert status["log_tail"] == ["line1", "line2", "line3"]
        finally:
            project_db.close()

    def test_log_tail_truncated_to_log_lines(self, tmp_path: Path) -> None:
        from tests._db_factory import make_db

        project_db = make_db(tmp_path, packs=["core"])
        try:
            log_dir = tmp_path / ".filigree" / "scans"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "run-many.log"
            log_file.write_text("\n".join(f"line{i}" for i in range(100)))

            log_rel = str(log_file.relative_to(tmp_path))
            _create_run(project_db, log_path=log_rel)

            status = project_db.get_scan_status("run-1", log_lines=5)
            assert len(status["log_tail"]) == 5
            # Should be the last 5 lines
            assert status["log_tail"][0] == "line95"
        finally:
            project_db.close()

    def test_missing_log_file_returns_empty_tail(self, db: FiligreeDB) -> None:
        _create_run(db, log_path=".filigree/scans/nonexistent.log")
        status = db.get_scan_status("run-1")
        assert status["log_tail"] == []

    def test_batch_run_data_warning_included(self, db: FiligreeDB) -> None:
        _create_run(
            db,
            file_paths=["src/a.py", "src/b.py"],
            file_ids=["f-1", "f-2"],
        )
        status = db.get_scan_status("run-1")
        assert any("Batch scan" in w for w in status["data_warnings"])

    def test_completed_run_has_no_process_alive(self, db: FiligreeDB) -> None:
        _create_run(db)
        db.update_scan_run_status("run-1", "running")
        db.update_scan_run_status("run-1", "completed")
        status = db.get_scan_status("run-1")
        assert status["status"] == "completed"
        assert status["process_alive"] is False


# ---------------------------------------------------------------------------
# TestCreateScanRun
# ---------------------------------------------------------------------------


class TestCreateScanRun:
    def test_creates_pending_run(self, db: FiligreeDB) -> None:
        run = db.create_scan_run(
            scan_run_id="run-2",
            scanner_name="mysc",
            scan_source="mysc",
            file_paths=["a.py"],
            file_ids=["fid-a"],
        )
        assert run["id"] == "run-2"
        assert run["status"] == "pending"
        assert run["scanner_name"] == "mysc"

    def test_duplicate_id_raises_value_error(self, db: FiligreeDB) -> None:
        _create_run(db)
        with pytest.raises(ValueError, match="already exists"):
            _create_run(db)

    def test_multiple_file_paths_stored(self, db: FiligreeDB) -> None:
        run = db.create_scan_run(
            scan_run_id="batch-run",
            scanner_name="sc",
            scan_source="sc",
            file_paths=["src/a.py", "src/b.py", "src/c.py"],
            file_ids=["f1", "f2", "f3"],
        )
        assert run["file_paths"] == ["src/a.py", "src/b.py", "src/c.py"]
        assert run["file_ids"] == ["f1", "f2", "f3"]

    def test_optional_fields_stored(self, db: FiligreeDB) -> None:
        run = db.create_scan_run(
            scan_run_id="run-opts",
            scanner_name="sc",
            scan_source="sc",
            file_paths=["a.py"],
            file_ids=["f1"],
            pid=1234,
            api_url="http://localhost:8377",
            log_path=".filigree/scans/run-opts.log",
        )
        assert run["pid"] == 1234
        assert run["api_url"] == "http://localhost:8377"
        assert run["log_path"] == ".filigree/scans/run-opts.log"


# ---------------------------------------------------------------------------
# TestUpdateScanRunStatus
# ---------------------------------------------------------------------------


class TestUpdateScanRunStatus:
    def test_pending_to_running(self, db: FiligreeDB) -> None:
        _create_run(db)
        run = db.update_scan_run_status("run-1", "running")
        assert run["status"] == "running"

    def test_running_to_completed(self, db: FiligreeDB) -> None:
        _create_run(db)
        db.update_scan_run_status("run-1", "running")
        run = db.update_scan_run_status("run-1", "completed", findings_count=5)
        assert run["status"] == "completed"
        assert run["findings_count"] == 5
        assert run["completed_at"] is not None

    def test_running_to_failed_with_error(self, db: FiligreeDB) -> None:
        _create_run(db)
        db.update_scan_run_status("run-1", "running")
        run = db.update_scan_run_status(
            "run-1",
            "failed",
            exit_code=1,
            error_message="scanner crashed",
        )
        assert run["status"] == "failed"
        assert run["exit_code"] == 1
        assert run["error_message"] == "scanner crashed"

    def test_pending_to_failed_directly(self, db: FiligreeDB) -> None:
        _create_run(db)
        run = db.update_scan_run_status("run-1", "failed")
        assert run["status"] == "failed"

    def test_invalid_transition_raises(self, db: FiligreeDB) -> None:
        _create_run(db)
        db.update_scan_run_status("run-1", "running")
        db.update_scan_run_status("run-1", "completed")
        with pytest.raises(ValueError, match="Invalid transition"):
            db.update_scan_run_status("run-1", "running")

    def test_invalid_status_string_raises(self, db: FiligreeDB) -> None:
        _create_run(db)
        with pytest.raises(ValueError, match="Invalid scan run status"):
            db.update_scan_run_status("run-1", "unknown-status")  # type: ignore[arg-type]

    def test_missing_run_raises_key_error(self, db: FiligreeDB) -> None:
        with pytest.raises(KeyError):
            db.update_scan_run_status("no-such-run", "running")

    def test_running_to_timeout(self, db: FiligreeDB) -> None:
        _create_run(db)
        db.update_scan_run_status("run-1", "running")
        run = db.update_scan_run_status("run-1", "timeout")
        assert run["status"] == "timeout"
        assert run["completed_at"] is not None


# ---------------------------------------------------------------------------
# TestScanCooldown
# ---------------------------------------------------------------------------


class TestScanCooldown:
    def test_no_cooldown_when_no_prior_run(self, db: FiligreeDB) -> None:
        result = db.check_scan_cooldown("mysc", "src/main.py")
        assert result is None

    def test_cooldown_blocked_by_pending_run(self, db: FiligreeDB) -> None:
        _create_run(db)  # status=pending, just created → within cooldown window
        blocking = db.check_scan_cooldown("codex", "src/main.py")
        assert blocking is not None
        assert blocking["id"] == "run-1"

    def test_cooldown_blocked_by_running_run(self, db: FiligreeDB) -> None:
        _create_run(db)
        db.update_scan_run_status("run-1", "running")
        blocking = db.check_scan_cooldown("codex", "src/main.py")
        assert blocking is not None

    def test_cooldown_blocked_by_completed_run(self, db: FiligreeDB) -> None:
        _create_run(db)
        db.update_scan_run_status("run-1", "running")
        db.update_scan_run_status("run-1", "completed")
        blocking = db.check_scan_cooldown("codex", "src/main.py")
        assert blocking is not None

    def test_failed_run_does_not_block(self, db: FiligreeDB) -> None:
        _create_run(db)
        db.update_scan_run_status("run-1", "failed")
        result = db.check_scan_cooldown("codex", "src/main.py")
        assert result is None

    def test_different_scanner_does_not_block(self, db: FiligreeDB) -> None:
        _create_run(db, scanner="scanner-a")
        result = db.check_scan_cooldown("scanner-b", "src/main.py")
        assert result is None

    def test_different_file_does_not_block(self, db: FiligreeDB) -> None:
        _create_run(db, file_paths=["src/other.py"])
        result = db.check_scan_cooldown("codex", "src/main.py")
        assert result is None

    def test_cooldown_constant_is_30_seconds(self) -> None:
        assert SCAN_COOLDOWN_SECONDS == 30


# ---------------------------------------------------------------------------
# TestGetScanRun
# ---------------------------------------------------------------------------


class TestGetScanRun:
    def test_round_trips_all_fields(self, db: FiligreeDB) -> None:
        db.create_scan_run(
            scan_run_id="full-run",
            scanner_name="sc",
            scan_source="sc",
            file_paths=["x.py", "y.py"],
            file_ids=["f1", "f2"],
            pid=42,
            api_url="http://localhost:8377",
            log_path=".filigree/scans/full-run.log",
        )
        run = db.get_scan_run("full-run")
        assert run["id"] == "full-run"
        assert run["scanner_name"] == "sc"
        assert run["file_paths"] == ["x.py", "y.py"]
        assert run["file_ids"] == ["f1", "f2"]
        assert run["pid"] == 42
        assert run["api_url"] == "http://localhost:8377"
        assert run["status"] == "pending"
        assert run["exit_code"] is None
        assert run["findings_count"] == 0
        assert run["error_message"] == ""
        assert run["data_warnings"] == []

    def test_not_found_raises_key_error(self, db: FiligreeDB) -> None:
        with pytest.raises(KeyError):
            db.get_scan_run("missing")

    def test_empty_log_path_defaults_to_empty_string(self, db: FiligreeDB) -> None:
        _create_run(db)
        run = db.get_scan_run("run-1")
        assert run["log_path"] == ""

    def test_pid_none_stored_and_retrieved(self, db: FiligreeDB) -> None:
        _create_run(db, pid=None)
        run = db.get_scan_run("run-1")
        assert run["pid"] is None


# ---------------------------------------------------------------------------
# TestScanRunStatusEdgeCases
# ---------------------------------------------------------------------------


class TestScanRunStatusEdgeCases:
    def test_get_scan_status_returns_status_dict_keys(self, db: FiligreeDB) -> None:
        _create_run(db)
        status = db.get_scan_status("run-1")
        required_keys = {
            "id",
            "status",
            "scanner_name",
            "file_paths",
            "file_ids",
            "process_alive",
            "log_tail",
            "started_at",
            "updated_at",
        }
        for key in required_keys:
            assert key in status, f"Missing key: {key}"

    def test_empty_log_path_gives_empty_tail(self, db: FiligreeDB) -> None:
        _create_run(db, log_path="")
        status = db.get_scan_status("run-1")
        assert status["log_tail"] == []

    def test_single_file_no_batch_warning(self, db: FiligreeDB) -> None:
        _create_run(db, file_paths=["src/single.py"], file_ids=["f-1"])
        status = db.get_scan_status("run-1")
        assert not any("Batch scan" in w for w in status["data_warnings"])

    def test_status_reflects_failed_transition(self, db: FiligreeDB) -> None:
        _create_run(db)
        db.update_scan_run_status("run-1", "running")
        db.update_scan_run_status("run-1", "failed", exit_code=2, error_message="timeout")
        status = db.get_scan_status("run-1")
        assert status["status"] == "failed"
        assert status["exit_code"] == 2
        assert status["error_message"] == "timeout"

    def test_multiple_batch_files_status(self, db: FiligreeDB) -> None:
        paths = [f"src/file{i}.py" for i in range(5)]
        ids = [f"f-{i}" for i in range(5)]
        db.create_scan_run(
            scan_run_id="batch-5",
            scanner_name="sc",
            scan_source="sc",
            file_paths=paths,
            file_ids=ids,
        )
        status = db.get_scan_status("batch-5")
        assert status["file_paths"] == paths
        # Batch warning mentions remaining count
        warnings = status["data_warnings"]
        assert any("4" in w for w in warnings)


# ---------------------------------------------------------------------------
# Fixture: mcp_db_for_report_finding
# Mirrors tests/mcp/conftest.py::mcp_db — sets up FiligreeDB and patches
# the MCP module globals so that _get_db() returns the test DB.
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp_db_for_report_finding(tmp_path: Path) -> Generator[FiligreeDB, None, None]:
    """FiligreeDB wired into the MCP server for report_finding handler tests."""
    filigree_dir = tmp_path / FILIGREE_DIR_NAME
    filigree_dir.mkdir()
    write_config(filigree_dir, {"prefix": "mcp", "version": 1})
    (filigree_dir / SUMMARY_FILENAME).write_text("# test\n")

    d = FiligreeDB(filigree_dir / DB_FILENAME, prefix="mcp")
    d.initialize()

    import filigree.mcp_server as mcp_mod

    original_db = mcp_mod.db
    original_dir = mcp_mod._filigree_dir
    mcp_mod.db = d
    mcp_mod._filigree_dir = filigree_dir

    yield d

    mcp_mod.db = original_db
    mcp_mod._filigree_dir = original_dir
    d.close()


# ---------------------------------------------------------------------------
# TestReportFindingTool
# ---------------------------------------------------------------------------


class TestReportFindingTool:
    """Tests for the report_finding MCP tool handler."""

    async def test_happy_path_all_fields(self, mcp_db_for_report_finding: FiligreeDB) -> None:
        """All optional fields are accepted and the response is the created finding."""
        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "src/auth/login.py",
                    "rule_id": "sql-injection",
                    "message": "Unsanitized input passed to query",
                    "severity": "high",
                    "line_start": 42,
                    "line_end": 45,
                    "category": "security",
                    "response_detail": "full",
                },
            )
        )
        assert data["finding_result"] == "created"
        assert data["status"] == "open"
        assert data["findings_created"] == 1
        assert data["findings_updated"] == 0
        assert data["file_created"] is True
        assert "finding_id" in data
        assert "id" not in data
        assert data["rule_id"] == "sql-injection"
        assert data["line_start"] == 42

    async def test_happy_path_minimal_required_fields(self, mcp_db_for_report_finding: FiligreeDB) -> None:
        """Only required fields — severity defaults to info and returns the finding."""
        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "src/utils/format.py",
                    "rule_id": "unused-import",
                    "message": "Module imported but never used",
                    "response_detail": "full",
                },
            )
        )
        assert data["finding_result"] == "created"
        assert data["status"] == "open"
        assert data["findings_created"] == 1
        assert data["file_created"] is True
        assert "finding_id" in data
        assert data["severity"] == "info"

    async def test_invalid_severity_returns_validation_error(self, mcp_db_for_report_finding: FiligreeDB) -> None:
        """An unrecognised severity value is rejected before any DB writes."""
        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "src/main.py",
                    "rule_id": "some-rule",
                    "message": "A message",
                    "severity": "catastrophic",
                },
            )
        )
        assert data["code"] == ErrorCode.VALIDATION
        assert "catastrophic" in data["error"]

    async def test_non_string_severity_returns_validation_error(self, mcp_db_for_report_finding: FiligreeDB) -> None:
        """Unhashable JSON values must not escape as raw TypeError."""
        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "src/main.py",
                    "rule_id": "some-rule",
                    "message": "A message",
                    "severity": [],
                },
            )
        )
        assert data["code"] == ErrorCode.VALIDATION
        assert "severity" in data["error"]

    async def test_missing_file_path_returns_validation_error(self, mcp_db_for_report_finding: FiligreeDB) -> None:
        """Omitting file_path triggers the required-fields validation error."""
        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "",
                    "rule_id": "some-rule",
                    "message": "A message",
                },
            )
        )
        assert data["code"] == ErrorCode.VALIDATION
        assert "file_path" in data["error"]

    async def test_missing_rule_id_returns_validation_error(self, mcp_db_for_report_finding: FiligreeDB) -> None:
        """Omitting rule_id triggers the required-fields validation error."""
        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "src/main.py",
                    "rule_id": "",
                    "message": "A message",
                },
            )
        )
        assert data["code"] == ErrorCode.VALIDATION
        assert "rule_id" in data["error"]

    async def test_missing_message_returns_validation_error(self, mcp_db_for_report_finding: FiligreeDB) -> None:
        """Omitting message triggers the required-fields validation error."""
        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "src/main.py",
                    "rule_id": "some-rule",
                    "message": "",
                },
            )
        )
        assert data["code"] == ErrorCode.VALIDATION
        assert "message" in data["error"]

    @pytest.mark.parametrize("severity", sorted(VALID_SEVERITIES))
    async def test_all_valid_severities_accepted(self, mcp_db_for_report_finding: FiligreeDB, severity: str) -> None:
        """Every member of VALID_SEVERITIES creates a finding without error."""
        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": f"src/file_{severity}.py",
                    "rule_id": "test-rule",
                    "message": f"Finding with severity {severity}",
                    "severity": severity,
                    "response_detail": "full",
                },
            )
        )
        assert data["finding_result"] == "created"
        assert data["status"] == "open"
        assert data["findings_created"] == 1

    async def test_duplicate_finding_returns_updated_status(self, mcp_db_for_report_finding: FiligreeDB) -> None:
        """Reporting the same rule_id+file_path twice yields status='updated' on the second call."""
        args = {
            "file_path": "src/core.py",
            "rule_id": "duplicate-rule",
            "message": "First report",
            "response_detail": "full",
        }
        first = _parse(await call_tool("report_finding", args))
        assert first["finding_result"] == "created"

        second = _parse(await call_tool("report_finding", {**args, "message": "Second report"}))
        assert second["finding_result"] == "updated"
        assert second["status"] == "open"
        assert second["finding_id"] == first["finding_id"]
        assert second["findings_created"] == 0
        assert second["findings_updated"] == 1

    async def test_db_error_returns_ingestion_error(self, mcp_db_for_report_finding: FiligreeDB) -> None:
        """sqlite3.Error from process_scan_results is caught and returned as ingestion_error."""
        import sqlite3

        with patch.object(
            mcp_db_for_report_finding,
            "process_scan_results",
            side_effect=sqlite3.OperationalError("disk full"),
        ):
            data = _parse(
                await call_tool(
                    "report_finding",
                    {
                        "file_path": "src/main.py",
                        "rule_id": "test-rule",
                        "message": "A message",
                    },
                )
            )
        assert data["code"] == ErrorCode.IO
        assert "disk full" in data["error"]

    async def test_file_created_false_for_existing_file(self, mcp_db_for_report_finding: FiligreeDB) -> None:
        """file_created is False when the file was already registered before reporting."""
        mcp_db_for_report_finding.register_file("src/existing.py")
        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "src/existing.py",
                    "rule_id": "existing-rule",
                    "message": "Finding on pre-existing file",
                    "response_detail": "full",
                },
            )
        )
        assert data["file_created"] is False

    async def test_slim_default_drops_batch_stats(self, mcp_db_for_report_finding: FiligreeDB) -> None:
        """Default response_detail='slim' drops batch-ingest stats keys (F3 — review-h).

        Slim keeps the flat ScanFinding + finding_result; 'full' adds
        findings_created etc. Paired observations are explicit opt-in.
        """
        slim = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "src/slim.py",
                    "rule_id": "slim-rule",
                    "message": "Slim default response check",
                },
            )
        )
        assert slim["finding_result"] == "created"
        assert "finding_id" in slim
        assert "observation_id" not in slim
        for noisy in (
            "findings_created",
            "findings_updated",
            "file_created",
            "observations_created",
            "observations_failed",
            "observation_ids",
        ):
            assert noisy not in slim, f"slim response unexpectedly carries {noisy!r}"

    async def test_default_skips_pairing(self, mcp_db_for_report_finding: FiligreeDB) -> None:
        """By default, report_finding creates only the finding (ADR-007)."""
        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "src/no-obs.py",
                    "rule_id": "no-obs-rule",
                    "message": "Finding without paired observation",
                },
            )
        )
        assert data["finding_result"] == "created"
        assert "observation_id" not in data
        # Confirm no observation row references this finding.
        row = mcp_db_for_report_finding.conn.execute(
            "SELECT COUNT(*) FROM observations WHERE source_finding_id = ?",
            (data["finding_id"],),
        ).fetchone()
        assert row[0] == 0

    async def test_actor_attribution_on_observation(self, mcp_db_for_report_finding: FiligreeDB) -> None:
        """When actor is passed, the paired observation's actor field records it
        instead of the default 'scanner:agent' (F3 — review-h).
        """
        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "src/actor.py",
                    "rule_id": "actor-rule",
                    "message": "Finding with explicit actor",
                    "actor": "my-agent-007",
                    "create_observation": True,
                },
            )
        )
        obs_id = data["observation_id"]
        row = mcp_db_for_report_finding.conn.execute(
            "SELECT actor FROM observations WHERE id = ?",
            (obs_id,),
        ).fetchone()
        assert row["actor"] == "my-agent-007"
        assert data["created_by"] == "my-agent-007"
        assert data["updated_by"] == "my-agent-007"

    async def test_create_observation_refreshes_summary(self, mcp_db_for_report_finding: FiligreeDB) -> None:
        with patch("filigree.mcp_server._refresh_summary") as refresh_summary:
            data = _parse(
                await call_tool(
                    "report_finding",
                    {
                        "file_path": "src/summary.py",
                        "rule_id": "needs-refresh",
                        "message": "Observation should appear in context",
                        "severity": "medium",
                        "create_observation": True,
                    },
                )
            )

        assert data["finding_result"] == "created"
        refresh_summary.assert_called_once()

    async def test_invalid_actor_on_observation_returns_validation_error(self, mcp_db_for_report_finding: FiligreeDB) -> None:
        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "src/bad-actor.py",
                    "rule_id": "bad-actor-rule",
                    "message": "Finding with invalid actor",
                    "actor": "\nbad",
                    "create_observation": True,
                },
            )
        )
        assert data["code"] == ErrorCode.VALIDATION
        assert "control" in data["error"].lower()
        row = mcp_db_for_report_finding.conn.execute(
            "SELECT COUNT(*) AS c FROM observations WHERE file_path = ?",
            ("src/bad-actor.py",),
        ).fetchone()
        assert row["c"] == 0

    async def test_observation_links_back_to_finding(self, mcp_db_for_report_finding: FiligreeDB) -> None:
        """report_finding creates an observation with source_finding_id linking
        back to the finding (filigree-cb980eee0d, P1.2). The dual-write that
        prior reviews flagged as a zombie source is now correlated.
        """
        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "src/cascade-link.py",
                    "rule_id": "review-d-link",
                    "message": "Linked observation",
                    "response_detail": "full",
                    "create_observation": True,
                },
            )
        )
        finding_id = data["finding_id"]
        observation_ids = data.get("observation_ids", [])
        assert len(observation_ids) == 1
        # The observation row carries source_finding_id pointing at the finding.
        row = mcp_db_for_report_finding.conn.execute(
            "SELECT source_finding_id FROM observations WHERE id = ?",
            (observation_ids[0],),
        ).fetchone()
        assert row is not None
        assert row["source_finding_id"] == finding_id

    async def test_dismiss_finding_cascades_to_observation(self, mcp_db_for_report_finding: FiligreeDB) -> None:
        """dismiss_finding auto-dismisses the linked observation (P1.2)."""
        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "src/cascade-dismiss.py",
                    "rule_id": "review-d-dismiss",
                    "message": "Will be dismissed",
                    "response_detail": "full",
                    "create_observation": True,
                },
            )
        )
        finding_id = data["finding_id"]
        observation_ids = data["observation_ids"]
        # Observation is alive before dismiss.
        live = mcp_db_for_report_finding.conn.execute(
            "SELECT COUNT(*) AS c FROM observations WHERE id = ?", (observation_ids[0],)
        ).fetchone()
        assert live["c"] == 1
        # Dismiss the finding.
        await call_tool("dismiss_finding", {"finding_id": finding_id, "reason": "duplicate"})
        # Observation gone, dismissal recorded.
        live = mcp_db_for_report_finding.conn.execute(
            "SELECT COUNT(*) AS c FROM observations WHERE id = ?", (observation_ids[0],)
        ).fetchone()
        assert live["c"] == 0
        dismissed = mcp_db_for_report_finding.conn.execute(
            "SELECT COUNT(*) AS c FROM dismissed_observations WHERE obs_id = ?", (observation_ids[0],)
        ).fetchone()
        assert dismissed["c"] == 1

    async def test_promote_finding_cascades_to_observation(self, mcp_db_for_report_finding: FiligreeDB) -> None:
        """promote_finding auto-dismisses the linked observation (P1.2)."""
        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "src/cascade-promote.py",
                    "rule_id": "review-d-promote",
                    "message": "Will be promoted",
                    "response_detail": "full",
                    "create_observation": True,
                },
            )
        )
        finding_id = data["finding_id"]
        observation_ids = data["observation_ids"]
        # Promote the finding.
        await call_tool("promote_finding", {"finding_id": finding_id})
        # Linked observation is gone.
        live = mcp_db_for_report_finding.conn.execute(
            "SELECT COUNT(*) AS c FROM observations WHERE id = ?", (observation_ids[0],)
        ).fetchone()
        assert live["c"] == 0
