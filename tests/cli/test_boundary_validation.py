"""CLI boundary validation tests for priority and actor."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from filigree.cli import cli
from filigree.types.api import ErrorCode
from tests.cli.conftest import _extract_id


class TestCLIPriorityValidation:
    """click.IntRange(0, 4) on all priority options."""

    def test_create_priority_too_high(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create", "Test", "--priority", "5"])
        assert result.exit_code != 0

    def test_create_priority_too_low(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create", "Test", "--priority", "-1"])
        assert result.exit_code != 0

    def test_create_priority_boundary_0(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create", "Test", "--priority", "0"])
        assert result.exit_code == 0
        assert "Created" in result.output

    def test_create_priority_boundary_4(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create", "Test", "--priority", "4"])
        assert result.exit_code == 0
        assert "Created" in result.output

    def test_list_priority_filter_too_high(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list", "--priority", "5"])
        assert result.exit_code != 0

    def test_update_priority_too_high(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        create_result = runner.invoke(cli, ["create", "Target"])
        assert create_result.exit_code == 0
        issue_id = _extract_id(create_result.output)
        result = runner.invoke(cli, ["update", issue_id, "--priority", "5"])
        assert result.exit_code != 0

    def test_claim_next_priority_min_too_low(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["claim-next", "--assignee", "bot", "--priority-min", "-1"])
        assert result.exit_code != 0

    def test_claim_next_priority_max_too_high(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["claim-next", "--assignee", "bot", "--priority-max", "5"])
        assert result.exit_code != 0

    def test_batch_update_priority_too_high(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        create_result = runner.invoke(cli, ["create", "Target"])
        issue_id = _extract_id(create_result.output)
        result = runner.invoke(cli, ["batch-update", issue_id, "--priority", "5"])
        assert result.exit_code != 0


class TestCLIActorValidation:
    """Actor validation in CLI group callback."""

    def test_empty_actor(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["--actor", "", "create", "Test"])
        assert result.exit_code != 0

    def test_control_char_actor(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["--actor", "\x00bad", "create", "Test"])
        assert result.exit_code != 0

    def test_valid_actor(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["--actor", "my-bot", "create", "Test"])
        assert result.exit_code == 0
        assert "Created" in result.output

    def test_default_actor_cli(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Default actor 'cli' should pass validation."""
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create", "Test"])
        assert result.exit_code == 0

    def test_nested_command_json_actor_error_emits_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Nested command --json must be visible to group-level actor validation."""
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["--actor", "   ", "templates", "reload", "--json"])

        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["code"] == ErrorCode.VALIDATION
        assert "actor must not be empty" in payload["error"]


class TestCLIPriorityEnvelopeEmission:
    """2b.3a: --priority out-of-range with --json emits the 2.0 flat envelope.

    The existing cross-surface parity scenario covers the high end (99) on
    ``create``. These tests pin the low-end boundary (-1) and keep the envelope
    shape explicit — a regression that reverted ``_range_check_priority`` to
    a Click callback or IntRange type would emit Click's stderr usage error
    instead of the 2.0 envelope, and existing exit-code-only tests would
    still pass.
    """

    def test_create_priority_neg1_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create", "Bad", "--priority", "-1", "--json"])
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload["code"] == ErrorCode.VALIDATION
        assert "-1" in payload["error"]

    def test_create_priority_5_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create", "Bad", "--priority", "5", "--json"])
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload["code"] == ErrorCode.VALIDATION


class TestIssuePriorityFilterEnvelopeEmission:
    """filigree-ff324e6a96: bounded ``--priority*`` options must emit the 2.0 envelope.

    ``list``, ``update``, ``claim-next`` and ``start-next-work`` previously declared
    ``--priority``/``--priority-min``/``--priority-max`` as ``click.IntRange(0, 4)``,
    which causes Click to fail with a ``BadParameter`` plain-text usage error
    *before* the command body runs — bypassing the unified ``{"error", "code"}``
    envelope contract (Phase E §9). The fix moves range validation into the
    command body via ``_range_check_int``, mirroring what ``create`` already
    does for the same reason. These tests pin both that the error code is
    ``VALIDATION`` and that the exit code is 1 (envelope path), not 2 (Click
    usage error path).
    """

    @staticmethod
    def _assert_validation_envelope(result: Result) -> None:
        assert result.exit_code != 0, result.output
        assert result.exit_code != 2, f"got Click usage error, not envelope: {result.output}"
        payload = json.loads(result.output)
        assert payload["code"] == ErrorCode.VALIDATION, payload

    def test_list_priority_high_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list", "--priority", "5", "--json"])
        self._assert_validation_envelope(result)

    def test_list_priority_neg1_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list", "--priority", "-1", "--json"])
        self._assert_validation_envelope(result)

    def test_list_issues_alias_priority_high_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list-issues", "--priority", "5", "--json"])
        self._assert_validation_envelope(result)

    def test_list_limit_negative_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list", "--limit", "-1", "--json"])
        self._assert_validation_envelope(result)

    def test_list_limit_huge_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list", "--limit", "9223372036854775807", "--json"])
        self._assert_validation_envelope(result)

    def test_list_issues_offset_negative_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list-issues", "--offset", "-1", "--json"])
        self._assert_validation_envelope(result)

    def test_list_issues_offset_huge_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list-issues", "--offset", "9223372036854775808", "--json"])
        self._assert_validation_envelope(result)

    def test_list_files_limit_huge_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list-files", "--limit", "9223372036854775807", "--json"])
        self._assert_validation_envelope(result)

    def test_list_annotations_limit_huge_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list-annotations", "--limit", "9223372036854775807", "--json"])
        self._assert_validation_envelope(result)

    def test_update_priority_high_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        create_result = runner.invoke(cli, ["create", "Target"])
        assert create_result.exit_code == 0
        issue_id = _extract_id(create_result.output)
        result = runner.invoke(cli, ["update", issue_id, "--priority", "5", "--json"])
        self._assert_validation_envelope(result)

    def test_update_issue_alias_priority_high_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        create_result = runner.invoke(cli, ["create", "Target"])
        issue_id = _extract_id(create_result.output)
        result = runner.invoke(cli, ["update-issue", issue_id, "--priority", "5", "--json"])
        self._assert_validation_envelope(result)

    def test_claim_next_priority_min_neg1_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["claim-next", "--assignee", "bot", "--priority-min", "-1", "--json"])
        self._assert_validation_envelope(result)

    def test_claim_next_priority_max_high_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["claim-next", "--assignee", "bot", "--priority-max", "5", "--json"])
        self._assert_validation_envelope(result)

    def test_start_next_work_priority_min_neg1_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["start-next-work", "--assignee", "bot", "--priority-min", "-1", "--json"])
        self._assert_validation_envelope(result)

    def test_start_next_work_priority_max_high_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["start-next-work", "--assignee", "bot", "--priority-max", "5", "--json"])
        self._assert_validation_envelope(result)


class TestOwnershipForeignIDEnvelopeEmission:
    """Foreign project IDs are caller-context validation errors, not claim conflicts."""

    @pytest.mark.parametrize(
        "args",
        [
            ["claim", "other-1234567890", "--assignee", "bot", "--json"],
            ["release", "other-1234567890", "--json"],
            ["heartbeat-work", "other-1234567890", "--json"],
            [
                "reclaim",
                "other-1234567890",
                "--assignee",
                "bot",
                "--expected-assignee",
                "old-bot",
                "--reason",
                "stale claim",
                "--json",
            ],
            ["start-work", "other-1234567890", "--assignee", "bot", "--json"],
        ],
    )
    def test_foreign_issue_id_json_is_validation(
        self,
        cli_in_project: tuple[CliRunner, Path],
        args: list[str],
    ) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, args)

        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["code"] == ErrorCode.VALIDATION, payload
        assert "belongs to project" in payload["error"]


class TestObservationCLIEnvelopeEmission:
    """Observation CLI numeric-range errors emit the 2.0 flat envelope under --json.

    Mirrors ``TestCLIPriorityEnvelopeEmission`` for ``observe``,
    ``list-observations``, and ``promote-observation``: range validation
    must run inside the command body (after ``--json`` has been parsed),
    not as a Click ``IntRange`` type — otherwise Click rejects the value
    before the body runs and emits a stderr usage error with exit 2.
    """

    @staticmethod
    def _assert_validation_envelope(result: Result) -> None:
        assert result.exit_code != 0, result.output
        assert result.exit_code != 2, f"got Click usage error, not envelope: {result.output}"
        payload = json.loads(result.output)
        assert payload["code"] == ErrorCode.VALIDATION, payload

    def test_observe_priority_high_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["observe", "x", "--priority", "9", "--json"])
        self._assert_validation_envelope(result)

    def test_observe_priority_neg1_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["observe", "x", "--priority", "-1", "--json"])
        self._assert_validation_envelope(result)

    def test_observe_line_negative_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["observe", "x", "--line", "-1", "--json"])
        self._assert_validation_envelope(result)

    def test_list_observations_limit_zero_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list-observations", "--limit", "0", "--json"])
        self._assert_validation_envelope(result)

    def test_list_observations_offset_negative_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list-observations", "--offset", "-1", "--json"])
        self._assert_validation_envelope(result)

    def test_list_observations_limit_huge_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list-observations", "--limit", "9223372036854775807", "--json"])
        self._assert_validation_envelope(result)

    def test_list_observations_offset_huge_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list-observations", "--offset", "9223372036854775808", "--json"])
        self._assert_validation_envelope(result)

    def test_list_observations_older_than_hours_huge_json_envelope(
        self, cli_in_project: tuple[CliRunner, Path]
    ) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list-observations", "--older-than-hours", "999999999999999999999", "--json"])
        self._assert_validation_envelope(result)

    def test_promote_observation_priority_high_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["promote-observation", "obs-nope", "--priority", "99", "--json"])
        self._assert_validation_envelope(result)

    def test_promote_observation_priority_neg1_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["promote-observation", "obs-nope", "--priority", "-1", "--json"])
        self._assert_validation_envelope(result)


class TestCLIActorEnvelopeEmission:
    """2b.3b: group-level ``--actor`` validation emits 2.0 envelope for every subcommand.

    The parity module only exercises ``update``, but the ``--actor`` option
    is defined on the Click group in ``src/filigree/cli.py:39`` — the same
    callback runs before every subcommand. These tests pin the envelope for
    the other ``--json``-capable subcommands that rely on ``ctx.obj["actor"]``.
    Without them, a regression that moved the envelope emission into the
    ``update`` body (instead of the group callback) would pass parity and
    the existing exit-code checks, but silently break every other subcommand.
    """

    @staticmethod
    def _assert_actor_envelope(result: Result) -> None:
        assert result.exit_code != 0, result.output
        payload = json.loads(result.output)
        assert payload["code"] == ErrorCode.VALIDATION, payload
        assert "actor" in payload["error"].lower(), payload

    def test_actor_whitespace_close_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["--actor", "   ", "close", "test-ffffffffff", "--json"])
        self._assert_actor_envelope(result)

    def test_actor_whitespace_claim_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["--actor", "   ", "claim", "test-ffffffffff", "--assignee", "bot", "--json"])
        self._assert_actor_envelope(result)

    def test_actor_whitespace_reopen_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["--actor", "   ", "reopen", "test-ffffffffff", "--json"])
        self._assert_actor_envelope(result)

    def test_actor_whitespace_add_comment_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["--actor", "   ", "add-comment", "test-ffffffffff", "note", "--json"])
        self._assert_actor_envelope(result)

    def test_actor_envelope_ignores_double_dash_positional(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Click's `--` terminator means trailing tokens are positional, not flags.

        Regression for filigree-df988a37fc: ``--json`` after ``--`` is the
        ``create`` title, not the JSON-mode flag, so the actor failure must
        surface as a plain Click usage error rather than the JSON envelope.
        """
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["--actor", "   ", "create", "--", "--json"])
        assert result.exit_code != 0
        assert not result.output.strip().startswith("{"), result.output
        assert "Usage:" in result.output or "actor" in result.output.lower(), result.output


