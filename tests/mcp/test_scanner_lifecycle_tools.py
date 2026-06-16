"""MCP tool tests for scanner lifecycle handlers (trigger_scan_batch, get_scan_status, preview_scan).

Tests the MCP handler layer via call_tool() — handler wiring, argument parsing,
validation, and error mapping. Core DB methods are covered in test_scans.py;
these tests verify the MCP integration layer on top.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from filigree.core import FiligreeDB, write_config
from filigree.mcp_server import call_tool, list_tools  # type: ignore[attr-defined]
from filigree.mcp_tools.scanners import _validate_localhost_url
from filigree.registry import RegistryUnavailableError, ResolvedFile
from filigree.types.api import ErrorCode
from tests._fakes.registry import PathRegistry
from tests.mcp._helpers import _parse


def _write_scanner_toml(mcp_db: FiligreeDB, name: str = "test-scanner") -> None:
    """Write a scanner TOML into the test .filigree/scanners/ dir."""
    import filigree.mcp_server as mcp_mod

    assert mcp_mod._filigree_dir is not None
    scanners_dir = mcp_mod._filigree_dir / "scanners"
    scanners_dir.mkdir(exist_ok=True)
    (scanners_dir / f"{name}.toml").write_text(
        f'[scanner]\nname = "{name}"\ndescription = "Test scanner"\n'
        'command = "echo"\n'
        'args = ["scan", "{file}", "--api-url", "{api_url}", "--scan-run-id", "{scan_run_id}"]\n'
        'file_types = ["py"]\n'
    )


def _make_target_files(mcp_db: FiligreeDB, names: list[str]) -> list[str]:
    """Create target files on disk and return their names."""
    import filigree.mcp_server as mcp_mod

    assert mcp_mod._filigree_dir is not None
    project_root = mcp_mod._filigree_dir.parent
    for name in names:
        (project_root / name).write_text("x = 1\n")
    return names


def _cleanup_files(mcp_db: FiligreeDB, names: list[str]) -> None:
    """Remove target files from disk."""
    import filigree.mcp_server as mcp_mod

    assert mcp_mod._filigree_dir is not None
    project_root = mcp_mod._filigree_dir.parent
    for name in names:
        (project_root / name).unlink(missing_ok=True)


class _FakeProc:
    """Minimal subprocess.Popen stand-in."""

    def __init__(self, pid: int = 12345) -> None:
        self.pid = pid

    def poll(self) -> None:
        return None


class TestPreviewScanTool:
    async def test_mcp_can_manage_bundled_scanner_registration(self, mcp_db: FiligreeDB) -> None:
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None

        available = _parse(await call_tool("scanner_available_list", {}))
        names = {item["name"] for item in available["items"]}
        assert {"codex", "claude"} <= names

        enabled = _parse(await call_tool("scanner_enable", {"scanner": "codex"}))
        assert enabled["status"] == "enabled"
        assert (mcp_mod._filigree_dir / "scanners" / "codex.toml").is_file()

        listed = _parse(await call_tool("scanner_list", {}))
        codex = next(item for item in listed["items"] if item["name"] == "codex")
        assert codex["accepts_prompt"] is True
        assert codex["prompt_pack_aware"] is True
        assert codex["prompt_packs_endpoint"] == "list_prompt_packs"
        assert "python-engineering" in codex["applicable_prompts"]
        assert "pytorch" in codex["applicable_prompts"]
        assert "rust" not in codex["applicable_prompts"]
        assert codex["managed"] is True
        assert codex["bundled_name"] is True
        assert codex["bundled_match"] is True
        assert codex["sandbox_class"] == "tool-sandboxed"
        assert codex["language_focus"] == ["python"]

        disabled = _parse(await call_tool("scanner_disable", {"scanner": "codex"}))
        assert disabled["status"] == "disabled"
        assert not (mcp_mod._filigree_dir / "scanners" / "codex.toml").exists()

    async def test_mcp_disable_refuses_custom_bundled_name_without_force(self, mcp_db: FiligreeDB) -> None:
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None
        scanner_path = mcp_mod._filigree_dir / "scanners" / "codex.toml"
        scanner_path.parent.mkdir(exist_ok=True)
        scanner_path.write_text(
            '[scanner]\nname = "codex"\ndescription = "Custom scanner"\ncommand = "python custom.py"\nargs = []\nfile_types = ["py"]\n'
        )

        data = _parse(await call_tool("scanner_disable", {"scanner": "codex"}))

        assert data["code"] == ErrorCode.CONFLICT
        assert "--force" in data["error"]
        assert data["details"]["conflict_kind"] == "custom"
        assert scanner_path.exists()

    async def test_mcp_disable_rejects_traversal_name_without_deleting_config(self, mcp_db: FiligreeDB) -> None:
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None
        (mcp_mod._filigree_dir / "scanners").mkdir(exist_ok=True)
        config = mcp_mod._filigree_dir / "config.toml"
        config.write_text("[local]\nkeep = true\n")

        data = _parse(await call_tool("scanner_disable", {"scanner": "../config"}))

        assert data["code"] == ErrorCode.VALIDATION
        assert config.exists()

    async def test_scanner_management_schema_is_exposed(self, mcp_db: FiligreeDB) -> None:
        tools = {tool.name: tool for tool in await list_tools()}

        assert "scanner_available_list" in tools
        assert "scanner_enable" in tools
        assert "scanner_disable" in tools
        assert "scanner_available_list" in tools["scanner_list"].description
        assert tools["scanner_enable"].inputSchema["properties"]["scanner"]["type"] == "string"

    async def test_preview_scan_rejects_missing_scanner(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("scan_preview", {"file_path": "preview_target.py"}))

        assert data["code"] == ErrorCode.VALIDATION
        assert "scanner" in data["error"]

    async def test_preview_scan_rejects_missing_file_path(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("scan_preview", {"scanner": "test-scanner"}))

        assert data["code"] == ErrorCode.VALIDATION
        assert "file_path" in data["error"]

    async def test_preview_scan(self, mcp_db: FiligreeDB) -> None:
        files = _make_target_files(mcp_db, ["preview_target.py"])
        _write_scanner_toml(mcp_db)
        try:
            data = _parse(
                await call_tool(
                    "scan_preview",
                    {"scanner": "test-scanner", "file_path": "preview_target.py"},
                )
            )
            assert data["valid"] is True
            assert data["scanner"] == "test-scanner"
            assert isinstance(data["command"], list)
            assert "preview_target.py" in data["command_string"]
            assert data["api_url_source"] in {"fallback_default", "ephemeral_port", "server_config"}
            assert data["execution_mode"] == "external_process"
            assert data["may_send_contents"] is True
            assert data["requires_dashboard"] is True
            assert data["estimated_cost"] == "unknown"
            assert data["safe_preview_only"] is True
            assert data["preview_recommended"] is True
            assert data["requires_approval"] is True
            assert data["sandbox_class"] == "custom"
            assert "repository files" in data["risk_summary"]
            assert data["prompt_pack_scope"] == "advisory"
            assert "does not restrict" in data["prompt_pack_scope_summary"]
        finally:
            _cleanup_files(mcp_db, files)

    async def test_preview_scan_accepts_prompt_pack(self, mcp_db: FiligreeDB) -> None:
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None
        files = _make_target_files(mcp_db, ["preview_prompt.py"])
        scanner_path = mcp_mod._filigree_dir / "scanners" / "codex.toml"
        scanner_path.parent.mkdir(exist_ok=True)
        scanner_path.write_text(
            "[scanner]\n"
            'name = "codex"\n'
            'description = "Codex"\n'
            'command = "echo"\n'
            'args = ["--file", "{file}", "--prompt", "{prompt}", "--api-url", "{api_url}"]\n'
            'file_types = ["py"]\n'
        )
        try:
            data = _parse(
                await call_tool(
                    "scan_preview",
                    {"scanner": "codex", "file_path": "preview_prompt.py", "prompt": "pytorch"},
                )
            )
            assert data["valid"] is True
            assert "--prompt pytorch" in data["command_string"]
        finally:
            _cleanup_files(mcp_db, files)

    async def test_preview_scan_rejects_unknown_prompt_pack(self, mcp_db: FiligreeDB) -> None:
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None
        files = _make_target_files(mcp_db, ["preview_bad_prompt.py"])
        _write_scanner_toml(mcp_db)
        try:
            data = _parse(
                await call_tool(
                    "scan_preview",
                    {"scanner": "test-scanner", "file_path": "preview_bad_prompt.py", "prompt": "not-a-pack"},
                )
            )
            assert data["code"] == ErrorCode.VALIDATION
            assert "Unknown prompt pack" in data["error"]
        finally:
            _cleanup_files(mcp_db, files)

    async def test_preview_scan_rejects_prompt_when_scanner_template_cannot_accept_it(self, mcp_db: FiligreeDB) -> None:
        files = _make_target_files(mcp_db, ["preview_no_prompt.py"])
        _write_scanner_toml(mcp_db)
        try:
            data = _parse(
                await call_tool(
                    "scan_preview",
                    {"scanner": "test-scanner", "file_path": "preview_no_prompt.py", "prompt": "security"},
                )
            )
            assert data["code"] == ErrorCode.VALIDATION
            assert "does not accept prompt packs" in data["error"]
        finally:
            _cleanup_files(mcp_db, files)

    async def test_list_prompt_packs_tool(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("prompt_pack_list", {}))

        names = {item["name"] for item in data["items"]}
        assert {"bug-hunt", "security", "typescript"} <= names
        security = next(item for item in data["items"] if item["name"] == "security")
        assert security["language"] == "any"
        assert security["prompt_pack_scope"] == "advisory"
        assert security["expected_relative_cost"] == "medium"
        assert "instructions" in security
        assert data["has_more"] is False

    async def test_list_prompt_packs_can_filter_by_language(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("prompt_pack_list", {"language": "python"}))

        names = {item["name"] for item in data["items"]}
        assert {"bug-hunt", "security", "python-engineering", "pytorch"} <= names
        assert "rust" not in names
        assert "terraform" not in names
        assert "react" not in names

    async def test_prompt_argument_schema_uses_prompt_pack_enum(self, mcp_db: FiligreeDB) -> None:
        tools = {tool.name: tool for tool in await list_tools()}

        for tool_name in ("scan_preview", "scan_trigger", "scan_trigger_batch"):
            schema = tools[tool_name].inputSchema
            prompt_schema = schema["properties"]["prompt"]
            assert "security" in prompt_schema["enum"]
            assert "not-a-pack" not in prompt_schema["enum"]
            assert prompt_schema["default"] == "bug-hunt"
            assert "accepts_prompt" in prompt_schema["description"]
            assert "advisory only" in prompt_schema["description"]
            assert "does not restrict scanner file access" in prompt_schema["description"]

        pack_schema = tools["prompt_pack_list"].inputSchema
        assert pack_schema["properties"]["language"]["type"] == "string"
        assert "language-specific" in pack_schema["properties"]["language"]["description"]

    async def test_preview_scan_uses_ethereal_port_file_for_default_api_url(self, mcp_db: FiligreeDB) -> None:
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None
        (mcp_mod._filigree_dir / "ephemeral.port").write_text("9229\n")
        files = _make_target_files(mcp_db, ["preview_port.py"])
        _write_scanner_toml(mcp_db)
        try:
            data = _parse(
                await call_tool(
                    "scan_preview",
                    {"scanner": "test-scanner", "file_path": "preview_port.py"},
                )
            )
            assert data["valid"] is True
            assert "http://localhost:9229" in data["command"]
            assert data["api_url"] == "http://localhost:9229"
            assert data["api_url_source"] == "ephemeral_port"
            assert "http://localhost:8377" not in data["command"]
        finally:
            _cleanup_files(mcp_db, files)

    async def test_preview_scan_invalid_project_mode_returns_validation(self, mcp_db: FiligreeDB) -> None:
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None
        write_config(mcp_mod._filigree_dir, {"prefix": "mcp", "version": 1, "mode": "bogus"})
        files = _make_target_files(mcp_db, ["preview_invalid_mode.py"])
        _write_scanner_toml(mcp_db)
        try:
            data = _parse(
                await call_tool(
                    "scan_preview",
                    {"scanner": "test-scanner", "file_path": "preview_invalid_mode.py"},
                )
            )
            assert data["code"] == ErrorCode.VALIDATION
            assert "Unknown mode" in data["error"]
        finally:
            _cleanup_files(mcp_db, files)

    async def test_preview_scan_not_found(self, mcp_db: FiligreeDB) -> None:
        data = _parse(
            await call_tool(
                "scan_preview",
                {"scanner": "nonexistent", "file_path": "foo.py"},
            )
        )
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_preview_scan_known_bundled_not_enabled_points_to_enable_flow(self, mcp_db: FiligreeDB) -> None:
        data = _parse(
            await call_tool(
                "scan_preview",
                {"scanner": "codex", "file_path": "foo.py"},
            )
        )

        assert data["code"] == ErrorCode.NOT_FOUND
        assert data["details"]["bundled"] is True
        assert data["details"]["enable_with"] == "scanner_enable"
        assert data["details"]["cli_enable_command"] == "filigree scanner enable codex"
        assert "scanner_available_list" in data["details"]["hint"]

    async def test_preview_scan_path_traversal(self, mcp_db: FiligreeDB) -> None:
        _write_scanner_toml(mcp_db)
        data = _parse(
            await call_tool(
                "scan_preview",
                {"scanner": "test-scanner", "file_path": "../../etc/passwd"},
            )
        )
        assert data["code"] == ErrorCode.VALIDATION


class TestGetScanStatusTool:
    async def test_get_scan_status(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_scan_run(
            scan_run_id="test-run-1",
            scanner_name="scanner",
            scan_source="scanner",
            file_paths=["src/a.py"],
            file_ids=["fid-1"],
        )
        data = _parse(await call_tool("scan_status_get", {"scan_run_id": "test-run-1"}))
        assert data["id"] == "test-run-1"
        assert data["status"] == "pending"
        assert "process_alive" in data
        assert "log_tail" in data
        # Posture echo: status carries the target file's findings breakdown (zero here).
        assert "file_summary" in data
        assert data["file_summary"]["total_findings"] == 0

    async def test_get_scan_status_file_summary_reflects_findings(self, mcp_db: FiligreeDB) -> None:
        fr = mcp_db.register_file("src/scanned.py", language="python")
        mcp_db.process_scan_results(
            scan_source="scanner",
            findings=[
                {"path": "src/scanned.py", "rule_id": "r1", "severity": "critical", "message": "boom"},
                {"path": "src/scanned.py", "rule_id": "r2", "severity": "low", "message": "meh"},
            ],
        )
        mcp_db.create_scan_run(
            scan_run_id="run-fs",
            scanner_name="scanner",
            scan_source="scanner",
            file_paths=["src/scanned.py"],
            file_ids=[fr.id],
        )
        data = _parse(await call_tool("scan_status_get", {"scan_run_id": "run-fs"}))
        # The vacuous-green fix: a status poll echoes the real findings posture.
        fs = data["file_summary"]
        assert fs["total_findings"] == 2
        assert fs["critical"] == 1
        assert fs["low"] == 1

    async def test_get_scan_status_not_found(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("scan_status_get", {"scan_run_id": "nonexistent"}))
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_get_scan_status_empty_id_rejected(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("scan_status_get", {"scan_run_id": ""}))
        assert data["code"] == ErrorCode.VALIDATION

    async def test_get_scan_status_log_lines_validated(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("scan_status_get", {"scan_run_id": "x", "log_lines": 0}))
        assert data["code"] == ErrorCode.VALIDATION

    async def test_get_scan_status_does_not_auto_fail_dead_process(self, mcp_db: FiligreeDB) -> None:
        """Status reads are read-only; dead process reconciliation is explicit."""
        mcp_db.create_scan_run(
            scan_run_id="dead-run",
            scanner_name="scanner",
            scan_source="scanner",
            file_paths=["src/a.py"],
            file_ids=["fid-1"],
            pid=99999,
        )
        mcp_db.update_scan_run_status("dead-run", "running")
        # os.kill will raise ProcessLookupError for a non-existent PID
        data = _parse(await call_tool("scan_status_get", {"scan_run_id": "dead-run"}))
        assert data["status"] == "running"
        assert data["process_alive"] is False
        assert any("appears dead" in warning for warning in data["data_warnings"])
        assert mcp_db.get_scan_run("dead-run")["status"] == "running"


class TestTriggerScanBatchTool:
    async def test_batch_scan_registry_unavailable_returns_error_response(self, mcp_db: FiligreeDB) -> None:
        class UnavailableRegistry:
            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                raise RegistryUnavailableError(
                    "Loomweave registry unavailable for test",
                    url="http://loomweave.test/api/v1/files?path=batch_registry_a.py",
                    path=path,
                    cause_kind="network",
                )

            def is_displaced(self) -> bool:
                return False

        files = _make_target_files(mcp_db, ["batch_registry_a.py"])
        _write_scanner_toml(mcp_db)
        mcp_db.registry = UnavailableRegistry()
        try:
            data = _parse(
                await call_tool(
                    "scan_trigger_batch",
                    {"approve_execution": True, "scanner": "test-scanner", "file_paths": ["batch_registry_a.py"]},
                )
            )

            assert data["code"] == ErrorCode.REGISTRY_UNAVAILABLE
            assert data["details"]["cause"] == "registry_unavailable"
            assert data["details"]["cause_kind"] == "network"
            assert data["details"]["path"] == "batch_registry_a.py"
            assert data["details"]["url"] == "http://loomweave.test/api/v1/files?path=batch_registry_a.py"
        finally:
            _cleanup_files(mcp_db, files)

    async def test_batch_scan_uses_registry_resolved_file_ids(self, mcp_db: FiligreeDB) -> None:
        files = _make_target_files(mcp_db, ["batch_registry_a.py", "batch_registry_b.py"])
        _write_scanner_toml(mcp_db)
        mcp_db.registry = PathRegistry()
        try:
            with patch(
                "filigree.scanner_runtime.subprocess.Popen",
                side_effect=[_FakeProc(100), _FakeProc(101)],
            ):
                data = _parse(
                    await call_tool(
                        "scan_trigger_batch",
                        {
                            "approve_execution": True,
                            "scanner": "test-scanner",
                            "file_paths": ["batch_registry_a.py", "batch_registry_b.py"],
                        },
                    )
                )

            expected_file_ids = ["core:file:batch_registry_a.py", "core:file:batch_registry_b.py"]
            for child_id, expected_file_id in zip(data["scan_run_ids"], expected_file_ids, strict=True):
                run = mcp_db.get_scan_run(child_id)
                assert run["file_ids"] == [expected_file_id]
        finally:
            _cleanup_files(mcp_db, files)

    async def test_batch_scan_custom_scanner_requires_explicit_execution_approval(self, mcp_db: FiligreeDB) -> None:
        files = _make_target_files(mcp_db, ["batch_unapproved.py"])
        _write_scanner_toml(mcp_db)
        try:
            with patch("filigree.scanner_runtime.subprocess.Popen") as popen:
                data = _parse(
                    await call_tool(
                        "scan_trigger_batch",
                        {"scanner": "test-scanner", "file_paths": ["batch_unapproved.py"]},
                    )
                )
            assert data["code"] == ErrorCode.VALIDATION
            assert "approve_execution=true" in data["error"]
            popen.assert_not_called()
        finally:
            _cleanup_files(mcp_db, files)

    async def test_batch_scan_success(self, mcp_db: FiligreeDB) -> None:
        files = _make_target_files(mcp_db, ["batch_a.py", "batch_b.py"])
        _write_scanner_toml(mcp_db)
        try:
            with patch(
                "filigree.scanner_runtime.subprocess.Popen",
                side_effect=[_FakeProc(100), _FakeProc(101)],
            ):
                data = _parse(
                    await call_tool(
                        "scan_trigger_batch",
                        {"approve_execution": True, "scanner": "test-scanner", "file_paths": ["batch_a.py", "batch_b.py"]},
                    )
                )
            assert data["status"] == "triggered"
            assert data["file_count"] == 2
            assert data["processes_spawned"] == 2
            assert data["api_url_source"] in {"fallback_default", "ephemeral_port", "server_config"}
            assert data["api_url"].startswith("http://localhost:")
            assert data["prompt_pack_scope"] == "advisory"
            assert data["preview_recommended"] is True
            assert data["sandbox_class"] == "custom"
            # Batch returns per-file scan_run_ids rather than one shared id —
            # each child's completion is tracked independently (filigree-ec33df4b86).
            assert "batch_id" in data
            assert isinstance(data["scan_run_ids"], list)
            assert len(data["scan_run_ids"]) == 2
            assert len(set(data["scan_run_ids"])) == 2  # unique per file
            for child_id in data["scan_run_ids"]:
                run = mcp_db.get_scan_run(child_id)
                assert len(run["file_paths"]) == 1
                assert run["status"] == "running"
            # Posture echo: each per_file entry carries the file's findings summary
            # (fresh files → zero findings, but the key is present, not vacuous).
            assert len(data["per_file"]) == 2
            for pf in data["per_file"]:
                assert "file_summary" in pf
                assert pf["file_summary"]["total_findings"] == 0
        finally:
            _cleanup_files(mcp_db, files)

    async def test_batch_scan_uses_ethereal_port_file_for_default_api_url(self, mcp_db: FiligreeDB) -> None:
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None
        (mcp_mod._filigree_dir / "ephemeral.port").write_text("9229\n")
        files = _make_target_files(mcp_db, ["batch_port.py"])
        _write_scanner_toml(mcp_db)
        try:
            with patch("filigree.scanner_runtime.subprocess.Popen", return_value=_FakeProc(100)) as popen:
                data = _parse(
                    await call_tool(
                        "scan_trigger_batch",
                        {"approve_execution": True, "scanner": "test-scanner", "file_paths": ["batch_port.py"]},
                    )
                )
            assert data["status"] == "triggered"
            assert "http://localhost:9229" in popen.call_args.args[0]
            assert "http://localhost:8377" not in popen.call_args.args[0]
        finally:
            _cleanup_files(mcp_db, files)

    async def test_batch_scan_explicit_api_url_overrides_port_file(self, mcp_db: FiligreeDB) -> None:
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None
        (mcp_mod._filigree_dir / "ephemeral.port").write_text("9229\n")
        files = _make_target_files(mcp_db, ["batch_override.py"])
        _write_scanner_toml(mcp_db)
        try:
            with patch("filigree.scanner_runtime.subprocess.Popen", return_value=_FakeProc(100)) as popen:
                data = _parse(
                    await call_tool(
                        "scan_trigger_batch",
                        {
                            "approve_execution": True,
                            "scanner": "test-scanner",
                            "file_paths": ["batch_override.py"],
                            "api_url": "http://localhost:9999",
                        },
                    )
                )
            assert data["status"] == "triggered"
            assert "http://localhost:9999" in popen.call_args.args[0]
            assert "http://localhost:9229" not in popen.call_args.args[0]
        finally:
            _cleanup_files(mcp_db, files)

    async def test_batch_scan_uses_server_config_port_in_server_mode(
        self,
        mcp_db: FiligreeDB,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import filigree.mcp_server as mcp_mod
        from filigree.server import ServerConfig

        assert mcp_mod._filigree_dir is not None
        write_config(mcp_mod._filigree_dir, {"prefix": "mcp", "version": 1, "mode": "server"})
        monkeypatch.setattr("filigree.server.read_server_config", lambda: ServerConfig(port=9230))
        files = _make_target_files(mcp_db, ["batch_server.py"])
        _write_scanner_toml(mcp_db)
        try:
            with patch("filigree.scanner_runtime.subprocess.Popen", return_value=_FakeProc(100)) as popen:
                data = _parse(
                    await call_tool(
                        "scan_trigger_batch",
                        {"approve_execution": True, "scanner": "test-scanner", "file_paths": ["batch_server.py"]},
                    )
                )
            assert data["status"] == "triggered"
            assert "http://localhost:9230" in popen.call_args.args[0]
        finally:
            _cleanup_files(mcp_db, files)

    async def test_batch_scan_partial_spawn_failure(self, mcp_db: FiligreeDB) -> None:
        """When some files fail to spawn, the successful child runs survive and the failed one is recorded."""
        files = _make_target_files(mcp_db, ["batch_ok.py", "batch_fail.py"])
        _write_scanner_toml(mcp_db)
        try:
            with patch(
                "filigree.scanner_runtime.subprocess.Popen",
                side_effect=[_FakeProc(100), OSError("mock fail")],
            ):
                data = _parse(
                    await call_tool(
                        "scan_trigger_batch",
                        {"approve_execution": True, "scanner": "test-scanner", "file_paths": ["batch_ok.py", "batch_fail.py"]},
                    )
                )
            assert data["status"] == "triggered"
            assert data["processes_spawned"] == 1
            assert data["file_count"] == 1
            assert len(data["spawn_errors"]) == 1
            assert "mock fail" in data["spawn_errors"][0]["reason"].lower() or "spawn" in data["spawn_errors"][0]["reason"].lower()

            # The surviving child run exists and references the ok file.
            assert len(data["scan_run_ids"]) == 1
            run = mcp_db.get_scan_run(data["scan_run_ids"][0])
            assert len(run["file_paths"]) == 1
            assert "batch_ok.py" in run["file_paths"][0]
            assert run["status"] == "running"
        finally:
            _cleanup_files(mcp_db, files)

    async def test_batch_scan_all_spawn_failure(self, mcp_db: FiligreeDB) -> None:
        files = _make_target_files(mcp_db, ["batch_all_fail.py"])
        _write_scanner_toml(mcp_db)
        try:
            with patch(
                "filigree.scanner_runtime.subprocess.Popen",
                side_effect=OSError("mock fail"),
            ):
                data = _parse(
                    await call_tool(
                        "scan_trigger_batch",
                        {"approve_execution": True, "scanner": "test-scanner", "file_paths": ["batch_all_fail.py"]},
                    )
                )
            assert data["code"] == ErrorCode.IO
        finally:
            _cleanup_files(mcp_db, files)

    async def test_batch_scan_all_rate_limited_returns_conflict(self, mcp_db: FiligreeDB) -> None:
        files = _make_target_files(mcp_db, ["batch_reserved.py"])
        _write_scanner_toml(mcp_db)
        try:
            with patch("filigree.scanner_runtime.subprocess.Popen", return_value=_FakeProc(100)):
                first = _parse(
                    await call_tool(
                        "scan_trigger_batch",
                        {"approve_execution": True, "scanner": "test-scanner", "file_paths": ["batch_reserved.py"]},
                    )
                )
                second = _parse(
                    await call_tool(
                        "scan_trigger_batch",
                        {"approve_execution": True, "scanner": "test-scanner", "file_paths": ["batch_reserved.py"]},
                    )
                )

            assert first["status"] == "triggered"
            assert second["code"] == ErrorCode.CONFLICT
            assert second["details"]["skipped"][0]["reason"] == "rate_limited"
            assert second["details"]["skipped"][0]["blocking_run_id"] == first["scan_run_ids"][0]
            assert second["details"]["blocking_run_ids"] == [first["scan_run_ids"][0]]
        finally:
            _cleanup_files(mcp_db, files)

    async def test_batch_scan_all_immediate_failure_preserves_prior_per_file_errors(self, mcp_db: FiligreeDB) -> None:
        files = _make_target_files(mcp_db, ["batch_spawn_fails.py", "batch_exits_fast.py"])
        _write_scanner_toml(mcp_db)
        proc = MagicMock(pid=100, poll=MagicMock(return_value=9))
        try:
            with patch(
                "filigree.scanner_runtime.subprocess.Popen",
                side_effect=[OSError("spawn broken"), proc],
            ):
                data = _parse(
                    await call_tool(
                        "scan_trigger_batch",
                        {
                            "approve_execution": True,
                            "scanner": "test-scanner",
                            "file_paths": ["batch_spawn_fails.py", "missing_batch_file.py", "batch_exits_fast.py"],
                        },
                    )
                )

            assert data["code"] == ErrorCode.IO
            assert data["details"]["spawn_errors"][0]["file_path"] == "batch_spawn_fails.py"
            assert "spawn broken" in data["details"]["spawn_errors"][0]["reason"]
            assert data["details"]["skipped"][0] == {"file_path": "missing_batch_file.py", "reason": "File not found"}
        finally:
            _cleanup_files(mcp_db, files)

    async def test_batch_scan_empty_paths_rejected(self, mcp_db: FiligreeDB) -> None:
        _write_scanner_toml(mcp_db)
        data = _parse(
            await call_tool(
                "scan_trigger_batch",
                {"approve_execution": True, "scanner": "test-scanner", "file_paths": []},
            )
        )
        assert data["code"] == ErrorCode.VALIDATION

    @pytest.mark.parametrize(
        ("arguments", "expected_error"),
        [
            ({"scanner": ["bad"], "file_paths": ["batch_bad.py"]}, "scanner must be a string"),
            ({"scanner": "test-scanner", "file_paths": [123]}, "file_paths entries must be strings"),
            ({"scanner": "test-scanner", "file_paths": ["batch_bad.py"], "prompt": ["bad"]}, "prompt must be a string"),
            ({"scanner": "test-scanner", "file_paths": ["batch_bad.py"], "api_url": ["bad"]}, "api_url must be a string"),
        ],
    )
    async def test_batch_scan_rejects_malformed_argument_types(
        self,
        mcp_db: FiligreeDB,
        arguments: dict[str, object],
        expected_error: str,
    ) -> None:
        _write_scanner_toml(mcp_db)

        data = _parse(await call_tool("scan_trigger_batch", arguments))

        assert data["code"] == ErrorCode.VALIDATION
        assert data["error"] == expected_error

    async def test_batch_scan_scanner_not_found(self, mcp_db: FiligreeDB) -> None:
        data = _parse(
            await call_tool(
                "scan_trigger_batch",
                {"approve_execution": True, "scanner": "nonexistent", "file_paths": ["foo.py"]},
            )
        )
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_batch_scan_non_localhost_rejected(self, mcp_db: FiligreeDB) -> None:
        files = _make_target_files(mcp_db, ["batch_url.py"])
        _write_scanner_toml(mcp_db)
        try:
            data = _parse(
                await call_tool(
                    "scan_trigger_batch",
                    {
                        "approve_execution": True,
                        "scanner": "test-scanner",
                        "file_paths": ["batch_url.py"],
                        "api_url": "https://evil.example.com",
                    },
                )
            )
            assert data["code"] == ErrorCode.INVALID_API_URL
        finally:
            _cleanup_files(mcp_db, files)

    async def test_batch_scan_skips_invalid_and_missing_files(self, mcp_db: FiligreeDB) -> None:
        files = _make_target_files(mcp_db, ["batch_valid.py"])
        _write_scanner_toml(mcp_db)
        try:
            with patch(
                "filigree.scanner_runtime.subprocess.Popen",
                return_value=_FakeProc(100),
            ):
                data = _parse(
                    await call_tool(
                        "scan_trigger_batch",
                        {
                            "approve_execution": True,
                            "scanner": "test-scanner",
                            "file_paths": ["batch_valid.py", "nonexistent.py", "../../etc/passwd"],
                        },
                    )
                )
            assert data["status"] == "triggered"
            assert data["file_count"] == 1
            assert len(data["skipped"]) == 2
        finally:
            _cleanup_files(mcp_db, files)

    async def test_batch_scan_per_file_log_files(self, mcp_db: FiligreeDB) -> None:
        """Each file in a batch gets its own log file (no clobbering)."""
        import filigree.mcp_server as mcp_mod

        files = _make_target_files(mcp_db, ["batch_log_a.py", "batch_log_b.py"])
        _write_scanner_toml(mcp_db)
        try:
            with patch(
                "filigree.scanner_runtime.subprocess.Popen",
                side_effect=[_FakeProc(100), _FakeProc(101)],
            ):
                data = _parse(
                    await call_tool(
                        "scan_trigger_batch",
                        {"approve_execution": True, "scanner": "test-scanner", "file_paths": ["batch_log_a.py", "batch_log_b.py"]},
                    )
                )
            batch_id = data["batch_id"]
            assert mcp_mod._filigree_dir is not None
            scan_log_dir = mcp_mod._filigree_dir / "scans"
            # Should have per-file log files, not a single shared one
            log_files = sorted(scan_log_dir.glob(f"{batch_id}*.log"))
            assert len(log_files) == 2
            assert any("-0.log" in str(f) for f in log_files)
            assert any("-1.log" in str(f) for f in log_files)
        finally:
            _cleanup_files(mcp_db, files)

    async def test_batch_scan_children_have_independent_run_ids(self, mcp_db: FiligreeDB) -> None:
        """Regression for filigree-ec33df4b86: each child gets its own scan_run_id
        so the fastest child's completion POST can't finalize the batch early."""
        files = _make_target_files(mcp_db, ["indep_a.py", "indep_b.py", "indep_c.py"])
        _write_scanner_toml(mcp_db)
        try:
            with patch(
                "filigree.scanner_runtime.subprocess.Popen",
                side_effect=[_FakeProc(100), _FakeProc(101), _FakeProc(102)],
            ):
                data = _parse(
                    await call_tool(
                        "scan_trigger_batch",
                        {"approve_execution": True, "scanner": "test-scanner", "file_paths": ["indep_a.py", "indep_b.py", "indep_c.py"]},
                    )
                )
            assert data["file_count"] == 3
            ids = data["scan_run_ids"]
            assert len(ids) == 3 == len(set(ids))
            # Each child's scan_run holds exactly one file_path — no shared row.
            for child_id in ids:
                run = mcp_db.get_scan_run(child_id)
                assert len(run["file_paths"]) == 1
                assert run["pid"] is not None
        finally:
            _cleanup_files(mcp_db, files)

    async def test_batch_scan_dedupes_repeated_file_paths(self, mcp_db: FiligreeDB) -> None:
        """Repeated file_paths in one request are deduped before reservation."""
        files = _make_target_files(mcp_db, ["dup_a.py"])
        _write_scanner_toml(mcp_db)
        try:
            with patch(
                "filigree.scanner_runtime.subprocess.Popen",
                side_effect=[_FakeProc(100)],
            ):
                data = _parse(
                    await call_tool(
                        "scan_trigger_batch",
                        {"approve_execution": True, "scanner": "test-scanner", "file_paths": ["dup_a.py", "dup_a.py"]},
                    )
                )
            assert data["file_count"] == 1
            assert any(s["reason"] == "duplicate" for s in data.get("skipped", []))
        finally:
            _cleanup_files(mcp_db, files)


class TestProcessScanResultsCompletion:
    """Test that process_scan_results auto-completes scan runs (#11)."""

    def test_scan_run_marked_completed(self, mcp_db: FiligreeDB) -> None:
        """When scan_run_id is provided, the scan run should transition to completed."""
        mcp_db.register_file("src/a.py")
        mcp_db.create_scan_run(
            scan_run_id="ingest-run",
            scanner_name="scanner",
            scan_source="scanner",
            file_paths=["src/a.py"],
            file_ids=["fid-1"],
        )
        mcp_db.update_scan_run_status("ingest-run", "running")

        mcp_db.process_scan_results(
            scan_source="scanner",
            scan_run_id="ingest-run",
            findings=[
                {"path": "src/a.py", "rule_id": "r1", "severity": "info", "message": "m1"},
            ],
        )

        run = mcp_db.get_scan_run("ingest-run")
        assert run["status"] == "completed"
        assert run["findings_count"] == 1

    def test_scan_run_completion_failure_does_not_lose_findings(self, mcp_db: FiligreeDB) -> None:
        """If scan run completion fails, findings should still be ingested."""
        mcp_db.register_file("src/b.py")

        # Use a non-existent scan_run_id — completion will fail but findings should persist
        result = mcp_db.process_scan_results(
            scan_source="scanner",
            scan_run_id="nonexistent-run",
            findings=[
                {"path": "src/b.py", "rule_id": "r1", "severity": "info", "message": "m1"},
            ],
        )

        assert result["findings_created"] == 1
        assert len(result["new_finding_ids"]) == 1


class TestValidateLocalhostUrl:
    """Edge-case coverage for the _validate_localhost_url security boundary."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8377/api/v1/scan-results",
            "http://127.0.0.1:8377/api/v1/scan-results",
            "http://[::1]:8377/api/v1/scan-results",
            "http://localhost/path",
        ],
    )
    def test_localhost_urls_accepted(self, url: str) -> None:
        assert _validate_localhost_url(url) is None

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.example.com/api",
            "http://localhost.evil.com/api",
            "http://192.168.1.1:8377/api",
            "http://10.0.0.1/api",
        ],
    )
    def test_non_localhost_urls_rejected(self, url: str) -> None:
        result = _validate_localhost_url(url)
        assert result is not None
        # Should be an ErrorResponse dict
        assert isinstance(result, dict)
        assert "error" in result
        assert "code" in result

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "   ",
            "no-scheme",
            "localhost:8377",
            "ftp://localhost/api",
            "file:///etc/passwd",
            "//localhost/api",
        ],
    )
    def test_unusable_urls_rejected(self, url: str) -> None:
        """Empty, scheme-less, or non-HTTP URLs must be rejected — the scanner
        helper can't build a usable POST target from them."""
        result = _validate_localhost_url(url)
        assert result is not None
        assert isinstance(result, dict)
        assert "error" in result
        assert "code" in result


class TestSpawnScanLogFileFailure:
    """_spawn_scan handles log file creation failure gracefully."""

    async def test_log_file_open_failure_still_spawns(self, mcp_db: FiligreeDB) -> None:
        files = _make_target_files(mcp_db, ["log_fail_target.py"])
        _write_scanner_toml(mcp_db)
        try:
            real_open = open

            def mock_open_fail(path, *a, **kw):
                if "scans" in str(path) and str(path).endswith(".log"):
                    raise OSError("disk full")
                return real_open(path, *a, **kw)

            with (
                patch("filigree.scanner_runtime.subprocess.Popen", return_value=_FakeProc(100)),
                patch("builtins.open", side_effect=mock_open_fail),
            ):
                data = _parse(
                    await call_tool(
                        "scan_trigger_batch",
                        {"approve_execution": True, "scanner": "test-scanner", "file_paths": ["log_fail_target.py"]},
                    )
                )
            assert data["status"] == "triggered"
            assert "log_warning" in data or any("log" in str(w).lower() for w in data.get("warnings", []))
        finally:
            _cleanup_files(mcp_db, files)


class TestBatchScanDbTrackingFailure:
    """trigger_scan_batch handles DB failures without leaking orphan processes."""

    async def test_reservation_failure_produces_no_eligible_files(self, mcp_db: FiligreeDB) -> None:
        """When reserve_scan_run itself fails, no process is ever spawned — so there's
        nothing to clean up, but we should surface the failure reason."""
        files = _make_target_files(mcp_db, ["db_fail_a.py", "db_fail_b.py"])
        _write_scanner_toml(mcp_db)
        try:
            with (
                patch("filigree.scanner_runtime.subprocess.Popen") as popen_mock,
                patch.object(mcp_db, "reserve_scan_run", side_effect=sqlite3.OperationalError("DB broken")),
            ):
                data = _parse(
                    await call_tool(
                        "scan_trigger_batch",
                        {"approve_execution": True, "scanner": "test-scanner", "file_paths": ["db_fail_a.py", "db_fail_b.py"]},
                    )
                )
            # No files eligible (all reservations failed); no processes spawned.
            assert data["code"] == ErrorCode.VALIDATION
            popen_mock.assert_not_called()
            # Extras are in details under the 2.0 envelope shape.
            skipped = data["details"]["skipped"]
            assert all("reservation_failed" in s["reason"] for s in skipped)
        finally:
            _cleanup_files(mcp_db, files)

    async def test_backfill_failure_kills_process(self, mcp_db: FiligreeDB) -> None:
        """If the post-spawn pid/log backfill fails, the spawned child is killed."""
        files = _make_target_files(mcp_db, ["backfill_fail.py"])
        _write_scanner_toml(mcp_db)
        proc = MagicMock(pid=100, poll=MagicMock(return_value=None))
        try:
            with (
                patch("filigree.scanner_runtime.subprocess.Popen", return_value=proc),
                patch.object(mcp_db, "set_scan_run_spawn_info", side_effect=sqlite3.OperationalError("DB broken")),
            ):
                data = _parse(
                    await call_tool(
                        "scan_trigger_batch",
                        {"approve_execution": True, "scanner": "test-scanner", "file_paths": ["backfill_fail.py"]},
                    )
                )
            assert data["code"] == ErrorCode.IO
            proc.kill.assert_called_once()
            row = mcp_db.conn.execute("SELECT status, error_message FROM scan_runs").fetchone()
            assert row is not None
            assert row["status"] == "failed"
            assert "DB tracking failed" in row["error_message"]
            assert mcp_db.check_scan_cooldown("test-scanner", "backfill_fail.py") is None
        finally:
            _cleanup_files(mcp_db, files)

    async def test_spawn_failure_status_update_failure_is_reported(self, mcp_db: FiligreeDB) -> None:
        files = _make_target_files(mcp_db, ["spawn_status_fail.py"])
        _write_scanner_toml(mcp_db)
        original_update = mcp_db.update_scan_run_status

        def fail_failed_status(scan_run_id: str, status: str, **kwargs: object) -> dict[str, object]:
            if status == "failed":
                raise sqlite3.OperationalError("status update broken")
            return original_update(scan_run_id, status, **kwargs)

        try:
            with (
                patch("filigree.scanner_runtime.subprocess.Popen", side_effect=OSError("spawn broken")),
                patch.object(mcp_db, "update_scan_run_status", side_effect=fail_failed_status),
            ):
                data = _parse(
                    await call_tool(
                        "scan_trigger_batch",
                        {"approve_execution": True, "scanner": "test-scanner", "file_paths": ["spawn_status_fail.py"]},
                    )
                )

            assert data["code"] == ErrorCode.IO
            assert data["details"]["spawn_errors"][0]["status_update_error"] == "status update broken"
        finally:
            _cleanup_files(mcp_db, files)

    async def test_immediate_batch_failure_status_update_failure_is_reported(self, mcp_db: FiligreeDB) -> None:
        files = _make_target_files(mcp_db, ["batch_immediate_status_fail.py"])
        _write_scanner_toml(mcp_db)
        proc = MagicMock(pid=100, poll=MagicMock(return_value=9))
        original_update = mcp_db.update_scan_run_status

        def fail_failed_status(scan_run_id: str, status: str, **kwargs: object) -> dict[str, object]:
            if status == "failed":
                raise sqlite3.OperationalError("status update broken")
            return original_update(scan_run_id, status, **kwargs)

        try:
            with (
                patch("filigree.scanner_runtime.subprocess.Popen", return_value=proc),
                patch.object(mcp_db, "update_scan_run_status", side_effect=fail_failed_status),
            ):
                data = _parse(
                    await call_tool(
                        "scan_trigger_batch",
                        {"approve_execution": True, "scanner": "test-scanner", "file_paths": ["batch_immediate_status_fail.py"]},
                    )
                )

            assert data["code"] == ErrorCode.IO
            assert data["details"]["status_update_errors"][0]["error"] == "status update broken"
        finally:
            _cleanup_files(mcp_db, files)


class TestTriggerScanCooldownReservation:
    """Regression tests for filigree-ed3be5a092: cooldown is reserved pre-spawn."""

    async def test_trigger_scan_registry_unavailable_returns_error_response(self, mcp_db: FiligreeDB) -> None:
        class UnavailableRegistry:
            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                raise RegistryUnavailableError(
                    "Loomweave registry unavailable for test",
                    url="http://loomweave.test/api/v1/files?path=trigger_registry.py",
                    path=path,
                    cause_kind="network",
                )

            def is_displaced(self) -> bool:
                return False

        files = _make_target_files(mcp_db, ["trigger_registry.py"])
        _write_scanner_toml(mcp_db)
        mcp_db.registry = UnavailableRegistry()
        try:
            data = _parse(
                await call_tool("scan_trigger", {"approve_execution": True, "scanner": "test-scanner", "file_path": "trigger_registry.py"})
            )

            assert data["code"] == ErrorCode.REGISTRY_UNAVAILABLE
            assert data["details"]["cause"] == "registry_unavailable"
            assert data["details"]["cause_kind"] == "network"
            assert data["details"]["path"] == "trigger_registry.py"
            assert data["details"]["url"] == "http://loomweave.test/api/v1/files?path=trigger_registry.py"
        finally:
            _cleanup_files(mcp_db, files)

    async def test_trigger_scan_uses_registry_resolved_file_id(self, mcp_db: FiligreeDB) -> None:
        files = _make_target_files(mcp_db, ["trigger_registry.py"])
        _write_scanner_toml(mcp_db)
        mcp_db.registry = PathRegistry()
        try:
            with patch("filigree.scanner_runtime.subprocess.Popen", return_value=_FakeProc(100)):
                data = _parse(
                    await call_tool(
                        "scan_trigger",
                        {"approve_execution": True, "scanner": "test-scanner", "file_path": "trigger_registry.py"},
                    )
                )

            assert data["file_id"] == "core:file:trigger_registry.py"
            run = mcp_db.get_scan_run(data["scan_run_id"])
            assert run["file_ids"] == ["core:file:trigger_registry.py"]
        finally:
            _cleanup_files(mcp_db, files)

    async def test_trigger_scan_custom_scanner_requires_explicit_execution_approval(self, mcp_db: FiligreeDB) -> None:
        files = _make_target_files(mcp_db, ["trigger_unapproved.py"])
        _write_scanner_toml(mcp_db)
        try:
            with patch("filigree.scanner_runtime.subprocess.Popen") as popen:
                data = _parse(
                    await call_tool(
                        "scan_trigger",
                        {"scanner": "test-scanner", "file_path": "trigger_unapproved.py"},
                    )
                )
            assert data["code"] == ErrorCode.VALIDATION
            assert "approve_execution=true" in data["error"]
            popen.assert_not_called()
        finally:
            _cleanup_files(mcp_db, files)

    async def test_second_trigger_blocked_by_pending_reservation(self, mcp_db: FiligreeDB) -> None:
        """Trigger #1 leaves a pending reservation row; trigger #2 should see it and
        return rate_limited rather than silently spawning a duplicate scanner."""
        files = _make_target_files(mcp_db, ["reserve_target.py"])
        _write_scanner_toml(mcp_db)
        try:
            with patch("filigree.scanner_runtime.subprocess.Popen", return_value=_FakeProc(100)):
                first = _parse(
                    await call_tool(
                        "scan_trigger",
                        {"approve_execution": True, "scanner": "test-scanner", "file_path": "reserve_target.py"},
                    )
                )
                assert first["status"] == "triggered"
                assert first["api_url_source"] in {"fallback_default", "ephemeral_port", "server_config"}
                assert first["api_url"].startswith("http://localhost:")
                assert first["prompt_pack_scope"] == "advisory"
                assert first["preview_recommended"] is True
                assert first["sandbox_class"] == "custom"
                assert "repository files" in first["risk_summary"]
                # Second call immediately after — the reservation row should block it
                # regardless of whether the first process has finished.
                second = _parse(
                    await call_tool(
                        "scan_trigger",
                        {"approve_execution": True, "scanner": "test-scanner", "file_path": "reserve_target.py"},
                    )
                )
            assert second["code"] == ErrorCode.CONFLICT
            assert second["details"]["blocking_run_id"] == first["scan_run_id"]
        finally:
            _cleanup_files(mcp_db, files)

    async def test_trigger_scan_rejects_prompt_when_scanner_template_cannot_accept_it(self, mcp_db: FiligreeDB) -> None:
        files = _make_target_files(mcp_db, ["trigger_no_prompt.py"])
        _write_scanner_toml(mcp_db)
        try:
            data = _parse(
                await call_tool(
                    "scan_trigger",
                    {"approve_execution": True, "scanner": "test-scanner", "file_path": "trigger_no_prompt.py", "prompt": "security"},
                )
            )
            assert data["code"] == ErrorCode.VALIDATION
            assert "does not accept prompt packs" in data["error"]
        finally:
            _cleanup_files(mcp_db, files)

    @pytest.mark.parametrize(
        ("arguments", "expected_error"),
        [
            ({"file_path": "trigger_bad.py"}, "scanner is required"),
            ({"scanner": "test-scanner"}, "file_path is required"),
            ({"scanner": ["bad"], "file_path": "trigger_bad.py"}, "scanner must be a string"),
            ({"scanner": "test-scanner", "file_path": 123}, "file_path must be a string"),
            ({"scanner": "test-scanner", "file_path": "trigger_bad.py", "prompt": ["bad"]}, "prompt must be a string"),
            ({"scanner": "test-scanner", "file_path": "trigger_bad.py", "api_url": ["bad"]}, "api_url must be a string"),
        ],
    )
    async def test_trigger_scan_rejects_malformed_argument_types(
        self,
        mcp_db: FiligreeDB,
        arguments: dict[str, object],
        expected_error: str,
    ) -> None:
        _write_scanner_toml(mcp_db)

        data = _parse(await call_tool("scan_trigger", arguments))

        assert data["code"] == ErrorCode.VALIDATION
        assert data["error"] == expected_error

    async def test_spawn_failure_marks_reservation_failed(self, mcp_db: FiligreeDB) -> None:
        """If the scanner fails to spawn, the reservation is transitioned to
        'failed' so cooldown doesn't keep blocking retries."""
        files = _make_target_files(mcp_db, ["spawn_fail_target.py"])
        _write_scanner_toml(mcp_db)
        try:
            with patch(
                "filigree.scanner_runtime.subprocess.Popen",
                side_effect=OSError("can't fork"),
            ):
                data = _parse(
                    await call_tool(
                        "scan_trigger",
                        {"approve_execution": True, "scanner": "test-scanner", "file_path": "spawn_fail_target.py"},
                    )
                )
            assert data["code"] == ErrorCode.IO
            # The cooldown query should no longer find a blocking run — failed
            # rows are excluded from the cooldown window.
            assert mcp_db.check_scan_cooldown("test-scanner", "spawn_fail_target.py") is None
        finally:
            _cleanup_files(mcp_db, files)

    async def test_trigger_scan_backfill_failure_marks_reservation_failed(self, mcp_db: FiligreeDB) -> None:
        files = _make_target_files(mcp_db, ["single_backfill_fail.py"])
        _write_scanner_toml(mcp_db)
        proc = MagicMock(pid=100, poll=MagicMock(return_value=None))
        try:
            with (
                patch("filigree.scanner_runtime.subprocess.Popen", return_value=proc),
                patch.object(mcp_db, "set_scan_run_spawn_info", side_effect=sqlite3.OperationalError("DB broken")),
            ):
                data = _parse(
                    await call_tool(
                        "scan_trigger",
                        {"approve_execution": True, "scanner": "test-scanner", "file_path": "single_backfill_fail.py"},
                    )
                )

            assert data["code"] == ErrorCode.IO
            proc.kill.assert_called_once()
            row = mcp_db.conn.execute("SELECT status, error_message FROM scan_runs").fetchone()
            assert row is not None
            assert row["status"] == "failed"
            assert "DB tracking failed" in row["error_message"]
            assert mcp_db.check_scan_cooldown("test-scanner", "single_backfill_fail.py") is None
        finally:
            _cleanup_files(mcp_db, files)

    async def test_immediate_exit_status_update_failure_preserves_scanner_error(self, mcp_db: FiligreeDB) -> None:
        files = _make_target_files(mcp_db, ["immediate_status_fail.py"])
        _write_scanner_toml(mcp_db)
        proc = MagicMock(pid=100, poll=MagicMock(return_value=7))
        original_update = mcp_db.update_scan_run_status

        def fail_failed_status(scan_run_id: str, status: str, **kwargs: object) -> dict[str, object]:
            if status == "failed":
                raise sqlite3.OperationalError("status update broken")
            return original_update(scan_run_id, status, **kwargs)

        try:
            with (
                patch("filigree.scanner_runtime.subprocess.Popen", return_value=proc),
                patch.object(mcp_db, "update_scan_run_status", side_effect=fail_failed_status),
            ):
                data = _parse(
                    await call_tool(
                        "scan_trigger",
                        {"approve_execution": True, "scanner": "test-scanner", "file_path": "immediate_status_fail.py"},
                    )
                )

            assert data["code"] == ErrorCode.IO
            assert "Scanner process exited immediately with code 7" in data["error"]
            assert data["details"]["status_update_error"] == "status update broken"
        finally:
            _cleanup_files(mcp_db, files)
