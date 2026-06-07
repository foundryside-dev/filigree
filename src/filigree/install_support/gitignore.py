"""Shared gitignore-aware parser for the project-root ``.filigree/`` rule.

Used both by ``ensure_gitignore`` (install side, which decides whether to
append a rule) and by ``run_doctor`` (which reports whether the rule is
already active). Keeping a single implementation prevents the two paths
from drifting on edge cases like comments, negations, and substrings —
the kind of drift that previously let the doctor pass projects whose
``.filigree/`` was not actually ignored (filigree-bc5d2af1ef).
"""

from __future__ import annotations

# Normalised forms that effectively ignore the project-root ``.filigree/``
# directory under gitignore semantics. ``.filigree[/]`` matches at any depth
# (including the root); the ``/``-anchored variants are explicitly root-scoped.
FILIGREE_IGNORE_RULES: frozenset[str] = frozenset({".filigree", ".filigree/", "/.filigree", "/.filigree/"})

# The federation store dir (``.weft/filigree/`` lives under it). ``.weft/`` is
# shared by all members, so the root-level rule ignores the whole shared dir.
WEFT_IGNORE_RULES: frozenset[str] = frozenset({".weft", ".weft/", "/.weft", "/.weft/"})


def has_active_ignore(content: str, rules: frozenset[str]) -> bool:
    """Return True if *content* has an active gitignore rule in *rules*.

    Honours gitignore syntax: blank lines and ``#`` comments are skipped,
    trailing whitespace is stripped. ``!``-prefixed negations are processed
    in declaration order — a later ``!<rule>`` un-ignores an earlier rule,
    matching ``git``'s actual semantics. Substring matches do not count.
    """
    state: bool | None = None
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        negated = stripped.startswith("!")
        candidate = stripped[1:] if negated else stripped
        if candidate in rules:
            state = not negated
    return state is True


def has_active_filigree_ignore(content: str) -> bool:
    """Return True if *content* actively ignores project-root ``.filigree/``."""
    return has_active_ignore(content, FILIGREE_IGNORE_RULES)


def has_active_weft_ignore(content: str) -> bool:
    """Return True if *content* actively ignores project-root ``.weft/``."""
    return has_active_ignore(content, WEFT_IGNORE_RULES)
