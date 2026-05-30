"""Regression tests for MCP boundary validation / error-escape cluster.

Covers:
- filigree-26be54f783: remove_dependency WrongProjectError escape
- filigree-36c7b6e18e: claim_issue/claim_next blank assignee
- filigree-bfbdfd0b60: promote_finding priority type validation
- filigree-772691017d: _resolve_pagination malformed inputs
- filigree-d5935566fd: _handle_observe bad-type args
- filigree-e87d310708: create_plan deps accepts floats
"""

from __future__ import annotations

import pytest

from filigree.core import FiligreeDB
from filigree.mcp_server import call_tool
from filigree.types.api import ErrorCode
from tests.mcp._helpers import _parse

# ---------------------------------------------------------------------------
# filigree-26be54f783: MCP remove_dependency lets WrongProjectError escape
# ---------------------------------------------------------------------------


class TestRemoveDependencyWrongProject:
    async def test_wrong_project_prefix_returns_structured_error(self, mcp_db: FiligreeDB) -> None:
        """remove_dependency with cross-project ID returns ErrorResponse, not exception."""
        a = mcp_db.create_issue("A")
        # mcp_db uses prefix "mcp"; a foreign-prefix id triggers WrongProjectError
        result = await call_tool(
            "remove_dependency",
            {"from_issue_id": a.id, "to_issue_id": "other-deadbeef12"},
        )
        data = _parse(result)
        assert isinstance(data, dict), f"expected error dict, got {data!r}"
        assert data.get("code") == ErrorCode.VALIDATION, data

    async def test_missing_dep_still_returns_not_found_status(self, mcp_db: FiligreeDB) -> None:
        """Removing non-existent dep between valid ids returns status=not_found (not an error)."""
        a = mcp_db.create_issue("A")
        b = mcp_db.create_issue("B")
        result = await call_tool(
            "remove_dependency",
            {"from_issue_id": a.id, "to_issue_id": b.id},
        )
        data = _parse(result)
        assert data.get("status") == "open"
        assert data.get("dependency_result") == "not_found"

    async def test_missing_same_prefix_source_returns_not_found_error(self, mcp_db: FiligreeDB) -> None:
        """Removing from a missing issue returns NOT_FOUND, not a not_found edge status."""
        b = mcp_db.create_issue("B")
        result = await call_tool(
            "remove_dependency",
            {"from_issue_id": "mcp-0000000000", "to_issue_id": b.id},
        )
        data = _parse(result)
        assert data.get("code") == ErrorCode.NOT_FOUND, data


# ---------------------------------------------------------------------------
# filigree-36c7b6e18e: MCP claim_issue/claim_next blank assignee
# ---------------------------------------------------------------------------


