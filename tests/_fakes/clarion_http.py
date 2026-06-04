"""Shared HTTP stub for Clarion read-API tests.

Both ``/api/v1/_capabilities`` (ADR-014 §4 handshake) and
``/api/v1/files`` (file-identity resolution) are served from the same
``ThreadingHTTPServer`` so a FiligreeDB constructed with this stub's URL
runs the same startup probe path a production Clarion would.

Two context managers are exposed:

- :func:`clarion_stub` — single, persistent ``instance_id``. The default for
  most tests.
- :func:`clarion_stub_with_rotation` — second probe returns a different
  ``instance_id``, exercising the dashboard rotation banner / WARN path.

Both yield a ``(base_url, requests)`` pair so call-tracing tests can assert
which paths were resolved without spinning up new infrastructure.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

DEFAULT_INSTANCE_ID = "test-clarion-instance"
DEFAULT_API_VERSION = 1


@dataclass
class ClarionStubState:
    """Mutable test state shared between the HTTP handler and its callers."""

    instance_id: str = DEFAULT_INSTANCE_ID
    api_version: int = DEFAULT_API_VERSION
    registry_backend: bool = True
    file_registry: bool = True
    file_requests: list[dict[str, list[str]]] = field(default_factory=list)
    capability_requests: int = 0
    # ``rotate_after_capability_probes`` triggers an instance_id rotation
    # after that many probes (so e.g. ``=1`` flips the answer on the *second*
    # probe — the typical mid-session-rotation test pattern).
    rotate_after_capability_probes: int | None = None
    rotated_instance_id: str = "test-clarion-instance-rotated"
    # When a Clarion-side file is briefing-blocked, Clarion 1.0 returns
    # HTTP 403 with body ``{"code": "BRIEFING_BLOCKED", ...}``. The set holds
    # paths to simulate that state. Filigree must raise
    # ``RegistryBriefingBlockedError`` (not fall back to local) on this body.
    briefing_blocked_paths: set[str] = field(default_factory=set)
    # When set, the next file resolution will return this HTTP status with
    # an error envelope, then the override clears. Used for transient-error
    # tests.
    next_file_status_override: int | None = None
    # When set, the stub requires ``Authorization: Bearer <required_token>``
    # on every request; mismatched or absent token returns HTTP 401.
    # ``None`` (default) accepts any request, with or without an Authorization
    # header (loopback-mode Clarion default).
    required_token: str | None = None
    # Every Authorization header seen (in arrival order). Tests assert this
    # to verify the client sent / suppressed the header per CONTRACT-2.
    auth_headers_seen: list[str | None] = field(default_factory=list)
    # Every POST /api/v1/files/batch request body (parsed). Tests assert the
    # number of entries (request count) and the queries-per-chunk shape.
    batch_requests: list[dict[str, list[dict[str, str]]]] = field(default_factory=list)
    # ---- SEI (ADR-038) surface -------------------------------------------
    # ``include_sei_capability`` controls whether ``_capabilities`` carries the
    # nested ``sei`` object at all (a pre-SEI Clarion omits it); ``sei_supported``
    # is the ``sei.supported`` flag when present. Both False represent the
    # oracle's ``capability_absent`` scenario.
    include_sei_capability: bool = True
    sei_supported: bool = False
    sei_version: int = 1
    # locator -> alive SEI. A submitted locator absent here resolves to the
    # ``not_found`` channel (orphaned); a submitted SEI-shaped string (reserved
    # ``clarion:eid:`` prefix) is rejected into the ``invalid`` channel (REQ-F-02).
    sei_by_locator: dict[str, str] = field(default_factory=dict)
    # Locators to force into the ``invalid`` channel even though they are not
    # SEI-shaped — models a malformed locator Clarion's validate_locator rejects.
    invalid_locators: set[str] = field(default_factory=set)
    # sei -> resolve_sei body (identity-axis ALIVE/ORPHANED). Absent → alive:false.
    sei_records: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Every POST /api/v1/identity/resolve:batch request's ``locators`` list.
    identity_resolve_requests: list[list[str]] = field(default_factory=list)


def _build_handler(state: ClarionStubState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _check_auth(self) -> bool:
            """Record the Authorization header and validate it if required.

            Returns True if the request should proceed, False if 401 was
            already written. Always records the seen header so tests can
            assert "no header sent" or "right header sent."
            """
            header = self.headers.get("Authorization")
            state.auth_headers_seen.append(header)
            if state.required_token is None:
                return True
            expected = f"Bearer {state.required_token}"
            if header == expected:
                return True
            body = json.dumps({"error": "unauthorized", "code": "UNAUTHORIZED"}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False

        def _write_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_identity_resolve_batch(self) -> None:
            """Mirror Clarion's ``POST /api/v1/identity/resolve:batch`` (ADR-038).

            Resolved locators map to ``{sei, current_locator, content_hash, alive}``;
            SEI-shaped inputs (reserved ``clarion:eid:`` prefix) are rejected into
            ``invalid`` (REQ-F-02); everything else falls to ``not_found``.
            """
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._write_json(400, {"error": "bad json", "code": "INVALID_PATH"})
                return
            locators = payload.get("locators") if isinstance(payload, dict) else None
            if not isinstance(locators, list):
                self._write_json(400, {"error": "locators[] required", "code": "INVALID_PATH"})
                return
            if len(locators) > 256:
                self._write_json(400, {"error": "batch too large", "code": "BATCH_TOO_LARGE"})
                return
            state.identity_resolve_requests.append(list(locators))
            resolved: dict[str, dict[str, Any]] = {}
            invalid: list[str] = []
            not_found: list[str] = []
            for locator in locators:
                if not isinstance(locator, str) or locator.startswith("clarion:eid:") or locator in state.invalid_locators:
                    invalid.append(locator)
                    continue
                sei = state.sei_by_locator.get(locator)
                if sei is None:
                    not_found.append(locator)
                    continue
                resolved[locator] = {
                    "sei": sei,
                    "current_locator": locator,
                    "content_hash": f"sha256:{locator}",
                    "alive": True,
                }
            self._write_json(200, {"resolved": resolved, "invalid": invalid, "not_found": not_found})

        def _handle_resolve_sei(self, path: str) -> None:
            """Mirror ``GET /api/v1/identity/sei/{sei}`` (identity axis)."""
            sei = path[len("/api/v1/identity/sei/") :]
            record = state.sei_records.get(sei)
            if record is not None and record.get("alive", False):
                self._write_json(200, {"sei": sei, **record})
                return
            lineage = record.get("lineage", []) if record is not None else []
            self._write_json(200, {"sei": sei, "alive": False, "lineage": lineage})

        def do_GET(self) -> None:
            if not self._check_auth():
                return
            parsed = urlparse(self.path)
            if parsed.path == "/api/v1/_capabilities":
                state.capability_requests += 1
                instance_id = state.instance_id
                if state.rotate_after_capability_probes is not None and state.capability_requests > state.rotate_after_capability_probes:
                    instance_id = state.rotated_instance_id
                capabilities: dict[str, Any] = {
                    "registry_backend": state.registry_backend,
                    "file_registry": state.file_registry,
                    "api_version": state.api_version,
                    "instance_id": instance_id,
                }
                if state.include_sei_capability:
                    capabilities["sei"] = {"supported": state.sei_supported, "version": state.sei_version}
                body = json.dumps(capabilities).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path.startswith("/api/v1/identity/sei/"):
                self._handle_resolve_sei(parsed.path)
                return
            if parsed.path == "/api/v1/files":
                query = parse_qs(parsed.query)
                state.file_requests.append(query)
                path = query.get("path", [""])[0]
                language = query.get("language", [""])[0]
                if state.next_file_status_override is not None:
                    status = state.next_file_status_override
                    state.next_file_status_override = None
                    body = json.dumps({"error": "stubbed", "code": "STUBBED"}).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path in state.briefing_blocked_paths:
                    body = json.dumps(
                        {
                            "error": f"file {path!r} is briefing-blocked",
                            "code": "BRIEFING_BLOCKED",
                        }
                    ).encode()
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = json.dumps(
                    {
                        "entity_id": f"core:file:stub@{path}",
                        "content_hash": f"sha256:{path}",
                        "canonical_path": path,
                        "language": language,
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            if not self._check_auth():
                return
            parsed = urlparse(self.path)
            if parsed.path == "/api/v1/identity/resolve:batch":
                self._handle_identity_resolve_batch()
                return
            if parsed.path != "/api/v1/files/batch":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_response(400)
                self.end_headers()
                return
            if not isinstance(payload, dict) or not isinstance(payload.get("queries"), list):
                self.send_response(400)
                self.end_headers()
                return
            state.batch_requests.append({"queries": payload["queries"]})
            queries = payload["queries"]
            if len(queries) > 256:
                body = json.dumps({"error": "batch too large", "code": "BATCH_TOO_LARGE"}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            resolved: list[dict[str, str]] = []
            not_found: list[str] = []
            briefing_blocked: list[str] = []
            for q in queries:
                path = q.get("path", "") if isinstance(q, dict) else ""
                language = q.get("language", "") if isinstance(q, dict) else ""
                if path in state.briefing_blocked_paths:
                    briefing_blocked.append(path)
                    continue
                if not path:
                    not_found.append(path)
                    continue
                resolved.append(
                    {
                        "requested_path": path,
                        "entity_id": f"core:file:stub@{path}",
                        "content_hash": f"sha256:{path}",
                        "canonical_path": path,
                        "language": language,
                    }
                )
            body = json.dumps(
                {
                    "resolved": resolved,
                    "not_found": not_found,
                    "briefing_blocked": briefing_blocked,
                    "errors": [],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


@contextmanager
def clarion_stub(
    *,
    instance_id: str = DEFAULT_INSTANCE_ID,
    api_version: int = DEFAULT_API_VERSION,
    registry_backend: bool = True,
    file_registry: bool = True,
    include_sei_capability: bool = True,
    sei_supported: bool = False,
    sei_by_locator: dict[str, str] | None = None,
) -> Iterator[tuple[str, ClarionStubState]]:
    """Run a Clarion HTTP stub serving ``_capabilities``, ``files``, and the SEI surface.

    Yields the ``base_url`` (e.g. ``http://127.0.0.1:54321``) and a
    :class:`ClarionStubState` that callers can mutate (briefing blocks,
    transient status overrides, ``sei_by_locator`` / ``sei_records``) or assert
    against (``file_requests``, ``capability_requests``, ``identity_resolve_requests``).

    SEI options seed the state up front so the capability probe a FiligreeDB runs
    at construction sees the intended ``sei`` advertisement.
    """
    state = ClarionStubState(
        instance_id=instance_id,
        api_version=api_version,
        registry_backend=registry_backend,
        file_registry=file_registry,
        include_sei_capability=include_sei_capability,
        sei_supported=sei_supported,
        sei_by_locator=dict(sei_by_locator) if sei_by_locator else {},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _build_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
