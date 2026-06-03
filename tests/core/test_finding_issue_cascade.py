"""Finding→issue wiring for Wardline A2 (promote-by-fingerprint + status cascade).

Two behaviours land together:

* ``find_finding_by_fingerprint`` + a ``created`` flag on
  ``promote_finding_to_issue`` back the HTTP promote-by-fingerprint route.
* A finding-status cascade keeps the *linked issue* honest as code changes:
  close-on-fixed (via the clean-stale path) and reopen-on-regress (via
  re-ingest), with the reopen direction gated so a human's terminal decision
  is never silently overturned.

See ``docs`` brief (2026-06-02) and ADR-017 for the freshness model.
"""

from __future__ import annotations

import logging

import pytest

from filigree.core import FiligreeDB


def _wln(path: str, fingerprint: str, **extra: object) -> dict[str, object]:
    return {
        "path": path,
        "rule_id": "WLN-001",
        "message": "tainted sink",
        "severity": "high",
        "fingerprint": fingerprint,
        **extra,
    }


def _ingest(db: FiligreeDB, fingerprint: str = "fp-abc") -> str:
    """Ingest one fingerprinted wardline finding and return its finding id."""
    db.process_scan_results(scan_source="wardline", findings=[_wln("src/a.py", fingerprint)])
    finding = db.find_finding_by_fingerprint("wardline", fingerprint)
    assert finding is not None
    return finding["id"]


def _is_done(db: FiligreeDB, issue_id: str) -> bool:
    issue = db.get_issue(issue_id)
    return db._resolve_status_category(issue.type, issue.status) == "done"


class TestFindFindingByFingerprint:
    def test_resolves_ingested_fingerprint(self, db: FiligreeDB) -> None:
        finding_id = _ingest(db, "fp-resolve")
        resolved = db.find_finding_by_fingerprint("wardline", "fp-resolve")
        assert resolved is not None
        assert resolved["id"] == finding_id
        assert resolved["fingerprint"] == "fp-resolve"

    def test_unknown_fingerprint_returns_none(self, db: FiligreeDB) -> None:
        _ingest(db, "fp-known")
        assert db.find_finding_by_fingerprint("wardline", "fp-nope") is None

    def test_scan_source_scopes_lookup(self, db: FiligreeDB) -> None:
        _ingest(db, "fp-scoped")
        # Same fingerprint, different scan_source → no match.
        assert db.find_finding_by_fingerprint("other", "fp-scoped") is None

    def test_blank_fingerprint_returns_none(self, db: FiligreeDB) -> None:
        _ingest(db, "fp-x")
        assert db.find_finding_by_fingerprint("wardline", "") is None


class TestPromoteCreatedFlag:
    def test_created_true_on_first_promote(self, db: FiligreeDB) -> None:
        finding_id = _ingest(db)
        result = db.promote_finding_to_issue(finding_id, actor="t")
        assert result["created"] is True

    def test_created_false_on_second_promote(self, db: FiligreeDB) -> None:
        finding_id = _ingest(db)
        first = db.promote_finding_to_issue(finding_id, actor="t")
        second = db.promote_finding_to_issue(finding_id, actor="t")
        assert second["created"] is False
        assert second["issue"].id == first["issue"].id


