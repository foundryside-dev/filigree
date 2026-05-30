#!/usr/bin/env python3
"""Custom workflow templates and state transitions in filigree.

This example walks through the full lifecycle of a bug issue, demonstrating
filigree's workflow template system:

  - Bug states: triage -> confirmed -> fixing -> verifying -> closed
  - Checking valid transitions before each status change
  - Setting fields at the right time (severity, root_cause, fix_verification)
  - Soft vs. hard enforcement of required fields
  - Catching ValueError when a hard-enforced transition fails

The bug workflow has both soft and hard enforcement:
  - triage -> confirmed: soft (warns if severity is missing)
  - fixing -> verifying: soft (warns if fix_verification is missing)
  - verifying -> closed: HARD (fails if fix_verification is missing)

How to run:
    python docs/examples/custom_workflow.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from filigree.core import FiligreeDB


def show_transitions(db: FiligreeDB, issue_id: str) -> None:
    """Display valid transitions for an issue."""
    transitions = db.get_valid_transitions(issue_id)
    if not transitions:
        print("    No transitions available.")
        return
    for t in transitions:
        ready_marker = "[ready]" if t.ready else "[blocked]"
        enforcement = f"({t.enforcement})" if t.enforcement else ""
        missing = f" -- needs: {', '.join(t.missing_fields)}" if t.missing_fields else ""
        print(f"    -> {t.to:<12} {t.category:<6} {enforcement:<8} {ready_marker}{missing}")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="filigree_wf_") as tmpdir:
        db_path = Path(tmpdir) / "workflow.db"
        db = FiligreeDB(db_path, prefix="demo")
        db.initialize()

        print("=== Custom Workflow Demo: Bug Lifecycle ===\n")

        # Create a bug -- starts in "triage" state (the initial state for bugs)
        bug = db.create_issue(
            "Login fails when password contains special characters",
            type="bug",
            priority=1,
            description="Users report 500 errors on login with passwords like p@ss!",
            fields={
                "steps_to_reproduce": "1. Go to /login\n2. Enter password: p@ss!\n3. Submit",
                "actual_behavior": "500 Internal Server Error",
                "expected_behavior": "Successful login",
            },
            actor="reporter",
        )
        print(f"Created bug: [{bug.id}] status={bug.status}")
        print(f"  Title: {bug.title}\n")

        # --- Step 1: triage -> confirmed ---
        print("Step 1: Triage -> Confirmed")
        print("  Valid transitions from 'triage':")
        show_transitions(db, bug.id)

        # Set severity (required_at: confirmed) before transitioning
        bug = db.update_issue(
            bug.id,
            status="confirmed",
            fields={"severity": "major"},
            actor="triager",
        )
        print(f"\n  Moved to: {bug.status}")
        print(f"  Severity set to: {bug.fields.get('severity')}\n")

        # --- Step 2: confirmed -> fixing ---
        print("Step 2: Confirmed -> Fixing")
        print("  Valid transitions from 'confirmed':")
        show_transitions(db, bug.id)

        # Set root_cause (required_at: fixing) along with the transition
        bug = db.update_issue(
            bug.id,
            status="fixing",
            fields={"root_cause": "Password not URL-encoded before SQL query"},
            actor="developer",
        )
        print(f"\n  Moved to: {bug.status}")
        print(f"  Root cause: {bug.fields.get('root_cause')}\n")

        # --- Step 3: fixing -> verifying ---
        print("Step 3: Fixing -> Verifying")
        print("  Valid transitions from 'fixing':")
        show_transitions(db, bug.id)

        # Set fix_verification (required_at: verifying, also required for
        # the hard-enforced verifying->closed transition)
        bug = db.update_issue(
            bug.id,
            status="verifying",
            fields={"fix_verification": "Login with p@ss! returns 200 OK"},
            actor="developer",
        )
        print(f"\n  Moved to: {bug.status}")
        print(f"  Fix verification: {bug.fields.get('fix_verification')}\n")

        # --- Step 4: Demonstrate hard enforcement failure ---
        print("Step 4: Demonstrate Hard Enforcement")
        print("  The verifying->closed transition is HARD enforced.")
        print("  It requires fix_verification to be set.\n")

        # Create another bug and try to jump to 'closed' from 'verifying'
        # without fix_verification
        bug2 = db.create_issue(
            "CSS misalignment on dashboard",
            type="bug",
            priority=3,
            actor="reporter",
        )
        # Walk it to verifying without setting fix_verification
        db.update_issue(bug2.id, status="confirmed", fields={"severity": "cosmetic"}, actor="triager")
        db.update_issue(bug2.id, status="fixing", fields={"root_cause": "Missing flex-wrap"}, actor="dev")
        db.update_issue(bug2.id, status="verifying", actor="dev")

        print(f"  Bug [{bug2.id}] is in 'verifying' state WITHOUT fix_verification set.")
        print("  Attempting verifying -> closed ...")

        try:
            db.update_issue(bug2.id, status="closed", actor="qa")
            print("  ERROR: Should have raised an exception!")
        except ValueError as e:
            print(f"  Caught expected error: {e}")
            print("\n  Fix: set fix_verification first, then close.")
            db.update_issue(
                bug2.id,
                status="closed",
                fields={"fix_verification": "Visual check: alignment matches mockup"},
                actor="qa",
            )
            bug2 = db.get_issue(bug2.id)
            print(f"  Bug [{bug2.id}] now: status={bug2.status}, closed_at={bug2.closed_at}\n")

        # --- Step 5: Close the original bug ---
        print("Step 5: Verifying -> Closed (original bug)")
        print("  Valid transitions from 'verifying':")
        show_transitions(db, bug.id)

        bug = db.update_issue(bug.id, status="closed", actor="qa")
        print(f"\n  Final status: {bug.status}")
        print(f"  Closed at: {bug.closed_at}\n")

        # Summary
        print("--- Summary ---")
        for issue in db.list_issues():
            print(f"  [{issue.id}] {issue.type:<5} {issue.status:<10} P{issue.priority}  {issue.title}")

        db.close()

    print("\nDemo complete.")


if __name__ == "__main__":
    main()
