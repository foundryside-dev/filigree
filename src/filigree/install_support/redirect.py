"""CLAUDE.md → AGENTS.md redirect detection (C-20 / weft-6a1fdb0192).

Some projects keep a single agent-context file — AGENTS.md — and reduce
CLAUDE.md to a pointer at it: a short human note plus a bare ``@AGENTS.md``
import line. Writing a managed instruction block into *both* files there
duplicates the always-loaded payload, which is exactly what C-20 budgets
against.

This module answers one question — "is this CLAUDE.md a redirect to
AGENTS.md?" — for the installer, the SessionStart freshness path, and doctor.
It never writes.

Lives in ``install_support`` rather than ``install`` so ``doctor`` can import
it without a cycle (``install`` imports ``doctor``).
"""

from __future__ import annotations

import re
from pathlib import Path

# Recognises ANY tool's instruction-block fence (open or close) by its vendor
# namespace. Used both to bound filigree's own rewrite at a *foreign* fence
# (never deleting a co-resident sibling block — C-4, filigree-bcbd4d66fd) and,
# here, to exclude every managed block from redirect detection. The namespace
# match is case-insensitive: an uppercase-namespaced sibling must still
# register as a boundary.
_INSTR_FENCE_RE = re.compile(r"<!--\s*/?([A-Za-z0-9_-]+):instructions")

# A line that is *solely* an @-import of AGENTS.md. "Solely" is the whole
# signal: `see @AGENTS.md for details` is prose about a file, not a redirect.
_AGENTS_IMPORT_RE = re.compile(r"^\s*@(?:\./)?AGENTS\.md\s*$", re.IGNORECASE)


def strip_managed_blocks(content: str) -> str:
    """Return *content* with every tool's managed instruction block removed.

    An ``@AGENTS.md`` line *inside* a managed block — filigree's own or a
    sibling's — is that tool's payload, not this project's redirect
    declaration, so it must not trigger detection.

    An unclosed block is treated as running to EOF. That is deliberately the
    conservative reading: the rest of the file is ignored, no redirect is
    detected, and the caller keeps its existing dual-write behaviour rather
    than migrating a block on the strength of a malformed file.
    """
    parts: list[str] = []
    open_ns: str | None = None
    last = 0
    for m in _INSTR_FENCE_RE.finditer(content):
        is_close = "/" in m.group(0)
        ns = m.group(1).lower()
        if open_ns is None:
            if not is_close:
                parts.append(content[last : m.start()])
                open_ns = ns
            # A stray close fence with no open is not a block boundary; the
            # surrounding text stays in scope.
        elif is_close and ns == open_ns:
            open_ns = None
            last = m.end()
        elif not is_close:
            # Nested/unclosed open: the previous block ends here and a new one
            # starts, so the region stays excluded either way.
            open_ns = ns
    if open_ns is None:
        parts.append(content[last:])
    return "".join(parts)


def is_agents_md_redirect(claude_md: Path) -> bool:
    """True when *claude_md* is a pointer at AGENTS.md rather than content.

    The test is a line, outside every managed block, that is solely an
    @-import of AGENTS.md (``@AGENTS.md`` or ``@./AGENTS.md``,
    case-insensitive).

    Known limitation, stated rather than silently accepted: an ``@AGENTS.md``
    line quoted inside a ``` fenced markdown block (rather than inside a
    *managed* block) still reads as a redirect. C-20 specifies managed-block
    exclusion only, and the sibling members implement the same boundary —
    uniform beats locally-clever.

    Every doubtful input reads as NO redirect, keeping dual-write: absent,
    unreadable, non-UTF-8 or symlinked CLAUDE.md all return False. A redundant
    block is recoverable; silently abandoning the block a project actually
    reads is not.
    """
    if claude_md.is_symlink():
        return False
    try:
        content = claude_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return any(_AGENTS_IMPORT_RE.match(line) for line in strip_managed_blocks(content).splitlines())
