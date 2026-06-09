"""Hook logic for Claude Code SessionStart hooks.

Separated from CLI for testability. These functions are called by the
``filigree session-context`` and ``filigree ensure-dashboard`` CLI
subcommands, which in turn are registered as Claude Code hooks by
``filigree install --hooks``.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError

import portalocker

from filigree.core import (
    FiligreeDB,
    ForeignDatabaseError,
    find_filigree_anchor,
    find_filigree_command,
    get_mode,
)
from filigree.install import (
    FILIGREE_INSTRUCTIONS_MARKER,
    _instructions_hash,
    inject_instructions,
    install_codex_skills,
    install_skills,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session context
# ---------------------------------------------------------------------------

READY_CAP = 15
CONTEXT_TITLE_MAX_LEN = 160


def _sanitize_context_title(raw: str) -> str:
    """Sanitize issue titles for hook context output safety."""
    text = str(raw or "")
    # Collapse structural whitespace to prevent line-breaking/context injection.
    text = " ".join(text.replace("\r", " ").replace("\n", " ").replace("\t", " ").split())
    # Drop remaining non-printable control characters.
    text = "".join(ch for ch in text if ch.isprintable())
    if len(text) > CONTEXT_TITLE_MAX_LEN:
        text = text[: CONTEXT_TITLE_MAX_LEN - 3] + "..."
    return text or "(untitled)"


def _build_context(db: FiligreeDB, filigree_dir: Path | None = None) -> str:
    """Assemble the project snapshot string from a live DB handle."""
    lines: list[str] = []
    lines.append("=== Filigree Project Snapshot ===")
    lines.append("")

    # Dashboard URL — restart if idle-shutdown killed it
    if filigree_dir is not None:
        from filigree.ephemeral import read_pid_file, read_port_file, verify_pid_ownership

        port_file = filigree_dir / "ephemeral.port"
        pid_file = filigree_dir / "ephemeral.pid"
        port = read_port_file(port_file)
        pid_info = read_pid_file(pid_file)
        # ``verify_pid_ownership`` re-reads the PID file and checks OS-level
        # cmdline identity, so a recycled PID owned by another process is
        # rejected before we emit a misleading DASHBOARD URL
        # (filigree-aa38935c28).
        dashboard_alive = (
            port is not None
            and pid_info is not None
            and verify_pid_ownership(pid_file, expected_cmd="filigree", required_args=("dashboard",))
            and _is_port_listening(port)
        )
        if not dashboard_alive and (pid_info is not None or port is not None):
            # Dashboard was running but died (idle-shutdown, crash) — try to restart
            try:
                url = ensure_dashboard_running()
                if url:
                    lines.append(f"DASHBOARD: {url}")
                    lines.append("")
            except Exception:
                logger.warning("Dashboard auto-restart failed", exc_info=True)
        elif dashboard_alive:
            lines.append(f"DASHBOARD: http://localhost:{port}")
            lines.append("")

    # In-progress work
    in_progress = db.list_issues(status="in_progress")
    if in_progress:
        lines.append("IN PROGRESS (resume these):")
        for issue in in_progress:
            title = _sanitize_context_title(issue.title)
            lines.append(f'P{issue.priority} {issue.id} [{issue.type}] "{title}"')
        lines.append("")

    try:
        stale_claims = db.get_stale_claims()
        if stale_claims:
            lines.append(f"STALE CLAIMS ({len(stale_claims)} assigned issue(s) older than 48h or past lease expiry):")
            for issue in stale_claims[:READY_CAP]:
                title = _sanitize_context_title(issue.title)
                lines.append(f'P{issue.priority} {issue.id} [{issue.type}] "{title}" -> {issue.assignee}')
            if len(stale_claims) > READY_CAP:
                lines.append("  ... (truncated, run 'filigree stale-claims' for full list)")
            lines.append("")
    except (sqlite3.OperationalError, AttributeError):
        logger.debug("stale claim stats unavailable in session context", exc_info=True)

    # Ready to work
    ready = db.get_ready()
    if ready:
        shown = ready[:READY_CAP]
        lines.append(f"READY TO WORK ({len(ready)} tasks with no blockers):")
        for issue in shown:
            title = _sanitize_context_title(issue.title)
            lines.append(f'P{issue.priority} {issue.id} [{issue.type}] "{title}"')
        if len(ready) > READY_CAP:
            lines.append("  ... (truncated, run 'filigree ready' for full list)")
        lines.append("")

    # Critical path
    crit = db.get_critical_path()
    if crit:
        lines.append("CRITICAL PATH (unblocks the most downstream work):")
        lines.append(f"Critical path ({len(crit)} issues):")
        for i, item in enumerate(crit):
            prefix = "  -> " if i > 0 else "  "
            title = _sanitize_context_title(str(item["title"]))
            lines.append(f'{prefix}P{item["priority"]} {item["id"]} [{item["type"]}] "{title}"')
        lines.append("")

    # Stats
    stats = db.get_stats()
    ready_count = stats.get("ready_count", 0)
    blocked_count = stats.get("blocked_count", 0)
    lines.append(f"STATS: {ready_count} ready, {blocked_count} blocked")

    # Analyzer findings awareness (F2): surface un-bridged findings so orientation
    # never silently reads "nothing to do" while un-promoted findings sit in
    # scan_findings. The baselined/suppressed split keeps an already-accepted
    # defect from reading as actionable work. Honest-empty: omit when 0 unbridged.
    # Guarded for pre-findings DBs where scan_findings may not exist.
    try:
        fstats = db.unbridged_finding_stats()
        if fstats["total"] > 0:
            lines.append("")
            lines.append(
                f"ANALYZER FINDINGS: {fstats['total']} not yet bridged to the tracker "
                f"({fstats['actionable']} actionable, {fstats['suppressed']} baselined/suppressed) "
                f"— review with `finding_list`, bridge with `finding_promote`"
            )
    except sqlite3.OperationalError:
        logger.debug("finding stats unavailable in session context", exc_info=True)

    # Observation awareness (read-only, guarded for pre-v7 DBs)
    try:
        obs_stats = db.observation_stats(sweep=False)
        if obs_stats["count"] > 0:
            lines.append("")
            if obs_stats["stale_count"] > 0:
                lines.append(f"STALE OBSERVATIONS: {obs_stats['stale_count']} older than 48h — run `observation_list` to triage")
            else:
                lines.append(f"OBSERVATIONS: {obs_stats['count']} pending — run `observation_list` to review")
    except sqlite3.OperationalError:
        logger.debug("observation stats unavailable in session context", exc_info=True)

    return "\n".join(lines)


def _extract_marker_hash(content: str) -> str | None:
    """Extract the hash from a filigree instructions marker comment.

    Looks for ``<!-- filigree:instructions:v{VER}:{HASH} -->`` and returns
    the HASH portion, or ``None`` if the marker is missing or uses the old
    format without a hash.
    """
    import re

    m = re.search(r"<!-- filigree:instructions:v[^:]+:([0-9a-f]+) -->", content)
    return m.group(1) if m else None


def _check_instructions_freshness(project_root: Path) -> list[str]:
    """Check whether CLAUDE.md/AGENTS.md instructions and skills are current.

    Compares the hash embedded in the marker comment against the hash of
    the currently installed instructions template.  Updates stale files
    in-place and returns a list of human-readable status messages.
    """
    messages: list[str] = []
    current_hash = _instructions_hash()

    # Check CLAUDE.md and AGENTS.md
    for filename in ("CLAUDE.md", "AGENTS.md"):
        md_path = project_root / filename
        if not md_path.exists():
            continue
        content = md_path.read_text()
        if FILIGREE_INSTRUCTIONS_MARKER not in content:
            continue
        embedded_hash = _extract_marker_hash(content)
        if embedded_hash == current_hash:
            continue
        # Stale or old-format marker — update
        inject_instructions(md_path)
        messages.append(f"Updated filigree instructions in {filename}")

    # Check skill packs (Claude Code and Codex use different target dirs).
    # Hash the entire skill tree (relative path + bytes) rather than just
    # SKILL.md — the installed skill pack ships companion files under
    # ``examples/`` and ``references/`` that the freshness check would
    # otherwise leave stale (filigree-4bd71d94c3).
    from filigree.install import _get_skills_source_dir

    source_root = _get_skills_source_dir() / "filigree-workflow"
    if source_root.is_dir():
        source_hash = _skill_tree_fingerprint(source_root)

        skill_targets = [
            (
                project_root / ".claude" / "skills" / "filigree-workflow",
                install_skills,
                "Updated filigree skill pack",
            ),
            (
                project_root / ".agents" / "skills" / "filigree-workflow",
                install_codex_skills,
                "Updated filigree Codex skill pack",
            ),
        ]
        for target_root, installer, msg in skill_targets:
            if not target_root.is_dir():
                continue
            target_hash = _skill_tree_fingerprint(target_root)
            if target_hash != source_hash:
                installer(project_root)
                messages.append(msg)

    return messages


def _skill_tree_fingerprint(root: Path) -> str:
    """Return a short hash of every file under *root* (path + bytes).

    Sorted by relative POSIX path so the digest is stable across filesystems.
    Used to detect whether an installed skill pack matches the bundled source.
    """
    import hashlib

    digest = hashlib.sha256()
    files = sorted(p for p in root.rglob("*") if p.is_file())
    for path in files:
        rel = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(rel)
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            # Unreadable file ⇒ treat as "differs" by mixing in path-only
            # entropy; the installer will overwrite either way.
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()[:8]


def generate_session_context() -> str | None:
    """Generate a project snapshot for Claude Code session context.

    Also checks whether filigree instructions in CLAUDE.md/AGENTS.md
    and the skill pack are up-to-date with the installed package version.

    Returns ``None`` when there is no filigree project (silent exit).
    """
    try:
        anchor = find_filigree_anchor(include_legacy_dir=False)
    except ForeignDatabaseError as exc:
        # Surface the remediation message rather than swallowing it as a
        # silent "no project" exit (filigree-acbacc5b3e).
        return f"=== Filigree Project Snapshot ===\n\nWARNING: Refusing to open ancestor filigree database.\n\n{exc}"
    except FileNotFoundError:
        return None

    project_root = anchor.project_root
    filigree_dir = anchor.store_dir

    # Check instruction freshness (best-effort — don't let failures block context)
    freshness_messages: list[str] = []
    try:
        freshness_messages = _check_instructions_freshness(project_root)
    except (OSError, UnicodeDecodeError, ValueError):
        logger.warning("Instructions freshness check failed for %s", project_root, exc_info=True)

    try:
        db = FiligreeDB.from_anchor(anchor)
    except (sqlite3.Error, ValueError, OSError):
        logger.warning("Database init failed for %s", filigree_dir, exc_info=True)
        context = (
            f"=== Filigree Project Snapshot ===\n\n"
            f"WARNING: Could not open project database. Run `filigree doctor` to diagnose.\n"
            f"Project directory: {filigree_dir.parent}"
        )
        if freshness_messages:
            context += "\n\n" + "\n".join(freshness_messages)
        return context
    try:
        context = _build_context(db, filigree_dir)
    except sqlite3.Error:
        logger.warning("Database error building session context for %s", filigree_dir, exc_info=True)
        context = (
            f"=== Filigree Project Snapshot ===\n\n"
            f"WARNING: Could not read project database. Run `filigree doctor` to diagnose.\n"
            f"Project directory: {filigree_dir.parent}"
        )
    except Exception:
        logger.error("BUG: Unexpected error building session context for %s", filigree_dir, exc_info=True)
        context = (
            f"=== Filigree Project Snapshot ===\n\n"
            f"WARNING: Unexpected error building session context. Run `filigree doctor` to diagnose.\n"
            f"Project directory: {filigree_dir.parent}"
        )
    finally:
        db.close()

    # Append freshness messages so they appear in hook output
    if freshness_messages:
        context += "\n\n" + "\n".join(freshness_messages)

    return context


# ---------------------------------------------------------------------------
# Dashboard management
# ---------------------------------------------------------------------------


def _is_port_listening(port: int, host: str = "127.0.0.1") -> bool:
    """Check whether *port* is accepting connections on *host*."""
    if not (1 <= port <= 65535):
        return False
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        try:
            return sock.connect_ex((host, port)) == 0
        finally:
            sock.close()
    except (OSError, OverflowError):
        return False


def _find_agent_filigree_dir() -> Path:
    """Resolve the metadata dir for implicit agent startup hooks.

    Agent hooks run automatically when a coding agent opens a folder. The
    consent signal is a ``.filigree.conf`` anchor or a ``.weft/filigree/``
    store; a bare legacy ``.filigree/`` ancestor is NOT treated as consent to
    attach (``include_legacy_dir=False``). The ``.git``-boundary
    ``ForeignDatabaseError`` guard is preserved by ``find_filigree_anchor``.
    """
    return find_filigree_anchor(include_legacy_dir=False).store_dir


def ensure_dashboard_running(port: int | None = None) -> str:
    """Ensure the filigree dashboard is running.

    In ethereal mode (default): spawns a single-project dashboard on a
    deterministic port, with PID/port files in .filigree/.
    In server mode: just verifies the daemon is reachable.
    """
    try:
        filigree_dir = _find_agent_filigree_dir()
    except ForeignDatabaseError as exc:
        # Don't silently spawn a dashboard against an ancestor's database
        # (filigree-acbacc5b3e). ``ForeignDatabaseError`` subclasses
        # ``FileNotFoundError`` so the catch order matters.
        return f"Filigree dashboard: {exc}"
    except FileNotFoundError:
        return ""

    try:
        mode = get_mode(filigree_dir)
    except ValueError:
        logger.warning("Invalid mode in config, falling back to ethereal", exc_info=True)
        mode = "ethereal"

    if mode == "server":
        return _ensure_dashboard_server_mode(filigree_dir, port)

    return _ensure_dashboard_ethereal_mode(filigree_dir)


def _acquire_port(filigree_dir: Path) -> int:
    """Find an available port, falling back to deterministic port in sandboxed environments.

    Returns the port number on success, or raises ``RuntimeError`` on failure.
    """
    from filigree.ephemeral import compute_port, find_available_port

    try:
        return find_available_port(filigree_dir)
    except (OSError, RuntimeError) as exc:
        is_sandbox = isinstance(exc, PermissionError) or isinstance(exc.__cause__, PermissionError) or "Operation not permitted" in str(exc)
        if is_sandbox:
            port = compute_port(filigree_dir)
            logger.warning(
                "Port probe failed with permission error (%s); falling back to deterministic port %d",
                exc,
                port,
            )
            return port
        msg = f"Failed to choose dashboard port: {exc}"
        raise RuntimeError(msg) from exc


def _terminate_orphan_dashboard(pid: int, *, sigterm_grace: float = 2.0) -> None:
    """Best-effort terminate an owned dashboard whose port never came up.

    Signals SIGTERM, polls aliveness for ``sigterm_grace`` seconds, then
    falls back to SIGKILL. On Windows the terminate is best-effort via
    ``taskkill``. Errors are logged but never raised — leaking is preferable
    to crashing the start path.
    """
    from filigree.ephemeral import is_pid_alive

    if pid <= 0:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T"],
                capture_output=True,
                timeout=2.0,
                check=False,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        logger.warning("Failed to signal orphan dashboard pid %d", pid, exc_info=True)
        return

    deadline = time.monotonic() + sigterm_grace
    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            return
        time.sleep(0.1)

    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid), "/T"],
                capture_output=True,
                timeout=2.0,
                check=False,
            )
        else:
            os.kill(pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        logger.warning("Failed to SIGKILL orphan dashboard pid %d", pid, exc_info=True)


def _ensure_dashboard_ethereal_mode(filigree_dir: Path) -> str:
    """Ethereal mode: session-scoped dashboard on a deterministic port."""
    from filigree.ephemeral import (
        DASHBOARD_STARTUP_GRACE_SECONDS,
        cleanup_legacy_tmp_files,
        cleanup_stale_pid,
        read_pid_file,
        read_port_file,
        verify_pid_ownership,
        write_pid_file,
        write_port_file,
    )

    cleanup_legacy_tmp_files()

    pid_file = filigree_dir / "ephemeral.pid"
    port_file = filigree_dir / "ephemeral.port"
    lock_file = filigree_dir / "ephemeral.lock"

    def _probe_running_dashboard(*, allow_cleanup: bool) -> str | None:
        """Check if a usable dashboard is already running.

        When ``allow_cleanup`` is False (the pre-lock probe), this is purely
        read-only: a stale or non-listening record reports "not usable" but
        does not delete or terminate anything, so a concurrent peer's freshly
        written metadata cannot be erased outside the lock
        (filigree-48215e8343).

        When ``allow_cleanup`` is True (under the lock), this also reaps
        owned-but-non-listening dashboards whose startup grace expired —
        ``cleanup_stale_pid`` would otherwise leave them in place because
        the PID is still ours, leaking the old process when we respawn
        (filigree-fd3ac0feec).
        """
        pid_info = read_pid_file(pid_file)
        existing_port = read_port_file(port_file)
        if not pid_info or not existing_port:
            return None
        if not verify_pid_ownership(pid_file, expected_cmd="filigree", required_args=("dashboard",)):
            if allow_cleanup:
                pid_file.unlink(missing_ok=True)
                port_file.unlink(missing_ok=True)
            return None
        if _is_port_listening(existing_port):
            return f"Filigree dashboard running on http://localhost:{existing_port}"
        # PID is alive and ours, but the port isn't accepting yet. If we're
        # inside the startup grace window, treat the dashboard as "starting"
        # rather than respawning a competing process
        # (filigree-ea2a1959e1). Outside the window, fall through so the
        # caller can clean up and respawn.
        startup_ts = pid_info.get("startup_ts")
        if isinstance(startup_ts, (int, float)) and time.time() - startup_ts < DASHBOARD_STARTUP_GRACE_SECONDS:
            return f"Filigree dashboard starting on http://localhost:{existing_port} (initializing)"
        if allow_cleanup:
            _terminate_orphan_dashboard(pid_info["pid"])
            pid_file.unlink(missing_ok=True)
            port_file.unlink(missing_ok=True)
        return None

    # Pre-lock probe is read-only — a concurrent starter that races between
    # our read and our verify must never have its fresh metadata deleted
    # outside the lock (filigree-48215e8343). The destructive cleanup
    # (`cleanup_stale_pid`, the unlink/terminate paths) all run under the
    # lock below.
    running_message = _probe_running_dashboard(allow_cleanup=False)
    if running_message:
        return running_message

    # Atomic start with lock
    lock_fd = None
    try:
        lock_fd = open(lock_file, "w")  # noqa: SIM115
        try:
            portalocker.lock(lock_fd, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except (OSError, portalocker.LockException):
            return "Filigree dashboard: another session is starting it, skipping"

        # Now under the lock — clean up stale or orphaned state, then re-probe
        # in case a peer started a dashboard between our pre-lock probe and
        # acquiring the lock.
        cleanup_stale_pid(pid_file)
        running_message = _probe_running_dashboard(allow_cleanup=True)
        if running_message:
            return running_message

        try:
            port = _acquire_port(filigree_dir)
        except RuntimeError as exc:
            return str(exc)
        filigree_cmd = find_filigree_command()

        log_file = filigree_dir / "ephemeral.log"
        try:
            with open(log_file, "w") as log_fd:
                proc = subprocess.Popen(
                    [*filigree_cmd, "dashboard", "--no-browser", "--port", str(port)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log_fd,
                    start_new_session=True,
                )
        except OSError as exc:
            return f"Failed to start dashboard: {exc}"

        # If metadata writes fail after Popen, the child is already detached
        # (``start_new_session=True``) — terminate it and clean up rather than
        # leak an untracked dashboard (filigree-89e7a1c833).
        try:
            write_pid_file(pid_file, proc.pid, cmd="filigree dashboard", port=port)
            write_port_file(port_file, port)
        except OSError as exc:
            logger.warning(
                "Failed to persist dashboard metadata for pid %d; terminating orphan",
                proc.pid,
                exc_info=True,
            )
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        logger.error("Orphan dashboard pid %d did not exit after SIGKILL", proc.pid)
            except OSError:
                logger.warning("Failed to terminate orphan dashboard pid %d", proc.pid, exc_info=True)
            pid_file.unlink(missing_ok=True)
            port_file.unlink(missing_ok=True)
            return f"Failed to record dashboard metadata: {exc}"

        # Wait for startup
        for _ in range(10):
            time.sleep(0.3)
            exit_code = proc.poll()
            if exit_code is not None:
                pid_file.unlink(missing_ok=True)
                port_file.unlink(missing_ok=True)
                stderr_output = log_file.read_text().strip()
                detail = f": {stderr_output}" if stderr_output else ""
                return f"Dashboard process exited (pid {proc.pid}, code {exit_code}){detail}"
            if _is_port_listening(port):
                return f"Started Filigree dashboard on http://localhost:{port}"

        return f"Started Filigree dashboard on http://localhost:{port} (may still be initializing)"
    finally:
        if lock_fd is not None:
            lock_fd.close()


def _ensure_dashboard_server_mode(filigree_dir: Path, port: int | None) -> str:
    """Server mode: register this project, then notify the daemon to reload.

    1. ``register_project()`` is idempotent and lock-protected.
    2. If the daemon is reachable, POST ``/api/reload`` so it picks up
       the (possibly new) registration.  Uses a 2-second timeout so
       session startup isn't blocked by a slow daemon.
    """
    from filigree.server import read_server_config, register_project

    daemon_port = port if port is not None else read_server_config().port

    try:
        register_project(filigree_dir)
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        logger.warning("Failed to register project in server.json", exc_info=True)
        return f"Filigree server registration failed: {exc}"

    if not _is_port_listening(daemon_port):
        return f"Filigree server not running on port {daemon_port}. Start it with: filigree server start"

    # Notify daemon to reload project list
    reload_warning = ""
    try:
        import urllib.request

        req = urllib.request.Request(
            f"http://127.0.0.1:{daemon_port}/api/reload",
            method="POST",
            data=b"",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2):  # noqa: S310
            pass
    except (URLError, TimeoutError, OSError) as exc:
        logger.warning("Failed to POST /api/reload to daemon: %s", exc, exc_info=True)
        reload_warning = f" (reload failed: {type(exc).__name__})"

    return f"Filigree server running on http://localhost:{daemon_port}{reload_warning}"
