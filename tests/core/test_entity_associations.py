"""Tests for entity_associations CRUD (ADR-029, Loomweave B.7 / WP9-A).

Covers the data-layer surface of :class:`EntityAssociationsMixin`. The
MCP tool layer and HTTP route layer have their own test files. The
federation §5 audit lives in ``test_entity_associations_federation.py``.
"""

from __future__ import annotations

import threading
import time

import pytest

from filigree.core import FiligreeDB
from tests._db_factory import make_db


class TestAddEntityAssociation:
    def test_attach_creates_row(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Refactor parser", priority=2)
        row = db.add_entity_association(
            issue.id,
            "py:func:parser.tokenize",
            content_hash="hash-a",
            actor="alice",
        )
        assert row["issue_id"] == issue.id
        assert row["loomweave_entity_id"] == "py:func:parser.tokenize"
        assert row["content_hash_at_attach"] == "hash-a"
        assert row["attached_by"] == "alice"
        assert row["attached_at"]  # non-empty timestamp

    def test_attach_records_audit_event(self, db: FiligreeDB) -> None:
        issue = db.create_issue("t", priority=2)

        db.add_entity_association(issue.id, "py:func:a", content_hash="h1", actor="alice")

        events = db.get_issue_events(issue.id, limit=10)
        added = [event for event in events if event["event_type"] == "entity_association_added"]
        assert len(added) == 1
        assert added[0]["actor"] == "alice"
        assert added[0]["new_value"] == "py:func:a"
        assert added[0]["comment"] == "h1"

    def test_attach_is_idempotent_and_refreshes_hash(self, db: FiligreeDB) -> None:
        """Re-attaching the same (issue, entity) updates the hash and timestamp
        but preserves the original attached_by — the audit signal "who first
        bound this issue" survives drift refreshes.
        """
        issue = db.create_issue("Refactor parser", priority=2)
        first = db.add_entity_association(issue.id, "py:func:parser.tokenize", content_hash="hash-a", actor="alice")
        second = db.add_entity_association(issue.id, "py:func:parser.tokenize", content_hash="hash-b", actor="bob")
        assert second["content_hash_at_attach"] == "hash-b"
        assert second["attached_by"] == "alice"  # preserved
        # attached_at may have advanced; assert it didn't go backwards.
        assert second["attached_at"] >= first["attached_at"]

        # Only one row exists.
        rows = db.list_entity_associations(issue.id)
        assert len(rows) == 1

    def test_reattach_records_refresh_audit_event(self, db: FiligreeDB) -> None:
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, "py:func:a", content_hash="h1", actor="alice")

        db.add_entity_association(issue.id, "py:func:a", content_hash="h2", actor="bob")

        events = db.get_issue_events(issue.id, limit=10)
        refreshed = [event for event in events if event["event_type"] == "entity_association_refreshed"]
        assert len(refreshed) == 1
        assert refreshed[0]["actor"] == "bob"
        assert refreshed[0]["old_value"] == "h1"
        assert refreshed[0]["new_value"] == "h2"
        assert refreshed[0]["comment"] == "py:func:a"

    def test_attach_rejects_missing_issue(self, db: FiligreeDB) -> None:
        # Use the test fixture's project prefix so the prefix guard passes
        # and we exercise the actual "issue not found" path.
        with pytest.raises(KeyError, match="Issue not found"):
            db.add_entity_association("test-nonexistent", "py:func:foo", content_hash="hash")

    def test_attach_rejects_empty_entity_id(self, db: FiligreeDB) -> None:
        issue = db.create_issue("t", priority=2)
        with pytest.raises(ValueError, match="entity_id must not be blank"):
            db.add_entity_association(issue.id, "", content_hash="hash")

    def test_attach_rejects_whitespace_entity_id(self, db: FiligreeDB) -> None:
        """Match the MCP/HTTP layers, which both reject .strip() == ""."""
        issue = db.create_issue("t", priority=2)
        with pytest.raises(ValueError, match="entity_id must not be blank"):
            db.add_entity_association(issue.id, "   ", content_hash="hash")

    def test_attach_rejects_empty_content_hash(self, db: FiligreeDB) -> None:
        issue = db.create_issue("t", priority=2)
        with pytest.raises(ValueError, match="content_hash must not be blank"):
            db.add_entity_association(issue.id, "py:func:foo", content_hash="")

    def test_attach_rejects_whitespace_content_hash(self, db: FiligreeDB) -> None:
        issue = db.create_issue("t", priority=2)
        with pytest.raises(ValueError, match="content_hash must not be blank"):
            db.add_entity_association(issue.id, "py:func:foo", content_hash="\t\n ")

    @pytest.mark.parametrize(
        "bad_hash",
        [
            " padded",
            "padded ",
            "has space",
            "line\nbreak",
            "null\x00byte",
            "x" * 513,
        ],
    )
    def test_attach_rejects_garbage_content_hash(self, db: FiligreeDB, bad_hash: str) -> None:
        issue = db.create_issue("t", priority=2)
        with pytest.raises(ValueError, match="content_hash"):
            db.add_entity_association(issue.id, "py:func:foo", content_hash=bad_hash)

    def test_attach_rejects_foreign_prefix(self, db: FiligreeDB) -> None:
        """Prefix enforcement matches every other write-side mutation."""
        from filigree.core import WrongProjectError

        with pytest.raises(WrongProjectError):
            db.add_entity_association("other-1234567890", "py:func:foo", content_hash="hash")

    def test_attach_retries_when_writer_lock_clears(self, tmp_path) -> None:
        owner = make_db(tmp_path, check_same_thread=False)
        issue = owner.create_issue("entity contention target", priority=2)
        db_path = owner.db_path
        owner.close()

        holder = FiligreeDB(db_path, prefix="test", check_same_thread=False)
        writer = FiligreeDB(db_path, prefix="test", check_same_thread=False)
        try:
            holder.conn.execute("BEGIN IMMEDIATE")
            writer.conn.execute("PRAGMA busy_timeout=1")
            rows: list[dict[str, object]] = []
            errors: list[BaseException] = []

            def attach() -> None:
                try:
                    rows.append(dict(writer.add_entity_association(issue.id, "py:func:locked", content_hash="h")))
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=attach)
            thread.start()
            time.sleep(0.02)
            holder.conn.commit()
            thread.join(timeout=2)

            assert not thread.is_alive()
            assert errors == []
            assert rows[0]["issue_id"] == issue.id
            assert rows[0]["loomweave_entity_id"] == "py:func:locked"
        finally:
            if holder.conn.in_transaction:
                holder.conn.rollback()
            holder.close()
            writer.close()


