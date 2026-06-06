"""Tests for install_support/doctor.py — health check system."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from filigree.core import (
    CONF_FILENAME,
    CONFIG_FILENAME,
    DB_FILENAME,
    FILIGREE_DIR_NAME,
    SUMMARY_FILENAME,
    FiligreeDB,
    write_conf,
    write_config,
)
from filigree.db_schema import CURRENT_SCHEMA_VERSION
from filigree.install_support import (
    FILIGREE_INSTRUCTIONS_MARKER,
    SKILL_MARKER,
    SKILL_NAME,
)
from filigree.install_support.doctor import (
    CheckResult,
    _doctor_ethereal_checks,
    _doctor_install_method,
    _doctor_mcp_token_result,
    _doctor_server_checks,
    _find_all_filigree_binaries,
    _is_absolute_command_path,
    _is_venv_binary,
    _unresolved_env_refs,
    doctor_check_id,
    run_doctor,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(tmp_path: Path, *, with_db: bool = True, with_config: bool = True, with_summary: bool = True) -> Path:
    """Create a minimal filigree project under tmp_path. Returns project root."""
    filigree_dir = tmp_path / FILIGREE_DIR_NAME
    filigree_dir.mkdir()

    if with_config:
        write_config(filigree_dir, {"prefix": "tst", "version": 1})

    if with_db:
        d = FiligreeDB(filigree_dir / DB_FILENAME, prefix="tst")
        d.initialize()
        d.close()

    if with_summary:
        (filigree_dir / SUMMARY_FILENAME).write_text("# summary\n")

    return tmp_path


# ---------------------------------------------------------------------------
# CheckResult
# ---------------------------------------------------------------------------


class TestCheckResult:
    def test_passed_construction(self) -> None:
        r = CheckResult(name="My check", passed=True, message="All good")
        assert r.name == "My check"
        assert r.passed is True
        assert r.message == "All good"
        assert r.fix_hint == ""

    def test_failed_construction(self) -> None:
        r = CheckResult(name="My check", passed=False, message="Broken", fix_hint="Run: fix it")
        assert r.passed is False
        assert r.fix_hint == "Run: fix it"

    def test_icon_pass(self) -> None:
        r = CheckResult(name="x", passed=True, message="ok")
        assert r.icon == "OK"

    def test_icon_fail(self) -> None:
        r = CheckResult(name="x", passed=False, message="bad")
        assert r.icon == "!!"

    def test_icon_only_two_values(self) -> None:
        """icon must be one of two strings — no other values allowed."""
        assert CheckResult("a", True, "").icon in {"OK", "!!"}
        assert CheckResult("a", False, "").icon in {"OK", "!!"}


# ---------------------------------------------------------------------------
# _is_venv_binary
# ---------------------------------------------------------------------------


class TestIsVenvBinary:
    def test_path_inside_venv(self, tmp_path: Path) -> None:
        venv = tmp_path / "myenv"
        bin_dir = venv / "bin"
        bin_dir.mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("[pyvenv]\n")
        exe = bin_dir / "python"
        exe.write_text("")
        assert _is_venv_binary(str(exe)) is True

    def test_path_not_inside_venv(self, tmp_path: Path) -> None:
        # No pyvenv.cfg anywhere in parents
        exe = tmp_path / "usr" / "bin" / "python"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        assert _is_venv_binary(str(exe)) is False

    def test_pyvenv_cfg_several_levels_up(self, tmp_path: Path) -> None:
        # pyvenv.cfg is at grandparent
        venv = tmp_path / "venv"
        deep = venv / "lib" / "python3.11" / "site-packages"
        deep.mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("[pyvenv]\n")
        target = deep / "somefile.py"
        target.write_text("")
        assert _is_venv_binary(str(target)) is True

    def test_nonexistent_path_no_venv_cfg(self, tmp_path: Path) -> None:
        # Path doesn't exist; no pyvenv.cfg in tree — should be False
        fake = tmp_path / "nosuchdir" / "python"
        assert _is_venv_binary(str(fake)) is False

    def test_root_level_binary(self) -> None:
        # /usr/bin/python — no pyvenv.cfg up to root
        assert _is_venv_binary("/usr/bin/python") is False


# ---------------------------------------------------------------------------
# _is_absolute_command_path
# ---------------------------------------------------------------------------


class TestIsAbsoluteCommandPath:
    def test_empty_string(self) -> None:
        assert _is_absolute_command_path("") is False

    def test_unix_absolute(self) -> None:
        assert _is_absolute_command_path("/usr/bin/python") is True

    def test_unix_home_relative(self) -> None:
        # ~ is not absolute — it's not expanded here
        assert _is_absolute_command_path("~/bin/filigree") is False

    def test_relative_path(self) -> None:
        assert _is_absolute_command_path("./filigree") is False

    def test_bare_command(self) -> None:
        assert _is_absolute_command_path("filigree") is False

    def test_windows_unc_path(self) -> None:
        assert _is_absolute_command_path("\\\\server\\share\\bin\\filigree") is True

    def test_windows_drive_letter_forward_slash(self) -> None:
        assert _is_absolute_command_path("C:/Users/user/bin/filigree") is True

    def test_windows_drive_letter_backslash(self) -> None:
        assert _is_absolute_command_path("C:\\Users\\user\\bin\\filigree") is True

    def test_windows_drive_letter_too_short(self) -> None:
        # "C:" alone has no separator at index 2
        assert _is_absolute_command_path("C:") is False

    def test_non_alpha_drive_letter(self) -> None:
        # Digit at position 0 shouldn't be treated as Windows drive
        assert _is_absolute_command_path("1:/path") is False


# ---------------------------------------------------------------------------
# run_doctor — .filigree/ directory check
# ---------------------------------------------------------------------------


class TestDoctorFiligreeDir:
    def test_missing_filigree_dir_returns_immediately(self, tmp_path: Path) -> None:
        results = run_doctor(tmp_path)
        assert len(results) == 1
        r = results[0]
        assert ".filigree/" in r.name
        assert r.passed is False
        assert "filigree init" in r.fix_hint

    def test_filigree_dir_present(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        results = run_doctor(tmp_path)
        first = results[0]
        assert ".filigree/" in first.name
        assert first.passed is True

    def test_filigree_dir_found_in_parent(self, tmp_path: Path) -> None:
        """Doctor should walk up to find .filigree/ when not in cwd."""
        _make_project(tmp_path)
        subdir = tmp_path / "subproject"
        subdir.mkdir()
        results = run_doctor(subdir)
        first = results[0]
        assert first.passed is True

    def test_foreign_database_is_reported_with_specific_message(self, tmp_path: Path) -> None:
        """Doctor must surface the full ForeignDatabaseError message — not
        collapse it into the generic "No .filigree/ found" line — so agents
        and users see *why* we refused and what to do next.
        """
        _make_project(tmp_path)  # outer project has .filigree/ and .filigree.conf
        inner = tmp_path / "inner-repo"
        inner.mkdir()
        (inner / ".git").mkdir()  # inner is a separate git repo, no anchor of its own

        results = run_doctor(inner)
        assert len(results) == 1
        r = results[0]
        assert r.passed is False
        assert "Refusing to latch" in r.message
        assert "filigree init" in r.fix_hint
        assert str(inner.resolve()) in r.fix_hint


# ---------------------------------------------------------------------------
# run_doctor — config.json check
# ---------------------------------------------------------------------------


class TestDoctorConfigJson:
    def test_valid_config(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        results = run_doctor(tmp_path)
        config_result = next(r for r in results if r.name == "config.json")
        assert config_result.passed is True
        assert "tst" in config_result.message

    def test_missing_config(self, tmp_path: Path) -> None:
        _make_project(tmp_path, with_config=False)
        results = run_doctor(tmp_path)
        config_result = next(r for r in results if r.name == "config.json")
        assert config_result.passed is False
        assert "Missing" in config_result.message
        assert "filigree init" in config_result.fix_hint

    def test_corrupt_json_config(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        config_path = tmp_path / FILIGREE_DIR_NAME / CONFIG_FILENAME
        config_path.write_text("{not valid json")
        results = run_doctor(tmp_path)
        config_result = next(r for r in results if r.name == "config.json")
        assert config_result.passed is False
        assert "Invalid JSON" in config_result.message

    def test_config_not_object(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        config_path = tmp_path / FILIGREE_DIR_NAME / CONFIG_FILENAME
        config_path.write_text("[1, 2, 3]")
        results = run_doctor(tmp_path)
        config_result = next(r for r in results if r.name == "config.json")
        assert config_result.passed is False
        assert "object" in config_result.message.lower()

    def test_config_directory_reports_failure(self, tmp_path: Path) -> None:
        _make_project(tmp_path, with_config=False)
        config_path = tmp_path / FILIGREE_DIR_NAME / CONFIG_FILENAME
        config_path.mkdir()

        results = run_doctor(tmp_path)

        config_result = next(r for r in results if r.name == "config.json")
        assert config_result.passed is False
        assert "unreadable" in config_result.message.lower()
        assert "config.json" in config_result.fix_hint


# ---------------------------------------------------------------------------
# run_doctor — filigree.db check
# ---------------------------------------------------------------------------


class TestDoctorDatabase:
    def test_valid_db(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        results = run_doctor(tmp_path)
        db_result = next(r for r in results if r.name == "filigree.db")
        assert db_result.passed is True
        assert "issues" in db_result.message

    def test_missing_db(self, tmp_path: Path) -> None:
        _make_project(tmp_path, with_db=False)
        results = run_doctor(tmp_path)
        db_result = next(r for r in results if r.name == "filigree.db")
        assert db_result.passed is False
        assert "Missing" in db_result.message
        assert "filigree init" in db_result.fix_hint

    def test_corrupt_db(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        db_path = tmp_path / FILIGREE_DIR_NAME / DB_FILENAME
        db_path.write_bytes(b"this is not sqlite")
        results = run_doctor(tmp_path)
        db_result = next(r for r in results if r.name == "filigree.db")
        assert db_result.passed is False
        # Either the schema-version probe fails first (post-PR-#33 ordering)
        # or a later table query fails — both produce a corrupted-DB hint.
        assert "error" in db_result.message.lower() or "schema version" in db_result.message.lower()
        assert "corrupted" in (db_result.fix_hint or "").lower()

    def test_schema_version_current(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        results = run_doctor(tmp_path)
        schema_result = next(r for r in results if r.name == "Schema version")
        assert schema_result.passed is True
        assert str(CURRENT_SCHEMA_VERSION) in schema_result.message

    def test_schema_version_too_old(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        db_path = tmp_path / FILIGREE_DIR_NAME / DB_FILENAME
        conn = sqlite3.connect(str(db_path))
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION - 1}")
        conn.commit()
        conn.close()
        results = run_doctor(tmp_path)
        schema_result = next(r for r in results if r.name == "Schema version")
        assert schema_result.passed is False
        assert "outdated" in schema_result.fix_hint.lower() or "filigree doctor" in schema_result.fix_hint

    def test_schema_version_too_new(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        db_path = tmp_path / FILIGREE_DIR_NAME / DB_FILENAME
        conn = sqlite3.connect(str(db_path))
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
        conn.commit()
        conn.close()
        results = run_doctor(tmp_path)
        schema_result = next(r for r in results if r.name == "Schema version")
        assert schema_result.passed is False
        assert "newer" in schema_result.fix_hint.lower() or "Upgrade" in schema_result.fix_hint

    def test_loomweave_configured_project_warns_on_local_file_records(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        db_path = tmp_path / FILIGREE_DIR_NAME / DB_FILENAME
        db = FiligreeDB(db_path, prefix="tst")
        try:
            db.initialize()
            db.register_file("src/legacy.py")
        finally:
            db.close()
        write_conf(
            tmp_path / CONF_FILENAME,
            {
                "version": 1,
                "project_name": "tst",
                "prefix": "tst",
                "db": ".filigree/filigree.db",
                "registry_backend": "loomweave",
                "loomweave": {"base_url": "http://loomweave.test"},
            },
        )

        results = run_doctor(tmp_path)

        registry_result = next(r for r in results if r.name == "File registry backend state")
        assert registry_result.passed is False
        assert "configured for Loomweave" in registry_result.message
        assert "1 file_records row(s)" in registry_result.message
        assert "migrate-registry --to loomweave" in registry_result.fix_hint


class TestDoctorHonorsConfDbPath:
    """Bug filigree-3572d3b273: run_doctor must resolve the DB path from
    ``.filigree.conf`` when one exists, not hardcode ``.filigree/filigree.db``.

    Custom relocations like ``db = "storage/track.db"`` are explicitly
    supported by ``FiligreeDB.from_conf`` and ``filigree init``; doctor
    cannot silently inspect the wrong file (or report a false "missing DB").
    """

    def test_honors_custom_db_path_from_conf(self, tmp_path: Path) -> None:
        from filigree.core import CONF_FILENAME, write_conf

        # Build a project whose DB lives at storage/track.db, not .filigree/filigree.db
        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        storage_dir = tmp_path / "storage"
        storage_dir.mkdir()
        custom_db = storage_dir / "track.db"

        write_config(filigree_dir, {"prefix": "tst", "version": 1})
        (filigree_dir / SUMMARY_FILENAME).write_text("# summary\n")

        db = FiligreeDB(custom_db, prefix="tst")
        db.initialize()
        db.close()

        write_conf(
            tmp_path / CONF_FILENAME,
            {"version": 1, "project_name": "tst", "prefix": "tst", "db": "storage/track.db"},
        )

        results = run_doctor(tmp_path)
        db_result = next(r for r in results if r.name == "filigree.db")
        assert db_result.passed is True, f"doctor did not find DB at {custom_db}: {db_result.message}"

    def test_reports_missing_custom_db(self, tmp_path: Path) -> None:
        """If conf declares a DB path that doesn't exist, doctor reports missing."""
        from filigree.core import CONF_FILENAME, write_conf

        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        write_config(filigree_dir, {"prefix": "tst", "version": 1})
        (filigree_dir / SUMMARY_FILENAME).write_text("# summary\n")
        # Conf points at a DB that was never created
        write_conf(
            tmp_path / CONF_FILENAME,
            {"version": 1, "project_name": "tst", "prefix": "tst", "db": "storage/track.db"},
        )

        results = run_doctor(tmp_path)
        db_result = next(r for r in results if r.name == "filigree.db")
        assert db_result.passed is False
        assert "Missing" in db_result.message

    def test_falls_back_to_legacy_layout_without_conf(self, tmp_path: Path) -> None:
        """Legacy installs without .filigree.conf must keep working."""
        _make_project(tmp_path)  # no conf written
        results = run_doctor(tmp_path)
        db_result = next(r for r in results if r.name == "filigree.db")
        assert db_result.passed is True

    def test_unreadable_conf_surfaces_but_falls_back(self, tmp_path: Path) -> None:
        """A corrupt conf must surface as a check failure, but not block the DB check."""
        from filigree.core import CONF_FILENAME

        _make_project(tmp_path)
        (tmp_path / CONF_FILENAME).write_text("not json at all {")
        results = run_doctor(tmp_path)
        anchor_result = next(r for r in results if r.name == ".filigree.conf anchor")
        assert anchor_result.passed is False
        # DB check should still report the legacy DB as fine
        db_result = next(r for r in results if r.name == "filigree.db")
        assert db_result.passed is True


