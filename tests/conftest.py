"""Shared pytest fixtures for filigree tests."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from click.testing import CliRunner

from filigree.core import (
    DB_FILENAME,
    FILIGREE_DIR_NAME,
    SUMMARY_FILENAME,
    FiligreeDB,
    write_config,
)
from tests._db_factory import make_db


@dataclass
class PopulatedDB:
    """Wrapper around FiligreeDB with pre-seeded issue IDs.

    Avoids monkey-patching ``_test_ids`` onto FiligreeDB instances.
    Access the underlying database via ``.db`` and the seed-issue IDs
    via ``.ids`` (keys: ``epic``, ``a``, ``b``, ``c``).
    """

    db: FiligreeDB
    ids: dict[str, str] = field(default_factory=dict)


@pytest.fixture
def db(tmp_path: Path) -> Generator[FiligreeDB, None, None]:
    """Fresh FiligreeDB for each test (default packs)."""
    d = make_db(tmp_path)
    yield d
    d.close()


@pytest.fixture
def populated_db(db: FiligreeDB) -> PopulatedDB:
    """FiligreeDB pre-populated with a representative issue set.

    Returns a ``PopulatedDB`` wrapper.  Access the database via ``.db``
    and the seed-issue IDs via ``.ids``.

    Creates:
    - 3 issues (A=open P1, B=open P2, C=closed P3)
    - Dependency: A depends on B
    - Labels on A: ["defect", "urgent"]
    - Comment on B
    - Epic E with child A
    """
    epic = db.create_issue("Epic E", type="epic", priority=1)
    a = db.create_issue("Issue A", priority=1, labels=["defect", "urgent"], parent_id=epic.id)
    b = db.create_issue("Issue B", priority=2)
    c = db.create_issue("Issue C", priority=3)
    db.close_issue(c.id, reason="done")
    db.add_dependency(a.id, b.id)
    db.add_comment(b.id, "Test comment", author="tester")
    return PopulatedDB(db=db, ids={"epic": epic.id, "a": a.id, "b": b.id, "c": c.id})


@pytest.fixture
def filigree_project(tmp_path: Path) -> Path:
    """A tmp directory set up as a filigree project (.filigree/ with config + db).

    Returns the project root (parent of .filigree/).
    """
    filigree_dir = tmp_path / FILIGREE_DIR_NAME
    filigree_dir.mkdir()
    write_config(filigree_dir, {"prefix": "proj", "version": 1})

    d = FiligreeDB(filigree_dir / DB_FILENAME, prefix="proj")
    d.initialize()
    d.close()

    # Create empty context.md
    (filigree_dir / SUMMARY_FILENAME).write_text("# summary\n")

    return tmp_path


@pytest.fixture
def cli_runner() -> CliRunner:
    """Click CLI test runner."""
    return CliRunner()


@pytest.fixture
def concurrent_db_workers(tmp_path: Path) -> Generator[list[FiligreeDB], None, None]:
    """Open multiple FiligreeDB connections to the same on-disk database.

    Returns a list of two connections by default; tests requesting a
    different N can call ``.append(make_db(tmp_path))`` to add more —
    every connection points at the same ``tmp_path / "filigree.db"``
    file so they exercise true SQLite-level concurrency. Lands as part
    of 2.1.0 §0.1 (the update_issue CAS test needs a real second writer
    to exercise the AND assignee = ? guard).
    """
    workers = [make_db(tmp_path) for _ in range(2)]
    try:
        yield workers
    finally:
        for w in workers:
            w.close()


def _fresh_project(tmp_path: Path) -> Path:
    """Run `filigree init` into tmp_path and return the project root."""
    from filigree.cli import cli

    original = os.getcwd()
    os.chdir(str(tmp_path))
    try:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--prefix", "test"])
        assert result.exit_code == 0, result.output
        return tmp_path
    finally:
        os.chdir(original)


@pytest.fixture
def initialized_project(tmp_path: Path) -> Path:
    """A freshly `filigree init`'d project. Returns the project root."""
    return _fresh_project(tmp_path)


@pytest.fixture
def release_db(tmp_path: Path) -> Generator[FiligreeDB, None, None]:
    """FiligreeDB with core + planning + release packs enabled."""
    d = make_db(tmp_path, packs=["core", "planning", "release"])
    yield d
    d.close()


