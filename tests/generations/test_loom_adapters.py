"""Unit tests for filigree.generations.weft.adapters.

Covers the shape transformations that feed the loom generation's HTTP
responses. The tests here pin the adapter's key-set and field routing
against the fixture at ``tests/fixtures/contracts/loom/scan-results.json``.
When Phase C1 mounts ``POST /api/loom/scan-results``, these adapter
tests plus the fixture-backed parity tests (added in B4) together gate
the loom-generation contract.
"""

from __future__ import annotations

from filigree.generations.weft.adapters import scan_ingest_result_to_weft
from filigree.types.files import ScanIngestResult


class TestScanIngestResultToWeft:
    def test_empty_findings_clean_scan(self) -> None:
        result = ScanIngestResult(
            files_created=0,
            files_updated=0,
            findings_created=0,
            findings_updated=0,
            new_finding_ids=[],
            observations_created=0,
            observations_failed=0,
            warnings=[],
        )
        loom = scan_ingest_result_to_weft(result)

        # Top-level envelope keys.
        assert set(loom.keys()) == {"succeeded", "failed", "stats", "warnings"}
        assert loom["succeeded"] == []
        assert loom["failed"] == []
        assert loom["warnings"] == []

        # Stats key-set mirrors ScanIngestResult minus new_finding_ids + warnings.
        assert set(loom["stats"].keys()) == {
            "files_created",
            "files_updated",
            "findings_created",
            "findings_updated",
            "observations_created",
            "observations_failed",
        }
        assert all(loom["stats"][k] == 0 for k in loom["stats"])

    def test_one_finding_populates_succeeded_and_stats(self) -> None:
        result = ScanIngestResult(
            files_created=1,
            files_updated=0,
            findings_created=1,
            findings_updated=0,
            new_finding_ids=["sf_abc123"],
            observations_created=0,
            observations_failed=0,
            warnings=["unknown severity 'xxx' coerced to 'info'"],
        )
        loom = scan_ingest_result_to_weft(result)

        # new_finding_ids routes to succeeded.
        assert loom["succeeded"] == ["sf_abc123"]
        # Counts route to stats.
        assert loom["stats"]["files_created"] == 1
        assert loom["stats"]["findings_created"] == 1
        # warnings stays at top level.
        assert loom["warnings"] == ["unknown severity 'xxx' coerced to 'info'"]
        # failed always present as [] in 2.0.
        assert loom["failed"] == []

    def test_adapter_does_not_alias_input_lists(self) -> None:
        """Adapter returns independent lists so mutation-after-adapt does not
        leak back into the caller's ScanIngestResult."""
        ids = ["sf_one"]
        warnings = ["w1"]
        result = ScanIngestResult(
            files_created=0,
            files_updated=0,
            findings_created=0,
            findings_updated=0,
            new_finding_ids=ids,
            observations_created=0,
            observations_failed=0,
            warnings=warnings,
        )
        loom = scan_ingest_result_to_weft(result)
        loom["succeeded"].append("sf_two")
        loom["warnings"].append("w2")
        assert ids == ["sf_one"]
        assert warnings == ["w1"]
