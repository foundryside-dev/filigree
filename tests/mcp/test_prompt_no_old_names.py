"""Guard: the ``filigree-workflow`` MCP prompt names no OLD (pre-rename) tool.

After the ADR-016 §7 namespacing rename, every agent-facing surface serves the
NEW ``<entity>_<verb>`` names (``RENAME_MAP`` values). The MCP **prompt** text
(``mcp_server._WORKFLOW_TEXT_STATIC`` and the dynamic ``_build_workflow_text()``
output) is one such surface: it names tools in prose that an agent reads to
learn the workflow. Any token that is still a ``RENAME_MAP`` *key* would point
the agent at a name that no longer appears in ``list_tools()``.

This guard asserts no OLD tool name survives as a whole-word token in either the
static constant or the rendered dynamic prompt. The technique mirrors
``test_no_old_names_in_served_prose.py``: a ``\b``-anchored alternation over
``RENAME_MAP.keys()``. Word boundaries keep a NEW name that *contains* an old
token as a substring (e.g. ``issue_get`` vs old ``get_issue``) from
false-positiving.

Note: ``observe`` is both a ``RENAME_MAP`` key and a plain English verb, so the
regex cannot tell the two apart. It does not appear in this prompt today, so
there is no false positive to suppress.
"""

from __future__ import annotations

import re

import filigree.mcp_server as mcp_server
from filigree.core import FiligreeDB
from filigree.mcp_tools.rename import RENAME_MAP
from tests._seeds import seed_observations

# Whole-word alternation of every OLD tool name. Longest-first is cosmetic; the
# \b anchors make the match unambiguous.
_OLD_NAMES = sorted(RENAME_MAP.keys(), key=len, reverse=True)
_OLD_NAME_RE = re.compile(r"\b(" + "|".join(re.escape(name) for name in _OLD_NAMES) + r")\b")


def _offenders(text: str) -> list[str]:
    return sorted(set(_OLD_NAME_RE.findall(text)))


def test_static_prompt_names_no_old_tool() -> None:
    """The static prompt constant contains no OLD tool name as a whole word.

    Asserted directly so coverage holds even without a database.
    """
    hits = _offenders(mcp_server._WORKFLOW_TEXT_STATIC)
    suggested = {old: RENAME_MAP[old] for old in hits}
    assert not hits, f"_WORKFLOW_TEXT_STATIC names old tools {hits} -> use {suggested}"


def test_dynamic_prompt_names_no_old_tool(mcp_db: FiligreeDB) -> None:
    """The rendered dynamic prompt (with a seeded DB so the dynamic branch runs,
    including the observation-awareness block) names no OLD tool as a whole word."""
    # Seed an issue and observations so the Registered Types, Enabled Packs,
    # and Observations branches all render.
    mcp_db.create_issue("Seed", type="bug", priority=2)
    seed_observations(mcp_db, count=1)

    text = mcp_server._build_workflow_text()
    # Sanity: we exercised the dynamic branch, not the static fallback.
    assert "## Registered Types" in text
    assert "## Observations" in text

    hits = _offenders(text)
    suggested = {old: RENAME_MAP[old] for old in hits}
    assert not hits, f"_build_workflow_text() names old tools {hits} -> use {suggested}"


def test_guard_can_detect_an_old_name() -> None:
    """Sanity: the regex fires on a known OLD name and not on its NEW successor."""
    sample_old = "get_ready"
    sample_new = RENAME_MAP[sample_old]
    assert _OLD_NAME_RE.search(f"call {sample_old} to find work")
    assert not _OLD_NAME_RE.search(f"call {sample_new} to find work")
