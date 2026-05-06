"""CLI tests for scanner lifecycle commands.

MCP shape verification (verified against mcp_tools/scanners.py handlers):

- list-scanners: ListResponse[T] — {items: [...], has_more: bool}
  Item keys: name, description, file_types (from ScannerConfig.to_dict())
- trigger-scan success: {status, scanner, file_path, file_id, scan_run_id, pid, log_path, message}
- trigger-scan-batch success: {status, scanner, file_count, processes_spawned, batch_id, scan_run_ids, per_file}
- get-scan-status: ScanRunStatusDict — {id, status, scanner_name, ..., process_alive, log_tail}
- preview-scan: {scanner, file_path, command, command_string, valid, validation_error}
- report-finding: {status, findings_created, findings_updated, file_created, [finding_id], [warnings]}

Subprocess mocking: patch "filigree.scanner_runtime.subprocess.Popen" (same path as MCP tests).
trigger-scan and trigger-scan-batch are mocked at the subprocess level.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from filigree.cli import cli
from tests._seeds import SeededProject

# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


def _write_scanner_toml(project_path: Path, name: str = "test-scanner") -> None:
    """Write a scanner TOML into <project>/.filigree/scanners/."""
    scanners_dir = project_path / ".filigree" / "scanners"
    scanners_dir.mkdir(parents=True, exist_ok=True)
    (scanners_dir / f"{name}.toml").write_text(
        f'[scanner]\nname = "{name}"\ndescription = "Test scanner"\n'
        f'command = "echo"\nargs = ["scan", "{{file}}", "--scan-run-id", "{{scan_run_id}}"]\nfile_types = ["py"]\n'
    )


def _make_target_file(project_path: Path, name: str = "target.py") -> Path:
    """Create a target file and return its absolute path."""
    target = project_path / name
    target.write_text("x = 1\n")
    return target


class _FakeProc:
    """Minimal subprocess.Popen stand-in."""

    def __init__(self, pid: int = 12345) -> None:
        self.pid = pid

    def poll(self) -> None:
        return None


@pytest.fixture
def project_with_scanner(initialized_project: Path) -> SeededProject:
    """A project with a test-scanner TOML and a target.py file."""
    _write_scanner_toml(initialized_project)
    _make_target_file(initialized_project, "target.py")
    return SeededProject(path=initialized_project)


# ---------------------------------------------------------------------------
# TestListScannersCommand
# ---------------------------------------------------------------------------


class TestListScannersCommand:
    def test_list_empty_returns_envelope(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["list-scanners", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["items"] == []
            assert data["has_more"] is False
        finally:
            os.chdir(original)

    def test_list_scanner_present_item_shape(self, initialized_project: Path) -> None:
        """Items must include ScannerConfig.to_dict() scanner metadata."""
        _write_scanner_toml(initialized_project)
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["list-scanners", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert len(data["items"]) == 1
            item = data["items"][0]
            assert item["name"] == "test-scanner"
            assert "description" in item
            assert "file_types" in item
            # Exact key set matches ScannerConfig.to_dict()
            assert set(item.keys()) == {
                "description",
                "estimated_cost",
                "execution_mode",
                "file_types",
                "may_send_contents",
                "name",
                "requires_approval",
                "requires_dashboard",
                "risk_summary",
                "safe_preview_only",
            }
        finally:
            os.chdir(original)

    def test_list_plain_text_empty(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["list-scanners"])
            assert result.exit_code == 0
            assert "No scanners" in result.output
        finally:
            os.chdir(original)

    def test_list_plain_text_populated(self, initialized_project: Path) -> None:
        _write_scanner_toml(initialized_project)
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["list-scanners"])
            assert result.exit_code == 0
            assert "test-scanner" in result.output
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestPreviewScanCommand
# ---------------------------------------------------------------------------


class TestPreviewScanCommand:
    def test_preview_scan_happy_path(self, project_with_scanner: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            result = runner.invoke(cli, ["preview-scan", "test-scanner", "target.py", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["scanner"] == "test-scanner"
            assert data["file_path"] == "target.py"
            assert isinstance(data["command"], list)
            assert "target.py" in data["command_string"]
            assert data["valid"] is True
            assert data["validation_error"] is None
        finally:
            os.chdir(original)

    def test_preview_scan_not_found(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["preview-scan", "nonexistent", "foo.py", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "NOT_FOUND"
        finally:
            os.chdir(original)

    def test_preview_scan_path_traversal(self, initialized_project: Path) -> None:
        _write_scanner_toml(initialized_project)
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["preview-scan", "test-scanner", "../../etc/passwd", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "VALIDATION"
        finally:
            os.chdir(original)

    def test_preview_scan_plain_text(self, project_with_scanner: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            result = runner.invoke(cli, ["preview-scan", "test-scanner", "target.py"])
            assert result.exit_code == 0
            assert "test-scanner" in result.output
            assert "target.py" in result.output
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestGetScanStatusCommand
# ---------------------------------------------------------------------------


class TestGetScanStatusCommand:
    def test_get_scan_status_happy_path(self, initialized_project: Path) -> None:
        from filigree.cli_common import get_db

        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            with get_db() as db:
                db.create_scan_run(
                    scan_run_id="test-run-cli-1",
                    scanner_name="scanner",
                    scan_source="scanner",
                    file_paths=["src/a.py"],
                    file_ids=["fid-1"],
                )
            runner = CliRunner()
            result = runner.invoke(cli, ["get-scan-status", "test-run-cli-1", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["id"] == "test-run-cli-1"
            assert data["status"] == "pending"
            assert "process_alive" in data
            assert "log_tail" in data
        finally:
            os.chdir(original)

    def test_get_scan_status_not_found(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["get-scan-status", "nonexistent-run", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "NOT_FOUND"
        finally:
            os.chdir(original)

    def test_get_scan_status_empty_id_rejected(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["get-scan-status", "   ", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "VALIDATION"
        finally:
            os.chdir(original)

    def test_get_scan_status_plain_text(self, initialized_project: Path) -> None:
        from filigree.cli_common import get_db

        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            with get_db() as db:
                db.create_scan_run(
                    scan_run_id="test-run-cli-plain",
                    scanner_name="scanner",
                    scan_source="scanner",
                    file_paths=["src/a.py"],
                    file_ids=["fid-2"],
                )
            runner = CliRunner()
            result = runner.invoke(cli, ["get-scan-status", "test-run-cli-plain"])
            assert result.exit_code == 0
            assert "test-run-cli-plain" in result.output
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestReportFindingCommand
# ---------------------------------------------------------------------------


_REPORT_FINDING_JSON = json.dumps(
    {
        "path": "src/foo.py",
        "rule_id": "test-rule",
        "message": "This is a test finding",
        "severity": "high",
    }
)

_REPORT_FINDING_KEYS = frozenset({"status", "findings_created", "findings_updated", "file_created"})


class TestReportFindingCommand:
    def test_report_finding_stdin(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["report-finding", "--json"], input=_REPORT_FINDING_JSON)
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert _REPORT_FINDING_KEYS.issubset(set(data.keys()))
            assert data["findings_created"] == 1
            assert data["file_created"] is True
            assert "finding_id" in data
        finally:
            os.chdir(original)

    def test_report_finding_file_option(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        finding_file = initialized_project / "finding.json"
        finding_file.write_text(_REPORT_FINDING_JSON)
        try:
            result = runner.invoke(cli, ["report-finding", "--file", "finding.json", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert _REPORT_FINDING_KEYS.issubset(set(data.keys()))
            assert data["findings_created"] == 1
        finally:
            os.chdir(original)

    def test_report_finding_stdin_and_file_same_result(self, initialized_project: Path) -> None:
        """stdin and --file paths should both succeed and produce the required key set."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        # Use distinct findings to avoid dedup (which suppresses finding_id on update)
        finding_a = json.dumps({"path": "src/a.py", "rule_id": "rule-a", "message": "A"})
        finding_b = json.dumps({"path": "src/b.py", "rule_id": "rule-b", "message": "B"})
        finding_file = initialized_project / "finding_b.json"
        finding_file.write_text(finding_b)
        try:
            stdin_result = runner.invoke(cli, ["report-finding", "--json"], input=finding_a)
            file_result = runner.invoke(cli, ["report-finding", "--file", "finding_b.json", "--json"])
            assert stdin_result.exit_code == 0, stdin_result.output
            assert file_result.exit_code == 0, file_result.output
            stdin_data = json.loads(stdin_result.output)
            file_data = json.loads(file_result.output)
            # Both should have the required keys
            assert _REPORT_FINDING_KEYS.issubset(set(stdin_data.keys()))
            assert _REPORT_FINDING_KEYS.issubset(set(file_data.keys()))
            assert stdin_data["findings_created"] == 1
            assert file_data["findings_created"] == 1
        finally:
            os.chdir(original)

    def test_report_finding_invalid_json(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["report-finding", "--json"], input="not valid json{")
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "VALIDATION"
        finally:
            os.chdir(original)

    def test_report_finding_missing_required_fields(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["report-finding", "--json"], input='{"path": "foo.py"}')
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "VALIDATION"
        finally:
            os.chdir(original)

    def test_report_finding_invalid_severity(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        bad_json = json.dumps({"path": "foo.py", "rule_id": "r", "message": "m", "severity": "SUPER_CRITICAL"})
        try:
            result = runner.invoke(cli, ["report-finding", "--json"], input=bad_json)
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "VALIDATION"
        finally:
            os.chdir(original)

    def test_report_finding_accepts_file_path_alias(self, initialized_project: Path) -> None:
        """The CLI should accept file_path as an alias for path."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        alias_json = json.dumps({"file_path": "src/bar.py", "rule_id": "r", "message": "m"})
        try:
            result = runner.invoke(cli, ["report-finding", "--json"], input=alias_json)
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["findings_created"] == 1
        finally:
            os.chdir(original)

    def test_report_finding_plain_text(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["report-finding"], input=_REPORT_FINDING_JSON)
            assert result.exit_code == 0
            assert "src/foo.py" in result.output
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestTriggerScanCommand
# ---------------------------------------------------------------------------

# NOTE: trigger-scan and trigger-scan-batch spawn subprocesses. We mock
# filigree.scanner_runtime.subprocess.Popen to avoid actually running a process.
# Validation/error-path tests do not need mocking.


class TestTriggerScanCommand:
    def test_trigger_scan_success(self, project_with_scanner: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            with patch("filigree.scanner_runtime.subprocess.Popen", return_value=_FakeProc(12345)):
                result = runner.invoke(cli, ["trigger-scan", "test-scanner", "target.py", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["status"] == "triggered"
            assert data["scanner"] == "test-scanner"
            assert data["file_path"] == "target.py"
            assert "file_id" in data
            assert "scan_run_id" in data
            assert data["pid"] == 12345
            assert "log_path" in data
            assert "message" in data
        finally:
            os.chdir(original)

    def test_trigger_scan_scanner_not_found(self, initialized_project: Path) -> None:
        _make_target_file(initialized_project, "target.py")
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["trigger-scan", "nonexistent", "target.py", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "NOT_FOUND"
        finally:
            os.chdir(original)

    def test_trigger_scan_file_not_found(self, initialized_project: Path) -> None:
        _write_scanner_toml(initialized_project)
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["trigger-scan", "test-scanner", "no_such_file.py", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] in ("NOT_FOUND", "VALIDATION")
        finally:
            os.chdir(original)

    def test_trigger_scan_path_traversal(self, initialized_project: Path) -> None:
        _write_scanner_toml(initialized_project)
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["trigger-scan", "test-scanner", "../../etc/passwd", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "VALIDATION"
        finally:
            os.chdir(original)

    def test_trigger_scan_invalid_api_url(self, project_with_scanner: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            result = runner.invoke(
                cli,
                ["trigger-scan", "test-scanner", "target.py", "--api-url", "https://evil.example.com", "--json"],
            )
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "INVALID_API_URL"
        finally:
            os.chdir(original)

    def test_trigger_scan_spawn_failure(self, project_with_scanner: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            with patch("filigree.scanner_runtime.subprocess.Popen", side_effect=OSError("mock spawn fail")):
                result = runner.invoke(cli, ["trigger-scan", "test-scanner", "target.py", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "IO"
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestTriggerScanBatchCommand
# ---------------------------------------------------------------------------


class TestTriggerScanBatchCommand:
    def test_batch_scan_success(self, project_with_scanner: SeededProject) -> None:
        _make_target_file(project_with_scanner.path, "target2.py")
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            with patch(
                "filigree.scanner_runtime.subprocess.Popen",
                side_effect=[_FakeProc(100), _FakeProc(101)],
            ):
                result = runner.invoke(
                    cli,
                    ["trigger-scan-batch", "test-scanner", "target.py", "target2.py", "--json"],
                )
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["status"] == "triggered"
            assert data["scanner"] == "test-scanner"
            assert data["file_count"] == 2
            assert data["processes_spawned"] == 2
            assert "batch_id" in data
            assert len(data["scan_run_ids"]) == 2
            assert len(set(data["scan_run_ids"])) == 2  # unique per file
            assert len(data["per_file"]) == 2
        finally:
            os.chdir(original)

    def test_batch_scan_all_spawn_failure(self, project_with_scanner: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            with patch("filigree.scanner_runtime.subprocess.Popen", side_effect=OSError("mock fail")):
                result = runner.invoke(
                    cli,
                    ["trigger-scan-batch", "test-scanner", "target.py", "--json"],
                )
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "IO"
        finally:
            os.chdir(original)

    def test_batch_scan_scanner_not_found(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(
                cli,
                ["trigger-scan-batch", "nonexistent", "foo.py", "--json"],
            )
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "NOT_FOUND"
        finally:
            os.chdir(original)

    def test_batch_scan_skips_invalid_paths(self, project_with_scanner: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            with patch("filigree.scanner_runtime.subprocess.Popen", return_value=_FakeProc(100)):
                result = runner.invoke(
                    cli,
                    ["trigger-scan-batch", "test-scanner", "target.py", "nonexistent.py", "../../etc/passwd", "--json"],
                )
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["status"] == "triggered"
            assert data["file_count"] == 1
            assert len(data["skipped"]) == 2
        finally:
            os.chdir(original)

    def test_batch_scan_invalid_api_url(self, project_with_scanner: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            result = runner.invoke(
                cli,
                ["trigger-scan-batch", "test-scanner", "target.py", "--api-url", "https://evil.example.com", "--json"],
            )
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "INVALID_API_URL"
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestForeignDatabaseDiagnostic — bug fix: scanner CLI must surface the rich
# ForeignDatabaseError message, not collapse it into a generic
# "Project directory not initialized" line. Mirrors the regression contract
# in tests/test_doctor.py::test_foreign_database_is_reported_with_specific_message.
# ---------------------------------------------------------------------------


class TestForeignDatabaseDiagnostic:
    @staticmethod
    def _make_foreign_layout(tmp_path: Path) -> Path:
        """Outer project has .filigree/, inner is a separate git repo with no anchor."""
        outer = tmp_path / "outer"
        outer.mkdir()
        (outer / ".git").mkdir()
        (outer / ".filigree.conf").write_text("[filigree]\n", encoding="utf-8")
        (outer / ".filigree").mkdir()
        inner = outer / "inner-repo"
        inner.mkdir()
        (inner / ".git").mkdir()
        return inner

    def _assert_foreign_message(self, output: str) -> None:
        data = json.loads(output)
        assert data["code"] == "NOT_INITIALIZED"
        # The whole point: the rich diagnostic survives, not the old generic line.
        assert "Refusing to latch" in data["error"]
        assert "filigree init" in data["error"]
        assert data["error"] != "Project directory not initialized"

    @pytest.mark.parametrize(
        "argv",
        [
            ["list-scanners", "--json"],
            ["preview-scan", "any-scanner", "any-file.py", "--json"],
            ["trigger-scan", "any-scanner", "any-file.py", "--json"],
            ["trigger-scan-batch", "any-scanner", "any-file.py", "--json"],
            # report-finding --file used to read/validate the file before
            # resolving the project, masking ForeignDatabaseError as VALIDATION.
            ["report-finding", "--file", "missing.json", "--json"],
        ],
    )
    def test_foreign_database_message_survives(self, tmp_path: Path, argv: list[str]) -> None:
        inner = self._make_foreign_layout(tmp_path)
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(inner))
        try:
            result = runner.invoke(cli, argv)
            assert result.exit_code == 1, result.output
            self._assert_foreign_message(result.output)
        finally:
            os.chdir(original)

    def test_trigger_scan_batch_empty_filepaths_returns_validation_envelope(self, initialized_project: Path) -> None:
        """`trigger-scan-batch <scanner> --json` (no file paths) must emit a
        structured VALIDATION envelope, matching the MCP batch handler's
        contract. Click's variadic ``required=True`` previously preempted the
        in-callback empty-list guard with raw usage text on stderr.
        """
        _write_scanner_toml(initialized_project)
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["trigger-scan-batch", "test-scanner", "--json"])
            assert result.exit_code == 1, result.output
            data = json.loads(result.output)
            assert data["code"] == "VALIDATION"
            assert "non-empty" in data["error"]
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestReportFindingTypeValidation — bug fix: malformed JSON field types must
# yield ErrorCode.VALIDATION envelopes, never raw TypeError or ErrorCode.IO.
# ---------------------------------------------------------------------------


class TestReportFindingTypeValidation:
    @staticmethod
    def _invoke(initialized_project: Path, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["report-finding", "--json"], input=json.dumps(payload))
            return result.exit_code, json.loads(result.output)
        finally:
            os.chdir(original)

    def test_severity_unhashable_does_not_crash(self, initialized_project: Path) -> None:
        """`"severity": []` used to raise TypeError from `not in VALID_SEVERITIES`."""
        code, data = self._invoke(
            initialized_project,
            {"path": "foo.py", "rule_id": "r", "message": "m", "severity": []},
        )
        assert code == 1
        assert data["code"] == "VALIDATION"
        assert "severity" in str(data["error"])

    def test_path_must_be_string(self, initialized_project: Path) -> None:
        """A truthy non-string path used to slip past the falsy guard and surface as IO."""
        code, data = self._invoke(
            initialized_project,
            {"path": [1, 2], "rule_id": "r", "message": "m"},
        )
        assert code == 1
        assert data["code"] == "VALIDATION"
        assert "path" in str(data["error"])

    def test_rule_id_must_be_string(self, initialized_project: Path) -> None:
        code, data = self._invoke(
            initialized_project,
            {"path": "foo.py", "rule_id": 42, "message": "m"},
        )
        assert code == 1
        assert data["code"] == "VALIDATION"
        assert "rule_id" in str(data["error"])

    def test_message_must_be_string(self, initialized_project: Path) -> None:
        code, data = self._invoke(
            initialized_project,
            {"path": "foo.py", "rule_id": "r", "message": {"nested": "no"}},
        )
        assert code == 1
        assert data["code"] == "VALIDATION"
        assert "message" in str(data["error"])

    def test_line_start_non_int_validation(self, initialized_project: Path) -> None:
        """line_start used to reach the DB layer and surface as ErrorCode.IO."""
        code, data = self._invoke(
            initialized_project,
            {"path": "foo.py", "rule_id": "r", "message": "m", "line_start": "ten"},
        )
        assert code == 1
        assert data["code"] == "VALIDATION"
        assert "line_start" in str(data["error"])

    def test_line_end_non_int_validation(self, initialized_project: Path) -> None:
        code, data = self._invoke(
            initialized_project,
            {"path": "foo.py", "rule_id": "r", "message": "m", "line_end": 1.5},
        )
        assert code == 1
        assert data["code"] == "VALIDATION"
        assert "line_end" in str(data["error"])
