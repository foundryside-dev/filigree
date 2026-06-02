"""Producer-side SEI conformance oracle (ADR-038 §8) — fast lane.

The shared fixture (``fixtures/sei-conformance-oracle.json``, vendored from
Clarion) defines six scenarios written from Clarion's *authority* perspective
(mint / carry / orphan / lineage). Filigree is a *producer*: it stores the SEI
opaquely and degrades when Clarion lacks the capability. Producer-side, the six
scenarios reduce to a small set of obligations this module proves against the
extended Clarion stub:

- **identity_round_trip_and_opacity** — the backfill rewrites a stored locator to
  the alive SEI in place; the value round-trips byte-for-byte, carries the
  reserved ``clarion:eid:`` prefix, and is never parsed.
- **rename / move** — once stored as an SEI the binding is stable: a re-run never
  re-points it (the SEI is carried unchanged Clarion-side; Filigree skips it on
  the prefix check). ``content_hash_at_attach`` is untouched.
- **ambiguous / delete** — an unresolvable locator is flagged ORPHAN (stamped,
  kept verbatim), never dropped; the content axis stays inspectable.
- **capability_absent** — against a pre-SEI / SEI-unsupported Clarion the backfill
  refuses cleanly (no partial writes) and the tracker keeps working on locators.

Plus the backfill-branch coverage the producer surface adds: prefix-skip /
resume, the ``invalid`` (REQ-F-02) channel, PK-collision merge, and the
historical ``deleted_issues.entity_ids`` rewrite (REQ-F-01).

The faithful "no grandfathering" gate — the same scenarios against a live
``clarion serve`` — lives in ``test_sei_oracle_live_clarion.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from filigree.core import FiligreeDB
from filigree.sei_backfill import SeiBackfillError, run_sei_backfill
from tests._fakes.clarion_http import clarion_stub

ORACLE_PATH = Path(__file__).parent / "fixtures" / "sei-conformance-oracle.json"

# Each fixture scenario id is claimed by at least one test here, asserted by
# ``test_every_oracle_scenario_is_covered`` so the suite can't silently fall out
# of sync with the shared standard.
COVERED_SCENARIOS = {
    "identity_round_trip_and_opacity",
    "rename",
    "move",
    "ambiguous",
    "delete",
    "capability_absent",
}


def _clarion_db(tmp_path: Path, base_url: str) -> FiligreeDB:
    db = FiligreeDB(
        tmp_path / "filigree.db",
        prefix="test",
        registry_backend="clarion",
        clarion_config={"base_url": base_url, "timeout_seconds": 2},
    )
    db.initialize()
    return db


def _insert_tombstone(db: FiligreeDB, issue_id: str, entity_ids: list[str]) -> None:
    """Insert a deleted-issue tombstone directly (bypasses the close→delete
    lifecycle so the test stays focused on the backfill's array rewrite)."""
    db.conn.execute(
        "INSERT INTO deleted_issues (issue_id, title, type, deleted_at, deleted_by, reason, entity_ids) "
        "VALUES (?, 't', 'task', '2026-05-01T00:00:00+00:00', 'x', '', ?)",
        (issue_id, json.dumps(entity_ids)),
    )
    db.conn.commit()


# ---------------------------------------------------------------------------
# Fixture provenance / coverage
# ---------------------------------------------------------------------------


def _clarion_oracle_source() -> Path | None:
    """Locate Clarion's canonical oracle fixture, if the repo is present."""
    candidates = []
    env = os.environ.get("CLARION_REPO")
    if env:
        candidates.append(Path(env) / "docs" / "federation" / "fixtures" / "sei-conformance-oracle.json")
    # Sibling checkout: <home>/clarion next to <home>/filigree.
    candidates.append(Path(__file__).resolve().parents[3] / "clarion" / "docs" / "federation" / "fixtures" / "sei-conformance-oracle.json")
    return next((c for c in candidates if c.exists()), None)


def test_vendored_oracle_matches_clarion_source() -> None:
    """The vendored copy must not drift from Clarion's canonical fixture."""
    source = _clarion_oracle_source()
    if source is None:
        pytest.skip("Clarion repo not found (set CLARION_REPO to enable the drift check)")
    assert json.loads(ORACLE_PATH.read_text()) == json.loads(source.read_text()), (
        "Vendored sei-conformance-oracle.json has drifted from Clarion's source; re-copy it."
    )


def test_every_oracle_scenario_is_covered() -> None:
    """Every scenario id in the fixture is claimed by a test in this module."""
    fixture = json.loads(ORACLE_PATH.read_text())
    fixture_ids = {s["id"] for s in fixture["scenarios"]}
    assert fixture_ids == COVERED_SCENARIOS


# ---------------------------------------------------------------------------
# §8.1 identity_round_trip_and_opacity
# ---------------------------------------------------------------------------


def test_identity_round_trip_and_opacity(tmp_path: Path) -> None:
    locator = "py:func:auth.tokens::issue_token"
    sei = "clarion:eid:deadbeefdeadbeefdeadbeefdeadbeef"
    with clarion_stub(sei_supported=True, sei_by_locator={locator: sei}) as (base_url, _state):
        db = _clarion_db(tmp_path, base_url)
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, locator, content_hash="sha256:body")

        report = run_sei_backfill(db, dry_run=False, actor="op")

        assert report.associations_migrated == 1
        assert report.associations_orphaned == 0
        rows = db.list_entity_associations(issue.id)
        assert len(rows) == 1
        stored = rows[0]["clarion_entity_id"]
        # Opacity + round-trip: the SEI is stored verbatim, carries the reserved
        # prefix, and is not the locator.
        assert stored == sei
        assert stored.startswith("clarion:eid:")
        assert stored != locator
        # Reverse lookup keys on the new SEI.
        assert [r["clarion_entity_id"] for r in db.list_associations_by_entity(sei)] == [sei]
        db.close()


