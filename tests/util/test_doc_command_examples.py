"""Lint `filigree …` command examples in the agent-facing docs against the CLI.

This guards against doc/CLI drift: every `filigree <verb>` invocation that
appears in the agent-facing doc surface must name a real Click command, and
every ``--option`` (or short ``-x``) used on it must be a real option on that
command (or a real root-group option when it appears before the verb).

Scope (the surface an agent onboards from):

* ``src/filigree/data/instructions.md`` — bundled CLAUDE.md instructions
* ``src/filigree/skills/filigree-workflow/SKILL.md`` and its ``references/*.md``

What it checks (deliberately conservative — the high-value drift class):

* The verb after ``filigree`` exists as a registered Click command.
* Each long/short option token used is a valid option on that command, or
  (when it appears before the verb) on the root group.

What it does NOT check, by design:

* Option *arguments* / values, positional argument arity, or mutual exclusion.
* Subcommands of command *groups* (``filigree scanner …`` / ``server …``):
  such lines are recognised as valid (the group exists) but option
  validation is skipped, because options there belong to the subcommand, not
  the group. None of the in-scope docs currently use that form.
* Non-``filigree`` shell lines (``cd``, ``git``, ``GET /api/…``, comments)
  are ignored entirely.
* Placeholder tokens like ``<id>`` / ``<name>`` are skipped (they are not
  options and not verbs).

Keeping it conservative means a malformed example fails loudly rather than a
clever parser silently mis-attributing an option.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import click
import pytest

from filigree.cli import cli

ROOT = Path(__file__).resolve().parent.parent.parent
SKILL_DIR = ROOT / "src" / "filigree" / "skills" / "filigree-workflow"

# The agent-facing doc surface this linter governs.
DOC_FILES: list[Path] = [
    ROOT / "src" / "filigree" / "data" / "instructions.md",
    SKILL_DIR / "SKILL.md",
    *sorted((SKILL_DIR / "references").glob("*.md")),
]

_FENCE_RE = re.compile(r"```(?:bash|sh|shell|console)\n(.*?)```", re.DOTALL)


def _option_tokens(command: click.Command) -> set[str]:
    """All long/short option spellings (incl. flag negations) for a command."""
    opts: set[str] = set()
    for param in command.params:
        if isinstance(param, click.Option):
            opts.update(param.opts)
            opts.update(param.secondary_opts)
    return opts


ROOT_OPTS = _option_tokens(cli)


def _has_unbalanced_quotes(s: str) -> bool:
    """True when *s* cannot be tokenised because a quote is left open."""
    try:
        shlex.split(s)
    except ValueError:
        return True
    return False


def _command_lines_in_text(text: str, path: Path) -> list[tuple[Path, int, str]]:
    """Extract (path, line_no, logical_line) for each `filigree …` invocation in
    one doc's *text*.

    Only fenced bash/shell blocks are scanned. Two kinds of line continuation are
    joined into one logical line: trailing-``\\`` continuations, and open-quote
    continuations — a quoted argument that spans newlines with no trailing
    backslash (valid shell, e.g. a multi-line ``-d "…"`` body). Without the
    latter the extractor would capture only the first fragment (quote left open),
    which then fails ``shlex`` and is skipped, so the verb/options in multi-line
    examples escape drift-checking. ``line_no`` points at the line where the
    invocation begins (1-based, within the file).
    """
    out: list[tuple[Path, int, str]] = []
    for fence in _FENCE_RE.finditer(text):
        block = fence.group(1)
        # 1-based line number in the file of the first block line.
        block_start = text.count("\n", 0, fence.start(1)) + 1

        raw_lines = block.split("\n")
        i = 0
        while i < len(raw_lines):
            start_idx = i
            # Join backslash continuations.
            parts = [raw_lines[i]]
            while raw_lines[i].rstrip().endswith("\\") and i + 1 < len(raw_lines):
                parts[-1] = parts[-1].rstrip()[:-1]  # drop trailing backslash
                i += 1
                parts.append(raw_lines[i])
            logical = " ".join(p.strip() for p in parts).strip()
            # Join open-quote continuations: keep consuming raw lines (newline
            # preserved inside the quote) until the quotes balance or the block
            # ends. A still-unbalanced line at block end falls through and is
            # captured anyway, so the validity test sees it and skips it.
            while i + 1 < len(raw_lines) and _has_unbalanced_quotes(logical):
                i += 1
                logical = f"{logical}\n{raw_lines[i].rstrip()}"
            i += 1

            # Strip inline comments (everything after an unquoted '#').
            logical = _strip_comment(logical)
            if not logical:
                continue
            # Only validate filigree invocations.
            if re.match(r"^filigree(\s|$)", logical):
                out.append((path, block_start + start_idx, logical))
    return out


def _iter_command_lines() -> list[tuple[Path, int, str]]:
    """Yield (file, line_no, logical_line) for each `filigree …` invocation across
    the whole agent-facing doc surface (see ``_command_lines_in_text``)."""
    out: list[tuple[Path, int, str]] = []
    for path in DOC_FILES:
        if not path.exists():
            continue
        out.extend(_command_lines_in_text(path.read_text(encoding="utf-8"), path))
    return out


def _strip_comment(line: str) -> str:
    """Drop a trailing ``# …`` comment that is not inside quotes."""
    in_single = in_double = False
    for idx, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double and (idx == 0 or line[idx - 1].isspace()):
            # A comment only when preceded by whitespace / at line start.
            return line[:idx].rstrip()
    return line.rstrip()


