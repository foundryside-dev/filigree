"""Consumer-side finding-identity wire conformance oracle.

Wardline AUTHORS finding identity — the ``{fingerprint, qualname, spans}`` it
derives from fixed inputs (rule_id / path / qualname / taint_path / location) via
``wardline.core.finding.{compute_finding_fingerprint, format_fingerprint,
_to_wire_qualname}`` and ``Finding.to_jsonl``. Wardline froze that identity to a
committed golden + a PRODUCER-SOURCE recheck (its
``test_filigree_finding_identity_wire_golden.py`` re-derives each vector from the
LIVE producers and asserts it ties to the golden, so the byte-pin is not
circular on the authority side).

Filigree is the CONSUMER: it keys issues/findings on ``(scan_source,
fingerprint)`` and stores the identity fields (the span ``line_start``/
``line_end`` columns and the nested ``metadata.wardline.qualname`` axis). This
module is the missing *consumer* half of "both peers load the shared corpus": it
proves Filigree genuinely ingests Wardline's authoritative identity vectors
through its REAL fingerprint-keyed ingest + lookup path, not a restatement of the
golden against itself.

Three layers, mirroring the SEI oracle (``test_sei_conformance_oracle.py``), the
suppression-filter oracle, and the scan-results wire oracle:

- **Layer 1 — byte-pin (default suite).** ``UPSTREAM_BLOB_SHA`` is the git-blob
  sha of the vendored golden; an unmarked test recomputes it from bytes. Any
  edit to the vendored fixture reds this immediately, in every CI run. (It is the
  SAME sha Wardline's producer test pins, because the vendored copy is
  byte-identical.)
- **Consumer ingest oracle (the non-circular core, default suite).** Feeds each
  golden identity vector through Filigree's REAL fingerprint-keyed ingest
  (``FiligreeDB.process_scan_results``) and reads it back through the REAL
  identity lookup (``find_finding_by_fingerprint`` — the exact
  ``(scan_source, fingerprint)`` join ``/api/weft/findings/promote`` keys on) and
  the REAL nested-metadata query grammar (``list_findings_global(qualname=...)``,
  which the ``/api/weft/findings`` route exposes). It asserts each finding
  persists and round-trips its fingerprint / span / qualname exactly, AND — the
  JOIN-KEY SOUNDNESS the golden encodes — that the ``collision_pair_*`` vectors,
  which share ``(rule_id, path, line_start)`` and differ ONLY in the
  source-derived ``taint_path`` discriminator (columns Filigree never sees),
  remain TWO DISTINCT findings keyed by their distinct fingerprints. A consumer
  that fell back to the legacy ``(file, rule, line_start)`` dedup heuristic would
  collapse them to one and red here. This reads the real ingest, not the golden
  against itself.
- **Layer 2 — drift recheck (release-gate, skip-clean).** Byte-compares the
  vendored copy against Wardline's authority source
  (``$WARDLINE_REPO/tests/conformance/fixtures/wardline-finding-identity-wire.golden.json``,
  default ``/home/john/wardline``). Skips cleanly when the sibling repo is absent
  (e.g. CI); fails closed on any byte divergence.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from filigree.core import FiligreeDB

# The vendored consumer copy of Wardline's authoritative finding-identity wire.
GOLDEN_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "contracts" / "wardline-finding-identity-wire.golden.json"

# Git-blob sha1 of the vendored golden (``sha1(b"blob %d\0" % len + data)``).
# Recomputed from bytes by ``test_vendored_golden_byte_pin`` so any edit to the
# fixture reds in the default suite, on every CI run. Identical to the sha
# Wardline's producer test (``test_filigree_finding_identity_wire_golden.py``)
# pins, because the vendored copy is byte-identical to the authority source.
UPSTREAM_BLOB_SHA = "4eec05f0c53b301cb433331092731c567a7754db"


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


def _finding_from_vector(vec: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal but WIRE-SHAPED finding carrying a golden vector's identity.

    Filigree's ingest needs a full finding body (``path``/``rule_id``/``message``
    are required), so this constructs the smallest valid finding that carries the
    vector's identity fields exactly as the real scan-results wire delivers them:

    * ``fingerprint`` — the bare 64-hex digest (``wire_fingerprint``); this is the
      value Filigree's ``(scan_source, fingerprint)`` join keys on. ``to_jsonl``
      emits the bare form, so that is what the consumer receives on the wire.
    * ``line_start``/``line_end`` — the span the wire carries as flat columns
      (Filigree persists these on ``scan_findings``).
    * ``metadata.wardline.qualname`` — the nested qualname axis Filigree consumes
      as a server-side finding-list filter. We store the ``to_jsonl`` wire form
      (``wire_jsonl_qualname``) and query the SAME value; the choice between
      ``wire_qualname`` and ``wire_jsonl_qualname`` is not load-bearing for the
      round-trip because Filigree stores and matches the value verbatim (pure
      equality on ``$.wardline.qualname``, no normalization).

    Severity is NOT part of identity (the golden omits it); we use a Filigree-valid
    value (``high``) to avoid the unknown-severity warning path. We deliberately do
    NOT fabricate ``col_start``/``col_end`` into the metadata: the real wire does
    not carry them as a Filigree field, and stuffing them in just to round-trip
    them would test Filigree storing data the producer never emits. The column-level
    identity those columns encode is instead proven transitively via the
    collision-pair join-key soundness check below.
    """
    spans = vec["spans"]
    wire_qualname = vec["wire_jsonl_qualname"]
    metadata: dict[str, Any] = {"wardline": {}}
    if wire_qualname is not None:
        metadata["wardline"]["qualname"] = wire_qualname
    return {
        "path": spans["path"],
        "rule_id": vec["inputs"]["rule_id"],
        "message": "identity vector",
        "severity": "high",
        "line_start": spans["line_start"],
        "line_end": spans["line_end"],
        "fingerprint": vec["wire_fingerprint"],
        "metadata": metadata,
    }


