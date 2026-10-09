"""Commit-anchor reachability check for closes (Task 0.6, S2 prototype / X3).

A close may cite a commit anchor (``branch@sha``, warpline seam contract B).
An anchor on a branch that never reached the integration branch is a false
"shipped" signal, so ``close_issue`` asks git whether the anchored sha is an
ancestor of ``origin/<integration_ref>`` and attaches a warning when it is not.
The check is advisory and never blocks a close in 3.x.

Three-valued result (``close_commit_reachable``):

- ``True``  -- ``git merge-base --is-ancestor`` exited 0.
- ``False`` -- in a full (non-shallow) clone, after a successful fetch:
  merge-base exited 1 (the sha exists locally but is not an ancestor), or it
  exited 128 and git has no object with that prefix at all (a commit that only
  exists in another clone cannot be an ancestor of the freshly fetched ref). A
  squash-merged branch sha is ``False`` on purpose (Review Focus 1): the work
  may be on main, but the cited sha is not, and the warning names sha and ref.
- ``"unknown"`` -- git could not answer: no project root, the root is not a git
  checkout, git is not installed, the anchor carries no valid sha, the
  configured ref is unsafe, ``git fetch`` failed or timed out, the clone is
  shallow (or its shallowness cannot be read), the prefix is ambiguous or names
  a non-commit, or merge-base exited with anything else. ``unknown`` never
  produces a warning that blames the commit.

Trust boundary: the anchor and the configured ref are caller/config supplied.
Both are validated (sha ``^[0-9a-f]{7,40}$``; ref a conservative git ref-name
subset that can never start with ``-``) before they reach ``argv``; git runs
without a shell, with a timeout, with ``GIT_TERMINAL_PROMPT=0`` and (unless the
user set one) ``GIT_SSH_COMMAND="ssh -o BatchMode=yes"`` so a fetch cannot hang
on a prompt, and with inherited ``GIT_DIR`` / ``GIT_WORK_TREE`` /
``GIT_INDEX_FILE`` stripped so it always inspects the project root.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

Reachable = bool | Literal["unknown"]

DEFAULT_INTEGRATION_REF = "main"
FETCH_TIMEOUT_S = 10.0
MERGE_BASE_TIMEOUT_S = 10.0
FETCH_CACHE_TTL_S = 60.0
FETCH_FAILURE_TTL_S = 30.0
WARNING_CODE = "commit_not_reachable_from_integration_ref"

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
# Conservative subset of git-check-ref-format: first char alphanumeric (so never
# an option), then [A-Za-z0-9._/-]; the structural rules below reject the rest.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

# Per-process cache of fetch outcomes: (resolved root, ref) -> (monotonic time,
# succeeded). A success is reused for 60 s; a failure for 30 s, so a dead origin
# costs one fetch timeout per window rather than one per close.
_fetch_cache: dict[tuple[str, str], tuple[float, bool]] = {}
_fetch_lock = threading.Lock()


@dataclass(frozen=True)
class ReachabilityCheck:
    """Outcome of one reachability check."""

    reachable: Reachable
    sha: str | None
    ref: str  # e.g. "origin/main" ("origin/<ref>" as configured, even if unsafe)

    @property
    def warning(self) -> str | None:
        """The agent-facing warning, only for a definite ``False``."""
        if self.reachable is False:
            return f"{WARNING_CODE}: {self.sha} not in {self.ref}"
        return None

    @property
    def stored_value(self) -> str:
        """Event encoding of :attr:`reachable`: ``true`` / ``false`` / ``unknown``."""
        if self.reachable == "unknown":
            return "unknown"
        return "true" if self.reachable else "false"


def parse_stored_value(value: str | None) -> Reachable | None:
    """Inverse of :attr:`ReachabilityCheck.stored_value`; ``None`` when unrecognised."""
    if value == "true":
        return True
    if value == "false":
        return False
    if value == "unknown":
        return "unknown"
    return None


def extract_sha(anchor: str) -> str | None:
    """Return the lower-cased sha of a ``branch@sha`` (or bare ``sha``) anchor, else ``None``."""
    candidate = anchor.rsplit("@", 1)[-1].strip().lower()
    return candidate if _SHA_RE.match(candidate) else None


def is_safe_ref(ref: str) -> bool:
    """Whether *ref* is a safe branch name to pass to git as an argv element."""
    if not _REF_RE.match(ref):
        return False
    if ".." in ref or "//" in ref or ref.endswith(("/", ".", ".lock")):
        return False
    return not any(part.startswith(".") for part in ref.split("/"))


def reset_fetch_cache() -> None:
    """Forget cached fetch outcomes (tests)."""
    with _fetch_lock:
        _fetch_cache.clear()


# Inherited variables that would point git somewhere other than ``cwd``.
_STRIPPED_GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_GIT_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    # An ssh host-key or passphrase prompt would otherwise hang until timeout.
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


def _run_git(root: Path, args: list[str], timeout: float) -> tuple[int, str] | None:
    """Run ``git <args>`` in *root*; return ``(exit code, stdout)``, or ``None`` if git could not run."""
    try:
        proc = subprocess.run(
            ["git", *args],  # fixed argv, validated operands, no shell
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("commit reachability: git %s failed to run", args[0], exc_info=True)
        return None
    return proc.returncode, proc.stdout or ""


def _fetch(root: Path, ref: str) -> bool:
    """Fetch ``origin <ref>``; successes are cached for 60 s, failures for 30 s."""
    key = (str(root.resolve()), ref)
    with _fetch_lock:
        cached = _fetch_cache.get(key)
        if cached is not None:
            at, ok = cached
            ttl = FETCH_CACHE_TTL_S if ok else FETCH_FAILURE_TTL_S
            if time.monotonic() - at < ttl:
                return ok
    result = _run_git(root, ["fetch", "--quiet", "origin", ref], FETCH_TIMEOUT_S)
    ok = result is not None and result[0] == 0
    with _fetch_lock:
        _fetch_cache[key] = (time.monotonic(), ok)
    return ok


def _is_shallow(root: Path) -> bool | None:
    """``True``/``False`` from ``git rev-parse --is-shallow-repository``; ``None`` if git cannot say."""
    result = _run_git(root, ["rev-parse", "--is-shallow-repository"], MERGE_BASE_TIMEOUT_S)
    if result is None or result[0] != 0:
        return None
    answer = result[1].strip()
    return True if answer == "true" else False if answer == "false" else None


def _object_missing(root: Path, sha: str) -> bool:
    """True only when git knows NO object with this prefix (not missing-or-ambiguous)."""
    result = _run_git(root, ["rev-parse", f"--disambiguate={sha}"], MERGE_BASE_TIMEOUT_S)
    return result is not None and result[0] == 0 and not result[1].strip()


def check_commit_reachable(project_root: Path | None, anchor: str, integration_ref: str) -> ReachabilityCheck:
    """Is the sha in *anchor* reachable from ``origin/<integration_ref>``? Never raises.

    Blocking (fetch and merge-base, up to ~10 s each). Async callers run it via
    ``asyncio.to_thread``; it touches no database connection.
    """
    remote_ref = f"origin/{integration_ref}"
    sha = extract_sha(anchor)

    def unknown() -> ReachabilityCheck:
        return ReachabilityCheck(reachable="unknown", sha=sha, ref=remote_ref)

    if sha is None or project_root is None or not is_safe_ref(integration_ref):
        return unknown()
    # No walk-up: only a project root that is itself a checkout (``.git`` dir,
    # or file for a worktree) is asked. Keeps tmp-dir stores off git entirely.
    if not (project_root / ".git").exists():
        return unknown()
    if not _fetch(project_root, integration_ref):
        return unknown()
    result = _run_git(project_root, ["merge-base", "--is-ancestor", sha, remote_ref], MERGE_BASE_TIMEOUT_S)
    code = result[0] if result is not None else None
    if code == 0:
        return ReachabilityCheck(reachable=True, sha=sha, ref=remote_ref)
    if code not in (1, 128):
        return unknown()
    # A shallow history can hide ancestry (exit 1 at the boundary) and lacks
    # objects that exist upstream, so neither exit code is an answer there.
    if _is_shallow(project_root) is not False:
        return unknown()
    if code == 1:
        return ReachabilityCheck(reachable=False, sha=sha, ref=remote_ref)
    # Exit 128 after a successful fetch into a full clone: every commit reachable
    # from origin/<ref> is local, so a sha with no local object cannot be one of
    # them. An ambiguous prefix or a non-commit object stays unknown.
    if _object_missing(project_root, sha):
        return ReachabilityCheck(reachable=False, sha=sha, ref=remote_ref)
    return unknown()
