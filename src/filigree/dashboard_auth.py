"""Opt-in bearer-token authentication for the weft federation surface.

Filigree's HTTP API is loopback-only and historically performs no inbound
auth (ADR-012: the transport is the trust boundary). When an operator sets
``WEFT_FEDERATION_TOKEN`` (or the deprecated aliases ``FILIGREE_FEDERATION_API_TOKEN``
/ ``FILIGREE_API_TOKEN``), this module gates the **weft federation surface** (``/api/weft/*`` plus the
living-surface federation aliases), scanner ingest aliases, and dashboard MCP
HTTP endpoint behind a bearer token, while leaving the classic dashboard API
and the local dashboard UI open.

Design: docs/superpowers/specs/2026-06-03-weft-bearer-token-auth-design.md
ADR-018 (the decision); ADR-012 (the threat model this partially lifts).

The weft-route enforcement is **opt-in**: with the env var unset, ``create_app``
does not install the middleware for weft routes. The high-privilege MCP HTTP
transport is stricter: it is only mounted when this bearer token exists.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.middleware.base import BaseHTTPMiddleware

#: Living-surface paths (trailing segment, no ``/api`` prefix) that route to the
#: weft generation and must be enforced alongside ``/api/weft/*``. Add new
#: federation-write aliases here when living-surface routers grow.
LIVING_FEDERATION_ALIASES: frozenset[str] = frozenset({"scan-results", "observations"})
CLASSIC_FEDERATION_ALIASES: frozenset[str] = frozenset({"v1/scan-results", "v1/observations"})


def is_weft_scoped_path(path: str) -> bool:
    """Return True when *path* is part of the weft federation surface.

    Strips the ``/api`` root and an optional server-mode ``/p/{key}`` segment,
    then matches the weft prefix or a living federation alias. The classic
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


def extract_federation_scope(path: str, project_query: str | None) -> str | None:
    """Resolve the project key a federation request is scoped to, or ``None``.

    Returns the ``/api/p/{key}`` path segment when present; otherwise the
    ``?project=`` query value when *path* is part of the weft federation surface
    (uniform with how ``/mcp`` is scoped by its ASGI wrapper); otherwise ``None``
    (unscoped → falls back to the daemon's default project for reads, or fails
    closed for writes). Used to scope routing (the request ContextVar) and auth
    (the per-project token) from one predicate so they cannot diverge.
    """
    if path.startswith("/api/p/"):
        parts = path.split("/", 5)  # ['', 'api', 'p', key, ...]
        if len(parts) >= 4 and parts[3]:
            return parts[3]
        return None
    if project_query and is_weft_scoped_path(path):
        key = project_query.strip()
        return key or None
    return None


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


def build_auth_middleware(
    token: str,
    *,
    env_pin: str = "",
    project_token_resolver: Callable[[str], str] | None = None,
) -> type[BaseHTTPMiddleware]:
    """Build a middleware class that gates the weft surface behind a bearer token.

    Only called when an operator has configured a non-empty token, so the
    middleware always has a real secret to compare against. Comparisons are
    constant-time (``hmac.compare_digest``). Non-weft paths and CORS preflight
    (``OPTIONS``) pass straight through.

    Scope-aware validation (server mode). A request carrying an explicit project
    scope (``/api/p/{key}/…`` or ``?project={key}``) is validated against **that
    project's** federation token, or against an operator tier-1 ``env_pin`` if one
    is set — *not* against the daemon's home-store token. This keeps each
    project's inbound token the load-bearing credential for its own writes and
    stops one project's token from authorising another's (the F1 defect was the
    inverse: every request was checked only against the home token, so a project
    presenting its own token got 401).

    - *token* — the daemon's resolved token (env pin or home-store file). Used for
      **unscoped** requests and for the single-project (ethereal) case, where
      ``project_token_resolver`` is ``None`` and behaviour is unchanged.
    - *env_pin* — the tier-1 ``WEFT_FEDERATION_TOKEN`` value when it came from the
      environment (``""`` when the daemon token is a home-store file). Accepted
      across all project scopes (the operator-unification path).
    - *project_token_resolver* — maps a project key to that project's persisted
      federation token (``""`` when absent). Server mode only.
    """
    from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response

    from filigree.dashboard_routes.common import _error_response
    from filigree.types.api import ErrorCode

    def _reject() -> Response:
        resp = _error_response("Missing or invalid bearer token", ErrorCode.PERMISSION, 401)
        resp.headers["WWW-Authenticate"] = "Bearer"
        return resp

    class BearerTokenAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
            if request.method == "OPTIONS" or not is_weft_scoped_path(request.url.path):
                return await call_next(request)
            provided = _extract_bearer(request.headers.get("Authorization"))
            if provided is None:
                return _reject()
            key = extract_federation_scope(request.url.path, request.query_params.get("project"))
            if key is not None and project_token_resolver is not None:
                # Scoped request: accept the operator pin (if any) or THIS
                # project's own token. The home-store token is intentionally
                # not acceptable here.
                acceptable = [t for t in (env_pin, project_token_resolver(key)) if t]
            else:
                # Unscoped (or ethereal single-project): the daemon token.
                acceptable = [token]
            if any(_token_matches(provided, candidate) for candidate in acceptable):
                return await call_next(request)
            return _reject()

    return BearerTokenAuthMiddleware
