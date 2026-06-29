"""Wire-level tests for ``LoomweaveRegistry.resolve_entity_content_hashes``.

RED-1: the closure gate resolves each governed binding's CURRENT content_hash
through this surface and compares it to the attach snapshot. These tests run a
throwaway HTTP server that mirrors Loomweave's REAL identity-resolve wire shapes
(``crates/loomweave-cli/src/http_read/identity.rs``) so the form-dispatch and the
``content_hash`` extraction are exercised against the production response shape —
NOT a convenient fake that would return a hash for any id and hide a false-green:

- locators  -> ``POST /api/v1/identity/resolve:batch`` -> ``resolved[locator] =
  {sei, current_locator, content_hash, alive:true}``; SEI-shaped inputs land in
  ``invalid``, unknown valid locators in ``not_found``.
- SEIs       -> ``GET /api/v1/identity/sei/{sei}`` -> ``{sei, current_locator,
  content_hash, alive:true}`` for an alive SEI, or ``{alive:false, lineage:[]}``.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import pytest

from filigree.registry import LoomweaveRegistry, RegistryUnavailableError

# Loomweave's identity DB, keyed by the form the consumer submits.
_LOCATOR_HASHES = {
    "py:func:mod::f": "sha256:current-f",
    "core:file:abc@src/x.py": "sha256:current-x",
}
_SEI_HASHES = {
    "loomweave:eid:00000000000000000000000000000001": "sha256:current-sei",
}
# An SEI that resolves alive:false (orphaned / renamed away).
_ORPHANED_SEI = "loomweave:eid:0000000000000000000000000000dead"


class _IdentityHandler(BaseHTTPRequestHandler):
    """Stub of Loomweave's identity-resolve endpoints (alive records only)."""

    def _send(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        resolved: dict[str, object] = {}
        invalid: list[str] = []
        not_found: list[str] = []
        for locator in request.get("locators", []):
            if locator.startswith("loomweave:eid:"):
                invalid.append(locator)  # resolve:batch rejects SEIs
            elif locator in _LOCATOR_HASHES:
                resolved[locator] = {
                    "sei": "loomweave:eid:resolved",
                    "current_locator": locator,
                    "content_hash": _LOCATOR_HASHES[locator],
                    "alive": True,
                }
            else:
                not_found.append(locator)
        self._send({"resolved": resolved, "invalid": invalid, "not_found": not_found})

    def do_GET(self) -> None:
        # Path: /api/v1/identity/sei/<percent-encoded sei>
        sei = unquote(urlparse(self.path).path.rsplit("/", 1)[-1])
        if sei in _SEI_HASHES:
            self._send(
                {
                    "sei": sei,
                    "current_locator": "py:func:mod::renamed",
                    "content_hash": _SEI_HASHES[sei],
                    "alive": True,
                }
            )
        else:
            self._send({"sei": sei, "alive": False, "lineage": []})

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def identity_registry() -> object:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _IdentityHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    registry = LoomweaveRegistry(f"http://127.0.0.1:{server.server_port}", timeout_seconds=2)
    try:
        yield registry
    finally:
        registry.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_resolves_locator_content_hash_via_batch(identity_registry: LoomweaveRegistry) -> None:
    result = identity_registry.resolve_entity_content_hashes(["py:func:mod::f"])
    assert result["resolved"] == {"py:func:mod::f": "sha256:current-f"}
    assert result["unresolved"] == []


def test_resolves_sei_content_hash_via_by_sei_get(identity_registry: LoomweaveRegistry) -> None:
    sei = "loomweave:eid:00000000000000000000000000000001"
    result = identity_registry.resolve_entity_content_hashes([sei])
    assert result["resolved"] == {sei: "sha256:current-sei"}
    assert result["unresolved"] == []


def test_resolves_mixed_locator_and_sei_forms(identity_registry: LoomweaveRegistry) -> None:
    sei = "loomweave:eid:00000000000000000000000000000001"
    result = identity_registry.resolve_entity_content_hashes(["py:func:mod::f", sei, "core:file:abc@src/x.py"])
    assert result["resolved"] == {
        "py:func:mod::f": "sha256:current-f",
        "core:file:abc@src/x.py": "sha256:current-x",
        sei: "sha256:current-sei",
    }
    assert result["unresolved"] == []


def test_orphaned_sei_is_unresolved_not_fresh(identity_registry: LoomweaveRegistry) -> None:
    result = identity_registry.resolve_entity_content_hashes([_ORPHANED_SEI])
    assert result["resolved"] == {}
    assert result["unresolved"] == [_ORPHANED_SEI]


def test_unknown_locator_is_unresolved(identity_registry: LoomweaveRegistry) -> None:
    result = identity_registry.resolve_entity_content_hashes(["py:func:mod::missing"])
    assert result["resolved"] == {}
    assert result["unresolved"] == ["py:func:mod::missing"]


def test_backend_unreachable_raises_registry_unavailable() -> None:
    # Bind a port, close it -> connection refused -> whole-backend availability
    # failure surfaces as RegistryUnavailableError (the gate degrades to UNKNOWN).
    registry = LoomweaveRegistry("http://127.0.0.1:1", timeout_seconds=1)
    try:
        with pytest.raises(RegistryUnavailableError):
            registry.resolve_entity_content_hashes(["py:func:mod::f"])
    finally:
        registry.close()


# --- fallback wrapper delegation ---------------------------------------------
# In loomweave mode with allow_local_fallback, ``db.registry`` is the
# _LoomweaveLocalFallbackRegistry wrapper. If it did NOT expose
# resolve_entity_content_hashes, the gate's getattr would miss it and degrade to
# UNKNOWN even when Loomweave is UP — a false-green that disables the drift gate
# whenever fallback is enabled. These pin the delegation.


class _PrimaryWithResolver:
    def __init__(self, resolution: dict[str, object]) -> None:
        self._resolution = resolution

    def resolve_entity_content_hashes(self, entity_ids: list[str]) -> dict[str, object]:
        return self._resolution

    def is_displaced(self) -> bool:
        return True


class _LegacyPrimaryNoResolver:
    """A pre-surface injected primary (e.g. an older fake) lacking the method."""

    def is_displaced(self) -> bool:
        return True


def test_fallback_wrapper_delegates_to_primary() -> None:
    from filigree.core import LocalRegistry, _LoomweaveLocalFallbackRegistry

    resolution = {"resolved": {"py:func:mod::f": "sha256:x"}, "unresolved": []}
    wrapper = _LoomweaveLocalFallbackRegistry(
        _PrimaryWithResolver(resolution),
        LocalRegistry(lambda: "f-local"),
        base_url="http://loomweave.test",
    )
    assert wrapper.resolve_entity_content_hashes(["py:func:mod::f"]) == resolution


def test_fallback_wrapper_without_primary_surface_degrades_to_unresolved() -> None:
    from filigree.core import LocalRegistry, _LoomweaveLocalFallbackRegistry

    wrapper = _LoomweaveLocalFallbackRegistry(
        _LegacyPrimaryNoResolver(),
        LocalRegistry(lambda: "f-local"),
        base_url="http://loomweave.test",
    )
    result = wrapper.resolve_entity_content_hashes(["a", "b"])
    assert result["resolved"] == {}
    assert result["unresolved"] == ["a", "b"]