# ---------------------------------------------------------------------------
# §8.2 / §8.3 rename + move — the producer never re-points a stable SEI
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", ["rename", "move"])
def test_stable_sei_is_never_repointed(tmp_path: Path, scenario: str) -> None:
    """After a rename/move Clarion carries the SAME SEI; Filigree must keep its
    binding keyed on it and never silently re-point. Modelled producer-side as:
    a value already SEI-shaped is skipped on re-run (prefix check), and its
    ``content_hash_at_attach`` is untouched — independent of Clarion-side churn."""
    locator = "py:func:mod.a::f"
    sei = "clarion:eid:00000000000000000000000000000001"
    with clarion_stub(sei_supported=True, sei_by_locator={locator: sei}) as (base_url, state):
        db = _clarion_db(tmp_path, base_url)
        issue = db.create_issue(scenario, priority=2)
        db.add_entity_association(issue.id, locator, content_hash="sha256:body")
        run_sei_backfill(db, dry_run=False, actor="op")
        hash_after_first = db.list_entity_associations(issue.id)[0]["content_hash_at_attach"]

        # A rename/move on Clarion's side carries the same SEI to a new locator,
        # but the stored value is already that SEI. Re-running must be a no-op:
        # the prefix check skips it — no resolve call, no re-point.
        state.identity_resolve_requests.clear()
        report = run_sei_backfill(db, dry_run=False, actor="op")

        assert report.associations_already_sei == 1
        assert report.associations_migrated == 0
        # No locator was sent for resolution (the only stored value was an SEI).
        assert state.identity_resolve_requests == [] or all(req == [] for req in state.identity_resolve_requests)
        row = db.list_entity_associations(issue.id)[0]
        assert row["clarion_entity_id"] == sei
        assert row["content_hash_at_attach"] == hash_after_first
        db.close()


# ---------------------------------------------------------------------------
# §8.4 / §8.5 ambiguous + delete — orphan, never dropped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", ["ambiguous", "delete"])
def test_unresolvable_locator_is_orphaned_not_dropped(tmp_path: Path, scenario: str) -> None:
    locator = "py:func:gone.module::vanished"
    # Stub maps nothing → the locator resolves to ``not_found`` (alive:false),
    # which is exactly how both the ambiguous (fail-closed, old binding orphaned)
    # and delete scenarios present to a producer.
    with clarion_stub(sei_supported=True) as (base_url, _state):
        db = _clarion_db(tmp_path, base_url)
        issue = db.create_issue(scenario, priority=2)
        db.add_entity_association(issue.id, locator, content_hash="sha256:body")

        report = run_sei_backfill(db, dry_run=False, actor="op")

        assert report.associations_migrated == 0
        assert report.associations_orphaned == 1
        assert [(o.source, o.locator, o.reason) for o in report.orphans] == [("association", locator, "unresolved")]
        # The binding is KEPT verbatim (never dropped) and flagged for review.
        row = db.conn.execute(
            "SELECT clarion_entity_id, content_hash_at_attach, migration_orphaned_at FROM entity_associations WHERE issue_id = ?",
            (issue.id,),
        ).fetchone()
        assert row["clarion_entity_id"] == locator
        assert row["migration_orphaned_at"] is not None
        # The content axis stays inspectable on the orphan.
        assert row["content_hash_at_attach"] == "sha256:body"
        # A re-run skips the already-flagged orphan (no duplicate work).
        report2 = run_sei_backfill(db, dry_run=False, actor="op")
        assert report2.associations_scanned == 0
        db.close()