class TestRemoveEntityAssociation:
    def test_remove_existing_returns_true(self, db: FiligreeDB) -> None:
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, "py:func:foo", content_hash="h")
        assert db.remove_entity_association(issue.id, "py:func:foo") is True
        assert db.list_entity_associations(issue.id) == []

    def test_remove_missing_returns_false(self, db: FiligreeDB) -> None:
        """Idempotent — no-op on missing association."""
        issue = db.create_issue("t", priority=2)
        assert db.remove_entity_association(issue.id, "py:func:never-attached") is False

    def test_remove_only_targets_named_entity(self, db: FiligreeDB) -> None:
        """Removing one association leaves siblings intact (composite-key precision)."""
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, "py:func:a", content_hash="h1")
        db.add_entity_association(issue.id, "py:func:b", content_hash="h2")
        db.remove_entity_association(issue.id, "py:func:a")
        rows = db.list_entity_associations(issue.id)
        assert len(rows) == 1
        assert rows[0]["loomweave_entity_id"] == "py:func:b"

    def test_remove_records_audit_event(self, db: FiligreeDB) -> None:
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, "py:func:a", content_hash="h1")

        assert db.remove_entity_association(issue.id, "py:func:a", actor="alice") is True

        events = db.get_issue_events(issue.id, limit=10)
        removed = [event for event in events if event["event_type"] == "entity_association_removed"]
        assert len(removed) == 1
        assert removed[0]["actor"] == "alice"
        assert removed[0]["old_value"] == "py:func:a"

    def test_remove_rejects_empty_entity_id(self, db: FiligreeDB) -> None:
        issue = db.create_issue("t", priority=2)
        with pytest.raises(ValueError, match="entity_id must not be blank"):
            db.remove_entity_association(issue.id, "")

    def test_remove_rejects_whitespace_entity_id(self, db: FiligreeDB) -> None:
        issue = db.create_issue("t", priority=2)
        with pytest.raises(ValueError, match="entity_id must not be blank"):
            db.remove_entity_association(issue.id, "  ")

    def test_remove_retries_when_writer_lock_clears_and_records_event(self, tmp_path) -> None:
        owner = make_db(tmp_path, check_same_thread=False)
        issue = owner.create_issue("entity removal contention target", priority=2)
        owner.add_entity_association(issue.id, "py:func:locked", content_hash="h")
        db_path = owner.db_path
        owner.close()

        holder = FiligreeDB(db_path, prefix="test", check_same_thread=False)
        writer = FiligreeDB(db_path, prefix="test", check_same_thread=False)
        try:
            holder.conn.execute("BEGIN IMMEDIATE")
            writer.conn.execute("PRAGMA busy_timeout=1")
            results: list[bool] = []
            errors: list[BaseException] = []

            def remove() -> None:
                try:
                    results.append(writer.remove_entity_association(issue.id, "py:func:locked", actor="alice"))
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=remove)
            thread.start()
            time.sleep(0.02)
            holder.conn.commit()
            thread.join(timeout=2)

            assert not thread.is_alive()
            assert errors == []
            assert results == [True]
            assert writer.list_entity_associations(issue.id) == []
            events = writer.get_issue_events(issue.id, limit=10)
            removed = [event for event in events if event["event_type"] == "entity_association_removed"]
            assert len(removed) == 1
            assert removed[0]["actor"] == "alice"
        finally:
            if holder.conn.in_transaction:
                holder.conn.rollback()
            holder.close()
            writer.close()


