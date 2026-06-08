"""Outbound client to the Legis governance service (B5).

Legis governs sign-offs; Filigree owns issue state. Before closing a
*governed* issue, Filigree calls Legis's read-only, fail-closed
closure-gate and refuses the close if Legis does not allow it. Filigree
never verifies any signature — it has no key; Legis owns governance.

Configuration is environment-driven and "invisible until wanted":

- ``LEGIS_URL`` unset → governance is OFF (:func:`is_configured` is False
  and :func:`check_closure_gate` returns ``NOT_CONFIGURED`` without any
  network call).
- ``LEGIS_API_TOKEN`` (optional) → sent as a ``Bearer`` token, mirroring
  the scanner client's ``post_to_api``.

The client is stdlib-only (``urllib.request``), modelled on
``scanner_scripts/scan_utils.py`` and ``hooks.py``. It maps the gate's
HTTP contract to a typed :class:`LegisGateResult` and never raises on a
network failure — an unreachable Legis degrades to ``UNREACHABLE`` so the
caller (not the transport) decides the fail-closed policy.

Wire contract consumed (Legis side — do not change here):
    GET {LEGIS_URL}/filigree/issues/{issue_id}/closure-gate
      200 → allowed            {"allowed": true,  "reason", "evidence"}
      409 → blocked            {"allowed": false, "reason", "evidence": null}
      404 → ledger not enabled (Legis configured without an HMAC key)
      500 → integrity failure  (tampered ledger — fail closed, surface)

Only an exact ``500`` is read as Legis's integrity signal. Any other 5xx
(``502``/``503``/``504`` — a restarting Legis or an interposed proxy / load
balancer) is a transport failure, not a ledger-tamper claim, and degrades to
``UNREACHABLE`` so the caller's fail-closed policy and batch short-circuit apply.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

LEGIS_URL_ENV = "LEGIS_URL"
LEGIS_TOKEN_ENV = "LEGIS_API_TOKEN"  # noqa: S105 - env var name, not a token value
DEFAULT_TIMEOUT_SECONDS = 5.0


class LegisGateStatus(Enum):
    """Typed outcome of a closure-gate request.

    Transport-level classification only — the governed/ungoverned policy
    decision lives in :mod:`filigree.governance`.
    """

    ALLOWED = "allowed"
    BLOCKED = "blocked"
    NOT_ENABLED = "not_enabled"
    INTEGRITY_FAILURE = "integrity_failure"
    UNREACHABLE = "unreachable"
    # A 2xx whose body does not affirm allowed=true — Legis *answered*, so this is
    # NOT a connectivity failure. Distinct from UNREACHABLE so a single malformed
    # answer fails closed for that one issue without short-circuiting a whole
    # cascade batch the way a Legis-down verdict does (the answer cost a fast 2xx,
    # not a timeout, so re-asking the next issue is cheap and correct).
    INVALID_RESPONSE = "invalid_response"
    NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class LegisGateResult:
    """Result of :func:`check_closure_gate`."""

    status: LegisGateStatus
    reason: str = ""
    evidence: dict[str, Any] | None = None


def legis_base_url() -> str | None:
    """Return the configured Legis base URL, or None when governance is OFF."""
    raw = os.environ.get(LEGIS_URL_ENV, "").strip()
    return raw or None


def is_configured() -> bool:
    """True when ``LEGIS_URL`` is set (governance is in play)."""
    return legis_base_url() is not None


def check_closure_gate(issue_id: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> LegisGateResult:
    """Ask Legis whether *issue_id* may be closed.

    Never raises on a network failure: a timeout / connection error
    degrades to ``UNREACHABLE`` so the caller applies the fail-closed
    policy. ``issue_id`` is Filigree's own id (the namespace Legis
    received at attach time).
    """
    base = legis_base_url()
    if base is None:
        return LegisGateResult(LegisGateStatus.NOT_CONFIGURED)
    url = f"{base.rstrip('/')}/filigree/issues/{issue_id}/closure-gate"
    headers = {"Accept": "application/json"}
    token = os.environ.get(LEGIS_TOKEN_ENV, "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310 (URL is operator-configured)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (URL is operator-configured)
            body = _read_json(resp.read())
            # Fail closed unless the 2xx body explicitly affirms allowed=true.
            # The wire contract is 200={"allowed": true}=allow / 409=blocked, so
            # any 2xx that does not carry allowed=true is a contract violation:
            # an empty/unparseable body ({} from _read_json), allowed=false, or a
            # non-true value (incl. an interposed proxy/cache/captive-portal 2xx).
            # Trusting it would defeat the gate's fail-closed posture (DECISION 2).
            # Do not echo body['reason'] here — an interposed 2xx could inject a
            # reassuring message; name the contract violation instead. (B7)
            if body.get("allowed") is not True:
                logger.warning("Legis closure-gate returned 2xx without allowed=true for %s", issue_id)
                # INVALID_RESPONSE, not UNREACHABLE: Legis answered (a 2xx), so it is
                # reachable — the defect is in THIS response, not connectivity. Mapping
                # it to UNREACHABLE would let one malformed answer poison the rest of a
                # cascade batch (the batch short-circuits after a Legis-down verdict).
                # Fail closed for this issue only; the next issue still gets its own gate.
                return LegisGateResult(
                    LegisGateStatus.INVALID_RESPONSE,
                    reason="Legis 2xx did not affirm allowed=true (wire-contract violation; fail-closed)",
                )
            return LegisGateResult(
                LegisGateStatus.ALLOWED,
                reason=str(body.get("reason", "")),
                evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else None,
            )
    except urllib.error.HTTPError as exc:
        return _classify_http_error(exc)
    except (urllib.error.URLError, OSError) as exc:
        # Timeout, connection refused, DNS failure — degrade, do not hang.
        logger.warning("Legis closure-gate unreachable for %s: %s", issue_id, exc)
        return LegisGateResult(LegisGateStatus.UNREACHABLE, reason=f"Legis unreachable: {exc}")


def _read_json(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _classify_http_error(exc: urllib.error.HTTPError) -> LegisGateResult:
    body: dict[str, Any] = {}
    try:
        body = _read_json(exc.read())
    except OSError:
        body = {}
    reason = str(body.get("reason", ""))
    if exc.code == 409:
        return LegisGateResult(
            LegisGateStatus.BLOCKED,
            reason=reason or "blocked by Legis closure-gate",
            evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else None,
        )
    if exc.code == 404:
        return LegisGateResult(LegisGateStatus.NOT_ENABLED, reason=reason or "Legis binding ledger not enabled")
    if exc.code == 500:
        # Per the wire contract (module docstring), Legis emits **exactly** 500
        # for a tampered/integrity-failed ledger — fail closed and surface it.
        return LegisGateResult(
            LegisGateStatus.INTEGRITY_FAILURE,
            reason=reason or "Legis binding integrity failure (HTTP 500)",
        )
    if exc.code > 500:
        # 502/503/504 etc. are transport/gateway failures (Legis restarting, or a
        # proxy / load balancer in front of it) — NOT a ledger-integrity signal.
        # Degrade to UNREACHABLE so a governed-close batch cascade short-circuits
        # after one timeout, rather than mislabelling each issue as INTEGRITY_FAILURE
        # (a per-issue verdict that deliberately never short-circuits — see
        # finding_issue_cascade and governance.GateOutcome.STALE notes).
        logger.warning("Legis closure-gate gateway/transport error: HTTP %s", exc.code)
        return LegisGateResult(LegisGateStatus.UNREACHABLE, reason=reason or f"Legis gateway/transport error (HTTP {exc.code})")
    # Any other unexpected status: fail closed conservatively as unreachable.
    logger.warning("Legis closure-gate unexpected status %s", exc.code)
    return LegisGateResult(LegisGateStatus.UNREACHABLE, reason=f"Unexpected Legis status {exc.code}")
