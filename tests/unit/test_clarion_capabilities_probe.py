"""ADR-014 §4 capability-probe handshake tests (Filigree-side F-1).

Covers the four exit criteria the post-implementation review named:

1. Probe runs at ``FiligreeDB.__init__`` when ``registry_backend='clarion'``.
2. ``api_version`` mismatch raises (no fallback can rescue a wire-break).
3. ``instance_id`` rotation between probes flips the dashboard banner state.
4. Probe failure interacts correctly with ``allow_local_fallback``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from filigree.core import FiligreeDB
from filigree.registry import (
    EXPECTED_CLARION_API_VERSION,
    RegistryUnavailableError,
    RegistryVersionMismatchError,
    probe_clarion_capabilities,
    validate_clarion_capabilities,
)
from filigree.registry_errors import registry_error_response
from filigree.types.api import ErrorCode
from tests._fakes.clarion_http import clarion_stub


def test_probe_returns_capabilities_payload_with_typed_shape() -> None:
    with clarion_stub(instance_id="probe-instance") as (base_url, _state):
        capabilities = probe_clarion_capabilities(base_url, timeout_seconds=1)

    assert capabilities["instance_id"] == "probe-instance"
    assert capabilities["api_version"] == EXPECTED_CLARION_API_VERSION
    assert capabilities["registry_backend"] is True
    assert capabilities["file_registry"] is True


def test_probe_rejects_unreachable_url_with_unavailable_error() -> None:
    # ``http://127.0.0.1:1`` is a reserved port that should immediately refuse.
    with pytest.raises(RegistryUnavailableError) as exc:
        probe_clarion_capabilities("http://127.0.0.1:1", timeout_seconds=0.1)
    assert exc.value.cause_kind in {"network", "http_error"}


def test_validate_raises_on_api_version_mismatch() -> None:
    with clarion_stub(api_version=EXPECTED_CLARION_API_VERSION + 1) as (base_url, _state):
        capabilities = probe_clarion_capabilities(base_url, timeout_seconds=1)
        with pytest.raises(RegistryVersionMismatchError) as exc:
            validate_clarion_capabilities(capabilities, base_url=base_url)

    assert exc.value.advertised == EXPECTED_CLARION_API_VERSION + 1
    assert exc.value.expected == EXPECTED_CLARION_API_VERSION


def test_registry_error_response_maps_api_version_mismatch() -> None:
    exc = RegistryVersionMismatchError(
        "Clarion advertised an incompatible registry API",
        url="http://clarion.test/api/v1/_capabilities",
        expected=EXPECTED_CLARION_API_VERSION,
        advertised=EXPECTED_CLARION_API_VERSION + 1,
    )

    response = registry_error_response(exc, action="resolving file identity")

    assert response["code"] == ErrorCode.CLARION_REGISTRY_VERSION_MISMATCH
    assert response["details"] == {
        "cause": "clarion_registry_version_mismatch",
        "url": "http://clarion.test/api/v1/_capabilities",
        "expected": EXPECTED_CLARION_API_VERSION,
        "advertised": EXPECTED_CLARION_API_VERSION + 1,
    }


def test_validate_raises_when_clarion_declines_registry_backend_role() -> None:
    with clarion_stub(registry_backend=False) as (base_url, _state):
        capabilities = probe_clarion_capabilities(base_url, timeout_seconds=1)
        with pytest.raises(RegistryUnavailableError) as exc:
            validate_clarion_capabilities(capabilities, base_url=base_url)

    assert exc.value.cause_kind == "role_declined"


def test_filigree_db_startup_probe_captures_instance_id_and_api_version(tmp_path: Path) -> None:
    with clarion_stub(instance_id="instance-startup") as (base_url, state):
        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            registry_backend="clarion",
            clarion_config={"base_url": base_url, "timeout_seconds": 1},
        )
        try:
            db.initialize()
            assert db.clarion_instance_id == "instance-startup"
            assert db.clarion_api_version == EXPECTED_CLARION_API_VERSION
            assert db.clarion_instance_rotated is False
            assert db.clarion_capabilities is not None
            assert state.capability_requests == 1
        finally:
            db.close()


def test_filigree_db_startup_raises_on_api_version_mismatch(tmp_path: Path) -> None:
    with clarion_stub(api_version=EXPECTED_CLARION_API_VERSION + 1) as (base_url, _state), pytest.raises(RegistryVersionMismatchError):
        FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            registry_backend="clarion",
            clarion_config={"base_url": base_url, "timeout_seconds": 1},
        )


def test_filigree_db_startup_raises_on_version_mismatch_even_with_fallback(tmp_path: Path) -> None:
    """Version mismatch is a wire-protocol break — no fallback can rescue it."""
    with clarion_stub(api_version=EXPECTED_CLARION_API_VERSION + 1) as (base_url, _state), pytest.raises(RegistryVersionMismatchError):
        FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            registry_backend="clarion",
            clarion_config={
                "base_url": base_url,
                "timeout_seconds": 1,
                "allow_local_fallback": True,
            },
        )


def test_filigree_db_startup_probe_failure_without_fallback_aborts_init(tmp_path: Path) -> None:
    with pytest.raises(RegistryUnavailableError):
        FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            registry_backend="clarion",
            clarion_config={"base_url": "http://127.0.0.1:1", "timeout_seconds": 0.1},
        )


def test_filigree_db_startup_probe_failure_with_fallback_downgrades_to_warn(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="filigree.core"):
        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            registry_backend="clarion",
            clarion_config={
                "base_url": "http://127.0.0.1:1",
                "timeout_seconds": 0.1,
                "allow_local_fallback": True,
            },
        )
        try:
            db.initialize()
            assert db.clarion_instance_id is None
            assert db.clarion_api_version is None
            assert "Clarion capability probe failed at startup" in caplog.text
        finally:
            db.close()


def test_reprobe_detects_instance_id_rotation_and_flips_banner(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mid-session re-probe flips ``clarion_instance_rotated`` when Clarion was re-indexed."""
    with clarion_stub(instance_id="instance-original") as (base_url, state):
        state.rotate_after_capability_probes = 1
        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            registry_backend="clarion",
            clarion_config={"base_url": base_url, "timeout_seconds": 1},
        )
        try:
            db.initialize()
            assert db.clarion_instance_id == "instance-original"
            assert db.clarion_instance_rotated is False

            with caplog.at_level(logging.WARNING, logger="filigree.core"):
                result = db.reprobe_clarion_capabilities()

            assert result is not None
            assert result["instance_id"] == "test-clarion-instance-rotated"
            assert db.clarion_instance_id == "test-clarion-instance-rotated"
            assert db.clarion_instance_rotated is True
            assert "Clarion instance_id rotated mid-session" in caplog.text
        finally:
            db.close()