# ---------------------------------------------------------------------------
# Layer 1 — byte-pin (default suite)
# ---------------------------------------------------------------------------


def test_vendored_golden_byte_pin() -> None:
    """The vendored golden's bytes hash to the pinned git-blob sha.

    A single-byte edit to the fixture changes the sha and reds this test in the
    default suite — the cheapest possible drift tripwire, no sibling repo needed.
    """
    assert _blob_sha(GOLDEN_PATH.read_bytes()) == UPSTREAM_BLOB_SHA


# ---------------------------------------------------------------------------
# Consumer ingest oracle — the non-circular core (default suite)
# ---------------------------------------------------------------------------


def test_real_ingest_persists_and_round_trips_each_identity(tmp_path: Path) -> None:
    """Drive every golden identity vector through Filigree's REAL fingerprint path.

    Non-circular core: ingest each vector via the exact primitive the
    ``/api/weft/scan-results`` route calls (``process_scan_results``), then resolve
    it by its golden ``(scan_source, fingerprint)`` identity via the exact lookup
    ``/api/weft/findings/promote`` keys on (``find_finding_by_fingerprint``), and
    assert the persisted finding carries the SAME fingerprint and span the golden
    froze. This reads the real ingest + lookup, NOT the golden against itself: if
    Filigree mangled the fingerprint on the join, or dropped/garbled the span, this
    reds.

    ``project_root`` is intentionally unset (mirroring the sibling oracles) so the
    line-attribution guard does not reject these synthetic spans against
    nonexistent files.
    """
    golden = _load_golden()
    vectors = golden["vectors"]
    assert vectors, "identity golden carries no vectors — a vacuous corpus must not pass"
    scan_source = "wardline"

    findings = [_finding_from_vector(vec) for vec in vectors.values()]

    db = FiligreeDB(tmp_path / "filigree.db", prefix="test")
    db.initialize()
    try:
        result = db.process_scan_results(
            scan_source=scan_source,
            findings=findings,
            fingerprint_scheme=golden["fingerprint_scheme"],
        )
        # Every vector ingested as its OWN finding (none collapsed at the boundary).
        assert result["findings_created"] == len(vectors)

        for name, vec in vectors.items():
            fingerprint = vec["wire_fingerprint"]
            persisted = db.find_finding_by_fingerprint(scan_source, fingerprint)
            assert persisted is not None, f"{name}: fingerprint not persisted: {fingerprint}"
            # The join key round-trips byte-for-byte (the bare 64-hex digest).
            assert persisted["fingerprint"] == fingerprint, f"{name}: fingerprint drift on round-trip"
            # The span columns Filigree persists round-trip exactly.
            spans = vec["spans"]
            assert persisted["line_start"] == spans["line_start"], f"{name}: line_start drift"
            assert persisted["line_end"] == spans["line_end"], f"{name}: line_end drift"
            assert persisted["rule_id"] == vec["inputs"]["rule_id"], f"{name}: rule_id drift"
    finally:
        db.close()


