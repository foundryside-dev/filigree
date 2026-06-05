"""Finding-to-issue cascade orchestration.

This module keeps cross-domain policy for scan findings and issues out of the
file persistence mixin. The DB layer still owns the transactional primitives;
the service owns when best-effort cascade failures become reconciliation debt.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Protocol

from filigree.db_base import _now_iso
from filigree.models import Issue
from filigree.types.core import StatusCategory

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
        conn.execute(
            "INSERT INTO comments (issue_id, author, text, created_at) VALUES (?, ?, ?, ?)",
            (issue_id, actor, f"{RECONCILIATION_DEBT_PREFIX} {text}", _now_iso()),
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
        """Best-effort close of an issue whose linked finding just went fixed."""
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
