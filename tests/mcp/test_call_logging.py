"""Call-outcome logging on the MCP surface (Task 0.1).

Every ``call_tool`` emits one ``event="call"`` record whose ``outcome`` is
derived from the *returned* envelope, not only from raised exceptions.
"""

from __future__ import annotations

import jsonschema
import pytest
from mcp.types import CallToolRequest, CallToolRequestParams

import filigree.mcp_server as mcp_mod
from filigree.core import FiligreeDB
from filigree.mcp_server import call_tool
from tests.conftest import JsonLogCapture


async def test_error_envelope_logged_as_error(mcp_db: FiligreeDB, caplog_json: JsonLogCapture) -> None:
    await call_tool("issue_get", {"issue_id": "mcp-doesnotexist"})
    rec = caplog_json.last(event="call", surface="mcp")
    assert rec["outcome"] == "error"
    assert rec["code"] == "NOT_FOUND"


async def test_unknown_argument_rejection_logged_as_validation(mcp_db: FiligreeDB, caplog_json: JsonLogCapture) -> None:
    # Unknown arguments come back as a VALIDATION envelope (no exception in the
    # in-process path); the dead-end must still be visible in the log.
    await call_tool("issue_get", {"issue_id": "x", "bogus": 1})
    rec = caplog_json.last(event="call")
    assert rec["outcome"] == "validation"
    assert rec["code"] == "VALIDATION"


async def test_validator_exception_logged_as_validation_and_reraised(
    mcp_db: FiligreeDB, caplog_json: JsonLogCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(_arguments: dict[str, object]) -> list[object]:
        raise jsonschema.ValidationError("bad shape")

    monkeypatch.setitem(mcp_mod._all_handlers, "get_issue", _boom)
    with pytest.raises(jsonschema.ValidationError):
        await call_tool("issue_get", {"issue_id": "x"})
    assert caplog_json.last(event="call")["outcome"] == "validation"


async def test_handler_exception_logged_as_error_and_reraised(
    mcp_db: FiligreeDB, caplog_json: JsonLogCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(_arguments: dict[str, object]) -> list[object]:
        raise RuntimeError("kaboom")

    monkeypatch.setitem(mcp_mod._all_handlers, "get_issue", _boom)
    with pytest.raises(RuntimeError):
        await call_tool("issue_get", {"issue_id": "x"})
    rec = caplog_json.last(event="call")
    assert rec["outcome"] == "error"
    assert rec["code"] == "INTERNAL"


async def test_sdk_input_schema_rejection_logged_as_validation(mcp_db: FiligreeDB, caplog_json: JsonLogCapture) -> None:
    # The SDK validates ``inputSchema`` before ``call_tool`` runs and answers with
    # an isError result; without the handler wrapper that rejection is invisible.
    handler = mcp_mod.server.request_handlers[CallToolRequest]
    req = CallToolRequest(method="tools/call", params=CallToolRequestParams(name="issue_get", arguments={"issue_id": 123}))
    result = await handler(req)
    assert result.root.isError is True
    rec = caplog_json.last(event="call", surface="mcp")
    assert rec["outcome"] == "validation"
    assert rec["name"] == "issue_get"


async def test_no_op_sentinel_logged_as_no_op(mcp_db: FiligreeDB, caplog_json: JsonLogCapture) -> None:
    issue = mcp_db.create_issue("fresh issue")
    # Nobody holds a fresh issue: releasing it is the {"result": "no_op"} sentinel.
    await call_tool("work_release", {"issue_id": issue.id, "actor": "t"})
    assert caplog_json.last(event="call")["outcome"] == "no_op"


async def test_ok_call_has_full_record_shape(mcp_db: FiligreeDB, caplog_json: JsonLogCapture) -> None:
    issue = mcp_db.create_issue("shape")
    await call_tool("issue_get", {"issue_id": issue.id})
    rec = caplog_json.last(event="call", surface="mcp")
    assert rec["outcome"] == "ok"
    assert rec["code"] is None
    assert isinstance(rec["duration_ms"], float)
    assert "population" in rec
    # The legacy field other tooling reads is kept alongside the new ones.
    assert rec["args"] == {"issue_id": issue.id}


async def test_tool_call_uses_served_name(mcp_db: FiligreeDB, caplog_json: JsonLogCapture) -> None:
    await call_tool("work_ready", {})
    assert caplog_json.last(event="call")["name"] == "work_ready"


async def test_legacy_tool_name_is_not_logged(mcp_db: FiligreeDB, caplog_json: JsonLogCapture) -> None:
    await call_tool("get_issue", {"issue_id": "x"})  # removed legacy name -> NOT_FOUND
    rec = caplog_json.last(event="call")
    assert rec["name"] != "get_issue"
    assert rec["outcome"] == "error"
    assert rec["code"] == "NOT_FOUND"


async def test_population_read_from_project_config(mcp_db: FiligreeDB, caplog_json: JsonLogCapture) -> None:
    from filigree.core import read_config, write_config

    config = dict(read_config(mcp_db.meta_dir))
    config["population"] = "suite-construction"
    write_config(mcp_db.meta_dir, config)
    await call_tool("work_ready", {})
    assert caplog_json.last(event="call")["population"] == "suite-construction"


async def test_healthy_mcp_status_get_logged_ok(mcp_db: FiligreeDB, caplog_json: JsonLogCapture) -> None:
    # Healthy status carries ``error: null`` / ``code: null`` keys; that is not a dead-end.
    await call_tool("mcp_status_get", {})
    rec = caplog_json.last(event="call", surface="mcp")
    assert rec["name"] == "mcp_status_get"
    assert rec["outcome"] == "ok"
    assert rec["code"] is None
