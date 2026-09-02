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
  content_hash, alive:true}`` for an alive SEI, or ``{alive:false, lineage:[...]}``.
- lineage    -> ``GET /api/v1/identity/lineage/{sei}`` -> ``{sei, lineage:[...]}``
  (the fallback source of the rename hint when an ``alive:false`` body carries no
  inline ``lineage`` list; an older Loomweave without the route answers 404).
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
# An orphaned SEI whose by-SEI body carries NO ``lineage`` key at all — models a
# Loomweave that does not inline lineage, forcing the lineage-route fallback.
_ORPHANED_SEI_NO_INLINE = "loomweave:eid:0000000000000000000000000000beef"

_BORN = {"event": "born", "old_locator": None, "new_locator": "py:func:mod::f", "run_id": "run-1", "recorded_at": "2026-01-01T00:00:00Z"}
_RENAMED = {
    "event": "locator_changed",
    "old_locator": "py:func:mod::f",
    "new_locator": "py:func:mod::g",
    "run_id": "run-2",
    "recorded_at": "2026-02-01T00:00:00Z",
}

# Mutable per-test wire state (reset by the ``identity_registry`` fixture).
# sei -> lineage list served INLINE on the alive:false by-SEI body.
_SEI_INLINE_LINEAGE: dict[str, object] = {}
# sei -> lineage list served by the lineage ROUTE.
_SEI_ROUTE_LINEAGE: dict[str, object] = {}
# When set, the lineage route answers this HTTP status (404 = older Loomweave).
_LINEAGE_STATUS: list[int] = []
# Every SEI the lineage route was asked for.
LINEAGE_REQUESTS: list[str] = []


