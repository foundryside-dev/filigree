"""Tests for atomic compose operations (Phase D6).

Covers ``FiligreeDB.start_work`` and ``FiligreeDB.start_next_work`` plus
``TypeTemplate.canonical_working_status``. Atomicity is implemented via
compensating actions (release_claim on transition failure); these tests
verify both the happy path and the rollback contract.
"""

from __future__ import annotations

import pytest

from filigree.core import FiligreeDB
from filigree.templates import StateDefinition, TransitionDefinition, TypeTemplate
from filigree.types.api import AmbiguousTransitionError, InvalidTransitionError


class TestCanonicalWorkingStatus:
    def test_unique_wip_returns_it(self, db: FiligreeDB) -> None:
        """The default 'task' type has a single wip status (in_progress)."""
        tpl = db.templates.get_type("task")
        assert tpl is not None
        assert tpl.canonical_working_status() == "in_progress"

    def test_no_wip_raises_invalid(self, db: FiligreeDB) -> None:
        """A type with zero wip-category statuses raises InvalidTransitionError."""
        from filigree.templates import StateDefinition, TypeTemplate

        tpl = TypeTemplate(
            type="terminal_only",
            display_name="Terminal-Only",
            description="",
            pack="test",
            states=(StateDefinition(name="open", category="open"), StateDefinition(name="closed", category="done")),
            initial_state="open",
            transitions=(),
            fields_schema=(),
        )
        with pytest.raises(InvalidTransitionError):
            tpl.canonical_working_status()

    def test_multiple_wip_raises_ambiguous(self, db: FiligreeDB) -> None:
        """A type with multiple wip-category statuses raises AmbiguousTransitionError
        carrying the candidate list."""
        from filigree.templates import StateDefinition, TypeTemplate

        tpl = TypeTemplate(
            type="multi_wip",
            display_name="Multi-Wip",
            description="",
            pack="test",
            states=(
                StateDefinition(name="open", category="open"),
                StateDefinition(name="in_progress", category="wip"),
                StateDefinition(name="in_review", category="wip"),
                StateDefinition(name="closed", category="done"),
            ),
            initial_state="open",
            transitions=(),
            fields_schema=(),
        )
        with pytest.raises(AmbiguousTransitionError) as excinfo:
            tpl.canonical_working_status()
        assert set(excinfo.value.candidates) == {"in_progress", "in_review"}


