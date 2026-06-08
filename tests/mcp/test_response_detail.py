"""MCP-side tests for ``response_detail`` opt-in on batch tools.

Closes the gap where the agent guidance promised ``response_detail="full"``
on every batch tool but only the dashboard HTTP routes implemented it.
The matching CLI side lives in ``tests/cli/test_response_detail.py``.
"""

from __future__ import annotations

import pytest

from filigree.core import FiligreeDB
from filigree.mcp_server import call_tool  # type: ignore[attr-defined]
from filigree.types.api import ErrorCode, parse_response_detail
from tests._seeds import seed_file, seed_finding, seed_observations
from tests.mcp._helpers import _parse

_SLIM_KEYS = {"issue_id", "title", "status", "priority", "type"}
# Keys present on PublicIssue and never on SlimIssue — proof "full" returned more.
_FULL_ONLY_KEYS = {"description", "labels", "blocks", "blocked_by", "is_ready", "fields"}


# ---------------------------------------------------------------------------
# Shared parser
# ---------------------------------------------------------------------------


class TestParseResponseDetail:
    def test_default_is_slim(self) -> None:
        assert parse_response_detail(None) == "slim"

    def test_explicit_slim(self) -> None:
        assert parse_response_detail("slim") == "slim"

    def test_full(self) -> None:
        assert parse_response_detail("full") == "full"

    def test_invalid_returns_validation_error(self) -> None:
        result = parse_response_detail("medium")
        assert isinstance(result, dict)
        assert result["code"] == ErrorCode.VALIDATION
        assert "medium" in result["error"]


