"""Producer-side SEI conformance oracle (ADR-038 §8) — fast lane.

The shared fixture (``fixtures/sei-conformance-oracle.json``, vendored from
Loomweave) defines six scenarios written from Loomweave's *authority* perspective
(mint / carry / orphan / lineage). Filigree is a *producer*: it stores the SEI
opaquely and degrades when Loomweave lacks the capability. Producer-side, the six
scenarios reduce to a small set of obligations this module proves against the
extended Loomweave stub:

- **identity_round_trip_and_opacity** — the backfill rewrites a stored locator to
  the alive SEI in place; the value round-trips byte-for-byte, carries the
  reserved ``loomweave:eid:`` prefix, and is never parsed.
- **rename / move** — once stored as an SEI the binding is stable: a re-run never
  re-points it (the SEI is carried unchanged Loomweave-side; Filigree skips it on
  the prefix check). ``content_hash_at_attach`` is untouched.
- **ambiguous / delete** — an unresolvable locator is flagged ORPHAN (stamped,
  kept verbatim), never dropped; the content axis stays inspectable.
- **capability_absent** — against a pre-SEI / SEI-unsupported Loomweave the backfill
  refuses cleanly (no partial writes) and the tracker keeps working on locators.

Plus the backfill-branch coverage the producer surface adds: prefix-skip /
resume, the ``invalid`` (REQ-F-02) channel, PK-collision merge, and the
historical ``deleted_issues.entity_ids`` rewrite (REQ-F-01).

The faithful "no grandfathering" gate — the same scenarios against a live
``loomweave serve`` — lives in ``test_sei_oracle_live_loomweave.py``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from filigree import sei_backfill
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


def _loomweave_db(tmp_path: Path, base_url: str) -> FiligreeDB:
    db = FiligreeDB(
        tmp_path / "filigree.db",
        prefix="test",
        registry_backend="loomweave",
        loomweave_config={"base_url": base_url, "timeout_seconds": 2},
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


def _loomweave_oracle_source() -> Path | None:
    """Locate Loomweave's canonical oracle fixture, if the repo is present."""
    candidates = []
    env = os.environ.get("CLARION_REPO")
    if env:
        candidates.append(Path(env) / "docs" / "federation" / "fixtures" / "sei-conformance-oracle.json")
    # Sibling checkout: <home>/loomweave next to <home>/filigree.
    candidates.append(
        Path(__file__).resolve().parents[3] / "loomweave" / "docs" / "federation" / "fixtures" / "sei-conformance-oracle.json"
    )
    return next((c for c in candidates if c.exists()), None)


def test_vendored_oracle_matches_loomweave_source() -> None:
    """The vendored copy must not drift from Loomweave's canonical fixture."""
    source = _loomweave_oracle_source()
    if source is None:
        pytest.skip("Loomweave repo not found (set CLARION_REPO to enable the drift check)")
    assert json.loads(ORACLE_PATH.read_text()) == json.loads(source.read_text()), (
        "Vendored sei-conformance-oracle.json has drifted from Loomweave's source; re-copy it."
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
    sei = "loomweave:eid:deadbeefdeadbeefdeadbeefdeadbeef"
    with clarion_stub(sei_supported=True, sei_by_locator={locator: sei}) as (base_url, _state):
        db = _loomweave_db(tmp_path, base_url)
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, locator, content_hash="sha256:body")

        report = run_sei_backfill(db, dry_run=False, actor="op")

        assert report.associations_migrated == 1
        assert report.associations_orphaned == 0
        rows = db.list_entity_associations(issue.id)
        assert len(rows) == 1
        stored = rows[0]["loomweave_entity_id"]
        # Opacity + round-trip: the SEI is stored verbatim, carries the reserved
        # prefix, and is not the locator.
        assert stored == sei
        assert stored.startswith("loomweave:eid:")
        assert stored != locator
        # Reverse lookup keys on the new SEI.
        assert [r["loomweave_entity_id"] for r in db.list_associations_by_entity(sei)] == [sei]
        db.close()


