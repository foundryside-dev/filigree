"""CLI for the filigree issue tracker.

Convention-based: discovers .filigree/ by walking up from cwd.
Commands are defined in cli_commands/ subpackage modules.
"""

from __future__ import annotations

import copy
import json as json_mod
import re
import sys
import time
from collections.abc import Sequence
from typing import Any

import click
from click.core import ParameterSource

from filigree import __version__
from filigree.cli_commands import admin, files, issues, meta, observations, planning, scanners, sei, server, workflow
from filigree.cli_commands import annotations as annotations_cmds
from filigree.cli_common import _detect_json_via_parse, _resolve_command_path, _wants_json
from filigree.logging import CallOutcome, classify_body
from filigree.types.api import ErrorCode
from filigree.validation import sanitize_actor

# Cap on captured stdout used to read the JSON envelope of a finished command.
# Past it the output is treated as unclassifiable body-wise (exit code only).
_CAPTURE_LIMIT = 256 * 1024
_LAST_CODE_RE = re.compile(r'"code"\s*:\s*"([A-Z][A-Z_]*)"')
# Click exits 2 for usage errors (bad/missing option or argument).
_CLICK_USAGE_EXIT = 2


class _StdoutTee:
    """Pass-through ``sys.stdout`` proxy that remembers (a bounded copy of) what was written.

    Lets the root group read the JSON error envelope a command already emits
    without every command having to report its outcome. Everything not
    overridden (``buffer``, ``isatty``, ``fileno`` ...) is delegated untouched.
    """

    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped
        self._chunks: list[str] = []
        self._size = 0
        self.truncated = False

    def write(self, text: str | bytes) -> int:
        if not self.truncated:
            # click.echo can hand a text wrapper bytes (its binary fast path).
            piece = text.decode("utf-8", "replace") if isinstance(text, bytes) else text
            self._size += len(piece)
            if self._size > _CAPTURE_LIMIT:
                self.truncated = True
                self._chunks.clear()
            else:
                self._chunks.append(piece)
        return int(self._wrapped.write(text))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    @property
    def captured(self) -> str:
        return "".join(self._chunks)


def _classify_cli_call(status: int, tee: _StdoutTee) -> tuple[CallOutcome, str | None]:
    """Derive ``(outcome, code)`` from the exit status and the emitted JSON envelope."""
    outcome: CallOutcome = "ok"
    code: str | None = None
    text = tee.captured.strip()
    body: Any = None
    if text and not tee.truncated:
        try:
            body = json_mod.loads(text)
        except ValueError:
            body = None
    if body is not None:
        outcome, code = classify_body(body)
    elif status != 0 and text:
        # Not a single JSON document (e.g. progress lines before the envelope):
        # take the last "code" the command printed.
        codes = _LAST_CODE_RE.findall(text)
        code = codes[-1] if codes else None
    if status != 0 and outcome in ("ok", "no_op"):
        outcome = "validation" if status == _CLICK_USAGE_EXIT else "error"
        if outcome == "validation" and code is None:
            code = ErrorCode.VALIDATION
    return outcome, code


def _log_cli_call(group: click.Group, raw_args: list[str], status: int, tee: _StdoutTee, t0: float) -> None:
    """Emit the shared ``event="call"`` record for a finished CLI invocation. Never raises."""
    try:
        name = _resolve_command_path(group, raw_args)
        if not name:
            return  # bare --help / --version / unresolved: not a command call
        import logging

        from filigree.core import find_filigree_anchor
        from filigree.logging import log_outcome, population_for, setup_logging

        logger = logging.getLogger("filigree")
        population = None
        try:
            store_dir = find_filigree_anchor().store_dir
            logger = setup_logging(store_dir)
            population = population_for(store_dir)
        except Exception:
            # No project (or unreadable one): still emit the record, just
            # without a file sink or population tag.
            logging.getLogger(__name__).debug("cli call log: no project store", exc_info=True)
        outcome, code = _classify_cli_call(status, tee)
        log_outcome(
            logger,
            surface="cli",
            name=name,
            outcome=outcome,
            code=code,
            duration_ms=round((time.monotonic() - t0) * 1000, 1),
            population=population,
        )
    except Exception:
        import logging

        logging.getLogger(__name__).debug("cli call logging failed", exc_info=True)