def test_real_ingest_records_declared_fingerprint_scheme(tmp_path: Path) -> None:
    """The golden's ``fingerprint_scheme`` (``wlfp2``) is recorded by the REAL
    scheme-echo handshake on first ingest.

    Filigree records the scanner-declared scheme per ``scan_source`` so a later
    silent scheme bump is caught (PDR-0023 / Weft seam G4). Feeding the golden's
    declared scheme through the real ingest must establish it as the baseline —
    proving the consumer consumes the identity SCHEME, not just the digest. Read
    back through the real accessor (``_get_scan_source_scheme``), not a restatement.
    """
    golden = _load_golden()
    scan_source = "wardline"
    findings = [_finding_from_vector(vec) for vec in golden["vectors"].values()]

    db = FiligreeDB(tmp_path / "filigree.db", prefix="test")
    db.initialize()
    try:
        db.process_scan_results(
            scan_source=scan_source,
            findings=findings,
            fingerprint_scheme=golden["fingerprint_scheme"],
        )
        assert db._get_scan_source_scheme(scan_source) == golden["fingerprint_scheme"]
    finally:
        db.close()


def test_real_query_resolves_finding_by_nested_qualname_axis(tmp_path: Path) -> None:
    """The nested ``metadata.wardline.qualname`` axis is queryable through the REAL
    server-side filter grammar.

    Wardline OWNS the qualname; Filigree CONSUMES it as a server-side finding-list
    filter axis (the ``qualname`` filter on ``list_findings_global``, which the
    ``/api/weft/findings`` route exposes via ``_wardline_field_eq_sql`` →
    ``json_extract(metadata, '$.wardline.qualname')``). After ingesting the golden,
    each vector's qualname must resolve its finding through that real query — proving
    the consumer didn't merely store the blob but wired the qualname into its real
    query grammar. The expected value is the wire qualname the golden froze, so the
    assertion stays tied to Wardline's authored identity.
    """
    golden = _load_golden()
    vectors = golden["vectors"]
    scan_source = "wardline"
    findings = [_finding_from_vector(vec) for vec in vectors.values()]

    db = FiligreeDB(tmp_path / "filigree.db", prefix="test")
    db.initialize()
    try:
        db.process_scan_results(
            scan_source=scan_source,
            findings=findings,
            fingerprint_scheme=golden["fingerprint_scheme"],
        )
        for name, vec in vectors.items():
            wire_qualname = vec["wire_jsonl_qualname"]
            if wire_qualname is None:
                continue
            got = db.list_findings_global(qualname=wire_qualname, suppression="all", limit=1000)
            # At least the vector with this exact qualname must surface; the
            # query keys on the SAME nested value the golden authored, so a
            # consumer that failed to index the nested qualname axis reds.
            fingerprints = {f["fingerprint"] for f in got["findings"]}
            assert vec["wire_fingerprint"] in fingerprints, (
                f"{name}: qualname={wire_qualname!r} did not resolve its finding via the nested-metadata query"
            )
    finally:
        db.close()


