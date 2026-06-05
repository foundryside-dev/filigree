"""MCP-stdio transport-bound actor identity tests (ADR-012, schema v24).

The real entry point is ``mcp_server._attempt_startup(filigree_dir, conf_path)``
(there is no ``_init_db``). It sets the module-global ``db``, so each test
monkeypatches the relevant globals (mirroring ``test_relocated_db_startup``) and
closes the DB in a finally to avoid leaking state into the broad suite.

``asyncio_mode = "auto"`` (pyproject) runs ``async def test_*`` without a marker.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.types import TextContent

import filigree.mcp_server as mcp_server
from filigree.core import DB_FILENAME, FILIGREE_DIR_NAME, SUMMARY_FILENAME, FiligreeDB, write_config
from filigree.mcp_tools.common import _inject_warnings


def _make_project(tmp_path: Path) -> Path:
    """Build a legacy-layout project and return its ``.filigree`` dir."""
    filigree_dir = tmp_path / FILIGREE_DIR_NAME
    filigree_dir.mkdir()
    write_config(filigree_dir, {"prefix": "mcp", "version": 1})
    (filigree_dir / SUMMARY_FILENAME).write_text("# test\n")
    seed = FiligreeDB(filigree_dir / DB_FILENAME, prefix="mcp")
    seed.initialize()
    seed.close()
    return filigree_dir


def _reset_globals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "db", None)
    monkeypatch.setattr(mcp_server, "_filigree_dir", None)
    monkeypatch.setattr(mcp_server, "_schema_mismatch", None)
    monkeypatch.setattr(mcp_server, "_registry_startup_error", None)
    monkeypatch.setattr(mcp_server, "_db_open_error", None)


def test_attempt_startup_sets_verified_actor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("filigree.actor_identity.resolve_os_actor", lambda: "alice")
    _reset_globals(monkeypatch)
    filigree_dir = _make_project(tmp_path)
    mcp_server._attempt_startup(filigree_dir)
    try:
        assert mcp_server.db is not None
        assert mcp_server.db._verified_actor == "alice"
    finally:
        if mcp_server.db is not None:
            mcp_server._tool_locks.pop(mcp_server.db, None)
            mcp_server.db.close()


async def test_call_tool_injects_actor_mismatch_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("filigree.actor_identity.resolve_os_actor", lambda: "alice")
    _reset_globals(monkeypatch)
    filigree_dir = _make_project(tmp_path)
    mcp_server._attempt_startup(filigree_dir)
    try:
        result = await mcp_server.call_tool("issue_create", {"type": "task", "title": "t", "actor": "agent-x"})
        payload = json.loads(result[0].text)
        assert "warnings" in payload
        assert any(w["code"] == "ACTOR_MISMATCH" for w in payload["warnings"])
    finally:
        if mcp_server.db is not None:
            mcp_server._tool_locks.pop(mcp_server.db, None)
            mcp_server.db.close()


async def test_call_tool_no_warning_for_placeholder_actor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("filigree.actor_identity.resolve_os_actor", lambda: "alice")
    _reset_globals(monkeypatch)
    filigree_dir = _make_project(tmp_path)
    mcp_server._attempt_startup(filigree_dir)
    try:
        result = await mcp_server.call_tool("issue_create", {"type": "task", "title": "t", "actor": "mcp"})
        assert "ACTOR_MISMATCH" not in result[0].text  # placeholder claim → no warning
    finally:
        if mcp_server.db is not None:
            mcp_server._tool_locks.pop(mcp_server.db, None)
            mcp_server.db.close()


# ---------------------------------------------------------------------------
# _inject_warnings: pure-function edge branches (no DB needed)
# ---------------------------------------------------------------------------

_MISMATCH = {"code": "ACTOR_MISMATCH", "claimed": "agent-x", "verified": "alice"}


def test_inject_warnings_appends_to_existing() -> None:
    result = [TextContent(type="text", text=json.dumps({"ok": True, "warnings": [{"code": "X"}]}))]
    out = _inject_warnings(result, [_MISMATCH])
    payload = json.loads(out[0].text)
    assert payload["warnings"] == [{"code": "X"}, _MISMATCH]  # existing first, new appended


def test_inject_warnings_creates_array_when_absent() -> None:
    result = [TextContent(type="text", text=json.dumps({"ok": True}))]
    out = _inject_warnings(result, [_MISMATCH])
    payload = json.loads(out[0].text)
    assert payload["ok"] is True
    assert payload["warnings"] == [_MISMATCH]


def test_inject_warnings_leaves_bare_string_untouched() -> None:
    result = [TextContent(type="text", text="hello")]
    out = _inject_warnings(result, [_MISMATCH])
    assert out is result
    assert out[0].text == "hello"


def test_inject_warnings_leaves_non_dict_json_untouched() -> None:
    result = [TextContent(type="text", text="[1, 2]")]
    out = _inject_warnings(result, [_MISMATCH])
    assert out is result
    assert out[0].text == "[1, 2]"


def test_inject_warnings_empty_warnings_is_noop() -> None:
    result = [TextContent(type="text", text=json.dumps({"ok": True}))]
    out = _inject_warnings(result, [])
    assert out is result
