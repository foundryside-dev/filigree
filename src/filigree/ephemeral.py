"""Ephemeral (session-scoped) dashboard lifecycle.

Handles deterministic port selection, PID tracking, and stale process cleanup
for the ethereal installation mode. (The 'ephemeral' module name predates the
mode rename to 'ethereal'.)
"""

from __future__ import annotations

import hashlib
import json as _json
import logging
import math
import os
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import NotRequired, TypedDict

logger = logging.getLogger(__name__)


class PidInfo(TypedDict):
    """Typed structure returned by :func:`read_pid_file`."""

    pid: int
    cmd: str
    # Port the process was launched on, when recorded. Used by
    # ``verify_pid_ownership`` to distinguish this project's dashboard from
    # another filigree project's after PID recycling on the same host.
    port: NotRequired[int | None]
    # Wall-clock seconds when the PID was first written. Used to gate
    # respawn during the startup window (filigree-ea2a1959e1).
    startup_ts: NotRequired[float | None]


PORT_BASE = 8400
PORT_RANGE = 1000
PORT_RETRIES = 5
# Grace period after write_pid_file during which callers should treat a live
# process whose port isn't yet listening as "starting" rather than "stuck."
DASHBOARD_STARTUP_GRACE_SECONDS = 30.0


def _tokens_contain_args(tokens: list[str], required_args: tuple[str, ...]) -> bool:
    """Return True when *required_args* appear in-order within *tokens*."""
    if not required_args:
        return True
    pos = 0
    lowered = [tok.strip().lower() for tok in tokens]
    for token in lowered:
        if token == required_args[pos]:
            pos += 1
            if pos == len(required_args):
                return True
    return False


def _expected_process_arg_candidates(tokens: list[str], *, expected_cmd: str) -> list[list[str]]:
    """Return argv tails that follow a matching expected process command."""
    if not tokens:
        return []

    expected = expected_cmd.lower()
    candidates: list[list[str]] = []

    executable = Path(tokens[0]).name.lower()
    exe_stem = Path(executable).stem  # strip .exe / .cmd / .bat
    if executable == expected or exe_stem == expected:
        candidates.append(tokens[1:])

    if len(tokens) > 1:
        if tokens[1] == "-m" and len(tokens) > 2:
            module_name = tokens[2].strip().lower()
            module_root = module_name.split(".", 1)[0]
            if module_name == expected or module_root == expected:
                candidates.append(tokens[3:])
        else:
            first_arg = Path(tokens[1]).name.lower()
            first_arg_stem = Path(first_arg).stem
            if first_arg == expected or first_arg_stem == expected:
                candidates.append(tokens[2:])

    return candidates


def _effective_option_value(tokens: list[str], option: str) -> str | None:
    """Return the last value for a repeated argv option, matching common CLI parsing."""
    option_lower = option.lower()
    prefix = f"{option_lower}="
    effective: str | None = None
    i = 0
    while i < len(tokens):
        token = tokens[i].strip()
        lowered = token.lower()
        if lowered == option_lower:
            effective = tokens[i + 1].strip() if i + 1 < len(tokens) else None
            i += 2
            continue
        if lowered.startswith(prefix):
            effective = token[len(option) + 1 :].strip()
        i += 1
    return effective


def _matches_expected_process(tokens: list[str], *, expected_cmd: str, required_args: tuple[str, ...] = ()) -> bool:
    """Return True when argv tokens identify the expected process shape."""
    required_args = tuple(arg.strip().lower() for arg in required_args)
    candidates = _expected_process_arg_candidates(tokens, expected_cmd=expected_cmd)
    return any(_tokens_contain_args(candidate, required_args) for candidate in candidates)


def compute_port(filigree_dir: Path) -> int:
    """Deterministic port from project path: 8400 + hash(path) % 1000."""
    h = hashlib.sha256(str(filigree_dir.resolve()).encode()).hexdigest()
    return PORT_BASE + (int(h, 16) % PORT_RANGE)