class TestListEntityAssociations:
    def test_empty_issue_returns_empty_list(self, db: FiligreeDB) -> None:
        issue = db.create_issue("t", priority=2)
        assert db.list_entity_associations(issue.id) == []

    def test_returns_all_attached_entities(self, db: FiligreeDB) -> None:
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, "py:func:a", content_hash="h1")
        db.add_entity_association(issue.id, "py:func:b", content_hash="h2")
        db.add_entity_association(issue.id, "py:class:C", content_hash="h3")

        rows = db.list_entity_associations(issue.id)
        ids = {row["loomweave_entity_id"] for row in rows}
        assert ids == {"py:func:a", "py:func:b", "py:class:C"}

    def test_does_not_leak_other_issues_associations(self, db: FiligreeDB) -> None:
        a = db.create_issue("a", priority=2)
        b = db.create_issue("b", priority=2)
        db.add_entity_association(a.id, "py:func:x", content_hash="h1")
        db.add_entity_association(b.id, "py:func:y", content_hash="h2")

        rows_a = db.list_entity_associations(a.id)
        assert {r["loomweave_entity_id"] for r in rows_a} == {"py:func:x"}

    def test_list_does_not_compute_drift(self, db: FiligreeDB) -> None:
        """ADR-029 §"Decision 3" — drift comparison is the consumer's job
        (Loomweave's issues_for after fetching). list_entity_associations
        returns raw rows; no drift_warning field exists.
        """
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, "py:func:a", content_hash="original")
        rows = db.list_entity_associations(issue.id)
        assert "drift_warning" not in rows[0]
        # The stored hash is returned verbatim so the caller can compare.
        assert rows[0]["content_hash_at_attach"] == "original"


