"""Inbound federation bearer token: 3-tier resolution + anchor-minted persistence.

The federation bearer gates the weft federation surface (``/api/weft/*``, the
federation scanner/observation aliases) and the dashboard ``/mcp`` transport. It
resolves in three tiers (highest precedence first):

  1. ``$WEFT_FEDERATION_TOKEN`` (or the deprecated ``$FILIGREE_FEDERATION_API_TOKEN``
     / ``$FILIGREE_API_TOKEN`` aliases) — operator override; the only tier that
     works across hosts (no shared filesystem).
  2. ``<store_dir>/federation_token`` — auto-minted by the daemon on first serve
     and read back here. The single-host default: a sibling on the same machine
     reads it from the ``.weft/`` subtree it is already allowed to read (C-9e).
     Single-project daemons resolve ``<store_dir>`` to the project store
     (``.weft/filigree/``); the server-mode daemon uses ``~/.config/filigree/``.
  3. absent → ``("", None)`` → federation auth stays off (graceful degrade,
     unchanged from the pre-mint behaviour).

This is loopback deconfliction plumbing, **not** an authority key (C-8): the
0600 file mode is the only boundary — do not add hardening.

Behaviour note: because tier 2 auto-mints, a token always exists after a
daemon's first serve, so federation auth is on-by-default on that daemon. The
env var remains the cross-host escape hatch.

Minting is a deliberate write performed only at real daemon boot (see
``dashboard.run``) and by ``filigree install`` / ``filigree doctor --fix``.
So is reconciliation (:func:`reconcile_token_file`): when tier 1 is active and
the file holds a different value, boot and ``doctor --fix`` rewrite the file to
the env token, so the published value is always the one the daemon accepts.
:func:`resolve_federation_token` is strictly read-only so the many call sites
that merely *resolve* the token (including ``create_app``, which tests invoke
directly) never create a file as a side effect.
"""

from __future__ import annotations

import contextlib
import enum
import hashlib
import logging
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Canonical inbound env var. Distinct from the OUTBOUND registry token
#: ``WEFT_TOKEN`` (registry.py). The ``FILIGREE_*`` names are deprecated aliases,
#: read as a soft fallback; removal post-1.0.
WEFT_FEDERATION_ENV_VAR = "WEFT_FEDERATION_TOKEN"
DEPRECATED_FEDERATION_ENV_VARS = ("FILIGREE_FEDERATION_API_TOKEN", "FILIGREE_API_TOKEN")
#: Read order: canonical first, then deprecated aliases.
FEDERATION_TOKEN_ENV_VARS = (WEFT_FEDERATION_ENV_VAR, *DEPRECATED_FEDERATION_ENV_VARS)

#: Filename of the persisted (tier-2) token inside a store dir. Underscore form,
#: matching what ``filigree install`` writes and the store migration copies.
FEDERATION_TOKEN_FILENAME = "federation_token"  # noqa: S105 - filename, not a token value
#: Reported as the auth "source" (health endpoint / logs) when tier 2 wins —
#: there is no env-var name to report for a file-sourced token.
FEDERATION_TOKEN_FILE_SOURCE = "federation_token (file)"  # noqa: S105 - display label, not a token value


def read_env_token() -> tuple[str, str | None]:
    """Tier 1: the first non-empty federation token in the environment.

    Returns ``(token, env_var_name)``, or ``("", None)`` when none is set. A
    variable that is set but blank/whitespace is skipped with a warning (an empty
    export is almost always an unset-by-accident, and silently treating it as
    "auth off" hides the mistake).
    """
    empty: list[str] = []
    for name in FEDERATION_TOKEN_ENV_VARS:
        raw = os.environ.get(name)
        if raw is None:
            continue
        token = raw.strip()
        if token:
            if name in DEPRECATED_FEDERATION_ENV_VARS:
                # Auto-migration: the deprecated alias is still honoured (soft
                # fallback), but nudge the operator to the canonical var so the
                # rename actually completes. Only fires when no canonical var is
                # set — setting WEFT_FEDERATION_TOKEN silences it.
                logger.warning(
                    "%s is DEPRECATED — set %s instead (the deprecated name is still honoured for now, but will be removed)",
                    name,
                    WEFT_FEDERATION_ENV_VAR,
                )
            return token, name
        empty.append(name)
    for name in empty:
        logger.warning("%s is set but empty/whitespace — federation auth is NOT enabled from that variable", name)
    return "", None


def read_token_file(store_dir: Path) -> str:
    """Tier 2 read: the persisted token in *store_dir*, or ``""`` if absent/unreadable.

    Strictly read-only — never creates the file (see module docstring).
    """
    try:
        return (store_dir / FEDERATION_TOKEN_FILENAME).read_text().strip()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError):
        # Honour the "unreadable -> ''" contract for a present-but-corrupt file
        # (UnicodeDecodeError is a ValueError, NOT an OSError — read_text decodes
        # UTF-8). Warn rather than fail silently: a corrupt token reads as
        # "auth off", which a bare swallow would hide. Fails closed (no token).
        logger.warning(
            "federation_token file in %s is present but unreadable — treating as no token (federation auth NOT enabled from it)",
            store_dir,
        )
        return ""


