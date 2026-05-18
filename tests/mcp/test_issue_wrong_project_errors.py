"""MCP issue-tool error code regressions for foreign project IDs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from filigree.core import FiligreeDB
from filigree.mcp_tools.issues import (
    _handle_claim_issue,
    _handle_close_issue,
    _handle_heartbeat_work,
    _handle_reclaim_issue,
    _handle_release_claim,
    _handle_reopen_issue,
    _handle_start_work,
)
from filigree.types.api import ErrorCode
from tests.mcp._helpers import _parse

Handler = Callable[[dict[str, Any]], Awaitable[list[Any]]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "args"),
    [
        (_handle_close_issue, {"issue_id": "other-1234567890"}),
        (_handle_reopen_issue, {"issue_id": "other-1234567890"}),
        (_handle_claim_issue, {"issue_id": "other-1234567890", "assignee": "agent"}),
        (_handle_release_claim, {"issue_id": "other-1234567890"}),
        (_handle_heartbeat_work, {"issue_id": "other-1234567890", "actor": "agent"}),
        (
            _handle_reclaim_issue,
            {
                "issue_id": "other-1234567890",
                "assignee": "new-agent",
                "expected_assignee": "old-agent",
                "reason": "stale",
            },
        ),
        (_handle_start_work, {"issue_id": "other-1234567890", "assignee": "agent"}),
    ],
)
async def test_wrong_project_issue_ids_are_validation_errors(
    mcp_db: FiligreeDB,
    handler: Handler,
    args: dict[str, Any],
) -> None:
    assert mcp_db.prefix == "mcp"

    data = _parse(await handler(args))

    assert data["code"] == ErrorCode.VALIDATION
    # 2.1.0 §1.2: MCP serialisation uses ``WrongProjectError.safe_message``,
    # so the offending prefix no longer round-trips. The canonical safe
    # wording is asserted instead.
    from filigree.core import WrongProjectError

    assert data["error"] == WrongProjectError.SAFE_MESSAGE
    assert "other-" not in data["error"]