class TestCloseOnFixed:
    def test_clean_stale_closes_linked_issue(self, db: FiligreeDB) -> None:
        finding_id = _ingest(db)
        issue = db.promote_finding_to_issue(finding_id, actor="t")["issue"]
        # Age the finding into the stale-unseen window.
        db.conn.execute(
            "UPDATE scan_findings SET status = 'unseen_in_latest', last_seen_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (finding_id,),
        )
        db.conn.commit()

        result = db.clean_stale_findings(days=30)

        assert result["findings_fixed"] == 1
        assert issue.id in result["closed_issue_ids"]
        assert _is_done(db, issue.id)

    def test_already_done_issue_not_recosed_no_error(self, db: FiligreeDB) -> None:
        finding_id = _ingest(db)
        issue = db.promote_finding_to_issue(finding_id, actor="t")["issue"]
        db.close_issue(issue.id, actor="human", force=True)  # already closed before the sweep
        db.conn.execute(
            "UPDATE scan_findings SET status = 'unseen_in_latest', last_seen_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (finding_id,),
        )
        db.conn.commit()

        result = db.clean_stale_findings(days=30)
        assert result["findings_fixed"] == 1
        # Not re-closed by the cascade (it was already done before the sweep).
        assert issue.id not in result["closed_issue_ids"]

    def test_reingest_between_sweep_and_cascade_does_not_close_issue(
        self,
        db: FiligreeDB,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If a finding reappears after the stale sweep commits but before the
        best-effort issue cascade runs, the cascade must not close the linked
        issue based on the stale fixed snapshot."""
        finding_id = _ingest(db, "fp-clean-reingest-race")
        issue = db.promote_finding_to_issue(finding_id, actor="t")["issue"]
        db.conn.execute(
            "UPDATE scan_findings SET status = 'unseen_in_latest', last_seen_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (finding_id,),
        )
        db.conn.commit()
        original_sweep = db._sweep_stale_findings_to_fixed

        def sweep_then_reingest(*, days: int, scan_source: str | None, actor: str) -> list[tuple[str, str | None]]:
            fixed = original_sweep(days=days, scan_source=scan_source, actor=actor)
            db.process_scan_results(scan_source="wardline", findings=[_wln("src/a.py", "fp-clean-reingest-race")])
            return fixed

        monkeypatch.setattr(db, "_sweep_stale_findings_to_fixed", sweep_then_reingest)

        result = db.clean_stale_findings(days=30)

        assert result["findings_fixed"] == 1
        assert result["closed_issue_ids"] == []
        assert db.get_finding(finding_id)["status"] == "open"
        assert not _is_done(db, issue.id)

    def test_unlinked_finding_no_cascade(self, db: FiligreeDB) -> None:
        finding_id = _ingest(db)  # never promoted → no issue link
        db.conn.execute(
            "UPDATE scan_findings SET status = 'unseen_in_latest', last_seen_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (finding_id,),
        )
        db.conn.commit()
        result = db.clean_stale_findings(days=30)
        assert result["findings_fixed"] == 1
        assert result["closed_issue_ids"] == []
        assert result["warnings"] == []  # happy path: no cascade advisories

    def test_cascade_close_failure_surfaced_in_warnings(self, db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        """A best-effort cascade close that fails is reported in the returned
        ``warnings`` (I3) — not just logged — so an HTTP/CLI caller learns the
        close partially failed. The sweep itself still succeeds.
        """
        finding_id = _ingest(db, "fp-failclose")
        issue = db.promote_finding_to_issue(finding_id, actor="t")["issue"]
        db.conn.execute(
            "UPDATE scan_findings SET status = 'unseen_in_latest', last_seen_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (finding_id,),
        )
        db.conn.commit()

        def boom(*args: object, **kwargs: object) -> None:
            raise ValueError("workflow forbids close")

        monkeypatch.setattr(db, "close_issue", boom)
        result = db.clean_stale_findings(days=30)

        assert result["findings_fixed"] == 1  # the sweep still happened
        assert issue.id not in result["closed_issue_ids"]  # cascade close failed
        assert any(f"cascade close of issue {issue.id} failed" in w for w in result["warnings"])


class TestReopenOnRegress:
    def _auto_close(self, db: FiligreeDB, fingerprint: str = "fp-abc") -> tuple[str, str]:
        """Ingest, promote, and cascade-close → (finding_id, issue_id)."""
        finding_id = _ingest(db, fingerprint)
        issue = db.promote_finding_to_issue(finding_id, actor="t")["issue"]
        db.conn.execute(
            "UPDATE scan_findings SET status = 'unseen_in_latest', last_seen_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (finding_id,),
        )
        db.conn.commit()
        db.clean_stale_findings(days=30)
        assert _is_done(db, issue.id)
        return finding_id, issue.id

    def test_reingest_reopens_auto_closed_issue(self, db: FiligreeDB) -> None:
        _, issue_id = self._auto_close(db)
        db.process_scan_results(scan_source="wardline", findings=[_wln("src/a.py", "fp-abc")])
        assert not _is_done(db, issue_id)

    def test_human_closed_issue_not_reopened(self, db: FiligreeDB) -> None:
        finding_id = _ingest(db, "fp-human")
        issue = db.promote_finding_to_issue(finding_id, actor="t")["issue"]
        db.close_issue(issue.id, actor="human", force=True)  # human decision, no marker
        db.conn.execute("UPDATE scan_findings SET status = 'fixed' WHERE id = ?", (finding_id,))
        db.conn.commit()

        db.process_scan_results(scan_source="wardline", findings=[_wln("src/a.py", "fp-human")])
        assert db.get_finding(finding_id)["status"] == "open"  # finding regressed
        assert _is_done(db, issue.id)  # issue stays closed

    def test_auto_close_reopen_human_close_regress_stays_closed(self, db: FiligreeDB) -> None:
        """The marker-clear is load-bearing: after we reopen, a human's later
        close must survive the next regress."""
        finding_id, issue_id = self._auto_close(db, "fp-cycle")
        # Regress #1 → we reopen (marker present), then clear the marker.
        db.process_scan_results(scan_source="wardline", findings=[_wln("src/a.py", "fp-cycle")])
        assert not _is_done(db, issue_id)
        # A human now closes it as won't-fix.
        db.close_issue(issue_id, actor="human", force=True)
        # Drive the finding fixed, then regress #2.
        db.conn.execute("UPDATE scan_findings SET status = 'fixed' WHERE id = ?", (finding_id,))
        db.conn.commit()
        db.process_scan_results(scan_source="wardline", findings=[_wln("src/a.py", "fp-cycle")])
        assert _is_done(db, issue_id)  # human's decision preserved

    def test_human_reopen_then_reclose_not_auto_reopened(self, db: FiligreeDB) -> None:
        """Human disagrees with an auto-close: reopens, then recloses won't-fix.
        A later regress must NOT auto-reopen over the human's terminal decision —
        the reopen gate is derived from event history, not a sticky field that a
        human reopen leaves stale.
        """
        _finding_id, issue_id = self._auto_close(db, "fp-hr")
        db.reopen_issue(issue_id, actor="human")  # human disagrees with the auto-close
        assert not _is_done(db, issue_id)
        db.close_issue(issue_id, actor="human", force=True)  # human recloses won't-fix
        assert _is_done(db, issue_id)

        db.process_scan_results(scan_source="wardline", findings=[_wln("src/a.py", "fp-hr")])
        assert _is_done(db, issue_id)  # human's reclose preserved

    def test_open_issue_regress_is_noop(self, db: FiligreeDB) -> None:
        """A finding linked to a still-open issue regressing needs no reopen."""
        finding_id = _ingest(db, "fp-open")
        issue = db.promote_finding_to_issue(finding_id, actor="t")["issue"]
        db.conn.execute("UPDATE scan_findings SET status = 'unseen_in_latest' WHERE id = ?", (finding_id,))
        db.conn.commit()
        db.process_scan_results(scan_source="wardline", findings=[_wln("src/a.py", "fp-open")])
        assert not _is_done(db, issue.id)

    def test_reopen_cascade_failure_is_logged_and_on_the_wire(
        self, db: FiligreeDB, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed best-effort reopen is surfaced via ``stats["warnings"]`` (the
        wire) AND logged per-failure (I2). The prior comment claimed the inverse
        (not on the wire, logged) — both halves were false; this pins reality so
        a maintainer cannot regress to the inverted behaviour.
        """
        _finding_id, issue_id = self._auto_close(db, "fp-failreopen")

        def boom(*args: object, **kwargs: object) -> None:
            raise ValueError("workflow forbids reopen")

        monkeypatch.setattr(db, "reopen_issue", boom)
        with caplog.at_level(logging.WARNING, logger="filigree.db_files"):
            stats = db.process_scan_results(scan_source="wardline", findings=[_wln("src/a.py", "fp-failreopen")])

        # On the wire: the failure rides out in stats["warnings"].
        assert any(f"cascade reopen of issue {issue_id} failed" in w for w in stats["warnings"])
        # In the logs: a per-failure warning (so "every cascade is failing" is visible).
        assert any("reopen cascade" in r.message for r in caplog.records)
        # The reopen genuinely failed → issue stays closed.
        assert _is_done(db, issue_id)
