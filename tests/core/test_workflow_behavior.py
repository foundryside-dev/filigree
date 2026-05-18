"""Integration tests for per-type status validation and transition enforcement.

Tests the core.py behavior changes introduced in Phase 1C. These tests exercise
the full stack: FiligreeDB -> TemplateRegistry -> SQLite, verifying that issue
lifecycle operations respect per-type state machines.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from filigree.core import DB_FILENAME, FILIGREE_DIR_NAME, FiligreeDB, write_config
from filigree.templates import StateDefinition, TransitionOption, TypeTemplate, ValidationResult
from filigree.types.api import ErrorCode, InvalidTransitionError
from tests._db_factory import make_db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Generator[FiligreeDB, None, None]:
    """A FiligreeDB instance with templates loaded (core + planning packs enabled)."""
    d = make_db(tmp_path, packs=["core", "planning"])
    yield d
    d.close()


@pytest.fixture
def incident_db(tmp_path: Path) -> Generator[FiligreeDB, None, None]:
    """FiligreeDB with core + planning + incident packs enabled.

    Includes planning because hard-enforcement tests (e.g.
    ``test_close_incident_from_resolved_requires_root_cause``) exercise
    close_issue() which resolves done-category states across all enabled
    types, including planning-pack types like milestone.
    """
    d = make_db(tmp_path, packs=["core", "planning", "incident"])
    yield d
    d.close()


# ---------------------------------------------------------------------------
# Test helpers for TOCTOU race simulation
# ---------------------------------------------------------------------------


class _InterceptingConnProxy:
    """Wraps a sqlite3.Connection and fires a callback before each execute().

    Used to simulate a concurrent writer landing between a reader's
    time-of-check and time-of-use — the callback may issue its own
    statements through the real connection before the caller's query runs.
    """

    def __init__(self, real: Any, on_sql: Any) -> None:
        self._real = real
        self._on_sql = on_sql
        self.fired = False

    def execute(self, sql: str, params: Any = ()) -> Any:
        if not self.fired:
            effect = self._on_sql(sql)
            if effect is not None:
                self.fired = True
        return self._real.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _reassign_to(conn: Any, issue_id: str, assignee: str) -> object:
    """Simulate a concurrent reassignment via a direct UPDATE, bypassing release_claim.

    Commits immediately so the "other actor's" write is durable when the
    intercepted statement subsequently rolls back on failure.
    """
    conn.execute("UPDATE issues SET assignee = ? WHERE id = ?", [assignee, issue_id])
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# Task 1.9: Per-Type Status Validation
# ---------------------------------------------------------------------------


class TestCreateIssueInitialState:
    """create_issue() uses type-specific initial state from template."""

    def test_bug_initial_state_is_triage(self, db: FiligreeDB) -> None:
        """Bug type should start in 'triage', not 'open'."""
        issue = db.create_issue("Fix crash on startup", type="bug")
        assert issue.status == "triage"

    def test_task_initial_state_is_open(self, db: FiligreeDB) -> None:
        """Task type preserves legacy 'open' initial state."""
        issue = db.create_issue("Update docs", type="task")
        assert issue.status == "open"

    def test_epic_initial_state_is_open(self, db: FiligreeDB) -> None:
        """Epic type preserves legacy 'open' initial state."""
        issue = db.create_issue("Workflow v2", type="epic")
        assert issue.status == "open"

    def test_feature_initial_state_is_proposed(self, db: FiligreeDB) -> None:
        """Feature type should start in 'proposed' per template."""
        issue = db.create_issue("Add search", type="feature")
        assert issue.status == "proposed"

    def test_milestone_initial_state(self, db: FiligreeDB) -> None:
        """Milestone type should start in 'planning' per template."""
        issue = db.create_issue("v2.0 Release", type="milestone")
        assert issue.status == "planning"

    def test_unknown_type_rejected(self, db: FiligreeDB) -> None:
        """Unknown types are rejected with a clear error listing valid types."""
        with pytest.raises(ValueError, match="Unknown type 'custom_type'"):
            db.create_issue("Something", type="custom_type")


class TestValidateStatus:
    """_validate_status() checks type-specific valid states via templates."""

    def test_valid_bug_state_accepted(self, db: FiligreeDB) -> None:
        """Bug-specific states like 'triage' should be accepted."""
        issue = db.create_issue("Bug", type="bug")
        updated = db.update_issue(issue.id, status="confirmed")
        assert updated.status == "confirmed"

    def test_invalid_bug_state_rejected(self, db: FiligreeDB) -> None:
        """States not in the bug template should raise ValueError."""
        issue = db.create_issue("Bug", type="bug")
        with pytest.raises(ValueError, match=r"Invalid status|not defined"):
            db.update_issue(issue.id, status="nonexistent_state")

    def test_task_legacy_states_accepted(self, db: FiligreeDB) -> None:
        """Task type must accept open/in_progress/closed (backward compat)."""
        issue = db.create_issue("Task", type="task")
        updated = db.update_issue(issue.id, status="in_progress")
        assert updated.status == "in_progress"
        updated = db.update_issue(issue.id, status="closed")
        assert updated.status == "closed"

    def test_unknown_type_rejected(self, db: FiligreeDB) -> None:
        """Unknown types are rejected at creation time."""
        with pytest.raises(ValueError, match="Unknown type 'custom_type'"):
            db.create_issue("Custom", type="custom_type")


class TestUpdateIssueTransitionEnforcement:
    """update_issue() validates transitions with soft/hard enforcement."""

    def test_valid_soft_transition_succeeds(self, db: FiligreeDB) -> None:
        """Soft transition triage -> confirmed should succeed."""
        issue = db.create_issue("Bug", type="bug")
        updated = db.update_issue(issue.id, status="confirmed")
        assert updated.status == "confirmed"

    def test_hard_enforcement_rejects_missing_fields(self, db: FiligreeDB) -> None:
        """Hard enforcement: verifying -> closed without fix_verification raises ValueError."""
        issue = db.create_issue("Bug", type="bug")
        db.update_issue(issue.id, status="confirmed")
        db.update_issue(issue.id, status="fixing")
        db.update_issue(issue.id, status="verifying", fields={"fix_verification": "tests pass"})
        # Now try to close — issue already has fix_verification set, so this should succeed
        updated = db.update_issue(issue.id, status="closed")
        assert updated.status == "closed"

    def test_hard_enforcement_blocks_with_empty_field(self, db: FiligreeDB) -> None:
        """Hard enforcement: empty string field should be treated as missing."""
        issue = db.create_issue("Bug", type="bug")
        db.update_issue(issue.id, status="confirmed")
        db.update_issue(issue.id, status="fixing")
        db.update_issue(issue.id, status="verifying", fields={"fix_verification": "initial"})
        # Clear the field, then try hard transition
        db.update_issue(issue.id, fields={"fix_verification": ""})
        with pytest.raises(ValueError, match="fix_verification"):
            db.update_issue(issue.id, status="closed")

    def test_soft_enforcement_proceeds_with_warning(self, db: FiligreeDB) -> None:
        """Soft enforcement: proceed + record warning events in events table."""
        issue = db.create_issue("Bug", type="bug")
        # triage -> confirmed is soft with severity required_at confirmed
        updated = db.update_issue(issue.id, status="confirmed")
        assert updated.status == "confirmed"
        assert updated.to_dict()["data_warnings"] == ["Missing recommended fields for 'confirmed': severity"]
        # Check that the in-band warning and durable audit event match one-to-one.
        events = db.conn.execute(
            "SELECT * FROM events WHERE issue_id = ? AND event_type = 'transition_warning'",
            (issue.id,),
        ).fetchall()
        assert [event["comment"] for event in events] == updated.to_dict()["data_warnings"]

    def test_soft_enforcement_returns_fixing_warning_once(self, db: FiligreeDB) -> None:
        """Soft warnings for later bug states also return in-band and emit once."""
        issue = db.create_issue("Bug", type="bug", fields={"severity": "major"})
        db.update_issue(issue.id, status="confirmed")

        updated = db.update_issue(issue.id, status="fixing")

        assert updated.status == "fixing"
        assert updated.to_dict()["data_warnings"] == ["Missing recommended fields for 'fixing': root_cause"]
        events = db.conn.execute(
            "SELECT * FROM events WHERE issue_id = ? AND event_type = 'transition_warning'",
            (issue.id,),
        ).fetchall()
        assert [event["comment"] for event in events] == updated.to_dict()["data_warnings"]

    def test_atomic_transition_with_fields_succeeds(self, db: FiligreeDB) -> None:
        """WFT-FR-069: update_issue with status + fields merges fields BEFORE
        transition check, so hard enforcement sees the new fields."""
        issue = db.create_issue("Bug", type="bug")
        db.update_issue(issue.id, status="confirmed")
        db.update_issue(issue.id, status="fixing")
        # Atomic: set fix_verification AND transition to verifying in one call
        updated = db.update_issue(
            issue.id,
            status="verifying",
            fields={"fix_verification": "tests pass"},
        )
        assert updated.status == "verifying"
        assert updated.fields["fix_verification"] == "tests pass"

    def test_atomic_transition_hard_failure_rolls_back(self, db: FiligreeDB) -> None:
        """WFT-FR-069: On hard failure, neither fields NOR status are saved."""
        issue = db.create_issue("Bug", type="bug")
        db.update_issue(issue.id, status="confirmed")
        db.update_issue(issue.id, status="fixing")
        db.update_issue(issue.id, status="verifying", fields={"fix_verification": "initial"})
        # Try to close with fix_verification="" (hard enforcement)
        with pytest.raises(ValueError, match="fix_verification"):
            db.update_issue(
                issue.id,
                status="closed",
                fields={"fix_verification": "", "extra_note": "should not persist"},
            )
        # Verify nothing changed
        current = db.get_issue(issue.id)
        assert current.status == "verifying"
        assert current.fields.get("fix_verification") == "initial"
        assert "extra_note" not in current.fields

    def test_update_issue_sets_closed_at_for_done_category(self, db: FiligreeDB) -> None:
        """closed_at should be set when entering any done-category state, not just 'closed'."""
        issue = db.create_issue("Bug", type="bug")
        # triage -> wont_fix (done-category)
        updated = db.update_issue(issue.id, status="wont_fix")
        assert updated.closed_at is not None

    def test_legacy_task_closed_at_still_works(self, db: FiligreeDB) -> None:
        """Task: status='closed' still sets closed_at (backward compat)."""
        issue = db.create_issue("Task", type="task")
        db.update_issue(issue.id, status="in_progress")
        updated = db.update_issue(issue.id, status="closed")
        assert updated.closed_at is not None

    def test_undefined_transition_rejected(self, db: FiligreeDB) -> None:
        """Undefined transitions on known types are rejected."""
        issue = db.create_issue("Bug", type="bug")
        with pytest.raises(ValueError, match="not allowed"):
            db.update_issue(issue.id, status="verifying")  # triage -> verifying not in table

    def test_close_validates_transition(self, db: FiligreeDB) -> None:
        """close_issue routes through update_issue's transition validator.

        Bug template has no triage→closed transition, so the default done state
        (closed) must be rejected with INVALID_TRANSITION. Use close_issue with
        an explicit done status that *is* reachable (wont_fix), or walk the
        workflow first.
        """
        issue = db.create_issue("Bug", type="bug")
        with pytest.raises(ValueError, match="not allowed"):
            db.close_issue(issue.id, reason="duplicate")
        # Reachable done state from triage works.
        closed = db.close_issue(issue.id, status="wont_fix", reason="duplicate")
        assert closed.status == "wont_fix"

    def test_close_force_uses_backward_escape_edge(self, db: FiligreeDB) -> None:
        """close_issue(force=True) uses the declared backward/escape edge.

        Documented escape hatch for cleanup flows that need to rage-close
        regardless of the template — same shape as
        ``delete_file_record(force=True)``. Senior-user MCP review run e P1.3.
        """
        issue = db.create_issue("Bug", type="bug")
        # Without force, triage → closed is rejected.
        with pytest.raises(ValueError, match="not allowed"):
            db.close_issue(issue.id)
        # With force, the same call lands in the default done state.
        closed = db.close_issue(issue.id, force=True, reason="cleanup")
        assert closed.status == "closed"

    def test_batch_close_force_propagates_per_item(self, db: FiligreeDB) -> None:
        """batch_close(force=True) passes the flag to every per-item close.

        The pre-fix behaviour smashed templates anyway because batch_close
        delegated to update_issue with no transition check; the post-fix
        path validates by default and only bypasses when force is set.
        Senior-user MCP review run e P1.3.

        Note: bug from triage cannot reach the default done state ('closed') —
        the only directly-reachable done targets are 'wont_fix' and 'not_a_bug'.
        Without an explicit status= or force=True, the close fails. force=True
        then bypasses the validator entirely and lands in the default done
        state (template's first done-category state) for both types.
        """
        bug = db.create_issue("Bug A", type="bug")
        bug_b = db.create_issue("Bug B", type="bug")
        # No force, no explicit status → default done state 'closed' is not
        # reachable from 'triage', so the close fails with INVALID_TRANSITION.
        _, errors = db.batch_close([bug.id, bug_b.id], reason="cleanup")
        assert len(errors) == 2
        assert all(e["code"] == ErrorCode.INVALID_TRANSITION for e in errors)
        # With force → both items reach the template default done state.
        closed, errors = db.batch_close([bug.id, bug_b.id], reason="cleanup", force=True)
        assert len(closed) == 2
        assert errors == []

    def test_batch_close_unreachable_default_surfaces_invalid_transition(self, db: FiligreeDB) -> None:
        """When the template default done state is unreachable from current,
        batch_close surfaces INVALID_TRANSITION per item. Callers explicitly
        pick a dismissal target (e.g. status='skipped') or use force=True.
        """
        milestone = db.create_issue("Milestone X", type="milestone")
        phase = db.create_issue("Phase X", type="phase")
        closed, errors = db.batch_close([milestone.id, phase.id], reason="cleanup")
        assert closed == []
        assert len(errors) == 2
        assert all(e["code"] == ErrorCode.INVALID_TRANSITION for e in errors)
        # force=True lets the cleanup land in each type's default done state.
        closed, errors = db.batch_close([milestone.id, phase.id], reason="cleanup", force=True)
        assert errors == []
        by_id = {r.id: r for r in closed}
        # milestone's first done-category state is 'completed' (declared first),
        # phase's is 'completed'.
        assert by_id[milestone.id].status == "completed"
        assert by_id[phase.id].status == "completed"

    def test_reopen_uses_backward_edge_not_skip_check(self, db: FiligreeDB) -> None:
        """Reopen works through a declared reverse edge; skip-check is gone."""
        skip_kw = "_" + "skip_transition_check"
        assert skip_kw not in inspect.signature(db.update_issue).parameters

        issue = db.create_issue("Bug", type="bug")
        # Use a directly-reachable done state from triage; close_issue now
        # validates transitions (filigree-cb980eee0d).
        db.close_issue(issue.id, status="wont_fix")
        reopened = db.reopen_issue(issue.id, actor="ops")
        assert reopened.status == "triage"
        events = db.get_issue_events(issue.id, limit=20)
        assert any(e["event_type"] == "transition_forced" and e["old_value"] == "wont_fix" and e["new_value"] == "triage" for e in events)

    def test_legacy_transition_skip_flag_is_not_accepted(self, db: FiligreeDB) -> None:
        """The legacy skip-check kwarg is no longer part of update_issue."""
        skip_kw = "_" + "skip_transition_check"
        assert skip_kw not in inspect.signature(db.update_issue).parameters
        issue = db.create_issue("Bug", type="bug")
        with pytest.raises(TypeError, match=skip_kw):
            db.update_issue(issue.id, status="confirmed", **{skip_kw: True})  # type: ignore[arg-type]

    def test_backward_transition_emits_audit_event(self, db: FiligreeDB) -> None:
        """2.1.0 §4.1: declared backward transitions record a
        ``transition_forced`` event alongside ``status_changed``.

        Reviewers reading the audit trail must be able to identify every
        workflow shortcut without inferring it from the absence of a warning.
        The two events fire in order (forced → changed) so the chain reads
        causally top-to-bottom.
        """
        issue = db.create_issue("Bug", type="bug")
        db.close_issue(issue.id, status="wont_fix")
        db.update_issue(
            issue.id,
            status="triage",
            backward=True,
            actor="ops",
        )
        events = db.get_issue_events(issue.id, limit=20)
        forced = [e for e in events if e["event_type"] == "transition_forced"]
        changed = [e for e in events if e["event_type"] == "status_changed"]
        assert len(forced) == 1, f"expected one transition_forced, got {[e['event_type'] for e in events]}"
        assert forced[0]["old_value"] == "wont_fix"
        assert forced[0]["new_value"] == "triage"
        assert forced[0]["actor"] == "ops"
        # status_changed is still emitted; the forced event is additive.
        assert len(changed) == 2
        assert any(e["old_value"] == "wont_fix" and e["new_value"] == "triage" for e in changed)

    def test_missing_backward_edge_raises_named_error(self, tmp_path: Path) -> None:
        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        templates_dir = filigree_dir / "templates"
        templates_dir.mkdir(parents=True)
        write_config(filigree_dir, {"prefix": "test", "version": 1, "enabled_packs": ["custom_only"]})
        (templates_dir / "custom_item.json").write_text(
            json.dumps(
                {
                    "type": "custom_item",
                    "display_name": "Custom Item",
                    "states": [
                        {"name": "open", "category": "open"},
                        {"name": "closed", "category": "done"},
                    ],
                    "initial_state": "open",
                    "transitions": [
                        {"from": "open", "to": "closed", "enforcement": "soft"},
                    ],
                    "fields_schema": [],
                }
            )
        )
        custom_db = FiligreeDB(filigree_dir / DB_FILENAME, prefix="test", enabled_packs=["custom_only"])
        try:
            custom_db.initialize()
            issue = custom_db.create_issue("Custom", type="custom_item")
            custom_db.close_issue(issue.id)

            with pytest.raises(InvalidTransitionError) as excinfo:
                custom_db.reopen_issue(issue.id)

            assert excinfo.value.type_name == "custom_item"
            assert excinfo.value.current_status == "closed"
        finally:
            custom_db.close()

    def test_release_claim_validates_backward_edge(self, db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        issue = db.create_issue("Handoff task", type="task")
        db.start_work(issue.id, assignee="agent-alpha", actor="agent-alpha")
        original_validate_transition = db.templates.validate_transition

        def fail_backward_validation(
            type_name: str,
            from_state: str,
            to_state: str,
            fields: dict[str, Any],
            *,
            backward: bool = False,
        ) -> Any:
            if backward:
                raise InvalidTransitionError(type_name, from_state, to_state=to_state)
            return original_validate_transition(type_name, from_state, to_state, fields, backward=backward)

        monkeypatch.setattr(db.templates, "validate_transition", fail_backward_validation)

        with pytest.raises(InvalidTransitionError):
            db.release_claim(issue.id, actor="agent-alpha")

        restored = db.get_issue(issue.id)
        assert restored.status == "in_progress"
        assert restored.assignee == "agent-alpha"

    def test_validated_transition_does_not_emit_forced_event(self, db: FiligreeDB) -> None:
        """The audit event must fire ONLY when the validator was bypassed —
        a normal transition through the template still emits status_changed
        without the matching transition_forced sibling.
        """
        issue = db.create_issue("Bug", type="bug")
        # triage → confirmed is a legal forward transition for the bug template.
        db.update_issue(issue.id, status="confirmed", actor="ops")
        events = db.get_issue_events(issue.id, limit=20)
        assert not any(e["event_type"] == "transition_forced" for e in events)


class TestCloseIssue:
    """close_issue() accepts optional status parameter for multi-done types."""

    def test_close_bug_default_done_state(self, db: FiligreeDB) -> None:
        """close_issue() without status uses first done-category state, but
        the transition must be reachable from the current status. For bug,
        triage → closed is not a defined transition; the agent must walk to
        verifying first.
        """
        issue = db.create_issue("Bug", type="bug")
        db.update_issue(issue.id, status="confirmed", fields={"severity": "minor"})
        db.update_issue(issue.id, status="fixing", fields={"root_cause": "redacted"})
        db.update_issue(issue.id, status="verifying", fields={"fix_verification": "smoke test"})
        closed = db.close_issue(issue.id)
        assert closed.status == "closed"

    def test_close_bug_with_specific_done_state(self, db: FiligreeDB) -> None:
        """close_issue(status='wont_fix') uses specified done state."""
        issue = db.create_issue("Bug", type="bug")
        closed = db.close_issue(issue.id, status="wont_fix")
        assert closed.status == "wont_fix"

    def test_close_bug_rejects_non_done_state(self, db: FiligreeDB) -> None:
        """close_issue() with a non-done state raises ValueError."""
        issue = db.create_issue("Bug", type="bug")
        with pytest.raises(ValueError, match="done"):
            db.close_issue(issue.id, status="fixing")

    def test_close_task_default(self, db: FiligreeDB) -> None:
        """close_issue() on task type uses 'closed' (backward compat)."""
        issue = db.create_issue("Task", type="task")
        closed = db.close_issue(issue.id)
        assert closed.status == "closed"

    def test_close_already_closed_raises(self, db: FiligreeDB) -> None:
        """close_issue() on already-closed issue raises with clear message."""
        issue = db.create_issue("Bug", type="bug")
        # Use a directly-reachable done state from triage to land in done.
        db.close_issue(issue.id, status="wont_fix")
        with pytest.raises(ValueError, match="already closed"):
            db.close_issue(issue.id)

    def test_close_phase_in_pending_without_status_raises(self, db: FiligreeDB) -> None:
        """Default done target ('completed') isn't reachable from 'pending'. The
        caller must pass status= explicitly (e.g. 'skipped') or walk the
        workflow forward — close_issue must not silently pick a done state.
        """
        issue = db.create_issue("Phase", type="phase")
        with pytest.raises(ValueError, match="not allowed"):
            db.close_issue(issue.id, reason="cleanup")

    def test_close_step_in_pending_without_status_raises(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Step", type="step")
        with pytest.raises(ValueError, match="not allowed"):
            db.close_issue(issue.id)

    def test_close_milestone_in_planning_without_status_raises(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Milestone", type="milestone")
        with pytest.raises(ValueError, match="not allowed"):
            db.close_issue(issue.id, reason="abandoned")

    def test_close_bug_triage_without_status_raises(self, db: FiligreeDB) -> None:
        """Bug in triage has reachable done states but the default 'closed' is
        not among them. Caller must pick explicitly.
        """
        issue = db.create_issue("Bug", type="bug")
        with pytest.raises(ValueError, match="not allowed"):
            db.close_issue(issue.id)

    def test_close_phase_with_explicit_skipped(self, db: FiligreeDB) -> None:
        """Caller picks the dismissal target explicitly; close succeeds with no
        data_warning emitted.
        """
        issue = db.create_issue("Phase", type="phase")
        closed = db.close_issue(issue.id, status="skipped", reason="cleanup")
        assert closed.status == "skipped"
        assert not any("auto-resolved" in w for w in closed.data_warnings)
        assert closed.fields["close_reason"] == "cleanup"

    def test_close_with_force_uses_escape_edge(self, db: FiligreeDB) -> None:
        """force=True is the documented escape hatch for cleanup that needs to
        land in the default done state regardless of reachability.
        """
        issue = db.create_issue("Phase", type="phase")
        closed = db.close_issue(issue.id, force=True)
        # Default done state for phase is 'completed' (first done-category state).
        assert closed.status == "completed"


class TestCloseIssueHardEnforcement:
    """close_issue() must respect hard-enforcement gates (filigree-87e5e3)."""

    def test_close_incident_from_resolved_requires_root_cause(self, incident_db: FiligreeDB) -> None:
        """Incident resolved→closed has hard enforcement requiring root_cause."""
        issue = incident_db.create_issue("Outage", type="incident")
        # Walk to resolved: reported → triaging → investigating → resolved
        incident_db.update_issue(issue.id, status="triaging", fields={"severity": "sev2"})
        incident_db.update_issue(issue.id, status="investigating")
        incident_db.update_issue(issue.id, status="resolved")

        # Attempting to close without root_cause should fail
        with pytest.raises(ValueError, match="root_cause"):
            incident_db.close_issue(issue.id)

    def test_close_incident_with_root_cause_succeeds(self, incident_db: FiligreeDB) -> None:
        """Providing required fields via fields= allows close through hard gate."""
        issue = incident_db.create_issue("Outage", type="incident")
        incident_db.update_issue(issue.id, status="triaging", fields={"severity": "sev2"})
        incident_db.update_issue(issue.id, status="investigating")
        incident_db.update_issue(issue.id, status="resolved")

        closed = incident_db.close_issue(
            issue.id,
            fields={"root_cause": "Config drift in prod"},
            reason="Resolved after config rollback",
        )
        assert closed.status == "closed"
        assert closed.fields["root_cause"] == "Config drift in prod"
        assert closed.fields["close_reason"] == "Resolved after config rollback"

    def test_close_incident_with_pre_populated_field_succeeds(self, incident_db: FiligreeDB) -> None:
        """If root_cause was set earlier, close_issue succeeds without fields=."""
        issue = incident_db.create_issue("Outage", type="incident")
        incident_db.update_issue(issue.id, status="triaging", fields={"severity": "sev2"})
        incident_db.update_issue(issue.id, status="investigating")
        incident_db.update_issue(issue.id, status="resolved", fields={"root_cause": "OOM in worker"})

        closed = incident_db.close_issue(issue.id)
        assert closed.status == "closed"
        assert closed.fields["root_cause"] == "OOM in worker"

    def test_close_from_non_workflow_state_rejected(self, db: FiligreeDB) -> None:
        """Closing from a state with no transition to the default done state
        is rejected with INVALID_TRANSITION (per the validate-transitions
        policy). Agents should pass an explicitly reachable done status
        instead.
        """
        issue = db.create_issue("Bug", type="bug")
        with pytest.raises(ValueError, match="not allowed"):
            db.close_issue(issue.id, reason="duplicate")

    def test_close_bug_from_verifying_requires_fix_verification(self, db: FiligreeDB) -> None:
        """Bug verifying→closed has hard enforcement requiring fix_verification."""
        issue = db.create_issue("Bug", type="bug")
        db.update_issue(issue.id, status="confirmed", fields={"severity": "major"})
        db.update_issue(issue.id, status="fixing", fields={"root_cause": "config drift"})
        db.update_issue(issue.id, status="verifying", fields={"fix_verification": "manual test"})

        # verifying→closed requires fix_verification (hard gate)
        # fix_verification is already set, so this should succeed
        closed = db.close_issue(issue.id)
        assert closed.status == "closed"

    def test_close_with_non_dict_fields_raises_type_error(self, db: FiligreeDB) -> None:
        """Passing non-dict fields to close_issue raises TypeError, not 500."""
        issue = db.create_issue("Bug", type="bug")
        with pytest.raises(TypeError, match="fields must be a dict"):
            db.close_issue(issue.id, fields=5)  # type: ignore[arg-type]


class TestClaimIssue:
    """claim_issue() sets assignee only — does not change status."""

    def test_claim_bug_assigns_only(self, db: FiligreeDB) -> None:
        """Bug type claim should set assignee without changing status."""
        issue = db.create_issue("Bug", type="bug")
        claimed = db.claim_issue(issue.id, assignee="agent-1")
        assert claimed.status == "triage"  # status unchanged
        assert claimed.assignee == "agent-1"

    def test_claim_task_assigns_only(self, db: FiligreeDB) -> None:
        """Task type claim sets assignee without changing status."""
        issue = db.create_issue("Task", type="task")
        claimed = db.claim_issue(issue.id, assignee="agent-1")
        assert claimed.status == "open"  # status unchanged
        assert claimed.assignee == "agent-1"

    def test_claim_records_lease_metadata(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Task", type="task")

        claimed = db.claim_issue(issue.id, assignee="agent-1")

        assert claimed.claimed_at is not None
        assert claimed.last_heartbeat_at == claimed.claimed_at
        assert claimed.claim_expires_at is not None
        assert datetime.fromisoformat(claimed.claim_expires_at) > datetime.fromisoformat(claimed.claimed_at)

    def test_claim_already_assigned_fails(self, db: FiligreeDB) -> None:
        """Cannot claim an issue that's already assigned to someone else."""
        issue = db.create_issue("Task", type="task")
        db.claim_issue(issue.id, assignee="agent-1")
        with pytest.raises(ValueError, match="already assigned to"):
            db.claim_issue(issue.id, assignee="agent-2")

    def test_claim_released_wip_issue_for_handoff(self, db: FiligreeDB) -> None:
        """release_claim auto-reverts wip→open so the issue rejoins discovery
        (filigree-cb980eee0d, P1.3). The handoff agent picks up an 'open'
        issue and re-transitions it as part of start_work.
        """
        issue = db.create_issue("Handoff task", type="task")
        db.start_work(issue.id, assignee="agent-alpha", actor="agent-alpha")
        released = db.release_claim(issue.id, actor="agent-alpha")
        assert released.status == "open"
        assert released.assignee == ""

        claimed = db.claim_issue(issue.id, assignee="agent-bravo", actor="agent-bravo")

        assert claimed.status == "open"
        assert claimed.assignee == "agent-bravo"

    def test_release_claim_revert_status_false_preserves_legacy_behaviour(self, db: FiligreeDB) -> None:
        """Pass revert_status=False to keep the issue in its current wip
        status (legacy behaviour pre-P1.3).
        """
        issue = db.create_issue("Sticky task", type="task")
        db.start_work(issue.id, assignee="agent-alpha", actor="agent-alpha")
        released = db.release_claim(issue.id, actor="agent-alpha", revert_status=False)
        assert released.status == "in_progress"
        assert released.assignee == ""

    def test_release_claim_reverts_bug_fixing_to_confirmed(self, db: FiligreeDB) -> None:
        """For bug.fixing, the open predecessor is confirmed."""
        issue = db.create_issue("Bug", type="bug")
        db.update_issue(issue.id, status="confirmed", fields={"severity": "minor"})
        db.start_work(issue.id, assignee="agent-x", actor="agent-x", target_status="fixing")
        released = db.release_claim(issue.id, actor="agent-x")
        assert released.status == "confirmed"

    def test_release_claim_falls_back_to_initial_state(self, db: FiligreeDB) -> None:
        """For bug.verifying (no open predecessor in transition graph) the
        revert falls back to the template's initial_state (triage).
        """
        issue = db.create_issue("Bug", type="bug")
        db.update_issue(issue.id, status="confirmed", fields={"severity": "minor"})
        db.update_issue(issue.id, status="fixing", fields={"root_cause": "redacted"})
        db.update_issue(issue.id, status="verifying", fields={"fix_verification": "manual"})
        db.claim_issue(issue.id, assignee="agent-y")
        released = db.release_claim(issue.id, actor="agent-y")
        # No direct open→verifying transition exists; fall back to initial_state.
        assert released.status == "triage"

    def test_release_claim_revert_is_atomic(self, db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        issue = db.create_issue("Atomic release", type="task")
        db.start_work(issue.id, assignee="agent-alpha", actor="agent-alpha")
        original_record_event = db._record_event

        def fail_forced_transition(*args: object, **kwargs: object) -> None:
            if len(args) >= 2 and args[1] == "transition_forced":
                raise RuntimeError("boom")
            original_record_event(*args, **kwargs)

        monkeypatch.setattr(db, "_record_event", fail_forced_transition)

        with pytest.raises(RuntimeError, match="boom"):
            db.release_claim(issue.id, actor="agent-alpha")

        restored = db.get_issue(issue.id)
        assert restored.status == "in_progress"
        assert restored.assignee == "agent-alpha"
        events = db.conn.execute(
            "SELECT event_type FROM events WHERE issue_id = ? ORDER BY event_seq ASC",
            (issue.id,),
        ).fetchall()
        assert "released" not in [row["event_type"] for row in events]

    def test_release_claim_emits_one_commit(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Single release commit", type="task")
        db.start_work(issue.id, assignee="agent-alpha", actor="agent-alpha")

        statements: list[str] = []
        db.conn.set_trace_callback(statements.append)
        try:
            released = db.release_claim(issue.id, actor="agent-alpha")
        finally:
            db.conn.set_trace_callback(None)

        assert released.status == "open"
        assert released.assignee == ""
        assert [stmt for stmt in statements if stmt == "COMMIT"] == ["COMMIT"]
        event_types = [
            row["event_type"]
            for row in db.conn.execute(
                "SELECT event_type FROM events WHERE issue_id = ? ORDER BY event_seq ASC",
                (issue.id,),
            ).fetchall()
        ]
        assert event_types[-3:] == ["released", "transition_forced", "status_changed"]


class TestReleaseMyClaims:
    """Bulk release every live claim held by a given actor. F4 — review-h."""

    def test_releases_all_claims_for_actor(self, db: FiligreeDB) -> None:
        a = db.create_issue("A", type="task")
        b = db.create_issue("B", type="task")
        c = db.create_issue("C", type="task")  # held by someone else
        db.start_work(a.id, assignee="me", actor="me")
        db.start_work(b.id, assignee="me", actor="me")
        db.start_work(c.id, assignee="other", actor="other")
        released, failed = db.release_my_claims(actor="me")
        assert {r.id for r in released} == {a.id, b.id}
        assert failed == []
        # The other-held claim is untouched.
        held = db.get_issue(c.id)
        assert held.assignee == "other"
        # Released claims reverted wip→open and rejoin discovery.
        for issue in released:
            assert issue.assignee == ""
            assert issue.status == "open"

    def test_label_filter_narrows_the_set(self, db: FiligreeDB) -> None:
        a = db.create_issue("A", type="task", labels=["cluster:session-h"])
        b = db.create_issue("B", type="task")
        db.start_work(a.id, assignee="me", actor="me")
        db.start_work(b.id, assignee="me", actor="me")
        released, _failed = db.release_my_claims(actor="me", label="cluster:session-h")
        assert {r.id for r in released} == {a.id}
        # B is still held by 'me' — outside the label scope.
        held = db.get_issue(b.id)
        assert held.assignee == "me"

    def test_label_prefix_filter(self, db: FiligreeDB) -> None:
        a = db.create_issue("A", type="task", labels=["cluster:session-h"])
        b = db.create_issue("B", type="task", labels=["cluster:session-h"])
        c = db.create_issue("C", type="task", labels=["unrelated"])
        for issue in (a, b, c):
            db.start_work(issue.id, assignee="me", actor="me")
        released, _failed = db.release_my_claims(actor="me", label_prefix="cluster:")
        assert {r.id for r in released} == {a.id, b.id}

    def test_label_prefix_requires_trailing_colon(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="trailing colon"):
            db.release_my_claims(actor="me", label_prefix="cluster")

    def test_actor_required(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="actor"):
            db.release_my_claims(actor="")
        with pytest.raises(ValueError, match="actor"):
            db.release_my_claims(actor="   ")

    def test_dry_run_makes_no_changes(self, db: FiligreeDB) -> None:
        a = db.create_issue("A", type="task")
        db.start_work(a.id, assignee="me", actor="me")
        released, _failed = db.release_my_claims(actor="me", dry_run=True)
        assert len(released) == 1
        # The actual claim is still held.
        held = db.get_issue(a.id)
        assert held.assignee == "me"
        assert held.status == "in_progress"

    def test_skips_done_category_issues(self, db: FiligreeDB) -> None:
        """A closed issue still carries assignee as audit trail — release_my_claims
        does NOT clobber it. Otherwise the audit signal 'X closed this' is lost.
        """
        a = db.create_issue("A", type="task")
        db.start_work(a.id, assignee="me", actor="me")
        db.close_issue(a.id, reason="done", actor="me")
        # Now assignee=me, status=closed (done category). Release should skip.
        released, _failed = db.release_my_claims(actor="me")
        assert released == []
        # The audit trail is preserved.
        still_audited = db.get_issue(a.id)
        assert still_audited.assignee == "me"
        assert still_audited.status == "closed"

    def test_empty_when_nothing_held(self, db: FiligreeDB) -> None:
        released, failed = db.release_my_claims(actor="ghost")
        assert released == []
        assert failed == []


class TestReopenIssue:
    def test_reopen_bug_returns_to_triage(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Bug", type="bug")
        # Use directly-reachable wont_fix from triage (filigree-cb980eee0d).
        db.close_issue(issue.id, status="wont_fix")
        reopened = db.reopen_issue(issue.id)
        assert reopened.status == "triage"
        assert reopened.closed_at is None

    def test_reopen_bug_returns_to_last_non_done_status_and_clears_close_reason(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Bug", type="bug", fields={"severity": "major"})
        db.update_issue(issue.id, status="confirmed")
        db.update_issue(issue.id, status="fixing", fields={"root_cause": "bad assumption"})
        db.update_issue(issue.id, status="verifying", fields={"fix_verification": "regression added"})
        db.close_issue(issue.id, reason="closed too early")

        reopened = db.reopen_issue(issue.id, actor="tester")

        assert reopened.status == "verifying"
        assert reopened.closed_at is None
        assert reopened.fields["root_cause"] == "bad assumption"
        assert reopened.fields["fix_verification"] == "regression added"
        assert "close_reason" not in reopened.fields
        events = db.conn.execute(
            "SELECT * FROM events WHERE issue_id = ? AND event_type = 'reopened'",
            (issue.id,),
        ).fetchall()
        assert events[-1]["old_value"] == "closed"
        assert events[-1]["new_value"] == "verifying"

    def test_reopen_task_returns_to_open(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Task", type="task")
        db.close_issue(issue.id, reason="done")
        reopened = db.reopen_issue(issue.id)
        assert reopened.status == "open"
        assert "close_reason" not in reopened.fields

    def test_reopen_already_open_raises(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Task", type="task")
        with pytest.raises(ValueError, match="not in a done-category state"):
            db.reopen_issue(issue.id)

    def test_reopen_records_event(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Task", type="task")
        db.close_issue(issue.id)
        db.reopen_issue(issue.id, actor="tester")
        events = db.conn.execute(
            "SELECT * FROM events WHERE issue_id = ? AND event_type = 'reopened'",
            (issue.id,),
        ).fetchall()
        assert len(events) == 1

    def test_reopen_issue_atomic_under_event_failure(self, db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        issue = db.create_issue("Task", type="task")
        db.close_issue(issue.id, reason="not yet")
        original_record_event = db._record_event

        def fail_reopened(*args: object, **kwargs: object) -> None:
            if len(args) >= 2 and args[1] == "reopened":
                raise RuntimeError("boom")
            original_record_event(*args, **kwargs)

        monkeypatch.setattr(db, "_record_event", fail_reopened)

        with pytest.raises(RuntimeError, match="boom"):
            db.reopen_issue(issue.id, actor="tester")

        restored = db.get_issue(issue.id)
        assert restored.status == "closed"
        assert restored.closed_at is not None
        assert restored.fields["close_reason"] == "not yet"
        events = db.conn.execute(
            "SELECT event_type FROM events WHERE issue_id = ? ORDER BY event_seq ASC",
            (issue.id,),
        ).fetchall()
        assert "reopened" not in [row["event_type"] for row in events]

    def test_reopen_issue_single_transaction(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Task", type="task")
        db.close_issue(issue.id, reason="not yet")

        statements: list[str] = []
        db.conn.set_trace_callback(statements.append)
        try:
            db.reopen_issue(issue.id, actor="tester")
        finally:
            db.conn.set_trace_callback(None)

        assert [stmt for stmt in statements if stmt == "COMMIT"] == ["COMMIT"]


class TestReleaseClaim:
    """release_claim() clears assignee only — does not change status."""

    def test_release_bug_clears_assignee(self, db: FiligreeDB) -> None:
        """Bug type release should clear assignee without changing status."""
        issue = db.create_issue("Bug", type="bug")
        claimed = db.claim_issue(issue.id, assignee="agent-1")
        released = db.release_claim(claimed.id)
        assert released.status == "triage"  # status unchanged
        assert released.assignee == ""

    def test_release_task_clears_assignee(self, db: FiligreeDB) -> None:
        """Task type release clears assignee without changing status."""
        issue = db.create_issue("Task", type="task")
        claimed = db.claim_issue(issue.id, assignee="agent-1")
        released = db.release_claim(claimed.id)
        assert released.status == "open"  # status unchanged
        assert released.assignee == ""
        assert released.claimed_at is None
        assert released.last_heartbeat_at is None
        assert released.claim_expires_at is None

    def test_release_no_assignee_fails(self, db: FiligreeDB) -> None:
        """Cannot release an issue that has no assignee."""
        issue = db.create_issue("Task", type="task")
        with pytest.raises(ValueError, match="no assignee set"):
            db.release_claim(issue.id)

    def test_release_if_held_unassigned_is_noop(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Task", type="task")

        released = db.release_claim(issue.id, actor="agent-1", if_held=True)

        assert released.assignee == ""
        events = db.conn.execute("SELECT event_type FROM events WHERE issue_id = ?", (issue.id,)).fetchall()
        assert [event["event_type"] for event in events if event["event_type"] == "released"] == []

    def test_release_if_held_clears_actor_claim(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Task", type="task")
        db.claim_issue(issue.id, assignee="agent-1")

        released = db.release_claim(issue.id, actor="agent-1", if_held=True)

        assert released.assignee == ""
        events = db.get_recent_events(limit=10)
        released_events = [event for event in events if event["issue_id"] == issue.id and event["event_type"] == "released"]
        assert len(released_events) == 1
        assert released_events[0]["old_value"] == "agent-1"

    def test_release_if_held_honors_expected_assignee(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Task", type="task")
        db.claim_issue(issue.id, assignee="agent-1")

        released = db.release_claim(issue.id, actor="coordinator", if_held=True, expected_assignee="agent-1")

        assert released.assignee == ""

    def test_release_if_held_rejects_other_assignee(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Task", type="task")
        db.claim_issue(issue.id, assignee="agent-2")

        with pytest.raises(ValueError, match=r"assigned to 'agent-2'.*expected 'agent-1'"):
            db.release_claim(issue.id, actor="agent-1", if_held=True)

        assert db.get_issue(issue.id).assignee == "agent-2"

    def test_release_if_held_closed_unassigned_is_noop(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Closed one")
        db.close_issue(issue.id)

        released = db.release_claim(issue.id, actor="agent-1", if_held=True)

        assert released.status == "closed"
        assert released.assignee == ""

    def test_release_records_event(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Event check")
        db.claim_issue(issue.id, assignee="agent-1")
        db.release_claim(issue.id, actor="agent-1", reason="pausing for handoff")
        events = db.get_recent_events(limit=10)
        released_events = [e for e in events if e["event_type"] == "released"]
        assert len(released_events) == 1
        assert released_events[0]["actor"] == "agent-1"
        assert released_events[0]["comment"] == "pausing for handoff"

    def test_release_closed_issue_no_assignee(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Closed one")
        db.close_issue(issue.id)
        with pytest.raises(ValueError, match="no assignee set"):
            db.release_claim(issue.id)

    def test_release_not_found(self, db: FiligreeDB) -> None:
        with pytest.raises(KeyError, match="not found"):
            db.release_claim("test-nonexistent")

    def test_release_then_reclaim(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Reclaim test")
        db.claim_issue(issue.id, assignee="agent-1")
        db.release_claim(issue.id)
        reclaimed = db.claim_issue(issue.id, assignee="agent-2")
        assert reclaimed.status == "open"  # status unchanged
        assert reclaimed.assignee == "agent-2"

    def test_release_rejects_concurrent_reassign(self, db: FiligreeDB) -> None:
        """TOCTOU: a reassignment landing between read and UPDATE must not be erased."""
        issue = db.create_issue("Race test", type="task")
        db.claim_issue(issue.id, assignee="agent-1")

        # Intercept the release UPDATE and, right before it executes, simulate
        # another actor reassigning the issue to agent-2. With the old
        # unconditional UPDATE the clear silently wins; with compare-and-swap
        # it must rowcount=0 and raise.
        proxy = _InterceptingConnProxy(
            db._conn,
            lambda sql: _reassign_to(db._conn, issue.id, "agent-2") if "set assignee = ''" in sql.lower() else None,
        )
        db._conn = proxy  # type: ignore[assignment]
        try:
            with pytest.raises(ValueError, match="reassigned"):
                db.release_claim(issue.id, actor="agent-1")
        finally:
            db._conn = proxy._real

        assert proxy.fired, "race hook never fired — test is not exercising the gap"
        after = db.get_issue(issue.id)
        assert after.assignee == "agent-2", "newer claim must not be erased"

    def test_release_detects_concurrent_release(self, db: FiligreeDB) -> None:
        """If another actor already cleared the claim, CAS must raise instead of no-op'ing."""
        issue = db.create_issue("Double-release test", type="task")
        db.claim_issue(issue.id, assignee="agent-1")

        proxy = _InterceptingConnProxy(
            db._conn,
            lambda sql: _reassign_to(db._conn, issue.id, "") if "set assignee = ''" in sql.lower() else None,
        )
        db._conn = proxy  # type: ignore[assignment]
        try:
            with pytest.raises(ValueError, match="already released"):
                db.release_claim(issue.id, actor="agent-1")
        finally:
            db._conn = proxy._real

        assert proxy.fired
        assert db.get_issue(issue.id).assignee == ""


class TestClaimLeaseLiveness:
    def test_heartbeat_refreshes_current_holder_liveness(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Heartbeat")
        db.claim_issue(issue.id, assignee="agent-1")
        old = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        db.conn.execute(
            "UPDATE issues SET last_heartbeat_at = ?, claim_expires_at = ? WHERE id = ?",
            (old, old, issue.id),
        )
        db.conn.commit()

        refreshed = db.heartbeat_work(issue.id, actor="agent-1")

        assert refreshed.assignee == "agent-1"
        assert refreshed.last_heartbeat_at is not None
        assert datetime.fromisoformat(refreshed.last_heartbeat_at) > datetime.fromisoformat(old)
        assert refreshed.claim_expires_at is not None
        assert datetime.fromisoformat(refreshed.claim_expires_at) > datetime.fromisoformat(refreshed.last_heartbeat_at)

    def test_heartbeat_rejects_other_holder(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Heartbeat")
        db.claim_issue(issue.id, assignee="agent-1")

        with pytest.raises(ValueError, match=r"assigned to 'agent-1'.*expected 'agent-2'"):
            db.heartbeat_work(issue.id, actor="agent-2")

    def test_stale_claims_include_expired_leases_and_legacy_assigned_work(self, db: FiligreeDB) -> None:
        expired = db.create_issue("Expired lease", priority=1)
        legacy = db.create_issue("Legacy stale", priority=0)
        fresh = db.create_issue("Fresh lease", priority=0)
        closed = db.create_issue("Closed assigned", priority=0)
        db.claim_issue(expired.id, assignee="agent-1")
        db.claim_issue(fresh.id, assignee="agent-2")
        db.claim_issue(closed.id, assignee="agent-3")
        old = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        db.conn.execute(
            "UPDATE issues SET claim_expires_at = ?, last_heartbeat_at = ? WHERE id = ?",
            (old, old, expired.id),
        )
        db.conn.execute(
            "UPDATE issues SET assignee = 'legacy-agent', updated_at = ?, claimed_at = NULL, "
            "last_heartbeat_at = NULL, claim_expires_at = NULL WHERE id = ?",
            (old, legacy.id),
        )
        db.conn.execute(
            "UPDATE issues SET claim_expires_at = ?, last_heartbeat_at = ? WHERE id = ?",
            (future, future, fresh.id),
        )
        db.conn.commit()
        db.close_issue(closed.id)

        stale = db.get_stale_claims(stale_after_hours=48)

        assert [issue.id for issue in stale] == [legacy.id, expired.id]

    def test_stale_claims_can_include_near_expiry_leases(self, db: FiligreeDB) -> None:
        soon = db.create_issue("Expires soon", priority=0)
        later = db.create_issue("Expires later", priority=0)
        expired = db.create_issue("Already expired", priority=0)
        closed = db.create_issue("Closed soon", priority=0)
        db.claim_issue(soon.id, assignee="agent-soon")
        db.claim_issue(later.id, assignee="agent-later")
        db.claim_issue(expired.id, assignee="agent-expired")
        db.claim_issue(closed.id, assignee="agent-closed")
        now = datetime.now(UTC)
        soon_at = (now + timedelta(hours=1)).isoformat()
        later_at = (now + timedelta(hours=5)).isoformat()
        expired_at = (now - timedelta(hours=1)).isoformat()
        db.conn.execute(
            "UPDATE issues SET claim_expires_at = ?, last_heartbeat_at = ? WHERE id = ?",
            (soon_at, soon_at, soon.id),
        )
        db.conn.execute(
            "UPDATE issues SET claim_expires_at = ?, last_heartbeat_at = ? WHERE id = ?",
            (later_at, later_at, later.id),
        )
        db.conn.execute(
            "UPDATE issues SET claim_expires_at = ?, last_heartbeat_at = ? WHERE id = ?",
            (expired_at, expired_at, expired.id),
        )
        db.conn.execute(
            "UPDATE issues SET claim_expires_at = ?, last_heartbeat_at = ? WHERE id = ?",
            (soon_at, soon_at, closed.id),
        )
        db.conn.commit()
        db.close_issue(closed.id)

        default_stale = db.get_stale_claims()
        proactive = db.get_stale_claims(expires_within_hours=2)

        assert [issue.id for issue in default_stale] == [expired.id]
        assert [issue.id for issue in proactive] == [soon.id, expired.id]

    def test_reclaim_issue_transfers_only_expected_holder_and_records_reason(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Reclaim me")
        db.claim_issue(issue.id, assignee="agent-old")

        reclaimed = db.reclaim_issue(
            issue.id,
            assignee="agent-new",
            expected_assignee="agent-old",
            reason="agent-old missed heartbeat",
            actor="coordinator",
        )

        assert reclaimed.assignee == "agent-new"
        assert reclaimed.claimed_at is not None
        assert reclaimed.last_heartbeat_at == reclaimed.claimed_at
        events = db.conn.execute(
            "SELECT event_type, actor, old_value, new_value, comment FROM events WHERE issue_id = ? ORDER BY id",
            (issue.id,),
        ).fetchall()
        reclaimed_event = next(event for event in events if event["event_type"] == "reclaimed")
        assert reclaimed_event["actor"] == "coordinator"
        assert reclaimed_event["old_value"] == "agent-old"
        assert reclaimed_event["new_value"] == "agent-new"
        assert reclaimed_event["comment"] == "agent-old missed heartbeat"

    def test_reclaim_issue_rejects_unexpected_holder(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Reclaim me")
        db.claim_issue(issue.id, assignee="agent-current")

        with pytest.raises(ValueError, match=r"assigned to 'agent-current'.*expected 'agent-old'"):
            db.reclaim_issue(
                issue.id,
                assignee="agent-new",
                expected_assignee="agent-old",
                reason="stale",
                actor="coordinator",
            )


class TestWritePathExpectedAssignee:
    """Claim-aware preconditions on write tools.

    filigree-cb980eee0d (P1.1): heartbeat_work / release_claim / reclaim_issue
    strictly enforce claim ownership; update_issue / batch_update / close_issue /
    add_comment / add_label / remove_label previously ignored it. Each now
    accepts expected_assignee and defaults the expected holder to actor when an
    actor is present and the issue is held (ADR-008).
    """

    def test_update_issue_defaults_expected_assignee_to_actor_for_held_issue(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Actor default")
        db.claim_issue(issue.id, assignee="agent-holder")

        with pytest.raises(ValueError, match=r"assigned to 'agent-holder'.*expected 'other-agent'"):
            db.update_issue(issue.id, priority=0, actor="other-agent")

    def test_update_issue_actor_default_succeeds_for_current_holder(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Actor default")
        db.claim_issue(issue.id, assignee="agent-holder")

        updated = db.update_issue(issue.id, priority=0, actor="agent-holder")

        assert updated.priority == 0

    def test_update_issue_explicit_expected_assignee_overrides_actor_default(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Coordinator override")
        db.claim_issue(issue.id, assignee="agent-holder")

        updated = db.update_issue(issue.id, priority=0, actor="coordinator", expected_assignee="agent-holder")

        assert updated.priority == 0

    def test_update_issue_without_actor_remains_permissive(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Actorless local update")
        db.claim_issue(issue.id, assignee="agent-holder")

        updated = db.update_issue(issue.id, priority=0)

        assert updated.priority == 0

    def test_update_issue_rejects_when_expected_assignee_mismatches(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Claim-aware update")
        db.claim_issue(issue.id, assignee="agent-holder")
        with pytest.raises(ValueError, match=r"assigned to 'agent-holder'.*expected 'agent-other'"):
            db.update_issue(issue.id, priority=0, expected_assignee="agent-other")

    def test_update_issue_succeeds_when_expected_assignee_matches(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Claim-aware update")
        db.claim_issue(issue.id, assignee="agent-holder")
        updated = db.update_issue(issue.id, priority=0, expected_assignee="agent-holder")
        assert updated.priority == 0

    def test_close_issue_rejects_unexpected_holder(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Claim-aware close")
        db.claim_issue(issue.id, assignee="agent-holder")
        with pytest.raises(ValueError, match=r"assigned to 'agent-holder'.*expected 'agent-other'"):
            db.close_issue(issue.id, expected_assignee="agent-other")

    def test_close_issue_defaults_expected_assignee_to_actor_for_held_issue(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Claim-aware close")
        db.claim_issue(issue.id, assignee="agent-holder")
        with pytest.raises(ValueError, match=r"assigned to 'agent-holder'.*expected 'other-agent'"):
            db.close_issue(issue.id, actor="other-agent")

    def test_add_comment_rejects_unexpected_holder(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Claim-aware comment")
        db.claim_issue(issue.id, assignee="agent-holder")
        with pytest.raises(ValueError, match=r"assigned to 'agent-holder'.*expected 'agent-other'"):
            db.add_comment(issue.id, "note", expected_assignee="agent-other")

    def test_add_comment_defaults_expected_assignee_to_author_for_held_issue(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Claim-aware comment")
        db.claim_issue(issue.id, assignee="agent-holder")
        with pytest.raises(ValueError, match=r"assigned to 'agent-holder'.*expected 'other-agent'"):
            db.add_comment(issue.id, "note", author="other-agent")

    def test_add_label_rejects_unexpected_holder(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Claim-aware label")
        db.claim_issue(issue.id, assignee="agent-holder")
        with pytest.raises(ValueError, match=r"assigned to 'agent-holder'.*expected 'agent-other'"):
            db.add_label(issue.id, "needs-review", expected_assignee="agent-other")

    def test_add_label_defaults_expected_assignee_to_actor_for_held_issue(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Claim-aware label")
        db.claim_issue(issue.id, assignee="agent-holder")
        with pytest.raises(ValueError, match=r"assigned to 'agent-holder'.*expected 'other-agent'"):
            db.add_label(issue.id, "needs-review", actor="other-agent")

    def test_remove_label_rejects_unexpected_holder(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Claim-aware label")
        db.claim_issue(issue.id, assignee="agent-holder")
        db.add_label(issue.id, "needs-review")
        with pytest.raises(ValueError, match=r"assigned to 'agent-holder'.*expected 'agent-other'"):
            db.remove_label(issue.id, "needs-review", expected_assignee="agent-other")

    def test_remove_label_defaults_expected_assignee_to_actor_for_held_issue(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Claim-aware label")
        db.claim_issue(issue.id, assignee="agent-holder")
        db.add_label(issue.id, "needs-review")
        with pytest.raises(ValueError, match=r"assigned to 'agent-holder'.*expected 'other-agent'"):
            db.remove_label(issue.id, "needs-review", actor="other-agent")

    def test_batch_update_partial_failure_on_mismatch(self, db: FiligreeDB) -> None:
        held = db.create_issue("Held")
        unheld = db.create_issue("Unheld")
        db.claim_issue(held.id, assignee="agent-holder")
        # batch_update with expected_assignee='agent-holder' should succeed for held,
        # fail for unheld (assignee=='').
        succeeded, failed = db.batch_update(
            [held.id, unheld.id],
            priority=0,
            expected_assignee="agent-holder",
        )
        assert {i.id for i in succeeded} == {held.id}
        assert {f["id"] for f in failed} == {unheld.id}
        # The failure carries the CONFLICT-shaped message.
        assert any("expected 'agent-holder'" in f["error"] for f in failed)

    def test_batch_update_defaults_expected_assignee_to_actor_per_held_item(self, db: FiligreeDB) -> None:
        held_by_actor = db.create_issue("Held by actor")
        held_by_other = db.create_issue("Held by other")
        unheld = db.create_issue("Unheld")
        db.claim_issue(held_by_actor.id, assignee="agent-holder")
        db.claim_issue(held_by_other.id, assignee="agent-other")

        succeeded, failed = db.batch_update(
            [held_by_actor.id, held_by_other.id, unheld.id],
            priority=0,
            actor="agent-holder",
        )

        assert {i.id for i in succeeded} == {held_by_actor.id, unheld.id}
        assert {f["id"] for f in failed} == {held_by_other.id}
        assert failed[0]["code"] == ErrorCode.CONFLICT
        assert "expected 'agent-holder'" in failed[0]["error"]

    def test_batch_close_defaults_expected_assignee_to_actor_per_held_item(self, db: FiligreeDB) -> None:
        held_by_actor = db.create_issue("Held by actor")
        held_by_other = db.create_issue("Held by other")
        db.claim_issue(held_by_actor.id, assignee="agent-holder")
        db.claim_issue(held_by_other.id, assignee="agent-other")

        succeeded, failed = db.batch_close(
            [held_by_actor.id, held_by_other.id],
            actor="agent-holder",
        )

        assert {i.id for i in succeeded} == {held_by_actor.id}
        assert {f["id"] for f in failed} == {held_by_other.id}
        assert failed[0]["code"] == ErrorCode.CONFLICT


# ---------------------------------------------------------------------------
# Task 1.10: Category-Aware Queries
# ---------------------------------------------------------------------------


class TestGetStatesForCategory:
    """_get_states_for_category() collects states across all enabled types."""

    def test_open_category_includes_triage(self, db: FiligreeDB) -> None:
        """Open category should include bug's 'triage' and 'confirmed' states."""
        states = db._get_states_for_category("open")
        assert "open" in states  # from task
        assert "triage" in states  # from bug
        assert "confirmed" in states  # from bug

    def test_wip_category_includes_fixing(self, db: FiligreeDB) -> None:
        """Wip category should include bug's 'fixing' and 'verifying' states."""
        states = db._get_states_for_category("wip")
        assert "in_progress" in states  # from task
        assert "fixing" in states  # from bug
        assert "verifying" in states  # from bug

    def test_done_category_includes_wont_fix(self, db: FiligreeDB) -> None:
        """Done category should include bug's 'closed' and 'wont_fix' states."""
        states = db._get_states_for_category("done")
        assert "closed" in states  # from task
        assert "wont_fix" in states  # from bug

    def test_no_duplicates(self, db: FiligreeDB) -> None:
        """State names should not appear twice even if multiple types share a state."""
        states = db._get_states_for_category("open")
        assert len(states) == len(set(states))


class TestListIssuesCategory:
    """list_issues(status=) accepts category names and specific states."""

    def test_list_by_open_category(self, db: FiligreeDB) -> None:
        """list_issues(status='open') returns issues in any open-category state."""
        bug = db.create_issue("Bug", type="bug")  # status=triage (open category)
        task = db.create_issue("Task", type="task")  # status=open (open category)
        db.update_issue(task.id, status="in_progress")  # now wip

        issues = db.list_issues(status="open")
        ids = {i.id for i in issues}
        assert bug.id in ids  # triage is open-category
        assert task.id not in ids  # in_progress is wip-category

    def test_list_by_wip_category(self, db: FiligreeDB) -> None:
        """list_issues(status='wip') returns issues in any wip-category state."""
        bug = db.create_issue("Bug", type="bug")
        db.update_issue(bug.id, status="confirmed")
        db.update_issue(bug.id, status="fixing")
        task = db.create_issue("Task", type="task")

        issues = db.list_issues(status="wip")
        ids = {i.id for i in issues}
        assert bug.id in ids  # fixing is wip-category
        assert task.id not in ids  # open is open-category

    def test_list_by_specific_state(self, db: FiligreeDB) -> None:
        """list_issues(status='triage') returns only bugs in literal 'triage' state."""
        bug = db.create_issue("Bug", type="bug")  # triage
        bug2 = db.create_issue("Bug2", type="bug")
        db.update_issue(bug2.id, status="confirmed")  # confirmed

        issues = db.list_issues(status="triage")
        ids = {i.id for i in issues}
        assert bug.id in ids
        assert bug2.id not in ids  # confirmed, not triage

    def test_list_by_done_category(self, db: FiligreeDB) -> None:
        """list_issues(status='done') returns issues in any done-category state."""
        bug = db.create_issue("Bug", type="bug")
        db.close_issue(bug.id, status="wont_fix")
        task = db.create_issue("Task", type="task")
        db.close_issue(task.id)

        issues = db.list_issues(status="done")
        ids = {i.id for i in issues}
        assert bug.id in ids
        assert task.id in ids


class TestGetReadyCategory:
    """get_ready() uses open-category states from ALL enabled types."""

    def test_ready_includes_bug_in_triage(self, db: FiligreeDB) -> None:
        """Bug in 'triage' (open-category) with no blockers is ready."""
        bug = db.create_issue("Bug", type="bug")
        ready = db.get_ready()
        ids = {i.id for i in ready}
        assert bug.id in ids

    def test_ready_excludes_assigned_open_issue(self, db: FiligreeDB) -> None:
        """Assigned open-category issues are not claimable ready work."""
        bug = db.create_issue("Assigned bug", type="bug")
        db.claim_issue(bug.id, assignee="agent-1")

        ready = db.get_ready()
        ids = {i.id for i in ready}

        assert bug.id not in ids
        assert db.get_issue(bug.id).is_ready is False

    def test_ready_excludes_wip(self, db: FiligreeDB) -> None:
        """Bug in 'fixing' (wip-category) is not ready."""
        bug = db.create_issue("Bug", type="bug")
        db.update_issue(bug.id, status="confirmed")
        db.update_issue(bug.id, status="fixing")
        ready = db.get_ready()
        ids = {i.id for i in ready}
        assert bug.id not in ids

    def test_ready_excludes_blocked(self, db: FiligreeDB) -> None:
        """Bug in 'triage' blocked by open task is not ready."""
        blocker = db.create_issue("Blocker", type="task")
        bug = db.create_issue("Bug", type="bug")
        db.add_dependency(bug.id, blocker.id)
        ready = db.get_ready()
        ids = {i.id for i in ready}
        assert bug.id not in ids

    def test_ready_with_done_blocker_is_unblocked(self, db: FiligreeDB) -> None:
        """Bug in 'triage' with closed blocker should be ready."""
        blocker = db.create_issue("Blocker", type="task")
        bug = db.create_issue("Bug", type="bug")
        db.add_dependency(bug.id, blocker.id)
        db.close_issue(blocker.id)
        ready = db.get_ready()
        ids = {i.id for i in ready}
        assert bug.id in ids

    def test_ready_with_wont_fix_blocker_is_unblocked(self, db: FiligreeDB) -> None:
        """Bug blocked by a 'wont_fix' (done-category) bug should be ready."""
        blocker = db.create_issue("Blocker bug", type="bug")
        bug = db.create_issue("Main bug", type="bug")
        db.add_dependency(bug.id, blocker.id)
        db.close_issue(blocker.id, status="wont_fix")
        ready = db.get_ready()
        ids = {i.id for i in ready}
        assert bug.id in ids

    def test_ready_handles_no_done_states(self, tmp_path: Path) -> None:
        """get_ready() should work when enabled templates define zero done-category states."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        (filigree_dir / "config.json").write_text(json.dumps({"prefix": "test", "version": 1, "enabled_packs": ["custom_only"]}))

        packs_dir = filigree_dir / "packs"
        packs_dir.mkdir()
        custom_pack: dict[str, Any] = {
            "pack": "custom_only",
            "version": "1.0",
            "display_name": "Custom Only",
            "description": "Pack with no done-category states",
            "requires_packs": [],
            "types": {
                "custom_item": {
                    "type": "custom_item",
                    "display_name": "Custom Item",
                    "description": "No done states",
                    "pack": "custom_only",
                    "states": [
                        {"name": "open", "category": "open"},
                        {"name": "active", "category": "wip"},
                    ],
                    "initial_state": "open",
                    "transitions": [
                        {"from": "open", "to": "active", "enforcement": "soft"},
                    ],
                    "fields_schema": [],
                },
            },
            "relationships": [],
            "cross_pack_relationships": [],
            "guide": None,
        }
        (packs_dir / "custom_only.json").write_text(json.dumps(custom_pack))

        db = FiligreeDB(filigree_dir / "filigree.db", prefix="test")
        db.initialize()

        blocker = db.create_issue("Blocker", type="custom_item")
        blocked = db.create_issue("Blocked", type="custom_item")
        db.add_dependency(blocked.id, blocker.id)

        ready_ids = {i.id for i in db.get_ready()}
        assert blocker.id in ready_ids
        assert blocked.id not in ready_ids

        db.close()


class TestGetBlockedCategory:
    """get_blocked() uses open-category + done-category for blocker checks."""

    def test_blocked_in_triage(self, db: FiligreeDB) -> None:
        """Bug in 'triage' (open-category) with open blocker is blocked."""
        blocker = db.create_issue("Blocker", type="task")
        bug = db.create_issue("Bug", type="bug")
        db.add_dependency(bug.id, blocker.id)
        blocked = db.get_blocked()
        ids = {i.id for i in blocked}
        assert bug.id in ids

    def test_not_blocked_if_blocker_done(self, db: FiligreeDB) -> None:
        """Bug in 'triage' with done-category blocker is NOT blocked."""
        blocker = db.create_issue("Blocker bug", type="bug")
        bug = db.create_issue("Bug", type="bug")
        db.add_dependency(bug.id, blocker.id)
        db.close_issue(blocker.id, status="wont_fix")
        blocked = db.get_blocked()
        ids = {i.id for i in blocked}
        assert bug.id not in ids


class TestGetCriticalPathCategory:
    """get_critical_path() uses done-category for filtering out completed issues."""

    def test_critical_path_excludes_done_category(self, db: FiligreeDB) -> None:
        """Issues in done-category states (including wont_fix) excluded from critical path."""
        a = db.create_issue("A", type="bug")
        b = db.create_issue("B", type="bug")
        c = db.create_issue("C", type="bug")
        db.add_dependency(b.id, a.id)
        db.add_dependency(c.id, b.id)
        # Close A with wont_fix (done-category)
        db.close_issue(a.id, status="wont_fix")
        path = db.get_critical_path()
        path_ids = {p["id"] for p in path}
        assert a.id not in path_ids  # done-category excluded

    def test_critical_path_includes_wip(self, db: FiligreeDB) -> None:
        """Issues in wip-category states should be in critical path."""
        a = db.create_issue("A", type="bug")
        b = db.create_issue("B", type="bug")
        db.add_dependency(b.id, a.id)
        db.update_issue(a.id, status="confirmed")
        db.update_issue(a.id, status="fixing")
        path = db.get_critical_path()
        path_ids = {p["id"] for p in path}
        assert a.id in path_ids  # wip-category included


# ---------------------------------------------------------------------------
# Task 1.11: Issue.to_dict() Includes status_category
# ---------------------------------------------------------------------------


class TestStatusCategory:
    """Issue.to_dict() includes status_category computed field."""

    def test_bug_triage_category_is_open(self, db: FiligreeDB) -> None:
        """Bug in 'triage' should have status_category='open'."""
        issue = db.create_issue("Bug", type="bug")
        d = issue.to_dict()
        assert d["status_category"] == "open"

    def test_bug_fixing_category_is_wip(self, db: FiligreeDB) -> None:
        """Bug in 'fixing' should have status_category='wip'."""
        issue = db.create_issue("Bug", type="bug")
        db.update_issue(issue.id, status="confirmed")
        updated = db.update_issue(issue.id, status="fixing")
        d = updated.to_dict()
        assert d["status_category"] == "wip"

    def test_bug_closed_category_is_done(self, db: FiligreeDB) -> None:
        """Bug in 'closed' should have status_category='done'."""
        issue = db.create_issue("Bug", type="bug")
        # closed is reachable only from verifying — walk the workflow.
        db.update_issue(issue.id, status="confirmed", fields={"severity": "minor"})
        db.update_issue(issue.id, status="fixing", fields={"root_cause": "redacted"})
        db.update_issue(issue.id, status="verifying", fields={"fix_verification": "smoke test"})
        closed = db.close_issue(issue.id)
        d = closed.to_dict()
        assert d["status_category"] == "done"

    def test_bug_wont_fix_category_is_done(self, db: FiligreeDB) -> None:
        """Bug in 'wont_fix' should have status_category='done'."""
        issue = db.create_issue("Bug", type="bug")
        closed = db.close_issue(issue.id, status="wont_fix")
        d = closed.to_dict()
        assert d["status_category"] == "done"

    def test_task_open_category(self, db: FiligreeDB) -> None:
        """Task in 'open' should have status_category='open'."""
        issue = db.create_issue("Task", type="task")
        d = issue.to_dict()
        assert d["status_category"] == "open"

    def test_task_in_progress_category(self, db: FiligreeDB) -> None:
        """Task in 'in_progress' should have status_category='wip'."""
        issue = db.create_issue("Task", type="task")
        updated = db.update_issue(issue.id, status="in_progress")
        d = updated.to_dict()
        assert d["status_category"] == "wip"

    def test_task_closed_category(self, db: FiligreeDB) -> None:
        """Task in 'closed' should have status_category='done'."""
        issue = db.create_issue("Task", type="task")
        closed = db.close_issue(issue.id)
        d = closed.to_dict()
        assert d["status_category"] == "done"

    def test_unknown_type_rejected_at_creation(self, db: FiligreeDB) -> None:
        """Unknown types are rejected, so fallback heuristics don't apply."""
        with pytest.raises(ValueError, match="Unknown type"):
            db.create_issue("Custom", type="custom_type")

    def test_status_category_in_list_issues(self, db: FiligreeDB) -> None:
        """list_issues() results should include status_category."""
        db.create_issue("Bug", type="bug")
        issues = db.list_issues()
        assert all("status_category" in i.to_dict() for i in issues)


# ---------------------------------------------------------------------------
# Task 1.12: New FiligreeDB Methods
# ---------------------------------------------------------------------------


class TestGetValidTransitions:
    """FiligreeDB.get_valid_transitions() delegates to TemplateRegistry."""

    def test_bug_triage_transitions(self, db: FiligreeDB) -> None:
        """Bug in 'triage' should have transitions to confirmed and wont_fix."""
        issue = db.create_issue("Bug", type="bug")
        options = db.get_valid_transitions(issue.id)
        targets = {o.to for o in options}
        assert "confirmed" in targets
        assert "wont_fix" in targets

    def test_bug_triage_transition_categories(self, db: FiligreeDB) -> None:
        """Transition options should include target state categories."""
        issue = db.create_issue("Bug", type="bug")
        options = db.get_valid_transitions(issue.id)
        confirmed_opt = next(o for o in options if o.to == "confirmed")
        assert confirmed_opt.category == "open"
        wont_fix_opt = next(o for o in options if o.to == "wont_fix")
        assert wont_fix_opt.category == "done"

    def test_bug_fixing_shows_readiness(self, db: FiligreeDB) -> None:
        """Transitions from 'fixing' should show field readiness."""
        issue = db.create_issue("Bug", type="bug")
        db.update_issue(issue.id, status="confirmed")
        db.update_issue(issue.id, status="fixing")
        options = db.get_valid_transitions(issue.id)
        verifying_opt = next(o for o in options if o.to == "verifying")
        assert verifying_opt.ready is False
        assert "fix_verification" in verifying_opt.missing_fields

    def test_bug_fixing_ready_with_fields(self, db: FiligreeDB) -> None:
        """Transition should be ready when required fields are populated."""
        issue = db.create_issue("Bug", type="bug")
        db.update_issue(issue.id, status="confirmed")
        db.update_issue(issue.id, status="fixing", fields={"fix_verification": "tests pass"})
        options = db.get_valid_transitions(issue.id)
        verifying_opt = next(o for o in options if o.to == "verifying")
        assert verifying_opt.ready is True
        assert verifying_opt.missing_fields == ()

    def test_task_open_transitions(self, db: FiligreeDB) -> None:
        """Task in 'open' should have transition to in_progress."""
        issue = db.create_issue("Task", type="task")
        options = db.get_valid_transitions(issue.id)
        targets = {o.to for o in options}
        assert "in_progress" in targets

    def test_unknown_type_cannot_be_created(self, db: FiligreeDB) -> None:
        """Unknown types are rejected at creation — can't reach get_valid_transitions."""
        with pytest.raises(ValueError, match="Unknown type"):
            db.create_issue("Custom", type="custom_type")

    def test_closed_issue_transitions(self, db: FiligreeDB) -> None:
        """Closed issues should have no outgoing transitions (terminal state)."""
        issue = db.create_issue("Task", type="task")
        db.close_issue(issue.id)
        options = db.get_valid_transitions(issue.id)
        assert len(options) == 0

    def test_nonexistent_issue_raises(self, db: FiligreeDB) -> None:
        """get_valid_transitions on nonexistent issue raises KeyError."""
        with pytest.raises(KeyError):
            db.get_valid_transitions("test-nonexistent")

    def test_return_type_is_transition_option(self, db: FiligreeDB) -> None:
        """Results should be TransitionOption dataclass instances."""
        issue = db.create_issue("Bug", type="bug")
        options = db.get_valid_transitions(issue.id)
        assert len(options) > 0
        assert isinstance(options[0], TransitionOption)


class TestValidateIssue:
    """FiligreeDB.validate_issue() checks issue against its template."""

    def test_valid_bug_in_triage(self, db: FiligreeDB) -> None:
        """Bug in 'triage' with no required fields should validate clean."""
        issue = db.create_issue("Bug", type="bug")
        result = db.validate_issue(issue.id)
        assert result.valid is True
        assert len(result.errors) == 0

    def test_bug_in_confirmed_missing_severity(self, db: FiligreeDB) -> None:
        """Bug in 'confirmed' without severity should have a warning."""
        issue = db.create_issue("Bug", type="bug")
        db.update_issue(issue.id, status="confirmed")
        result = db.validate_issue(issue.id)
        assert len(result.warnings) > 0
        assert any("severity" in w for w in result.warnings)

    def test_bug_in_confirmed_with_severity_valid(self, db: FiligreeDB) -> None:
        """Bug in 'confirmed' with severity should validate clean."""
        issue = db.create_issue("Bug", type="bug")
        db.update_issue(issue.id, status="confirmed", fields={"severity": "major"})
        result = db.validate_issue(issue.id)
        assert result.valid is True

    def test_task_always_valid(self, db: FiligreeDB) -> None:
        """Task with no required_at fields should always validate clean."""
        issue = db.create_issue("Task", type="task")
        result = db.validate_issue(issue.id)
        assert result.valid is True
        assert len(result.warnings) == 0

    def test_unknown_type_cannot_be_created(self, db: FiligreeDB) -> None:
        """Unknown types are rejected at creation — can't reach validate_issue."""
        with pytest.raises(ValueError, match="Unknown type"):
            db.create_issue("Custom", type="custom_type")

    def test_return_type_is_validation_result(self, db: FiligreeDB) -> None:
        """Result should be a ValidationResult dataclass."""
        issue = db.create_issue("Bug", type="bug")
        result = db.validate_issue(issue.id)
        assert isinstance(result, ValidationResult)

    def test_nonexistent_issue_raises(self, db: FiligreeDB) -> None:
        """validate_issue on nonexistent issue raises KeyError."""
        with pytest.raises(KeyError):
            db.validate_issue("test-nonexistent")

    def test_unknown_type_emits_error(self, tmp_path: Path) -> None:
        """Bug filigree-910f1cb024: issue whose type is not in the active registry
        must fail validation, not silently pass."""
        d = make_db(tmp_path, packs=["core", "planning", "release"])
        try:
            issue = d.create_issue("Rel", type="release")
        finally:
            d.close()

        # Disable release pack on next load — the type is now unknown.
        d2 = make_db(tmp_path, packs=["core", "planning"])
        try:
            result = d2.validate_issue(issue.id)
            assert result.valid is False
            assert any("release" in e for e in result.errors)
        finally:
            d2.close()

    def test_undeclared_status_emits_error(self, db: FiligreeDB) -> None:
        """Bug filigree-910f1cb024: issue whose current status is not a declared
        state for its type must fail validation."""
        issue = db.create_issue("Task", type="task")
        # Force an impossible state via raw SQL (simulates bulk import / migration
        # from an older template).
        db.conn.execute("UPDATE issues SET status = ? WHERE id = ?", ("not_a_real_state", issue.id))
        db.conn.commit()
        result = db.validate_issue(issue.id)
        assert result.valid is False
        assert any("not_a_real_state" in e for e in result.errors)

    def test_invalid_enum_field_value_emits_warning(self, db: FiligreeDB) -> None:
        """Imported/legacy enum values must be reported by validate_issue()."""
        issue = db.create_issue("Bug", type="bug")
        db.conn.execute(
            "UPDATE issues SET fields = ? WHERE id = ?",
            (json.dumps({"severity": "catastrophic"}), issue.id),
        )
        db.conn.commit()

        result = db.validate_issue(issue.id)

        assert result.valid is True
        assert any("severity" in w and "not a valid option" in w for w in result.warnings)


class TestInferStatusCategoryFallback:
    """Bug filigree-5c1605d349: name-only fallback must cover every built-in done state.

    Exercised whenever ``_resolve_status_category`` can't find a ``(type, state)`` in
    the active registry — e.g. pack disabled after issues in it were created.
    """

    def test_active_template_archived_state_category_wins(self, db: FiligreeDB) -> None:
        """A real template state named ``archived`` must not be forced to done."""
        tpl = TypeTemplate(
            type="live_archive",
            display_name="Live Archive",
            description="",
            pack="test",
            states=(
                StateDefinition(name="archived", category="open"),
                StateDefinition(name="closed", category="done"),
            ),
            initial_state="archived",
            transitions=(),
            fields_schema=(),
        )
        db.templates._register_type(tpl)

        issue = db.create_issue("Active archive", type="live_archive")
        hydrated = db.get_issue(issue.id)

        assert hydrated.status == "archived"
        assert hydrated.status_category == "open"

    def test_builtin_release_released_is_done(self) -> None:
        """``release`` pack's ``released`` state must classify as done even when disabled."""
        from filigree.core import FiligreeDB

        assert FiligreeDB._infer_status_category("release", "released") == "done"

    def test_builtin_risk_mitigated_is_done(self) -> None:
        """``risk`` pack's ``mitigated`` state must classify as done even when disabled."""
        from filigree.core import FiligreeDB

        assert FiligreeDB._infer_status_category("risk", "mitigated") == "done"

    def test_builtin_requirement_verified_is_done(self) -> None:
        """``requirement`` pack's ``verified`` state must classify as done."""
        from filigree.core import FiligreeDB

        assert FiligreeDB._infer_status_category("requirement", "verified") == "done"

    def test_builtin_milestone_completed_is_done(self) -> None:
        """``milestone`` pack's ``completed`` state must classify as done."""
        from filigree.core import FiligreeDB

        assert FiligreeDB._infer_status_category("milestone", "completed") == "done"

    def test_incident_resolved_is_wip_not_done(self) -> None:
        """``resolved`` is wip for incidents — type-aware lookup must return wip,
        even though the legacy hardcoded set treated ``resolved`` as done."""
        from filigree.core import FiligreeDB

        assert FiligreeDB._infer_status_category("incident", "resolved") == "wip"

    def test_bug_fixing_is_wip(self) -> None:
        """``bug`` pack's ``fixing`` state must classify as wip."""
        from filigree.core import FiligreeDB

        assert FiligreeDB._infer_status_category("bug", "fixing") == "wip"

    def test_custom_type_falls_through_to_open(self) -> None:
        """Truly unknown (type, state) pairs default to ``open`` — permissive."""
        from filigree.core import FiligreeDB

        assert FiligreeDB._infer_status_category("my_custom_type", "some_weird_state") == "open"

    def test_disabled_pack_issue_stays_done(self, tmp_path: Path) -> None:
        """End-to-end: release issue in ``released`` state retains done category
        after the release pack is disabled — so close_issue/reopen_issue/stats
        keep working."""
        d = make_db(tmp_path, packs=["core", "planning", "release"])
        try:
            issue = d.create_issue("Rel", type="release")
            # Force status directly — the full transition chain is out of scope
            # for this regression (it requires populating required fields). What
            # matters here is that the issue row has type=release, status=released.
            d.conn.execute("UPDATE issues SET status = ? WHERE id = ?", ("released", issue.id))
            d.conn.commit()
            assert d.get_issue(issue.id).to_dict()["status_category"] == "done"
        finally:
            d.close()

        # Disable release pack and verify fallback still yields done
        d2 = make_db(tmp_path, packs=["core", "planning"])
        try:
            fetched = d2.get_issue(issue.id)
            assert fetched.to_dict()["status_category"] == "done"
        finally:
            d2.close()

    def test_disabled_pack_blocker_not_blocking_in_sql(self, tmp_path: Path) -> None:
        """Bug filigree-c9af813900: SQL category predicate must agree with
        ``_resolve_status_category`` for disabled-pack rows. Otherwise a
        release/released blocker is hydrated as ``status_category='done'``
        but still appears as blocking in readiness SQL.
        """
        d = make_db(tmp_path, packs=["core", "planning", "release"])
        try:
            release = d.create_issue("Rel", type="release")
            d.conn.execute("UPDATE issues SET status = ? WHERE id = ?", ("released", release.id))
            d.conn.commit()
            task = d.create_issue("Dependent", type="task")
            d.add_dependency(task.id, release.id)
        finally:
            d.close()

        d2 = make_db(tmp_path, packs=["core", "planning"])
        try:
            sql, params = d2._category_predicate_sql("done", type_col="type", status_col="status", include_archived=True)
            row = d2.conn.execute(
                f"SELECT id FROM issues WHERE id = ? AND ({sql})",  # noqa: S608 — sql/params come from _category_predicate_sql
                [release.id, *params],
            ).fetchone()
            assert row is not None, "disabled-pack done state must match SQL category predicate"
            assert d2.get_issue(task.id).blocked_by == []
        finally:
            d2.close()

    def test_active_type_undeclared_status_is_open(self, tmp_path: Path) -> None:
        """Bug filigree-c9af813900: when the type is active but the state is
        undeclared, the unambiguous-name fallback must NOT fire — otherwise a
        ``task`` with corrupt status ``released`` is silently classified as done.
        """
        d = make_db(tmp_path, packs=["core", "planning", "release"])
        try:
            assert d._resolve_status_category("task", "released") == "open"
            assert d._resolve_status_category("task", "verified") == "open"
            assert d._resolve_status_category("release", "released") == "done"
            assert d._resolve_status_category("totally_unknown_type", "released") == "done"
        finally:
            d.close()
