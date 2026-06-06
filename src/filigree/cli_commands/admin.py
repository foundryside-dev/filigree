"""CLI commands for admin: init, install, doctor, migrate, dashboard, metrics, export/import, archive, compact."""

from __future__ import annotations

import json as json_mod
import logging
import os
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

import click

from filigree.cli_commands.files import finding_group
from filigree.cli_common import add_hidden_flat_alias, get_db, refresh_summary
from filigree.core import (
    CONF_FILENAME,
    CONF_VERSION,
    DB_FILENAME,
    FILIGREE_DIR_NAME,
    SUMMARY_FILENAME,
    FiligreeDB,
    ProjectConfig,
    ProjectNotInitialisedError,
    find_filigree_root,
    get_mode,
    read_conf,
    read_config,
    read_schema_version,
    write_conf,
    write_config,
)
from filigree.db_schema import CURRENT_SCHEMA_VERSION
from filigree.install_support.doctor import (
    _DEPRECATED_FEDERATION_ENV_VARS,
    _WEFT_FEDERATION_ENV_VAR,
    CheckResult,
    build_doctor_summary,
    doctor_check_id,
)
from filigree.install_support.version_marker import (
    format_schema_mismatch_guidance,
    read_install_version,
    write_install_version,
)
from filigree.summary import write_summary
from filigree.types.api import SchemaVersionMismatchError


def _read_project_config_or_exit(filigree_dir: Path) -> ProjectConfig:
    try:
        return read_config(filigree_dir)
    except ValueError as exc:
        click.echo(f"Invalid project config: {exc}", err=True)
        sys.exit(1)