# ---------------------------------------------------------------------------
# §8.2 / §8.3 rename + move — the producer never re-points a stable SEI
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", ["rename", "move"])
def test_stable_sei_is_never_repointed(tmp_path: Path, scenario: str) -> None:
    """After a rename/move Loomweave carries the SAME SEI; Filigree must keep its
    binding keyed on it and never silently re-point. Modelled producer-side as:
    a value already SEI-shaped is skipped on re-run (prefix check), and its
    ``content_hash_at_attach`` is untouched — independent of Loomweave-side churn."""
    locator = "py:func:mod.a::f"
    sei = "loomweave:eid:00000000000000000000000000000001"
    with clarion_stub(sei_supported=True, sei_by_locator={locator: sei}) as (base_url, state):
        db = _loomweave_db(tmp_path, base_url)
        issue = db.create_issue(scenario, priority=2)
        db.add_entity_association(issue.id, locator, content_hash="sha256:body")
        run_sei_backfill(db, dry_run=False, actor="op")
        hash_after_first = db.list_entity_associations(issue.id)[0]["content_hash_at_attach"]

        # A rename/move on Loomweave's side carries the same SEI to a new locator,
        # but the stored value is already that SEI. Re-running must be a no-op:
        # the prefix check skips it — no resolve call, no re-point.
        state.identity_resolve_requests.clear()
        report = run_sei_backfill(db, dry_run=False, actor="op")

        assert report.associations_already_sei == 1
        assert report.associations_migrated == 0
        # No locator was sent for resolution (the only stored value was an SEI).
        assert state.identity_resolve_requests == [] or all(req == [] for req in state.identity_resolve_requests)
        row = db.list_entity_associations(issue.id)[0]
        assert row["loomweave_entity_id"] == sei
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
        db = _loomweave_db(tmp_path, base_url)
        issue = db.create_issue(scenario, priority=2)
        db.add_entity_association(issue.id, locator, content_hash="sha256:body")

        report = run_sei_backfill(db, dry_run=False, actor="op")

        assert report.associations_migrated == 0
        assert report.associations_orphaned == 1
        assert [(o.source, o.locator, o.reason) for o in report.orphans] == [("association", locator, "unresolved")]
        # The binding is KEPT verbatim (never dropped) and flagged for review.
        row = db.conn.execute(
            "SELECT loomweave_entity_id, content_hash_at_attach, migration_orphaned_at FROM entity_associations WHERE issue_id = ?",
            (issue.id,),
        ).fetchone()
        assert row["loomweave_entity_id"] == locator
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
        db = _loomweave_db(tmp_path, base_url)
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, locator, content_hash="sha256:body")

        with pytest.raises(SeiBackfillError):
            run_sei_backfill(db, dry_run=False, actor="op")

        # Degrades gracefully: the binding is untouched and still readable on its
        # locator (no crash, no partial write, "identity unavailable").
        assert db.list_entity_associations(issue.id)[0]["loomweave_entity_id"] == locator
        stored = db.conn.execute(
            "SELECT loomweave_entity_id, migration_orphaned_at FROM entity_associations WHERE issue_id = ?",
            (issue.id,),
        ).fetchone()
        assert stored["loomweave_entity_id"] == locator
        assert stored["migration_orphaned_at"] is None
        db.close()


# ---------------------------------------------------------------------------
# Backfill-branch coverage beyond the six scenarios
# ---------------------------------------------------------------------------


def test_invalid_locator_is_orphaned_with_invalid_reason(tmp_path: Path) -> None:
    """A locator Loomweave rejects as malformed (REQ-F-02 ``invalid`` channel) is
    orphaned with reason ``invalid`` — distinguished from ``unresolved``."""
    locator = "malformed-locator"
    with clarion_stub(sei_supported=True) as (base_url, state):
        state.invalid_locators.add(locator)
        db = _loomweave_db(tmp_path, base_url)
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
    sei = "loomweave:eid:00000000000000000000000000000002"
    with clarion_stub(sei_supported=True, sei_by_locator={loc_a: sei, loc_b: sei}) as (base_url, _state):
        db = _loomweave_db(tmp_path, base_url)
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, loc_a, content_hash="h1")
        db.add_entity_association(issue.id, loc_b, content_hash="h2")

        report = run_sei_backfill(db, dry_run=False, actor="op")

        assert report.associations_merged == 1
        rows = db.list_entity_associations(issue.id)
        assert [r["loomweave_entity_id"] for r in rows] == [sei]
        db.close()


