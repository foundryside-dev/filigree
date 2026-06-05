"""Project installation helpers for filigree.

Handles:
- MCP server configuration for Claude Code and Codex
- Workflow instructions injection into CLAUDE.md / AGENTS.md
- Health checks (doctor)

Implementation is split across ``install_support/`` submodules;
this module re-exports all public symbols for backward compatibility.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.resources
import os
import shutil
import stat
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Re-exports from install_support subpackage
# ---------------------------------------------------------------------------
# These maintain backward compatibility for all existing callers:
#   - tests/test_install.py
#   - tests/test_hooks.py
#   - tests/test_peripheral_fixes.py
#   - tests/test_mcp.py
#   - src/filigree/hooks.py
#   - src/filigree/cli_commands/admin.py
from filigree.install_support import (
    FILIGREE_INSTRUCTIONS_MARKER,
    SKILL_MARKER,
    SKILL_NAME,
)
from filigree.install_support.doctor import (
    CheckResult,
    run_doctor,
)
from filigree.install_support.gitignore import (
    FILIGREE_IGNORE_RULES as _FILIGREE_IGNORE_RULES,  # noqa: F401  (back-compat re-export)
)
from filigree.install_support.gitignore import (
    has_active_filigree_ignore as _has_active_filigree_ignore,
)
from filigree.install_support.hooks import (
    ENSURE_DASHBOARD_COMMAND,
    SESSION_CONTEXT_COMMAND,
    _extract_hook_binary,
    _has_hook_command,
    _hook_cmd_matches,
    _upgrade_hook_commands,
    install_claude_code_hooks,
)
from filigree.install_support.integrations import (
    _find_filigree_mcp_command,
    _read_mcp_json,
    install_claude_code_mcp,
    install_codex_mcp,
)
from filigree.install_support.safe_paths import (
    UnsafeInstallPathError,
    ensure_project_dir,
    project_path,
    reject_symlink,
)

__all__ = [
    # Constants
    "ENSURE_DASHBOARD_COMMAND",
    "FILIGREE_INSTRUCTIONS",
    "FILIGREE_INSTRUCTIONS_MARKER",
    "SESSION_CONTEXT_COMMAND",
    "SKILL_MARKER",
    "SKILL_NAME",
    # Doctor
    "CheckResult",
    # Local
    "_build_instructions_block",
    # Hooks
    "_extract_hook_binary",
    # Integrations
    "_find_filigree_mcp_command",
    "_get_skills_source_dir",
    "_has_hook_command",
    "_hook_cmd_matches",
    "_install_skill_to",
    "_instructions_hash",
    "_instructions_text",
    "_instructions_version",
    "_read_mcp_json",
    "_upgrade_hook_commands",
    "ensure_gitignore",
    "inject_instructions",
    "install_claude_code_hooks",
    "install_claude_code_mcp",
    "install_codex_mcp",
    "install_codex_skills",
    "install_skills",
    "run_doctor",
]

# ---------------------------------------------------------------------------
# Workflow instructions (injected into CLAUDE.md / AGENTS.md)
# ---------------------------------------------------------------------------

_END_MARKER = "<!-- /filigree:instructions -->"


def _instructions_text() -> str:
    """Read the instructions template from the shipped data file."""
    ref = importlib.resources.files("filigree.data").joinpath("instructions.md")
    return ref.read_text(encoding="utf-8")


def _instructions_hash() -> str:
    """Return first 8 hex characters of SHA256 of the instructions content."""
    return hashlib.sha256(_instructions_text().encode()).hexdigest()[:8]


def _instructions_version() -> str:
    """Return a sensible filigree version for instructions markers.

    Falls back to the package ``__version__`` (which itself handles
    source-checkout cases) when distribution metadata is unavailable.
    """
    try:
        return importlib.metadata.version("filigree")
    except importlib.metadata.PackageNotFoundError:
        from filigree import __version__

        return __version__ or "0.0.0-dev"


def _build_instructions_block() -> str:
    """Build the full instructions block with versioned markers."""
    text = _instructions_text()
    version = _instructions_version()
    h = _instructions_hash()
    opening = f"<!-- filigree:instructions:v{version}:{h} -->"
    return f"{opening}\n{text}{_END_MARKER}"


FILIGREE_INSTRUCTIONS = _build_instructions_block()


def _atomic_write_text(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via write-to-temp + rename.

    Preserves the destination's permissions when it already exists.
    `tempfile.mkstemp()` creates files with mode 0o600; without an explicit
    chmod, the rename would leak that mode onto the destination, making
    user-visible files (CLAUDE.md, .gitignore, etc.) owner-only.
    """
    existing_mode: int | None
    reject_symlink(path)
    try:
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        existing_mode = None

    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=path.name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        if existing_mode is not None:
            os.chmod(tmp, existing_mode)
        else:
            # New file: respect process umask instead of mkstemp's 0o600.
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Instruction file injection
# ---------------------------------------------------------------------------


