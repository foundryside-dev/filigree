"""Sync test: MCP JSON Schema <-> TypedDict structural agreement.

Parametrized test that introspects each MCP tool's inputSchema and verifies
the corresponding TypedDict's keys and required/optional annotations match.
Analogous to test_mixin_contracts.py but for the MCP input boundary.

Scope: This test verifies *structural* agreement (key names and
required/optional status). It does NOT verify type-level agreement
(e.g. str vs int) — that is left to mypy via the TypedDict annotations.
"""

from __future__ import annotations

import importlib
from typing import get_type_hints

import pytest
from mcp.types import Tool

from filigree.types.inputs import TOOL_ARGS_MAP

# ---------------------------------------------------------------------------
# Discovery: collect all MCP tools from all modules
# ---------------------------------------------------------------------------

_MCP_MODULES = [
    "filigree.mcp_tools.issues",
    "filigree.mcp_tools.planning",
    "filigree.mcp_tools.meta",
    "filigree.mcp_tools.workflow",
    "filigree.mcp_tools.files",
    "filigree.mcp_tools.annotations",
    "filigree.mcp_tools.observations",
    "filigree.mcp_tools.scanners",
    "filigree.mcp_tools.entities",
    "filigree.mcp_tools.federation",
]


def _discover_tools() -> list[tuple[str, Tool]]:
    """Call register() on each MCP module, collect (tool_name, Tool) pairs."""
    result: list[tuple[str, Tool]] = []
    for mod_path in _MCP_MODULES:
        mod = importlib.import_module(mod_path)
        # Scanner module needs include_legacy=True to include all tools
        if mod_path.endswith(".scanners"):
            tools, _ = mod.register(include_legacy=True)
        else:
            tools, _ = mod.register()
        for tool in tools:
            result.append((tool.name, tool))
    return result


_ALL_TOOLS = _discover_tools()

# Tools with non-empty properties (need TypedDicts)
_TOOLS_WITH_ARGS = [(name, tool) for name, tool in _ALL_TOOLS if tool.inputSchema.get("properties", {})]

# Tools with empty properties (should NOT be in TOOL_ARGS_MAP)
_TOOLS_WITHOUT_ARGS = [(name, tool) for name, tool in _ALL_TOOLS if not tool.inputSchema.get("properties", {})]


# ---------------------------------------------------------------------------
# Section 1: Structural sync — keys and required/optional
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool_name", "tool"),
    _TOOLS_WITH_ARGS,
    ids=[name for name, _ in _TOOLS_WITH_ARGS],
)
class TestSchemaTypedDictSync:
    """Verify each tool's JSON Schema matches its TypedDict structurally."""

    def test_typeddict_registered(self, tool_name: str, tool: Tool) -> None:
        """Every tool with arguments must have a TypedDict in TOOL_ARGS_MAP."""
        assert tool_name in TOOL_ARGS_MAP, (
            f"Tool '{tool_name}' has inputSchema properties but no TypedDict in TOOL_ARGS_MAP. Add one to types/inputs.py."
        )

    def test_keys_match(self, tool_name: str, tool: Tool) -> None:
        """TypedDict annotation keys == JSON Schema property keys."""
        if tool_name not in TOOL_ARGS_MAP:
            pytest.skip("No TypedDict registered yet")
        td_cls = TOOL_ARGS_MAP[tool_name]
        schema_keys = set(tool.inputSchema.get("properties", {}).keys())
        td_keys = set(get_type_hints(td_cls).keys())
        assert td_keys == schema_keys, (
            f"Key mismatch for '{tool_name}':\n  TypedDict extra: {td_keys - schema_keys}\n  Schema extra:    {schema_keys - td_keys}"
        )

    def test_required_fields_match(self, tool_name: str, tool: Tool) -> None:
        """TypedDict __required_keys__ == JSON Schema required array."""
        if tool_name not in TOOL_ARGS_MAP:
            pytest.skip("No TypedDict registered yet")
        td_cls = TOOL_ARGS_MAP[tool_name]
        schema_required = set(tool.inputSchema.get("required", []))
        # Python 3.11+ TypedDict exposes __required_keys__ / __optional_keys__
        td_required = td_cls.__required_keys__  # type: ignore[attr-defined]
        assert td_required == schema_required, (
            f"Required mismatch for '{tool_name}':\n  TypedDict required: {td_required}\n  Schema required:    {schema_required}"
        )

    def test_optional_fields_match(self, tool_name: str, tool: Tool) -> None:
        """TypedDict __optional_keys__ == schema properties minus required."""
        if tool_name not in TOOL_ARGS_MAP:
            pytest.skip("No TypedDict registered yet")
        td_cls = TOOL_ARGS_MAP[tool_name]
        schema_props = set(tool.inputSchema.get("properties", {}).keys())
        schema_required = set(tool.inputSchema.get("required", []))
        schema_optional = schema_props - schema_required
        td_optional = td_cls.__optional_keys__  # type: ignore[attr-defined]
        assert td_optional == schema_optional, (
            f"Optional mismatch for '{tool_name}':\n  TypedDict optional: {td_optional}\n  Schema optional:    {schema_optional}"
        )


# ---------------------------------------------------------------------------
# Section 2: No-arg tools should NOT be in the map
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool_name", "tool"),
    _TOOLS_WITHOUT_ARGS,
    ids=[name for name, _ in _TOOLS_WITHOUT_ARGS],
)
def test_no_arg_tool_excluded(tool_name: str, tool: Tool) -> None:
    """Tools with empty inputSchema should not have a TypedDict mapping."""
    assert tool_name not in TOOL_ARGS_MAP, (
        f"Tool '{tool_name}' has no inputSchema properties but is registered in TOOL_ARGS_MAP — remove it."
    )


# ---------------------------------------------------------------------------
# Section 3: Coverage guards
# ---------------------------------------------------------------------------


def test_all_mcp_modules_covered() -> None:
    """Ensure we're scanning all mcp_tools modules."""
    from pathlib import Path

    mcp_dir = Path(__file__).resolve().parents[2] / "src" / "filigree" / "mcp_tools"
    helper_modules = {"__init__", "common", "payloads", "tiers", "rename"}
    actual_modules = {f.stem for f in mcp_dir.glob("*.py") if f.stem not in helper_modules}
    scanned_modules = {m.rsplit(".", 1)[-1] for m in _MCP_MODULES}
    assert actual_modules == scanned_modules, f"Module mismatch:\n  On disk: {actual_modules}\n  Scanned: {scanned_modules}"


def test_tools_discovered() -> None:
    """Sanity: we find a reasonable number of tools."""
    assert len(_ALL_TOOLS) >= 50, f"Expected >=50 tools, found {len(_ALL_TOOLS)}. Did a module's register() break?"


def test_args_map_not_empty() -> None:
    """Guard against regression — all 43 tools with arguments must have TypedDicts."""
    assert len(TOOL_ARGS_MAP) >= 43
