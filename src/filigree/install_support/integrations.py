"""MCP server installation for Claude Code and Codex.

Handles writing ``.mcp.json`` (Claude Code) and ``~/.codex/config.toml``
(Codex) entries that point to the ``filigree-mcp`` binary.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import quote

from filigree.core import FILIGREE_DIR_NAME, read_config
from filigree.install_support.safe_paths import (
    UnsafeInstallPathError,
    project_path,
    reject_symlink,
)

logger = logging.getLogger(__name__)

# Canonical inbound federation bearer env var (source of truth:
# dashboard.FEDERATION_TOKEN_ENV_VARS). Duplicated here as plain strings so the
# installer needn't import the heavy dashboard/FastAPI module. The FILIGREE_*
# names are deprecated aliases, read as a soft fallback; removal post-1.0.
WEFT_FEDERATION_ENV_VAR = "WEFT_FEDERATION_TOKEN"
_DEPRECATED_FEDERATION_ENV_VARS = (
    "FILIGREE_FEDERATION_API_TOKEN",
    "FILIGREE_API_TOKEN",
)
_FEDERATION_ENV_VARS = (WEFT_FEDERATION_ENV_VAR, *_DEPRECATED_FEDERATION_ENV_VARS)


def _resolve_federation_token() -> tuple[str | None, str | None]:
    """Return (value, env_name) of the first set federation token, else (None, None)."""
    for name in _FEDERATION_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value, name
    return None, None


def _ensure_minted_federation_token(project_root: Path) -> str:
    """Return a persisted federation token, minting + storing one if absent.

    Idempotent: re-running install reuses the recorded token rather than churning
    a new value. Stored under the (gitignored) ``.filigree/`` dir at 0600. This is
    deconfliction plumbing, not a security secret — file permissions are the only
    boundary (README security note); do not add hardening.
    """
    token_file = project_root / FILIGREE_DIR_NAME / "federation_token"
    try:
        existing = token_file.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    try:
        token_file.write_text(token + "\n")
        token_file.chmod(0o600)
    except OSError as exc:
        logger.warning("Could not persist federation token to %s: %s", token_file, exc)
    return token


def _negotiate_federation_token_message(project_root: Path) -> str:
    """Ensure a usable ``WEFT_FEDERATION_TOKEN`` and return operator guidance.

    The committed header references ``${WEFT_FEDERATION_TOKEN}``; Claude Code
    expands it from its own process env, which filigree cannot write at install
    time. So we *negotiate*: reuse an existing canonical token, migrate a
    deprecated alias's value, or mint a fresh one — and tell the operator the one
    line they must export (which also reaches the daemon, since the SessionStart
    hook starts it from the same shell).
    """
    value, env_name = _resolve_federation_token()
    if env_name == WEFT_FEDERATION_ENV_VAR:
        return ""  # canonical token already exported — header resolves, daemon agrees
    if value is not None:
        # A deprecated alias is set; migrate its value onto the canonical name.
        return (
            f"\n  NOTE: a federation token is set under the deprecated ${env_name}, but the "
            f"config references ${WEFT_FEDERATION_ENV_VAR}. To connect, run:\n"
            f'    export {WEFT_FEDERATION_ENV_VAR}="${env_name}"\n'
            "  (add it to your shell profile and restart the daemon)."
        )
    token = _ensure_minted_federation_token(project_root)
    return (
        f"\n  No federation token was set — minted one. The /mcp transport will 401 until "
        "you export it (filigree can't set the agent's environment). Run:\n"
        f"    export {WEFT_FEDERATION_ENV_VAR}={token}\n"
        f"  (add it to your shell profile and restart the daemon; value recorded in "
        f"{FILIGREE_DIR_NAME}/federation_token)."
    )


# ---------------------------------------------------------------------------
# Command discovery
# ---------------------------------------------------------------------------


def _find_filigree_mcp_command() -> str:
    """Find the filigree-mcp executable path.

    Resolution order:
    1. uv tool binary (``~/.local/bin/filigree-mcp``) — stable global install
    2. ``shutil.which("filigree-mcp")`` — absolute path if on PATH
    3. Sibling of the running Python interpreter (covers venv case),
       probing ``filigree-mcp`` and ``filigree-mcp.exe``
    4. Sibling of the filigree binary if on PATH, probing the same names
    5. Bare ``"filigree-mcp"`` fallback
    """
    # Prefer uv tool install — it's the stable global path that survives
    # venv changes and project switches. Probe both POSIX and Windows
    # executable names so a Windows uv-tool layout isn't skipped in favour
    # of the bare-``filigree-mcp`` fallback.
    uv_tool_dir = Path.home() / ".local" / "bin"
    for name in ("filigree-mcp", "filigree-mcp.exe"):
        uv_tool_bin = uv_tool_dir / name
        if uv_tool_bin.is_file():
            return str(uv_tool_bin)
    which = shutil.which("filigree-mcp")
    if which:
        return which
    # Check next to the running Python (works in venv even when not on PATH)
    for name in ("filigree-mcp", "filigree-mcp.exe"):
        candidate = Path(sys.executable).parent / name
        if candidate.is_file():
            return str(candidate)
    # Fall back to looking in the same dir as filigree
    filigree_path = shutil.which("filigree")
    if filigree_path:
        filigree_dir = Path(filigree_path).parent
        for name in ("filigree-mcp", "filigree-mcp.exe"):
            candidate = filigree_dir / name
            if candidate.is_file():
                return str(candidate)
    return "filigree-mcp"


def _codex_config_path() -> Path:
    """Return the Codex MCP config path currently honored by Codex CLI."""
    return Path.home() / ".codex" / "config.toml"


def _toml_quote(value: str) -> str:
    """Escape a string for inclusion in a TOML double-quoted string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _codex_server_mode_url(project_root: Path, port: int) -> str:
    """Build the streamable-HTTP URL for a project-keyed daemon route."""
    project_key = "filigree"
    try:
        config = read_config(project_root / FILIGREE_DIR_NAME)
        prefix = config.get("prefix")
        if isinstance(prefix, str) and prefix.strip():
            project_key = prefix.strip()
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Unable to read project prefix for server-mode MCP install: %s", exc)
    encoded_key = quote(project_key, safe="")
    return f"http://localhost:{port}/mcp/?project={encoded_key}"