def test_reprobe_with_matching_instance_id_does_not_set_rotation_flag(tmp_path: Path) -> None:
    with clarion_stub(instance_id="instance-stable") as (base_url, _state):
        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            registry_backend="clarion",
            clarion_config={"base_url": base_url, "timeout_seconds": 1},
        )
        try:
            db.initialize()
            assert db.clarion_instance_rotated is False
            db.reprobe_clarion_capabilities()
            assert db.clarion_instance_rotated is False
            assert db.clarion_instance_id == "instance-stable"
        finally:
            db.close()


def test_reprobe_returns_none_for_local_backend(tmp_path: Path) -> None:
    db = FiligreeDB(tmp_path / "filigree.db", prefix="test")
    try:
        db.initialize()
        assert db.reprobe_clarion_capabilities() is None
    finally:
        db.close()


def test_skip_capability_probe_leaves_state_unset(tmp_path: Path) -> None:
    """Tests that point at non-Clarion HTTP servers can opt out via the explicit flag."""
    db = FiligreeDB(
        tmp_path / "filigree.db",
        prefix="test",
        registry_backend="clarion",
        clarion_config={"base_url": "http://clarion.test", "timeout_seconds": 0.1},
        skip_clarion_capability_probe=True,
    )
    try:
        db.initialize()
        assert db.clarion_instance_id is None
        assert db.clarion_api_version is None
    finally:
        db.close()
