# tests/core/test_config.py
"""Tests for filigree.core — config, utility functions, write_atomic, get_mode."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from filigree import cli_common
from filigree.core import (
    FILIGREE_DIR_NAME,
    FiligreeDB,
    Issue,
    find_filigree_command,
    find_filigree_root,
    get_mode,
    read_config,
    write_atomic,
    write_config,
)


class TestReadConfig:
    """Verify read_config handles edge cases."""

    def test_default_registry_backend_is_local(self, tmp_path: Path) -> None:
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()

        config = read_config(filigree_dir)

        assert config["registry_backend"] == "local"

    def test_read_config_preserves_loomweave_registry_settings(self, tmp_path: Path) -> None:
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        write_config(
            filigree_dir,
            {
                "prefix": "proj",
                "version": 1,
                "registry_backend": "loomweave",
                "loomweave": {
                    "base_url": "http://localhost:9111",
                    "timeout_seconds": 3,
                    "allow_local_fallback": True,
                },
            },
        )

        config = read_config(filigree_dir)

        assert config["registry_backend"] == "loomweave"
        assert config["loomweave"]["base_url"] == "http://localhost:9111"
        assert config["loomweave"]["timeout_seconds"] == 3
        assert config["loomweave"]["allow_local_fallback"] is True

    def test_registry_backend_accepts_loomweave_literal(self) -> None:
        from filigree.core import VALID_REGISTRY_BACKENDS

        assert "loomweave" in VALID_REGISTRY_BACKENDS
        assert "clarion" not in VALID_REGISTRY_BACKENDS

    def test_existing_clarion_config_migrates_on_load(self, tmp_path: Path) -> None:
        """A deployed .filigree.conf still on the pre-3.0 clarion names loads as
        loomweave via the one-shot rename-on-load shim (no manual edit needed)."""
        from filigree.core import read_conf

        conf = tmp_path / ".filigree.conf"
        conf.write_text(
            json.dumps(
                {
                    "prefix": "f",
                    "db": "f.db",
                    "version": 1,
                    "registry_backend": "clarion",
                    "clarion": {"base_url": "http://x"},
                }
            )
        )

        cfg = read_conf(conf)

        assert cfg["registry_backend"] == "loomweave"
        assert "loomweave" in cfg
        assert "clarion" not in cfg
        assert cfg["loomweave"]["base_url"] == "http://x"

    def test_read_config_rejects_invalid_loomweave_base_url(self, tmp_path: Path) -> None:
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        write_config(
            filigree_dir,
            {
                "prefix": "proj",
                "version": 1,
                "registry_backend": "loomweave",
                "loomweave": {"base_url": "file:///tmp/loomweave"},
            },
        )

        with pytest.raises(ValueError, match=r"base_url"):
            read_config(filigree_dir)

    def test_read_config_rejects_unknown_loomweave_keys(self, tmp_path: Path) -> None:
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        write_config(
            filigree_dir,
            {
                "prefix": "proj",
                "version": 1,
                "registry_backend": "loomweave",
                "loomweave": {"base-url": "http://localhost:9111"},
            },
        )

        with pytest.raises(ValueError, match=r"base-url"):
            read_config(filigree_dir)

    def test_read_config_requires_loomweave_base_url_when_backend_is_loomweave(self, tmp_path: Path) -> None:
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        write_config(filigree_dir, {"prefix": "proj", "version": 1, "registry_backend": "loomweave"})

        with pytest.raises(ValueError, match=r"loomweave\.base_url"):
            read_config(filigree_dir)

    def test_non_dict_json_returns_defaults(self, tmp_path: Path) -> None:
        """Config with valid JSON that is not an object falls back to defaults."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        (filigree_dir / "config.json").write_text('"just a string"')

        config = read_config(filigree_dir)
        assert config["prefix"] == "filigree"
        assert config["version"] == 1

    def test_array_json_returns_defaults(self, tmp_path: Path) -> None:
        """Config with JSON array falls back to defaults."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        (filigree_dir / "config.json").write_text("[1, 2, 3]")

        config = read_config(filigree_dir)
        assert config["prefix"] == "filigree"

    def test_missing_prefix_gets_default(self, tmp_path: Path) -> None:
        """Config dict missing 'prefix' key gets default injected."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        (filigree_dir / "config.json").write_text('{"name": "test"}')

        config = read_config(filigree_dir)
        assert config["prefix"] == "filigree"
        assert config["version"] == 1

    def test_missing_version_gets_default(self, tmp_path: Path) -> None:
        """Config dict missing 'version' key gets default injected."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        (filigree_dir / "config.json").write_text('{"prefix": "proj"}')

        config = read_config(filigree_dir)
        assert config["prefix"] == "proj"
        assert config["version"] == 1

    def test_invalid_utf8_config_returns_defaults(self, tmp_path: Path) -> None:
        """Invalid bytes are corrupt config and should use defaults."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        (filigree_dir / "config.json").write_bytes(b"\xff\xfe")

        config = read_config(filigree_dir)

        assert config["prefix"] == "filigree"
        assert config["version"] == 1


