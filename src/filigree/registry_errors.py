"""Public error-envelope helpers for registry-backed file identity failures."""

from __future__ import annotations

from typing import Any

from filigree.registry import (
    DEFAULT_LOOMWEAVE_TOKEN_ENV,
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

# Action label for a registry failure raised while serving a request against
# an ALREADY-OPEN project DB (e.g. ``POST /api/observations`` -> ``register_file``
# -> Loomweave resolve). Distinct from ``"opening project database"``, which is
# reserved for the DB-open paths, so an operator reading the envelope is not
# told the project failed to open when it is the mid-session resolve that failed.
REGISTRY_REQUEST_ACTION = "handling request"

_FALLBACK_CLAUSE = "set loomweave.allow_local_fallback=true in the Filigree config to keep working with local file ids"
_TOKEN_ENV_CLAUSE = f"the environment variable named by loomweave.token_env (default {DEFAULT_LOOMWEAVE_TOKEN_ENV!r})"

REGISTRY_UNAVAILABLE_STARTUP_HINT = f"Start Loomweave for this project (`loomweave serve`) or {_FALLBACK_CLAUSE} until it is back."
REGISTRY_VERSION_MISMATCH_STARTUP_HINT = "Upgrade Filigree or Loomweave so their registry API versions match."

# One remedy line per ``RegistryUnavailableError.cause_kind``. Only the
# reachability kinds (``network`` / ``timeout`` / ``http_error`` / ``unknown``)
# are fixed by starting Loomweave; the rest are configuration or protocol
# problems that "start Loomweave" would send the operator chasing in vain.
# ``invalid_response`` deliberately offers no fallback: a reachable Loomweave
# that violates the resolver contract fails closed AT RESOLVE TIME even with
# ``allow_local_fallback=true`` (``_LoomweaveLocalFallbackRegistry._should_fallback``),
# so a fallback would only defer the same failure to the first resolve. (The
# startup capability probe itself does honour ``allow_local_fallback`` for
# every ``RegistryUnavailableError`` kind, this one included.)
_REGISTRY_UNAVAILABLE_HINTS: dict[str, str] = {
    "auth_token_missing": (
        f"Loomweave requires a bearer token but Filigree resolved none: export the Loomweave token in {_TOKEN_ENV_CLAUSE}, "
        f"switch this project to registry_backend='local', or {_FALLBACK_CLAUSE}."
    ),
    "auth": (
        f"Loomweave rejected Filigree's bearer token (HTTP 401): check that {_TOKEN_ENV_CLAUSE} holds the token Loomweave "
        f"was started with, or {_FALLBACK_CLAUSE} until it is fixed."
    ),
    "auth_mode_unsupported": (
        "Loomweave is serving an authentication mode Filigree does not implement: reconfigure Loomweave's serving mode "
        f"(Filigree sends only 'Authorization: Bearer'), switch this project to registry_backend='local', or {_FALLBACK_CLAUSE}."
    ),
    "role_declined": (
        "Loomweave is up but declines the registry-backend role: reconfigure it to serve registry_backend + file_registry, "
        f"switch this project to registry_backend='local', or {_FALLBACK_CLAUSE}."
    ),
    "invalid_response": (
        "Loomweave answered with a malformed registry response: verify loomweave.base_url points at a Loomweave registry API "
        "and upgrade Filigree or Loomweave to a matching pair (starting Loomweave will not fix this)."
    ),
}


def registry_unavailable_hint(cause_kind: str) -> str:
    """Return the one-line operator remedy for a ``RegistryUnavailableError`` by ``cause_kind``.

    Shared by the CLI (``cli_common``), the MCP status / tool envelopes and
    the dashboard so every surface prints the same remedy for the same cause.
    Unknown kinds get the generic outage line.
    """
    return _REGISTRY_UNAVAILABLE_HINTS.get(cause_kind, REGISTRY_UNAVAILABLE_STARTUP_HINT)


def registry_startup_hint(exc: RegistryStartupError) -> str:
    """Return the one-line operator remedy for a registry failure at DB open."""
    if isinstance(exc, RegistryVersionMismatchError):
        return REGISTRY_VERSION_MISMATCH_STARTUP_HINT
    return registry_unavailable_hint(exc.cause_kind)


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
