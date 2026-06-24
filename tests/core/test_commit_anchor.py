"""Commit-anchor capture tests (warpline seam, contract B).

Filigree stores an opaque ``branch@sha`` commit anchor supplied by the caller at
claim and at close, alongside ``claimed_at`` / ``closed_at``. It is stored
verbatim and never parsed (git/CI is Legis's domain). These tests pin the
capture + the clear-point mirror invariant: ``claim_commit`` is cleared wherever
``claimed_at`` is cleared; ``close_commit`` wherever ``closed_at`` is cleared.

Reads go straight to the raw columns so the capture invariant is decoupled from
the read-exposure surface (Issue dataclass / weft / reverse-lookup), which has
its own tests.
"""

from __future__ import annotations

import pytest

from filigree.core import FiligreeDB


def _anchors(db: FiligreeDB, issue_id: str) -> tuple[str | None, str | None]:
    row = db.conn.execute("SELECT claim_commit, close_commit FROM issues WHERE id = ?", (issue_id,)).fetchone()
    return row["claim_commit"], row["close_commit"]


class TestCloseCommitCapture:
    def test_close_with_commit_sets_close_commit(self, db: FiligreeDB) -> None:
        issue = db.create_issue("to close", priority=2)
        db.close_issue(issue.id, reason="done", commit="main@abc123")
        _, close_commit = _anchors(db, issue.id)
        assert close_commit == "main@abc123"

    def test_close_without_commit_leaves_close_commit_null(self, db: FiligreeDB) -> None:
        """WARPLINE-ABSENT parity: a no-commit close behaves exactly as today."""
        issue = db.create_issue("to close", priority=2)
        db.close_issue(issue.id, reason="done")
        _, close_commit = _anchors(db, issue.id)
        assert close_commit is None

    def test_reopen_clears_close_commit(self, db: FiligreeDB) -> None:
        """Mirror invariant: reopen clears ``closed_at`` (via the done->non-done
        status hop in update_issue), so ``close_commit`` must be cleared too — a
        stale anchor surviving a reopen is a bug."""
        issue = db.create_issue("to close+reopen", priority=2)
        db.close_issue(issue.id, reason="done", commit="main@abc123")
        assert _anchors(db, issue.id)[1] == "main@abc123"
        db.reopen_issue(issue.id)
        _, close_commit = _anchors(db, issue.id)
        assert close_commit is None

    def test_update_issue_close_commit_threaded_on_done_entry(self, db: FiligreeDB) -> None:
        issue = db.create_issue("done via update", priority=2)
        # task: open -> in_progress (wip) -> closed (done)
        db.update_issue(issue.id, status="in_progress")
        db.update_issue(issue.id, status="closed", close_commit="feat@deadbeef")
        _, close_commit = _anchors(db, issue.id)
        assert close_commit == "feat@deadbeef"


