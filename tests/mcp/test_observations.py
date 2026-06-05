"""MCP tool tests for observation tools."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from filigree.core import FiligreeDB
from filigree.mcp_server import call_tool
from filigree.registry import BatchQuery, BatchResolution, RegistryUnavailableError, ResolvedFile
from filigree.types.api import ErrorCode
from tests.mcp._helpers import _parse


class TestObserveTool:
    async def test_observe_registry_unavailable_returns_error_response(self, mcp_db: FiligreeDB) -> None:
        class UnavailableRegistry:
            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                raise RegistryUnavailableError(
                    "Loomweave registry unavailable for test",
                    url="http://loomweave.test/api/v1/files?path=src%2Fobserved.py",
                    path=path,
                    cause_kind="network",
                )

            def resolve_files_batch(self, queries: list[BatchQuery], *, actor: str = "") -> BatchResolution:
                raise RegistryUnavailableError(
                    "Loomweave registry unavailable for test",
                    url="http://loomweave.test/api/v1/files",
                    cause_kind="network",
                )

            def is_displaced(self) -> bool:
                return False

        mcp_db.registry = UnavailableRegistry()

        result = await call_tool("observe", {"summary": "registry-backed note", "file_path": "src/observed.py"})
        data = _parse(result)

        assert data["code"] == ErrorCode.REGISTRY_UNAVAILABLE
        assert data["details"]["cause"] == "registry_unavailable"
        assert data["details"]["cause_kind"] == "network"
        assert data["details"]["path"] == "src/observed.py"
        assert data["details"]["url"] == "http://loomweave.test/api/v1/files?path=src%2Fobserved.py"

    async def test_observe_creates_observation(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("observe", {"summary": "Something looks wrong"})
        data = _parse(result)
        assert data["observation_id"].startswith("mcp-")
        assert "id" not in data
        assert data["summary"] == "Something looks wrong"
        assert data["priority"] == 3

    async def test_observe_with_all_fields(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool(
            "observe",
            {
                "summary": "Null deref risk",
                "detail": "result.data used without check",
                "file_path": "src/core.py",
                "line": 42,
                "priority": 1,
                "actor": "claude",
            },
        )
        data = _parse(result)
        assert data["summary"] == "Null deref risk"
        assert data["priority"] == 1

    async def test_observe_empty_summary_fails(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("observe", {"summary": ""})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_observe_priority_zero(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("observe", {"summary": "critical", "priority": 0})
        data = _parse(result)
        assert data["priority"] == 0

    async def test_observe_priority_four(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("observe", {"summary": "backlog", "priority": 4})
        data = _parse(result)
        assert data["priority"] == 4

    async def test_observe_with_source_issue(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool(
            "observe",
            {
                "summary": "side note",
                "source_issue_id": "mcp-abc123",
            },
        )
        data = _parse(result)
        assert data["source_issue_id"] == "mcp-abc123"

    async def test_observe_huge_line_rejected_before_sqlite(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("observe", {"summary": "line overflow", "line": 9_223_372_036_854_775_808}))

        assert data["code"] == ErrorCode.VALIDATION
        assert "line must be <=" in data["error"]


class TestListObservationsTool:
    async def test_list_empty(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("list_observations", {})
        data = _parse(result)
        assert data["items"] == []
        assert data["has_more"] is False

    async def test_list_returns_observations(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_observation("First")
        mcp_db.create_observation("Second")
        result = await call_tool("list_observations", {})
        data = _parse(result)
        assert len(data["items"]) == 2
        assert all("observation_id" in item for item in data["items"])
        assert all("id" not in item for item in data["items"])

    async def test_list_with_file_path_filter(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_observation("api bug", file_path="src/api/routes.py")
        mcp_db.create_observation("core bug", file_path="src/core.py")
        result = await call_tool("list_observations", {"file_path": "src/api"})
        data = _parse(result)
        assert len(data["items"]) == 1
        assert data["items"][0]["summary"] == "api bug"

    async def test_list_with_limit(self, mcp_db: FiligreeDB) -> None:
        for i in range(5):
            mcp_db.create_observation(f"Obs {i}")
        result = await call_tool("list_observations", {"limit": 2})
        data = _parse(result)
        assert len(data["items"]) == 2

    async def test_list_has_more_true_when_more_results(self, mcp_db: FiligreeDB) -> None:
        for i in range(5):
            mcp_db.create_observation(f"Obs {i}")
        result = await call_tool("list_observations", {"limit": 2})
        data = _parse(result)
        assert len(data["items"]) == 2
        assert data["has_more"] is True

    async def test_list_has_more_false_when_no_more_results(self, mcp_db: FiligreeDB) -> None:
        for i in range(2):
            mcp_db.create_observation(f"Obs {i}")
        result = await call_tool("list_observations", {"limit": 5})
        data = _parse(result)
        assert len(data["items"]) == 2
        assert data["has_more"] is False

    async def test_list_with_file_id_filter(self, mcp_db: FiligreeDB) -> None:
        obs = mcp_db.create_observation("api bug", file_path="src/api.py")
        mcp_db.create_observation("other bug", file_path="src/other.py")
        result = await call_tool("list_observations", {"file_id": obs["file_id"]})
        data = _parse(result)
        assert len(data["items"]) == 1
        assert data["items"][0]["summary"] == "api bug"

    async def test_list_with_source_issue_filter(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_observation("from issue", source_issue_id="mcp-issue-1")
        mcp_db.create_observation("unrelated", source_issue_id="mcp-issue-2")
        result = await call_tool("list_observations", {"source_issue_id": "mcp-issue-1"})
        data = _parse(result)
        assert len(data["items"]) == 1
        assert data["items"][0]["summary"] == "from issue"

    async def test_list_does_not_sweep_expired_observations_from_read_only_tool(self, mcp_db: FiligreeDB) -> None:
        obs = mcp_db.create_observation("expired scratchpad")
        mcp_db.conn.execute("UPDATE observations SET expires_at = ? WHERE id = ?", ("2020-01-01T00:00:00+00:00", obs["id"]))
        mcp_db.conn.commit()

        result = await call_tool("list_observations", {})
        data = _parse(result)

        assert data["items"] == []
        row = mcp_db.conn.execute("SELECT id FROM observations WHERE id = ?", (obs["id"],)).fetchone()
        assert row is not None


class TestDismissObservationTool:
    async def test_dismiss(self, mcp_db: FiligreeDB) -> None:
        obs = mcp_db.create_observation("To dismiss")
        result = await call_tool("dismiss_observation", {"observation_id": obs["id"]})
        data = _parse(result)
        assert data["status"] == "dismissed"

    async def test_dismiss_with_reason(self, mcp_db: FiligreeDB) -> None:
        obs = mcp_db.create_observation("Not a bug")
        result = await call_tool(
            "dismiss_observation",
            {
                "observation_id": obs["id"],
                "reason": "false positive",
                "actor": "tester",
            },
        )
        _parse(result)
        row = mcp_db.conn.execute("SELECT reason FROM dismissed_observations WHERE obs_id = ?", (obs["id"],)).fetchone()
        assert row["reason"] == "false positive"

    async def test_dismiss_nonexistent_fails(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("dismiss_observation", {"observation_id": "nope-123"})
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND


class TestBatchDismissTool:
    async def test_batch_dismiss(self, mcp_db: FiligreeDB) -> None:
        o1 = mcp_db.create_observation("One")
        o2 = mcp_db.create_observation("Two")
        mcp_db.create_observation("Three")
        result = await call_tool(
            "batch_dismiss_observations",
            {
                "observation_ids": [o1["id"], o2["id"]],
            },
        )
        data = _parse(result)
        remaining = mcp_db.list_observations()
        assert len(remaining) == 1
        assert remaining[0]["summary"] == "Three"
        assert len(data["succeeded"]) == 2
        assert set(data["succeeded"]) == {o1["id"], o2["id"]}
        assert data["failed"] == []

    async def test_batch_dismiss_empty_list(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("batch_dismiss_observations", {"observation_ids": []})
        data = _parse(result)
        assert data["succeeded"] == []
        assert data["failed"] == []

    async def test_batch_dismiss_invalid_ids_reports_not_found(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("batch_dismiss_observations", {"observation_ids": ["nope-1", "nope-2"]})
        data = _parse(result)
        assert data["succeeded"] == []
        assert {f["id"] for f in data["failed"]} == {"nope-1", "nope-2"}
        assert all(f["code"] == ErrorCode.NOT_FOUND for f in data["failed"])

    async def test_batch_dismiss_rejects_bare_string_ids(self, mcp_db: FiligreeDB) -> None:
        """filigree-45580755aa: bare string must not be iterated char-by-char."""
        result = await call_tool("batch_dismiss_observations", {"observation_ids": "obs-123"})
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION

    async def test_batch_dismiss_rejects_non_string_members(self, mcp_db: FiligreeDB) -> None:
        """filigree-45580755aa: non-string ids rejected up front."""
        result = await call_tool("batch_dismiss_observations", {"observation_ids": [1, 2, 3]})
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION

    async def test_batch_dismiss_full_prefetch_sqlite_error_returns_io(self, mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        obs = mcp_db.create_observation("Prefetch failure")

        def _raise_sqlite_error(self: FiligreeDB, *args: object, **kwargs: object) -> object:
            raise sqlite3.OperationalError("observation prefetch failed")

        monkeypatch.setattr(FiligreeDB, "get_observations_by_ids", _raise_sqlite_error)

        result = _parse(
            await call_tool(
                "batch_dismiss_observations",
                {"observation_ids": [obs["id"]], "response_detail": "full"},
            )
        )

        assert result["code"] == ErrorCode.IO
        assert "observation prefetch failed" in result["error"]


class TestBatchPromoteTool:
    async def test_batch_promote_promotes_multiple_observations(self, mcp_db: FiligreeDB) -> None:
        o1 = mcp_db.create_observation("One", priority=2)
        o2 = mcp_db.create_observation("Two", priority=3)
        mcp_db.create_observation("Three")

        result = await call_tool(
            "batch_promote_observations",
            {
                "observation_ids": [o1["id"], o2["id"]],
                "type": "bug",
                "priority": 1,
                "actor": "tester",
            },
        )

        data = _parse(result)
        assert len(data["succeeded"]) == 2
        assert data["failed"] == []
        assert [item["title"] for item in data["succeeded"]] == ["One", "Two"]
        assert {item["type"] for item in data["succeeded"]} == {"bug"}
        assert {item["priority"] for item in data["succeeded"]} == {1}
        issue_ids = [item["issue_id"] for item in data["succeeded"]]
        assert mcp_db.list_observations()[0]["summary"] == "Three"
        assert all("from-observation" in mcp_db.get_issue(issue_id).labels for issue_id in issue_ids)

    async def test_batch_promote_reports_missing_ids_per_item(self, mcp_db: FiligreeDB) -> None:
        obs = mcp_db.create_observation("Promote me")

        result = await call_tool(
            "batch_promote_observations",
            {"observation_ids": [obs["id"], "obs-missing"]},
        )

        data = _parse(result)
        assert len(data["succeeded"]) == 1
        assert data["succeeded"][0]["title"] == "Promote me"
        assert len(data["failed"]) == 1
        assert data["failed"][0]["id"] == "obs-missing"
        assert data["failed"][0]["code"] == ErrorCode.NOT_FOUND

    async def test_batch_promote_refresh_sqlite_error_returns_io(self, mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        def _promote(self: FiligreeDB, *args: object, **kwargs: object) -> tuple[list[dict[str, object]], list[object]]:
            return ([{"issue": SimpleNamespace(id="filigree-refresh-target")}], [])

        def _raise_sqlite_error(self: FiligreeDB, issue_id: str) -> object:
            raise sqlite3.OperationalError(f"issue refresh failed for {issue_id}")

        monkeypatch.setattr(FiligreeDB, "batch_promote_observations", _promote)
        monkeypatch.setattr(FiligreeDB, "get_issue", _raise_sqlite_error)

        data = _parse(await call_tool("batch_promote_observations", {"observation_ids": ["obs-1"]}))

        assert data["code"] == ErrorCode.IO
        assert "issue refresh failed" in data["error"]

    async def test_batch_promote_rejects_bare_string_ids(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("batch_promote_observations", {"observation_ids": "obs-123"})
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION

    async def test_batch_promote_rejects_non_string_members(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("batch_promote_observations", {"observation_ids": [1, 2, 3]})
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION


class TestObservationTriageTools:
    async def test_link_observation_to_issue(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Existing issue")
        obs = mcp_db.create_observation("Same evidence")

        result = await call_tool(
            "link_observation",
            {
                "observation_id": obs["id"],
                "issue_id": issue.id,
                "disposition": "duplicate",
                "reason": "same failure",
                "actor": "triager",
            },
        )

        data = _parse(result)
        assert data["observation_id"] == obs["id"]
        assert data["issue_id"] == issue.id
        assert data["disposition"] == "duplicate"
        assert mcp_db.list_observations() == []

    async def test_batch_link_observations(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Existing issue")
        obs = mcp_db.create_observation("Attach me")

        result = await call_tool(
            "batch_link_observations",
            {
                "observation_ids": [obs["id"], "obs-missing"],
                "issue_id": issue.id,
                "disposition": "evidence",
            },
        )

        data = _parse(result)
        assert [item["observation_id"] for item in data["succeeded"]] == [obs["id"]]
        assert data["failed"][0]["id"] == "obs-missing"
        assert data["failed"][0]["code"] == ErrorCode.NOT_FOUND

    async def test_batch_link_missing_issue_id_rejected(self, mcp_db: FiligreeDB) -> None:
        obs = mcp_db.create_observation("Attach me")

        data = _parse(await call_tool("batch_link_observations", {"observation_ids": [obs["id"]]}))

        assert data["code"] == ErrorCode.VALIDATION
        assert data["error"] == "issue_id is required"

    async def test_batch_link_non_string_issue_id_rejected(self, mcp_db: FiligreeDB) -> None:
        obs = mcp_db.create_observation("Attach me")

        data = _parse(await call_tool("batch_link_observations", {"observation_ids": [obs["id"]], "issue_id": ["bad"]}))

        assert data["code"] == ErrorCode.VALIDATION
        assert data["error"] == "issue_id must be a string"

    async def test_batch_link_non_string_disposition_rejected(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Existing issue")
        obs = mcp_db.create_observation("Attach me")

        data = _parse(
            await call_tool(
                "batch_link_observations",
                {"observation_ids": [obs["id"]], "issue_id": issue.id, "disposition": ["evidence"]},
            )
        )

        assert data["code"] == ErrorCode.VALIDATION
        assert data["error"] == "disposition must be a string"

    async def test_batch_link_non_string_reason_rejected(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Existing issue")
        obs = mcp_db.create_observation("Attach me")

        data = _parse(
            await call_tool(
                "batch_link_observations",
                {"observation_ids": [obs["id"]], "issue_id": issue.id, "reason": ["bad"]},
            )
        )

        assert data["code"] == ErrorCode.VALIDATION
        assert data["error"] == "reason must be a string"

    async def test_promote_observations_to_issue(self, mcp_db: FiligreeDB) -> None:
        obs1 = mcp_db.create_observation("First signal")
        obs2 = mcp_db.create_observation("Second signal", priority=1)

        result = await call_tool(
            "promote_observations_to_issue",
            {
                "observation_ids": [obs1["id"], obs2["id"]],
                "type": "bug",
                "title": "Merged issue",
            },
        )

        data = _parse(result)
        assert data["issue_id"].startswith("mcp-")
        assert data["title"] == "Merged issue"
        assert data["priority"] == 1
        assert data["fields"]["source_observation_ids"] == [obs1["id"], obs2["id"]]

    async def test_promote_observations_to_issue_refresh_sqlite_error_returns_io(
        self, mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issue = mcp_db.create_issue("Promoted shell")

        def _promote(self: FiligreeDB, *args: object, **kwargs: object) -> dict[str, object]:
            return {"issue": SimpleNamespace(id=issue.id)}

        def _raise_sqlite_error(self: FiligreeDB, *args: object, **kwargs: object) -> object:
            raise sqlite3.OperationalError("issue refresh failed")

        monkeypatch.setattr(FiligreeDB, "promote_observations_to_issue", _promote)
        monkeypatch.setattr(FiligreeDB, "get_issue", _raise_sqlite_error)

        result = _parse(await call_tool("promote_observations_to_issue", {"observation_ids": ["obs-1"]}))

        assert result["code"] == ErrorCode.IO
        assert "issue refresh failed" in result["error"]


class TestSummaryRefreshOnObservationMutations:
    """filigree-134296bf02: observe / dismiss_observation / batch_dismiss_observations
    must refresh context.md so the session snapshot stays in sync.
    """

    async def test_observe_refreshes_summary(self, mcp_db: FiligreeDB) -> None:
        with patch("filigree.mcp_server.write_summary") as mock_write:
            await call_tool("observe", {"summary": "fresh observation"})
        assert mock_write.called, "observe must call _refresh_summary"

    async def test_dismiss_refreshes_summary(self, mcp_db: FiligreeDB) -> None:
        obs = mcp_db.create_observation("will dismiss")
        with patch("filigree.mcp_server.write_summary") as mock_write:
            await call_tool("dismiss_observation", {"observation_id": obs["id"]})
        assert mock_write.called, "dismiss_observation must call _refresh_summary"

    async def test_batch_dismiss_refreshes_summary(self, mcp_db: FiligreeDB) -> None:
        o1 = mcp_db.create_observation("A")
        o2 = mcp_db.create_observation("B")
        with patch("filigree.mcp_server.write_summary") as mock_write:
            await call_tool("batch_dismiss_observations", {"observation_ids": [o1["id"], o2["id"]]})
        assert mock_write.called, "batch_dismiss_observations must call _refresh_summary"

    async def test_batch_promote_refreshes_summary(self, mcp_db: FiligreeDB) -> None:
        obs = mcp_db.create_observation("will promote")
        with patch("filigree.mcp_server.write_summary") as mock_write:
            await call_tool("batch_promote_observations", {"observation_ids": [obs["id"]]})
        assert mock_write.called, "batch_promote_observations must call _refresh_summary"


class TestPromoteObservationTool:
    async def test_promote(self, mcp_db: FiligreeDB) -> None:
        obs = mcp_db.create_observation(
            "Null pointer risk",
            detail="result.data used without check",
            file_path="src/api.py",
            priority=2,
        )
        result = await call_tool(
            "promote_observation",
            {
                "observation_id": obs["id"],
                "type": "bug",
            },
        )
        data = _parse(result)
        assert "issue_id" in data
        assert data["title"] == "Null pointer risk"
        assert mcp_db.list_observations() == []

    async def test_promote_returns_flat_public_issue_after_enrichments(self, mcp_db: FiligreeDB) -> None:
        obs = mcp_db.create_observation("Promote shape")

        result = await call_tool("promote_observation", {"observation_id": obs["id"]})

        data = _parse(result)
        assert "issue" not in data
        assert "issue_id" in data
        assert "id" not in data
        assert data["title"] == "Promote shape"
        assert "from-observation" in data["labels"]

    async def test_promote_with_title_override(self, mcp_db: FiligreeDB) -> None:
        obs = mcp_db.create_observation("Original summary")
        result = await call_tool(
            "promote_observation",
            {"observation_id": obs["id"], "title": "Better title"},
        )
        data = _parse(result)
        assert data["title"] == "Better title"

    async def test_promote_with_extra_description(self, mcp_db: FiligreeDB) -> None:
        obs = mcp_db.create_observation(
            "Bug spotted",
            detail="Some detail",
        )
        result = await call_tool(
            "promote_observation",
            {"observation_id": obs["id"], "description": "Extra context from review"},
        )
        data = _parse(result)
        assert "Extra context from review" in data["description"]
        assert "Some detail" in data["description"]

    async def test_promote_with_title_and_description(self, mcp_db: FiligreeDB) -> None:
        obs = mcp_db.create_observation("Original", detail="Original detail")
        result = await call_tool(
            "promote_observation",
            {
                "observation_id": obs["id"],
                "title": "Custom title",
                "description": "Prepended context",
            },
        )
        data = _parse(result)
        assert data["title"] == "Custom title"
        assert "Prepended context" in data["description"]

    async def test_promote_nonexistent_fails(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("promote_observation", {"observation_id": "nope-123"})
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_promote_invalid_type_returns_validation_error(self, mcp_db: FiligreeDB) -> None:
        """Invalid issue_type should return 'validation_error', not 'not_found'."""
        obs = mcp_db.create_observation("test obs for type validation")
        result = await call_tool(
            "promote_observation",
            {"observation_id": obs["id"], "type": "nonexistent_type"},
        )
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "nonexistent_type" in data["error"]

    async def test_promote_missing_observation_id_rejected(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("promote_observation", {}))

        assert data["code"] == ErrorCode.VALIDATION
        assert data["error"] == "observation_id is required"

    async def test_promote_non_string_observation_id_rejected(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("promote_observation", {"observation_id": ["obs-1"]}))

        assert data["code"] == ErrorCode.VALIDATION
        assert data["error"] == "observation_id must be a string"

    @pytest.mark.parametrize("field", ["type", "title", "description"])
    async def test_promote_rejects_non_string_optional_fields(self, mcp_db: FiligreeDB, field: str) -> None:
        obs = mcp_db.create_observation("test obs for field validation")

        data = _parse(await call_tool("promote_observation", {"observation_id": obs["id"], field: ["bad"]}))

        assert data["code"] == ErrorCode.VALIDATION
        assert data["error"] == f"{field} must be a string"

    async def test_promote_refresh_sqlite_error_returns_io(self, mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        def _promote(self: FiligreeDB, *args: object, **kwargs: object) -> dict[str, object]:
            return {"issue": SimpleNamespace(id="filigree-refresh-target")}

        def _raise_sqlite_error(self: FiligreeDB, issue_id: str) -> object:
            raise sqlite3.OperationalError(f"issue refresh failed for {issue_id}")

        monkeypatch.setattr(FiligreeDB, "promote_observation", _promote)
        monkeypatch.setattr(FiligreeDB, "get_issue", _raise_sqlite_error)

        data = _parse(await call_tool("promote_observation", {"observation_id": "obs-1"}))

        assert data["code"] == ErrorCode.IO
        assert "issue refresh failed" in data["error"]

    async def test_promote_surfaces_warnings(self, mcp_db: FiligreeDB) -> None:
        """MCP handler surfaces warnings from promote_observation on enrichment failure."""
        from unittest.mock import patch

        obs = mcp_db.create_observation("will warn")
        with patch.object(mcp_db, "add_label", side_effect=sqlite3.OperationalError("label boom")):
            result = await call_tool("promote_observation", {"observation_id": obs["id"]})
        data = _parse(result)
        assert "issue_id" in data
        assert "warnings" in data
        assert any("label" in w for w in data["warnings"])


class TestListObservationsStatsGuard:
    """Verify _handle_list_observations handles list_observations() DB failures.

    The stats-fallback path was removed in Phase D2 (MCP list_observations no
    longer surfaces a ``stats`` sibling — consumers needing observation
    statistics use ``observation_stats`` directly via the dashboard or a
    future dedicated tool). The IO-error path remains relevant.
    """

    async def test_list_observations_catches_sqlite_error(self, mcp_db: FiligreeDB) -> None:
        """sqlite3.Error from list_observations itself returns error response."""
        import sqlite3
        from unittest.mock import patch

        with patch.object(mcp_db, "list_observations", side_effect=sqlite3.InterfaceError("connection closed")):
            result = await call_tool("list_observations", {})
        data = _parse(result)
        assert data["code"] == ErrorCode.IO


class TestPromoteExpiredObservationMCP:
    """filigree-0aef8403ca: MCP-layer test for promoting expired observation."""

    async def test_promote_expired_returns_validation_error(self, mcp_db: FiligreeDB) -> None:
        """Promoting an expired observation via MCP should return validation_error."""
        obs = mcp_db.create_observation("stale finding")
        mcp_db.conn.execute(
            "UPDATE observations SET expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (obs["id"],),
        )
        mcp_db.conn.commit()
        result = await call_tool("promote_observation", {"observation_id": obs["id"]})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