def test_collision_pair_remains_distinct_findings_on_the_join_key(tmp_path: Path) -> None:
    """JOIN-KEY SOUNDNESS — the property the cross-tool join rests on.

    ``collision_pair_a`` and ``collision_pair_b`` share ``(rule_id, path,
    line_start, line_end)`` and differ ONLY in the source-derived ``taint_path``
    discriminator — which Wardline folded into the fingerprint via the differing
    columns (8:20 vs 30:42). FILIGREE NEVER SEES THE COLUMNS: on every flat field
    it stores, the two findings are identical. The ONLY thing keeping them as two
    distinct rows is their distinct fingerprint. So this drives the real ingest and
    asserts:

    * both ingest as SEPARATE findings (``findings_created == 2``) — a consumer that
      fell back to the legacy ``(file, scan_source, rule_id, line_start)`` dedup
      heuristic would collapse them to ONE and silently drop a real defect on the
      join;
    * each resolves by its OWN fingerprint to a DISTINCT finding id;
    * the project-wide query for the shared rule returns BOTH.

    This transitively proves the column-level identity Wardline encodes survives
    across the seam — via the fingerprint join key — without Filigree needing a
    first-class column field at all.
    """
    golden = _load_golden()
    a = golden["vectors"]["collision_pair_a"]
    b = golden["vectors"]["collision_pair_b"]

    # Non-vacuity guard (mirrors the producer test): the pair MUST share the flat
    # fields Filigree stores and differ ONLY in the fingerprint, or this test would
    # pass on degenerate data.
    a_in, b_in = a["inputs"], b["inputs"]
    assert (a_in["rule_id"], a_in["path"]) == (b_in["rule_id"], b_in["path"]), (
        "collision-pair vectors must share (rule_id, path) so the join-key test is non-vacuous"
    )
    assert a["spans"]["line_start"] == b["spans"]["line_start"], (
        "collision-pair vectors must share line_start (the legacy dedup discriminator) so the test is non-vacuous"
    )
    assert a["wire_fingerprint"] != b["wire_fingerprint"], (
        "collision-pair vectors must carry DISTINCT fingerprints (the only field that separates them)"
    )

    scan_source = "wardline"
    findings = [_finding_from_vector(a), _finding_from_vector(b)]

    db = FiligreeDB(tmp_path / "filigree.db", prefix="test")
    db.initialize()
    try:
        result = db.process_scan_results(
            scan_source=scan_source,
            findings=findings,
            fingerprint_scheme=golden["fingerprint_scheme"],
        )
        # Two findings in → two findings persisted (not collapsed by legacy dedup).
        assert result["findings_created"] == 2, (
            "collision-pair collapsed to one finding — Filigree fell back to the legacy "
            "(file, rule, line_start) heuristic instead of keying on the distinct fingerprint; "
            "the cross-tool join key is unsound"
        )

        found_a = db.find_finding_by_fingerprint(scan_source, a["wire_fingerprint"])
        found_b = db.find_finding_by_fingerprint(scan_source, b["wire_fingerprint"])
        assert found_a is not None, "fingerprint a must resolve to a persisted finding"
        assert found_b is not None, "fingerprint b must resolve to a persisted finding"
        assert found_a["id"] != found_b["id"], (
            "distinct fingerprints resolved to the SAME finding id — the (scan_source, fingerprint) join collapsed them"
        )

        listed = db.list_findings_global(rule_id=a_in["rule_id"], suppression="all", limit=1000)
        assert listed["total"] == 2, (
            f"expected both collision-pair findings under rule {a_in['rule_id']!r}, got {listed['total']}"
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Layer 2 — drift recheck against Wardline's authority source (skip-clean)
# ---------------------------------------------------------------------------


def _wardline_authority_source() -> Path | None:
    """Locate Wardline's canonical finding-identity wire golden, if the sibling
    repo is present. Honors a ``WARDLINE_REPO`` override; defaults to
    ``/home/john/wardline``.
    """
    repo = Path(os.environ.get("WARDLINE_REPO", "/home/john/wardline"))
    source = repo / "tests" / "conformance" / "fixtures" / "wardline-finding-identity-wire.golden.json"
    return source if source.exists() else None


def test_vendored_golden_matches_wardline_authority_source() -> None:
    """The vendored consumer copy must be BYTE-identical to Wardline's authority
    source. Fails closed on any divergence; skips cleanly when the sibling repo is
    absent (e.g. CI), where Layer 1 + the consumer ingest oracle still gate the PR."""
    source = _wardline_authority_source()
    if source is None:
        pytest.skip("Wardline repo not found (set WARDLINE_REPO to enable the byte-drift check)")
    assert GOLDEN_PATH.read_bytes() == source.read_bytes(), (
        "Vendored wardline-finding-identity-wire.golden.json has drifted from Wardline's authority source; re-vendor it byte-identical."
    )