# ---------------------------------------------------------------------------
# Issue batch tools (batch_close, batch_update)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestIssueBatchDetail:
    async def test_batch_close_slim_default(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("A")
        b = mcp_db.create_issue("B")
        result = await call_tool("issue_batch_close", {"issue_ids": [a.id, b.id]})
        data = _parse(result)
        assert len(data["succeeded"]) == 2
        for item in data["succeeded"]:
            assert set(item.keys()) == _SLIM_KEYS

    async def test_batch_close_full(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("A", description="full-detail trigger")
        result = await call_tool("issue_batch_close", {"issue_ids": [a.id], "response_detail": "full"})
        data = _parse(result)
        item = data["succeeded"][0]
        assert set(item.keys()) >= _FULL_ONLY_KEYS
        assert item["description"] == "full-detail trigger"

    async def test_batch_update_slim_default(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("A")
        result = await call_tool("issue_batch_update", {"issue_ids": [a.id], "priority": 0})
        data = _parse(result)
        assert set(data["succeeded"][0].keys()) == _SLIM_KEYS

    async def test_batch_update_full(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("A", description="d")
        result = await call_tool(
            "issue_batch_update",
            {"issue_ids": [a.id], "priority": 0, "response_detail": "full"},
        )
        data = _parse(result)
        assert set(data["succeeded"][0].keys()) >= _FULL_ONLY_KEYS

    async def test_batch_close_invalid_detail_returns_validation(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("A")
        result = await call_tool("issue_batch_close", {"issue_ids": [a.id], "response_detail": "medium"})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_batch_update_invalid_detail_returns_validation(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("A")
        result = await call_tool(
            "issue_batch_update",
            {"issue_ids": [a.id], "priority": 0, "response_detail": "medium"},
        )
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION


# ---------------------------------------------------------------------------
# Meta batch tools (batch_add_label, batch_add_comment)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMetaBatchDetail:
    async def test_batch_add_label_slim_default(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("A")
        result = await call_tool("label_batch_add", {"issue_ids": [a.id], "label": "x"})
        data = _parse(result)
        assert data["succeeded"] == [a.id]

    async def test_batch_add_label_full(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("A", description="d")
        result = await call_tool(
            "label_batch_add",
            {"issue_ids": [a.id], "label": "x", "response_detail": "full"},
        )
        data = _parse(result)
        item = data["succeeded"][0]
        assert isinstance(item, dict)
        assert set(item.keys()) >= _FULL_ONLY_KEYS
        assert "x" in item["labels"]

    async def test_batch_remove_label_slim_default(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("A", labels=["x"])
        result = await call_tool("label_batch_remove", {"issue_ids": [a.id], "label": "x"})
        data = _parse(result)
        assert data["succeeded"] == [a.id]

    async def test_batch_remove_label_full(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("A", description="d", labels=["x"])
        result = await call_tool(
            "label_batch_remove",
            {"issue_ids": [a.id], "label": "x", "response_detail": "full"},
        )
        data = _parse(result)
        item = data["succeeded"][0]
        assert isinstance(item, dict)
        assert set(item.keys()) >= _FULL_ONLY_KEYS
        assert "x" not in item["labels"]

    async def test_batch_add_comment_slim_default(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("A")
        result = await call_tool("comment_batch_add", {"issue_ids": [a.id], "text": "hi"})
        data = _parse(result)
        assert data["succeeded"] == [a.id]

    async def test_batch_add_comment_full(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("A", description="d")
        result = await call_tool(
            "comment_batch_add",
            {"issue_ids": [a.id], "text": "hi", "response_detail": "full"},
        )
        data = _parse(result)
        item = data["succeeded"][0]
        assert isinstance(item, dict)
        assert set(item.keys()) >= _FULL_ONLY_KEYS


# ---------------------------------------------------------------------------
# Findings batch (batch_update_findings)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestFindingsBatchDetail:
    async def test_batch_update_findings_slim_default(self, mcp_db: FiligreeDB) -> None:
        file_id = seed_file(mcp_db, path="src/x.py")
        finding_id = seed_finding(mcp_db, file_id=file_id)
        result = await call_tool("finding_batch_update", {"finding_ids": [finding_id], "status": "fixed"})
        data = _parse(result)
        assert data["succeeded"] == [finding_id]

    async def test_batch_update_findings_full(self, mcp_db: FiligreeDB) -> None:
        file_id = seed_file(mcp_db, path="src/y.py")
        finding_id = seed_finding(mcp_db, file_id=file_id)
        result = await call_tool(
            "finding_batch_update",
            {"finding_ids": [finding_id], "status": "fixed", "response_detail": "full"},
        )
        data = _parse(result)
        item = data["succeeded"][0]
        assert isinstance(item, dict)
        assert "finding_id" in item
        assert "id" not in item
        assert item["status"] == "fixed"


# ---------------------------------------------------------------------------
# Observations batch (batch_dismiss_observations)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestObservationsBatchDetail:
    async def test_batch_dismiss_slim_default(self, mcp_db: FiligreeDB) -> None:
        ids = seed_observations(mcp_db, count=2)
        result = await call_tool("observation_batch_dismiss", {"observation_ids": ids})
        data = _parse(result)
        assert set(data["succeeded"]) == set(ids)

    async def test_batch_dismiss_full(self, mcp_db: FiligreeDB) -> None:
        ids = seed_observations(mcp_db, count=2)
        result = await call_tool(
            "observation_batch_dismiss",
            {"observation_ids": ids, "response_detail": "full"},
        )
        data = _parse(result)
        assert len(data["succeeded"]) == 2
        for item in data["succeeded"]:
            assert isinstance(item, dict)
            assert {"observation_id", "summary"} <= set(item.keys())
            assert "id" not in item

    async def test_batch_promote_slim_default(self, mcp_db: FiligreeDB) -> None:
        ids = seed_observations(mcp_db, count=2)
        result = await call_tool("observation_batch_promote", {"observation_ids": ids})
        data = _parse(result)
        assert len(data["succeeded"]) == 2
        for item in data["succeeded"]:
            assert set(item.keys()) == _SLIM_KEYS

    async def test_batch_promote_full(self, mcp_db: FiligreeDB) -> None:
        ids = seed_observations(mcp_db, count=2)
        result = await call_tool(
            "observation_batch_promote",
            {"observation_ids": ids, "response_detail": "full"},
        )
        data = _parse(result)
        assert len(data["succeeded"]) == 2
        for item in data["succeeded"]:
            assert isinstance(item, dict)
            assert set(item.keys()) >= _FULL_ONLY_KEYS