class TestStartWork:
    def test_atomic_default_target(self, db: FiligreeDB) -> None:
        """start_work with no target_status uses the type's canonical wip status."""
        issue = db.create_issue("d6-default-target", type="task")
        result = db.start_work(issue.id, assignee="alice", actor="alice")
        assert result.assignee == "alice"
        assert result.status == "in_progress"

    def test_explicit_target_status(self, db: FiligreeDB) -> None:
        """target_status overrides the canonical default."""
        issue = db.create_issue("d6-explicit-target", type="task")
        result = db.start_work(issue.id, assignee="alice", target_status="in_progress", actor="alice")
        assert result.status == "in_progress"

    def test_confirmed_bug_defaults_to_reachable_fixing_status(self, db: FiligreeDB) -> None:
        """Bug has two wip states, but confirmed can only enter fixing."""
        issue = db.create_issue("d6-confirmed-bug", type="bug", fields={"severity": "major"})
        db.update_issue(issue.id, status="confirmed")

        result = db.start_work(issue.id, assignee="alice", actor="alice")

        assert result.assignee == "alice"
        assert result.status == "fixing"

    def test_fresh_bug_default_reports_no_reachable_wip_status(self, db: FiligreeDB) -> None:
        """A fresh triage bug has no direct wip transition, so the error should be state-specific."""
        issue = db.create_issue("d6-fresh-bug", type="bug")

        with pytest.raises(InvalidTransitionError, match="triage"):
            db.start_work(issue.id, assignee="alice", actor="alice")

        after = db.get_issue(issue.id)
        assert after.assignee == ""
        assert after.status == "triage"

    def test_default_target_ignores_unreachable_wip_statuses(self, db: FiligreeDB) -> None:
        """Only reachable wip targets from the current state participate in default selection."""
        tpl = TypeTemplate(
            type="single_reachable_wip",
            display_name="Single Reachable Wip",
            description="",
            pack="test",
            states=(
                StateDefinition(name="open", category="open"),
                StateDefinition(name="review", category="wip"),
                StateDefinition(name="revise", category="wip"),
                StateDefinition(name="closed", category="done"),
            ),
            initial_state="open",
            transitions=(
                TransitionDefinition(from_state="open", to_state="review", enforcement="soft"),
                TransitionDefinition(from_state="review", to_state="revise", enforcement="soft"),
                TransitionDefinition(from_state="review", to_state="closed", enforcement="soft"),
                TransitionDefinition(from_state="revise", to_state="closed", enforcement="soft"),
            ),
            fields_schema=(),
        )
        db.templates._register_type(tpl)
        issue = db.create_issue("d6-single-reachable", type="single_reachable_wip")

        result = db.start_work(issue.id, assignee="alice", actor="alice")

        assert result.status == "review"

    def test_default_target_still_rejects_multiple_reachable_wip_statuses(self, db: FiligreeDB) -> None:
        """When the current state can enter multiple wip states, callers must choose."""
        tpl = TypeTemplate(
            type="multiple_reachable_wip",
            display_name="Multiple Reachable Wip",
            description="",
            pack="test",
            states=(
                StateDefinition(name="open", category="open"),
                StateDefinition(name="review", category="wip"),
                StateDefinition(name="revise", category="wip"),
                StateDefinition(name="closed", category="done"),
            ),
            initial_state="open",
            transitions=(
                TransitionDefinition(from_state="open", to_state="review", enforcement="soft"),
                TransitionDefinition(from_state="open", to_state="revise", enforcement="soft"),
                TransitionDefinition(from_state="review", to_state="closed", enforcement="soft"),
                TransitionDefinition(from_state="revise", to_state="closed", enforcement="soft"),
            ),
            fields_schema=(),
        )
        db.templates._register_type(tpl)
        issue = db.create_issue("d6-multiple-reachable", type="multiple_reachable_wip")

        with pytest.raises(AmbiguousTransitionError) as excinfo:
            db.start_work(issue.id, assignee="alice", actor="alice")

        assert set(excinfo.value.candidates) == {"review", "revise"}

    def test_actor_defaults_to_assignee(self, db: FiligreeDB) -> None:
        """When actor is omitted, the assignee is used for the audit trail."""
        issue = db.create_issue("d6-actor-default", type="task")
        db.start_work(issue.id, assignee="bob")
        # The claim event should record actor="bob" since it defaulted from assignee.
        events = db.get_issue_events(issue.id, limit=10)
        claim_events = [e for e in events if e["event_type"] == "claimed"]
        assert claim_events
        assert claim_events[0]["actor"] == "bob"

    def test_rolls_back_on_invalid_transition(self, db: FiligreeDB) -> None:
        """If the transition fails (e.g. an unknown target status), the claim is
        released so assignee and status return to their prior values."""
        issue = db.create_issue("d6-rollback", type="task")
        original_status = issue.status
        with pytest.raises(ValueError, match=r"status|transition"):
            db.start_work(issue.id, assignee="alice", target_status="nonexistent_status", actor="alice")
        after = db.get_issue(issue.id)
        assert after.assignee == "", f"claim should have rolled back; got assignee={after.assignee!r}"
        assert after.status == original_status, (
            f"transition should have rolled back; got status={after.status!r}, expected {original_status!r}"
        )

    def test_unknown_issue_raises_keyerror(self, db: FiligreeDB) -> None:
        """An unknown issue surfaces a KeyError from claim_issue (no rollback needed)."""
        with pytest.raises(KeyError):
            db.start_work("test-deadbeef00", assignee="alice", actor="alice")


class TestStartNextWork:
    def test_picks_highest_priority(self, db: FiligreeDB) -> None:
        """start_next_work picks the highest-priority ready issue and transitions it."""
        db.create_issue("d6-low", type="task", priority=4)
        high = db.create_issue("d6-high", type="task", priority=0)
        result = db.start_next_work(assignee="carol")
        assert result is not None
        assert result.id == high.id
        assert result.assignee == "carol"
        assert result.status == "in_progress"

    def test_returns_none_when_no_match(self, db: FiligreeDB) -> None:
        """Returns None when no ready issue matches the filters."""
        db.create_issue("d6-task", type="task", priority=4)
        # No 'bug' types exist in default pack ready set
        result = db.start_next_work(assignee="dan", type_filter="nonexistent_type")
        assert result is None

    def test_priority_filter(self, db: FiligreeDB) -> None:
        """priority_max filters out low-priority candidates."""
        db.create_issue("d6-low", type="task", priority=4)
        med = db.create_issue("d6-med", type="task", priority=2)
        result = db.start_next_work(assignee="erin", priority_max=2)
        assert result is not None
        assert result.id == med.id