class _FiligreeGroup(click.Group):
    """Click Group that stashes the raw invocation args for downstream use.

    Stage 2B task 2b.3b: the group-level ``--actor`` callback needs to
    detect whether the caller also passed ``--json`` on the subcommand
    so a validation failure can surface as the 2.0 flat envelope rather
    than Click's stderr usage error. By group-callback time,
    ``ctx.args``/``ctx.protected_args`` are empty and ``sys.argv`` is
    untouched by ``CliRunner``; the only reliable way to see the raw
    invocation is to capture it during ``parse_args`` (which runs
    before the callback) and stash it in ``ctx.meta``.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        ctx.meta["filigree_raw_args"] = list(args)
        return super().parse_args(ctx, args)

    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        **extra: Any,
    ) -> Any:
        """Run the CLI and log one ``event="call"`` record for the invocation."""
        raw_args = list(sys.argv[1:] if args is None else args)
        t0 = time.monotonic()
        real_stdout = sys.stdout
        tee = _StdoutTee(real_stdout)
        sys.stdout = tee
        status = 0
        try:
            result = self._main_impl(args=args, prog_name=prog_name, complete_var=complete_var, standalone_mode=standalone_mode, **extra)
            status = result if isinstance(result, int) else 0
            return result
        except SystemExit as exc:
            status = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            raise
        except click.ClickException as exc:
            status = exc.exit_code
            raise
        except BaseException:
            status = 1
            raise
        finally:
            sys.stdout = real_stdout
            _log_cli_call(self, raw_args, status, tee, t0)

    def _main_impl(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        **extra: Any,
    ) -> Any:
        raw_args = list(sys.argv[1:] if args is None else args)
        try:
            result = super().main(
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=False,
                **extra,
            )
            if standalone_mode and isinstance(result, int) and result != 0:
                sys.exit(result)
            return result
        except click.ClickException as exc:
            if _detect_json_via_parse(self, raw_args):
                click.echo(json_mod.dumps({"error": exc.format_message(), "code": ErrorCode.VALIDATION}))
            elif standalone_mode:
                exc.show()
            if standalone_mode:
                sys.exit(exc.exit_code)
            raise
        except click.Abort:
            if standalone_mode:
                click.echo("Aborted!", file=sys.stderr)
                sys.exit(1)
            raise


@click.group(cls=_FiligreeGroup)
@click.version_option(version=__version__, prog_name="filigree")
@click.option("--actor", default="cli", help="Actor identity for audit trail (default: cli)")
@click.pass_context
def cli(ctx: click.Context, actor: str) -> None:
    """Filigree — agent-native issue tracker."""
    ctx.ensure_object(dict)
    cleaned, err = sanitize_actor(actor)
    if err:
        # Stage 2B task 2b.3b: when the caller is running a subcommand
        # with ``--json``, emit the 2.0 envelope instead of Click's
        # stderr usage error. Detection delegates to ``_wants_json``
        # (cli_common) so both the group-level actor check and the
        # ``get_db()`` startup-failure path agree on what counts as a
        # JSON-mode invocation — including ignoring tokens after Click's
        # ``--`` option terminator. (filigree-df988a37fc)
        if _wants_json():
            click.echo(json_mod.dumps({"error": err, "code": ErrorCode.VALIDATION}))
            ctx.exit(1)
        raise click.BadParameter(err, param_hint="'--actor'")
    ctx.obj["actor"] = cleaned
    # FIL-3 (filigree-3028a8d0f8): record whether --actor was explicitly
    # provided (vs the implicit "cli" default) so claim-shaped verbs can
    # default an omitted --assignee from an *explicit* actor identity only.
    ctx.obj["actor_explicit"] = ctx.get_parameter_source("actor") != ParameterSource.DEFAULT


# Register domain command modules
for _mod in (issues, planning, meta, workflow, admin, server, observations, files, annotations_cmds, scanners, sei):
    _mod.register(cli)


# Surface consolidation (filigree-c73c75b652): each of these long-form verbs
# mirrors an MCP tool name but is a pure duplicate of a shorter, canonical CLI
# verb that the docs/skill-pack teach (e.g. ``get-ready``→``ready``,
# ``update-issue``→``update``, ``get-issue``→``show``). Hiding them declutters
# ``--help`` (125→~103 visible verbs) WITHOUT removing them: the long forms stay
# fully functional for MCP-name muscle-memory and existing scripts — they just
# no longer appear in help. The canonical short verb in each pair stays visible.
_HIDDEN_ALIAS_VERBS = (
    "get-issue",
    "get-ready",
    "get-blocked",
    "get-changes",
    "get-plan",
    "get-critical-path",
    "get-type-info",
    "get-valid-transitions",
    "get-workflow-statuses",
    "get-workflow-guide",
    "get-label-taxonomy",
    "get-issue-events",
    "get-stale-claims",
    "list-issues",
    "list-labels",
    "list-types",
    "list-packs",
    "update-issue",
    "validate-issue",
    "reclaim-issue",
    "release-claim",
    "undo-last",
)
for _alias in _HIDDEN_ALIAS_VERBS:
    _cmd = cli.commands.get(_alias)
    if _cmd is None:
        continue
    # A few aliases (e.g. ``reclaim``/``reclaim-issue``, ``stale-claims``/
    # ``get-stale-claims``) are the SAME Command object registered under two
    # names. Setting ``.hidden`` on a shared object would also hide its visible
    # canonical sibling, so clone the object for the alias registration.
    _shares_object = any(c is _cmd and n != _alias for n, c in cli.commands.items())
    if _shares_object:
        _clone = copy.copy(_cmd)
        _clone.hidden = True
        cli.commands[_alias] = _clone
    else:
        _cmd.hidden = True


if __name__ == "__main__":
    cli()
