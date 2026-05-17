"""Entity-association CRUD (ADR-029, Clarion B.7 / WP9-A).

Binds Filigree issues to Clarion entities via opaque string IDs. The
``clarion_entity_id`` carries Clarion's three-segment grammar
(``{plugin_id}:{kind}:{canonical_qualified_name}``, per Clarion's
ADR-003) but Filigree never parses it — the federation enrich-only
rule (``loom.md`` §5) requires that Clarion's entity-ID grammar remain
Clarion's contract with itself. Filigree stores the ID as a string and
hands ``content_hash_at_attach`` back at query time so the consumer
(Clarion's ``issues_for`` MCP tool, lands separately in B.6) can
compute drift.

Three operations form the surface:

- :meth:`EntityAssociationsMixin.add_entity_association` — idempotent
  on ``(issue_id, entity_id)``; re-attach refreshes
  ``content_hash_at_attach`` and ``attached_at`` while preserving the
  original ``attached_by``.
- :meth:`EntityAssociationsMixin.remove_entity_association` — composite
  key, not a surrogate.
- :meth:`EntityAssociationsMixin.list_entity_associations` — returns
  raw rows; drift detection is the consumer's job.
"""

from __future__ import annotations

from typing import TypedDict

from filigree.db_base import DBMixinProtocol, _now_iso


class EntityAssociationRow(TypedDict):
    """One row of the entity_associations table."""

    issue_id: str
    clarion_entity_id: str
    content_hash_at_attach: str
    attached_at: str
    attached_by: str


