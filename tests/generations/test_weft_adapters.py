"""Unit tests for filigree.generations.weft.adapters.

Covers the shape transformations that feed the weft generation's HTTP
responses. The tests here pin the adapter's key-set and field routing
against the fixture at ``tests/fixtures/contracts/weft/scan-results.json``.
When Phase C1 mounts ``POST /api/weft/scan-results``, these adapter
tests plus the fixture-backed parity tests (added in B4) together gate
the weft-generation contract.
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
            failed=[],
            unchanged=[],
            requested=0,
            applied=0,
        )
        weft = scan_ingest_result_to_weft(result)

        # Top-level envelope keys.
        assert set(weft.keys()) == {"succeeded", "failed", "unchanged", "stats", "warnings"}
        assert weft["unchanged"] == []
        assert weft["succeeded"] == []
        assert weft["failed"] == []
        assert weft["warnings"] == []

        # Stats key-set mirrors ScanIngestResult minus new_finding_ids + warnings.
        assert set(weft["stats"].keys()) == {
            "files_created",
            "files_updated",
            "findings_created",
            "findings_updated",
            "observations_created",
            "observations_failed",
            "requested",
            "applied",
        }
        assert all(weft["stats"][k] == 0 for k in weft["stats"])

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
            failed=[],
            unchanged=[],
            requested=1,
            applied=1,
        )
        weft = scan_ingest_result_to_weft(result)

        # new_finding_ids routes to succeeded.
        assert weft["succeeded"] == ["sf_abc123"]
        # Counts route to stats.
        assert weft["stats"]["files_created"] == 1
        assert weft["stats"]["findings_created"] == 1
        # warnings stays at top level.
        assert weft["warnings"] == ["unknown severity 'xxx' coerced to 'info'"]
        assert weft["failed"] == []
        assert weft["stats"]["requested"] == 1
        assert weft["stats"]["applied"] == 1

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
            failed=[],
            unchanged=[],
            requested=0,
            applied=0,
        )
        weft = scan_ingest_result_to_weft(result)
        weft["succeeded"].append("sf_two")
        weft["warnings"].append("w2")
        assert ids == ["sf_one"]
        assert warnings == ["w1"]

    def test_failed_and_unchanged_populate_the_wire(self) -> None:
        """HTTP F2: per-finding failures and replays are surfaced, in independent lists."""
        failure = {"index": 2, "fingerprint": "fp-x", "code": "OVER_CAP", "reason": "too big"}
        unchanged = {"id": "sf_old", "reason": "already_present"}
        result = ScanIngestResult(
            files_created=0,
            files_updated=1,
            findings_created=0,
            findings_updated=1,
            new_finding_ids=[],
            observations_created=0,
            observations_failed=0,
            warnings=["operator text"],
            failed=[failure],
            unchanged=[unchanged],
            requested=3,
            applied=1,
        )
        weft = scan_ingest_result_to_weft(result)

        assert weft["failed"] == [failure]
        assert weft["unchanged"] == [unchanged]
        assert weft["stats"]["requested"] == 3
        assert weft["stats"]["applied"] == 1
        # Independent copies: mutating the wire must not leak back.
        weft["failed"][0]["code"] = "MUTATED"
        assert failure["code"] == "OVER_CAP"
