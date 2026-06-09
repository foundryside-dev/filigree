"""Tests for the stdlib Legis closure-gate client (B5).

Exercises the real urllib request path against a ThreadingHTTPServer stub —
status-to-result mapping plus the no-hang timeout guarantee. Filigree never
verifies any signature here; it only consumes Legis's read-only decision.
"""

from __future__ import annotations

import pytest

from filigree import legis_client
from filigree.legis_client import LegisGateStatus
from tests._fakes.legis_http import legis_redirect_to_sink, legis_stub


def _set_url(monkeypatch: pytest.MonkeyPatch, url: str | None) -> None:
    if url is None:
        monkeypatch.delenv(legis_client.LEGIS_URL_ENV, raising=False)
    else:
        monkeypatch.setenv(legis_client.LEGIS_URL_ENV, url)


def _set_token(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    monkeypatch.setenv(legis_client.LEGIS_TOKEN_ENV, token)


def test_unset_url_returns_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_url(monkeypatch, None)
    assert not legis_client.is_configured()
    result = legis_client.check_closure_gate("iss-1")
    assert result.status is LegisGateStatus.NOT_CONFIGURED


def test_200_maps_to_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    with legis_stub() as (url, state):
        state.status = 200
        state.body = {"allowed": True, "reason": "verified", "evidence": {"signoff_seq": 3}}
        _set_url(monkeypatch, url)
        result = legis_client.check_closure_gate("iss-1")
    assert result.status is LegisGateStatus.ALLOWED
    assert result.evidence == {"signoff_seq": 3}


def test_200_allowed_false_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 carrying ``allowed: false`` violates the wire contract (409 means
    blocked), so it must NOT read as ALLOWED. It fails closed as INVALID_RESPONSE
    — Legis *answered*, so this is a per-issue contract violation, not a
    connectivity failure (it must not short-circuit a cascade batch). (B7)"""
    with legis_stub() as (url, state):
        state.status = 200
        state.body = {"allowed": False, "reason": "no verified binding"}
        _set_url(monkeypatch, url)
        result = legis_client.check_closure_gate("iss-1")
    assert result.status is LegisGateStatus.INVALID_RESPONSE


def test_200_empty_body_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 with no ``allowed`` field — an empty/unparseable body (``{}`` from
    ``_read_json``) or an interposed proxy/cache 2xx — must not be silently
    treated as an allow. INVALID_RESPONSE (per-issue), not UNREACHABLE (B7)."""
    with legis_stub() as (url, state):
        state.status = 200
        state.body = {}
        _set_url(monkeypatch, url)
        result = legis_client.check_closure_gate("iss-1")
    assert result.status is LegisGateStatus.INVALID_RESPONSE


def test_200_non_true_allowed_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """``allowed`` must be the JSON ``true`` literal: a truthy string or ``1`` is
    a contract violation, not an allow — no truthiness coercion on a security
    gate. INVALID_RESPONSE (per-issue), not UNREACHABLE (B7)."""
    with legis_stub() as (url, state):
        state.status = 200
        state.body = {"allowed": "true", "reason": "stringly-typed"}
        _set_url(monkeypatch, url)
        result = legis_client.check_closure_gate("iss-1")
    assert result.status is LegisGateStatus.INVALID_RESPONSE


def test_409_maps_to_blocked_with_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    with legis_stub() as (url, state):
        state.status = 409
        state.body = {"allowed": False, "reason": "no verified binding"}
        _set_url(monkeypatch, url)
        result = legis_client.check_closure_gate("iss-1")
    assert result.status is LegisGateStatus.BLOCKED
    assert "no verified binding" in result.reason


def test_404_maps_to_not_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    with legis_stub() as (url, state):
        state.status = 404
        state.body = {}
        _set_url(monkeypatch, url)
        result = legis_client.check_closure_gate("iss-1")
    assert result.status is LegisGateStatus.NOT_ENABLED


def test_500_maps_to_integrity_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    with legis_stub() as (url, state):
        state.status = 500
        state.body = {"reason": "tampered ledger"}
        _set_url(monkeypatch, url)
        result = legis_client.check_closure_gate("iss-1")
    assert result.status is LegisGateStatus.INTEGRITY_FAILURE


@pytest.mark.parametrize("status", [502, 503, 504])
def test_transient_5xx_maps_to_unreachable(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """A 502/503/504 is a transport/gateway failure (restarting Legis or an
    interposed proxy), NOT a ledger-tamper claim. It must degrade to UNREACHABLE
    — only an exact 500 is the integrity signal. Mislabelling these as
    INTEGRITY_FAILURE defeats the cascade's one-timeout-per-batch short-circuit,
    since INTEGRITY_FAILURE is a per-issue verdict that never short-circuits."""
    with legis_stub() as (url, state):
        state.status = status
        state.body = {"reason": "gateway error"}
        _set_url(monkeypatch, url)
        result = legis_client.check_closure_gate("iss-1")
    assert result.status is LegisGateStatus.UNREACHABLE


def test_connection_refused_maps_to_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing listening on this port → fast connection error → UNREACHABLE.
    _set_url(monkeypatch, "http://127.0.0.1:1")
    result = legis_client.check_closure_gate("iss-1", timeout=1.0)
    assert result.status is LegisGateStatus.UNREACHABLE


def test_slow_legis_does_not_hang(monkeypatch: pytest.MonkeyPatch) -> None:
    """The no-hang guarantee lives in urlopen(timeout=…): a slow Legis
    returns UNREACHABLE within the timeout instead of blocking forever."""
    with legis_stub() as (url, state):
        state.delay_seconds = 5.0
        _set_url(monkeypatch, url)
        result = legis_client.check_closure_gate("iss-1", timeout=0.3)
    assert result.status is LegisGateStatus.UNREACHABLE


def test_non_redirecting_gate_still_sends_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bearer IS sent on a normal (non-redirecting) gate call — the redirect
    hardening must not regress ordinary token auth. (B3)"""
    with legis_stub() as (url, state):
        state.status = 200
        state.body = {"allowed": True, "reason": "ok"}
        _set_url(monkeypatch, url)
        _set_token(monkeypatch, "secret-token")
        result = legis_client.check_closure_gate("iss-1")
    assert result.status is LegisGateStatus.ALLOWED
    assert state.auth_headers == ["Bearer secret-token"]


def test_redirect_does_not_leak_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 302 from Legis must NOT re-send the bearer to the redirect target.

    urllib's default redirect handler copies request headers (minus
    content-length/content-type) onto the redirect with no same-origin check,
    so a malicious/open-redirecting Legis could 302-exfiltrate the bearer to an
    attacker host. The redirect is still followed (no fail-closed regression),
    but the Authorization header must be stripped. (B3)
    """
    with legis_redirect_to_sink() as (url, sink):
        _set_url(monkeypatch, url)
        _set_token(monkeypatch, "secret-token")
        legis_client.check_closure_gate("iss-1")
    # Redirect was followed (the request reached the sink)...
    assert sink.requests == ["/filigree/issues/iss-1/closure-gate"]
    # ...but the bearer was NOT carried across it.
    assert sink.auth_headers == [None]


def test_non_http_scheme_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-http(s) LEGIS_URL is refused before any request / bearer attach —
    fail closed with a scheme-named reason rather than letting urllib handle a
    file:// (or other) scheme. (B3)"""
    _set_url(monkeypatch, "file:///etc/passwd")
    _set_token(monkeypatch, "secret-token")
    result = legis_client.check_closure_gate("iss-1")
    assert result.status is LegisGateStatus.UNREACHABLE
    assert "scheme" in result.reason.lower()
