"""Guard: no agent-facing *runtime* prose tells a caller to invoke a tool by an
OLD (pre-rename) MCP name.

After the ADR-016 §7 namespacing rename, ``test_no_old_names_in_served_prose``
covers the static ``list_tools()`` surface (tool + parameter descriptions). But
hints and messages built in handler code, plus session-context / summary output
(``hooks.py``, ``summary.py``, response ``message`` fields), are returned in tool
*responses* — they are not enumerable from the served ``Tool`` objects, so the
served-prose guard cannot reach them. They were cut over by hand (63beb7c); this
guard makes that cutover durable for the runtime surface too.

Approach — why a directive pattern instead of a bare name scan:
- Old tool names are still the **canonical handler ids** (``name="get_issue"``,
  dispatch-dict keys), and many also name a Python method (``get_valid_transitions()``)
  or a CLI verb. A bare ``\b<oldname>\b`` scan therefore drowns in legitimate
  self-references. So this guard fires only on an *instructional* construction —
  a directive verb (``use``/``call``/``run``/``see``/``via``/``invoke``)
  immediately followed by an OLD tool name — which is unambiguously "go call this
  tool", and an old name there is a regression.
- A name followed by ``(`` is a method-call reference (e.g. the DB method
  ``get_valid_transitions()``, which was NOT renamed) and is excluded.

Scope / known gaps:
- A bare, non-directive mention of an old name in runtime prose (e.g. a name in
  a list without a directive verb) is not caught here; reword such prose as a
  directive, or backtick the NEW name. The high-value class — "Use/Run X" — is
  covered.
- ``rename.py`` is the old→new source of truth and legitimately contains every
  old name as a dict key, so it is excluded.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import filigree
from filigree.mcp_tools.rename import RENAME_MAP

_SRC_ROOT = Path(filigree.__file__).resolve().parent

# A directive verb immediately before an OLD tool name (optionally backticked),
# where the name is NOT a method call (no trailing ``(``). Longest-first so the
# alternation prefers the longest old name; ``\b`` keeps matches whole-word.
_OLD_NAMES = sorted(RENAME_MAP.keys(), key=len, reverse=True)
_DIRECTIVE_OLD_NAME_RE = re.compile(
    r"\b(?:use|call|run|see|via|invoke)\s+`?(" + "|".join(re.escape(n) for n in _OLD_NAMES) + r")`?\b(?!\s*\()",
    re.IGNORECASE,
)

# A backticked old tool name — the convention for "this is a tool to invoke" in
# agent-facing prose. Used only against the prose-emitter files below, which
# contain no canonical handler registrations or method references, so a
# backticked old name there is unambiguously a stale tool reference.
_BACKTICKED_OLD_NAME_RE = re.compile(r"`(" + "|".join(re.escape(n) for n in _OLD_NAMES) + r")`")

# Modules whose string output is read by agents/users as tool-invocation
# guidance (session-context + project summary). They emit no Tool objects, so
# the served-prose guard never sees them; assert ANY backticked old name here,
# not only directive-prefixed ones (which the broad scan above already covers).
_PROSE_EMITTER_FILES = ("summary.py", "hooks.py")


def _iter_string_constants(tree: ast.AST) -> list[tuple[int, str]]:
    """Yield (lineno, value) for every string literal in a parsed module."""
    return [(node.lineno, node.value) for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def test_no_old_tool_names_in_runtime_directive_prose() -> None:
    offenders: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if path.name == "rename.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for lineno, text in _iter_string_constants(tree):
            for old_name in _DIRECTIVE_OLD_NAME_RE.findall(text):
                rel = path.relative_to(_SRC_ROOT.parent)
                offenders.append(f"{rel}:{lineno}: directive references old tool name {old_name!r} (use {RENAME_MAP[old_name]!r})")

    assert not offenders, "Runtime prose must reference NEW (post-ADR-016) MCP tool names:\n" + "\n".join(offenders)


def test_prose_emitter_files_have_no_backticked_old_tool_names() -> None:
    """Tighter guard for the known agent-facing prose emitters: no backticked old
    tool name at all (catches non-directive references the broad scan misses,
    e.g. ``\\`a\\`, \\`b\\`, or \\`c\\``-style lists)."""
    offenders: list[str] = []
    for name in _PROSE_EMITTER_FILES:
        path = _SRC_ROOT / name
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for lineno, text in _iter_string_constants(tree):
            for old_name in _BACKTICKED_OLD_NAME_RE.findall(text):
                offenders.append(f"{name}:{lineno}: backticked old tool name `{old_name}` (use `{RENAME_MAP[old_name]}`)")

    assert not offenders, "Agent-facing prose emitters must reference NEW MCP tool names:\n" + "\n".join(offenders)