def _build_codex_server_config() -> dict[str, Any]:
    """Return the Codex MCP server config.

    Codex config is global, so pinning a specific project path or daemon URL
    causes cross-project writes when users switch folders. Always launch the
    stdio server without ``--project`` and let ``filigree-mcp`` discover the
    active project from Codex's working directory at runtime.
    """
    return {
        "command": _find_filigree_mcp_command(),
        "args": [],
    }


def _codex_server_block(server_config: dict[str, Any]) -> str:
    """Serialize a Codex MCP server table for config.toml."""
    lines: list[str] = ["[mcp_servers.filigree]"]
    if "url" in server_config:
        lines.append(f'url = "{_toml_quote(str(server_config["url"]))}"')
    else:
        lines.append(f'command = "{_toml_quote(str(server_config["command"]))}"')
        args = server_config.get("args", [])
        rendered_args = ", ".join(f'"{_toml_quote(str(arg))}"' for arg in args)
        lines.append(f"args = [{rendered_args}]")
    return "\n".join(lines) + "\n"


_TOML_HEADER_RE = re.compile(r"(?m)^\[([^\r\n\]]+)\][ \t]*(?:#[^\r\n]*)?(?:\r\n|\n|\r)")


def _parse_toml_header_path(inner: str) -> tuple[str, ...] | None:
    """Return the dotted-key path of a TOML table header's inner contents.

    Accepts any header form ``tomllib`` accepts — bare keys, quoted keys, and
    whitespace around dots — and normalizes them to a tuple of unquoted parts.
    Returns ``None`` when the contents are not a valid table key path.
    """
    try:
        parsed = tomllib.loads(f"[{inner}]\n")
    except tomllib.TOMLDecodeError:
        return None
    path: list[str] = []
    cur: Any = parsed
    while True:
        if not isinstance(cur, dict):
            return None
        if not cur:
            return tuple(path) if path else None
        if len(cur) != 1:
            return None
        key, value = next(iter(cur.items()))
        path.append(key)
        cur = value