class _IdentityHandler(BaseHTTPRequestHandler):
    """Stub of Loomweave's identity-resolve endpoints (alive records only)."""

    def _send(self, payload: dict[str, object], status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
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
        path = urlparse(self.path).path
        sei = unquote(path.rsplit("/", 1)[-1])
        if path.startswith("/api/v1/identity/lineage/"):
            # Mirror ``get_identity_lineage``: ``{sei, lineage}``; an unknown SEI
            # is an empty lineage (never 404 on a Loomweave that has the route).
            LINEAGE_REQUESTS.append(sei)
            if _LINEAGE_STATUS:
                self._send({"error": {"code": "not_found", "message": "no route"}}, status=_LINEAGE_STATUS[0])
                return
            self._send({"sei": sei, "lineage": _SEI_ROUTE_LINEAGE.get(sei, [])})
            return
        # Path: /api/v1/identity/sei/<percent-encoded sei>
        if sei in _SEI_HASHES:
            self._send(
                {
                    "sei": sei,
                    "current_locator": "py:func:mod::renamed",
                    "content_hash": _SEI_HASHES[sei],
                    "alive": True,
                }
            )
        elif sei == _ORPHANED_SEI_NO_INLINE:
            self._send({"sei": sei, "alive": False})
        else:
            self._send({"sei": sei, "alive": False, "lineage": _SEI_INLINE_LINEAGE.get(sei, [])})

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def identity_registry() -> object:
    _SEI_INLINE_LINEAGE.clear()
    _SEI_ROUTE_LINEAGE.clear()
    _LINEAGE_STATUS.clear()
    LINEAGE_REQUESTS.clear()
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


# --- rename-lineage hint on orphaned SEIs (filigree-4e13d133f7) ---------------
# An ``alive:false`` SEI is a dead end for the agent unless the gate can name the
# re-bind target. The hint is the LAST lineage event, taken from the ``lineage``
# list inlined on the by-SEI body; only a body WITHOUT that list falls back to
# ``GET /api/v1/identity/lineage/{sei}``. Enrich-only: no failure of the hint
# path may raise or change ``resolved`` / ``unresolved``.


def test_orphaned_sei_hint_is_last_inline_lineage_event(identity_registry: LoomweaveRegistry) -> None:
    _SEI_INLINE_LINEAGE[_ORPHANED_SEI] = [_BORN, _RENAMED]
    result = identity_registry.resolve_entity_content_hashes([_ORPHANED_SEI])
    assert result["resolved"] == {}
    assert result["unresolved"] == [_ORPHANED_SEI]
    assert result["lineage_hints"] == {_ORPHANED_SEI: _RENAMED}  # LAST event wins
    assert LINEAGE_REQUESTS == []  # inline lineage present -> no second round-trip


def test_orphaned_sei_with_empty_inline_lineage_has_no_hint_and_no_route_call(identity_registry: LoomweaveRegistry) -> None:
    _SEI_INLINE_LINEAGE[_ORPHANED_SEI] = []
    _SEI_ROUTE_LINEAGE[_ORPHANED_SEI] = [_RENAMED]  # would answer, but must not be asked
    result = identity_registry.resolve_entity_content_hashes([_ORPHANED_SEI])
    assert result["unresolved"] == [_ORPHANED_SEI]
    assert "lineage_hints" not in result
    assert LINEAGE_REQUESTS == []


def test_orphaned_sei_without_inline_lineage_falls_back_to_lineage_route(identity_registry: LoomweaveRegistry) -> None:
    _SEI_ROUTE_LINEAGE[_ORPHANED_SEI_NO_INLINE] = [_BORN, _RENAMED]
    result = identity_registry.resolve_entity_content_hashes([_ORPHANED_SEI_NO_INLINE])
    assert result["unresolved"] == [_ORPHANED_SEI_NO_INLINE]
    assert result["lineage_hints"] == {_ORPHANED_SEI_NO_INLINE: _RENAMED}
    assert LINEAGE_REQUESTS == [_ORPHANED_SEI_NO_INLINE]


def test_lineage_route_404_older_loomweave_yields_no_hint_and_never_raises(identity_registry: LoomweaveRegistry) -> None:
    _LINEAGE_STATUS.append(404)
    result = identity_registry.resolve_entity_content_hashes([_ORPHANED_SEI_NO_INLINE, "py:func:mod::f"])
    assert result["resolved"] == {"py:func:mod::f": "sha256:current-f"}  # siblings undegraded
    assert result["unresolved"] == [_ORPHANED_SEI_NO_INLINE]
    assert "lineage_hints" not in result
    assert LINEAGE_REQUESTS == [_ORPHANED_SEI_NO_INLINE]  # one round-trip, no retry on 404


def test_lineage_route_5xx_is_swallowed_after_retry_budget(identity_registry: LoomweaveRegistry) -> None:
    _LINEAGE_STATUS.append(503)
    registry = LoomweaveRegistry(identity_registry.base_url, timeout_seconds=0.5)
    try:
        result = registry.resolve_entity_content_hashes([_ORPHANED_SEI_NO_INLINE])
    finally:
        registry.close()
    assert result["unresolved"] == [_ORPHANED_SEI_NO_INLINE]
    assert "lineage_hints" not in result
    assert LINEAGE_REQUESTS  # the route WAS attempted (and retried inside the budget)


def test_alive_sei_never_consults_lineage(identity_registry: LoomweaveRegistry) -> None:
    sei = "loomweave:eid:00000000000000000000000000000001"
    _SEI_ROUTE_LINEAGE[sei] = [_RENAMED]
    result = identity_registry.resolve_entity_content_hashes([sei])
    assert result["resolved"] == {sei: "sha256:current-sei"}
    assert "lineage_hints" not in result
    assert LINEAGE_REQUESTS == []


@pytest.mark.parametrize(
    "lineage",
    [
        "not-a-list",
        [_BORN, "not-a-dict"],
        [{"old_locator": "a", "new_locator": "b", "run_id": "r", "recorded_at": "t"}],  # missing event
        [{"event": "locator_changed", "old_locator": 1, "new_locator": "b", "run_id": "r", "recorded_at": "t"}],
        [{"event": "locator_changed", "old_locator": "a", "new_locator": "b", "recorded_at": "t"}],  # missing run_id
    ],
    ids=["not-list", "last-not-dict", "missing-event", "bad-locator-type", "missing-run-id"],
)
def test_malformed_lineage_yields_no_hint_and_never_raises(identity_registry: LoomweaveRegistry, lineage: object) -> None:
    _SEI_INLINE_LINEAGE[_ORPHANED_SEI] = lineage
    _SEI_ROUTE_LINEAGE[_ORPHANED_SEI_NO_INLINE] = lineage
    result = identity_registry.resolve_entity_content_hashes([_ORPHANED_SEI, _ORPHANED_SEI_NO_INLINE])
    assert result["unresolved"] == [_ORPHANED_SEI, _ORPHANED_SEI_NO_INLINE]
    assert "lineage_hints" not in result


def test_hint_event_carries_exactly_the_five_lineage_keys(identity_registry: LoomweaveRegistry) -> None:
    # Extra producer-side keys are dropped; the hint is the documented 5-key event.
    _SEI_INLINE_LINEAGE[_ORPHANED_SEI] = [{**_RENAMED, "extra": "ignored"}]
    result = identity_registry.resolve_entity_content_hashes([_ORPHANED_SEI])
    assert result["lineage_hints"] == {_ORPHANED_SEI: _RENAMED}


def test_unknown_locator_never_gets_a_hint(identity_registry: LoomweaveRegistry) -> None:
    # resolve:batch reports a locator in ``not_found`` without an SEI, and
    # Loomweave has no locator-keyed lineage surface: documented limitation.
    result = identity_registry.resolve_entity_content_hashes(["py:func:mod::missing"])
    assert result["unresolved"] == ["py:func:mod::missing"]
    assert "lineage_hints" not in result
    assert LINEAGE_REQUESTS == []


# --- the same contract against the shared Clarion/Loomweave stub ---------------
# ``tests/_fakes/clarion_http.py`` is the stub the integration suites build a
# FiligreeDB against; keep its lineage surface honest with the wire tests above.


def test_shared_stub_serves_inline_lineage_hint() -> None:
    from tests._fakes.clarion_http import clarion_stub

    with clarion_stub(sei_supported=True) as (base_url, state):
        state.sei_records[_ORPHANED_SEI] = {"alive": False, "lineage": [_BORN, _RENAMED]}
        registry = LoomweaveRegistry(base_url, timeout_seconds=2)
        try:
            result = registry.resolve_entity_content_hashes([_ORPHANED_SEI])
        finally:
            registry.close()
        assert result["unresolved"] == [_ORPHANED_SEI]
        assert result["lineage_hints"] == {_ORPHANED_SEI: _RENAMED}
        assert state.lineage_requests == []


def test_shared_stub_route_fallback_and_older_loomweave_404() -> None:
    from tests._fakes.clarion_http import clarion_stub

    with clarion_stub(sei_supported=True) as (base_url, state):
        state.sei_records[_ORPHANED_SEI] = {"alive": False, "lineage": None}  # no inline list
        state.lineage_records[_ORPHANED_SEI] = [_BORN, _RENAMED]
        registry = LoomweaveRegistry(base_url, timeout_seconds=2)
        try:
            result = registry.resolve_entity_content_hashes([_ORPHANED_SEI])
            assert result["lineage_hints"] == {_ORPHANED_SEI: _RENAMED}
            assert state.lineage_requests == [_ORPHANED_SEI]

            state.lineage_route_status = 404  # older Loomweave: route absent
            result = registry.resolve_entity_content_hashes([_ORPHANED_SEI])
        finally:
            registry.close()
        assert result["unresolved"] == [_ORPHANED_SEI]
        assert "lineage_hints" not in result
        assert state.lineage_requests == [_ORPHANED_SEI, _ORPHANED_SEI]


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
