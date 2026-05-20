"""Decorator-stack tests for 2.1.0 §2.1.

``@_in_immediate_tx`` and ``@_retry_busy`` add transaction discipline +
SQLITE_BUSY recovery to every public write method on ``IssuesMixin``. These
unit tests pin the decorators' contracts directly so a regression surfaces
without running the full mixin suite under contention.
"""

from __future__ import annotations

import sqlite3
import types

import pytest

from filigree.core import FiligreeDB
from filigree.db_base import _in_immediate_tx, _retry_busy


def _sqlite_operational_error(message: str, code: int | None = None) -> sqlite3.OperationalError:
    exc = sqlite3.OperationalError(message)
    if code is not None:
        exc.sqlite_errorcode = code
    return exc


def test_immediate_transaction_decorator_rolls_back_on_exception(db: FiligreeDB) -> None:
    """Body that raises mid-transaction must rollback all writes."""
    issue = db.create_issue("decorator rollback target", priority=2)

    @_in_immediate_tx("test_op")
    def op(self: FiligreeDB) -> None:
        self.conn.execute("UPDATE issues SET title = ? WHERE id = ?", ("MUTATED", issue.id))
        msg = "boom"
        raise RuntimeError(msg)

    bound = types.MethodType(op, db)
    with pytest.raises(RuntimeError, match="boom"):
        bound()
    # Mutation must not be visible — decorator rolled back.
    row = db.conn.execute("SELECT title FROM issues WHERE id = ?", (issue.id,)).fetchone()
    assert row["title"] == "decorator rollback target"
    # Transaction must be closed.
    assert db.conn.in_transaction is False


def test_immediate_transaction_decorator_rolls_back_when_commit_is_busy() -> None:
    """A COMMIT-time SQLITE_BUSY must rollback before the outer retry runs."""

    class FakeConn:
        def __init__(self) -> None:
            self.in_transaction = False
            self.commits = 0
            self.rollbacks = 0
            self.begins = 0

        def execute(self, sql: str) -> None:
            assert sql == "BEGIN IMMEDIATE"
            if self.in_transaction:
                msg = "cannot start a transaction within a transaction"
                raise RuntimeError(msg)
            self.in_transaction = True
            self.begins += 1

        def commit(self) -> None:
            self.commits += 1
            if self.commits == 1:
                raise _sqlite_operational_error("database is locked", sqlite3.SQLITE_BUSY)
            self.in_transaction = False

        def rollback(self) -> None:
            self.rollbacks += 1
            self.in_transaction = False

    class Owner:
        def __init__(self) -> None:
            self.conn = FakeConn()
            self.calls = 0

    @_retry_busy(attempts=2, base=0.01, sleep=lambda _: None)
    @_in_immediate_tx("commit_busy_op")
    def op(self: Owner) -> int:
        self.calls += 1
        return self.calls

    owner = Owner()

    assert op(owner) == 2
    assert owner.calls == 2
    assert owner.conn.begins == 2
    assert owner.conn.commits == 2
    assert owner.conn.rollbacks == 1
    assert owner.conn.in_transaction is False


def test_immediate_transaction_decorator_skip_begin_is_passthrough(db: FiligreeDB) -> None:
    """``_skip_begin=True`` makes the decorator a no-op for tx lifecycle."""

    saw_in_tx: list[bool] = []

    @_in_immediate_tx("test_op")
    def op(self: FiligreeDB) -> None:
        saw_in_tx.append(self.conn.in_transaction)

    bound = types.MethodType(op, db)
    # No outer tx: pass-through means body runs with no tx open.
    bound(_skip_begin=True)
    assert saw_in_tx == [False]

    # With outer tx already open: pass-through must not BEGIN again
    # (would raise) and must not COMMIT (would close outer prematurely).
    db.conn.execute("BEGIN IMMEDIATE")
    try:
        bound(_skip_begin=True)
        assert saw_in_tx == [False, True]
        assert db.conn.in_transaction is True
    finally:
        db.conn.rollback()


class _BusyFn:
    def __init__(self, busy_for: int) -> None:
        self.busy_for = busy_for
        self.calls = {"n": 0}

    def __call__(self, self_arg: object) -> int:
        self.calls["n"] += 1
        if self.calls["n"] <= self.busy_for:
            raise _sqlite_operational_error("database is locked", sqlite3.SQLITE_BUSY)
        return self.calls["n"]


def _make_busy_fn(busy_for: int) -> _BusyFn:
    """Return a callable that raises SQLITE_BUSY for the first N calls, then succeeds."""
    return _BusyFn(busy_for)


def test_busy_retry_decorator_transparently_recovers() -> None:
    """Two transient BUSY failures then success: caller sees the success."""
    slept: list[float] = []
    fn = _make_busy_fn(busy_for=2)
    wrapped = _retry_busy(attempts=3, base=0.01, sleep=slept.append)(fn)
    result = wrapped(object())
    assert result == 3  # third call succeeded
    assert slept == [0.01, 0.02]  # backoff before retries 2 and 3


def test_busy_retry_decorator_uses_sqlite_errorcode_not_message() -> None:
    """A BUSY error with non-standard text still consumes the retry budget."""
    slept: list[float] = []
    calls = {"n": 0}

    def fn(self: object) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _sqlite_operational_error("WAL checkpoint is unavailable", sqlite3.SQLITE_BUSY)
        return calls["n"]

    wrapped = _retry_busy(attempts=2, base=0.01, sleep=slept.append)(fn)

    assert wrapped(object()) == 2
    assert slept == [0.01]


def test_busy_retry_decorator_re_raises_after_exhaustion() -> None:
    """Three BUSY failures with attempts=3: original OperationalError surfaces."""
    slept: list[float] = []
    fn = _make_busy_fn(busy_for=5)  # always busy within budget
    wrapped = _retry_busy(attempts=3, base=0.01, sleep=slept.append)(fn)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        wrapped(object())
    assert fn.calls["n"] == 3
    assert slept == [0.01, 0.02]  # no sleep after final failure


def test_busy_retry_decorator_passes_through_when_skip_begin_true(db: FiligreeDB) -> None:
    """Inner call inside an outer tx must NOT retry — BUSY propagates so the
    outer's retry loop can rollback + retry the whole composite.

    Uses the real decorator stack (``@_retry_busy @_in_immediate_tx``) because
    ``_in_immediate_tx`` is what consumes ``_skip_begin``; ``_retry_busy``
    forwards it. Running the stack against a real DB connection mirrors how
    composed callers like ``start_work`` invoke decorated leaf methods.
    """
    slept: list[float] = []
    calls = {"n": 0}

    @_retry_busy(attempts=3, base=0.01, sleep=slept.append)
    @_in_immediate_tx("inner_op")
    def inner(self: FiligreeDB) -> None:
        calls["n"] += 1
        raise _sqlite_operational_error("database is locked", sqlite3.SQLITE_BUSY)

    bound = types.MethodType(inner, db)
    # Open an outer tx so _skip_begin makes sense.
    db.conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            bound(_skip_begin=True)
    finally:
        if db.conn.in_transaction:
            db.conn.rollback()
    assert calls["n"] == 1  # single call, no retry
    assert slept == []


def test_busy_retry_decorator_does_not_retry_other_operational_errors() -> None:
    """Only SQLITE_BUSY / SQLITE_LOCKED are retried — other OperationalErrors propagate."""
    slept: list[float] = []
    calls = {"n": 0}

    def fn(self: object) -> None:
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: phantom")

    wrapped = _retry_busy(attempts=3, base=0.01, sleep=slept.append)(fn)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        wrapped(object())
    assert calls["n"] == 1
    assert slept == []
