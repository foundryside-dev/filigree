"""Shared stale-claim rules for the agent-facing prose guards (Task 0.9).

The ``test_no_old_names_*`` / ``test_prompt_no_old_names`` guards were built to
keep OLD (pre-ADR-016) tool names out of served prose. The same surfaces also
drifted from *behaviour* (LX-11, LX-14, LX-16): they named archived Weft members
(Legis, Warpline; archived 2026-10-01) as live tools, claimed a ``--agent-id``
flag ``filigree-mcp`` never accepted, pointed at the pre-3.0 ``.filigree/``
store, documented a ``current_assignee`` CONFLICT detail the server never sends,
and promised a CLI exit code 4 for CONFLICT (every error envelope exits 1).
Each guard applies :func:`stale_claims` to the text of its own surface, so this
module is the single list of forbidden claims.

Scope rules, so legitimate text does not trip:

- **Archived-member tool globs** (``mcp__legis__*`` / ``mcp__warpline__*``) are
  always a live-tool claim; nothing exempts them.
- **Archived members** otherwise match case-insensitively as whole words, so
  code identifiers stay out: ``\\b`` excludes ``legis_client`` / ``LEGIS_URL`` /
  ``warpline_worklist_ingest``, and ``(?!\\.\\w)`` excludes the wire schema id
  ``warpline.reverify_worklist.v1``. A quoted name in a *value* position — after
  ``default(s)`` / ``label(s)`` / ``actor`` / ``scan_source`` / ``producer`` or a
  ``=`` / ``:`` (e.g. ``(default 'warpline')``, ``labels ('warpline', ...)``) —
  is data, not a tool claim. A bare backticked name (```legis``` CLI) is NOT a
  value and is flagged. A *sentence* (split on ``.``/``!``/``?``/``;`` and at
  list items / table rows) that itself says the member is *archived* or
  *retired* describes current behaviour honestly and is allowed; the word
  elsewhere in the paragraph does not exempt the mention.
- **Version framing** (``2.0``) is checked only in the packaged skill, whose
  sheets must describe the current release; docs that record history ("since
  2.0") are out of scope.
- History (CHANGELOG, ADRs, PDRs, ``docs/plans/``, ``docs/superpowers/``) is never
  passed to these rules; each guard curates the served surface it covers.
"""

from __future__ import annotations

import re

_ARCHIVED_TOOL_GLOB_RE = re.compile(r"mcp__(?:legis|warpline)__", re.IGNORECASE)
_ARCHIVED_MEMBER_RE = re.compile(r"\b(?:legis|warpline)\b(?!\.\w)", re.IGNORECASE)
# A quoted member name in a value position: after a value-ish key word (within a
# few non-sentence chars, e.g. "default `warpline`", "labels ('warpline', ...")
# or right after "=" / ":".
_QUOTED_MEMBER_VALUE_RE = re.compile(
    r"(?:\b(?:defaults?|labels?|actor|scan_source|producer)\b[^.;\n`'\"]{0,12}|[=:]\s*)([`'\"])(?:legis|warpline)\1",
    re.IGNORECASE,
)
_ARCHIVED_QUALIFIER_RE = re.compile(r"\b(?:archived|retired)\b", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n(?=[ \t]*(?:[-*|]|\d+\.)\s)")

_LITERAL_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("`--agent-id` (filigree-mcp accepts --project only; LX-11)", re.compile(r"--agent-id")),
    (
        "`current_assignee` (CONFLICT details are {issue_id, observed, expected}; LX-14)",
        re.compile(r"\bcurrent_assignee\b"),
    ),
    ("`.filigree/` (the store is .weft/filigree/ since 3.0; LX-14)", re.compile(r"(?<![\w/])\.filigree/")),
    (
        "CLI exit code 4 (every error envelope exits 1; branch on `code`)",
        re.compile(r"\bexits?(?: with)?(?: (?:code|status))? 4\b", re.IGNORECASE),
    ),
)

_SKILL_ONLY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (("'2.0' version framing (LX-14)", re.compile(r"(?<![\w.])2\.0(?!\.?\d)")),)

_ARCHIVED_LABEL = "Legis/Warpline named without saying it is archived (both archived 2026-10-01)"
_ARCHIVED_GLOB_LABEL = "mcp__legis__* / mcp__warpline__* tool glob (archived 2026-10-01; no such tools)"


def _names_archived_member_as_live(paragraph: str) -> bool:
    for sentence in _SENTENCE_SPLIT_RE.split(paragraph):
        scrubbed = _QUOTED_MEMBER_VALUE_RE.sub("<value>", sentence)
        if _ARCHIVED_MEMBER_RE.search(scrubbed) and not _ARCHIVED_QUALIFIER_RE.search(sentence):
            return True
    return False


def _paragraph_hits(paragraph: str, *, skill: bool) -> list[str]:
    hits: list[str] = []
    if _ARCHIVED_TOOL_GLOB_RE.search(paragraph):
        hits.append(_ARCHIVED_GLOB_LABEL)
    if _names_archived_member_as_live(paragraph):
        hits.append(_ARCHIVED_LABEL)
    rules = _LITERAL_RULES + (_SKILL_ONLY_RULES if skill else ())
    hits.extend(label for label, pattern in rules if pattern.search(paragraph))
    return hits


def stale_claims(text: str, *, skill: bool = False) -> list[tuple[int, str]]:
    """Return ``(line, label)`` for every stale claim in *text*.

    *text* is split into blank-line-separated paragraphs (a single tool
    description is one paragraph); ``line`` is the 1-based first line of the
    offending paragraph. ``skill=True`` adds the skill-only rules.
    """
    out: list[tuple[int, str]] = []
    line = 1
    for chunk in re.split(r"(\n[ \t]*\n)", text):
        if chunk.strip() and not re.fullmatch(r"\n[ \t]*\n", chunk):
            lead = len(chunk) - len(chunk.lstrip("\n"))
            out.extend((line + lead, label) for label in _paragraph_hits(chunk, skill=skill))
        line += chunk.count("\n")
    return out
