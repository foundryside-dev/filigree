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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import pytest

from filigree.registry import LOOMWEAVE_LINEAGE_HINT_TIMEOUT_SECONDS, LoomweaveRegistry, RegistryUnavailableError

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
# A second such SEI, for tests that need more than one route-fallback orphan.
_ORPHANED_SEI_NO_INLINE_2 = "loomweave:eid:0000000000000000000000000000cafe"

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
# When set, the lineage route sleeps this long before answering (a hanging route).
_LINEAGE_DELAY: list[float] = []
# How long the "hanging" lineage route sleeps: comfortably above the 1.0s hint
# cap so a fallback that is NOT bounded is unmistakable, without dragging the
# suite. Tests assert on the request log first and use this only as a loose bound.
_HANGING_ROUTE_SECONDS = 4.0
# Every SEI the lineage route was asked for.
LINEAGE_REQUESTS: list[str] = []


class _IdentityHandler(BaseHTTPRequestHandler):
    """Stub of Loomweave's identity-resolve endpoints (alive records only)."""

    def _send(self, payload: dict[str, object], status: int = 200) -> None:
        body = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return  # the client gave up (timed out) before the answer went out

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
            if _LINEAGE_DELAY:
                time.sleep(_LINEAGE_DELAY[0])
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
        elif sei in (_ORPHANED_SEI_NO_INLINE, _ORPHANED_SEI_NO_INLINE_2):
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
    _LINEAGE_DELAY.clear()
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


def test_lineage_route_5xx_is_swallowed_after_a_single_attempt(identity_registry: LoomweaveRegistry) -> None:
    """The fallback is enrich-only, so it does NOT spend the read paths' retry
    budget: one attempt per orphaned SEI, then no hint."""
    _LINEAGE_STATUS.append(503)
    result = identity_registry.resolve_entity_content_hashes([_ORPHANED_SEI_NO_INLINE])
    assert result["unresolved"] == [_ORPHANED_SEI_NO_INLINE]
    assert "lineage_hints" not in result
    assert "lineage_unavailable" not in result  # Loomweave answered — not a connectivity failure
    assert LINEAGE_REQUESTS == [_ORPHANED_SEI_NO_INLINE]  # exactly one attempt, no retry


def test_lineage_route_404_is_memoized_per_registry(identity_registry: LoomweaveRegistry) -> None:
    """An older Loomweave without the route (404) is asked ONCE per registry
    instance; every later orphaned SEI skips the round-trip."""
    _LINEAGE_STATUS.append(404)
    first = identity_registry.resolve_entity_content_hashes([_ORPHANED_SEI_NO_INLINE])
    assert "lineage_hints" not in first
    assert LINEAGE_REQUESTS == [_ORPHANED_SEI_NO_INLINE]
    # A later call, with a different route-fallback orphan too: no further lineage requests.
    second = identity_registry.resolve_entity_content_hashes([_ORPHANED_SEI_NO_INLINE, _ORPHANED_SEI_NO_INLINE_2])
    assert second["unresolved"] == [_ORPHANED_SEI_NO_INLINE, _ORPHANED_SEI_NO_INLINE_2]
    assert "lineage_hints" not in second
    assert LINEAGE_REQUESTS == [_ORPHANED_SEI_NO_INLINE]
    assert "lineage_unavailable" not in second  # unsupported is deterministic, not an outage


def test_lineage_route_5xx_is_not_memoized(identity_registry: LoomweaveRegistry) -> None:
    """Only the deterministic 404 is memoized: a 5xx is re-asked next time (once)."""
    _LINEAGE_STATUS.append(503)
    identity_registry.resolve_entity_content_hashes([_ORPHANED_SEI_NO_INLINE])
    identity_registry.resolve_entity_content_hashes([_ORPHANED_SEI_NO_INLINE])
    assert LINEAGE_REQUESTS == [_ORPHANED_SEI_NO_INLINE, _ORPHANED_SEI_NO_INLINE]


