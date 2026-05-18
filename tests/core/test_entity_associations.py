"""Tests for entity_associations CRUD (ADR-029, Clarion B.7 / WP9-A).

Covers the data-layer surface of :class:`EntityAssociationsMixin`. The
MCP tool layer and HTTP route layer have their own test files. The
federation §5 audit lives in ``test_entity_associations_federation.py``.
"""

from __future__ import annotations

import pytest

from filigree.core import FiligreeDB


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
        assert row["clarion_entity_id"] == "py:func:parser.tokenize"
        assert row["content_hash_at_attach"] == "hash-a"
        assert row["attached_by"] == "alice"
        assert row["attached_at"]  # non-empty timestamp

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

    def test_attach_rejects_foreign_prefix(self, db: FiligreeDB) -> None:
        """Prefix enforcement matches every other write-side mutation."""
        from filigree.core import WrongProjectError

        with pytest.raises(WrongProjectError):
            db.add_entity_association("other-1234567890", "py:func:foo", content_hash="hash")


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
        assert rows[0]["clarion_entity_id"] == "py:func:b"

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
        ids = {row["clarion_entity_id"] for row in rows}
        assert ids == {"py:func:a", "py:func:b", "py:class:C"}

    def test_does_not_leak_other_issues_associations(self, db: FiligreeDB) -> None:
        a = db.create_issue("a", priority=2)
        b = db.create_issue("b", priority=2)
        db.add_entity_association(a.id, "py:func:x", content_hash="h1")
        db.add_entity_association(b.id, "py:func:y", content_hash="h2")

        rows_a = db.list_entity_associations(a.id)
        assert {r["clarion_entity_id"] for r in rows_a} == {"py:func:x"}

    def test_list_does_not_compute_drift(self, db: FiligreeDB) -> None:
        """ADR-029 §"Decision 3" — drift comparison is the consumer's job
        (Clarion's issues_for after fetching). list_entity_associations
        returns raw rows; no drift_warning field exists.
        """
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, "py:func:a", content_hash="original")
        rows = db.list_entity_associations(issue.id)
        assert "drift_warning" not in rows[0]
        # The stored hash is returned verbatim so the caller can compare.
        assert rows[0]["content_hash_at_attach"] == "original"


class TestListAssociationsByEntity:
    """Reverse lookup — the surface Clarion's issues_for (B.6) calls."""

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
        assert all(row["clarion_entity_id"] == target for row in rows)

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
        assert rows[0]["clarion_entity_id"] == weird


# Cascade behaviour (ON DELETE CASCADE on issue_id) is pinned at the schema
# level in test_schema.TestEntityAssociationsSchema::test_cascade_delete_removes_associations,
# using a raw issues row with no other FK dependents. Replicating it here would
# need an isolated fixture that doesn't create dependencies/events/labels —
# overkill for a property already covered.
