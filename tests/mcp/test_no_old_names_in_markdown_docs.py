"""Guard: no agent-facing *markdown doc* references a tool by an OLD (pre-rename)
MCP name.

After the ADR-016 §7 namespacing rename, two automated guards keep prose current:
``test_no_old_names_in_served_prose`` (the static ``list_tools()`` surface) and
``test_no_old_names_in_runtime_prose`` (Python string constants — hints, messages,
session-context/summary emitters). Neither reaches the hand-written **markdown**
docs that agents read directly: the MCP reference, the agent-integration guide,
the bundled ``instructions.md``, and the workflow ``SKILL.md``. Those are clean
today, but only by hand; this guard makes the cutover durable for them too.

Approach — backticked names only:
- These docs reference a tool to invoke by backticking it (``\\`work_start\\```),
  the same convention the runtime-prose emitter guard keys on. Scanning for a
  backticked OLD name is unambiguous ("call this tool") and side-steps the one
  English-word collision in ``RENAME_MAP`` — ``observe`` is both a key and a plain
  verb, but ``\\`observe\\``` (backticked) is only ever the tool reference.

Scope / known gaps:
- A bare, non-backticked mention of an old name (e.g. inside a prose sentence or a
  heading) is not caught. Backtick the NEW name — that is the doc convention.
- The file set is curated, NOT a glob over all markdown: CHANGELOG.md and the ADRs
  legitimately record old names as history ("renamed ``get_issue`` -> ``issue_get``")
  and must not be scanned.
- Only the git-tracked *source* ``SKILL.md`` is scanned. The ``.claude/`` and
  ``.agents/`` copies are gitignored, install-time byte-copies of this source
  (``install_skills``), so guarding the source covers what ships.
"""

from __future__ import annotations

import re
from pathlib import Path

import filigree
from filigree.mcp_tools.rename import RENAME_MAP

_SRC_ROOT = Path(filigree.__file__).resolve().parent
_REPO_ROOT = _SRC_ROOT.parent.parent

# Agent-facing markdown docs that name tools to invoke. Curated on purpose (see
# the module docstring): a glob would false-positive on CHANGELOG/ADR history.
_DOC_FILES = (
    _REPO_ROOT / "docs" / "mcp.md",
    _REPO_ROOT / "docs" / "agent-integration.md",
    _SRC_ROOT / "data" / "instructions.md",
    _SRC_ROOT / "skills" / "filigree-workflow" / "SKILL.md",
)

# A backticked OLD tool name. Longest-first alternation; the backticks are the
# "this is a tool" signal, so no word-boundary or directive-verb heuristic needed.
_OLD_NAMES = sorted(RENAME_MAP.keys(), key=len, reverse=True)
_BACKTICKED_OLD_NAME_RE = re.compile(r"`(" + "|".join(re.escape(n) for n in _OLD_NAMES) + r")`")


def test_markdown_doc_files_exist() -> None:
    """Non-vacuity: a future file move must not silently disable this guard."""
    missing = [str(p) for p in _DOC_FILES if not p.is_file()]
    assert not missing, "guarded markdown doc(s) not found (move the path or this guard goes dark):\n" + "\n".join(missing)


def test_no_markdown_doc_names_an_old_tool() -> None:
    """No curated agent-facing markdown doc backticks an OLD MCP tool name."""
    offenders: list[str] = []
    for path in _DOC_FILES:
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for old_name in sorted(set(_BACKTICKED_OLD_NAME_RE.findall(line))):
                rel = path.relative_to(_REPO_ROOT)
                offenders.append(f"{rel}:{lineno}: backticked old tool name `{old_name}` (use `{RENAME_MAP[old_name]}`)")
    assert not offenders, "Markdown docs must reference NEW (post-ADR-016) MCP tool names:\n" + "\n".join(offenders)


def test_guard_can_detect_an_old_name() -> None:
    """Sanity: the regex fires on a known OLD name and not on its NEW successor."""
    sample_old = "get_issue"
    sample_new = RENAME_MAP[sample_old]
    assert _BACKTICKED_OLD_NAME_RE.search(f"call `{sample_old}` to read it")
    assert not _BACKTICKED_OLD_NAME_RE.search(f"call `{sample_new}` to read it")