_EARLIER = "2026-01-01T00:00:00+00:00"
_LATER = "2026-06-01T00:00:00+00:00"


def _set_attach_metadata(
    db: FiligreeDB,
    issue_id: str,
    locator: str,
    *,
    attached_at: str,
    content_hash: str,
    attached_by: str,
) -> None:
    """Pin attach metadata on an association directly (``add_entity_association``
    stamps ``attached_at`` with now() and won't let two rows differ
    deterministically)."""
    db.conn.execute(
        "UPDATE entity_associations SET attached_at = ?, content_hash_at_attach = ?, attached_by = ? "
        "WHERE issue_id = ? AND loomweave_entity_id = ?",
        (attached_at, content_hash, attached_by, issue_id, locator),
    )
    db.conn.commit()


@pytest.mark.parametrize("incoming_newer", [True, False], ids=["incoming_newer", "incoming_older"])
def test_merge_keeps_newest_attach_axis_and_preserves_attached_by(tmp_path: Path, incoming_newer: bool) -> None:
    """When two locators collapse onto one SEI, the survivor must carry the
    *newest* (attached_at, content_hash_at_attach) pair and keep its original
    ``attached_by``. Both branches of the ``incoming.attached_at >
    survivor.attached_at`` preference rule are pinned here, in both directions:

    - ``incoming_newer``: the UPDATE branch fires — the survivor adopts the
      incoming row's fresher content hash. An inverted/never-firing comparison
      would leave the stale hash and silently regress the content axis (false
      freshness on a live binding).
    - ``incoming_older``: the branch must NOT fire — the survivor keeps its own
      fresher hash; an always-firing comparison would overwrite it with stale.

    Either way the collapsed row resolves to the newest attach across the pair,
    and ``attached_by`` is the first binder's (the merge never rewrites it)."""
    # loc_a is inserted first, so it is the row that migrates to the SEI and
    # becomes the survivor; loc_b collides onto it and is the incoming/merged row.
    loc_a = "py:func:mod::f"
    loc_b = "py:func:mod::f_alias"
    sei = "loomweave:eid:00000000000000000000000000000006"
    with clarion_stub(sei_supported=True, sei_by_locator={loc_a: sei, loc_b: sei}) as (base_url, _state):
        db = _loomweave_db(tmp_path, base_url)
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, loc_a, content_hash="sha256:seed_a")
        db.add_entity_association(issue.id, loc_b, content_hash="sha256:seed_b")

        if incoming_newer:
            # survivor (loc_a) older/stale; incoming (loc_b) newer/fresh.
            _set_attach_metadata(db, issue.id, loc_a, attached_at=_EARLIER, content_hash="sha256:stale", attached_by="alice")
            _set_attach_metadata(db, issue.id, loc_b, attached_at=_LATER, content_hash="sha256:fresh", attached_by="bob")
        else:
            # survivor (loc_a) newer/fresh; incoming (loc_b) older/stale.
            _set_attach_metadata(db, issue.id, loc_a, attached_at=_LATER, content_hash="sha256:fresh", attached_by="alice")
            _set_attach_metadata(db, issue.id, loc_b, attached_at=_EARLIER, content_hash="sha256:stale", attached_by="bob")

        report = run_sei_backfill(db, dry_run=False, actor="op")

        assert report.associations_merged == 1
        row = db.conn.execute(
            "SELECT loomweave_entity_id, content_hash_at_attach, attached_at, attached_by FROM entity_associations WHERE issue_id = ?",
            (issue.id,),
        ).fetchall()
        assert len(row) == 1
        survivor = row[0]
        assert survivor["loomweave_entity_id"] == sei
        # The freshness invariant: newest attach wins, in BOTH directions.
        assert survivor["attached_at"] == _LATER
        assert survivor["content_hash_at_attach"] == "sha256:fresh"
        # attached_by is the first binder's identity, never the merged row's.
        assert survivor["attached_by"] == "alice"
        db.close()


