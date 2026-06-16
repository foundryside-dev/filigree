"""TDD spec for the ``TransitionMode`` enum that replaces the internal ``backward`` bool.

filigree-9b4bb6e52e — the transition direction was a bare ``backward: bool``
threaded through ``validate_transition`` / ``update_issue`` /
``DBMixinProtocol`` / ``InvalidTransitionError``. It is internal Python API (no
MCP/CLI/wire exposure), so 3.0.0 replaces it outright with a self-documenting
enum instead of carrying a compatibility alias.
"""

from __future__ import annotations

from pathlib import Path

from filigree.types.api import InvalidTransitionError, TransitionMode
from tests._db_factory import make_db


def test_transition_mode_enum_values() -> None:
    assert TransitionMode.FORWARD.value == "forward"
    assert TransitionMode.BACKWARD.value == "backward"


def test_invalid_transition_error_carries_mode_backward() -> None:
    exc = InvalidTransitionError("bug", "wont_fix", to_state="triage", mode=TransitionMode.BACKWARD)
    assert exc.mode is TransitionMode.BACKWARD
    # The reverse-lane message branch must fire on the enum, not a bool.
    assert "Reverse transition" in str(exc)
    enriched = exc.with_valid_transitions([{"to": "triage", "category": "open", "ready": True}])
    assert enriched is not exc
    assert enriched.mode is TransitionMode.BACKWARD
    assert str(enriched) == str(exc)


def test_invalid_transition_error_defaults_to_forward() -> None:
    exc = InvalidTransitionError("task", "open", to_state="closed")
    assert exc.mode is TransitionMode.FORWARD
    assert "Reverse" not in str(exc)


def test_update_issue_accepts_transition_mode_backward(tmp_path: Path) -> None:
    db = make_db(tmp_path, packs=["core", "planning"])
    try:
        issue = db.create_issue("Bug", type="bug")
        db.close_issue(issue.id, status="wont_fix")
        # Reverse/escape lane: wont_fix (done) -> triage, selected via the enum
        # rather than a bare True.
        db.update_issue(issue.id, status="triage", mode=TransitionMode.BACKWARD, actor="ops")
        assert db.get_issue(issue.id).status == "triage"
        # The reverse lane still records the transition_forced audit event.
        events = db.get_issue_events(issue.id, limit=20)
        assert any(e["event_type"] == "transition_forced" for e in events)
    finally:
        db.close()


def test_validate_transition_mode_routes_reverse_lane(tmp_path: Path) -> None:
    db = make_db(tmp_path, packs=["core", "planning"])
    try:
        # Same hop is allowed only on the reverse lane; the default (forward)
        # lane rejects it. This pins both that the kwarg is accepted and that
        # the default is FORWARD, not a truthy enum member.
        rev = db.templates.validate_transition("bug", "wont_fix", "triage", {}, mode=TransitionMode.BACKWARD)
        assert rev.allowed
        fwd = db.templates.validate_transition("bug", "wont_fix", "triage", {})
        assert not fwd.allowed
    finally:
        db.close()
