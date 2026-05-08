"""Tests for core plan operations — get_plan, create_plan, rollback."""

from __future__ import annotations

import pytest

from filigree.core import FiligreeDB


class TestGetPlan:
    def test_plan_tree(self, db: FiligreeDB) -> None:
        ms = db.create_issue("Milestone 1", type="milestone")
        p1 = db.create_issue("Phase 1", type="phase", parent_id=ms.id, fields={"sequence": 1})
        p2 = db.create_issue("Phase 2", type="phase", parent_id=ms.id, fields={"sequence": 2})
        s1 = db.create_issue("Step 1", type="step", parent_id=p1.id, fields={"sequence": 1})
        db.create_issue("Step 2", type="step", parent_id=p1.id, fields={"sequence": 2})
        db.create_issue("Step 3", type="step", parent_id=p2.id, fields={"sequence": 1})
        # step requires walking pending → in_progress → completed (validate-transitions
        # policy, filigree-cb980eee0d).
        db.update_issue(s1.id, status="in_progress")
        db.close_issue(s1.id)

        plan = db.get_plan(ms.id)
        assert plan["total_steps"] == 3
        assert plan["completed_steps"] == 1
        assert len(plan["phases"]) == 2
        # Phase 1 has 2 steps, 1 completed
        assert plan["phases"][0]["total"] == 2
        assert plan["phases"][0]["completed"] == 1
        # Phase 2 has 1 step, 0 completed
        assert plan["phases"][1]["total"] == 1
        assert plan["phases"][1]["completed"] == 0

    def test_plan_nonexistent_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(KeyError):
            db.get_plan("nonexistent-abc123")


