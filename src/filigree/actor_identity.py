"""Transport-bound actor identity resolution (ADR-012, schema v24).

The ``actor`` string on a Filigree write is an unauthenticated logical alias,
not a proof. This module resolves a best-effort *verified* identity from the
transport (the OS user the process runs as). Logical aliases and OS principals
occupy different namespaces, so a difference between them is recorded as
provenance rather than treated as a mismatch. Resolution never raises or blocks
a write: a missing or unresolvable identity yields ``None`` and the write
proceeds with ``verified_actor = NULL``.
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
    """Legacy warning shape retained for import and type compatibility."""

    code: str
    claimed: str
    verified: str


def actor_mismatch_warning(claimed: str | None, verified: str | None) -> ActorMismatchWarning | None:
    """Compatibility shim: logical aliases never conflict with OS provenance.

    ``claimed`` remains the operational actor used for claim-aware writes;
    ``verified`` is stored separately as transport provenance. Comparing the two
    produced false positives whenever several agents shared one OS account, so
    current policy never emits a mismatch warning. Retaining this helper avoids
    breaking imports while callers migrate away from the obsolete warning path.
    """
    return None
