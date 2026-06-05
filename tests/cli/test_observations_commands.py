"""CLI tests for observation commands: observe, list-observations, dismiss-observation,
promote-observation, batch-dismiss-observations, batch-promote-observations.

MCP shape verification:
- observe: {"observation_id", "summary", "detail", "file_id", "file_path", "line",
            "source_issue_id", "priority", "actor", "created_at", "expires_at"}
- list-observations: ListResponse — {"items": [...], "has_more": bool} + "next_offset" when has_more
- dismiss-observation: {"status": "dismissed", "observation_id": <str>}
- promote-observation: flat PublicIssue + optional "warnings"
- batch-dismiss-observations: BatchResponse[str] — {"succeeded": [...], "failed": [...]}
- batch-promote-observations: BatchResponse[SlimIssue] — {"succeeded": [...], "failed": [...]}
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from filigree.cli import cli
from filigree.core import FiligreeDB
from filigree.registry import RegistryUnavailableError
from tests.cli.conftest import SeededProject

# ---------------------------------------------------------------------------
# Canonical MCP key-sets (verified against mcp_tools/observations.py handlers)
# ---------------------------------------------------------------------------

_OBSERVE_KEYS = frozenset(
    {
        "observation_id",
        "summary",
        "detail",
        "file_id",
        "file_path",
        "line",
        "source_issue_id",
        # Added v11→v12 (filigree-cb980eee0d, P1.2): link observations
        # back to findings for cleanup cascade.
        "source_finding_id",
        "priority",
        "actor",
        "created_at",
        "expires_at",
        # Added v23→v24 (ADR-012): nullable transport-bound actor identity.
        # Surfaces as None until later tasks stamp it; output shape tracks
        # the SCHEMA_SQL column mechanically (SELECT * → dict(row)).
        "verified_actor",
    }
)

_LIST_ENVELOPE_KEYS_NO_MORE = frozenset({"items", "has_more"})
_LIST_ENVELOPE_KEYS_HAS_MORE = frozenset({"items", "has_more", "next_offset"})

_DISMISS_KEYS = frozenset({"status", "observation_id"})

_BATCH_DISMISS_KEYS = frozenset({"succeeded", "failed"})
_BATCH_PROMOTE_KEYS = frozenset({"succeeded", "failed"})

_PROMOTE_KEYS_MIN = frozenset({"issue"})  # warnings is optional
_SLIM_ISSUE_KEYS = frozenset({"issue_id", "title", "status", "priority", "type"})


# ---------------------------------------------------------------------------
# TestObserveCommand
# ---------------------------------------------------------------------------


class TestObserveCommand:
    def test_observe_registry_unavailable_returns_structured_code(
        self,
        cli_in_project: tuple[CliRunner, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def unavailable_register_file(self: FiligreeDB, path: str, **kwargs: object) -> object:
            raise RegistryUnavailableError(
                "Loomweave registry unavailable for test",
                url="http://loomweave.test/api/v1/files?path=src%2Fobserved.py",
                path=path,
                cause_kind="network",
            )

        runner, _ = cli_in_project
        monkeypatch.setattr(FiligreeDB, "register_file", unavailable_register_file)
        result = runner.invoke(cli, ["observe", "registry-backed note", "--file-path", "src/observed.py", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "REGISTRY_UNAVAILABLE"
        assert data["details"]["cause"] == "registry_unavailable"
        assert data["details"]["cause_kind"] == "network"
        assert data["details"]["path"] == "src/observed.py"
        assert data["details"]["url"] == "http://loomweave.test/api/v1/files?path=src%2Fobserved.py"

    def test_observe_happy_path_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["observe", "spotted a code smell", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        # Exact MCP key-set
        assert set(data.keys()) == _OBSERVE_KEYS, f"Shape mismatch: {set(data.keys()) ^ _OBSERVE_KEYS}"
        assert "id" not in data
        assert data["summary"] == "spotted a code smell"
        assert data["priority"] == 2  # CLI default

    def test_observe_with_all_options(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(
            cli,
            [
                "observe",
                "suspicious loop",
                "--detail",
                "may be O(n^2)",
                "--file-path",
                "src/foo.py",
                "--line",
                "42",
                "--source-issue-id",
                "test-abc",
                "--priority",
                "1",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["summary"] == "suspicious loop"
        assert data["detail"] == "may be O(n^2)"
        assert data["line"] == 42
        assert data["priority"] == 1
        assert data["source_issue_id"] == "test-abc"

    def test_observe_plain_text_output(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["observe", "plain text test"])
        assert result.exit_code == 0, result.output
        assert "Observed" in result.output

    def test_observe_empty_summary_exits_1(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Empty summary should fail — db raises ValueError."""
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["observe", "   ", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data
        assert data["code"] == "VALIDATION"

    def test_observe_required_arg_missing(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Omitting the required summary positional arg exits non-zero."""
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["observe"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# TestListObservationsCommand
# ---------------------------------------------------------------------------


class TestListObservationsCommand:
    def test_list_empty_returns_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list-observations", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["items"] == []
        assert data["has_more"] is False
        # next_offset must be absent when has_more is False
        assert "next_offset" not in data

    def test_list_populated_has_correct_item_shape(self, initialized_project_with_observation: SeededProject) -> None:
        """Items must have the exact MCP observe-dict key-set."""
        import os

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_observation.path))
        try:
            result = runner.invoke(cli, ["list-observations", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert len(data["items"]) == 1
            item = data["items"][0]
            # Every key the MCP emits must be present; no extra keys allowed.
            assert set(item.keys()) == _OBSERVE_KEYS, f"Item key mismatch: {set(item.keys()) ^ _OBSERVE_KEYS}"
            assert "id" not in item
        finally:
            os.chdir(original)

    def test_list_envelope_keys_no_more(self, initialized_project_with_observation: SeededProject) -> None:
        """When has_more is False, envelope must have exactly {items, has_more}."""
        import os

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_observation.path))
        try:
            result = runner.invoke(cli, ["list-observations", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert set(data.keys()) == _LIST_ENVELOPE_KEYS_NO_MORE
        finally:
            os.chdir(original)

    def test_list_pagination_boundary_overfetch(self, initialized_project_with_many_obs: SeededProject) -> None:
        """With limit=2 and 3 obs seeded, has_more=True and next_offset is present."""
        import os

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_many_obs.path))
        try:
            result = runner.invoke(cli, ["list-observations", "--limit", "2", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["has_more"] is True
            assert len(data["items"]) == 2
            assert "next_offset" in data
            assert data["next_offset"] == 2
            assert set(data.keys()) == _LIST_ENVELOPE_KEYS_HAS_MORE
        finally:
            os.chdir(original)

    def test_list_pagination_second_page(self, initialized_project_with_many_obs: SeededProject) -> None:
        """Second page with offset=2, limit=2 against 3 obs returns last 1, has_more=False."""
        import os

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_many_obs.path))
        try:
            result = runner.invoke(cli, ["list-observations", "--limit", "2", "--offset", "2", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert len(data["items"]) == 1
            assert data["has_more"] is False
            assert "next_offset" not in data
        finally:
            os.chdir(original)

    def test_list_plain_text_empty(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list-observations"])
        assert result.exit_code == 0
        assert "No observations" in result.output

    def test_list_plain_text_populated(self, initialized_project_with_observation: SeededProject) -> None:
        import os

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_observation.path))
        try:
            result = runner.invoke(cli, ["list-observations"])
            assert result.exit_code == 0
            assert "note" in result.output  # the seed obs summary is "note"
        finally:
            os.chdir(original)

    def test_list_filters_by_source_issue_id(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["observe", "from issue", "--source-issue-id", "test-issue-1"])
        runner.invoke(cli, ["observe", "unrelated", "--source-issue-id", "test-issue-2"])

        result = runner.invoke(cli, ["list-observations", "--source-issue-id", "test-issue-1", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data["items"]) == 1
        assert data["items"][0]["summary"] == "from issue"


# ---------------------------------------------------------------------------
# TestDismissObservationCommand
# ---------------------------------------------------------------------------


class TestDismissObservationCommand:
    def test_dismiss_happy_path_json(self, initialized_project_with_observation: SeededProject) -> None:
        import os

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_observation.path))
        try:
            obs_id = initialized_project_with_observation.obs_id
            result = runner.invoke(cli, ["dismiss-observation", obs_id, "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            # Exact MCP shape
            assert set(data.keys()) == _DISMISS_KEYS, f"Shape mismatch: {set(data.keys()) ^ _DISMISS_KEYS}"
            assert data["status"] == "dismissed"
            assert data["observation_id"] == obs_id
        finally:
            os.chdir(original)

    def test_dismiss_plain_text(self, initialized_project_with_observation: SeededProject) -> None:
        import os

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_observation.path))
        try:
            obs_id = initialized_project_with_observation.obs_id
            result = runner.invoke(cli, ["dismiss-observation", obs_id])
            assert result.exit_code == 0
            assert "Dismissed" in result.output
        finally:
            os.chdir(original)

    def test_dismiss_not_found_json_returns_error_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["dismiss-observation", "obs-nonexistent", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data
        assert data["code"] == "NOT_FOUND"

    def test_dismiss_not_found_plain_text_to_stderr(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["dismiss-observation", "obs-nonexistent"])
        assert result.exit_code == 1

    def test_dismiss_with_reason(self, initialized_project_with_observation: SeededProject) -> None:
        import os

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_observation.path))
        try:
            obs_id = initialized_project_with_observation.obs_id
            result = runner.invoke(cli, ["dismiss-observation", obs_id, "--reason", "false positive", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["status"] == "dismissed"
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestPromoteObservationCommand
# ---------------------------------------------------------------------------


class TestPromoteObservationCommand:
    def test_promote_happy_path_json(self, initialized_project_with_observation: SeededProject) -> None:
        import os

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_observation.path))
        try:
            obs_id = initialized_project_with_observation.obs_id
            result = runner.invoke(cli, ["promote-observation", obs_id, "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            # Mirror MCP: flat PublicIssue projection, refreshed after enrichment.
            assert "issue" not in data
            assert "id" not in data
            assert "issue_id" in data
            assert data["title"] == "note"
            assert data["status"] == "open"
            assert data["type"] == "task"
            assert "from-observation" in data["labels"]
        finally:
            os.chdir(original)

    def test_promote_returns_flat_public_issue_not_wrapped_internal_issue(
        self, initialized_project_with_observation: SeededProject
    ) -> None:
        """Regression guard: issue must be the public flat shape, not an internal wrapper."""
        import os

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_observation.path))
        try:
            obs_id = initialized_project_with_observation.obs_id
            result = runner.invoke(cli, ["promote-observation", obs_id, "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert isinstance(data["issue_id"], str)
            assert data["issue_id"]
            assert "issue" not in data
            assert "id" not in data
        finally:
            os.chdir(original)

    def test_promote_not_found_json_error_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["promote-observation", "obs-nonexistent", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data
        assert data["code"] == "NOT_FOUND"

    def test_promote_with_type_and_priority(self, initialized_project_with_observation: SeededProject) -> None:
        import os

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_observation.path))
        try:
            obs_id = initialized_project_with_observation.obs_id
            result = runner.invoke(
                cli,
                ["promote-observation", obs_id, "--type", "bug", "--priority", "1", "--json"],
            )
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["type"] == "bug"
            assert data["priority"] == 1
        finally:
            os.chdir(original)

    def test_promote_plain_text(self, initialized_project_with_observation: SeededProject) -> None:
        import os

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_observation.path))
        try:
            obs_id = initialized_project_with_observation.obs_id
            result = runner.invoke(cli, ["promote-observation", obs_id])
            assert result.exit_code == 0
            assert "Promoted" in result.output
            assert obs_id in result.output
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestBatchDismissObservationsCommand
# ---------------------------------------------------------------------------


class TestBatchDismissObservationsCommand:
    def test_batch_dismiss_all_valid_json(self, initialized_project_with_many_obs: SeededProject) -> None:
        import os

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_many_obs.path))
        try:
            ids = initialized_project_with_many_obs.obs_ids
            result = runner.invoke(cli, ["batch-dismiss-observations", *ids, "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            # Exact BatchResponse[str] shape
            assert set(data.keys()) == _BATCH_DISMISS_KEYS, f"Shape mismatch: {set(data.keys()) ^ _BATCH_DISMISS_KEYS}"
            assert len(data["succeeded"]) == len(ids)
            assert data["failed"] == []
        finally:
            os.chdir(original)

    def test_batch_dismiss_mixed_valid_invalid(self, initialized_project_with_observation: SeededProject) -> None:
        """Mixed valid + invalid IDs produce succeeded + failed lists."""
        import os

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_observation.path))
        try:
            obs_id = initialized_project_with_observation.obs_id
            result = runner.invoke(
                cli,
                ["batch-dismiss-observations", obs_id, "obs-nonexistent", "--json"],
            )
            assert result.exit_code == 1  # partial failure exits 1
            data = json.loads(result.output)
            assert set(data.keys()) == _BATCH_DISMISS_KEYS
            assert obs_id in data["succeeded"]
            assert len(data["failed"]) == 1
            assert data["failed"][0]["id"] == "obs-nonexistent"
            assert data["failed"][0]["code"] == "NOT_FOUND"
        finally:
            os.chdir(original)

    def test_batch_dismiss_all_invalid(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["batch-dismiss-observations", "obs-bad-1", "obs-bad-2", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["succeeded"] == []
        assert len(data["failed"]) == 2

    def test_batch_dismiss_preserves_input_order_in_succeeded(self, initialized_project_with_many_obs: SeededProject) -> None:
        """succeeded list must preserve the input order of IDs (mirrors MCP dict.fromkeys)."""
        import os

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_many_obs.path))
        try:
            ids = initialized_project_with_many_obs.obs_ids
            # Pass in reverse order — succeeded must reflect that order
            reversed_ids = list(reversed(ids))
            result = runner.invoke(cli, ["batch-dismiss-observations", *reversed_ids, "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["succeeded"] == reversed_ids
        finally:
            os.chdir(original)

    def test_batch_dismiss_plain_text(self, initialized_project_with_observation: SeededProject) -> None:
        import os

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_observation.path))
        try:
            obs_id = initialized_project_with_observation.obs_id
            result = runner.invoke(cli, ["batch-dismiss-observations", obs_id])
            assert result.exit_code == 0
            assert "Dismissed" in result.output
        finally:
            os.chdir(original)

    def test_batch_dismiss_with_reason(self, initialized_project_with_observation: SeededProject) -> None:
        import os

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_observation.path))
        try:
            obs_id = initialized_project_with_observation.obs_id
            result = runner.invoke(
                cli,
                ["batch-dismiss-observations", obs_id, "--reason", "bulk cleanup", "--json"],
            )
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert obs_id in data["succeeded"]
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestBatchPromoteObservationsCommand
# ---------------------------------------------------------------------------


class TestBatchPromoteObservationsCommand:
    def test_batch_promote_all_valid_json(self, initialized_project_with_many_obs: SeededProject) -> None:
        import os

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_many_obs.path))
        try:
            ids = initialized_project_with_many_obs.obs_ids[:2]
            result = runner.invoke(
                cli,
                ["batch-promote-observations", *ids, "--type", "bug", "--priority", "1", "--json"],
            )
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert set(data.keys()) == _BATCH_PROMOTE_KEYS
            assert len(data["succeeded"]) == len(ids)
            assert data["failed"] == []
            for item in data["succeeded"]:
                assert set(item.keys()) == _SLIM_ISSUE_KEYS
                assert item["type"] == "bug"
                assert item["priority"] == 1
        finally:
            os.chdir(original)

    def test_batch_promote_mixed_valid_invalid(self, initialized_project_with_observation: SeededProject) -> None:
        import os

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_observation.path))
        try:
            obs_id = initialized_project_with_observation.obs_id
            result = runner.invoke(
                cli,
                ["batch-promote-observations", obs_id, "obs-nonexistent", "--json"],
            )
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert set(data.keys()) == _BATCH_PROMOTE_KEYS
            assert len(data["succeeded"]) == 1
            assert data["succeeded"][0]["title"] == "note"
            assert len(data["failed"]) == 1
            assert data["failed"][0]["id"] == "obs-nonexistent"
            assert data["failed"][0]["code"] == "NOT_FOUND"
        finally:
            os.chdir(original)

    def test_batch_promote_plain_text(self, initialized_project_with_observation: SeededProject) -> None:
        import os

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_observation.path))
        try:
            obs_id = initialized_project_with_observation.obs_id
            result = runner.invoke(cli, ["batch-promote-observations", obs_id])
            assert result.exit_code == 0
            assert "Promoted" in result.output
            assert obs_id in result.output
        finally:
            os.chdir(original)


class TestObservationTriageCommands:
    def test_link_observation_json(self, initialized_project_with_observation: SeededProject) -> None:
        import os

        from filigree.cli_common import get_db

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_observation.path))
        try:
            with get_db() as db:
                issue = db.create_issue("Existing issue")
            result = runner.invoke(
                cli,
                [
                    "link-observation",
                    initialized_project_with_observation.obs_id,
                    issue.id,
                    "--disposition",
                    "duplicate",
                    "--reason",
                    "same issue",
                    "--json",
                ],
            )
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["observation_id"] == initialized_project_with_observation.obs_id
            assert data["issue_id"] == issue.id
            assert data["disposition"] == "duplicate"
        finally:
            os.chdir(original)

    def test_batch_link_observations_json(self, initialized_project_with_many_obs: SeededProject) -> None:
        import os

        from filigree.cli_common import get_db

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_many_obs.path))
        try:
            with get_db() as db:
                issue = db.create_issue("Existing issue")
            obs_id = initialized_project_with_many_obs.obs_ids[0]
            result = runner.invoke(
                cli,
                ["batch-link-observations", issue.id, obs_id, "obs-missing", "--json"],
            )
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert [item["observation_id"] for item in data["succeeded"]] == [obs_id]
            assert data["failed"][0]["id"] == "obs-missing"
        finally:
            os.chdir(original)

    def test_promote_observations_to_issue_json(self, initialized_project_with_many_obs: SeededProject) -> None:
        import os

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_many_obs.path))
        try:
            ids = initialized_project_with_many_obs.obs_ids[:2]
            result = runner.invoke(
                cli,
                [
                    "promote-observations-to-issue",
                    *ids,
                    "--type",
                    "bug",
                    "--title",
                    "Merged issue",
                    "--json",
                ],
            )
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["title"] == "Merged issue"
            assert data["type"] == "bug"
            assert data["fields"]["source_observation_ids"] == ids
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# Regression: documented `--file` alias for `observe` (filigree-6f8d9816b7)
# ---------------------------------------------------------------------------


class TestObserveFileAlias:
    """The `--file` alias must be accepted alongside `--file-path`. The short
    form has been the documented spelling since filigree-6f8d9816b7 and
    external scripts depend on it, even though the agent-facing prompt no
    longer pins specific CLI examples for `observe`.
    """

    def test_observe_accepts_file_alias(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(
            cli,
            ["observe", "alias test", "--file", "src/foo.py", "--line", "42", "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["file_path"] == "src/foo.py"
        assert data["line"] == 42

    def test_observe_file_path_long_form_still_works(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Sanity: original `--file-path` spelling still works."""
        runner, _ = cli_in_project
        result = runner.invoke(
            cli,
            ["observe", "long form", "--file-path", "src/bar.py", "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["file_path"] == "src/bar.py"


# ---------------------------------------------------------------------------
# Regression: sqlite3.Error → ErrorCode.IO envelope (filigree-9ca1f5ace8)
# ---------------------------------------------------------------------------


class TestObservationDbErrorEnvelope:
    """sqlite3.Error from observation DB calls must surface as ErrorCode.IO
    JSON envelopes (mirroring mcp_tools/observations.py and cli_commands/files.py)."""

    def test_observe_sqlite_error_returns_io_envelope(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _ = cli_in_project

        def _raise(*_a: object, **_kw: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("filigree.core.FiligreeDB.create_observation", _raise)
        result = runner.invoke(cli, ["observe", "x", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "IO"
        assert "database is locked" in data["error"]

    def test_list_observations_sqlite_error_returns_io_envelope(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _ = cli_in_project

        def _raise(*_a: object, **_kw: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("filigree.core.FiligreeDB.list_observations", _raise)
        result = runner.invoke(cli, ["list-observations", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "IO"
        assert "database is locked" in data["error"]

    def test_dismiss_observation_sqlite_error_returns_io_envelope(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _ = cli_in_project

        def _raise(*_a: object, **_kw: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("filigree.core.FiligreeDB.dismiss_observation", _raise)
        result = runner.invoke(cli, ["dismiss-observation", "obs-anything", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "IO"
        assert "database is locked" in data["error"]

    def test_batch_dismiss_full_prefetch_sqlite_error_returns_io_envelope(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _ = cli_in_project

        def _raise(*_a: object, **_kw: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("filigree.core.FiligreeDB.get_observations_by_ids", _raise)
        result = runner.invoke(
            cli,
            ["batch-dismiss-observations", "obs-anything", "--detail", "full", "--json"],
        )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "IO"
        assert "database is locked" in data["error"]

    def test_promote_observation_sqlite_error_returns_io_envelope(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _ = cli_in_project

        def _raise(*_a: object, **_kw: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("filigree.core.FiligreeDB.promote_observation", _raise)
        result = runner.invoke(cli, ["promote-observation", "obs-anything", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "IO"
        assert "database is locked" in data["error"]

    def test_promote_observation_refresh_sqlite_error_returns_io_envelope(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _ = cli_in_project

        def _promote(*_a: object, **_kw: object) -> dict[str, object]:
            return {"issue": SimpleNamespace(id="test-issue")}

        def _raise(*_a: object, **_kw: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("filigree.core.FiligreeDB.promote_observation", _promote)
        monkeypatch.setattr("filigree.core.FiligreeDB.get_issue", _raise)
        result = runner.invoke(cli, ["promote-observation", "obs-anything", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "IO"
        assert "database is locked" in data["error"]

    def test_batch_promote_refresh_sqlite_error_returns_io_envelope(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _ = cli_in_project

        def _promote(*_a: object, **_kw: object) -> tuple[list[dict[str, object]], list[object]]:
            return ([{"issue": SimpleNamespace(id="test-issue")}], [])

        def _raise(*_a: object, **_kw: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("filigree.core.FiligreeDB.batch_promote_observations", _promote)
        monkeypatch.setattr("filigree.core.FiligreeDB.get_issue", _raise)
        result = runner.invoke(cli, ["batch-promote-observations", "obs-anything", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "IO"
        assert "database is locked" in data["error"]

    def test_promote_observations_to_issue_refresh_sqlite_error_returns_io_envelope(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _ = cli_in_project

        def _promote(*_a: object, **_kw: object) -> dict[str, object]:
            return {"issue": SimpleNamespace(id="test-issue")}

        def _raise(*_a: object, **_kw: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("filigree.core.FiligreeDB.promote_observations_to_issue", _promote)
        monkeypatch.setattr("filigree.core.FiligreeDB.get_issue", _raise)
        result = runner.invoke(cli, ["promote-observations-to-issue", "obs-one", "obs-two", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "IO"
        assert "database is locked" in data["error"]
