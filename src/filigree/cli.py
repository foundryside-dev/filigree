"""CLI for the filigree issue tracker.

Convention-based: discovers .filigree/ by walking up from cwd.
Commands are defined in cli_commands/ subpackage modules.
"""

from __future__ import annotations

import copy
import json as json_mod
import sys
from collections.abc import Sequence
from typing import Any

import click

from filigree import __version__
from filigree.cli_commands import admin, files, issues, meta, observations, planning, scanners, sei, server, workflow
from filigree.cli_commands import annotations as annotations_cmds
from filigree.cli_common import _detect_json_via_parse, _wants_json
from filigree.types.api import ErrorCode
from filigree.validation import sanitize_actor


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

    # ADR-012: surface a non-blocking warning when the claimed --actor disagrees
    # with the transport-verified OS identity. Resolution + warning never raise
    # and never block the command. Placeholder defaults ("cli") are suppressed
    # by actor_mismatch_warning.
    from filigree import actor_identity

    verified = actor_identity.resolve_os_actor()
    mismatch = actor_identity.actor_mismatch_warning(cleaned, verified)
    if mismatch is not None:
        click.echo(
            f"warning: {mismatch['code']} claimed={mismatch['claimed']!r} verified={mismatch['verified']!r}",
            err=True,
        )


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
