"""Composition point for the Filigree issue tracker database.

Assembles DB mixins (db_files, db_issues, db_events, db_workflow, db_meta,
db_planning, db_observations, db_annotations) into the ``FiligreeDB`` class. Also provides
convention-based ``.filigree/`` directory discovery, configuration I/O,
template seeding, and shared file helpers.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid as _uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from filigree.db_annotations import (
    VALID_ANNOTATION_INTENTS,
    VALID_ANNOTATION_RELATIONSHIPS,
    VALID_ANNOTATION_STATUSES,
    VALID_ANNOTATION_TARGET_TYPES,
    AnnotationsMixin,
)
from filigree.db_base import _now_iso
from filigree.db_entity_associations import EntityAssociationsMixin
from filigree.db_events import EventsMixin
from filigree.db_files import (
    VALID_ASSOC_TYPES,
    VALID_FINDING_STATUSES,
    VALID_SEVERITIES,
    FilesMixin,
    _normalize_scan_path,
)
from filigree.db_issues import IssuesMixin
from filigree.db_meta import MetaMixin
from filigree.db_observations import ObservationsMixin
from filigree.db_planning import PlanningMixin
from filigree.db_scans import ScansMixin
from filigree.db_schema import CURRENT_SCHEMA_VERSION, SCHEMA_SQL
from filigree.db_workflow import WorkflowMixin
from filigree.models import _EMPTY_TS, FileRecord, Issue, ScanFinding
from filigree.types.core import (
    AssocType,
    FileRecordDict,
    FindingStatus,
    ISOTimestamp,
    IssueDict,
    PaginatedResult,
    ProjectConfig,
    ScanFindingDict,
    Severity,
)

if TYPE_CHECKING:
    from filigree.templates import TemplateRegistry

logger = logging.getLogger(__name__)

# Re-exported names from db_files, models, and types.core for backward compatibility.
__all__ = [
    "VALID_ANNOTATION_INTENTS",
    "VALID_ANNOTATION_RELATIONSHIPS",
    "VALID_ANNOTATION_STATUSES",
    "VALID_ANNOTATION_TARGET_TYPES",
    "VALID_ASSOC_TYPES",
    "VALID_FINDING_STATUSES",
    "VALID_SEVERITIES",
    "_EMPTY_TS",
    "AssocType",
    "FileRecord",
    "FileRecordDict",
    "FindingStatus",
    "ISOTimestamp",
    "Issue",
    "IssueDict",
    "PaginatedResult",
    "ProjectConfig",
    "ScanFinding",
    "ScanFindingDict",
    "Severity",
    "_normalize_scan_path",
]


# ---------------------------------------------------------------------------
# Convention-based discovery
# ---------------------------------------------------------------------------

FILIGREE_DIR_NAME = ".filigree"
DB_FILENAME = "filigree.db"
CONFIG_FILENAME = "config.json"
CONF_FILENAME = ".filigree.conf"
SUMMARY_FILENAME = "context.md"

# Schema version for .filigree.conf — bump if the file format changes incompatibly.
CONF_VERSION = 1


def read_schema_version(conn: sqlite3.Connection) -> int:
    """Return the on-disk schema version for *conn*.

    Single source of truth for "what schema version is this DB?". Called by
    :meth:`FiligreeDB.get_schema_version` and by ``filigree doctor``'s raw
    ``sqlite3.connect`` path so a future migration that changes how the
    version is stored only has to update this one function — the alternative
    (each surface inlining ``PRAGMA user_version``) silently drifts.
    """
    result: int = conn.execute("PRAGMA user_version").fetchone()[0]
    return result


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProjectNotInitialisedError(FileNotFoundError):
    """Raised when no ``.filigree.conf`` is found anywhere up to the filesystem root.

    Inherits from FileNotFoundError so existing call sites that catch
    FileNotFoundError still work during the v2.0 transition.
    """


class ForeignDatabaseError(ProjectNotInitialisedError):
    """Walk-up discovery crossed a ``.git/`` boundary before finding an anchor.

    The current working directory sits inside a git repository that has no
    ``.filigree.conf`` (or legacy ``.filigree/``) of its own, but an ancestor
    *above* the git root does. Silently opening that ancestor's database
    would dump tickets into a foreign project, so discovery refuses.

    Subclasses :class:`ProjectNotInitialisedError` so generic "not set up"
    handlers still work; catch :class:`ForeignDatabaseError` specifically
    when you want to surface the richer message (e.g. in the MCP server or
    ``filigree doctor``).
    """

    SAFE_MESSAGE = "Filigree is not initialized for this project"

    def __init__(self, *, cwd: Path, found_anchor: Path, git_boundary: Path) -> None:
        self.cwd = cwd
        self.found_anchor = found_anchor
        self.git_boundary = git_boundary
        malformed_git_hint = ""
        git_path = git_boundary / ".git"
        if _classify_git_entry(git_path) == "malformed_file":
            malformed_git_hint = f"\n\nIf `{git_path}` is malformed, fix or remove it before running `filigree init`."
        msg = (
            "Refusing to latch onto another project's filigree database.\n"
            "\n"
            f"  Current directory: {cwd}\n"
            f"  Nearest anchor:    {found_anchor}\n"
            f"  Git boundary at:   {git_boundary}\n"
            "\n"
            "The nearest filigree anchor sits above a .git/ boundary, so it "
            "belongs to a different project. To track work here, install "
            "filigree in this project:\n"
            "\n"
            f"  cd {git_boundary} && filigree init\n"
            "\n"
            "If MCP is configured, ask the user to restart the MCP server "
            "after `filigree init` so it picks up the new project's "
            "database. To operate on the outer project intentionally, `cd` "
            "above the git boundary."
            f"{malformed_git_hint}"
        )
        super().__init__(msg)

    @property
    def safe_message(self) -> str:
        """Generic, path-free wording suitable for structured logs."""
        return self.SAFE_MESSAGE


class WrongProjectError(ValueError):
    """Raised when an issue ID's prefix doesn't match the open DB's prefix.

    Indicates the caller is operating on a ticket that belongs to a different
    project. Common cause: an agent climbed into a parent's database and is
    trying to act on an ID copy-pasted from somewhere else.

    The ``str(exc)`` form embeds the offending prefix and the open DB's
    prefix for CLI / stderr / ``filigree doctor`` diagnostics. Untrusted
    callers (HTTP, MCP) get :attr:`safe_message` instead, which omits
    both prefixes so cross-project IDs cannot be probed by attempting
    foreign reads and pattern-matching the error.
    """

    SAFE_MESSAGE = "Issue ID does not belong to this project"

    @property
    def safe_message(self) -> str:
        """Generic, prefix-free wording suitable for untrusted clients.

        2.1.0 §1.2: HTTP and MCP responses surface this string so a
        successful guess of "is project X open?" can't be made from a
        4xx response body. CLI handlers and ``filigree doctor`` keep
        ``str(exc)`` so operators still see the offending prefix.
        """
        return self.SAFE_MESSAGE


def _resolve_to_main_worktree(start: Path) -> Path:
    """Redirect *start* to the main worktree root when it sits inside a git worktree.

    Git linked worktrees place a ``.git`` *file* (not directory) at the
    worktree root pointing at ``<main_repo>/.git/worktrees/<name>/``. Walk-up
    discovery would otherwise treat that ``.git`` file as a project boundary
    and refuse to find the project's anchor in the main worktree — raising
    :class:`ForeignDatabaseError` for what is, in fact, the same project.

    The redirect is suppressed when a closer anchor (``.filigree.conf`` or
    legacy ``.filigree/``) sits between *start* and the worktree's ``.git``
    pointer — that nested anchor wins, preserving the "child anchor
    overrides parent" contract for sub-projects nested inside a worktree.

    Returns the main worktree root when *start* (or an ancestor up to the
    first ``.git`` entry) is inside a linked worktree AND no nested anchor
    exists in that subtree. Returns *start* unchanged in every other case:
    a closer anchor was found first, plain repos (``.git`` is a directory),
    submodules (``.git`` file points at ``<parent>/.git/modules/<name>/``),
    no ``.git`` found, or a malformed ``.git`` file.
    """
    for parent in [start, *start.parents]:
        # A nested anchor in the worktree subtree wins — don't redirect past it.
        if (parent / CONF_FILENAME).is_file() or (parent / FILIGREE_DIR_NAME).is_dir():
            return start
        git_path = parent / ".git"
        if not git_path.exists():
            continue
        # Plain repo: existing walk-up handles it correctly.
        if git_path.is_dir():
            return start
        # ``.git`` is a file — worktree pointer or submodule pointer.
        gitdir = _read_gitdir_pointer(git_path)
        if gitdir is None:
            return start
        if not gitdir.is_absolute():
            gitdir = (parent / gitdir).resolve()
        # Worktree shape: <main_repo>/.git/worktrees/<name>
        # Submodule shape: <parent_repo>/.git/modules/<name> — leave alone.
        if gitdir.parent.name != "worktrees":
            return start
        main_git_dir = gitdir.parent.parent
        if main_git_dir.name != ".git" or not main_git_dir.is_dir():
            return start
        return main_git_dir.parent
    return start


def _read_gitdir_pointer(git_path: Path) -> Path | None:
    """Return the raw gitdir pointer from a ``.git`` file, if it is parseable."""
    try:
        content = git_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    gitdir_line = next(
        (line for line in content.splitlines() if line.startswith("gitdir:")),
        None,
    )
    if gitdir_line is None:
        return None
    gitdir_raw = gitdir_line.split(":", 1)[1].strip()
    if not gitdir_raw:
        return None
    return Path(gitdir_raw)


def _classify_git_entry(git_path: Path) -> str:
    """Classify a ``.git`` filesystem entry for discovery diagnostics."""
    if git_path.is_dir():
        return "directory"
    if not git_path.exists() or not git_path.is_file():
        return "malformed_file"
    gitdir = _read_gitdir_pointer(git_path)
    if gitdir is None:
        return "malformed_file"
    if not gitdir.is_absolute():
        gitdir = (git_path.parent / gitdir).resolve()
    main_git_dir = gitdir.parent.parent
    if gitdir.parent.name == "worktrees" and main_git_dir.name == ".git" and main_git_dir.is_dir():
        return "worktree_pointer"
    return "gitdir_file"


def find_filigree_conf(start: Path | None = None) -> Path:
    """Walk up from *start* (default cwd) looking for ``.filigree.conf``.

    Strict and read-only: returns the path to an existing conf file or raises.
    Does **not** auto-migrate legacy installs — that would require a write,
    which makes inspection-only commands fail on read-only mounts. For
    discovery that tolerates legacy installs without writing, use
    :func:`find_filigree_anchor`.

    Nested ``.filigree.conf`` files override their parents — first hit wins.

    When *start* sits inside a git linked worktree, discovery is redirected
    to the main worktree root so the worktree's ``.git`` file is not
    mistaken for a project boundary. See :func:`_resolve_to_main_worktree`.

    Raises:
        ProjectNotInitialisedError: if no ``.filigree.conf`` is found in
            *start* or any ancestor up to ``/``. The error message points at
            ``filigree init`` and ``filigree doctor``.
        ForeignDatabaseError: if the walk-up passes a ``.git/`` boundary
            before finding ``.filigree.conf`` — that conf belongs to a
            different project and silently opening it would write to the
            wrong database.
    """
    orig = (start or Path.cwd()).resolve()
    current = _resolve_to_main_worktree(orig)
    git_boundary: Path | None = None
    for parent in [current, *current.parents]:
        conf = parent / CONF_FILENAME
        if conf.is_file():
            if git_boundary is not None:
                raise ForeignDatabaseError(cwd=orig, found_anchor=conf, git_boundary=git_boundary)
            return conf
        if git_boundary is None and (parent / ".git").exists():
            git_boundary = parent
    msg = (
        f"No {CONF_FILENAME} found in {orig} or any parent directory. "
        f"Run `filigree init` here to create one, or `filigree doctor` to diagnose."
    )
    raise ProjectNotInitialisedError(msg)


def find_filigree_anchor(start: Path | None = None) -> tuple[Path, Path | None]:
    """Walk up from *start* for either a v2.0 conf or a legacy ``.filigree/`` dir.

    Returns a ``(project_root, conf_path)`` pair. ``conf_path`` is the path
    to the resolved ``.filigree.conf`` file when one exists, or ``None`` for a
    legacy install (``.filigree/`` present, no conf yet). The walk is
    closer-first: a child anchor wins over an ancestor regardless of type.

    Pure read — never writes. Use this when discovery must work on read-only
    mounts (inspection commands, MCP startup, ``filigree doctor``). To force
    a backfill, run ``filigree init`` (or another explicit write path) on
    a writable copy of the project.

    When *start* sits inside a git linked worktree, discovery is redirected
    to the main worktree root so the worktree's ``.git`` file is not
    mistaken for a project boundary. See :func:`_resolve_to_main_worktree`.

    Raises:
        ProjectNotInitialisedError: if neither anchor is found anywhere up
            to ``/``.
        ForeignDatabaseError: if the walk-up passes a ``.git/`` boundary
            before finding any anchor — the ancestor anchor belongs to a
            different project and silently opening it would write to the
            wrong database.
    """
    orig = (start or Path.cwd()).resolve()
    current = _resolve_to_main_worktree(orig)
    git_boundary: Path | None = None
    for parent in [current, *current.parents]:
        conf = parent / CONF_FILENAME
        if conf.is_file():
            if git_boundary is not None:
                raise ForeignDatabaseError(cwd=orig, found_anchor=conf, git_boundary=git_boundary)
            return parent, conf
        legacy_dir = parent / FILIGREE_DIR_NAME
        if legacy_dir.is_dir():
            if git_boundary is not None:
                raise ForeignDatabaseError(cwd=orig, found_anchor=legacy_dir, git_boundary=git_boundary)
            return parent, None
        if git_boundary is None and (parent / ".git").exists():
            git_boundary = parent
    msg = (
        f"No {CONF_FILENAME} or {FILIGREE_DIR_NAME}/ found in {orig} or any parent directory. "
        f"Run `filigree init` here to create one, or `filigree doctor` to diagnose."
    )
    raise ProjectNotInitialisedError(msg)


def find_filigree_root(start: Path | None = None) -> Path:
    """Return the project's ``.filigree/`` directory (back-compat helper).

    Locates the project via :func:`find_filigree_conf` (the v2.0 anchor) and
    returns the ``.filigree/`` directory next to that conf. The contract is
    "the literal ``.filigree/`` directory" — every caller in this codebase
    concatenates ``SUMMARY_FILENAME``, ``ephemeral.pid``, or ``DB_FILENAME``
    onto the result, or does ``.parent`` to derive the project root, so the
    return value must point at ``conf.parent / .filigree``, regardless of any
    custom ``db`` location declared in the conf.

    The conf's ``db`` field can still relocate the database itself; callers
    that need the actual DB path should use :meth:`FiligreeDB.from_conf`.

    Resolves through :func:`find_filigree_anchor` so legacy installs (which
    have no conf yet) are still discoverable without writing.

    New code should prefer :func:`find_filigree_anchor` plus
    :meth:`FiligreeDB.from_conf` / :meth:`FiligreeDB.from_filigree_dir` over
    this helper.
    """
    project_root, _conf_path = find_filigree_anchor(start)
    return project_root / FILIGREE_DIR_NAME


def read_conf(conf_path: Path) -> dict[str, Any]:
    """Read and validate a ``.filigree.conf`` file.

    Returns the parsed JSON dict. Raises ``ValueError`` if the file is not a
    JSON object, is missing required keys (``prefix``, ``db``), or contains
    malformed values for ``prefix``, ``db``, or ``enabled_packs``.

    Type validation here ensures downstream callers (notably
    :meth:`FiligreeDB.from_conf`, which evaluates ``Path / data["db"]``) get
    a well-formed dict instead of raw ``TypeError`` from the wrong scalar
    type.
    """
    raw: Any = json.loads(conf_path.read_text())
    if not isinstance(raw, dict):
        msg = f"{conf_path}: must be a JSON object, got {type(raw).__name__}"
        raise ValueError(msg)
    missing = [k for k in ("prefix", "db") if k not in raw]
    if missing:
        msg = f"{conf_path}: missing required keys: {', '.join(missing)}"
        raise ValueError(msg)
    for key in ("prefix", "db"):
        value = raw[key]
        if not isinstance(value, str) or not value:
            msg = f"{conf_path}: {key!r} must be a non-empty string, got {type(value).__name__}: {value!r}"
            raise ValueError(msg)
    if "enabled_packs" in raw:
        packs = raw["enabled_packs"]
        if not isinstance(packs, list) or not all(isinstance(p, str) for p in packs):
            msg = f"{conf_path}: 'enabled_packs' must be a list of strings, got {type(packs).__name__}: {packs!r}"
            raise ValueError(msg)
    # Trust boundary: a checked-in .filigree.conf must not be able to redirect
    # the database to an arbitrary filesystem path. Reject absolute paths and
    # any path whose resolved location escapes the conf's directory.
    db_value: str = raw["db"]
    if Path(db_value).is_absolute():
        msg = f"{conf_path}: 'db' must be a project-relative path, got absolute: {db_value!r}"
        raise ValueError(msg)
    project_root = conf_path.parent.resolve()
    db_resolved = (conf_path.parent / db_value).resolve()
    try:
        db_resolved.relative_to(project_root)
    except ValueError as exc:
        msg = f"{conf_path}: 'db' must resolve under the project root {project_root}, got {db_resolved}"
        raise ValueError(msg) from exc
    return raw


def write_conf(conf_path: Path, data: dict[str, Any]) -> None:
    """Write a ``.filigree.conf`` file atomically."""
    write_atomic(conf_path, json.dumps(data, indent=2) + "\n")


def read_config(filigree_dir: Path) -> ProjectConfig:
    """Read .filigree/config.json. Returns defaults if missing or corrupt."""
    defaults = ProjectConfig(prefix="filigree", version=1, enabled_packs=["core", "planning", "release"])
    config_path = filigree_dir / CONFIG_FILENAME
    if not config_path.exists():
        return defaults
    try:
        raw: Any = json.loads(config_path.read_text())
        if not isinstance(raw, dict):
            logger.warning("Config %s is not a JSON object, using defaults", config_path)
            return defaults
        result: ProjectConfig = raw  # type: ignore[assignment]
        # Ensure required keys have defaults (config.json may predate these fields)
        if "prefix" not in result:
            result["prefix"] = defaults["prefix"]
        if "version" not in result:
            result["version"] = defaults["version"]
        return result
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.warning("Failed to read %s, using defaults: %s", config_path, exc)
        return defaults


def write_config(filigree_dir: Path, config: dict[str, Any] | ProjectConfig) -> None:
    """Write .filigree/config.json."""
    config_path = filigree_dir / CONFIG_FILENAME
    write_atomic(config_path, json.dumps(config, indent=2) + "\n")


def _raw_config_prefix(config_path: Path) -> str | None:
    """Return the ``prefix`` key from config.json as it was literally written.

    Unlike :func:`read_config`, this does not backfill defaults. Returns
    ``None`` when the file is missing, unreadable, not a JSON object, or
    lacks a non-empty string ``prefix`` — letting callers distinguish
    "user declared this prefix" from "read_config made one up".
    """
    if not config_path.exists():
        return None
    try:
        raw: Any = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    value = raw.get("prefix")
    if isinstance(value, str) and value:
        return value
    return None


VALID_MODES: frozenset[str] = frozenset({"ethereal", "server"})


def get_mode(filigree_dir: Path) -> str:
    """Return the installation mode for a project. Defaults to 'ethereal'.

    Raises ValueError if the config contains an explicit but invalid mode string.
    """
    config = read_config(filigree_dir)
    mode: Any = config.get("mode", "ethereal")
    if not isinstance(mode, str) or mode not in VALID_MODES:
        raise ValueError(f"Unknown mode {mode!r} in config. Valid modes: {sorted(VALID_MODES)}")
    return mode


# ---------------------------------------------------------------------------
# Shared CLI / file helpers
# ---------------------------------------------------------------------------


def find_filigree_command() -> list[str]:
    """Locate the filigree CLI command as a list of argument tokens.

    Resolution order:
    1. uv tool binary (~/.local/bin/filigree) -- stable global install
    2. shutil.which("filigree") -- absolute path if on PATH
    3. Sibling of running Python interpreter (covers venv case)
    4. sys.executable -m filigree -- module invocation fallback
    """
    # Prefer uv tool install — stable path that survives venv changes
    uv_tool_bin = Path.home() / ".local" / "bin" / "filigree"
    if uv_tool_bin.is_file() and os.access(uv_tool_bin, os.X_OK):
        return [str(uv_tool_bin)]

    which = shutil.which("filigree")
    if which:
        return [which]

    # Check sibling of Python interpreter (common in venvs)
    python_dir = Path(sys.executable).parent
    candidate = python_dir / "filigree"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return [str(candidate)]

    return [sys.executable, "-m", "filigree"]


def write_atomic(path: Path, content: str) -> None:
    """Write content to path atomically via temp file + os.replace().

    Uses a unique per-writer temp file in ``path.parent`` so that concurrent
    writers to the same target cannot collide on a shared staging path.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        try:
            f = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        with f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _seed_builtin_packs(conn: sqlite3.Connection, now: str) -> int:
    """Seed built-in packs and type templates into the database.

    Returns the number of type templates seeded.
    """
    from filigree.templates_data import BUILT_IN_PACKS

    count = 0
    default_enabled = {"core", "planning", "release"}

    for pack_name, pack_data in BUILT_IN_PACKS.items():
        enabled = 1 if pack_name in default_enabled else 0
        conn.execute(
            "INSERT OR IGNORE INTO packs (name, version, definition, is_builtin, enabled) VALUES (?, ?, ?, 1, ?)",
            (pack_name, pack_data.get("version", "1.0"), json.dumps(pack_data), enabled),
        )
        logger.debug("Seeded pack: %s (enabled=%d)", pack_name, enabled)

        for type_name, type_data in pack_data.get("types", {}).items():
            conn.execute(
                "INSERT OR REPLACE INTO type_templates (type, pack, definition, is_builtin, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (type_name, pack_name, json.dumps(type_data), now, now),
            )
            count += 1
            logger.debug("Seeded type template: %s (pack=%s)", type_name, pack_name)

    return count


