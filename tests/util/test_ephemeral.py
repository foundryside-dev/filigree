from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

from filigree.ephemeral import (
    _matches_expected_process,
    _read_os_command_line,
    _tokens_contain_args,
    cleanup_legacy_tmp_files,
    cleanup_stale_pid,
    compute_port,
    find_available_port,
    is_pid_alive,
    read_pid_file,
    read_port_file,
    verify_pid_ownership,
    write_pid_file,
    write_port_file,
)


class TestTokensContainArgs:
    def test_empty_required_args(self) -> None:
        assert _tokens_contain_args(["foo", "bar"], ()) is True

    def test_single_match(self) -> None:
        assert _tokens_contain_args(["dashboard", "--port", "8400"], ("dashboard",)) is True

    def test_in_order_match(self) -> None:
        assert _tokens_contain_args(["dashboard", "--server-mode"], ("dashboard", "--server-mode")) is True

    def test_out_of_order_rejects(self) -> None:
        assert _tokens_contain_args(["--server-mode", "dashboard"], ("dashboard", "--server-mode")) is False

    def test_missing_token_rejects(self) -> None:
        assert _tokens_contain_args(["dashboard"], ("dashboard", "--server-mode")) is False

    def test_empty_tokens(self) -> None:
        assert _tokens_contain_args([], ("dashboard",)) is False

    def test_case_insensitive(self) -> None:
        assert _tokens_contain_args(["Dashboard", "--Server-Mode"], ("dashboard", "--server-mode")) is True


class TestMatchesExpectedProcess:
    def test_empty_tokens(self) -> None:
        assert _matches_expected_process([], expected_cmd="filigree") is False

    def test_direct_executable(self) -> None:
        assert _matches_expected_process(["/usr/bin/filigree", "dashboard"], expected_cmd="filigree") is True

    def test_unrelated_executable_with_prefix_rejected(self) -> None:
        """M3: filigree-unrelated-tool must NOT match 'filigree'."""
        assert _matches_expected_process(["filigree-dashboard", "serve"], expected_cmd="filigree") is False
        assert _matches_expected_process(["filigree-unrelated-tool"], expected_cmd="filigree") is False

    def test_executable_with_exe_suffix(self) -> None:
        """Windows .exe suffix should match after stripping."""
        assert _matches_expected_process(["filigree.exe", "dashboard"], expected_cmd="filigree") is True

    def test_python_module_invocation(self) -> None:
        assert _matches_expected_process(["python", "-m", "filigree", "dashboard"], expected_cmd="filigree") is True

    def test_python_module_dotted(self) -> None:
        assert _matches_expected_process(["python", "-m", "filigree.cli", "serve"], expected_cmd="filigree") is True

    def test_unrelated_module_rejects(self) -> None:
        assert _matches_expected_process(["python", "-m", "other_tool", "serve"], expected_cmd="filigree") is False

    def test_required_args_checked(self) -> None:
        assert (
            _matches_expected_process(
                ["/usr/bin/filigree", "dashboard", "--server-mode"],
                expected_cmd="filigree",
                required_args=("dashboard",),
            )
            is True
        )

    def test_required_args_missing(self) -> None:
        assert (
            _matches_expected_process(
                ["/usr/bin/filigree", "session-context"],
                expected_cmd="filigree",
                required_args=("dashboard",),
            )
            is False
        )

    def test_second_arg_as_script_path(self) -> None:
        assert (
            _matches_expected_process(
                ["python", "/usr/local/bin/filigree", "serve"],
                expected_cmd="filigree",
            )
            is True
        )


class TestComputePort:
    def test_deterministic_for_same_path(self) -> None:
        """Same path always produces same port."""
        p = Path("/home/john/myproject/.filigree")
        assert compute_port(p) == compute_port(p)

    def test_in_valid_range(self) -> None:
        """Port is between 8400 and 9399."""
        p = Path("/home/john/myproject/.filigree")
        port = compute_port(p)
        assert 8400 <= port <= 9399

    def test_different_paths_likely_different_ports(self) -> None:
        """Different paths are unlikely to produce the same port."""
        ports = {compute_port(Path(f"/project-{i}/.filigree")) for i in range(20)}
        # With 1000-slot range and 20 samples, collisions are possible but
        # getting fewer than 15 unique ports would be suspicious
        assert len(ports) >= 15


