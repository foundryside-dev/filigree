"""MCP server installation for Claude Code and Codex.

Handles writing ``.mcp.json`` (Claude Code) and ``~/.codex/config.toml``
(Codex) entries that point to the ``filigree-mcp`` binary.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import quote

from filigree.core import read_config, resolve_store_dir
from filigree.install_support.gitignore import (
    MCP_JSON_IGNORE_RULES,
    has_active_ignore,
)
from filigree.install_support.safe_paths import (
    UnsafeInstallPathError,
    project_path,
    reject_symlink,
)

logger = logging.getLogger(__name__)

# The server-mode ``.mcp.json`` Authorization header carries the LITERAL
# federation token (see ``_install_mcp_server_mode``), not a ``${ENV}`` reference:
# the env-var indirection forced an export in every client shell — recurring 401
# toil, and the daemon validates against the same minted token. The token is
# loopback deconfliction plumbing, not a security secret — but the literal still
# must not enter git history: it is minted per-machine in ``.weft/filigree/``, so a
# committed copy makes every *other* clone present one machine's token and the
# daemon 401s the rest (the exact failure ``_doctor_federation_token_checks``
# reports). So the artifact is chmod-tightened toward ``0600`` (best-effort: a
# no-op that returns success on filesystems like WSL DrvFs / CIFS, so the install
# message stats and reports the *real* mode rather than assuming 0600) AND
# gitignore-guarded — see ``_guard_mcp_json_gitignore``. (The 0600 token *file*
# itself lives under the already-gitignored ``.weft/``; this guard protects the
# literal copy.)


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
        config = read_config(resolve_store_dir(project_root))
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

    # Embed the LITERAL per-project federation token (not a ${ENV} reference).
    # The URL is project-scoped (``/mcp/?project={key}``), and the server daemon
    # validates a scoped request against THAT project's own token — not the
    # daemon's home-store token (filigree-23574069a1). So embed the token minted
    # in this project's store dir; mint_token_file reads the existing value (the
    # daemon reuses it on serve). Writing the literal means the client works with
    # zero export — the token is loopback deconfliction plumbing, not a secret.
    from filigree.federation_token import mint_token_file

    token = mint_token_file(resolve_store_dir(project_root))
    mcp_config["mcpServers"]["filigree"] = {
        "type": "streamable-http",
        "url": _codex_server_mode_url(project_root, port),
        "headers": {"Authorization": f"Bearer {token}"},
    }

    mcp_json_path.write_text(json.dumps(mcp_config, indent=2) + "\n")
    # The literal token makes this artifact machine-private: tighten to 0600
    # (matching the source token file) and gitignore-guard it so it never lands
    # in git history. Both are best-effort — the install still succeeds if the
    # filesystem/git refuses.
    try:
        mcp_json_path.chmod(0o600)
    except OSError as exc:
        logger.warning("Could not chmod %s to 0600: %s", mcp_json_path, exc)
    # Report the ACTUAL resulting mode, never an assumed 0600. chmod is
    # best-effort and on filesystems like WSL DrvFs / CIFS it is a no-op that
    # *returns success* (no OSError) while the file stays at the umask default —
    # so conditioning on the call not raising would still misreport. Stat the
    # file and state the real mode: claiming "mode 0600" when it is not is the
    # false-posture class d2597d0 fixed, and must not creep back into the
    # success message.
    try:
        actual_mode = mcp_json_path.stat().st_mode & 0o777
        mode_note = "mode 0600" if actual_mode == 0o600 else f"mode {actual_mode:04o} (could not tighten to 0600 on this filesystem)"
    except OSError:
        mode_note = "mode unknown"
    note = _guard_mcp_json_gitignore(project_root)
    msg = f"Wrote {mcp_json_path} (streamable-http, port {port}; literal federation token embedded, {mode_note})"
    if note:
        msg += f"; {note}"
    return True, msg


def _git_tracks(project_root: Path, relpath: str) -> bool:
    """Return True if *relpath* is already tracked by git under *project_root*.

    Best-effort: no git, no repo, or any error → False (treated as not tracked).
    An inconclusive probe (raised exception, or a non-{0,1} return code such as a
    missing repo or index-lock contention) is logged at debug — it is otherwise
    indistinguishable from a clean not-tracked answer (rc 1), which silences the
    "already-tracked → git rm --cached" token warning the caller derives from it.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "ls-files", "--error-unmatch", relpath],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        # Probe failed (no git binary, timeout, odd invocation). Degrade to
        # "not tracked" so the install proceeds — but log it, because this is
        # indistinguishable to the caller from a clean "not tracked" result, and
        # a silent error here makes the "already-tracked → run git rm --cached"
        # warning vanish on a slow/odd git, leaving a committed token unflagged.
        logger.debug("git ls-files probe for %r failed; treating as untracked: %s", relpath, exc)
        return False
    if result.returncode == 0:
        return True
    # rc 1 is the documented "cleanly not tracked" answer (silent). Any other
    # code (e.g. 128: not a repo, or index-lock contention) means git ran but
    # could not give a definitive answer — same blind spot as the exception
    # path, so log it rather than let it pass as a clean negative.
    if result.returncode != 1:
        logger.debug(
            "git ls-files probe for %r returned rc=%s; treating as untracked: %s",
            relpath,
            result.returncode,
            result.stderr.strip(),
        )
    return False


def _guard_mcp_json_gitignore(project_root: Path) -> str | None:
    """Ensure the project-root ``.gitignore`` ignores ``.mcp.json`` (server mode).

    The server-mode ``.mcp.json`` carries the LITERAL per-machine federation
    token; committing it makes every other clone present this machine's token
    and the daemon 401s the rest. Appending the rule keeps the literal out of
    git history. Returns a human-readable note (folded into the install message),
    or ``None`` when nothing needed saying.

    A ``.gitignore`` rule is a no-op on an already-*tracked* file, so if
    ``.mcp.json`` is already committed the literal still lands on the next commit
    — that case is surfaced as an explicit warning telling the operator to
    ``git rm --cached`` it. Best-effort: a write/path failure degrades to a note
    rather than failing the install.
    """
    notes: list[str] = []
    try:
        gitignore = project_path(project_root, ".gitignore")
    except UnsafeInstallPathError as exc:
        logger.warning("Could not resolve .gitignore for MCP guard: %s", exc)
        gitignore = None
    if gitignore is not None:
        content = gitignore.read_text() if gitignore.exists() else ""
        if has_active_ignore(content, MCP_JSON_IGNORE_RULES):
            notes.append(".mcp.json already gitignored")
        else:
            block = "\n# Filigree server-mode MCP config embeds a literal federation token\n.mcp.json\n"
            if content and not content.endswith("\n"):
                content += "\n"
            try:
                gitignore.write_text((content + block).lstrip("\n"))
                notes.append("added .mcp.json to .gitignore")
            except OSError as exc:
                logger.warning("Could not update .gitignore for MCP guard: %s", exc)
    if _git_tracks(project_root, ".mcp.json"):
        notes.append(
            "WARNING: .mcp.json is already git-tracked — the literal token will still be committed; run `git rm --cached .mcp.json`"
        )
    return "; ".join(notes) if notes else None


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