class TestListAssociationsByEntity:
    """Reverse lookup — the surface Loomweave's issues_for (B.6) calls."""

    def test_empty_entity_returns_empty_list(self, db: FiligreeDB) -> None:
        assert db.list_associations_by_entity("py:func:never-attached") == []

    def test_returns_all_issues_bound_to_entity(self, db: FiligreeDB) -> None:
        a = db.create_issue("a", priority=2)
        b = db.create_issue("b", priority=2)
        c = db.create_issue("c", priority=2)
        target = "py:func:parser.tokenize"
        db.add_entity_association(a.id, target, content_hash="h1")
        db.add_entity_association(b.id, target, content_hash="h2")
        db.add_entity_association(c.id, "py:func:unrelated", content_hash="h3")

        rows = db.list_associations_by_entity(target)
        issue_ids = {row["issue_id"] for row in rows}
        assert issue_ids == {a.id, b.id}
        # The unrelated entity's binding does not appear in the result.
        assert all(row["loomweave_entity_id"] == target for row in rows)

    def test_returns_raw_hash_for_drift_comparison(self, db: FiligreeDB) -> None:
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, "py:func:x", content_hash="original")
        rows = db.list_associations_by_entity("py:func:x")
        assert rows[0]["content_hash_at_attach"] == "original"
        assert "drift_warning" not in rows[0]

    def test_rejects_blank_entity_id(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="entity_id must not be blank"):
            db.list_associations_by_entity("")
        with pytest.raises(ValueError, match="entity_id must not be blank"):
            db.list_associations_by_entity("   ")

    def test_treats_entity_id_opaquely(self, db: FiligreeDB) -> None:
        """Federation enrich-only: malformed entity IDs round-trip verbatim,
        with no parsing or schema enforcement on the lookup side."""
        issue = db.create_issue("t", priority=2)
        weird = "::: not a real grammar :::"
        db.add_entity_association(issue.id, weird, content_hash="h")
        rows = db.list_associations_by_entity(weird)
        assert len(rows) == 1
        assert rows[0]["loomweave_entity_id"] == weird


# Cascade behaviour (ON DELETE CASCADE on issue_id) is pinned at the schema
# level in test_schema.TestEntityAssociationsSchema::test_cascade_delete_removes_associations,
# using a raw issues row with no other FK dependents. Replicating it here would
# need an isolated fixture that doesn't create dependencies/events/labels —
# overkill for a property already covered.