def test_hanging_lineage_route_is_bounded_and_flags_lineage_unavailable(identity_registry: LoomweaveRegistry) -> None:
    """A hanging lineage route costs at most ONE short hint deadline per
    resolution (not the registry's full ``timeout_seconds`` x retries per
    orphaned SEI): after the first no-answer, later orphans in the same call
    skip the route. It never raises, and reports the connectivity failure on
    ``lineage_unavailable`` so the gate can count it toward its advisory
    ``loomweave_unavailable``."""
    _LINEAGE_DELAY.append(_HANGING_ROUTE_SECONDS)
    hint_budget = 0.3
    registry = LoomweaveRegistry(identity_registry.base_url, timeout_seconds=hint_budget)
    orphans = [_ORPHANED_SEI_NO_INLINE, _ORPHANED_SEI_NO_INLINE_2]
    try:
        started = time.monotonic()
        result = registry.resolve_entity_content_hashes([*orphans, "py:func:mod::f"])
        elapsed = time.monotonic() - started
    finally:
        registry.close()
    assert result["resolved"] == {"py:func:mod::f": "sha256:current-f"}  # siblings undegraded
    assert result["unresolved"] == orphans
    assert "lineage_hints" not in result
    assert result["lineage_unavailable"] is True
    # Primary oracle: ONE attempt, then the route is skipped for the rest of this call.
    assert [orphans[0]] == LINEAGE_REQUESTS
    # Secondary, deliberately loose wall-clock bound: an unbounded fallback would
    # wait out the hanging route (>= _HANGING_ROUTE_SECONDS) at least once.
    assert elapsed < _HANGING_ROUTE_SECONDS, f"lineage fallback not bounded: {elapsed:.2f}s"


def test_lineage_hint_deadline_is_capped_below_the_registry_timeout(identity_registry: LoomweaveRegistry) -> None:
    """The fallback's deadline is ``min(timeout_seconds, hint cap)``: a
    long-timeout registry does not hand a hanging lineage route its full budget.
    Runs against the fixture's stub (which clears ``_LINEAGE_DELAY`` on
    teardown) with a second, long-timeout registry pointed at it."""
    _LINEAGE_DELAY.append(_HANGING_ROUTE_SECONDS)
    registry = LoomweaveRegistry(identity_registry.base_url, timeout_seconds=30)
    try:
        started = time.monotonic()
        result = registry.resolve_entity_content_hashes([_ORPHANED_SEI_NO_INLINE])
        elapsed = time.monotonic() - started
    finally:
        registry.close()
    assert result["lineage_unavailable"] is True
    assert LINEAGE_REQUESTS == [_ORPHANED_SEI_NO_INLINE]  # one attempt, no retry on the hint path
    # Loose wall-clock bound: the 30s registry budget must NOT be what bounds this.
    assert LOOMWEAVE_LINEAGE_HINT_TIMEOUT_SECONDS < _HANGING_ROUTE_SECONDS < 30
    assert elapsed < _HANGING_ROUTE_SECONDS, f"lineage fallback not capped: {elapsed:.2f}s"


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


# --- HTTP 413 on the content-hash locator path -------------------------------
# The drift read chunks by body bytes like its siblings, but a Loomweave whose
# transport cap is tighter than ours still answers 413. That is a per-chunk
# sizing problem, NOT a whole-backend outage: the chunk is halved and retried,
# and a lone locator Loomweave still refuses is UNKNOWN on its own (never a
# RegistryUnavailableError that would poison a batch caller's known-down flag).


def test_locator_content_hash_path_splits_chunk_on_http_413() -> None:
    from tests._fakes.clarion_http import clarion_stub

    locators = [f"core:file:{'h' * 12}@src/pkg/subsystem_{i % 7}/implementation_module_{i:04d}.py" for i in range(60)]
    sei_by_locator = {loc: f"loomweave:eid:{i:040x}" for i, loc in enumerate(locators)}
    with clarion_stub(max_body_bytes=1024, sei_supported=True, sei_by_locator=sei_by_locator) as (base_url, state):
        registry = LoomweaveRegistry(base_url, timeout_seconds=5)
        try:
            result = registry.resolve_entity_content_hashes(locators)
        finally:
            registry.close()
    assert state.rejected_body_bytes  # the tight cap DID answer 413 at least once
    assert result["resolved"] == {loc: f"sha256:{loc}" for loc in locators}
    assert result["unresolved"] == []
    assert all(size <= 1024 for size in state.identity_resolve_request_body_bytes)


def test_locator_content_hash_path_reports_lone_oversize_locator_unresolved_not_unavailable() -> None:
    from tests._fakes.clarion_http import clarion_stub

    small = "core:file:hhhhhhhhhhhh@src/small.py"
    huge = "core:file:hhhhhhhhhhhh@src/" + "x" * 600 + ".py"
    with clarion_stub(max_body_bytes=256, sei_supported=True, sei_by_locator={small: "loomweave:eid:1", huge: "loomweave:eid:2"}) as (
        base_url,
        state,
    ):
        registry = LoomweaveRegistry(base_url, timeout_seconds=5)
        try:
            result = registry.resolve_entity_content_hashes([huge, small])
        finally:
            registry.close()
    assert state.rejected_body_bytes  # the lone oversize locator was refused
    assert result["resolved"] == {small: f"sha256:{small}"}
    assert result["unresolved"] == [huge]
    assert "lineage_unavailable" not in result


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
