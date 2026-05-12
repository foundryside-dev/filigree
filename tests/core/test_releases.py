"""Tests for release tree operations — get_releases_summary, get_release_tree, helpers."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from filigree.core import DB_FILENAME, FILIGREE_DIR_NAME, FiligreeDB, Issue, _seed_builtin_packs, write_config
from filigree.types.planning import TreeNode


def make_release_hierarchy(db: FiligreeDB, *, include_done: bool = False) -> tuple[Issue, Issue, Issue]:
    """Returns (release, epic, task) for standard 3-level test hierarchy."""
    release = db.create_issue("v1.0.0", type="release", fields={"version": "v1.0.0"})
    epic = db.create_issue("Epic A", type="epic", parent_id=release.id)
    task = db.create_issue("Task A", type="task", parent_id=epic.id)
    if include_done:
        db.close_issue(task.id)
    return release, epic, task


# ---------------------------------------------------------------------------
# TestGetReleasesSummary
# ---------------------------------------------------------------------------


class TestGetReleasesSummary:
    def test_returns_only_active_releases_by_default(self, release_db: FiligreeDB) -> None:
        db = release_db
        db.create_issue("R1", type="release")
        db.create_issue("R2", type="release")
        r3 = db.create_issue("R3", type="release")
        # Advance R3 to a done state so it is excluded
        db.update_issue(r3.id, status="development")
        db.update_issue(r3.id, fields={"version": "v3.0.0"}, status="frozen")
        db.update_issue(r3.id, status="testing")
        db.update_issue(r3.id, status="staged")
        db.update_issue(r3.id, status="released")

        result = db.get_releases_summary()
        # R1 + R2 + auto-seeded Future = 3 active releases (R3 is released/done)
        assert len(result) == 3

    def test_include_released_flag_returns_all(self, release_db: FiligreeDB) -> None:
        db = release_db
        db.create_issue("R1", type="release")
        db.create_issue("R2", type="release")
        r3 = db.create_issue("R3", type="release")
        db.update_issue(r3.id, status="development")
        db.update_issue(r3.id, fields={"version": "v3.0.0"}, status="frozen")
        db.update_issue(r3.id, status="testing")
        db.update_issue(r3.id, status="staged")
        db.update_issue(r3.id, status="released")

        result = db.get_releases_summary(include_released=True)
        # R1 + R2 + R3 + auto-seeded Future = 4 total
        assert len(result) == 4

    def test_include_released_returns_more_than_default_page(self, release_db: FiligreeDB) -> None:
        db = release_db
        release_ids = {db.create_issue(f"R{i:03d}", type="release").id for i in range(105)}

        result = db.get_releases_summary(include_released=True)

        returned_ids = {entry["id"] for entry in result}
        assert release_ids <= returned_ids

    def test_rolled_back_release_is_included_in_active(self, release_db: FiligreeDB) -> None:
        db = release_db
        r = db.create_issue("R1", type="release")
        # Advance through: planning -> development -> frozen -> testing -> staged -> released -> rolled_back
        db.update_issue(r.id, status="development")
        db.update_issue(r.id, fields={"version": "v1.0.0"}, status="frozen")
        db.update_issue(r.id, status="testing")
        db.update_issue(r.id, status="staged")
        db.update_issue(r.id, status="released")
        db.update_issue(r.id, status="rolled_back")

        result = db.get_releases_summary()
        ids = [entry["id"] for entry in result]
        assert r.id in ids

    def test_cancelled_release_excluded_by_default(self, release_db: FiligreeDB) -> None:
        db = release_db
        r = db.create_issue("R1", type="release")
        # planning -> cancelled is a valid transition
        db.update_issue(r.id, status="cancelled")

        result = db.get_releases_summary()
        ids = [entry["id"] for entry in result]
        assert r.id not in ids

    def test_empty_release_no_children(self, release_db: FiligreeDB) -> None:
        db = release_db
        r = db.create_issue("Empty", type="release")

        result = db.get_releases_summary()
        # Auto-seeded Future + the "Empty" release = 2
        assert len(result) == 2
        entry = next(e for e in result if e["id"] == r.id)
        assert entry["progress"] == {"total": 0, "completed": 0, "in_progress": 0, "open": 0, "pct": 0}
        assert entry["child_summary"] == {"epics": 0, "milestones": 0, "tasks": 0, "bugs": 0, "other": 0, "total": 0}

    def test_progress_counts_only_leaf_descendants(self, release_db: FiligreeDB) -> None:
        db = release_db
        release, _epic, _task = make_release_hierarchy(db)

        result = db.get_releases_summary()
        entry = next(e for e in result if e["id"] == release.id)
        # Only the task is a leaf; the epic has children so it is not counted
        assert entry["progress"]["total"] == 1

    def test_progress_pct_calculation(self, release_db: FiligreeDB) -> None:
        db = release_db
        release = db.create_issue("R", type="release")
        epic = db.create_issue("E", type="epic", parent_id=release.id)
        t1 = db.create_issue("T1", type="task", parent_id=epic.id)
        db.create_issue("T2", type="task", parent_id=epic.id)
        db.create_issue("T3", type="task", parent_id=epic.id)
        db.close_issue(t1.id)

        result = db.get_releases_summary()
        entry = next(e for e in result if e["id"] == release.id)
        assert entry["progress"]["pct"] == 33

    def test_progress_pct_zero_when_no_leaves(self, release_db: FiligreeDB) -> None:
        db = release_db
        db.create_issue("Empty", type="release")

        result = db.get_releases_summary()
        entry = result[0]
        assert entry["progress"]["pct"] == 0

    def test_progress_pct_100_when_all_complete(self, release_db: FiligreeDB) -> None:
        db = release_db
        release = db.create_issue("R", type="release")
        t1 = db.create_issue("T1", type="task", parent_id=release.id)
        t2 = db.create_issue("T2", type="task", parent_id=release.id)
        db.close_issue(t1.id)
        db.close_issue(t2.id)

        result = db.get_releases_summary()
        entry = next(e for e in result if e["id"] == release.id)
        assert entry["progress"]["pct"] == 100

    def test_intermediate_nodes_not_counted_as_leaves(self, release_db: FiligreeDB) -> None:
        db = release_db
        release = db.create_issue("R", type="release")
        epic = db.create_issue("E", type="epic", parent_id=release.id)
        db.create_issue("T1", type="task", parent_id=epic.id)
        db.create_issue("T2", type="task", parent_id=epic.id)

        result = db.get_releases_summary()
        entry = next(e for e in result if e["id"] == release.id)
        # Only the 2 tasks are leaves, not the epic
        assert entry["progress"]["total"] == 2

    def test_child_summary_counts_by_type(self, release_db: FiligreeDB) -> None:
        db = release_db
        release = db.create_issue("R", type="release")
        db.create_issue("E1", type="epic", parent_id=release.id)
        db.create_issue("E2", type="epic", parent_id=release.id)
        db.create_issue("T1", type="task", parent_id=release.id)
        db.create_issue("B1", type="bug", parent_id=release.id)

        result = db.get_releases_summary()
        entry = next(e for e in result if e["id"] == release.id)
        assert entry["child_summary"] == {
            "epics": 2,
            "milestones": 0,
            "tasks": 1,
            "bugs": 1,
            "other": 0,
            "total": 4,
        }

    def test_child_summary_other_bucket(self, release_db: FiligreeDB) -> None:
        db = release_db
        release = db.create_issue("R", type="release")
        # "feature" type is provided by the core pack and maps to "other"
        # in the type_map (epic/milestone/task/bug are the only mapped keys)
        db.create_issue("F1", type="feature", parent_id=release.id)

        result = db.get_releases_summary()
        entry = next(e for e in result if e["id"] == release.id)
        assert entry["child_summary"]["other"] == 1

    def test_blocks_resolved_to_id_and_title(self, release_db: FiligreeDB) -> None:
        db = release_db
        r1 = db.create_issue("Blocker", type="release")
        r2 = db.create_issue("Blocked", type="release")
        # r2 depends on r1 → r1 blocks r2
        db.add_dependency(r2.id, r1.id)

        result = db.get_releases_summary()
        entry_r2 = next(e for e in result if e["id"] == r2.id)
        assert entry_r2["blocked_by"] == [{"id": r1.id, "title": "Blocker", "type": "release"}]

    def test_blocked_by_resolved_to_id_and_title(self, release_db: FiligreeDB) -> None:
        db = release_db
        r1 = db.create_issue("Blocker", type="release")
        r2 = db.create_issue("Blocked", type="release")
        # r2 depends on r1 → r1 blocks r2
        db.add_dependency(r2.id, r1.id)

        result = db.get_releases_summary()
        entry_r1 = next(e for e in result if e["id"] == r1.id)
        assert entry_r1["blocks"] == [{"id": r2.id, "title": "Blocked", "type": "release"}]

    def test_resolve_refs_handles_deleted_issue_with_dangling_flag(self, release_db: FiligreeDB) -> None:
        db = release_db
        # Call _resolve_issue_refs directly with a bogus ID
        refs = db._resolve_issue_refs(["nonexistent-id-12345"])
        assert len(refs) == 1
        assert refs[0]["id"] == "nonexistent-id-12345"
        assert refs[0]["title"] == "(deleted)"
        assert refs[0]["type"] == "unknown"
        assert refs[0]["dangling"] is True

    def test_resolve_refs_valid_issue_has_no_dangling_flag(self, release_db: FiligreeDB) -> None:
        db = release_db
        r1 = db.create_issue("Real issue", type="release")
        refs = db._resolve_issue_refs([r1.id])
        assert len(refs) == 1
        assert "dangling" not in refs[0]

    def test_version_and_target_date_from_fields(self, release_db: FiligreeDB) -> None:
        db = release_db
        db.create_issue(
            "R",
            type="release",
            fields={"version": "v1.0.0", "target_date": "2026-04-01"},
        )

        result = db.get_releases_summary()
        data = result[0]
        assert data["version"] == "v1.0.0"
        assert data["target_date"] == "2026-04-01"

    def test_version_null_when_absent(self, release_db: FiligreeDB) -> None:
        db = release_db
        db.create_issue("R", type="release")

        result = db.get_releases_summary()
        data = result[0]
        assert data["version"] is None


# ---------------------------------------------------------------------------
# TestGetReleaseTree
# ---------------------------------------------------------------------------


class TestGetReleaseTree:
    def test_returns_release_and_children_keys(self, release_db: FiligreeDB) -> None:
        db = release_db
        release = db.create_issue("R", type="release")

        result = db.get_release_tree(release.id)
        assert "release" in result
        assert "children" in result

    def test_flat_release_with_leaf_children(self, release_db: FiligreeDB) -> None:
        db = release_db
        release = db.create_issue("R", type="release")
        db.create_issue("T1", type="task", parent_id=release.id)
        db.create_issue("T2", type="task", parent_id=release.id)

        result = db.get_release_tree(release.id)
        assert len(result["children"]) == 2
        for child in result["children"]:
            assert child["progress"] is None

    def test_nested_tree_structure(self, release_db: FiligreeDB) -> None:
        db = release_db
        release = db.create_issue("R", type="release")
        epic = db.create_issue("E", type="epic", parent_id=release.id)
        ms = db.create_issue("M", type="milestone", parent_id=epic.id)
        db.create_issue("S", type="step", parent_id=ms.id)

        result = db.get_release_tree(release.id)
        # 4 levels: release -> epic -> milestone -> step
        assert len(result["children"]) == 1  # epic
        epic_node = result["children"][0]
        assert len(epic_node["children"]) == 1  # milestone
        ms_node = epic_node["children"][0]
        assert len(ms_node["children"]) == 1  # step
        step_node = ms_node["children"][0]
        assert step_node["children"] == []

    def test_progress_on_non_leaf_nodes(self, release_db: FiligreeDB) -> None:
        db = release_db
        release = db.create_issue("R", type="release")
        epic = db.create_issue("E", type="epic", parent_id=release.id)
        t1 = db.create_issue("T1", type="task", parent_id=epic.id)
        db.create_issue("T2", type="task", parent_id=epic.id)
        db.close_issue(t1.id)

        result = db.get_release_tree(release.id)
        epic_node = result["children"][0]
        assert epic_node["progress"] is not None
        assert epic_node["progress"]["pct"] == 50

    def test_progress_null_on_leaf_nodes(self, release_db: FiligreeDB) -> None:
        db = release_db
        release = db.create_issue("R", type="release")
        db.create_issue("T1", type="task", parent_id=release.id)

        result = db.get_release_tree(release.id)
        leaf = result["children"][0]
        assert leaf["progress"] is None

    def test_empty_release(self, release_db: FiligreeDB) -> None:
        db = release_db
        release = db.create_issue("R", type="release")

        result = db.get_release_tree(release.id)
        assert result["children"] == []

    def test_raises_keyerror_for_nonexistent_id(self, release_db: FiligreeDB) -> None:
        with pytest.raises(KeyError):
            release_db.get_release_tree("nonexistent-abc123")

    def test_raises_valueerror_for_non_release_type(self, release_db: FiligreeDB) -> None:
        db = release_db
        epic = db.create_issue("E", type="epic")
        with pytest.raises(ValueError, match="not a release"):
            db.get_release_tree(epic.id)

    def test_deeply_nested_five_levels(self, release_db: FiligreeDB) -> None:
        db = release_db
        release = db.create_issue("R", type="release")
        parent_id = release.id
        for i in range(4):
            child = db.create_issue(f"Level {i + 1}", type="task", parent_id=parent_id)
            parent_id = child.id

        result = db.get_release_tree(release.id)
        # Walk down 4 levels
        node = result["children"][0]
        for _ in range(3):
            assert len(node["children"]) == 1
            node = node["children"][0]
        # Deepest node is a leaf
        assert node["children"] == []

    def test_mixed_leaf_and_nonleaf_at_same_level(self, release_db: FiligreeDB) -> None:
        db = release_db
        release = db.create_issue("R", type="release")
        epic = db.create_issue("E", type="epic", parent_id=release.id)
        db.create_issue("T-under-epic", type="task", parent_id=epic.id)
        db.create_issue("T-standalone", type="task", parent_id=release.id)

        result = db.get_release_tree(release.id)
        assert len(result["children"]) == 2

        # Find which child is the epic (has children) and which is the standalone task
        for child in result["children"]:
            if child["issue"]["type"] == "epic":
                assert child["progress"] is not None
            else:
                assert child["progress"] is None

    def test_truncated_tree_surfaces_data_warnings(self, release_db: FiligreeDB) -> None:
        """get_release_tree surfaces truncation warnings at the top level."""
        db = release_db
        release = db.create_issue("R", type="release")
        # Build chain deep enough to trigger truncation (depth > 10)
        parent_id = release.id
        for i in range(12):
            child = db.create_issue(f"T{i}", type="task", parent_id=parent_id)
            parent_id = child.id
        # Add a child that will be truncated
        db.create_issue("Truncated", type="task", parent_id=parent_id)

        result = db.get_release_tree(release.id)
        assert "data_warnings" in result
        assert any("truncated" in w.lower() for w in result["data_warnings"])

    def test_shallow_tree_has_no_data_warnings(self, release_db: FiligreeDB) -> None:
        """Shallow trees should not produce data_warnings."""
        db = release_db
        release = db.create_issue("R", type="release")
        db.create_issue("T1", type="task", parent_id=release.id)

        result = db.get_release_tree(release.id)
        assert result["data_warnings"] == []


# ---------------------------------------------------------------------------
# TestProgressFromSubtree
# ---------------------------------------------------------------------------


class TestProgressFromSubtree:
    def test_single_leaf_done(self, release_db: FiligreeDB) -> None:
        nodes: list[TreeNode] = [
            {"issue": {"status_category": "done"}, "progress": None, "children": []},
        ]
        result = release_db._progress_from_subtree(nodes)
        assert result == {"total": 1, "completed": 1, "in_progress": 0, "open": 0, "pct": 100}

    def test_wip_increments_in_progress(self, release_db: FiligreeDB) -> None:
        nodes: list[TreeNode] = [
            {"issue": {"status_category": "wip"}, "progress": None, "children": []},
        ]
        result = release_db._progress_from_subtree(nodes)
        assert result["in_progress"] == 1

    def test_open_increments_open(self, release_db: FiligreeDB) -> None:
        nodes: list[TreeNode] = [
            {"issue": {"status_category": "open"}, "progress": None, "children": []},
        ]
        result = release_db._progress_from_subtree(nodes)
        assert result["open"] == 1

    def test_rounding_at_boundary(self, release_db: FiligreeDB) -> None:
        nodes: list[TreeNode] = [
            {"issue": {"status_category": "done"}, "progress": None, "children": []},
            {"issue": {"status_category": "open"}, "progress": None, "children": []},
            {"issue": {"status_category": "open"}, "progress": None, "children": []},
        ]
        result = release_db._progress_from_subtree(nodes)
        assert result["pct"] == 33

    def test_empty_nodes_list(self, release_db: FiligreeDB) -> None:
        result = release_db._progress_from_subtree([])
        assert result == {"total": 0, "completed": 0, "in_progress": 0, "open": 0, "pct": 0}

    def test_truncated_sentinel_not_counted_as_leaf(self, release_db: FiligreeDB) -> None:
        """Truncation sentinels must not inflate progress totals as fake open leaves.

        Regression: filigree-719ec7afe3. A deeply nested chain that hits
        ``_MAX_TREE_DEPTH`` produces a single truncated sentinel node at the
        depth boundary. The sentinel has empty children and is not a real
        leaf; progress math must skip it (warnings surface via
        ``_collect_tree_warnings`` instead).
        """
        db = release_db
        release = db.create_issue("R", type="release")
        parent_id = release.id
        last_id = None
        for i in range(12):
            child = db.create_issue(f"T{i}", type="task", parent_id=parent_id)
            parent_id = child.id
            last_id = child.id
        # Give the deepest node a child that will be entirely hidden by truncation
        db.create_issue("HiddenByTruncation", type="task", parent_id=last_id)

        summary = db.get_releases_summary()
        entry = next(e for e in summary if e["id"] == release.id)
        # Every visible task on the spine has one child, so none are real leaves.
        # The only "leaf" in the tree is the truncation sentinel, which must be
        # skipped. Before fix: total == 1 (sentinel counted as open). After: 0.
        assert entry["progress"]["total"] == 0
        assert entry["progress"]["open"] == 0
        # Truncation should still surface as a data warning, not as silent zero.
        assert any("truncat" in w.lower() for w in entry.get("data_warnings", []))


# ---------------------------------------------------------------------------
# TestBuildTree
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestVersionValidation — semver pattern + uniqueness
# ---------------------------------------------------------------------------


class TestVersionValidation:
    """Tests for version field pattern validation and uniqueness enforcement."""

    def test_valid_semver_creates(self, release_db: FiligreeDB) -> None:
        r = release_db.create_issue("R1", type="release", fields={"version": "v1.2.3"})
        assert r.fields["version"] == "v1.2.3"

    def test_invalid_format_raises(self, release_db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="does not match"):
            release_db.create_issue("Bad", type="release", fields={"version": "1.2.3"})

    def test_future_accepted(self, release_db: FiligreeDB) -> None:
        # "Future" is already seeded, so creating another should fail (uniqueness)
        # But the pattern itself accepts "Future"
        r = release_db.get_issue(
            release_db.conn.execute(
                "SELECT id FROM issues WHERE type='release' AND json_extract(fields, '$.version') = 'Future'"
            ).fetchone()["id"]
        )
        assert r.fields["version"] == "Future"

    def test_lowercase_future_rejected(self, release_db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="does not match"):
            release_db.create_issue("Bad", type="release", fields={"version": "future"})

    def test_uniqueness_duplicate_version_raises(self, release_db: FiligreeDB) -> None:
        release_db.create_issue("R1", type="release", fields={"version": "v1.0.0"})
        with pytest.raises(ValueError, match="Duplicate value"):
            release_db.create_issue("R2", type="release", fields={"version": "v1.0.0"})

    def test_uniqueness_same_value_noop_allowed(self, release_db: FiligreeDB) -> None:
        r = release_db.create_issue("R1", type="release", fields={"version": "v1.0.0"})
        # Updating with the same version should not raise
        updated = release_db.update_issue(r.id, fields={"version": "v1.0.0"})
        assert updated.fields["version"] == "v1.0.0"

    def test_uniqueness_update_to_conflict_raises(self, release_db: FiligreeDB) -> None:
        release_db.create_issue("R1", type="release", fields={"version": "v1.0.0"})
        r2 = release_db.create_issue("R2", type="release", fields={"version": "v2.0.0"})
        with pytest.raises(ValueError, match="Duplicate value"):
            release_db.update_issue(r2.id, fields={"version": "v1.0.0"})

    def test_uniqueness_across_closed(self, release_db: FiligreeDB) -> None:
        r = release_db.create_issue("R1", type="release", fields={"version": "v1.0.0"})
        # Close it by advancing through workflow
        release_db.update_issue(r.id, status="development")
        release_db.update_issue(r.id, status="frozen")
        release_db.update_issue(r.id, status="testing")
        release_db.update_issue(r.id, status="staged")
        release_db.update_issue(r.id, status="released")
        # Creating with same version should still fail
        with pytest.raises(ValueError, match="Duplicate value"):
            release_db.create_issue("R2", type="release", fields={"version": "v1.0.0"})

    def test_auto_seed_future_exists_after_init(self, release_db: FiligreeDB) -> None:
        row = release_db.conn.execute(
            "SELECT id FROM issues WHERE type='release' AND json_extract(fields, '$.version') = 'Future'"
        ).fetchone()
        assert row is not None

    def test_auto_seed_idempotent(self, release_db: FiligreeDB) -> None:
        # Call _seed_future_release again — should not create a duplicate
        release_db._seed_future_release()
        release_db.conn.commit()
        rows = release_db.conn.execute(
            "SELECT id FROM issues WHERE type='release' AND json_extract(fields, '$.version') = 'Future'"
        ).fetchall()
        assert len(rows) == 1

    def test_auto_seed_not_created_without_release_pack(self, tmp_path: Path) -> None:
        from tests._db_factory import make_db

        db = make_db(tmp_path, packs=["core", "planning"])
        row = db.conn.execute("SELECT id FROM issues WHERE type='release' AND json_extract(fields, '$.version') = 'Future'").fetchone()
        assert row is None
        db.close()

    def test_cannot_create_second_future(self, release_db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="Duplicate value"):
            release_db.create_issue("Another Future", type="release", fields={"version": "Future"})

    def test_no_version_field_allowed(self, release_db: FiligreeDB) -> None:
        """Creating a release without a version field is fine (version only required at frozen)."""
        r = release_db.create_issue("No Version", type="release")
        assert "version" not in r.fields or r.fields.get("version") is None


class TestBuildTree:
    def test_depth_guard_at_10_levels_sets_truncated_flag(self, release_db: FiligreeDB) -> None:
        db = release_db
        # Build a chain: release -> t0 -> t1 -> ... -> t11 (12 children, 13 levels total)
        # _build_tree starts at _depth=0. At _depth > 10 it returns a truncated sentinel.
        release = db.create_issue("R", type="release")
        parent_id = release.id
        last_id = None
        for i in range(12):
            child = db.create_issue(f"T{i}", type="task", parent_id=parent_id)
            parent_id = child.id
            last_id = child.id

        # Give the deepest node a child that should be truncated
        db.create_issue("Truncated", type="task", parent_id=last_id)

        tree = db._build_tree(release.id)
        # Walk down 11 levels (depth 0 through 10)
        node = tree[0]
        for _ in range(10):
            assert len(node["children"]) == 1, "Expected child at this depth"
            assert node.get("truncated") is not True
            node = node["children"][0]

        # At depth 11, _build_tree returns a sentinel with truncated=True
        assert len(node["children"]) == 1
        sentinel = node["children"][0]
        assert sentinel["truncated"] is True
        assert sentinel["children"] == []

    def test_returns_empty_for_no_children(self, release_db: FiligreeDB) -> None:
        db = release_db
        release = db.create_issue("R", type="release")

        result = db._build_tree(release.id)
        assert result == []

    def test_sort_order_follows_list_issues(self, release_db: FiligreeDB) -> None:
        db = release_db
        release = db.create_issue("R", type="release")
        # Create children with different priorities — list_issues returns by priority (asc)
        db.create_issue("Low", type="task", parent_id=release.id, priority=3)
        db.create_issue("High", type="task", parent_id=release.id, priority=1)
        db.create_issue("Med", type="task", parent_id=release.id, priority=2)

        tree = db._build_tree(release.id)
        titles = [node["issue"]["title"] for node in tree]
        assert titles == ["High", "Med", "Low"]


# ── M6: _seed_future_release and _seed_builtin_packs edge cases ────────


class TestSeedFutureReleaseEdgeCases:
    """Cover untested _seed_future_release paths (M6)."""

    def test_missing_release_type_template_logs_warning(self, release_db: FiligreeDB, caplog: pytest.LogCaptureFixture) -> None:
        """When release type template is missing, _seed_future_release logs warning and skips."""
        # Remove the release type template so get_type("release") returns None
        with (
            patch.object(release_db.templates, "get_type", return_value=None),
            caplog.at_level(logging.WARNING, logger="filigree.core"),
        ):
            release_db._seed_future_release()

        assert any("Release pack enabled but 'release' type not registered" in r.message for r in caplog.records)

    def test_corrupt_release_fields_does_not_abort_init(self, tmp_path: Path) -> None:
        """Bug filigree-20ea5411e1: a release row with malformed JSON in ``fields``
        used to make ``json_extract`` raise ``OperationalError: malformed JSON``,
        aborting the entire DB ``initialize()``. The Future-singleton check must
        be tolerant of pre-existing corruption (it's an idempotent maintenance
        step, not a place to enforce schema integrity).
        """
        # Set up a v2.0 project so FiligreeDB.from_filigree_dir works.
        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        write_config(filigree_dir, {"prefix": "rel", "version": 1, "enabled_packs": ["core", "planning", "release"]})
        db_path = filigree_dir / DB_FILENAME

        # First open: normal init seeds the Future release.
        db = FiligreeDB.from_filigree_dir(filigree_dir)
        db.close()

        # Inject a corrupt-fields release row directly via SQLite, simulating
        # data damage or a buggy migration.
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO issues (id, title, status, priority, type, assignee, "
                "created_at, updated_at, description, notes, fields) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "rel-corrupt",
                    "Bad",
                    "planning",
                    4,
                    "release",
                    "",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                    "",
                    "",
                    "this-is-not-json",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        # Re-opening must not raise: _seed_future_release should skip the bad row.
        reopened = FiligreeDB.from_filigree_dir(filigree_dir)
        try:
            # Future singleton should still resolve to the original good row.
            futures = [
                row
                for row in reopened.conn.execute("SELECT id, fields FROM issues WHERE type = 'release'").fetchall()
                if row[0] != "rel-corrupt"
            ]
            assert any(r[1] and '"Future"' in r[1] for r in futures), (
                "Future release singleton must still exist after corrupt-row tolerance"
            )
        finally:
            reopened.close()

    def test_seed_builtin_packs_returns_type_count(self, tmp_path: Path) -> None:
        """_seed_builtin_packs returns count of type templates seeded."""
        import sqlite3

        from filigree.db_base import _now_iso
        from filigree.db_schema import SCHEMA_SQL

        db_path = tmp_path / "count_test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA_SQL)

        count = _seed_builtin_packs(conn, _now_iso())
        assert isinstance(count, int)
        assert count > 0  # should seed at least the core types

        # Verify the count matches actual rows inserted
        actual = conn.execute("SELECT COUNT(*) FROM type_templates").fetchone()[0]
        assert count == actual
        conn.close()

    def test_seed_builtin_packs_idempotent(self, tmp_path: Path) -> None:
        """Calling _seed_builtin_packs twice returns same count (INSERT OR REPLACE)."""
        import sqlite3

        from filigree.db_base import _now_iso
        from filigree.db_schema import SCHEMA_SQL

        db_path = tmp_path / "idempotent_test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA_SQL)

        now = _now_iso()
        count1 = _seed_builtin_packs(conn, now)
        count2 = _seed_builtin_packs(conn, now)
        assert count1 == count2

        actual = conn.execute("SELECT COUNT(*) FROM type_templates").fetchone()[0]
        assert actual == count1
        conn.close()
