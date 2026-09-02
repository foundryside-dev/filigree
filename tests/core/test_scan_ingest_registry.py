"""Scan-ingest handling of the registry's per-item ``errors`` channel.

``LoomweaveRegistry.resolve_files_batch`` reports a single query that cannot
fit under Loomweave's 16 KiB transport body cap on the ``errors`` channel with
``code=BODY_TOO_LARGE`` *instead of failing the batch* (filigree-b57d4eb7d9).
``process_scan_results`` must honour that per-item contract: the offending
path is reported unresolved via ``warnings`` and its findings are dropped,
while every neighbour in the same batch is ingested. Any other structured
error code keeps the fail-closed whole-batch rejection.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from filigree.core import FiligreeDB
from filigree.registry import (
    LOOMWEAVE_BATCH_BODY_CAP_BYTES,
    LOOMWEAVE_BATCH_BODY_TOO_LARGE_CODE,
    BatchQuery,
    BatchResolution,
    BatchResolutionError,
    RegistryResolutionError,
    ResolvedFile,
    resolve_files_batch_via_loop,
)
from filigree.types.core import make_entity_id
from tests._fakes.clarion_http import clarion_stub

PATHOLOGICAL_PATH = "src/" + "p" * (LOOMWEAVE_BATCH_BODY_CAP_BYTES + 100) + ".py"


def _finding(path: str, rule_id: str = "E501") -> dict[str, Any]:
    return {"path": path, "language": "python", "rule_id": rule_id, "severity": "low", "message": f"finding at {path}"}


class _ErrorChannelRegistry:
    """Registry fake whose batch resolve puts ``error_paths`` on the ``errors`` channel."""

    def __init__(self, *, code: str, error_paths: set[str], displaced: bool = True) -> None:
        self.code = code
        self.error_paths = error_paths
        self.displaced = displaced
        self.single_resolve_calls: list[str] = []

    def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
        self.single_resolve_calls.append(path)
        return cast(
            ResolvedFile,
            {
                "file_id": make_entity_id(f"core:file:stable@{path}"),
                "content_hash": "sha256:fake",
                "canonical_path": path,
                "language": language,
                "registry_backend": "loomweave",
            },
        )

    def resolve_files_batch(self, queries: list[BatchQuery], *, actor: str = "") -> BatchResolution:
        ok = [q for q in queries if q["path"] not in self.error_paths]
        batch = resolve_files_batch_via_loop(self, ok, actor=actor)
        for q in queries:
            if q["path"] in self.error_paths:
                msg = f"fake registry rejected {q['path']!r}"
                batch["errors"].append(BatchResolutionError(requested_path=q["path"], code=self.code, message=msg))
                batch["messages"][q["path"]] = msg
        return batch

    def is_displaced(self) -> bool:
        return self.displaced


def test_body_too_large_path_is_reported_unresolved_and_neighbours_are_ingested(tmp_path: Path) -> None:
    """One >16 KiB path among normal ones: warned + dropped; the rest resolve and ingest (fake server cap)."""
    neighbours = [f"src/ok_{i}.py" for i in range(5)]
    findings = [_finding(neighbours[0]), _finding(neighbours[1]), _finding(PATHOLOGICAL_PATH)]
    findings += [_finding(p) for p in neighbours[2:]]
    with clarion_stub(max_body_bytes=LOOMWEAVE_BATCH_BODY_CAP_BYTES) as (base_url, state):
        db = FiligreeDB(
            tmp_path / "filigree.db",
            prefix="test",
            registry_backend="loomweave",
            loomweave_config={"base_url": base_url, "timeout_seconds": 2},
        )
        try:
            db.initialize()

            result = db.process_scan_results(scan_source="ruff", findings=findings)

            assert result["files_created"] == len(neighbours)
            assert result["findings_created"] == len(neighbours)
            assert len(result["new_finding_ids"]) == len(neighbours)
            for path in neighbours:
                record = db.get_file_by_path(path)
                assert record is not None
                assert record.registry_backend == "loomweave"
            assert db.get_file_by_path(PATHOLOGICAL_PATH) is None
            assert len(result["warnings"]) == 1
            warning = result["warnings"][0]
            assert LOOMWEAVE_BATCH_BODY_TOO_LARGE_CODE in warning
            assert PATHOLOGICAL_PATH[:20] in warning
            assert "not ingested" in warning
        finally:
            db.close()
    # The pathological body never went on the wire, and the single-item
    # ``resolve_file`` fallback inside the write transaction was never tried.
    assert state.rejected_body_bytes == []
    assert all(q["path"] != PATHOLOGICAL_PATH for req in state.batch_requests for q in req["queries"])
    assert all(req.get("path") != PATHOLOGICAL_PATH for req in state.file_requests)


def test_body_too_large_with_no_ingestible_row_still_rejects_the_batch(tmp_path: Path) -> None:
    """A batch whose only path is over the cap has no neighbours to keep: fail closed as before."""
    registry = _ErrorChannelRegistry(code=LOOMWEAVE_BATCH_BODY_TOO_LARGE_CODE, error_paths={PATHOLOGICAL_PATH})
    db = FiligreeDB(tmp_path / "filigree.db", prefix="test", registry=registry, registry_backend="loomweave")
    try:
        db.initialize()
        with pytest.raises(RegistryResolutionError, match=LOOMWEAVE_BATCH_BODY_TOO_LARGE_CODE):
            db.process_scan_results(scan_source="ruff", findings=[_finding(PATHOLOGICAL_PATH)])
        assert registry.single_resolve_calls == []
        assert db.get_file_by_path(PATHOLOGICAL_PATH) is None
    finally:
        db.close()


def test_other_structured_error_codes_still_reject_the_whole_batch(tmp_path: Path) -> None:
    """Only BODY_TOO_LARGE is per-row; any other ``errors`` code keeps fail-closed batch semantics."""
    registry = _ErrorChannelRegistry(code="INVALID_PATH", error_paths={"src/bad.py"})
    db = FiligreeDB(tmp_path / "filigree.db", prefix="test", registry=registry, registry_backend="loomweave")
    try:
        db.initialize()
        with pytest.raises(RegistryResolutionError, match="INVALID_PATH"):
            db.process_scan_results(scan_source="ruff", findings=[_finding("src/ok.py"), _finding("src/bad.py")])
        assert db.get_file_by_path("src/ok.py") is None
        assert db.get_file_by_path("src/bad.py") is None
    finally:
        db.close()


def test_body_too_large_on_an_already_registered_path_keeps_its_findings(tmp_path: Path) -> None:
    """Displaced-registry refresh of a known path that no longer fits: findings ingest, metadata is not refreshed."""
    registry = _ErrorChannelRegistry(code=LOOMWEAVE_BATCH_BODY_TOO_LARGE_CODE, error_paths=set())
    db = FiligreeDB(tmp_path / "filigree.db", prefix="test", registry=registry, registry_backend="loomweave")
    try:
        db.initialize()
        first = db.process_scan_results(scan_source="ruff", findings=[_finding(PATHOLOGICAL_PATH, "E501")])
        assert first["files_created"] == 1
        assert first["warnings"] == []

        # The path now exceeds the cap on the refresh round-trip.
        registry.error_paths = {PATHOLOGICAL_PATH}
        second = db.process_scan_results(
            scan_source="ruff",
            findings=[_finding(PATHOLOGICAL_PATH, "E501"), _finding(PATHOLOGICAL_PATH, "E502"), _finding("src/ok.py")],
        )

        assert second["files_updated"] == 1
        assert second["files_created"] == 1
        assert second["findings_updated"] == 1
        assert second["findings_created"] == 2
        assert len(second["warnings"]) == 1
        assert LOOMWEAVE_BATCH_BODY_TOO_LARGE_CODE in second["warnings"][0]
        assert "not refreshed" in second["warnings"][0]
        # Neither ingest fell back to the single-item resolve inside the write window.
        assert PATHOLOGICAL_PATH not in registry.single_resolve_calls[1:]
        # The earlier E501 finding was seen again, so the mark-unseen sweep left it open.
        record = db.get_file_by_path(PATHOLOGICAL_PATH)
        assert record is not None
        findings = db.list_findings_global(file_id=record.id)["findings"]
        assert {f["rule_id"]: f["status"] for f in findings} == {"E501": "open", "E502": "open"}
    finally:
        db.close()
