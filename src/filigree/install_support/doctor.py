"""Health-check system (``filigree doctor``).

Runs a battery of checks against the project's ``.filigree/`` directory,
MCP configuration, Claude Code hooks, skills, and more.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filigree.core import (
    CONF_FILENAME,
    CONFIG_FILENAME,
    DB_FILENAME,
    FILIGREE_DIR_NAME,
    SUMMARY_FILENAME,
    ForeignDatabaseError,
    find_filigree_root,
    read_conf,
    read_schema_version,
)
from filigree.db_schema import CURRENT_SCHEMA_VERSION
from filigree.install_support import (
    FILIGREE_INSTRUCTIONS_MARKER,
    SKILL_MARKER,
    SKILL_NAME,
)
from filigree.install_support.gitignore import has_active_filigree_ignore
from filigree.install_support.hooks import (
    SESSION_CONTEXT_COMMAND,
    _extract_hook_binary,
    _has_hook_command,
)
from filigree.install_support.integrations import _codex_config_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """Result of a single doctor check."""

    name: str
    passed: bool
    message: str
    fix_hint: str = ""
    code: str | None = None  # machine-readable check identifier; e.g. "schema_mismatch_forward"
    check_id: str | None = None

    @property
    def icon(self) -> str:
        return "OK" if self.passed else "!!"


_RESERVED_SUMMARY_CHECK_IDS = (
    "dashboard.port",
    "mcp.registration",
    "api.availability",
    "auth.config",
    "scanner.results",
    "entity_associations.routes",
)

_CHECK_ID_BY_NAME = {
    ".filigree/ directory": "project.directory",
    ".filigree.conf anchor": "project.anchor",
    "Home-directory .filigree.conf": "project.home_anchor",
    "config.json": "project.config",
    "filigree.db": "database.access",
    "Schema version": "database.schema",
    "File registry backend state": "file_registry.backend",
    "context.md": "context.summary",
    ".gitignore": "git.ignore",
    "Claude Code MCP": "mcp.registration",
    "Codex MCP": "mcp.registration",
    "Claude Code hooks": "hooks.session_context",
    "Claude Code skills": "skills.claude_code",
    "Codex skills": "skills.codex",
    "CLAUDE.md": "instructions.claude_md",
    "AGENTS.md": "instructions.agents_md",
    "Ephemeral PID": "dashboard.port",
    "Ephemeral port": "dashboard.port",
    "Server daemon": "dashboard.port",
    "API routes": "api.availability",
    "Auth config": "auth.config",
    "Scan results routes": "scanner.results",
    "Entity association routes": "entity_associations.routes",
    "Bundled scanner registrations": "scanner.registration",
    "Git working tree": "git.working_tree",
    "Installation": "installation.method",
    "Installation method": "installation.method",
}


def doctor_check_id(result: CheckResult) -> str:
    """Return the stable JSON-contract check id for a human doctor result."""
    if result.check_id:
        return result.check_id
    mapped = _CHECK_ID_BY_NAME.get(result.name)
    if mapped is not None:
        return mapped
    normalized = re.sub(r"[^a-z0-9]+", ".", result.name.lower()).strip(".")
    return normalized or "unknown"


def build_doctor_summary(
    results: list[CheckResult],
    *,
    fixed_check_ids: set[str] | None = None,
    fixed_check_names: set[str] | None = None,
) -> dict[str, Any]:
    """Build the shared machine-readable doctor summary contract.

    The contract is intentionally compact so other agent-side tools can consume
    it without parsing Filigree's human diagnostic text.

    **Check-id collapse is deliberate.** Multiple :class:`CheckResult`s that map
    to the same ``check_id`` (e.g. the per-client "Claude Code MCP" and "Codex
    MCP" checks both fold into ``mcp.registration``) are merged into a single
    summary entry. The merge is order-independent and conservative: ``failed``
    always wins, and ``fixed`` is only recorded when nothing under that id
    failed. This keeps the machine contract coarse on purpose — consumers get a
    stable per-concern status, not per-sub-check granularity. Callers needing to
    know *which* sub-check failed should read the human ``results`` list, whose
    granularity is preserved.
    """
    fixed = fixed_check_ids or set()
    fixed_names = fixed_check_names or set()
    by_id: dict[str, dict[str, Any]] = {}
    next_actions: list[str] = []
    seen_actions: set[str] = set()

    for result in results:
        check_id = doctor_check_id(result)
        entry = by_id.setdefault(check_id, {"id": check_id, "status": "ok", "fixed": False})
        if check_id in fixed or result.name in fixed_names:
            if entry["status"] != "failed":
                entry["status"] = "fixed"
                entry["fixed"] = True
            continue
        if not result.passed:
            entry["status"] = "failed"
            entry["fixed"] = False
            if result.fix_hint:
                action = f"{check_id}: {result.fix_hint}"
                if action not in seen_actions:
                    next_actions.append(action)
                    seen_actions.add(action)

    for check_id in _RESERVED_SUMMARY_CHECK_IDS:
        by_id.setdefault(check_id, {"id": check_id, "status": "ok", "fixed": False})

    checks = [by_id[check_id] for check_id in sorted(by_id)]
    return {
        "ok": all(check["status"] != "failed" for check in checks),
        "checks": checks,
        "next_actions": next_actions,
    }


def _is_venv_binary(path: str) -> bool:
    """Return True when *path* is inside a Python virtual environment."""
    p = Path(path)
    # Walk up looking for pyvenv.cfg (the marker for any venv/virtualenv)
    return any((parent / "pyvenv.cfg").exists() for parent in p.parents)


def _validate_filigree_mcp_entry(entry: object) -> dict[str, object]:
    """Return *entry* if valid, raise ``ValueError`` otherwise.

    Accepts either of the two shapes the installer emits
    (``install_support/integrations.py``):

    * stdio: a dict with ``type == "stdio"`` (or no ``type``), a non-empty
      string ``command``, and ``args`` as a list of strings (when present).
    * streamable-http: a dict with ``type == "streamable-http"`` and a
      non-empty string ``url``.

    Anything else (non-dict, missing/empty fields, unknown transport) flows
    into the existing "Invalid .mcp.json" branch — see
    filigree-466bcb6279 for the prior accept-anything-truthy behaviour.
    """
    if not isinstance(entry, dict):
        raise ValueError("mcpServers.filigree must be a JSON object")
    transport = entry.get("type", "stdio")
    if transport == "stdio":
        command = entry.get("command")
        if not isinstance(command, str) or not command:
            raise ValueError("mcpServers.filigree.command must be a non-empty string")
        args = entry.get("args", [])
        if not isinstance(args, list):
            raise ValueError("mcpServers.filigree.args must be a list")
        if not all(isinstance(arg, str) for arg in args):
            raise ValueError("mcpServers.filigree.args entries must be strings")
    elif transport == "streamable-http":
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("mcpServers.filigree.url must be a non-empty string")
    else:
        raise ValueError(f"unknown mcpServers.filigree.type: {transport!r}")
    return entry


def _doctor_file_registry_backend_state(
    conn: sqlite3.Connection,
    *,
    registry_settings: dict[str, Any] | None,
    schema_version: int | None,
) -> CheckResult | None:
    """Return a doctor result for ADR-014 registry/data consistency."""
    if schema_version is None or schema_version < 17:
        return None
    settings = registry_settings or {}
    if settings.get("registry_backend", "local") != "loomweave":
        return None
    loomweave = settings.get("loomweave")
    allow_local_fallback = bool(loomweave.get("allow_local_fallback", False)) if isinstance(loomweave, dict) else False
    if allow_local_fallback:
        return CheckResult(
            "File registry backend state",
            True,
            "Loomweave is configured with local fallback enabled; local file_records may be intentional during fallback.",
        )
    try:
        local_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM file_records WHERE registry_backend != ?",
                ("loomweave",),
            ).fetchone()[0]
        )
    except sqlite3.Error as exc:
        return CheckResult(
            "File registry backend state",
            False,
            f"Could not inspect file registry backend state: {exc}",
            fix_hint="Database may be corrupted. Restore from backup.",
        )
    if local_count:
        return CheckResult(
            "File registry backend state",
            False,
            f"Project is configured for Loomweave but {local_count} file_records row(s) still use local registry identity.",
            fix_hint="Run: filigree migrate-registry --to loomweave --dry-run, then --execute after reviewing unresolved rows.",
            code="registry_backend_hybrid_state",
        )
    return CheckResult("File registry backend state", True, "All file_records rows use Loomweave registry identity.")


def _is_absolute_command_path(path: str) -> bool:
    """Return True when *path* looks like an absolute command path."""
    if not path:
        return False
    if Path(path).is_absolute():
        return True
    # Handle Windows absolute paths when running on non-Windows hosts.
    if path.startswith("\\\\"):
        return True
    return len(path) > 2 and path[0].isalpha() and path[1] == ":" and path[2] in ("/", "\\")


def _doctor_bundled_scanner_checks(filigree_dir: Path) -> list[CheckResult]:
    """Check project scanner TOML against current bundled scanner definitions."""
    from filigree.bundled_scanners import BUNDLED_SCANNERS, bundled_scanner_config_status

    scanners_dir = filigree_dir / "scanners"
    if not scanners_dir.is_dir():
        return [
            CheckResult(
                "Bundled scanner registrations",
                True,
                "0 scanners enabled",
                fix_hint="Run: filigree scanner available",
            )
        ]

    stale: list[str] = []
    current: list[str] = []
    missing_commands: list[str] = []
    for scanner_name, bundled in sorted(BUNDLED_SCANNERS.items()):
        status = bundled_scanner_config_status(scanners_dir, scanner_name)
        if status == "current":
            current.append(scanner_name)
            if shutil.which(bundled.command) is None:
                missing_commands.append(f"{scanner_name} ({bundled.command})")
        elif status == "stale_bundled":
            stale.append(scanner_name)

    if stale:
        commands = ", ".join(f"filigree scanner enable {name} --force" for name in stale)
        return [
            CheckResult(
                "Bundled scanner registrations",
                False,
                f"Stale bundled scanner registration(s): {', '.join(stale)}",
                fix_hint=f"Run: {commands}",
                code="stale_bundled_scanner",
            )
        ]

    if missing_commands:
        return [
            CheckResult(
                "Bundled scanner registrations",
                False,
                f"Enabled bundled scanner missing command(s): {', '.join(missing_commands)}",
                fix_hint="Run: uv tool install --upgrade filigree",
                code="bundled_scanner_command_missing",
            )
        ]

    if current:
        return [CheckResult("Bundled scanner registrations", True, f"Current bundled scanner registration(s): {', '.join(current)}")]
    configured = sorted(p.stem for p in scanners_dir.glob("*.toml"))
    if configured:
        return [CheckResult("Bundled scanner registrations", True, "No bundled scanner registrations")]
    return [
        CheckResult(
            "Bundled scanner registrations",
            True,
            "0 scanners enabled",
            fix_hint="Run: filigree scanner available",
        )
    ]


def _route_supports(route_table: dict[str, set[str]], path: str, method: str) -> bool:
    return method.upper() in route_table.get(path, set())


def _doctor_dashboard_contract_checks() -> list[CheckResult]:
    """Validate dashboard/API route registration without issuing mutating calls."""
    try:
        from filigree.dashboard import FEDERATION_API_ENV_VAR, LEGACY_API_ENV_VAR, create_app

        app = create_app(server_mode=False)
        route_table: dict[str, set[str]] = {}
        for route in getattr(app, "routes", []):
            path = getattr(route, "path", "")
            if not isinstance(path, str) or not path:
                continue
            methods = getattr(route, "methods", None)
            if methods is None:
                continue
            route_table.setdefault(path, set()).update(str(method).upper() for method in methods)
    except Exception as exc:
        message = f"Could not inspect dashboard route table: {exc}"
        return [
            CheckResult("API routes", False, message, fix_hint="Run: filigree doctor --verbose"),
            CheckResult("Scan results routes", False, message, fix_hint="Run: filigree doctor --verbose"),
            CheckResult("Entity association routes", False, message, fix_hint="Run: filigree doctor --verbose"),
            CheckResult("Auth config", False, message, fix_hint="Run: filigree doctor --verbose"),
        ]

    results: list[CheckResult] = []

    if _route_supports(route_table, "/api/health", "GET"):
        results.append(CheckResult("API routes", True, "GET /api/health registered"))
    else:
        results.append(
            CheckResult(
                "API routes",
                False,
                "GET /api/health is not registered",
                fix_hint="Reinstall or upgrade filigree; dashboard route registration is incomplete.",
            )
        )

    scanner_routes = (
        ("/api/loom/scan-results", "POST"),
        ("/api/scan-results", "POST"),
        ("/api/files/_schema", "GET"),
    )
    missing_scanner = [f"{method} {path}" for path, method in scanner_routes if not _route_supports(route_table, path, method)]
    if missing_scanner:
        results.append(
            CheckResult(
                "Scan results routes",
                False,
                f"Missing route(s): {', '.join(missing_scanner)}",
                fix_hint="Reinstall or upgrade filigree; scanner/result API route registration is incomplete.",
            )
        )
    else:
        results.append(CheckResult("Scan results routes", True, "scan-results and file schema routes registered"))

    entity_routes = (
        ("/api/issue/{issue_id}/entity-associations", "GET"),
        ("/api/issue/{issue_id}/entity-associations", "POST"),
        ("/api/issue/{issue_id}/entity-associations", "DELETE"),
        ("/api/entity-associations", "GET"),
    )
    missing_entity = [f"{method} {path}" for path, method in entity_routes if not _route_supports(route_table, path, method)]
    if missing_entity:
        results.append(
            CheckResult(
                "Entity association routes",
                False,
                f"Missing route(s): {', '.join(missing_entity)}",
                fix_hint="Reinstall or upgrade filigree; entity-association API route registration is incomplete.",
            )
        )
    else:
        results.append(CheckResult("Entity association routes", True, "entity-association routes registered"))

    auth_envs = {
        FEDERATION_API_ENV_VAR: os.environ.get(FEDERATION_API_ENV_VAR),
        LEGACY_API_ENV_VAR: os.environ.get(LEGACY_API_ENV_VAR),
    }
    empty_envs = sorted(name for name, value in auth_envs.items() if value is not None and not value.strip())
    configured_envs = sorted(name for name, value in auth_envs.items() if value is not None and value.strip())
    if empty_envs:
        results.append(
            CheckResult(
                "Auth config",
                False,
                f"Empty auth token environment variable(s): {', '.join(empty_envs)}",
                fix_hint=f"Unset {', '.join(empty_envs)} or set a non-empty token before starting the dashboard.",
            )
        )
    elif configured_envs:
        results.append(CheckResult("Auth config", True, f"Federation bearer auth configured via {configured_envs[0]}"))
    else:
        results.append(CheckResult("Auth config", True, "Federation auth disabled; loopback dashboard remains open by default"))

    return results


# ---------------------------------------------------------------------------
# Mode-specific checks
# ---------------------------------------------------------------------------


def _doctor_ethereal_checks(filigree_dir: Path) -> list[CheckResult]:
    """Ethereal mode health checks."""
    from filigree.ephemeral import read_pid_file, read_port_file, verify_pid_ownership

    results: list[CheckResult] = []
    pid_file = filigree_dir / "ephemeral.pid"
    port_file = filigree_dir / "ephemeral.port"

    if pid_file.exists():
        info = read_pid_file(pid_file)
        # Ownership (liveness + argv identity + recorded-port) — not raw aliveness —
        # so a recycled PID belonging to an unrelated process is reported as stale
        # rather than as a healthy dashboard (filigree-aa80d21b97).
        if info and verify_pid_ownership(
            pid_file,
            expected_cmd="filigree",
            required_args=("dashboard",),
        ):
            results.append(CheckResult("Ephemeral PID", True, f"Process {info['pid']} alive"))
        else:
            pid_val = info["pid"] if info else "unknown"
            results.append(
                CheckResult(
                    "Ephemeral PID",
                    False,
                    f"Stale PID file (pid {pid_val})",
                    fix_hint="Remove .filigree/ephemeral.pid or run: filigree ensure-dashboard",
                )
            )

    if port_file.exists():
        from filigree.hooks import _is_port_listening

        port = read_port_file(port_file)
        if port and _is_port_listening(port):
            results.append(CheckResult("Ephemeral port", True, f"Port {port} listening"))
        else:
            results.append(
                CheckResult(
                    "Ephemeral port",
                    False,
                    f"Port {port} not listening",
                    fix_hint="Dashboard may have crashed. Run: filigree ensure-dashboard",
                )
            )

    return results


def _doctor_server_checks(filigree_dir: Path) -> list[CheckResult]:
    """Server mode health checks."""
    from filigree.server import daemon_status, read_server_config

    results: list[CheckResult] = []
    status = daemon_status()
    if status.running:
        results.append(
            CheckResult(
                "Server daemon",
                True,
                f"Running (pid {status.pid}, port {status.port}, {status.project_count} projects)",
            )
        )
    else:
        results.append(
            CheckResult(
                "Server daemon",
                False,
                "Not running",
                fix_hint="Run: filigree server start",
            )
        )

    # Check registered projects health
    config = read_server_config()
    for path_str, info in config.projects.items():
        p = Path(path_str)
        if not p.is_dir():
            results.append(
                CheckResult(
                    f'Project "{info.get("prefix", "?")}"',
                    False,
                    f"Directory gone: {path_str}",
                    fix_hint=f"Run: filigree server unregister {p.parent}",
                )
            )

    return results


def _check_codex_mcp(filigree_dir: Path) -> CheckResult:
    """Check Codex MCP configuration with early returns for clarity."""
    codex_config = _codex_config_path()
    if not codex_config.exists():
        return CheckResult("Codex MCP", False, "No ~/.codex/config.toml found", fix_hint="Run: filigree install --codex")

    try:
        parsed = tomllib.loads(codex_config.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        return CheckResult(
            "Codex MCP", False, "Invalid ~/.codex/config.toml", fix_hint="Fix ~/.codex/config.toml or run: filigree install --codex"
        )

    mcp_servers = parsed.get("mcp_servers", {})
    filigree_server = mcp_servers.get("filigree") if isinstance(mcp_servers, dict) else None
    if not isinstance(filigree_server, dict):
        return CheckResult("Codex MCP", False, "filigree not in ~/.codex/config.toml", fix_hint="Run: filigree install --codex")

    # Codex config is global. Project-pinned args/URLs are unsafe because they
    # outlive the folder the user is currently working in.
    if "url" in filigree_server:
        return CheckResult(
            "Codex MCP",
            False,
            "filigree in ~/.codex/config.toml uses deprecated URL-based routing",
            fix_hint="Run: filigree install --codex",
        )

    # Stdio-mode check (command + args config)
    args = filigree_server.get("args")
    command = filigree_server.get("command")
    if args != [] or not isinstance(command, str) or not command:
        return CheckResult(
            "Codex MCP",
            False,
            "filigree in ~/.codex/config.toml must use runtime project autodiscovery",
            fix_hint="Run: filigree install --codex",
        )
    if _is_absolute_command_path(command) and not Path(command).exists():
        return CheckResult("Codex MCP", False, f"Binary not found at {command}", fix_hint="Run: filigree install --codex")
    if _is_absolute_command_path(command) and _is_venv_binary(command):
        uv_tool_bin = Path.home() / ".local" / "bin" / "filigree-mcp"
        if uv_tool_bin.exists():
            return CheckResult(
                "Codex MCP",
                False,
                f"Codex config points at venv binary ({command}) but uv tool is installed",
                fix_hint="Run: filigree install --codex  (to update to global uv tool path)",
            )
    return CheckResult("Codex MCP", True, "Configured in ~/.codex/config.toml")


# ---------------------------------------------------------------------------
# Main doctor entry point
# ---------------------------------------------------------------------------


def run_doctor(project_root: Path | None = None) -> list[CheckResult]:
    """Run all health checks. Returns list of CheckResult."""
    results: list[CheckResult] = []
    cwd = project_root or Path.cwd()

    # 1. Check .filigree/ exists
    filigree_dir = cwd / FILIGREE_DIR_NAME
    if not filigree_dir.is_dir():
        # Try walking up
        try:
            filigree_dir = find_filigree_root(cwd)
        except ForeignDatabaseError as exc:
            # Walk-up crossed a .git/ boundary — surface the full message so
            # users (and agents) see exactly why we refused to open the
            # ancestor anchor.  ``ForeignDatabaseError`` is also a
            # ``FileNotFoundError`` so the generic handler would otherwise
            # swallow it into a bland "No .filigree/ found" line.
            results.append(
                CheckResult(
                    ".filigree/ directory",
                    False,
                    str(exc),
                    fix_hint=f"Run `filigree init` in {exc.git_boundary} (this project).",
                )
            )
            return results  # Can't proceed without a local anchor
        except FileNotFoundError:
            results.append(
                CheckResult(
                    ".filigree/ directory",
                    False,
                    f"No {FILIGREE_DIR_NAME}/ found in {cwd} or parents",
                    fix_hint="Run: filigree init",
                )
            )
            return results  # Can't proceed without .filigree/
    results.append(CheckResult(".filigree/ directory", True, f"Found at {filigree_dir}"))

    # 1b. Check .filigree.conf anchor (v2.0). Warn if missing — this means the
    # project predates the conf anchor; running any filigree command will
    # auto-backfill on first open, but flagging it lets users see it's pending.
    project_root = filigree_dir.parent
    conf_path = project_root / CONF_FILENAME
    # conf_db_path is the authoritative DB location when the conf declares it;
    # falls back to .filigree/DB_FILENAME for legacy installs or unreadable confs.
    conf_db_path: Path | None = None
    conf_data: dict[str, Any] | None = None
    if conf_path.exists():
        try:
            conf_data = read_conf(conf_path)
            conf_db_path = (conf_path.parent / conf_data["db"]).resolve()
            results.append(CheckResult(".filigree.conf anchor", True, f"Found at {conf_path}"))
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            results.append(
                CheckResult(
                    ".filigree.conf anchor",
                    False,
                    f"Found at {conf_path} but unreadable: {exc}",
                    fix_hint=f"Fix or regenerate {conf_path}",
                )
            )
    else:
        results.append(
            CheckResult(
                ".filigree.conf anchor",
                False,
                f"Missing at {conf_path} — this v2.0 anchor will be auto-written on next use.",
                fix_hint="No action required; run any filigree command to backfill.",
            )
        )

    # 1c. Warn if ~/.filigree.conf exists. A conf at $HOME claims everything
    # under $HOME — every uninitialised subdir falls into this DB unless the
    # subdir has its own .filigree.conf. Almost certainly a mistake.
    home_conf = Path.home() / CONF_FILENAME
    if home_conf.exists() and home_conf.resolve() != conf_path.resolve():
        results.append(
            CheckResult(
                "Home-directory .filigree.conf",
                False,
                f"{home_conf} exists. Any project under your home dir without its own {CONF_FILENAME} will fall into this database.",
                fix_hint=f"Remove {home_conf} (and the sibling {FILIGREE_DIR_NAME}/) "
                f"if it was created by accident, or `filigree init` in each "
                f"subproject so they have their own anchor.",
            )
        )

    # 2. Check config.json
    config_path = filigree_dir / CONFIG_FILENAME
    config_data: dict[str, Any] | None = None
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            if not isinstance(config, dict):
                raise ValueError("config.json must be a JSON object")
            config_data = config
            prefix = config.get("prefix", "?")
            results.append(CheckResult("config.json", True, f"Prefix: {prefix}"))
        except json.JSONDecodeError as e:
            results.append(
                CheckResult(
                    "config.json",
                    False,
                    f"Invalid JSON: {e}",
                    fix_hint="Fix or regenerate .filigree/config.json",
                )
            )
        except ValueError:
            results.append(
                CheckResult(
                    "config.json",
                    False,
                    "Invalid JSON shape: expected an object",
                    fix_hint="Fix or regenerate .filigree/config.json",
                )
            )
        except OSError as exc:
            results.append(
                CheckResult(
                    "config.json",
                    False,
                    f"Found at {config_path} but unreadable: {exc}",
                    fix_hint="Fix or regenerate .filigree/config.json",
                )
            )
    else:
        results.append(
            CheckResult(
                "config.json",
                False,
                "Missing",
                fix_hint="Run: filigree init",
            )
        )

    # 3. Check filigree.db exists and is accessible. Prefer the DB path declared
    # in .filigree.conf (v2.0 — users may relocate the DB); fall back to the
    # legacy .filigree/filigree.db when no conf is present or it's unreadable.
    db_path = conf_db_path if conf_db_path is not None else filigree_dir / DB_FILENAME
    if db_path.exists():
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(str(db_path))
            # Resolve schema version FIRST in its own try block so a v+1
            # mismatch is reported as schema-mismatch even if a subsequent
            # query (e.g. ``SELECT COUNT(*) FROM issues``) fails because of
            # an as-yet-unmigrated table change. Without this ordering,
            # users on a v+1 DB would see "Database may be corrupted.
            # Restore from backup." instead of "upgrade filigree". Routed
            # through ``read_schema_version`` so doctor and FiligreeDB
            # share one source of truth.
            schema_version: int | None = None
            try:
                schema_version = read_schema_version(conn)
            except sqlite3.Error as e:
                results.append(
                    CheckResult(
                        "filigree.db",
                        False,
                        f"Cannot read schema version: {e}",
                        fix_hint="Database may be corrupted. Restore from backup.",
                    )
                )

            if schema_version is not None and schema_version > CURRENT_SCHEMA_VERSION:
                from filigree.install_support.version_marker import format_schema_mismatch_guidance

                results.append(
                    CheckResult(
                        "Schema version",
                        False,
                        f"v{schema_version} (this filigree supports v{CURRENT_SCHEMA_VERSION})",
                        fix_hint=format_schema_mismatch_guidance(CURRENT_SCHEMA_VERSION, schema_version),
                        code="schema_mismatch_forward",
                    )
                )
                # Skip the COUNT(*) probe — querying tables on a v+1 DB
                # may itself fail and would only yield duplicate noise.
            elif schema_version is not None:
                # Schema is at-or-behind installed; safe to probe rows.
                try:
                    count = conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
                    results.append(CheckResult("filigree.db", True, f"{count} issues"))
                except sqlite3.Error as e:
                    results.append(
                        CheckResult(
                            "filigree.db",
                            False,
                            f"Database error: {e}",
                            fix_hint="Database may be corrupted. Restore from backup.",
                        )
                    )

                if schema_version < CURRENT_SCHEMA_VERSION:
                    results.append(
                        CheckResult(
                            "Schema version",
                            False,
                            f"v{schema_version} (current: v{CURRENT_SCHEMA_VERSION})",
                            fix_hint=(
                                "Database schema is outdated. After backing up, run a normal DB command "
                                "with the upgraded binary (for example: filigree stats)."
                            ),
                        )
                    )
                else:
                    results.append(CheckResult("Schema version", True, f"v{schema_version}"))
                    registry_state = _doctor_file_registry_backend_state(
                        conn,
                        registry_settings=conf_data if conf_data is not None else config_data,
                        schema_version=schema_version,
                    )
                    if registry_state is not None:
                        results.append(registry_state)
        except sqlite3.Error as e:
            results.append(
                CheckResult(
                    "filigree.db",
                    False,
                    f"Database error: {e}",
                    fix_hint="Database may be corrupted. Restore from backup.",
                )
            )
        finally:
            if conn is not None:
                conn.close()
    else:
        results.append(
            CheckResult(
                "filigree.db",
                False,
                "Missing",
                fix_hint="Run: filigree init",
            )
        )

    # 4. Check context.md freshness
    summary_path = filigree_dir / SUMMARY_FILENAME
    if summary_path.exists():
        if not summary_path.is_file():
            results.append(
                CheckResult(
                    "context.md",
                    False,
                    f"Found at {summary_path} but not a file",
                    fix_hint="Run any filigree mutation command to refresh generated context.",
                )
            )
        else:
            try:
                mtime = datetime.fromtimestamp(summary_path.stat().st_mtime, tz=UTC)
            except OSError as exc:
                results.append(
                    CheckResult(
                        "context.md",
                        False,
                        f"Found at {summary_path} but unreadable: {exc}",
                        fix_hint="Run any filigree mutation command to refresh generated context.",
                    )
                )
            else:
                age_minutes = (datetime.now(UTC) - mtime).total_seconds() / 60
                if age_minutes > 60:
                    results.append(
                        CheckResult(
                            "context.md",
                            False,
                            f"Stale ({int(age_minutes)} minutes old)",
                            fix_hint="Run any filigree mutation command to refresh generated context.",
                        )
                    )
                else:
                    results.append(CheckResult("context.md", True, f"Fresh ({int(age_minutes)}m old)"))
    else:
        results.append(
            CheckResult(
                "context.md",
                False,
                "Missing",
                fix_hint="Run any filigree mutation command to refresh generated context.",
            )
        )

    # 5. Check .gitignore includes .filigree/
    # Uses the same gitignore-aware parser as ``ensure_gitignore`` so the two
    # paths can't drift on edge cases (comments, ``!``-negations, non-root
    # substrings) — see filigree-bc5d2af1ef for the previous divergence.
    gitignore = (filigree_dir.parent) / ".gitignore"
    if gitignore.exists():
        try:
            content = gitignore.read_text()
        except OSError as exc:
            results.append(
                CheckResult(
                    ".gitignore",
                    False,
                    f"Found at {gitignore} but unreadable: {exc}",
                    fix_hint="Replace it with a readable .gitignore file containing .filigree/",
                )
            )
        else:
            if has_active_filigree_ignore(content):
                results.append(CheckResult(".gitignore", True, ".filigree/ is ignored"))
            else:
                results.append(
                    CheckResult(
                        ".gitignore",
                        False,
                        ".filigree/ not in .gitignore",
                        fix_hint="Run: filigree install --gitignore",
                    )
                )
    else:
        results.append(
            CheckResult(
                ".gitignore",
                False,
                "No .gitignore found",
                fix_hint="Create .gitignore with .filigree/ entry",
            )
        )

    # 6. Check MCP configuration — Claude Code
    mcp_json = (filigree_dir.parent) / ".mcp.json"
    if mcp_json.exists():
        try:
            mcp = json.loads(mcp_json.read_text())
            if not isinstance(mcp, dict):
                raise ValueError("not a JSON object")
            servers = mcp.get("mcpServers", {})
            if not isinstance(servers, dict):
                raise ValueError("mcpServers must be a JSON object")
            if "filigree" not in servers:
                results.append(
                    CheckResult(
                        "Claude Code MCP",
                        False,
                        "filigree not in .mcp.json",
                        fix_hint="Run: filigree install --claude-code",
                    )
                )
            else:
                # Validate the per-server schema before declaring it healthy.
                # Previously a truthy non-dict (e.g. a string or list) silently
                # passed because ``command`` was coerced to "" and both
                # absolute-path branches were skipped (filigree-466bcb6279).
                filigree_mcp_entry = _validate_filigree_mcp_entry(servers["filigree"])
                mcp_command_raw = filigree_mcp_entry.get("command", "")
                mcp_command = mcp_command_raw if isinstance(mcp_command_raw, str) else ""
                if _is_absolute_command_path(mcp_command) and not Path(mcp_command).exists():
                    results.append(
                        CheckResult(
                            "Claude Code MCP",
                            False,
                            f"Binary not found at {mcp_command}",
                            fix_hint="Run: filigree install --claude-code",
                        )
                    )
                elif _is_absolute_command_path(mcp_command) and _is_venv_binary(mcp_command):
                    uv_tool_bin = Path.home() / ".local" / "bin" / "filigree-mcp"
                    if uv_tool_bin.exists():
                        results.append(
                            CheckResult(
                                "Claude Code MCP",
                                False,
                                f"MCP points at venv binary ({mcp_command}) but uv tool is installed",
                                fix_hint="Run: filigree install --claude-code  (to update to global uv tool path)",
                            )
                        )
                    else:
                        results.append(CheckResult("Claude Code MCP", True, "Configured in .mcp.json (venv path)"))
                else:
                    results.append(CheckResult("Claude Code MCP", True, "Configured in .mcp.json"))
        except (json.JSONDecodeError, ValueError, OSError):
            results.append(
                CheckResult(
                    "Claude Code MCP",
                    False,
                    "Invalid .mcp.json",
                    fix_hint="Fix .mcp.json or run: filigree install --claude-code",
                )
            )
    else:
        results.append(
            CheckResult(
                "Claude Code MCP",
                False,
                "No .mcp.json found",
                fix_hint="Run: filigree install --claude-code",
            )
        )

    # 7. Check MCP configuration — Codex
    results.append(_check_codex_mcp(filigree_dir))

    # 8. Check Claude Code hooks
    settings_json = (filigree_dir.parent) / ".claude" / "settings.json"
    if settings_json.exists():
        try:
            s = json.loads(settings_json.read_text())
            if _has_hook_command(s, SESSION_CONTEXT_COMMAND):
                # Structural validation only: if the hook is a module-form
                # invocation (``<abs-path> -m filigree …``), we used to also
                # subprocess-run the interpreter with ``-c "import filigree"``
                # to detect a venv-purged install (bug filigree-36539914b3).
                # That probe was removed (filigree-e6828dcdb1) because the
                # interpreter path is read from project-controlled
                # ``.claude/settings.json`` — a hostile or compromised repo
                # could plant a binary at that path and get arbitrary code
                # executed under anyone running ``filigree doctor``. The
                # original venv-purge case still surfaces as a SessionStart
                # failure on the next session; running ``filigree install
                # --hooks`` repairs it.
                hook_binary = _extract_hook_binary(s, SESSION_CONTEXT_COMMAND)
                if hook_binary and _is_absolute_command_path(hook_binary) and not Path(hook_binary).exists():
                    results.append(
                        CheckResult(
                            "Claude Code hooks",
                            False,
                            f"Binary not found at {hook_binary}",
                            fix_hint="Run: filigree install --hooks",
                        )
                    )
                else:
                    results.append(CheckResult("Claude Code hooks", True, "session-context hook registered"))
            else:
                results.append(
                    CheckResult(
                        "Claude Code hooks",
                        False,
                        "session-context hook not found in .claude/settings.json",
                        fix_hint="Run: filigree install --hooks",
                    )
                )
        except (json.JSONDecodeError, OSError):
            results.append(
                CheckResult(
                    "Claude Code hooks",
                    False,
                    "Invalid .claude/settings.json",
                    fix_hint="Fix .claude/settings.json or run: filigree install --hooks",
                )
            )
    else:
        results.append(
            CheckResult(
                "Claude Code hooks",
                False,
                "No .claude/settings.json found",
                fix_hint="Run: filigree install --hooks",
            )
        )

    # 9. Check Claude Code skills
    skill_md = (filigree_dir.parent) / ".claude" / "skills" / SKILL_NAME / SKILL_MARKER
    if skill_md.exists():
        results.append(CheckResult("Claude Code skills", True, f"{SKILL_NAME} skill installed"))
    else:
        results.append(
            CheckResult(
                "Claude Code skills",
                False,
                f"{SKILL_NAME} skill not found in .claude/skills/",
                fix_hint="Run: filigree install --skills",
            )
        )

    # 9b. Check Codex skills
    codex_skill_md = (filigree_dir.parent) / ".agents" / "skills" / SKILL_NAME / SKILL_MARKER
    if codex_skill_md.exists():
        results.append(CheckResult("Codex skills", True, f"{SKILL_NAME} skill installed"))
    else:
        results.append(
            CheckResult(
                "Codex skills",
                False,
                f"{SKILL_NAME} skill not found in .agents/skills/",
                fix_hint="Run: filigree install --codex-skills",
            )
        )

    # 10. Check CLAUDE.md has instructions
    claude_md = (filigree_dir.parent) / "CLAUDE.md"
    if claude_md.exists():
        try:
            content = claude_md.read_text()
        except OSError as exc:
            results.append(
                CheckResult(
                    "CLAUDE.md",
                    False,
                    f"Found at {claude_md} but unreadable: {exc}",
                    fix_hint="Run: filigree install --claude-md",
                )
            )
        else:
            if FILIGREE_INSTRUCTIONS_MARKER in content:
                results.append(CheckResult("CLAUDE.md", True, "Filigree instructions present"))
            else:
                results.append(
                    CheckResult(
                        "CLAUDE.md",
                        False,
                        "No filigree instructions",
                        fix_hint="Run: filigree install --claude-md",
                    )
                )
    else:
        results.append(
            CheckResult(
                "CLAUDE.md",
                False,
                "File not found",
                fix_hint="Run: filigree install --claude-md",
            )
        )

    # 11. Check AGENTS.md has instructions
    agents_md = (filigree_dir.parent) / "AGENTS.md"
    if agents_md.exists():
        try:
            content = agents_md.read_text()
        except OSError as exc:
            results.append(
                CheckResult(
                    "AGENTS.md",
                    False,
                    f"Found at {agents_md} but unreadable: {exc}",
                    fix_hint="Run: filigree install --agents-md",
                )
            )
        else:
            if FILIGREE_INSTRUCTIONS_MARKER in content:
                results.append(CheckResult("AGENTS.md", True, "Filigree instructions present"))
            else:
                results.append(
                    CheckResult(
                        "AGENTS.md",
                        False,
                        "No filigree instructions",
                        fix_hint="Run: filigree install --agents-md",
                    )
                )
    # AGENTS.md is optional — don't warn if it doesn't exist

    # 12. Mode-specific checks
    from filigree.core import get_mode

    try:
        mode = get_mode(filigree_dir)
    except (AttributeError, ValueError, json.JSONDecodeError, OSError):
        mode = "ethereal"  # Fall back to default if config is unreadable

    if mode == "ethereal":
        results.extend(_doctor_ethereal_checks(filigree_dir))
    elif mode == "server":
        results.extend(_doctor_server_checks(filigree_dir))

    # 13. Check dashboard/API route registration without mutating records.
    results.extend(_doctor_dashboard_contract_checks())

    # 14. Check scanner registration drift
    results.extend(_doctor_bundled_scanner_checks(filigree_dir))

    # 15. Check git working tree status
    try:
        result = subprocess.run(
            ["git", "-C", str(filigree_dir.parent), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            changes = result.stdout.strip()
            if changes:
                line_count = len(changes.splitlines())
                results.append(
                    CheckResult(
                        "Git working tree",
                        False,
                        f"{line_count} uncommitted change(s)",
                        fix_hint="Commit or stash changes",
                    )
                )
            else:
                results.append(CheckResult("Git working tree", True, "Clean"))
    except FileNotFoundError:
        pass  # git not installed — not an error
    except subprocess.TimeoutExpired:
        results.append(
            CheckResult(
                "Git working tree",
                False,
                "git status timed out (5s)",
                fix_hint="Check for .git/index.lock or repository corruption",
            )
        )

    # 16. Check installation method
    results.extend(_doctor_install_method())

    return results


def _find_all_filigree_binaries(which_result: str, uv_tool_bin: Path) -> list[str]:
    """Find filigree installs other than the uv tool.

    Checks common locations: pip user site, system site-packages, other
    entries on PATH that aren't the uv tool binary.
    """
    import site

    uv_resolved = str(uv_tool_bin.resolve()) if uv_tool_bin.exists() else ""
    others: list[str] = []

    # Check if shutil.which found something different from the uv tool
    if which_result and uv_resolved:
        try:
            which_resolved = str(Path(which_result).resolve())
            if which_resolved != uv_resolved:
                others.append(which_result)
        except (OSError, ValueError) as exc:
            logger.debug("Could not resolve path %s: %s", which_result, exc)

    # Check pip user and system site-packages for filigree metadata
    for site_dir in {*site.getsitepackages(), site.getusersitepackages()}:
        site_path = Path(site_dir)
        if not site_path.is_dir():
            continue
        # pip installs leave dist-info directories
        for dist_info in site_path.glob("filigree-*.dist-info"):
            # Make sure this isn't the uv tool's own site-packages
            if uv_resolved and str(dist_info.resolve()).startswith(str(Path(uv_resolved).parent.parent.resolve())):
                continue
            others.append(str(dist_info.parent))
            break

    return others


def _doctor_install_method() -> list[CheckResult]:
    """Check how filigree is installed and recommend uv tool if appropriate."""
    import shutil
    import sys

    results: list[CheckResult] = []

    # Detect current installation type
    current_exe = shutil.which("filigree") or ""
    uv_tools_dir = Path.home() / ".local" / "share" / "uv" / "tools" / "filigree"
    uv_tool_bin = Path.home() / ".local" / "bin" / "filigree"
    has_uv_tool = uv_tools_dir.is_dir() and uv_tool_bin.exists()

    # Check if currently running from a uv tool environment
    running_from_uv_tool = False
    if has_uv_tool:
        try:
            uv_tools_resolved = uv_tools_dir.resolve()
            exe_resolved = Path(sys.executable).resolve()
            running_from_uv_tool = str(exe_resolved).startswith(str(uv_tools_resolved))
        except (OSError, ValueError) as exc:
            logger.debug("Could not resolve executable path: %s", exc)

    # Check if running from a project-local venv (dev checkout or project dep)
    running_from_venv = False
    venv_path = ""
    exe_path = Path(sys.executable)
    for parent in exe_path.parents:
        if (parent / "pyvenv.cfg").exists():
            running_from_venv = True
            venv_path = str(parent)
            break

    # The uv tool venv's python is typically a symlink to the system Python,
    # so Path(sys.executable).resolve() escapes the venv and the startswith
    # check above fails.  Fall back to checking whether the *venv* we found
    # is the uv tool's own venv (resolve both to canonicalise before
    # comparing).
    if has_uv_tool and running_from_venv and not running_from_uv_tool:
        try:
            if Path(venv_path).resolve() == uv_tools_dir.resolve():
                running_from_uv_tool = True
                running_from_venv = False  # it's the uv tool, not an extra venv
        except (OSError, ValueError) as exc:
            logger.debug("Could not resolve venv/uv tool paths: %s", exc)

    # Detect other installs that may shadow the uv tool
    other_installs: list[str] = []
    if has_uv_tool:
        # Check for pip/pipx installs that could conflict
        for candidate in _find_all_filigree_binaries(current_exe, uv_tool_bin):
            other_installs.append(candidate)

    if running_from_uv_tool:
        if other_installs:
            results.append(
                CheckResult(
                    "Installation",
                    False,
                    f"uv tool installed (good) but also found: {', '.join(other_installs)}",
                    fix_hint=(
                        "Duplicate installs can cause version conflicts. Remove the extra copies: "
                        + "; ".join(f"pip uninstall filigree (in {p})" if "site-packages" in p else f"remove {p}" for p in other_installs)
                    ),
                )
            )
        else:
            results.append(CheckResult("Installation", True, "Installed as uv tool (recommended)"))
    elif has_uv_tool and running_from_venv:
        # Both exist — the current session is using the venv copy, but a global tool is also installed
        results.append(
            CheckResult(
                "Installation",
                False,
                f"Running from venv ({venv_path}) but uv tool also installed",
                fix_hint=(
                    "Duplicate install detected. To use the global tool: "
                    "remove filigree from this venv (uv remove filigree / pip uninstall filigree) "
                    "and ensure ~/.local/bin is on PATH"
                ),
            )
        )
    elif running_from_venv and not has_uv_tool:
        results.append(
            CheckResult(
                "Installation",
                False,
                f"Installed in project venv ({venv_path})",
                fix_hint=("Consider installing as a uv tool for global availability: uv tool install filigree"),
            )
        )
    elif has_uv_tool:
        # uv tool exists but we're not running from it (unusual — maybe PATH issue)
        results.append(
            CheckResult(
                "Installation",
                False,
                "uv tool installed but not on PATH",
                fix_hint="Ensure ~/.local/bin is on your PATH",
            )
        )
    else:
        # No uv tool, not in a recognizable venv — system-level pip or something else
        results.append(
            CheckResult(
                "Installation",
                False,
                f"Installed via pip/system ({current_exe or 'unknown location'})",
                fix_hint="Consider installing as a uv tool for isolation: uv tool install filigree",
            )
        )

    return results
