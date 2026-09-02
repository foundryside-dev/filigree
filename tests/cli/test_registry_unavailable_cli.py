"""Fail-closed Loomweave outage surfaces as an error envelope, not a traceback.

filigree-8fd300e2f7: with ``registry_backend=loomweave`` and
``allow_local_fallback=false``, an unreachable Loomweave used to escape
``cli_common.get_db`` (and the MCP / dashboard DB-open paths) as a raw
``RegistryUnavailableError`` traceback. Every surface must instead emit the
shared ``REGISTRY_UNAVAILABLE`` envelope naming the backend URL and
``cause_kind``, plus an actionable hint (start ``loomweave serve`` or set
``loomweave.allow_local_fallback=true``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

from filigree.cli import cli
from filigree.core import DB_FILENAME, FILIGREE_DIR_NAME, FiligreeDB, write_config
from filigree.registry import RegistryUnavailableError
from filigree.types.api import ErrorCode

HINT_FRAGMENTS = ("loomweave serve", "allow_local_fallback")


def _closed_port() -> int:
    """Return a loopback port that nothing is listening on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def unreachable_loomweave_project(tmp_path: Path) -> tuple[Path, str]:
    """A project configured for loomweave mode whose Loomweave is unreachable.

    Returns ``(project_root, base_url)``; the base_url points at a closed
    loopback port so the startup capability probe fails with
    ``cause_kind='network'``.
    """
    filigree_dir = tmp_path / FILIGREE_DIR_NAME
    filigree_dir.mkdir()
    db = FiligreeDB(filigree_dir / DB_FILENAME, prefix="proj")
    db.initialize()
    db.close()
    base_url = f"http://127.0.0.1:{_closed_port()}"
    write_config(
        filigree_dir,
        {
            "prefix": "proj",
            "version": 1,
            "registry_backend": "loomweave",
            "loomweave": {"base_url": base_url, "timeout_seconds": 1, "allow_local_fallback": False},
        },
    )
    return tmp_path, base_url


def _assert_envelope(payload: dict[str, Any], base_url: str) -> None:
    assert payload["code"] == ErrorCode.REGISTRY_UNAVAILABLE
    assert "Registry unavailable" in payload["error"]
    details = payload["details"]
    assert details["cause"] == "registry_unavailable"
    assert details["cause_kind"] == "network"
    assert details["url"].startswith(base_url)
    assert details["backend"] == "loomweave"
    for fragment in HINT_FRAGMENTS:
        assert fragment in details["hint"]