def _is_port_free(port: int) -> bool:
    """Check whether a port is available for binding."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        finally:
            sock.close()
    except OSError:
        return False


def find_available_port(filigree_dir: Path) -> int:
    """Find an available port, starting with the deterministic one.

    Tries the deterministic port first, then up to PORT_RETRIES sequential
    ports, then falls back to OS-assigned (port 0).

    Note: There is a small TOCTOU race between checking port availability
    and the subprocess binding to it. This is acceptable because: (1) the
    deterministic port makes collisions rare, (2) uvicorn will fail-fast
    with a clear "address in use" error, and (3) the caller can retry.
    """
    base = compute_port(filigree_dir)
    for offset in range(PORT_RETRIES + 1):
        candidate = base + offset
        if candidate >= 65536:
            break
        if _is_port_free(candidate):
            return candidate

    # Fallback: OS-assigned
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            port: int = sock.getsockname()[1]
        finally:
            sock.close()
    except OSError as exc:
        raise RuntimeError(f"Cannot allocate a local dashboard port: {exc}") from exc
    return port


# ---------------------------------------------------------------------------
# PID lifecycle
# ---------------------------------------------------------------------------


def write_pid_file(pid_file: Path, pid: int, *, cmd: str = "filigree", port: int | None = None) -> None:
    """Write PID + process identity to file (JSON format, atomic).

    When *port* is supplied it is embedded in the record so that
    :func:`verify_pid_ownership` can confirm the live process is serving the
    same port this project recorded — the identifying detail that separates
    one filigree project's dashboard from another's after PID recycling.
    """
    from filigree.core import write_atomic

    payload: dict[str, object] = {"pid": pid, "cmd": cmd, "startup_ts": time.time()}
    if port is not None:
        payload["port"] = int(port)
    content = _json.dumps(payload)
    write_atomic(pid_file, content)


# PID upper bound: tolerate any plausible kernel value (Linux pid_max can be
# raised to 2**22; some BSDs allow more). 2**31 - 1 is wide enough for any
# documented kernel and still rejects pathological JSON.
_PID_MAX = 2**31 - 1


def _coerce_pos_int(raw: object, *, lo: int, hi: int) -> int | None:
    """Strictly coerce a JSON value to an int in [lo, hi].

    Rejects ``bool`` (int subclass), ``float`` (truncation hides bugs and
    non-finite values raise ``OverflowError``), and any string that is not a
    plain non-negative decimal. Returns None if the value cannot be safely
    interpreted as an integer in range.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if lo <= raw <= hi else None
    if isinstance(raw, str):
        s = raw.strip()
        if not s or not s.isdigit():
            return None
        try:
            v = int(s)
        except ValueError:
            return None
        return v if lo <= v <= hi else None
    return None


def read_pid_file(pid_file: Path) -> PidInfo | None:
    """Read PID info from file. Returns None if missing or corrupt.

    Supports both JSON format (new) and plain integer (legacy).
    """
    if not pid_file.exists():
        return None
    try:
        text = pid_file.read_text().strip()
        # Try JSON first (new format)
        try:
            data = _json.loads(text)
            if isinstance(data, dict) and "pid" in data:
                pid = _coerce_pos_int(data["pid"], lo=1, hi=_PID_MAX)
                if pid is None:
                    logger.warning("PID file %s: invalid pid value %r", pid_file, data["pid"])
                    return None
                info: PidInfo = {"pid": pid, "cmd": data.get("cmd", "unknown")}
                raw_port = data.get("port")
                if raw_port is not None:
                    port_val = _coerce_pos_int(raw_port, lo=1, hi=65535)
                    if port_val is not None:
                        info["port"] = port_val
                    else:
                        logger.debug("PID file %s: ignoring invalid port %r", pid_file, raw_port)
                raw_ts = data.get("startup_ts")
                if raw_ts is not None:
                    try:
                        startup_ts = float(raw_ts)
                    except (TypeError, ValueError):
                        logger.debug("PID file %s: ignoring non-numeric startup_ts %r", pid_file, raw_ts)
                    else:
                        if math.isfinite(startup_ts):
                            info["startup_ts"] = startup_ts
                        else:
                            logger.debug("PID file %s: ignoring non-finite startup_ts %r", pid_file, raw_ts)
                return info
            # Valid JSON but wrong shape — don't fall through to legacy parser
            if isinstance(data, dict):
                logger.warning("PID file %s: JSON dict missing 'pid' key", pid_file)
                return None
            if not isinstance(data, (int, float)):
                # Non-numeric, non-dict JSON (string, array, etc.)
                logger.warning("PID file %s: unexpected JSON shape: %s", pid_file, type(data).__name__)
                return None
            # Bare numeric JSON — fall through to legacy integer parse
        except _json.JSONDecodeError:
            pass  # Not JSON — try legacy integer format
        except TypeError:
            pass  # json.loads returned something weird — try legacy
        # Fall back to plain integer (legacy format)
        pid = _coerce_pos_int(text, lo=1, hi=_PID_MAX)
        if pid is None:
            logger.warning("Corrupt PID file %s: not JSON and not a plain integer", pid_file)
            return None
        return {"pid": pid, "cmd": "unknown"}
    except (ValueError, OSError, OverflowError) as exc:
        logger.warning("Corrupt PID file %s: %s", pid_file, exc)
        return None


