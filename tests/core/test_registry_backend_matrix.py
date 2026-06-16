"""Default registry-backend matrix for ADR-014 file identity behavior."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest

import filigree.registry as registry_module
from filigree.core import FiligreeDB
from filigree.registry import DEFAULT_TEST_REGISTRY_BACKENDS, RegistryBackend, ResolvedFile
from tests._fakes.clarion_http import ClarionStubState, clarion_stub


class _DisplacedRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
        self.calls.append((path, language, actor))
        return cast(
            ResolvedFile,
            {
                "file_id": f"loomweave:file:{path.replace('/', ':')}",
                "content_hash": f"hash:{path}",
                "canonical_path": path,
                "language": language,
                "registry_backend": "loomweave",
            },
        )

    def is_displaced(self) -> bool:
        return True


@contextmanager
def _matrix_db(tmp_path: Path, registry_backend: RegistryBackend) -> Iterator[tuple[FiligreeDB, ClarionStubState | None]]:
    if registry_backend == "loomweave":
        with clarion_stub() as (base_url, state):
            db = FiligreeDB(
                tmp_path / "filigree.db",
                prefix="test",
                registry_backend="loomweave",
                loomweave_config={"base_url": base_url, "timeout_seconds": 1},
                project_root=tmp_path,
            )
            try:
                db.initialize()
                yield db, state
            finally:
                db.close()
        return

    db = FiligreeDB(tmp_path / "filigree.db", prefix="test", project_root=tmp_path)
    try:
        db.initialize()
        yield db, None
    finally:
        db.close()


def _expected_file_id(registry_backend: RegistryBackend, path: str) -> str:
    return f"core:file:stub@{path}" if registry_backend == "loomweave" else ""


def _assert_registry_file_record(db: FiligreeDB, registry_backend: RegistryBackend, path: str) -> None:
    file_record = db.get_file_by_path(path)
    assert file_record is not None
    assert file_record.registry_backend == registry_backend
    if registry_backend == "loomweave":
        assert file_record.id == _expected_file_id(registry_backend, path)
        assert file_record.content_hash == f"sha256:{path}"
    else:
        assert file_record.id.startswith("test-f-")
        assert file_record.content_hash == ""


def test_default_registry_backend_matrix_covers_local_and_loomweave() -> None:
    assert DEFAULT_TEST_REGISTRY_BACKENDS == ("local", "loomweave")
    assert not hasattr(registry_module, "SUPPORTED_REGISTRY_BACKENDS")


@pytest.mark.parametrize("registry_backend", DEFAULT_TEST_REGISTRY_BACKENDS)
def test_register_file_round_trips_default_registry_backend(tmp_path: Path, registry_backend: RegistryBackend) -> None:
    with _matrix_db(tmp_path, registry_backend) as (db, state):
        file_record = db.register_file("src/default_backend.py", language="python")

        _assert_registry_file_record(db, registry_backend, "src/default_backend.py")
        assert file_record.id == db.get_file_by_path("src/default_backend.py").id  # type: ignore[union-attr]
        if registry_backend == "loomweave":
            assert state is not None
            assert state.file_requests == [{"path": ["src/default_backend.py"], "language": ["python"]}]


@pytest.mark.parametrize("registry_backend", DEFAULT_TEST_REGISTRY_BACKENDS)
def test_scan_ingest_round_trips_default_registry_backend(tmp_path: Path, registry_backend: RegistryBackend) -> None:
    with _matrix_db(tmp_path, registry_backend) as (db, state):
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

        _assert_registry_file_record(db, registry_backend, "src/default_backend.py")
        finding = db.get_finding(result["new_finding_ids"][0])
        file_record = db.get_file_by_path("src/default_backend.py")
        assert file_record is not None
        assert finding["file_id"] == file_record.id
        if registry_backend == "loomweave":
            assert state is not None
            # CONTRACT-1: scan-results batches via POST /api/v1/files/batch.
            assert state.file_requests == []
            assert state.batch_requests == [{"queries": [{"path": "src/default_backend.py", "language": "python"}]}]


@pytest.mark.parametrize("registry_backend", DEFAULT_TEST_REGISTRY_BACKENDS)
def test_observation_file_path_round_trips_default_registry_backend(tmp_path: Path, registry_backend: RegistryBackend) -> None:
    with _matrix_db(tmp_path, registry_backend) as (db, state):
        db.create_observation(summary="Observed", file_path="src/default_backend.py")

        _assert_registry_file_record(db, registry_backend, "src/default_backend.py")
        if registry_backend == "loomweave":
            assert state is not None
            assert state.file_requests == [{"path": ["src/default_backend.py"], "language": ["python"]}]


@pytest.mark.parametrize("registry_backend", DEFAULT_TEST_REGISTRY_BACKENDS)
def test_annotation_file_path_round_trips_default_registry_backend(tmp_path: Path, registry_backend: RegistryBackend) -> None:
    with _matrix_db(tmp_path, registry_backend) as (db, state):
        source = tmp_path / "src" / "default_backend.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("alpha\nbeta\n")

        annotation = db.annotate_file("src/default_backend.py", "Read beta", line_start=2, actor="annotator")

        _assert_registry_file_record(db, registry_backend, "src/default_backend.py")
        file_record = db.get_file_by_path("src/default_backend.py")
        assert file_record is not None
        assert annotation["file_id"] == file_record.id
        if registry_backend == "loomweave":
            assert state is not None
            assert state.file_requests == [{"path": ["src/default_backend.py"], "language": ["python"]}]


def test_implicit_auto_create_paths_thread_displaced_registry_ids(tmp_path: Path) -> None:
    registry = _DisplacedRegistry()
    db = FiligreeDB(
        tmp_path / "filigree.db",
        prefix="test",
        registry=registry,
        registry_backend="loomweave",
        project_root=tmp_path,
    )
    try:
        db.initialize()
        source = tmp_path / "src" / "annotated.py"
        source.parent.mkdir()
        source.write_text("alpha\nbeta\n")

        registered = db.register_file("src/registered.py", language="python", actor="direct")
        observation = db.create_observation("Observed", file_path="src/observed.py", actor="observer")
        annotation = db.annotate_file("src/annotated.py", "Read beta", line_start=2, actor="annotator")
        scan = db.process_scan_results(
            scan_source="ruff",
            findings=[
                {
                    "path": "src/scanned.py",
                    "language": "python",
                    "rule_id": "E501",
                    "severity": "low",
                    "message": "Line too long",
                }
            ],
            observation_actor="scanner",
        )

        assert registered.id == "loomweave:file:src:registered.py"
        assert observation["file_id"] == "loomweave:file:src:observed.py"
        assert annotation["file_id"] == "loomweave:file:src:annotated.py"
        finding = db.get_finding(scan["new_finding_ids"][0])
        assert finding["file_id"] == "loomweave:file:src:scanned.py"

        for path in ("src/registered.py", "src/observed.py", "src/annotated.py", "src/scanned.py"):
            file_record = db.get_file_by_path(path)
            assert file_record is not None
            assert file_record.registry_backend == "loomweave"
            assert file_record.content_hash == f"hash:{path}"
    finally:
        db.close()