@click.command()
@click.option("--prefix", default=None, help="ID prefix for issues (default: directory name)")
@click.option("--name", default=None, help="Human-readable project name (default: directory name)")
@click.option(
    "--mode",
    type=click.Choice(["ethereal", "server"], case_sensitive=False),
    default=None,
    help="Installation mode (default: ethereal)",
)
def init(prefix: str | None, name: str | None, mode: str | None) -> None:
    """Initialize .filigree/ in the current directory."""
    cwd = Path.cwd()
    filigree_dir = cwd / FILIGREE_DIR_NAME

    if filigree_dir.exists():
        click.echo(f"{FILIGREE_DIR_NAME}/ already exists in {cwd}")
        previous_marker = read_install_version(filigree_dir)
        # Still ensure DB is initialized and migrated
        config = _read_project_config_or_exit(filigree_dir)
        # filigree-fa6309d551: route DB open through the v2.0 anchor-aware
        # constructors so a custom .filigree.conf `db` path is honoured.
        # Without this, init silently migrates the legacy
        # .filigree/filigree.db while the project's actual DB (declared in
        # the conf) stays un-migrated.
        conf_path = cwd / CONF_FILENAME
        if conf_path.is_file():
            try:
                existing_conf = read_conf(conf_path)
                db_path = (conf_path.parent / existing_conf["db"]).resolve()
            except (json_mod.JSONDecodeError, ValueError, OSError) as exc:
                click.echo(f"Cannot read {conf_path}: {exc}", err=True)
                sys.exit(1)
        else:
            db_path = filigree_dir / DB_FILENAME
        # Read pre-init schema version directly so we can detect upgrades —
        # the from_* constructors call initialize() internally, which would
        # mask the old version.
        old_version: int | None = None
        if db_path.exists():
            raw_conn = sqlite3.connect(str(db_path))
            try:
                old_version = read_schema_version(raw_conn)
            finally:
                raw_conn.close()
        try:
            db = FiligreeDB.from_conf(conf_path) if conf_path.is_file() else FiligreeDB.from_filigree_dir(filigree_dir)
        except SchemaVersionMismatchError as exc:
            # The DB was written by a newer filigree; this older binary
            # cannot safely touch it. Emit the same guidance text and
            # exit code (3) used by `filigree doctor` / `filigree
            # dashboard`, and do NOT update INSTALL_VERSION (which would
            # falsely advertise this older version against a v+1 DB).
            click.echo(
                format_schema_mismatch_guidance(exc.installed, exc.database),
                err=True,
            )
            sys.exit(3)
        try:
            new_version = db.get_schema_version()
            opened_prefix = db.prefix
        finally:
            db.close()
        if old_version is not None and new_version > old_version:
            click.echo(f"  Schema upgraded v{old_version} → v{new_version}")
        (filigree_dir / "scanners").mkdir(exist_ok=True)
        from filigree.install import ensure_filigree_dir_gitignore

        ensure_filigree_dir_gitignore(filigree_dir)
        # filigree-f22fc98687: backfill the v2.0 anchor on legacy installs
        # where the existing-project branch was reached without a conf. Do
        # not overwrite an existing custom anchor.
        if not conf_path.exists():
            project_name = config.get("name") or cwd.name
            backfill_conf: dict[str, object] = {
                "version": CONF_VERSION,
                "project_name": project_name,
                "prefix": opened_prefix,
                "db": f"{FILIGREE_DIR_NAME}/{DB_FILENAME}",
            }
            conf_mode = config.get("mode")
            if conf_mode and conf_mode != "ethereal":
                backfill_conf["mode"] = conf_mode
            write_conf(conf_path, backfill_conf)
            click.echo(f"  Backfilled v2.0 anchor: {conf_path}")
        # Cross-tool skew warning: if a previous marker recorded an older
        # schema, other tools / sessions pinned to that version will now
        # report SCHEMA_MISMATCH against this DB.
        if previous_marker is not None and previous_marker < CURRENT_SCHEMA_VERSION:
            click.echo(
                f"Note: this project's previous .filigree/ used schema v{previous_marker}; "
                f"the new DB is at v{CURRENT_SCHEMA_VERSION}. Other tools or sessions "
                f"pinned to filigree v{previous_marker} will report SCHEMA_MISMATCH against "
                f"this DB.",
                err=True,
            )
        write_install_version(filigree_dir, CURRENT_SCHEMA_VERSION)
        # Update name/mode if explicitly provided
        updated = False
        if name is not None:
            config["name"] = name
            updated = True
            click.echo(f"  Name: {name}")
        if mode is not None:
            config["mode"] = mode
            updated = True
            click.echo(f"  Mode: {mode}")
        if updated:
            write_config(filigree_dir, config)
        return

    prefix = prefix or cwd.name
    name = name or cwd.name
    mode = mode or "ethereal"
    filigree_dir.mkdir()
    (filigree_dir / "scanners").mkdir()

    from filigree.install import ensure_filigree_dir_gitignore

    ensure_filigree_dir_gitignore(filigree_dir)

    config = {"prefix": prefix, "name": name, "version": 1, "mode": mode}
    write_config(filigree_dir, config)

    # v2.0: also write the .filigree.conf anchor — this is the file agents
    # walk up looking for, the authoritative declaration that "this folder and
    # its subtree belong to this filigree project".
    conf_data: dict[str, object] = {
        "version": CONF_VERSION,
        "project_name": name,
        "prefix": prefix,
        "db": f"{FILIGREE_DIR_NAME}/{DB_FILENAME}",
    }
    if mode and mode != "ethereal":
        conf_data["mode"] = mode
    write_conf(cwd / CONF_FILENAME, conf_data)

    db = FiligreeDB(filigree_dir / DB_FILENAME, prefix=prefix)
    db.initialize()
    write_summary(db, filigree_dir / SUMMARY_FILENAME)
    db.close()

    # Record the schema version this project was last initialized at — used
    # by future `init` runs to warn about cross-tool schema skew.
    write_install_version(filigree_dir, CURRENT_SCHEMA_VERSION)

    click.echo(f"Initialized {FILIGREE_DIR_NAME}/ in {cwd}")
    click.echo(f"  Prefix: {prefix}")
    click.echo(f"  Mode: {mode}")
    click.echo(f"  Database: {filigree_dir / DB_FILENAME}")
    click.echo(f"  Anchor: {cwd / CONF_FILENAME}")
    click.echo(f"  Scanners: {filigree_dir / 'scanners'}/ (add .toml files to register scanners)")
    click.echo("\nNext: filigree install")


def _run_install_step(name: str, installer: Callable[[], tuple[bool, str]]) -> tuple[str, bool, str]:
    try:
        ok, msg = installer()
    except Exception as exc:
        logging.getLogger(__name__).debug("Install step %s failed", name, exc_info=True)
        return name, False, str(exc) or exc.__class__.__name__
    return name, ok, msg


