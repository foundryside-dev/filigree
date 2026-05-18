"""Phase 5.1 concurrency guardrails for 2.1.0 release prep."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

import filigree.db_base as db_base
from filigree.core import FiligreeDB
from filigree.types.api import ClaimConflictError


def _open_thread_db(db_path: Path) -> FiligreeDB:
    db = FiligreeDB(db_path, prefix="test")
    db.initialize()
    return db


def test_claim_simultaneous_two_agents(tmp_path: Path) -> None:
    """Two agents racing to claim the same issue produce one winner and one typed conflict."""
    db_path = tmp_path / "filigree.db"
    seed = _open_thread_db(db_path)
    issue = seed.create_issue("simultaneous claim target")
    seed.close()

    barrier = threading.Barrier(2)
    successes: list[str] = []
    conflicts: list[ClaimConflictError] = []
    errors: list[BaseException] = []

    def worker(agent: str) -> None:
        db = _open_thread_db(db_path)
        try:
            barrier.wait(timeout=5)
            try:
                claimed = db.claim_issue(issue.id, assignee=agent, actor=agent)
                successes.append(claimed.assignee)
            except ClaimConflictError as exc:
                conflicts.append(exc)
        except BaseException as exc:  # pragma: no cover - surfaced by assertions
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker, args=(agent,)) for agent in ("alice", "bob")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(successes) == 1
    assert len(conflicts) == 1

    verify = _open_thread_db(db_path)
    try:
        final = verify.get_issue(issue.id)
        assert final.assignee == successes[0]
        assert conflicts[0].observed == final.assignee
        assert conflicts[0].expected in {"alice", "bob"} - {final.assignee}
        claim_events = [event for event in verify.get_issue_events(issue.id, limit=10) if event["event_type"] == "claimed"]
        assert len(claim_events) == 1
    finally:
        verify.close()


def test_reclaim_heartbeat_race(tmp_path: Path) -> None:
    """A reclaim racing a heartbeat settles on the reclaimed holder without raw lock errors."""
    db_path = tmp_path / "filigree.db"
    seed = _open_thread_db(db_path)
    issue = seed.create_issue("reclaim heartbeat target")
    seed.claim_issue(issue.id, assignee="alice", actor="alice")
    seed.close()

    barrier = threading.Barrier(2)
    successes: list[str] = []
    conflicts: list[ClaimConflictError] = []
    errors: list[BaseException] = []

    def heartbeat() -> None:
        db = _open_thread_db(db_path)
        try:
            barrier.wait(timeout=5)
            try:
                db.heartbeat_work(issue.id, actor="alice", expected_assignee="alice")
                successes.append("heartbeat")
            except ClaimConflictError as exc:
                conflicts.append(exc)
        except BaseException as exc:  # pragma: no cover - surfaced by assertions
            errors.append(exc)
        finally:
            db.close()

    def reclaim() -> None:
        db = _open_thread_db(db_path)
        try:
            barrier.wait(timeout=5)
            db.reclaim_issue(
                issue.id,
                assignee="bob",
                expected_assignee="alice",
                reason="lease stale",
                actor="bob",
            )
            successes.append("reclaim")
        except BaseException as exc:  # pragma: no cover - surfaced by assertions
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=heartbeat), threading.Thread(target=reclaim)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert "reclaim" in successes
    assert len(conflicts) <= 1

    verify = _open_thread_db(db_path)
    try:
        assert verify.get_issue(issue.id).assignee == "bob"
    finally:
        verify.close()


def test_busy_timeout_retry_behavior(db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    """Transient SQLITE_BUSY from BEGIN IMMEDIATE is retried before surfacing."""
    real_begin = db_base._begin_immediate
    attempts = {"count": 0}

    def flaky_begin(conn: sqlite3.Connection, operation: str) -> None:
        if operation == "create_issue" and attempts["count"] < 2:
            attempts["count"] += 1
            exc = sqlite3.OperationalError("database is locked")
            exc.sqlite_errorcode = sqlite3.SQLITE_BUSY  # type: ignore[attr-defined]
            raise exc
        attempts["count"] += 1
        real_begin(conn, operation)

    monkeypatch.setattr(db_base, "_begin_immediate", flaky_begin)

    issue = db.create_issue("busy retry eventually succeeds")

    assert issue.title == "busy retry eventually succeeds"
    assert attempts["count"] == 3


def test_begin_immediate_retries_real_sqlite_busy(tmp_path: Path) -> None:
    """A real locked SQLite writer is retried until the blocker commits."""
    db_path = tmp_path / "filigree.db"
    seed = _open_thread_db(db_path)
    seed.close()

    contender = FiligreeDB(db_path, prefix="test", check_same_thread=False)
    contender.initialize()
    contender.conn.execute("PRAGMA busy_timeout=1")
    begin_attempted = threading.Event()
    begin_statements: list[str] = []

    def trace(sql: str) -> None:
        if sql.lstrip().upper().startswith("BEGIN IMMEDIATE"):
            begin_statements.append(sql)
            begin_attempted.set()

    contender.conn.set_trace_callback(trace)
    blocker = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        blocker.execute("PRAGMA journal_mode=WAL")
        blocker.execute("BEGIN IMMEDIATE")

        errors: list[BaseException] = []
        created: list[str] = []

        def worker() -> None:
            try:
                created.append(contender.create_issue("real busy retry").id)
            except BaseException as exc:  # pragma: no cover - surfaced by assertions
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        assert begin_attempted.wait(timeout=2.0)
        time.sleep(0.02)
        blocker.commit()
        thread.join(timeout=5.0)

        assert not thread.is_alive()
        assert errors == []
        assert len(created) == 1
        assert len(begin_statements) >= 2
    finally:
        contender.conn.set_trace_callback(None)
        blocker.close()
        contender.close()
