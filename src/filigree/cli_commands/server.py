"""CLI commands for server daemon management."""

from __future__ import annotations

import sys
from pathlib import Path

import click


def _resolve_store_dir_arg(project_path: Path) -> Path:
    """Resolve the store dir for a ``server register``/``unregister`` argument.

    Accepts a project root (resolves to ``.weft/filigree/`` or legacy
    ``.filigree/``) or a store dir passed directly. Layout-aware so a
    federation-layout project registers its real store, not a phantom
    ``<root>/.filigree``.
    """
    from filigree.core import FILIGREE_DIR_NAME, WEFT_DIR_NAME, WEFT_MEMBER_SUBDIR, resolve_store_dir

    if project_path.name == FILIGREE_DIR_NAME:
        return project_path
    if project_path.name == WEFT_MEMBER_SUBDIR and project_path.parent.name == WEFT_DIR_NAME:
        return project_path
    return resolve_store_dir(project_path)


def _reload_server_daemon_if_running() -> tuple[bool, str]:
    """POST /api/reload to a running daemon so it picks up server.json changes."""
    from filigree.server import daemon_status

    status = daemon_status()
    if not status.running or status.port is None:
        return True, "daemon_not_running"

    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"http://127.0.0.1:{status.port}/api/reload",
        method="POST",
        data=b"",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as _resp:  # noqa: S310
            pass
    except urllib.error.HTTPError as e:
        return False, f"daemon reload failed with HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, f"daemon reload request failed: {e}"
    return True, "daemon_reloaded"


@click.group()
def server() -> None:
    """Manage the filigree server daemon."""


@server.command("start")
@click.option(
    "--port",
    default=None,
    type=click.IntRange(1, 65535),
    help="Override port (valid TCP range: 1-65535)",
)
def server_start(port: int | None) -> None:
    """Start the filigree daemon."""
    from filigree.server import start_daemon

    result = start_daemon(port=port)
    click.echo(result.message)
    if not result.success:
        sys.exit(1)


@server.command("stop")
def server_stop() -> None:
    """Stop the filigree daemon."""
    from filigree.server import stop_daemon

    result = stop_daemon()
    click.echo(result.message)
    if not result.success:
        sys.exit(1)


@server.command("status")
def server_status_cmd() -> None:
    """Show daemon status."""
    from filigree.server import daemon_status

    status = daemon_status()
    if status.running:
        click.echo(f"Filigree daemon running (pid {status.pid}) on port {status.port}")
        click.echo(f"  Projects: {status.project_count}")
    else:
        click.echo("Filigree daemon is not running")


@server.command("register")
@click.argument("path", default=".", type=click.Path(exists=True))
def server_register(path: str) -> None:
    """Register a project with the server."""
    from filigree.server import register_project

    project_path = Path(path).resolve()
    filigree_dir = _resolve_store_dir_arg(project_path)
    if not filigree_dir.is_dir():
        click.echo(f"No filigree store (.weft/filigree/ or .filigree/) found at {project_path}", err=True)
        sys.exit(1)
    try:
        register_project(filigree_dir)
    except Exception as e:
        click.echo(str(e), err=True)
        sys.exit(1)
    click.echo(f"Registered {filigree_dir}")
    # Reload is best-effort: the registry change is already committed, so
    # failure here doesn't undo the register. Warn loudly and suggest a
    # manual reload, but exit 0 so scripts don't mistake a partial success
    # for a failed registration.
    ok, reason = _reload_server_daemon_if_running()
    if not ok:
        click.echo(f"Warning: {reason}", err=True)
        click.echo("Restart the daemon manually: `filigree server stop && filigree server start`", err=True)
    elif reason == "daemon_reloaded":
        click.echo("Reloaded running daemon")


@server.command("unregister")
@click.argument("path", default=".", type=click.Path())
def server_unregister(path: str) -> None:
    """Unregister a project from the server."""
    from filigree.server import unregister_project

    project_path = Path(path).resolve()
    filigree_dir = _resolve_store_dir_arg(project_path)
    try:
        unregister_project(filigree_dir)
    except Exception as e:
        click.echo(str(e), err=True)
        sys.exit(1)
    click.echo(f"Unregistered {filigree_dir}")
    # Same best-effort semantics as register: the unregister already
    # committed. Reload failure gets a warning + manual-restart hint, not
    # a non-zero exit.
    ok, reason = _reload_server_daemon_if_running()
    if not ok:
        click.echo(f"Warning: {reason}", err=True)
        click.echo("Restart the daemon manually: `filigree server stop && filigree server start`", err=True)
    elif reason == "daemon_reloaded":
        click.echo("Reloaded running daemon")


def register(cli: click.Group) -> None:
    """Register server group with the CLI."""
    cli.add_command(server)
