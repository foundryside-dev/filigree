from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import portalocker
import pytest

from filigree.core import CONF_FILENAME, FILIGREE_DIR_NAME, write_conf
from filigree.db_schema import CURRENT_SCHEMA_VERSION, SCHEMA_SQL
from filigree.server import (
    ServerConfig,
    read_server_config,
    register_project,
    unregister_project,
    unregister_projects,
    write_server_config,
)


class TestServerConfigPortValidation:
    """Task filigree-afbc13: ServerConfig __post_init__ port validation."""

    def test_port_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="port must be between 1 and 65535"):
            ServerConfig(port=0)

    def test_port_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="port must be between 1 and 65535"):
            ServerConfig(port=-1)

    def test_port_above_max_raises(self) -> None:
        with pytest.raises(ValueError, match="port must be between 1 and 65535"):
            ServerConfig(port=65536)

    def test_port_min_boundary_succeeds(self) -> None:
        config = ServerConfig(port=1)
        assert config.port == 1

    def test_port_max_boundary_succeeds(self) -> None:
        config = ServerConfig(port=65535)
        assert config.port == 65535

    def test_default_port_succeeds(self) -> None:
        config = ServerConfig()
        assert config.port == 8377


class TestServerConfig:
    def test_default_config(self) -> None:
        config = ServerConfig()
        assert config.port == 8377
        assert config.projects == {}

    def test_write_and_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        config = ServerConfig(port=9000)
        write_server_config(config)
        loaded = read_server_config()
        assert loaded.port == 9000

    def test_read_missing_returns_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        config = read_server_config()
        assert config.port == 8377

    def test_roundtrip_with_special_chars_in_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Paths with quotes, spaces, and unicode survive serialization."""
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        weird_path = '/home/alice/my "quoted" project/.filigree'
        config = ServerConfig(projects={weird_path: {"prefix": 'weird"prefix'}})
        write_server_config(config)
        loaded = read_server_config()
        assert weird_path in loaded.projects
        assert loaded.projects[weird_path]["prefix"] == 'weird"prefix'


class TestProjectRegistration:
    def test_register_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        filigree_dir = tmp_path / "myproject" / ".filigree"
        filigree_dir.mkdir(parents=True)
        (filigree_dir / "config.json").write_text('{"prefix": "myproject"}')

        register_project(filigree_dir)
        config = read_server_config()
        assert str(filigree_dir.resolve()) in config.projects

    def test_unregister_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        filigree_dir = tmp_path / "myproject" / ".filigree"
        filigree_dir.mkdir(parents=True)
        (filigree_dir / "config.json").write_text('{"prefix": "myproject"}')

        register_project(filigree_dir)
        unregister_project(filigree_dir)
        config = read_server_config()
        assert str(filigree_dir.resolve()) not in config.projects

    def test_unregister_project_uses_exclusive_lock(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        filigree_dir = tmp_path / "myproject" / ".filigree"
        filigree_dir.mkdir(parents=True)
        (filigree_dir / "config.json").write_text('{"prefix": "myproject"}')
        register_project(filigree_dir)

        lock_ops: list[int] = []

        def _fake_flock(_fd: object, op: int) -> None:
            lock_ops.append(op)

        monkeypatch.setattr("filigree.server.portalocker.lock", _fake_flock)
        unregister_project(filigree_dir)

        assert lock_ops
        assert lock_ops[0] == portalocker.LOCK_EX

    def test_register_project_rejects_prefix_collision(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        first = tmp_path / "project-a" / ".filigree"
        second = tmp_path / "project-b" / ".filigree"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        (first / "config.json").write_text('{"prefix": "filigree"}')
        (second / "config.json").write_text('{"prefix": "filigree"}')

        register_project(first)
        with pytest.raises(ValueError, match="Prefix collision"):
            register_project(second)

    def test_register_project_is_idempotent_for_same_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        filigree_dir = tmp_path / "myproject" / ".filigree"
        filigree_dir.mkdir(parents=True)
        (filigree_dir / "config.json").write_text('{"prefix": "myproject"}')

        register_project(filigree_dir)
        register_project(filigree_dir)
        config = read_server_config()
        assert list(config.projects.keys()) == [str(filigree_dir.resolve())]


class TestBatchUnregister:
    """``unregister_projects`` — single-locked-pass cleanup used by doctor --fix."""

    def test_removes_only_requested_keys_and_returns_removed_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        gone_a = str(tmp_path / "vanished_a" / ".filigree")
        gone_b = str(tmp_path / "vanished_b" / ".filigree")
        alive = str(tmp_path / "alive" / ".filigree")
        write_server_config(
            ServerConfig(
                port=8377,
                projects={
                    gone_a: {"prefix": "a"},
                    gone_b: {"prefix": "b"},
                    alive: {"prefix": "c"},
                },
            )
        )

        removed = unregister_projects([gone_a, gone_b])

        assert removed == {gone_a, gone_b}
        remaining = read_server_config().projects
        assert set(remaining) == {alive}

    def test_matches_keys_exactly_without_resolving(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The stored key is what register wrote; a gone directory must be removed
        # by its exact stored string, never by a re-resolve() that could diverge.
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        stored_key = str(tmp_path / "vanished" / ".filigree")
        write_server_config(ServerConfig(port=8377, projects={stored_key: {"prefix": "v"}}))

        removed = unregister_projects([stored_key])

        assert removed == {stored_key}
        assert read_server_config().projects == {}

    def test_unknown_keys_are_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        alive = str(tmp_path / "alive" / ".filigree")
        write_server_config(ServerConfig(port=8377, projects={alive: {"prefix": "c"}}))

        removed = unregister_projects([str(tmp_path / "never_registered" / ".filigree")])

        assert removed == set()
        assert set(read_server_config().projects) == {alive}

    def test_empty_input_is_a_noop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        alive = str(tmp_path / "alive" / ".filigree")
        write_server_config(ServerConfig(port=8377, projects={alive: {"prefix": "c"}}))

        removed = unregister_projects([])

        assert removed == set()
        assert set(read_server_config().projects) == {alive}

    def test_uses_exclusive_lock(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        gone = str(tmp_path / "vanished" / ".filigree")
        write_server_config(ServerConfig(port=8377, projects={gone: {"prefix": "v"}}))

        lock_ops: list[int] = []
        monkeypatch.setattr("filigree.server.portalocker.lock", lambda _fd, op: lock_ops.append(op))

        unregister_projects([gone])

        assert lock_ops
        assert lock_ops[0] == portalocker.LOCK_EX


class TestVersionEnforcement:
    def test_register_rejects_incompatible_schema(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Projects with newer schema versions are rejected."""
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        filigree_dir = tmp_path / "future-project" / ".filigree"
        filigree_dir.mkdir(parents=True)
        (filigree_dir / "config.json").write_text(
            json.dumps(
                {
                    "prefix": "future",
                    "version": 1,
                }
            )
        )
        conn = sqlite3.connect(filigree_dir / "filigree.db")
        conn.executescript(SCHEMA_SQL)
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
        conn.commit()
        conn.close()

        with pytest.raises(ValueError, match="schema version"):
            register_project(filigree_dir)

    def test_register_rejects_incompatible_relocated_schema(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Registration must validate the DB path declared by adjacent .filigree.conf."""
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        project_root = tmp_path / "future-relocated"
        filigree_dir = project_root / FILIGREE_DIR_NAME
        db_dir = project_root / "storage"
        filigree_dir.mkdir(parents=True)
        db_dir.mkdir()
        (filigree_dir / "config.json").write_text(json.dumps({"prefix": "future", "version": 1}))
        write_conf(
            project_root / CONF_FILENAME,
            {
                "version": 1,
                "project_name": "future",
                "prefix": "future",
                "db": "storage/track.db",
            },
        )
        conn = sqlite3.connect(db_dir / "track.db")
        conn.executescript(SCHEMA_SQL)
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
        conn.commit()
        conn.close()

        with pytest.raises(ValueError, match="schema version"):
            register_project(filigree_dir)

        assert read_server_config().projects == {}

    def test_register_project_tolerates_transient_lock(self, tmp_path: Path) -> None:
        """A transiently locked DB should not prevent project registration."""
        from unittest.mock import patch

        filigree_dir = tmp_path / "locked" / ".filigree"
        filigree_dir.mkdir(parents=True)
        (filigree_dir / "config.json").write_text(json.dumps({"prefix": "locked", "version": 1}))
        conn = sqlite3.connect(filigree_dir / "filigree.db")
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        conn.close()

        # Simulate OperationalError on PRAGMA user_version (transient lock)
        from filigree.server import _read_project_schema_version

        with patch("filigree.server.sqlite3.connect") as mock_connect:
            mock_conn = mock_connect.return_value
            mock_conn.execute.side_effect = sqlite3.OperationalError("database is locked")
            version = _read_project_schema_version(filigree_dir)
        # Should return 0 (safe default), not raise
        assert version == 0


class TestConfigValidation:
    """Bugs filigree-11862e / filigree-ddceff: read_server_config schema validation."""

    def _setup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        return config_dir

    def test_non_dict_json_returns_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = self._setup(tmp_path, monkeypatch)
        (config_dir / "server.json").write_text('["a", "list"]')
        config = read_server_config()
        assert config.port == 8377
        assert config.projects == {}

    def test_string_port_coerced_to_int(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = self._setup(tmp_path, monkeypatch)
        (config_dir / "server.json").write_text('{"port": "9000"}')
        config = read_server_config()
        assert config.port == 9000

    def test_non_numeric_port_returns_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = self._setup(tmp_path, monkeypatch)
        (config_dir / "server.json").write_text('{"port": "not-a-number"}')
        config = read_server_config()
        assert config.port == 8377

    def test_bool_port_returns_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = self._setup(tmp_path, monkeypatch)
        (config_dir / "server.json").write_text('{"port": true}')
        config = read_server_config()
        assert config.port == 8377

    def test_fractional_port_returns_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = self._setup(tmp_path, monkeypatch)
        (config_dir / "server.json").write_text('{"port": 9000.5}')
        config = read_server_config()
        assert config.port == 8377

    def test_out_of_range_port_returns_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = self._setup(tmp_path, monkeypatch)
        (config_dir / "server.json").write_text('{"port": 99999}')
        config = read_server_config()
        assert config.port == 8377

    def test_negative_port_returns_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = self._setup(tmp_path, monkeypatch)
        (config_dir / "server.json").write_text('{"port": -1}')
        config = read_server_config()
        assert config.port == 8377

    def test_port_zero_returns_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = self._setup(tmp_path, monkeypatch)
        (config_dir / "server.json").write_text('{"port": 0}')
        config = read_server_config()
        assert config.port == 8377

    def test_non_dict_projects_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = self._setup(tmp_path, monkeypatch)
        (config_dir / "server.json").write_text('{"projects": "not-a-dict"}')
        config = read_server_config()
        assert config.projects == {}

    def test_non_dict_project_values_dropped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = self._setup(tmp_path, monkeypatch)
        (config_dir / "server.json").write_text('{"projects": {"/good": {"prefix": "a"}, "/bad": "string-value"}}')
        config = read_server_config()
        assert "/good" in config.projects
        assert "/bad" not in config.projects

    def test_project_values_with_non_string_prefix_dropped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = self._setup(tmp_path, monkeypatch)
        (config_dir / "server.json").write_text('{"projects": {"/good": {"prefix": "a"}, "/bad": {"prefix": ["not", "hashable"]}}}')
        config = read_server_config()
        assert config.projects == {"/good": {"prefix": "a"}}

    def test_null_json_returns_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = self._setup(tmp_path, monkeypatch)
        (config_dir / "server.json").write_text("null")
        config = read_server_config()
        assert config.port == 8377

    def test_empty_config_file_returns_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = self._setup(tmp_path, monkeypatch)
        (config_dir / "server.json").write_text("")
        config = read_server_config()
        assert config.port == 8377
        assert config.projects == {}


class TestDaemonStatusInvariant:
    """Bug filigree-2c232d: DaemonStatus must reject running=True with pid=None."""

    def test_running_true_requires_pid_and_port(self) -> None:
        from filigree.server import DaemonStatus

        with pytest.raises(ValueError, match="requires both pid and port"):
            DaemonStatus(running=True, pid=None, port=None)

    def test_running_true_requires_port(self) -> None:
        from filigree.server import DaemonStatus

        with pytest.raises(ValueError, match="requires both pid and port"):
            DaemonStatus(running=True, pid=123, port=None)

    def test_running_false_allows_none(self) -> None:
        from filigree.server import DaemonStatus

        status = DaemonStatus(running=False)
        assert status.pid is None
        assert status.port is None

    def test_running_true_with_both_succeeds(self) -> None:
        from filigree.server import DaemonStatus

        status = DaemonStatus(running=True, pid=123, port=8377)
        assert status.pid == 123
        assert status.port == 8377


class TestCorruptConfigBackup:
    """Bug filigree-c4d454: corrupt server.json should be backed up before defaults overwrite it."""

    def _setup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        return config_dir

    def test_corrupt_json_creates_backup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = self._setup(tmp_path, monkeypatch)
        corrupt_content = "{this is not valid json!!!"
        (config_dir / "server.json").write_text(corrupt_content)

        config = read_server_config()
        assert config.port == 8377  # Returns defaults

        backup = config_dir / "server.json.bak"
        assert backup.exists()
        assert backup.read_text() == corrupt_content

    def test_non_dict_json_creates_backup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = self._setup(tmp_path, monkeypatch)
        non_dict_content = '["a", "list"]'
        (config_dir / "server.json").write_text(non_dict_content)

        read_server_config()

        backup = config_dir / "server.json.bak"
        assert backup.exists()
        assert backup.read_text() == non_dict_content

    def test_valid_config_no_backup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = self._setup(tmp_path, monkeypatch)
        (config_dir / "server.json").write_text('{"port": 9000}')

        config = read_server_config()
        assert config.port == 9000

        backup = config_dir / "server.json.bak"
        assert not backup.exists()


@pytest.mark.slow
class TestPidOwnership:
    """Bug filigree-f56a78: start_daemon/daemon_status must verify PID ownership."""

    def test_start_daemon_clears_stale_foreign_pid(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """If PID file points to a live non-filigree process, start should proceed."""
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        pid_file = config_dir / "server.pid"
        pid_file.write_text(json.dumps({"pid": 99999, "cmd": "filigree"}))

        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", pid_file)

        # PID is alive but NOT a filigree process
        monkeypatch.setattr("filigree.server.is_pid_alive", lambda pid: True)
        monkeypatch.setattr("filigree.server.verify_pid_ownership", lambda *a, **kw: False)

        def mock_popen(cmd, **kwargs):  # type: ignore[no-untyped-def]
            mock = MagicMock()
            mock.pid = 11111
            mock.poll.return_value = None
            return mock

        monkeypatch.setattr("filigree.server.subprocess.Popen", mock_popen)

        from filigree.server import start_daemon

        result = start_daemon()
        assert result.success
        assert "11111" in result.message

    def test_daemon_status_not_running_for_foreign_pid(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """If PID file points to a live non-filigree process, status should be not running."""
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        pid_file = config_dir / "server.pid"
        pid_file.write_text(json.dumps({"pid": 99999, "cmd": "filigree"}))

        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", pid_file)
        monkeypatch.setattr("filigree.server.is_pid_alive", lambda pid: True)
        monkeypatch.setattr("filigree.server.verify_pid_ownership", lambda *a, **kw: False)

        from filigree.server import daemon_status

        status = daemon_status()
        assert not status.running


@pytest.mark.slow
class TestDaemonLifecycle:
    def test_start_writes_pid_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", config_dir / "server.pid")

        spawned: list[list[str]] = []

        def mock_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
            mock = MagicMock()
            mock.pid = 54321
            mock.poll.return_value = None
            spawned.append(cmd)
            return mock

        monkeypatch.setattr("filigree.server.subprocess.Popen", mock_popen)
        from filigree.server import start_daemon

        result = start_daemon()
        assert result.success
        pid_data = json.loads((config_dir / "server.pid").read_text())
        assert pid_data["pid"] == 54321
        assert pid_data["cmd"] == "filigree dashboard --server-mode"

    def test_start_daemon_immediate_exit_reports_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bug filigree-154be9: if daemon crashes immediately, start should report failure and clean up PID file."""
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        pid_file = config_dir / "server.pid"
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", pid_file)

        log_file = config_dir / "server.log"

        def mock_popen(cmd, **kwargs):  # type: ignore[no-untyped-def]
            # Write something to the log file to simulate stderr output
            log_file.write_text("ImportError: no module named uvicorn")
            mock = MagicMock()
            mock.pid = 99999
            mock.poll.return_value = 1  # Exited immediately with code 1
            return mock

        monkeypatch.setattr("filigree.server.subprocess.Popen", mock_popen)
        monkeypatch.setattr("filigree.server.time.sleep", lambda _: None)

        from filigree.server import start_daemon

        result = start_daemon()
        assert not result.success
        assert "exited immediately" in result.message.lower()
        assert "code 1" in result.message
        # PID file should be cleaned up
        assert not pid_file.exists()

    def test_stop_kills_process(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        pid_file = config_dir / "server.pid"
        pid_file.write_text(json.dumps({"pid": 54321, "cmd": "filigree"}))
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", pid_file)

        killed: list[int] = []
        monkeypatch.setattr("os.kill", lambda pid, sig: killed.append(pid))

        # Mock is_pid_alive: first call returns True (so stop proceeds to kill),
        # subsequent calls return False (process exited after SIGTERM)
        alive_calls: list[int] = []

        def mock_is_pid_alive(pid: int) -> bool:
            alive_calls.append(pid)
            return len(alive_calls) == 1  # True first time only

        monkeypatch.setattr("filigree.server.is_pid_alive", mock_is_pid_alive)
        monkeypatch.setattr("filigree.server.verify_pid_ownership", lambda *a, **kw: True)

        from filigree.server import stop_daemon

        result = stop_daemon()
        assert result.success
        assert 54321 in killed

    def test_start_daemon_passes_server_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """start_daemon() must include --server-mode in the spawned command."""
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", config_dir / "server.pid")

        spawned: list[list[str]] = []

        def mock_popen(cmd: list[str], **kwargs: object) -> MagicMock:
            mock = MagicMock()
            mock.pid = 99999
            mock.poll.return_value = None
            spawned.append(cmd)
            return mock

        monkeypatch.setattr("filigree.server.subprocess.Popen", mock_popen)
        from filigree.server import start_daemon

        result = start_daemon()
        assert result.success
        assert len(spawned) == 1
        assert "--server-mode" in spawned[0]

    def test_start_daemon_persists_port_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", config_dir / "server.pid")

        def mock_popen(cmd: list[str], **kwargs: object) -> MagicMock:
            mock = MagicMock()
            mock.pid = 88888
            mock.poll.return_value = None
            return mock

        monkeypatch.setattr("filigree.server.subprocess.Popen", mock_popen)
        from filigree.server import start_daemon

        result = start_daemon(port=9911)
        assert result.success
        cfg = read_server_config()
        assert cfg.port == 9911

    def test_status_reports_not_running(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", config_dir / "server.pid")

        from filigree.server import daemon_status

        status = daemon_status()
        assert not status.running

    def test_claim_current_process_as_daemon_writes_pid_and_port(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", config_dir / "server.pid")

        from filigree.server import claim_current_process_as_daemon, read_server_config

        assert claim_current_process_as_daemon(port=9911)
        pid_data = json.loads((config_dir / "server.pid").read_text())
        assert pid_data["pid"] > 0
        assert pid_data["cmd"] == "filigree dashboard --server-mode"
        assert read_server_config().port == 9911

    def test_claim_succeeds_when_tracked_pid_is_foreign_process(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """PID file exists, PID alive, but NOT filigree → should claim successfully."""
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        pid_file = config_dir / "server.pid"
        pid_file.write_text(json.dumps({"pid": 54321, "cmd": "filigree"}))
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", pid_file)

        # PID alive but NOT a filigree process
        monkeypatch.setattr("filigree.server.is_pid_alive", lambda pid: pid == 54321)
        monkeypatch.setattr("filigree.server.verify_pid_ownership", lambda *a, **kw: False)

        from filigree.server import claim_current_process_as_daemon

        assert claim_current_process_as_daemon(port=9911)

    def test_claim_current_process_as_daemon_refuses_live_filigree_pid(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        pid_file = config_dir / "server.pid"
        pid_file.write_text(json.dumps({"pid": 54321, "cmd": "filigree"}))
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", pid_file)
        monkeypatch.setattr("filigree.server.is_pid_alive", lambda pid: pid == 54321)
        monkeypatch.setattr("filigree.server.verify_pid_ownership", lambda *a, **kw: True)

        from filigree.server import claim_current_process_as_daemon

        assert not claim_current_process_as_daemon(port=9911)
        pid_data = json.loads(pid_file.read_text())
        assert pid_data["pid"] == 54321

    def test_release_daemon_pid_if_owned(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        pid_file = config_dir / "server.pid"
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", pid_file)

        from filigree.server import release_daemon_pid_if_owned

        pid_file.write_text(json.dumps({"pid": 123, "cmd": "filigree"}))
        release_daemon_pid_if_owned(999)
        assert pid_file.exists()

        release_daemon_pid_if_owned(123)
        assert not pid_file.exists()

    def test_release_daemon_pid_if_owned_leaves_pid_on_rename_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        pid_file = config_dir / "server.pid"
        pid_file.write_text(json.dumps({"pid": 123, "cmd": "filigree"}))
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", pid_file)

        original_rename = Path.rename

        def _rename(self: Path, target: Path) -> Path:
            if self == pid_file:
                raise OSError("permission denied")
            return original_rename(self, target)

        monkeypatch.setattr(Path, "rename", _rename)

        from filigree.server import release_daemon_pid_if_owned

        release_daemon_pid_if_owned(123)
        assert pid_file.exists()


class TestClaimDaemonLocking:
    """Bug filigree-f0707e: claim_current_process_as_daemon must acquire file lock."""

    def test_claim_acquires_flock(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """claim_current_process_as_daemon must use portalocker.lock like start_daemon."""
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", config_dir / "server.pid")

        lock_calls: list[tuple[Any, Any]] = []
        original_lock = portalocker.lock

        def _spy_lock(fd: Any, operation: Any) -> None:
            lock_calls.append((fd, operation))
            return original_lock(fd, operation)

        monkeypatch.setattr("filigree.server.portalocker.lock", _spy_lock)

        from filigree.server import claim_current_process_as_daemon

        assert claim_current_process_as_daemon(port=9911)
        assert len(lock_calls) >= 1, "claim_current_process_as_daemon must acquire portalocker.lock"
        # Verify exclusive lock was requested
        assert any(op & portalocker.LOCK_EX for _, op in lock_calls), "Must use LOCK_EX"


class TestStopDaemonEarlyReturns:
    """Bug filigree-6908d9: stop_daemon() early-return branches need test coverage."""

    def test_stop_daemon_no_pid_file_returns_not_running(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """No PID file at all → success=False, descriptive message."""
        config_dir = tmp_path / ".config" / "filigree"
        pid_file = config_dir / "server.pid"
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", pid_file)

        from filigree.server import stop_daemon

        result = stop_daemon()
        assert not result.success
        assert "No PID file" in result.message

    def test_stop_daemon_dead_pid_cleans_up_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """PID file exists but process is dead → success=True, PID file removed."""
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        pid_file = config_dir / "server.pid"
        pid_file.write_text(json.dumps({"pid": 99999, "cmd": "filigree"}))
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", pid_file)
        monkeypatch.setattr("filigree.server.is_pid_alive", lambda pid: False)

        from filigree.server import stop_daemon

        result = stop_daemon()
        assert result.success
        assert "not running" in result.message.lower()
        assert not pid_file.exists(), "PID file must be cleaned up when process is dead"

    def test_stop_daemon_acquires_server_lock(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """stop_daemon must serialize PID-file read/cleanup with start/claim writers."""
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        pid_file = config_dir / "server.pid"
        pid_file.write_text(json.dumps({"pid": 99999, "cmd": "filigree"}))
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", pid_file)
        monkeypatch.setattr("filigree.server.is_pid_alive", lambda pid: False)

        lock_ops: list[int] = []
        original_lock = portalocker.lock

        def tracking_lock(fd: object, op: int) -> None:
            lock_ops.append(op)
            original_lock(fd, op)  # type: ignore[arg-type]

        monkeypatch.setattr("filigree.server.portalocker.lock", tracking_lock)

        from filigree.server import stop_daemon

        result = stop_daemon()

        assert result.success
        assert portalocker.LOCK_EX in lock_ops


@pytest.mark.slow
class TestStartDaemonLocking:
    """Bug filigree-f6c971: start_daemon must serialize with portalocker.lock."""

    def test_start_daemon_acquires_lock(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", config_dir / "server.pid")

        lock_ops: list[int] = []
        original_lock = portalocker.lock

        def tracking_lock(fd: object, op: int) -> None:
            lock_ops.append(op)
            original_lock(fd, op)  # type: ignore[arg-type]

        monkeypatch.setattr("filigree.server.portalocker.lock", tracking_lock)

        def mock_popen(cmd, **kwargs):  # type: ignore[no-untyped-def]
            mock = MagicMock()
            mock.pid = 77777
            mock.poll.return_value = None
            return mock

        monkeypatch.setattr("filigree.server.subprocess.Popen", mock_popen)

        from filigree.server import start_daemon

        result = start_daemon()
        assert result.success
        assert portalocker.LOCK_EX in lock_ops


class TestStartDaemonAlreadyRunning:
    """Bug filigree-075ff0: start_daemon must detect already-running filigree daemon."""

    def test_start_daemon_already_running_prevents_duplicate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """If PID file points to a live filigree process, start must fail (no duplicate spawn)."""
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        pid_file = config_dir / "server.pid"
        pid_file.write_text(json.dumps({"pid": 12345, "cmd": "filigree"}))
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", pid_file)

        # PID is alive AND is a filigree process
        monkeypatch.setattr("filigree.server.is_pid_alive", lambda pid: True)
        monkeypatch.setattr("filigree.server.verify_pid_ownership", lambda *a, **kw: True)

        popen_calls: list[object] = []

        def mock_popen(cmd, **kwargs):  # type: ignore[no-untyped-def]
            popen_calls.append(cmd)
            mock = MagicMock()
            mock.pid = 99999
            mock.poll.return_value = None
            return mock

        monkeypatch.setattr("filigree.server.subprocess.Popen", mock_popen)

        from filigree.server import start_daemon

        result = start_daemon()
        assert not result.success
        assert "already running" in result.message.lower()
        assert popen_calls == [], "Must NOT spawn a new process when daemon already running"


class TestStartDaemonPopenFailure:
    """B2 from plan review: Popen OSError must return DaemonResult, not raise."""

    def test_start_returns_failure_when_popen_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", config_dir / "server.pid")

        def raising_popen(cmd, **kwargs):  # type: ignore[no-untyped-def]
            raise FileNotFoundError("filigree command not found")

        monkeypatch.setattr("filigree.server.subprocess.Popen", raising_popen)

        from filigree.server import start_daemon

        result = start_daemon()
        assert not result.success
        assert "Failed to start" in result.message


class TestStopDaemonRefusesNonFiligree:
    """Bug filigree-250c9a: stop_daemon must refuse to kill non-filigree processes."""

    def test_stop_daemon_refuses_non_filigree_pid(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """PID alive but not filigree → success=False, refuses to kill."""
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        pid_file = config_dir / "server.pid"
        pid_file.write_text(json.dumps({"pid": 54321, "cmd": "filigree"}))
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", pid_file)
        monkeypatch.setattr("filigree.server.is_pid_alive", lambda pid: True)
        monkeypatch.setattr("filigree.server.verify_pid_ownership", lambda *a, **kw: False)

        kills: list[int] = []
        monkeypatch.setattr("os.kill", lambda pid, sig: kills.append(pid))

        from filigree.server import stop_daemon

        result = stop_daemon()
        assert not result.success
        assert "refusing to kill" in result.message.lower()
        assert kills == [], "Must NOT send any signal to non-filigree process"


class TestStopDaemonSigkill:
    """Bug filigree-186813: stop_daemon must verify kill succeeded after SIGKILL."""

    def test_stop_succeeds_when_process_dies_before_sigterm(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """os.kill raises ProcessLookupError on SIGTERM → success + PID cleanup."""
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        pid_file = config_dir / "server.pid"
        pid_file.write_text(json.dumps({"pid": 54321, "cmd": "filigree"}))
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", pid_file)

        monkeypatch.setattr("filigree.server.is_pid_alive", lambda pid: True)
        monkeypatch.setattr("filigree.server.verify_pid_ownership", lambda *a, **kw: True)

        def mock_kill(pid: int, sig: int) -> None:
            raise ProcessLookupError("No such process")

        monkeypatch.setattr("os.kill", mock_kill)

        from filigree.server import stop_daemon

        result = stop_daemon()
        assert result.success
        assert not pid_file.exists()

    def test_stop_returns_failure_when_sigkill_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        pid_file = config_dir / "server.pid"
        pid_file.write_text(json.dumps({"pid": 54321, "cmd": "filigree"}))
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", pid_file)

        # Process is always alive — survives both SIGTERM and SIGKILL
        monkeypatch.setattr("filigree.server.is_pid_alive", lambda pid: True)
        monkeypatch.setattr("filigree.server.verify_pid_ownership", lambda *a, **kw: True)
        monkeypatch.setattr("os.kill", lambda pid, sig: None)
        monkeypatch.setattr("time.sleep", lambda _: None)

        from filigree.server import stop_daemon

        result = stop_daemon()
        assert not result.success
        assert "SIGKILL" in result.message or "Failed" in result.message

    def test_stop_succeeds_when_sigkill_kills_process(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        pid_file = config_dir / "server.pid"
        pid_file.write_text(json.dumps({"pid": 54321, "cmd": "filigree"}))
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", pid_file)

        # Process survives SIGTERM (50 checks), but dies after SIGKILL
        alive_count = 0

        def mock_alive(pid: int) -> bool:
            nonlocal alive_count
            alive_count += 1
            # First call (stop_daemon liveness check) + 50 SIGTERM waits = 51 calls alive
            # After SIGKILL (call 52+) = dead
            return alive_count <= 51

        monkeypatch.setattr("filigree.server.is_pid_alive", mock_alive)
        monkeypatch.setattr("filigree.server.verify_pid_ownership", lambda *a, **kw: True)
        monkeypatch.setattr("os.kill", lambda pid, sig: None)
        monkeypatch.setattr("time.sleep", lambda _: None)

        from filigree.server import stop_daemon

        result = stop_daemon()
        assert result.success
        assert "Force-killed" in result.message

    def test_stop_handles_sigkill_permission_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        pid_file = config_dir / "server.pid"
        pid_file.write_text(json.dumps({"pid": 54321, "cmd": "filigree"}))
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", pid_file)

        monkeypatch.setattr("filigree.server.is_pid_alive", lambda pid: True)
        monkeypatch.setattr("filigree.server.verify_pid_ownership", lambda *a, **kw: True)
        monkeypatch.setattr("time.sleep", lambda _: None)

        kill_count = 0

        def mock_kill(pid: int, sig: int) -> None:
            nonlocal kill_count
            kill_count += 1
            if kill_count > 1:  # SIGKILL (second kill call)
                raise PermissionError("Operation not permitted")

        monkeypatch.setattr("os.kill", mock_kill)

        from filigree.server import stop_daemon

        result = stop_daemon()
        assert not result.success
        assert "Permission denied" in result.message

    def test_stop_returns_failure_on_sigterm_permission_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """PermissionError on initial SIGTERM (e.g., daemon owned by different user)."""
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        pid_file = config_dir / "server.pid"
        pid_file.write_text(json.dumps({"pid": 54321, "cmd": "filigree"}))
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", pid_file)

        monkeypatch.setattr("filigree.server.is_pid_alive", lambda pid: True)
        monkeypatch.setattr("filigree.server.verify_pid_ownership", lambda *a, **kw: True)

        def mock_kill(pid: int, sig: int) -> None:
            raise PermissionError("Operation not permitted")

        monkeypatch.setattr("os.kill", mock_kill)

        from filigree.server import stop_daemon

        result = stop_daemon()
        assert not result.success
        assert "Permission denied" in result.message

    def test_sigkill_failure_still_cleans_pid_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """B1 from plan review: PID file must be cleaned up even when SIGKILL fails."""
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        pid_file = config_dir / "server.pid"
        pid_file.write_text(json.dumps({"pid": 54321, "cmd": "filigree"}))
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", pid_file)

        monkeypatch.setattr("filigree.server.is_pid_alive", lambda pid: True)
        monkeypatch.setattr("filigree.server.verify_pid_ownership", lambda *a, **kw: True)
        monkeypatch.setattr("os.kill", lambda pid, sig: None)
        monkeypatch.setattr("time.sleep", lambda _: None)

        from filigree.server import stop_daemon

        result = stop_daemon()
        assert not result.success
        # Critical: PID file must be removed to prevent stuck state
        assert not pid_file.exists()
