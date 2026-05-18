"""Default registry-backend matrix for ADR-014 file identity behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from filigree.core import FiligreeDB
from filigree.registry import DEFAULT_TEST_REGISTRY_BACKENDS


@pytest.mark.parametrize("registry_backend", DEFAULT_TEST_REGISTRY_BACKENDS)
def test_register_file_round_trips_default_registry_backend(tmp_path: Path, registry_backend: str) -> None:
    db = FiligreeDB(tmp_path / "filigree.db", prefix="test")
    try:
        db.initialize()

        file_record = db.register_file("src/default_backend.py", language="python")

        assert registry_backend == "local"
        assert file_record.registry_backend == registry_backend
        assert file_record.content_hash == ""
    finally:
        db.close()


@pytest.mark.parametrize("registry_backend", DEFAULT_TEST_REGISTRY_BACKENDS)
def test_scan_ingest_round_trips_default_registry_backend(tmp_path: Path, registry_backend: str) -> None:
    db = FiligreeDB(tmp_path / "filigree.db", prefix="test")
    try:
        db.initialize()

        result = db.process_scan_results(
            scan_source="ruff",
            findings=[
                {
                    "path": "src/default_backend.py",
                    "language": "python",
                    "rule_id": "E501",
                    "severity": "low",
                    "message": "Line too long",
                }
            ],
        )

        assert registry_backend == "local"
        file_record = db.get_file_by_path("src/default_backend.py")
        assert file_record is not None
        assert file_record.registry_backend == registry_backend
        assert file_record.content_hash == ""
        finding = db.get_finding(result["new_finding_ids"][0])
        assert finding["file_id"] == file_record.id
    finally:
        db.close()


@pytest.mark.parametrize("registry_backend", DEFAULT_TEST_REGISTRY_BACKENDS)
def test_observation_file_path_round_trips_default_registry_backend(tmp_path: Path, registry_backend: str) -> None:
    db = FiligreeDB(tmp_path / "filigree.db", prefix="test")
    try:
        db.initialize()

        db.create_observation(summary="Observed", file_path="src/default_backend.py")

        assert registry_backend == "local"
        file_record = db.get_file_by_path("src/default_backend.py")
        assert file_record is not None
        assert file_record.registry_backend == registry_backend
        assert file_record.content_hash == ""
    finally:
        db.close()
