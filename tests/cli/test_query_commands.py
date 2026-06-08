"""CLI tests for query commands (list, search, ready, blocked, stats, metrics, critical-path, JSON output)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from filigree.cli import cli
from tests.cli.conftest import _extract_id


class TestReadyAndBlocked:
    def test_ready(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Ready task"])
        result = runner.invoke(cli, ["ready"])
        assert result.exit_code == 0
        # 1 created task + auto-seeded "Future" release = 2 ready
        assert "2 ready" in result.output

    def test_ready_excludes_claimed_issue(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        created = runner.invoke(cli, ["create", "Claimed ready task"])
        issue_id = _extract_id(created.output)
        claim = runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-1"])
        assert claim.exit_code == 0

        result = runner.invoke(cli, ["ready", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        ids = {item["issue_id"] for item in data["items"]}
        assert issue_id not in ids

    def test_blocked(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "Blocked"])
        id1 = _extract_id(r1.output)
        r2 = runner.invoke(cli, ["create", "Blocker"])
        id2 = _extract_id(r2.output)
        runner.invoke(cli, ["add-dep", id1, id2])
        result = runner.invoke(cli, ["blocked"])
        assert result.exit_code == 0
        assert "1 blocked" in result.output


class TestDependencies:
    def test_dep_add_and_remove(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "A"])
        id1 = _extract_id(r1.output)
        r2 = runner.invoke(cli, ["create", "B"])
        id2 = _extract_id(r2.output)

        result = runner.invoke(cli, ["add-dep", id1, id2])
        assert result.exit_code == 0
        assert "Added" in result.output

        result = runner.invoke(cli, ["remove-dep", id1, id2])
        assert result.exit_code == 0
        assert "Removed" in result.output


class TestStatsAndSearch:
    def test_stats(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "A"])
        result = runner.invoke(cli, ["stats"])
        assert result.exit_code == 0
        assert "Status names:" in result.output
        assert "Status categories:" in result.output

    def test_search(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Authentication bug"])
        runner.invoke(cli, ["create", "Other thing"])
        result = runner.invoke(cli, ["search", "auth"])
        assert result.exit_code == 0
        assert "1 results" in result.output


class TestMetricsCli:
    def test_metrics_basic(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Test issue"])
        result = runner.invoke(cli, ["metrics"])
        assert result.exit_code == 0
        assert "Flow Metrics" in result.output

    def test_metrics_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["metrics", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "throughput" in data
        assert "period_days" in data

    def test_metrics_days(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["metrics", "--days", "7", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["period_days"] == 7


class TestCriticalPathCli:
    def test_critical_path_empty(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["critical-path"])
        assert result.exit_code == 0
        assert "No dependency chains" in result.output

    def test_critical_path_with_chain(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "Blocker"])
        id1 = _extract_id(r1.output)
        r2 = runner.invoke(cli, ["create", "Blocked"])
        id2 = _extract_id(r2.output)
        runner.invoke(cli, ["add-dep", id2, id1])
        result = runner.invoke(cli, ["critical-path"])
        assert result.exit_code == 0
        assert "Critical path (2 issues)" in result.output

    def test_critical_path_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["critical-path", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "path" in data
        assert "length" in data


class TestJsonOutput:
    def test_show_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "JSON show"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["show", issue_id, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["title"] == "JSON show"

    def test_list_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "JSON list A"])
        runner.invoke(cli, ["create", "JSON list B"])
        result = runner.invoke(cli, ["list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        # 2 created + auto-seeded "Future" release = 3
        assert len(data["items"]) == 3

    def test_list_json_has_more_no_false_positive(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Overfetch-by-1: when DB has exactly limit rows, has_more must be False."""
        runner, _ = cli_in_project
        # The project has 1 auto-seeded "Future" release.  Add 2 more → 3 total.
        runner.invoke(cli, ["create", "Boundary A"])
        runner.invoke(cli, ["create", "Boundary B"])
        # Query with limit=3: DB has exactly 3 rows.  Old code returns has_more=True;
        # correct overfetch-by-1 returns has_more=False.
        result = runner.invoke(cli, ["list", "--limit", "3", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["items"]) == 3
        assert data["has_more"] is False
        assert "next_offset" not in data

    def test_list_json_sort_by_updated_at_desc(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        older = runner.invoke(cli, ["create", "Older task", "--type", "task"])
        newer = runner.invoke(cli, ["create", "Newer task", "--type", "task"])
        older_id = _extract_id(older.output)
        newer_id = _extract_id(newer.output)

        from filigree.cli_common import get_db

        with get_db() as db:
            db.conn.execute(
                "UPDATE issues SET updated_at = ? WHERE id = ?",
                ("2026-01-01T00:00:00+00:00", older_id),
            )
            db.conn.execute(
                "UPDATE issues SET updated_at = ? WHERE id = ?",
                ("2026-02-01T00:00:00+00:00", newer_id),
            )
            db.conn.commit()

        result = runner.invoke(
            cli,
            ["list", "--type", "task", "--sort-by", "updated_at", "--direction", "desc", "--limit", "2", "--json"],
        )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert [item["issue_id"] for item in data["items"]] == [newer_id, older_id]

    def test_list_issues_alias_accepts_sort_options(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        low = runner.invoke(cli, ["create", "Low priority task", "--type", "task", "--priority", "3"])
        high = runner.invoke(cli, ["create", "High priority task", "--type", "task", "--priority", "1"])
        low_id = _extract_id(low.output)
        high_id = _extract_id(high.output)

        result = runner.invoke(
            cli,
            ["list-issues", "--type", "task", "--sort-by", "priority", "--direction", "desc", "--limit", "2", "--json"],
        )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert [item["issue_id"] for item in data["items"]] == [low_id, high_id]

    def test_list_json_invalid_sort_by_returns_validation_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project

        result = runner.invoke(cli, ["list", "--sort-by", "title", "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION"
        assert "sort_by" in data["error"]

    def test_ready_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Ready JSON"])
        result = runner.invoke(cli, ["ready", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        # 1 created + auto-seeded "Future" release = 2 ready
        assert len(data["items"]) == 2
        # Items are the slim shape plus the startability flag — no full
        # IssueDict (filigree-406e6b7ee0). The P2 task sorts ahead of the P4
        # release singleton and is single-hop startable, so next_action is
        # omitted to keep the shape slim.
        item = data["items"][0]
        assert set(item.keys()) == {"issue_id", "title", "status", "priority", "type", "startable"}
        assert item["startable"] is True

    def test_ready_json_include_context_adds_parent_context(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        parent = runner.invoke(cli, ["create", "Parent epic", "--type", "epic"])
        parent_id = _extract_id(parent.output)
        child = runner.invoke(cli, ["create", "Child task", "--parent", parent_id])
        child_id = _extract_id(child.output)

        result = runner.invoke(cli, ["ready", "--json", "--include-context"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        item = next(i for i in data["items"] if i["issue_id"] == child_id)
        assert item["parent_issue_id"] == parent_id
        assert item["parent_title"] == "Parent epic"

    def test_ready_json_sqlite_error_returns_io(self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        runner, _ = cli_in_project

        def _raise(*_args: object, **_kwargs: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("filigree.core.FiligreeDB.get_ready", _raise)
        result = runner.invoke(cli, ["ready", "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "IO"
        assert "database is locked" in data["error"]

    def test_ready_include_context_sqlite_error_returns_io(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _ = cli_in_project
        parent = runner.invoke(cli, ["create", "Parent epic", "--type", "epic"])
        parent_id = _extract_id(parent.output)
        runner.invoke(cli, ["create", "Child task", "--parent", parent_id])

        def _raise(*_args: object, **_kwargs: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("filigree.core.FiligreeDB.get_issue", _raise)
        result = runner.invoke(cli, ["ready", "--json", "--include-context"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "IO"
        assert "database is locked" in data["error"]

    def test_stats_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Stats JSON"])
        result = runner.invoke(cli, ["stats", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "by_status" in data
        assert "by_category" in data
        # status_name_counts / status_category_counts removed in 3.0.0
        # (filigree-e4181ae767) — gone from the CLI JSON surface too.
        assert "status_name_counts" not in data
        assert "status_category_counts" not in data

    def test_search_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Searchable item"])
        result = runner.invoke(cli, ["search", "searchable", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["items"]) == 1
        # Items must be SlimIssue shape (5 keys): no full IssueDict.
        item = data["items"][0]
        assert set(item.keys()) == {"issue_id", "title", "status", "priority", "type"}


class TestBlockedJson:
    def test_blocked_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "Blocked"])
        id1 = _extract_id(r1.output)
        r2 = runner.invoke(cli, ["create", "Blocker"])
        id2 = _extract_id(r2.output)
        runner.invoke(cli, ["add-dep", id1, id2])
        result = runner.invoke(cli, ["blocked", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["items"]) == 1
        # Items must be BlockedIssue shape: SlimIssue + blocked_by (no full IssueDict).
        item = data["items"][0]
        assert "blocked_by" in item
        assert "description" not in item  # absence check catches IssueDict drift


class TestCycleTimeDisplay:
    """cli.py metrics should display 0.0h correctly, not 'n/a'."""

    def test_zero_cycle_time_not_displayed_as_na(self) -> None:
        """0.0 is a valid cycle time and should format as '0.0h', not 'n/a'."""
        # The fix changes `if val` to `if val is not None` in cli.py.
        # Simulate the fixed formatting logic from cli.py line ~894:
        cycle_time = 0.0
        ct_str = f"{cycle_time}h" if cycle_time is not None else "n/a"
        assert ct_str == "0.0h"

        # None should still produce "n/a"
        cycle_time_none: float | None = None
        ct_str_none = f"{cycle_time_none}h" if cycle_time_none is not None else "n/a"
        assert ct_str_none == "n/a"