class TestFromFiligreeDir:
    """Verify FiligreeDB.from_filigree_dir construction."""

    def test_registry_backend_passed_from_project_config(self, tmp_path: Path) -> None:
        from tests._fakes.clarion_http import clarion_stub

        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        # Real ``from_filigree_dir`` runs the ADR-014 capability probe at
        # __init__; point it at a live stub so the probe handshake succeeds
        # and the test stays focused on config plumbing rather than network.
        with clarion_stub() as (base_url, _state):
            write_config(
                filigree_dir,
                {
                    "prefix": "proj",
                    "version": 1,
                    "registry_backend": "loomweave",
                    "loomweave": {"base_url": base_url, "timeout_seconds": 1},
                },
            )

            db = FiligreeDB.from_filigree_dir(filigree_dir)
            try:
                assert db.registry_backend == "loomweave"
            finally:
                db.close()

    def test_missing_config_uses_defaults(self, tmp_path: Path) -> None:
        """from_filigree_dir with no config.json should succeed with defaults.

        The prefix defaults to the project directory's name (mirroring
        ``filigree init``'s default), not the hardcoded string ``"filigree"``
        — see bug filigree-fda0e2a340.
        """
        project_root = tmp_path / "myproj"
        project_root.mkdir()
        filigree_dir = project_root / ".filigree"
        filigree_dir.mkdir()

        db = FiligreeDB.from_filigree_dir(filigree_dir)
        assert db.prefix == "myproj"
        assert db.enabled_packs == ["core", "planning", "release"]
        db.close()

    def test_check_same_thread_passthrough(self, tmp_path: Path) -> None:
        """from_filigree_dir passes check_same_thread to constructor."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        write_config(filigree_dir, {"prefix": "proj", "version": 1})

        db = FiligreeDB.from_filigree_dir(filigree_dir, check_same_thread=False)
        assert db._check_same_thread is False
        db.close()

    def test_invalid_utf8_config_uses_project_prefix_fallback(self, tmp_path: Path) -> None:
        """from_filigree_dir treats undecodable config.json like a corrupt partial config."""
        project_root = tmp_path / "broken-config"
        project_root.mkdir()
        filigree_dir = project_root / ".filigree"
        filigree_dir.mkdir()
        (filigree_dir / "config.json").write_bytes(b"\xff\xfe")

        db = FiligreeDB.from_filigree_dir(filigree_dir)
        try:
            assert db.prefix == "broken-config"
        finally:
            db.close()


class TestConfigEnabledPacks:
    """Verify enabled_packs default and passthrough."""

    def test_read_config_missing_enabled_packs_gets_default(self, tmp_path: Path) -> None:
        """Config without enabled_packs receives the validated project default."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        write_config(filigree_dir, {"prefix": "test", "version": 1})

        config = read_config(filigree_dir)
        assert config["enabled_packs"] == ["core", "planning", "release"]
        db = FiligreeDB(filigree_dir / "filigree.db", prefix="test", enabled_packs=config.get("enabled_packs"))
        db.initialize()
        assert db.enabled_packs == ["core", "planning", "release"]
        db.close()

    def test_read_config_preserves_explicit_enabled_packs(self, tmp_path: Path) -> None:
        """Config with explicit enabled_packs should be preserved as-is."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        write_config(filigree_dir, {"prefix": "test", "version": 1, "enabled_packs": ["core"]})

        config = read_config(filigree_dir)
        assert config["enabled_packs"] == ["core"]

    def test_read_config_empty_enabled_packs_preserved(self, tmp_path: Path) -> None:
        """Config with empty enabled_packs (feature flag off) should stay empty."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        write_config(filigree_dir, {"prefix": "test", "version": 1, "enabled_packs": []})

        config = read_config(filigree_dir)
        assert config["enabled_packs"] == []

    def test_read_config_no_file_gets_defaults(self, tmp_path: Path) -> None:
        """Missing config.json should return defaults including enabled_packs."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        # No config file written

        config = read_config(filigree_dir)
        assert config["prefix"] == "filigree"
        assert config["enabled_packs"] == ["core", "planning", "release"]

    def test_from_project_passes_enabled_packs(self, tmp_path: Path) -> None:
        """FiligreeDB.from_project() should read enabled_packs from config."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        write_config(
            filigree_dir,
            {
                "prefix": "proj",
                "version": 1,
                "enabled_packs": ["core", "planning", "risk"],
            },
        )

        # Initialize the database so from_project can open it
        init_db = FiligreeDB(filigree_dir / "filigree.db", prefix="proj")
        init_db.initialize()
        init_db.close()

        db = FiligreeDB.from_project(tmp_path)
        assert db.enabled_packs == ["core", "planning", "risk"]
        db.close()

    def test_refresh_enabled_packs_bad_json_raises(self, tmp_path: Path) -> None:
        """Bug filigree-996a574447: _refresh_enabled_packs must raise on corrupt config.json."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        config_path = filigree_dir / "config.json"
        config_path.write_text("{invalid json")

        db = FiligreeDB(filigree_dir / "filigree.db", prefix="test")
        db.initialize()

        with pytest.raises(ValueError, match="could not be parsed"):
            db._refresh_enabled_packs()
        db.close()

    def test_refresh_enabled_packs_no_file_uses_defaults(self, tmp_path: Path) -> None:
        """When config.json does not exist, _refresh_enabled_packs uses defaults (no error)."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        # No config.json written

        db = FiligreeDB(filigree_dir / "filigree.db", prefix="test")
        db.initialize()
        db._refresh_enabled_packs()
        assert db.enabled_packs == ["core", "planning", "release"]
        db.close()

    def test_from_project_default_enabled_packs(self, tmp_path: Path) -> None:
        """FiligreeDB.from_project() with no enabled_packs in config gets default."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        write_config(filigree_dir, {"prefix": "proj", "version": 1})

        init_db = FiligreeDB(filigree_dir / "filigree.db", prefix="proj")
        init_db.initialize()
        init_db.close()

        db = FiligreeDB.from_project(tmp_path)
        assert db.enabled_packs == ["core", "planning", "release"]
        db.close()

    def test_from_filigree_dir_passes_enabled_packs(self, tmp_path: Path) -> None:
        """FiligreeDB.from_filigree_dir() should preserve explicit enabled_packs."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        write_config(filigree_dir, {"prefix": "proj", "version": 1, "enabled_packs": ["core"]})

        init_db = FiligreeDB(filigree_dir / "filigree.db", prefix="proj", enabled_packs=["core"])
        init_db.initialize()
        init_db.close()

        db = FiligreeDB.from_filigree_dir(filigree_dir)
        assert db.enabled_packs == ["core"]
        db.close()

    def test_cli_common_get_db_uses_configured_enabled_packs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """cli_common.get_db() should not fall back to default packs when config disables them."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        write_config(filigree_dir, {"prefix": "proj", "version": 1, "enabled_packs": ["core"]})

        init_db = FiligreeDB(filigree_dir / "filigree.db", prefix="proj", enabled_packs=["core"])
        init_db.initialize()
        init_db.close()

        monkeypatch.chdir(tmp_path)
        db = cli_common.get_db()
        assert db.enabled_packs == ["core"]
        assert db.list_issues() == []
        db.close()


# ===========================================================================
# Utility functions (from test_core.py)
# ===========================================================================


class TestGetMode:
    @pytest.mark.parametrize(
        ("config", "expected"),
        [
            ({"prefix": "test", "version": 1}, "ethereal"),
            ({"prefix": "test", "version": 1, "mode": "ethereal"}, "ethereal"),
            ({"prefix": "test", "version": 1, "mode": "server"}, "server"),
        ],
        ids=["no-mode-field", "explicit-ethereal", "explicit-server"],
    )
    def test_mode_from_config(self, tmp_path: Path, config: dict[str, Any], expected: str) -> None:
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        (filigree_dir / "config.json").write_text(json.dumps(config))
        assert get_mode(filigree_dir) == expected

    def test_missing_config_defaults_to_ethereal(self, tmp_path: Path) -> None:
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        assert get_mode(filigree_dir) == "ethereal"

    def test_unknown_mode_raises_value_error(self, tmp_path: Path) -> None:
        """Unknown mode values raise ValueError instead of silently falling back."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        config = {"prefix": "test", "version": 1, "mode": "bogus"}
        (filigree_dir / "config.json").write_text(json.dumps(config))
        with pytest.raises(ValueError, match="bogus"):
            get_mode(filigree_dir)

    @pytest.mark.parametrize("bad_mode", [[], {}, 1, True, None])
    def test_non_string_mode_raises_value_error(self, tmp_path: Path, bad_mode: Any) -> None:
        """Bug filigree-cff0de463f: a JSON-valid non-string ``mode`` (e.g. a list)
        used to raise ``TypeError`` from a frozenset membership test, bypassing
        callers that recover from ``ValueError``. It must raise ``ValueError``.
        """
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        config = {"prefix": "test", "version": 1, "mode": bad_mode}
        (filigree_dir / "config.json").write_text(json.dumps(config))
        with pytest.raises(ValueError, match="mode"):
            get_mode(filigree_dir)


