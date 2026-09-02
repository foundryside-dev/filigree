"""Public error-envelope helpers for registry-backed file identity failures."""

from __future__ import annotations

from typing import Any

from filigree.registry import (
    RegistryBriefingBlockedError,
    RegistryFileNotFoundError,
    RegistryResolutionError,
    RegistryUnavailableError,
    RegistryVersionMismatchError,
)
from filigree.types.api import ErrorCode, ErrorResponse

RegistryPublicError = RegistryResolutionError | RegistryUnavailableError | RegistryVersionMismatchError

# The two registry failures that can escape ``FiligreeDB`` construction when the
# project is in ``registry_backend=loomweave`` mode: the startup capability probe
# either cannot reach / negotiate with Loomweave (``RegistryUnavailableError``,
# fail-closed only when ``allow_local_fallback`` is false) or reaches it and
# finds a wire-protocol break (``RegistryVersionMismatchError``, never
# fallback-able). Every DB-open surface (CLI ``get_db``, MCP ``_attempt_startup``,
# dashboard startup / per-project open) must render both as envelopes, never as
# tracebacks (filigree-8fd300e2f7).
RegistryStartupError = RegistryUnavailableError | RegistryVersionMismatchError

REGISTRY_UNAVAILABLE_STARTUP_HINT = (
    "Start Loomweave for this project (`loomweave serve`) or set loomweave.allow_local_fallback=true "
    "in the Filigree config to keep working with local file ids until it is back."
)
REGISTRY_VERSION_MISMATCH_STARTUP_HINT = "Upgrade Filigree or Loomweave so their registry API versions match."


def registry_startup_hint(exc: RegistryStartupError) -> str:
    """Return the one-line operator remedy for a registry failure at DB open."""
    if isinstance(exc, RegistryVersionMismatchError):
        return REGISTRY_VERSION_MISMATCH_STARTUP_HINT
    return REGISTRY_UNAVAILABLE_STARTUP_HINT


def registry_startup_error_response(exc: RegistryStartupError, *, action: str) -> ErrorResponse:
    """Envelope for a registry failure that prevented the project DB from opening.

    Same shape as :func:`registry_error_response` (so ``code`` / ``details.cause``
    / ``details.cause_kind`` switch identically). The version-mismatch envelope
    is returned byte-identical — its ``details`` are a pinned wire contract.
    The recoverable outage (``RegistryUnavailableError``) additionally carries
    ``details.backend`` naming the configured registry backend and
    ``details.hint`` with :func:`registry_startup_hint`, so ``--json`` consumers
    get the same remedy the plain-text CLI prints, and its ``error`` line names
    backend + ``cause_kind`` + probed URL so it is self-sufficient on its own.
    """
    response = registry_error_response(exc, action=action)
    if isinstance(exc, RegistryVersionMismatchError):
        return response
    details = dict(response.get("details") or {})
    details["backend"] = "loomweave"
    details["hint"] = registry_startup_hint(exc)
    response["details"] = details
    response["error"] = f"Registry unavailable while {action} (backend=loomweave, cause_kind={exc.cause_kind}): {exc}"
    return response


def registry_error_response(exc: RegistryPublicError, *, action: str) -> ErrorResponse:
    """Translate registry exceptions into the shared CLI/MCP/API error envelope."""
    if isinstance(exc, RegistryVersionMismatchError):
        return ErrorResponse(
            error=f"Loomweave registry API version mismatch while {action}: {exc}",
            code=ErrorCode.LOOMWEAVE_REGISTRY_VERSION_MISMATCH,
            details={
                "cause": "loomweave_registry_version_mismatch",
                "url": exc.url,
                "expected": exc.expected,
                "advertised": exc.advertised,
            },
        )

    if isinstance(exc, RegistryUnavailableError):
        details: dict[str, Any] = {
            "cause": "registry_unavailable",
            "cause_kind": exc.cause_kind,
        }
        if exc.path:
            details["path"] = exc.path
        if exc.url:
            details["url"] = exc.url
        return ErrorResponse(
            error=f"Registry unavailable while {action}: {exc}",
            code=ErrorCode.REGISTRY_UNAVAILABLE,
            details=details,
        )

    if isinstance(exc, RegistryBriefingBlockedError):
        return ErrorResponse(
            error=f"Registry could not resolve file while {action}: {exc}",
            code=ErrorCode.BRIEFING_BLOCKED,
            details={
                "cause": "registry_briefing_blocked",
                "status_code": exc.status_code,
                "url": exc.url,
            },
        )

    cause = "registry_file_not_found" if isinstance(exc, RegistryFileNotFoundError) else "registry_resolution_rejected"
    details = {
        "cause": cause,
        "status_code": exc.status_code,
        "url": exc.url,
    }
    code = ErrorCode.NOT_FOUND if isinstance(exc, RegistryFileNotFoundError) else ErrorCode.VALIDATION
    return ErrorResponse(
        error=f"Registry could not resolve file while {action}: {exc}",
        code=code,
        details=details,
    )