def _looks_like_placeholder(tok: str) -> bool:
    """`<id>`, `<name>`, `…`, or bracket placeholders — not a real verb."""
    return tok.startswith(("<", "[")) or tok in {"…", "...", "$@"} or "<" in tok


COMMAND_LINES = _iter_command_lines()


def test_doc_surface_has_command_examples() -> None:
    """Guard against the extractor silently matching nothing (e.g. fence regex
    drift) — if this fires, the linter below is vacuously passing."""
    assert len(COMMAND_LINES) >= 20, (
        f"Only found {len(COMMAND_LINES)} filigree examples across the doc surface; the extractor likely broke."
    )


def test_multiline_quoted_command_is_assembled_not_skipped() -> None:
    """A ``filigree`` example whose quoted argument spans newlines with NO trailing
    backslash (valid shell — a literal newline inside ``"…"``) must be assembled
    into one balanced logical line, so its verb/options are drift-checked rather
    than captured as an unbalanced-quote fragment and skipped."""
    text = '```bash\nfiligree add-comment <id> "Root cause: off-by-one.\nFixed in abc123. Tested boundaries."\n```\n'
    lines = _command_lines_in_text(text, Path("synthetic.md"))
    assert len(lines) == 1
    logical = lines[0][2]
    # Balanced now — shlex.split would raise on the old first-line-only fragment.
    assert shlex.split(logical)[:2] == ["filigree", "add-comment"]


def test_backslash_then_multiline_quote_is_assembled() -> None:
    """Mixed continuation: ``\\``-continued option lines followed by a quoted
    value that itself spans newlines (the workflow-patterns bug-report shape).
    The whole command must assemble and parse."""
    text = '```bash\nfiligree create "Short desc" \\\n  --type=bug \\\n  -d "Steps: ...\nExpected: ...\nActual: ..."\n```\n'
    lines = _command_lines_in_text(text, Path("synthetic.md"))
    assert len(lines) == 1
    tokens = shlex.split(lines[0][2])
    assert tokens[:2] == ["filigree", "create"]
    assert "--type=bug" in tokens


def test_genuinely_unbalanced_quote_still_extracts_for_skip() -> None:
    """Safety valve preserved: an example whose quote never closes within the
    block is still captured (so the validity test sees it and skips), not dropped."""
    text = '```bash\nfiligree add-comment <id> "never closed\n```\n'
    lines = _command_lines_in_text(text, Path("synthetic.md"))
    assert len(lines) == 1
    with pytest.raises(ValueError, match="No closing quotation"):
        shlex.split(lines[0][2])


@pytest.mark.parametrize(
    ("path", "line_no", "line"),
    COMMAND_LINES,
    ids=[f"{p.name}:{n}" for p, n, _ in COMMAND_LINES],
)
def test_doc_command_example_is_valid(path: Path, line_no: int, line: str) -> None:
    where = f"{path.relative_to(ROOT)}:{line_no}: `{line}`"

    try:
        tokens = shlex.split(line)
    except ValueError:
        # Unbalanced quotes etc. — skip rather than false-fail; these are
        # almost always prose-y examples, and a parse failure is not CLI drift.
        pytest.skip(f"unparseable shell line: {where}")

    assert tokens, where
    assert tokens[0] == "filigree", where
    rest = tokens[1:]

    # Walk pre-verb tokens: they must be root-group options (with their values).
    idx = 0
    while idx < len(rest):
        tok = rest[idx]
        if tok.startswith("-"):
            opt = tok.split("=", 1)[0]
            assert opt in ROOT_OPTS, f"unknown root option `{opt}` in {where}"
            # If the root option takes a value and uses space form, skip it.
            if "=" not in tok and not _is_flag(cli, opt):
                idx += 1
            idx += 1
        else:
            break  # this token is the verb

    if idx >= len(rest):
        # `filigree --version` / `filigree --help` style — no verb. Valid.
        return

    verb = rest[idx]
    if _looks_like_placeholder(verb):
        pytest.skip(f"placeholder verb in {where}")

    command = cli.commands.get(verb)
    assert command is not None, f"unknown filigree verb `{verb}` in {where}"

    # A command group (scanner/server): the group exists; its subcommand owns
    # the options, so we do not validate further. (No in-scope doc uses this.)
    if isinstance(command, click.Group):
        return

    valid_opts = _option_tokens(command)
    for tok in rest[idx + 1 :]:
        if not tok.startswith("-"):
            continue  # positional arg / value — not validated
        if _looks_like_placeholder(tok):
            continue
        opt = tok.split("=", 1)[0]
        assert opt in valid_opts, f"unknown option `{opt}` for `filigree {verb}` in {where} (valid: {sorted(valid_opts)})"


def _is_flag(command: click.Command, opt: str) -> bool:
    """Whether ``opt`` on ``command`` is a boolean flag (consumes no value)."""
    for param in command.params:
        if isinstance(param, click.Option) and opt in (param.opts + param.secondary_opts):
            return bool(param.is_flag)
    return False
