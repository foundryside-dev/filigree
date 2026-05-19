"""Fixtures for MCP server tests."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from filigree.core import DB_FILENAME, FILIGREE_DIR_NAME, SUMMARY_FILENAME, FiligreeDB, write_config
from filigree.types.core import ClarionConfig, RegistryBackend
from tests._seeds import SeededMCPClient, seed_observations, seed_open_bug

# Re-export _parse so existing ``from tests.mcp.conftest import _parse``
# imports continue to work during migration.  New code should import from
# ``tests.mcp._helpers`` instead.
from tests.mcp._helpers import _parse as _parse


def _make_mcp_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    registry_backend: RegistryBackend = "local",
    clarion_config: ClarionConfig | None = None,
) -> FiligreeDB:
    filigree_dir = tmp_path / FILIGREE_DIR_NAME
    filigree_dir.mkdir()
    config: dict[str, Any] = {"prefix": "mcp", "version": 1}
    if registry_backend != "local":
        config["registry_backend"] = registry_backend
        config["clarion"] = dict(clarion_config or {})
    write_config(filigree_dir, config)
    (filigree_dir / SUMMARY_FILENAME).write_text("# test\n")

    d = FiligreeDB(
        filigree_dir / DB_FILENAME,
        prefix="mcp",
        registry_backend=registry_backend,
        clarion_config=clarion_config,
        # MCP fixtures stub Clarion absence; skip the startup capability
        # probe so the placeholder base_url doesn't abort __init__.
        skip_clarion_capability_probe=True,
    )
    d.initialize()

    import filigree.mcp_server as mcp_mod

    monkeypatch.setattr(mcp_mod, "db", d)
    monkeypatch.setattr(mcp_mod, "_filigree_dir", filigree_dir)
    return d


@pytest.fixture
def mcp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[FiligreeDB, None, None]:
    """Set up a FiligreeDB and patch the MCP module globals.

    Uses ``monkeypatch.setattr`` so globals are restored even if the
    test raises between the patch and the ``yield`` — a manual
    save/restore pattern leaks state in that window.
    """
    d = _make_mcp_db(tmp_path, monkeypatch)

    try:
        yield d
    finally:
        d.close()


@pytest.fixture
def clarion_mcp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[FiligreeDB, None, None]:
    """Set up MCP globals with a constructor-validated Clarion registry DB."""
    d = _make_mcp_db(
        tmp_path,
        monkeypatch,
        registry_backend="clarion",
        clarion_config={"base_url": "http://localhost:9111"},
    )

    try:
        yield d
    finally:
        d.close()


class _InProcessMCPClient:
    """Direct handle on the installed tool handlers for in-process testing.

    Mirrors the shape the tests use: `.call_tool(name, args)` returns an
    object with `.content[0].text` (a JSON string).
    """

    def __init__(self, db: FiligreeDB) -> None:
        self._db = db
        # Import lazily to avoid import cycles at conftest collection.
        from filigree import mcp_server as _mod

        self._mod = _mod

    def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        # mcp_server.py (verified 2026-04-18) exposes a module-level
        # `async def call_tool(name, arguments)` at line 344 decorated
        # with `@server.call_tool()`; there is NO `handle_call_tool`
        # method. Invoke the module-level function directly so tests
        # hit the same dispatch path the MCP runtime calls.
        import asyncio

        content = asyncio.run(self._mod.call_tool(name, args))

        # Return an object whose .content[0].text is the JSON payload
        class _Result:
            def __init__(self, content_: Any) -> None:
                self.content = content_

        return _Result(content)

    def list_tools(self) -> Any:
        # Module-level `async def list_tools()` at mcp_server.py:339,
        # decorated with `@server.list_tools()`.
        import asyncio

        return asyncio.run(self._mod.list_tools())


@pytest.fixture
def mcp_client_for_empty_project(mcp_db: FiligreeDB) -> SeededMCPClient:
    """Empty initialized project, no issues."""
    return SeededMCPClient(client=_InProcessMCPClient(mcp_db))


@pytest.fixture
def mcp_client_with_open_bug(mcp_db: FiligreeDB) -> SeededMCPClient:
    """One open, unclaimed bug."""
    bug_id = seed_open_bug(mcp_db)
    return SeededMCPClient(client=_InProcessMCPClient(mcp_db), bug_id=bug_id)


@pytest.fixture
def mcp_client_with_open_bug_with_files(mcp_db: FiligreeDB) -> SeededMCPClient:
    bug = mcp_db.create_issue("Test bug", type="bug", priority=2)
    # `register_file(path)` returns a FileRecord; grab its `.id` so the
    # association links by file_id, not path. Signature is
    # `add_file_association(file_id, issue_id, assoc_type)`; assoc_type
    # must be one of VALID_ASSOC_TYPES (see db_files.py):
    # "bug_in", "task_for", "scan_finding", "mentioned_in".
    fr = mcp_db.register_file("src/example.py")
    mcp_db.add_file_association(fr.id, bug.id, "mentioned_in")
    return SeededMCPClient(client=_InProcessMCPClient(mcp_db), bug_id=bug.id)


@pytest.fixture
def mcp_client_with_wip_bug_owned_by_alice(mcp_db: FiligreeDB) -> SeededMCPClient:
    bug = mcp_db.create_issue("Alice's bug", type="bug", priority=2)
    mcp_db.claim_issue(bug.id, assignee="alice")
    mcp_db.update_issue(bug.id, status="confirmed")  # canonical wip for bug pack
    return SeededMCPClient(client=_InProcessMCPClient(mcp_db), bug_id=bug.id)


@pytest.fixture
def mcp_client_with_wip_bug_owned_by_bob(mcp_db: FiligreeDB) -> SeededMCPClient:
    bug = mcp_db.create_issue("Bob's bug", type="bug", priority=2)
    mcp_db.claim_issue(bug.id, assignee="bob")
    mcp_db.update_issue(bug.id, status="confirmed")
    return SeededMCPClient(client=_InProcessMCPClient(mcp_db), bug_id=bug.id)


@pytest.fixture
def mcp_client_with_closed_bug(mcp_db: FiligreeDB) -> SeededMCPClient:
    bug = mcp_db.create_issue("Closed bug", type="bug", priority=2)
    mcp_db.close_issue(bug.id, reason="fixed")
    return SeededMCPClient(client=_InProcessMCPClient(mcp_db), bug_id=bug.id)


@pytest.fixture
def mcp_client_with_open_issues(mcp_db: FiligreeDB) -> SeededMCPClient:
    """5 open issues (bugs + tasks, mixed priorities)."""
    ids: list[str] = []
    for i in range(5):
        issue = mcp_db.create_issue(f"Issue {i}", priority=(i % 3))
        ids.append(issue.id)
    return SeededMCPClient(client=_InProcessMCPClient(mcp_db), open_issue_ids=ids)


@pytest.fixture
def mcp_client_with_many_open_issues(mcp_db: FiligreeDB) -> SeededMCPClient:
    """12 open issues — enough to exercise list pagination at limit=5."""
    ids: list[str] = []
    for i in range(12):
        issue = mcp_db.create_issue(f"Issue {i}", priority=2)
        ids.append(issue.id)
    return SeededMCPClient(client=_InProcessMCPClient(mcp_db), open_issue_ids=ids)


@pytest.fixture
def mcp_client_with_ready_queue(mcp_db: FiligreeDB) -> SeededMCPClient:
    """One issue per priority 0..2 (ready)."""
    for p in (0, 1, 2):
        mcp_db.create_issue(f"P{p}", type="bug", priority=p)
    return SeededMCPClient(client=_InProcessMCPClient(mcp_db))


@pytest.fixture
def mcp_client_with_same_priority_queue(mcp_db: FiligreeDB) -> SeededMCPClient:
    """Two P2 bugs, A created first, then B (deterministic created_at order)."""
    a = mcp_db.create_issue("A", type="bug", priority=2)
    b = mcp_db.create_issue("B", type="bug", priority=2)
    return SeededMCPClient(
        client=_InProcessMCPClient(mcp_db),
        issue_a_id=a.id,
        issue_b_id=b.id,
        a_id=a.id,
        b_id=b.id,
    )


@pytest.fixture
def mcp_client_with_two_issues(mcp_db: FiligreeDB) -> SeededMCPClient:
    a = mcp_db.create_issue("A", priority=2)
    b = mcp_db.create_issue("B", priority=2)
    return SeededMCPClient(client=_InProcessMCPClient(mcp_db), a_id=a.id, b_id=b.id)


@pytest.fixture
def mcp_client_with_observations(mcp_db: FiligreeDB) -> SeededMCPClient:
    obs_ids = seed_observations(mcp_db, count=3)
    return SeededMCPClient(client=_InProcessMCPClient(mcp_db), obs_ids=obs_ids)


@pytest.fixture
def mcp_client_with_ambiguous_pack_issue(mcp_db: FiligreeDB) -> SeededMCPClient:
    """Issue on a type whose `open` state transitions to TWO wip-category targets.

    Building this requires a WorkflowPack with branching workflow. Inspect
    src/filigree/packs/ for a pack that qualifies, or register a custom one
    via mcp_db.templates. If no builtin pack branches this way, copy the
    'bug' pack and add a second wip-target state in a test-only pack file
    under tests/fixtures/packs/.

    Yields a SeededMCPClient with `.issue_id` set to the ambiguous issue.
    """
    # Implementation note: the Stage 3 canonical_working_status code lands
    # the exceptions this scenario exercises; this fixture is used by
    # Task 3.2. At fixture-creation time, just define the pack and an
    # issue on it; the client call is what raises.
    pytest.skip(
        "Task 3.2 scaffold: requires a branching WorkflowPack (see tests/workflows/"
        "conftest.py builtin_pack_with_two_wip for a minimal example) wired into "
        "mcp_db.templates before returning the SeededMCPClient."
    )


@pytest.fixture
def mcp_client_with_too_new_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SeededMCPClient:
    """Initialize a DB, then bump user_version so the next open fails.

    Scaffolded — requires Task 5.4's `_attempt_startup()` to fully wire.
    Tests using this fixture should be gated on Stage 5 being complete.
    """
    pytest.skip(
        "Task 5.4 scaffold: mcp_client_with_too_new_db requires _attempt_startup() "
        "wiring in mcp_server.py. The fixture body below shows the intended shape "
        "(seed filigree_dir + bump user_version), but the degraded-mode path it "
        "exercises doesn't exist until Task 5.4."
    )
    # Intended fixture body (uncomment when Task 5.4 lands):
    # import sqlite3
    # from filigree.core import DB_FILENAME, FILIGREE_DIR_NAME, SUMMARY_FILENAME, write_config
    # from filigree.db_schema import CURRENT_SCHEMA_VERSION
    #
    # filigree_dir = tmp_path / FILIGREE_DIR_NAME
    # filigree_dir.mkdir()
    # write_config(filigree_dir, {"prefix": "mcp", "version": 1})
    # (filigree_dir / SUMMARY_FILENAME).write_text("# test\n")
    #
    # d = FiligreeDB(filigree_dir / DB_FILENAME, prefix="mcp")
    # d.initialize()
    # d.close()
    #
    # conn = sqlite3.connect(filigree_dir / DB_FILENAME)
    # conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
    # conn.commit()
    # conn.close()
    #
    # # Point mcp_server at this filigree_dir so startup sees the too-new DB.
    # # monkeypatch auto-restores on teardown regardless of test exceptions.
    # import filigree.mcp_server as mcp_mod
    # monkeypatch.setattr(mcp_mod, "db", None)
    # monkeypatch.setattr(mcp_mod, "_filigree_dir", filigree_dir)
    # # Call Task 5.4's _attempt_startup() here — it sets the degraded-mode flag.
    # # mcp_mod._attempt_startup()
    # return SeededMCPClient(client=_InProcessMCPClient(mcp_mod.db))  # type: ignore[arg-type]


@pytest.fixture
def mcp_client_for_same_project(initialized_project: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[SeededMCPClient, None, None]:
    """MCP client pointed at the same filigree_dir as the CLI `initialized_project` fixture."""
    import filigree.mcp_server as mcp_mod
    from filigree.core import FILIGREE_DIR_NAME, FiligreeDB

    filigree_dir = initialized_project / FILIGREE_DIR_NAME
    d = FiligreeDB.from_filigree_dir(filigree_dir)
    monkeypatch.setattr(mcp_mod, "db", d)
    monkeypatch.setattr(mcp_mod, "_filigree_dir", filigree_dir)
    yield SeededMCPClient(client=_InProcessMCPClient(d))
    d.close()
