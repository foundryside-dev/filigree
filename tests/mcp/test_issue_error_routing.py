"""MCP issue error-code routing regressions."""

from __future__ import annotations

from typing import Any

import pytest

from filigree.core import FiligreeDB
from filigree.mcp_server import call_tool  # type: ignore[attr-defined]
from filigree.types.api import ErrorCode
from tests.mcp._helpers import _parse

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    ("tool_name", "db_method", "extra_args"),
    [
        ("issue_reopen", "reopen_issue", {}),
        ("issue_close", "close_issue", {"reason": "done"}),
        ("work_release", "release_claim", {"actor": "agent"}),
        ("work_heartbeat", "heartbeat_work", {"actor": "agent"}),
        (
            "work_reclaim",
            "reclaim_issue",
            {"assignee": "new-agent", "expected_assignee": "old-agent", "reason": "stale", "actor": "agent"},
        ),
    ],
)
async def test_issue_write_residual_value_errors_are_validation(
    mcp_db: FiligreeDB,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    db_method: str,
    extra_args: dict[str, Any],
) -> None:
    issue = mcp_db.create_issue("residual validation routing", priority=2)

    def raise_validation(*_args: object, **_kwargs: object) -> object:
        raise ValueError("synthetic validation failure")

    monkeypatch.setattr(mcp_db, db_method, raise_validation)

    result = _parse(await call_tool(tool_name, {"issue_id": issue.id, **extra_args}))

    assert result["code"] == ErrorCode.VALIDATION
    assert result["error"] == "synthetic validation failure"