@click.command()
@click.option("--claude-code", is_flag=True, help="Install MCP for Claude Code only")
@click.option("--codex", is_flag=True, help="Install MCP for Codex only")
@click.option("--claude-md", is_flag=True, help="Inject instructions into CLAUDE.md only")
@click.option("--agents-md", is_flag=True, help="Inject instructions into AGENTS.md only")
@click.option("--gitignore", is_flag=True, help="Add .filigree/ to .gitignore only")
@click.option("--hooks", "hooks_only", is_flag=True, help="Install Claude Code hooks only")
@click.option("--skills", "skills_only", is_flag=True, help="Install Claude Code skills only")
@click.option("--codex-skills", "codex_skills_only", is_flag=True, help="Install Codex skills only")
@click.option(
    "--mode",
    type=click.Choice(["ethereal", "server"], case_sensitive=False),
    default=None,
    help="Installation mode (default: preserve existing or ethereal)",
)
def install(
    claude_code: bool,
    codex: bool,
    claude_md: bool,
    agents_md: bool,
    gitignore: bool,
    hooks_only: bool,
    skills_only: bool,
    codex_skills_only: bool,
    mode: str | None,
) -> None:
    """Install filigree into the current project.

    With no flags, installs everything: MCP servers, instructions, gitignore, hooks, skills.
    With specific flags, installs only the selected components.
    """
    from filigree.install import (
        ensure_filigree_dir_gitignore,
        ensure_gitignore,
        inject_instructions,
        install_claude_code_hooks,
        install_claude_code_mcp,
        install_codex_mcp,
        install_codex_skills,
        install_skills,
    )

    try:
        filigree_dir = find_filigree_root()
    except ProjectNotInitialisedError as exc:
        # filigree-dad647cf35: catch the rich subclass before the generic
        # FileNotFoundError so ForeignDatabaseError's git-boundary remediation
        # message reaches the user instead of "No .filigree/ found".
        click.echo(str(exc), err=True)
        sys.exit(1)

    # Update mode in config if explicitly provided
    if mode is not None:
        config = _read_project_config_or_exit(filigree_dir)
        config["mode"] = mode
        write_config(filigree_dir, config)

    # Resolve effective mode (explicit flag > config > default)
    if not mode:
        try:
            mode = get_mode(filigree_dir)
        except ValueError as exc:
            click.echo(f"⚠ {exc}. Falling back to 'ethereal'.", err=True)
            mode = "ethereal"
    if mode is None:
        mode = "ethereal"

    project_root = filigree_dir.parent
    install_all = not any([claude_code, codex, claude_md, agents_md, gitignore, hooks_only, skills_only, codex_skills_only])

    results: list[tuple[str, bool, str]] = []
    server_port = 8377
    if mode == "server":
        try:
            from filigree.server import read_server_config

            server_port = read_server_config().port
        except Exception:
            logging.getLogger(__name__).debug("Failed to read server config port; defaulting to 8377", exc_info=True)

    install_steps: list[tuple[bool, str, Callable[[], tuple[bool, str]]]] = [
        (
            install_all or claude_code,
            "Claude Code MCP",
            lambda: install_claude_code_mcp(project_root, mode=mode, server_port=server_port),
        ),
        (
            install_all or codex,
            "Codex MCP",
            lambda: install_codex_mcp(project_root, mode=mode, server_port=server_port),
        ),
        (
            install_all or claude_md,
            "CLAUDE.md",
            lambda: inject_instructions(project_root / "CLAUDE.md"),
        ),
        (
            install_all or agents_md,
            "AGENTS.md",
            lambda: inject_instructions(project_root / "AGENTS.md"),
        ),
        (
            install_all or gitignore,
            ".gitignore",
            lambda: ensure_gitignore(project_root),
        ),
        (
            install_all or gitignore,
            ".filigree/.gitignore",
            lambda: ensure_filigree_dir_gitignore(filigree_dir),
        ),
        (
            install_all or hooks_only,
            "Claude Code hooks",
            lambda: install_claude_code_hooks(project_root),
        ),
        (
            install_all or skills_only,
            "Claude Code skills",
            lambda: install_skills(project_root),
        ),
        (
            install_all or codex_skills_only,
            "Codex skills",
            lambda: install_codex_skills(project_root),
        ),
    ]
    for selected, name, installer in install_steps:
        if selected:
            results.append(_run_install_step(name, installer))

    # Server mode: register project in server.json
    if mode == "server":
        try:
            from filigree.cli_commands.server import _reload_server_daemon_if_running
            from filigree.server import register_project

            register_project(filigree_dir)
            results.append(("Server registration", True, "Registered in server.json"))
            # filigree-80753e4b54: ask any running daemon to reload its
            # registry; otherwise it serves a stale view until restart.
            # Helper short-circuits cleanly when the daemon isn't running.
            ok, reason = _reload_server_daemon_if_running()
            if not ok:
                results.append(("Server reload", False, reason))
            elif reason == "daemon_not_running":
                click.echo('\nNote: start the daemon with "filigree server start"')
            else:
                results.append(("Server reload", True, "Reloaded running daemon"))
        except Exception as e:
            results.append(("Server registration", False, str(e)))

    for name, ok, msg in results:
        icon = "OK" if ok else "!!"
        click.echo(f"  {icon}  {name}: {msg}")

    ok_count = sum(1 for _, ok, _ in results if ok)
    click.echo(f"\n{ok_count}/{len(results)} installed successfully")

    # filigree-ca4e5d28dd: exit 1 if any selected installer step failed so
    # callers (CI, shell pipelines) don't treat partial success as success.
    # Also suppress the "Next:" hint, which would mislead the user into
    # thinking the install completed.
    if any(not ok for _, ok, _ in results):
        click.echo("Some install steps failed. See messages above.", err=True)
        sys.exit(1)

    click.echo('Next: filigree create "My first issue"')


