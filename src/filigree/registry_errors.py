"""Public error-envelope helpers for registry-backed file identity failures."""

from __future__ import annotations

from typing import Any

from filigree.registry import RegistryFileNotFoundError, RegistryResolutionError, RegistryUnavailableError
from filigree.types.api import ErrorCode, ErrorResponse

RegistryPublicError = RegistryResolutionError | RegistryUnavailableError


def registry_error_response(exc: RegistryPublicError, *, action: str) -> ErrorResponse:
    """Translate registry exceptions into the shared CLI/MCP/API error envelope."""
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
