"""Tests for the warpline reverify-worklist consumer (federation Seam 2A).

Covers the file/link/skip decision per item, the explicit-action (apply) gate,
producer-identity + affected-entity-key stamping, priority mapping, envelope
normalisation, and the loop-closure contract: a filed item's SEI association is
exactly what makes the same entity report as ``linked`` on the next ingest.
"""

from __future__ import annotations

from typing import Any

from filigree.core import FiligreeDB
from filigree.warpline_consumer import (
    ENTITY_KIND,
    PRODUCER_LABELS,
    UNVERIFIED_CONTENT_HASH,
    ingest_reverify_worklist,
)


def _item(sei: str | None, *, locator: str = "pkg.mod.fn", priority: str = "unknown", **extra: Any) -> dict[str, Any]:
    entity: dict[str, Any] = {"locator": locator, "sei": sei}
    if "content_hash" in extra:
        entity["content_hash"] = extra.pop("content_hash")
    return {
        "entity": entity,
        "priority": priority,
        "reason": extra.pop("reason", "changed"),
        "depth": extra.pop("depth", 0),
        "why": extra.pop("why", []),
        "suggested_verification": [{"kind": "test", "command": "run tests"}],
        "enrichment": {"work": [], "risk": [], "governance": [], "requirements": []},
        **extra,
    }


def _worklist(*items: dict[str, Any]) -> dict[str, Any]:
    return {"completeness": "FULL", "items": list(items), "next_actions": {"filigree": []}}


class TestPreview:
    def test_preview_does_not_write(self, db: FiligreeDB) -> None:
        before = len(db.list_issues())
        report = ingest_reverify_worklist(db, _worklist(_item("loomweave:eid:AAA")))
        assert report["applied"] is False
        assert report["summary"] == {"filed": 1, "linked": 0, "skipped": 0, "total": 1}
        assert report["results"][0]["action"] == "filed"
        assert "issue_id" not in report["results"][0]  # nothing created in preview
        assert len(db.list_issues()) == before
        assert db.list_associations_by_entity("loomweave:eid:AAA") == []


class TestApplyFiles:
    def test_files_issue_with_producer_identity_and_entity_key(self, db: FiligreeDB) -> None:
        sei = "loomweave:eid:BBB"
        report = ingest_reverify_worklist(db, _worklist(_item(sei, priority="P1")), apply=True)
        result = report["results"][0]
        assert result["action"] == "filed"
        assert result["priority"] == 1
        assert result["content_hash_source"] == "sentinel"
        issue_id = result["issue_id"]

        issue = db.get_issue(issue_id)
        assert set(PRODUCER_LABELS).issubset(set(issue.labels))  # producer identity
        assert issue.priority == 1

        # affected-entity key — the ADR-029 association warpline reads back.
        assocs = db.list_associations_by_entity(sei)
        assert [a["issue_id"] for a in assocs] == [issue_id]
        assert assocs[0]["content_hash_at_attach"] == UNVERIFIED_CONTENT_HASH
        assert assocs[0]["entity_kind"] == ENTITY_KIND

    def test_supplied_content_hash_is_used(self, db: FiligreeDB) -> None:
        report = ingest_reverify_worklist(db, _worklist(_item("loomweave:eid:CCC", content_hash="deadbeef")), apply=True)
        result = report["results"][0]
        assert result["content_hash_source"] == "provided"
        assocs = db.list_associations_by_entity("loomweave:eid:CCC")
        assert assocs[0]["content_hash_at_attach"] == "deadbeef"

    def test_priority_override(self, db: FiligreeDB) -> None:
        report = ingest_reverify_worklist(db, _worklist(_item("loomweave:eid:DDD", priority="P1")), apply=True, priority_override=3)
        assert db.get_issue(report["results"][0]["issue_id"]).priority == 3


class TestLink:
    def test_already_tracked_links_not_files(self, db: FiligreeDB) -> None:
        sei = "loomweave:eid:EEE"
        existing = db.create_issue("pre-existing work", priority=2)
        db.add_entity_association(existing.id, sei, content_hash="h0")

        report = ingest_reverify_worklist(db, _worklist(_item(sei)), apply=True)
        result = report["results"][0]
        assert result["action"] == "linked"
        assert result["linked_issue_ids"] == [existing.id]
        assert report["summary"]["filed"] == 0
        # no duplicate issue filed for the entity
        assert len(db.list_associations_by_entity(sei)) == 1

    def test_loop_closure_file_then_link(self, db: FiligreeDB) -> None:
        """The headline contract: filing binds the SEI, so a re-ingest links it."""
        sei = "loomweave:eid:FFF"
        wl = _worklist(_item(sei))

        first = ingest_reverify_worklist(db, wl, apply=True)
        assert first["results"][0]["action"] == "filed"
        filed_id = first["results"][0]["issue_id"]

        second = ingest_reverify_worklist(db, wl, apply=True)
        assert second["results"][0]["action"] == "linked"
        assert second["results"][0]["linked_issue_ids"] == [filed_id]

    def test_closed_binding_refiles_and_reports_prior(self, db: FiligreeDB) -> None:
        sei = "loomweave:eid:GGG"
        closed = db.create_issue("old reverify", priority=2)
        db.add_entity_association(closed.id, sei, content_hash="h0")
        db.close_issue(closed.id, reason="done")

        report = ingest_reverify_worklist(db, _worklist(_item(sei)), apply=True)
        result = report["results"][0]
        assert result["action"] == "filed"
        assert result["prior_closed_issue_ids"] == [closed.id]


class TestSkipAndShapes:
    def test_no_sei_is_skipped(self, db: FiligreeDB) -> None:
        report = ingest_reverify_worklist(db, _worklist(_item(None, locator="pkg.unresolved")), apply=True)
        result = report["results"][0]
        assert result["action"] == "skipped"
        assert result["sei"] is None
        assert report["summary"]["skipped"] == 1

    def test_accepts_full_envelope(self, db: FiligreeDB) -> None:
        envelope = {"schema": "warpline.reverify_worklist.v1", "data": _worklist(_item("loomweave:eid:HHH"))}
        report = ingest_reverify_worklist(db, envelope, apply=True)
        assert report["summary"]["filed"] == 1

    def test_empty_and_malformed_items_tolerated(self, db: FiligreeDB) -> None:
        report = ingest_reverify_worklist(db, {"items": [None, 7, {}]}, apply=True)  # type: ignore[list-item]
        # None/7 skipped silently; {} has no sei -> skipped result
        assert report["summary"]["total"] == 1
        assert report["results"][0]["action"] == "skipped"

    def test_unknown_priority_defaults_to_p2(self, db: FiligreeDB) -> None:
        report = ingest_reverify_worklist(db, _worklist(_item("loomweave:eid:III", priority="unknown")), apply=True)
        assert db.get_issue(report["results"][0]["issue_id"]).priority == 2

    def test_mixed_worklist_summary(self, db: FiligreeDB) -> None:
        tracked = db.create_issue("tracked", priority=2)
        db.add_entity_association(tracked.id, "loomweave:eid:TRK", content_hash="h")
        report = ingest_reverify_worklist(
            db,
            _worklist(
                _item("loomweave:eid:NEW"),
                _item("loomweave:eid:TRK"),
                _item(None),
            ),
            apply=True,
        )
        assert report["summary"] == {"filed": 1, "linked": 1, "skipped": 1, "total": 3}