class TestCreatePlan:
    def test_basic_plan(self, db: FiligreeDB) -> None:
        plan = db.create_plan(
            {"title": "v1.0"},
            [
                {
                    "title": "Phase 1",
                    "steps": [
                        {"title": "Step 1.1"},
                        {"title": "Step 1.2", "deps": [0]},
                    ],
                },
            ],
        )
        assert plan["milestone"]["title"] == "v1.0"
        assert plan["total_steps"] == 2
        # Step 1.2 depends on Step 1.1
        steps = plan["phases"][0]["steps"]
        step_1_2 = steps[1]
        step_1_1 = steps[0]
        assert step_1_1["id"] in step_1_2["blocked_by"]

    def test_cross_phase_deps(self, db: FiligreeDB) -> None:
        plan = db.create_plan(
            {"title": "Cross-phase"},
            [
                {"title": "P1", "steps": [{"title": "S1.1"}]},
                {"title": "P2", "steps": [{"title": "S2.1", "deps": ["0.0"]}]},
            ],
        )
        p1_step = plan["phases"][0]["steps"][0]
        p2_step = plan["phases"][1]["steps"][0]
        assert p1_step["id"] in p2_step["blocked_by"]

    def test_plan_hierarchy_types(self, db: FiligreeDB) -> None:
        plan = db.create_plan(
            {"title": "Typed plan"},
            [{"title": "Phase A", "steps": [{"title": "Step A.1"}]}],
        )
        assert plan["milestone"]["type"] == "milestone"
        assert plan["phases"][0]["phase"]["type"] == "phase"
        assert plan["phases"][0]["steps"][0]["type"] == "step"

    def test_plan_sequence_fields(self, db: FiligreeDB) -> None:
        plan = db.create_plan(
            {"title": "Sequenced"},
            [
                {"title": "Phase 1", "steps": [{"title": "S1"}, {"title": "S2"}]},
                {"title": "Phase 2", "steps": [{"title": "S3"}]},
            ],
        )
        assert plan["phases"][0]["phase"]["fields"]["sequence"] == 1
        assert plan["phases"][1]["phase"]["fields"]["sequence"] == 2
        assert plan["phases"][0]["steps"][0]["fields"]["sequence"] == 1
        assert plan["phases"][0]["steps"][1]["fields"]["sequence"] == 2

    def test_plan_uses_template_initial_states(self, db: FiligreeDB) -> None:
        plan = db.create_plan(
            {"title": "Initial states"},
            [{"title": "Phase 1", "steps": [{"title": "Step 1"}]}],
        )
        assert plan["milestone"]["status"] == "planning"
        assert plan["phases"][0]["phase"]["status"] == "pending"
        assert plan["phases"][0]["steps"][0]["status"] == "pending"

    def test_plan_empty_phases(self, db: FiligreeDB) -> None:
        plan = db.create_plan({"title": "Empty"}, [{"title": "No steps"}])
        assert plan["total_steps"] == 0
        assert len(plan["phases"]) == 1

    def test_plan_labels_propagate_with_level_overrides(self, db: FiligreeDB) -> None:
        plan = db.create_plan(
            {"title": "Labelled", "labels": ["release:v1", " scratch "]},
            [
                {
                    "title": "Phase 1",
                    "labels": ["phase:build"],
                    "steps": [
                        {"title": "Step 1", "labels": ["step:one"]},
                        {"title": "Step 2"},
                    ],
                },
            ],
        )

        milestone = plan["milestone"]
        phase = plan["phases"][0]["phase"]
        step_with_override = plan["phases"][0]["steps"][0]
        inherited_step = plan["phases"][0]["steps"][1]

        assert set(milestone["labels"]) == {"release:v1", "scratch"}
        assert set(phase["labels"]) == {"release:v1", "scratch", "phase:build"}
        assert set(step_with_override["labels"]) == {"release:v1", "scratch", "phase:build", "step:one"}
        assert set(inherited_step["labels"]) == {"release:v1", "scratch", "phase:build"}

    def test_label_subtree_adds_label_to_root_and_descendants(self, db: FiligreeDB) -> None:
        plan = db.create_plan(
            {"title": "Needs label"},
            [{"title": "Phase", "steps": [{"title": "Step"}]}],
        )

        labeled, errors = db.label_subtree(plan["milestone"]["id"], label="scratch")

        assert errors == []
        assert {row["id"] for row in labeled} == {
            plan["milestone"]["id"],
            plan["phases"][0]["phase"]["id"],
            plan["phases"][0]["steps"][0]["id"],
        }
        assert "scratch" in db.get_issue(plan["milestone"]["id"]).labels
        assert "scratch" in db.get_issue(plan["phases"][0]["phase"]["id"]).labels
        assert "scratch" in db.get_issue(plan["phases"][0]["steps"][0]["id"]).labels

    def test_plan_empty_milestone_title_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="Milestone 'title' is required"):
            db.create_plan({"title": ""}, [{"title": "Phase 1"}])

    def test_plan_empty_phase_title_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="Phase 1 'title' is required"):
            db.create_plan({"title": "MS"}, [{"title": ""}])

    def test_plan_empty_step_title_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="Phase 1, Step 1 'title' is required"):
            db.create_plan(
                {"title": "MS"},
                [{"title": "Phase", "steps": [{"title": ""}]}],
            )

    def test_plan_whitespace_only_title_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="Milestone 'title' is required"):
            db.create_plan({"title": "   "}, [{"title": "Phase 1"}])

    def test_plan_rejects_negative_dep_index(self, db: FiligreeDB) -> None:
        """Negative indices silently resolve to wrong step via Python list[-1]."""
        with pytest.raises((ValueError, IndexError)):
            db.create_plan(
                {"title": "MS"},
                [{"title": "Phase", "steps": [{"title": "S1", "deps": [-1]}]}],
            )

    def test_plan_rejects_self_dependency(self, db: FiligreeDB) -> None:
        """Step referencing itself as a dep should raise, not silently insert."""
        with pytest.raises(ValueError, match="self-dependency"):
            db.create_plan(
                {"title": "MS"},
                [{"title": "Phase", "steps": [{"title": "S1", "deps": [0]}]}],
            )

    def test_plan_rejects_cycle(self, db: FiligreeDB) -> None:
        """Mutual deps between steps should raise, not silently insert."""
        with pytest.raises(ValueError, match="cycle"):
            db.create_plan(
                {"title": "MS"},
                [
                    {
                        "title": "Phase",
                        "steps": [
                            {"title": "S1", "deps": [1]},
                            {"title": "S2", "deps": [0]},
                        ],
                    },
                ],
            )

    def test_plan_records_dependency_events(self, db: FiligreeDB) -> None:
        """Dependencies created in a plan should have events, like add_dependency()."""
        plan = db.create_plan(
            {"title": "MS"},
            [
                {
                    "title": "Phase",
                    "steps": [
                        {"title": "S1"},
                        {"title": "S2", "deps": [0]},
                    ],
                },
            ],
        )
        step_2_id = plan["phases"][0]["steps"][1]["id"]
        events = db.get_issue_events(step_2_id)
        dep_events = [e for e in events if e["event_type"] == "dependency_added"]
        assert len(dep_events) == 1

    def test_plan_dedups_duplicate_step_deps(self, db: FiligreeDB) -> None:
        """Bug fix: filigree-fcac6acf6c — a step with duplicate dep indices

        creates one dep row (INSERT OR IGNORE) but must not emit duplicate
        dependency_added events. Duplicate events block undo_last() from
        reaching earlier reversible events on the same step.
        """
        plan = db.create_plan(
            {"title": "Dup Deps MS"},
            [
                {
                    "title": "Phase",
                    "steps": [
                        {"title": "S1"},
                        {"title": "S2", "deps": [0, 0, 0]},
                    ],
                },
            ],
        )
        step_2_id = plan["phases"][0]["steps"][1]["id"]
        events = db.get_issue_events(step_2_id)
        dep_events = [e for e in events if e["event_type"] == "dependency_added"]
        assert len(dep_events) == 1, f"Expected 1 dependency_added event for duplicate deps, got {len(dep_events)}"

    def test_plan_rejects_out_of_range_priority(self, db: FiligreeDB) -> None:
        """Bug fix: filigree-a5e7090f76 — invalid priority must raise ValueError

        before the transaction begins, not surface as sqlite3.IntegrityError
        from the DB-layer CHECK constraint.
        """
        with pytest.raises(ValueError, match="priority"):
            db.create_plan(
                {"title": "Bad Priority MS", "priority": 99},
                [{"title": "Phase", "steps": [{"title": "S1"}]}],
            )

    def test_plan_rejects_out_of_range_phase_priority(self, db: FiligreeDB) -> None:
        """Phase priority must also be validated up front."""
        with pytest.raises(ValueError, match="priority"):
            db.create_plan(
                {"title": "MS"},
                [{"title": "Phase", "priority": 99, "steps": [{"title": "S1"}]}],
            )

    def test_plan_rejects_out_of_range_step_priority(self, db: FiligreeDB) -> None:
        """Step priority must also be validated up front."""
        with pytest.raises(ValueError, match="priority"):
            db.create_plan(
                {"title": "MS"},
                [{"title": "Phase", "steps": [{"title": "S1", "priority": -1}]}],
            )

    def test_plan_rejects_non_int_priority(self, db: FiligreeDB) -> None:
        """Non-integer priority must raise ValueError, not sqlite3.IntegrityError."""
        with pytest.raises(ValueError, match="priority"):
            db.create_plan(
                {"title": "MS", "priority": "high"},  # type: ignore[typeddict-item]
                [{"title": "Phase", "steps": [{"title": "S1"}]}],
            )

    def test_plan_bad_priority_does_not_orphan_rows(self, db: FiligreeDB) -> None:
        """Priority validation must run before any INSERT — no orphan milestones."""
        issues_before = len(db.list_issues())
        with pytest.raises(ValueError, match="priority"):
            db.create_plan(
                {"title": "MS"},
                [{"title": "Phase", "steps": [{"title": "S1", "priority": 99}]}],
            )
        assert len(db.list_issues()) == issues_before