def mint_token_file(store_dir: Path, *, rotate: bool = False) -> str:
    """Persist (idempotently) a federation token in *store_dir* and return it.

    Reuses an existing non-empty file — even one that differs from an exported
    env token; realigning that is :func:`reconcile_token_file`'s job. Otherwise records an already-exported env
    token's value — so a freshly minted file matches a daemon already running on
    that env token — or, failing that, mints a fresh ``secrets.token_urlsafe(32)``.
    Writes ``0600``.

    *rotate* forces a (re)write, ignoring any existing file — the supported
    rotation path (no separate "delete then re-mint" dance, which races a reader).
    It keeps the same value rule (an exported env token is written through,
    reconciling a stale file *to* the env the daemon already enforces; otherwise a
    fresh secret), so it closes both lockout shapes: a file-only deployment gets a
    new secret, and an env-pinned deployment's stale file is realigned to the env.
    The change is only live after the daemon (and any siblings) restart — the token
    is resolved once at ``create_app`` and baked into the auth middleware.

    Best-effort: a write failure (read-only mount, missing parent that cannot be
    created) is logged, and the in-memory value is returned so the caller still
    has a usable token for this run rather than crashing the daemon.
    """
    existing = read_token_file(store_dir)
    if existing and not rotate:
        return existing
    env_value, _env_name = read_env_token()
    token = env_value or secrets.token_urlsafe(32)
    _write_token_file(store_dir, token)
    return token


def _write_token_file(store_dir: Path, token: str) -> bool:
    """Atomically publish *token* to ``<store_dir>/federation_token`` (``0600``).

    Returns ``True`` on success. Best-effort: an ``OSError`` (read-only mount,
    uncreatable parent) is logged and reported as ``False`` rather than raised.
    """
    path = store_dir / FEDERATION_TOKEN_FILENAME
    tmp_name: str | None = None
    try:
        store_dir.mkdir(parents=True, exist_ok=True)
        # Atomic publish: stage 0600 to a dest-dir temp, then os.replace. A bare
        # write_text open-truncates the existing file first, so a concurrent
        # reader (server mode resolves a project's token per request) could read an
        # empty file mid-rotate and 401 a valid bearer. Staging keeps the path
        # always-complete; os.replace is an atomic same-dir rename.
        fd, tmp_name = tempfile.mkstemp(dir=store_dir, prefix=FEDERATION_TOKEN_FILENAME + ".", suffix=".tmp")
        try:
            os.write(fd, (token + "\n").encode())
        finally:
            os.close(fd)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        tmp_name = None
    except OSError as exc:
        logger.warning("Could not persist federation token to %s: %s", path, exc)
        if tmp_name is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
        return False
    return True


def token_fingerprint(token: str) -> str:
    """A short, non-reversible identifier for *token* (first 8 hex of sha256).

    For logs and diagnostics only — never log the token value itself.
    """
    return hashlib.sha256(token.encode()).hexdigest()[:8]


class ReconcileStatus(enum.Enum):
    """Outcome of :func:`reconcile_token_file`."""

    REWRITTEN = "rewritten"  # file differed from the env token and was realigned
    ALREADY_MATCHES = "already_matches"  # file already holds the env token
    NO_ENV = "no_env"  # no env token active — the file IS the credential
    NO_FILE = "no_file"  # no (readable, non-empty) file — minting covers it
    WRITE_FAILED = "write_failed"  # mismatch found but the rewrite failed


@dataclass(frozen=True)
class ReconcileResult:
    status: ReconcileStatus
    env_name: str | None = None

    @property
    def reconciled(self) -> bool:
        return self.status is ReconcileStatus.REWRITTEN


def reconcile_token_file(store_dir: Path) -> ReconcileResult:
    """Realign the published token file in *store_dir* to the active env token.

    The file is Filigree's *published* token: same-host siblings (Wardline,
    Loomweave) read it to authenticate. When an env token is active (tier 1) it
    is what the daemon enforces, so a file holding a different value makes every
    sibling that trusts the file 401 (HTTP F14). This rewrites the file to the env
    token — only when the env token is set AND the file exists with a different
    non-empty value; an absent file is :func:`mint_token_file`'s job (it already
    writes an exported env token through). Same atomic ``0600`` publish as minting.

    Never touches ``os.environ`` (the flow is env → file only, never the reverse).
    Emits a ``token_file_reconciled`` record carrying fingerprints, never values.
    Called at daemon boot (``dashboard.run``) and by ``filigree doctor --fix`` —
    never from :func:`resolve_federation_token`, which stays read-only.
    """
    env_token, env_name = read_env_token()
    if not env_token:
        return ReconcileResult(ReconcileStatus.NO_ENV)
    file_token = read_token_file(store_dir)
    if not file_token:
        return ReconcileResult(ReconcileStatus.NO_FILE, env_name)
    if file_token == env_token:
        return ReconcileResult(ReconcileStatus.ALREADY_MATCHES, env_name)
    if not _write_token_file(store_dir, env_token):
        return ReconcileResult(ReconcileStatus.WRITE_FAILED, env_name)
    logger.info(
        "token_file_reconciled",
        extra={
            "tool": "federation_token",
            "args_data": {
                "store_dir": str(store_dir),
                "env_var": env_name,
                "old_fingerprint": token_fingerprint(file_token),
                "new_fingerprint": token_fingerprint(env_token),
            },
        },
    )
    return ReconcileResult(ReconcileStatus.REWRITTEN, env_name)


def resolve_federation_token(store_dir: Path | None) -> tuple[str, str | None]:
    """Read-only 3-tier resolution. Returns ``(token, source)``.

    *source* is the env-var name (tier 1), :data:`FEDERATION_TOKEN_FILE_SOURCE`
    (tier 2), or ``None`` when auth stays off (tier 3). Passing ``store_dir=None``
    (no resolvable store, e.g. a server daemon with no config dir) skips tier 2.
    Never writes — minting is an explicit boot/install/doctor step.
    """
    token, env_name = read_env_token()
    if token:
        return token, env_name
    if store_dir is not None:
        file_token = read_token_file(store_dir)
        if file_token:
            return file_token, FEDERATION_TOKEN_FILE_SOURCE
    return "", None
