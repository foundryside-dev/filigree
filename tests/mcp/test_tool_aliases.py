"""MCP tool-name namespacing (Phase-1 aliasing) — surface + seam tests.

The wire surface (``list_tools``) serves ONLY the namespaced ``<entity>_<verb>``
names. Both the new and the legacy/old name resolve through ``call_tool`` (the
old name stays the internal canonical identity), so the served rename does not
break callers still using the legacy names, and every downstream guard —
including the ``get_mcp_status`` degraded-mode exemptions — keeps working when
reached via the new name.
"""

from __future__ import annotations

import filigree.mcp_server as mcp_mod
from filigree.core import FiligreeDB
from filigree.mcp_server import call_tool, list_tools
from filigree.mcp_tools.rename import RENAME_MAP
from filigree.registry import RegistryVersionMismatchError
from filigree.types.api import ErrorCode, SchemaVersionMismatchError
from tests.mcp._helpers import _parse


class TestBothNamesResolve:
    async def test_old_and_new_name_return_same_result(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Alias round-trip")

        via_old = _parse(await call_tool("get_issue", {"issue_id": issue.id}))
        via_new = _parse(await call_tool("issue_get", {"issue_id": issue.id}))

        assert via_old == via_new
        assert via_new["issue_id"] == issue.id


class TestServedSurface:
    async def test_list_tools_serves_exactly_the_new_names(self) -> None:
        names = [tool.name for tool in await list_tools()]

        assert len(names) == 115
        assert len(set(names)) == 115, "served names must be unique"
        # Every served name is a NEW name; no OLD name leaks onto the surface.
        assert set(names) == set(RENAME_MAP.values())
        assert set(names) & set(RENAME_MAP.keys()) == set()


class TestUnknownTool:
    async def test_unknown_tool_returns_not_found(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("totally_unknown", {})
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND
        assert "totally_unknown" in data["error"]


class TestArgValidationThroughNewName:
    async def test_unknown_argument_on_new_name_is_validation_error(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Arg validation via new name")

        result = await call_tool("issue_get", {"issue_id": issue.id, "not_a_real_arg": 1})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

        # Same behaviour through the old name.
        result_old = await call_tool("get_issue", {"issue_id": issue.id, "not_a_real_arg": 1})
        assert _parse(result_old)["code"] == ErrorCode.VALIDATION


class TestDegradedModeReachability:
    """The key seam: a new name must still hit the get_mcp_status exemptions.

    call_tool exempts ``get_mcp_status`` from THREE degraded-mode guards, each
    keyed off the canonical name. Canonicalization happens before the guards, so
    the new ``mcp_status_get`` name resolves to ``get_mcp_status`` and stays
    exempt. Two of the three exemptions are exercised here:

    * ``_schema_mismatch`` (startup found a v+1 DB), and
    * ``_registry_startup_error`` (Loomweave advertised an incompatible registry
      API version).

    The third — the per-call runtime-drift gate (live ``PRAGMA user_version``
    > installed) — needs a forward-migrated DB under a live connection, which is
    out of scope for these in-process fixtures, so it is not exercised here.
    """

    async def test_mcp_status_reachable_via_new_name_under_schema_mismatch(self, mcp_db: FiligreeDB, monkeypatch) -> None:
        monkeypatch.setattr(
            mcp_mod,
            "_schema_mismatch",
            SchemaVersionMismatchError(installed=1, database=2),
        )

        # mcp_status_get -> get_mcp_status canonicalizes at the top, so the
        # `name != "get_mcp_status"` guard is False and the diagnostic status
        # payload is served — NOT the bare short-circuit SCHEMA_MISMATCH envelope
        # (which is only `{error, code}`). The status payload reports the
        # mismatch as diagnostic data, distinguishable by its `status` field and
        # full diagnostics.
        status = _parse(await call_tool("mcp_status_get", {}))
        assert status["status"] == "schema_mismatch"
        assert status["schema_compatible"] is False
        assert status["database_schema_version"] == 2
        assert "runtime" in status

        # A normal tool IS blocked with SCHEMA_MISMATCH, even via the new name.
        blocked = _parse(await call_tool("issue_get", {"issue_id": "anything"}))
        assert blocked["code"] == ErrorCode.SCHEMA_MISMATCH

    async def test_mcp_status_reachable_via_new_name_under_registry_startup_error(self, mcp_db: FiligreeDB, monkeypatch) -> None:
        monkeypatch.setattr(
            mcp_mod,
            "_registry_startup_error",
            RegistryVersionMismatchError(
                "incompatible registry api version",
                url="http://localhost:9111",
                expected=1,
                advertised=2,
            ),
        )

        # mcp_status_get -> get_mcp_status stays exempt from the registry guard,
        # so the diagnostic status payload is served (status field +
        # full diagnostics), NOT the bare CLARION_REGISTRY_VERSION_MISMATCH envelope.
        status = _parse(await call_tool("mcp_status_get", {}))
        assert status["status"] == "registry_version_mismatch"
        assert status["code"] == ErrorCode.CLARION_REGISTRY_VERSION_MISMATCH
        assert "runtime" in status

        # A normal tool IS blocked with the registry-mismatch envelope, via the new name.
        blocked = _parse(await call_tool("issue_get", {"issue_id": "anything"}))
        assert blocked["code"] == ErrorCode.CLARION_REGISTRY_VERSION_MISMATCH


class TestServedTaggingIntegrity:
    async def test_served_tools_preserve_hints_and_tier_markers(self) -> None:
        served = {tool.name: tool for tool in await list_tools()}

        assert served["issue_get"].annotations is not None
        assert served["issue_get"].annotations.readOnlyHint is True

        assert served["issue_delete"].annotations is not None
        assert served["issue_delete"].annotations.destructiveHint is True

        for name, tool in served.items():
            assert tool.description is not None, f"{name} has no description"
            assert "[tier:" in tool.description, f"{name} lost its tier marker"