# ---------------------------------------------------------------------------
# run_doctor — context.md freshness check
# ---------------------------------------------------------------------------


class TestDoctorContextMd:
    def test_fresh_summary(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        results = run_doctor(tmp_path)
        ctx_result = next(r for r in results if r.name == "context.md")
        assert ctx_result.passed is True
        assert "Fresh" in ctx_result.message

    def test_missing_summary(self, tmp_path: Path) -> None:
        _make_project(tmp_path, with_summary=False)
        results = run_doctor(tmp_path)
        ctx_result = next(r for r in results if r.name == "context.md")
        assert ctx_result.passed is False
        assert "Missing" in ctx_result.message

    def test_stale_summary(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        summary_path = tmp_path / FILIGREE_DIR_NAME / SUMMARY_FILENAME
        # Set mtime 90 minutes ago
        old_mtime = time.time() - 90 * 60

        os.utime(str(summary_path), (old_mtime, old_mtime))
        results = run_doctor(tmp_path)
        ctx_result = next(r for r in results if r.name == "context.md")
        assert ctx_result.passed is False
        assert "Stale" in ctx_result.message
        assert "90" in ctx_result.message or "minutes" in ctx_result.message

    def test_summary_directory_reports_failure(self, tmp_path: Path) -> None:
        _make_project(tmp_path, with_summary=False)
        summary_path = tmp_path / FILIGREE_DIR_NAME / SUMMARY_FILENAME
        summary_path.mkdir()

        results = run_doctor(tmp_path)

        ctx_result = next(r for r in results if r.name == "context.md")
        assert ctx_result.passed is False
        assert "not a file" in ctx_result.message
        assert "generated context" in ctx_result.fix_hint


# ---------------------------------------------------------------------------
# run_doctor — .gitignore check
# ---------------------------------------------------------------------------


class TestDoctorGitignore:
    def test_gitignore_with_filigree_entry(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        (tmp_path / ".gitignore").write_text(".filigree/\n")
        results = run_doctor(tmp_path)
        gi_result = next(r for r in results if r.name == ".gitignore")
        assert gi_result.passed is True

    def test_gitignore_without_filigree_entry(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        (tmp_path / ".gitignore").write_text("*.pyc\n__pycache__/\n")
        results = run_doctor(tmp_path)
        gi_result = next(r for r in results if r.name == ".gitignore")
        assert gi_result.passed is False
        assert "not in .gitignore" in gi_result.message

    def test_no_gitignore(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        results = run_doctor(tmp_path)
        gi_result = next(r for r in results if r.name == ".gitignore")
        assert gi_result.passed is False
        assert "No .gitignore" in gi_result.message

    def test_gitignore_bare_entry_without_slash(self, tmp_path: Path) -> None:
        """`.filigree` without trailing slash also satisfies the check."""
        _make_project(tmp_path)
        (tmp_path / ".gitignore").write_text(".filigree\n")
        results = run_doctor(tmp_path)
        gi_result = next(r for r in results if r.name == ".gitignore")
        assert gi_result.passed is True

    def test_gitignore_directory_reports_failure(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        (tmp_path / ".gitignore").mkdir()

        results = run_doctor(tmp_path)

        gi_result = next(r for r in results if r.name == ".gitignore")
        assert gi_result.passed is False
        assert "unreadable" in gi_result.message


# ---------------------------------------------------------------------------
# run_doctor — bundled scanner registration drift
# ---------------------------------------------------------------------------


class TestDoctorBundledScanners:
    def test_no_scanners_gives_enablement_nudge(self, tmp_path: Path) -> None:
        _make_project(tmp_path)

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            results = run_doctor(tmp_path)

        scanner_result = next(r for r in results if r.name == "Bundled scanner registrations")
        assert scanner_result.passed is True
        assert "0 scanners enabled" in scanner_result.message
        assert "filigree scanner available" in scanner_result.fix_hint

    def test_reports_stale_bundled_scanner_registration(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        scanners_dir = tmp_path / FILIGREE_DIR_NAME / "scanners"
        scanners_dir.mkdir()
        (scanners_dir / "codex.toml").write_text(
            "[scanner]\n"
            'name = "codex"\n'
            'description = "Per-file bug hunt using Codex CLI"\n'
            'command = "filigree-scanner-codex"\n'
            'args = ["--root", "{project_root}", "--file", "{file}"]\n'
            'file_types = ["py"]\n'
        )

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            results = run_doctor(tmp_path)

        scanner_result = next(r for r in results if r.name == "Bundled scanner registrations")
        assert scanner_result.passed is False
        assert "codex" in scanner_result.message
        assert "scanner enable codex --force" in scanner_result.fix_hint

    def test_current_bundled_scanner_registration_passes(self, tmp_path: Path) -> None:
        from filigree.bundled_scanners import BUNDLED_SCANNERS

        _make_project(tmp_path)
        scanners_dir = tmp_path / FILIGREE_DIR_NAME / "scanners"
        scanners_dir.mkdir()
        (scanners_dir / "codex.toml").write_text(BUNDLED_SCANNERS["codex"].toml())

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            results = run_doctor(tmp_path)

        scanner_result = next(r for r in results if r.name == "Bundled scanner registrations")
        assert scanner_result.passed is True

    def test_current_bundled_scanner_with_missing_command_fails(self, tmp_path: Path) -> None:
        from filigree.bundled_scanners import BUNDLED_SCANNERS

        _make_project(tmp_path)
        scanners_dir = tmp_path / FILIGREE_DIR_NAME / "scanners"
        scanners_dir.mkdir()
        (scanners_dir / "codex.toml").write_text(BUNDLED_SCANNERS["codex"].toml())

        with (
            patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")),
            patch("filigree.install_support.doctor.shutil.which", return_value=None),
        ):
            results = run_doctor(tmp_path)

        scanner_result = next(r for r in results if r.name == "Bundled scanner registrations")
        assert scanner_result.passed is False
        assert "missing command(s)" in scanner_result.message
        assert "uv tool install --upgrade filigree" in scanner_result.fix_hint


# ---------------------------------------------------------------------------
# run_doctor — Claude Code MCP (.mcp.json) check
# ---------------------------------------------------------------------------


class TestDoctorClaudeCodeMcp:
    def _base_mcp(self, command: str = "filigree-mcp", args: list[str] | None = None) -> dict:
        args = [] if args is None else args
        return {"mcpServers": {"filigree": {"command": command, "args": args}}}

    def test_no_mcp_json(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        results = run_doctor(tmp_path)
        mcp_result = next(r for r in results if r.name == "Claude Code MCP")
        assert mcp_result.passed is False
        assert "No .mcp.json" in mcp_result.message

    def test_valid_mcp_json_relative_command(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        mcp_data = self._base_mcp("filigree-mcp")
        (tmp_path / ".mcp.json").write_text(json.dumps(mcp_data))
        results = run_doctor(tmp_path)
        mcp_result = next(r for r in results if r.name == "Claude Code MCP")
        assert mcp_result.passed is True

    def test_mcp_json_no_filigree_entry(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {}}}))
        results = run_doctor(tmp_path)
        mcp_result = next(r for r in results if r.name == "Claude Code MCP")
        assert mcp_result.passed is False
        assert "filigree not in .mcp.json" in mcp_result.message

    def test_corrupt_mcp_json(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        (tmp_path / ".mcp.json").write_text("{bad json")
        results = run_doctor(tmp_path)
        mcp_result = next(r for r in results if r.name == "Claude Code MCP")
        assert mcp_result.passed is False
        assert "Invalid .mcp.json" in mcp_result.message

    def test_mcp_json_directory_reports_failure(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        (tmp_path / ".mcp.json").mkdir()

        results = run_doctor(tmp_path)

        mcp_result = next(r for r in results if r.name == "Claude Code MCP")
        assert mcp_result.passed is False
        assert "Invalid .mcp.json" in mcp_result.message

    def test_mcp_json_absolute_command_missing_binary(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        missing_bin = tmp_path / "bin" / "filigree-mcp"
        # Don't create the binary
        mcp_data = {"mcpServers": {"filigree": {"command": str(missing_bin), "args": []}}}
        (tmp_path / ".mcp.json").write_text(json.dumps(mcp_data))
        results = run_doctor(tmp_path)
        mcp_result = next(r for r in results if r.name == "Claude Code MCP")
        assert mcp_result.passed is False
        assert "Binary not found" in mcp_result.message

    def test_mcp_json_absolute_command_existing_binary(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        binary = bin_dir / "filigree-mcp"
        binary.write_text("#!/bin/sh\n")
        mcp_data = {"mcpServers": {"filigree": {"command": str(binary), "args": []}}}
        (tmp_path / ".mcp.json").write_text(json.dumps(mcp_data))
        results = run_doctor(tmp_path)
        mcp_result = next(r for r in results if r.name == "Claude Code MCP")
        # Passes unless it's in a venv with a uv tool installed
        assert mcp_result.name == "Claude Code MCP"


# ---------------------------------------------------------------------------
# run_doctor — Claude Code hooks (.claude/settings.json) check
# ---------------------------------------------------------------------------


class TestDoctorClaudeCodeHooks:
    def _settings_with_hook(self, command: str = "filigree session-context") -> dict:
        return {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [{"type": "command", "command": command}],
                    }
                ]
            }
        }

    def test_no_settings_json(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        results = run_doctor(tmp_path)
        hook_result = next(r for r in results if r.name == "Claude Code hooks")
        assert hook_result.passed is False
        assert "No .claude/settings.json" in hook_result.message

    def test_corrupt_settings_json(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text("{bad")
        results = run_doctor(tmp_path)
        hook_result = next(r for r in results if r.name == "Claude Code hooks")
        assert hook_result.passed is False
        assert "Invalid .claude/settings.json" in hook_result.message

    def test_settings_json_directory_reports_failure(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").mkdir()

        results = run_doctor(tmp_path)

        hook_result = next(r for r in results if r.name == "Claude Code hooks")
        assert hook_result.passed is False
        assert "Invalid .claude/settings.json" in hook_result.message
        assert "filigree install --hooks" in hook_result.fix_hint

    def test_settings_missing_hook(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(json.dumps({"hooks": {}}))
        results = run_doctor(tmp_path)
        hook_result = next(r for r in results if r.name == "Claude Code hooks")
        assert hook_result.passed is False
        assert "session-context hook not found" in hook_result.message

    def test_settings_with_hook_relative_command(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = self._settings_with_hook("filigree session-context")
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        results = run_doctor(tmp_path)
        hook_result = next(r for r in results if r.name == "Claude Code hooks")
        assert hook_result.passed is True
        assert "session-context hook registered" in hook_result.message

    def test_settings_with_hook_absolute_missing_binary(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        missing = str(tmp_path / "bin" / "filigree")
        settings = self._settings_with_hook(f"{missing} session-context")
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        results = run_doctor(tmp_path)
        hook_result = next(r for r in results if r.name == "Claude Code hooks")
        assert hook_result.passed is False
        assert "Binary not found" in hook_result.message

    def test_module_form_hook_with_existing_interpreter_passes(self, tmp_path: Path) -> None:
        """Module-form hooks (``<abs-path> -m filigree session-context``)
        get structural validation only — if the interpreter file exists
        the check passes.

        This used to also subprocess-run the interpreter with
        ``-c "import filigree"`` to detect a venv-purged install
        (filigree-36539914b3), but the path comes from
        project-controlled ``.claude/settings.json`` and could be
        weaponised for arbitrary code execution under
        ``filigree doctor`` — so the probe was removed
        (filigree-e6828dcdb1). The security regression test lives in
        ``tests/install/test_install.py::test_doctor_does_not_execute_hook_binary``.
        """
        _make_project(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        fake_python = tmp_path / "bin" / "python3"
        fake_python.parent.mkdir()
        fake_python.touch()
        settings = self._settings_with_hook(f"{fake_python} -m filigree session-context")
        (claude_dir / "settings.json").write_text(json.dumps(settings))

        results = run_doctor(tmp_path)
        hook_result = next(r for r in results if r.name == "Claude Code hooks")
        assert hook_result.passed is True
        assert "session-context hook registered" in hook_result.message


# ---------------------------------------------------------------------------
# run_doctor — Claude Code skills check
# ---------------------------------------------------------------------------


class TestDoctorClaudeCodeSkills:
    def test_skill_missing(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        results = run_doctor(tmp_path)
        skill_result = next(r for r in results if r.name == "Claude Code skills")
        assert skill_result.passed is False
        assert SKILL_NAME in skill_result.message
        assert "filigree install --skills" in skill_result.fix_hint

    def test_skill_present(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        skill_dir = tmp_path / ".claude" / "skills" / SKILL_NAME
        skill_dir.mkdir(parents=True)
        (skill_dir / SKILL_MARKER).write_text("# skill\n")
        results = run_doctor(tmp_path)
        skill_result = next(r for r in results if r.name == "Claude Code skills")
        assert skill_result.passed is True
        assert SKILL_NAME in skill_result.message


# ---------------------------------------------------------------------------
# run_doctor — Codex skills check
# ---------------------------------------------------------------------------


class TestDoctorCodexSkills:
    def test_codex_skill_missing(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        results = run_doctor(tmp_path)
        skill_result = next(r for r in results if r.name == "Codex skills")
        assert skill_result.passed is False
        assert SKILL_NAME in skill_result.message
        assert "filigree install --codex-skills" in skill_result.fix_hint

    def test_codex_skill_present(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        skill_dir = tmp_path / ".agents" / "skills" / SKILL_NAME
        skill_dir.mkdir(parents=True)
        (skill_dir / SKILL_MARKER).write_text("# skill\n")
        results = run_doctor(tmp_path)
        skill_result = next(r for r in results if r.name == "Codex skills")
        assert skill_result.passed is True
        assert SKILL_NAME in skill_result.message


# ---------------------------------------------------------------------------
# run_doctor — CLAUDE.md and AGENTS.md instructions check
# ---------------------------------------------------------------------------


class TestDoctorInstructionFiles:
    def test_claude_md_missing(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        results = run_doctor(tmp_path)
        claude_result = next(r for r in results if r.name == "CLAUDE.md")
        assert claude_result.passed is False
        assert "File not found" in claude_result.message

    def test_claude_md_without_marker(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        (tmp_path / "CLAUDE.md").write_text("# Project\n\nNothing filigree here.\n")
        results = run_doctor(tmp_path)
        claude_result = next(r for r in results if r.name == "CLAUDE.md")
        assert claude_result.passed is False
        assert "No filigree instructions" in claude_result.message

    def test_claude_md_with_marker(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        (tmp_path / "CLAUDE.md").write_text(f"# Project\n\n{FILIGREE_INSTRUCTIONS_MARKER}\n")
        results = run_doctor(tmp_path)
        claude_result = next(r for r in results if r.name == "CLAUDE.md")
        assert claude_result.passed is True
        assert "instructions present" in claude_result.message.lower()

    def test_claude_md_directory_reports_failure(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        (tmp_path / "CLAUDE.md").mkdir()

        results = run_doctor(tmp_path)

        claude_result = next(r for r in results if r.name == "CLAUDE.md")
        assert claude_result.passed is False
        assert "unreadable" in claude_result.message
        assert "filigree install --claude-md" in claude_result.fix_hint

    def test_agents_md_with_marker(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        (tmp_path / "AGENTS.md").write_text(f"# Agents\n\n{FILIGREE_INSTRUCTIONS_MARKER}\n")
        results = run_doctor(tmp_path)
        agents_result = next((r for r in results if r.name == "AGENTS.md"), None)
        assert agents_result is not None
        assert agents_result.passed is True

    def test_agents_md_without_marker(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        (tmp_path / "AGENTS.md").write_text("# Agents\n\nNo instructions here.\n")
        results = run_doctor(tmp_path)
        agents_result = next((r for r in results if r.name == "AGENTS.md"), None)
        assert agents_result is not None
        assert agents_result.passed is False

    def test_agents_md_absent_not_reported(self, tmp_path: Path) -> None:
        """AGENTS.md is optional — its absence should not produce a check result."""
        _make_project(tmp_path)
        # Make sure AGENTS.md doesn't exist
        agents_md = tmp_path / "AGENTS.md"
        if agents_md.exists():
            agents_md.unlink()
        results = run_doctor(tmp_path)
        agents_result = next((r for r in results if r.name == "AGENTS.md"), None)
        assert agents_result is None

    def test_agents_md_directory_reports_failure(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        (tmp_path / "AGENTS.md").mkdir()

        results = run_doctor(tmp_path)

        agents_result = next((r for r in results if r.name == "AGENTS.md"), None)
        assert agents_result is not None
        assert agents_result.passed is False
        assert "unreadable" in agents_result.message
        assert "filigree install --agents-md" in agents_result.fix_hint


# ---------------------------------------------------------------------------
# run_doctor — git working tree check
# ---------------------------------------------------------------------------


class TestDoctorGitWorkingTree:
    def test_clean_working_tree(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        clean_output = MagicMock(returncode=0, stdout="")
        with patch("subprocess.run", return_value=clean_output) as mock_run:
            results = run_doctor(tmp_path)
        git_result = next((r for r in results if r.name == "Git working tree"), None)
        assert git_result is not None
        assert git_result.passed is True
        assert "Clean" in git_result.message
        # Verify git was called with the right args
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "git"
        assert "status" in call_args

    def test_dirty_working_tree(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        dirty_output = MagicMock(returncode=0, stdout=" M src/file.py\n?? new_file.py\n")
        with patch("subprocess.run", return_value=dirty_output):
            results = run_doctor(tmp_path)
        git_result = next((r for r in results if r.name == "Git working tree"), None)
        assert git_result is not None
        assert git_result.passed is False
        assert "2" in git_result.message
        assert "uncommitted" in git_result.message

    def test_git_not_installed(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        with patch("subprocess.run", side_effect=FileNotFoundError):
            results = run_doctor(tmp_path)
        # No git result — not an error condition
        git_result = next((r for r in results if r.name == "Git working tree"), None)
        assert git_result is None

    def test_git_timeout(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["git"], 5)):
            results = run_doctor(tmp_path)
        git_result = next((r for r in results if r.name == "Git working tree"), None)
        assert git_result is not None
        assert git_result.passed is False
        assert "timed out" in git_result.message

    def test_git_not_a_repo(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        # Non-zero returncode means not a git repo
        not_repo = MagicMock(returncode=128, stdout="")
        with patch("subprocess.run", return_value=not_repo):
            results = run_doctor(tmp_path)
        # returncode != 0 means no result appended
        git_result = next((r for r in results if r.name == "Git working tree"), None)
        assert git_result is None


# ---------------------------------------------------------------------------
# run_doctor — result list structure
# ---------------------------------------------------------------------------


class TestDoctorResultStructure:
    def test_all_results_are_check_result_instances(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            results = run_doctor(tmp_path)
        for r in results:
            assert isinstance(r, CheckResult)

    def test_all_results_have_non_empty_name(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            results = run_doctor(tmp_path)
        for r in results:
            assert r.name.strip(), f"Empty name in result: {r!r}"

    def test_all_results_have_message(self, tmp_path: Path) -> None:
        _make_project(tmp_path)
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            results = run_doctor(tmp_path)
        for r in results:
            assert isinstance(r.message, str)

    def test_failed_results_have_fix_hint(self, tmp_path: Path) -> None:
        """Every failed check should provide actionable guidance."""
        _make_project(tmp_path)
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            results = run_doctor(tmp_path)
        for r in results:
            if not r.passed and r.name not in {"Git working tree", "Installation"}:
                assert r.fix_hint.strip(), f"Failed check '{r.name}' has no fix_hint"


class TestDoctorSharedContractChecks:
    def test_run_doctor_emits_real_route_and_auth_checks(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path)

        results = run_doctor(project)
        by_id = {doctor_check_id(result): result for result in results}

        assert by_id["api.availability"].name == "API routes"
        assert by_id["scanner.results"].name == "Scan results routes"
        assert by_id["entity_associations.routes"].name == "Entity association routes"
        assert by_id["auth.config"].name == "Auth config"
        assert by_id["api.availability"].passed is True
        assert by_id["scanner.results"].passed is True
        assert by_id["entity_associations.routes"].passed is True
        assert by_id["auth.config"].passed is True


# ---------------------------------------------------------------------------
# _find_all_filigree_binaries
# ---------------------------------------------------------------------------


class TestFindAllFiligreeeBinaries:
    """Unit tests for _find_all_filigree_binaries().

    The function accepts (which_result, uv_tool_bin) and returns a list of
    other install paths found outside the uv tool environment.
    """

    def test_no_other_installs_same_binary(self, tmp_path: Path) -> None:
        """When shutil.which points at the uv tool binary, no extras returned."""
        uv_bin = tmp_path / "uv_bin" / "filigree"
        uv_bin.parent.mkdir(parents=True)
        uv_bin.write_text("#!/bin/sh\n")

        with patch("site.getsitepackages", return_value=[]), patch("site.getusersitepackages", return_value=str(tmp_path / "no_user_site")):
            result = _find_all_filigree_binaries(str(uv_bin), uv_bin)

        assert result == []

    def test_which_points_to_different_binary(self, tmp_path: Path) -> None:
        """When shutil.which finds a different binary, it is reported."""
        uv_bin = tmp_path / "uv_bin" / "filigree"
        uv_bin.parent.mkdir(parents=True)
        uv_bin.write_text("#!/bin/sh\n")

        other_bin = tmp_path / "other_bin" / "filigree"
        other_bin.parent.mkdir(parents=True)
        other_bin.write_text("#!/bin/sh\n")

        with patch("site.getsitepackages", return_value=[]), patch("site.getusersitepackages", return_value=str(tmp_path / "no_user_site")):
            result = _find_all_filigree_binaries(str(other_bin), uv_bin)

        assert str(other_bin) in result

    def test_pip_dist_info_found_in_site_packages(self, tmp_path: Path) -> None:
        """A filigree dist-info directory in site-packages is reported."""
        # Place the uv tool under a deep path so its grandparent does not overlap
        # with the system site-packages location used in the test.
        uv_home = tmp_path / "uv_home"
        uv_bin = uv_home / ".local" / "share" / "uv" / "tools" / "filigree" / "bin" / "filigree"
        uv_bin.parent.mkdir(parents=True)
        uv_bin.write_text("#!/bin/sh\n")

        # System site-packages lives elsewhere under tmp_path
        site_pkg = tmp_path / "system" / "lib" / "python3.11" / "site-packages"
        site_pkg.mkdir(parents=True)
        dist_info = site_pkg / "filigree-1.0.0.dist-info"
        dist_info.mkdir()

        with (
            patch("site.getsitepackages", return_value=[str(site_pkg)]),
            patch("site.getusersitepackages", return_value=str(tmp_path / "no_user_site")),
        ):
            result = _find_all_filigree_binaries("", uv_bin)

        assert str(site_pkg) in result

    def test_dist_info_in_uv_tool_site_packages_is_skipped(self, tmp_path: Path) -> None:
        """dist-info inside the uv tool tree itself is not reported as an extra."""
        uv_tools_dir = tmp_path / "uv_tools" / "filigree"
        uv_bin = uv_tools_dir / "bin" / "filigree"
        uv_bin.parent.mkdir(parents=True)
        uv_bin.write_text("#!/bin/sh\n")

        # Place dist-info inside the uv tool's own lib tree
        site_pkg = uv_tools_dir / "lib" / "python3.11" / "site-packages"
        site_pkg.mkdir(parents=True)
        dist_info = site_pkg / "filigree-1.0.0.dist-info"
        dist_info.mkdir()

        with (
            patch("site.getsitepackages", return_value=[str(site_pkg)]),
            patch("site.getusersitepackages", return_value=str(tmp_path / "no_user_site")),
        ):
            result = _find_all_filigree_binaries("", uv_bin)

        assert result == []

    def test_empty_which_result_no_uv_tool(self, tmp_path: Path) -> None:
        """When which returns '' and uv_bin doesn't exist, nothing crashes."""
        uv_bin = tmp_path / "nonexistent" / "filigree"  # does not exist

        with patch("site.getsitepackages", return_value=[]), patch("site.getusersitepackages", return_value=str(tmp_path / "no_user_site")):
            result = _find_all_filigree_binaries("", uv_bin)

        assert result == []

    def test_nonexistent_site_packages_dir_skipped(self, tmp_path: Path) -> None:
        """site-packages dirs that don't exist on disk are silently skipped."""
        uv_bin = tmp_path / "uv_bin" / "filigree"
        uv_bin.parent.mkdir(parents=True)
        uv_bin.write_text("#!/bin/sh\n")

        missing_site = str(tmp_path / "missing_site_packages")

        with patch("site.getsitepackages", return_value=[missing_site]), patch("site.getusersitepackages", return_value=missing_site):
            result = _find_all_filigree_binaries("", uv_bin)

        assert result == []


# ---------------------------------------------------------------------------
# _doctor_install_method — all 5 branches
# ---------------------------------------------------------------------------


class TestDoctorInstallMethod:
    """Unit tests for _doctor_install_method().

    Each test exercises one of the five conditional branches by controlling:
    - sys.executable (to simulate running from uv tool or venv)
    - Path.home() / uv tool directories
    - _find_all_filigree_binaries() return value
    """

    def _patch_home(self, tmp_path: Path):
        """Return a patch that redirects Path.home() to tmp_path."""
        return patch("pathlib.Path.home", return_value=tmp_path)

    def test_branch_uv_tool_no_other_installs(self, tmp_path: Path) -> None:
        """Branch 1: running from uv tool, no competing installs → passes."""
        uv_tools_dir = tmp_path / ".local" / "share" / "uv" / "tools" / "filigree"
        uv_tool_bin = tmp_path / ".local" / "bin" / "filigree"
        uv_tools_dir.mkdir(parents=True)
        uv_tool_bin.parent.mkdir(parents=True)
        uv_tool_bin.write_text("#!/bin/sh\n")

        # sys.executable inside the uv tool tree
        fake_exe = uv_tools_dir / "bin" / "python"
        fake_exe.parent.mkdir(parents=True)
        fake_exe.write_text("")

        with (
            self._patch_home(tmp_path),
            patch("sys.executable", str(fake_exe)),
            patch(
                "filigree.install_support.doctor._find_all_filigree_binaries",
                return_value=[],
            ),
        ):
            results = _doctor_install_method()

        assert len(results) == 1
        r = results[0]
        assert r.name == "Installation"
        assert r.passed is True
        assert "uv tool" in r.message

    def test_branch_uv_tool_with_other_installs(self, tmp_path: Path) -> None:
        """Branch 2: running from uv tool but competing installs exist → fails with coexistence warning."""
        uv_tools_dir = tmp_path / ".local" / "share" / "uv" / "tools" / "filigree"
        uv_tool_bin = tmp_path / ".local" / "bin" / "filigree"
        uv_tools_dir.mkdir(parents=True)
        uv_tool_bin.parent.mkdir(parents=True)
        uv_tool_bin.write_text("#!/bin/sh\n")

        fake_exe = uv_tools_dir / "bin" / "python"
        fake_exe.parent.mkdir(parents=True)
        fake_exe.write_text("")

        other_install = "/usr/lib/python3/dist-packages"

        with (
            self._patch_home(tmp_path),
            patch("sys.executable", str(fake_exe)),
            patch(
                "filigree.install_support.doctor._find_all_filigree_binaries",
                return_value=[other_install],
            ),
        ):
            results = _doctor_install_method()

        assert len(results) == 1
        r = results[0]
        assert r.name == "Installation"
        assert r.passed is False
        assert "uv tool" in r.message
        assert other_install in r.message
        assert r.fix_hint

    def test_branch_venv_only(self, tmp_path: Path) -> None:
        """Branch 3: running from venv, no uv tool installed → fails, suggests uv tool."""
        venv_dir = tmp_path / "myenv"
        fake_exe = venv_dir / "bin" / "python"
        fake_exe.parent.mkdir(parents=True)
        fake_exe.write_text("")
        (venv_dir / "pyvenv.cfg").write_text("[pyvenv]\n")

        # No uv tool: uv_tools_dir does NOT exist
        with (
            self._patch_home(tmp_path),
            patch("sys.executable", str(fake_exe)),
        ):
            results = _doctor_install_method()

        assert len(results) == 1
        r = results[0]
        assert r.name == "Installation"
        assert r.passed is False
        assert "venv" in r.message.lower()
        assert "uv tool" in r.fix_hint.lower()

    def test_branch_uv_tool_exists_but_not_on_path(self, tmp_path: Path) -> None:
        """Branch 4: uv tool installed but current executable is neither uv-tool nor a venv."""
        uv_tools_dir = tmp_path / ".local" / "share" / "uv" / "tools" / "filigree"
        uv_tool_bin = tmp_path / ".local" / "bin" / "filigree"
        uv_tools_dir.mkdir(parents=True)
        uv_tool_bin.parent.mkdir(parents=True)
        uv_tool_bin.write_text("#!/bin/sh\n")

        # sys.executable is somewhere completely different (no pyvenv.cfg in tree)
        system_python = tmp_path / "usr" / "bin" / "python3"
        system_python.parent.mkdir(parents=True)
        system_python.write_text("")

        with (
            self._patch_home(tmp_path),
            patch("sys.executable", str(system_python)),
            patch(
                "filigree.install_support.doctor._find_all_filigree_binaries",
                return_value=[],
            ),
        ):
            results = _doctor_install_method()

        assert len(results) == 1
        r = results[0]
        assert r.name == "Installation"
        assert r.passed is False
        assert "PATH" in r.fix_hint or "path" in r.fix_hint.lower()

    def test_branch_system_pip_install(self, tmp_path: Path) -> None:
        """Branch 5: no uv tool, not in venv → system pip fallback."""
        # sys.executable in a system location with no pyvenv.cfg in parents
        system_python = tmp_path / "usr" / "bin" / "python3"
        system_python.parent.mkdir(parents=True)
        system_python.write_text("")

        with (
            self._patch_home(tmp_path),
            patch("sys.executable", str(system_python)),
            patch("shutil.which", return_value="/usr/local/bin/filigree"),
        ):
            results = _doctor_install_method()

        assert len(results) == 1
        r = results[0]
        assert r.name == "Installation"
        assert r.passed is False
        assert "pip" in r.message.lower() or "system" in r.message.lower()
        assert "uv tool" in r.fix_hint.lower()

    def test_branch_venv_with_uv_tool_coexist(self, tmp_path: Path) -> None:
        """Branch: has_uv_tool=True but running_from_venv — warns about duplicate."""
        uv_tools_dir = tmp_path / ".local" / "share" / "uv" / "tools" / "filigree"
        uv_tool_bin = tmp_path / ".local" / "bin" / "filigree"
        uv_tools_dir.mkdir(parents=True)
        uv_tool_bin.parent.mkdir(parents=True)
        uv_tool_bin.write_text("#!/bin/sh\n")

        # sys.executable inside a project venv (not inside uv_tools_dir)
        venv_dir = tmp_path / "project" / ".venv"
        fake_exe = venv_dir / "bin" / "python"
        fake_exe.parent.mkdir(parents=True)
        fake_exe.write_text("")
        (venv_dir / "pyvenv.cfg").write_text("[pyvenv]\n")

        with (
            self._patch_home(tmp_path),
            patch("sys.executable", str(fake_exe)),
            patch(
                "filigree.install_support.doctor._find_all_filigree_binaries",
                return_value=[],
            ),
        ):
            results = _doctor_install_method()

        assert len(results) == 1
        r = results[0]
        assert r.name == "Installation"
        assert r.passed is False
        assert "venv" in r.message.lower()
        assert "uv tool" in r.message.lower()
        assert r.fix_hint

    def test_branch_uv_tool_symlinked_python_not_false_duplicate(self, tmp_path: Path) -> None:
        """Bug fix: uv tool venv with symlinked system python must not trigger duplicate warning.

        uv tool venvs symlink their python to a uv-managed interpreter
        outside the venv.  Path(sys.executable).resolve() follows the
        symlink and escapes the venv tree, so the startswith check
        fails.  The fallback should recognise that the *venv* found by
        walking unresolved parents is the uv tool's own venv.
        """
        uv_tools_dir = tmp_path / ".local" / "share" / "uv" / "tools" / "filigree"
        uv_tool_bin = tmp_path / ".local" / "bin" / "filigree"
        uv_tools_dir.mkdir(parents=True)
        uv_tool_bin.parent.mkdir(parents=True)
        uv_tool_bin.write_text("#!/bin/sh\n")

        # Create a "system" python outside the venv to symlink to —
        # this simulates the uv-managed interpreter that the venv's
        # python symlinks to.
        system_python = tmp_path / "uv" / "python" / "bin" / "python3"
        system_python.parent.mkdir(parents=True)
        system_python.write_text("")

        # The venv's python is a symlink to the system python.
        # resolve() will follow it outside the uv tools dir.
        venv_python = uv_tools_dir / "bin" / "python"
        venv_python.parent.mkdir(parents=True, exist_ok=True)
        venv_python.symlink_to(system_python)
        (uv_tools_dir / "pyvenv.cfg").write_text("[pyvenv]\n")

        with (
            self._patch_home(tmp_path),
            patch("sys.executable", str(venv_python)),
            patch(
                "filigree.install_support.doctor._find_all_filigree_binaries",
                return_value=[],
            ),
        ):
            results = _doctor_install_method()

        assert len(results) == 1
        r = results[0]
        assert r.name == "Installation"
        assert r.passed is True, f"Expected pass but got: {r.message} | hint: {r.fix_hint}"
        assert "uv tool" in r.message


# ---------------------------------------------------------------------------
# _doctor_ethereal_checks
# ---------------------------------------------------------------------------


class TestDoctorEtherealChecks:
    """Unit tests for _doctor_ethereal_checks().

    The function reads optional .filigree/ephemeral.pid and .filigree/ephemeral.port
    files and calls imported helpers from filigree.ephemeral and filigree.hooks.
    """

    def test_no_pid_no_port_files(self, tmp_path: Path) -> None:
        """When neither ephemeral file exists, returns an empty list."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        results = _doctor_ethereal_checks(filigree_dir)
        assert results == []

    def test_pid_file_alive(self, tmp_path: Path) -> None:
        """Alive PID owned by this project → passing check with process info."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        pid_file = filigree_dir / "ephemeral.pid"
        pid_file.write_text("42\n")

        with (
            patch("filigree.ephemeral.read_pid_file", return_value={"pid": 42}),
            patch("filigree.ephemeral.verify_pid_ownership", return_value=True),
        ):
            results = _doctor_ethereal_checks(filigree_dir)

        assert len(results) == 1
        r = results[0]
        assert r.name == "Ephemeral PID"
        assert r.passed is True
        assert "42" in r.message

    def test_pid_file_stale_with_known_pid(self, tmp_path: Path) -> None:
        """Stale PID (process gone) → failing check with stale message."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        pid_file = filigree_dir / "ephemeral.pid"
        pid_file.write_text("99\n")

        with (
            patch("filigree.ephemeral.read_pid_file", return_value={"pid": 99}),
            patch("filigree.ephemeral.verify_pid_ownership", return_value=False),
        ):
            results = _doctor_ethereal_checks(filigree_dir)

        assert len(results) == 1
        r = results[0]
        assert r.name == "Ephemeral PID"
        assert r.passed is False
        assert "Stale" in r.message
        assert "99" in r.message
        assert r.fix_hint

    def test_pid_file_stale_unreadable(self, tmp_path: Path) -> None:
        """If read_pid_file returns None, pid shows as 'unknown'."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        pid_file = filigree_dir / "ephemeral.pid"
        pid_file.write_text("garbage\n")

        with (
            patch("filigree.ephemeral.read_pid_file", return_value=None),
            patch("filigree.ephemeral.verify_pid_ownership", return_value=False),
        ):
            results = _doctor_ethereal_checks(filigree_dir)

        assert len(results) == 1
        r = results[0]
        assert r.passed is False
        assert "unknown" in r.message

    def test_pid_file_recycled_reports_stale(self, tmp_path: Path) -> None:
        """filigree-aa80d21b97: recycled PID (alive but not ours) must report as stale.

        Pre-fix the check used raw ``is_pid_alive``, so any unrelated process
        that happened to reuse the recorded PID passed the Ephemeral PID check
        as a healthy dashboard.
        """
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        pid_file = filigree_dir / "ephemeral.pid"
        pid_file.write_text("1234\n")

        # read_pid_file returns a valid record, but verify_pid_ownership
        # rejects it — the live process with PID 1234 isn't our dashboard.
        # (is_pid_alive would return True here; doctor must NOT trust that
        # signal alone.)
        with (
            patch("filigree.ephemeral.read_pid_file", return_value={"pid": 1234}),
            patch("filigree.ephemeral.is_pid_alive", return_value=True),
            patch("filigree.ephemeral.verify_pid_ownership", return_value=False),
        ):
            results = _doctor_ethereal_checks(filigree_dir)

        assert len(results) == 1
        r = results[0]
        assert r.name == "Ephemeral PID"
        assert r.passed is False, "Recycled PID must not be reported as healthy"
        assert "Stale" in r.message
        assert "1234" in r.message

    def test_port_file_listening(self, tmp_path: Path) -> None:
        """Port file exists and port is listening → passing check."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        port_file = filigree_dir / "ephemeral.port"
        port_file.write_text("8377\n")

        with (
            patch("filigree.ephemeral.read_port_file", return_value=8377),
            patch("filigree.hooks._is_port_listening", return_value=True),
        ):
            results = _doctor_ethereal_checks(filigree_dir)

        assert len(results) == 1
        r = results[0]
        assert r.name == "Ephemeral port"
        assert r.passed is True
        assert "8377" in r.message

    def test_port_file_not_listening(self, tmp_path: Path) -> None:
        """Port file exists but port is not listening → failing check."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        port_file = filigree_dir / "ephemeral.port"
        port_file.write_text("8377\n")

        with (
            patch("filigree.ephemeral.read_port_file", return_value=8377),
            patch("filigree.hooks._is_port_listening", return_value=False),
        ):
            results = _doctor_ethereal_checks(filigree_dir)

        assert len(results) == 1
        r = results[0]
        assert r.name == "Ephemeral port"
        assert r.passed is False
        assert "not listening" in r.message
        assert r.fix_hint

    def test_both_files_healthy(self, tmp_path: Path) -> None:
        """Both PID and port files present, both healthy → two passing results."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        (filigree_dir / "ephemeral.pid").write_text("55\n")
        (filigree_dir / "ephemeral.port").write_text("8377\n")

        with (
            patch("filigree.ephemeral.read_pid_file", return_value={"pid": 55}),
            patch("filigree.ephemeral.verify_pid_ownership", return_value=True),
            patch("filigree.ephemeral.read_port_file", return_value=8377),
            patch("filigree.hooks._is_port_listening", return_value=True),
        ):
            results = _doctor_ethereal_checks(filigree_dir)

        assert len(results) == 2
        assert all(r.passed for r in results)


# ---------------------------------------------------------------------------
# _doctor_server_checks
# ---------------------------------------------------------------------------


class TestDoctorServerChecks:
    """Unit tests for _doctor_server_checks().

    Mocks filigree.server.daemon_status and filigree.server.read_server_config
    to exercise each branch without a real daemon.
    """

    def _make_server_config(self, projects: dict | None = None):
        """Build a minimal ServerConfig-like object."""
        from filigree.server import ServerConfig

        return ServerConfig(port=8377, projects=projects or {})

    def test_daemon_running_no_projects(self, tmp_path: Path) -> None:
        """Running daemon, no registered projects → single passing result."""
        from filigree.server import DaemonStatus

        status = DaemonStatus(running=True, pid=1234, port=8377, project_count=0)
        config = self._make_server_config()

        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()

        with (
            patch("filigree.server.daemon_status", return_value=status),
            patch("filigree.server.read_server_config", return_value=config),
        ):
            results = _doctor_server_checks(filigree_dir)

        assert len(results) == 1
        r = results[0]
        assert r.name == "Server daemon"
        assert r.passed is True
        assert "1234" in r.message
        assert "8377" in r.message

    def test_daemon_not_running(self, tmp_path: Path) -> None:
        """Daemon not running → failing check with start hint."""
        from filigree.server import DaemonStatus

        status = DaemonStatus(running=False)
        config = self._make_server_config()

        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()

        with (
            patch("filigree.server.daemon_status", return_value=status),
            patch("filigree.server.read_server_config", return_value=config),
        ):
            results = _doctor_server_checks(filigree_dir)

        assert len(results) >= 1
        daemon_result = next(r for r in results if r.name == "Server daemon")
        assert daemon_result.passed is False
        assert "Not running" in daemon_result.message
        assert "filigree server start" in daemon_result.fix_hint

    def test_registered_project_directory_exists(self, tmp_path: Path) -> None:
        """A registered project whose directory still exists produces no extra failures."""
        from filigree.server import DaemonStatus

        # Create the directory so the path check passes
        project_dir = tmp_path / "my_project" / ".filigree"
        project_dir.mkdir(parents=True)

        status = DaemonStatus(running=True, pid=1, port=8377, project_count=1)
        config = self._make_server_config(projects={str(project_dir): {"prefix": "myp"}})

        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()

        with (
            patch("filigree.server.daemon_status", return_value=status),
            patch("filigree.server.read_server_config", return_value=config),
        ):
            results = _doctor_server_checks(filigree_dir)

        project_failures = [r for r in results if not r.passed and "Project" in r.name]
        assert project_failures == []

    def test_registered_project_directory_gone(self, tmp_path: Path) -> None:
        """A registered project whose directory no longer exists → failing check."""
        from filigree.server import DaemonStatus

        missing_dir = tmp_path / "vanished_project" / ".filigree"
        # Deliberately NOT created

        status = DaemonStatus(running=True, pid=1, port=8377, project_count=1)
        config = self._make_server_config(projects={str(missing_dir): {"prefix": "van"}})

        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()

        with (
            patch("filigree.server.daemon_status", return_value=status),
            patch("filigree.server.read_server_config", return_value=config),
        ):
            results = _doctor_server_checks(filigree_dir)

        project_failures = [r for r in results if not r.passed and "van" in r.name]
        assert len(project_failures) == 1
        r = project_failures[0]
        assert "Directory gone" in r.message
        assert r.fix_hint
        # Machine-routable repair: doctor --fix keys off the stable code and
        # acts on the exact stored config key carried in fix_target.
        assert r.code == "server_registry_orphan"
        assert r.fix_target == str(missing_dir)


# ---------------------------------------------------------------------------
# run_doctor — Codex MCP (~/.codex/config.toml) check
# ---------------------------------------------------------------------------


class TestDoctorCodexMcp:
    """Tests for _check_codex_mcp() exercised via run_doctor().

    The real ~/.codex/config.toml is never touched; every test redirects
    _codex_config_path via unittest.mock.patch.
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _write_codex_config(self, config_path: Path, content: str) -> None:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(content)

    def _stdio_toml(self, command: str, args: str = "[]") -> str:
        return f'[mcp_servers.filigree]\ncommand = "{command}"\nargs = {args}\n'

    def _run(self, tmp_path: Path, config_path: Path) -> list:
        with patch(
            "filigree.install_support.doctor._codex_config_path",
            return_value=config_path,
        ):
            return run_doctor(tmp_path)

    def _codex_result(self, results: list) -> CheckResult:
        return next(r for r in results if r.name == "Codex MCP")

    # ------------------------------------------------------------------
    # No config file
    # ------------------------------------------------------------------

    def test_no_codex_config_file(self, tmp_path: Path) -> None:
        """Missing ~/.codex/config.toml produces a failing check with install hint."""
        _make_project(tmp_path)
        missing = tmp_path / ".codex" / "config.toml"  # deliberately not created
        result = self._codex_result(self._run(tmp_path, missing))
        assert result.passed is False
        assert "No ~/.codex/config.toml" in result.message
        assert "filigree install --codex" in result.fix_hint

    # ------------------------------------------------------------------
    # Corrupt TOML
    # ------------------------------------------------------------------

    def test_corrupt_toml(self, tmp_path: Path) -> None:
        """Unreadable TOML returns a failing result with a repair hint."""
        _make_project(tmp_path)
        config_path = tmp_path / ".codex" / "config.toml"
        self._write_codex_config(config_path, "this = [invalid toml\n")
        result = self._codex_result(self._run(tmp_path, config_path))
        assert result.passed is False
        assert "Invalid ~/.codex/config.toml" in result.message
        assert "filigree install --codex" in result.fix_hint

    def test_codex_config_directory_reports_failure(self, tmp_path: Path) -> None:
        """Unreadable ~/.codex/config.toml paths should fail the check, not doctor."""
        _make_project(tmp_path)
        config_path = tmp_path / ".codex" / "config.toml"
        config_path.parent.mkdir()
        config_path.mkdir()

        result = self._codex_result(self._run(tmp_path, config_path))

        assert result.passed is False
        assert "Invalid ~/.codex/config.toml" in result.message
        assert "filigree install --codex" in result.fix_hint

    # ------------------------------------------------------------------
    # Missing filigree entry
    # ------------------------------------------------------------------

    def test_missing_filigree_entry(self, tmp_path: Path) -> None:
        """mcp_servers exists but has no filigree key."""
        _make_project(tmp_path)
        config_path = tmp_path / ".codex" / "config.toml"
        self._write_codex_config(config_path, '[mcp_servers.other]\ncommand = "other-tool"\n')
        result = self._codex_result(self._run(tmp_path, config_path))
        assert result.passed is False
        assert "filigree not in ~/.codex/config.toml" in result.message
        assert "filigree install --codex" in result.fix_hint

    def test_mcp_servers_is_not_a_table(self, tmp_path: Path) -> None:
        """mcp_servers is a TOML value but not a table — treated the same as absent."""
        _make_project(tmp_path)
        config_path = tmp_path / ".codex" / "config.toml"
        # A bare string array is valid TOML but the code expects a dict.
        self._write_codex_config(config_path, 'mcp_servers = ["not", "a", "table"]\n')
        result = self._codex_result(self._run(tmp_path, config_path))
        assert result.passed is False
        assert "filigree not in" in result.message

    # ------------------------------------------------------------------
    # Valid stdio config (relative command)
    # ------------------------------------------------------------------

    def test_valid_stdio_relative_command(self, tmp_path: Path) -> None:
        """Relative command + empty args is the happy-path stdio case."""
        _make_project(tmp_path)
        config_path = tmp_path / ".codex" / "config.toml"
        self._write_codex_config(config_path, self._stdio_toml("filigree-mcp"))
        result = self._codex_result(self._run(tmp_path, config_path))
        assert result.passed is True
        assert "Configured in ~/.codex/config.toml" in result.message

    def test_stdio_pinned_project_root_fails(self, tmp_path: Path) -> None:
        """Pinned --project args should fail because global config must not target one folder."""
        _make_project(tmp_path)
        config_path = tmp_path / ".codex" / "config.toml"
        self._write_codex_config(
            config_path,
            self._stdio_toml("filigree-mcp", args='["--project", "/tmp/some_other_project"]'),
        )
        result = self._codex_result(self._run(tmp_path, config_path))
        assert result.passed is False
        assert "runtime project autodiscovery" in result.message
        assert "filigree install --codex" in result.fix_hint

    def test_stdio_empty_command(self, tmp_path: Path) -> None:
        """Empty command string with otherwise-correct args should fail."""
        _make_project(tmp_path)
        config_path = tmp_path / ".codex" / "config.toml"
        toml_content = '[mcp_servers.filigree]\ncommand = ""\nargs = []\n'
        self._write_codex_config(config_path, toml_content)
        result = self._codex_result(self._run(tmp_path, config_path))
        assert result.passed is False
        assert "runtime project autodiscovery" in result.message

    # ------------------------------------------------------------------
    # URL-based config
    # ------------------------------------------------------------------

    def test_url_config_is_rejected(self, tmp_path: Path) -> None:
        """A URL entry is deprecated because it pins routing outside the workspace."""
        _make_project(tmp_path)
        config_path = tmp_path / ".codex" / "config.toml"
        toml_content = '[mcp_servers.filigree]\nurl = "http://localhost:8377/mcp/?project=filigree"\n'
        self._write_codex_config(config_path, toml_content)
        result = self._codex_result(self._run(tmp_path, config_path))
        assert result.passed is False
        assert "deprecated URL-based routing" in result.message
        assert "filigree install --codex" in result.fix_hint

    # ------------------------------------------------------------------
    # Absolute path — missing binary
    # ------------------------------------------------------------------

    def test_absolute_command_missing_binary(self, tmp_path: Path) -> None:
        """Absolute command path that does not exist on disk → binary-not-found failure."""
        _make_project(tmp_path)
        config_path = tmp_path / ".codex" / "config.toml"
        missing_bin = tmp_path / "bin" / "filigree-mcp"
        # Do NOT create the binary file
        self._write_codex_config(config_path, self._stdio_toml(str(missing_bin)))
        result = self._codex_result(self._run(tmp_path, config_path))
        assert result.passed is False
        assert "Binary not found" in result.message
        assert str(missing_bin) in result.message
        assert "filigree install --codex" in result.fix_hint

    # ------------------------------------------------------------------
    # Absolute path — existing binary (not in a venv)
    # ------------------------------------------------------------------

    def test_absolute_command_existing_binary_not_venv(self, tmp_path: Path) -> None:
        """Absolute command path that exists and is not in a venv passes."""
        _make_project(tmp_path)
        config_path = tmp_path / ".codex" / "config.toml"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        binary = bin_dir / "filigree-mcp"
        binary.write_text("#!/bin/sh\n")
        # No pyvenv.cfg anywhere in parents — _is_venv_binary returns False
        self._write_codex_config(config_path, self._stdio_toml(str(binary)))
        result = self._codex_result(self._run(tmp_path, config_path))
        assert result.passed is True
        assert "Configured in ~/.codex/config.toml" in result.message

    # ------------------------------------------------------------------
    # Venv binary warning
    # ------------------------------------------------------------------

    def test_venv_binary_with_uv_tool_installed_warns(self, tmp_path: Path) -> None:
        """Venv binary + uv tool present at ~/.local/bin/filigree-mcp → warning failure."""
        _make_project(tmp_path)
        config_path = tmp_path / ".codex" / "config.toml"

        # Create a fake venv so _is_venv_binary returns True
        venv_dir = tmp_path / "venv"
        venv_bin = venv_dir / "bin"
        venv_bin.mkdir(parents=True)
        (venv_dir / "pyvenv.cfg").write_text("[pyvenv]\n")
        venv_binary = venv_bin / "filigree-mcp"
        venv_binary.write_text("#!/bin/sh\n")

        # Create a fake home with the uv tool binary present
        fake_home = tmp_path / "fakehome"
        uv_local_bin = fake_home / ".local" / "bin"
        uv_local_bin.mkdir(parents=True)
        (uv_local_bin / "filigree-mcp").write_text("#!/bin/sh\n")

        self._write_codex_config(config_path, self._stdio_toml(str(venv_binary)))

        with (
            patch(
                "filigree.install_support.doctor._codex_config_path",
                return_value=config_path,
            ),
            patch("pathlib.Path.home", return_value=fake_home),
        ):
            results = run_doctor(tmp_path)

        result = self._codex_result(results)
        assert result.passed is False
        assert "venv binary" in result.message
        assert "uv tool is installed" in result.message
        assert "filigree install --codex" in result.fix_hint

    def test_venv_binary_without_uv_tool_passes(self, tmp_path: Path) -> None:
        """Venv binary but no uv tool present — warning branch skipped, check passes."""
        _make_project(tmp_path)
        config_path = tmp_path / ".codex" / "config.toml"

        # Create a fake venv so _is_venv_binary returns True
        venv_dir = tmp_path / "venv"
        venv_bin = venv_dir / "bin"
        venv_bin.mkdir(parents=True)
        (venv_dir / "pyvenv.cfg").write_text("[pyvenv]\n")
        venv_binary = venv_bin / "filigree-mcp"
        venv_binary.write_text("#!/bin/sh\n")

        # Fake home with NO uv tool binary at .local/bin/filigree-mcp
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()

        self._write_codex_config(config_path, self._stdio_toml(str(venv_binary)))

        with (
            patch(
                "filigree.install_support.doctor._codex_config_path",
                return_value=config_path,
            ),
            patch("pathlib.Path.home", return_value=fake_home),
        ):
            results = run_doctor(tmp_path)

        result = self._codex_result(results)
        # uv_tool_bin does not exist → warning branch skipped → passes
        assert result.passed is True
        assert "Configured in ~/.codex/config.toml" in result.message


# ---------------------------------------------------------------------------
# WEFT_FEDERATION_TOKEN: .mcp.json header token-reference connectivity check
# (filigree-0e4bc3d81a)
# ---------------------------------------------------------------------------


class TestMcpTokenReference:
    """The streamable-http header token must resolve, or /mcp silently 401s."""

    def test_unresolved_env_refs_bare_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WEFT_FEDERATION_TOKEN", raising=False)
        assert _unresolved_env_refs("Bearer ${WEFT_FEDERATION_TOKEN}") == ["WEFT_FEDERATION_TOKEN"]

    def test_unresolved_env_refs_resolved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WEFT_FEDERATION_TOKEN", "secret")
        assert _unresolved_env_refs("Bearer ${WEFT_FEDERATION_TOKEN}") == []

    def test_unresolved_env_refs_literal_default_counts_resolved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WEFT_FEDERATION_TOKEN", raising=False)
        # ${VAR:-default} expands to the literal default even when VAR is unset.
        assert _unresolved_env_refs("Bearer ${WEFT_FEDERATION_TOKEN:-fallback}") == []

    def test_token_result_unset_canonical_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WEFT_FEDERATION_TOKEN", raising=False)
        entry = {
            "type": "streamable-http",
            "url": "http://localhost:8749/mcp/?project=x",
            "headers": {"Authorization": "Bearer ${WEFT_FEDERATION_TOKEN}"},
        }
        result = _doctor_mcp_token_result(entry)
        assert result is not None
        assert result.passed is False
        assert result.code == "mcp_token_unresolved"
        assert "WEFT_FEDERATION_TOKEN" in result.fix_hint

    def test_token_result_deprecated_name_hints_fix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FILIGREE_API_TOKEN", raising=False)
        entry = {
            "type": "streamable-http",
            "url": "http://localhost:8749/mcp/?project=x",
            "headers": {"Authorization": "Bearer ${FILIGREE_API_TOKEN}"},
        }
        result = _doctor_mcp_token_result(entry)
        assert result is not None
        assert "doctor --fix" in result.fix_hint

    def test_token_result_resolved_is_healthy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WEFT_FEDERATION_TOKEN", "secret")
        entry = {
            "type": "streamable-http",
            "url": "http://localhost:8749/mcp/?project=x",
            "headers": {"Authorization": "Bearer ${WEFT_FEDERATION_TOKEN}"},
        }
        assert _doctor_mcp_token_result(entry) is None

    def test_token_result_no_header_not_applicable(self) -> None:
        entry = {"type": "streamable-http", "url": "http://localhost:8749/mcp"}
        assert _doctor_mcp_token_result(entry) is None

    def test_token_result_stdio_not_applicable(self) -> None:
        entry = {"type": "stdio", "command": "/usr/bin/filigree-mcp", "args": []}
        assert _doctor_mcp_token_result(entry) is None