class TestClaimBlankAssignee:
    async def test_claim_issue_blank_assignee_with_actor_is_validation_error(self, mcp_db: FiligreeDB) -> None:
        """claim_issue with explicit actor but blank assignee must be validation_error."""
        issue = mcp_db.create_issue("Target")
        result = await call_tool(
            "claim_issue",
            {"id": issue.id, "assignee": "", "actor": "user"},
        )
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION, data

    async def test_claim_issue_whitespace_assignee_is_validation_error(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Target")
        result = await call_tool(
            "claim_issue",
            {"id": issue.id, "assignee": "   ", "actor": "user"},
        )
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION, data

    async def test_claim_next_blank_assignee_with_actor_returns_error(self, mcp_db: FiligreeDB) -> None:
        """claim_next with explicit actor but blank assignee must be validation_error (not crash)."""
        mcp_db.create_issue("Target")
        result = await call_tool(
            "claim_next",
            {"assignee": "", "actor": "user"},
        )
        data = _parse(result)
        assert isinstance(data, dict), f"claim_next crashed instead of returning error: {data!r}"
        assert data.get("code") == ErrorCode.VALIDATION, data

    async def test_claim_next_whitespace_assignee_returns_error(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("Target")
        result = await call_tool(
            "claim_next",
            {"assignee": "\t\t", "actor": "user"},
        )
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION, data


# ---------------------------------------------------------------------------
# filigree-bfbdfd0b60: promote_finding priority TypeError
# ---------------------------------------------------------------------------


@pytest.fixture
def finding_id(mcp_db: FiligreeDB) -> str:
    """Create a single finding for promotion tests."""
    mcp_db.register_file("src/a.py", language="python")
    result = mcp_db.process_scan_results(
        scan_source="test-scanner",
        scan_run_id="run-1",
        findings=[
            {"path": "src/a.py", "rule_id": "R1", "severity": "high", "message": "bad"},
        ],
    )
    ids = result["new_finding_ids"]
    assert ids, "expected at least one finding"
    return ids[0]


class TestPromoteFindingPriority:
    async def test_string_priority_rejected_as_validation_error(self, mcp_db: FiligreeDB, finding_id: str) -> None:
        """String priority must return validation_error, not TypeError."""
        result = await call_tool(
            "promote_finding",
            {"finding_id": finding_id, "priority": "high"},
        )
        data = _parse(result)
        assert isinstance(data, dict), f"crashed: {data!r}"
        assert data.get("code") == ErrorCode.VALIDATION, data

    async def test_float_priority_rejected(self, mcp_db: FiligreeDB, finding_id: str) -> None:
        result = await call_tool(
            "promote_finding",
            {"finding_id": finding_id, "priority": 2.5},
        )
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION, data

    async def test_out_of_range_priority_rejected(self, mcp_db: FiligreeDB, finding_id: str) -> None:
        result = await call_tool(
            "promote_finding",
            {"finding_id": finding_id, "priority": 99},
        )
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION, data

    async def test_bool_priority_rejected(self, mcp_db: FiligreeDB, finding_id: str) -> None:
        """bool is an int subclass but must be rejected (True would silently become priority=1)."""
        result = await call_tool(
            "promote_finding",
            {"finding_id": finding_id, "priority": True},
        )
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION, data


# ---------------------------------------------------------------------------
# filigree-772691017d: _resolve_pagination malformed inputs
# ---------------------------------------------------------------------------


class TestPaginationValidation:
    async def test_string_limit_rejected(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("search_issues", {"query": "x", "limit": "5"})
        data = _parse(result)
        assert isinstance(data, dict), f"crashed: {data!r}"
        assert data.get("code") == ErrorCode.VALIDATION, data

    async def test_negative_limit_rejected(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("list_issues", {"limit": -1})
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION, data

    async def test_negative_offset_rejected(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("list_issues", {"offset": -1})
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION, data

    async def test_string_no_limit_rejected(self, mcp_db: FiligreeDB) -> None:
        """'false' as string must not be treated as truthy."""
        result = await call_tool("search_issues", {"query": "x", "no_limit": "false"})
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION, data

    async def test_bool_limit_rejected(self, mcp_db: FiligreeDB) -> None:
        """Bool is int subclass — must not be accepted as limit."""
        result = await call_tool("list_issues", {"limit": True})
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION, data

    async def test_no_limit_huge_limit_rejected(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("list_issues", {"no_limit": True, "limit": 9223372036854775807})
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION, data

    async def test_huge_offset_rejected(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("list_observations", {"offset": 9223372036854775808})
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION, data


# ---------------------------------------------------------------------------
# filigree-d5935566fd: _handle_observe bad-type args
# ---------------------------------------------------------------------------


class TestObserveValidation:
    async def test_bool_priority_rejected(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("observe", {"summary": "x", "priority": True})
        data = _parse(result)
        assert isinstance(data, dict), f"crashed: {data!r}"
        assert data.get("code") == ErrorCode.VALIDATION, data

    async def test_string_line_rejected(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("observe", {"summary": "x", "line": "42"})
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION, data

    async def test_non_string_file_path_rejected(self, mcp_db: FiligreeDB) -> None:
        """file_path=42 would crash in _normalize_scan_path via .replace()."""
        result = await call_tool("observe", {"summary": "x", "file_path": 42})
        data = _parse(result)
        assert isinstance(data, dict), f"crashed: {data!r}"
        assert data.get("code") == ErrorCode.VALIDATION, data

    async def test_non_string_detail_rejected(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("observe", {"summary": "x", "detail": {"nope": 1}})
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION, data


# ---------------------------------------------------------------------------
# filigree-e87d310708: create_plan deps accepts floats
# ---------------------------------------------------------------------------


class TestCreatePlanDeps:
    async def test_float_dep_rejected(self, mcp_db: FiligreeDB) -> None:
        """Float 0.1 must not be reinterpreted as 'phase 0 step 1' cross-phase dep."""
        result = await call_tool(
            "create_plan",
            {
                "milestone": {"title": "M"},
                "phases": [
                    {
                        "title": "P0",
                        "steps": [
                            {"title": "S0"},
                            {"title": "S1", "deps": [0.1]},
                        ],
                    }
                ],
            },
        )
        data = _parse(result)
        assert isinstance(data, dict), f"crashed: {data!r}"
        assert data.get("code") == ErrorCode.VALIDATION, data

    async def test_bool_dep_rejected(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool(
            "create_plan",
            {
                "milestone": {"title": "M"},
                "phases": [{"title": "P0", "steps": [{"title": "S0"}, {"title": "S1", "deps": [True]}]}],
            },
        )
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION, data

    async def test_object_dep_rejected(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool(
            "create_plan",
            {
                "milestone": {"title": "M"},
                "phases": [{"title": "P0", "steps": [{"title": "S0"}, {"title": "S1", "deps": [{"bad": 1}]}]}],
            },
        )
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION, data

    async def test_malformed_string_dep_rejected(self, mcp_db: FiligreeDB) -> None:
        """'1.2.3' or 'abc' must be rejected, not crash via int()."""
        result = await call_tool(
            "create_plan",
            {
                "milestone": {"title": "M"},
                "phases": [{"title": "P0", "steps": [{"title": "S0"}, {"title": "S1", "deps": ["abc"]}]}],
            },
        )
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION, data

    async def test_valid_int_dep_still_works(self, mcp_db: FiligreeDB) -> None:
        """Valid same-phase int dep still creates plan."""
        result = await call_tool(
            "create_plan",
            {
                "milestone": {"title": "M"},
                "phases": [{"title": "P0", "steps": [{"title": "S0"}, {"title": "S1", "deps": [0]}]}],
            },
        )
        data = _parse(result)
        assert "milestone" in data, data

    async def test_valid_cross_phase_string_dep_still_works(self, mcp_db: FiligreeDB) -> None:
        """Valid 'p.s' cross-phase string dep still creates plan."""
        result = await call_tool(
            "create_plan",
            {
                "milestone": {"title": "M"},
                "phases": [
                    {"title": "P0", "steps": [{"title": "S0"}]},
                    {"title": "P1", "steps": [{"title": "S0", "deps": ["0.0"]}]},
                ],
            },
        )
        data = _parse(result)
        assert "milestone" in data, data


class TestCreatePlanMalformedOptionalFields:
    @pytest.mark.parametrize(
        "payload",
        [
            {"milestone": {"title": "MS", "fields": ["not", "a", "dict"]}, "phases": []},
            {"milestone": {"title": "MS", "description": []}, "phases": []},
            {"milestone": {"title": "MS"}, "phases": [{"title": "P", "fields": ["bad"]}]},
            {"milestone": {"title": "MS"}, "phases": [{"title": "P", "steps": [{"title": "S", "description": []}]}]},
        ],
    )
    async def test_malformed_optional_fields_return_validation(self, mcp_db: FiligreeDB, payload: dict[str, object]) -> None:
        result = await call_tool("create_plan", payload)
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION, data
