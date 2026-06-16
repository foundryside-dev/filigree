"""Wardline-supplied fingerprint as cross-run finding identity (Weft §3.B).

When a finding carries a non-empty ``fingerprint``, Filigree keys its
lifecycle/seen_count on ``(scan_source, fingerprint)`` instead of the
``(file_id, scan_source, rule_id, line_start)`` heuristic. The heuristic is
itself unstable (line attribution is clamped/cleared against file length on
ingest), so a stable per-finding fingerprint pins identity through line moves.
The column is generic — any scanner may supply it; absent → legacy behaviour.
"""

from __future__ import annotations

import pytest

from filigree.core import FiligreeDB


def _finding(path: str, rule_id: str, line_start: int, **extra: object) -> dict[str, object]:
    return {"path": path, "rule_id": rule_id, "message": "m", "severity": "high", "line_start": line_start, **extra}


class TestFingerprintDedup:
    def test_same_fingerprint_collapses_across_line_move(self, db: FiligreeDB) -> None:
        """A finding that moves lines but keeps its fingerprint is ONE finding.

        seen_count increments and the stored line attribution is refreshed to
        the latest scan's position.
        """
        db.process_scan_results(
            scan_source="wardline",
            findings=[_finding("src/a.py", "WLN-001", 10, line_end=12, fingerprint="fp-abc")],
        )
        db.process_scan_results(
            scan_source="wardline",
            findings=[_finding("src/a.py", "WLN-001", 25, line_end=27, fingerprint="fp-abc")],
        )

        result = db.list_findings_global(scan_source="wardline")
        assert result["total"] == 1
        finding = result["findings"][0]
        assert finding["seen_count"] == 2
        assert finding["line_start"] == 25  # refreshed, not stuck at 10
        assert finding["line_end"] == 27

    def test_distinct_fingerprints_same_site_coexist(self, db: FiligreeDB) -> None:
        """Two findings at the same file/rule/line with different fingerprints
        are distinct — e.g. two taint paths into one sink. The pre-v19
        non-partial unique index would have rejected the second."""
        db.process_scan_results(
            scan_source="wardline",
            findings=[
                _finding("src/a.py", "WLN-002", 5, fingerprint="fp-path-1"),
                _finding("src/a.py", "WLN-002", 5, fingerprint="fp-path-2"),
            ],
        )
        result = db.list_findings_global(scan_source="wardline")
        assert result["total"] == 2

    def test_no_fingerprint_uses_legacy_dedup(self, db: FiligreeDB) -> None:
        """Findings without a fingerprint keep the (file, source, rule, line)
        dedup behaviour unchanged."""
        for _ in range(2):
            db.process_scan_results(
                scan_source="ruff",
                findings=[_finding("src/a.py", "E501", 7)],
            )
        result = db.list_findings_global(scan_source="ruff")
        assert result["total"] == 1
        assert result["findings"][0]["seen_count"] == 2

    def test_fingerprintless_and_fingerprinted_do_not_collide(self, db: FiligreeDB) -> None:
        """A fingerprint-bearing finding and a fingerprint-less one at the same
        site are distinct rows (the partial indexes partition cleanly)."""
        db.process_scan_results(
            scan_source="wardline",
            findings=[
                _finding("src/a.py", "WLN-003", 9, fingerprint="fp-xyz"),
                _finding("src/a.py", "WLN-003", 9),
            ],
        )
        result = db.list_findings_global(scan_source="wardline")
        assert result["total"] == 2

    def test_fingerprint_surfaced_in_finding_dict(self, db: FiligreeDB) -> None:
        """The stored fingerprint round-trips on the read projection."""
        res = db.process_scan_results(
            scan_source="wardline",
            findings=[_finding("src/a.py", "WLN-004", 3, fingerprint="fp-read")],
        )
        finding_id = res["new_finding_ids"][0]
        finding = db.get_finding(finding_id)
        assert finding["fingerprint"] == "fp-read"

    def test_fingerprintless_finding_has_empty_fingerprint(self, db: FiligreeDB) -> None:
        """Legacy findings report an empty-string fingerprint, never null."""
        res = db.process_scan_results(
            scan_source="ruff",
            findings=[_finding("src/a.py", "E501", 1)],
        )
        finding = db.get_finding(res["new_finding_ids"][0])
        assert finding["fingerprint"] == ""

    def test_non_string_fingerprint_rejected(self, db: FiligreeDB) -> None:
        """A non-string fingerprint is a VALIDATION error, not a 500 / silent
        TEXT-affinity coercion that would break the next scan's dedup."""
        with pytest.raises(ValueError, match="fingerprint must be a string"):
            db.process_scan_results(
                scan_source="wardline",
                findings=[_finding("src/a.py", "WLN-005", 1, fingerprint=123)],
            )


