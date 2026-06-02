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
            exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise exc
        attempts["count"] += 1
        real_begin(conn, operation)

    monkeypatch.setattr(db_base, "_begin_immediate", flaky_begin)

    issue = db.create_issue("busy retry eventually succeeds")

    assert issue.title == "busy retry eventually succeeds"
    assert attempts["count"] == 3


@pytest.mark.parametrize(
    ("operation", "mutate"),
    [
        ("add_comment", lambda db, issue_id: db.add_comment(issue_id, "busy comment")),
        ("add_label", lambda db, issue_id: db.add_label(issue_id, "busy-label")),
        ("remove_label", lambda db, issue_id: db.remove_label(issue_id, "busy-label")),
    ],
)
def test_meta_writes_use_busy_retry_and_begin_immediate(
    db: FiligreeDB,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    mutate: object,
) -> None:
    issue = db.create_issue("meta busy retry target")
    if operation == "remove_label":
        db.add_label(issue.id, "busy-label")
    real_begin = db_base._begin_immediate
    attempts = {"count": 0}

    def flaky_begin(conn: sqlite3.Connection, op: str) -> None:
        if op == operation and attempts["count"] < 2:
            attempts["count"] += 1
            exc = sqlite3.OperationalError("database is locked")
            exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise exc
        if op == operation:
            attempts["count"] += 1
        real_begin(conn, op)

    monkeypatch.setattr(db_base, "_begin_immediate", flaky_begin)

    mutate(db, issue.id)  # type: ignore[operator]

    assert attempts["count"] == 3


def _seed_finding(db: FiligreeDB) -> dict[str, str]:
    """Ingest one finding and return its id (for the update_finding case)."""
    stats = db.process_scan_results(
        scan_source="ruff",
        findings=[{"path": "src/seed.py", "rule_id": "E501", "severity": "low", "message": "m"}],
    )
    return {"finding_id": stats["new_finding_ids"][0]}


@pytest.mark.parametrize(
    ("operation", "setup", "mutate"),
    [
        pytest.param(
            "update_finding",
            _seed_finding,
            lambda db, ctx: db.update_finding(ctx["finding_id"], status="acknowledged", actor="t"),
            id="update_finding",
        ),
        pytest.param(
            "clean_stale_findings",
            lambda db: {},
            lambda db, ctx: db.clean_stale_findings(days=0, actor="t"),
            id="clean_stale_findings",
        ),
        pytest.param(
            "process_scan_results",
            lambda db: {},
            lambda db, ctx: db.process_scan_results(
                scan_source="ruff",
                findings=[{"path": "src/proc.py", "rule_id": "E501", "severity": "low", "message": "m"}],
            ),
            id="process_scan_results",
        ),
    ],
)
def test_finding_writes_use_busy_retry_and_begin_immediate(
    db: FiligreeDB,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    setup: object,
    mutate: object,
) -> None:
    """I2: the scan write paths now acquire the writer lock via BEGIN IMMEDIATE
    and retry transient BUSY, like every other write surface (e.g. create_issue).

    Pre-fix these methods used lazy DEFERRED transactions and never called
    ``_begin_immediate`` for their own operation, so ``attempts`` stays 0 and the
    final assertion fails (0 != 3).
    """
    ctx = setup(db)  # type: ignore[operator]
    real_begin = db_base._begin_immediate
    attempts = {"count": 0}

    def flaky_begin(conn: sqlite3.Connection, op: str) -> None:
        if op == operation and attempts["count"] < 2:
            attempts["count"] += 1
            exc = sqlite3.OperationalError("database is locked")
            exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise exc
        if op == operation:
            attempts["count"] += 1
        real_begin(conn, op)

    monkeypatch.setattr(db_base, "_begin_immediate", flaky_begin)

    mutate(db, ctx)  # type: ignore[operator]

    assert attempts["count"] == 3


def test_process_scan_results_create_observations_runs_in_one_immediate_tx(db: FiligreeDB) -> None:
    """I2 regression guard: the BEGIN IMMEDIATE write window wraps the whole
    ingest loop, including the ``create_observations`` path that
    ``report_finding`` drives in production. If that path opened or committed a
    transaction of its own it would trip ``_begin_immediate``'s nested-tx guard;
    asserting an observation lands and the tx is closed proves it stays
    commit-free inside the single window.
    """
    stats = db.process_scan_results(
        scan_source="ruff",
        findings=[{"path": "src/obs.py", "rule_id": "E501", "severity": "low", "message": "m"}],
        create_observations=True,
    )

    assert stats["observations_created"] == 1
    obs_count = db.conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()["n"]
    assert obs_count == 1
    # The single write window committed cleanly — no transaction left dangling.
    assert db.conn.in_transaction is False


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
