#!/usr/bin/env python3
"""Multi-agent coordination with filigree.

This example demonstrates how multiple agents can safely coordinate using
filigree's atomic claim_next mechanism. Two simulated agents run concurrently
in threads, each competing to claim and complete tasks from a shared pool.

Key concepts shown:
  - Creating tasks with varying priorities
  - Using claim_next() for race-safe work acquisition
  - Advancing issues through the workflow (claim -> in_progress -> closed)
  - Adding comments as an audit trail
  - Handling the "no more work" case when claim_next returns None

How to run:
    python docs/examples/multi_agent.py
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

from filigree.core import FiligreeDB


def create_work_items(db: FiligreeDB) -> list[str]:
    """Create 5 tasks with varying priorities and return their IDs."""
    tasks = [
        ("Set up CI pipeline", 0),  # P0 - Critical
        ("Write unit tests", 1),  # P1 - High
        ("Update documentation", 2),  # P2 - Medium
        ("Refactor logging module", 3),  # P3 - Low
        ("Add changelog entry", 4),  # P4 - Backlog
    ]

    ids = []
    for title, priority in tasks:
        issue = db.create_issue(title, priority=priority, actor="coordinator")
        ids.append(issue.id)
        print(f"  Created: [{issue.id}] P{priority} - {title}")

    return ids


def agent_loop(db: FiligreeDB, agent_name: str) -> None:
    """Simulate an agent that claims and completes tasks in a loop.

    Each iteration:
      1. Try to claim the next highest-priority ready task
      2. If none available, stop
      3. Move to in_progress
      4. Add a comment documenting the work
      5. Close the issue
    """
    while True:
        # Attempt to claim the highest-priority available task.
        # claim_next is atomic -- if two agents race, only one wins.
        issue = db.claim_next(agent_name, actor=agent_name)

        if issue is None:
            print(f"  [{agent_name}] No more work available. Shutting down.")
            break

        print(f"  [{agent_name}] Claimed: [{issue.id}] P{issue.priority} - {issue.title}")

        # Advance to in_progress
        db.update_issue(issue.id, status="in_progress", actor=agent_name)
        print(f"  [{agent_name}] Started work on: {issue.title}")

        # Simulate doing work
        time.sleep(0.05)

        # Leave an audit trail
        db.add_comment(
            issue.id,
            f"Completed by {agent_name}. All checks passed.",
            author=agent_name,
        )

        # Close the issue
        db.close_issue(issue.id, reason="Done", actor=agent_name)
        print(f"  [{agent_name}] Closed: [{issue.id}] - {issue.title}")


def main() -> None:
    # Create a temporary directory for the demo database
    with tempfile.TemporaryDirectory(prefix="filigree_demo_") as tmpdir:
        db_path = Path(tmpdir) / "demo.db"

        # Initialize the database. FiligreeDB handles schema creation,
        # migrations, and workflow template seeding automatically.
        db = FiligreeDB(db_path, prefix="demo")
        db.initialize()

        print("=== Multi-Agent Coordination Demo ===\n")

        # Step 1: Create a pool of work items
        print("Creating work items:")
        create_work_items(db)

        # Step 2: Launch two agents in parallel threads
        print("\nStarting agents:")
        agent_a = threading.Thread(target=agent_loop, args=(db, "agent-alpha"))
        agent_b = threading.Thread(target=agent_loop, args=(db, "agent-beta"))

        agent_a.start()
        agent_b.start()

        agent_a.join()
        agent_b.join()

        # Step 3: Show final state
        print("\n--- Final State ---")
        all_issues = db.list_issues()
        for issue in all_issues:
            comments = db.get_comments(issue.id)
            author = comments[-1]["author"] if comments else "n/a"
            print(f"  [{issue.id}] {issue.status:<12} P{issue.priority}  {issue.title:<30} (completed by: {author})")

        stats = db.get_stats()
        print(f"\nStats: {stats['by_category']}")

        db.close()

    print("\nDemo complete. Temp database cleaned up.")


if __name__ == "__main__":
    main()
