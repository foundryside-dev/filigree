"""The held-claim fallback of claim-next / start-next is a 60 s retry window (final review I3).

Task 0.4 made ``start_next_work`` / ``claim_next`` hand back the claim an assignee
already holds (``already_holding``) so a timed-out retry does not claim a second
issue (MCP F4). With one shared actor across sessions (the default since the
SessionStart hook carries a fixed ``--actor``), that also handed session 2 the
issue session 1 is working on. Ruling: without a matching ``client_request_id``
the fallback applies only when the held claim was made within the last 60 s,
measured from the ``claimed`` event's ``created_at``; a matching
``client_request_id`` always replays; outside the window the call claims the
next ready issue (the pre-3.4 behaviour).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

import filigree.db_issues as db_issues
from filigree.core import FiligreeDB


def _advance_clock(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Move the claim-path clock ``seconds`` past real time (the event rows keep real time)."""
    monkeypatch.setattr(db_issues, "_now_iso", lambda: (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat())


def _start_next(db: FiligreeDB, **kwargs: object):  # type: ignore[no-untyped-def]
    return db.start_next_work(assignee="claude-filigree", **kwargs)  # type: ignore[arg-type]


def _claim_next(db: FiligreeDB, **kwargs: object):  # type: ignore[no-untyped-def]
    return db.claim_next("claude-filigree", **kwargs)  # type: ignore[arg-type]


CALLS: list[tuple[str, Callable[..., object]]] = [("start_next_work", _start_next), ("claim_next", _claim_next)]


@pytest.fixture
def two_ready(db: FiligreeDB) -> FiligreeDB:
    db.create_issue("first", type="task", priority=0)
    db.create_issue("second", type="task", priority=1)
    return db


@pytest.mark.parametrize(("name", "call"), CALLS)
def test_second_call_within_window_returns_the_held_claim(two_ready: FiligreeDB, name: str, call: Callable[..., object]) -> None:
    first = call(two_ready)

    second = call(two_ready)

    assert second.id == first.id  # type: ignore[attr-defined]
    assert second.already_holding is True  # type: ignore[attr-defined]


@pytest.mark.parametrize(("name", "call"), CALLS)
def test_second_call_after_window_claims_a_different_issue(
    two_ready: FiligreeDB, monkeypatch: pytest.MonkeyPatch, name: str, call: Callable[..., object]
) -> None:
    first = call(two_ready)
    _advance_clock(monkeypatch, 61)

    second = call(two_ready)

    assert second is not None
    assert second.id != first.id  # type: ignore[attr-defined]
    assert second.already_holding is False  # type: ignore[attr-defined]
    held = {r["id"] for r in two_ready.conn.execute("SELECT id FROM issues WHERE assignee = 'claude-filigree'").fetchall()}
    assert held == {first.id, second.id}  # type: ignore[attr-defined]


@pytest.mark.parametrize(("name", "call"), CALLS)
def test_matching_client_request_id_replays_after_window(
    two_ready: FiligreeDB, monkeypatch: pytest.MonkeyPatch, name: str, call: Callable[..., object]
) -> None:
    first = call(two_ready, client_request_id="req-1")
    _advance_clock(monkeypatch, 3600)

    second = call(two_ready, client_request_id="req-1")

    assert second.id == first.id  # type: ignore[attr-defined]
    assert second.already_holding is True  # type: ignore[attr-defined]


@pytest.mark.parametrize(("name", "call"), CALLS)
def test_non_matching_client_request_id_after_window_claims_next(
    two_ready: FiligreeDB, monkeypatch: pytest.MonkeyPatch, name: str, call: Callable[..., object]
) -> None:
    first = call(two_ready, client_request_id="req-1")
    _advance_clock(monkeypatch, 61)

    second = call(two_ready, client_request_id="req-2")

    assert second.id != first.id  # type: ignore[attr-defined]
    assert second.already_holding is False  # type: ignore[attr-defined]
