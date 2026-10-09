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
from tests.mcp._stale_prose import stale_claims

_SRC_ROOT = Path(filigree.__file__).resolve().parent
_REPO_ROOT = _SRC_ROOT.parent.parent

# Agent-facing markdown docs that name tools to invoke. Curated on purpose (see
# the module docstring): a glob would false-positive on CHANGELOG/ADR history.
_DOC_FILES = (
    _REPO_ROOT / "docs" / "mcp.md",
    _REPO_ROOT / "docs" / "agent-integration.md",
    _SRC_ROOT / "data" / "instructions.md",
    _SRC_ROOT / "skills" / "filigree-workflow" / "SKILL.md",
    # C-20 (weft-6a1fdb0192) budgeted instructions.md and SKILL.md and moved
    # the tool-naming prose into the skill's reference sheets. The guard
    # follows the content, or it goes dark on everything that moved.
    _SRC_ROOT / "skills" / "filigree-workflow" / "references" / "commands.md",
    _SRC_ROOT / "skills" / "filigree-workflow" / "references" / "observations.md",
    _SRC_ROOT / "skills" / "filigree-workflow" / "references" / "error-codes.md",
    _SRC_ROOT / "skills" / "filigree-workflow" / "references" / "workflow-patterns.md",
    _SRC_ROOT / "skills" / "filigree-workflow" / "references" / "team-coordination.md",
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


# ---------------------------------------------------------------------------
# Stale behavioural claims (Task 0.9; LX-11, LX-14, LX-16)
# ---------------------------------------------------------------------------
#
# Same curated file set: the served/agent-facing markdown. ``data/instructions.md``
# is the template of the managed CLAUDE.md / AGENTS.md block, so guarding it
# guards every regenerated block. The packaged skill additionally gets the
# skill-only rules ("2.0" version framing). History (CHANGELOG, ADRs, PDRs,
# docs/plans/, docs/superpowers/) is deliberately not in this set.

_SKILL_DIR = _SRC_ROOT / "skills" / "filigree-workflow"


def test_no_markdown_doc_makes_a_stale_claim() -> None:
    """No curated agent-facing doc names Legis/Warpline as live, claims
    ``--agent-id``, points at ``.filigree/``, documents ``current_assignee`` or a
    CLI exit 4 — and no skill sheet frames itself as "filigree 2.0"."""
    offenders: list[str] = []
    for path in _DOC_FILES:
        skill = _SKILL_DIR in path.parents
        for lineno, label in stale_claims(path.read_text(encoding="utf-8"), skill=skill):
            offenders.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}: {label}")
    assert not offenders, "Agent-facing markdown makes stale claims:\n" + "\n".join(offenders)


def test_stale_claim_rules_fire_and_spare_identifiers() -> None:
    """Sanity: each rule fires on a stale sentence, and code identifiers,
    quoted values and "archived" history do not."""
    for stale in (
        "Prefer the `mcp__legis__*` tools.",
        "Run warpline to see the blast radius.",
        "Pass the MCP launch-bound `--agent-id`.",
        "Filigree data lives in `.filigree/`.",
        'details: {current_assignee: "agent-1"}',
        "A CONFLICT exits with code 4.",
        "CONFLICT → CLI exit 4, retryable",
        "Exit status 4 means conflict.",
        # Review round 1: globs are unconditional, the archived exemption is
        # same-sentence only, and a bare backticked name is a tool claim.
        "Prefer the `mcp__legis__*` tools for overrides. Closed issues are archived after 30 days.",
        'Use Warpline to compute blast radius; "state" was retired as a word.',
        "Fall back to the `legis` CLI.",
        "Run `warpline` before claiming done.",
    ):
        assert stale_claims(stale), stale
    assert stale_claims("Protocols for filigree 2.0.", skill=True)
    for clean in (
        "`legis_client.py` and `LEGIS_URL` are code identifiers.",
        "The `warpline_worklist_ingest` tool reads `warpline.reverify_worklist.v1`.",
        "The actor defaults to 'warpline'.",
        "Identity recorded as attached_by (default 'warpline').",
        "Filed items carry the producer labels (`warpline`, `federation`).",
        "Legis is retired and is never consulted.",
        "The Warpline producer is archived.",
        "The store lives in `.weft/filigree/`.",
        "Every error envelope exits 1.",
        "Standardised since 2.0.",
    ):
        assert not stale_claims(clean), clean