def _emit_doctor_json(
    results: list[CheckResult],
    *,
    fixed_check_ids: set[str] | None = None,
    fixed_check_names: set[str] | None = None,
) -> None:
    click.echo(
        json_mod.dumps(
            build_doctor_summary(
                results,
                fixed_check_ids=fixed_check_ids,
                fixed_check_names=fixed_check_names,
            ),
            indent=2,
        )
    )


def _remove_stale_doctor_pointer(path: Path) -> tuple[bool, str]:
    try:
        if path.exists():
            path.unlink()
            return True, f"Removed {path}"
        return True, f"{path} already absent"
    except OSError as exc:
        return False, str(exc)


def _fix_mcp_token_reference(project_root: Path) -> tuple[bool, str]:
    """Migrate a deprecated token env-var name in the .mcp.json filigree header to
    the canonical ``${WEFT_FEDERATION_TOKEN}``.

    Commit-safe: only the *name* changes, never a secret value. Returns
    ``(fixed, message)``. Does NOT claim to fix the case where the canonical var is
    merely unset in the environment — filigree cannot write the agent's process
    env, so that stays the operator's one export.
    """
    mcp_path = project_root / ".mcp.json"
    try:
        data = json_mod.loads(mcp_path.read_text())
        entry = data["mcpServers"]["filigree"]
        auth = entry["headers"]["Authorization"]
    except (OSError, json_mod.JSONDecodeError, KeyError, TypeError):
        return False, "Could not read .mcp.json filigree Authorization header"
    if not isinstance(auth, str):
        return False, "filigree Authorization header is not a string"

    new_auth = auth
    for deprecated in _DEPRECATED_FEDERATION_ENV_VARS:
        new_auth = new_auth.replace(f"${{{deprecated}}}", f"${{{_WEFT_FEDERATION_ENV_VAR}}}")
    if new_auth == auth:
        return False, (
            f"Header already references ${{{_WEFT_FEDERATION_ENV_VAR}}}; "
            f"export {_WEFT_FEDERATION_ENV_VAR}=<token> and restart the daemon "
            "(filigree can't set the agent's environment)."
        )

    entry["headers"]["Authorization"] = new_auth
    mcp_path.write_text(json_mod.dumps(data, indent=2) + "\n")
    return True, (f"Migrated header to ${{{_WEFT_FEDERATION_ENV_VAR}}} — export {_WEFT_FEDERATION_ENV_VAR}=<token> to connect.")


