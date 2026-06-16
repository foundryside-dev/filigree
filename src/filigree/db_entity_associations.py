"""Entity-association CRUD (ADR-029, Loomweave B.7 / WP9-A).

Binds Filigree issues to opaque external entity IDs. The historical
SQLite column is named ``loomweave_entity_id`` for compatibility, but the
public projection exposes canonical ``entity_id`` and treats the value
as an opaque string. The value may be a Loomweave SEI, a legacy locator, or
another caller-owned ID; Filigree never parses or validates its grammar.

Four operations form the surface:

- :meth:`EntityAssociationsMixin.add_entity_association` — idempotent
  on ``(issue_id, entity_id)``; re-attach refreshes
  ``content_hash_at_attach`` and ``attached_at`` while preserving the
  original ``attached_by``.
- :meth:`EntityAssociationsMixin.remove_entity_association` — composite
  key, not a surrogate.
- :meth:`EntityAssociationsMixin.list_entity_associations` — returns
  raw rows; drift detection is the consumer's job.
- :meth:`EntityAssociationsMixin.list_associations_by_entity` — reverse
  lookup from opaque entity ID to every bound issue in this project.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypedDict

from filigree.db_base import DBMixinProtocol, _in_immediate_tx, _now_iso, _retry_busy
from filigree.types.core import (
    ContentHash,
    ISOTimestamp,
    IssueId,
    LoomweaveEntityId,
    make_content_hash,
    make_issue_id,
    make_loomweave_entity_id,
)


class EntityAssociationRow(TypedDict):
    """One row of the entity_associations table."""

    issue_id: IssueId
    entity_id: LoomweaveEntityId
    loomweave_entity_id: LoomweaveEntityId
    entity_kind: str
    content_hash_at_attach: ContentHash
    attached_at: ISOTimestamp
    attached_by: str
    migration_orphaned_at: ISOTimestamp | None
    # ``orphan_status`` is two-state by design, per ADR-017's two-axis model.
    # Filigree owns only the *content* axis (``freshness_status``); the
    # *identity* axis (ALIVE/ORPHANED) is Loomweave's via ``resolve_sei``.
    # ``"orphaned"`` reports Filigree's own ``migration_orphaned_at`` marker;
    # the non-orphaned value is ``"unknown"`` — an explicit deferral, NOT
    # "active"/"healthy", because Filigree must never assert identity-axis
    # liveness it does not own. See ``_row_to_entity_association``.
    orphan_status: str
    freshness_status: str
    # v25 (B1): opaque Legis governed-sign-off binding fields. ``signature`` is
    # an HMAC over ``{issue_id, entity_id, content_hash, signoff_seq}`` that
    # Filigree stores verbatim and NEVER verifies (it has no key); ``signoff_seq``
    # is Legis's sign-off sequence. Both NULL when Legis sends no key / for
    # non-governed bindings. Echoed on read, treated like content_hash_at_attach.
    signature: str | None
    signoff_seq: int | None
    # v27: the content_hash the current ``signature`` was cut over (the HMAC binds
    # content_hash). Set = content_hash on any write carrying a signature, PRESERVED
    # across a signatureless re-attach. ``signed_content_hash != content_hash_at_attach``
    # means the sign-off has drifted — the gate fails closed (GateOutcome.STALE).
    signed_content_hash: ContentHash | None


class GovernedAssociationRemovalError(ValueError):
    """Refused: a caller tried to delete a Legis-signed (governed) binding.

    Removing the only signed association of a governed issue is a non-Legis
    governed->ungoverned downgrade — the closure gate (governance.py DECISION
    1A: governed = >=1 non-null ``signature``) then short-circuits to PROCEED
    with no Legis call. This is the removal-vector twin of the v27 write-clobber
    fix, refused structurally at the data layer.

    A ``ValueError`` subclass so the existing untrusted-surface handlers (the
    MCP and HTTP entity-association remove routes both ``except ValueError``)
    render it as a refusal without bespoke wiring.
    """


def _normalise_optional_entity_kind(entity_kind: str | None) -> str:
    if entity_kind is None:
        return ""
    if not isinstance(entity_kind, str):
        msg = "entity_kind must be a string"
        raise TypeError(msg)
    return entity_kind.strip()


def _normalise_optional_signature(signature: str | None) -> str | None:
    """Collapse a blank Legis ``signature`` to ``None`` (data-layer, covers every
    write path: route, MCP, import, promote).

    DECISION 1A defines governed-ness as a *non-null* signature, but a blank
    string is non-null-yet-falsy and would masquerade as ungoverned. Mapping
    ""/whitespace -> None here means the column is strictly {real-signature |
    NULL} no matter which surface wrote it. A real signature is preserved
    verbatim (an HMAC is exact — never stripped).
    """
    if signature is None:
        return None
    if not isinstance(signature, str):
        msg = "signature must be a string"
        raise TypeError(msg)
    return signature if signature.strip() else None


def _freshness_status(content_hash_at_attach: str, current_content_hash: str | None) -> str:
    if current_content_hash is None:
        return "unknown"
    return "fresh" if current_content_hash == content_hash_at_attach else "stale"


def _row_to_entity_association(r: Mapping[str, Any], *, current_content_hash: str | None = None) -> EntityAssociationRow:
    entity_id = LoomweaveEntityId(r["loomweave_entity_id"])
    migration_orphaned_at = r["migration_orphaned_at"]
    content_hash_at_attach = ContentHash(r["content_hash_at_attach"])
    return EntityAssociationRow(
        issue_id=IssueId(r["issue_id"]),
        entity_id=entity_id,
        loomweave_entity_id=entity_id,
        entity_kind=r["entity_kind"],
        content_hash_at_attach=content_hash_at_attach,
        attached_at=ISOTimestamp(r["attached_at"]),
        attached_by=r["attached_by"],
        migration_orphaned_at=ISOTimestamp(migration_orphaned_at) if migration_orphaned_at else None,
        # "unknown" (not "active") for the non-orphaned case is deliberate —
        # see the ADR-017 note on ``EntityAssociationRow.orphan_status``.
        orphan_status="orphaned" if migration_orphaned_at else "unknown",
        freshness_status=_freshness_status(str(content_hash_at_attach), current_content_hash),
        signature=r["signature"],
        signoff_seq=r["signoff_seq"],
        signed_content_hash=ContentHash(r["signed_content_hash"]) if r["signed_content_hash"] else None,
    )


class EntityAssociationsMixin(DBMixinProtocol):
    """CRUD for the ``entity_associations`` table (ADR-029).

    Composed into :class:`filigree.core.FiligreeDB` via MRO. The mixin
    deliberately knows nothing about Loomweave's entity-ID grammar; every
    method treats ``entity_id`` as an opaque string.
    """

    @_retry_busy()
    @_in_immediate_tx("add_entity_association")
    def add_entity_association(
        self,
        issue_id: IssueId,
        entity_id: LoomweaveEntityId,
        content_hash: ContentHash,
        *,
        actor: str = "",
        entity_kind: str | None = None,
        signature: str | None = None,
        signoff_seq: int | None = None,
        _skip_begin: bool = False,
    ) -> EntityAssociationRow:
        """Attach a Loomweave entity to a Filigree issue (or refresh an existing
        attachment).

        Idempotent on ``(issue_id, entity_id)``. Re-attaching updates
        ``content_hash_at_attach`` and ``attached_at``; the original
        ``attached_by`` is preserved so the audit signal "who first
        bound this issue to this entity" survives drift refreshes. First
        attach records ``entity_association_added``; re-attach records
        ``entity_association_refreshed`` with the prior and replacement
        content hashes.

        Args:
            issue_id: Filigree issue ID. Must exist; verified by FK.
            entity_id: Loomweave entity ID (opaque to Filigree).
            content_hash: Loomweave's current ``entities.content_hash`` for
                the entity, snapshotted at attach time. Filigree stores
                this verbatim and never interprets it.
            actor: Identity recorded as ``attached_by`` on first attach.
                Defaults to empty string per the existing actor pattern.

        Returns:
            The resulting row as an :class:`EntityAssociationRow`.

        Raises:
            KeyError: ``issue_id`` doesn't exist.
            ValueError: arguments are blank or invalid where they must not be.
        """
        issue_id = make_issue_id(issue_id)
        entity_id = make_loomweave_entity_id(entity_id)
        content_hash = make_content_hash(content_hash)
        entity_kind = _normalise_optional_entity_kind(entity_kind)
        signature = _normalise_optional_signature(signature)
        # The signature binds to this content snapshot (the HMAC covers
        # content_hash); record what it was so the gate can detect later drift.
        # NULL when this write carries no signature, so an ungoverned row keeps a
        # NULL signed_content_hash (consistent with the v27 backfill).
        signed_content_hash = str(content_hash) if signature is not None else None
        self._check_id_prefix(issue_id)
        # Validate issue exists (FK would catch this too, but the SQLite
        # error is less informative than a typed ValueError).
        row = self.conn.execute("SELECT 1 FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None:
            msg = f"Issue not found: {issue_id}"
            raise KeyError(msg)

        existing = self.conn.execute(
            """
            SELECT content_hash_at_attach
            FROM entity_associations
            WHERE issue_id = ? AND loomweave_entity_id = ?
            """,
            (issue_id, entity_id),
        ).fetchone()

        now = _now_iso()
        # Idempotent: insert-or-update on the composite PK. The
        # excluded.* alias is the row we tried to insert; we
        # deliberately do NOT update attached_by, preserving the
        # original attribution.
        #
        # Governance stickiness (v27, PR #52 fix): the three Legis governed
        # sign-off columns (signature, signoff_seq, signed_content_hash) are
        # updated ONLY when *this* write carries a signature. A signatureless
        # re-attach (a routine drift refresh by an agent, who structurally can
        # never sign — only Legis can) therefore PRESERVES the existing sign-off
        # instead of clobbering it to NULL. content_hash_at_attach still advances
        # unconditionally, so a drifted-but-still-signed binding ends up with
        # signed_content_hash != content_hash_at_attach, which the closure gate
        # reads as STALE and fails closed. Only Legis (a signed write via the
        # HTTP binding route) moves a binding between governed states — never an
        # incidental work-state refresh.
        self.conn.execute(
            """
            INSERT INTO entity_associations
                (issue_id, loomweave_entity_id, content_hash_at_attach, attached_at, attached_by,
                 entity_kind, signature, signoff_seq, signed_content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(issue_id, loomweave_entity_id) DO UPDATE SET
                content_hash_at_attach = excluded.content_hash_at_attach,
                attached_at = excluded.attached_at,
                entity_kind = CASE
                    WHEN excluded.entity_kind <> '' THEN excluded.entity_kind
                    ELSE entity_associations.entity_kind
                END,
                signature = CASE
                    WHEN excluded.signature IS NOT NULL THEN excluded.signature
                    ELSE entity_associations.signature
                END,
                signoff_seq = CASE
                    WHEN excluded.signature IS NOT NULL THEN excluded.signoff_seq
                    ELSE entity_associations.signoff_seq
                END,
                signed_content_hash = CASE
                    WHEN excluded.signature IS NOT NULL THEN excluded.signed_content_hash
                    ELSE entity_associations.signed_content_hash
                END
            """,
            (issue_id, entity_id, content_hash, now, actor, entity_kind, signature, signoff_seq, signed_content_hash),
        )

        # Re-read the row — necessary because re-attach preserves the
        # original attached_by, which differs from the value we just
        # passed in for an existing row.
        stored = self.conn.execute(
            """
            SELECT issue_id, loomweave_entity_id, entity_kind, content_hash_at_attach,
                   attached_at, attached_by, migration_orphaned_at, signature, signoff_seq,
                   signed_content_hash
            FROM entity_associations
            WHERE issue_id = ? AND loomweave_entity_id = ?
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
        if existing is None:
            self._record_event(
                str(issue_id),
                "entity_association_added",
                actor=actor,
                new_value=str(entity_id),
                comment=str(content_hash),
            )
        else:
            self._record_event(
                str(issue_id),
                "entity_association_refreshed",
                actor=actor,
                old_value=existing["content_hash_at_attach"],
                new_value=str(content_hash),
                comment=str(entity_id),
            )
        return _row_to_entity_association(stored)

    @_retry_busy()
    @_in_immediate_tx("remove_entity_association")
    def remove_entity_association(
        self,
        issue_id: IssueId,
        entity_id: LoomweaveEntityId,
        *,
        actor: str = "",
    ) -> bool:
        """Remove the association identified by the composite key.

        Refuses to delete a Legis-signed (governed) binding: removing the signed
        row is a governed->ungoverned downgrade that would let the closure gate
        (governance.py DECISION 1A: governed = >=1 non-null ``signature``) wave a
        governed close through with no Legis call — the removal-vector twin of the
        v27 write-clobber fix. No agent surface can sign (only Legis holds the HMAC
        key) and Filigree cannot verify a wire-supplied signature, so there is no
        safe signatureless removal of a signed row; a genuine privileged detach
        goes through an explicit admin path (``issue_delete``'s ``ON DELETE
        CASCADE`` / the owner-gated ``sei-backfill`` merge), never this method.

        Returns:
            ``True`` if a row was deleted, ``False`` if the association
            did not exist (idempotent — no-op on missing).

        Raises:
            GovernedAssociationRemovalError: the target row carries a Legis
                ``signature`` (the issue is governed by it).
        """
        issue_id = make_issue_id(issue_id)
        entity_id = make_loomweave_entity_id(entity_id)
        self._check_id_prefix(issue_id)
        # Governed-binding guard. Mirror the gate's own predicate exactly
        # (``signature is not None``, governance.py:127) so refuse-on-remove and
        # governed-on-close can never disagree; a blank signature is already
        # NULL-normalised on write, so the column is strictly {real-signature |
        # NULL}. Keyed on the durable signature, NOT on whether Legis is currently
        # configured: unsetting LEGIS_URL must not become a way to delete the
        # sign-off. Read-only, before any mutation, so the refusal rolls back clean.
        existing = self.conn.execute(
            "SELECT signature FROM entity_associations WHERE issue_id = ? AND loomweave_entity_id = ?",
            (issue_id, entity_id),
        ).fetchone()
        if existing is not None and existing["signature"] is not None:
            msg = (
                f"Cannot remove the Legis-signed (governed) association {entity_id} from {issue_id}: "
                "deleting a signed binding would silently downgrade the issue to ungoverned. "
                "Only Legis can sign or release a governed binding."
            )
            raise GovernedAssociationRemovalError(msg)
        cursor = self.conn.execute(
            "DELETE FROM entity_associations WHERE issue_id = ? AND loomweave_entity_id = ?",
            (issue_id, entity_id),
        )
        if cursor.rowcount > 0:
            self._record_event(
                str(issue_id),
                "entity_association_removed",
                actor=actor,
                old_value=str(entity_id),
            )
        return cursor.rowcount > 0

    def list_entity_associations(self, issue_id: IssueId) -> list[EntityAssociationRow]:
        """Return all entity associations for an issue.

        Returns raw rows in attach-time order. Drift detection is the
        caller's job — Filigree does not compute or surface
        ``drift_warning`` here per ADR-029 §"Decision 3"; that's the
        consumer's (Loomweave's ``issues_for``) responsibility after
        fetching the rows.
        """
        issue_id = make_issue_id(issue_id)
        self._check_id_prefix(issue_id)
        rows = self.conn.execute(
            """
            SELECT issue_id, loomweave_entity_id, entity_kind, content_hash_at_attach,
                   attached_at, attached_by, migration_orphaned_at, signature, signoff_seq,
                   signed_content_hash
            FROM entity_associations
            WHERE issue_id = ?
            ORDER BY attached_at ASC, loomweave_entity_id ASC
            """,
            (issue_id,),
        ).fetchall()
        return [_row_to_entity_association(r) for r in rows]

    def list_associations_by_entity(
        self,
        entity_id: LoomweaveEntityId,
        *,
        current_content_hash: ContentHash | str | None = None,
    ) -> list[EntityAssociationRow]:
        """Return all issue bindings for a given Loomweave entity.

        The reverse of :meth:`list_entity_associations`: given an
        opaque Loomweave entity ID, return every Filigree issue currently
        bound to it. This is the surface Loomweave's ``issues_for`` MCP
        tool (B.6) calls to answer "what issues are about this code I'm
        reading?" in one round trip.

        Uses the ``ix_entity_assoc_entity`` index. Isolation
        between projects is by DB file — every row in this query
        already belongs to the project hosting this database.

        Raw rows are returned in attach-time order; drift detection is
        the consumer's job per ADR-029 §"Decision 3".
        """
        entity_id = make_loomweave_entity_id(entity_id)
        if current_content_hash is not None:
            current_content_hash = make_content_hash(current_content_hash)
        rows = self.conn.execute(
            """
            SELECT issue_id, loomweave_entity_id, entity_kind, content_hash_at_attach,
                   attached_at, attached_by, migration_orphaned_at, signature, signoff_seq,
                   signed_content_hash
            FROM entity_associations
            WHERE loomweave_entity_id = ?
            ORDER BY attached_at ASC, issue_id ASC
            """,
            (entity_id,),
        ).fetchall()
        current_hash = str(current_content_hash) if current_content_hash is not None else None
        return [_row_to_entity_association(r, current_content_hash=current_hash) for r in rows]