@pytest.fixture
def beads_db(tmp_path: Path) -> Path:
    """Create a minimal beads-schema SQLite DB for migration tests.

    Schema mirrors the beads issue tracker with representative data.
    """
    db_path = tmp_path / "beads.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE issues (
            id TEXT PRIMARY KEY,
            title TEXT,
            status TEXT DEFAULT 'open',
            priority INTEGER DEFAULT 2,
            issue_type TEXT DEFAULT 'task',
            parent_id TEXT,
            parent_epic TEXT,
            assignee TEXT DEFAULT '',
            created_at TEXT,
            updated_at TEXT,
            closed_at TEXT,
            deleted_at TEXT,
            description TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            metadata TEXT DEFAULT 'null',
            design TEXT DEFAULT '',
            acceptance_criteria TEXT DEFAULT '',
            estimated_minutes INTEGER DEFAULT 0,
            close_reason TEXT DEFAULT '',
            external_ref TEXT DEFAULT '',
            mol_type TEXT DEFAULT '',
            work_type TEXT DEFAULT '',
            quality_score TEXT DEFAULT '',
            source_system TEXT DEFAULT '',
            event_kind TEXT DEFAULT '',
            actor TEXT DEFAULT '',
            target TEXT DEFAULT '',
            payload TEXT DEFAULT '',
            source_repo TEXT DEFAULT '',
            await_type TEXT DEFAULT '',
            await_id TEXT DEFAULT '',
            role_type TEXT DEFAULT '',
            rig TEXT DEFAULT '',
            spec_id TEXT DEFAULT '',
            wisp_type TEXT DEFAULT '',
            sender TEXT DEFAULT ''
        );

        CREATE TABLE dependencies (
            issue_id TEXT NOT NULL,
            depends_on_id TEXT NOT NULL,
            type TEXT DEFAULT 'blocks',
            PRIMARY KEY (issue_id, depends_on_id)
        );

        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            issue_id TEXT NOT NULL,
            event_type TEXT,
            actor TEXT DEFAULT '',
            old_value TEXT,
            new_value TEXT,
            comment TEXT DEFAULT '',
            created_at TEXT
        );

        CREATE TABLE labels (
            issue_id TEXT NOT NULL,
            label TEXT NOT NULL,
            PRIMARY KEY (issue_id, label)
        );

        CREATE TABLE comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            issue_id TEXT NOT NULL,
            author TEXT DEFAULT '',
            text TEXT NOT NULL,
            created_at TEXT
        );
    """)

    # Insert test data
    now = "2026-01-15T10:00:00+00:00"
    conn.execute(
        "INSERT INTO issues (id, title, status, priority, issue_type, parent_id, parent_epic, "
        "assignee, created_at, updated_at, description, metadata, design) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("bd-aaa111", "Epic one", "open", 1, "epic", None, None, "", now, now, "An epic", "null", ""),
    )
    conn.execute(
        "INSERT INTO issues (id, title, status, priority, issue_type, parent_id, parent_epic, "
        "assignee, created_at, updated_at, description, metadata, design) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("bd-bbb222", "Task under epic", "open", 2, "task", None, "bd-aaa111", "alice", now, now, "A task", "null", ""),
    )
    conn.execute(
        "INSERT INTO issues (id, title, status, priority, issue_type, parent_id, parent_epic, "
        "assignee, created_at, updated_at, closed_at, description, metadata, design) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "bd-ccc333",
            "Closed bug",
            "closed",
            0,
            "bug",
            "bd-aaa111",
            None,
            "bob",
            now,
            now,
            now,
            "A bug",
            json.dumps({"source": "import"}),
            "fix the thing",
        ),
    )
    # Deleted issue (should NOT be migrated)
    conn.execute(
        "INSERT INTO issues (id, title, status, priority, issue_type, deleted_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("bd-del999", "Deleted issue", "open", 2, "task", now, now, now),
    )
    # Issue with unknown status
    conn.execute(
        "INSERT INTO issues (id, title, status, priority, issue_type, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("bd-ddd444", "Weird status", "review", 2, "task", now, now),
    )

    # Dependencies
    conn.execute(
        "INSERT INTO dependencies (issue_id, depends_on_id, type) VALUES (?, ?, ?)",
        ("bd-bbb222", "bd-ccc333", "blocks"),
    )
    # Dangling dependency (references deleted issue)
    conn.execute(
        "INSERT INTO dependencies (issue_id, depends_on_id, type) VALUES (?, ?, ?)",
        ("bd-bbb222", "bd-del999", "blocks"),
    )

    # Events
    conn.execute(
        "INSERT INTO events (issue_id, event_type, actor, new_value, created_at) VALUES (?, ?, ?, ?, ?)",
        ("bd-aaa111", "created", "system", "Epic one", now),
    )

    # Labels
    conn.execute("INSERT INTO labels (issue_id, label) VALUES (?, ?)", ("bd-aaa111", "important"))
    conn.execute("INSERT INTO labels (issue_id, label) VALUES (?, ?)", ("bd-bbb222", "backend"))

    # Comments
    conn.execute(
        "INSERT INTO comments (issue_id, author, text, created_at) VALUES (?, ?, ?, ?)",
        ("bd-bbb222", "alice", "working on this", now),
    )

    conn.commit()
    conn.close()
    return db_path