class TestSignatureAndSignoffSeq:
    """B1: opaque Legis HMAC ``signature`` + ``signoff_seq`` persistence.

    Filigree stores both verbatim and echoes them on read; it never verifies
    the signature (it has no key), exactly as it treats content_hash_at_attach.
    """

    def test_attach_persists_signature_and_signoff_seq(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Governed work", priority=2)
        row = db.add_entity_association(
            issue.id,
            "sei:abc",
            content_hash="h1",
            actor="legis",
            signature="deadbeef",
            signoff_seq=7,
        )
        assert row["signature"] == "deadbeef"
        assert row["signoff_seq"] == 7
        # Echoed back through both read surfaces.
        listed = db.list_entity_associations(issue.id)
        assert listed[0]["signature"] == "deadbeef"
        assert listed[0]["signoff_seq"] == 7
        by_entity = db.list_associations_by_entity("sei:abc")
        assert by_entity[0]["signature"] == "deadbeef"
        assert by_entity[0]["signoff_seq"] == 7


class TestReverseLookupLifecycleFacts:
    """b-ii (warpline seam): the reverse lookup enriches each binding with the
    bound issue's lifecycle facts — ``claimed_at``, ``closed_at``, ``status``,
    and ``status_category`` — so warpline can correlate "changed since
    claimed/closed" against its own changed-set in one round trip. closed_at is
    the proven-good signal (issue closed at commit X); warpline maps the
    timestamp to a commit on its side.

    The forward per-issue list (``list_entity_associations``) stays a pure
    binding projection and must NOT grow these keys — the shared mapper is
    untouched; only the reverse query joins ``issues``.
    """

    def test_open_issue_row_exposes_null_close_and_open_category(self, db: FiligreeDB) -> None:
        issue = db.create_issue("open work", priority=2)
        db.add_entity_association(issue.id, "py:func:open-target", content_hash="h1", actor="alice")
        (row,) = db.list_associations_by_entity("py:func:open-target")
        assert row["closed_at"] is None
        assert row["claimed_at"] is None
        assert row["status"] == issue.status
        assert row["status_category"] == "open"

    def test_closed_issue_row_exposes_closed_at_and_done_category(self, db: FiligreeDB) -> None:
        issue = db.create_issue("to close", priority=2)
        db.add_entity_association(issue.id, "py:func:closed-target", content_hash="h1", actor="alice")
        db.close_issue(issue.id, reason="done")
        (row,) = db.list_associations_by_entity("py:func:closed-target")
        assert row["closed_at"] is not None
        assert row["status_category"] == "done"

    def test_claimed_issue_row_exposes_claimed_at(self, db: FiligreeDB) -> None:
        issue = db.create_issue("to claim", priority=2)
        db.add_entity_association(issue.id, "py:func:claimed-target", content_hash="h1", actor="alice")
        db.claim_issue(issue.id, assignee="alice")
        (row,) = db.list_associations_by_entity("py:func:claimed-target")
        assert row["claimed_at"] is not None

    def test_orphaned_binding_still_returns_with_null_facts(self, db: FiligreeDB) -> None:
        """LEFT JOIN, not INNER: a binding whose issue row is absent must still
        appear (warpline_consumer contemplates 'binding outlived its issue'),
        with null lifecycle facts rather than being dropped."""
        issue = db.create_issue("soon orphaned", priority=2)
        db.add_entity_association(issue.id, "py:func:orphan-target", content_hash="h1", actor="alice")
        # Delete the issue row directly, leaving the binding (bypassing cascade).
        db.conn.execute("PRAGMA foreign_keys = OFF")
        db.conn.execute("DELETE FROM issues WHERE id = ?", (issue.id,))
        db.conn.execute("PRAGMA foreign_keys = ON")
        (row,) = db.list_associations_by_entity("py:func:orphan-target")
        assert row["issue_id"] == issue.id
        assert row["closed_at"] is None
        assert row["status"] is None
        assert row["status_category"] is None

    def test_forward_list_omits_lifecycle_facts(self, db: FiligreeDB) -> None:
        issue = db.create_issue("forward", priority=2)
        db.add_entity_association(issue.id, "py:func:fwd-target", content_hash="h1", actor="alice")
        (fwd,) = db.list_entity_associations(issue.id)
        assert "closed_at" not in fwd
        assert "claimed_at" not in fwd
        assert "status" not in fwd
        assert "status_category" not in fwd

    def test_attach_without_signature_stores_null(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Ungoverned work", priority=2)
        row = db.add_entity_association(issue.id, "sei:plain", content_hash="h1", actor="x")
        assert row["signature"] is None
        assert row["signoff_seq"] is None
        listed = db.list_entity_associations(issue.id)
        assert listed[0]["signature"] is None
        assert listed[0]["signoff_seq"] is None

    def test_reattach_updates_signature_but_preserves_attached_by(self, db: FiligreeDB) -> None:
        """Re-attach refreshes signature/signoff_seq to the latest binding
        (they pertain to the latest sign-off), mirroring content_hash_at_attach,
        while attached_by stays the original first-attach attribution."""
        issue = db.create_issue("Governed work", priority=2)
        db.add_entity_association(issue.id, "sei:abc", content_hash="h1", actor="alice", signature="sig1", signoff_seq=1)
        second = db.add_entity_association(issue.id, "sei:abc", content_hash="h2", actor="bob", signature="sig2", signoff_seq=2)
        assert second["signature"] == "sig2"
        assert second["signoff_seq"] == 2
        assert second["attached_by"] == "alice"  # preserved

    def test_reattach_without_signature_preserves_prior(self, db: FiligreeDB) -> None:
        """A signatureless re-attach PRESERVES the prior Legis sign-off (v27,
        sticky governance — PR #52 fix). Only Legis can sign; an agent's drift
        refresh must not silently revoke governance by clobbering the signature
        to NULL. signature/signoff_seq/signed_content_hash all survive, while
        content_hash_at_attach advances — leaving the binding governed-but-stale."""
        issue = db.create_issue("Governed stays governed", priority=2)
        db.add_entity_association(issue.id, "sei:abc", content_hash="h1", actor="alice", signature="sig1", signoff_seq=1)
        second = db.add_entity_association(issue.id, "sei:abc", content_hash="h2", actor="bob")
        assert second["signature"] == "sig1"  # preserved, not clobbered
        assert second["signoff_seq"] == 1
        assert second["signed_content_hash"] == "h1"  # still bound to the signed snapshot
        assert second["content_hash_at_attach"] == "h2"  # but the content advanced -> stale

    def test_signed_attach_records_signed_content_hash(self, db: FiligreeDB) -> None:
        """A signed write records the content the signature was cut over."""
        issue = db.create_issue("Governed", priority=2)
        row = db.add_entity_association(issue.id, "sei:abc", content_hash="h1", actor="legis", signature="sig1", signoff_seq=1)
        assert row["signed_content_hash"] == "h1"

    def test_unsigned_attach_leaves_signed_content_hash_null(self, db: FiligreeDB) -> None:
        """An ungoverned binding has no signed snapshot."""
        issue = db.create_issue("Ungoverned", priority=2)
        row = db.add_entity_association(issue.id, "sei:plain", content_hash="h1", actor="x")
        assert row["signed_content_hash"] is None

    def test_signed_reattach_advances_signed_content_hash(self, db: FiligreeDB) -> None:
        """A re-sign (write carrying a new signature) re-binds the snapshot to the
        new content, making the binding fresh again."""
        issue = db.create_issue("Re-signed", priority=2)
        db.add_entity_association(issue.id, "sei:abc", content_hash="h1", actor="legis", signature="sig1", signoff_seq=1)
        second = db.add_entity_association(issue.id, "sei:abc", content_hash="h2", actor="legis", signature="sig2", signoff_seq=2)
        assert second["signature"] == "sig2"
        assert second["signed_content_hash"] == "h2"  # re-bound to current content
        assert second["content_hash_at_attach"] == "h2"  # fresh again

    def test_empty_string_signature_normalises_to_null(self, db: FiligreeDB) -> None:
        """A blank signature is stored as NULL (data-layer normalisation), so it
        cannot masquerade as a governed-but-falsy binding (vector a)."""
        issue = db.create_issue("Blank sig", priority=2)
        row = db.add_entity_association(issue.id, "sei:abc", content_hash="h1", actor="x", signature="")
        assert row["signature"] is None
        assert row["signed_content_hash"] is None

    def test_export_import_round_trips_signature_and_signoff_seq(self, tmp_path: object) -> None:
        """B1: a governed binding's signature/signoff_seq survive a JSONL
        backup/restore verbatim (export uses SELECT *; import threads them)."""
        from pathlib import Path

        src_dir = Path(str(tmp_path)) / "src" / ".filigree"
        src_dir.mkdir(parents=True)
        src = FiligreeDB.from_filigree_dir(src_dir)
        issue = src.create_issue(title="Governed", priority=2)
        src.add_entity_association(issue.id, "sei:abc", content_hash="h1", actor="legis", signature="sigX", signoff_seq=9)
        # Drift the content via a signatureless re-attach so signed_content_hash
        # (h1) and content_hash_at_attach (h2) diverge — the round-trip must keep
        # them distinct or a backup/restore silently un-stales a drifted binding.
        src.add_entity_association(issue.id, "sei:abc", content_hash="h2", actor="agent")
        export_path = Path(str(tmp_path)) / "dump.jsonl"
        src.export_jsonl(export_path)

        # v26: the exported JSONL carries the renamed key, never the old one.
        blob = export_path.read_text()
        assert "loomweave_entity_id" in blob
        assert "clarion_entity_id" not in blob

        dst_dir = Path(str(tmp_path)) / "dst" / ".filigree"
        dst_dir.mkdir(parents=True)
        dst = FiligreeDB.from_filigree_dir(dst_dir)
        dst.import_jsonl(export_path, allow_foreign_ids=True)

        # Read via raw conn: the imported issue keeps its source prefix, which
        # the dst project's _check_id_prefix would reject on the public read path.
        row = dst.conn.execute(
            "SELECT signature, signoff_seq, signed_content_hash, content_hash_at_attach FROM entity_associations WHERE issue_id = ?",
            (issue.id,),
        ).fetchone()
        assert row is not None
        assert row["signature"] == "sigX"  # preserved across the drift refresh
        assert row["signoff_seq"] == 9
        assert row["signed_content_hash"] == "h1"  # signed snapshot survives restore
        assert row["content_hash_at_attach"] == "h2"  # drifted content survives -> still stale

    def test_import_normalises_blank_signature_to_null(self, tmp_path: object) -> None:
        """A foreign/hand-built JSONL carrying ``signature: ""`` must not land an
        empty string in the column (import uses a raw INSERT that bypasses
        add_entity_association, so the normalisation is applied there too). Else a
        blank signature would be classified governed (``is not None``) on a row
        whose blank value was never a real sign-off."""
        from pathlib import Path

        src_dir = Path(str(tmp_path)) / "src" / ".filigree"
        src_dir.mkdir(parents=True)
        src = FiligreeDB.from_filigree_dir(src_dir)
        issue = src.create_issue(title="Blank sig on import", priority=2)
        src.add_entity_association(issue.id, "sei:abc", content_hash="h1", actor="legis", signature="sigX", signoff_seq=9)
        export_path = Path(str(tmp_path)) / "dump.jsonl"
        src.export_jsonl(export_path)

        # Tamper the export: blank out the signature on the entity_association line.
        tampered = export_path.read_text().replace('"signature": "sigX"', '"signature": ""')
        assert '"signature": ""' in tampered
        export_path.write_text(tampered)

        dst_dir = Path(str(tmp_path)) / "dst" / ".filigree"
        dst_dir.mkdir(parents=True)
        dst = FiligreeDB.from_filigree_dir(dst_dir)
        dst.import_jsonl(export_path, allow_foreign_ids=True)

        row = dst.conn.execute(
            "SELECT signature FROM entity_associations WHERE issue_id = ?",
            (issue.id,),
        ).fetchone()
        assert row is not None
        assert row["signature"] is None  # "" normalised away on import


class TestCreateIssueWithEntity:
    """SEAM SEI-on-create (ADR-029, L1): create_issue binds an opaque entity_id
    inline so a hand-filed ticket enters ON the spine in one call.
    """

    def test_create_with_entity_id_binds_inline(self, db: FiligreeDB) -> None:
        issue = db.create_issue(
            "Hand-filed, on the spine",
            priority=2,
            entity_id="py:func:parser.tokenize",
            content_hash="hash-a",
            entity_kind="function",
            actor="alice",
        )
        rows = db.list_entity_associations(issue.id)
        assert len(rows) == 1
        assert rows[0]["entity_id"] == "py:func:parser.tokenize"
        assert rows[0]["content_hash_at_attach"] == "hash-a"
        assert rows[0]["entity_kind"] == "function"
        assert rows[0]["attached_by"] == "alice"

    def test_create_with_entity_id_records_added_event(self, db: FiligreeDB) -> None:
        issue = db.create_issue("t", priority=2, entity_id="py:func:a", content_hash="h1", actor="alice")
        events = db.get_issue_events(issue.id, limit=10)
        added = [e for e in events if e["event_type"] == "entity_association_added"]
        assert len(added) == 1
        assert added[0]["new_value"] == "py:func:a"

    def test_create_with_entity_id_no_content_hash_stamps_sentinel(self, db: FiligreeDB) -> None:
        from filigree.db_issues import UNVERIFIED_CONTENT_HASH

        issue = db.create_issue("t", priority=2, entity_id="py:func:a", actor="alice")
        rows = db.list_entity_associations(issue.id)
        assert len(rows) == 1
        assert rows[0]["content_hash_at_attach"] == UNVERIFIED_CONTENT_HASH

    def test_create_without_entity_id_binds_nothing(self, db: FiligreeDB) -> None:
        issue = db.create_issue("plain", priority=2)
        assert db.list_entity_associations(issue.id) == []

    def test_create_reverse_lookup_finds_the_issue(self, db: FiligreeDB) -> None:
        """The whole point: the binding makes the issue discoverable by entity."""
        issue = db.create_issue("bound", priority=2, entity_id="py:func:z", content_hash="h1")
        found = db.list_associations_by_entity("py:func:z")
        assert [r["issue_id"] for r in found] == [issue.id]

    def test_create_blank_entity_id_binds_nothing(self, db: FiligreeDB) -> None:
        issue = db.create_issue("t", priority=2, entity_id="   ")
        assert db.list_entity_associations(issue.id) == []

    def test_create_content_hash_without_entity_id_rejected(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="content_hash was supplied without entity_id"):
            db.create_issue("t", priority=2, content_hash="h1")

    def test_create_entity_kind_without_entity_id_rejected(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="entity_kind was supplied without entity_id"):
            db.create_issue("t", priority=2, entity_kind="function")

    def test_bad_content_hash_rolls_back_the_issue(self, db: FiligreeDB) -> None:
        """A bad inline binding fails the whole create atomically — no orphan issue."""
        before = len(db.list_issues())
        with pytest.raises(ValueError, match="content_hash"):
            db.create_issue("t", priority=2, entity_id="py:func:a", content_hash="has space")
        assert len(db.list_issues()) == before
