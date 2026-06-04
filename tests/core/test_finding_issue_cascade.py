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

    def test_sibling_open_finding_blocks_clean_stale_close(self, db: FiligreeDB) -> None:
        """The sibling-open guard protects the clean-stale path too (shared tx):
        archiving one of an issue's findings to ``fixed`` must not close the
        issue while another linked finding is still open."""
        a_id = _ingest(db, "fp-cs-sibA")  # src/a.py
        issue = db.promote_finding_to_issue(a_id, actor="t")["issue"]
        db.process_scan_results(scan_source="wardline", findings=[_wln("src/b.py", "fp-cs-sibB")])
        b_id = db.find_finding_by_fingerprint("wardline", "fp-cs-sibB")["id"]  # type: ignore[index]
        db.update_finding(b_id, issue_id=issue.id, actor="t")
        # Age A into the stale window; B stays open.
        db.conn.execute(
            "UPDATE scan_findings SET status = 'unseen_in_latest', last_seen_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (a_id,),
        )
        db.conn.commit()

        result = db.clean_stale_findings(days=30)

        assert result["findings_fixed"] == 1  # A archived to fixed
        assert issue.id not in result["closed_issue_ids"]  # B still open → not closed
        assert not _is_done(db, issue.id)


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


class TestCloseOnFixedFromIngest:
    """Close-on-fixed fires eagerly from scan ingest, not just from the
    age-gated ``clean_stale_findings`` sweep.

    The product gap this closes: when an agent fixes the last/only finding in a
    file and re-scans, that file is clean — it carries zero findings in the
    batch. The per-(file, scan_source) unseen sweep only visits files present in
    the batch, so without ``scanned_paths`` the fixed file is never swept and the
    linked issue never closes. Wardline already emits ``scanned_paths`` (the full
    scanned-file set incl. clean files) and ``mark_unseen=True``; ingest now
    consumes both. NONE of these tests use a same-file "decoy" finding — the
    fixed file is present solely via ``scanned_paths``.
    """

    def test_fix_only_finding_in_file_closes_issue_no_decoy(self, db: FiligreeDB) -> None:
        """The headline DoD: fix the only finding in a file → re-scan with the
        file present solely via ``scanned_paths`` → finding goes unseen AND the
        linked issue closes. No decoy finding keeps the file in the batch."""
        finding_id = _ingest(db, "fp-fix")  # src/a.py
        issue = db.promote_finding_to_issue(finding_id, actor="t")["issue"]
        assert not _is_done(db, issue.id)

        db.process_scan_results(
            scan_source="wardline",
            findings=[],
            scanned_paths=["src/a.py"],
            mark_unseen=True,
        )

        assert db.get_finding(finding_id)["status"] == "unseen_in_latest"
        assert _is_done(db, issue.id)

    def test_mixed_file_closes_only_the_disappeared_finding(self, db: FiligreeDB) -> None:
        """A file with one surviving and one fixed finding: only the disappeared
        finding goes unseen and only its issue closes; the survivor is untouched."""
        db.process_scan_results(
            scan_source="wardline",
            findings=[_wln("src/a.py", "fp-stays"), _wln("src/a.py", "fp-goes")],
        )
        f_stays = db.find_finding_by_fingerprint("wardline", "fp-stays")["id"]  # type: ignore[index]
        f_goes = db.find_finding_by_fingerprint("wardline", "fp-goes")["id"]  # type: ignore[index]
        issue_stays = db.promote_finding_to_issue(f_stays, actor="t")["issue"]
        issue_goes = db.promote_finding_to_issue(f_goes, actor="t")["issue"]

        db.process_scan_results(
            scan_source="wardline",
            findings=[_wln("src/a.py", "fp-stays")],
            scanned_paths=["src/a.py"],
            mark_unseen=True,
        )

        assert db.get_finding(f_goes)["status"] == "unseen_in_latest"
        assert db.get_finding(f_stays)["status"] != "unseen_in_latest"
        assert _is_done(db, issue_goes.id)
        assert not _is_done(db, issue_stays.id)

    def test_reingest_reopens_after_close_from_ingest(self, db: FiligreeDB) -> None:
        """Reopen-on-regress still works on top of the ingest close: a finding
        that reappears reopens the issue the ingest cascade had closed."""
        finding_id = _ingest(db, "fp-re")
        issue = db.promote_finding_to_issue(finding_id, actor="t")["issue"]
        db.process_scan_results(scan_source="wardline", findings=[], scanned_paths=["src/a.py"], mark_unseen=True)
        assert _is_done(db, issue.id)

        db.process_scan_results(scan_source="wardline", findings=[_wln("src/a.py", "fp-re")])

        assert not _is_done(db, issue.id)
        assert db.get_finding(finding_id)["status"] == "open"

    def test_human_closed_issue_neither_reclosed_nor_reopened(self, db: FiligreeDB) -> None:
        """A human's terminal decision is the authority: the ingest cascade does
        not re-close it (the ``== "done"`` guard), and a later regress does not
        reopen it (the reopen gate sees the human actor, not the cascade)."""
        finding_id = _ingest(db, "fp-h")
        issue = db.promote_finding_to_issue(finding_id, actor="t")["issue"]
        db.close_issue(issue.id, actor="human", force=True)

        # Clean re-scan: cascade must not touch a human-closed issue.
        db.process_scan_results(scan_source="wardline", findings=[], scanned_paths=["src/a.py"], mark_unseen=True)
        assert _is_done(db, issue.id)

        # Finding regresses: must not reopen over the human decision.
        db.process_scan_results(scan_source="wardline", findings=[_wln("src/a.py", "fp-h")])
        assert _is_done(db, issue.id)

    def test_idempotent_with_clean_stale(self, db: FiligreeDB) -> None:
        """After the ingest cascade closes an issue, clean-stale archiving the
        now-unseen finding to ``fixed`` must not error or re-close (the issue is
        already done — clean-stale's close hits the ``== "done"`` guard)."""
        finding_id = _ingest(db, "fp-idem")
        issue = db.promote_finding_to_issue(finding_id, actor="t")["issue"]
        db.process_scan_results(scan_source="wardline", findings=[], scanned_paths=["src/a.py"], mark_unseen=True)
        assert _is_done(db, issue.id)

        # Age the unseen finding into the stale window, then sweep.
        db.conn.execute(
            "UPDATE scan_findings SET last_seen_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (finding_id,),
        )
        db.conn.commit()
        result = db.clean_stale_findings(days=30)

        assert result["findings_fixed"] == 1
        assert db.get_finding(finding_id)["status"] == "fixed"
        assert issue.id not in result["closed_issue_ids"]  # already done → no re-close
        assert _is_done(db, issue.id)

    def test_no_spurious_close_when_finding_still_present(self, db: FiligreeDB) -> None:
        """A re-POST that re-includes the finding (and its file in scanned_paths)
        closes nothing: the finding is still seen, so it never transitions."""
        finding_id = _ingest(db, "fp-keep")
        issue = db.promote_finding_to_issue(finding_id, actor="t")["issue"]

        db.process_scan_results(
            scan_source="wardline",
            findings=[_wln("src/a.py", "fp-keep")],
            scanned_paths=["src/a.py"],
            mark_unseen=True,
        )

        assert db.get_finding(finding_id)["status"] == "open"
        assert not _is_done(db, issue.id)

    def test_empty_batch_without_scanned_paths_still_rejected(self, db: FiligreeDB) -> None:
        """The empty-batch guard still fires when there is genuinely nothing to
        sweep — no findings AND no scanned_paths."""
        with pytest.raises(ValueError, match="at least one finding or scanned path"):
            db.process_scan_results(scan_source="wardline", findings=[], scanned_paths=[], mark_unseen=True)

    def test_unknown_scanned_path_is_a_noop(self, db: FiligreeDB) -> None:
        """A scanned path with no prior file record is skipped (lookup, not
        upsert) — no error, nothing swept."""
        stats = db.process_scan_results(
            scan_source="wardline",
            findings=[],
            scanned_paths=["never/seen.py"],
            mark_unseen=True,
        )
        assert stats["findings_created"] == 0

    def test_close_cascade_failure_surfaced_in_warnings_and_logged(
        self, db: FiligreeDB, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed best-effort close rides out in ``stats["warnings"]`` (the
        wire) and is logged per-failure, mirroring the reopen path."""
        finding_id = _ingest(db, "fp-failclose")
        issue = db.promote_finding_to_issue(finding_id, actor="t")["issue"]

        def boom(*args: object, **kwargs: object) -> None:
            raise ValueError("workflow forbids close")

        monkeypatch.setattr(db, "close_issue", boom)
        with caplog.at_level(logging.WARNING, logger="filigree.db_files"):
            stats = db.process_scan_results(scan_source="wardline", findings=[], scanned_paths=["src/a.py"], mark_unseen=True)

        assert any(f"cascade close of issue {issue.id} failed" in w for w in stats["warnings"])
        assert any("close cascade" in r.message for r in caplog.records)
        assert not _is_done(db, issue.id)  # close genuinely failed → issue open

    def test_sibling_open_finding_blocks_close(self, db: FiligreeDB) -> None:
        """An issue linked to two findings must NOT close when only one resolves:
        a still-open sibling finding is an active defect. (An issue can link more
        than one finding via ``update_finding(..., issue_id=...)``.)"""
        a_id = _ingest(db, "fp-sibA")  # src/a.py
        issue = db.promote_finding_to_issue(a_id, actor="t")["issue"]
        db.process_scan_results(scan_source="wardline", findings=[_wln("src/b.py", "fp-sibB")])
        b_id = db.find_finding_by_fingerprint("wardline", "fp-sibB")["id"]  # type: ignore[index]
        db.update_finding(b_id, issue_id=issue.id, actor="t")  # second finding on the same issue

        # A's file is now clean (A absent); B is still present and open.
        db.process_scan_results(
            scan_source="wardline",
            findings=[_wln("src/b.py", "fp-sibB")],
            scanned_paths=["src/a.py", "src/b.py"],
            mark_unseen=True,
        )

        assert db.get_finding(a_id)["status"] == "unseen_in_latest"
        assert db.get_finding(b_id)["status"] == "open"
        assert not _is_done(db, issue.id)  # sibling B still open → issue stays open

    def test_same_batch_regress_and_resolve_keeps_issue_open(self, db: FiligreeDB) -> None:
        """When one finding regresses and another resolves on the SAME issue in
        one batch, reopen wins over close — a regress is an active defect."""
        db.process_scan_results(
            scan_source="wardline",
            findings=[_wln("src/a.py", "fp-colA"), _wln("src/a.py", "fp-colB")],
        )
        a_id = db.find_finding_by_fingerprint("wardline", "fp-colA")["id"]  # type: ignore[index]
        b_id = db.find_finding_by_fingerprint("wardline", "fp-colB")["id"]  # type: ignore[index]
        issue = db.promote_finding_to_issue(a_id, actor="t")["issue"]
        db.update_finding(b_id, issue_id=issue.id, actor="t")
        # A was resolved on a prior scan (unseen) but the issue stayed open (B open).
        db.conn.execute("UPDATE scan_findings SET status = 'unseen_in_latest' WHERE id = ?", (a_id,))
        db.conn.commit()

        # Collision batch: A reappears (regress → open), B disappears (resolve → unseen).
        db.process_scan_results(
            scan_source="wardline",
            findings=[_wln("src/a.py", "fp-colA")],
            scanned_paths=["src/a.py"],
            mark_unseen=True,
        )

        assert db.get_finding(a_id)["status"] == "open"  # regressed
        assert db.get_finding(b_id)["status"] == "unseen_in_latest"  # resolved
        assert not _is_done(db, issue.id)  # active regressed defect → not closed

    def test_issue_with_open_sibling_finding_not_closed(self, db: FiligreeDB) -> None:
        """One issue linked to two findings (different files); fixing one must NOT
        close the issue while the other finding is still open."""
        f1 = _ingest(db, "fp-sib1")  # src/a.py
        db.process_scan_results(scan_source="wardline", findings=[_wln("src/c.py", "fp-sib2")])
        f2 = db.find_finding_by_fingerprint("wardline", "fp-sib2")["id"]  # type: ignore[index]
        issue = db.promote_finding_to_issue(f1, actor="t")["issue"]
        db.update_finding(f2, issue_id=issue.id)  # link the second finding to the SAME issue
        # Clean a.py (f1 disappears); c.py still carries f2.
        db.process_scan_results(
            scan_source="wardline",
            findings=[_wln("src/c.py", "fp-sib2")],
            mark_unseen=True,
            scanned_paths=["src/a.py", "src/c.py"],
        )
        assert db.get_finding(f1)["status"] == "unseen_in_latest"
        assert db.get_finding(f2)["status"] == "open"
        assert not _is_done(db, issue.id)  # MUST stay open — f2 is still active
