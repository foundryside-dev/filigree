"""Composition point for the Filigree issue tracker database.

Assembles DB mixins (db_files, db_issues, db_events, db_workflow, db_meta,
db_planning, db_observations, db_annotations) into the ``FiligreeDB`` class. Also provides
convention-based ``.filigree/`` directory discovery, configuration I/O,
template seeding, and shared file helpers.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import tomllib
import uuid as _uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast, get_args

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
from filigree.registry import (
    DEFAULT_LOOMWEAVE_TOKEN_ENV,
    BatchQuery,
    BatchResolution,
    LocalRegistry,
    LoomweaveCapabilities,
    LoomweaveRegistry,
    RegistryProtocol,
    RegistryUnavailableError,
    RegistryVersionMismatchError,
    ResolvedFile,
    normalize_loomweave_base_url,
    probe_loomweave_capabilities,
    resolve_files_batch_via_loop,
    validate_loomweave_capabilities,
)
from filigree.types.core import (
    AssocType,
    FileRecordDict,
    FindingStatus,
    ISOTimestamp,
    IssueDict,
    LoomweaveConfig,
    PaginatedResult,
    ProjectConfig,
    RegistryBackend,
    ScanFindingDict,
    Severity,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

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
    "LocalRegistry",
    "LoomweaveRegistry",
    "PaginatedResult",
    "ProjectConfig",
    "RegistryProtocol",
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

# WEFT federation store convention (filigree-37e3f26145). The machine-owned
# store moves from ``.filigree/`` to ``.weft/filigree/``. ``.weft/`` is the
# shared federation dir (co-owned by sibling members); filigree owns exactly
# its ``.weft/filigree/`` subtree and is its sole writer. ``.filigree/``
# remains the frozen legacy layout (kept readable for back-compat).
WEFT_DIR_NAME = ".weft"
WEFT_MEMBER_SUBDIR = "filigree"
WEFT_TOML_FILENAME = "weft.toml"
# The store dir for a relative ``store_dir`` override defaults here.
LEGACY_MOVED_BREADCRUMB = "MOVED"

# Schema version for .filigree.conf — bump if the file format changes incompatibly.
CONF_VERSION = 1

# 32-bit application_id stamped on every filigree SQLite DB.
# 'FILG' big-endian ASCII. Chosen 2026-05-23; do not change without
# a migration that re-stamps existing DBs.
FILIGREE_APPLICATION_ID = 0x46494C47


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

    Public surfaces intentionally split status codes by operation class:
    server-mode read probes map this to NOT_FOUND/404 for anti-enumeration,
    while write endpoints map it to VALIDATION/400 because the mutation
    request is malformed for the current project. Both untrusted paths use
    ``safe_message`` rather than prefix-bearing diagnostic text.
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


class ForeignSqliteFileError(ProjectNotInitialisedError):
    """The file at the expected filigree DB path is a SQLite database, but
    its ``application_id`` is non-zero and does not match
    :data:`FILIGREE_APPLICATION_ID`.

    Distinguished from :class:`ForeignDatabaseError`, which is raised when
    walk-up discovery crosses a ``.git/`` boundary into another project's
    *filigree* DB. This error is raised when the file at the discovered
    path is *not a filigree DB at all* — e.g. an operator has put a SQLite
    DB from another tool at ``.filigree/filigree.db``. Silently overwriting
    that file with ``SCHEMA_SQL`` would destroy data, so discovery refuses.

    Inherits from :class:`ProjectNotInitialisedError` so generic
    "not set up" handlers still work; catch this class specifically when
    the operator needs to be told to *move* the foreign file out of the
    way before ``filigree init`` will succeed.
    """

    SAFE_MESSAGE = "A non-filigree SQLite file occupies the filigree database path"

    def __init__(self, *, path: Path, observed_application_id: int) -> None:
        self.path = path
        self.observed_application_id = observed_application_id
        msg = (
            f"Refusing to open {path}: it is a SQLite database with "
            f"application_id=0x{observed_application_id:08x}, which is not "
            f"filigree's (0x{FILIGREE_APPLICATION_ID:08x}). Move or rename "
            f"this file before running `filigree init` here."
        )
        super().__init__(msg)

    @property
    def safe_message(self) -> str:
        return self.SAFE_MESSAGE


def _resolve_to_main_worktree(start: Path) -> Path:
    """Redirect *start* to the main worktree root when it sits inside a git worktree.

    Git linked worktrees place a ``.git`` *file* (not directory) at the
    worktree root pointing at ``<main_repo>/.git/worktrees/<name>/``. Walk-up
    discovery would otherwise treat that ``.git`` file as a project boundary
    and refuse to find the project's anchor in the main worktree — raising
    :class:`ForeignDatabaseError` for what is, in fact, the same project.

    The redirect is suppressed when a closer nested anchor
    (``.filigree.conf`` or legacy ``.filigree/``) sits between *start* and the
    worktree's ``.git`` pointer — that nested anchor wins, preserving the
    "child anchor overrides parent" contract for sub-projects nested inside a
    worktree. Root-level ``.filigree`` files copied to a linked worktree are
    treated as the parent project's tracked files unless local
    ``.filigree/config.json`` metadata proves the worktree was explicitly
    initialised as its own Filigree project.

    Returns the main worktree root when *start* (or an ancestor up to the
    first ``.git`` entry) is inside a linked worktree AND no nested anchor
    exists in that subtree. Returns *start* unchanged in every other case:
    a closer anchor was found first, plain repos (``.git`` is a directory),
    submodules (``.git`` file points at ``<parent>/.git/modules/<name>/``),
    no ``.git`` found, or a malformed ``.git`` file.
    """
    for parent in [start, *start.parents]:
        git_path = parent / ".git"
        conf_path = parent / CONF_FILENAME
        legacy_dir = parent / FILIGREE_DIR_NAME
        weft_store = parent / WEFT_DIR_NAME / WEFT_MEMBER_SUBDIR
        has_conf = conf_path.is_file()
        has_legacy_dir = legacy_dir.is_dir()
        has_weft_store = weft_store.is_dir()
        if has_conf or has_weft_store or has_legacy_dir:
            main_worktree = _main_worktree_from_git_path(git_path) if git_path.exists() else None
            # A worktree is its own project only when it carries local config
            # metadata (in either the federation store or the legacy dir).
            has_local = _has_local_filigree_config(weft_store) or _has_local_filigree_config(legacy_dir)
            if main_worktree is not None and not has_local:
                return main_worktree
            return start
        if not git_path.exists():
            continue
        # Plain repo: existing walk-up handles it correctly.
        if git_path.is_dir():
            return start
        # ``.git`` is a file — worktree pointer or submodule pointer.
        main_worktree = _main_worktree_from_git_path(git_path)
        if main_worktree is None:
            return start
        return main_worktree
    return start


def _has_local_filigree_config(filigree_dir: Path) -> bool:
    """Return whether ``filigree_dir`` proves a worktree-local install exists."""
    return (filigree_dir / CONFIG_FILENAME).is_file()


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


def _main_worktree_from_git_path(git_path: Path) -> Path | None:
    """Return the main checkout root when ``git_path`` is a linked-worktree pointer."""
    if not git_path.is_file():
        return None
    gitdir = _read_gitdir_pointer(git_path)
    if gitdir is None:
        return None
    if not gitdir.is_absolute():
        gitdir = (git_path.parent / gitdir).resolve()
    # Worktree shape: <main_repo>/.git/worktrees/<name>
    # Submodule shape: <parent_repo>/.git/modules/<name> — leave alone.
    if gitdir.parent.name != "worktrees":
        return None
    main_git_dir = gitdir.parent.parent
    if main_git_dir.name != ".git" or not main_git_dir.is_dir():
        return None
    # Bidirectional verification: git records a ``gitdir`` back-pointer in the
    # admin dir that names *this* worktree's ``.git`` file. Requiring it to
    # resolve back to ``git_path`` defeats two failure modes — an attacker-
    # controlled ``.git`` file (an untrusted clone could otherwise redirect
    # discovery onto an arbitrary victim project) and stale pointers left after
    # ``git worktree remove`` or an admin-dir rename. On any mismatch or read
    # failure we decline to redirect and let ``.git`` stand as a boundary.
    if not _worktree_back_pointer_matches(gitdir, git_path):
        return None
    return main_git_dir.parent


def _worktree_back_pointer_matches(admin_dir: Path, git_path: Path) -> bool:
    """Return whether *admin_dir*'s ``gitdir`` back-pointer resolves to *git_path*.

    Git's linked-worktree bookkeeping is bidirectional: the worktree's ``.git``
    file points at ``<main>/.git/worktrees/<name>`` (``admin_dir``), and that
    admin dir contains a ``gitdir`` file holding the absolute path back to the
    worktree's ``.git`` file. A genuine worktree round-trips; a spoofed or stale
    pointer does not. A missing or unreadable back-pointer counts as no match.
    """
    back_pointer_file = admin_dir / "gitdir"
    try:
        recorded = back_pointer_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return False
    if not recorded:
        return False
    try:
        return Path(recorded).resolve() == git_path.resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _classify_git_entry(git_path: Path) -> str:
    """Classify a ``.git`` filesystem entry for discovery diagnostics."""
    if git_path.is_dir():
        return "directory"
    if not git_path.exists() or not git_path.is_file():
        return "malformed_file"
    if _read_gitdir_pointer(git_path) is None:
        return "malformed_file"
    if _main_worktree_from_git_path(git_path) is not None:
        return "worktree_pointer"
    return "gitdir_file"


class WeftConfigUnreadableError(RuntimeError):
    """``weft.toml`` is present but cannot be parsed (bad TOML / non-UTF-8 / I/O error).

    Passive, read-only paths (discovery, ``resolve_store_dir``) swallow this and
    boot on built-in defaults (C-9c). The **mutating** init/install path must
    NOT: an unreadable config may hide an operator ``[filigree].store_dir`` pin,
    and treating "broken" as "absent" there would skip the don't-auto-migrate
    guard and relocate the store somewhere the operator never chose. So the write
    path raises this and refuses rather than guessing.
    """


def _load_weft_filigree_table(project_root: Path) -> dict[str, Any] | None:
    """Strict reader for the ``[filigree]`` table in ``project_root/weft.toml``.

    Returns the table dict, ``{}`` when the file exists but has no (or a non-dict)
    ``[filigree]`` table, or ``None`` when the file is **absent**. Raises
    :class:`WeftConfigUnreadableError` when the file is **present but unreadable**
    (TOML syntax error, non-UTF-8 bytes — tomllib decodes UTF-8 internally — or an
    OS read error). This is the primitive that distinguishes "absent" from
    "broken"; callers choose whether to tolerate the latter.

    Reads only from *project_root* — no walk-up. Discovery has already settled
    the project root; an independent walk would constitute a second anchor.
    """
    path = project_root / WEFT_TOML_FILENAME
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        msg = f"weft.toml present but unreadable at {path}: {exc}"
        raise WeftConfigUnreadableError(msg) from exc
    table = data.get(WEFT_MEMBER_SUBDIR)
    if not isinstance(table, dict):
        return {}
    return table


def read_weft_filigree_table(project_root: Path) -> dict[str, Any]:
    """Lenient read of the operator-authored ``[filigree]`` table from ``weft.toml``.

    Mirrors legis' ``_weft_legis_config`` (the federation reference): returns an
    empty dict when ``weft.toml`` is absent, carries no ``[filigree]`` table, or
    cannot be parsed. ``weft.toml`` is **enrich-only, never load-bearing** — a
    missing or malformed file must still boot on built-in defaults (convention
    C-9c). This function is strictly **read-only**: filigree never writes
    ``weft.toml`` (it is operator-authored / written only by ``weft init``).

    Boot-on-defaults is right for **passive discovery**; the mutating init path
    must instead distinguish absent from broken via :func:`_load_weft_filigree_table`
    (it raises on unreadable) so it never auto-migrates over a config it can't read.
    """
    try:
        table = _load_weft_filigree_table(project_root)
    except WeftConfigUnreadableError as exc:
        # Surface for diagnosis, but never hard-fail on a read-only/discovery path.
        logger.warning("%s; using built-in defaults", exc)
        return {}
    return table if table is not None else {}


def resolve_store_dir(project_root: Path) -> Path:
    """Resolve the machine-owned store dir for *project_root* (single source of truth).

    Resolution order (highest precedence first):
      1. ``weft.toml`` ``[filigree].store_dir`` operator override. Only a
         **project-relative, under-root** path is honoured (resolved against
         *project_root*). An absolute path, a ``..``-escaping path, a non-string,
         or an empty value is ignored (warn + fall back): filigree threads the
         store dir through the ``.filigree.conf`` ``db`` field, whose trust
         boundary forbids absolute / escaping paths, so such a value cannot be
         represented consistently and is never honoured half-way.
      2. ``.weft/filigree/`` if it exists — UNLESS legacy ``.filigree/`` is still
         canonical. Legacy stays canonical when it holds the DB and either
         (a) weft is an empty husk (no DB) — an aborted migration can leave an
         empty ``.weft/filigree/`` behind, and selecting it would let a confless
         open stamp a fresh empty DB over the live legacy data (data loss); or
         (b) the install is CONFLESS (no ``.filigree.conf``) — for a confless
         install migration's legacy-DB delete is the de-facto commit point, so
         legacy stays canonical (writes flow there) until that delete, even once
         a weft DB exists. A CONF install's ``.filigree.conf`` ``db`` field is
         its commit marker, so the conf-present path keeps the plain DB-presence
         tie-break and is unchanged (filigree-6f4b6dcd78).
      3. legacy ``.filigree/`` (back-compat; also the bare fallback that a fresh
         install's default ``db`` literal points beside before the dir exists).

    Pure read — never writes, never raises. ``find_filigree_anchor`` calls this
    from **both** the conf and confless branches, so
    ``anchor.store_dir == resolve_store_dir(anchor.project_root)`` by
    construction — the no-split-brain guarantee.
    """
    override = read_weft_filigree_table(project_root).get("store_dir")
    if isinstance(override, str) and override:
        candidate = Path(override)
        # Narrow to project-relative, under-root paths. filigree threads the
        # store dir through the .filigree.conf ``db`` field, whose trust boundary
        # already forbids absolute / escaping paths — so an absolute or
        # ``..``-escaping store_dir cannot be represented consistently and is
        # ignored (warn + fall back to the default), never honoured half-way.
        if candidate.is_absolute():
            logger.warning(
                "weft.toml [filigree].store_dir is absolute (%s); ignoring (must be project-relative) and using the default store.",
                override,
            )
        else:
            resolved = (project_root / candidate).resolve()
            try:
                resolved.relative_to(project_root.resolve())
            except ValueError:
                logger.warning(
                    "weft.toml [filigree].store_dir %r escapes the project root; ignoring and using the default store.",
                    override,
                )
            else:
                return project_root / candidate
    weft_store = project_root / WEFT_DIR_NAME / WEFT_MEMBER_SUBDIR
    legacy = project_root / FILIGREE_DIR_NAME
    # Prefer the federation store — but NOT while legacy is still canonical. The
    # choice keys on DB *presence*, not bare dir existence (a busy- or copy-
    # aborted migration can leave an empty ``.weft/filigree/`` behind, the dir
    # being created before the abortable copy). Two cases keep legacy canonical;
    # see the keep_legacy comment below. A genuinely fresh weft install
    # (config.json, DB not yet stamped, no legacy) is unaffected — its
    # legacy_db guard is False.
    weft_db_present = (weft_store / DB_FILENAME).is_file()
    legacy_db_present = (legacy / DB_FILENAME).is_file()
    # For a CONFLESS install there is no .filigree.conf to mark the migration
    # commit point, so migrate_store_to_weft's legacy-DB delete (step 4) is the
    # de-facto commit. Until that delete, legacy stays canonical: a confless
    # writer must keep routing to legacy even once a weft DB exists, otherwise a
    # write lands in weft and migrate's unconditional re-copy clobbers it from
    # the still-canonical legacy DB (data loss — filigree-6f4b6dcd78). A CONF
    # install's .filigree.conf ``db`` field is its commit marker, so when the
    # conf is present keep_legacy collapses to the original DB-presence tie-break
    # (legacy_db_present and not weft_db_present) — conf installs are unchanged.
    conf_present = (project_root / CONF_FILENAME).is_file()
    keep_legacy = legacy_db_present and (not weft_db_present or not conf_present)
    if weft_store.is_dir() and not keep_legacy:
        return weft_store
    if legacy.is_dir():
        return legacy
    # Neither layout materialised yet (e.g. a fresh project_root): the canonical
    # default is the federation store, where a new install will create it.
    return weft_store


class StoreMigrationBusyError(RuntimeError):
    """A live writer holds the DB, so migration cannot safely checkpoint it."""


def _checkpoint_and_copy_sqlite(src_db: Path, dest_db: Path) -> None:
    """Checkpoint *src_db*'s WAL and COPY it to *dest_db*, preserving all data.

    A normal ``close()`` does **not** truncate the WAL, so committed pages can
    live in the ``-wal`` sidecar; a copy of only the main ``.db`` would orphan
    them. Checkpoint with ``TRUNCATE`` to fold the WAL fully into the main file
    first, then byte-copy (``shutil.copy2`` — cross-device safe, and preserves
    the file header including the FILG ``application_id``).

    We **copy, not move**: the legacy DB stays in place and valid until the
    caller rewrites the conf to point at the destination, so a crash mid-
    migration never leaves a dangling conf nor an empty re-stamped DB (the
    migration is crash-convergent — a re-run resumes).

    The copy is **atomic at the destination**: it stages to a unique temp file
    in the dest dir and publishes with ``os.replace`` (mirroring
    :func:`write_atomic`). ``dest_db`` therefore only ever appears as a *complete*
    file — a crash (SIGKILL/power loss) during the byte copy leaves only the
    temp, which is cleaned up, never a truncated DB at the final path that a
    re-run's existence guard would mistake for a finished copy. ``copy2`` to a
    temp in the dest dir keeps the byte copy cross-device safe while ``os.replace``
    stays a same-filesystem (atomic) rename.

    Raises :class:`StoreMigrationBusyError` when another connection holds a
    write lock: ``PRAGMA wal_checkpoint(TRUNCATE)`` returns ``(1, …)`` (busy)
    instead of ``(0, …)`` when it cannot fully checkpoint, so we detect that and
    abort rather than copy a DB with un-folded WAL pages.
    """
    conn = sqlite3.connect(str(src_db), timeout=5.0)
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        conn.close()
    # row == (busy, log_frames, checkpointed_frames); busy != 0 means a writer
    # blocked a full checkpoint. For a non-WAL DB the pragma returns (0, -1, -1).
    if row is not None and row[0] != 0:
        msg = f"Cannot migrate {src_db}: another process holds the database. Close other filigree sessions and re-run."
        raise StoreMigrationBusyError(msg)
    dest_db.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest_db.parent, prefix=dest_db.name + ".", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(str(src_db), str(tmp))
        os.replace(tmp, dest_db)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _ephemeral_dashboard_port_if_live(project_root: Path) -> int | None:
    """Best-effort: the project's ephemeral dashboard port if one is bound, else ``None``.

    A session-scoped ephemeral dashboard (``filigree dashboard`` without
    ``--server-mode``) binds the project's deterministic port and holds a
    ``FiligreeDB`` connection on the legacy store. An *idle* such process holds no
    DB lock at migration time, so a lock/checkpoint probe cannot see it — we probe
    its deterministic port instead. The probe is portable (a localhost bind-attempt,
    no ``lsof``/procfs) and BEST-EFFORT: a bound port is a strong but not certain
    signal (something unrelated could own it), so it only ever *contributes* a
    refusal; any error returns ``None`` and never blocks migration.
    """
    from filigree import ephemeral

    port = ephemeral.compute_port(project_root / FILIGREE_DIR_NAME)
    # A *free* (bindable) port means nothing is listening → no ephemeral dashboard.
    if ephemeral._is_port_free(port):
        return None
    return port


def _live_filigree_daemon_for_project(project_root: Path) -> int | None:
    """Return the port of a live filigree daemon serving *project_root*, else ``None``.

    Registry-based detection, NOT lock-based: an idle daemon holds no DB lock but
    still has an open connection that can commit *after* migration unlinks the
    legacy DB (orphaning that write). ``BEGIN EXCLUSIVE`` would not see such an idle
    connection, so we consult the daemon registry + a deterministic port probe:
      * server-mode (B1): the shared daemon is alive (PID-verified ``daemon_status``)
        AND ``server.json`` registers a store dir whose project root is *project_root*.
      * ephemeral (B2): the project's deterministic dashboard port is bound.

    A still-open in-session MCP/stdio connection (B3) cannot be detected from here;
    that residual is documented in the upgrade notes.
    """
    from filigree import server

    project_root = project_root.resolve()

    status = server.daemon_status()
    if status.running:
        config = server.read_server_config()
        for store_key in config.projects:
            try:
                key_root = server._project_root_from_store_dir(Path(store_key)).resolve()
            except (OSError, ValueError):
                continue
            if key_root == project_root:
                return status.port

    return _ephemeral_dashboard_port_if_live(project_root)


def _refuse_if_daemon_serving(project_root: Path) -> None:
    """Raise :class:`StoreMigrationBusyError` when a live filigree daemon serves
    *project_root* — it holds an open connection on the legacy DB and could commit
    to it during/after migration, orphaning that write on the about-to-be-unlinked
    inode (filigree-031f9a413f). BEST-EFFORT: detection that itself fails must never
    crash migration, so any error is swallowed and migration proceeds.
    """
    try:
        port = _live_filigree_daemon_for_project(project_root)
    except Exception:
        # Detection is best-effort; never let it crash migration.
        logger.debug("Daemon-liveness detection failed for %s; proceeding", project_root, exc_info=True)
        return
    if port is not None:
        msg = (
            f"Cannot migrate {project_root / FILIGREE_DIR_NAME}: a filigree dashboard/server appears to be "
            f"running for this project (port {port}). It holds an open connection on the legacy database and "
            f"could write to it during migration, losing that write. Stop the dashboard/server "
            f"(`filigree server stop`, or close the ephemeral dashboard) and re-run `filigree init`."
        )
        raise StoreMigrationBusyError(msg)


def migrate_store_to_weft(project_root: Path) -> tuple[Path, bool]:
    """Migrate a legacy ``.filigree/`` store forward to ``.weft/filigree/``.

    **Explicit only** — call from ``filigree init`` / ``install``, never from
    passive discovery (discovery must stay write-free for read-only mounts).

    Returns ``(store_dir, migrated)``. ``migrated`` is ``True`` only when a
    vanilla legacy install (DB at ``.filigree/filigree.db``) was actually copied
    forward this call. No-ops (returning the resolved store dir,
    ``migrated=False``) when:
      * an operator ``weft.toml`` ``[filigree].store_dir`` override is set — the
        operator pins a custom store; we never auto-migrate over their choice
        (the override is a project-relative under-root path; absolute / escaping
        values are ignored by :func:`resolve_store_dir`, not honoured here),
      * the migration already completed (idempotent re-run), or
      * there is no legacy install, or
      * the operator relocated the DB outside ``.filigree/`` via the conf's
        ``db`` field — a custom layout we must not disturb.

    **Crash-convergent.** We COPY the DB (preserving FILG ``application_id``),
    then rewrite the conf, then remove the legacy DB. The conf-pinned legacy DB
    is canonical until the conf commits to the weft destination, so a crash at
    any step leaves a consistent state the next explicit run resumes. Because
    legacy stays canonical until that commit — and may take writes after an
    interrupted copy — step 1 re-copies it forward *unconditionally* rather than
    trusting a weft copy that merely looks intact (which could be stale); the
    atomic publish makes that refresh safe and cheap. The committed case is
    fenced off by the top guard, so re-copy never fires post-commit.
    """
    weft_store = project_root / WEFT_DIR_NAME / WEFT_MEMBER_SUBDIR
    legacy_dir = project_root / FILIGREE_DIR_NAME
    conf_path = project_root / CONF_FILENAME
    weft_db = weft_store / DB_FILENAME
    legacy_db = legacy_dir / DB_FILENAME

    # Operator override pins a custom store — never auto-migrate over it. Read
    # weft.toml STRICTLY here (unlike passive discovery): a present-but-unreadable
    # config might hide a ``store_dir`` pin, so conflating "broken" with "absent"
    # would skip this guard and relocate the store the operator never chose. Refuse
    # — raise before any filesystem mutation — rather than guess (I1).
    #
    # Override-asymmetry note (#2): ANY non-empty ``store_dir`` is treated as an
    # operator pin and we no-op here, even when the value is invalid (absolute /
    # ``..``-escaping). That asymmetry vs :func:`resolve_store_dir` (which *ignores*
    # an invalid override and falls through) is intentional and harmless: this
    # no-op mutates nothing, and resolve_store_dir then reads the SAME legacy store
    # this leaves canonical — so the worst case is a functional surprise (migration
    # silently skipped on a typo'd pin), never data loss. Declining to migrate is
    # the safe side of the ambiguity; honouring an invalid pin would be the unsafe one.
    override = (_load_weft_filigree_table(project_root) or {}).get("store_dir")
    if isinstance(override, str) and override:
        return resolve_store_dir(project_root), False

    if not legacy_dir.is_dir():
        return resolve_store_dir(project_root), False

    def _conf_db() -> Path | None:
        if not conf_path.is_file():
            return None
        try:
            db_rel = str(read_conf(conf_path)["db"])
            return (conf_path.parent / db_rel).resolve()
        except (OSError, ValueError):
            return None

    conf_db = _conf_db()
    # Completed already (conf points at the weft DB and it exists) → idempotent.
    if weft_db.is_file() and conf_db == weft_db.resolve():
        return weft_store, False
    # Confless completion: a confless project has no conf to point at the weft DB,
    # so the check above can never fire for it (conf_db is None). When its legacy
    # DB is already gone and the weft DB exists, migration has fully completed —
    # re-running must be an idempotent no-op. Without this the confless path falls
    # through to a needless re-copy (migrated=True) or, with a live daemon, a
    # spurious StoreMigrationBusyError despite nothing being left to migrate. This
    # stays confless-SPECIFIC: a confful crash-mid-rename (conf_db points at the
    # now-deleted legacy DB, weft present) keeps conf_db non-None and must still
    # fall through below to rewrite the conf.
    if conf_db is None and weft_db.is_file() and not legacy_db.is_file():
        return weft_store, False

    # Only migrate the vanilla layout (DB inside .filigree/, or a half-finished
    # prior run whose legacy DB is already gone). An operator who relocated the
    # DB elsewhere via the conf keeps their custom layout untouched.
    current_db = conf_db if conf_db is not None else legacy_db.resolve()
    if current_db not in {legacy_db.resolve(), weft_db.resolve()}:
        return resolve_store_dir(project_root), False
    if not legacy_db.is_file() and not weft_db.is_file():
        # Nothing to carry forward (e.g. conf with no DB yet) — leave as-is.
        return resolve_store_dir(project_root), False

    # We have now DECIDED to migrate (legacy present, not idempotent-complete,
    # vanilla layout). DETECT-AND-REFUSE (registry-based quiesce, B1/B2): a live
    # daemon holds an open legacy-DB connection that can commit *after* our copy
    # and *after* our unlink, orphaning that write on the deleted inode. No
    # copy-time guard can close that — the write has not happened yet at copy time
    # — so we refuse up front and tell the operator to stop it. Runs BEFORE any
    # mutation so a refusal never litters a weft husk. Best-effort: detection that
    # itself fails never blocks migration (filigree-031f9a413f).
    _refuse_if_daemon_serving(project_root)

    # Do NOT pre-create weft_store here: the copy below can abort
    # (StoreMigrationBusyError before any mutation), and an empty .weft/filigree/
    # left behind would be picked up as canonical by a confless open and stamped
    # with a fresh empty DB (data loss — resolve_store_dir now also defends
    # against this, but we avoid littering the husk in the first place). The dir
    # is created inside _checkpoint_and_copy_sqlite, AFTER the busy check passes;
    # the metadata loop and conf rewrite below only run once weft_store exists
    # (legacy_db present → copy created it; legacy_db absent → weft_db present,
    # so the dir already exists).
    # 1. Copy the DB forward while the legacy DB exists. Re-copy is
    #    *unconditional* — NOT gated on the destination looking valid. Until the
    #    conf commits to weft, the conf-pinned legacy DB is canonical and may
    #    have taken writes since an interrupted copy, so a weft copy that merely
    #    *looks* intact can be stale; publishing it (then deleting legacy) would
    #    silently lose those writes. The atomic publish in
    #    _checkpoint_and_copy_sqlite makes an unconditional refresh safe and
    #    cheap, and the committed case already short-circuited at the top guard,
    #    so this never re-copies post-commit. (legacy gone but weft present →
    #    nothing to copy from; the existing weft DB is the survivor.)
    if legacy_db.is_file():
        _checkpoint_and_copy_sqlite(legacy_db, weft_db)
    # 2. Copy durable + runtime metadata beside the DB. config.json must follow
    #    so the enabled_packs fallback resolves; federation_token must follow so
    #    sibling continuity survives. Idempotent (skip if already present).
    for name in (CONFIG_FILENAME, "INSTALL_VERSION", SUMMARY_FILENAME, "federation_token", "scanners", "templates"):
        src = legacy_dir / name
        dest = weft_store / name
        if src.exists() and not dest.exists():
            if src.is_dir():
                shutil.copytree(str(src), str(dest))
            else:
                shutil.copy2(str(src), str(dest))
    # 3. Rewrite the conf to point at the destination — only now that a valid DB
    #    exists there. (Idempotent: skip if it already points at the weft DB.)
    new_db_rel = f"{WEFT_DIR_NAME}/{WEFT_MEMBER_SUBDIR}/{DB_FILENAME}"
    if conf_path.is_file():
        conf_data = read_conf(conf_path)
        if conf_data.get("db") != new_db_rel:
            conf_data["db"] = new_db_rel
            write_conf(conf_path, conf_data)
    # 4. Remove the now-superseded legacy DB (+ sidecars) so discovery never sees
    #    a stale second database. Done last: until here the legacy DB was the
    #    live one. The rest of the legacy dir is left as an auditable husk.
    for suffix in ("", "-wal", "-shm"):
        stale = legacy_dir / (DB_FILENAME + suffix)
        if stale.is_file():
            stale.unlink()
    # Auditable, non-destructive breadcrumb — never delete the legacy dir.
    (legacy_dir / LEGACY_MOVED_BREADCRUMB).write_text(
        f"This store was migrated to {WEFT_DIR_NAME}/{WEFT_MEMBER_SUBDIR}/.\n"
        f"The legacy {FILIGREE_DIR_NAME}/ directory is intentionally left in place.\n"
    )
    return weft_store, True


class FiligreeAnchor(NamedTuple):
    """The resolved project anchor: where the project is and where its store lives.

    ``conf_path`` is the ``.filigree.conf`` when one exists, else ``None`` for a
    dir-only (confless) anchor. ``store_dir`` is the resolved machine-owned store
    directory (``resolve_store_dir(project_root)``) — the single source of truth
    for config.json / runtime metadata, independent of where a conf's ``db``
    field relocates the database.
    """

    project_root: Path
    conf_path: Path | None
    store_dir: Path


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


def find_filigree_anchor(start: Path | None = None) -> FiligreeAnchor:
    """Walk up from *start* for a conf, a ``.weft/filigree/`` store, or a legacy dir.

    Returns a :class:`FiligreeAnchor` ``(project_root, conf_path, store_dir)``.
    ``conf_path`` is the resolved ``.filigree.conf`` when one exists, or ``None``
    for a dir-only install. ``store_dir`` is ``resolve_store_dir(project_root)``
    — the resolved machine-owned store directory. The walk is closer-first: a
    child anchor wins over an ancestor regardless of type. Within a directory,
    precedence is conf > ``.weft/filigree/`` > legacy ``.filigree/``.

    Pure read — never writes. Use this when discovery must work on read-only
    mounts (inspection commands, ``filigree doctor``, and explicit
    legacy-compatible code paths). Implicit agent startup surfaces use the
    stricter :func:`find_filigree_conf` path so a legacy ancestor is not
    treated as a project attachment signal. To force a backfill, run
    ``filigree init`` (or another explicit write path) on a writable copy of
    the project.

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
            return FiligreeAnchor(parent, conf, resolve_store_dir(parent))
        weft_store = parent / WEFT_DIR_NAME / WEFT_MEMBER_SUBDIR
        if weft_store.is_dir():
            if git_boundary is not None:
                raise ForeignDatabaseError(cwd=orig, found_anchor=weft_store, git_boundary=git_boundary)
            return FiligreeAnchor(parent, None, resolve_store_dir(parent))
        legacy_dir = parent / FILIGREE_DIR_NAME
        if legacy_dir.is_dir():
            if git_boundary is not None:
                raise ForeignDatabaseError(cwd=orig, found_anchor=legacy_dir, git_boundary=git_boundary)
            return FiligreeAnchor(parent, None, resolve_store_dir(parent))
        if git_boundary is None and (parent / ".git").exists():
            git_boundary = parent
    msg = (
        f"No {CONF_FILENAME}, {WEFT_DIR_NAME}/{WEFT_MEMBER_SUBDIR}/ or {FILIGREE_DIR_NAME}/ "
        f"found in {orig} or any parent directory. "
        f"Run `filigree init` here to create one, or `filigree doctor` to diagnose."
    )
    raise ProjectNotInitialisedError(msg)


def find_filigree_root(start: Path | None = None) -> Path:
    """Return the project's machine-owned **store** directory (metadata dir).

    Resolves the anchor via :func:`find_filigree_anchor` and returns its
    ``store_dir`` — ``.weft/filigree/`` for federation-layout installs, legacy
    ``.filigree/`` for back-compat installs, or an operator ``store_dir``
    override. Every caller concatenates ``SUMMARY_FILENAME``, ``ephemeral.pid``,
    ``config.json``, ``scanners/`` etc. onto the result; under the WEFT store
    consolidation those runtime files live in the resolved store dir, so
    returning ``store_dir`` (not the literal ``.filigree/``) keeps them in one
    place regardless of layout.

    The store dir is resolved **independently** of any custom ``db`` location a
    conf declares (the conf's ``db`` may relocate only the database file).
    Callers that need the actual DB path should use :meth:`FiligreeDB.from_conf`
    / :meth:`FiligreeDB.from_anchor`.

    Resolves through :func:`find_filigree_anchor` so legacy installs (which
    have no conf yet) are still discoverable without writing.
    """
    return find_filigree_anchor(start).store_dir


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
    _validate_registry_settings(raw, source=conf_path)
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
    defaults = ProjectConfig(prefix="filigree", version=1, enabled_packs=["core", "planning", "release"], registry_backend="local")
    config_path = filigree_dir / CONFIG_FILENAME
    if not config_path.exists():
        return defaults
    try:
        raw: Any = json.loads(config_path.read_text())
        if not isinstance(raw, dict):
            logger.warning("Config %s is not a JSON object, using defaults", config_path)
            return defaults
        result: ProjectConfig = dict(raw)  # type: ignore[assignment]
        prefix = raw.get("prefix", defaults["prefix"])
        if not isinstance(prefix, str) or not prefix:
            logger.warning("Config %s has malformed prefix, using default", config_path)
            result["prefix"] = defaults["prefix"]
        version = raw.get("version", defaults["version"])
        if isinstance(version, bool) or not isinstance(version, int):
            logger.warning("Config %s has malformed version, using default", config_path)
            result["version"] = defaults["version"]
        packs = raw.get("enabled_packs", defaults["enabled_packs"])
        if not isinstance(packs, list) or not all(isinstance(p, str) for p in packs):
            logger.warning("Config %s has malformed enabled_packs, using default", config_path)
            result["enabled_packs"] = defaults["enabled_packs"]
        if "prefix" not in result:
            result["prefix"] = defaults["prefix"]
        if "version" not in result:
            result["version"] = defaults["version"]
        if "enabled_packs" not in result:
            result["enabled_packs"] = defaults["enabled_packs"]
        if "registry_backend" not in result:
            result["registry_backend"] = defaults["registry_backend"]
        _validate_registry_settings(cast("dict[str, Any]", result), source=config_path)
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
VALID_REGISTRY_BACKENDS: frozenset[RegistryBackend] = frozenset(cast("tuple[RegistryBackend, ...]", get_args(RegistryBackend)))


class _LoomweaveLocalFallbackRegistry:
    """Try Loomweave first, then fall back to local IDs for availability failures."""

    @staticmethod
    def _should_fallback(exc: RegistryUnavailableError) -> bool:
        # ``invalid_response`` means Loomweave was reachable but violated the
        # resolver contract. Treat that as a fail-closed protocol error rather
        # than an availability failure; falling back locally could mask
        # security-bearing outcomes such as ``briefing_blocked`` embedded in an
        # ambiguous malformed batch response.
        return exc.cause_kind != "invalid_response"

    def __init__(self, primary: RegistryProtocol, fallback: LocalRegistry, *, base_url: str) -> None:
        self._primary = primary
        self._fallback = fallback
        self._base_url = base_url

    def resolve_file(
        self,
        path: str,
        *,
        language: str = "",
        actor: str = "",
    ) -> ResolvedFile:
        try:
            return self._primary.resolve_file(path, language=language, actor=actor)
        except RegistryUnavailableError as exc:
            if not self._should_fallback(exc):
                raise
            logger.warning(
                "Loomweave registry backend unavailable; using local file registry fallback",
                extra={
                    "registry_backend": "loomweave",
                    "loomweave_base_url": self._base_url,
                    "path": path,
                    "url": exc.url,
                    "cause_kind": exc.cause_kind,
                },
            )
            return self._fallback.resolve_file(path, language=language, actor=actor)

    def resolve_files_batch(
        self,
        queries: list[BatchQuery],
        *,
        actor: str = "",
    ) -> BatchResolution:
        """Whole-batch fallback semantics: if Loomweave is unreachable for the
        batch, every item in the batch resolves through ``LocalRegistry`` and
        a single WARN log captures the cause.

        Per-item failures (``not_found`` / ``briefing_blocked`` / ``errors``)
        from a *successful* batch call pass through verbatim — those are not
        availability failures and must NOT be silently re-attached locally
        (briefing-blocked in particular is a security-bearing refusal).
        Likewise, reachable malformed Loomweave responses (``cause_kind``
        ``invalid_response``) fail closed instead of falling back because they
        may contain ambiguous security-bearing outcomes.

        For primaries that only implement ``resolve_file`` (test fakes
        predating CONTRACT-1), the loop helper from ``filigree.registry``
        adapts the legacy single-item API.
        """
        primary_batch = getattr(self._primary, "resolve_files_batch", None)
        try:
            if primary_batch is not None:
                result: BatchResolution = primary_batch(queries, actor=actor)
                return result
            return resolve_files_batch_via_loop(self._primary, queries, actor=actor)
        except RegistryUnavailableError as exc:
            if not self._should_fallback(exc):
                raise
            logger.warning(
                "Loomweave registry backend unavailable for batch resolve; using local file registry fallback",
                extra={
                    "registry_backend": "loomweave",
                    "loomweave_base_url": self._base_url,
                    "batch_size": len(queries),
                    "url": exc.url,
                    "cause_kind": exc.cause_kind,
                },
            )
            return self._fallback.resolve_files_batch(queries, actor=actor)

    def is_displaced(self) -> bool:
        return self._primary.is_displaced()

    def close(self) -> None:
        close_primary = getattr(self._primary, "close", None)
        if callable(close_primary):
            close_primary()
        close_fallback = getattr(self._fallback, "close", None)
        if callable(close_fallback):
            close_fallback()


def _apply_allow_local_fallback_override(
    loomweave_config: LoomweaveConfig | None,
    override: bool | None,
) -> LoomweaveConfig | None:
    """Apply a ``--allow-local-fallback`` startup override to a loomweave config.

    Returns the input untouched when ``override is None`` (no flag passed).
    Otherwise produces a new dict with ``allow_local_fallback`` set to the
    override value. Used by the dashboard / CLI startup paths to thread the
    operator's recovery flag into the constructor before the capability
    probe runs.
    """
    if override is None:
        return loomweave_config
    merged: LoomweaveConfig = dict(loomweave_config or {})  # type: ignore[assignment]
    merged["allow_local_fallback"] = override
    return merged


def _migrate_legacy_registry_config(raw: dict[str, Any]) -> None:
    """Rename-on-load shim (3.0 Loomweave/Weft rebrand).

    A deployed ``.filigree.conf`` still carrying the pre-3.0 ``clarion`` names
    loads as ``loomweave`` without a manual edit: ``registry_backend: "clarion"``
    becomes ``"loomweave"`` and a ``[clarion]`` section moves to ``[loomweave]``.
    One-shot and in place. There is no reverse shim — once the config is
    re-saved it carries the new names, and a bare ``"clarion"`` value is no
    longer a valid backend.
    """
    if raw.get("registry_backend") == "clarion":
        raw["registry_backend"] = "loomweave"
    if "clarion" in raw and "loomweave" not in raw:
        raw["loomweave"] = raw.pop("clarion")


def _validate_registry_settings(raw: dict[str, Any], *, source: Path, require_loomweave_base_url: bool = True) -> None:
    """Validate ADR-014 registry backend settings in project config."""
    _migrate_legacy_registry_config(raw)
    if "registry_backend" in raw:
        backend = raw["registry_backend"]
        if not isinstance(backend, str) or backend not in VALID_REGISTRY_BACKENDS:
            msg = f"{source}: 'registry_backend' must be one of {sorted(VALID_REGISTRY_BACKENDS)}, got {backend!r}"
            raise ValueError(msg)

    if "loomweave" not in raw:
        if raw.get("registry_backend") == "loomweave":
            msg = f"{source}: 'loomweave.base_url' is required when registry_backend is 'loomweave'"
            raise ValueError(msg)
        return
    loomweave = raw["loomweave"]
    if not isinstance(loomweave, dict):
        msg = f"{source}: 'loomweave' must be a JSON object, got {type(loomweave).__name__}: {loomweave!r}"
        raise ValueError(msg)
    allowed_loomweave_keys = {"base_url", "timeout_seconds", "allow_local_fallback", "token_env"}
    unknown_loomweave_keys = sorted(set(loomweave) - allowed_loomweave_keys)
    if unknown_loomweave_keys:
        msg = f"{source}: unknown loomweave setting(s): {', '.join(unknown_loomweave_keys)}"
        raise ValueError(msg)
    if require_loomweave_base_url and raw.get("registry_backend") == "loomweave" and "base_url" not in loomweave:
        msg = f"{source}: 'loomweave.base_url' is required when registry_backend is 'loomweave'"
        raise ValueError(msg)
    if "base_url" in loomweave:
        try:
            normalize_loomweave_base_url(cast("str", loomweave["base_url"]))
        except ValueError as exc:
            msg = f"{source}: {exc}"
            raise ValueError(msg) from exc
    if "timeout_seconds" in loomweave:
        timeout = loomweave["timeout_seconds"]
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
            msg = f"{source}: 'loomweave.timeout_seconds' must be a positive number, got {timeout!r}"
            raise ValueError(msg)
    if "allow_local_fallback" in loomweave and not isinstance(loomweave["allow_local_fallback"], bool):
        msg = f"{source}: 'loomweave.allow_local_fallback' must be a boolean, got {loomweave['allow_local_fallback']!r}"
        raise ValueError(msg)
    if "token_env" in loomweave:
        token_env = loomweave["token_env"]
        if not isinstance(token_env, str) or not token_env.strip():
            msg = f"{source}: 'loomweave.token_env' must be a non-empty string naming an env var, got {token_env!r}"
            raise ValueError(msg)


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
    4. sys.executable -P -m filigree -- safe-path module invocation fallback
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

    # Use Python's safe-path mode for the module fallback.  ``python -m``
    # otherwise prepends the current working directory to sys.path, allowing an
    # untrusted project-local ``filigree.py`` or ``filigree/__main__.py`` to
    # shadow the installed package when hooks run from the project root.
    return [sys.executable, "-P", "-m", "filigree"]


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
# Schema-version gatekeeper
# ---------------------------------------------------------------------------


DbVerdict = Literal["fresh", "current", "needs_upgrade", "legacy_needs_upgrade"]


def classify_and_stamp_filigree_db(conn: sqlite3.Connection, *, db_path: Path) -> DbVerdict:
    """Inspect a SQLite DB and classify it.

    Reads ``PRAGMA application_id`` and ``PRAGMA user_version``. For a fresh
    DB (both zero), stamps ``application_id`` and returns ``"fresh"`` — the
    caller is responsible for running ``SCHEMA_SQL`` and stamping
    ``user_version``. For an existing filigree DB, returns one of
    ``"current"``, ``"needs_upgrade"``, or ``"legacy_needs_upgrade"`` (the
    last means ``application_id`` is still ``0`` from a pre-app-id-aware
    install and the next migration must stamp it). Raises
    :class:`SchemaVersionMismatchError` for newer-than-installed DBs and
    :class:`ForeignSqliteFileError` for files whose ``application_id`` is
    non-zero and not :data:`FILIGREE_APPLICATION_ID`.

    Has a write side-effect on fresh DBs (stamps ``application_id``); for
    a pure-read identity check on a separate connection (used by
    server-mode ``register_project``), see ``server._read_project_db_identity``.
    """
    from filigree.types.api import SchemaVersionMismatchError

    app_id: int = conn.execute("PRAGMA application_id").fetchone()[0]
    version: int = conn.execute("PRAGMA user_version").fetchone()[0]

    if app_id == 0 and version == 0:
        conn.execute(f"PRAGMA application_id = {FILIGREE_APPLICATION_ID}")
        return "fresh"

    if app_id == 0 and version > 0:
        # Pre-application_id filigree DB. Trust user_version. A value above the
        # installed schema is a too-new filigree DB (a downgrade), not a foreign
        # file — surface it as a version mismatch ("upgrade filigree") rather
        # than a foreign-file error ("move this file"), matching the verdict the
        # stamped path below gives for the same situation.
        if version > CURRENT_SCHEMA_VERSION:
            raise SchemaVersionMismatchError(
                installed=CURRENT_SCHEMA_VERSION,
                database=version,
            )
        return "current" if version == CURRENT_SCHEMA_VERSION else "legacy_needs_upgrade"

    if app_id != FILIGREE_APPLICATION_ID:
        raise ForeignSqliteFileError(path=db_path, observed_application_id=app_id)

    if version > CURRENT_SCHEMA_VERSION:
        raise SchemaVersionMismatchError(
            installed=CURRENT_SCHEMA_VERSION,
            database=version,
        )
    if version == CURRENT_SCHEMA_VERSION:
        return "current"
    return "needs_upgrade"


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
        meta_dir: str | Path | None = None,
        registry: RegistryProtocol | None = None,
        registry_backend: RegistryBackend = "local",
        loomweave_config: LoomweaveConfig | None = None,
        skip_loomweave_capability_probe: bool = False,
        verified_actor: str | None = None,
    ) -> None:
        # ``skip_loomweave_capability_probe`` exists for unit tests that stand up
        # stub HTTP servers serving only ``/api/v1/files``; production callers
        # should leave it ``False`` so ADR-014's fail-closed handshake runs.
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
        elif self.db_path.parent.name == WEFT_MEMBER_SUBDIR and self.db_path.parent.parent.name == WEFT_DIR_NAME:
            # ``.weft/filigree/filigree.db`` → project root is two dirs up from
            # the store dir. The federation-layout analogue of the legacy
            # ``.filigree/`` auto-derive above.
            self.project_root = self.db_path.parent.parent.parent
        else:
            self.project_root = None
        # ``meta_dir`` is the machine-owned store/metadata directory (config.json,
        # scanners/, ephemeral.pid, context.md, templates/). The anchor-aware
        # constructors (``from_anchor`` / ``from_conf`` / ``from_filigree_dir``)
        # pass the resolved ``store_dir`` explicitly so a conf-relocated DB still
        # points metadata at the store dir, not at ``db_path.parent``. The bare
        # fallback (``db_path.parent``) only fires for direct ``FiligreeDB(...)``
        # construction in tests / fresh init, where the two coincide.
        self.meta_dir: Path = Path(meta_dir) if meta_dir is not None else self.db_path.parent
        if enabled_packs is not None and isinstance(enabled_packs, str):
            msg = f"enabled_packs must be a list of strings, not a bare string: {enabled_packs!r}"
            raise TypeError(msg)
        self._enabled_packs_override = list(enabled_packs) if enabled_packs is not None else None
        self.enabled_packs = self._enabled_packs_override if self._enabled_packs_override is not None else ["core", "planning", "release"]
        self._conn: sqlite3.Connection | None = None
        self._check_same_thread = check_same_thread
        # ADR-012 (schema v24): the transport-verified identity for this session,
        # set once at the entry point (CLI get_db / MCP _init_db). None = no
        # transport proof; every runtime insert stamps this into verified_*.
        # ``borrow_for_worker_thread`` propagates it for free via copy.copy.
        self._verified_actor: str | None = verified_actor
        # Whether this instance owns (and must close) ``self.registry``.
        # ``borrow_for_worker_thread`` clones share the registry by reference
        # and set this False so tearing the clone down never closes the
        # parent's Loomweave client. See ``_close_registry``.
        self._owns_registry = True
        self._template_registry: TemplateRegistry | None = template_registry
        if registry_backend not in VALID_REGISTRY_BACKENDS:
            msg = f"registry_backend must be one of {sorted(VALID_REGISTRY_BACKENDS)}, got {registry_backend!r}"
            raise ValueError(msg)
        _validate_registry_settings(
            {
                "registry_backend": registry_backend,
                "loomweave": dict(loomweave_config or {}),
            },
            source=self.db_path,
            require_loomweave_base_url=registry is None,
        )
        self.registry_backend = registry_backend
        self.loomweave_config = cast("LoomweaveConfig", dict(loomweave_config or {}))
        self.allow_local_fallback = bool(self.loomweave_config.get("allow_local_fallback", False))
        # Loomweave capability-probe state — populated by the startup probe (or by
        # ``reprobe_loomweave_capabilities`` later). ``loomweave_instance_rotated`` is
        # set when a mid-session re-probe sees a different ``instance_id`` than
        # the startup probe; it is read by ``GET /api/files/_schema`` so the
        # dashboard can surface a "Loomweave was re-indexed; stored file IDs may
        # be stale" banner without a separate endpoint.
        self.loomweave_capabilities: LoomweaveCapabilities | None = None
        self.loomweave_instance_id: str | None = None
        self.loomweave_api_version: int | None = None
        self.loomweave_instance_rotated: bool = False
        if registry is not None:
            backend_displaced = registry_backend == "loomweave"
            registry_displaced = registry.is_displaced()
            if registry_displaced != backend_displaced:
                msg = (
                    "Injected registry displacement does not match registry_backend: "
                    f"registry.is_displaced()={registry_displaced}, registry_backend={registry_backend!r}"
                )
                raise ValueError(msg)
            self.registry = registry
            if self.allow_local_fallback and registry_backend == "loomweave":
                self.enable_local_registry_fallback()
        elif registry_backend == "loomweave":
            base_url_value = self.loomweave_config.get("base_url")
            if not isinstance(base_url_value, str) or not base_url_value:
                msg = "loomweave.base_url is required when registry_backend is 'loomweave'"
                raise ValueError(msg)
            base_url = normalize_loomweave_base_url(base_url_value)
            self.loomweave_config["base_url"] = base_url
            timeout_seconds = float(self.loomweave_config.get("timeout_seconds", 5))
            auth_token = self._resolve_loomweave_auth_token()
            # Pass auth_token only when set — keeps test fakes that monkeypatch
            # LoomweaveRegistry with the older 2-arg signature working without
            # forcing every test to add a keyword argument they don't use.
            registry_kwargs: dict[str, Any] = {"timeout_seconds": timeout_seconds}
            if auth_token is not None:
                registry_kwargs["auth_token"] = auth_token
            self.registry = LoomweaveRegistry(base_url, **registry_kwargs)
            if not skip_loomweave_capability_probe:
                self._run_initial_loomweave_capability_probe(base_url, timeout_seconds=timeout_seconds, auth_token=auth_token)
            if self.allow_local_fallback:
                self.enable_local_registry_fallback()
        else:
            self.registry = self._make_local_registry()

    def _make_local_registry(self) -> LocalRegistry:
        return LocalRegistry(lambda: self._generate_unique_id("file_records", "f"))

    def _rebind_local_id_factory_to_self(self) -> None:
        """Repoint any local file-id factory at THIS instance's connection.

        ``LocalRegistry``'s id factory closes over the ``FiligreeDB`` it was
        built on, minting ids via that instance's ``self.conn``. A clone from
        :meth:`borrow_for_worker_thread` shares the parent's registry object by
        reference, so without this rebind a worker-thread clone would mint local
        file ids through the PARENT's (shared, event-loop) connection — a
        cross-thread ``sqlite3.Connection`` misuse (``SQLITE_MISUSE``). Rebind
        the local registry, or the local-fallback half of a Loomweave fallback
        wrapper, so id minting uses this clone's private connection. The shared
        Loomweave ``_primary`` (the ``httpx.Client``) is kept by reference and is
        never closed by the non-owning clone.
        """
        registry = self.registry
        if isinstance(registry, LocalRegistry):
            self.registry = self._make_local_registry()
        elif isinstance(registry, _LoomweaveLocalFallbackRegistry):
            self.registry = _LoomweaveLocalFallbackRegistry(
                registry._primary,
                self._make_local_registry(),
                base_url=registry._base_url,
            )

    def _loomweave_base_url(self) -> str | None:
        """Return the configured Loomweave base URL, or ``None`` if absent.

        ``LoomweaveConfig`` is ``TypedDict(total=False)`` so ``.get("base_url")``
        is typed as ``str | None``; this wrapper centralises the access so
        callers don't have to re-derive the contract at each call site.
        """
        value = self.loomweave_config.get("base_url")
        if not isinstance(value, str) or not value:
            return None
        return value

    def _loomweave_timeout_seconds(self) -> float:
        """Return the configured Loomweave HTTP timeout in seconds."""
        return float(self.loomweave_config.get("timeout_seconds", 5))

    def _resolve_loomweave_auth_token(self) -> str | None:
        """Resolve the Bearer token for Loomweave calls from the configured env var.

        Per the Loomweave 1.0 cross-product contract: ``LoomweaveConfig.token_env``
        names the env var (default ``WEFT_TOKEN``); if it resolves to
        a non-empty value, send ``Authorization: Bearer <token>``; if it is
        unset or empty, send no auth header. When ``token_env`` was set
        explicitly in config but the env var is missing or empty, emit a WARN
        so operators can notice silent loopback-only fallback.
        """
        token_env_name = self.loomweave_config.get("token_env", DEFAULT_LOOMWEAVE_TOKEN_ENV)
        token_env_was_explicit = "token_env" in self.loomweave_config
        value = os.environ.get(token_env_name, "")
        if value:
            return value
        if token_env_was_explicit:
            logger.warning(
                "Loomweave token_env %r is configured but the environment variable is missing or empty; "
                "sending no Authorization header. Loomweave will accept on loopback bind and reject on non-loopback.",
                token_env_name,
                extra={"token_env": token_env_name, "loomweave_base_url": self.loomweave_config.get("base_url", "")},
            )
        return None

    def _run_initial_loomweave_capability_probe(self, base_url: str, *, timeout_seconds: float, auth_token: str | None = None) -> None:
        """Probe Loomweave's ``_capabilities`` endpoint at startup and capture identity.

        Fail-closed semantics per ADR-014 §7:
        - api_version mismatch always raises (no fallback can save a wire-break).
        - reachable Loomweave that declines the registry-backend role raises
          ``RegistryUnavailableError`` (transient; respects ``allow_local_fallback``).
        - probe-time HTTP/network failure raises ``RegistryUnavailableError``
          (caller's ``allow_local_fallback`` decides whether to downgrade).

        Version-mismatch failures bypass the fallback policy because they
        signal a permanent protocol incompatibility; transient/reachability
        failures fall through to the existing fallback wrapping in
        ``__init__``.
        """
        try:
            capabilities = probe_loomweave_capabilities(base_url, timeout_seconds=timeout_seconds, auth_token=auth_token)
            validate_loomweave_capabilities(capabilities, base_url=base_url)
        except RegistryVersionMismatchError:
            raise
        except RegistryUnavailableError as exc:
            if self.allow_local_fallback:
                logger.warning(
                    "Loomweave capability probe failed at startup; allow_local_fallback=true, "
                    "auto-creates will route through LocalRegistry until Loomweave recovers",
                    extra={
                        "url": exc.url,
                        "cause_kind": exc.cause_kind,
                        "registry_backend": "loomweave",
                    },
                )
                return
            raise
        self.loomweave_capabilities = capabilities
        self.loomweave_instance_id = capabilities["instance_id"]
        self.loomweave_api_version = capabilities["api_version"]
        logger.info(
            "Loomweave capability probe succeeded",
            extra={
                "loomweave_base_url": base_url,
                "instance_id": capabilities["instance_id"],
                "api_version": capabilities["api_version"],
            },
        )

    def reprobe_loomweave_capabilities(self) -> LoomweaveCapabilities | None:
        """Re-issue the capability probe and flag a banner on instance_id rotation.

        Returns ``None`` if this DB is not running in ``loomweave`` mode, or if
        Loomweave is unreachable (the unavailability is logged at WARN; callers
        that need fail-closed behaviour should call ``resolve_file`` instead,
        which already has the strict policy). Returns the probe payload
        otherwise.

        On instance_id rotation — Loomweave was re-indexed mid-session and any
        stored Loomweave file IDs may be stale — sets
        ``loomweave_instance_rotated=True`` and logs at WARN. The dashboard
        surfaces this through ``GET /api/files/_schema``.
        """
        if self.registry_backend != "loomweave":
            return None
        base_url_value = self._loomweave_base_url()
        if base_url_value is None:
            return None
        timeout_seconds = self._loomweave_timeout_seconds()
        auth_token = self._resolve_loomweave_auth_token()
        try:
            capabilities = probe_loomweave_capabilities(base_url_value, timeout_seconds=timeout_seconds, auth_token=auth_token)
            validate_loomweave_capabilities(capabilities, base_url=base_url_value)
        except RegistryUnavailableError as exc:
            logger.warning(
                "Loomweave capability re-probe unreachable",
                extra={
                    "url": exc.url,
                    "cause_kind": exc.cause_kind,
                    "registry_backend": "loomweave",
                },
            )
            return None
        previous_instance_id = self.loomweave_instance_id
        self.loomweave_capabilities = capabilities
        self.loomweave_instance_id = capabilities["instance_id"]
        self.loomweave_api_version = capabilities["api_version"]
        if previous_instance_id is not None and previous_instance_id != capabilities["instance_id"]:
            self.loomweave_instance_rotated = True
            logger.warning(
                "Loomweave instance_id rotated mid-session; stored Loomweave file IDs may be stale",
                extra={
                    "previous_instance_id": previous_instance_id,
                    "current_instance_id": capabilities["instance_id"],
                    "loomweave_base_url": base_url_value,
                },
            )
        return capabilities

    def enable_local_registry_fallback(self) -> None:
        """Allow Loomweave projects to use local IDs only after Loomweave is unavailable."""
        if self.registry_backend != "loomweave":
            return
        self.allow_local_fallback = True
        if isinstance(self.registry, _LoomweaveLocalFallbackRegistry):
            return
        if not self.registry.is_displaced():
            msg = "Cannot enable local fallback for a non-displaced registry"
            raise ValueError(msg)
        self.registry = _LoomweaveLocalFallbackRegistry(
            self.registry,
            self._make_local_registry(),
            base_url=self._loomweave_base_url() or "",
        )

    @classmethod
    def from_store_dir(
        cls,
        store_dir: Path,
        *,
        project_root: Path,
        check_same_thread: bool = True,
        allow_local_fallback_override: bool | None = None,
    ) -> FiligreeDB:
        """Create a FiligreeDB from a confless store directory + explicit project root.

        The generalised opener for a dir-only (confless) anchor — legacy
        ``.filigree/`` or federation ``.weft/filigree/``. ``store_dir`` is where
        ``config.json`` and ``filigree.db`` live; ``project_root`` is passed
        explicitly (NOT inferred from ``store_dir.parent``, which is wrong for
        ``.weft/filigree/`` — two segments deep).

        When ``config.json`` is missing or omits the ``prefix`` key, fall back
        to ``project_root``'s own name rather than the hardcoded ``"filigree"``
        default. This mirrors what ``filigree init`` writes (prefix defaults to
        ``cwd.name``) and prevents a confless install from silently opening with
        the wrong identity.

        ``allow_local_fallback_override`` is the dashboard / CLI escape hatch
        for ADR-014 §7: an operator passing ``--allow-local-fallback`` at
        startup wants to override whatever ``allow_local_fallback`` is in the
        project's ``config.json``, so the capability probe at ``__init__`` time
        downgrades to a WARN instead of aborting when Loomweave is offline.
        """
        config = read_config(store_dir)
        configured_prefix = _raw_config_prefix(store_dir / CONFIG_FILENAME)
        prefix = configured_prefix if configured_prefix is not None else (project_root.name or "filigree")
        loomweave_config = _apply_allow_local_fallback_override(config.get("loomweave"), allow_local_fallback_override)
        db = cls(
            store_dir / DB_FILENAME,
            prefix=prefix,
            enabled_packs=config.get("enabled_packs"),
            check_same_thread=check_same_thread,
            project_root=project_root.resolve(),
            meta_dir=store_dir,
            registry_backend=config.get("registry_backend", "local"),
            loomweave_config=loomweave_config,
        )
        try:
            db.initialize()
        except BaseException:
            # ``initialize()`` opens the connection lazily on its first line
            # (``get_schema_version()`` → ``self.conn``). If it raises before
            # returning, the caller never receives ``db`` and so cannot close
            # the connection — close it here to avoid leaking the handle and
            # its WAL/SHM sidecar files.
            try:
                db.close()
            except Exception:
                logger.error("Failed to close database after from_store_dir initialize() failure", exc_info=True)
            raise
        return db

    @classmethod
    def from_filigree_dir(
        cls,
        filigree_dir: Path,
        *,
        check_same_thread: bool = True,
        allow_local_fallback_override: bool | None = None,
    ) -> FiligreeDB:
        """Create a FiligreeDB from an existing legacy ``.filigree/`` directory.

        Thin back-compat wrapper over :meth:`from_store_dir`: the legacy layout
        always has ``project_root == filigree_dir.parent``. Preserved verbatim so
        existing callers and tests keep working.
        """
        return cls.from_store_dir(
            filigree_dir,
            project_root=filigree_dir.resolve().parent,
            check_same_thread=check_same_thread,
            allow_local_fallback_override=allow_local_fallback_override,
        )

    @classmethod
    def from_conf(
        cls,
        conf_path: Path,
        *,
        store_dir: Path | None = None,
        check_same_thread: bool = True,
        allow_local_fallback_override: bool | None = None,
    ) -> FiligreeDB:
        """Create a FiligreeDB from a ``.filigree.conf`` anchor file (v2.0).

        Resolves the DB path relative to the conf file's directory (the conf's
        ``db`` field may relocate the database anywhere project-relative — the
        fg-da8d50 fix). ``store_dir`` is the resolved machine-owned metadata
        directory (``config.json``, runtime files); when omitted it is resolved
        via :func:`resolve_store_dir` so the ``enabled_packs`` fallback reads
        config from the right place (``.weft/filigree/`` or legacy ``.filigree/``),
        **independently** of where ``db`` points.

        ``allow_local_fallback_override`` — see :meth:`from_store_dir`.
        """
        data = read_conf(conf_path)
        db_path = (conf_path.parent / data["db"]).resolve()
        resolved_store_dir = store_dir if store_dir is not None else resolve_store_dir(conf_path.resolve().parent)
        prefix: str = data["prefix"]
        enabled_packs = data.get("enabled_packs")
        enabled_packs_from_project_config = False
        if enabled_packs is None:
            config = read_config(resolved_store_dir)
            enabled_packs = config.get("enabled_packs")
            enabled_packs_from_project_config = enabled_packs is not None
        loomweave_config = _apply_allow_local_fallback_override(data.get("loomweave"), allow_local_fallback_override)
        db = cls(
            db_path,
            prefix=prefix,
            enabled_packs=enabled_packs,
            check_same_thread=check_same_thread,
            project_root=conf_path.resolve().parent,
            meta_dir=resolved_store_dir,
            registry_backend=cast("RegistryBackend", data.get("registry_backend", "local")),
            loomweave_config=loomweave_config,
        )
        try:
            db.initialize()
            if enabled_packs_from_project_config:
                db._enabled_packs_override = None
        except BaseException:
            try:
                db.close()
            except Exception:
                logger.error("Failed to close database after from_conf initialize() failure", exc_info=True)
            raise
        return db

    @classmethod
    def from_anchor(
        cls,
        anchor: FiligreeAnchor,
        *,
        check_same_thread: bool = True,
        allow_local_fallback_override: bool | None = None,
    ) -> FiligreeDB:
        """Create a FiligreeDB from a resolved :class:`FiligreeAnchor`.

        The single open entry point for all runtime surfaces (CLI, dashboard,
        MCP, hooks). Reuses :meth:`from_conf` when a conf exists, else
        :meth:`from_store_dir` — both threaded with the anchor's resolved
        ``store_dir`` so every surface resolves to the same metadata dir (no
        split-brain).
        """
        if anchor.conf_path is not None:
            return cls.from_conf(
                anchor.conf_path,
                store_dir=anchor.store_dir,
                check_same_thread=check_same_thread,
                allow_local_fallback_override=allow_local_fallback_override,
            )
        return cls.from_store_dir(
            anchor.store_dir,
            project_root=anchor.project_root,
            check_same_thread=check_same_thread,
            allow_local_fallback_override=allow_local_fallback_override,
        )

    @classmethod
    def from_project(cls, project_path: Path | None = None) -> FiligreeDB:
        """Create a FiligreeDB by discovering the project anchor from *project_path* (or cwd).

        Walks up via :func:`find_filigree_anchor` so legacy installs (a bare
        dir with no conf yet) still open without requiring write access during
        discovery, then opens through the single :meth:`from_anchor` entry point.
        """
        return cls.from_anchor(find_filigree_anchor(project_path))

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

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
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

        Verifies the file at ``self.db_path`` is a filigree DB before touching it
        (catalog §6.8 errata): a foreign SQLite file at the same path raises
        :class:`ForeignSqliteFileError` instead of being silently overwritten.
        Schema-newer-than-installed raises ``SchemaVersionMismatchError`` from
        inside the classifier.
        """
        verdict = classify_and_stamp_filigree_db(self.conn, db_path=self.db_path)

        if verdict == "fresh":
            self.conn.executescript(SCHEMA_SQL)
            self.conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
        elif verdict in ("needs_upgrade", "legacy_needs_upgrade"):
            from filigree.migrations import apply_pending_migrations

            apply_pending_migrations(self.conn, CURRENT_SCHEMA_VERSION)
        # "current" — nothing to do.

        self._seed_templates()
        self._seed_future_release()
        self.conn.commit()
        self._warn_if_registry_backend_hybrid_state()

    def _warn_if_registry_backend_hybrid_state(self) -> None:
        """Warn when Loomweave config and stored file rows disagree.

        v17 backfills legacy ``file_records`` rows as ``registry_backend='local'``.
        A project can then switch its config to Loomweave without running
        ``migrate-registry``, leaving old rows under local identity while new
        implicit paths resolve through Loomweave. Startup should make that hybrid
        state visible without preventing read-only recovery commands.
        """
        if self.registry_backend != "loomweave" or self.allow_local_fallback:
            return
        try:
            local_count = int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM file_records WHERE registry_backend != ?",
                    ("loomweave",),
                ).fetchone()[0]
            )
        except sqlite3.Error:
            logger.warning(
                "file_registry_hybrid_state_check_failed",
                extra={"registry_backend": self.registry_backend, "db_path": str(self.db_path)},
                exc_info=True,
            )
            return
        if local_count:
            logger.warning(
                "file_registry_hybrid_state_detected",
                extra={
                    "registry_backend": self.registry_backend,
                    "local_file_records": local_count,
                    "db_path": str(self.db_path),
                },
            )

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
        try:
            self._close_registry()
        finally:
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
        try:
            self._close_registry()
        finally:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def _close_registry(self) -> None:
        # Borrowed clones (see ``borrow_for_worker_thread``) share the parent's
        # registry by reference and do not own it; closing such a clone — via
        # the context manager, ``close``, or ``__del__`` — must leave the
        # parent's Loomweave client open.
        if not self._owns_registry:
            return
        close_registry = getattr(self.registry, "close", None)
        if callable(close_registry):
            close_registry()

    def set_verified_actor(self, value: str | None) -> None:
        """Set the transport-verified identity for this session.

        Entry points (CLI ``get_db``, MCP ``_init_db``) construct the DB before
        resolving identity, then call this. Every subsequent runtime write
        stamps ``value`` into its ``verified_*`` column. ``None`` (the default)
        leaves writes unverified (``verified_* = NULL``).
        """
        self._verified_actor = value

    @contextlib.contextmanager
    def borrow_for_worker_thread(self) -> Iterator[FiligreeDB]:
        """Yield a short-lived sibling bound to its OWN sqlite connection.

        Dashboard handlers that run DB work on an asyncio worker thread
        (scan-results ingest, clean-stale sweep) must not touch the shared
        event-loop connection from that thread: concurrent cross-thread use of
        one ``sqlite3.Connection`` interleaves statements on the connection's
        single implicit transaction, so one writer's ``COMMIT``/``ROLLBACK``
        can land mid-transaction in another's (silent partial/lost writes).
        A separate connection per worker is what closes that race class (the
        CONTRACT-E follow-up). Worker/worker and worker/event-loop write
        contention is then mediated entirely by SQLite's own file locking
        (WAL admits one writer at a time; ``busy_timeout`` makes the loser wait
        rather than error) — no application-level lock is needed, and the
        worker paths run fully in parallel up to the brief write window.

        The clone shares all config, the Loomweave HTTP client, and the Loomweave
        capability-probe state by reference (read-only on the worker side, so
        no second ADR-014 probe runs) but lazily opens its OWN connection on
        first ``self.conn`` access. ``check_same_thread=True`` on the clone
        turns any stray cross-thread use of that connection into a loud
        ``ProgrammingError`` rather than silent interleaving. The shared
        Loomweave ``httpx.Client`` is safe for concurrent calls, so two worker
        clones may resolve against Loomweave at the same time.

        The local file-id factory is the one piece NOT shared verbatim: a
        ``LocalRegistry`` mints ids through the ``FiligreeDB`` it closed over,
        so the clone gets its factory rebound to itself
        (:meth:`_rebind_local_id_factory_to_self`) — otherwise worker-thread id
        minting in local (or Loomweave-fallback) mode would reach back into the
        parent's shared connection and raise ``SQLITE_MISUSE``.

        The clone does NOT own the registry: exiting the context tears down
        only the private connection (commit on clean exit, rollback on an
        in-flight transaction), never the shared Loomweave client.

        MUST be entered and exited entirely within the worker thread (i.e.
        inside the ``asyncio.to_thread`` callable) so the connection is
        opened, used, committed, and closed on one and the same thread.
        """
        clone = copy.copy(self)
        clone._conn = None
        clone._check_same_thread = True
        clone._owns_registry = False
        # The shared registry's local-id factory closes over the PARENT's
        # connection; rebind it to the clone so worker-thread file-id minting
        # uses the clone's private connection, not the shared one.
        clone._rebind_local_id_factory_to_self()
        try:
            yield clone
        finally:
            conn = clone._conn
            clone._conn = None
            if conn is not None:
                try:
                    if conn.in_transaction:
                        conn.rollback()
                    else:
                        conn.commit()
                finally:
                    conn.close()
