"""Consumer-side Loomweave scan-results wire conformance oracle.

Loomweave is the PRODUCER of the cross-repo ``scan-results-lw`` seam: its
``loomweave analyze`` Phase 8 maps persisted findings onto Filigree's intake
schema via ``loomweave_federation::scan_results::prepare_batch`` /
``wire_finding`` (``scan_source="loomweave"``) and POSTs the assembled
``ScanResultsRequest`` body to ``POST /api/v1/scan-results``. Loomweave freezes
that wire to a committed golden plus a NON-CIRCULAR producer-source recheck (its
``crates/loomweave-federation/tests/scan_results_wire_conformance_oracle.rs``
re-invokes the REAL ``prepare_batch`` on fixed inputs and asserts the produced
body ties to the golden, so the byte-pin is not circular).

Filigree is the CONSUMER. This module is the missing *consumer* half: it vendors
Loomweave's authoritative golden BYTE-IDENTICAL and proves Filigree genuinely
ingests Loomweave's wire through its REAL intake code path — not a restatement
of the golden against itself.

The DISTINGUISHING contract this oracle pins (vs the sibling wardline
scan-results oracle, ``test_scan_results_wire_conformance_oracle.py``) is that
Loomweave's wire is **fingerprint-LESS / scheme-LESS / scanned_paths-LESS**:
Loomweave omits wardline's per-finding ``fingerprint`` + ``fingerprint_scheme``
and the request-level ``scanned_paths``, relying on Filigree to compute dedup
identity server-side. Each fingerprint-less finding therefore round-trips
through Filigree's LEGACY positional dedup branch
(``db_files.FilesMixin._upsert_finding`` ~line 1288: the
``WHERE file_id=? AND scan_source=? AND rule_id=? AND coalesce(line_start,-1)=?
AND fingerprint=''`` arm taken when ``fingerprint == ''``), with the nested
``metadata.loomweave.*`` axes preserved.

Layers, mirroring ``test_scan_results_wire_conformance_oracle.py`` and the
entity-associations oracle:

- **Layer 1 — byte-pin (default suite).** ``UPSTREAM_BLOB_SHA`` is the git-blob
  sha of the vendored golden; an unmarked test recomputes it from bytes. Any
  edit to the vendored fixture reds this immediately, in every CI run. A tamper
  test proves the pin is load-bearing.
- **Consumer intake oracle (the non-circular core, default suite).** Drives the
  golden body through Filigree's REAL intake — the exact code the
  ``/api/v1/scan-results`` (and ``/api/weft/scan-results``) route runs:
  ``_parse_scan_results_body`` (the shared request validator) then
  ``FiligreeDB.process_scan_results`` (the ingest primitive). It asserts BOTH:

  * the WHOLE golden is REJECTED at the boundary, because its synthetic-anchor
    row carries the absolute placeholder ``path="/repo/root"`` and Filigree's
    intake (``_is_project_relative_scan_path``) rejects absolute paths — this is
    by-design on BOTH sides (see the SEAM NOTE below), surfaced rather than
    buried; and
  * the PRODUCTION-REPRESENTATIVE subset (synthetic-anchor rows dropped, exactly
    as production Loomweave does — see the SEAM NOTE) ROUND-TRIPS through the
    legacy positional dedup path with ``fingerprint==''`` and the nested
    ``metadata.loomweave.*`` axes preserved.

- **Layer 2 — drift recheck (release-gate, skip-clean + fail-closed arming
  env).** Byte-compares the vendored copy against Loomweave's authority source
  (``$LOOMWEAVE_REPO/docs/federation/fixtures/loomweave-scan-results-wire.golden.json``,
  default ``/home/john/loomweave``). Fails closed on any byte divergence. When
  the sibling repo is absent it skips cleanly (e.g. CI) UNLESS
  ``FILIGREE_REQUIRE_LOOMWEAVE_REPO`` arms it — matching the capabilities oracle's
  arming env — in which case an absent source is a hard failure, so a release-gate
  run can be made to never silently skip the cross-repo authority recheck.

── SEAM NOTE: the synthetic-anchor row is rejected BY DESIGN on both sides ──

The golden's finding[2] (a subsystem ``synthetic_anchor=true`` finding) carries
``path="/repo/root"``. That value is a PRODUCER-ORACLE FIXTURE ARTIFACT: the
Loomweave producer oracle's ``fixed_opts()`` sets ``default_path=Some("/repo/root")``
purely to exercise the synthetic-anchor *emit* branch in isolation (its own
docstring disclaims that this shape is ingestible — Filigree computes dedup
server-side). In REAL ``loomweave analyze`` Phase 8 the emit caller sets
``default_path = None`` (verified in Loomweave source
``crates/loomweave-cli/src/analyze.rs`` ~line 4090, whose comment documents the
*exact* Filigree constraint: "Filigree's scan-results intake rejects every
synthetic stand-in: an absolute project root (absolute paths rejected), AND the
relative '.' …"), so ``wire_finding`` returns ``None`` for path-less rows and
they are SKIPPED — they never reach the wire. So a path-less synthetic-anchor
finding is NOT a shape Filigree ever receives in production; both peers already
agree it must not be sent. This is therefore NOT a seam defect: the rejection is
correct on both sides. We assert it explicitly (not slice it away silently) so a
future regression that started sending such a row — or a Filigree change that
silently accepted an absolute path — would red.

NOTE on the suppression/kind axis: unlike the wardline oracle, this oracle does
NOT assert server-side ``kind`` / ``suppression`` finding-list filters. Those
Filigree filters read ``metadata.wardline.kind`` / ``metadata.wardline.``-nested
suppression state (wardline's owned vocabulary); Loomweave nests ``kind`` at the
top of ``metadata`` (``metadata.kind``, not ``metadata.wardline.kind``), so those
indexed filters do not apply to Loomweave's wire — verified empirically
(``kind='defect'`` returns 0 for the loomweave population). We instead assert the
nested ``metadata.loomweave.*`` blob survives the round-trip key-by-key.

NOTE on "severities coerced": Filigree's ``_validate_scan_findings`` runs its
severity-normalization path over every finding, but for THIS wire it is an
identity no-op — Loomweave already emits canonical lowercase severities
(``medium`` / ``info`` / ``high``); the WARN→medium etc. mapping happened on the
PRODUCER. We assert the persisted severities equal the wire's lowercase values
(the coercion path is exercised and leaves them unchanged), not that the consumer
performs the vocabulary mapping.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from filigree.core import FiligreeDB

# The exact request validator the live ``POST /api/v1/scan-results`` and
# ``POST /api/weft/scan-results`` routes funnel through (see
# ``filigree.dashboard_routes.files``: both call ``_parse_scan_results_body``
# then hand the parsed kwargs to ``db.process_scan_results``). Driving these two
# is driving the real intake — the route adds only the HTTP envelope and the
# worker-thread hop, not any parsing/persistence logic of its own.
from filigree.dashboard_routes.files import _parse_scan_results_body

# The vendored consumer copy of Loomweave's authoritative scan-results wire.
GOLDEN_PATH = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "contracts"
    / "loomweave-scan-results-wire.golden.json"
)

# Git-blob sha1 of the vendored golden (``sha1(b"blob %d\0" % len + data)``).
# Recomputed from bytes by ``test_vendored_golden_byte_pin`` so any edit to the
# fixture reds in the default suite, on every CI run.
UPSTREAM_BLOB_SHA = "3f20f1ea95f5152e3dc9d848c721ed376670fae1"


def _blob_sha(data: bytes) -> str:
    """git's blob object id for ``data``: ``sha1(b"blob <len>\\0" + data)``.

    ``usedforsecurity=False`` is honest — this is content addressing (git's own
    object-id scheme), not a security primitive — and keeps ruff's S324 quiet
    without a per-line suppression.
    """
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def _load_golden() -> dict[str, Any]:
    golden: dict[str, Any] = json.loads(GOLDEN_PATH.read_text())
    return golden


def _is_synthetic_anchor(finding: dict[str, Any]) -> bool:
    """A finding whose ``metadata.loomweave.synthetic_anchor`` is truthy.

    These path-less anchors emit ONLY when the producer is handed a
    ``default_path`` fallback (the producer-oracle fixture does; production sets
    ``default_path=None`` and skips them). See the module SEAM NOTE.
    """
    md = finding.get("metadata")
    if not isinstance(md, dict):
        return False
    lw = md.get("loomweave")
    if not isinstance(lw, dict):
        return False
    return bool(lw.get("synthetic_anchor"))


def _production_representative_subset(golden: dict[str, Any]) -> dict[str, Any]:
    """The findings Filigree actually receives in production: synthetic-anchor
    rows dropped, mirroring production Loomweave's ``default_path=None`` skip.

    A principled, documented filter — NOT a slice to fake a green. The whole-body
    rejection is asserted separately (see ``test_real_intake_rejects_whole_golden``)
    so the dropped row is surfaced, not buried.
    """
    subset = copy.deepcopy(golden)
    subset["findings"] = [f for f in golden["findings"] if not _is_synthetic_anchor(f)]
    return subset


# ---------------------------------------------------------------------------
# Layer 1 — byte-pin (default suite)
# ---------------------------------------------------------------------------


def test_vendored_golden_byte_pin() -> None:
    """The vendored golden's bytes hash to the pinned git-blob sha.

    A single-byte edit to the fixture changes the sha and reds this test in the
    default suite — the cheapest possible drift tripwire, no sibling repo needed.
    """
    assert _blob_sha(GOLDEN_PATH.read_bytes()) == UPSTREAM_BLOB_SHA


def test_byte_pin_rejects_a_mutated_byte() -> None:
    """Tamper proof: flipping one byte of the vendored golden yields a different
    git-blob sha, demonstrating the Layer-1 pin is load-bearing (it would catch a
    silent single-byte edit of the fixture), not decorative."""
    tampered = bytearray(GOLDEN_PATH.read_bytes())
    tampered[0] ^= 0x01
    assert _blob_sha(bytes(tampered)) != UPSTREAM_BLOB_SHA


# ---------------------------------------------------------------------------
# Consumer intake oracle — the non-circular core (default suite)
# ---------------------------------------------------------------------------


def test_real_intake_accepts_production_representative_subset() -> None:
    """Filigree's REAL request validator accepts Loomweave's production wire.

    ``_parse_scan_results_body`` is the single shared validator every
    scan-results route runs before any persistence. Feeding it the
    production-representative subset (synthetic-anchor rows dropped) must yield
    the parsed ingest-kwargs dict, NOT a ``_ScanResultsBodyError``.

    This affirmatively pins the fingerprint-LESS / scheme-LESS / scanned_paths-LESS
    contract: the parser accepts a body with NO ``fingerprint_scheme`` (defaults
    to ``''``) and NO ``scanned_paths`` (defaults to ``[]``), and the per-finding
    rows carry no ``fingerprint``.
    """
    subset = _production_representative_subset(_load_golden())
    parsed = _parse_scan_results_body(subset)
    # A validation failure is a dataclass instance, not a plain kwargs dict.
    assert isinstance(parsed, dict), f"intake rejected the loomweave wire: {parsed!r}"
    assert parsed["scan_source"] == subset["scan_source"] == "loomweave"
    # Fingerprint-LESS / scheme-LESS / scanned_paths-LESS: the consumer defaults
    # these absent fields (Loomweave deliberately omits all three).
    assert parsed["fingerprint_scheme"] == "", "loomweave wire declares no fingerprint_scheme"
    assert parsed["scanned_paths"] == [], "loomweave wire carries no scanned_paths"
    assert "scanned_paths" not in subset, "the golden body must not carry wardline's scanned_paths"
    assert parsed["mark_unseen"] == subset["mark_unseen"]
    assert len(parsed["findings"]) == len(subset["findings"])
    # No per-finding fingerprint on the wire.
    for finding in parsed["findings"]:
        assert "fingerprint" not in finding, "loomweave findings carry no fingerprint"


def test_real_intake_rejects_whole_golden(tmp_path: Path) -> None:
    """The WHOLE golden is rejected at Filigree's intake boundary — BY DESIGN.

    The golden's synthetic-anchor finding[2] carries the absolute placeholder
    ``path="/repo/root"``; Filigree's ``_validate_scan_findings`` rejects
    absolute paths (``_is_project_relative_scan_path``) BEFORE any writes, so the
    whole batch 400s atomically (no finding persists). This is correct on BOTH
    sides of the seam — production Loomweave sets ``default_path=None`` and never
    emits such a row (see the module SEAM NOTE).

    We surface this explicitly so a regression that began sending an absolute-path
    finding, OR a Filigree change that silently accepted one, would red here.
    """
    golden = _load_golden()
    # Sanity: the golden genuinely contains a synthetic-anchor absolute-path row,
    # so this rejection assertion is meaningful (not vacuously true).
    synthetic = [f for f in golden["findings"] if _is_synthetic_anchor(f)]
    assert synthetic, "expected the golden to carry a synthetic-anchor row"
    assert any(Path(f["path"]).is_absolute() for f in synthetic), "expected an absolute synthetic-anchor path"

    parsed = _parse_scan_results_body(golden)
    assert isinstance(parsed, dict), f"unexpected body-validation failure: {parsed!r}"

    db = FiligreeDB(tmp_path / "filigree.db", prefix="test")
    db.initialize()
    try:
        with pytest.raises(ValueError, match="project-relative"):
            db.process_scan_results(**parsed)
        # Atomic: nothing persisted from the rejected batch.
        assert db.list_findings_global(suppression="all", limit=1000)["total"] == 0
    finally:
        db.close()


def test_real_intake_round_trips_production_subset(tmp_path: Path) -> None:
    """Drive Loomweave's production wire through Filigree's REAL ingest and read
    it back — the non-circular core.

    Every fingerprint-LESS finding must persist through the LEGACY positional
    dedup branch (``_upsert_finding`` ~db_files.py:1288, taken when
    ``fingerprint == ''``), keep its rule/message/severity, and carry its nested
    ``metadata.loomweave.*`` blob untouched key-by-key. We read back through the
    public ``list_findings_global`` query (the ``/api/weft/findings`` surface) and
    match on ``(rule_id, line_start)`` since there is NO fingerprint to key on —
    that absence is itself the legacy-path contract.
    """
    golden = _load_golden()
    subset = _production_representative_subset(golden)
    parsed = _parse_scan_results_body(subset)
    assert isinstance(parsed, dict)
    # ``process_scan_results`` mutates ``parsed["findings"]`` in place (path /
    # severity normalization), so assert the round-trip against a PRISTINE re-load
    # of the wire rather than the just-ingested objects — keeps the comparison
    # non-circular (the persisted row is checked against the untouched golden).
    wire_findings = _production_representative_subset(_load_golden())["findings"]

    db = FiligreeDB(tmp_path / "filigree.db", prefix="test")
    db.initialize()
    try:
        result = db.process_scan_results(**parsed)

        # Every wire finding was ingested through the legacy positional path.
        assert result["findings_created"] == len(wire_findings)

        listed = db.list_findings_global(suppression="all", limit=1000)
        assert listed["total"] == len(wire_findings)

        # Index the persisted population by (rule_id, line_start) — the legacy
        # positional identity (there is no fingerprint to key on).
        persisted_by_key: dict[tuple[str, Any], dict[str, Any]] = {}
        for row in listed["findings"]:
            # Fingerprint-LESS contract: every persisted loomweave finding took the
            # legacy positional branch, so its stored fingerprint is ''.
            assert row["fingerprint"] == "", (
                f"loomweave finding {row['rule_id']!r} unexpectedly carries a "
                f"fingerprint {row['fingerprint']!r} — it should round-trip "
                "through the legacy positional dedup path"
            )
            persisted_by_key[(row["rule_id"], row.get("line_start"))] = row

        for wire_finding in wire_findings:
            key = (wire_finding["rule_id"], wire_finding.get("line_start"))
            persisted = persisted_by_key.get(key)
            assert persisted is not None, f"finding not persisted: {key}"
            assert persisted["message"] == wire_finding["message"]
            # Severity "coercion" is an identity no-op for this wire — Loomweave
            # already emits canonical lowercase; the normalization path runs and
            # leaves it unchanged.
            assert persisted["severity"] == wire_finding["severity"]
            assert persisted["severity"] == wire_finding["severity"].lower()

            # The nested metadata.loomweave.* blob round-trips key-by-key. Subset
            # direction is wire ⊆ persisted (Filigree MAY add its own keys);
            # per-key equality, not set ``items() <=``, since values include
            # lists (``related_entities`` / ``supports``) which are unhashable.
            wire_md = wire_finding["metadata"]
            persisted_md = persisted.get("metadata")
            assert isinstance(persisted_md, dict), f"metadata not persisted for {key}"
            wire_lw = wire_md["loomweave"]
            persisted_lw = persisted_md.get("loomweave")
            assert isinstance(persisted_lw, dict), f"loomweave metadata not persisted for {key}"
            for axis, wire_value in wire_lw.items():
                assert persisted_lw.get(axis) == wire_value, (
                    f"metadata.loomweave[{axis!r}] did not round-trip for {key}: "
                    f"wire={wire_value!r} persisted={persisted_lw.get(axis)!r}"
                )
            # Top-level metadata.kind (Loomweave nests kind here, NOT under
            # metadata.wardline) also survives.
            assert persisted_md.get("kind") == wire_md.get("kind")

        # The wire's per-finding ``path`` was genuinely consumed at the boundary:
        # each distinct scanned path became a tracked file record.
        tracked_paths = {f.path for f in db.list_files(limit=1000)}
        assert {wf["path"] for wf in wire_findings} <= tracked_paths
    finally:
        db.close()


def test_high_and_critical_severities_round_trip(tmp_path: Path) -> None:
    """ERROR→``high`` and CRITICAL→``critical`` severities round-trip through the
    consumer's severity-normalization path unchanged.

    The golden's only ``high`` row is the synthetic-anchor (absolute-path, rejected
    by design), so the production-subset round-trip test exercises only ``medium`` /
    ``info``. This closes that coverage gap WITHOUT editing the golden (no byte-pin
    / cross-repo blast radius): it DERIVES an in-test fixture by deep-copying a real
    relative-path golden finding and forcing ``severity`` to ``high`` then
    ``critical`` on distinct paths/rules (so they don't collide with the originals),
    then asserts each persists with the SAME canonical-lowercase severity. Loomweave
    emits canonical lowercase, so Filigree's ``_validate_scan_findings`` normalization
    is an identity no-op here; this proves that holds for high/critical too — and
    would RED if Filigree mangled a high/critical severity on the legacy path.
    """
    golden = _load_golden()
    # A real, ingestible relative-path finding to clone (NOT the synthetic anchor).
    template = next(f for f in golden["findings"] if not _is_synthetic_anchor(f))
    assert not Path(template["path"]).is_absolute()

    derived: list[dict[str, Any]] = []
    for idx, severity in enumerate(("high", "critical")):
        row = copy.deepcopy(template)
        # Distinct positional identity so each is a separate INSERT on the legacy
        # path (file_id + rule_id + line_start key), not a dedup of the template.
        row["path"] = f"src/derived/sev_{severity}.py"
        row["rule_id"] = f"LMWV-DERIVED-SEV-{idx}"
        row["line_start"] = 100 + idx
        row["line_end"] = 100 + idx  # keep line_end >= line_start (template had 12..20)
        row["severity"] = severity
        derived.append(row)

    body = copy.deepcopy(golden)
    body["findings"] = derived

    parsed = _parse_scan_results_body(body)
    assert isinstance(parsed, dict), f"intake rejected the derived high/critical wire: {parsed!r}"

    db = FiligreeDB(tmp_path / "filigree.db", prefix="test")
    db.initialize()
    try:
        result = db.process_scan_results(**parsed)
        assert result["findings_created"] == len(derived)

        listed = db.list_findings_global(suppression="all", limit=1000)
        persisted_by_rule = {row["rule_id"]: row for row in listed["findings"]}
        for wire in derived:
            persisted = persisted_by_rule.get(wire["rule_id"])
            assert persisted is not None, f"derived finding {wire['rule_id']!r} not persisted"
            # The load-bearing assertion: high/critical survive the consumer's
            # normalization path unchanged (canonical lowercase identity).
            assert persisted["severity"] == wire["severity"]
            assert persisted["severity"] in ("high", "critical")
            # Still the fingerprint-less legacy positional path.
            assert persisted["fingerprint"] == ""
    finally:
        db.close()


def test_legacy_positional_path_dedups_on_re_ingest(tmp_path: Path) -> None:
    """Re-ingesting the SAME fingerprint-less wire is idempotent — the legacy
    positional dedup SELECT matches and updates rather than re-inserting.

    The first ingest only exercises the legacy branch's "not-found → INSERT" leg.
    This second ``process_scan_results`` on the identical body is what actually
    drives the positional dedup SELECT (``WHERE file_id=? AND scan_source=? AND
    rule_id=? AND coalesce(line_start,-1)=? AND fingerprint=''`` —
    ``_upsert_finding`` ~db_files.py:1288): the rows must be FOUND (no new
    fingerprint to distinguish them), so ``findings_created==0`` and the total
    stays put. This proves the dedup leg the test name claims, not just insertion.
    """
    subset = _production_representative_subset(_load_golden())
    expected = len(subset["findings"])

    db = FiligreeDB(tmp_path / "filigree.db", prefix="test")
    db.initialize()
    try:
        first = _parse_scan_results_body(_production_representative_subset(_load_golden()))
        assert isinstance(first, dict)
        r1 = db.process_scan_results(**first)
        assert r1["findings_created"] == expected

        # Identical re-ingest: the positional dedup SELECT matches every row.
        second = _parse_scan_results_body(_production_representative_subset(_load_golden()))
        assert isinstance(second, dict)
        r2 = db.process_scan_results(**second)
        assert r2["findings_created"] == 0, "fingerprint-less re-ingest must dedup, not duplicate"

        # Population unchanged — no duplicate rows accreted.
        assert db.list_findings_global(suppression="all", limit=1000)["total"] == expected
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Layer 2 — drift recheck against Loomweave's authority source (skip-clean)
# ---------------------------------------------------------------------------


def _loomweave_authority_source() -> Path | None:
    """Locate Loomweave's canonical scan-results wire golden, if the sibling repo
    is present. Honors a ``LOOMWEAVE_REPO`` override; defaults to
    ``/home/john/loomweave``.
    """
    repo = Path(os.environ.get("LOOMWEAVE_REPO", "/home/john/loomweave"))
    source = repo / "docs" / "federation" / "fixtures" / "loomweave-scan-results-wire.golden.json"
    return source if source.exists() else None


def test_vendored_golden_matches_loomweave_authority_source() -> None:
    """The vendored consumer copy must be BYTE-identical to Loomweave's authority
    source. Fails closed on any divergence; skips cleanly when the sibling repo is
    absent (e.g. CI), where Layer 1 + the consumer intake oracle still gate the
    PR. Loomweave is the producer/authority for this body; the consumer copy is a
    sync, not a second source of truth.

    Setting ``FILIGREE_REQUIRE_LOOMWEAVE_REPO`` (the strict arming env, off by
    default, NOT set in the ``loomweave-contract`` CI job) turns an absent source
    into a hard failure instead of a skip — matching the capabilities oracle's
    arming env, so a release-gate run can require the cross-repo recheck to
    actually execute rather than silently skip."""
    source = _loomweave_authority_source()
    if source is None:
        if os.environ.get("FILIGREE_REQUIRE_LOOMWEAVE_REPO"):
            pytest.fail(
                "FILIGREE_REQUIRE_LOOMWEAVE_REPO is set but Loomweave's authority golden was not found "
                "(set LOOMWEAVE_REPO to the sibling repo so the byte-drift check can arm)."
            )
        pytest.skip("Loomweave repo not found (set LOOMWEAVE_REPO to enable the byte-drift check)")
    assert GOLDEN_PATH.read_bytes() == source.read_bytes(), (
        "Vendored loomweave-scan-results-wire.golden.json has drifted from Loomweave's authority "
        "source; re-vendor it byte-identical (Loomweave is the producer)."
    )
