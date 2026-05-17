"""Federation §5 audit for entity_associations (ADR-029, Clarion B.7 / WP9-A).

The Loom federation doctrine (``clarion/docs/suite/loom.md`` §5) names
three failure modes that the enrich-only rule must rule out:

  1. **Semantic coupling** — does Filigree depend on Clarion to function?
  2. **Initialisation coupling** — must Filigree wait for Clarion to start
     (or vice versa)?
  3. **Pipeline coupling** — does Filigree's data become wrong if Clarion
     goes away?

ADR-029 §"Federation check" argues "no" on all three. This test module
encodes those arguments as named tests so the audit is mechanically
visible — failing any one is a stop-the-line signal that the binding
has gained an unintended cross-product dependency.

Each test is deliberately self-contained and uses no Clarion fixtures.
"""

from __future__ import annotations

import socket

import pytest

from filigree.core import FiligreeDB


class TestFederationSemanticCoupling:
    """Failure mode 1: Filigree must not parse, validate, or interpret
    Clarion's entity-ID grammar. Storing any string as ``entity_id`` must
    succeed; round-tripping it through the data layer must preserve it
    byte-for-byte.
    """

    def test_malformed_entity_id_is_accepted(self, db: FiligreeDB) -> None:
        """A string with no Clarion structure at all must round-trip."""
        issue = db.create_issue("Federation §5 test", priority=2)
        # No colons, no plugin_id, no kind — the opposite of ADR-003's
        # three-segment composite. Filigree must not care.
        db.add_entity_association(issue.id, "not-a-valid-clarion-id", content_hash="h")
        rows = db.list_entity_associations(issue.id)
        assert len(rows) == 1
        assert rows[0]["clarion_entity_id"] == "not-a-valid-clarion-id"

    def test_grammar_violations_round_trip_unchanged(self, db: FiligreeDB) -> None:
        """Strings that look syntactically wrong (wrong segment count,
        empty segments, unicode, whitespace) must all round-trip
        verbatim. Filigree never imposes a shape.
        """
        issue = db.create_issue("t", priority=2)
        odd_ids = [
            "a:b",  # two segments, not three
            "a:b:c:d",  # four segments
            ":empty-prefix",
            "ends-with-colon:",
            "py:func:Δ☃🦀",  # unicode payload
            "py:func:has spaces and  tabs",
            "py:func:a/b/c",
        ]
        for entity_id in odd_ids:
            db.add_entity_association(issue.id, entity_id, content_hash="h")
        rows = db.list_entity_associations(issue.id)
        stored = {row["clarion_entity_id"] for row in rows}
        assert stored == set(odd_ids)

    def test_content_hash_is_opaque_too(self, db: FiligreeDB) -> None:
        """Filigree stores content_hash verbatim — never hashes, parses, or
        interprets it. A caller could pass any non-empty string and
        Filigree returns it unchanged at query time. This is load-bearing
        for ADR-029 §"Decision 3" (Clarion does the comparison).
        """
        issue = db.create_issue("t", priority=2)
        for h in ["sha256:abc", "blake3:def", "hash-with-no-prefix", "💩"]:
            db.add_entity_association(issue.id, f"py:func:{h}", content_hash=h)
        rows = db.list_entity_associations(issue.id)
        stored = {row["content_hash_at_attach"] for row in rows}
        assert stored == {"sha256:abc", "blake3:def", "hash-with-no-prefix", "💩"}


