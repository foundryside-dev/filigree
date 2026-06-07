"""Finding-to-issue cascade orchestration.

This module keeps cross-domain policy for scan findings and issues out of the
file persistence mixin. The DB layer still owns the transactional primitives;
the service owns when best-effort cascade failures become reconciliation debt.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from filigree.db_base import _now_iso
from filigree.models import Issue
from filigree.types.core import StatusCategory

if TYPE_CHECKING:
    from filigree.governance import GateDecision

logger = logging.getLogger(__name__)

FINDING_CASCADE_MARKER = "finding-cascade"
RECONCILIATION_DEBT_ACTOR = "filigree:reconciliation"
RECONCILIATION_DEBT_PREFIX = "[reconciliation-debt]"


class FindingIssueCascadeStore(Protocol):
    @property
    def conn(self) -> sqlite3.Connection: ...

    def get_issue(self, issue_id: str) -> Issue: ...
    def _resolve_status_category(self, issue_type: str, status: str) -> StatusCategory: ...
    def _close_issue_for_fixed_finding_tx(self, finding_id: str, issue_id: str) -> bool: ...
    # Lets the store satisfy ``governance._AssocReader`` so the cascade can
    # consult the Legis closure gate (Design A) without importing FiligreeDB.
    # ``Any`` issue_id mirrors ``governance._AssocReader`` and avoids a
    # str-vs-IssueId contravariance mismatch with the concrete mixin.
    def list_entity_associations(self, issue_id: Any) -> list[Any]: ...

    def close_issue(
        self,
        issue_id: str,
        *,
        reason: str,
        actor: str,
        force: bool = False,
        _skip_begin: bool = False,
    ) -> Issue: ...

    def reopen_issue(self, issue_id: str, *, actor: str) -> Issue: ...


def record_reconciliation_debt_comment(
    conn: sqlite3.Connection,
    issue_id: str,
    text: str,
    *,
    actor: str = RECONCILIATION_DEBT_ACTOR,
) -> None:
    try:
        # ADR-012: reconciliation-debt is a system-authored cascade write with no
        # transport proof (bare conn, system actor). verified_author is left NULL
        # intentionally — this is a NEW record, not a restored one.
        full_text = f"{RECONCILIATION_DEBT_PREFIX} {text}"
        # Design A re-evaluates a blocked governed issue on every ingest/sweep;
        # skip when an identical (issue_id, author, text) debt already exists so
        # the debt list (B5) is not drowned in duplicates. A *different* reason
        # on the same issue still records.
        existing = conn.execute(
            "SELECT 1 FROM comments WHERE issue_id = ? AND author = ? AND text = ? LIMIT 1",
            (issue_id, actor, full_text),
        ).fetchone()
        if existing is not None:
            return
        conn.execute(
            "INSERT INTO comments (issue_id, author, text, created_at) VALUES (?, ?, ?, ?)",
            (issue_id, actor, full_text, _now_iso()),
        )
        conn.commit()
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        logger.warning("Failed to persist reconciliation debt comment for issue %s", issue_id, exc_info=True)


@dataclass(frozen=True)
class FindingIssueCascadeService:
    store: FindingIssueCascadeStore

    def close_fixed_finding(self, finding_id: str, issue_id: str, *, warnings: list[str]) -> bool:
        """Best-effort close of an issue whose linked finding just went fixed.

        Governed issues (DECISION 1A) are closed only if the Legis closure gate
        allows; a blocked / unavailable / stale / integrity verdict fails closed
        and is recorded as reconciliation debt (Design A). The gate makes no
        network call for ungoverned issues, for a drifted (stale) sign-off, or
        when ``LEGIS_URL`` is unset.
        """
        # Function-local import: the data layer must not import the (network-
        # touching) governance module at module scope.
        from filigree import governance

        decision = governance.evaluate_closure_gate(self.store, issue_id)
        return self._apply_close_decision(finding_id, issue_id, decision, warnings=warnings)

    def close_resolved_findings(self, candidates: list[tuple[str, str]], *, warnings: list[str]) -> list[str]:
        """Gate-and-close a batch of ``(finding_id, issue_id)``; return the ids
        that closed.

        Suppresses the per-issue Legis *network call* after the first
        ``UNAVAILABLE`` verdict so a down/slow Legis costs at most one timeout
        per batch. The suppression is threaded into ``evaluate_closure_gate``
        (``legis_known_down``) rather than fabricated here, so the gate's cheap
        local checks still run for every candidate: an ungoverned or
        governance-off issue later in the batch still PROCEEDs (DECISION 1A —
        ungoverned closes never touch Legis), and a stale binding still reports
        ``STALE``. Only a governed, non-stale issue fails closed as
        ``UNAVAILABLE`` without a further network call. ``INTEGRITY_FAILURE`` is
        a per-issue ledger-tamper verdict, not a connectivity problem, so it
        never sets ``legis_down``.
        """
        from filigree import governance
        from filigree.governance import GateOutcome

        closed: list[str] = []
        legis_down = False
        for finding_id, issue_id in candidates:
            decision = governance.evaluate_closure_gate(self.store, issue_id, legis_known_down=legis_down)
            if decision.outcome is GateOutcome.UNAVAILABLE:
                legis_down = True
            if self._apply_close_decision(finding_id, issue_id, decision, warnings=warnings):
                closed.append(issue_id)
        return closed

    def _apply_close_decision(self, finding_id: str, issue_id: str, decision: GateDecision, *, warnings: list[str]) -> bool:
        """Apply a closure-gate *decision* to one resolved finding's issue.

        Shared by the single (`close_fixed_finding`) and batch
        (`close_resolved_findings`) paths: a non-PROCEED verdict fails closed
        and records reconciliation debt; a PROCEED runs the close transaction
        (whose own failure also becomes debt).
        """
        if not decision.allowed:
            warning = f"governed issue {issue_id} not auto-closed by cascade: {decision.reason}"
            warnings.append(warning)
            record_reconciliation_debt_comment(
                self.store.conn,
                issue_id,
                f"Finding {finding_id} was marked fixed, but the linked governed issue "
                f"was not auto-closed ({decision.outcome.value}): {decision.reason}",
            )
            return False
        try:
            return self.store._close_issue_for_fixed_finding_tx(finding_id, issue_id)
        except (KeyError, ValueError, sqlite3.Error) as exc:
            warning = f"cascade close of issue {issue_id} failed: {exc}"
            warnings.append(warning)
            record_reconciliation_debt_comment(
                self.store.conn,
                issue_id,
                f"Finding {finding_id} was marked fixed, but the linked issue could not be cascade-closed: {warning}",
            )
            return False

    def issue_last_closed_by_cascade(self, issue: Issue) -> bool:
        """True iff the most recent transition into a done state was the cascade."""
        rows = self.store.conn.execute(
            "SELECT actor, new_value FROM events WHERE issue_id = ? AND event_type = 'status_changed' ORDER BY created_at DESC, id DESC",
            (issue.id,),
        ).fetchall()
        for row in rows:
            new_status = row["new_value"]
            if not new_status:
                continue
            try:
                category = self.store._resolve_status_category(issue.type, new_status)
            except (KeyError, ValueError):
                continue
            if category == "done":
                return bool(row["actor"] == FINDING_CASCADE_MARKER)
        return False

    def reopen_regressed_finding(self, issue_id: str, *, warnings: list[str]) -> bool:
        """Best-effort reopen of a cascade-closed issue whose finding regressed."""
        try:
            issue = self.store.get_issue(issue_id)
        except KeyError:
            return False
        if self.store._resolve_status_category(issue.type, issue.status) != "done":
            return False
        if not self.issue_last_closed_by_cascade(issue):
            return False
        try:
            self.store.reopen_issue(issue_id, actor=FINDING_CASCADE_MARKER)
        except (KeyError, ValueError, sqlite3.Error) as exc:
            warning = f"cascade reopen of issue {issue_id} failed: {exc}"
            warnings.append(warning)
            record_reconciliation_debt_comment(
                self.store.conn,
                issue_id,
                f"Issue {issue_id} was cascade-closed, but its linked finding regressed and the issue could not be reopened: {warning}",
            )
            return False
        return True