class TestCreatePlanRollback:
    """Bug fix: filigree-4135c6 — create_plan no rollback."""

    def test_bad_dep_reference_rolls_back(self, db: FiligreeDB) -> None:
        """create_plan with a bad dep index should not leave orphan milestone/phases."""
        issues_before = len(db.list_issues())

        with pytest.raises((IndexError, ValueError)):
            db.create_plan(
                milestone={"title": "Orphan Test Milestone"},
                phases=[
                    {
                        "title": "Phase 1",
                        "steps": [
                            {"title": "Step A"},
                            {
                                "title": "Step B",
                                "deps": [99],  # Invalid: no step at index 99
                            },
                        ],
                    }
                ],
            )

        # No orphan issues should remain after rollback
        issues_after = len(db.list_issues())
        assert issues_after == issues_before, f"Expected {issues_before} issues after rollback, got {issues_after}"

    def test_successful_plan_commits(self, db: FiligreeDB) -> None:
        """A valid plan should commit successfully."""
        plan = db.create_plan(
            milestone={"title": "Good Milestone"},
            phases=[
                {
                    "title": "Phase 1",
                    "steps": [
                        {"title": "Step A"},
                        {"title": "Step B", "deps": [0]},
                    ],
                }
            ],
        )
        assert plan["milestone"]["title"] == "Good Milestone"
        assert len(plan["phases"]) == 1
        assert plan["phases"][0]["total"] == 2


