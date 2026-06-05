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


def _build_handler(state: LegisStubState) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:  # silence test noise
            pass

        def do_GET(self) -> None:  # stdlib BaseHTTPRequestHandler naming
            state.requests.append(self.path)
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
