"""Shared CLI helpers extracted from cli.py.

Provides ``get_db()`` and ``refresh_summary()`` so that both the main
``cli.py`` and future ``cli_commands/*.py`` subpackages can access them
without circular imports.
"""

from __future__ import annotations

import json as json_mod
import logging
import sqlite3
import sys
import warnings

import click

from filigree.core import (
    FILIGREE_DIR_NAME,
    SUMMARY_FILENAME,
    FiligreeDB,
    ProjectNotInitialisedError,
    find_filigree_anchor,
    find_filigree_root,
)
from filigree.registry import RegistryVersionMismatchError
from filigree.registry_errors import registry_error_response
from filigree.summary import write_summary
from filigree.types.api import ErrorCode, SchemaVersionMismatchError

logger = logging.getLogger(__name__)


def _wants_json() -> bool:
    """Return True when the active CLI invocation passed ``--json``.

    Fast path — walk the active context stack for an already-parsed
    ``as_json``. Correct any time a subcommand callback (or anything it
    calls, including ``get_db``) is running, because Click has already
    distinguished ``--`` as its option terminator from ``--`` as the
    value of a value-taking option (e.g. ``--description --``). The
    convention ``"--json", "as_json"`` is used uniformly for the
    JSON-mode binding across the CLI.

    Slow path — when no ancestor context has parsed ``as_json`` yet
    (e.g. an ``--actor`` validation failure inside the group callback,
    before any subcommand is parsed), reparse the raw argv stashed by
    ``_FiligreeGroup.parse_args`` using Click's own parser with
    ``resilient_parsing=True`` so the parse is option-aware (handles
    ``--description --`` correctly) and side-effect-free (no callbacks
    run, no errors raised for missing required args). The detector must
    never raise, so the slow path is wrapped in a broad guard.
    (filigree-df988a37fc, filigree-e2cbfb247b)
    """
    ctx = click.get_current_context(silent=True)
    if ctx is None:
        return False
    cur: click.Context | None = ctx
    while cur is not None:
        if "as_json" in cur.params:
            return bool(cur.params["as_json"])
        cur = cur.parent
    root_ctx = ctx.find_root()
    raw_args = root_ctx.meta.get("filigree_raw_args", [])
    if not raw_args:
        return False
    root_cmd = root_ctx.command
    if not isinstance(root_cmd, click.Group):
        return False
    try:
        return _detect_json_via_parse(root_cmd, list(raw_args))
    except Exception:
        # Detector must never raise; any parse failure means we cannot
        # confirm --json so default to plain text.
        return False


def _detect_json_via_parse(group: click.Group, raw_args: list[str]) -> bool:
    """Reparse ``raw_args`` with Click to determine if any command has ``--json``.

    ``resilient_parsing=True`` skips option callbacks (notably the
    ``--actor`` validator we may be running inside) and tolerates
    missing required args. Click 9 collapses ``protected_args`` into
    ``args``, so the helper concatenates both for forward compatibility.
    """

    def remaining_args(ctx: click.Context) -> list[str]:
        with warnings.catch_warnings():
            # Click 8.x emits DeprecationWarning on protected_args access; the
            # getattr keeps us correct after Click 9 removes the attribute.
            warnings.simplefilter("ignore", DeprecationWarning)
            protected = list(getattr(ctx, "protected_args", None) or [])
        return protected + list(ctx.args)

    def parse_command(command: click.Command, args: list[str], parent: click.Context | None = None) -> bool:
        with click.Context(command, parent=parent, resilient_parsing=True) as ctx:
            command.parse_args(ctx, args)
            if bool(ctx.params.get("as_json", False)):
                return True
            if not isinstance(command, click.Group):
                return False
            sub_tokens = remaining_args(ctx)
            if not sub_tokens:
                return False
            sub_cmd = command.get_command(ctx, sub_tokens[0])
            if sub_cmd is None:
                return False
            return parse_command(sub_cmd, sub_tokens[1:], ctx)

    return parse_command(group, raw_args)


def _emit_startup_failure(exc: Exception, code: ErrorCode, *, human_prefix: str = "") -> None:
    """Render a ``get_db`` failure as JSON envelope (--json) or plain stderr."""
    if _wants_json():
        click.echo(json_mod.dumps({"error": str(exc), "code": code}))
    else:
        click.echo(f"{human_prefix}{exc}" if human_prefix else str(exc), err=True)


def _emit_registry_startup_failure(exc: RegistryVersionMismatchError) -> None:
    """Render registry protocol failures from DB startup with public envelopes."""
    response = registry_error_response(exc, action="opening project database")
    if _wants_json():
        click.echo(json_mod.dumps(response))
    else:
        click.echo(response["error"], err=True)


def get_db() -> FiligreeDB:
    """Discover the project anchor and return an initialized FiligreeDB.

    Uses :func:`find_filigree_anchor` so legacy installs (no ``.filigree.conf``
    yet) still open without requiring write access — the conf is only created
    by explicit init/install paths, not by discovery.

    Surfaces corrupt-conf / unreadable-DB / schema-mismatch failures as clean
    ``ClickException``-style exits (stderr + exit 1), or — when the active
    invocation passed ``--json`` — as the 2.0 flat envelope on stdout, rather
    than letting raw ValueError / OSError / sqlite3.Error / TypeError /
    KeyError tracebacks escape from every command. ``TypeError`` and
    ``KeyError`` cover malformed-but-JSON-valid configs (e.g. non-string
    ``db``, non-list ``enabled_packs``, missing required keys) — see GH PR
    #33 review. ``SchemaVersionMismatchError`` is a ``ValueError`` subclass
    and so must be caught before the broader ``ValueError`` arm to map to
    its own ``SCHEMA_MISMATCH`` code.
    """
    try:
        project_root, conf_path = find_filigree_anchor()
    except ProjectNotInitialisedError as exc:
        _emit_startup_failure(exc, ErrorCode.NOT_INITIALIZED)
        sys.exit(1)
    try:
        if conf_path is not None:
            return FiligreeDB.from_conf(conf_path)
        return FiligreeDB.from_filigree_dir(project_root / FILIGREE_DIR_NAME)
    except SchemaVersionMismatchError as exc:
        _emit_startup_failure(exc, ErrorCode.SCHEMA_MISMATCH, human_prefix="Error opening project database: ")
        sys.exit(1)
    except RegistryVersionMismatchError as exc:
        _emit_registry_startup_failure(exc)
        sys.exit(1)
    except (OSError, sqlite3.Error) as exc:
        _emit_startup_failure(exc, ErrorCode.IO, human_prefix="Error opening project database: ")
        sys.exit(1)
    except (ValueError, TypeError, KeyError) as exc:
        _emit_startup_failure(exc, ErrorCode.VALIDATION, human_prefix="Error opening project database: ")
        sys.exit(1)


def refresh_summary(db: FiligreeDB) -> None:
    """Regenerate context.md after mutations.

    Best-effort: the mutation has already committed by the time we're called,
    so a summary-write failure (disk full, permission, missing dir) must not
    turn a successful command into a non-zero exit. Log and continue.
    """
    try:
        filigree_dir = find_filigree_root()
        write_summary(db, filigree_dir / SUMMARY_FILENAME)
    except FileNotFoundError:
        pass  # No .filigree/ dir — skip summary
    except OSError as exc:
        logger.warning("Failed to refresh context.md summary: %s", exc)
    except Exception:
        logger.warning("Unexpected error refreshing context.md summary", exc_info=True)