class TestClaimCommitCapture:
    def test_claim_with_commit_sets_claim_commit(self, db: FiligreeDB) -> None:
        issue = db.create_issue("to claim", priority=2)
        db.claim_issue(issue.id, assignee="alice", commit="main@c0ffee")
        claim_commit, _ = _anchors(db, issue.id)
        assert claim_commit == "main@c0ffee"

    def test_claim_without_commit_leaves_claim_commit_null(self, db: FiligreeDB) -> None:
        """WARPLINE-ABSENT parity: a no-commit claim behaves exactly as today."""
        issue = db.create_issue("to claim", priority=2)
        db.claim_issue(issue.id, assignee="alice")
        claim_commit, _ = _anchors(db, issue.id)
        assert claim_commit is None

    def test_start_work_with_commit_sets_claim_commit(self, db: FiligreeDB) -> None:
        issue = db.create_issue("to start", priority=2)
        db.start_work(issue.id, assignee="alice", commit="main@1234abcd")
        claim_commit, _ = _anchors(db, issue.id)
        assert claim_commit == "main@1234abcd"

    def test_release_clears_claim_commit(self, db: FiligreeDB) -> None:
        """Mirror invariant: release_claim clears ``claimed_at``, so it must
        clear ``claim_commit`` too."""
        issue = db.create_issue("to claim+release", priority=2)
        db.claim_issue(issue.id, assignee="alice", commit="main@c0ffee")
        assert _anchors(db, issue.id)[0] == "main@c0ffee"
        db.release_claim(issue.id, actor="alice")
        claim_commit, _ = _anchors(db, issue.id)
        assert claim_commit is None

    def test_update_unassign_clears_claim_commit(self, db: FiligreeDB) -> None:
        """update_issue(assignee='') clears ``claimed_at`` -> clear claim_commit."""
        issue = db.create_issue("to unassign", priority=2)
        db.claim_issue(issue.id, assignee="alice", commit="main@c0ffee")
        db.update_issue(issue.id, assignee="", expected_assignee="alice")
        claim_commit, _ = _anchors(db, issue.id)
        assert claim_commit is None

    def test_update_assignee_set_sets_claim_commit(self, db: FiligreeDB) -> None:
        issue = db.create_issue("to assign", priority=2)
        db.update_issue(issue.id, assignee="bob", claim_commit="main@feed")
        claim_commit, _ = _anchors(db, issue.id)
        assert claim_commit == "main@feed"

    def test_reclaim_overwrites_claim_commit(self, db: FiligreeDB) -> None:
        """Reclaim transfers to a NEW holder with a fresh claimed_at; the prior
        holder's commit must NOT survive (overwrite, not COALESCE)."""
        issue = db.create_issue("to reclaim", priority=2)
        db.claim_issue(issue.id, assignee="alice", commit="main@old")
        db.reclaim_issue(
            issue.id,
            assignee="bob",
            expected_assignee="alice",
            reason="stale",
            commit="main@new",
        )
        claim_commit, _ = _anchors(db, issue.id)
        assert claim_commit == "main@new"

    def test_reclaim_without_commit_nulls_claim_commit(self, db: FiligreeDB) -> None:
        """Reclaim with no commit must NULL the anchor, not leave the prior
        holder's stale commit."""
        issue = db.create_issue("to reclaim", priority=2)
        db.claim_issue(issue.id, assignee="alice", commit="main@old")
        db.reclaim_issue(issue.id, assignee="bob", expected_assignee="alice", reason="stale")
        claim_commit, _ = _anchors(db, issue.id)
        assert claim_commit is None


class TestCommitAnchorReadExposure:
    """The commit anchors are exposed on every issue read surface (classic
    dataclass / to_dict, weft projection) so warpline can read them."""

    def test_issue_to_dict_carries_commit_anchors(self, db: FiligreeDB) -> None:
        issue = db.create_issue("read", priority=2)
        db.claim_issue(issue.id, assignee="alice", commit="main@claimsha")
        d = db.get_issue(issue.id).to_dict()
        assert d["claim_commit"] == "main@claimsha"
        assert d["close_commit"] is None

    def test_issue_to_dict_null_when_absent(self, db: FiligreeDB) -> None:
        issue = db.create_issue("read-null", priority=2)
        d = db.get_issue(issue.id).to_dict()
        assert d["claim_commit"] is None
        assert d["close_commit"] is None

    def test_classic_read_carries_close_commit(self, db: FiligreeDB) -> None:
        issue = db.create_issue("read-close", priority=2)
        db.close_issue(issue.id, reason="done", commit="main@closesha")
        loaded = db.get_issue(issue.id)
        assert loaded.close_commit == "main@closesha"

    def test_weft_projection_carries_commit_anchors(self, db: FiligreeDB) -> None:
        from filigree.generations.weft.adapters import issue_to_weft

        issue = db.create_issue("weft", priority=2)
        db.claim_issue(issue.id, assignee="alice", commit="main@weftsha")
        weft = issue_to_weft(db.get_issue(issue.id))
        assert weft["claim_commit"] == "main@weftsha"
        assert weft["close_commit"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