# ---------------------------------------------------------------------------
# FiligreeDB — the core
# ---------------------------------------------------------------------------


class FiligreeDB(
    FilesMixin,
    ScansMixin,
    IssuesMixin,
    EventsMixin,
    WorkflowMixin,
    MetaMixin,
    PlanningMixin,
    ObservationsMixin,
    AnnotationsMixin,
    EntityAssociationsMixin,
):
    """Direct SQLite operations. No daemon, no sync. Importable by CLI and MCP."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        prefix: str = "filigree",
        enabled_packs: list[str] | None = None,
        template_registry: TemplateRegistry | None = None,
        check_same_thread: bool = True,
        project_root: str | Path | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.prefix = prefix
        # ``project_root`` anchors filesystem paths stored relative to the
        # project (e.g. scanner log files). None means "derive from db_path",
        # which only works for the legacy .filigree/filigree.db layout;
        # v2.0 conf installs may place the DB anywhere and must set this.
        if project_root is not None:
            self.project_root: Path | None = Path(project_root)
        elif self.db_path.parent.name == FILIGREE_DIR_NAME:
            self.project_root = self.db_path.parent.parent
        else:
            self.project_root = None
        if enabled_packs is not None and isinstance(enabled_packs, str):
            msg = f"enabled_packs must be a list of strings, not a bare string: {enabled_packs!r}"
            raise TypeError(msg)
        self._enabled_packs_override = list(enabled_packs) if enabled_packs is not None else None
        self.enabled_packs = self._enabled_packs_override if self._enabled_packs_override is not None else ["core", "planning", "release"]
        self._conn: sqlite3.Connection | None = None
        self._check_same_thread = check_same_thread
        self._template_registry: TemplateRegistry | None = template_registry

    @classmethod
    def from_filigree_dir(cls, filigree_dir: Path, *, check_same_thread: bool = True) -> FiligreeDB:
        """Create a FiligreeDB from an existing ``.filigree/`` directory.

        When ``config.json`` is missing or omits the ``prefix`` key, fall back
        to the project directory's own name rather than the hardcoded
        ``"filigree"`` default. This mirrors what ``filigree init`` writes
        (prefix defaults to ``cwd.name``) and prevents a legacy install from
        silently opening with the wrong identity — every write to its own
        pre-existing issues would otherwise raise ``WrongProjectError``.
        """
        config = read_config(filigree_dir)
        # ``read_config`` backfills a "filigree" prefix into its return value
        # for missing/partial configs, so inspect the raw JSON to detect
        # whether the user ever declared one explicitly.
        configured_prefix = _raw_config_prefix(filigree_dir / CONFIG_FILENAME)
        prefix = configured_prefix if configured_prefix is not None else (filigree_dir.parent.name or "filigree")
        db = cls(
            filigree_dir / DB_FILENAME,
            prefix=prefix,
            enabled_packs=config.get("enabled_packs"),
            check_same_thread=check_same_thread,
            project_root=filigree_dir.resolve().parent,
        )
        try:
            db.initialize()
        except BaseException:
            # ``initialize()`` opens the connection lazily on its first line
            # (``get_schema_version()`` → ``self.conn``). If it raises before
            # returning, the caller never receives ``db`` and so cannot close
            # the connection — close it here to avoid leaking the handle and
            # its WAL/SHM sidecar files.
            db.close()
            raise
        return db

    @classmethod
    def from_conf(cls, conf_path: Path, *, check_same_thread: bool = True) -> FiligreeDB:
        """Create a FiligreeDB from a ``.filigree.conf`` anchor file (v2.0).

        Resolves the DB path relative to the conf file's directory.
        """
        data = read_conf(conf_path)
        db_path = (conf_path.parent / data["db"]).resolve()
        prefix: str = data["prefix"]
        enabled_packs = data.get("enabled_packs")
        enabled_packs_from_project_config = False
        if enabled_packs is None:
            config = read_config(conf_path.parent / FILIGREE_DIR_NAME)
            enabled_packs = config.get("enabled_packs")
            enabled_packs_from_project_config = enabled_packs is not None
        db = cls(
            db_path,
            prefix=prefix,
            enabled_packs=enabled_packs,
            check_same_thread=check_same_thread,
            project_root=conf_path.resolve().parent,
        )
        try:
            db.initialize()
            if enabled_packs_from_project_config:
                db._enabled_packs_override = None
        except BaseException:
            db.close()
            raise
        return db

    @classmethod
    def from_project(cls, project_path: Path | None = None) -> FiligreeDB:
        """Create a FiligreeDB by discovering the project anchor from *project_path* (or cwd).

        Walks up via :func:`find_filigree_anchor` so legacy installs (a bare
        ``.filigree/`` directory with no conf yet) still open without requiring
        write access during discovery. Returns the v2.0 conf-based DB if a
        conf is present, otherwise falls back to ``from_filigree_dir``.
        """
        project_root, conf_path = find_filigree_anchor(project_path)
        if conf_path is not None:
            return cls.from_conf(conf_path)
        return cls.from_filigree_dir(project_root / FILIGREE_DIR_NAME)

    def __enter__(self) -> FiligreeDB:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, *exc: object) -> None:
        if exc_type is not None and self._conn is not None:
            try:
                self._conn.rollback()
            except Exception:
                logger.error("Rollback failed during __exit__", exc_info=True)
            # After rollback, skip the commit in close() — the rolled-back
            # transaction's changes are lost. Skipping the commit avoids
            # accidentally committing any stray implicit transaction.
            try:
                self._close_no_commit()
            except Exception:
                logger.error("Close failed during __exit__", exc_info=True)
        else:
            self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path),
                isolation_level="DEFERRED",
                check_same_thread=self._check_same_thread,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def _check_id_prefix(self, issue_id: str) -> None:
        """Reject IDs whose prefix doesn't match this DB's prefix.

        Catches cross-project ID confusion — e.g. ``update_issue("alpha-xyz")``
        against a DB with ``prefix="beefdata"``. IDs without a recognisable
        ``<prefix>-<infix>`` structure are passed through; the not-found path
        handles them as before.

        Prefixes may contain hyphens (``filigree init`` defaults the prefix to
        ``cwd.name``, which is unconstrained), so recognisable generated IDs
        are parsed from their terminal hex suffix instead of splitting on the
        first hyphen.
        """
        if "-" not in issue_id:
            return
        candidate_prefix = issue_id.rsplit("-", 1)[0]
        suffix = issue_id.rsplit("-", 1)[1]
        suffix_is_id = 6 <= len(suffix) <= 16 and all(c in "0123456789abcdef" for c in suffix.lower())
        if suffix_is_id:
            if candidate_prefix == self.prefix:
                return
        elif issue_id.startswith(self.prefix + "-"):
            return

        msg = (
            f"Issue ID {issue_id!r} belongs to project {candidate_prefix!r}, "
            f"but this database is for project {self.prefix!r}. "
            f"You may be in the wrong project directory, or you copied an ID "
            f"from another project's docs."
        )
        raise WrongProjectError(msg)

    def initialize(self) -> None:
        """Create tables (if new) or migrate (if existing), then seed templates.

        For a fresh database (user_version == 0), creates all tables from
        SCHEMA_SQL and stamps the current version. For an existing database,
        applies any pending migrations to bring it up to CURRENT_SCHEMA_VERSION.
        """
        current_version = self.get_schema_version()

        if current_version == 0:
            # Fresh database — create everything from scratch
            self.conn.executescript(SCHEMA_SQL)
            self.conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
        elif current_version > CURRENT_SCHEMA_VERSION:
            from filigree.types.api import SchemaVersionMismatchError

            raise SchemaVersionMismatchError(
                installed=CURRENT_SCHEMA_VERSION,
                database=current_version,
            )
        elif current_version < CURRENT_SCHEMA_VERSION:
            # Existing database — apply pending migrations
            from filigree.migrations import apply_pending_migrations

            apply_pending_migrations(self.conn, CURRENT_SCHEMA_VERSION)

        self._seed_templates()
        self._seed_future_release()
        self.conn.commit()

    def _seed_future_release(self) -> None:
        """Create the "Future" release singleton if it doesn't exist.

        Only runs when the ``release`` pack is enabled. Uses raw SQL to
        avoid circular validation during init. Idempotent — skips if a
        release with ``version == "Future"`` already exists.
        """
        if "release" not in self.enabled_packs:
            return

        if self.templates.get_type("release") is None:
            logger.warning("Release pack enabled but 'release' type not registered — skipping Future release seed")
            return

        # Guard json_extract with json_valid: a single corrupt fields row would
        # otherwise raise ``OperationalError: malformed JSON`` and abort init.
        # Migrations already tolerate corrupt fields elsewhere; the
        # Future-singleton check must do the same.
        existing = self.conn.execute(
            "SELECT id FROM issues WHERE type = 'release' AND json_valid(fields) AND json_extract(fields, '$.version') = 'Future'"
        ).fetchone()
        if existing is not None:
            return

        initial_state = self.templates.get_initial_state("release")
        issue_id = f"{self.prefix}-{_uuid.uuid4().hex[:10]}"
        now = _now_iso()
        self.conn.execute(
            "INSERT INTO issues (id, title, status, priority, type, assignee, "
            "created_at, updated_at, description, notes, fields) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (issue_id, "Future", initial_state, 4, "release", "", now, now, "", "", '{"version": "Future"}'),
        )
        logger.info("Seeded Future release singleton: %s", issue_id)

    def get_schema_version(self) -> int:
        """Return the current schema version from PRAGMA user_version."""
        return read_schema_version(self.conn)

    def reconnect(self, *, check_same_thread: bool = True) -> None:
        """Close the current connection so the next access reopens it with a new ``check_same_thread`` setting.

        The reconnection is lazy — it happens on the next access to the
        ``self.conn`` property, which re-applies PRAGMAs at that point.

        If the connection has an in-flight transaction, it is rolled back to
        avoid persisting partial state.  Callers should ideally avoid calling
        this with an active transaction, as uncommitted work will be lost.

        Useful in tests where a DB created with the default
        ``check_same_thread=True`` needs to be shared across threads
        (e.g. async FastAPI test clients).
        """
        try:
            if self._conn is not None:
                try:
                    if self._conn.in_transaction:
                        logger.warning("reconnect: rolling back in-flight transaction")
                        self._conn.rollback()
                finally:
                    try:
                        self._conn.close()
                    finally:
                        self._conn = None
        finally:
            self._check_same_thread = check_same_thread

    def close(self) -> None:
        """Close the database connection.

        If an uncommitted transaction is active, it is rolled back with a
        warning — all mixin methods commit their own transactions, so this
        indicates a bug rather than normal operation.  When no transaction
        is active, a final commit is issued (a no-op in practice).
        """
        if self._conn is not None:
            try:
                if self._conn.in_transaction:
                    logger.warning("close: rolling back in-flight transaction")
                    self._conn.rollback()
                else:
                    self._conn.commit()
            finally:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def _close_no_commit(self) -> None:
        """Close the connection without committing (used after rollback)."""
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
