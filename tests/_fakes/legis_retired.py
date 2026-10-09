"""Test helper for the retired-Legis closure gate (Task 0.2, M-7 / HTTP F1).

Legis is archived: with ``LEGIS_URL`` set the closure gate must classify governed
issues but never reach Legis. :func:`governance_on` turns governance ON and makes
any Legis network use fail the test loudly, so a regression that re-introduces the
probe cannot pass silently.
"""

from __future__ import annotations

import pytest

from filigree import legis_client

ARCHIVED_WARNING = "governance_provider_archived: LEGIS_URL is set but Legis is retired; the close proceeded without an external gate"


def governance_on(monkeypatch: pytest.MonkeyPatch, url: str = "http://legis.test") -> None:
    """Set ``LEGIS_URL`` and make the Legis client and its urllib opener raise if touched."""
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, url)

    def _no_network(*_a: object, **_k: object) -> None:
        raise AssertionError("network call attempted although Legis is archived")

    monkeypatch.setattr(legis_client, "check_closure_gate", _no_network)
    monkeypatch.setattr(legis_client._OPENER, "open", _no_network)