class TestCLICloseJSONBatchBoundary:
    """2b.3c: ``close --json`` with N=1 emits flat envelope; N≥2 keeps batch wrapper.

    The boundary is ``len(issue_ids) == 1`` at ``cli_commands/issues.py:330``.
    Flipping ``==`` to ``<=`` (or inverting the condition) would silently drop
    all but the first error on ``close a b --json``. The existing parity test
    only exercises N=1; this pins the other side.
    """

    def test_close_n2_both_failing_keeps_batch_wrapper(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["close", "test-ffffffffff", "test-eeeeeeeeee", "--json"])
        assert result.exit_code != 0, result.output
        payload = json.loads(result.output)
        # Batch-shape wrapper: {succeeded, failed, newly_unblocked} — NOT the flat
        # {error, code} envelope that N=1 produces.
        assert "succeeded" in payload, payload
        assert "failed" in payload, payload
        assert "code" not in payload, f"N≥2 close must keep batch wrapper; flat envelope would have top-level 'code': {payload!r}"
        assert len(payload["failed"]) == 2, payload
        for err in payload["failed"]:
            assert set(err.keys()) >= {"id", "error", "code"}, err
            assert err["code"] == ErrorCode.NOT_FOUND, err

    def test_close_n1_failing_emits_flat_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        # Positive companion to the N=2 test: pins that N=1 still emits the
        # flat envelope. Keeps both sides of the boundary nailed down together
        # so a future refactor has to break both tests to flip the shape.
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["close", "test-ffffffffff", "--json"])
        assert result.exit_code != 0, result.output
        payload = json.loads(result.output)
        assert payload["code"] == ErrorCode.NOT_FOUND, payload
        assert "succeeded" not in payload, payload


class TestCLILabelsTopValidation:
    """filigree-39c410ef92: labels --top must reject negatives (0 is the documented unlimited sentinel)."""

    def test_labels_top_negative_rejected(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["labels", "--top", "-1"])
        assert result.exit_code != 0

    def test_labels_top_zero_accepted(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """0 is the documented unlimited sentinel and must still work."""
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["labels", "--top", "0"])
        assert result.exit_code == 0
