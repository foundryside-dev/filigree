"""Tests for the stdlib Legis closure-gate client (B5).

Exercises the real urllib request path against a ThreadingHTTPServer stub —
status-to-result mapping plus the no-hang timeout guarantee. Filigree never
verifies any signature here; it only consumes Legis's read-only decision.
"""

from __future__ import annotations

import pytest

from filigree import legis_client
from filigree.legis_client import LegisGateStatus
from tests._fakes.legis_http import legis_stub


def _set_url(monkeypatch: pytest.MonkeyPatch, url: str | None) -> None:
    if url is None:
        monkeypatch.delenv(legis_client.LEGIS_URL_ENV, raising=False)
    else:
        monkeypatch.setenv(legis_client.LEGIS_URL_ENV, url)


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
    blocked), so it must NOT read as ALLOWED. The gate fails closed (B7)."""
    with legis_stub() as (url, state):
        state.status = 200
        state.body = {"allowed": False, "reason": "no verified binding"}
        _set_url(monkeypatch, url)
        result = legis_client.check_closure_gate("iss-1")
    assert result.status is LegisGateStatus.UNREACHABLE


def test_200_empty_body_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 with no ``allowed`` field — an empty/unparseable body (``{}`` from
    ``_read_json``) or an interposed proxy/cache 2xx — must not be silently
    treated as an allow (B7)."""
    with legis_stub() as (url, state):
        state.status = 200
        state.body = {}
        _set_url(monkeypatch, url)
        result = legis_client.check_closure_gate("iss-1")
    assert result.status is LegisGateStatus.UNREACHABLE


def test_200_non_true_allowed_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """``allowed`` must be the JSON ``true`` literal: a truthy string or ``1`` is
    a contract violation, not an allow — no truthiness coercion on a security
    gate (B7)."""
    with legis_stub() as (url, state):
        state.status = 200
        state.body = {"allowed": "true", "reason": "stringly-typed"}
        _set_url(monkeypatch, url)
        result = legis_client.check_closure_gate("iss-1")
    assert result.status is LegisGateStatus.UNREACHABLE


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