def _upsert_toml_table(content: str, table_name: str, table_block: str) -> str:
    """Replace or append a top-level TOML table without disturbing other content.

    Locates the existing block by parsing each ``^\\[...\\]`` header through
    ``tomllib`` and matching the dotted key path semantically — so equivalent
    spellings (``[mcp_servers."filigree"]``, ``[mcp_servers . filigree]``)
    are replaced in place rather than duplicated. Without this normalization,
    ``tomllib`` rejects the resulting file under its duplicate-table rule.

    Assumes simple TOML structure (no multiline strings containing bare ``[``
    at line start). Suitable for machine-generated configs like Codex MCP.
    """
    newline_match = re.search(r"\r\n|\n|\r", content)
    newline = newline_match.group(0) if newline_match else "\n"
    rendered_block = newline.join(table_block.splitlines())
    if table_block.endswith(("\r\n", "\n", "\r")):
        rendered_block += newline

    target_path = tuple(table_name.split("."))
    match_span: tuple[int, int] | None = None
    for header in _TOML_HEADER_RE.finditer(content):
        if _parse_toml_header_path(header.group(1)) != target_path:
            continue
        next_header = _TOML_HEADER_RE.search(content, header.end())
        end = next_header.start() if next_header else len(content)
        match_span = (header.start(), end)
        break

    if match_span is not None:
        start, end = match_span
        updated = content[:start] + rendered_block + content[end:]
    else:
        updated = content
        if updated and not updated.endswith(("\r\n", "\n", "\r")):
            updated += newline
        updated += newline
        updated += rendered_block
    if not updated.endswith(("\r\n", "\n", "\r")):
        updated += newline
    return updated


# ---------------------------------------------------------------------------
# MCP JSON helpers
# ---------------------------------------------------------------------------


def _read_mcp_json(mcp_json_path: Path) -> dict[str, Any]:
    """Read existing .mcp.json or return a default structure."""
    reject_symlink(mcp_json_path)
    if mcp_json_path.exists():
        try:
            raw = json.loads(mcp_json_path.read_text())
            if not isinstance(raw, dict):
                raise ValueError("not a JSON object")
            mcp_config = raw
        except (json.JSONDecodeError, ValueError):
            # Back up the corrupt/non-object file and start fresh
            backup_path = mcp_json_path.parent / (mcp_json_path.name + ".bak")
            reject_symlink(backup_path)
            shutil.copy2(mcp_json_path, backup_path)
            logger.warning(
                "Malformed .mcp.json detected; backed up to %s and creating fresh config",
                backup_path,
            )
            mcp_config = {}
    else:
        mcp_config = {}

    if "mcpServers" not in mcp_config or not isinstance(mcp_config["mcpServers"], dict):
        mcp_config["mcpServers"] = {}

    return mcp_config


# ---------------------------------------------------------------------------
# Claude Code MCP installation
# ---------------------------------------------------------------------------


