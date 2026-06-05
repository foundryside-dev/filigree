"""Smoke-test the pinned staging Loomweave HTTP deployment.

The local live-Loomweave tests spawn a ``clarion`` binary from PATH. This check
covers the release-governance gap where the deployed wire surface drifts even
when the local binary lane still passes. It is optional for contributor runs,
but required in the scheduled/manual live lane via
``FILIGREE_REQUIRE_LIVE_CLARION=1``.
"""

from __future__ import annotations

import os

import pytest

from filigree.registry import (
    RegistryUnavailableError,
    RegistryVersionMismatchError,
    normalize_loomweave_base_url,
    probe_loomweave_capabilities,
    validate_loomweave_capabilities,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
]


def _live_unavailable(reason: str) -> None:
    if os.environ.get("FILIGREE_REQUIRE_LIVE_CLARION") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


def test_pinned_staging_loomweave_capabilities_are_compatible() -> None:
    raw_base_url = os.environ.get("CLARION_STAGING_BASE_URL", "").strip()
    if not raw_base_url:
        _live_unavailable("CLARION_STAGING_BASE_URL is required for the scheduled Live Loomweave Integration lane")

    try:
        base_url = normalize_loomweave_base_url(raw_base_url)
    except ValueError as exc:
        _live_unavailable(f"CLARION_STAGING_BASE_URL is invalid: {exc}")

    try:
        capabilities = probe_loomweave_capabilities(base_url, timeout_seconds=10)
        validate_loomweave_capabilities(capabilities, base_url=base_url)
    except (RegistryUnavailableError, RegistryVersionMismatchError) as exc:
        _live_unavailable(f"staging Loomweave capability probe failed: {exc}")
