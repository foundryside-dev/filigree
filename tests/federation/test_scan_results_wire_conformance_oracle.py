"""Consumer-side scan-results wire conformance oracle.

Wardline is the PRODUCER of the ``POST /api/weft/scan-results`` request body:
``core.filigree_emit.build_scan_results_body`` authors the wire, and Wardline
freezes that wire to a committed golden + a PRODUCER-SOURCE recheck (its
``test_filigree_scan_results_wire_golden.py`` regenerates the body from fixed
inputs and asserts it ties to the golden, so the byte-pin is not circular).

Filigree is the CONSUMER: its ``POST /api/weft/scan-results`` handler must
parse, accept, persist, and round-trip exactly that wire. This module is the
missing *consumer* half of "both peers load the shared corpus" — it proves
Filigree genuinely ingests Wardline's authoritative wire through its REAL intake
code path, not a restatement of the golden against itself.

Three layers, mirroring the SEI oracle (``test_sei_conformance_oracle.py``) and
the suppression-filter oracle (``test_suppression_filter_conformance_oracle.py``):

- **Layer 1 — byte-pin (default suite).** ``UPSTREAM_BLOB_SHA`` is the git-blob
  sha of the vendored golden; an unmarked test recomputes it from bytes. Any
  edit to the vendored fixture reds this immediately, in every CI run.
- **Consumer intake oracle (the non-circular core, default suite).** Drives the
  golden body through Filigree's REAL scan-results intake — the exact code the
  ``/api/weft/scan-results`` route runs: ``_parse_scan_results_body`` (the
  shared request validator) then ``FiligreeDB.process_scan_results`` (the ingest
  primitive). It then reads the persisted findings back through the public query
  surfaces (``find_finding_by_fingerprint``, ``list_findings_global``) and
  asserts the wire's findings, fingerprints, nested ``metadata.wardline.*`` axes,
  and suppression/kind distribution all round-tripped. This reads the real
  intake, not the golden against itself: if Filigree's parser rejected a field
  the wire carries, or the ingest dropped/garbled a finding or its nested
  axes, this reds.
- **Stage 0 kind policy (Task 0.5a).** The golden carries Wardline telemetry
  kinds (``fact`` / ``classification`` / ``metric`` / ``suggestion``) beside its
  defects. The round-trip tests below are about the LEGACY full-population
  intake, so they set ``scan_ingest.accept_kinds = ["*"]`` first;
  ``test_default_policy_rejects_golden_telemetry_kinds`` pins that the default
  policy rejects exactly the golden's non-defect rows per-finding.
- **Layer 2 — drift recheck (release-gate, skip-clean).** Lives in
  ``test_sibling_drift.py`` (registry entry ``wardline_scan_results``):
  byte-compares the vendored copy against Wardline's authority source
  (``tests/conformance/fixtures/wardline-scan-results-wire.golden.json`` in the
  sibling checkout located by ``_oracle.sibling_source``). Skips cleanly when the
  sibling is absent unless ``FILIGREE_REQUIRE_WARDLINE_REPO`` arms it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from filigree.core import FiligreeDB

# The exact request validator + ingest primitive the live
# ``POST /api/weft/scan-results`` route funnels through (see
# ``create_weft_router`` / ``api_weft_scan_results`` in
# ``filigree.dashboard_routes.files``: it calls ``_parse_scan_results_body`` then
# hands the parsed kwargs to ``db.process_scan_results``). Driving these two is
# driving the real intake — the route adds only the HTTP envelope and the
# worker-thread hop, not any parsing/persistence logic of its own.
from filigree.dashboard_routes.files import _parse_scan_results_body
from filigree.db_files import NON_DEFECT_WARDLINE_FINDING_KINDS
from tests._db_factory import set_scan_ingest_accept_kinds
from tests.federation._oracle import blob_sha, load_golden

pytestmark = pytest.mark.federation_contract

# The vendored consumer copy of Wardline's authoritative scan-results wire.
GOLDEN_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "contracts" / "wardline-scan-results-wire.golden.json"

# Git-blob sha1 of the vendored golden (``sha1(b"blob %d\0" % len + data)``).
# Recomputed from bytes by ``test_vendored_golden_byte_pin`` so any edit to the
# fixture reds in the default suite, on every CI run.
UPSTREAM_BLOB_SHA = "164404bea8a8c29eec9814156441c38a098b9fc8"


# ---------------------------------------------------------------------------
# Layer 1 — byte-pin (default suite)
# ---------------------------------------------------------------------------


def test_vendored_golden_byte_pin() -> None:
    """The vendored golden's bytes hash to the pinned git-blob sha.

    A single-byte edit to the fixture changes the sha and reds this test in the
    default suite — the cheapest possible drift tripwire, no sibling repo needed.
    """
    assert blob_sha(GOLDEN_PATH.read_bytes()) == UPSTREAM_BLOB_SHA


# ---------------------------------------------------------------------------
# Consumer intake oracle — the non-circular core (default suite)
# ---------------------------------------------------------------------------


def test_real_intake_accepts_golden_body() -> None:
    """Filigree's REAL request validator accepts Wardline's wire unchanged.

    ``_parse_scan_results_body`` is the single shared validator every
    scan-results route (classic ``/api/v1``, weft ``/api/weft``, the living
    ``/api`` alias) runs before any persistence. Feeding it the golden body must
    yield the parsed ingest-kwargs dict, NOT a ``_ScanResultsBodyError`` — i.e.
    the consumer accepts every field the producer emits. If Wardline added a
    field Filigree's validator rejects (or tightened a bound the wire trips),
    this reds.
    """
    golden = load_golden(GOLDEN_PATH)
    parsed = _parse_scan_results_body(golden)
    # A validation failure is a dataclass instance, not a plain kwargs dict.
    assert isinstance(parsed, dict), f"intake rejected the golden wire: {parsed!r}"
    # The parsed kwargs carry the wire's top-level scan envelope through to the
    # ingest primitive verbatim.
    assert parsed["scan_source"] == golden["scan_source"]
    assert parsed["fingerprint_scheme"] == golden["fingerprint_scheme"]
    assert parsed["mark_unseen"] == golden["mark_unseen"]
    assert parsed["scanned_paths"] == golden["scanned_paths"]
    assert len(parsed["findings"]) == len(golden["findings"])


def test_real_intake_persists_and_round_trips_golden(tmp_path: Path) -> None:
    """Drive the golden body through Filigree's REAL ingest and read it back.

    This is the non-circular core: it exercises the exact ingest primitive the
    ``/api/weft/scan-results`` route calls (``process_scan_results``) on the
    parsed wire, then reads the persisted findings back through the public query
    surfaces — NOT by restating the golden against itself. Every wire finding
    must persist; each fingerprint must resolve to its rule; and the nested
    ``metadata.wardline.*`` axes (kind, qualname) must survive the round-trip.

    If Filigree's intake dropped a finding, mangled a fingerprint, or flattened
    the nested wardline metadata, this reds.
    """
    golden = load_golden(GOLDEN_PATH)
    parsed = _parse_scan_results_body(golden)
    assert isinstance(parsed, dict)

    # Legacy full-population intake: accept every wardline kind (Stage 0 opt-out).
    set_scan_ingest_accept_kinds(tmp_path, ["*"])
    db = FiligreeDB(tmp_path / "filigree.db", prefix="test")
    db.initialize()
    try:
        result = db.process_scan_results(**parsed)
        # ``process_scan_results`` normalises the finding dicts IN PLACE (path,
        # severity, language), and ``parsed["findings"]`` aliases the golden's
        # list — so every comparison below must run against a pristine re-read,
        # or intake-time mangling could never red.
        golden = load_golden(GOLDEN_PATH)

        # Every wire finding was ingested (none dropped at the boundary).
        assert result["findings_created"] == len(golden["findings"])

        # HTTP F2: the per-finding outcome keys are REQUIRED on the ingest result
        # (the weft adapter lifts them onto the wire), and a clean first ingest of
        # Wardline's golden reports nothing failed or replayed.
        assert result["failed"] == []
        assert result["unchanged"] == []
        assert result["rejected_by_kind"] == 0
        assert result["requested"] == len(golden["findings"])
        assert result["applied"] == result["findings_created"] + result["findings_updated"] == len(golden["findings"])

        # The full population round-trips through the project-wide query the
        # ``/api/weft/findings`` route serves. ``suppression="all"`` keeps the
        # suppressed rows (baselined/waived) in the count.
        listed = db.list_findings_global(suppression="all", limit=1000)
        assert listed["total"] == len(golden["findings"])

        # Each wire finding resolves by its own ``(scan_source, fingerprint)``
        # identity — the same lookup ``/api/weft/findings/promote`` keys on — and
        # its rule_id and nested wardline axes survived the round-trip.
        scan_source = golden["scan_source"]
        for wire_finding in golden["findings"]:
            fingerprint = wire_finding["fingerprint"]
            persisted = db.find_finding_by_fingerprint(scan_source, fingerprint)
            assert persisted is not None, f"fingerprint not persisted: {fingerprint}"
            assert persisted["rule_id"] == wire_finding["rule_id"]
            assert persisted["message"] == wire_finding["message"]
            assert persisted["severity"] == wire_finding["severity"]

            wire_wardline = wire_finding["metadata"]["wardline"]
            persisted_md = persisted.get("metadata")
            assert isinstance(persisted_md, dict), f"metadata not persisted for {fingerprint}"
            persisted_wardline = persisted_md.get("wardline")
            assert isinstance(persisted_wardline, dict), f"wardline metadata not persisted for {fingerprint}"
            # EVERY wire ``metadata.wardline.*`` axis must round-trip untouched —
            # not just the indexed kind/qualname filter axes. A consumer that
            # persisted only the indexed axes and dropped the rest (confidence,
            # internal_severity, properties, related_entities, suppression_*) would
            # otherwise stay green. Subset direction is wire ⊆ persisted (Filigree
            # MAY add its own keys); per-key equality, not set ``items() <=``, since
            # ``properties`` (dict) / ``related_entities`` (list) are unhashable.
            for key, wire_value in wire_wardline.items():
                assert persisted_wardline.get(key) == wire_value, (
                    f"metadata.wardline[{key!r}] did not round-trip for {fingerprint}: "
                    f"wire={wire_value!r} persisted={persisted_wardline.get(key)!r}"
                )

        # The wire's per-finding ``path`` was genuinely consumed at the boundary:
        # each distinct scanned path became a tracked file record.
        listed_files = db.list_files(limit=1000)
        tracked_paths = {f.path for f in listed_files}
        assert {wf["path"] for wf in golden["findings"]} <= tracked_paths
    finally:
        db.close()


def test_real_intake_round_trips_suppression_and_kind_axes(tmp_path: Path) -> None:
    """The wire's nested suppression/kind axes are queryable post-ingest.

    Wardline OWNS the ``metadata.wardline.suppression_state`` and ``.kind``
    vocabularies; Filigree CONSUMES them as server-side finding-list filter axes
    (the ``suppression`` / ``kind`` filters on ``list_findings_global``, which
    the ``/api/weft/findings`` route exposes). After ingesting the golden, those
    nested axes must be filterable and partition the population exactly as the
    wire carries it — proving the consumer didn't just store the metadata blob
    but wired it into its real query grammar.

    The expected counts are DERIVED from the golden (not hard-coded), so the
    assertion stays tied to the wire: if a future wire revision changes the
    suppression/kind mix, the derived expectation tracks it, and a consumer that
    failed to index a new axis value reds.
    """
    golden = load_golden(GOLDEN_PATH)
    parsed = _parse_scan_results_body(golden)
    assert isinstance(parsed, dict)

    # Derive the expected per-axis distribution straight from the wire.
    expected_supp: dict[str, int] = {}
    expected_kind: dict[str, int] = {}
    for wire_finding in golden["findings"]:
        wardline = wire_finding["metadata"]["wardline"]
        # Absent suppression_state means an un-suppressed (active) finding.
        supp = wardline.get("suppression_state", "active")
        expected_supp[supp] = expected_supp.get(supp, 0) + 1
        kind = wardline.get("kind")
        if kind is not None:
            expected_kind[kind] = expected_kind.get(kind, 0) + 1

    # Legacy full-population intake: accept every wardline kind (Stage 0 opt-out).
    set_scan_ingest_accept_kinds(tmp_path, ["*"])
    db = FiligreeDB(tmp_path / "filigree.db", prefix="test")
    db.initialize()
    try:
        db.process_scan_results(**parsed)

        # Suppression axis: each owned state filters to exactly the wire's count.
        for state, count in expected_supp.items():
            got = db.list_findings_global(suppression=state, limit=1000)
            assert got["total"] == count, f"suppression={state!r}: expected {count}, got {got['total']}"

        # Kind axis: each owned kind filters to exactly the wire's count.
        for kind, count in expected_kind.items():
            got = db.list_findings_global(kind=kind, suppression="all", limit=1000)
            assert got["total"] == count, f"kind={kind!r}: expected {count}, got {got['total']}"
    finally:
        db.close()


def test_replayed_golden_reports_every_finding_unchanged(tmp_path: Path) -> None:
    """Re-POSTing Wardline's golden is a pure replay: every finding is reported ``unchanged``.

    The golden is fingerprint-keyed, so the second ingest matches each stored row
    by ``(scan_source, fingerprint)`` with identical content. The producer must be
    able to tell that from a batch that landed nothing: ``new_finding_ids`` is
    empty, ``unchanged`` lists every previously-created id, and ``requested`` /
    ``applied`` still account for the whole batch.
    """
    golden = load_golden(GOLDEN_PATH)
    first_parsed = _parse_scan_results_body(golden)
    assert isinstance(first_parsed, dict)

    # Legacy full-population intake: accept every wardline kind (Stage 0 opt-out).
    set_scan_ingest_accept_kinds(tmp_path, ["*"])
    db = FiligreeDB(tmp_path / "filigree.db", prefix="test")
    db.initialize()
    try:
        first = db.process_scan_results(**first_parsed)
        replay_parsed = _parse_scan_results_body(load_golden(GOLDEN_PATH))
        assert isinstance(replay_parsed, dict)
        replay = db.process_scan_results(**replay_parsed)

        assert replay["new_finding_ids"] == []
        assert replay["failed"] == []
        assert replay["unchanged"] == [{"id": fid, "reason": "already_present"} for fid in first["new_finding_ids"]]
        assert replay["requested"] == len(first["new_finding_ids"])
        assert replay["applied"] == replay["findings_updated"] == replay["requested"]
    finally:
        db.close()


def test_default_policy_rejects_golden_telemetry_kinds(tmp_path: Path) -> None:
    """Under the default ``accept_kinds`` (``["defect"]``) the golden's telemetry rows are refused per-finding.

    Expectations are DERIVED from the wire: every finding whose
    ``metadata.wardline.kind`` is a non-defect kind (or whose path is the
    ``<engine>`` pseudo-path) must come back in ``failed[]`` as
    ``KIND_NOT_ACCEPTED`` at its request index, and only the defects persist.
    """
    golden = load_golden(GOLDEN_PATH)
    parsed = _parse_scan_results_body(golden)
    assert isinstance(parsed, dict)
    golden = load_golden(GOLDEN_PATH)
    telemetry_indices = [
        index
        for index, f in enumerate(golden["findings"])
        if f["metadata"]["wardline"].get("kind") in NON_DEFECT_WARDLINE_FINDING_KINDS or f["path"] == "<engine>"
    ]
    # The golden must actually exercise the policy, or this test proves nothing.
    assert telemetry_indices
    assert len(telemetry_indices) < len(golden["findings"])

    db = FiligreeDB(tmp_path / "filigree.db", prefix="test")
    db.initialize()
    try:
        result = db.process_scan_results(**parsed)

        assert [(f["index"], f["fingerprint"], f["code"]) for f in result["failed"]] == [
            (index, golden["findings"][index]["fingerprint"], "KIND_NOT_ACCEPTED") for index in telemetry_indices
        ]
        assert result["rejected_by_kind"] == len(telemetry_indices)
        assert result["requested"] == len(golden["findings"])
        defects = len(golden["findings"]) - len(telemetry_indices)
        assert result["findings_created"] == result["applied"] == defects
        listed = db.list_findings_global(suppression="all", limit=1000)
        assert listed["total"] == defects
        for index in telemetry_indices:
            assert db.find_finding_by_fingerprint(golden["scan_source"], golden["findings"][index]["fingerprint"]) is None
    finally:
        db.close()
