"""Documentation contract checks for public surface counts and defaults."""

from __future__ import annotations

import re
from pathlib import Path

from filigree.mcp_server import _all_tools
from filigree.mcp_tools.rename import RENAME_MAP

ROOT = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_public_docs_mcp_tool_count_matches_registry() -> None:
    count = len(_all_tools)
    docs = {
        "README.md": _read("README.md"),
        "docs/README.md": _read("docs/README.md"),
        "docs/getting-started.md": _read("docs/getting-started.md"),
    }

    for path, text in docs.items():
        assert "71 tools" not in text, path
        assert "71 MCP tools" not in text, path
        assert f"{count} tools" in text or f"{count} MCP tools" in text, path


def test_api_reference_documents_default_release_pack() -> None:
    text = _read("docs/api-reference.md")

    assert '["core", "planning", "release"]' in text
    assert 'defaults to `["core", "planning"]`' not in text
    assert '"enabled_packs": ["core", "planning"]' not in text


# --- MCP tool-name namespacing guard (ADR-016 §7) ---------------------------
#
# The MCP tool names were renamed old->new (RENAME_MAP). list_tools() now
# serves the NEW names, so the LIVE agent-facing docs and the skill pack must
# reference the new names too. This guard fails if a stale OLD MCP tool name
# leaks back into those docs.
#
# Scope is deliberately limited to the live, agent-facing surface. It does NOT
# include ADR-016, the rename-plan docs, or other docs/plans history: those
# intentionally discuss the old names as a record of the rename and must keep
# them.
LIVE_AGENT_DOCS = (
    "docs/mcp.md",
    "docs/agent-integration.md",
    "docs/federation/contracts.md",
    "src/filigree/data/instructions.md",
    "src/filigree/skills/filigree-workflow/SKILL.md",
    "src/filigree/skills/filigree-workflow/references/workflow-patterns.md",
    "src/filigree/skills/filigree-workflow/references/team-coordination.md",
    "src/filigree/skills/filigree-workflow/examples/sprint-plan.json",
    "CLAUDE.md",
)

# A handful of OLD tool names must NOT be flagged by the whole-word check, by
# file, because the surviving occurrence is legitimately something other than a
# served MCP tool name:
#   - observe: also a plain English verb ("don't observe things") and the name
#     of a CLI command (`filigree observe`). Exempted ONLY in the two files
#     where the bare token legitimately occurs as English/CLI:
#     docs/federation/contracts.md (the `cli_commands/observations.py` command
#     list) and the skill's SKILL.md ("Don't observe things…"). The bare-token
#     guard stays ACTIVE for `observe` in the other 7 files (mcp.md,
#     instructions.md, CLAUDE.md, …) so a stale bare `observe` that should be
#     `observation_create` there is still caught.
#   - get_workflow_statuses / explain_status: in docs/federation/contracts.md
#     these appear only as the *target* of a documented historical rename arrow
#     ("get_workflow_states -> get_workflow_statuses"). Rewriting the arrow's
#     right-hand side to the current name would fabricate a one-hop rename that
#     never happened, so that one bullet keeps the as-of-that-phase names.
#   - start_work / start_next_work: docs/federation/contracts.md names the core
#     methods `FiligreeDB.start_work` / `start_next_work` (Python methods, not
#     the MCP tools) and the CLI wrapper `start-next-work`. Those are separate
#     surfaces from the MCP tools and keep their names.
_WHOLEWORD_EXCEPTIONS: dict[str, frozenset[str]] = {
    "observe": frozenset(
        {
            "docs/federation/contracts.md",
            "src/filigree/skills/filigree-workflow/SKILL.md",
        }
    ),
    "get_workflow_statuses": frozenset({"docs/federation/contracts.md"}),
    "explain_status": frozenset({"docs/federation/contracts.md"}),
    "start_work": frozenset({"docs/federation/contracts.md"}),
    "start_next_work": frozenset({"docs/federation/contracts.md"}),
}


def test_live_docs_do_not_reference_old_mcp_tool_names() -> None:
    """No live agent-facing doc may name a renamed MCP tool by its OLD name.

    Two complementary checks:

    1. The fully-qualified ``mcp__filigree__<old>`` client form: unambiguous —
       it can only denote the MCP tool, so it is flagged for every old name in
       every live doc with no exceptions.
    2. A whole-word check on the bare ``<old>`` token. Whole-word here means
       not flanked by ``\\w`` or ``-``; this is what makes the check
       false-positive-safe against CLI verbs, which are dash-form
       (``filigree start-next-work``) — ``start_next_work`` never matches
       ``start-next-work``. Field names like ``scan_run_id`` are not
       RENAME_MAP keys, so they are never searched. The exceptions table above
       documents the few bare tokens we deliberately leave.
    """
    docs = {rel: _read(rel) for rel in LIVE_AGENT_DOCS}

    for old in RENAME_MAP:
        qualified = f"mcp__filigree__{old}"
        bare = re.compile(rf"(?<![\w-]){re.escape(old)}(?![\w-])")
        exempt_files = _WHOLEWORD_EXCEPTIONS.get(old, frozenset())
        for rel, text in docs.items():
            assert qualified not in text, f"{rel}: stale `{qualified}`"
            if rel in exempt_files:
                continue
            assert not bare.search(text), f"{rel}: stale old MCP tool name `{old}`"
