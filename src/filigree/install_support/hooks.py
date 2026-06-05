"""Claude Code hook installation and management.

Handles registration, upgrade, and introspection of filigree hooks
in ``.claude/settings.json`` SessionStart entries.
"""

from __future__ import annotations

import json
import logging
import shlex
import shutil
from pathlib import Path
from typing import Any

from filigree.core import find_filigree_command
from filigree.install_support.safe_paths import (
    UnsafeInstallPathError,
    ensure_project_dir,
    reject_symlink,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_CONTEXT_COMMAND = "filigree session-context"
ENSURE_DASHBOARD_COMMAND = "filigree ensure-dashboard"

# ---------------------------------------------------------------------------
# Hook command matching
# ---------------------------------------------------------------------------


def _hook_cmd_matches(hook_command: str, bare_command: str) -> bool:
    """Check whether *hook_command* is a bare, absolute-path, or module form of *bare_command*.

    Uses ``shlex.split`` so paths containing spaces (common on Windows)
    are handled correctly.

    Accepts only these shapes (everything else is rejected so that incidental
    user commands like ``echo filigree session-context`` don't get rewritten):

    - Exact: ``"filigree session-context"``
    - Path:  ``"/path/to/filigree session-context"``
    - Quoted path: ``"'/path with spaces/filigree' session-context"``
    - Module: ``"<python> -m filigree session-context"`` — interpreter token
      is unconstrained because ``-m filigree`` is itself the discriminator
      (works for ``python``, ``python3``, ``pypy3``, ``uv run python``-style
      wrappers, etc.).
    """
    if hook_command == bare_command:
        return True
    try:
        hook_tokens = shlex.split(hook_command)
        bare_tokens = shlex.split(bare_command)
    except ValueError:
        return False
    if not hook_tokens or not bare_tokens:
        return False
    n = len(bare_tokens)
    bare_bin = bare_tokens[0]  # e.g. "filigree"

    # Bare/path form: single binary token followed by the bare subcommand args.
    if len(hook_tokens) == n:
        if hook_tokens[1:] != bare_tokens[1:]:
            return False
        hook_bin = hook_tokens[0]
        if hook_bin == bare_bin:
            return True
        hook_base = hook_bin.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        return hook_base.lower() in {bare_bin.lower(), f"{bare_bin.lower()}.exe"}

    # Module form: ``<python> -m <bare_bin> <subcommand args>``. The pair
    # ``-m <bare_bin>`` is the sole discriminator — anything else with a
    # ``filigree`` token in the prefix (``echo filigree``, ``time filigree``,
    # ``env FOO=bar filigree``, ``bash filigree``) is rejected.
    if len(hook_tokens) == n + 2:
        if hook_tokens[1] != "-m" or hook_tokens[2] != bare_bin:
            return False
        return hook_tokens[3:] == bare_tokens[1:]

    return False


def _has_hook_command(settings: dict[str, Any], command: str) -> bool:
    """Check whether *command* already appears in SessionStart hooks (any scope)."""
    if not isinstance(settings, dict):
        return False
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return False
    session_start = hooks.get("SessionStart", [])
    if not isinstance(session_start, list):
        return False
    for matcher in session_start:
        if not isinstance(matcher, dict):
            continue
        hook_list = matcher.get("hooks", [])
        if not isinstance(hook_list, list):
            continue
        for hook in hook_list:
            if isinstance(hook, dict) and _hook_cmd_matches(hook.get("command", ""), command):
                return True
    return False


def _has_unscoped_session_start_hook(settings: dict[str, Any], command: str) -> bool:
    """Check whether *command* appears in an unscoped/wildcard SessionStart block.

    The reuse-block logic in :func:`install_claude_code_hooks` only treats
    matcher-less or ``"*"`` blocks as authoritative for cold-start coverage;
    the install-time gate must use the same rule, otherwise a user's scoped
    ``{"matcher":"resume"}`` entry suppresses the unscoped install and the
    hook silently never fires on cold startup (bug filigree-48d6f0d8da).
    """
    if not isinstance(settings, dict):
        return False
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return False
    session_start = hooks.get("SessionStart", [])
    if not isinstance(session_start, list):
        return False
    for matcher in session_start:
        if not isinstance(matcher, dict):
            continue
        if "matcher" in matcher and matcher.get("matcher") not in (None, "*"):
            continue
        hook_list = matcher.get("hooks", [])
        if not isinstance(hook_list, list):
            continue
        for hook in hook_list:
            if isinstance(hook, dict) and _hook_cmd_matches(hook.get("command", ""), command):
                return True
    return False


def _upgrade_hook_commands(settings: dict[str, Any], bare_command: str, new_command: str) -> bool:
    """Replace hook commands matching *bare_command* with *new_command*.

    Walks the settings structure and replaces hook commands that match
    the bare form (either bare or stale absolute path) with the current
    absolute-path command.  Returns ``True`` if anything was changed.
    """
    changed = False
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return False
    session_start = hooks.get("SessionStart", [])
    if not isinstance(session_start, list):
        return False
    for matcher in session_start:
        if not isinstance(matcher, dict):
            continue
        hook_list = matcher.get("hooks", [])
        if not isinstance(hook_list, list):
            continue
        for hook in hook_list:
            if not isinstance(hook, dict):
                continue
            cmd = hook.get("command", "")
            if _hook_cmd_matches(cmd, bare_command) and cmd != new_command:
                hook["command"] = new_command
                changed = True
    return changed


def _extract_hook_binary(settings: dict[str, Any], bare_command: str) -> str | None:
    """Extract the binary path from the first hook matching *bare_command*.

    Uses ``shlex.split`` to correctly handle quoted paths that contain
    spaces.  Returns ``None`` if no matching hook is found.
    """
    tokens = _extract_hook_tokens(settings, bare_command)
    if not tokens:
        return None
    return tokens[0]


def _extract_hook_tokens(settings: dict[str, Any], bare_command: str) -> list[str] | None:
    """Return the tokenised command for the first hook matching *bare_command*.

    Unlike :func:`_extract_hook_binary` this preserves the full token list,
    letting callers distinguish a direct invocation (``["filigree", ...]``)
    from a module-form invocation (``["/path/to/python", "-m", "filigree",
    ...]``). Returns ``None`` if no matching hook is found or the command
    is empty.
    """
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return None
    session_start = hooks.get("SessionStart", [])
    if not isinstance(session_start, list):
        return None
    for matcher in session_start:
        if not isinstance(matcher, dict):
            continue
        hook_list = matcher.get("hooks", [])
        if not isinstance(hook_list, list):
            continue
        for hook in hook_list:
            if not isinstance(hook, dict):
                continue
            cmd = hook.get("command", "")
            if _hook_cmd_matches(cmd, bare_command):
                if not cmd:
                    return None
                try:
                    tokens = shlex.split(cmd)
                except ValueError:
                    tokens = cmd.split()
                return tokens or None
    return None


def _is_module_form_tokens(tokens: list[str]) -> bool:
    """Return True when *tokens* invoke filigree via ``python -m filigree``.

    The ``-m filigree`` pair must be present anywhere before the subcommand
    — covering both ``python -m filigree session-context`` and quoted
    Windows interpreter paths.
    """
    return any(tokens[i] == "-m" and tokens[i + 1] == "filigree" for i in range(len(tokens) - 1))


PRE_TOOL_USE_MATCHER = "mcp__filigree__.*"


def _ensure_pre_tool_use_hook(settings: dict[str, Any], ensure_dashboard_cmd: str) -> bool:
    """Register ensure-dashboard as a PreToolUse hook scoped to filigree MCP tools.

    This restarts the dashboard if idle-shutdown killed it mid-session.
    Only fires when filigree MCP tools are invoked, not on every tool call.

    Idempotent and self-repairing:

    - In a *Filigree-only* block, upgrade stale commands and repair a drifted
      ``matcher`` to ``mcp__filigree__.*`` in place.
    - In a *mixed* block (Filigree's hook sharing a block with user hooks),
      do NOT rewrite the matcher — that would silently demote the user
      hooks from their original tool scope (bug filigree-53fb9d5906).
      Instead, extract Filigree's hook out and ensure a dedicated
      ``mcp__filigree__.*`` block exists.
    - Append a fresh dedicated block only when no Filigree-only block already
      satisfies the scope.

    Returns ``True`` if the settings dict was mutated.
    """
    changed = False
    if "hooks" not in settings or not isinstance(settings.get("hooks"), dict):
        settings["hooks"] = {}
    pre_hooks = settings["hooks"].get("PreToolUse")
    if not isinstance(pre_hooks, list):
        pre_hooks = []
        settings["hooks"]["PreToolUse"] = pre_hooks

    has_correctly_scoped_filigree_only_block = False
    for block in pre_hooks:
        if not isinstance(block, dict):
            continue
        hook_list = block.get("hooks", [])
        if not isinstance(hook_list, list):
            continue

        filigree_hooks = [h for h in hook_list if isinstance(h, dict) and _hook_cmd_matches(h.get("command", ""), ENSURE_DASHBOARD_COMMAND)]
        if not filigree_hooks:
            continue

        # Upgrade stale absolute paths / bare commands in place — even in
        # mixed blocks, until the hook is moved.
        for h in filigree_hooks:
            if h.get("command") != ensure_dashboard_cmd:
                h["command"] = ensure_dashboard_cmd
                changed = True

        non_filigree_count = sum(1 for h in hook_list if h not in filigree_hooks)
        if non_filigree_count > 0:
            # Mixed block — don't rewrite the matcher; lift Filigree hooks out
            # so the user's block keeps its original scope.
            for h in filigree_hooks:
                hook_list.remove(h)
            changed = True
            continue

        # Filigree-only block — repair the matcher in place if it has drifted.
        if block.get("matcher") != PRE_TOOL_USE_MATCHER:
            block["matcher"] = PRE_TOOL_USE_MATCHER
            changed = True
        has_correctly_scoped_filigree_only_block = True

    if not has_correctly_scoped_filigree_only_block:
        pre_hooks.append(
            {
                "matcher": PRE_TOOL_USE_MATCHER,
                "hooks": [{"type": "command", "command": ensure_dashboard_cmd, "timeout": 5000}],
            }
        )
        changed = True

    return changed


# ---------------------------------------------------------------------------
# Hook installation
# ---------------------------------------------------------------------------


def install_claude_code_hooks(project_root: Path) -> tuple[bool, str]:
    """Register ``filigree session-context`` and ``filigree ensure-dashboard``
    as Claude Code SessionStart hooks in ``.claude/settings.json``.

    Uses absolute paths for the filigree binary so hooks work even when
    filigree is installed in a project-local venv that isn't on PATH.

    Idempotent — won't duplicate existing entries.  Re-running upgrades
    bare or stale absolute-path commands to the current binary location.
    """
    try:
        claude_dir = ensure_project_dir(project_root, ".claude")
    except UnsafeInstallPathError as exc:
        return False, str(exc)
    settings_path = claude_dir / "settings.json"
    try:
        reject_symlink(settings_path)
    except UnsafeInstallPathError as exc:
        return False, str(exc)

    settings: dict[str, Any] = {}
    if settings_path.exists():
        try:
            raw = settings_path.read_text()
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("settings.json is not a JSON object")
            settings = parsed
        except (json.JSONDecodeError, ValueError):
            backup = settings_path.with_suffix(".json.bak")
            try:
                reject_symlink(backup)
            except UnsafeInstallPathError as exc:
                return False, str(exc)
            shutil.copy2(settings_path, backup)
            logger.warning("Malformed settings.json; backed up to %s", backup)

    # Resolve the filigree command tokens to build hook command strings.
    # shlex.join properly quotes tokens containing spaces so the resulting
    # shell command is safe on all platforms (e.g. Windows paths with spaces).
    filigree_tokens = find_filigree_command()
    filigree_prefix = shlex.join(filigree_tokens)
    session_context_cmd = f"{filigree_prefix} session-context"
    ensure_dashboard_cmd = f"{filigree_prefix} ensure-dashboard"

    # Upgrade existing bare/stale commands to current absolute paths
    upgraded: list[str] = []
    if _upgrade_hook_commands(settings, SESSION_CONTEXT_COMMAND, session_context_cmd):
        upgraded.append(SESSION_CONTEXT_COMMAND)
    if _upgrade_hook_commands(settings, ENSURE_DASHBOARD_COMMAND, ensure_dashboard_cmd):
        upgraded.append(ENSURE_DASHBOARD_COMMAND)

    # Build list of commands to add (those not already present in an unscoped
    # block — a hook in {"matcher":"resume"} or similar doesn't cover cold
    # startup and so doesn't satisfy this install).
    commands_to_add: list[str] = []
    if not _has_unscoped_session_start_hook(settings, SESSION_CONTEXT_COMMAND):
        commands_to_add.append(session_context_cmd)
    if not _has_unscoped_session_start_hook(settings, ENSURE_DASHBOARD_COMMAND):
        commands_to_add.append(ensure_dashboard_cmd)

    # Ensure PreToolUse hook for dashboard auto-restart on MCP tool calls
    # (handles case where idle-shutdown killed dashboard mid-session)
    pre_tool_use_changed = _ensure_pre_tool_use_hook(settings, ensure_dashboard_cmd)

    if not commands_to_add and not upgraded and not pre_tool_use_changed:
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        return True, "Hooks already registered in .claude/settings.json"

    if not commands_to_add and (upgraded or pre_tool_use_changed):
        # Only upgrades/repairs, no new hooks needed
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        return True, f"Upgraded hook commands in .claude/settings.json to use {filigree_prefix}"

    # Ensure structure exists (replace non-dict/non-list values)
    if "hooks" not in settings or not isinstance(settings.get("hooks"), dict):
        settings["hooks"] = {}
    if "SessionStart" not in settings["hooks"] or not isinstance(settings["hooks"].get("SessionStart"), list):
        settings["hooks"]["SessionStart"] = []

    # Find or create the matcher block for filigree hooks.
    #
    # Reuse is STRICT: only reuse a block whose matcher is empty/missing
    # (fires for every SessionStart event — startup, resume, clear,
    # compact) AND that already contains a known filigree hook command
    # (session-context or ensure-dashboard). A substring match on
    # "filigree" would happily attach to a user-authored block with a
    # narrower matcher like ``resume``, so the newly-installed
    # session-context hook would silently stop firing on cold startup
    # (bug filigree-9fb21f2b4b).
    filigree_hooks: list[dict[str, Any]] = []
    matcher_block = None
    for matcher in settings["hooks"]["SessionStart"]:
        if not isinstance(matcher, dict):
            continue
        # Only blocks that apply to every session source are safe to reuse.
        # An explicit ``matcher`` (even an empty string) signals the user
        # scoped this block intentionally; don't piggyback on that scope.
        if "matcher" in matcher and matcher.get("matcher") not in (None, "*"):
            continue
        hook_list = matcher.get("hooks", [])
        if not isinstance(hook_list, list):
            continue
        has_filigree_hook = any(
            isinstance(hook, dict)
            and (
                _hook_cmd_matches(hook.get("command", ""), SESSION_CONTEXT_COMMAND)
                or _hook_cmd_matches(hook.get("command", ""), ENSURE_DASHBOARD_COMMAND)
            )
            for hook in hook_list
        )
        if has_filigree_hook:
            matcher_block = matcher
            filigree_hooks = hook_list
            break

    if matcher_block is None:
        # Dedicated block with no matcher so filigree hooks fire on every
        # SessionStart source regardless of how neighbouring blocks are
        # scoped.
        matcher_block = {"hooks": []}
        settings["hooks"]["SessionStart"].append(matcher_block)
        filigree_hooks = matcher_block["hooks"]

    for cmd in commands_to_add:
        filigree_hooks.append({"type": "command", "command": cmd, "timeout": 5000})
    matcher_block["hooks"] = filigree_hooks

    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    added = ", ".join(commands_to_add)
    return True, f"Registered hooks in .claude/settings.json: {added}"
