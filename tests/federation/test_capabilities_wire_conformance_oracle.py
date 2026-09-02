"""Consumer-side capabilities wire conformance oracle.

Loomweave is the PRODUCER of the ``GET /api/v1/_capabilities`` response body: its
``get_capabilities`` handler authors the wire, and Loomweave freezes that wire to
a committed golden + a PRODUCER-SOURCE recheck that is NOT circular (its
``crates/loomweave-cli/tests/serve.rs`` re-invokes the live handler and asserts
the produced body ties to the golden, plus a blake3 byte-pin
``capabilities_golden_bytes_match_layer1_pin`` and a single-byte tamper proof
``capabilities_golden_byte_pin_rejects_a_mutated_byte``).

Filigree is the CONSUMER: at registry-backend startup (and on reprobe) it issues
that GET and must parse, accept, and version-gate exactly this wire. The real
consumer sequence lives in ``filigree.core`` (``_probe_and_validate`` at
``core.py:1937-1938`` and ``reprobe_loomweave_capabilities`` at
``core.py:1988-1989``): both call ``probe_loomweave_capabilities`` (HTTP +
shape-parse) immediately followed by ``validate_loomweave_capabilities``
(the api_version gate + registry-backend role check). This module is the missing
*consumer* half of "both peers load the shared corpus" — it proves Filigree
genuinely ingests Loomweave's authoritative wire through its REAL parse/validate
code path, not a restatement of the golden against itself.

To exercise the genuine parse path (and not a hand-extracted slice), the oracle
serves the golden body over a real loopback ``http.server`` and points the REAL
``probe_loomweave_capabilities`` at it — driving the production ``httpx`` client,
the ``json.loads`` parse, every inline shape check, and ``_parse_sei_capability``
exactly as a live Loomweave would. It then drives ``validate_loomweave_capabilities``
on the parsed result, mirroring the back-to-back production call sequence.

Three layers, mirroring the sibling federation oracles
(``test_scan_results_wire_conformance_oracle.py`` /
``test_suppression_filter_conformance_oracle.py`` /
``test_entity_associations_wire_conformance_oracle.py``):

- **Layer 1 — byte-pin (default suite).** ``UPSTREAM_BLOB_SHA`` is the git-blob
  sha of the vendored golden; an unmarked test recomputes it from bytes, plus a
  single-byte tamper proof. Any edit to the vendored fixture reds immediately, in
  every CI run.
- **Consumer parse/validate oracle (the non-circular core, default suite).**
  Serves the golden over loopback HTTP and drives the REAL
  ``probe_loomweave_capabilities`` + ``validate_loomweave_capabilities``: asserts
  the valid wire is ACCEPTED and the consumer-extracted fields tie to the golden's
  declared values, that an ``api_version`` gate violation raises
  ``RegistryVersionMismatchError``, and that a type/shape violation hard-fails
  with ``RegistryUnavailableError(cause_kind='invalid_response')``. These are real
  RED targets driven through the production code path.
- **Consumer auth posture (ADR-056, fixture v6).** The golden's normative
  ``authentication`` block (``{protected_routes, capabilities_probe,
  contract_version}``) is CONSUMED: the probe shape-parses it into
  ``caps["authentication"]`` and ``validate_loomweave_capabilities`` gates the
  mode vocabulary. Filigree sends only ``Authorization: Bearer`` and owns no HMAC
  contract, so ``protected_routes`` outside ``{none, bearer}`` or a
  ``capabilities_probe`` other than ``none`` is refused with one named
  ``RegistryUnavailableError(cause_kind='auth_mode_unsupported')`` at the
  probe/validate pair instead of surfacing later as per-operation 401s. A
  Loomweave that omits the block (pre-ADR-056) is treated as
  ``none``/``none``/``contract_version 1`` and keeps working; a malformed block
  is a shape break (``invalid_response``).
- **Layer 2 — drift recheck against Loomweave's authority source.** Lives in
  ``test_sibling_drift.py`` (registry entry ``capabilities``): byte-compares the
  vendored copy against Loomweave's producer golden
  (``docs/federation/fixtures/get-api-v1-capabilities.json`` in the sibling
  checkout located by ``_oracle.sibling_source``). Skip-clean when the sibling
  is absent unless ``FILIGREE_REQUIRE_LOOMWEAVE_REPO`` arms it.

Byte-identical provenance: the vendored copy's
sha256 ``61020b20aadaef75a3de523f0a8f83be03d1d503ffdca719c78d949d20beeced``
is identical to the producer report's sha256 for the authority golden.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

# The exact pair the live registry-backend startup gate funnels through (see
# ``LoomweaveRegistry._probe_and_validate`` / ``reprobe_loomweave_capabilities``
# in ``filigree.core``: ``probe_loomweave_capabilities`` immediately followed by
# ``validate_loomweave_capabilities``). Driving these two over real loopback HTTP
# is driving the real consumer intake — the production call site adds only the
# fallback-policy try/except around this same pair.
from filigree.registry import (
    EXPECTED_LOOMWEAVE_API_VERSION,
    LEGACY_LOOMWEAVE_AUTHENTICATION,
    RegistryUnavailableError,
    RegistryVersionMismatchError,
    probe_loomweave_capabilities,
    validate_loomweave_capabilities,
)
from tests.federation._oracle import blob_sha, load_golden

pytestmark = pytest.mark.federation_contract

# The vendored consumer copy of Loomweave's authoritative capabilities wire.
GOLDEN_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "contracts" / "get-api-v1-capabilities.json"

# Git-blob sha1 of the vendored golden (``sha1(b"blob %d\0" % len + data)``).
# Recomputed from bytes by ``test_vendored_golden_byte_pin`` so any edit to the
# fixture reds in the default suite, on every CI run.
UPSTREAM_BLOB_SHA = "c739e2a64550856de77668e70d9eb7faf413b43b"

# sha256 of the authority golden, copied verbatim from the producer report. A
# byte-identical vendoring reproduces it; recomputed by the byte-pin test as a
# cross-check independent of the git-blob convention.
UPSTREAM_SHA256 = "61020b20aadaef75a3de523f0a8f83be03d1d503ffdca719c78d949d20beeced"


def _golden_body() -> dict[str, Any]:
    """The ``capabilities_200`` example response body the consumer parses."""
    golden = load_golden(GOLDEN_PATH)
    for example in golden["examples"]:
        if example["name"] == "capabilities_200":
            body: dict[str, Any] = example["response"]["body"]
            return body
    raise AssertionError("missing golden example capabilities_200")


# ---------------------------------------------------------------------------
# Loopback HTTP harness — serves a body so the REAL probe parses real bytes
# ---------------------------------------------------------------------------


class _CapabilitiesServer:
    """A minimal loopback HTTP server that returns a fixed JSON body on GET.

    Used so ``probe_loomweave_capabilities`` exercises its genuine ``httpx``
    client + parse path against real wire bytes, exactly as it would against a
    live Loomweave ``serve`` — never a hand-extracted dict short-circuiting the
    parser.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args: Any) -> None:  # silence test server logging
                return

        # Bind loopback so ``probe_loomweave_capabilities`` accepts it as a
        # loopback origin (token-origin guard is a no-op here since we send no token).
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self._thread.start()
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host}:{port}"

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Layer 1 — byte-pin (default suite)
# ---------------------------------------------------------------------------