def _apply_doctor_fixes(
    results: list[CheckResult],
    *,
    emit: Callable[[str], None] | None,
) -> tuple[int, set[str], set[str]]:
    from filigree.install import (
        inject_instructions,
        install_claude_code_hooks,
        install_claude_code_mcp,
        install_codex_mcp,
    )

    try:
        filigree_dir = find_filigree_root()
    except ProjectNotInitialisedError as exc:
        if emit is not None:
            click.echo(str(exc), err=True)
        raise click.ClickException(str(exc)) from exc

    project_root = filigree_dir.parent
    try:
        mode = get_mode(filigree_dir)
    except ValueError as exc:
        if emit is not None:
            click.echo(f"⚠ {exc}. Falling back to 'ethereal'.", err=True)
        mode = "ethereal"
    server_port = 8377
    if mode == "server":
        try:
            from filigree.server import read_server_config

            server_port = read_server_config().port
        except Exception:
            logging.getLogger(__name__).debug("Failed to read server port for --fix; using default", exc_info=True)

    # filigree-f57cb498d4: instruction files and the generated context.md are
    # filigree-owned artifacts that `--fix` repairs directly. CLAUDE.md/AGENTS.md
    # go through inject_instructions (non-destructive: it manages only its own
    # marked block, preserving user content); context.md is regenerated from the
    # DB. ``.gitignore`` is still deliberately NOT auto-repaired here — see
    # test_doctor_fix_json_does_not_repair_gitignore.
    fixable: dict[str, str] = {
        "Claude Code MCP": "claude_code_mcp",
        "Codex MCP": "codex_mcp",
        "Claude Code hooks": "hooks",
        "Ephemeral PID": "ephemeral_pid",
        "Ephemeral port": "ephemeral_port",
        "CLAUDE.md": "claude_md",
        "AGENTS.md": "agents_md",
        "context.md": "context_md",
    }

    fixed = 0
    fixed_check_ids: set[str] = set()
    fixed_check_names: set[str] = set()
    for r in results:
        if r.passed or r.name not in fixable:
            continue

        # Token-reference repair is commit-safe and targeted: migrate a deprecated
        # token env-var NAME in the .mcp.json header to the canonical one. It must
        # take precedence over the generic reinstall path, which would rewrite the
        # whole entry (URL/port) and falsely report "fixed" when the real blocker is
        # an unset env var that filigree cannot write.
        if r.code == "mcp_token_unresolved":
            ok, msg = _fix_mcp_token_reference(project_root)
            if emit is not None:
                emit(f"  {'OK' if ok else '!!'} {r.name}: {msg}")
            if ok:
                fixed += 1
                fixed_check_ids.add(doctor_check_id(r))
                fixed_check_names.add(r.name)
            continue

        fix_key = fixable[r.name]
        ok = False
        try:
            if fix_key == "claude_code_mcp":
                ok, msg = install_claude_code_mcp(project_root, mode=mode, server_port=server_port)
                if emit is not None:
                    emit(f"  {'OK' if ok else '!!'} {r.name}: {msg}")
            elif fix_key == "codex_mcp":
                ok, msg = install_codex_mcp(project_root, mode=mode, server_port=server_port)
                if emit is not None:
                    emit(f"  {'OK' if ok else '!!'} {r.name}: {msg}")
            elif fix_key == "hooks":
                ok, msg = install_claude_code_hooks(project_root)
                if emit is not None:
                    emit(f"  {'OK' if ok else '!!'} {r.name}: {msg}")
            elif fix_key == "ephemeral_pid":
                ok, msg = _remove_stale_doctor_pointer(filigree_dir / "ephemeral.pid")
                if emit is not None:
                    emit(f"  {'OK' if ok else '!!'} {r.name}: {msg}")
            elif fix_key == "ephemeral_port":
                ok, msg = _remove_stale_doctor_pointer(filigree_dir / "ephemeral.port")
                if emit is not None:
                    emit(f"  {'OK' if ok else '!!'} {r.name}: {msg}")
            elif fix_key == "claude_md":
                ok, msg = inject_instructions(project_root / "CLAUDE.md")
                if emit is not None:
                    emit(f"  {'OK' if ok else '!!'} {r.name}: {msg}")
            elif fix_key == "agents_md":
                ok, msg = inject_instructions(project_root / "AGENTS.md")
                if emit is not None:
                    emit(f"  {'OK' if ok else '!!'} {r.name}: {msg}")
            elif fix_key == "context_md":
                # Regenerate filigree's own generated snapshot. Open the DB via
                # the v2.0 anchor-aware constructors (not get_db(), which
                # sys.exits past this try/except on a broken DB); a genuine DB
                # failure then surfaces as "Cannot fix context.md" and the loop
                # continues rather than aborting the whole doctor run.
                conf_path = project_root / CONF_FILENAME
                db = FiligreeDB.from_conf(conf_path) if conf_path.is_file() else FiligreeDB.from_filigree_dir(filigree_dir)
                try:
                    write_summary(db, filigree_dir / SUMMARY_FILENAME)
                finally:
                    db.close()
                ok, msg = True, "Regenerated context.md"
                if emit is not None:
                    emit(f"  OK {r.name}: {msg}")
            if ok:
                fixed += 1
                fixed_check_ids.add(doctor_check_id(r))
                fixed_check_names.add(r.name)
        except Exception as e:
            if emit is not None:
                click.echo(f"  !!  Cannot fix {r.name}: {e}", err=True)

    # Stale server-registry entries (vanished project directories) carry a
    # dynamic, non-unique check name (``Project "<prefix>"``), so they can't be
    # routed by the name-keyed table above. Route them by their stable ``code``
    # and unregister by the exact stored key in one locked pass. Safe to clean
    # because the project re-registers itself on its next use; only gone-dir
    # entries reach here (see _doctor_server_checks).
    orphans: list[CheckResult] = []
    orphan_keys: list[str] = []
    for r in results:
        target = r.fix_target
        if not r.passed and r.code == "server_registry_orphan" and target is not None:
            orphans.append(r)
            orphan_keys.append(target)
    if orphan_keys:
        from filigree.server import unregister_projects

        try:
            removed = unregister_projects(orphan_keys)
        except Exception as e:
            if emit is not None:
                click.echo(f"  !!  Cannot clean stale server registry: {e}", err=True)
            removed = set()
        for r in orphans:
            if r.fix_target in removed:
                fixed += 1
                fixed_check_ids.add(doctor_check_id(r))
                fixed_check_names.add(r.name)
                if emit is not None:
                    emit(f"  OK {r.name}: Unregistered stale project {r.fix_target}")

    return fixed, fixed_check_ids, fixed_check_names


