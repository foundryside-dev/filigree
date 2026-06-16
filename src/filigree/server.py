"""Server mode configuration and daemon management.

Handles the persistent multi-project daemon for server installation mode.
Config lives at $HOME/.config/filigree/server.json (see SERVER_CONFIG_DIR).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

import portalocker

from filigree.core import (
    CONF_FILENAME,
    DB_FILENAME,
    WEFT_DIR_NAME,
    WEFT_MEMBER_SUBDIR,
    ProjectNotInitialisedError,
    find_filigree_anchor,
    read_conf,
    read_config,
    write_atomic,
)
from filigree.db_schema import CURRENT_SCHEMA_VERSION
from filigree.ephemeral import is_pid_alive, read_pid_file, verify_pid_ownership, write_pid_file

logger = logging.getLogger(__name__)

SERVER_CONFIG_DIR = Path.home() / ".config" / "filigree"
SERVER_CONFIG_FILE = SERVER_CONFIG_DIR / "server.json"
SERVER_PID_FILE = SERVER_CONFIG_DIR / "server.pid"

DEFAULT_PORT = 8377
SUPPORTED_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION

# Expected process identity for verify_pid_ownership calls.
_DAEMON_CMD = "filigree"
_DAEMON_ARGS = ("dashboard", "--server-mode")


class ProjectEntry(TypedDict):
    """Typed structure for a registered project in server.json."""

    prefix: str


@dataclass
class ServerConfig:
    port: int = DEFAULT_PORT
    projects: dict[str, ProjectEntry] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (1 <= self.port <= 65535):
            raise ValueError(f"port must be between 1 and 65535, got {self.port}")


def _project_root_from_store_dir(store_dir: Path) -> Path:
    """Return the project root for a registered store dir via the canonical anchor.

    The project root is NOT recoverable from the store dir alone for an
    arbitrary-depth weft.toml ``store_dir`` override; stripping a fixed number of
    segments (the old ``.weft/filigree`` 2-seg / legacy 1-seg special-case) gives
    the wrong root. Resolve through :func:`find_filigree_anchor`, which reads
    weft.toml (I2). Fall back to the segment heuristic only when no anchor can be
    discovered (e.g. a registered dir whose anchor was removed) so a schema-version
    probe stays best-effort rather than raising during registration/listing.
    """
    try:
        return find_filigree_anchor(store_dir).project_root
    except ProjectNotInitialisedError:
        if store_dir.name == WEFT_MEMBER_SUBDIR and store_dir.parent.name == WEFT_DIR_NAME:
            return store_dir.parent.parent
        return store_dir.parent


def _project_db_path(filigree_dir: Path) -> Path:
    conf_path = _project_root_from_store_dir(filigree_dir) / CONF_FILENAME
    if conf_path.is_file():
        conf = read_conf(conf_path)
        db_name: str = conf["db"]
        return (conf_path.parent / db_name).resolve()
    return filigree_dir / DB_FILENAME


def _read_project_schema_version(filigree_dir: Path) -> int:
    """Return the SQLite schema version for a project, or 0 if no DB exists.

    Returns 0 when no database file exists or when the DB is transiently locked
    (another process holds an exclusive lock — common during concurrent writes).
    Raises sqlite3.DatabaseError or OSError for genuine corruption or
    permission errors.
    """
    db_path = _project_db_path(filigree_dir)
    if not db_path.exists():
        return 0

    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        # Transient: "database is locked", "database is busy", disk I/O.
        # Treat as version 0 so registration proceeds — the next session
        # start will re-read successfully once the lock is released.
        logger.debug("Could not read schema version from %s (transient lock), assuming 0", db_path, exc_info=True)
        return 0
    finally:
        conn.close()


def _backup_corrupt_config() -> None:
    """Back up a corrupt server.json before callers overwrite it with defaults."""
    backup_path = SERVER_CONFIG_FILE.parent / (SERVER_CONFIG_FILE.name + ".bak")
    try:
        shutil.copy2(SERVER_CONFIG_FILE, backup_path)
    except OSError:
        logger.debug("Could not back up corrupt config to %s", backup_path, exc_info=True)


def read_server_config() -> ServerConfig:
    """Read server.json. Returns defaults if missing or invalid."""
    if not SERVER_CONFIG_FILE.exists():
        return ServerConfig()
    try:
        data = json.loads(SERVER_CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        _backup_corrupt_config()
        logger.warning("Corrupt server config %s: %s — backed up to .bak", SERVER_CONFIG_FILE, exc)
        return ServerConfig()

    if not isinstance(data, dict):
        _backup_corrupt_config()
        logger.warning("Server config %s is not a JSON object; backed up to .bak, using defaults", SERVER_CONFIG_FILE)
        return ServerConfig()

    # Coerce port
    raw_port = data.get("port", DEFAULT_PORT)
    if isinstance(raw_port, bool):
        logger.warning("Invalid port value %r in server config; using default %d", raw_port, DEFAULT_PORT)
        port = DEFAULT_PORT
    elif isinstance(raw_port, int):
        port = raw_port
    elif isinstance(raw_port, str):
        try:
            port = int(raw_port)
        except ValueError:
            logger.warning("Invalid port value %r in server config; using default %d", raw_port, DEFAULT_PORT)
            port = DEFAULT_PORT
    else:
        logger.warning("Invalid port value %r in server config; using default %d", raw_port, DEFAULT_PORT)
        port = DEFAULT_PORT
    if not (1 <= port <= 65535):
        logger.warning("Port %d out of range (1-65535) in server config; using default %d", port, DEFAULT_PORT)
        port = DEFAULT_PORT

    # Coerce projects — must be dict of dicts with string prefixes.
    raw_projects = data.get("projects", {})
    if not isinstance(raw_projects, dict):
        raw_projects = {}
    projects: dict[str, ProjectEntry] = {}
    for key, value in raw_projects.items():
        if not isinstance(value, dict):
            continue
        prefix = value.get("prefix")
        if not isinstance(prefix, str):
            logger.warning("Invalid project prefix %r in server config; dropping project %r", prefix, key)
            continue
        projects[str(key)] = {"prefix": prefix}

    return ServerConfig(port=port, projects=projects)


def write_server_config(config: ServerConfig) -> None:
    """Write server.json atomically."""
    SERVER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        {"port": config.port, "projects": config.projects},
        indent=2,
    )
    write_atomic(SERVER_CONFIG_FILE, content + "\n")


def register_project(filigree_dir: Path) -> None:
    """Register a project in server.json.

    Uses ``portalocker.lock`` around the read-modify-write to prevent
    concurrent sessions from losing each other's registrations.

    Raises:
        ValueError: another project already uses the same prefix, or
            the database schema version is newer than supported.
        sqlite3.DatabaseError: the database file is genuinely corrupt.
        OSError: permission or filesystem error reading the database.
    """
    filigree_dir = filigree_dir.resolve()
    project_config = read_config(filigree_dir)

    # Version enforcement must read the database schema, not config.json.
    schema_version = _read_project_schema_version(filigree_dir)
    if schema_version > SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"Project schema version {schema_version} is newer than supported "
            f"version {SUPPORTED_SCHEMA_VERSION}. Upgrade filigree to manage this project."
        )

    SERVER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = SERVER_CONFIG_DIR / "server.lock"
    try:
        lock_fd = open(lock_path, "w")  # noqa: SIM115
    except OSError as e:
        raise RuntimeError(f"Cannot create lock file at {lock_path}: {e}. Check permissions on {SERVER_CONFIG_DIR}.") from e
    with lock_fd:
        portalocker.lock(lock_fd, portalocker.LOCK_EX)
        config = read_server_config()
        project_key = str(filigree_dir)
        prefix = str(project_config.get("prefix", "filigree"))
        new_root = _project_root_from_store_dir(filigree_dir).resolve()

        # Entries are keyed by store-dir string, but a project's IDENTITY is its
        # root. A store relocation (.filigree -> .weft/filigree) re-registers
        # under a new store dir while the old key lingers with the same prefix —
        # which the collision check below would otherwise read as a conflict and
        # refuse the move (filigree-a4925b59bb). Dedup by root: any existing entry
        # resolving to the same project root is the same project's stale key, so
        # drop it instead of colliding. (A running daemon's in-memory
        # project_store is not reconciled here — server.json is the durable record
        # this fixes; the daemon picks the change up on its next reload.)
        stale_keys: list[str] = []
        for existing_path, meta in config.projects.items():
            if existing_path == project_key:
                continue  # idempotent re-register (exact same store dir)
            if _project_root_from_store_dir(Path(existing_path)).resolve() == new_root:
                stale_keys.append(existing_path)  # same project, relocated store
                continue
            existing_prefix = str(meta.get("prefix", "filigree")) if isinstance(meta, dict) else "filigree"
            if existing_prefix == prefix:
                raise ValueError(
                    f"Prefix collision: {prefix!r} already registered by {existing_path}. Choose a unique prefix in .filigree/config.json."
                )

        for stale_key in stale_keys:
            del config.projects[stale_key]
        config.projects[project_key] = {"prefix": prefix}
        write_server_config(config)


def unregister_project(filigree_dir: Path) -> None:
    """Remove a project from server.json."""
    filigree_dir = filigree_dir.resolve()
    SERVER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = SERVER_CONFIG_DIR / "server.lock"
    try:
        lock_fd = open(lock_path, "w")  # noqa: SIM115
    except OSError as e:
        raise RuntimeError(f"Cannot create lock file at {lock_path}: {e}. Check permissions on {SERVER_CONFIG_DIR}.") from e
    with lock_fd:
        portalocker.lock(lock_fd, portalocker.LOCK_EX)
        config = read_server_config()
        config.projects.pop(str(filigree_dir), None)
        write_server_config(config)


def unregister_projects(project_keys: Iterable[str]) -> set[str]:
    """Remove several projects from server.json in a single locked pass.

    Unlike :func:`unregister_project`, the keys are matched *exactly* against
    the stored ``projects`` map — they are deliberately **not** re-``resolve()``d.
    Callers (notably ``doctor --fix`` cleaning up vanished-directory entries)
    already hold the exact config keys, and re-resolving a now-missing path can
    diverge from the string that was recorded at registration time, leaving the
    stale entry stranded.

    Returns the set of keys that were actually present and removed (so callers
    can report precisely what changed and tell a real removal from a no-op).
    """
    wanted = set(project_keys)
    if not wanted:
        return set()
    SERVER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = SERVER_CONFIG_DIR / "server.lock"
    try:
        lock_fd = open(lock_path, "w")  # noqa: SIM115
    except OSError as e:
        raise RuntimeError(f"Cannot create lock file at {lock_path}: {e}. Check permissions on {SERVER_CONFIG_DIR}.") from e
    removed: set[str] = set()
    with lock_fd:
        portalocker.lock(lock_fd, portalocker.LOCK_EX)
        config = read_server_config()
        for key in wanted:
            if config.projects.pop(key, None) is not None:
                removed.add(key)
        if removed:
            write_server_config(config)
    return removed


# ---------------------------------------------------------------------------
# Daemon lifecycle
# ---------------------------------------------------------------------------


@dataclass
class DaemonResult:
    success: bool
    message: str


@dataclass
class DaemonStatus:
    running: bool
    pid: int | None = None
    port: int | None = None
    project_count: int = 0

    def __post_init__(self) -> None:
        if self.running and (self.pid is None or self.port is None):
            raise ValueError("DaemonStatus with running=True requires both pid and port")


def start_daemon(port: int | None = None) -> DaemonResult:
    """Start the filigree server daemon."""
    from filigree.core import find_filigree_command

    SERVER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = SERVER_CONFIG_DIR / "server.lock"
    with open(lock_path, "w") as lock_fd:
        portalocker.lock(lock_fd, portalocker.LOCK_EX)

        # Check if already running — verify PID is actually a filigree process
        info = read_pid_file(SERVER_PID_FILE)
        if info and is_pid_alive(info["pid"]):
            if verify_pid_ownership(SERVER_PID_FILE, expected_cmd=_DAEMON_CMD, required_args=_DAEMON_ARGS):
                return DaemonResult(False, f"Daemon already running (pid {info['pid']})")
            # Stale PID from a reused process — clean up and proceed
            logger.warning("Stale PID file (pid %d is not filigree); cleaning up", info["pid"])
            SERVER_PID_FILE.unlink(missing_ok=True)

        config = read_server_config()
        daemon_port = port or config.port
        # Persist the effective daemon port so status/hooks/install agree.
        if config.port != daemon_port:
            config.port = daemon_port
            write_server_config(config)

        filigree_cmd = find_filigree_command()
        log_file = SERVER_CONFIG_DIR / "server.log"

        try:
            with open(log_file, "a") as log_fd:
                proc = subprocess.Popen(
                    [*filigree_cmd, "dashboard", "--no-browser", "--server-mode", "--port", str(daemon_port)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log_fd,
                    start_new_session=True,
                )
        except OSError as exc:
            return DaemonResult(False, f"Failed to start daemon: {exc}")

        write_pid_file(SERVER_PID_FILE, proc.pid, cmd="filigree dashboard --server-mode", port=daemon_port)
        # Lock released here — critical section is complete

    # Post-startup health check runs outside the lock to avoid blocking
    # concurrent register_project() calls
    time.sleep(0.5)
    exit_code = proc.poll()
    if exit_code is not None:
        SERVER_PID_FILE.unlink(missing_ok=True)
        stderr = log_file.read_text().strip()
        return DaemonResult(False, f"Daemon exited immediately (code {exit_code}): {stderr}")

    return DaemonResult(True, f"Started filigree daemon (pid {proc.pid}) on port {daemon_port}")


def stop_daemon() -> DaemonResult:
    """Stop the filigree server daemon."""
    SERVER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = SERVER_CONFIG_DIR / "server.lock"
    with open(lock_path, "w") as lock_fd:
        portalocker.lock(lock_fd, portalocker.LOCK_EX)
        return _stop_daemon_locked()


def _stop_daemon_locked() -> DaemonResult:
    """Stop daemon while caller holds ``server.lock``."""
    info = read_pid_file(SERVER_PID_FILE)
    if info is None:
        return DaemonResult(False, "No PID file found — daemon may not be running")

    pid = info["pid"]
    if not is_pid_alive(pid):
        SERVER_PID_FILE.unlink(missing_ok=True)
        return DaemonResult(True, f"Daemon (pid {pid}) was not running; cleaned up PID file")

    if not verify_pid_ownership(SERVER_PID_FILE, expected_cmd=_DAEMON_CMD, required_args=_DAEMON_ARGS):
        return DaemonResult(False, f"PID {pid} is not a filigree process — refusing to kill")

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        SERVER_PID_FILE.unlink(missing_ok=True)
        return DaemonResult(True, f"Daemon (pid {pid}) exited before SIGTERM; cleaned up PID file")
    except PermissionError:
        return DaemonResult(False, f"Permission denied sending SIGTERM to pid {pid}")

    # Wait for process to exit
    for _ in range(50):
        time.sleep(0.1)
        if not is_pid_alive(pid):
            SERVER_PID_FILE.unlink(missing_ok=True)
            return DaemonResult(True, f"Stopped filigree daemon (pid {pid})")

    # Escalate to SIGKILL
    try:
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.2)
    except ProcessLookupError:
        # Process died between last check and SIGKILL — success
        SERVER_PID_FILE.unlink(missing_ok=True)
        return DaemonResult(True, f"Force-killed filigree daemon (pid {pid})")
    except PermissionError:
        return DaemonResult(False, f"Permission denied sending SIGKILL to pid {pid}")

    # Verify the process is actually dead
    if is_pid_alive(pid):
        # Still clean up PID file even when SIGKILL fails, to prevent a permanently stuck state
        SERVER_PID_FILE.unlink(missing_ok=True)
        logger.warning("Failed to kill daemon (pid %d) even with SIGKILL", pid)
        return DaemonResult(False, f"Failed to kill daemon (pid {pid}) even with SIGKILL")

    SERVER_PID_FILE.unlink(missing_ok=True)
    return DaemonResult(True, f"Force-killed filigree daemon (pid {pid})")


def daemon_status() -> DaemonStatus:
    """Check daemon status."""
    info = read_pid_file(SERVER_PID_FILE)
    if info is None or not is_pid_alive(info["pid"]):
        return DaemonStatus(running=False)

    # Verify PID belongs to filigree before reporting as running
    if not verify_pid_ownership(SERVER_PID_FILE, expected_cmd=_DAEMON_CMD, required_args=_DAEMON_ARGS):
        return DaemonStatus(running=False)

    config = read_server_config()
    return DaemonStatus(
        running=True,
        pid=info["pid"],
        port=config.port,
        project_count=len(config.projects),
    )


def claim_current_process_as_daemon(*, port: int | None = None) -> bool:
    """Track the current process as the server daemon.

    Returns ``True`` when the current process owns (or successfully claimed)
    ``server.pid``. Returns ``False`` if a different live process is already
    tracked.

    Uses ``portalocker.lock`` to serialise with ``start_daemon()`` and
    ``register_project()``, preventing two callers from simultaneously
    reading an empty PID file and both writing their own PID.
    """
    current_pid = os.getpid()
    SERVER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = SERVER_CONFIG_DIR / "server.lock"
    with open(lock_path, "w") as lock_fd:
        portalocker.lock(lock_fd, portalocker.LOCK_EX)

        info = read_pid_file(SERVER_PID_FILE)
        if info is not None:
            tracked_pid = info["pid"]
            if tracked_pid == current_pid:
                if port is not None:
                    config = read_server_config()
                    if config.port != port:
                        config.port = port
                        write_server_config(config)
                return True
            if is_pid_alive(tracked_pid):
                if verify_pid_ownership(
                    SERVER_PID_FILE,
                    expected_cmd="filigree",
                    required_args=("dashboard", "--server-mode"),
                ):
                    return False
                # Stale PID from a reused process — clean up and proceed
                logger.warning("Stale PID file (pid %d is not filigree); cleaning up", tracked_pid)
            SERVER_PID_FILE.unlink(missing_ok=True)

        claim_port = port if port is not None else read_server_config().port
        write_pid_file(SERVER_PID_FILE, current_pid, cmd="filigree dashboard --server-mode", port=claim_port)
        if port is not None:
            config = read_server_config()
            if config.port != port:
                config.port = port
                write_server_config(config)
        return True


def release_daemon_pid_if_owned(pid: int) -> None:
    """Remove ``server.pid`` only if it currently points at *pid*.

    Uses rename-to-temp, check, then delete-or-restore to narrow the TOCTOU
    window between checking ownership and removing the file.
    """
    try:
        tmp = SERVER_PID_FILE.with_suffix(".pid.removing")
        SERVER_PID_FILE.rename(tmp)
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("Could not prepare PID file for release; leaving it in place", exc_info=True)
        return
    info = read_pid_file(tmp)
    if info is not None and info["pid"] == pid:
        tmp.unlink(missing_ok=True)
    else:
        # Not ours — restore
        try:
            tmp.rename(SERVER_PID_FILE)
        except OSError:
            logger.warning("Could not restore PID file for another process; removing temp")
            tmp.unlink(missing_ok=True)