def test_vendored_golden_byte_pin() -> None:
    """The vendored golden's bytes hash to the pinned git-blob sha (and sha256).

    A single-byte edit to the fixture changes both digests and reds this test in
    the default suite — the cheapest possible drift tripwire, no sibling repo
    needed. The sha256 cross-check ties the vendored bytes to the producer
    report's authority digest.
    """
    data = GOLDEN_PATH.read_bytes()
    assert blob_sha(data) == UPSTREAM_BLOB_SHA
    assert hashlib.sha256(data).hexdigest() == UPSTREAM_SHA256


def test_vendored_golden_byte_pin_rejects_a_mutated_byte() -> None:
    """Tamper proof: flipping one byte of the golden produces a git-blob sha that
    does NOT match the pin, demonstrating the byte-pin is load-bearing (mirrors
    the producer's ``capabilities_golden_byte_pin_rejects_a_mutated_byte``)."""
    tampered = bytearray(GOLDEN_PATH.read_bytes())
    tampered[0] ^= 0x01
    assert blob_sha(bytes(tampered)) != UPSTREAM_BLOB_SHA
    assert hashlib.sha256(bytes(tampered)).hexdigest() != UPSTREAM_SHA256


# ---------------------------------------------------------------------------
# Consumer parse/validate oracle — the non-circular core (default suite)
# ---------------------------------------------------------------------------


