"""§2.3 parity — SQL lease-expiry filter matches the Python fallback.

Confirms that the new ``get_stale_claims`` query (with the modern
``claim_expires_at`` check pushed into the WHERE clause) returns the
same row set as the pre-§2.3 Python-only filter for the modern path, and
that legacy rows (``claim_expires_at IS NULL``) still pick up the Python
fallback against heartbeat / claimed / updated timestamps.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from filigree.core import FiligreeDB


def _python_reference_filter(
    rows: list[dict],
    *,
    now: datetime,
    stale_after_hours: int,
    expires_within_hours: int | None,
) -> list[str]:
    """The pre-§2.3 Python filter, kept verbatim as the reference oracle."""
    from filigree.db_issues import _parse_issue_timestamp

    cutoff = now - timedelta(hours=stale_after_hours)
    expiry_cutoff = now + timedelta(hours=expires_within_hours) if expires_within_hours is not None else None
    stale_ids: list[str] = []
    for row in rows:
        expires_at = _parse_issue_timestamp(row["claim_expires_at"])
        if expires_at is not None:
            if expires_at <= now or (expiry_cutoff is not None and expires_at <= expiry_cutoff):
                stale_ids.append(row["id"])
            continue
        basis = (
            _parse_issue_timestamp(row["last_heartbeat_at"])
            or _parse_issue_timestamp(row["claimed_at"])
            or _parse_issue_timestamp(row["updated_at"])
        )
        if basis is None or basis <= cutoff:
            stale_ids.append(row["id"])
    return stale_ids


@pytest.mark.parametrize("expires_within_hours", [None, 2, 24])
def test_get_stale_claims_sql_filter_matches_python_filter(db: FiligreeDB, expires_within_hours: int | None) -> None:
    """Across a mix of expired / fresh / NULL / near-expiry rows, the new
    SQL filter must return the same set as the pre-§2.3 Python filter."""
    # Build a representative population.
    now = datetime.now(UTC)

    cases = {
        "modern_expired": (now - timedelta(hours=2)).isoformat(),
        "modern_near_expiry": (now + timedelta(hours=1)).isoformat(),
        "modern_far_future": (now + timedelta(hours=72)).isoformat(),
        # legacy: claim_expires_at IS NULL, basis = heartbeat
        "legacy_stale_heartbeat": None,
        "legacy_fresh_heartbeat": None,
        # legacy: claim_expires_at IS NULL, no heartbeat — falls back to claimed_at
        "legacy_stale_claimed": None,
    }
    ids: dict[str, str] = {}
    for name in cases:
        issue = db.create_issue(name, priority=0)
        ids[name] = issue.id
        db.claim_issue(issue.id, assignee=f"agent-{name}")

    old = (now - timedelta(days=5)).isoformat()
    fresh = (now - timedelta(minutes=10)).isoformat()

    # Modern paths
    db.conn.execute(
        "UPDATE issues SET claim_expires_at = ?, last_heartbeat_at = ? WHERE id = ?",
        (cases["modern_expired"], cases["modern_expired"], ids["modern_expired"]),
    )
    db.conn.execute(
        "UPDATE issues SET claim_expires_at = ?, last_heartbeat_at = ? WHERE id = ?",
        (cases["modern_near_expiry"], cases["modern_near_expiry"], ids["modern_near_expiry"]),
    )
    db.conn.execute(
        "UPDATE issues SET claim_expires_at = ?, last_heartbeat_at = ? WHERE id = ?",
        (cases["modern_far_future"], cases["modern_far_future"], ids["modern_far_future"]),
    )

    # Legacy paths (claim_expires_at NULL)
    db.conn.execute(
        "UPDATE issues SET claim_expires_at = NULL, last_heartbeat_at = ?, claimed_at = ?, updated_at = ? WHERE id = ?",
        (old, old, old, ids["legacy_stale_heartbeat"]),
    )
    db.conn.execute(
        "UPDATE issues SET claim_expires_at = NULL, last_heartbeat_at = ?, claimed_at = ?, updated_at = ? WHERE id = ?",
        (fresh, fresh, fresh, ids["legacy_fresh_heartbeat"]),
    )
    db.conn.execute(
        "UPDATE issues SET claim_expires_at = NULL, last_heartbeat_at = NULL, claimed_at = ?, updated_at = ? WHERE id = ?",
        (old, old, ids["legacy_stale_claimed"]),
    )
    db.conn.commit()

    # Pull the same row set the legacy-only oracle would see, to compute the
    # reference answer against the SAME population (skips done-category etc.).
    pred_sql, pred_params = db._category_predicate_sql("done", type_col="i.type", status_col="i.status")
    rows = db.conn.execute(
        f"SELECT i.id, i.claim_expires_at, i.last_heartbeat_at, i.claimed_at, i.updated_at FROM issues i WHERE COALESCE(i.assignee, '') != '' AND NOT ({pred_sql}) ORDER BY i.priority ASC, i.created_at ASC, i.id ASC",  # noqa: S608, E501 — pred_sql comes from _category_predicate_sql
        pred_params,
    ).fetchall()
    expected = _python_reference_filter(
        [dict(r) for r in rows],
        now=now,
        stale_after_hours=48,
        expires_within_hours=expires_within_hours,
    )

    actual = [issue.id for issue in db.get_stale_claims(expires_within_hours=expires_within_hours)]
    assert actual == expected, (
        f"SQL filter / Python reference mismatch for expires_within_hours={expires_within_hours}.\n"
        f"  actual:   {actual}\n"
        f"  expected: {expected}\n"
    )


def test_get_stale_claims_legacy_null_rows_use_python_fallback(db: FiligreeDB) -> None:
    """An assigned row with ``claim_expires_at IS NULL`` and an old
    ``last_heartbeat_at`` is treated as stale via the Python fallback."""
    issue = db.create_issue("legacy", priority=0)
    db.claim_issue(issue.id, assignee="legacy-agent")
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    db.conn.execute(
        "UPDATE issues SET claim_expires_at = NULL, last_heartbeat_at = ?, claimed_at = ?, updated_at = ? WHERE id = ?",
        (old, old, old, issue.id),
    )
    db.conn.commit()

    stale_ids = [i.id for i in db.get_stale_claims(stale_after_hours=48)]
    assert issue.id in stale_ids


def test_get_stale_claims_legacy_null_row_with_fresh_heartbeat_is_not_stale(db: FiligreeDB) -> None:
    """An assigned row with ``claim_expires_at IS NULL`` but a recent
    heartbeat is NOT stale — Python fallback still applies."""
    issue = db.create_issue("legacy-fresh", priority=0)
    db.claim_issue(issue.id, assignee="legacy-agent-fresh")
    fresh = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    db.conn.execute(
        "UPDATE issues SET claim_expires_at = NULL, last_heartbeat_at = ?, claimed_at = ?, updated_at = ? WHERE id = ?",
        (fresh, fresh, fresh, issue.id),
    )
    db.conn.commit()

    stale_ids = [i.id for i in db.get_stale_claims(stale_after_hours=48)]
    assert issue.id not in stale_ids


def test_get_stale_claims_malformed_expiry_uses_python_fallback(db: FiligreeDB) -> None:
    """Malformed non-NULL claim expiry text must not hide rows from fallback."""
    now = datetime.now(UTC)
    old = (now - timedelta(days=10)).isoformat()
    fresh = (now - timedelta(minutes=10)).isoformat()

    stale_issue = db.create_issue("malformed expiry stale fallback", priority=0)
    fresh_issue = db.create_issue("malformed expiry fresh fallback", priority=0)
    db.claim_issue(stale_issue.id, assignee="stale-agent")
    db.claim_issue(fresh_issue.id, assignee="fresh-agent")
    db.conn.execute(
        "UPDATE issues SET claim_expires_at = ?, last_heartbeat_at = ?, claimed_at = ?, updated_at = ? WHERE id = ?",
        ("not-a-date", old, old, old, stale_issue.id),
    )
    db.conn.execute(
        "UPDATE issues SET claim_expires_at = ?, last_heartbeat_at = ?, claimed_at = ?, updated_at = ? WHERE id = ?",
        ("", fresh, fresh, fresh, fresh_issue.id),
    )
    db.conn.commit()

    stale_ids = [i.id for i in db.get_stale_claims(stale_after_hours=48)]

    assert stale_issue.id in stale_ids
    assert fresh_issue.id not in stale_ids
