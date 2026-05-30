"""Fixtures for HTTP dashboard API tests (FastAPI)."""

from __future__ import annotations

import json
import socket
from collections.abc import AsyncIterator, Generator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import filigree.dashboard as dash_module
from filigree.core import DB_FILENAME, FiligreeDB, Issue, write_config
from filigree.dashboard import ProjectStore, create_app
from tests._db_factory import make_db
from tests._fakes.clarion_http import clarion_stub
from tests.conftest import PopulatedDB


@pytest.fixture
def dashboard_db(populated_db: PopulatedDB) -> PopulatedDB:
    """Use the populated_db fixture for dashboard tests.

    Reconnects the underlying DB with check_same_thread=False so sync
    handlers run in FastAPI's threadpool.  Returns the full PopulatedDB
    wrapper so tests can access ``.db`` and ``.ids``.
    """
    db = populated_db.db
    db.reconnect(check_same_thread=False)
    return populated_db


@pytest.fixture
async def client(dashboard_db: PopulatedDB) -> AsyncIterator[AsyncClient]:
    """Create a test client backed by a single-project DB (ethereal mode)."""
    dash_module._db = dashboard_db.db
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    dash_module._db = None


def _unused_localhost_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()
    return f"http://{host}:{port}"


@pytest.fixture
def clarion_fallback_dashboard_db(tmp_path: Path) -> Generator[FiligreeDB, None, None]:
    """Dashboard DB configured through the real Clarion fallback constructor path.

    Uses ``http://clarion.test`` (unresolvable) plus ``allow_local_fallback=True``
    so the startup capability probe fails with ``RegistryUnavailableError`` and
    is downgraded to a WARN — exercising the same fallback wiring an operator
    would hit with Clarion offline.
    """
    db = FiligreeDB(
        tmp_path / "filigree.db",
        prefix="test",
        check_same_thread=False,
        registry_backend="clarion",
        clarion_config={
            "base_url": "http://clarion.test",
            "allow_local_fallback": True,
        },
    )
    db.initialize()
    yield db
    db.close()


@pytest.fixture
async def clarion_fallback_client(clarion_fallback_dashboard_db: FiligreeDB) -> AsyncIterator[AsyncClient]:
    """Dashboard client backed by a constructor-validated Clarion fallback DB."""
    dash_module._db = clarion_fallback_dashboard_db
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    dash_module._db = None


@pytest.fixture
def unavailable_clarion_dashboard_db(tmp_path: Path) -> Generator[FiligreeDB, None, None]:
    """Dashboard DB configured for Clarion that became unreachable mid-session.

    Stands up a Clarion stub long enough for the startup capability probe to
    succeed, then shuts it down so subsequent ``resolve_file`` calls (e.g.
    from a scan-results POST) fail with ``RegistryUnavailableError``. This
    mirrors the real production failure mode (Filigree starts, Clarion crashes
    later) — the older variant that pointed at an unused port at construction
    time can no longer build a DB because ADR-014 fail-closed startup rejects
    a Clarion that was never reachable.
    """
    with clarion_stub() as (base_url, _state):
        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            check_same_thread=False,
            registry_backend="clarion",
            clarion_config={
                "base_url": base_url,
                "timeout_seconds": 0.1,
            },
        )
        db.initialize()
    # Stub has shut down — Clarion is now unreachable for the test's writes.
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
async def unavailable_clarion_client(unavailable_clarion_dashboard_db: FiligreeDB) -> AsyncIterator[AsyncClient]:
    """Dashboard client backed by a constructor-validated unreachable Clarion DB."""
    dash_module._db = unavailable_clarion_dashboard_db
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    dash_module._db = None


@pytest.fixture
def release_dashboard_db(tmp_path: Path) -> Generator[FiligreeDB, None, None]:
    """FiligreeDB initialized with the release workflow pack enabled."""
    db = make_db(tmp_path, packs=["core", "planning", "release"], check_same_thread=False)
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
async def release_client(release_dashboard_db: FiligreeDB) -> AsyncIterator[AsyncClient]:
    """Test client backed by a DB with the release pack (ethereal mode)."""
    dash_module._db = release_dashboard_db
    app = create_app()
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        dash_module._db = None


def make_release_hierarchy(db: FiligreeDB, *, include_done: bool = False) -> tuple[Issue, Issue, Issue]:
    """Returns (release, epic, task). Mirrors tests/core/test_releases.py."""
    release = db.create_issue("v1.0.0", type="release")
    epic = db.create_issue("Epic A", type="epic", parent_id=release.id)
    task = db.create_issue("Task A", type="task", parent_id=epic.id)
    if include_done:
        db.close_issue(task.id)
    return release, epic, task


def _create_project(base: Path, name: str, prefix: str, issue_count: int) -> Path:
    """Helper: create a .filigree/ project dir with *issue_count* issues."""
    filigree_dir = base / name / ".filigree"
    filigree_dir.mkdir(parents=True)
    write_config(filigree_dir, {"prefix": prefix, "version": 1})
    db = FiligreeDB(filigree_dir / DB_FILENAME, prefix=prefix, check_same_thread=False)
    db.initialize()
    for i in range(issue_count):
        db.create_issue(f"{prefix} issue {i + 1}")
    db.close()
    return filigree_dir


@pytest.fixture
def project_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[ProjectStore, None, None]:
    """Create a ProjectStore with two temp projects (alpha=1 issue, bravo=2 issues)."""
    config_dir = tmp_path / ".config" / "filigree"
    config_dir.mkdir(parents=True)
    monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
    monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")

    alpha_dir = _create_project(tmp_path, "proj-alpha", "alpha", 1)
    bravo_dir = _create_project(tmp_path, "proj-bravo", "bravo", 2)

    server_json = {
        "port": 8377,
        "projects": {
            str(alpha_dir): {"prefix": "alpha"},
            str(bravo_dir): {"prefix": "bravo"},
        },
    }
    (config_dir / "server.json").write_text(json.dumps(server_json))

    store = ProjectStore()
    store.load()
    yield store
    store.close_all()


@pytest.fixture
async def multi_client(project_store: ProjectStore) -> AsyncIterator[AsyncClient]:
    """Test client backed by a multi-project ProjectStore (server mode)."""
    dash_module._project_store = project_store
    app = create_app(server_mode=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    dash_module._project_store = None