def test_real_probe_accepts_golden_wire() -> None:
    """Filigree's REAL probe + validate ACCEPT Loomweave's golden wire unchanged.

    Serves the golden body over loopback HTTP and drives
    ``probe_loomweave_capabilities`` (the genuine ``httpx`` GET + ``json.loads`` +
    every inline shape check + ``_parse_sei_capability``) then
    ``validate_loomweave_capabilities`` — the exact back-to-back pair the
    startup gate runs. The consumer-EXTRACTED fields must tie to the golden's
    declared values. Non-circular: the assertion can only pass because the live
    parser genuinely extracted these values from the served wire bytes.

    Only the fields the consumer extracts are asserted — including the ADR-056
    ``authentication`` block (fixture v6), which the consumer now extracts
    verbatim. ``probe`` deliberately ignores ``linkages`` and ``taint_store``
    (and would ignore a ``version`` field), so this does NOT assert they
    round-trip — asserting that would red against correct code.
    """
    body = _golden_body()
    with _CapabilitiesServer(body) as url:
        caps = probe_loomweave_capabilities(url, timeout_seconds=5)
        # Must not raise: the golden's api_version matches what this Filigree
        # was built for, and Loomweave advertises the registry-backend role.
        validate_loomweave_capabilities(caps, base_url=url)

    # The consumer-extracted fields tie to the golden body's declared values.
    assert caps["registry_backend"] == body["registry_backend"]
    assert caps["file_registry"] == body["file_registry"]
    assert caps["api_version"] == body["api_version"]
    assert caps["instance_id"] == body["instance_id"]
    # Nested ``sei`` object → flattened consumer fields (ADR-038).
    assert caps["sei_supported"] == body["sei"]["supported"]
    assert caps["sei_version"] == body["sei"]["version"]
    # ADR-056 ``authentication`` discovery block → extracted verbatim, through the
    # REAL httpx + parse path. The golden advertises ``none``/``none``/``1``,
    # which is exactly the posture this consumer accepts without a gate trip.
    assert caps["authentication"] == body["authentication"]
    assert caps["authentication"] == {"protected_routes": "none", "capabilities_probe": "none", "contract_version": 1}

    # Sanity: the golden's api_version is exactly the version this build gates on,
    # which is why the accept path above did not raise.
    assert caps["api_version"] == EXPECTED_LOOMWEAVE_API_VERSION