@click.command()
@click.option("--fix", is_flag=True, help="Auto-fix issues where possible")
@click.option("--verbose", is_flag=True, help="Show all checks including passed")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable doctor summary")
def doctor(fix: bool, verbose: bool, as_json: bool) -> None:
    """Run health checks on the filigree installation."""
    from filigree.install import run_doctor

    results = run_doctor()

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)

    if not as_json:
        click.echo(f"filigree doctor  ──  {passed} passed  {failed} issues")
        click.echo()

        for r in results:
            if r.passed and not verbose:
                continue
            icon = "OK" if r.passed else "!!"
            click.echo(f"  {icon}  {r.name}: {r.message}")
            if not r.passed and r.fix_hint:
                click.echo(f"       -> {r.fix_hint}")

    # Schema-mismatch (v+1) is a distinct exit code (3) from generic check
    # failures (1). Don't attempt --fix on this — there's nothing to fix
    # forward when the DB is newer than the installed filigree.
    if any(r.code == "schema_mismatch_forward" for r in results):
        if as_json:
            _emit_doctor_json(results)
        sys.exit(3)

    fixed = 0
    fixed_check_ids: set[str] = set()
    fixed_check_names: set[str] = set()
    if fix and failed > 0:
        if not as_json:
            click.echo("\nApplying fixes...")
        try:
            fixed, fixed_check_ids, fixed_check_names = _apply_doctor_fixes(results, emit=None if as_json else click.echo)
        except click.ClickException:
            if as_json:
                _emit_doctor_json(results)
            sys.exit(1)

        unfixed = failed - fixed
        if unfixed > 0 and not as_json:
            click.echo(f"\n  Fixed {fixed}/{failed} issues. {unfixed} require manual intervention.")

    if as_json:
        _emit_doctor_json(results, fixed_check_ids=fixed_check_ids, fixed_check_names=fixed_check_names)
        if failed == 0 or (fix and (failed - fixed) == 0):
            return
        sys.exit(1)

    if failed == 0:
        click.echo("\nAll checks passed.")
        return

    # filigree-467d1e7487: surface non-schema failures as a non-zero exit
    # so CI scripts and `set -e` shells can detect breakage. Schema-mismatch
    # already exited(3) above; here we own exit(1) for the generic case.
    # Without --fix, every failure is unresolved. With --fix, only failures
    # the fixer could not address remain.
    if not fix or (failed - fixed) > 0:
        sys.exit(1)


@click.command()
@click.option("--from-beads", is_flag=True, help="Migrate from .beads database")
@click.option("--beads-db", default=None, help="Path to beads.db (default: .beads/beads.db)")
def migrate(from_beads: bool, beads_db: str | None) -> None:
    """Migrate issues from another system."""
    if not from_beads:
        click.echo("Only --from-beads is supported currently.", err=True)
        sys.exit(1)

    from filigree.migrate import migrate_from_beads

    beads_path = beads_db or str(Path.cwd() / ".beads" / "beads.db")
    if not Path(beads_path).exists():
        click.echo(f"Beads DB not found: {beads_path}", err=True)
        sys.exit(1)

    with get_db() as db:
        count = migrate_from_beads(beads_path, db)
        refresh_summary(db)
        click.echo(f"Migrated {count} issues from beads")