def is_pid_alive(pid: int) -> bool:
    """Check if a process is running. Uses signal 0 on POSIX, OpenProcess on Windows."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # type: ignore[union-attr]  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[union-attr]
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ProcessLookupError):
        return False


def _windows_wmic_path() -> str | None:
    """Return the trusted absolute path to Windows WMIC, if available."""
    if sys.platform != "win32":
        return None

    import ctypes

    try:
        kernel32 = ctypes.windll.kernel32
    except AttributeError:
        return None

    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if length <= 0 or length >= len(buffer):
        return None

    wmic_path = Path(buffer.value) / "wbem" / "WMIC.exe"
    try:
        if wmic_path.is_file():
            return str(wmic_path)
    except OSError:
        return None
    return None


def _read_os_command_line(pid: int) -> list[str] | None:
    """Best-effort read of OS process command line tokens.

    Fallback chain: /proc (Linux) -> ps (macOS/BSD) -> wmic (Windows).
    """
    # Linux: read /proc/{pid}/cmdline directly.
    proc_cmdline = Path("/proc") / str(pid) / "cmdline"
    try:
        if proc_cmdline.exists():
            raw = proc_cmdline.read_bytes()
            tokens = [tok.decode(errors="ignore") for tok in raw.split(b"\x00") if tok]
            if tokens:
                return tokens
    except OSError:
        logger.debug("Could not read %s", proc_cmdline, exc_info=True)

    # macOS/BSD: use ps.
    if sys.platform != "win32":
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=1.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        cmdline = result.stdout.strip()
        if not cmdline:
            return None
        try:
            return shlex.split(cmdline)
        except ValueError:
            return [cmdline]

    # Windows: use WMIC via its trusted system location.  Do not invoke a bare
    # executable name here: PID verification can run from untrusted project
    # directories, and Windows process creation may search those directories.
    wmic = _windows_wmic_path()
    if wmic is None:
        return None
    try:
        result = subprocess.run(
            [wmic, "process", "where", f"ProcessId={pid}", "get", "CommandLine", "/VALUE"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("CommandLine="):
            cmdline = line[len("CommandLine=") :].strip()
            if cmdline:
                try:
                    return shlex.split(cmdline, posix=False)
                except ValueError:
                    return [cmdline]
    return None


def verify_pid_ownership(
    pid_file: Path,
    *,
    expected_cmd: str = "filigree",
    required_args: tuple[str, ...] = (),
) -> bool:
    """Verify PID file refers to a live process with expected identity.

    Checks OS-level process identity first to avoid TOCTOU races — on Linux,
    reading /proc/{pid}/cmdline atomically confirms alive + identity.
    Falls back to is_pid_alive + PID-file metadata only when cmdline is unreadable.
    """
    info = read_pid_file(pid_file)
    if info is None:
        return False

    # When the PID record embeds a port, require the live process argv to
    # expose the matching ``--port <N>`` pair — this is what distinguishes
    # one project's dashboard from another after PID recycling
    # (filigree-563d5454e9).
    effective_required = required_args
    recorded_port = info.get("port")
    if isinstance(recorded_port, int) and recorded_port > 0:
        effective_required = (*required_args, "--port", str(recorded_port))

    # Try OS-level identity first — avoids the TOCTOU race of checking
    # is_pid_alive() then reading cmdline in separate steps.
    tokens = _read_os_command_line(info["pid"])
    if tokens:
        if isinstance(recorded_port, int) and recorded_port > 0:
            required_lower = tuple(arg.strip().lower() for arg in required_args)
            for candidate in _expected_process_arg_candidates(tokens, expected_cmd=expected_cmd):
                if _tokens_contain_args(candidate, required_lower) and _effective_option_value(candidate, "--port") == str(recorded_port):
                    return True
            return False
        return _matches_expected_process(tokens, expected_cmd=expected_cmd, required_args=effective_required)

    # Cmdline unreadable — process may be dead or in a constrained environment.
    # Check aliveness, then fall back to PID-file metadata.
    if not is_pid_alive(info["pid"]):
        return False

    pid_cmd = str(info.get("cmd", "")).strip().lower()
    if not pid_cmd or pid_cmd == "unknown":
        return False
    try:
        pid_tokens = shlex.split(pid_cmd, posix=os.name != "nt")
    except ValueError:
        pid_tokens = [pid_cmd]
    # Fallback path: PID-file `cmd` is recorded without the --port pair (port
    # is persisted as a separate metadata field). Validate caller-supplied
    # required_args only; trust the already-parsed `port` field as metadata.
    # The strict ``--port <N>`` requirement still applies on the OS-argv path
    # above, where the live process's actual argv is the source of truth.
    return _matches_expected_process(pid_tokens, expected_cmd=expected_cmd, required_args=required_args)


def cleanup_stale_pid(pid_file: Path) -> bool:
    """Remove PID file if the process is dead or not ours (PID recycled).

    Uses ``verify_pid_ownership`` to check both aliveness and identity,
    preventing stale PID files from persisting when the PID is recycled
    to an unrelated process.

    Uses a rename-then-recheck pattern (filigree-73e909e6cc): the atomic
    ``rename`` snapshots the file we inspected and leaves the PID-file slot
    free for any concurrent writer. We re-verify the quarantined copy to
    decide whether to unlink it, and restore it if the race made the record
    look fresh.
    """
    info = read_pid_file(pid_file)
    if info is None:
        return False
    if verify_pid_ownership(pid_file, expected_cmd="filigree", required_args=("dashboard",)):
        return False  # Process is alive and ours — not stale

    # Atomically move the file aside so a concurrent writer cannot have its
    # fresh record clobbered by our unlink. The quarantine name is unique per
    # cleaner so two cleaners on the same pid_file do not collide.
    quarantine = pid_file.with_suffix(f"{pid_file.suffix}.removing.{os.getpid()}.{time.monotonic_ns()}")
    try:
        pid_file.rename(quarantine)
    except FileNotFoundError:
        return False
    except OSError:
        logger.debug("cleanup_stale_pid: rename to quarantine failed", exc_info=True)
        return False

    # Re-verify the quarantined copy. If it still looks stale, drop it. If it
    # now looks fresh (a writer raced between our first verify and our
    # rename), try to restore it without clobbering any newer primary record:
    # ``os.link`` fails atomically with ``FileExistsError`` when pid_file
    # already exists, so a writer that filled the slot after our rename keeps
    # priority. ``os.rename`` cannot do this on POSIX (it overwrites).
    if verify_pid_ownership(quarantine, expected_cmd="filigree", required_args=("dashboard",)):
        try:
            os.link(quarantine, pid_file)
        except FileExistsError:
            logger.debug("cleanup_stale_pid: pid_file already rewritten by another writer; dropping our copy")
        except OSError:
            logger.debug("cleanup_stale_pid: failed to restore quarantined pid_file", exc_info=True)
        quarantine.unlink(missing_ok=True)
        return False

    quarantine.unlink(missing_ok=True)
    logger.info("Cleaned stale PID file %s (pid %d)", pid_file, info["pid"])
    return True


# ---------------------------------------------------------------------------
# Port file helpers
# ---------------------------------------------------------------------------


def write_port_file(port_file: Path, port: int) -> None:
    """Write the active dashboard port to file (atomic)."""
    from filigree.core import write_atomic

    write_atomic(port_file, str(port))


def read_port_file(port_file: Path) -> int | None:
    """Read the dashboard port from file. Returns None if missing/corrupt."""
    if not port_file.exists():
        return None
    try:
        return int(port_file.read_text().strip())
    except (ValueError, OSError) as exc:
        logger.warning("Corrupt port file %s: %s", port_file, exc)
        return None


# ---------------------------------------------------------------------------
# Legacy cleanup
# ---------------------------------------------------------------------------


def cleanup_legacy_tmp_files() -> None:
    """Remove legacy /tmp/filigree-dashboard.* files from the hybrid mode era."""
    for name in ("filigree-dashboard.pid", "filigree-dashboard.lock", "filigree-dashboard.log"):
        path = Path("/tmp", name)  # noqa: S108
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.debug("Failed to remove legacy tmp file %s", path, exc_info=True)