def test_real_probe_tolerates_unconsumed_extra_fields() -> None:
    """Postel tolerance is a DECISION, now pinned: the probe accepts a wire that
    carries fields it does not consume (a re-introduced top-level ``version`` and
    an unknown extra key) WITHOUT leaking them into ``caps`` and WITHOUT changing
    the fields it does extract.

    The producer side enforces the golden's ``forbidden_fields:[version]`` /
    ``allow_extra_fields:false`` shape_decl (``assert_body_matches_shape``); the
    CONSUMER probe is deliberately lenient (validates only the fields Filigree
    consumes — see the ``probe_loomweave_capabilities`` docstring). That tolerance
    was documented but untested, so a future tightening (or accidental field-leak)
    of the probe could not be detected here. This test makes the decision explicit
    and RED-on-regression:

    * the probe + validate ACCEPT the augmented body (no raise);
    * neither unconsumed field leaks into the returned ``caps`` dict; and
    * every consumer-extracted field is byte-for-byte identical to what the clean
      golden yields — proving the extra fields are inert, not silently mixed in.

    A ``deepcopy`` is augmented; the golden file is never touched. This widens
    nothing in production (it only confirms Filigree IGNORES what it already
    ignores) — it is a coverage pin on the Postel posture, not a tautology.
    """
    clean = _golden_body()
    # Baseline: what the consumer extracts from the pristine golden.
    with _CapabilitiesServer(clean) as url:
        baseline = probe_loomweave_capabilities(url, timeout_seconds=5)
        validate_loomweave_capabilities(baseline, base_url=url)

    augmented = copy.deepcopy(clean)
    # A re-introduced top-level ``version`` (the field the golden's shape_decl
    # forbids) and an arbitrary unknown extra key — both unconsumed by Filigree.
    augmented["version"] = "9.9.9"
    augmented["an_unknown_future_field"] = {"nested": [1, 2, 3]}

    with _CapabilitiesServer(augmented) as url:
        caps = probe_loomweave_capabilities(url, timeout_seconds=5)
        # Tolerated: the augmented body is accepted, not rejected.
        validate_loomweave_capabilities(caps, base_url=url)

    # The consumer-extracted fields are exactly what the clean golden yielded — the
    # extra fields are inert, neither dropped a real field nor perturbed one.
    # (``probe_loomweave_capabilities`` builds a fixed-key ``LoomweaveCapabilities``
    # literal, so an unconsumed field cannot leak by construction; this equality
    # is the load-bearing check.)
    assert caps == baseline, (
        f"extra unconsumed fields perturbed the consumer-extracted capabilities view:\n  clean={baseline!r}\n  augmented={caps!r}"
    )


def test_real_validate_rejects_api_version_gate_violation() -> None:
    """An ``api_version`` the consumer was not built for hard-fails the gate.

    ``probe_loomweave_capabilities`` accepts any integer ``api_version`` (it is a
    shape check, not a value gate); the version gate lives in
    ``validate_loomweave_capabilities``. Mutating ONLY ``api_version`` (a
    ``deepcopy`` of the golden body; the golden file is never touched) and driving
    the real pair must raise ``RegistryVersionMismatchError`` carrying the
    expected/advertised pair — the unmaskable wire-break signal.
    """
    bad = copy.deepcopy(_golden_body())
    bad["api_version"] = EXPECTED_LOOMWEAVE_API_VERSION + 1
    with _CapabilitiesServer(bad) as url:
        caps = probe_loomweave_capabilities(url, timeout_seconds=5)
        # Probe accepts the (well-shaped) higher version; the gate rejects it.
        with pytest.raises(RegistryVersionMismatchError) as excinfo:
            validate_loomweave_capabilities(caps, base_url=url)
    assert excinfo.value.expected == EXPECTED_LOOMWEAVE_API_VERSION
    assert excinfo.value.advertised == EXPECTED_LOOMWEAVE_API_VERSION + 1


def test_real_probe_rejects_type_shape_violation() -> None:
    """A malformed nested ``sei.supported`` hard-fails at the parse boundary.

    Mutating ``sei.supported`` to a non-boolean (a ``deepcopy``; the golden file
    is never touched) and serving it over loopback must make the REAL
    ``probe_loomweave_capabilities`` raise ``RegistryUnavailableError`` with
    ``cause_kind='invalid_response'`` — the consumer fails closed on a
    wire-contract shape break (``_parse_sei_capability``), never silently degrades.
    """
    bad = copy.deepcopy(_golden_body())
    bad["sei"]["supported"] = "yes"  # must be a bool on the wire
    with _CapabilitiesServer(bad) as url, pytest.raises(RegistryUnavailableError) as excinfo:
        probe_loomweave_capabilities(url, timeout_seconds=5)
    assert excinfo.value.cause_kind == "invalid_response"


def test_real_probe_rejects_wrong_typed_top_level_field() -> None:
    """A wrong-typed top-level ``registry_backend`` also hard-fails the parse.

    Second shape RED target, distinct from the nested-``sei`` one: a
    string-instead-of-bool ``registry_backend`` exercises the top-level boolean
    shape check (``bool_fields``) in ``probe_loomweave_capabilities``, proving the
    consumer fails closed across both the top-level and nested shape gates.
    """
    bad = copy.deepcopy(_golden_body())
    bad["registry_backend"] = "true"  # must be a bool on the wire
    with _CapabilitiesServer(bad) as url, pytest.raises(RegistryUnavailableError) as excinfo:
        probe_loomweave_capabilities(url, timeout_seconds=5)
    assert excinfo.value.cause_kind == "invalid_response"