class TestFindFiligreeCommand:
    def test_returns_list(self) -> None:
        """Command is always a list of strings."""
        result = find_filigree_command()
        assert isinstance(result, list)
        assert all(isinstance(s, str) for s in result)

    def test_at_least_one_element(self) -> None:
        result = find_filigree_command()
        assert len(result) >= 1

    def test_skips_non_executable_uv_tool_candidate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 0644 file at ~/.local/bin/filigree is not a runnable command."""
        home = tmp_path / "home"
        uv_tool_dir = home / ".local" / "bin"
        uv_tool_dir.mkdir(parents=True)
        uv_candidate = uv_tool_dir / "filigree"
        uv_candidate.write_text("#!/bin/sh\n")
        uv_candidate.chmod(0o644)

        path_dir = tmp_path / "path-bin"
        path_dir.mkdir()
        path_candidate = path_dir / "filigree"
        path_candidate.write_text("#!/bin/sh\n")
        path_candidate.chmod(0o755)

        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("PATH", str(path_dir))

        assert find_filigree_command() == [str(path_candidate)]

    def test_skips_non_executable_interpreter_sibling(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The venv sibling candidate must also be executable before use."""
        home = tmp_path / "home"
        home.mkdir()
        python_dir = tmp_path / "venv-bin"
        python_dir.mkdir()
        python_exe = python_dir / "python"
        python_exe.write_text("#!/bin/sh\n")
        sibling = python_dir / "filigree"
        sibling.write_text("#!/bin/sh\n")
        sibling.chmod(0o644)

        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("PATH", os.devnull)
        monkeypatch.setattr(sys, "executable", str(python_exe))

        assert find_filigree_command() == [str(python_exe), "-P", "-m", "filigree"]