class EntityAssociationsMixin(DBMixinProtocol):
    """CRUD for the ``entity_associations`` table (ADR-029).

    Composed into :class:`filigree.core.FiligreeDB` via MRO. The mixin
    deliberately knows nothing about Clarion's entity-ID grammar; every
    method treats ``entity_id`` as an opaque string.
    """

    def add_entity_association(
        self,
        issue_id: str,
        entity_id: str,
        content_hash: str,
        *,
        actor: str = "",
    ) -> EntityAssociationRow:
        """Attach a Clarion entity to a Filigree issue (or refresh an existing
        attachment).

        Idempotent on ``(issue_id, entity_id)``. Re-attaching updates
        ``content_hash_at_attach`` and ``attached_at``; the original
        ``attached_by`` is preserved so the audit signal "who first
        bound this issue to this entity" survives drift refreshes.

        Args:
            issue_id: Filigree issue ID. Must exist; verified by FK.
            entity_id: Clarion entity ID (opaque to Filigree).
            content_hash: Clarion's current ``entities.content_hash`` for
                the entity, snapshotted at attach time. Filigree stores
                this verbatim and never interprets it.
            actor: Identity recorded as ``attached_by`` on first attach.
                Defaults to empty string per the existing actor pattern.

        Returns:
            The resulting row as an :class:`EntityAssociationRow`.

        Raises:
            ValueError: ``issue_id`` doesn't exist, or arguments are
                blank where they must not be.
        """
        self._check_id_prefix(issue_id)
        if not entity_id or not entity_id.strip():
            msg = "entity_id must not be blank"
            raise ValueError(msg)
        if not content_hash or not content_hash.strip():
            msg = "content_hash must not be blank"
            raise ValueError(msg)
        # Validate issue exists (FK would catch this too, but the SQLite
        # error is less informative than a typed ValueError).
        row = self.conn.execute("SELECT 1 FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None:
            msg = f'Issue not found: "{issue_id}". Verify the issue exists before attaching an entity.'
            raise ValueError(msg)

        now = _now_iso()
        try:
            # Idempotent: insert-or-update on the composite PK. The
            # excluded.* alias is the row we tried to insert; we
            # deliberately do NOT update attached_by, preserving the
            # original attribution.
            self.conn.execute(
                """
                INSERT INTO entity_associations
                    (issue_id, clarion_entity_id, content_hash_at_attach, attached_at, attached_by)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(issue_id, clarion_entity_id) DO UPDATE SET
                    content_hash_at_attach = excluded.content_hash_at_attach,
                    attached_at = excluded.attached_at
                """,
                (issue_id, entity_id, content_hash, now, actor),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        # Re-read the row — necessary because re-attach preserves the
        # original attached_by, which differs from the value we just
        # passed in for an existing row.
        stored = self.conn.execute(
            """
            SELECT issue_id, clarion_entity_id, content_hash_at_attach, attached_at, attached_by
            FROM entity_associations
            WHERE issue_id = ? AND clarion_entity_id = ?
            """,
            (issue_id, entity_id),
        ).fetchone()
        if stored is None:
            # Unreachable under normal operation — we just committed the
            # row. Surfacing as RuntimeError makes any future corruption
            # path visible at the call site rather than letting a None
            # propagate.
            msg = f"entity_associations row for ({issue_id!r}, {entity_id!r}) vanished between insert and read"
            raise RuntimeError(msg)
        return EntityAssociationRow(
            issue_id=stored["issue_id"],
            clarion_entity_id=stored["clarion_entity_id"],
            content_hash_at_attach=stored["content_hash_at_attach"],
            attached_at=stored["attached_at"],
            attached_by=stored["attached_by"],
        )

    def remove_entity_association(self, issue_id: str, entity_id: str) -> bool:
        """Remove the association identified by the composite key.

        Returns:
            ``True`` if a row was deleted, ``False`` if the association
            did not exist (idempotent — no-op on missing).
        """
        self._check_id_prefix(issue_id)
        if not entity_id or not entity_id.strip():
            msg = "entity_id must not be blank"
            raise ValueError(msg)
        try:
            cursor = self.conn.execute(
                "DELETE FROM entity_associations WHERE issue_id = ? AND clarion_entity_id = ?",
                (issue_id, entity_id),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return cursor.rowcount > 0

    def list_entity_associations(self, issue_id: str) -> list[EntityAssociationRow]:
        """Return all entity associations for an issue.

        Returns raw rows in attach-time order. Drift detection is the
        caller's job — Filigree does not compute or surface
        ``drift_warning`` here per ADR-029 §"Decision 3"; that's the
        consumer's (Clarion's ``issues_for``) responsibility after
        fetching the rows.
        """
        self._check_id_prefix(issue_id)
        rows = self.conn.execute(
            """
            SELECT issue_id, clarion_entity_id, content_hash_at_attach, attached_at, attached_by
            FROM entity_associations
            WHERE issue_id = ?
            ORDER BY attached_at ASC, clarion_entity_id ASC
            """,
            (issue_id,),
        ).fetchall()
        return [
            EntityAssociationRow(
                issue_id=r["issue_id"],
                clarion_entity_id=r["clarion_entity_id"],
                content_hash_at_attach=r["content_hash_at_attach"],
                attached_at=r["attached_at"],
                attached_by=r["attached_by"],
            )
            for r in rows
        ]

    def list_associations_by_entity(self, entity_id: str) -> list[EntityAssociationRow]:
        """Return all issue bindings for a given Clarion entity.

        The reverse of :meth:`list_entity_associations`: given an
        opaque Clarion entity ID, return every Filigree issue currently
        bound to it. This is the surface Clarion's ``issues_for`` MCP
        tool (B.6) calls to answer "what issues are about this code I'm
        reading?" in one round trip.

        Uses the ``idx_entity_associations_entity`` index. Isolation
        between projects is by DB file — every row in this query
        already belongs to the project hosting this database.

        Raw rows are returned in attach-time order; drift detection is
        the consumer's job per ADR-029 §"Decision 3".
        """
        if not entity_id or not entity_id.strip():
            msg = "entity_id must not be blank"
            raise ValueError(msg)
        rows = self.conn.execute(
            """
            SELECT issue_id, clarion_entity_id, content_hash_at_attach, attached_at, attached_by
            FROM entity_associations
            WHERE clarion_entity_id = ?
            ORDER BY attached_at ASC, issue_id ASC
            """,
            (entity_id,),
        ).fetchall()
        return [
            EntityAssociationRow(
                issue_id=r["issue_id"],
                clarion_entity_id=r["clarion_entity_id"],
                content_hash_at_attach=r["content_hash_at_attach"],
                attached_at=r["attached_at"],
                attached_by=r["attached_by"],
            )
            for r in rows
        ]