class TestFederationInitialisationCoupling:
    """Failure mode 2: Filigree must start, run, and answer queries with no
    Clarion process anywhere on the machine and no network reachability.
    The binding's existence on disk does not change Filigree's startup
    behaviour.
    """

    def test_filigree_runs_with_no_outbound_clarion_calls(self, db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        """Block all socket creation; exercise full CRUD on entity_associations.

        This is a sentinel test: any code path that quietly attempted a
        network call to a Clarion service would raise ``OSError`` from
        the monkeypatched constructor and fail this test loudly.
        """
        original_socket_init = socket.socket.__init__
        outbound_calls = []

        def blocked_socket(self: socket.socket, *args: object, **kwargs: object) -> None:
            outbound_calls.append((args, kwargs))
            raise OSError("federation §5 violation: Filigree attempted a network call")

        monkeypatch.setattr(socket.socket, "__init__", blocked_socket)
        try:
            issue = db.create_issue("Air-gapped issue", priority=2)
            db.add_entity_association(issue.id, "py:func:a", content_hash="h1")
            db.add_entity_association(issue.id, "py:func:b", content_hash="h2")
            rows = db.list_entity_associations(issue.id)
            assert len(rows) == 2
            # Reverse lookup — the exact surface Clarion's issues_for
            # calls — must run with no Clarion process on the box.
            reverse = db.list_associations_by_entity("py:func:a")
            assert len(reverse) == 1
            assert reverse[0]["issue_id"] == issue.id
            removed = db.remove_entity_association(issue.id, "py:func:a")
            assert removed is True
        finally:
            # Restore so teardown doesn't get blocked.
            monkeypatch.setattr(socket.socket, "__init__", original_socket_init)
        assert outbound_calls == [], (
            "Filigree made an outbound network call during entity_associations CRUD — federation §5 (initialisation coupling) is broken."
        )

    def test_no_clarion_module_import(self) -> None:
        """The entity_associations module must not import anything named
        'clarion' or attempt to dispatch onto a Clarion client. Smoke test
        the source for the obvious anti-pattern.
        """
        import filigree.db_entity_associations as mod

        source = mod.__file__
        assert source is not None
        with open(source) as f:
            text = f.read()
        # Comments mentioning Clarion are fine and expected; imports/calls
        # of a literal "clarion" Python module are not.
        assert "import clarion" not in text
        assert "from clarion" not in text


class TestFederationPipelineCoupling:
    """Failure mode 3: an issue's lifecycle (create, update, comment, label,
    close, reopen) must work the same whether or not it has entity
    associations attached. Filigree's data integrity does not depend on
    Clarion being reachable, present, or even installed.
    """

    def test_issue_lifecycle_survives_with_associations_attached(self, db: FiligreeDB) -> None:
        """Full open-to-close lifecycle with three entity associations
        attached — assert the issue passes through every state cleanly
        and the associations remain intact until the issue is deleted
        (FK cascade is a separate concern, exercised in test_schema.py).
        """
        issue = db.create_issue("Lifecycle vs federation", priority=2)
        db.add_entity_association(issue.id, "py:func:a", content_hash="h1")
        db.add_entity_association(issue.id, "py:func:b", content_hash="h2")
        db.add_entity_association(issue.id, "py:class:C", content_hash="h3")

        # Update
        db.update_issue(issue.id, priority=1)
        # Comment
        db.add_comment(issue.id, "still running without Clarion", author="t")
        # Label
        db.add_label(issue.id, "no-clarion-needed")
        # Close
        db.close_issue(issue.id, reason="federation test complete")
        # Reopen
        db.reopen_issue(issue.id)

        # Associations untouched throughout.
        rows = db.list_entity_associations(issue.id)
        assert len(rows) == 3
        ids = {row["clarion_entity_id"] for row in rows}
        assert ids == {"py:func:a", "py:func:b", "py:class:C"}

    def test_associations_table_with_orphaned_entity_ids_does_not_break_reads(self, db: FiligreeDB) -> None:
        """An entity_id that no longer resolves on the Clarion side (a
        rename, a deletion, a missing scan) becomes a "stale reference"
        from Clarion's perspective — but Filigree's read path returns the
        row unchanged. The downstream consumer (Clarion's issues_for)
        classifies it as ``not_found`` per ADR-029 §"Decision 3" without
        any participation from Filigree.
        """
        issue = db.create_issue("Stale-anchor issue", priority=2)
        # Attach an entity_id that no real Clarion install would ever
        # resolve — Filigree must not care.
        db.add_entity_association(
            issue.id,
            "py:func:long-since-deleted::very-much-removed",
            content_hash="abandoned-hash",
        )
        rows = db.list_entity_associations(issue.id)
        assert len(rows) == 1
        assert rows[0]["clarion_entity_id"] == "py:func:long-since-deleted::very-much-removed"
        assert rows[0]["content_hash_at_attach"] == "abandoned-hash"
