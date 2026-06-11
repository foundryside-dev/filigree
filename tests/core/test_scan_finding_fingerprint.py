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