def test_applied_run_rolls_back_all_writes_on_mid_apply_fault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The applied run is a single ``BEGIN IMMEDIATE`` transaction whose only
    no-partial-writes guarantee is the ``except: rollback; raise`` in ``_apply``.
    Inject a fault after the first association has been written: the first row's
    migration must be undone (still its locator), no row may be left half-written,
    the fault must propagate, and the transaction must be closed — not leaked
    open onto the live production DB this irreversible migration runs against."""
    loc_a = "py:func:mod::a"
    loc_b = "py:func:mod::b"
    sei_a = "loomweave:eid:0000000000000000000000000000000a"
    sei_b = "loomweave:eid:0000000000000000000000000000000b"
    with clarion_stub(sei_supported=True, sei_by_locator={loc_a: sei_a, loc_b: sei_b}) as (base_url, _state):
        db = _loomweave_db(tmp_path, base_url)
        issue_a = db.create_issue("a", priority=2)
        issue_b = db.create_issue("b", priority=2)
        db.add_entity_association(issue_a.id, loc_a, content_hash="sha256:a")
        db.add_entity_association(issue_b.id, loc_b, content_hash="sha256:b")

        # Let the first association migrate for real (a committed-within-tx write),
        # then fault on the second so rollback has something to undo.
        real_apply = sei_backfill._apply_association
        calls = {"n": 0}

        def faulting_apply(*args: object, **kwargs: object) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                real_apply(*args, **kwargs)  # type: ignore[arg-type]
                return
            raise RuntimeError("injected mid-apply fault")

        monkeypatch.setattr(sei_backfill, "_apply_association", faulting_apply)

        with pytest.raises(RuntimeError, match="injected mid-apply fault"):
            run_sei_backfill(db, dry_run=False, actor="op")

        assert calls["n"] == 2  # the fault genuinely fired after the first write
        # No partial write survived: BOTH bindings are still their original
        # locators (the first row's migration was rolled back).
        stored = {
            r["issue_id"]: r["loomweave_entity_id"]
            for r in db.conn.execute("SELECT issue_id, loomweave_entity_id FROM entity_associations").fetchall()
        }
        assert stored == {issue_a.id: loc_a, issue_b.id: loc_b}
        # The transaction is closed, not leaked open on the live connection.
        assert db.conn.in_transaction is False
        db.close()


def test_dry_run_merge_count_matches_apply_for_already_sei_survivor(tmp_path: Path) -> None:
    """A locator that collapses onto a row already at its target SEI is a merge
    the applied run performs (the locator row is deleted). The dry-run must
    report it too — otherwise the preview undercounts a destructive collapse on
    a resumed/partial backfill (false-green on a row deletion)."""
    loc = "py:func:mod::f_alias"
    sei = "loomweave:eid:00000000000000000000000000000007"
    with clarion_stub(sei_supported=True, sei_by_locator={loc: sei}) as (base_url, _state):
        db = _loomweave_db(tmp_path, base_url)
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, sei, content_hash="h1")  # already SEI (survivor)
        db.add_entity_association(issue.id, loc, content_hash="h2")  # collapses onto it

        preview = run_sei_backfill(db, dry_run=True, actor="op")
        applied = run_sei_backfill(db, dry_run=False, actor="op")

        assert applied.associations_merged == 1
        assert preview.associations_merged == applied.associations_merged
        rows = db.list_entity_associations(issue.id)
        assert [r["loomweave_entity_id"] for r in rows] == [sei]
        db.close()


def test_deleted_issue_tombstones_are_rewritten(tmp_path: Path) -> None:
    """REQ-F-01: historical ``deleted_issues.entity_ids`` arrays are rewritten
    locator→SEI (orphans kept verbatim) so the changes feed is SEI-only."""
    resolved_loc = "py:func:mod::kept"
    orphan_loc = "py:func:mod::gone"
    sei = "loomweave:eid:00000000000000000000000000000003"
    with clarion_stub(sei_supported=True, sei_by_locator={resolved_loc: sei}) as (base_url, _state):
        db = _loomweave_db(tmp_path, base_url)
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


def _insert_raw_tombstone(db: FiligreeDB, issue_id: str, raw_entity_ids: str) -> None:
    """Insert a tombstone with a *verbatim* ``entity_ids`` blob (not json.dumps'd),
    so a test can plant a corrupt / non-array value the backfill must surface."""
    db.conn.execute(
        "INSERT INTO deleted_issues (issue_id, title, type, deleted_at, deleted_by, reason, entity_ids) "
        "VALUES (?, 't', 'task', '2026-05-01T00:00:00+00:00', 'x', '', ?)",
        (issue_id, raw_entity_ids),
    )
    db.conn.commit()


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry_run", "applied"])
@pytest.mark.parametrize(
    "raw_entity_ids",
    ["{not valid json", '{"not": "an array"}', '"a-bare-string"', "42", '["py:func:mod::kept", 42]'],
    ids=["corrupt_json", "object", "bare_string", "number", "mixed_array"],
)
def test_malformed_tombstone_entity_ids_is_surfaced_not_silently_dropped(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    dry_run: bool,
    raw_entity_ids: str,
) -> None:
    """A ``deleted_issues.entity_ids`` blob that is corrupt JSON or not a JSON
    array decodes to ``[]`` — the whole tombstone would otherwise vanish from the
    backfill with no log, counter, or orphan, violating the module's own "never
    silently drops an orphan" contract. It must instead emit a WARNING and a
    ``tombstones_corrupt`` counter, and the row must be left verbatim (never
    rewritten to garbage). Holds on both the dry-run and applied paths."""
    with clarion_stub(sei_supported=True) as (base_url, _state):
        db = _loomweave_db(tmp_path, base_url)
        _insert_raw_tombstone(db, "test-corrupt-1", raw_entity_ids)

        with caplog.at_level(logging.WARNING, logger="filigree.sei_backfill"):
            report = run_sei_backfill(db, dry_run=dry_run, actor="op")

        # Surfaced on the report: counted as corrupt, never miscounted as a clean
        # scan / migrate / orphan.
        assert report.tombstones_corrupt == 1
        assert report.to_dict()["tombstones_corrupt"] == 1
        assert report.tombstones_scanned == 0
        assert report.tombstone_locators_migrated == 0
        assert report.tombstone_locators_orphaned == 0
        # Surfaced in the log, naming the issue so an operator can inspect it.
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("test-corrupt-1" in r.getMessage() for r in warnings), warnings
        # The corrupt blob is left verbatim — never rewritten.
        stored = db.conn.execute("SELECT entity_ids FROM deleted_issues WHERE issue_id = 'test-corrupt-1'").fetchone()
        assert stored["entity_ids"] == raw_entity_ids
        db.close()


def test_empty_tombstone_entity_ids_is_not_flagged_corrupt(tmp_path: Path) -> None:
    """A legitimately empty tombstone (``"[]"`` or NULL) is a normal state — a
    deleted issue with no entity bindings — and must NOT be flagged corrupt."""
    with clarion_stub(sei_supported=True) as (base_url, _state):
        db = _loomweave_db(tmp_path, base_url)
        _insert_raw_tombstone(db, "test-empty-arr", "[]")
        _insert_raw_tombstone(db, "test-null-ids", "")  # stored NULL/empty

        report = run_sei_backfill(db, dry_run=False, actor="op")

        assert report.tombstones_corrupt == 0
        assert report.tombstones_scanned == 0
        db.close()


def test_dry_run_plans_without_writing(tmp_path: Path) -> None:
    locator = "py:func:mod::f"
    sei = "loomweave:eid:00000000000000000000000000000004"
    with clarion_stub(sei_supported=True, sei_by_locator={locator: sei}) as (base_url, _state):
        db = _loomweave_db(tmp_path, base_url)
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, locator, content_hash="h")

        report = run_sei_backfill(db, dry_run=True, actor="op")

        assert report.dry_run is True
        assert report.associations_migrated == 1
        # Nothing was written — the value is still the locator.
        assert db.list_entity_associations(issue.id)[0]["loomweave_entity_id"] == locator
        db.close()


def test_already_migrated_db_is_a_noop(tmp_path: Path) -> None:
    """Resumability: a fully-migrated DB re-runs to a clean no-op (no resolve
    calls, nothing rewritten)."""
    sei = "loomweave:eid:00000000000000000000000000000005"
    with clarion_stub(sei_supported=True) as (base_url, state):
        db = _loomweave_db(tmp_path, base_url)
        issue = db.create_issue("t", priority=2)
        db.add_entity_association(issue.id, sei, content_hash="h")  # already an SEI

        report = run_sei_backfill(db, dry_run=False, actor="op")

        assert report.associations_already_sei == 1
        assert report.associations_scanned == 0
        assert state.identity_resolve_requests == [] or all(req == [] for req in state.identity_resolve_requests)
        db.close()
