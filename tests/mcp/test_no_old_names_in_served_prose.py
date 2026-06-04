"""Guard: no served MCP tool prose references an OLD (pre-rename) tool name.

After the ADR-016 §7 namespacing rename, ``list_tools()`` serves the NEW
``<entity>_<verb>`` names (``RENAME_MAP`` values). But the served ``Tool``
objects are a one-time projection that renames only ``.name`` — every
``description`` (top-level and per-parameter, inside ``inputSchema``) is carried
across verbatim. Any prose that still names another tool by its OLD name
(a ``RENAME_MAP`` *key*) would point an agent at a name that no longer appears
in ``list_tools()``.

This guard walks the served surface and asserts no OLD tool name survives as a
whole-word token in any served description. A future description that references
an old name fails CI here, making the cutover durable.

Scope / known gaps:
- Runtime ``hint`` / error ``message`` strings are returned in tool *responses*,
  not in the static tool surface, so they cannot be enumerated here. They were
  fixed manually at their definition sites; this guard covers the static
  descriptions (top-level + parameter) that ARE enumerable.
- Word-boundary matching means a NEW name that *contains* an old token as a
  substring (e.g. ``issue_get`` vs old ``get_issue``) does not false-positive:
  ``get_issue`` is not a whole-word token inside ``issue_get``.
- ``observe`` is both a ``RENAME_MAP`` key (renamed to ``observation_create``)
  and a plain English verb. The ``\b``-anchored regex cannot tell the two apart,
  so a future description that legitimately uses "observe" as prose would
  false-positive here. There are no such occurrences today; if one is ever
  needed, the cheap fix is to reword (e.g. "record an observation") or backtick
  the intended tool name — not to teach this guard to parse English.
"""

from __future__ import annotations

import re
from typing import Any

from filigree.mcp_server import _served_tools
from filigree.mcp_tools.rename import RENAME_MAP

# Whole-word alternation of every OLD tool name. Longest-first so the regex
# engine prefers the longest match (cosmetic; \b anchors make it unambiguous).
_OLD_NAMES = sorted(RENAME_MAP.keys(), key=len, reverse=True)
_OLD_NAME_RE = re.compile(r"\b(" + "|".join(re.escape(name) for name in _OLD_NAMES) + r")\b")


def _iter_descriptions(tool: Any) -> list[tuple[str, str]]:
    """Yield (location, text) for the tool description and every parameter
    description nested anywhere inside its ``inputSchema``."""
    out: list[tuple[str, str]] = []
    if tool.description:
        out.append((f"{tool.name} (description)", tool.description))

    schema = tool.inputSchema
    if isinstance(schema, dict):

        def walk(node: Any, path: str) -> None:
            if isinstance(node, dict):
                desc = node.get("description")
                if isinstance(desc, str):
                    out.append((f"{tool.name} ({path}.description)", desc))
                for key, value in node.items():
                    if key == "description":
                        continue
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for idx, value in enumerate(node):
                    walk(value, f"{path}[{idx}]")

        walk(schema, "inputSchema")
    return out


def test_no_served_description_names_an_old_tool() -> None:
    """No served description (top-level or parameter) contains an OLD tool name
    as a whole-word token."""
    offenders: list[str] = []
    for tool in _served_tools:
        for location, text in _iter_descriptions(tool):
            hits = sorted(set(_OLD_NAME_RE.findall(text)))
            if hits:
                suggested = {old: RENAME_MAP[old] for old in hits}
                offenders.append(f"{location}: old names {hits} -> use {suggested}")
    assert not offenders, "served prose references old tool names:\n" + "\n".join(offenders)


def test_guard_can_detect_an_old_name() -> None:
    """Sanity: the regex actually fires on a known OLD name and not on its NEW
    successor (substring safety via word boundaries)."""
    sample_old = "get_issue"
    sample_new = RENAME_MAP[sample_old]
    assert _OLD_NAME_RE.search(f"call {sample_old} to read it")
    assert not _OLD_NAME_RE.search(f"call {sample_new} to read it")
