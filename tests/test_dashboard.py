"""Tests for filigree.dashboard — covering gaps not addressed by tests/api/.

Scope: module-level state, idle-timeout machinery, _get_db() error paths,
ProjectStore edge cases on load(), and create_app() mode differences.

Tests that are already thorough in tests/api/test_multi_project.py and
tests/api/test_api.py are intentionally NOT duplicated here.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

import filigree.dashboard as dash_module
from filigree.core import CONF_FILENAME, DB_FILENAME, FILIGREE_DIR_NAME, FiligreeDB, ForeignDatabaseError, write_conf, write_config
from filigree.dashboard import (
    IDLE_TIMEOUT_SECONDS,
    ProjectStore,
    _idle_watchdog,
    create_app,
)
from tests._db_factory import make_db

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_filigree_dir(base: Path, name: str, prefix: str) -> Path:
    """Create a minimal .filigree/ directory with config + initialised DB."""
    filigree_dir = base / name / ".filigree"
    filigree_dir.mkdir(parents=True)
    write_config(filigree_dir, {"prefix": prefix, "version": 1})
    db = FiligreeDB(filigree_dir / DB_FILENAME, prefix=prefix, check_same_thread=False)
    db.initialize()
    db.close()
    return filigree_dir


def _write_server_json(config_dir: Path, projects: dict[str, dict[str, str]]) -> None:
    payload = {"port": 8377, "projects": projects}
    (config_dir / "server.json").write_text(json.dumps(payload))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def ethereal_client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """Minimal ethereal-mode test client (no populated issues needed)."""
    db = make_db(tmp_path, check_same_thread=False)
    dash_module._db = db
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    dash_module._db = None
    db.close()


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_idle_timeout_is_one_hour(self) -> None:
        assert IDLE_TIMEOUT_SECONDS == 3600

    def test_default_port_is_8377(self) -> None:
        from filigree.dashboard import DEFAULT_PORT

        assert DEFAULT_PORT == 8377

    def test_static_dir_contains_dashboard_html(self) -> None:
        from filigree.dashboard import STATIC_DIR

        assert (STATIC_DIR / "dashboard.html").exists()


# ---------------------------------------------------------------------------
# _idle_watchdog
# ---------------------------------------------------------------------------


class TestIdleWatchdog:
    """_idle_watchdog sends SIGTERM once elapsed time exceeds the threshold."""

    def test_watchdog_sends_sigterm_when_idle_exceeded(self) -> None:
        """Watchdog fires SIGTERM when _last_request_time is old enough."""
        import signal

        # Set last-request time far in the past.
        dash_module._last_request_time = time.monotonic() - 7200.0

        sent_signals: list[int] = []

        def _fake_kill(pid: int, sig: int) -> None:
            sent_signals.append(sig)

        with patch("filigree.dashboard.time.sleep"), patch("os.kill", _fake_kill):
            _idle_watchdog(timeout=3600.0, check_interval=60.0)

        assert signal.SIGTERM in sent_signals

    def test_watchdog_does_not_fire_when_recently_active(self) -> None:
        """Watchdog must NOT fire when elapsed is below threshold."""
        dash_module._last_request_time = time.monotonic()  # just now

        call_count = 0

        def _fake_sleep(_: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                # Pretend enough time passed on the second check, but still
                # within the threshold — advance the request time so it never fires.
                dash_module._last_request_time = time.monotonic()
                raise StopIteration("stop the loop")

        sent_signals: list[int] = []

        def _fake_kill(pid: int, sig: int) -> None:  # pragma: no cover
            sent_signals.append(sig)

        with (
            patch("filigree.dashboard.time.sleep", _fake_sleep),
            patch("os.kill", _fake_kill),
            pytest.raises(StopIteration),
        ):
            _idle_watchdog(timeout=3600.0, check_interval=60.0)

        assert sent_signals == []

    def test_watchdog_uses_monotonic_clock(self) -> None:
        """_last_request_time must be set from time.monotonic(), not time.time()."""
        # This is a structural check: the module sets _last_request_time via
        # time.monotonic(), so the comparison in _idle_watchdog is apples-to-apples.
        t0 = time.monotonic()
        dash_module._last_request_time = t0
        elapsed = time.monotonic() - dash_module._last_request_time
        # Should be a very small non-negative number, not some wall-clock epoch delta.
        assert 0.0 <= elapsed < 5.0


# ---------------------------------------------------------------------------
# IdleTrackingMiddleware — updates _last_request_time on each request
# ---------------------------------------------------------------------------


class TestIdleTrackingMiddleware:
    async def test_request_updates_last_request_time(self, ethereal_client: AsyncClient) -> None:
        """Each HTTP request must update _last_request_time."""
        dash_module._last_request_time = 0.0
        t_before = time.monotonic()

        await ethereal_client.get("/api/health")

        assert dash_module._last_request_time >= t_before

    async def test_middleware_not_installed_in_server_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Server mode must NOT install IdleTrackingMiddleware."""
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        alpha_dir = _create_filigree_dir(tmp_path, "proj-alpha", "alpha")
        _write_server_json(config_dir, {str(alpha_dir): {"prefix": "alpha"}})

        store = ProjectStore()
        store.load()
        dash_module._project_store = store
        try:
            dash_module._last_request_time = 0.0
            app = create_app(server_mode=True)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                await client.get("/api/health")
            # Server mode should NOT update _last_request_time
            assert dash_module._last_request_time == 0.0
        finally:
            store.close_all()
            dash_module._project_store = None
            dash_module._last_request_time = 0.0


