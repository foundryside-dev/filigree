"""Tests for MCP tool tiering (tag + curated catalogue, no renames).

Three guarantees:

1. Completeness — every tool in ``_all_tools`` has an explicit tier in the
   central ``TIER_MAP`` (guards against a new tool silently missing a tier).
2. The tier marker actually appears in the served ``list_tools()`` payload.
3. The curated catalogue (get_workflow_guide.tool_catalog) lists the core
   tools and is consistent with the central mapping.
"""

from __future__ import annotations

from filigree.core import FiligreeDB
from filigree.mcp_server import _all_tools, _tool_argument_names, call_tool
from filigree.mcp_tools.tiers import _COMMON, _CORE, _NICHE, TIER_MAP, tier_for
from tests.mcp._helpers import _parse

_VALID_TIERS = {"core", "common", "niche"}


class TestTierCompleteness:
    def test_every_tool_has_explicit_tier(self) -> None:
        # Strict membership (not tier_for's defensive fallback) so CI fails
        # loudly when a new tool is added without a deliberate tier.
        tool_names = {tool.name for tool in _all_tools}
        untiered = tool_names - set(TIER_MAP)
        assert untiered == set(), f"tools missing an explicit tier: {sorted(untiered)}"

    def test_tier_map_has_no_stale_entries(self) -> None:
        tool_names = {tool.name for tool in _all_tools}
        stale = set(TIER_MAP) - tool_names
        assert stale == set(), f"TIER_MAP references unknown tools: {sorted(stale)}"

    def test_all_tier_values_valid(self) -> None:
        assert set(TIER_MAP.values()) <= _VALID_TIERS

    def test_tier_sets_are_pairwise_disjoint(self) -> None:
        # TIER_MAP is built by three sequential loops over _CORE/_COMMON/_NICHE;
        # a tool listed in two sets would be silently overwritten by the last
        # loop instead of raising. The completeness test only pins that every
        # tool *has* a tier, not that each tool has exactly one. Guard the
        # construction's disjointness explicitly.
        assert not (_CORE & _COMMON), f"_CORE ∩ _COMMON: {sorted(_CORE & _COMMON)}"
        assert not (_CORE & _NICHE), f"_CORE ∩ _NICHE: {sorted(_CORE & _NICHE)}"
        assert not (_COMMON & _NICHE), f"_COMMON ∩ _NICHE: {sorted(_COMMON & _NICHE)}"

    def test_core_set_is_small(self) -> None:
        core = [name for name, tier in TIER_MAP.items() if tier == "core"]
        assert 8 <= len(core) <= 15, f"core set drifted: {len(core)} tools"


class TestTierMarkerInDescriptions:
    # list_tools() (mcp_server.py) returns _all_tools verbatim in both normal
    # and degraded mode, so asserting on _all_tools descriptions is faithful to
    # the served payload and sidesteps awaiting the decorated async handler.
    def test_every_description_carries_its_tier_marker(self) -> None:
        for tool in _all_tools:
            tier = tier_for(tool.name)
            marker = f"[tier: {tier}]"
            assert tool.description is not None
            assert tool.description.endswith(f" {marker}"), f"{tool.name} description missing trailing {marker!r}"

    def test_marker_applied_exactly_once(self) -> None:
        for tool in _all_tools:
            assert tool.description is not None
            assert tool.description.count("[tier:") == 1, f"{tool.name} has a duplicated tier marker"

    def test_arg_validation_unaffected_by_marker(self) -> None:
        # Description post-processing must not perturb inputSchema-derived
        # argument names.
        assert "issue_id" in _tool_argument_names["get_issue"]
        assert "title" in _tool_argument_names["create_issue"]


class TestToolAnnotations:
    def _by_name(self, name: str) -> object:
        return next(t for t in _all_tools if t.name == name)

    def test_pure_getter_is_read_only(self) -> None:
        tool = self._by_name("get_issue")
        assert tool.annotations is not None  # type: ignore[attr-defined]
        assert tool.annotations.readOnlyHint is True  # type: ignore[attr-defined]

    def test_delete_issue_is_destructive(self) -> None:
        tool = self._by_name("delete_issue")
        assert tool.annotations is not None  # type: ignore[attr-defined]
        assert tool.annotations.destructiveHint is True  # type: ignore[attr-defined]

    def test_mutating_tool_not_marked_read_only(self) -> None:
        tool = self._by_name("update_issue")
        annotations = tool.annotations  # type: ignore[attr-defined]
        assert annotations is None or annotations.readOnlyHint is not True


class TestCuratedCatalogue:
    async def test_catalog_present_and_consistent(self, mcp_db: FiligreeDB) -> None:
        # The catalogue emits the served (namespaced) names, matching list_tools().
        # TIER_MAP / tier_for / _all_tools are keyed off the OLD/canonical names,
        # so translate across RENAME_MAP / NEW_TO_OLD when comparing.
        from filigree.mcp_tools.rename import NEW_TO_OLD, RENAME_MAP

        data = _parse(await call_tool("workflow_guide_get", {"pack": "core"}))
        catalog = data["tool_catalog"]

        # Core list matches the central mapping exactly (served names).
        expected_core = sorted(RENAME_MAP[name] for name, tier in TIER_MAP.items() if tier == "core")
        assert catalog["core"] == expected_core

        # Counts add up to the full surface.
        counts = catalog["tier_counts"]
        assert counts["core"] + counts["common"] + counts["niche"] == len(_all_tools)

        # Every tool appears exactly once across the by-subsystem grouping, and
        # under the tier the central mapping assigns it.
        seen: dict[str, str] = {}
        for _subsystem, by_tier in catalog["by_subsystem"].items():
            for tier, names in by_tier.items():
                for name in names:
                    assert name not in seen, f"{name} listed twice in catalog"
                    seen[name] = tier
                    canonical = NEW_TO_OLD[name]
                    assert tier_for(canonical) == tier, f"{name} catalog tier {tier} != mapping {tier_for(canonical)}"
        assert set(seen) == {RENAME_MAP[t.name] for t in _all_tools}

    async def test_catalog_lists_known_core_tools(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("workflow_guide_get", {"pack": "core"}))
        core = set(data["tool_catalog"]["core"])
        # Served (namespaced) names — old names must be absent.
        for expected in ("work_ready", "work_start", "issue_update", "issue_close", "issue_create"):
            assert expected in core
        for old in ("get_ready", "start_work", "update_issue", "close_issue", "create_issue"):
            assert old not in core