# ---------------------------------------------------------------------------
# Consumer auth posture — ADR-056 ``authentication`` block (fixture v6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "mode"),
    [
        ("protected_routes", "hmac"),
        ("capabilities_probe", "bearer"),
    ],
)
def test_real_validate_rejects_unimplemented_auth_mode(field: str, mode: str) -> None:
    """A protected mode Filigree cannot satisfy is refused with ONE named error.

    Loomweave's ``AuthenticationMode`` vocabulary is ``none|bearer|hmac``
    (ADR-056 §1). Filigree only ever sends ``Authorization: Bearer`` and owns no
    HMAC contract, so an ``hmac`` ``protected_routes`` (or any
    ``capabilities_probe`` other than ``none``) must not be silently marked
    usable and then fail per-operation with ``cause_kind='auth'`` 401s. Mutating
    ONLY the one auth field (a ``deepcopy``; the golden file is never touched):
    the REAL probe ACCEPTS (it is a well-shaped block — shape check, not value
    gate) and ``validate_loomweave_capabilities`` raises
    ``RegistryUnavailableError(cause_kind='auth_mode_unsupported')`` naming the
    field and the offending mode.
    """
    bad = copy.deepcopy(_golden_body())
    bad["authentication"][field] = mode
    with _CapabilitiesServer(bad) as url:
        caps = probe_loomweave_capabilities(url, timeout_seconds=5)
        assert dict(caps["authentication"])[field] == mode
        with pytest.raises(RegistryUnavailableError) as excinfo:
            validate_loomweave_capabilities(caps, base_url=url)
    assert excinfo.value.cause_kind == "auth_mode_unsupported"
    assert f"authentication.{field}" in str(excinfo.value)
    assert repr(mode) in str(excinfo.value)


def test_real_probe_degrades_when_authentication_block_absent() -> None:
    """A pre-ADR-056 Loomweave (no ``authentication`` block) keeps working.

    Deleting the block (a ``deepcopy``; the golden file is never touched) must
    make the REAL probe + validate ACCEPT, with ``caps["authentication"]`` set to
    the legacy assumption (``none``/``none``/``contract_version 1``) and every
    other consumer-extracted field identical to the clean golden's baseline —
    the older-Loomweave posture degrades, it does not crash.
    """
    clean = _golden_body()
    with _CapabilitiesServer(clean) as url:
        baseline = probe_loomweave_capabilities(url, timeout_seconds=5)
        validate_loomweave_capabilities(baseline, base_url=url)

    bad = copy.deepcopy(clean)
    del bad["authentication"]
    with _CapabilitiesServer(bad) as url:
        caps = probe_loomweave_capabilities(url, timeout_seconds=5)
        validate_loomweave_capabilities(caps, base_url=url)

    assert caps["authentication"] == LEGACY_LOOMWEAVE_AUTHENTICATION
    assert caps["authentication"] == {"protected_routes": "none", "capabilities_probe": "none", "contract_version": 1}
    assert {k: v for k, v in caps.items() if k != "authentication"} == {k: v for k, v in baseline.items() if k != "authentication"}


def test_real_probe_rejects_malformed_authentication_block() -> None:
    """A present-but-malformed ``authentication`` block is a wire-shape break.

    ``authentication: "none"`` (a string where ADR-056 mandates an object) must
    make the REAL probe raise ``RegistryUnavailableError`` with
    ``cause_kind='invalid_response'`` — the consumer fails closed on a malformed
    advertisement, it does not guess at a mode.
    """
    bad = copy.deepcopy(_golden_body())
    bad["authentication"] = "none"
    with _CapabilitiesServer(bad) as url, pytest.raises(RegistryUnavailableError) as excinfo:
        probe_loomweave_capabilities(url, timeout_seconds=5)
    assert excinfo.value.cause_kind == "invalid_response"
    assert "'authentication' must be an object" in str(excinfo.value)
