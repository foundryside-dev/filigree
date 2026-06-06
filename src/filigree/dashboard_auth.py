"""Opt-in bearer-token authentication for the loom federation surface.

Filigree's HTTP API is loopback-only and historically performs no inbound
auth (ADR-012: the transport is the trust boundary). When an operator sets
``WEFT_FEDERATION_TOKEN`` (or the deprecated aliases ``FILIGREE_FEDERATION_API_TOKEN``
/ ``FILIGREE_API_TOKEN``), this module gates the **loom federation surface** (``/api/weft/*`` plus the
living-surface federation aliases), scanner ingest aliases, and dashboard MCP
HTTP endpoint behind a bearer token, while leaving the classic dashboard API
and the local dashboard UI open.

Design: docs/superpowers/specs/2026-06-03-loom-bearer-token-auth-design.md
ADR-018 (the decision); ADR-012 (the threat model this partially lifts).

The loom-route enforcement is **opt-in**: with the env var unset, ``create_app``
does not install the middleware for loom routes. The high-privilege MCP HTTP
transport is stricter: it is only mounted when this bearer token exists.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.middleware.base import BaseHTTPMiddleware

#: Living-surface paths (trailing segment, no ``/api`` prefix) that route to the
#: loom generation and must be enforced alongside ``/api/weft/*``. Add new
#: federation-write aliases here when living-surface routers grow.
LIVING_FEDERATION_ALIASES: frozenset[str] = frozenset({"scan-results", "observations"})
CLASSIC_FEDERATION_ALIASES: frozenset[str] = frozenset({"v1/scan-results"})


def is_loom_scoped_path(path: str) -> bool:
    """Return True when *path* is part of the loom federation surface.

    Strips the ``/api`` root and an optional server-mode ``/p/{key}`` segment,
    then matches the loom prefix or a living federation alias. The classic
    surface (``/api/issue/...``), the root dashboard, and ``/api/health`` are
    out of scope. Scanner callback aliases and the dashboard-mounted MCP HTTP
    endpoint are gated by the same token because they accept agent/federation
    writes.
    """
    if path == "/mcp" or path.startswith("/mcp/"):
        return True
    if not path.startswith("/api/"):
        return False
    rest = path[len("/api/") :]
    # Drop an optional server-mode project segment: ``p/{key}/...``.
    if rest.startswith("p/"):
        parts = rest.split("/", 2)  # ['p', key, remainder]
        rest = parts[2] if len(parts) == 3 else ""
    return rest == "weft" or rest.startswith("weft/") or rest in LIVING_FEDERATION_ALIASES or rest in CLASSIC_FEDERATION_ALIASES


def _extract_bearer(header: str | None) -> str | None:
    """Return the token from an ``Authorization: Bearer <token>`` header.

    The scheme is matched case-insensitively. Returns ``None`` for a missing
    header, a non-Bearer scheme, or an empty token.
    """
    if not header:
        return None
    scheme, _, rest = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = rest.strip()
    return token or None


def _token_matches(provided: str, expected: str) -> bool:
    """Constant-time equality of two tokens.

    Encodes to UTF-8 bytes before comparing so a non-ASCII *provided* value
    (an inbound header decodes latin-1 server-side) returns ``False`` rather
    than raising ``TypeError`` from ``hmac.compare_digest`` on non-ASCII str.
    """
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def build_auth_middleware(token: str) -> type[BaseHTTPMiddleware]:
    """Build a middleware class that gates the loom surface behind *token*.

    Only called when an operator has configured a non-empty token, so the
    middleware always has a real secret to compare against. The comparison is
    constant-time (``hmac.compare_digest``). Non-loom paths and CORS preflight
    (``OPTIONS``) pass straight through.
    """
    from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response

    from filigree.dashboard_routes.common import _error_response
    from filigree.types.api import ErrorCode

    class BearerTokenAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
            if request.method == "OPTIONS" or not is_loom_scoped_path(request.url.path):
                return await call_next(request)
            provided = _extract_bearer(request.headers.get("Authorization"))
            if provided is None or not _token_matches(provided, token):
                resp = _error_response("Missing or invalid bearer token", ErrorCode.PERMISSION, 401)
                resp.headers["WWW-Authenticate"] = "Bearer"
                return resp
            return await call_next(request)

    return BearerTokenAuthMiddleware
