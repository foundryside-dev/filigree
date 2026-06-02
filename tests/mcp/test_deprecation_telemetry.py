"""Deprecation telemetry for old-name tool calls (rollout plan §5.3).

When a caller reaches a tool via its DEPRECATED OLD name, ``call_tool`` records
the inbound wire name: it increments a module-level counter and emits a
structured ``deprecated_tool_name`` log event. This proves, before old names
are removed in Phase 2, whether consumers still depend on them. Calls made via
the NEW (served) names do NOT count.
"""

from __future__ import annotations

import logging
from collections.abc import Generator

import pytest

import filigree.mcp_server as mcp_mod
from filigree.core import FiligreeDB
from filigree.mcp_server import call_tool, get_mcp_status_payload
from tests.mcp._helpers import _parse


@pytest.fixture(autouse=True)
def _reset_deprecation_counter() -> Generator[None, None, None]:
    """Clear the module-level counter so counts don't leak across tests.

    Cleared *before* the test (other suites call old names through
    ``call_tool`` and increment it) and again after, for hygiene.
    """
    mcp_mod._deprecated_tool_calls.clear()
    yield
    mcp_mod._deprecated_tool_calls.clear()


class TestOldNameRecorded:
    async def test_old_name_increments_counter_and_status_payload(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Deprecation telemetry — old name")

        await call_tool("get_issue", {"issue_id": issue.id})

        assert mcp_mod._deprecated_tool_calls["get_issue"] == 1

        payload = get_mcp_status_payload()
        assert payload["status"] == "ok"
        depr = payload["deprecated_tool_name_calls"]
        assert depr["total"] >= 1
        assert depr["by_name"]["get_issue"] == 1

    async def test_status_payload_via_new_name_reflects_usage(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Deprecation telemetry — status surface")

        await call_tool("get_issue", {"issue_id": issue.id})

        # mcp_status_get is the served (new) name for get_mcp_status; calling it
        # must NOT count as deprecated, but it must report the prior old-name use.
        status = _parse(await call_tool("mcp_status_get", {}))
        assert status["deprecated_tool_name_calls"]["total"] >= 1
        assert status["deprecated_tool_name_calls"]["by_name"]["get_issue"] == 1
        # The status call itself (new name) did not increment.
        assert "mcp_status_get" not in mcp_mod._deprecated_tool_calls
        assert "get_mcp_status" not in mcp_mod._deprecated_tool_calls


class TestNewNameNotRecorded:
    async def test_new_name_does_not_increment_counter(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Deprecation telemetry — new name")

        await call_tool("issue_get", {"issue_id": issue.id})

        assert sum(mcp_mod._deprecated_tool_calls.values()) == 0

    async def test_unknown_name_does_not_increment_counter(self, mcp_db: FiligreeDB) -> None:
        await call_tool("totally_unknown", {})

        assert sum(mcp_mod._deprecated_tool_calls.values()) == 0


class TestStructuredLogEvent:
    async def test_old_name_emits_deprecated_tool_name_log(
        self,
        mcp_db: FiligreeDB,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # _logger is None under the mcp_db fixture (only set by _attempt_startup
        # / serve), so the helper's ``if _logger`` guard would skip emission.
        # Install a real, propagating logger so caplog captures the record.
        logger = logging.getLogger("filigree.mcp_server")
        monkeypatch.setattr(mcp_mod, "_logger", logger)

        issue = mcp_db.create_issue("Deprecation telemetry — log event")

        with caplog.at_level(logging.INFO, logger="filigree.mcp_server"):
            await call_tool("get_issue", {"issue_id": issue.id, "actor": "tester"})

        records = [r for r in caplog.records if r.message == "deprecated_tool_name"]
        assert len(records) == 1
        record = records[0]
        assert record.tool == "get_issue"  # type: ignore[attr-defined]
        assert record.canonical == "issue_get"  # type: ignore[attr-defined]
        assert record.actor == "tester"  # type: ignore[attr-defined]

    async def test_new_name_emits_no_deprecation_log(
        self,
        mcp_db: FiligreeDB,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        logger = logging.getLogger("filigree.mcp_server")
        monkeypatch.setattr(mcp_mod, "_logger", logger)

        issue = mcp_db.create_issue("Deprecation telemetry — new name no log")

        with caplog.at_level(logging.INFO, logger="filigree.mcp_server"):
            await call_tool("issue_get", {"issue_id": issue.id})

        assert not [r for r in caplog.records if r.message == "deprecated_tool_name"]
