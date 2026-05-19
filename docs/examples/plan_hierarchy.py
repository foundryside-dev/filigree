#!/usr/bin/env python3
"""Milestone / Phase / Step planning with filigree.

This example demonstrates filigree's hierarchical planning system:

  - Creating a milestone with phases and steps using create_plan()
  - Viewing the plan tree with get_plan()
  - Walking through steps in dependency order
  - Tracking progress as steps are completed
  - Cross-phase dependency references

The plan hierarchy is:
    Milestone
      Phase 1: Backend
        Step 1.1: Design API schema
        Step 1.2: Implement endpoints (depends on 1.1)
        Step 1.3: Write API tests (depends on 1.2)
      Phase 2: Frontend
        Step 2.1: Create component library
        Step 2.2: Build dashboard page (depends on 2.1 and step 1.2 from Phase 1)
        Step 2.3: Integration testing (depends on 2.2)

How to run:
    python docs/examples/plan_hierarchy.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from filigree.core import FiligreeDB


def print_plan_tree(plan: dict) -> None:
    """Pretty-print the milestone -> phase -> step hierarchy."""
    ms = plan["milestone"]
    total = plan["total_steps"]
    completed = plan["completed_steps"]
    pct = (completed / total * 100) if total > 0 else 0

    print(f"  Milestone: {ms['title']}  [{completed}/{total} steps done, {pct:.0f}%]")
    print(f"  Status: {ms['status']}  Priority: P{ms['priority']}")
    print()

    for phase_info in plan["phases"]:
        phase = phase_info["phase"]
        p_total = phase_info["total"]
        p_done = phase_info["completed"]
        p_ready = phase_info["ready"]
        print(f"    Phase: {phase['title']}  (status: {phase['status']}, {p_done}/{p_total} done, {p_ready} ready)")

        for step in phase_info["steps"]:
            status_icon = {
                "pending": "[ ]",
                "in_progress": "[~]",
                "completed": "[x]",
                "skipped": "[-]",
            }.get(step["status"], "[?]")
            blocked = ""
            if step["blocked_by"]:
                blocked = f"  (blocked by: {', '.join(step['blocked_by'])})"
            print(f"      {status_icon} {step['title']}  P{step['priority']}{blocked}")
        print()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="filigree_plan_") as tmpdir:
        db_path = Path(tmpdir) / "planning.db"
        db = FiligreeDB(db_path, prefix="demo")
        db.initialize()

        print("=== Plan Hierarchy Demo ===\n")

        # --- Create the plan in one call ---
        # create_plan builds the full milestone -> phase -> step hierarchy
        # and wires up dependencies atomically.
        #
        # Step deps use integer indices within the same phase (0-based),
        # or "phase_idx.step_idx" strings for cross-phase references.

        print("Creating plan...\n")
        plan = db.create_plan(
            milestone={
                "title": "V2.0 Product Launch",
                "priority": 1,
                "description": "Ship the v2.0 release with new dashboard",
            },
            phases=[
                {
                    "title": "Backend API",
                    "description": "Build and test the REST API",
                    "steps": [
                        {
                            "title": "Design API schema",
                            "priority": 1,
                            "description": "Define OpenAPI spec for all endpoints",
                        },
                        {
                            "title": "Implement endpoints",
                            "priority": 1,
                            "description": "Build /users, /projects, /issues routes",
                            "deps": [0],  # depends on step 0 (Design API schema)
                        },
                        {
                            "title": "Write API tests",
                            "priority": 2,
                            "description": "Integration tests with 80% coverage",
                            "deps": [1],  # depends on step 1 (Implement endpoints)
                        },
                    ],
                },
                {
                    "title": "Frontend Dashboard",
                    "description": "React dashboard consuming the API",
                    "steps": [
                        {
                            "title": "Create component library",
                            "priority": 1,
                            "description": "Buttons, tables, forms, modals",
                        },
                        {
                            "title": "Build dashboard page",
                            "priority": 1,
                            "description": "Main dashboard with charts and tables",
                            "deps": [
                                0,  # depends on step 0 in this phase (component library)
                                "0.1",  # depends on phase 0, step 1 (Implement endpoints)
                            ],
                        },
                        {
                            "title": "Integration testing",
                            "priority": 2,
                            "description": "End-to-end tests for dashboard flows",
                            "deps": [1],  # depends on step 1 in this phase (Build dashboard)
                        },
                    ],
                },
            ],
            actor="planner",
        )

        milestone_id = plan["milestone"]["id"]
        print("Initial plan:")
        print_plan_tree(plan)

        # --- Walk through steps in order ---
        # Use get_ready() to find unblocked steps, complete them, and repeat.

        print("=" * 60)
        print("Working through the plan...\n")

        iteration = 0
        while True:
            iteration += 1

            # Refresh the plan to see current state
            plan = db.get_plan(milestone_id)
            if plan["completed_steps"] == plan["total_steps"]:
                break

            # Find ready steps (open + no blockers)
            ready_steps = []
            for phase_info in plan["phases"]:
                for step in phase_info["steps"]:
                    if step["is_ready"]:
                        ready_steps.append(step)

            if not ready_steps:
                print("  No ready steps but plan not complete -- something is wrong!")
                break

            print(f"  Round {iteration}: {len(ready_steps)} step(s) ready")

            # Complete all ready steps (simulating parallel work)
            for step in ready_steps:
                print(f"    Completing: {step['title']}")
                db.update_issue(step["id"], status="in_progress", actor="worker")
                db.close_issue(step["id"], reason="Done", actor="worker")

            # Show progress
            plan = db.get_plan(milestone_id)
            pct = plan["completed_steps"] / plan["total_steps"] * 100
            print(f"    Progress: {plan['completed_steps']}/{plan['total_steps']} ({pct:.0f}%)")
            print()

        # --- Final state ---
        print("=" * 60)
        print("Plan complete!\n")
        final_plan = db.get_plan(milestone_id)
        print_plan_tree(final_plan)

        db.close()

    print("Demo complete.")


if __name__ == "__main__":
    main()