class TestCliFailsClosedWithEnvelope:
    def test_plain_list_exits_1_with_one_line_error_and_hint(
        self,
        unreachable_loomweave_project: tuple[Path, str],
        cli_runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project_root, base_url = unreachable_loomweave_project
        monkeypatch.chdir(project_root)

        result = cli_runner.invoke(cli, ["list"])

        assert result.exit_code == 1, result.output
        assert "Traceback" not in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)
        err_lines = [line for line in result.stderr.splitlines() if line.strip()]
        assert len(err_lines) == 2, err_lines
        assert "Registry unavailable" in err_lines[0]
        assert base_url in err_lines[0]
        assert "network" in err_lines[0]
        for fragment in HINT_FRAGMENTS:
            assert fragment in err_lines[1]
        assert result.stdout == ""

    def test_json_list_emits_registry_unavailable_envelope(
        self,
        unreachable_loomweave_project: tuple[Path, str],
        cli_runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project_root, base_url = unreachable_loomweave_project
        monkeypatch.chdir(project_root)

        result = cli_runner.invoke(cli, ["list", "--json"])

        assert result.exit_code == 1, result.output
        assert "Traceback" not in result.output
        assert result.stderr == ""
        payload = json.loads(result.stdout)
        _assert_envelope(payload, base_url)

    def test_show_verb_shares_the_same_gate(
        self,
        unreachable_loomweave_project: tuple[Path, str],
        cli_runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The gate lives in ``get_db`` so every DB-backed verb is covered."""
        project_root, base_url = unreachable_loomweave_project
        monkeypatch.chdir(project_root)

        result = cli_runner.invoke(cli, ["show", "proj-0000000000", "--json"])

        assert result.exit_code == 1, result.output
        _assert_envelope(json.loads(result.stdout), base_url)


class TestMcpStartupFailsClosedWithEnvelope:
    """Same gap on the MCP stdio startup path (``_attempt_startup``)."""

    def test_attempt_startup_records_envelope_instead_of_raising(
        self,
        unreachable_loomweave_project: tuple[Path, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import filigree.mcp_server as mcp_mod

        project_root, base_url = unreachable_loomweave_project
        filigree_dir = project_root / FILIGREE_DIR_NAME

        monkeypatch.setattr(mcp_mod, "db", None)
        monkeypatch.setattr(mcp_mod, "_filigree_dir", None)
        monkeypatch.setattr(mcp_mod, "_schema_mismatch", None)
        monkeypatch.setattr(mcp_mod, "_registry_startup_error", None)
        monkeypatch.setattr(mcp_mod, "_db_open_error", None)
        # ``_attempt_startup`` also writes the retry state; restore it too, or
        # a later test that hand-seeds ``_registry_startup_error`` would find
        # ``_startup_args`` pointing at this (by then torn-down) project.
        monkeypatch.setattr(mcp_mod, "_startup_args", None)
        monkeypatch.setattr(mcp_mod, "_registry_retry_last_monotonic", None)
        monkeypatch.setattr(mcp_mod, "_registry_retry_last_at", None)

        mcp_mod._attempt_startup(filigree_dir)

        assert mcp_mod.db is None
        assert isinstance(mcp_mod._registry_startup_error, RegistryUnavailableError)
        assert mcp_mod._db_open_error is None

        payload = json.loads(asyncio.run(mcp_mod.call_tool("issue_list", {}))[0].text)
        _assert_envelope(payload, base_url)

        status = json.loads(asyncio.run(mcp_mod.call_tool("mcp_status_get", {}))[0].text)
        assert status["status"] == "registry_unavailable"
        assert status["code"] == ErrorCode.REGISTRY_UNAVAILABLE
        assert status["db_initialized"] is False
        for fragment in HINT_FRAGMENTS:
            assert fragment in status["guidance"]

        # The degraded-startup log line must not assume version-mismatch attributes.
        mcp_mod._log_startup_status(logging.getLogger("test.mcp.startup"))


class TestDashboardFailsClosedWithEnvelope:
    """Same gap on the dashboard: ephemeral startup and server-mode per-project open."""

    def test_ephemeral_main_exits_1_with_hint_no_traceback(
        self,
        unreachable_loomweave_project: tuple[Path, str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from filigree import dashboard

        project_root, base_url = unreachable_loomweave_project
        monkeypatch.chdir(project_root)

        with pytest.raises(SystemExit) as excinfo:
            dashboard.main(no_browser=True)

        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err
        assert "Registry unavailable" in captured.err
        assert base_url in captured.err
        for fragment in HINT_FRAGMENTS:
            assert fragment in captured.err

    async def test_server_mode_request_gets_503_envelope(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import filigree.dashboard as dash_module
        from filigree.dashboard import ProjectStore, create_app

        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        filigree_dir = tmp_path / "proj-alpha" / FILIGREE_DIR_NAME
        filigree_dir.mkdir(parents=True)
        write_config(filigree_dir, {"prefix": "alpha", "version": 1})
        db = FiligreeDB(filigree_dir / DB_FILENAME, prefix="alpha", check_same_thread=False)
        db.initialize()
        db.close()
        (config_dir / "server.json").write_text(json.dumps({"port": 8377, "projects": {str(filigree_dir): {"prefix": "alpha"}}}))

        base_url = f"http://127.0.0.1:{_closed_port()}"

        def _raise_unavailable(*_args: Any, **_kwargs: Any) -> FiligreeDB:
            raise RegistryUnavailableError(
                f"Loomweave capability probe unreachable at {base_url}/api/v1/_capabilities: connection refused",
                url=f"{base_url}/api/v1/_capabilities",
                cause_kind="network",
            )

        monkeypatch.setattr(dash_module, "_open_db_for_filigree_dir", _raise_unavailable)

        store = ProjectStore()
        store.load()
        dash_module._project_store = store
        try:
            app = create_app(server_mode=True)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/api/issues")
        finally:
            store.close_all()
            dash_module._project_store = None

        assert response.status_code == 503
        _assert_envelope(response.json(), base_url)
        # The lazy per-project open IS a DB open: keep that wording.
        assert "while opening project database" in response.json()["error"]

    async def test_request_time_registry_failure_is_labelled_as_request_handling(self, tmp_path: Path) -> None:
        """A registry failure AFTER the DB opened (e.g. ``POST /api/observations``
        -> ``register_file``) reaches the app-wide handler; it must not claim the
        project database failed to open (review F8)."""
        import filigree.dashboard as dash_module
        from filigree.dashboard import create_app
        from tests._fakes.clarion_http import clarion_stub

        with clarion_stub() as (base_url, _state):
            db = FiligreeDB(
                tmp_path / "filigree.db",
                prefix="test",
                check_same_thread=False,
                registry_backend="loomweave",
                loomweave_config={"base_url": base_url, "timeout_seconds": 0.5, "allow_local_fallback": False},
            )
            db.initialize()
        # Loomweave is now down; the DB is open and fail-closed.
        dash_module._db = db
        try:
            app = create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/api/observations",
                    json={"summary": "registry down mid-session", "file_path": "src/main.py"},
                )
        finally:
            dash_module._db = None
            db.close()

        assert response.status_code == 503, response.text
        payload = response.json()
        assert payload["code"] == ErrorCode.REGISTRY_UNAVAILABLE
        assert "while handling request" in payload["error"]
        assert "opening project database" not in payload["error"]
        assert payload["details"]["cause_kind"] == "network"
        assert payload["details"]["backend"] == "loomweave"
        for fragment in HINT_FRAGMENTS:
            assert fragment in payload["details"]["hint"]