def _install_mcp_ethereal_mode(project_root: Path) -> tuple[bool, str]:
    """Install Claude Code stdio MCP with runtime project autodiscovery."""
    filigree_mcp = _find_filigree_mcp_command()

    # Try using `claude mcp add` first
    claude_bin = shutil.which("claude")
    if claude_bin:
        try:
            result = subprocess.run(
                [
                    claude_bin,
                    "mcp",
                    "add",
                    "--transport",
                    "stdio",
                    "--scope",
                    "project",
                    "filigree",
                    "--",
                    filigree_mcp,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return True, "Installed via `claude mcp add` (runtime autodiscovery)"
            logger.warning(
                "`claude mcp add` failed (exit %d): %s",
                result.returncode,
                (result.stderr or "").strip(),
            )
        except subprocess.TimeoutExpired:
            logger.warning("`claude mcp add` timed out after 10s")
        except FileNotFoundError:
            logger.warning("claude binary disappeared between which() and run()")

    # Fall back to writing .mcp.json directly
    try:
        mcp_json_path = project_path(project_root, ".mcp.json")
        mcp_config = _read_mcp_json(mcp_json_path)
    except UnsafeInstallPathError as exc:
        return False, str(exc)

    mcp_config["mcpServers"]["filigree"] = {
        "type": "stdio",
        "command": filigree_mcp,
        "args": [],
    }

    mcp_json_path.write_text(json.dumps(mcp_config, indent=2) + "\n")
    return True, f"Wrote {mcp_json_path}"


def _install_mcp_server_mode(project_root: Path, port: int) -> tuple[bool, str]:
    """Write streamable-http MCP config pointing to the daemon."""
    try:
        mcp_json_path = project_path(project_root, ".mcp.json")
        mcp_config = _read_mcp_json(mcp_json_path)
    except UnsafeInstallPathError as exc:
        return False, str(exc)

    # The committed .mcp.json references the canonical env var rather than a
    # literal token — a secret can never get committed by accident (filigree-3ee7250b54).
    # Claude Code expands ${WEFT_FEDERATION_TOKEN} from its own process env at load time.
    mcp_config["mcpServers"]["filigree"] = {
        "type": "streamable-http",
        "url": _codex_server_mode_url(project_root, port),
        "headers": {"Authorization": f"Bearer ${{{WEFT_FEDERATION_ENV_VAR}}}"},
    }

    mcp_json_path.write_text(json.dumps(mcp_config, indent=2) + "\n")
    msg = f"Wrote {mcp_json_path} (streamable-http, port {port})"
    # Server-mode points Claude/Codex at the dashboard daemon's /mcp transport,
    # which is only mounted when a federation bearer token is configured, and the
    # client header above only resolves if WEFT_FEDERATION_TOKEN is exported into
    # the agent's environment. "Negotiate" a key as needed: reuse an existing
    # token, migrate a deprecated alias, or mint a fresh one — then instruct.
    msg += _negotiate_federation_token_message(project_root)
    return True, msg


def install_claude_code_mcp(
    project_root: Path,
    *,
    mode: str = "ethereal",
    server_port: int = 8377,
) -> tuple[bool, str]:
    """Install filigree MCP into Claude Code's config.

    In ethereal mode: stdio transport (per-session process).
    In server mode: streamable-http transport pointing to daemon.
    """
    if mode == "server":
        return _install_mcp_server_mode(project_root, server_port)
    return _install_mcp_ethereal_mode(project_root)


# ---------------------------------------------------------------------------
# Codex MCP installation
# ---------------------------------------------------------------------------


def install_codex_mcp(
    project_root: Path,
    *,
    mode: str = "ethereal",
    server_port: int = 8377,
) -> tuple[bool, str]:
    """Install filigree-mcp into Codex's MCP config.

    Codex currently reads MCP config from ``~/.codex/config.toml``.
    We update the shared ``mcp_servers.filigree`` entry so it targets
    the current project.
    """
    config_path = _codex_config_path()
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        reject_symlink(config_path.parent)
        reject_symlink(config_path)
    except UnsafeInstallPathError as exc:
        return False, str(exc)
    desired = _build_codex_server_config()

    # Read existing config if present
    existing = ""
    if config_path.exists():
        with config_path.open(newline="") as handle:
            existing = handle.read()

    # Check if already configured using proper TOML parsing
    if existing.strip():
        try:
            parsed = tomllib.loads(existing)
            mcp_servers = parsed.get("mcp_servers", {})
            filigree_server = mcp_servers.get("filigree") if isinstance(mcp_servers, dict) else None
            if isinstance(filigree_server, dict) and filigree_server == desired:
                return True, "Already configured in ~/.codex/config.toml"
        except tomllib.TOMLDecodeError:
            return False, f"Existing {config_path} contains malformed TOML; fix or remove it before configuring"

    updated = _upsert_toml_table(existing, "mcp_servers.filigree", _codex_server_block(desired))
    with config_path.open("w", newline="") as handle:
        handle.write(updated)

    return True, f"Wrote {config_path}"
