"""Transport-bound actor identity resolution (ADR-012, schema v24).

The ``actor`` string on a Filigree write is an unauthenticated *claim*, not a
proof. This module resolves a best-effort *verified* identity from the
transport (the OS user the process runs as) and builds the structured warning
emitted when the claimed and verified identities disagree. Resolution never
raises and never blocks a write: a missing or unresolvable identity yields
``None`` and the write proceeds with ``verified_actor = NULL``.
"""

from __future__ import annotations

from typing import TypedDict


def resolve_os_actor() -> str | None:
    """Best-effort OS-user identity, or ``None`` on any failure.

    Uses ``pwd.getpwuid(os.geteuid())`` on POSIX. Windows has no ``pwd``
    module, so the import fails and we return ``None`` (verified_actor stays
    NULL — no crash, per the cross-platform contract).
    """
    try:
        import os
        import pwd

        return pwd.getpwuid(os.geteuid()).pw_name or None
    except Exception:
        return None


class ActorMismatchWarning(TypedDict):
    """Structured warning emitted when claimed actor != verified actor."""

    code: str
    claimed: str
    verified: str


# Framework auto-default actor strings. A claim equal to one of these is NOT a
# genuine identity assertion (it is what Click/MCP fill in when the caller
# supplied nothing), so a difference from the verified OS user is expected and
# must not produce a warning. The DB still records the value verbatim; only the
# warning surface is suppressed. (ADR-012 decision 9.)
_PLACEHOLDER_ACTORS = frozenset({"cli", "mcp"})


def actor_mismatch_warning(claimed: str | None, verified: str | None) -> ActorMismatchWarning | None:
    """Return a structured warning when claimed and verified identities differ.

    Returns ``None`` (no warning) unless BOTH values are non-empty, differ, and
    the claim is a *genuine* identity (not a framework placeholder default). A
    missing/empty/placeholder claimed value, or an empty verified value, is an
    unverified surface rather than a conflict. Never raises, never blocks a write.
    """
    if claimed and verified and claimed != verified and claimed not in _PLACEHOLDER_ACTORS:
        return {"code": "ACTOR_MISMATCH", "claimed": claimed, "verified": verified}
    return None