class TestFingerprintSchemeHandshake:
    """Weft seam G4 — fingerprint-scheme echo handshake (PDR-0023).

    Wardline declares a ``fingerprint_scheme`` (e.g. ``wlfp2``) on every emit.
    The dedup join is keyed on the raw fingerprint VALUE, so a silent scheme bump
    (``wlfp2`` -> ``wlfp3``) re-mints every fingerprint: under ``mark_unseen`` the
    sweep would flip every prior-scheme finding to ``unseen_in_latest`` and the
    close-on-fixed cascade would silently close them as fixed. Filigree records
    the scheme per scan_source, detects the bump, REFUSES the sweep, and surfaces
    a structured ``scheme_mismatch`` weft-reason.
    """

    def test_scheme_recorded_on_first_ingest(self, db: FiligreeDB) -> None:
        """The first ingest declaring a scheme records it for the scan_source."""
        db.process_scan_results(
            scan_source="wardline",
            findings=[_finding("src/a.py", "WLN-001", 10, fingerprint="fp-1")],
            fingerprint_scheme="wlfp2",
        )
        assert db._get_scan_source_scheme("wardline") == "wlfp2"

    def test_matching_scheme_proceeds_normally(self, db: FiligreeDB) -> None:
        """Same declared scheme on re-ingest: no carrier, sweep behaves as today
        (the absent prior fingerprint flips to unseen_in_latest)."""
        db.process_scan_results(
            scan_source="wardline",
            findings=[_finding("src/a.py", "WLN-001", 10, fingerprint="fp-old")],
            fingerprint_scheme="wlfp2",
        )
        res = db.process_scan_results(
            scan_source="wardline",
            findings=[_finding("src/a.py", "WLN-001", 10, fingerprint="fp-new")],
            fingerprint_scheme="wlfp2",
            mark_unseen=True,
        )
        assert res["weft_reasons"] == []
        old = db.find_finding_by_fingerprint("wardline", "fp-old")
        assert old is not None
        # Same scheme: the absent prior fingerprint is genuinely swept.
        assert old["status"] == "unseen_in_latest"

    def test_blank_declared_scheme_is_legacy_passthrough(self, db: FiligreeDB) -> None:
        """A caller that declares no scheme (legacy) never trips the handshake
        and never records a baseline."""
        db.process_scan_results(
            scan_source="ruff",
            findings=[_finding("src/a.py", "E501", 1)],
        )
        res = db.process_scan_results(
            scan_source="ruff",
            findings=[_finding("src/a.py", "E501", 1)],
            mark_unseen=True,
        )
        assert res["weft_reasons"] == []
        assert db._get_scan_source_scheme("ruff") == ""

    def test_golden_vector_scheme_bump_does_not_cascade_close(self, db: FiligreeDB) -> None:
        """GOLDEN VECTOR (Weft seam G4 contract §3).

        Ingest a set under ``wlfp2``, then ingest the SAME findings RE-MINTED
        under ``wlfp3`` (new fingerprint values, as a real scheme bump produces)
        with ``mark_unseen=True``. Assert:
          1. the wlfp2 findings are NOT marked unseen/closed (the sweep is
             refused), AND
          2. a ``scheme_mismatch`` weft-reason carrier is surfaced with a
             mandatory cause + fix.
        """
        wlfp2_findings = [
            _finding("src/a.py", "WLN-001", 10, fingerprint="wlfp2:aaa"),
            _finding("src/b.py", "WLN-002", 20, fingerprint="wlfp2:bbb"),
        ]
        db.process_scan_results(
            scan_source="wardline",
            findings=wlfp2_findings,
            fingerprint_scheme="wlfp2",
            mark_unseen=True,
        )
        # Sanity: both wlfp2 findings are open.
        for fp in ("wlfp2:aaa", "wlfp2:bbb"):
            f = db.find_finding_by_fingerprint("wardline", fp)
            assert f is not None
            assert f["status"] == "open"

        # The bump: SAME logical findings, re-minted fingerprints, new scheme.
        wlfp3_findings = [
            _finding("src/a.py", "WLN-001", 10, fingerprint="wlfp3:aaa"),
            _finding("src/b.py", "WLN-002", 20, fingerprint="wlfp3:bbb"),
        ]
        res = db.process_scan_results(
            scan_source="wardline",
            findings=wlfp3_findings,
            fingerprint_scheme="wlfp3",
            mark_unseen=True,
        )

        # (1) INVARIANT: the wlfp2 findings are untouched — NOT swept, NOT closed.
        for fp in ("wlfp2:aaa", "wlfp2:bbb"):
            f = db.find_finding_by_fingerprint("wardline", fp)
            assert f is not None, f"wlfp2 finding {fp} vanished"
            assert f["status"] == "open", f"wlfp2 finding {fp} was cascade-closed under a scheme bump"

        # The new-scheme findings are still ingested (no sweep != no ingest).
        for fp in ("wlfp3:aaa", "wlfp3:bbb"):
            assert db.find_finding_by_fingerprint("wardline", fp) is not None

        # (2) a scheme_mismatch carrier is surfaced with mandatory cause + fix.
        carriers = [r for r in res["weft_reasons"] if r["reason_class"] == "scheme_mismatch"]
        assert len(carriers) == 1
        carrier = carriers[0]
        assert "wlfp3" in carrier["cause"]
        assert "wlfp2" in carrier["cause"]
        assert "wardline" in carrier["cause"]
        assert carrier["fix"]  # mandatory recruiting action, non-empty

        # The stored baseline is NOT overwritten by the rejected bump.
        assert db._get_scan_source_scheme("wardline") == "wlfp2"

    def test_scheme_mismatch_surfaced_on_weft_wire(self, db: FiligreeDB) -> None:
        """The scheme_mismatch carrier rides the weft envelope (NotRequired
        weft_reasons), and a clean ingest omits the key entirely."""
        from filigree.generations.weft.adapters import scan_ingest_result_to_weft

        clean = db.process_scan_results(
            scan_source="wardline",
            findings=[_finding("src/a.py", "WLN-001", 10, fingerprint="wlfp2:x")],
            fingerprint_scheme="wlfp2",
        )
        clean_wire = scan_ingest_result_to_weft(clean)
        assert "weft_reasons" not in clean_wire  # clean path: omitted

        bumped = db.process_scan_results(
            scan_source="wardline",
            findings=[_finding("src/a.py", "WLN-001", 10, fingerprint="wlfp3:x")],
            fingerprint_scheme="wlfp3",
            mark_unseen=True,
        )
        bumped_wire = scan_ingest_result_to_weft(bumped)
        assert bumped_wire["weft_reasons"][0]["reason_class"] == "scheme_mismatch"
