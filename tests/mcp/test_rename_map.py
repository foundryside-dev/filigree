"""Invariant guards for the MCP tool-name rename map (ADR-016 §7).

The map in ``filigree.mcp_tools.rename`` is the frozen source of truth for the
Phase-1 namespacing rename (docs/plans/2026-06-02-mcp-tool-namespacing-rename-
plan.md). These tests pin the three hard invariants against the *live* handler
set so the map cannot drift away from reality:

1. **Total coverage** — every registered tool has exactly one rename row, and the
   map references no tool that does not exist. (Mirrors the tier completeness
   test: a new tool added without a rename row fails CI loudly.)
2. **Injective** — no two current names collapse onto one new name (``NEW_TO_OLD``
   would silently drop a row otherwise).
3. **No-shadow** — no new name equals any *current* tool name, so old and new
   namespaces never alias the wrong handler during the dual-resolution window.

Plus a shape guard: the new names follow the ratified ``<entity>_<verb>``
convention with no ``filigree_`` prefix (D5).
"""

from __future__ import annotations

import re

from filigree.mcp_server import _all_handlers
from filigree.mcp_tools.rename import NEW_TO_OLD, RENAME_MAP

# lowercase, underscore-separated, at least two segments, never the dropped prefix.
_NAME_SHAPE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")


class TestCoverage:
    def test_every_live_tool_has_a_rename_row(self) -> None:
        missing = set(_all_handlers) - set(RENAME_MAP)
        assert missing == set(), f"tools missing a rename row: {sorted(missing)}"

    def test_no_stale_rows(self) -> None:
        stale = set(RENAME_MAP) - set(_all_handlers)
        assert stale == set(), f"RENAME_MAP references unknown tools: {sorted(stale)}"


class TestInjective:
    def test_no_two_old_names_collapse(self) -> None:
        values = list(RENAME_MAP.values())
        assert len(values) == len(set(values)), "RENAME_MAP is not injective (duplicate target names)"

    def test_inverse_is_a_true_bijection(self) -> None:
        # If any value collided, NEW_TO_OLD would have fewer entries than RENAME_MAP.
        assert len(NEW_TO_OLD) == len(RENAME_MAP)
        assert set(NEW_TO_OLD.values()) == set(RENAME_MAP)


class TestNoShadow:
    def test_no_new_name_equals_a_current_name(self) -> None:
        shadow = set(RENAME_MAP.values()) & set(_all_handlers)
        assert shadow == set(), f"new names shadow existing tool names: {sorted(shadow)}"


class TestShape:
    def test_new_names_follow_convention(self) -> None:
        bad = [n for n in RENAME_MAP.values() if not _NAME_SHAPE.match(n)]
        assert bad == [], f"new names violate <entity>_<verb> shape: {bad}"

    def test_no_filigree_prefix(self) -> None:
        # D5: the client supplies `mcp__filigree__`; the tool name must not repeat it.
        prefixed = [n for n in RENAME_MAP.values() if n.startswith("filigree_")]
        assert prefixed == [], f"new names must not carry the filigree_ prefix: {prefixed}"