def inject_instructions(file_path: Path) -> tuple[bool, str]:
    """Inject filigree workflow instructions into a markdown file.

    If the file doesn't exist, creates it with just the instructions.
    If it exists and already has the marker, replaces the block.
    If it exists without the marker, appends the block.
    """
    try:
        reject_symlink(file_path)
    except UnsafeInstallPathError as exc:
        return False, str(exc)

    if file_path.exists():
        content = file_path.read_text()
        if FILIGREE_INSTRUCTIONS_MARKER in content:
            # Replace existing block
            start = content.index(FILIGREE_INSTRUCTIONS_MARKER)
            end_pos = content.find(_END_MARKER, start)
            if end_pos != -1:
                end = end_pos + len(_END_MARKER)
                content = content[:start] + FILIGREE_INSTRUCTIONS + content[end:]
            else:
                # Malformed — end marker missing. The block is unclosed, so
                # anything from the start marker onward belongs to the broken
                # block and cannot be safely preserved.  Replace from the
                # start marker through EOF.  (The previous behaviour of
                # preserving the tail left orphan content behind that the
                # next run could no longer distinguish from genuine user
                # text, so the corruption persisted indefinitely.)
                content = content[:start] + FILIGREE_INSTRUCTIONS
            _atomic_write_text(file_path, content)
            return True, f"Updated instructions in {file_path}"
        else:
            # Append
            if not content.endswith("\n"):
                content += "\n"
            content += "\n" + FILIGREE_INSTRUCTIONS + "\n"
            _atomic_write_text(file_path, content)
            return True, f"Appended instructions to {file_path}"
    else:
        _atomic_write_text(file_path, FILIGREE_INSTRUCTIONS + "\n")
        return True, f"Created {file_path}"


# ---------------------------------------------------------------------------
# .gitignore
# ---------------------------------------------------------------------------


def ensure_gitignore(project_root: Path) -> tuple[bool, str]:
    """Ensure .filigree/ is in .gitignore."""
    try:
        gitignore = project_path(project_root, ".gitignore")
    except UnsafeInstallPathError as exc:
        return False, str(exc)
    filigree_pattern = ".filigree/"

    if gitignore.exists():
        content = gitignore.read_text()
        if _has_active_filigree_ignore(content):
            return True, ".filigree/ already in .gitignore"
        if not content.endswith("\n"):
            content += "\n"
        content += f"\n# Filigree issue tracker\n{filigree_pattern}\n"
        _atomic_write_text(gitignore, content)
        return True, f"Added {filigree_pattern} to .gitignore"
    else:
        _atomic_write_text(gitignore, f"# Filigree issue tracker\n{filigree_pattern}\n")
        return True, f"Created .gitignore with {filigree_pattern}"


# ---------------------------------------------------------------------------
# Claude Code skills
# ---------------------------------------------------------------------------


def _get_skills_source_dir() -> Path:
    """Return the path to the bundled skills directory inside the package."""
    return Path(__file__).parent / "skills"


def _install_skill_to(project_root: Path, target_subpath: Path) -> tuple[bool, str]:
    """Copy the filigree skill pack into *target_subpath* under *project_root*.

    Idempotent — overwrites existing skill files to keep them up-to-date
    with the installed filigree version. Safe under concurrent invocation:
    each call stages into a unique per-invocation directory, and the final
    swap tolerates a concurrent peer winning the rename race (their staged
    content is identical to ours).
    """
    source_dir = _get_skills_source_dir()
    skill_source = source_dir / SKILL_NAME
    if not skill_source.is_dir():
        return False, f"Skill source not found at {skill_source}"

    try:
        target_parent = ensure_project_dir(project_root, *target_subpath.parts)
    except UnsafeInstallPathError as exc:
        return False, str(exc)
    target_dir = target_parent / SKILL_NAME
    try:
        reject_symlink(target_dir)
    except UnsafeInstallPathError as exc:
        return False, str(exc)

    # Stage into a unique per-call directory. mkdtemp's name is collision-free
    # even when multiple installers race (e.g. concurrent Claude Code sessions
    # firing SessionStart hooks). Remove the empty placeholder so copytree
    # can create the directory itself.
    staging = Path(tempfile.mkdtemp(dir=target_dir.parent, prefix=f"{SKILL_NAME}.installing."))
    staging.rmdir()
    staging_consumed = False
    backup: Path | None = None
    try:
        shutil.copytree(skill_source, staging)

        # Move any existing target aside under a unique name so a concurrent
        # swapper can't collide with our backup. If the target vanishes before
        # we rename it, another swapper already moved it — that's fine.
        if target_dir.exists():
            backup_holder = Path(tempfile.mkdtemp(dir=target_dir.parent, prefix=f"{SKILL_NAME}.old."))
            backup_holder.rmdir()
            try:
                os.rename(target_dir, backup_holder)
                backup = backup_holder
            except FileNotFoundError:
                pass

        try:
            os.rename(staging, target_dir)
            staging_consumed = True
        except OSError:
            # A peer raced ahead and installed their staging into target_dir.
            # Their content matches ours (same source), so accept their result.
            pass
    finally:
        if not staging_consumed and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backup is not None and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)

    return True, f"Installed skill pack to {target_dir}"


def install_skills(project_root: Path) -> tuple[bool, str]:
    """Copy filigree skill pack into ``.claude/skills/`` for the project."""
    return _install_skill_to(project_root, Path(".claude") / "skills")


def install_codex_skills(project_root: Path) -> tuple[bool, str]:
    """Copy filigree skill pack into ``.agents/skills/`` for Codex."""
    return _install_skill_to(project_root, Path(".agents") / "skills")
