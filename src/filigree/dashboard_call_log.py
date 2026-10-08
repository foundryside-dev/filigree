"""ASGI middleware that logs one ``event="call"`` record per dashboard HTTP request.

``name`` is ``"<METHOD> <route template>"`` (e.g. ``GET /api/issues/{issue_id}``)
so every issue id collapses to one name; an unmatched path falls back to the raw
path. ``outcome`` comes from the response status (2xx/3xx ``ok``; 4xx/5xx
``error``, or ``validation`` when the error envelope's ``code`` is ``VALIDATION``)
and ``code`` from the JSON error envelope the dashboard already returns.

Pure ASGI (not ``BaseHTTPMiddleware``) so streaming responses are untouched and
the matched ``scope["route"]`` is visible after the inner app has run. ``/mcp``
is skipped — MCP tool calls log themselves on the ``mcp`` surface — as is the
static asset mount.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from filigree.logging import CallOutcome, classify_body, log_outcome
from filigree.types.api import ErrorCode

_BODY_CAPTURE_LIMIT = 64 * 1024
_SKIPPED_PREFIXES = ("/static/", "/mcp")
_HTTP_ERROR_FLOOR = 400

PopulationResolver = Callable[[Scope], str | None]


def _skipped(path: str) -> bool:
    return path.startswith(_SKIPPED_PREFIXES) or path == "/static"


def _route_template(scope: Scope) -> str | None:
    """Return the matched route's full path template, or ``None`` when no route matched.

    FastAPI >= 0.137 resolves ``include_router(..., prefix=...)`` lazily:
    ``scope["route"]`` is then the *unprefixed* original route and the prefixed
    template lives on the effective route context FastAPI stashes in
    ``scope["fastapi"]``. Older FastAPI bakes the prefix into ``route.path``.
    ``tests/api/test_http_call_logging.py`` pins the resulting names, so a
    FastAPI change that moves this fails loudly rather than silently coarsening
    the log.
    """
    fastapi_scope = scope.get("fastapi")
    effective = fastapi_scope.get("effective_route_context") if isinstance(fastapi_scope, dict) else None
    for candidate in (getattr(effective, "path", None), getattr(scope.get("route"), "path", None)):
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _route_name(scope: Scope) -> str:
    return f"{scope.get('method', 'GET')} {_route_template(scope) or scope.get('path', '')}"


def _classify_response(status: int, body: bytes) -> tuple[CallOutcome, str | None]:
    if status < _HTTP_ERROR_FLOOR:
        return "ok", None
    code: str | None = None
    try:
        outcome, code = classify_body(json.loads(body))
    except (ValueError, UnicodeDecodeError):
        outcome = "error"
    # A 4xx/5xx is a dead-end even if its body carried no recognisable envelope.
    return ("validation" if outcome == "validation" else "error"), code


class CallLogMiddleware:
    def __init__(self, app: ASGIApp, *, population: PopulationResolver | None = None) -> None:
        self.app = app
        self._population = population

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _skipped(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        t0 = time.monotonic()
        status = 0
        body = bytearray()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            elif message["type"] == "http.response.body" and status >= _HTTP_ERROR_FLOOR and len(body) < _BODY_CAPTURE_LIMIT:
                body.extend(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Unhandled exception: the server will answer 500 (or the response
            # already started and the connection drops). Record the dead-end.
            self._log(scope, t0, "error", ErrorCode.INTERNAL)
            raise
        outcome, code = _classify_response(status, bytes(body))
        self._log(scope, t0, outcome, code)

    def _log(self, scope: Scope, t0: float, outcome: CallOutcome, code: str | None) -> None:
        try:
            population: str | None = None
            if self._population is not None:
                try:
                    population = self._population(scope)
                except Exception:
                    logging.getLogger(__name__).debug("http call log: population lookup failed", exc_info=True)
            log_outcome(
                logging.getLogger("filigree"),
                surface="http",
                name=_route_name(scope),
                outcome=outcome,
                code=code,
                duration_ms=round((time.monotonic() - t0) * 1000, 1),
                population=population,
            )
        except Exception:  # pragma: no cover - instrumentation must never break a request
            logging.getLogger(__name__).debug("http call logging failed", exc_info=True)
