"""Inbound federation bearer token: 3-tier resolution + anchor-minted persistence.

The federation bearer gates the loom federation surface (``/api/weft/*``, the
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
:func:`resolve_federation_token` is strictly read-only so the many call sites
that merely *resolve* the token (including ``create_app``, which tests invoke
directly) never create a file as a side effect.
"""

from __future__ import annotations

import logging
import os
import secrets
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


def mint_token_file(store_dir: Path) -> str:
    """Persist (idempotently) a federation token in *store_dir* and return it.

    Reuses an existing non-empty file. Otherwise records an already-exported env
    token's value — so a freshly minted file matches a daemon already running on
    that env token — or, failing that, mints a fresh ``secrets.token_urlsafe(32)``.
    Writes ``0600``.

    Best-effort: a write failure (read-only mount, missing parent that cannot be
    created) is logged, and the in-memory value is returned so the caller still
    has a usable token for this run rather than crashing the daemon.
    """
    existing = read_token_file(store_dir)
    if existing:
        return existing
    env_value, _env_name = read_env_token()
    token = env_value or secrets.token_urlsafe(32)
    path = store_dir / FEDERATION_TOKEN_FILENAME
    try:
        store_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(token + "\n")
        path.chmod(0o600)
    except OSError as exc:
        logger.warning("Could not persist federation token to %s: %s", path, exc)
    return token


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