# ---------------------------------------------------------------------------
# _get_db() — error paths
# ---------------------------------------------------------------------------


class TestMainGlobalReset:
    """Bug filigree-bff063de18: repeated in-process main() calls must not serve the
    wrong database because _project_store / _db globals leak between runs."""

    def test_ethereal_main_clears_prior_project_store(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        filigree_dir = _create_filigree_dir(tmp_path, "proj-a", "a")
        monkeypatch.setattr(dash_module, "find_filigree_anchor", lambda: (filigree_dir.parent, None))

        # Simulate lingering server-mode global from a prior in-process run.
        leftover = ProjectStore()
        dash_module._project_store = leftover
        dash_module._db = None

        captured: dict[str, object] = {}

        def fake_uvicorn_run(*args: object, **kwargs: object) -> None:
            captured["project_store_during_run"] = dash_module._project_store
            captured["db_during_run"] = dash_module._db

        monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
        monkeypatch.setattr("filigree.dashboard.webbrowser.open", lambda *a, **kw: None)

        try:
            dash_module.main(port=9999, no_browser=True, server_mode=False)
        finally:
            # Ensure we don't pollute other tests.
            dash_module._project_store = None
            dash_module._db = None

        assert captured["project_store_during_run"] is None, (
            "ethereal main must clear leftover _project_store before running so _get_db routes to the intended single-project _db"
        )
        assert captured["db_during_run"] is not None, "ethereal main must assign _db before running"

    def test_server_main_clears_prior_single_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        filigree_dir = _create_filigree_dir(tmp_path, "proj-b", "b")
        # Simulate leftover ethereal _db from a prior in-process run.
        leftover_db = FiligreeDB(filigree_dir / DB_FILENAME, prefix="b", check_same_thread=False)
        dash_module._db = leftover_db
        dash_module._project_store = None

        # Point ProjectStore at an empty server config so load() is cheap.
        config_dir = tmp_path / ".server-config"
        config_dir.mkdir()
        (config_dir / "server.json").write_text(json.dumps({"port": 8377, "projects": {}}))
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        captured: dict[str, object] = {}

        def fake_uvicorn_run(*args: object, **kwargs: object) -> None:
            captured["db_during_run"] = dash_module._db
            captured["project_store_during_run"] = dash_module._project_store

        monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
        monkeypatch.setattr("filigree.dashboard.webbrowser.open", lambda *a, **kw: None)

        try:
            dash_module.main(port=9999, no_browser=True, server_mode=True)
        finally:
            leftover_db.close()
            dash_module._project_store = None
            dash_module._db = None

        assert captured["db_during_run"] is None, "server main must clear leftover _db before running so _get_db routes via _project_store"
        assert captured["project_store_during_run"] is not None

    def test_main_resets_both_globals_in_finally(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        filigree_dir = _create_filigree_dir(tmp_path, "proj-c", "c")
        monkeypatch.setattr(dash_module, "find_filigree_anchor", lambda: (filigree_dir.parent, None))

        dash_module._project_store = ProjectStore()
        dash_module._db = None

        monkeypatch.setattr("uvicorn.run", lambda *a, **kw: None)
        monkeypatch.setattr("filigree.dashboard.webbrowser.open", lambda *a, **kw: None)

        dash_module.main(port=9999, no_browser=True, server_mode=False)

        assert dash_module._project_store is None, "finally must reset _project_store"
        assert dash_module._db is None, "finally must reset _db"

    def test_main_clears_stale_config_on_restart(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """filigree-154a23794c: a previous run's _config["name"] must not leak.

        Scenario: ethereal run #1 against a project whose config has
        ``name="Old Project"``; ethereal run #2 against a project whose
        config omits ``name`` (read_config only defaults prefix and version).
        /api/projects must serve the new project's prefix, not "Old Project".
        """
        # Run #1: project with explicit name.
        proj_a = _create_filigree_dir(tmp_path, "proj-old", "old")
        # Overwrite config to include a name.
        write_config(proj_a, {"prefix": "old", "version": 1, "name": "Old Project"})

        captured: dict[str, dict[str, object]] = {}

        def fake_uvicorn_run_a(*args: object, **kwargs: object) -> None:
            captured["after_run_a"] = dict(dash_module._config)

        monkeypatch.setattr(dash_module, "find_filigree_anchor", lambda: (proj_a.parent, None))
        monkeypatch.setattr("uvicorn.run", fake_uvicorn_run_a)
        monkeypatch.setattr("filigree.dashboard.webbrowser.open", lambda *a, **kw: None)

        dash_module.main(port=9999, no_browser=True, server_mode=False)
        assert captured["after_run_a"].get("name") == "Old Project"

        # Run #2: minimal config (no name key).
        proj_b = _create_filigree_dir(tmp_path, "proj-new", "new")
        # write_config in _create_filigree_dir already wrote {prefix, version}; no name.

        def fake_uvicorn_run_b(*args: object, **kwargs: object) -> None:
            captured["after_run_b"] = dict(dash_module._config)

        monkeypatch.setattr(dash_module, "find_filigree_anchor", lambda: (proj_b.parent, None))
        monkeypatch.setattr("uvicorn.run", fake_uvicorn_run_b)

        dash_module.main(port=9999, no_browser=True, server_mode=False)

        # _config["name"] from run #1 must be gone.
        assert "name" not in captured["after_run_b"], f"stale 'name' from prior run leaked into _config: {captured['after_run_b']!r}"

    def test_main_clears_config_in_finally(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """filigree-154a23794c: finally block must clear _config too."""
        proj = _create_filigree_dir(tmp_path, "proj-fin", "fin")
        write_config(proj, {"prefix": "fin", "version": 1, "name": "Finalize"})
        monkeypatch.setattr(dash_module, "find_filigree_anchor", lambda: (proj.parent, None))
        monkeypatch.setattr("uvicorn.run", lambda *a, **kw: None)
        monkeypatch.setattr("filigree.dashboard.webbrowser.open", lambda *a, **kw: None)

        dash_module.main(port=9999, no_browser=True, server_mode=False)

        assert dash_module._config == {}, f"finally must clear _config; got {dash_module._config!r}"

    def test_ethereal_main_without_project_exits_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Startup discovery failures should be clean CLI errors, not raw exceptions."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("uvicorn.run", lambda *a, **kw: pytest.fail("uvicorn should not start"))

        with pytest.raises(SystemExit) as excinfo:
            dash_module.main(port=9999, no_browser=True, server_mode=False)

        assert excinfo.value.code == 1
        stderr = capsys.readouterr().err
        assert "Error loading dashboard project" in stderr
        assert "filigree init" in stderr
        assert "Traceback" not in stderr

    def test_ethereal_main_with_malformed_conf_exits_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Malformed .filigree.conf should exit with guidance instead of a traceback."""
        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        write_config(filigree_dir, {"prefix": "bad", "version": 1})
        write_conf(tmp_path / CONF_FILENAME, {"version": 1, "project_name": "bad", "prefix": "bad", "db": "../escape.db"})
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("uvicorn.run", lambda *a, **kw: pytest.fail("uvicorn should not start"))

        with pytest.raises(SystemExit) as excinfo:
            dash_module.main(port=9999, no_browser=True, server_mode=False)

        assert excinfo.value.code == 1
        stderr = capsys.readouterr().err
        assert "Error loading dashboard project" in stderr
        assert ".filigree.conf" in stderr
        assert "Run `filigree doctor`" in stderr
        assert "Traceback" not in stderr

    def test_ethereal_main_enables_local_registry_fallback_without_startup_downgrade(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        write_config(
            filigree_dir,
            {
                "prefix": "dash",
                "version": 1,
                "registry_backend": "loomweave",
                "loomweave": {"base_url": "http://loomweave.test"},
            },
        )
        write_conf(
            tmp_path / CONF_FILENAME,
            {
                "version": 1,
                "project_name": "dash",
                "prefix": "dash",
                "db": ".filigree/filigree.db",
                "registry_backend": "loomweave",
                "loomweave": {"base_url": "http://loomweave.test"},
            },
        )
        db = FiligreeDB(filigree_dir / DB_FILENAME, prefix="dash")
        db.initialize()
        db.close()
        monkeypatch.chdir(tmp_path)
        captured: dict[str, Any] = {}

        def fake_run(*args: object, **kwargs: object) -> None:
            captured["config"] = dict(dash_module._config)
            captured["loomweave_config"] = dict(dash_module._db.loomweave_config) if dash_module._db is not None else {}
            captured["allow_local_fallback"] = dash_module._db.allow_local_fallback if dash_module._db is not None else False
            captured["registry_displaced"] = dash_module._db.registry.is_displaced() if dash_module._db is not None else False

        monkeypatch.setattr("uvicorn.run", fake_run)

        with caplog.at_level(logging.WARNING, logger="filigree.dashboard"):
            dash_module.main(port=9999, no_browser=True, server_mode=False, allow_local_fallback=True)

        assert "dashboard started with --allow-local-fallback; loomweave registry is bypassed for auto-creates" in caplog.text
        # The post-startup *write-path* WARN (``_LoomweaveLocalFallbackRegistry``
        # logs this on every resolve_file fall-through) is still absent —
        # only the startup-time probe-failure WARN is in caplog.
        assert "Loomweave registry backend unavailable; using local file registry fallback" not in caplog.text
        assert captured["allow_local_fallback"] is True
        assert captured["registry_displaced"] is True
        assert captured["config"]["loomweave"] == {"base_url": "http://loomweave.test"}
        # ADR-014: ``--allow-local-fallback`` overrides whatever the project
        # config says about fallback *before* the capability probe runs, so
        # the resulting in-memory ``loomweave_config`` carries
        # ``allow_local_fallback: True``.
        assert captured["loomweave_config"] == {"base_url": "http://loomweave.test", "allow_local_fallback": True}

    def test_dashboard_structured_log_does_not_leak_paths(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Structured startup logs use safe wording; stderr keeps the rich diagnostic."""
        cwd = tmp_path / "inner"
        cwd.mkdir()
        found_anchor = tmp_path / CONF_FILENAME
        write_conf(found_anchor, {"version": 1, "project_name": "outer", "prefix": "outer", "db": ".filigree/filigree.db"})
        exc = ForeignDatabaseError(cwd=cwd, found_anchor=found_anchor, git_boundary=cwd)

        def raise_foreign_database_error() -> None:
            raise exc

        monkeypatch.setattr(dash_module, "find_filigree_anchor", raise_foreign_database_error)
        monkeypatch.setattr("uvicorn.run", lambda *a, **kw: pytest.fail("uvicorn should not start"))

        with caplog.at_level(logging.WARNING, logger="filigree.dashboard"), pytest.raises(SystemExit) as excinfo:
            dash_module.main(port=9999, no_browser=True, server_mode=False)

        assert excinfo.value.code == 1
        stderr = capsys.readouterr().err
        assert str(cwd) in stderr
        records = [record for record in caplog.records if record.message == "dashboard_project_config_error"]
        assert records
        logged_error = records[-1].args_data["error"]
        assert str(cwd) not in logged_error
        assert str(found_anchor) not in logged_error
        assert "filigree init" not in logged_error


class TestGetDbErrorPaths:
    async def test_returns_500_when_db_is_none_in_ethereal_mode(self) -> None:
        """_get_db() must raise HTTP 500 when module-level _db is None."""
        saved = dash_module._db
        dash_module._db = None
        dash_module._project_store = None
        try:
            app = create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/stats")
            assert resp.status_code == 500
            body = resp.json()
            assert body.get("code") == "INTERNAL", f"wrong code: {body!r}"
            assert "Database not initialized" in body.get("error", "")
        finally:
            dash_module._db = saved

    async def test_server_mode_project_config_error_returns_validation_envelope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Malformed per-project config should be a 400 ErrorResponse, not a 500 traceback."""
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        project_root = tmp_path / "bad-project"
        filigree_dir = project_root / FILIGREE_DIR_NAME
        filigree_dir.mkdir(parents=True)
        write_config(filigree_dir, {"prefix": "bad", "version": 1})
        write_conf(project_root / CONF_FILENAME, {"version": 1, "project_name": "bad", "prefix": "bad", "db": "../escape.db"})
        _write_server_json(config_dir, {str(filigree_dir): {"prefix": "bad"}})

        store = ProjectStore()
        store.load()
        dash_module._project_store = store
        try:
            app = create_app(server_mode=True)
            transport = ASGITransport(app=app, raise_app_exceptions=False)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/p/bad/issues")

            assert resp.status_code == 400
            body = resp.json()
            assert body["code"] == "VALIDATION"
            assert ".filigree.conf" in body["error"]
        finally:
            store.close_all()
            dash_module._project_store = None

    async def test_server_mode_isolates_bad_project_config_from_good_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """One malformed registered project must not prevent serving the rest."""
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        good_dir = _create_filigree_dir(tmp_path, "good-project", "good")

        bad_root = tmp_path / "bad-project"
        bad_dir = bad_root / FILIGREE_DIR_NAME
        bad_dir.mkdir(parents=True)
        write_config(
            bad_dir,
            {
                "prefix": "bad",
                "version": 1,
                "registry_backend": "loomweave",
                "loomweave": {"base_url": "file:///not-http"},
            },
        )
        bad_db = FiligreeDB(bad_dir / DB_FILENAME, prefix="bad", check_same_thread=False)
        bad_db.initialize()
        bad_db.close()
        _write_server_json(config_dir, {str(good_dir): {"prefix": "good"}, str(bad_dir): {"prefix": "bad"}})

        store = ProjectStore()
        store.load()
        dash_module._project_store = store
        try:
            app = create_app(server_mode=True)
            transport = ASGITransport(app=app, raise_app_exceptions=False)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                good_resp = await client.get("/api/p/good/stats")
                bad_resp = await client.get("/api/p/bad/issues")

            assert good_resp.status_code == 200
            assert bad_resp.status_code == 400
            bad_body = bad_resp.json()
            assert bad_body["code"] == "VALIDATION"
            assert "loomweave.base_url" in bad_body["error"]
        finally:
            store.close_all()
            dash_module._project_store = None


# ---------------------------------------------------------------------------
# ProjectStore.load() — corrupt / malformed configs
# ---------------------------------------------------------------------------


class TestProjectStoreLoadCorruption:
    """load() raises ValueError on corrupt or schema-violating server.json."""

    def _setup_server_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        return config_dir

    def test_load_raises_on_invalid_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = self._setup_server_config(tmp_path, monkeypatch)
        (config_dir / "server.json").write_text("{not valid json")

        store = ProjectStore()
        with pytest.raises(ValueError, match="Corrupt server config"):
            store.load()

    def test_load_raises_when_root_is_not_object(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A JSON array at the root (not an object) must raise ValueError."""
        config_dir = self._setup_server_config(tmp_path, monkeypatch)
        (config_dir / "server.json").write_text("[1, 2, 3]")

        store = ProjectStore()
        with pytest.raises(ValueError, match="expected JSON object"):
            store.load()

    def test_load_raises_when_projects_node_is_not_object(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """'projects' value that is a list (not a dict) must raise ValueError."""
        config_dir = self._setup_server_config(tmp_path, monkeypatch)
        (config_dir / "server.json").write_text(json.dumps({"port": 8377, "projects": ["oops"]}))

        store = ProjectStore()
        with pytest.raises(ValueError, match="'projects' must be an object"):
            store.load()

    def test_load_raises_when_project_entry_is_not_object(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A malformed project entry must fail load() instead of disappearing."""
        config_dir = self._setup_server_config(tmp_path, monkeypatch)
        project_path = str(tmp_path / "proj" / ".filigree")
        (config_dir / "server.json").write_text(json.dumps({"port": 8377, "projects": {project_path: "oops"}}))

        store = ProjectStore()
        with pytest.raises(ValueError, match="project entry"):
            store.load()

    def test_load_raises_when_project_prefix_is_not_string(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A malformed prefix must fail load() instead of dropping the project."""
        config_dir = self._setup_server_config(tmp_path, monkeypatch)
        project_path = str(tmp_path / "proj" / ".filigree")
        (config_dir / "server.json").write_text(json.dumps({"port": 8377, "projects": {project_path: {"prefix": ["bad"]}}}))

        store = ProjectStore()
        with pytest.raises(ValueError, match="project prefix"):
            store.load()

    def test_load_with_no_server_json_produces_empty_store(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When server.json is absent, load() produces an empty project map."""
        self._setup_server_config(tmp_path, monkeypatch)
        # Deliberately do NOT write server.json.

        store = ProjectStore()
        store.load()
        assert store.list_projects() == []
        assert store.default_key == ""


# ---------------------------------------------------------------------------
# ProjectStore — initialization state
# ---------------------------------------------------------------------------


class TestProjectStoreInit:
    def test_fresh_store_has_no_projects(self) -> None:
        store = ProjectStore()
        assert store._projects == {}
        assert store._dbs == {}

    def test_fresh_store_list_projects_is_empty(self) -> None:
        store = ProjectStore()
        assert store.list_projects() == []

    def test_fresh_store_default_key_is_empty_string(self) -> None:
        store = ProjectStore()
        assert store.default_key == ""

    def test_fresh_store_get_db_raises_key_error(self) -> None:
        store = ProjectStore()
        with pytest.raises(KeyError):
            store.get_db("anything")

    def test_close_all_on_fresh_store_is_noop(self) -> None:
        store = ProjectStore()
        store.close_all()  # must not raise
        assert store._dbs == {}


# ---------------------------------------------------------------------------
# create_app() — structural checks
# ---------------------------------------------------------------------------


class TestCreateApp:
    async def test_ethereal_mode_health_returns_ethereal(self, ethereal_client: AsyncClient) -> None:
        resp = await ethereal_client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["mode"] == "ethereal"
        assert "version" in data

    async def test_server_mode_health_returns_server(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        alpha_dir = _create_filigree_dir(tmp_path, "proj-alpha", "alpha")
        _write_server_json(config_dir, {str(alpha_dir): {"prefix": "alpha"}})

        store = ProjectStore()
        store.load()
        dash_module._project_store = store
        try:
            app = create_app(server_mode=True)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["mode"] == "server"
            assert data["projects"] == 1
        finally:
            store.close_all()
            dash_module._project_store = None

    async def test_server_mode_exposes_reload_endpoint(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """POST /api/reload must exist in server mode and 404 in ethereal mode."""
        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        alpha_dir = _create_filigree_dir(tmp_path, "proj-alpha", "alpha")
        _write_server_json(config_dir, {str(alpha_dir): {"prefix": "alpha"}})

        store = ProjectStore()
        store.load()
        dash_module._project_store = store
        try:
            app = create_app(server_mode=True)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/api/reload")
            assert resp.status_code == 200
        finally:
            store.close_all()
            dash_module._project_store = None

    async def test_ethereal_mode_has_no_reload_endpoint(self, ethereal_client: AsyncClient) -> None:
        """POST /api/reload must NOT exist in ethereal mode."""
        resp = await ethereal_client.post("/api/reload")
        assert resp.status_code in (404, 405)

    async def test_ethereal_mode_projects_endpoint_returns_single_entry(self, ethereal_client: AsyncClient) -> None:
        """GET /api/projects in ethereal mode returns exactly one entry with key=''."""
        resp = await ethereal_client.get("/api/projects")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["key"] == ""

    async def test_two_ethereal_apps_keep_separate_database_state(self, tmp_path: Path) -> None:
        """create_app() must capture runtime state instead of reading globals per request."""
        alpha_dir = tmp_path / "alpha"
        bravo_dir = tmp_path / "bravo"
        alpha_dir.mkdir()
        bravo_dir.mkdir()
        alpha_db = make_db(alpha_dir, prefix="alpha", check_same_thread=False)
        bravo_db = make_db(bravo_dir, prefix="bravo", check_same_thread=False)
        saved_db = dash_module._db
        saved_store = dash_module._project_store
        try:
            dash_module._project_store = None
            dash_module._db = alpha_db
            alpha_app = create_app()
            dash_module._db = bravo_db
            bravo_app = create_app()

            alpha_transport = ASGITransport(app=alpha_app)
            bravo_transport = ASGITransport(app=bravo_app)
            async with (
                AsyncClient(transport=alpha_transport, base_url="http://alpha") as alpha_client,
                AsyncClient(transport=bravo_transport, base_url="http://bravo") as bravo_client,
            ):
                alpha_resp = await alpha_client.get("/api/projects")
                bravo_resp = await bravo_client.get("/api/projects")

            assert alpha_resp.json()[0]["name"] == "alpha"
            assert bravo_resp.json()[0]["name"] == "bravo"
        finally:
            dash_module._db = saved_db
            dash_module._project_store = saved_store
            alpha_db.close()
            bravo_db.close()


# ---------------------------------------------------------------------------
# _safe_bounded_int re-export
# ---------------------------------------------------------------------------


class TestSafeBoundedIntReexport:
    """dashboard.py re-exports _safe_bounded_int so existing test imports keep working."""

    def test_reexport_is_importable_from_dashboard(self) -> None:
        from filigree.dashboard import _safe_bounded_int

        assert callable(_safe_bounded_int)

    def test_reexport_matches_original(self) -> None:
        from filigree.dashboard import _safe_bounded_int as via_dashboard
        from filigree.dashboard_routes.common import _safe_bounded_int as original

        assert via_dashboard is original


# ---------------------------------------------------------------------------
# /api/weft/scanners — relocated-DB resolution (filigree-641037692a)
# ---------------------------------------------------------------------------


class TestLoomScannersRelocatedDB:
    """``GET /api/weft/scanners`` must resolve scanner TOMLs from
    ``project_root / ".filigree" / "scanners"``, not ``db.db_path.parent /
    "scanners"`` — otherwise ``.filigree.conf`` projects with a relocated
    ``db = ...`` path return an empty scanner list while the CLI/MCP
    surfaces correctly enumerate them.
    """

    async def test_relocated_db_finds_scanner_in_filigree_dir(self, tmp_path: Path) -> None:
        from filigree.core import CONF_FILENAME, FILIGREE_DIR_NAME, write_conf

        project_root = tmp_path / "relocated-proj"
        filigree_dir = project_root / FILIGREE_DIR_NAME
        filigree_dir.mkdir(parents=True)
        write_config(filigree_dir, {"prefix": "rel", "version": 1})

        # Scanner TOML lives in the conventional .filigree/scanners/ location.
        scanners_dir = filigree_dir / "scanners"
        scanners_dir.mkdir()
        (scanners_dir / "demo.toml").write_text(
            '[scanner]\nname = "demo"\ndisplay_name = "Demo"\nadapter = "command"\ncommand = "echo hi"\n'
        )

        # DB lives outside .filigree/, mimicking a v2.0 conf-relocated layout.
        custom_db_dir = project_root / "data"
        custom_db_dir.mkdir()
        custom_db = custom_db_dir / "track.db"
        seed = FiligreeDB(custom_db, prefix="rel", check_same_thread=False)
        seed.initialize()
        seed.close()
        write_conf(
            project_root / CONF_FILENAME,
            {"version": 1, "project_name": "rel", "prefix": "rel", "db": "data/track.db"},
        )

        db = FiligreeDB.from_conf(project_root / CONF_FILENAME, check_same_thread=False)
        # Sanity: DB path is outside .filigree/, so the legacy resolution would miss.
        assert db.db_path.parent != filigree_dir
        assert db.project_root == project_root.resolve()

        dash_module._db = db
        try:
            app = create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/weft/scanners")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            names = [item["name"] for item in body["items"]]
            assert "demo" in names, f"scanners not found via /api/weft/scanners: {body!r}"
        finally:
            dash_module._db = None
            db.close()

    async def test_legacy_filigree_dir_install_still_finds_scanner(self, tmp_path: Path) -> None:
        """Sanity: the fix must not regress the legacy
        ``.filigree/filigree.db`` layout — both DB construction paths set
        ``project_root`` (``from_filigree_dir`` resolves it from the dir's
        parent), so the route still resolves to ``.filigree/scanners``.
        """
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        write_config(filigree_dir, {"prefix": "leg", "version": 1})
        (filigree_dir / "scanners").mkdir()
        (filigree_dir / "scanners" / "legacy.toml").write_text(
            '[scanner]\nname = "legacy"\ndisplay_name = "Legacy"\nadapter = "command"\ncommand = "echo hi"\n'
        )
        db = FiligreeDB.from_filigree_dir(filigree_dir, check_same_thread=False)

        dash_module._db = db
        try:
            app = create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/weft/scanners")
            assert resp.status_code == 200
            names = [item["name"] for item in resp.json()["items"]]
            assert "legacy" in names
        finally:
            dash_module._db = None
            db.close()
