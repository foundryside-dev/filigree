"""Smoke tests verifying verb-noun alias parity for Phase E3 aliases.

Each test invokes both the short form and the new alias with --json and
asserts identical exit code and output.  For commands that mutate state,
the test seeds two independent issues so neither invocation sees the
state left by the other.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from filigree.cli import cli

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _create_issue(runner: CliRunner, title: str = "Alias test issue") -> str:
    """Create an issue and return its ID."""
    result = runner.invoke(cli, ["create", title, "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["issue_id"]


# ---------------------------------------------------------------------------
# planning.py aliases
# ---------------------------------------------------------------------------


class TestPlanningAliases:
    """Alias parity for planning commands."""

    def test_ready_get_ready_parity(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Both `ready` and `get-ready` produce identical exit code and JSON."""
        runner, _ = cli_in_project
        out_short = runner.invoke(cli, ["ready", "--json"])
        out_alias = runner.invoke(cli, ["get-ready", "--json"])
        assert out_short.exit_code == out_alias.exit_code == 0
        assert json.loads(out_short.output) == json.loads(out_alias.output)

    def test_blocked_get_blocked_parity(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Both `blocked` and `get-blocked` produce identical exit code and JSON."""
        runner, _ = cli_in_project
        out_short = runner.invoke(cli, ["blocked", "--json"])
        out_alias = runner.invoke(cli, ["get-blocked", "--json"])
        assert out_short.exit_code == out_alias.exit_code == 0
        assert json.loads(out_short.output) == json.loads(out_alias.output)

    def test_plan_get_plan_parity(self, cli_in_project: tuple[CliRunner, Path], tmp_path: Path) -> None:
        """Both `plan` and `get-plan` produce identical exit code and JSON for a milestone."""
        runner, _ = cli_in_project
        plan_json = json.dumps(
            {
                "milestone": {"title": "M1"},
                "phases": [{"title": "Phase A", "steps": [{"title": "Step 1"}]}],
            }
        )
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(plan_json)

        create_result = runner.invoke(cli, ["create-plan", "--file", str(plan_path), "--json"])
        assert create_result.exit_code == 0, create_result.output
        milestone_id = json.loads(create_result.output)["milestone"]["id"]

        out_short = runner.invoke(cli, ["plan", milestone_id, "--json"])
        out_alias = runner.invoke(cli, ["get-plan", milestone_id, "--json"])
        assert out_short.exit_code == out_alias.exit_code == 0
        assert json.loads(out_short.output) == json.loads(out_alias.output)

    def test_critical_path_get_critical_path_parity(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Both `critical-path` and `get-critical-path` produce identical JSON."""
        runner, _ = cli_in_project
        out_short = runner.invoke(cli, ["critical-path", "--json"])
        out_alias = runner.invoke(cli, ["get-critical-path", "--json"])
        assert out_short.exit_code == out_alias.exit_code == 0
        assert json.loads(out_short.output) == json.loads(out_alias.output)

    def test_changes_get_changes_parity(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Both `changes` and `get-changes` produce identical JSON."""
        runner, _ = cli_in_project
        since = "2000-01-01T00:00:00"
        out_short = runner.invoke(cli, ["changes", "--since", since, "--json"])
        out_alias = runner.invoke(cli, ["get-changes", "--since", since, "--json"])
        assert out_short.exit_code == out_alias.exit_code == 0
        assert json.loads(out_short.output) == json.loads(out_alias.output)


# ---------------------------------------------------------------------------
# workflow.py aliases
# ---------------------------------------------------------------------------


class TestWorkflowAliases:
    """Alias parity for workflow commands."""

    def test_types_list_types_parity(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        out_short = runner.invoke(cli, ["types", "--json"])
        out_alias = runner.invoke(cli, ["list-types", "--json"])
        assert out_short.exit_code == out_alias.exit_code == 0
        assert json.loads(out_short.output) == json.loads(out_alias.output)

    def test_type_info_get_type_info_parity(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        out_short = runner.invoke(cli, ["type-info", "task", "--json"])
        out_alias = runner.invoke(cli, ["get-type-info", "task", "--json"])
        assert out_short.exit_code == out_alias.exit_code == 0
        assert json.loads(out_short.output) == json.loads(out_alias.output)

    def test_transitions_get_valid_transitions_parity(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        issue_id = _create_issue(runner)
        out_short = runner.invoke(cli, ["transitions", issue_id, "--json"])
        out_alias = runner.invoke(cli, ["get-valid-transitions", issue_id, "--json"])
        assert out_short.exit_code == out_alias.exit_code == 0
        assert json.loads(out_short.output) == json.loads(out_alias.output)

    def test_packs_list_packs_parity(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        out_short = runner.invoke(cli, ["packs", "--json"])
        out_alias = runner.invoke(cli, ["list-packs", "--json"])
        assert out_short.exit_code == out_alias.exit_code == 0
        assert json.loads(out_short.output) == json.loads(out_alias.output)

    def test_validate_validate_issue_parity(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        issue_id = _create_issue(runner)
        out_short = runner.invoke(cli, ["validate", issue_id, "--json"])
        out_alias = runner.invoke(cli, ["validate-issue", issue_id, "--json"])
        assert out_short.exit_code == out_alias.exit_code == 0
        assert json.loads(out_short.output) == json.loads(out_alias.output)

    def test_guide_get_workflow_guide_parity(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        out_short = runner.invoke(cli, ["guide", "core", "--json"])
        out_alias = runner.invoke(cli, ["get-workflow-guide", "core", "--json"])
        assert out_short.exit_code == out_alias.exit_code == 0
        assert json.loads(out_short.output) == json.loads(out_alias.output)

    def test_workflow_statuses_get_workflow_statuses_parity(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        out_short = runner.invoke(cli, ["workflow-statuses", "--json"])
        out_alias = runner.invoke(cli, ["get-workflow-statuses", "--json"])
        assert out_short.exit_code == out_alias.exit_code == 0
        assert json.loads(out_short.output) == json.loads(out_alias.output)


# ---------------------------------------------------------------------------
# meta.py aliases
# ---------------------------------------------------------------------------


class TestMetaAliases:
    """Alias parity for metadata commands."""

    def test_labels_list_labels_parity(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        out_short = runner.invoke(cli, ["labels", "--json"])
        out_alias = runner.invoke(cli, ["list-labels", "--json"])
        assert out_short.exit_code == out_alias.exit_code == 0
        assert json.loads(out_short.output) == json.loads(out_alias.output)

    def test_taxonomy_get_label_taxonomy_parity(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        out_short = runner.invoke(cli, ["taxonomy", "--json"])
        out_alias = runner.invoke(cli, ["get-label-taxonomy", "--json"])
        assert out_short.exit_code == out_alias.exit_code == 0
        assert json.loads(out_short.output) == json.loads(out_alias.output)

    def test_events_get_issue_events_parity(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        issue_id = _create_issue(runner)
        out_short = runner.invoke(cli, ["events", issue_id, "--json"])
        out_alias = runner.invoke(cli, ["get-issue-events", issue_id, "--json"])
        assert out_short.exit_code == out_alias.exit_code == 0
        assert json.loads(out_short.output) == json.loads(out_alias.output)


# ---------------------------------------------------------------------------
# issues.py aliases
# ---------------------------------------------------------------------------


class TestIssueAliases:
    """Alias parity for issue CRUD commands."""

    def test_show_get_issue_parity(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        issue_id = _create_issue(runner)
        out_short = runner.invoke(cli, ["show", issue_id, "--json"])
        out_alias = runner.invoke(cli, ["get-issue", issue_id, "--json"])
        assert out_short.exit_code == out_alias.exit_code == 0
        # Structural parity (both are the same issue dict)
        d_short = json.loads(out_short.output)
        d_alias = json.loads(out_alias.output)
        assert d_short["issue_id"] == d_alias["issue_id"] == issue_id
        assert d_short["title"] == d_alias["title"]

    def test_list_list_issues_parity(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        _create_issue(runner, "List parity issue")
        out_short = runner.invoke(cli, ["list", "--json"])
        out_alias = runner.invoke(cli, ["list-issues", "--json"])
        assert out_short.exit_code == out_alias.exit_code == 0
        d_short = json.loads(out_short.output)
        d_alias = json.loads(out_alias.output)
        assert d_short["has_more"] == d_alias["has_more"]
        assert len(d_short["items"]) == len(d_alias["items"])
        # IDs must match (same order since same DB state)
        short_ids = [i["issue_id"] for i in d_short["items"]]
        alias_ids = [i["issue_id"] for i in d_alias["items"]]
        assert short_ids == alias_ids

    def test_update_update_issue_parity(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """update and update-issue produce identical JSON when updating description."""
        runner, _ = cli_in_project
        # Seed two independent issues for the two invocations
        id1 = _create_issue(runner, "Update short form")
        id2 = _create_issue(runner, "Update alias form")
        out_short = runner.invoke(cli, ["update", id1, "--description", "desc-short", "--json"])
        out_alias = runner.invoke(cli, ["update-issue", id2, "--description", "desc-alias", "--json"])
        assert out_short.exit_code == out_alias.exit_code == 0
        d_short = json.loads(out_short.output)
        d_alias = json.loads(out_alias.output)
        # Both should return an issue dict with the same structural keys
        assert set(d_short.keys()) == set(d_alias.keys())
        assert d_short["status"] == d_alias["status"]
        assert d_short["type"] == d_alias["type"]

    def test_release_release_claim_parity(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """release and release-claim produce the same envelopes: the no-op on an
        unclaimed issue and the holder-check CONFLICT on a peer's claim."""
        runner, _ = cli_in_project
        id1 = _create_issue(runner, "Release short form")
        id2 = _create_issue(runner, "Release alias form")
        out_short = runner.invoke(cli, ["--actor", "agent-1", "release", id1, "--json"])
        out_alias = runner.invoke(cli, ["--actor", "agent-1", "release-claim", id2, "--json"])
        assert out_short.exit_code == out_alias.exit_code == 0
        assert json.loads(out_short.output) == json.loads(out_alias.output) == {"result": "no_op", "reason": "not_claimed"}

        runner.invoke(cli, ["claim", id1, "--assignee", "agent-2"])
        runner.invoke(cli, ["claim", id2, "--assignee", "agent-2"])
        out_short = runner.invoke(cli, ["--actor", "agent-1", "release", id1, "--json"])
        out_alias = runner.invoke(cli, ["--actor", "agent-1", "release-claim", id2, "--json"])
        assert out_short.exit_code == out_alias.exit_code == 1
        d_short = json.loads(out_short.output)
        d_alias = json.loads(out_alias.output)
        assert d_short["code"] == d_alias["code"] == "CONFLICT"
        assert set(d_short["details"]) == set(d_alias["details"])

    def test_undo_undo_last_parity(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """undo and undo-last share --expected-event-id semantics: the no-op on a
        fresh issue, CONFLICT on a stale event id, success on the right one."""
        runner, _ = cli_in_project
        id1 = _create_issue(runner, "Undo short form")
        id2 = _create_issue(runner, "Undo alias form")
        out_short = runner.invoke(cli, ["undo", id1, "--expected-event-id", "1", "--json"])
        out_alias = runner.invoke(cli, ["undo-last", id2, "--expected-event-id", "1", "--json"])
        assert out_short.exit_code == out_alias.exit_code == 0
        assert json.loads(out_short.output) == json.loads(out_alias.output) == {"result": "no_op", "reason": "no_reversible_event"}

        for issue_id in (id1, id2):
            runner.invoke(cli, ["update", issue_id, "--title", "renamed"])
        stale_short = runner.invoke(cli, ["undo", id1, "--expected-event-id", "1", "--json"])
        stale_alias = runner.invoke(cli, ["undo-last", id2, "--expected-event-id", "1", "--json"])
        assert stale_short.exit_code == stale_alias.exit_code == 1
        d_short = json.loads(stale_short.output)
        d_alias = json.loads(stale_alias.output)
        assert d_short["code"] == d_alias["code"] == "CONFLICT"

        ok_short = runner.invoke(cli, ["undo", id1, "--expected-event-id", str(d_short["details"]["latest_event_id"]), "--json"])
        ok_alias = runner.invoke(cli, ["undo-last", id2, "--expected-event-id", str(d_alias["details"]["latest_event_id"]), "--json"])
        assert ok_short.exit_code == ok_alias.exit_code == 0
        assert json.loads(ok_short.output)["undone"] is json.loads(ok_alias.output)["undone"] is True
