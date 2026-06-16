"""Minimal HTTP stub for Legis closure-gate tests (B5).

Serves ``GET /filigree/issues/{issue_id}/closure-gate`` from a
``ThreadingHTTPServer`` so the stdlib ``legis_client`` exercises its real
urllib request/timeout/error-classification path against a live socket —
no live Legis required.

The stub is driven by :class:`LegisStubState`:

- ``status`` / ``body`` — the canned response for the next gate request.
- ``delay_seconds`` — sleep before responding, to exercise the client's
  ``urlopen(timeout=…)`` no-hang behaviour.
- ``requests`` — every requested path, in arrival order, for call-tracing.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class LegisStubState:
    """Mutable test state shared between the HTTP handler and its callers."""

    status: int = 200
    body: dict[str, Any] = field(default_factory=lambda: {"allowed": True, "reason": "ok"})
    delay_seconds: float = 0.0
    requests: list[str] = field(default_factory=list)
    # Authorization header seen on each request (None when absent) — lets a test
    # assert the bearer IS sent on a normal (non-redirecting) gate call. (B3)
    auth_headers: list[str | None] = field(default_factory=list)


def _build_handler(state: LegisStubState) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:  # silence test noise
            pass

        def do_GET(self) -> None:  # stdlib BaseHTTPRequestHandler naming
            state.requests.append(self.path)
            state.auth_headers.append(self.headers.get("Authorization"))
            if state.delay_seconds:
                time.sleep(state.delay_seconds)
            payload = json.dumps(state.body).encode("utf-8")
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return _Handler


@contextmanager
def legis_stub() -> Iterator[tuple[str, LegisStubState]]:
    """Yield ``(base_url, state)`` for a running Legis closure-gate stub."""
    state = LegisStubState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _build_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


@dataclass
class RedirectSinkState:
    """Records what the redirect TARGET (the 'sink' host) received.

    Models a Legis that 302-redirects the closure-gate request to a different
    host (an open-redirect on an honest Legis, or a compromised one). The sink
    stands in for the attacker-chosen target: it records the ``Authorization``
    header of anything that reaches it, so a test can prove the bearer was NOT
    re-sent across the redirect. (B3)
    """

    auth_headers: list[str | None] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)


def _build_sink_handler(state: RedirectSinkState) -> type[BaseHTTPRequestHandler]:
    class _SinkHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:  # stdlib BaseHTTPRequestHandler naming
            state.requests.append(self.path)
            state.auth_headers.append(self.headers.get("Authorization"))
            payload = json.dumps({"allowed": True, "reason": "sink"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return _SinkHandler


def _build_redirect_handler(sink_url: str, state: RedirectSinkState) -> type[BaseHTTPRequestHandler]:
    class _RedirectHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:  # stdlib BaseHTTPRequestHandler naming
            # Record what the ORIGIN (operator-configured) host received, so a
            # test can prove the bearer WAS sent here and only stripped at the
            # redirect boundary — not merely "never sent". (B3)
            state.requests.append(self.path)
            state.auth_headers.append(self.headers.get("Authorization"))
            self.send_response(302)
            self.send_header("Location", f"{sink_url}{self.path}")
            self.end_headers()

    return _RedirectHandler


@contextmanager
def legis_redirect_to_sink() -> Iterator[tuple[str, RedirectSinkState, RedirectSinkState]]:
    """Yield ``(legis_base_url, origin_state, sink_state)``.

    The Legis server (the operator-configured origin) 302-redirects every
    closure-gate request to a separate sink host. ``origin_state`` records what
    the origin received (the bearer SHOULD reach hop 1); ``sink_state`` records
    what the redirect target received (the bearer must NOT cross the redirect).
    Together they prove the strip happens precisely at the redirect boundary,
    and that the redirect was still followed (benign redirects are not
    blocked). (B3)
    """
    sink_state = RedirectSinkState()
    origin_state = RedirectSinkState()
    sink_server = ThreadingHTTPServer(("127.0.0.1", 0), _build_sink_handler(sink_state))
    sink_thread = threading.Thread(target=sink_server.serve_forever, daemon=True)
    sink_thread.start()
    try:
        sink_host, sink_port = sink_server.server_address[:2]
        sink_url = f"http://{sink_host}:{sink_port}"
        legis_server = ThreadingHTTPServer(("127.0.0.1", 0), _build_redirect_handler(sink_url, origin_state))
        legis_thread = threading.Thread(target=legis_server.serve_forever, daemon=True)
        legis_thread.start()
        try:
            legis_host, legis_port = legis_server.server_address[:2]
            yield f"http://{legis_host}:{legis_port}", origin_state, sink_state
        finally:
            legis_server.shutdown()
            legis_server.server_close()
            legis_thread.join(timeout=2.0)
    finally:
        sink_server.shutdown()
        sink_server.server_close()
        sink_thread.join(timeout=2.0)