@click.command()
@click.option(
    "--port",
    default=None,
    # filigree-31da65493c: validate at the CLI boundary (mirrors `server
    # start` at server.py:43-49). Without this, `--server-mode --port 0`
    # would persist a bogus port into daemon state before bind failed.
    type=click.IntRange(1, 65535),
    help="Server port (valid TCP range: 1-65535; defaults to configured daemon port in --server-mode, else 8377)",
)
@click.option("--no-browser", is_flag=True, help="Don't auto-open browser")
@click.option("--server-mode", is_flag=True, help="Multi-project server mode (reads server.json)")
@click.option(
    "--allow-http-force-close",
    is_flag=True,
    help=(
        "Permit ``force=true`` on POST /api/batch/close and "
        "POST /api/weft/batch/close. Off by default — HTTP callers cannot "
        "use the workflow escape lane unless explicitly opted in."
    ),
)
@click.option(
    "--allow-local-fallback",
    is_flag=True,
    help="When registry_backend=loomweave, route file auto-creates through the local registry for recovery.",
)
def dashboard(
    port: int | None,
    no_browser: bool,
    server_mode: bool,
    allow_http_force_close: bool,
    allow_local_fallback: bool,
) -> None:
    """Launch the web dashboard."""
    from filigree.dashboard import DEFAULT_PORT
    from filigree.dashboard import main as dashboard_main

    pid_claimed = False
    current_pid = os.getpid()
    if server_mode:
        from filigree.server import claim_current_process_as_daemon, read_server_config

        effective_port = port if port is not None else read_server_config().port
        # Pass only the user-specified port to claim — ``None`` means "don't
        # overwrite the configured daemon port" (filigree-f863b9d1f8).
        pid_claimed = claim_current_process_as_daemon(port=port)
        if not pid_claimed:
            # A different live daemon is already tracked — refuse rather than
            # racing a second server (filigree-ceb2da2411).
            click.echo(
                "Another filigree daemon is already running. Stop it with `filigree server stop` first.",
                err=True,
            )
            sys.exit(1)
    else:
        effective_port = port if port is not None else DEFAULT_PORT

    try:
        dashboard_main(
            port=effective_port,
            no_browser=no_browser,
            server_mode=server_mode,
            allow_http_force_close=allow_http_force_close,
            allow_local_fallback=allow_local_fallback,
        )
    finally:
        if server_mode and pid_claimed:
            from filigree.server import release_daemon_pid_if_owned

            release_daemon_pid_if_owned(current_pid)


@click.command("session-context")
def session_context() -> None:
    """Output project snapshot for Claude Code session context."""
    try:
        from filigree.hooks import generate_session_context

        context = generate_session_context()
        if context:
            click.echo(context)
    except Exception:
        logging.getLogger(__name__).warning("session-context hook failed", exc_info=True)
        click.echo("Warning: session-context hook failed (run with -v for details)", err=True)


@click.command("ensure-dashboard")
@click.option(
    "--port",
    default=None,
    # filigree-31da65493c: same boundary validation as `dashboard --port`.
    type=click.IntRange(1, 65535),
    help="Dashboard port override (valid TCP range: 1-65535; server mode)",
)
def ensure_dashboard_cmd(port: int | None) -> None:
    """Ensure the filigree dashboard is running."""
    try:
        from filigree.hooks import ensure_dashboard_running

        message = ensure_dashboard_running(port=port)
        if message:
            click.echo(message)
    except Exception:
        logging.getLogger(__name__).warning("ensure-dashboard hook failed", exc_info=True)
        click.echo("Warning: ensure-dashboard hook failed (run with -v for details)", err=True)


@click.command()
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.option(
    "--days",
    default=30,
    type=click.IntRange(1, 3650),
    help="Lookback window in days (1-3650)",
)
def metrics(as_json: bool, days: int) -> None:
    """Show flow metrics: cycle time, lead time, throughput."""
    from filigree.analytics import get_flow_metrics

    with get_db() as db:
        data = get_flow_metrics(db, days=days)

    if as_json:
        click.echo(json_mod.dumps(data, indent=2, default=str))
        return

    click.echo(f"Flow Metrics (last {data['period_days']} days)")
    click.echo(f"  Throughput:     {data['throughput']} closed")
    avg_ct = data["avg_cycle_time_hours"]
    avg_lt = data["avg_lead_time_hours"]
    click.echo(f"  Avg cycle time: {f'{avg_ct}h' if avg_ct is not None else 'n/a'}")
    click.echo(f"  Avg lead time:  {f'{avg_lt}h' if avg_lt is not None else 'n/a'}")
    if data["by_type"]:
        click.echo("\n  By type:")
        for t, m in sorted(data["by_type"].items()):
            ct_str = f"{m['avg_cycle_time_hours']}h" if m["avg_cycle_time_hours"] is not None else "n/a"
            click.echo(f"    {t:<12} {m['count']} closed, avg cycle: {ct_str}")


@click.command("export")
@click.argument("output", type=click.Path())
def export_data(output: str) -> None:
    """Export full project data to a JSONL file."""
    with get_db() as db:
        # filigree-48613c1c55: surface FS errors (missing parent dir,
        # permission denied, disk full) and DB errors as a clean
        # "Export failed: …" line, matching `import`'s contract.
        try:
            count = db.export_jsonl(output)
        except (OSError, sqlite3.Error) as e:
            click.echo(f"Export failed: {e}", err=True)
            sys.exit(1)
        click.echo(f"Exported {count} records to {output}")