class TestPlanTreeChildLimit:
    """Bug filigree-07d55ee5e5: tree builders inherited list_issues' default
    page size of 100 and silently dropped children beyond it."""

    def test_get_plan_includes_all_steps_beyond_default_page(self, db: FiligreeDB) -> None:
        steps = [{"title": f"s{i}"} for i in range(101)]
        result = db.create_plan({"title": "Big M"}, [{"title": "P0", "steps": steps}])
        plan = db.get_plan(result["milestone"]["id"])
        assert plan["total_steps"] == 101
        assert plan["phases"][0]["total"] == 101
        assert len(plan["phases"][0]["steps"]) == 101

    def test_get_plan_includes_all_phases_beyond_default_page(self, db: FiligreeDB) -> None:
        phases = [{"title": f"P{i}", "steps": [{"title": "s"}]} for i in range(101)]
        result = db.create_plan({"title": "MS"}, phases)
        plan = db.get_plan(result["milestone"]["id"])
        assert len(plan["phases"]) == 101
        assert plan["total_steps"] == 101

    def test_release_tree_includes_all_children_beyond_default_page(self, db: FiligreeDB) -> None:
        release = db.create_issue("Big release", type="release")
        for i in range(101):
            db.create_issue(f"task {i}", parent_id=release.id)
        tree = db.get_release_tree(release.id)
        assert len(tree["children"]) == 101


class TestCreatePlanDepRefValidation:
    """Bug filigree-6802ed02e0: ``str(dep_ref)`` silently coerced floats and
    other non-int/non-str types into well-formed-looking dependency indices.
    """

    @pytest.mark.parametrize(
        "bad_dep",
        [
            0.1,  # float that splits into "0.1"
            1.0,  # whole-number float
            True,  # bool — int subclass that should still be rejected
            "0.1.2",  # too many dots
            "0.",  # empty step component
            ".0",  # empty phase component
            "+0",  # signed
            "-1",  # negative string
            "0.-1",  # negative component
            " 1 ",  # whitespace
            None,  # NoneType
            [0],  # nested list
        ],
    )
    def test_invalid_dep_ref_raises(self, db: FiligreeDB, bad_dep: object) -> None:
        with pytest.raises(ValueError, match="dep"):
            db.create_plan(
                {"title": "MS"},
                [
                    {"title": "P0", "steps": [{"title": "s0"}, {"title": "s1"}]},
                    {"title": "P1", "steps": [{"title": "s0", "deps": [bad_dep]}]},
                ],
            )

    def test_invalid_dep_ref_does_not_orphan_rows(self, db: FiligreeDB) -> None:
        """Dep validation must run inside the transaction so partial inserts roll back."""
        issues_before = len(db.list_issues())
        with pytest.raises(ValueError, match="dep"):
            db.create_plan(
                {"title": "MS"},
                [{"title": "P", "steps": [{"title": "s", "deps": [0.5]}]}],
            )
        assert len(db.list_issues()) == issues_before

    def test_valid_string_dep_refs_still_accepted(self, db: FiligreeDB) -> None:
        """Stringified valid refs ('0', '0.1') keep working."""
        plan = db.create_plan(
            {"title": "MS"},
            [
                {"title": "P0", "steps": [{"title": "s0"}, {"title": "s1"}]},
                {"title": "P1", "steps": [{"title": "s0", "deps": ["0.1"]}]},
            ],
        )
        # Verify the dep was wired to phase 0 step 1, not silently misrouted.
        p1_step0_id = plan["phases"][1]["steps"][0]["id"]
        p0_step1_id = plan["phases"][0]["steps"][1]["id"]
        rows = db.conn.execute(
            "SELECT depends_on_id FROM dependencies WHERE issue_id = ?",
            (p1_step0_id,),
        ).fetchall()
        assert [r["depends_on_id"] for r in rows] == [p0_step1_id]
