"""Retry-safe claim-next under concurrency (Task 0.4 review ruling (b)).

The held-claim check in ``start_next_work`` / ``claim_next`` must be decided
under the writer lock. Otherwise two concurrent retries by the same assignee
both pass an unlocked "do I already hold something?" read and each claim a
different issue, stranding one for the lease (MCP F4).

Each test uses two connections to the same on-disk database
(``concurrent_db_workers``). The interleaving is forced deterministically:
connection B's candidate discovery (``get_ready``, which runs after B's
unlocked held check) first runs connection A's whole call to completion. B
then reaches its locked claim with A's claim already committed.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import pytest

from filigree.core import FiligreeDB
from tests._db_factory import make_db


def _live_claims(db: FiligreeDB, assignee: str) -> list[str]:
    rows = db.conn.execute("SELECT id FROM issues WHERE assignee = ? ORDER BY id", (assignee,)).fetchall()
    return [r["id"] for r in rows]


def _interleave(monkeypatch: pytest.MonkeyPatch, b: FiligreeDB, run_a: Callable[[], object]) -> None:
    original = b.get_ready

    def get_ready_after_a_commits():  # type: ignore[no-untyped-def]
        run_a()
        return original()

    monkeypatch.setattr(b, "get_ready", get_ready_after_a_commits)


def test_concurrent_start_next_same_assignee_holds_one_claim(
    concurrent_db_workers: list[FiligreeDB], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = concurrent_db_workers
    a.create_issue("first", type="task", priority=0)
    a.create_issue("second", type="task", priority=1)
    first_result: list[object] = []
    _interleave(monkeypatch, b, lambda: first_result.append(a.start_next_work(assignee="alice")))

    second = b.start_next_work(assignee="alice")

    assert len(_live_claims(a, "alice")) == 1
    assert second is not None
    assert second.already_holding is True
    assert second.id == first_result[0].id  # type: ignore[attr-defined]


def test_concurrent_claim_next_same_assignee_holds_one_claim(
    concurrent_db_workers: list[FiligreeDB], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = concurrent_db_workers
    a.create_issue("first", type="task", priority=0)
    a.create_issue("second", type="task", priority=1)
    first_result: list[object] = []
    _interleave(monkeypatch, b, lambda: first_result.append(a.claim_next("alice")))

    second = b.claim_next("alice")

    assert len(_live_claims(a, "alice")) == 1
    assert second is not None
    assert second.already_holding is True
    assert second.id == first_result[0].id  # type: ignore[attr-defined]


def test_threaded_start_next_retries_hold_exactly_one_claim(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Real threads on two connections: the same assignee fires start_next twice at once."""
    seed = make_db(tmp_path, check_same_thread=False)
    for priority in range(4):
        seed.create_issue(f"candidate {priority}", type="task", priority=priority)
    workers = [seed, make_db(tmp_path, check_same_thread=False)]
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def run(db: FiligreeDB) -> None:
        try:
            barrier.wait()
            db.start_next_work(assignee="alice")
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(w,)) for w in workers]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(_live_claims(seed, "alice")) == 1
    finally:
        for w in workers:
            w.close()
