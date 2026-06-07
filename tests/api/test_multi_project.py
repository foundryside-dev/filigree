"""Dashboard API tests — multi-project (server mode), ProjectStore, and routing."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import filigree.dashboard as dash_module
from filigree.core import FiligreeDB
from filigree.dashboard import ProjectStore, create_app
from filigree.types.api import ErrorCode
from tests.api.conftest import _create_project


class TestProjectStore:
    """Unit tests for ProjectStore."""

    def test_load_discovers_projects(self, project_store: ProjectStore) -> None:
        projects = project_store.list_projects()
        assert len(projects) == 2
        keys = {p["key"] for p in projects}
        assert keys == {"alpha", "bravo"}

    def test_get_db_returns_correct_db(self, project_store: ProjectStore) -> None:
        db = project_store.get_db("alpha")
        assert db.prefix == "alpha"
        db2 = project_store.get_db("bravo")
        assert db2.prefix == "bravo"

    def test_get_db_unknown_key_raises(self, project_store: ProjectStore) -> None:
        with pytest.raises(KeyError):
            project_store.get_db("nonexistent")

    def test_store_dir_for_resolves_canonical_weft_store(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """store_dir_for must return the CANONICAL store dir (where the federation
        token lives) — .weft/filigree/ after consolidation — NOT the legacy
        .filigree/ path registered in server.json. The F1 per-project token
        resolver reads this dir; if it returns the legacy path, a project whose
        token only lives in the consolidated store fails auth (the incompleteness
        behind weft-7a399b8124's strict-auth on a real WEFT-consolidated deploy).
        It must agree with get_db, which already opens the .weft DB.
        """
        import json

        from filigree.core import CONF_FILENAME, write_conf, write_config

        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        proj = tmp_path / "proj"
        legacy = proj / ".filigree"  # registered path (no DB here post-migration)
        legacy.mkdir(parents=True)
        write_config(legacy, {"prefix": "acme", "version": 1})
        weft = proj / ".weft" / "filigree"  # canonical store: DB + token live here
        weft.mkdir(parents=True)
        db = FiligreeDB(weft / "filigree.db", prefix="acme", check_same_thread=False)
        db.initialize()
        db.close()
        write_conf(
            proj / CONF_FILENAME,
            {"version": 1, "project_name": "acme", "prefix": "acme", "db": ".weft/filigree/filigree.db"},
        )
        (config_dir / "server.json").write_text(json.dumps({"port": 8377, "projects": {str(legacy): {"prefix": "acme"}}}))

        store = ProjectStore()
        store.load()
        try:
            resolved = store.store_dir_for("acme")
            assert resolved == weft, f"expected canonical .weft store, got {resolved}"
            # Must agree with where get_db actually opens the DB.
            assert store.get_db("acme").db_path.parent == weft
        finally:
            store.close_all()

    def test_get_db_closes_connection_on_init_failure(self, project_store: ProjectStore) -> None:
        """Bug filigree-6128be: DB connection must be closed if initialize() fails."""
        from unittest.mock import patch

        with (
            patch.object(FiligreeDB, "initialize", side_effect=RuntimeError("migration exploded")),
            pytest.raises(RuntimeError, match="migration exploded"),
        ):
            project_store.get_db("alpha")

        # After failure, the key should NOT be cached — next call retries
        assert "alpha" not in project_store._dbs

    def test_reload_adds_new_project(self, project_store: ProjectStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import json

        charlie_dir = _create_project(tmp_path, "proj-charlie", "charlie", 3)
        config_dir = tmp_path / ".config" / "filigree"

        # Read existing, add charlie
        existing = json.loads((config_dir / "server.json").read_text())
        existing["projects"][str(charlie_dir)] = {"prefix": "charlie"}
        (config_dir / "server.json").write_text(json.dumps(existing))

        diff = project_store.reload()
        assert "charlie" in diff["added"]
        assert len(diff["removed"]) == 0
        assert len(project_store.list_projects()) == 3

    def test_reload_removes_project(self, project_store: ProjectStore, tmp_path: Path) -> None:
        import json

        config_dir = tmp_path / ".config" / "filigree"
        existing = json.loads((config_dir / "server.json").read_text())
        # Remove bravo
        to_remove = [k for k, v in existing["projects"].items() if v["prefix"] == "bravo"]
        for k in to_remove:
            del existing["projects"][k]
        (config_dir / "server.json").write_text(json.dumps(existing))

        diff = project_store.reload()
        assert "bravo" in diff["removed"]
        assert len(project_store.list_projects()) == 1

    def test_reload_evicts_removed_project_db_handles_close_deferred_to_close_all(
        self, project_store: ProjectStore, tmp_path: Path
    ) -> None:
        """filigree-e43edbc067: reload evicts removed handles from ``_dbs`` but
        does NOT close them synchronously — closing under a concurrent request
        would race with that request's SQLite call. The single drain point is
        ``close_all`` (process shutdown).
        """
        import json

        bravo_db = project_store.get_db("bravo")
        assert "bravo" in project_store._dbs

        config_dir = tmp_path / ".config" / "filigree"
        existing = json.loads((config_dir / "server.json").read_text())
        to_remove = [k for k, v in existing["projects"].items() if v["prefix"] == "bravo"]
        for k in to_remove:
            del existing["projects"][k]
        (config_dir / "server.json").write_text(json.dumps(existing))

        diff = project_store.reload()

        assert "bravo" in diff["removed"]
        assert "bravo" not in project_store._dbs
        # Handle is parked for deferred close, not closed yet.
        assert bravo_db in project_store._evicted_dbs
        assert bravo_db._conn is not None

        # close_all is the drain point.
        project_store.close_all()
        assert bravo_db._conn is None
        assert project_store._evicted_dbs == []

    def test_evicted_project_db_handles_drain_during_runtime(self, project_store: ProjectStore, tmp_path: Path) -> None:
        """Evicted DB handles should not be retained until process shutdown."""
        import json

        project_store._evicted_close_grace_seconds = 0
        bravo_db = project_store.get_db("bravo")

        config_dir = tmp_path / ".config" / "filigree"
        existing = json.loads((config_dir / "server.json").read_text())
        for key in [k for k, v in existing["projects"].items() if v["prefix"] == "bravo"]:
            del existing["projects"][key]
        (config_dir / "server.json").write_text(json.dumps(existing))

        project_store.reload()
        assert bravo_db in project_store._evicted_dbs

        project_store.list_projects()

        assert bravo_db._conn is None
        assert bravo_db not in project_store._evicted_dbs

    def test_close_all_logs_db_close_error_at_warning(self, project_store: ProjectStore, tmp_path: Path) -> None:
        """Bug filigree-191611: close failures must log at WARNING (not DEBUG).

        After filigree-e43edbc067 the actual close happens in ``close_all``,
        not ``reload``. Drain reload's eviction first (via close_all) and
        verify the warning surfaces from close_all.
        """
        import json
        from unittest.mock import patch

        # Force a DB handle to exist
        bravo_db = project_store.get_db("bravo")
        assert "bravo" in project_store._dbs

        # Make close() raise
        original_close = bravo_db.close
        bravo_db.close = lambda: (_ for _ in ()).throw(RuntimeError("close failed"))  # type: ignore[method-assign]

        # Remove bravo from config so reload evicts it
        config_dir = tmp_path / ".config" / "filigree"
        existing = json.loads((config_dir / "server.json").read_text())
        to_remove = [k for k, v in existing["projects"].items() if v["prefix"] == "bravo"]
        for k in to_remove:
            del existing["projects"][k]
        (config_dir / "server.json").write_text(json.dumps(existing))

        project_store.reload()
        with patch("filigree.dashboard.logger") as mock_logger:
            project_store.close_all()

        mock_logger.warning.assert_called_once()
        assert "evicted" in str(mock_logger.warning.call_args).lower()
        bravo_db.close = original_close  # type: ignore[method-assign]

    def test_reload_evicts_db_handle_when_project_path_changes(self, project_store: ProjectStore, tmp_path: Path) -> None:
        """Path-changed projects evict the old handle from ``_dbs`` and the
        next ``get_db`` opens the new path. Old handle stays alive on
        ``_evicted_dbs`` until ``close_all`` (filigree-e43edbc067), so
        an in-flight request that already holds it does not race a close.
        """
        import json

        bravo_db = project_store.get_db("bravo")
        old_db_path = bravo_db.db_path
        assert "bravo" in project_store._dbs

        replacement_dir = _create_project(tmp_path, "proj-bravo-replacement", "bravo", 4)
        config_dir = tmp_path / ".config" / "filigree"
        existing = json.loads((config_dir / "server.json").read_text())

        old_bravo_paths = [k for k, v in existing["projects"].items() if v["prefix"] == "bravo"]
        assert len(old_bravo_paths) == 1
        del existing["projects"][old_bravo_paths[0]]
        existing["projects"][str(replacement_dir)] = {"prefix": "bravo"}
        (config_dir / "server.json").write_text(json.dumps(existing))

        diff = project_store.reload()

        assert diff["added"] == []
        assert diff["removed"] == []
        assert "bravo" not in project_store._dbs
        # Old handle parked, NOT closed — concurrent readers can finish using it.
        assert bravo_db in project_store._evicted_dbs
        assert bravo_db._conn is not None

        reopened = project_store.get_db("bravo")
        assert reopened.db_path != old_db_path
        assert reopened.db_path.parent == replacement_dir

        project_store.close_all()
        assert bravo_db._conn is None

    def test_reload_corrupt_file_retains_state(self, project_store: ProjectStore, tmp_path: Path) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        before_keys = {p["key"] for p in project_store.list_projects()}

        (config_dir / "server.json").write_text("{bad json")
        diff = project_store.reload()

        assert diff["added"] == []
        assert diff["removed"] == []
        assert diff["error"]
        after_keys = {p["key"] for p in project_store.list_projects()}
        assert after_keys == before_keys

    def test_reload_malformed_project_entry_retains_state(self, project_store: ProjectStore, tmp_path: Path) -> None:
        import json

        config_dir = tmp_path / ".config" / "filigree"
        before_keys = {p["key"] for p in project_store.list_projects()}
        existing = json.loads((config_dir / "server.json").read_text())
        alpha_path = next(path for path, meta in existing["projects"].items() if meta["prefix"] == "alpha")
        existing["projects"][alpha_path] = "oops"
        (config_dir / "server.json").write_text(json.dumps(existing))

        diff = project_store.reload()

        assert diff["added"] == []
        assert diff["removed"] == []
        assert diff["error"]
        after_keys = {p["key"] for p in project_store.list_projects()}
        assert after_keys == before_keys

    def test_get_db_logs_and_reraises_open_failure(
        self,
        project_store: ProjectStore,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _boom(_self: FiligreeDB) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(FiligreeDB, "initialize", _boom)
        with caplog.at_level("ERROR"), pytest.raises(RuntimeError, match="boom"):
            project_store.get_db("alpha")
        assert "Failed to open project DB" in caplog.text

    def test_get_db_config_warning_includes_underlying_cause(
        self,
        project_store: ProjectStore,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _bad_config(_filigree_dir: Path, *, check_same_thread: bool = True) -> FiligreeDB:
            raise ValueError("loomweave.base_url must use http(s)")

        monkeypatch.setattr(dash_module, "_open_db_for_filigree_dir", _bad_config)
        with caplog.at_level("WARNING", logger="filigree.dashboard"), pytest.raises(ValueError, match=r"loomweave\.base_url"):
            project_store.get_db("alpha")
        assert "Invalid project configuration" in caplog.text
        assert "loomweave.base_url must use http(s)" in caplog.text

    def test_load_skips_missing_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import json

        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        # Register a path that doesn't exist
        server_json = {
            "port": 8377,
            "projects": {"/nonexistent/.filigree": {"prefix": "ghost"}},
        }
        (config_dir / "server.json").write_text(json.dumps(server_json))

        store = ProjectStore()
        store.load()
        assert len(store.list_projects()) == 0

    def test_empty_store_default_key(self) -> None:
        store = ProjectStore()
        assert store.default_key == ""

    def test_close_all_closes_db_handles(self, project_store: ProjectStore) -> None:
        """close_all() closes all open DB connections and clears the cache."""
        project_store.get_db("alpha")
        project_store.get_db("bravo")
        assert len(project_store._dbs) == 2

        project_store.close_all()
        assert project_store._dbs == {}

    def test_close_all_idempotent(self, project_store: ProjectStore) -> None:
        """Calling close_all() twice should not raise."""
        project_store.get_db("alpha")
        project_store.close_all()
        project_store.close_all()  # second call is a no-op
        assert project_store._dbs == {}

    def test_get_db_concurrent_first_open_serialized(self, project_store: ProjectStore) -> None:
        """filigree-732f6b31e4: concurrent first get_db() must open exactly once.

        Without the internal lock, two threads each pass the cache-miss check
        and each call ``FiligreeDB.from_store_dir`` (which migrates and
        seeds-inserts), only the loser's handle gets cached, and the winner's
        handle is leaked unclosed.
        """
        import threading
        from unittest.mock import patch

        original = FiligreeDB.from_store_dir
        opened: list[FiligreeDB] = []
        opened_lock = threading.Lock()
        gate = threading.Event()

        def slow_open(*args: object, **kwargs: object) -> FiligreeDB:
            # Block until both threads are inside the open path so they race.
            gate.wait(timeout=2.0)
            db = original(*args, **kwargs)  # type: ignore[arg-type]
            with opened_lock:
                opened.append(db)
            return db

        results: list[FiligreeDB] = []
        results_lock = threading.Lock()

        def worker() -> None:
            db = project_store.get_db("alpha")
            with results_lock:
                results.append(db)

        with patch.object(FiligreeDB, "from_store_dir", side_effect=slow_open):
            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            t2.start()
            # Let both threads converge on the get_db call.
            gate.set()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)

        assert len(opened) == 1, f"from_store_dir called {len(opened)}x, expected 1 (race / leak)"
        assert len(results) == 2
        assert results[0] is results[1], "both threads must observe the same cached handle"
        assert project_store._dbs["alpha"] is opened[0]

    def test_slow_uncached_open_does_not_block_cached_other_project(
        self, project_store: ProjectStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A slow first open for one project must not serialize cached reads for another."""
        import threading
        import time

        bravo_db = project_store.get_db("bravo")
        alpha_path = Path(next(p["path"] for p in project_store.list_projects() if p["key"] == "alpha"))
        original_open = dash_module._open_db_for_filigree_dir
        slow_open_entered = threading.Event()
        release_slow_open = threading.Event()
        errors: list[BaseException] = []

        def slow_alpha_open(filigree_dir: Path, *, check_same_thread: bool = True) -> FiligreeDB:
            if filigree_dir == alpha_path:
                slow_open_entered.set()
                release_slow_open.wait(timeout=5.0)
            return original_open(filigree_dir, check_same_thread=check_same_thread)

        def open_alpha() -> None:
            try:
                project_store.get_db("alpha")
            except BaseException as exc:  # captured for assertion in the test thread
                errors.append(exc)

        monkeypatch.setattr(dash_module, "_open_db_for_filigree_dir", slow_alpha_open)
        thread = threading.Thread(target=open_alpha)
        thread.start()
        try:
            assert slow_open_entered.wait(timeout=2.0), "alpha open did not enter the patched slow path"
            timer = threading.Timer(0.5, release_slow_open.set)
            timer.start()
            started = time.monotonic()
            cached = project_store.get_db("bravo")
            elapsed = time.monotonic() - started
            timer.cancel()
        finally:
            release_slow_open.set()
            thread.join(timeout=5.0)

        assert not thread.is_alive(), "alpha opener did not finish"
        assert errors == []
        assert cached is bravo_db
        assert elapsed < 0.25, f"cached read waited behind unrelated slow open for {elapsed:.3f}s"

    def test_get_db_honors_custom_db_path_from_conf(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """filigree-da8d5aba0f: ProjectStore must open the conf-declared db
        path, not silently fall back to ``.filigree/filigree.db``.

        Mirrors ``tests/test_doctor.py::TestDoctorHonorsConfDbPath``.
        """
        import json

        from filigree.core import CONF_FILENAME, FILIGREE_DIR_NAME, write_conf

        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        # Build a project whose DB lives at storage/track.db (not .filigree/filigree.db).
        project_root = tmp_path / "custom-db-proj"
        filigree_dir = project_root / FILIGREE_DIR_NAME
        filigree_dir.mkdir(parents=True)
        from filigree.core import write_config

        write_config(filigree_dir, {"prefix": "tst", "version": 1})
        storage_dir = project_root / "storage"
        storage_dir.mkdir()
        custom_db = storage_dir / "track.db"
        db = FiligreeDB(custom_db, prefix="tst", check_same_thread=False)
        db.initialize()
        db.create_issue("issue in custom-path db")
        db.close()
        write_conf(
            project_root / CONF_FILENAME,
            {"version": 1, "project_name": "tst", "prefix": "tst", "db": "storage/track.db"},
        )

        (config_dir / "server.json").write_text(json.dumps({"port": 8377, "projects": {str(filigree_dir): {"prefix": "tst"}}}))

        store = ProjectStore()
        store.load()
        try:
            opened = store.get_db("tst")
            assert opened.db_path == custom_db, f"dashboard opened {opened.db_path}, expected conf-declared {custom_db}"
            # Sanity: issue created via the custom-path DB is visible.
            assert any(i.title == "issue in custom-path db" for i in opened.list_issues())
        finally:
            store.close_all()

    def test_reload_atomic_under_concurrent_get_db(self, project_store: ProjectStore, tmp_path: Path) -> None:
        """filigree-e43edbc067: a reader concurrent with reload() must observe
        a consistent ``(_projects[key], _dbs[key])`` pair — never a torn view
        where ``_projects[key]`` points at the new path while ``_dbs[key]`` is
        the old handle (or vice versa). The handle returned must also be open.
        """
        import json
        import threading

        # Prime alpha and bravo so the handles exist.
        project_store.get_db("alpha")
        old_bravo = project_store.get_db("bravo")

        replacement_dir = _create_project(tmp_path, "proj-bravo-new", "bravo", 5)
        config_dir = tmp_path / ".config" / "filigree"
        existing = json.loads((config_dir / "server.json").read_text())
        old_bravo_paths = [k for k, v in existing["projects"].items() if v["prefix"] == "bravo"]
        del existing["projects"][old_bravo_paths[0]]
        existing["projects"][str(replacement_dir)] = {"prefix": "bravo"}
        (config_dir / "server.json").write_text(json.dumps(existing))

        observations: list[tuple[Path, Path]] = []  # (projects[bravo].path, db.db_path.parent)
        barrier = threading.Barrier(2)

        def reader() -> None:
            barrier.wait()
            for _ in range(50):
                try:
                    db = project_store.get_db("bravo")
                except KeyError:
                    continue
                # Snapshot the projects entry observed-by-the-store at this moment.
                projects_snapshot = {p["key"]: Path(p["path"]) for p in project_store.list_projects()}
                if "bravo" in projects_snapshot:
                    observations.append((projects_snapshot["bravo"], db.db_path.parent))
                # The handle we just got must be open.
                assert db._conn is not None, "get_db returned a closed handle"

        def reloader() -> None:
            barrier.wait()
            for _ in range(20):
                project_store.reload()

        t1 = threading.Thread(target=reader)
        t2 = threading.Thread(target=reloader)
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)
        assert not t1.is_alive(), "reader thread did not finish"
        assert not t2.is_alive(), "reloader thread did not finish"

        # Every observation must be consistent: projects[bravo] path matches the
        # parent of the DB path the store handed back. No torn views.
        for projects_path, db_parent in observations:
            assert projects_path == db_parent, f"torn view: projects[bravo]={projects_path} but db_path.parent={db_parent}"

        # Old handle must still be alive (parked on _evicted_dbs); close_all drains.
        assert old_bravo._conn is not None
        project_store.close_all()
        assert old_bravo._conn is None

    def test_prefix_collision_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import json

        config_dir = tmp_path / ".config" / "filigree"
        config_dir.mkdir(parents=True)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

        dir_a = _create_project(tmp_path, "dup-a", "samename", 1)
        dir_b = _create_project(tmp_path, "dup-b", "samename", 1)

        server_json = {
            "port": 8377,
            "projects": {
                str(dir_a): {"prefix": "samename"},
                str(dir_b): {"prefix": "samename"},
            },
        }
        (config_dir / "server.json").write_text(json.dumps(server_json))

        store = ProjectStore()
        with pytest.raises(ValueError, match="Prefix collision"):
            store.load()


class TestMultiProjectRouting:
    """Integration tests for multi-project URL routing."""

    async def test_default_project_issues(self, multi_client: AsyncClient) -> None:
        """GET /api/issues returns the default (first) project's issues."""
        resp = await multi_client.get("/api/issues")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2  # alpha has 1 issue + auto-seeded Future release

    async def test_scoped_project_issues(self, multi_client: AsyncClient) -> None:
        """GET /api/p/bravo/issues returns bravo's 2 issues + auto-seeded Future release."""
        resp = await multi_client.get("/api/p/bravo/issues")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3  # 2 issues + auto-seeded Future release

    async def test_unknown_project_404(self, multi_client: AsyncClient) -> None:
        """GET /api/p/nonexistent/issues returns 2.0 envelope 404, not raw stack trace."""
        resp = await multi_client.get("/api/p/nonexistent/issues")
        assert resp.status_code == 404
        data = resp.json()
        assert data.get("code") == "NOT_FOUND", f"wrong envelope: {data!r}"
        assert "nonexistent" in data.get("error", ""), f"missing project key in error: {data!r}"

    async def test_empty_project_key_returns_404(self, multi_client: AsyncClient) -> None:
        """GET /api/p//issues with empty key does not match any route."""
        resp = await multi_client.get("/api/p//issues")
        assert resp.status_code == 404

    async def test_mcp_unknown_project_returns_404(self, multi_client: AsyncClient) -> None:
        """MCP should reject unknown project keys and never reuse a stale DB."""
        resp = await multi_client.get("/mcp/?project=nonexistent")
        assert resp.status_code == 404
        data = resp.json()
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_stats_per_project(self, multi_client: AsyncClient) -> None:
        """Stats endpoint returns different prefixes per project."""
        alpha_resp = await multi_client.get("/api/p/alpha/stats")
        bravo_resp = await multi_client.get("/api/p/bravo/stats")
        assert alpha_resp.status_code == 200
        assert bravo_resp.status_code == 200
        assert alpha_resp.json()["prefix"] == "alpha"
        assert bravo_resp.json()["prefix"] == "bravo"

    async def test_empty_store_returns_503(self) -> None:
        """A ProjectStore with 0 projects returns 503."""
        empty_store = ProjectStore()
        dash_module._project_store = empty_store
        try:
            app = create_app(server_mode=True)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/issues")
                assert resp.status_code == 503
        finally:
            dash_module._project_store = None

    async def test_empty_store_503_propagates_to_multiple_endpoints(self) -> None:
        """503 from _get_db() propagates to all project-scoped endpoints when no projects registered."""
        empty_store = ProjectStore()
        dash_module._project_store = empty_store
        try:
            app = create_app(server_mode=True)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                # Verify multiple endpoints return 503 with 2.0 flat envelope
                for endpoint in ("/api/issues", "/api/stats", "/api/graph"):
                    resp = await client.get(endpoint)
                    assert resp.status_code == 503, f"{endpoint} returned {resp.status_code}, expected 503"
                    body = resp.json()
                    assert body.get("code") == "NOT_INITIALIZED", f"{endpoint} missing/wrong code: {body!r}"
                    assert "No projects registered" in body.get("error", ""), f"{endpoint} missing 'error' text: {body!r}"
        finally:
            dash_module._project_store = None

    async def test_unmapped_framework_4xx_uses_client_error_code(self, multi_client: AsyncClient) -> None:
        """Framework-raised 4xx responses such as 405 must not be labelled
        INTERNAL in the 2.0 envelope.
        """
        resp = await multi_client.post("/api/health")

        assert resp.status_code == 405
        body = resp.json()
        assert body["code"] == ErrorCode.VALIDATION
        assert body["code"] != ErrorCode.INTERNAL


_SCAN_BODY = {
    "scan_source": "wardline",
    "findings": [{"path": "src/a.py", "rule_id": "WLN-1", "message": "m", "severity": "high", "line_start": 10}],
}


class TestServerModeFederationWriteScope:
    """weft-7a399b8124: a federation write must resolve to the CALLER's project;
    an unscoped write fails closed instead of silently hitting the home project.
    """

    async def test_unscoped_federation_write_fails_closed(self, multi_client: AsyncClient) -> None:
        """POST /api/weft/scan-results with no project scope → 400, not a home write.

        The 400 alone is not enough: the contamination this fix prevents is a
        silently-landed row, so assert ZERO findings were written to EITHER
        project (fail-closed means the write never reached any DB).
        """
        resp = await multi_client.post("/api/weft/scan-results", json=_SCAN_BODY)
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == ErrorCode.VALIDATION
        assert "scope to a project" in body["error"]

        alpha = await multi_client.get("/api/p/alpha/weft/findings")
        bravo = await multi_client.get("/api/p/bravo/weft/findings")
        assert alpha.json()["items"] == [], "unscoped write must not land in the default project"
        assert bravo.json()["items"] == [], "unscoped write must not land in any project"

    async def test_scoped_write_unknown_key_404_before_any_write(self, multi_client: AsyncClient) -> None:
        """Criterion 3: a scoped write to an UNKNOWN project key 404s before any
        write, so a 2xx is proof the destination resolved to a real project.
        No row lands in any registered project."""
        resp = await multi_client.post("/api/p/nosuchproject/weft/scan-results", json=_SCAN_BODY)
        assert resp.status_code == 404, resp.text

        alpha = await multi_client.get("/api/p/alpha/weft/findings")
        bravo = await multi_client.get("/api/p/bravo/weft/findings")
        assert alpha.json()["items"] == []
        assert bravo.json()["items"] == []

    async def test_unscoped_living_alias_write_fails_closed(self, multi_client: AsyncClient) -> None:
        """The living-surface alias POST /api/scan-results also fails closed."""
        resp = await multi_client.post("/api/scan-results", json=_SCAN_BODY)
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == ErrorCode.VALIDATION

    async def test_path_scoped_write_lands_in_that_project_only(self, multi_client: AsyncClient) -> None:
        """A path-scoped write creates findings in the named project, names it in
        the response header, and does NOT touch the other project."""
        resp = await multi_client.post("/api/p/bravo/weft/scan-results", json=_SCAN_BODY)
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("X-Filigree-Project") == "bravo"

        bravo = await multi_client.get("/api/p/bravo/weft/findings")
        alpha = await multi_client.get("/api/p/alpha/weft/findings")
        assert len(bravo.json()["items"]) == 1
        assert alpha.json()["items"] == []  # untouched — no contamination

    async def test_query_scoped_write_routes_like_path(self, multi_client: AsyncClient) -> None:
        """?project= scopes the federation API uniformly with /mcp."""
        resp = await multi_client.post("/api/weft/scan-results?project=alpha", json=_SCAN_BODY)
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("X-Filigree-Project") == "alpha"
        alpha = await multi_client.get("/api/weft/findings?project=alpha")
        assert len(alpha.json()["items"]) == 1

    async def test_unscoped_read_stays_lenient(self, multi_client: AsyncClient) -> None:
        """Reads keep the default-project fallback — only writes fail closed."""
        resp = await multi_client.get("/api/weft/issues")
        assert resp.status_code == 200, resp.text

    async def test_unscoped_read_echoes_resolved_default_project(self, multi_client: AsyncClient) -> None:
        """C-10(a) honest-seams: an unscoped read silently resolves to the
        default project, so the response must name which project it hit — a
        defaulted read is not allowed to be silent about its destination."""
        resp = await multi_client.get("/api/weft/issues")
        assert resp.status_code == 200, resp.text
        # alpha is the first-loaded project => the resolved default.
        assert resp.headers.get("X-Filigree-Project") == "alpha"


class TestServerModeCrossProjectReadBlocking:
    """2.1.0 §1.3: in multi-project server mode, read endpoints reject
    foreign-prefix IDs at the route boundary with 404/safe_message.

    The data layer's ``db.get_issue`` deliberately *tolerates* foreign
    IDs (returns KeyError) so jsonl import, migration, ``filigree
    doctor``, and direct CLI/MCP read paths keep working cross-prefix.
    The route layer is where cross-project probing becomes a meaningful
    attack surface — only one process serves both projects — so the
    route-level guard fires only when ``_project_store`` is set.
    """

    async def test_dashboard_server_mode_blocks_cross_project_read(self, multi_client: AsyncClient) -> None:
        """A bravo-prefixed ID requested through the alpha scope returns
        404/safe_message — indistinguishable from a same-project miss."""
        from filigree.core import WrongProjectError

        resp = await multi_client.get("/api/p/alpha/issue/bravo-deadbeef00")
        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body["code"] == "NOT_FOUND", body
        # safe_message must be returned verbatim; no prefix leaked.
        assert body["error"] == WrongProjectError.SAFE_MESSAGE
        assert "bravo" not in body["error"]
        assert "alpha" not in body["error"]

    async def test_dashboard_server_mode_blocks_cross_project_loom_read(self, multi_client: AsyncClient) -> None:
        """The loom mirror enforces the same guard."""
        from filigree.core import WrongProjectError

        resp = await multi_client.get("/api/p/alpha/weft/issues/bravo-deadbeef00")
        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body["code"] == "NOT_FOUND", body
        assert body["error"] == WrongProjectError.SAFE_MESSAGE

    async def test_dashboard_server_mode_blocks_cross_project_plan_read(self, multi_client: AsyncClient) -> None:
        """Plan reads are issue-backed and must use the same route boundary guard."""
        from filigree.core import WrongProjectError

        resp = await multi_client.get("/api/p/alpha/plan/bravo-deadbeef00")

        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body["code"] == "NOT_FOUND", body
        assert body["error"] == WrongProjectError.SAFE_MESSAGE
        assert "bravo" not in body["error"]
        assert "alpha" not in body["error"]

    async def test_dashboard_server_mode_blocks_cross_project_release_tree_read(self, multi_client: AsyncClient) -> None:
        """Release-tree reads must use the same route boundary guard as
        issue/plan reads — otherwise the KeyError handler echoes the
        foreign-prefixed ID instead of the safe cross-project wording."""
        from filigree.core import WrongProjectError

        resp = await multi_client.get("/api/p/alpha/release/bravo-deadbeef00/tree")

        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body["code"] == "NOT_FOUND", body
        assert body["error"] == WrongProjectError.SAFE_MESSAGE
        assert "bravo" not in body["error"]
        assert "alpha" not in body["error"]

    async def test_server_mode_same_project_miss_indistinguishable(self, multi_client: AsyncClient) -> None:
        """Same-project miss and cross-project miss must return the same
        status code so a probe cannot distinguish the two. Bodies differ
        (one carries the offending ID, one is safe wording) but a caller
        gating on ``status_code`` alone learns nothing about other projects.
        """
        cross = await multi_client.get("/api/p/alpha/issue/bravo-deadbeef00")
        same = await multi_client.get("/api/p/alpha/issue/alpha-deadbeef00")
        assert cross.status_code == 404
        assert same.status_code == 404
        assert cross.json()["code"] == "NOT_FOUND"
        assert same.json()["code"] == "NOT_FOUND"

    async def test_patch_foreign_prefix_uses_safe_message(self, multi_client: AsyncClient) -> None:
        """Write-side WrongProjectError should not leak either project prefix."""
        from filigree.core import WrongProjectError

        resp = await multi_client.patch("/api/p/alpha/issue/bravo-deadbeef00", json={"title": "probe"})

        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert body["error"] == WrongProjectError.SAFE_MESSAGE
        assert "alpha" not in body["error"]
        assert "bravo" not in body["error"]


class TestGetIssueReadToleranceLocally:
    """2.1.0 §1.3: db.get_issue keeps its documented read-tolerance for
    cross-prefix IDs so jsonl import, migration, doctor, and direct
    CLI / MCP read paths continue to work — the route-layer guard only
    fires in server mode through the HTTP boundary, not on the data
    layer itself.
    """

    def test_get_issue_cli_still_tolerates_foreign_id_locally(self, tmp_path: Path) -> None:
        from filigree.core import FiligreeDB, WrongProjectError

        db = FiligreeDB(tmp_path / "alpha.db", prefix="alpha")
        db.initialize()
        # Direct data-layer call must raise KeyError, NOT WrongProjectError,
        # so cross-prefix reads stay tolerant for the data layer.
        with pytest.raises(KeyError):
            db.get_issue("bravo-deadbeef00")
        # And the prefix-check helper still raises rich WrongProjectError
        # for the CLI / doctor diagnostics path.
        with pytest.raises(WrongProjectError) as excinfo:
            db._check_id_prefix("bravo-deadbeef00")
        rich = str(excinfo.value)
        assert "alpha" in rich
        assert "bravo" in rich
        db.close()


class TestMultiProjectManagement:
    """Tests for server-mode management endpoints."""

    async def test_list_projects(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.get("/api/projects")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        keys = {p["key"] for p in data}
        assert keys == {"alpha", "bravo"}

    async def test_reload_endpoint(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.post("/api/reload")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "added" in data
        assert "removed" in data
        # filigree-173e76a28a: frontend ui.js reads data.ok and data.projects.
        assert data["ok"] is True
        assert data["projects"] == 2
        assert data["status"] == "ok"

    async def test_reload_endpoint_surfaces_errors(self, multi_client: AsyncClient, tmp_path: Path) -> None:
        config_dir = tmp_path / ".config" / "filigree"
        (config_dir / "server.json").write_text("{bad json")

        resp = await multi_client.post("/api/reload")
        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == "IO"
        assert "reload" in body["error"].lower()

    async def test_health_in_server_mode(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["mode"] == "server"
        assert data["projects"] == 2


class TestEtherealProjectsEndpoint:
    """Backward-compat: /api/projects in ethereal mode."""

    async def test_projects_returns_single_with_empty_key(self, client: AsyncClient) -> None:
        resp = await client.get("/api/projects")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["key"] == ""
        assert data[0]["name"] == "test"  # from populated_db prefix