@click.command("import")
@click.argument("input_file", type=click.Path(exists=True))
@click.option("--merge", is_flag=True, help="Skip existing records instead of failing on conflict")
@click.option(
    "--allow-foreign-ids",
    is_flag=True,
    help=(
        "Keep source issue IDs even when their prefix doesn't match this "
        "project (migration escape hatch; imported rows become readable "
        "but not mutable)."
    ),
)
def import_data(input_file: str, merge: bool, allow_foreign_ids: bool) -> None:
    """Import full project data from a JSONL file."""
    with get_db() as db:
        try:
            result = db.import_jsonl(input_file, merge=merge, allow_foreign_ids=allow_foreign_ids)
        except (json_mod.JSONDecodeError, KeyError, ValueError, sqlite3.IntegrityError, OSError) as e:
            click.echo(f"Import failed: {e}", err=True)
            sys.exit(1)
        refresh_summary(db)
        click.echo(f"Imported {result['count']} records from {input_file}")
        if result["skipped_types"]:
            for rtype, rcount in result["skipped_types"].items():
                click.echo(f"  Warning: skipped {rcount} record(s) with unknown type {rtype!r}", err=True)


@click.command("archive")
@click.option("--days", default=30, type=click.IntRange(min=0), help="Archive issues closed more than N days ago (default: 30)")
@click.option("--label", default=None, type=str, help="Only archive closed issues currently carrying this label")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def archive(ctx: click.Context, days: int, label: str | None, as_json: bool) -> None:
    """Archive old closed issues to reduce active issue count."""
    if days < 7 and not (label and label.strip()):
        click.echo(
            f"Error: --days {days} requires a non-empty --label scope to avoid archiving recent issues project-wide.",
            err=True,
        )
        sys.exit(1)
    with get_db() as db:
        try:
            archived = db.archive_closed(days_old=days, actor=ctx.obj["actor"], label=label)
        except ValueError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        if as_json:
            click.echo(json_mod.dumps({"archived": archived, "count": len(archived)}, indent=2, default=str))
        else:
            if archived:
                scope = f" with label {label!r}" if label is not None else ""
                click.echo(f"Archived {len(archived)} issues{scope} (closed > {days} days)")
                for aid in archived:
                    click.echo(f"  {aid}")
            else:
                click.echo("No issues to archive")
        refresh_summary(db)


@click.command("clean-stale-findings")
@click.option("--days", default=30, type=click.IntRange(min=0), help="Mark as fixed if unseen for more than N days (default: 30)")
@click.option("--scan-source", default=None, type=str, help="Only clean findings from this scan source")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def clean_stale_findings(ctx: click.Context, days: int, scan_source: str | None, as_json: bool) -> None:
    """Move stale unseen_in_latest findings to fixed status."""
    with get_db() as db:
        result = db.clean_stale_findings(days=days, scan_source=scan_source, actor=ctx.obj["actor"])
        if as_json:
            click.echo(json_mod.dumps(result))
            return
        if result["findings_fixed"] > 0:
            click.echo(f"Fixed {result['findings_fixed']} stale findings (unseen > {days} days)")
        else:
            click.echo("No stale findings to clean")
        # Surface best-effort finding→issue cascade advisories so a partial
        # cascade failure is visible in human mode too, not just --json.
        for warning in result["warnings"]:
            click.echo(f"Warning: {warning}", err=True)


@click.command("compact")
@click.option("--keep", default=50, type=click.IntRange(min=0), help="Keep N most recent events per archived issue (default: 50)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def compact(keep: int, as_json: bool) -> None:
    """Compact event history for archived issues."""
    with get_db() as db:
        deleted = db.compact_events(keep_recent=keep)
        if as_json:
            click.echo(json_mod.dumps({"deleted_events": deleted}))
        else:
            click.echo(f"Compacted {deleted} events")
        if deleted > 0:
            db.vacuum()
            if not as_json:
                click.echo("Vacuumed database")


def register(cli: click.Group) -> None:
    """Register admin commands with the CLI group."""
    cli.add_command(init)
    cli.add_command(install)
    cli.add_command(doctor)
    cli.add_command(migrate)
    cli.add_command(dashboard)
    cli.add_command(ensure_dashboard_cmd)
    cli.add_command(session_context)
    cli.add_command(metrics)
    cli.add_command(export_data)
    cli.add_command(import_data)
    cli.add_command(archive)
    cli.add_command(compact)
    # clean-stale-findings: canonical grouped form ``finding clean-stale``; the
    # flat name stays as a hidden back-compat alias. (filigree-03303d6c5a)
    finding_group.add_command(clean_stale_findings, "clean-stale")
    add_hidden_flat_alias(cli, clean_stale_findings, "clean-stale-findings")