class TestWriteAtomic:
    def test_writes_content(self, tmp_path: Path) -> None:
        target = tmp_path / "test.txt"
        write_atomic(target, "hello")
        assert target.read_text() == "hello"

    def test_no_tmp_file_left(self, tmp_path: Path) -> None:
        target = tmp_path / "test.txt"
        write_atomic(target, "hello")
        assert not (tmp_path / "test.txt.tmp").exists()

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        """Overwriting an existing file works correctly."""
        target = tmp_path / "test.txt"
        target.write_text("original")
        write_atomic(target, "updated")
        assert target.read_text() == "updated"

    def test_error_cleanup_removes_tmp_and_preserves_original(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bug filigree-07485f: on os.replace failure, temp file must be removed and original preserved."""
        target = tmp_path / "test.txt"
        target.write_text("precious data")

        def failing_replace(src: object, dst: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("os.replace", failing_replace)

        with pytest.raises(OSError, match="disk full"):
            write_atomic(target, "new content that should not land")

        assert target.read_text() == "precious data", "Original file must be untouched"
        leftover = list(tmp_path.glob("test.txt.*.tmp"))
        assert leftover == [], f"Temp file must be cleaned up on failure (found {leftover})"

    def test_concurrent_writers_do_not_collide(self, tmp_path: Path) -> None:
        """Bug filigree-9bb033331a: each writer must use a unique temp file.

        Two concurrent writers used to share ``target.tmp`` — one writer's
        replace could install the other's content, or fail/clobber the other's
        stage. A unique per-writer staging path eliminates the collision.
        """
        import threading

        target = tmp_path / "shared.txt"
        target.write_text("initial")
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def writer(idx: int) -> None:
            try:
                barrier.wait()
                for _ in range(20):
                    write_atomic(target, f"writer-{idx}")
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"writers raised: {errors}"
        # Final content must be one of the writers' values, not garbage.
        assert target.read_text().startswith("writer-")
        # No temp files should be left behind under any naming pattern.
        leftover = list(tmp_path.glob("shared.txt.*.tmp")) + list(tmp_path.glob("shared.txt.tmp"))
        assert leftover == [], f"Temp files leaked: {leftover}"


# ===========================================================================
# Config and root discovery (from test_core_gaps.py)
# ===========================================================================


class TestFindFiligreeRoot:
    def test_finds_in_current_dir(self, tmp_path: Path) -> None:
        (tmp_path / FILIGREE_DIR_NAME).mkdir()
        result = find_filigree_root(tmp_path)
        assert result == tmp_path / FILIGREE_DIR_NAME

    def test_finds_in_parent_dir(self, tmp_path: Path) -> None:
        (tmp_path / FILIGREE_DIR_NAME).mkdir()
        child = tmp_path / "subdir"
        child.mkdir()
        result = find_filigree_root(child)
        assert result == tmp_path / FILIGREE_DIR_NAME

    def test_raises_when_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            find_filigree_root(tmp_path)


class TestConfig:
    def test_read_write_roundtrip(self, tmp_path: Path) -> None:
        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        write_config(filigree_dir, {"prefix": "myproj", "version": 1})
        config = read_config(filigree_dir)
        assert config["prefix"] == "myproj"
        assert config["version"] == 1

    def test_read_missing_returns_defaults(self, tmp_path: Path) -> None:
        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        config = read_config(filigree_dir)
        assert config["prefix"] == "filigree"

    def test_read_config_fills_missing_prefix(self, tmp_path: Path) -> None:
        """Config without 'prefix' should get the default filled in."""
        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        (filigree_dir / "config.json").write_text(json.dumps({"version": 1}))
        config = read_config(filigree_dir)
        assert config["prefix"] == "filigree"
        assert config["version"] == 1

    def test_read_config_fills_missing_version(self, tmp_path: Path) -> None:
        """Config without 'version' should get the default filled in."""
        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        (filigree_dir / "config.json").write_text(json.dumps({"prefix": "myproj"}))
        config = read_config(filigree_dir)
        assert config["prefix"] == "myproj"
        assert config["version"] == 1

    def test_read_config_fills_both_missing_required_keys(self, tmp_path: Path) -> None:
        """Config with neither 'prefix' nor 'version' should get both defaults."""
        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        (filigree_dir / "config.json").write_text(json.dumps({"mode": "ethereal"}))
        config = read_config(filigree_dir)
        assert config["prefix"] == "filigree"
        assert config["version"] == 1
        assert config.get("mode") == "ethereal"


# ===========================================================================
# Issue dataclass __post_init__ validation
# ===========================================================================


class TestIssuePostInit:
    """Bug filigree-83986ec674: Issue must validate status_category and priority."""

    def test_valid_status_categories_accepted(self) -> None:
        for cat in ("open", "wip", "done"):
            issue = Issue(id="x", title="t", status_category=cat)
            assert issue.status_category == cat

    def test_invalid_status_category_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid status_category"):
            Issue(id="x", title="t", status_category="bogus")  # type: ignore[arg-type]

    def test_valid_priorities_accepted(self) -> None:
        for p in range(5):
            issue = Issue(id="x", title="t", priority=p)
            assert issue.priority == p

    def test_negative_priority_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid priority"):
            Issue(id="x", title="t", priority=-1)

    def test_priority_above_4_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid priority"):
            Issue(id="x", title="t", priority=5)
