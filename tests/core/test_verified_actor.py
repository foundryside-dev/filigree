"""Tests for transport-bound verified-actor plumbing (ADR-012, schema v24)."""

from __future__ import annotations

from filigree.core import FiligreeDB


def test_constructor_defaults_verified_actor_to_none(db: FiligreeDB) -> None:
    assert db._verified_actor is None


def test_set_verified_actor_updates_field(db: FiligreeDB) -> None:
    db.set_verified_actor("alice")
    assert db._verified_actor == "alice"
    db.set_verified_actor(None)
    assert db._verified_actor is None


def test_borrow_for_worker_thread_propagates_verified_actor(db: FiligreeDB) -> None:
    db.set_verified_actor("alice")
    with db.borrow_for_worker_thread() as clone:
        assert clone._verified_actor == "alice"
