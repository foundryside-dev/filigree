"""CLI tests for composed operations: start-work and start-next-work (Phase E4).

These commands wrap FiligreeDB.start_work / start_next_work (D6) and must
mirror the MCP handler shapes exactly.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from filigree.cli import cli
from filigree.types.api import AmbiguousTransitionError
from tests.cli.conftest import _extract_id


class TestStartWorkCli:
    def test_happy_path_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """start-work --json returns a full public issue with status and assignee set."""
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Work item"])
        issue_id = _extract_id(r.output)

        result = runner.invoke(cli, ["start-work", issue_id, "--assignee", "alice", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["issue_id"] == issue_id
        assert "id" not in data
        assert data["assignee"] == "alice"
        # Default task type canonical wip status
        assert data["status"] == "in_progress"
        # Full public issue keys present
        assert "title" in data
        assert "type" in data
        assert "priority" in data

    def test_start_work_returns_conflict_for_already_claimed_issue(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """start-work --json must emit ErrorCode.CONFLICT (not VALIDATION) for a claim race.

        The optimistic-lock check raises ``ClaimConflictError`` (a ``ValueError``
        subclass). Catching it as a generic ValueError previously misclassified
        the race as VALIDATION; JSON consumers branching on ``code`` could no
        longer distinguish "another agent holds this" from "bad input."
        """
        runner, _ = cli_in_project
        created = runner.invoke(cli, ["create", "Race for ownership"])
        assert created.exit_code == 0
        issue_id = _extract_id(created.output)
        runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-holder"])

        result = runner.invoke(cli, ["start-work", issue_id, "--assignee", "agent-challenger", "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "CONFLICT"
        assert data["details"] == {
            "issue_id": issue_id,
            "observed": "agent-holder",
            "expected": "agent-challenger",
        }

    def test_happy_path_with_target_status(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """--target-status lets caller override the canonical wip status."""
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Work item with target"])
        issue_id = _extract_id(r.output)

        result = runner.invoke(
            cli,
            ["start-work", issue_id, "--assignee", "bob", "--target-status", "in_progress", "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "in_progress"
        assert data["assignee"] == "bob"

    def test_confirmed_bug_defaults_to_reachable_fixing(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Bug has two wip states, but confirmed can only enter fixing."""
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Bug work item", "--type", "bug", "--field", "severity=major"])
        issue_id = _extract_id(r.output)
        update = runner.invoke(cli, ["update", issue_id, "--status", "confirmed", "--json"])
        assert update.exit_code == 0, update.output

        result = runner.invoke(cli, ["start-work", issue_id, "--assignee", "bug-bot", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["issue_id"] == issue_id
        assert data["assignee"] == "bug-bot"
        assert data["status"] == "fixing"

    def test_fresh_bug_default_reports_no_reachable_wip(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Fresh triage bugs need a state-specific error, not type-level ambiguity."""
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Fresh bug work item", "--type", "bug"])
        issue_id = _extract_id(r.output)

        result = runner.invoke(cli, ["start-work", issue_id, "--assignee", "bug-bot", "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "INVALID_TRANSITION"
        # filigree-406e6b7ee0: the error names the source state and the
        # intermediate status the agent must move through first.
        assert "triage" in data["error"]
        assert "not directly startable" in data["error"]
        assert "confirmed" in data["error"]
        show = runner.invoke(cli, ["show", issue_id, "--json"])
        assert show.exit_code == 0, show.output
        current = json.loads(show.output)
        assert current["assignee"] == ""
        assert current["status"] == "triage"

    def test_fresh_bug_advance_walks_to_wip(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """filigree-406e6b7ee0 Part 2: --advance walks triage->confirmed->fixing."""
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Fresh bug to advance", "--type", "bug"])
        issue_id = _extract_id(r.output)

        result = runner.invoke(cli, ["start-work", issue_id, "--assignee", "bug-bot", "--advance", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "fixing"
        assert data["assignee"] == "bug-bot"

    def test_plain_text_output(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Without --json, emits 'Started work on ...' message on stdout."""
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Plain text work"])
        issue_id = _extract_id(r.output)

        result = runner.invoke(cli, ["start-work", issue_id, "--assignee", "carol"])
        assert result.exit_code == 0
        assert "Started work on" in result.output
        assert issue_id in result.output
        assert "status=" in result.output
        assert "assignee=" in result.output

    def test_unknown_issue_not_found_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Unknown issue_id emits NOT_FOUND envelope and exit 1."""
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["start-work", "test-deadbeef00", "--assignee", "dan", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "NOT_FOUND"
        assert "not found" in data["error"].lower() or "deadbeef" in data["error"]

    def test_unknown_issue_plain_text(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Unknown issue_id exits 1 with error on stderr (plain text)."""
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["start-work", "test-deadbeef00", "--assignee", "dan"])
        assert result.exit_code == 1

    def test_invalid_transition_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """An invalid target_status produces INVALID_TRANSITION envelope."""
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Transition test"])
        issue_id = _extract_id(r.output)

        result = runner.invoke(
            cli,
            ["start-work", issue_id, "--assignee", "erin", "--target-status", "nonexistent_status", "--json"],
        )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "INVALID_TRANSITION"
        assert "error" in data

    def test_invalid_target_status_includes_valid_transitions(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Typed InvalidTransitionError still carries transition hints."""
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Fresh bug", "--type", "bug", "--field", "severity=major"])
        issue_id = _extract_id(r.output)

        result = runner.invoke(
            cli,
            ["start-work", issue_id, "--assignee", "erin", "--target-status", "fixing", "--json"],
        )

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "INVALID_TRANSITION"
        assert {t["to"] for t in data["valid_transitions"]} == {"confirmed", "wont_fix", "not_a_bug"}
        assert data["hint"] == "Use 'filigree transitions <id>' to see allowed state changes"

    def test_ambiguous_transition_includes_candidate_details(self, monkeypatch) -> None:
        class FakeDB:
            def __enter__(self) -> FakeDB:
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                pass

            def start_work(self, *_args: object, **_kwargs: object) -> object:
                raise AmbiguousTransitionError("custom", ["review", "revise"], current_status="open")

        monkeypatch.setattr("filigree.cli_commands.issues.get_db", lambda: FakeDB())
        runner = CliRunner()

        result = runner.invoke(cli, ["start-work", "test-1234567890", "--assignee", "erin", "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "INVALID_TRANSITION"
        assert data["details"] == {"candidates": ["review", "revise"], "current_status": "open", "type": "custom"}

    def test_actor_defaults_to_assignee(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """When --actor is omitted, the audit-trail actor should be the assignee.

        We verify by reading events after the command and checking that the
        'claimed' event has actor == assignee.
        """
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Actor default test"])
        issue_id = _extract_id(r.output)

        # Run without --actor; the group-level default is "cli", but
        # start-work must override this with the assignee.
        result = runner.invoke(cli, ["start-work", issue_id, "--assignee", "frank", "--json"])
        assert result.exit_code == 0

        # Check the events for this issue via CLI.
        events_result = runner.invoke(cli, ["events", issue_id, "--json"])
        assert events_result.exit_code == 0
        events_data = json.loads(events_result.output)
        events = events_data.get("items", []) if isinstance(events_data, dict) else events_data
        claimed_events = [e for e in events if e.get("event_type") == "claimed"]
        assert claimed_events, f"No 'claimed' event found; all events: {events}"
        assert claimed_events[0]["actor"] == "frank"

    def test_blank_assignee_validation_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Blank assignee emits VALIDATION envelope (not a server error)."""
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Blank assignee test"])
        issue_id = _extract_id(r.output)

        result = runner.invoke(cli, ["start-work", issue_id, "--assignee", "   ", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION"


class TestStartNextWorkCli:
    def test_happy_path_claims_highest_priority(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """start-next-work claims the highest-priority ready issue."""
        runner, _ = cli_in_project

        # Use --type task to avoid auto-seeded release issues (which have
        # ambiguous wip statuses and would cause INVALID_TRANSITION).
        runner.invoke(cli, ["create", "Low priority task", "-p", "4", "--type", "task"])
        r_high = runner.invoke(cli, ["create", "High priority task", "-p", "0", "--type", "task"])
        high_id = _extract_id(r_high.output)

        result = runner.invoke(cli, ["start-next-work", "--assignee", "grace", "--type", "task", "--json"])
        assert result.exit_code == 0, f"Expected exit 0, got {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["assignee"] == "grace"
        assert data["status"] == "in_progress"
        assert data["issue_id"] == high_id

    def test_no_match_returns_empty_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """When no issues match, emits empty envelope with exit 0."""
        runner, _ = cli_in_project
        # Create a task but filter for a nonexistent type
        runner.invoke(cli, ["create", "A task", "--type", "task"])

        result = runner.invoke(cli, ["start-next-work", "--assignee", "henry", "--type", "nonexistent_type", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "empty"
        assert data["reason"] == "No ready issues matching filters"

    def test_no_match_plain_text(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """When no issues match, plain-text mode prints message to stdout and exits 0."""
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "A task", "--type", "task"])

        result = runner.invoke(cli, ["start-next-work", "--assignee", "ivan", "--type", "nonexistent_type"])
        assert result.exit_code == 0
        assert "No ready issues matching filters" in result.output

    def test_priority_max_filter(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """--priority-max skips issues with priority > max (numerically).

        Uses --type task to avoid auto-seeded release issues (ambiguous wip).
        """
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Low-pri (P4)", "-p", "4", "--type", "task"])
        r_med = runner.invoke(cli, ["create", "Med-pri (P2)", "-p", "2", "--type", "task"])
        med_id = _extract_id(r_med.output)

        # --priority-max 2 means P0/P1/P2 are eligible; P4 is not.
        result = runner.invoke(
            cli,
            [
                "start-next-work",
                "--assignee",
                "judy",
                "--type",
                "task",
                "--priority-max",
                "2",
                "--target-status",
                "in_progress",
                "--json",
            ],
        )
        assert result.exit_code == 0, f"Expected exit 0, got {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        # Must have claimed med_id (P2), not low_id (P4)
        assert data["issue_id"] == med_id
        assert data["assignee"] == "judy"

    def test_type_filter(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """--type filters candidates to matching issue type only.

        Uses 'task' (unique wip) with --target-status to avoid ambiguity.
        Creates two tasks and verifies the claimed issue has type='task'.
        """
        runner, _ = cli_in_project
        r_task = runner.invoke(cli, ["create", "A task to claim", "--type", "task", "-p", "1"])
        task_id = _extract_id(r_task.output)
        # Create another task at lower priority — only one should be claimed.
        runner.invoke(cli, ["create", "Another task", "--type", "task", "-p", "3"])

        result = runner.invoke(
            cli,
            ["start-next-work", "--assignee", "kate", "--type", "task", "--target-status", "in_progress", "--json"],
        )
        assert result.exit_code == 0, f"Expected exit 0, got {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["type"] == "task"
        assert data["assignee"] == "kate"
        assert data["issue_id"] == task_id

    def test_happy_path_plain_text(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Plain-text success prints 'Started work on ...' line."""
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Plain text next work", "-p", "0", "--type", "task"])

        result = runner.invoke(cli, ["start-next-work", "--assignee", "lena", "--type", "task", "--target-status", "in_progress"])
        assert result.exit_code == 0
        assert "Started work on" in result.output
        assert "status=" in result.output
        assert "assignee=" in result.output

    def test_blank_assignee_validation_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Blank assignee emits VALIDATION envelope (parity with MCP)."""
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["start-next-work", "--assignee", "   ", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION"

    def test_invalid_target_status_classified_as_invalid_transition_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """start-next-work with bogus --target-status emits INVALID_TRANSITION.

        Mirrors start-work's classify_value_error handling — sibling commands
        must agree on error codes for the same class of failure.
        Regression for filigree-eed112d722.
        """
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Some task", "-p", "0", "--type", "task"])
        result = runner.invoke(
            cli,
            [
                "start-next-work",
                "--assignee",
                "alice",
                "--type",
                "task",
                "--target-status",
                "nonexistent_status",
                "--json",
            ],
        )
        assert result.exit_code == 1, result.output
        data = json.loads(result.output)
        assert data["code"] == "INVALID_TRANSITION", data


class TestComposeActorSanitization:
    """Regression for filigree-d9fae9d8f0:

    The composed start-work / start-next-work commands declare a local
    ``--actor`` option that bypassed sanitize_actor — blank/control/overlong
    values were previously persisted to the audit trail.
    """

    def test_start_work_blank_actor_rejected_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Actor blank target"])
        issue_id = _extract_id(r.output)

        result = runner.invoke(cli, ["start-work", issue_id, "--assignee", "alice", "--actor", "   ", "--json"])
        assert result.exit_code == 1, result.output
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION", data
        assert "actor" in data["error"].lower()

    def test_start_work_control_char_actor_rejected_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Actor control target"])
        issue_id = _extract_id(r.output)

        result = runner.invoke(cli, ["start-work", issue_id, "--assignee", "alice", "--actor", "bad\nactor", "--json"])
        assert result.exit_code == 1, result.output
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION", data

    def test_start_next_work_blank_actor_rejected_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Some task", "-p", "0", "--type", "task"])

        result = runner.invoke(
            cli,
            [
                "start-next-work",
                "--assignee",
                "alice",
                "--type",
                "task",
                "--actor",
                "   ",
                "--json",
            ],
        )
        assert result.exit_code == 1, result.output
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION", data

    def test_start_work_overlong_actor_rejected_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Actor overlong target"])
        issue_id = _extract_id(r.output)

        long_actor = "a" * 200
        result = runner.invoke(cli, ["start-work", issue_id, "--assignee", "alice", "--actor", long_actor, "--json"])
        assert result.exit_code == 1, result.output
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION", data