# ---------------------------------------------------------------------------
# §8.6 capability_absent — refuse cleanly, keep working on locators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("include_sei_capability", "sei_supported"),
    [(True, False), (False, False)],
    ids=["sei_supported_false", "sei_object_absent"],
)
def test_capability_absent_refuses_cleanly(tmp_path: Path, include_sei_capability: bool, sei_supported: bool) -> None:
    locator = "py:func:mod::f"
    with clarion_stub(include_sei_capability=include_sei_capability, sei_supported=sei_supported) as (base_url, _state):
        db = _clarion_db(tmp_path, base_url)
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, locator, content_hash="sha256:body")

        with pytest.raises(SeiBackfillError):
            run_sei_backfill(db, dry_run=False, actor="op")

        # Degrades gracefully: the binding is untouched and still readable on its
        # locator (no crash, no partial write, "identity unavailable").
        assert db.list_entity_associations(issue.id)[0]["clarion_entity_id"] == locator
        stored = db.conn.execute(
            "SELECT clarion_entity_id, migration_orphaned_at FROM entity_associations WHERE issue_id = ?",
            (issue.id,),
        ).fetchone()
        assert stored["clarion_entity_id"] == locator
        assert stored["migration_orphaned_at"] is None
        db.close()


# ---------------------------------------------------------------------------
# Backfill-branch coverage beyond the six scenarios
# ---------------------------------------------------------------------------


def test_invalid_locator_is_orphaned_with_invalid_reason(tmp_path: Path) -> None:
    """A locator Clarion rejects as malformed (REQ-F-02 ``invalid`` channel) is
    orphaned with reason ``invalid`` — distinguished from ``unresolved``."""
    locator = "malformed-locator"
    with clarion_stub(sei_supported=True) as (base_url, state):
        state.invalid_locators.add(locator)
        db = _clarion_db(tmp_path, base_url)
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, locator, content_hash="h")

        report = run_sei_backfill(db, dry_run=False, actor="op")

        assert [(o.reason, o.locator) for o in report.orphans] == [("invalid", locator)]
        db.close()


def test_pk_collision_merges_to_single_row(tmp_path: Path) -> None:
    """Two locators on one issue that resolve to the SAME SEI collapse to one
    row (the duplicate is merged, not a PK crash)."""
    loc_a = "py:func:mod::f"
    loc_b = "py:func:mod::f_alias"
    sei = "clarion:eid:00000000000000000000000000000002"
    with clarion_stub(sei_supported=True, sei_by_locator={loc_a: sei, loc_b: sei}) as (base_url, _state):
        db = _clarion_db(tmp_path, base_url)
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, loc_a, content_hash="h1")
        db.add_entity_association(issue.id, loc_b, content_hash="h2")

        report = run_sei_backfill(db, dry_run=False, actor="op")

        assert report.associations_merged == 1
        rows = db.list_entity_associations(issue.id)
        assert [r["clarion_entity_id"] for r in rows] == [sei]
        db.close()


def test_deleted_issue_tombstones_are_rewritten(tmp_path: Path) -> None:
    """REQ-F-01: historical ``deleted_issues.entity_ids`` arrays are rewritten
    locator→SEI (orphans kept verbatim) so the changes feed is SEI-only."""
    resolved_loc = "py:func:mod::kept"
    orphan_loc = "py:func:mod::gone"
    sei = "clarion:eid:00000000000000000000000000000003"
    with clarion_stub(sei_supported=True, sei_by_locator={resolved_loc: sei}) as (base_url, _state):
        db = _clarion_db(tmp_path, base_url)
        _insert_tombstone(db, "test-deleted-1", [resolved_loc, orphan_loc])

        report = run_sei_backfill(db, dry_run=False, actor="op")

        assert report.tombstone_locators_migrated == 1
        assert report.tombstone_locators_orphaned == 1
        stored = json.loads(
            db.conn.execute("SELECT entity_ids FROM deleted_issues WHERE issue_id = 'test-deleted-1'").fetchone()["entity_ids"]
        )
        # Resolved locator → SEI; orphan locator kept verbatim. Never a mix lost.
        assert stored == [sei, orphan_loc]
        db.close()


def test_dry_run_plans_without_writing(tmp_path: Path) -> None:
    locator = "py:func:mod::f"
    sei = "clarion:eid:00000000000000000000000000000004"
    with clarion_stub(sei_supported=True, sei_by_locator={locator: sei}) as (base_url, _state):
        db = _clarion_db(tmp_path, base_url)
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, locator, content_hash="h")

        report = run_sei_backfill(db, dry_run=True, actor="op")

        assert report.dry_run is True
        assert report.associations_migrated == 1
        # Nothing was written — the value is still the locator.
        assert db.list_entity_associations(issue.id)[0]["clarion_entity_id"] == locator
        db.close()


def test_already_migrated_db_is_a_noop(tmp_path: Path) -> None:
    """Resumability: a fully-migrated DB re-runs to a clean no-op (no resolve
    calls, nothing rewritten)."""
    sei = "clarion:eid:00000000000000000000000000000005"
    with clarion_stub(sei_supported=True) as (base_url, state):
        db = _clarion_db(tmp_path, base_url)
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, sei, content_hash="h")  # already an SEI

        report = run_sei_backfill(db, dry_run=False, actor="op")

        assert report.associations_already_sei == 1
        assert report.associations_scanned == 0
        assert state.identity_resolve_requests == [] or all(req == [] for req in state.identity_resolve_requests)
        db.close()