class TestFindAvailablePort:
    def test_returns_deterministic_port_when_free(self) -> None:
        """When the deterministic port is free, use it."""
        p = Path("/home/john/myproject/.filigree")
        expected = compute_port(p)
        port = find_available_port(p)
        assert port == expected

    def test_skips_occupied_port(self) -> None:
        """When deterministic port is occupied, tries next ones."""
        p = Path("/home/john/myproject/.filigree")
        base = compute_port(p)
        # Occupy the base port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", base))
        sock.listen(1)
        try:
            port = find_available_port(p)
            assert port != base
            assert port > base  # should try sequential ports next
        finally:
            sock.close()

    def test_tries_base_plus_retries_candidates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Loop tries 1 base + PORT_RETRIES sequential ports before OS fallback."""
        from filigree import ephemeral

        checked: list[int] = []

        def fake_is_port_free(port: int) -> bool:
            checked.append(port)
            return False  # all ports "occupied"

        monkeypatch.setattr(ephemeral, "_is_port_free", fake_is_port_free)
        port = find_available_port(Path("/some/project/.filigree"))

        # Should check base + PORT_RETRIES sequential candidates
        assert len(checked) == ephemeral.PORT_RETRIES + 1
        # OS fallback must return a valid port
        assert 1 <= port <= 65535

    def test_socket_permission_error_raises_runtime_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("filigree.ephemeral.socket.socket", lambda *_a, **_k: (_ for _ in ()).throw(PermissionError(1, "denied")))
        with pytest.raises(RuntimeError):
            find_available_port(Path("/some/project/.filigree"))


class TestPidLifecycle:
    def test_write_and_read_pid(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "ephemeral.pid"
        write_pid_file(pid_file, 12345, cmd="filigree")
        info = read_pid_file(pid_file)
        assert info is not None
        assert info["pid"] == 12345
        assert info["cmd"] == "filigree"

    def test_read_missing_pid_returns_none(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "ephemeral.pid"
        assert read_pid_file(pid_file) is None

    def test_read_corrupt_pid_returns_none(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "ephemeral.pid"
        pid_file.write_text("not-json")
        assert read_pid_file(pid_file) is None

    def test_read_legacy_plain_pid(self, tmp_path: Path) -> None:
        """Backward compat: plain integer PID files still work."""
        pid_file = tmp_path / "ephemeral.pid"
        pid_file.write_text("12345")
        info = read_pid_file(pid_file)
        assert info is not None
        assert info["pid"] == 12345
        assert info["cmd"] == "unknown"

    def test_read_non_positive_pid_returns_none(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "ephemeral.pid"
        pid_file.write_text("0")
        assert read_pid_file(pid_file) is None

    def test_read_corrupt_pid_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Bug filigree-0c570e: corrupt PID file must log a warning, not silently return None."""
        import logging

        pid_file = tmp_path / "ephemeral.pid"
        pid_file.write_text("not-a-number-or-json!!!")
        with caplog.at_level(logging.WARNING):
            result = read_pid_file(pid_file)
        assert result is None
        assert any("corrupt" in r.message.lower() or "pid" in r.message.lower() for r in caplog.records), (
            "read_pid_file must log a warning when PID file exists but can't be parsed"
        )

    def test_read_pid_json_array_returns_none(self, tmp_path: Path) -> None:
        """Non-dict JSON (array) should return None with a warning, not fall through."""
        pid_file = tmp_path / "ephemeral.pid"
        pid_file.write_text("[12345]")
        assert read_pid_file(pid_file) is None

    def test_read_pid_json_string_returns_none(self, tmp_path: Path) -> None:
        """Non-dict JSON (string) should return None with a warning, not fall through."""
        pid_file = tmp_path / "ephemeral.pid"
        pid_file.write_text('"12345"')
        assert read_pid_file(pid_file) is None

    def test_read_pid_json_dict_without_pid_returns_none(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """JSON dict missing 'pid' key should return None with a specific warning."""
        import logging

        pid_file = tmp_path / "ephemeral.pid"
        pid_file.write_text('{"cmd": "foo"}')
        with caplog.at_level(logging.WARNING):
            result = read_pid_file(pid_file)
        assert result is None
        assert any("missing 'pid' key" in r.message for r in caplog.records)

    def test_read_pid_rejects_float_pid(self, tmp_path: Path) -> None:
        """filigree-626c12d368: float pid must be rejected (no silent truncation)."""
        pid_file = tmp_path / "ephemeral.pid"
        pid_file.write_text('{"pid": 12.9, "cmd": "filigree"}')
        assert read_pid_file(pid_file) is None

    def test_read_pid_rejects_non_finite_pid(self, tmp_path: Path) -> None:
        """filigree-626c12d368: non-finite pid (1e999 → inf) must not raise OverflowError."""
        pid_file = tmp_path / "ephemeral.pid"
        pid_file.write_text('{"pid": 1e999, "cmd": "filigree"}')
        # Must return None — and must not propagate OverflowError.
        assert read_pid_file(pid_file) is None

    def test_read_pid_rejects_bool_pid(self, tmp_path: Path) -> None:
        """filigree-626c12d368: bool is an int subclass; it must be rejected, not coerced to 1."""
        pid_file = tmp_path / "ephemeral.pid"
        pid_file.write_text('{"pid": true, "cmd": "filigree"}')
        assert read_pid_file(pid_file) is None

    def test_read_pid_rejects_float_port(self, tmp_path: Path) -> None:
        """filigree-626c12d368: float port must be ignored (info returned without 'port')."""
        pid_file = tmp_path / "ephemeral.pid"
        pid_file.write_text('{"pid": 1234, "cmd": "filigree", "port": 8401.9}')
        info = read_pid_file(pid_file)
        assert info is not None
        assert info["pid"] == 1234
        assert "port" not in info, "float port must not be silently truncated"

    def test_read_pid_ignores_non_finite_startup_ts(self, tmp_path: Path) -> None:
        """Non-finite startup_ts must not keep dead dashboards in startup grace forever."""
        pid_file = tmp_path / "ephemeral.pid"
        pid_file.write_text('{"pid": 1234, "cmd": "filigree dashboard", "startup_ts": 1e999}')
        info = read_pid_file(pid_file)
        assert info is not None
        assert info["pid"] == 1234
        assert "startup_ts" not in info

    def test_is_pid_alive_for_self(self) -> None:
        assert is_pid_alive(os.getpid()) is True

    def test_is_pid_alive_for_dead(self) -> None:
        assert is_pid_alive(99999999) is False

    def test_is_pid_alive_non_positive_false(self) -> None:
        assert is_pid_alive(0) is False
        assert is_pid_alive(-1) is False

    def test_is_pid_alive_permission_denied_means_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _deny_kill(_pid: int, _sig: int) -> None:
            raise PermissionError(1, "operation not permitted")

        monkeypatch.setattr("filigree.ephemeral.os.kill", _deny_kill)
        assert is_pid_alive(12345) is True

    def test_verify_pid_ownership_for_self(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "ephemeral.pid"
        write_pid_file(pid_file, os.getpid(), cmd="python")
        expected = Path(sys.executable).name
        assert verify_pid_ownership(pid_file, expected_cmd=expected) is True

    def test_verify_pid_ownership_wrong_cmd(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "ephemeral.pid"
        write_pid_file(pid_file, os.getpid(), cmd="filigree")
        assert verify_pid_ownership(pid_file, expected_cmd="definitely-not-real") is False

    def test_verify_pid_ownership_ignores_stale_pid_file_cmd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pid_file = tmp_path / "ephemeral.pid"
        write_pid_file(pid_file, os.getpid(), cmd="filigree")
        monkeypatch.setattr("filigree.ephemeral._read_os_command_line", lambda _pid: ["python", "worker.py"])
        assert verify_pid_ownership(pid_file, expected_cmd="filigree") is False

    def test_verify_pid_ownership_accepts_python_module_invocation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pid_file = tmp_path / "ephemeral.pid"
        write_pid_file(pid_file, os.getpid(), cmd="filigree")
        monkeypatch.setattr(
            "filigree.ephemeral._read_os_command_line",
            lambda _pid: [sys.executable, "-m", "filigree", "dashboard", "--server-mode"],
        )
        assert verify_pid_ownership(pid_file, expected_cmd="filigree") is True

    def test_verify_pid_ownership_rejects_wrong_filigree_subcommand(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pid_file = tmp_path / "ephemeral.pid"
        write_pid_file(pid_file, os.getpid(), cmd="filigree dashboard")
        monkeypatch.setattr(
            "filigree.ephemeral._read_os_command_line",
            lambda _pid: [sys.executable, "-m", "filigree", "session-context"],
        )
        assert verify_pid_ownership(pid_file, expected_cmd="filigree", required_args=("dashboard",)) is False

    def test_verify_pid_ownership_rejects_unrelated_python_module(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pid_file = tmp_path / "ephemeral.pid"
        write_pid_file(pid_file, os.getpid(), cmd="filigree")
        monkeypatch.setattr(
            "filigree.ephemeral._read_os_command_line",
            lambda _pid: [sys.executable, "-m", "othermodule", "serve"],
        )
        assert verify_pid_ownership(pid_file, expected_cmd="filigree") is False

    def test_verify_pid_ownership_falls_back_to_pid_file_cmd_when_os_lookup_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid_file = tmp_path / "ephemeral.pid"
        write_pid_file(pid_file, os.getpid(), cmd="filigree")
        monkeypatch.setattr("filigree.ephemeral._read_os_command_line", lambda _pid: None)
        assert verify_pid_ownership(pid_file, expected_cmd="filigree") is True

    def test_verify_pid_ownership_fallback_requires_expected_args(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pid_file = tmp_path / "ephemeral.pid"
        write_pid_file(pid_file, os.getpid(), cmd="filigree dashboard --server-mode")
        monkeypatch.setattr("filigree.ephemeral._read_os_command_line", lambda _pid: None)
        assert verify_pid_ownership(pid_file, expected_cmd="filigree", required_args=("dashboard", "--server-mode")) is True
        assert verify_pid_ownership(pid_file, expected_cmd="filigree", required_args=("session-context",)) is False

    def test_verify_pid_ownership_fallback_rejects_mismatched_pid_file_cmd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pid_file = tmp_path / "ephemeral.pid"
        write_pid_file(pid_file, os.getpid(), cmd="not-filigree")
        monkeypatch.setattr("filigree.ephemeral._read_os_command_line", lambda _pid: None)
        assert verify_pid_ownership(pid_file, expected_cmd="filigree") is False

    def test_verify_pid_ownership_returns_false_for_legacy_unknown_cmd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Task filigree-a26e15: legacy plain-integer PID file sets cmd='unknown'.

        verify_pid_ownership should return False because 'unknown' is not a
        trustworthy process identity.
        """
        pid_file = tmp_path / "ephemeral.pid"
        # Simulate a legacy plain-integer PID file
        pid_file.write_text(str(os.getpid()))
        info = read_pid_file(pid_file)
        assert info is not None
        assert info["cmd"] == "unknown"

        # OS command line lookup is unavailable
        monkeypatch.setattr("filigree.ephemeral._read_os_command_line", lambda _pid: None)

        # Should return False because cmd="unknown" is not trustworthy
        assert verify_pid_ownership(pid_file, expected_cmd="filigree") is False

    def test_verify_pid_ownership_rejects_cross_project_port_mismatch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """filigree-563d5454e9: PID record stores the project's port; verify must reject
        a live filigree dashboard running on a different port (different project, possibly
        after PID recycling)."""
        pid_file = tmp_path / "ephemeral.pid"
        # This project's dashboard is recorded at port 8401.
        write_pid_file(pid_file, os.getpid(), cmd="filigree dashboard", port=8401)
        # The live process with that PID is actually another filigree project's
        # dashboard on port 8923 (PID recycling across projects on same host).
        monkeypatch.setattr(
            "filigree.ephemeral._read_os_command_line",
            lambda _pid: ["/usr/bin/filigree", "dashboard", "--no-browser", "--port", "8923"],
        )
        assert verify_pid_ownership(pid_file, expected_cmd="filigree", required_args=("dashboard",)) is False, (
            "must reject when recorded port does not appear in the live process argv"
        )

    def test_verify_pid_ownership_accepts_matching_port(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """filigree-563d5454e9: verify accepts when recorded port matches argv."""
        pid_file = tmp_path / "ephemeral.pid"
        write_pid_file(pid_file, os.getpid(), cmd="filigree dashboard", port=8401)
        monkeypatch.setattr(
            "filigree.ephemeral._read_os_command_line",
            lambda _pid: ["/usr/bin/filigree", "dashboard", "--no-browser", "--port", "8401"],
        )
        assert verify_pid_ownership(pid_file, expected_cmd="filigree", required_args=("dashboard",)) is True

    def test_verify_pid_ownership_rejects_when_effective_port_differs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Repeated --port uses the last value; an earlier matching token is not ownership proof."""
        pid_file = tmp_path / "ephemeral.pid"
        write_pid_file(pid_file, os.getpid(), cmd="filigree dashboard", port=8401)
        monkeypatch.setattr(
            "filigree.ephemeral._read_os_command_line",
            lambda _pid: ["/usr/bin/filigree", "dashboard", "--port", "8401", "--port", "8923"],
        )
        assert verify_pid_ownership(pid_file, expected_cmd="filigree", required_args=("dashboard",)) is False

    def test_verify_pid_ownership_fallback_accepts_record_with_port(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """filigree-403dd029c3: when OS argv is unavailable, the PID-file fallback must
        accept a record where ``cmd`` lacks --port and ``port`` is recorded as
        a separate metadata field (the actual shape callers write)."""
        pid_file = tmp_path / "ephemeral.pid"
        write_pid_file(pid_file, os.getpid(), cmd="filigree dashboard", port=8401)
        monkeypatch.setattr("filigree.ephemeral._read_os_command_line", lambda _pid: None)
        # The fallback must trust the recorded port as metadata; cmd does not
        # (and never will) include --port.
        assert verify_pid_ownership(pid_file, expected_cmd="filigree", required_args=("dashboard",)) is True

    def test_verify_pid_ownership_without_port_is_unchanged(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """filigree-563d5454e9: PID records without a port field behave as before."""
        pid_file = tmp_path / "ephemeral.pid"
        write_pid_file(pid_file, os.getpid(), cmd="filigree dashboard")
        monkeypatch.setattr(
            "filigree.ephemeral._read_os_command_line",
            lambda _pid: ["/usr/bin/filigree", "dashboard", "--no-browser", "--port", "8923"],
        )
        assert verify_pid_ownership(pid_file, expected_cmd="filigree", required_args=("dashboard",)) is True

    def test_verify_pid_ownership_uses_cmdline_before_alive_check(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """TOCTOU fix: cmdline should be checked first, not is_pid_alive.

        If _read_os_command_line returns valid tokens, is_pid_alive should not
        be called (it creates a race window on PID recycling).
        """
        pid_file = tmp_path / "ephemeral.pid"
        write_pid_file(pid_file, os.getpid(), cmd="filigree")

        alive_called = []
        orig_alive = is_pid_alive

        def _tracking_alive(pid: int) -> bool:
            alive_called.append(pid)
            return orig_alive(pid)

        monkeypatch.setattr("filigree.ephemeral.is_pid_alive", _tracking_alive)
        monkeypatch.setattr(
            "filigree.ephemeral._read_os_command_line",
            lambda _pid: ["/usr/bin/filigree", "dashboard"],
        )

        result = verify_pid_ownership(pid_file, expected_cmd="filigree")
        assert result is True
        # is_pid_alive should NOT have been called when cmdline is available
        assert alive_called == [], f"is_pid_alive was called unnecessarily: {alive_called}"

    def test_cleanup_stale_pid_removes_dead(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "ephemeral.pid"
        write_pid_file(pid_file, 99999999, cmd="filigree")
        cleanup_stale_pid(pid_file)
        assert not pid_file.exists()

    def test_cleanup_stale_pid_keeps_alive_filigree(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """PID file should be kept if the process is alive and is filigree dashboard."""
        pid_file = tmp_path / "ephemeral.pid"
        write_pid_file(pid_file, os.getpid(), cmd="filigree dashboard")
        monkeypatch.setattr(
            "filigree.ephemeral._read_os_command_line",
            lambda _pid: ["/usr/bin/filigree", "dashboard"],
        )
        cleanup_stale_pid(pid_file)
        assert pid_file.exists()

    def test_cleanup_stale_pid_does_not_unlink_fresh_pid_written_during_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """filigree-73e909e6cc: if a concurrent session writes a fresh PID between
        cleanup's initial read and the unlink, the fresh file must survive."""
        from filigree import ephemeral as _eph

        pid_file = tmp_path / "ephemeral.pid"
        # Initially, a stale PID file that looks dead.
        write_pid_file(pid_file, 99999999, cmd="filigree dashboard", port=8401)

        fresh_payload = '{"pid": ' + str(os.getpid()) + ', "cmd": "filigree dashboard", "port": 8401}'

        # When cleanup calls verify_pid_ownership, simulate another session
        # taking the lock, writing a fresh PID file, then releasing. The
        # concurrent writer's payload must remain on disk afterwards.
        orig_verify = _eph.verify_pid_ownership

        def _verify_with_concurrent_write(*args: object, **kwargs: object) -> bool:
            result = orig_verify(*args, **kwargs)
            # Concurrent writer
            pid_file.write_text(fresh_payload)
            return result

        monkeypatch.setattr("filigree.ephemeral.verify_pid_ownership", _verify_with_concurrent_write)

        cleanup_stale_pid(pid_file)

        assert pid_file.exists(), "fresh PID file was unlinked by racing cleanup"
        assert pid_file.read_text() == fresh_payload, "fresh PID file content was clobbered"

    def test_cleanup_stale_pid_does_not_clobber_newest_writer(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """filigree-5aa3dc590a: the quarantine-restore branch must not clobber a newer
        primary PID file written by another writer between our quarantine and
        restore. POSIX ``os.rename`` overwrites; we use ``os.link`` so the
        restore atomically fails when pid_file already exists."""
        from filigree import ephemeral as _eph

        pid_file = tmp_path / "ephemeral.pid"
        write_pid_file(pid_file, 99999999, cmd="filigree dashboard", port=8401)

        fresh_a = '{"pid": ' + str(os.getpid()) + ', "cmd": "filigree dashboard", "port": 8401, "startup_ts": 1.0}'
        fresh_b = '{"pid": ' + str(os.getpid()) + ', "cmd": "filigree dashboard", "port": 8401, "startup_ts": 2.0}'

        # Make verify trust our PID as a filigree dashboard.
        monkeypatch.setattr(
            "filigree.ephemeral._read_os_command_line",
            lambda pid: ["/usr/bin/filigree", "dashboard", "--port", "8401"] if pid == os.getpid() else None,
        )

        orig_verify = _eph.verify_pid_ownership
        calls = {"n": 0}

        def patched_verify(path: Path, **kwargs: object) -> bool:
            result = orig_verify(path, **kwargs)
            calls["n"] += 1
            if calls["n"] == 1:
                # Writer A races between our first verify and the rename.
                pid_file.write_text(fresh_a)
            elif calls["n"] == 2:
                # Writer B fills the slot after our quarantine, before restore.
                pid_file.write_text(fresh_b)
            return result

        monkeypatch.setattr("filigree.ephemeral.verify_pid_ownership", patched_verify)

        cleanup_stale_pid(pid_file)

        # Writer B is the most recent legitimate write; it must survive.
        assert pid_file.read_text() == fresh_b, "quarantine restore clobbered the newest writer"

    def test_cleanup_stale_pid_uses_unique_quarantine_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """filigree-5aa3dc590a: the quarantine filename must be unique per cleaner so
        concurrent cleaners cannot collide on the same path."""
        pid_file = tmp_path / "ephemeral.pid"
        write_pid_file(pid_file, 99999999, cmd="filigree dashboard")

        # Make rename observable so we can inspect the quarantine path.
        captured: dict[str, Path] = {}
        orig_rename = Path.rename

        def spy_rename(self: Path, target: str | Path) -> Path:
            if self == pid_file:
                captured["quarantine"] = Path(target)
            return orig_rename(self, target)

        monkeypatch.setattr(Path, "rename", spy_rename)
        cleanup_stale_pid(pid_file)

        q = captured.get("quarantine")
        assert q is not None
        # Quarantine name should not be the legacy fixed ".removing" suffix.
        assert q.name != "ephemeral.pid.removing", f"quarantine name is shared (collision-prone): {q.name}"
        # It should still mark the file as being removed.
        assert ".removing" in q.name

    def test_cleanup_stale_pid_removes_recycled_pid(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """TOCTOU fix: cleanup should remove PID file when PID is alive but not our process."""
        pid_file = tmp_path / "ephemeral.pid"
        write_pid_file(pid_file, os.getpid(), cmd="filigree dashboard")

        # PID is alive (it's our test process), but cmdline doesn't match filigree
        monkeypatch.setattr(
            "filigree.ephemeral._read_os_command_line",
            lambda _pid: ["/usr/bin/some-other-app", "serve"],
        )

        result = cleanup_stale_pid(pid_file)
        assert result is True
        assert not pid_file.exists()


class TestPortFile:
    def test_write_and_read_port(self, tmp_path: Path) -> None:
        port_file = tmp_path / "ephemeral.port"
        write_port_file(port_file, 9173)
        assert read_port_file(port_file) == 9173

    def test_read_missing_port_returns_none(self, tmp_path: Path) -> None:
        port_file = tmp_path / "ephemeral.port"
        assert read_port_file(port_file) is None

    def test_read_corrupt_port_returns_none(self, tmp_path: Path) -> None:
        port_file = tmp_path / "ephemeral.port"
        port_file.write_text("not-a-number\ngarbage")
        assert read_port_file(port_file) is None


class TestReadOsCommandLine:
    """Task filigree-e43f60: tests for _read_os_command_line /proc and ps fallback."""

    @staticmethod
    def _make_path_factory(tmp_path: Path) -> object:
        """Return a callable that redirects Path("/proc") to tmp_path/proc."""
        _orig_path = Path

        def _factory(*args: object, **kwargs: object) -> Path:
            if args == ("/proc",):
                return _orig_path(str(tmp_path / "proc"))
            return _orig_path(*args, **kwargs)  # type: ignore[arg-type]

        return _factory

    @staticmethod
    def _make_noproc_path_factory(tmp_path: Path) -> object:
        """Return a callable that redirects Path("/proc") to a nonexistent dir."""
        _orig_path = Path

        def _factory(*args: object, **kwargs: object) -> Path:
            if args == ("/proc",):
                return _orig_path(str(tmp_path / "no-proc"))
            return _orig_path(*args, **kwargs)  # type: ignore[arg-type]

        return _factory

    def test_proc_cmdline_nul_separated_parsing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """NUL-separated tokens from /proc/{pid}/cmdline are split correctly."""
        fake_proc_dir = tmp_path / "proc" / "12345"
        fake_proc_dir.mkdir(parents=True)
        (fake_proc_dir / "cmdline").write_bytes(b"/usr/bin/filigree\x00dashboard\x00--port\x008400\x00")

        monkeypatch.setattr("filigree.ephemeral.Path", self._make_path_factory(tmp_path))

        result = _read_os_command_line(12345)
        assert result == ["/usr/bin/filigree", "dashboard", "--port", "8400"]

    def test_proc_cmdline_trailing_nuls_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty tokens from trailing NUL bytes are filtered out."""
        fake_proc_dir = tmp_path / "proc" / "99"
        fake_proc_dir.mkdir(parents=True)
        (fake_proc_dir / "cmdline").write_bytes(b"python\x00-m\x00filigree\x00\x00\x00")

        monkeypatch.setattr("filigree.ephemeral.Path", self._make_path_factory(tmp_path))

        result = _read_os_command_line(99)
        assert result == ["python", "-m", "filigree"]

    def test_ps_fallback_when_proc_unavailable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When /proc is not available, falls back to ps and uses shlex.split."""
        import subprocess as _subprocess

        monkeypatch.setattr("filigree.ephemeral.Path", self._make_noproc_path_factory(tmp_path))

        fake_result = _subprocess.CompletedProcess(
            args=["ps", "-p", "12345", "-o", "command="],
            returncode=0,
            stdout="/usr/bin/filigree dashboard --port 8400\n",
            stderr="",
        )
        monkeypatch.setattr("filigree.ephemeral.subprocess.run", lambda *a, **kw: fake_result)

        result = _read_os_command_line(12345)
        assert result == ["/usr/bin/filigree", "dashboard", "--port", "8400"]

    def test_ps_fallback_with_module_invocation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """ps output with python -m invocation is parsed correctly via shlex.split."""
        import subprocess as _subprocess

        monkeypatch.setattr("filigree.ephemeral.Path", self._make_noproc_path_factory(tmp_path))

        fake_result = _subprocess.CompletedProcess(
            args=["ps", "-p", "12345", "-o", "command="],
            returncode=0,
            stdout="/usr/bin/python -m filigree dashboard\n",
            stderr="",
        )
        monkeypatch.setattr("filigree.ephemeral.subprocess.run", lambda *a, **kw: fake_result)

        result = _read_os_command_line(12345)
        assert result == ["/usr/bin/python", "-m", "filigree", "dashboard"]

    def test_ps_fallback_empty_output_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """ps returning empty stdout results in None."""
        import subprocess as _subprocess

        monkeypatch.setattr("filigree.ephemeral.Path", self._make_noproc_path_factory(tmp_path))

        fake_result = _subprocess.CompletedProcess(
            args=["ps", "-p", "12345", "-o", "command="],
            returncode=0,
            stdout="",
            stderr="",
        )
        monkeypatch.setattr("filigree.ephemeral.subprocess.run", lambda *a, **kw: fake_result)

        result = _read_os_command_line(12345)
        assert result is None

    def test_ps_nonzero_returncode_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """ps returning non-zero exit code should be treated as failure, even with stdout."""
        import subprocess as _subprocess

        monkeypatch.setattr("filigree.ephemeral.Path", self._make_noproc_path_factory(tmp_path))

        fake_result = _subprocess.CompletedProcess(
            args=["ps", "-p", "12345", "-o", "command="],
            returncode=1,
            stdout="some garbage output\n",
            stderr="",
        )
        monkeypatch.setattr("filigree.ephemeral.subprocess.run", lambda *a, **kw: fake_result)

        result = _read_os_command_line(12345)
        assert result is None

    def test_both_proc_and_ps_fail_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When both /proc and ps are unavailable, returns None."""
        monkeypatch.setattr("filigree.ephemeral.Path", self._make_noproc_path_factory(tmp_path))

        def _raise_oserror(*args: object, **kwargs: object) -> None:
            raise OSError("ps not found")

        monkeypatch.setattr("filigree.ephemeral.subprocess.run", _raise_oserror)

        result = _read_os_command_line(12345)
        assert result is None


class TestReadOsCommandLineWmic:
    """M6: tests for _read_os_command_line Windows wmic branch."""

    @staticmethod
    def _make_noproc_path_factory(tmp_path: Path) -> object:
        _orig_path = Path

        def _factory(*args: object, **kwargs: object) -> Path:
            if args == ("/proc",):
                return _orig_path(str(tmp_path / "no-proc"))
            return _orig_path(*args, **kwargs)  # type: ignore[arg-type]

        return _factory

    def test_wmic_success_parses_command_line(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """wmic output with CommandLine= line is parsed correctly."""
        import subprocess as _subprocess

        monkeypatch.setattr("filigree.ephemeral.Path", self._make_noproc_path_factory(tmp_path))
        monkeypatch.setattr("filigree.ephemeral.sys.platform", "win32")

        fake_result = _subprocess.CompletedProcess(
            args=["wmic"],
            returncode=0,
            stdout="\r\nCommandLine=python -m filigree dashboard --port 8377\r\n\r\n",
            stderr="",
        )
        monkeypatch.setattr("filigree.ephemeral.subprocess.run", lambda *a, **kw: fake_result)

        result = _read_os_command_line(12345)
        assert result == ["python", "-m", "filigree", "dashboard", "--port", "8377"]

    def test_wmic_nonzero_returncode_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """wmic returning non-zero exit code returns None."""
        import subprocess as _subprocess

        monkeypatch.setattr("filigree.ephemeral.Path", self._make_noproc_path_factory(tmp_path))
        monkeypatch.setattr("filigree.ephemeral.sys.platform", "win32")

        fake_result = _subprocess.CompletedProcess(args=["wmic"], returncode=1, stdout="", stderr="error")
        monkeypatch.setattr("filigree.ephemeral.subprocess.run", lambda *a, **kw: fake_result)

        result = _read_os_command_line(12345)
        assert result is None

    def test_wmic_oserror_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """OSError from wmic subprocess returns None."""
        monkeypatch.setattr("filigree.ephemeral.Path", self._make_noproc_path_factory(tmp_path))
        monkeypatch.setattr("filigree.ephemeral.sys.platform", "win32")

        def _raise_oserror(*args: object, **kwargs: object) -> None:
            raise OSError("wmic not found")

        monkeypatch.setattr("filigree.ephemeral.subprocess.run", _raise_oserror)

        result = _read_os_command_line(12345)
        assert result is None

    def test_wmic_no_commandline_in_output_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """wmic output without CommandLine= prefix returns None."""
        import subprocess as _subprocess

        monkeypatch.setattr("filigree.ephemeral.Path", self._make_noproc_path_factory(tmp_path))
        monkeypatch.setattr("filigree.ephemeral.sys.platform", "win32")

        fake_result = _subprocess.CompletedProcess(args=["wmic"], returncode=0, stdout="No Instance(s) Available.\r\n", stderr="")
        monkeypatch.setattr("filigree.ephemeral.subprocess.run", lambda *a, **kw: fake_result)

        result = _read_os_command_line(12345)
        assert result is None

    def test_wmic_shlex_error_returns_raw(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """wmic CommandLine= with unparseable value falls back to raw string."""
        import subprocess as _subprocess

        monkeypatch.setattr("filigree.ephemeral.Path", self._make_noproc_path_factory(tmp_path))
        monkeypatch.setattr("filigree.ephemeral.sys.platform", "win32")

        # Unclosed quote triggers shlex.split ValueError
        fake_result = _subprocess.CompletedProcess(
            args=["wmic"],
            returncode=0,
            stdout='CommandLine=python -c "unclosed\r\n',
            stderr="",
        )
        monkeypatch.setattr("filigree.ephemeral.subprocess.run", lambda *a, **kw: fake_result)

        result = _read_os_command_line(12345)
        assert result is not None
        assert len(result) == 1  # raw string in a list


class TestLegacyCleanup:
    def test_cleanup_legacy_tmp_files_ignores_permission_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        def fake_unlink(self: Path, *, missing_ok: bool = False) -> None:
            calls.append(self.name)
            if self.name == "filigree-dashboard.pid":
                raise PermissionError("denied")

        monkeypatch.setattr(Path, "unlink", fake_unlink)

        cleanup_legacy_tmp_files()

        assert calls == [
            "filigree-dashboard.pid",
            "filigree-dashboard.lock",
            "filigree-dashboard.log",
        ]
