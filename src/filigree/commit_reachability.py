"""Commit-anchor reachability check for closes (Task 0.6, S2 prototype / X3).

A close may cite a commit anchor (``branch@sha``, warpline seam contract B).
An anchor on a branch that never reached the integration branch is a false
"shipped" signal, so ``close_issue`` asks git whether the anchored sha is an
ancestor of ``origin/<integration_ref>`` and attaches a warning when it is not.
The check is advisory and never blocks a close in 3.x.

Three-valued result (``close_commit_reachable``):

- ``True``  -- ``git merge-base --is-ancestor`` exited 0.
- ``False`` -- it exited 1: the sha exists locally but is not an ancestor. A
  squash-merged branch sha lands here on purpose (Review Focus 1): the work may
  be on main, but the cited sha is not, and the warning names both sha and ref.
- ``"unknown"`` -- git could not answer: no project root, the root is not a git
  checkout, git is not installed, the anchor carries no valid sha, the
  configured ref is unsafe, ``git fetch`` failed or timed out, or merge-base
  exited with anything else (128 = an object git does not have, or the ref is
  missing). ``unknown`` never produces a warning that blames the commit.

Trust boundary: the anchor and the configured ref are caller/config supplied.
Both are validated (sha ``^[0-9a-f]{7,40}$``; ref a conservative git ref-name
subset that can never start with ``-``) before they reach ``argv``; git runs
without a shell, with a timeout, and with ``GIT_TERMINAL_PROMPT=0`` so a fetch
cannot hang on a credential prompt.
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
WARNING_CODE = "commit_not_reachable_from_integration_ref"

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
# Conservative subset of git-check-ref-format: first char alphanumeric (so never
# an option), then [A-Za-z0-9._/-]; the structural rules below reject the rest.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

# Per-process cache of successful fetches: (resolved root, ref) -> monotonic
# time of the last successful ``git fetch``. Failures are not cached.
_fetch_cache: dict[tuple[str, str], float] = {}
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
    """Forget cached fetches (tests)."""
    with _fetch_lock:
        _fetch_cache.clear()


def _run_git(root: Path, args: list[str], timeout: float) -> int | None:
    """Run ``git <args>`` in *root*; return the exit code, or ``None`` if git could not run."""
    try:
        proc = subprocess.run(
            ["git", *args],  # fixed argv, validated operands, no shell
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("commit reachability: git %s failed to run", args[0], exc_info=True)
        return None
    return proc.returncode


def _fetch(root: Path, ref: str) -> bool:
    key = (str(root.resolve()), ref)
    now = time.monotonic()
    with _fetch_lock:
        last = _fetch_cache.get(key)
        if last is not None and now - last < FETCH_CACHE_TTL_S:
            return True
    ok = _run_git(root, ["fetch", "--quiet", "origin", ref], FETCH_TIMEOUT_S) == 0
    if ok:
        with _fetch_lock:
            _fetch_cache[key] = time.monotonic()
    return ok


def check_commit_reachable(project_root: Path | None, anchor: str, integration_ref: str) -> ReachabilityCheck:
    """Is the sha in *anchor* reachable from ``origin/<integration_ref>``? Never raises."""
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
    code = _run_git(project_root, ["merge-base", "--is-ancestor", sha, remote_ref], MERGE_BASE_TIMEOUT_S)
    if code == 0:
        return ReachabilityCheck(reachable=True, sha=sha, ref=remote_ref)
    if code == 1:
        return ReachabilityCheck(reachable=False, sha=sha, ref=remote_ref)
    return unknown()
