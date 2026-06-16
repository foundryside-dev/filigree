"""Unit-level pins for safe_message parity on claim/transition errors.

filigree-d25e75cebf: ``ClaimConflictError`` and ``InvalidTransitionError``
gain the same ``safe_message`` mechanism ``WrongProjectError`` already has —
HTTP/MCP error *strings* are generic, while the structured recovery details
(assignee, current state, allowed transitions) stay in the ``details`` /
``TransitionError`` payload so agents can still self-correct. CLI keeps the
rich operator string (``str(exc)``).

These tests pin the api.py primitives; cross-surface behaviour lives in
tests/util/test_cross_surface_parity.py.
"""

from __future__ import annotations

from filigree.types.api import (
    ClaimConflictError,
    ErrorCode,
    InvalidTransitionError,
    TransitionMode,
    claim_conflict_details,
    claim_conflict_envelope,
    invalid_transition_details,
    safe_error_message,
)


class TestClaimConflictSafeMessage:
    def test_safe_message_is_generic_and_omits_assignees(self) -> None:
        exc = ClaimConflictError("proj-1", observed="agent-a", expected="agent-b")
        assert exc.safe_message == ClaimConflictError.SAFE_MESSAGE
        # The rich str() names the assignees; the safe form must not.
        assert "agent-a" in str(exc)
        assert "agent-a" not in exc.safe_message
        assert "agent-b" not in exc.safe_message
        assert exc.safe_message != str(exc)

    def test_envelope_default_is_rich(self) -> None:
        exc = ClaimConflictError("proj-1", observed="agent-a", expected="agent-b")
        env = claim_conflict_envelope(exc)
        assert env["error"] == str(exc)
        assert env["code"] == ErrorCode.CONFLICT
        assert env["details"] == {"issue_id": "proj-1", "observed": "agent-a", "expected": "agent-b"}

    def test_envelope_safe_uses_safe_message_but_keeps_details(self) -> None:
        exc = ClaimConflictError("proj-1", observed="agent-a", expected="agent-b")
        env = claim_conflict_envelope(exc, safe=True)
        assert env["error"] == ClaimConflictError.SAFE_MESSAGE
        assert env["code"] == ErrorCode.CONFLICT
        # Coordination data (assignees) is retained for agent self-correction.
        assert env["details"] == claim_conflict_details(exc)
        assert env["details"] == {"issue_id": "proj-1", "observed": "agent-a", "expected": "agent-b"}


class TestInvalidTransitionSafeMessage:
    def test_safe_message_is_generic_and_omits_states(self) -> None:
        exc = InvalidTransitionError("bug", "triage", to_state="done")
        assert exc.safe_message == InvalidTransitionError.SAFE_MESSAGE
        assert "triage" in str(exc)
        assert "triage" not in exc.safe_message
        assert "done" not in exc.safe_message
        assert exc.safe_message != str(exc)

    def test_details_always_carry_state_even_without_valid_transitions(self) -> None:
        # No valid_transitions enrichment: details must still carry the state
        # so swapping the wire string for safe_message doesn't strip recovery.
        exc = InvalidTransitionError("bug", "triage", to_state="done")
        details = invalid_transition_details(exc)
        assert details is not None
        assert details["current_status"] == "triage"
        assert details["type_name"] == "bug"
        assert details["to_state"] == "done"
        assert "valid_transitions" not in details

    def test_details_include_valid_transitions_when_enriched(self) -> None:
        hints = [{"to": "confirmed", "category": "open"}]
        exc = InvalidTransitionError("bug", "triage", to_state="done", valid_transitions=hints)
        details = invalid_transition_details(exc)
        assert details is not None
        assert details["valid_transitions"] == hints
        assert details["current_status"] == "triage"

    def test_details_omit_to_state_when_absent(self) -> None:
        exc = InvalidTransitionError("bug", "triage")
        details = invalid_transition_details(exc)
        assert details is not None
        assert "to_state" not in details
        assert details["current_status"] == "triage"

    def test_details_carry_next_action_for_non_startable(self) -> None:
        # The start_work "move it to X first" hop must survive genericisation.
        exc = InvalidTransitionError("bug", "triage", next_action="confirmed")
        details = invalid_transition_details(exc)
        assert details is not None
        assert details["next_action"] == "confirmed"

    def test_details_carry_missing_fields(self) -> None:
        exc = InvalidTransitionError("bug", "confirmed", to_state="fixing", missing_fields=["severity"])
        details = invalid_transition_details(exc)
        assert details is not None
        assert details["missing_fields"] == ["severity"]

    def test_details_omit_recovery_hints_when_absent(self) -> None:
        exc = InvalidTransitionError("bug", "triage", to_state="fixing")
        details = invalid_transition_details(exc)
        assert details is not None
        assert "next_action" not in details
        assert "missing_fields" not in details

    def test_backward_mode_still_safe(self) -> None:
        exc = InvalidTransitionError("bug", "done", to_state="triage", mode=TransitionMode.BACKWARD)
        assert exc.safe_message == InvalidTransitionError.SAFE_MESSAGE

    def test_details_none_for_non_transition_error(self) -> None:
        assert invalid_transition_details(ValueError("nope")) is None


class TestSafeErrorMessageHelper:
    def test_claim_conflict_maps_to_safe(self) -> None:
        exc = ClaimConflictError("proj-1", observed="agent-a", expected="agent-b")
        assert safe_error_message(exc) == ClaimConflictError.SAFE_MESSAGE

    def test_invalid_transition_maps_to_safe(self) -> None:
        exc = InvalidTransitionError("bug", "triage", to_state="done")
        assert safe_error_message(exc) == InvalidTransitionError.SAFE_MESSAGE

    def test_plain_value_error_passes_through(self) -> None:
        exc = ValueError("priority must be between 0 and 4")
        assert safe_error_message(exc) == "priority must be between 0 and 4"
