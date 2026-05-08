"""Tests for the summary generator (context.md)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from filigree.core import FiligreeDB
from filigree.summary import _parse_iso as summary_parse_iso
from filigree.summary import _sanitize_title, generate_summary, write_summary
from tests.conftest import PopulatedDB


class TestGenerateSummary:
    def test_empty_db(self, db: FiligreeDB) -> None:
        summary = generate_summary(db)
        assert "Project Pulse" in summary
        # The auto-seeded "Future" release singleton means Open: 1, not 0
        assert "Open: 1" in summary
        assert "(none)" in summary

    def test_with_issues(self, populated_db: PopulatedDB) -> None:
        summary = generate_summary(populated_db.db)
        assert "Project Pulse" in summary
        # Should show vitals
        assert "Open:" in summary
        assert "Ready:" in summary
        assert "Blocked:" in summary

    def test_in_progress_section(self, db: FiligreeDB) -> None:
        issue = db.create_issue("WIP task")
        db.update_issue(issue.id, status="in_progress")
        summary = generate_summary(db)
        assert "In Progress" in summary
        assert "WIP task" in summary

    def test_blocked_section(self, db: FiligreeDB) -> None:
        a = db.create_issue("Blocked task")
        b = db.create_issue("Blocker")
        db.add_dependency(a.id, b.id)
        summary = generate_summary(db)
        assert "Blocked" in summary
        assert "Blocked task" in summary

    def test_plan_section(self, db: FiligreeDB) -> None:
        ms = db.create_issue("Milestone 1", type="milestone")
        p = db.create_issue("Phase 1", type="phase", parent_id=ms.id)
        s1 = db.create_issue("Step 1", type="step", parent_id=p.id)
        db.create_issue("Step 2", type="step", parent_id=p.id)
        # step requires walking pending → in_progress → completed
        # (filigree-cb980eee0d, validate-transitions policy).
        db.update_issue(s1.id, status="in_progress")
        db.close_issue(s1.id)
        summary = generate_summary(db)
        assert "Active Plans" in summary
        assert "Milestone 1" in summary
        assert "Phase 1" in summary

    def test_epic_progress_section(self, db: FiligreeDB) -> None:
        epic = db.create_issue("Epic A", type="epic")
        c1 = db.create_issue("Child 1", parent_id=epic.id)
        db.create_issue("Child 2", parent_id=epic.id)
        db.close_issue(c1.id)
        summary = generate_summary(db)
        assert "Epic Progress" in summary
        assert "Epic A" in summary

    def test_recent_activity_section(self, db: FiligreeDB) -> None:
        db.create_issue("Event source")
        summary = generate_summary(db)
        assert "Recent Activity" in summary
        assert "CREATED" in summary

    def test_ready_section_truncation(self, db: FiligreeDB) -> None:
        """More than 12 ready issues shows truncation message."""
        for i in range(18):
            db.create_issue(f"Ready {i}")
        summary = generate_summary(db)
        # 18 created + 1 auto-seeded Future release = 19 ready; 19 - 12 = 7 more
        assert "...and 7 more" in summary

    def test_stale_section(self, db: FiligreeDB) -> None:
        """In-progress issues >3 days old appear in stale section."""
        issue = db.create_issue("Stale task")
        db.update_issue(issue.id, status="in_progress")
        # Backdate the updated_at to 5 days ago
        old_ts = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        db.conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (old_ts, issue.id))
        db.conn.commit()
        summary = generate_summary(db)
        assert "Stale" in summary
        assert "Stale task" in summary
        assert "5d stale" in summary

    def test_stale_section_includes_malformed_updated_at(self, db: FiligreeDB) -> None:
        """Malformed updated_at should be surfaced as stale with a clear marker."""
        issue = db.create_issue("Malformed stale task")
        db.update_issue(issue.id, status="in_progress")
        db.conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", ("not-a-timestamp", issue.id))
        db.conn.commit()

        summary = generate_summary(db)
        assert "## Stale (in_progress >3 days, no activity)" in summary
        assert "Malformed stale task" in summary
        assert "malformed updated_at: not-a-timestamp" in summary

    def test_no_stale_when_recent(self, db: FiligreeDB) -> None:
        """Recently updated in-progress issues don't appear in stale section."""
        issue = db.create_issue("Fresh task")
        db.update_issue(issue.id, status="in_progress")
        summary = generate_summary(db)
        assert "Stale" not in summary

    def test_parent_lookup_not_n_plus_one(self, db: FiligreeDB) -> None:
        """Parent titles for in-progress issues must not trigger per-issue get_issue calls."""
        from unittest.mock import patch

        parent = db.create_issue("Parent epic", type="epic")
        for i in range(5):
            child = db.create_issue(f"Child {i}", parent_id=parent.id)
            db.update_issue(child.id, status="in_progress")

        original_get_issue = db.get_issue
        get_issue_calls: list[str] = []

        def tracking_get_issue(issue_id: str) -> object:
            get_issue_calls.append(issue_id)
            return original_get_issue(issue_id)

        with patch.object(db, "get_issue", side_effect=tracking_get_issue):
            summary = generate_summary(db)

        assert "Parent epic" in summary
        # With batch lookup, get_issue should NOT be called for parent lookups.
        # Before the fix, it was called once per in-progress child with a parent.
        parent_lookups = [c for c in get_issue_calls if c == parent.id]
        assert len(parent_lookups) <= 1, f"Expected at most 1 parent lookup, got {len(parent_lookups)}"

    def test_title_with_newlines_stays_single_line(self, db: FiligreeDB) -> None:
        """Issue titles with newlines must not break summary line structure."""
        db.create_issue("Normal start\n## Injected Header\nmore text")
        summary = generate_summary(db)
        # The title should appear sanitized — no raw newlines breaking the markdown
        for line in summary.split("\n"):
            if "Injected Header" in line or "Normal start" in line:
                # The title content should be on a single line, not split
                assert line.startswith("- "), f"Title broke out of list item: {line!r}"

    def test_title_with_control_chars_sanitized(self, db: FiligreeDB) -> None:
        """Control characters in titles should be stripped."""
        db.create_issue("Clean\x00title\x1bwith\x07control")
        summary = generate_summary(db)
        assert "\x00" not in summary
        assert "\x1b" not in summary
        assert "\x07" not in summary

    def test_blocked_section_uses_blocked_by_without_get_issue_lookups(self, db: FiligreeDB) -> None:
        """Blocked summary rendering should not do per-blocker get_issue DB lookups."""
        from unittest.mock import patch

        blocked_issue = db.create_issue("Blocked without lookup")
        blocker = db.create_issue("Blocking issue")
        db.add_dependency(blocked_issue.id, blocker.id)

        with patch.object(db, "get_issue", side_effect=AssertionError("generate_summary should not call get_issue")):
            summary = generate_summary(db)

        assert "Blocked without lookup" in summary
        assert f"blocked by: {blocker.id}" in summary


@pytest.mark.slow
class TestSummaryChunkedParentLookup:
    """Bug filigree-4ce103: generate_summary must chunk parent_id lookups to avoid SQLite bind limit."""

    def test_many_parent_ids_does_not_crash(self, db: FiligreeDB) -> None:
        """Create enough issues with parent_ids to exceed a naive IN(...) bind limit."""
        # Create a parent to reference
        parent = db.create_issue("Parent issue")
        # Create 600 child issues — exceeds 500-per-chunk, proving chunking works
        for i in range(600):
            db.create_issue(f"Child {i}", parent_id=parent.id)
        # Should not raise OperationalError: too many SQL variables
        summary = generate_summary(db)
        assert "Parent issue" in summary or "Ready" in summary

    def test_parent_titles_resolved_across_chunks(self, db: FiligreeDB) -> None:
        """Parent titles from different chunks should all be resolved."""
        parents = [db.create_issue(f"Parent-{i}") for i in range(3)]
        for i, p in enumerate(parents):
            # Spread children across different parents
            for j in range(200):
                db.create_issue(f"Child-{i}-{j}", parent_id=p.id)
        summary = generate_summary(db)
        # All parent titles should be fetchable without error
        assert "Ready" in summary


class TestCategoryAwareSummary:
    """Workflow-aware summary tests (Phase 4 — WFT-FR-060, WFT-FR-061, WFT-NFR-010, WFT-FR-071)."""

    def test_vitals_uses_categories(self, db: FiligreeDB) -> None:
        """Vitals line shows Open/In Progress/Done counts from categories."""
        db.create_issue("A")  # open
        b = db.create_issue("B")
        db.update_issue(b.id, status="in_progress")  # wip
        c = db.create_issue("C")
        db.close_issue(c.id)  # done
        summary = generate_summary(db)
        # Open: 2 = A + auto-seeded Future release (planning is open category)
        assert "Open: 2" in summary
        assert "In Progress: 1" in summary
        assert "Done: 1" in summary

    def test_ready_shows_state_in_parens(self, db: FiligreeDB) -> None:
        """Bug in 'confirmed' (open category) shows (confirmed) in ready section."""
        bug = db.create_issue("Login crash", type="bug")
        db.update_issue(bug.id, status="confirmed")
        summary = generate_summary(db)
        assert "(confirmed)" in summary

    def test_ready_omits_open_parens(self, db: FiligreeDB) -> None:
        """Task in 'open' status has no redundant (open) annotation."""
        db.create_issue("Basic task", type="task")
        summary = generate_summary(db)
        # Should show the title but NOT "(open)"
        assert "Basic task" in summary
        assert "(open)" not in summary

    def test_in_progress_uses_wip_category(self, db: FiligreeDB) -> None:
        """Bug in 'fixing' (wip category) appears in In Progress section."""
        bug = db.create_issue("Fixable bug", type="bug")
        db.update_issue(bug.id, status="confirmed")
        db.update_issue(bug.id, status="fixing", fields={"root_cause": "found it"})
        summary = generate_summary(db)
        assert "Fixable bug" in summary
        # Should be in the In Progress section
        lines = summary.split("\n")
        in_progress_idx = next(i for i, line in enumerate(lines) if line.startswith("## In Progress"))
        next_section_idx = next(
            (i for i, line in enumerate(lines) if line.startswith("## ") and i > in_progress_idx),
            len(lines),
        )
        in_progress_section = "\n".join(lines[in_progress_idx:next_section_idx])
        assert "Fixable bug" in in_progress_section

    def test_in_progress_shows_state_in_parens(self, db: FiligreeDB) -> None:
        """Bug in 'fixing' shows (fixing) in In Progress section."""
        bug = db.create_issue("Verifiable bug", type="bug")
        db.update_issue(bug.id, status="confirmed")
        db.update_issue(bug.id, status="fixing", fields={"root_cause": "root"})
        summary = generate_summary(db)
        assert "(fixing)" in summary

    def test_ready_truncation_at_12(self, db: FiligreeDB) -> None:
        """14 ready issues + 1 auto-seeded Future release = 15 ready; shows 12 + '...and 3 more'."""
        for i in range(14):
            db.create_issue(f"Ready {i}")
        summary = generate_summary(db)
        assert "...and 3 more" in summary

    def test_epic_limit_10(self, db: FiligreeDB) -> None:
        """12 open epics → only 10 shown in Epic Progress."""
        for i in range(12):
            epic = db.create_issue(f"Epic {i}", type="epic")
            db.create_issue(f"Child of {i}", parent_id=epic.id)
        summary = generate_summary(db)
        # Count epic entries in Epic Progress section
        lines = summary.split("\n")
        epic_section_lines = []
        in_epic = False
        for line in lines:
            if line.startswith("## Epic Progress"):
                in_epic = True
                continue
            if in_epic and line.startswith("## "):
                break
            if in_epic and line.startswith("- "):
                epic_section_lines.append(line)
        assert len(epic_section_lines) == 10

    def test_epic_done_uses_category(self, db: FiligreeDB) -> None:
        """Child with done-category status (e.g., wont_fix) counts as done in epic."""
        epic = db.create_issue("Test Epic", type="epic")
        child = db.create_issue("Bug child", type="bug", parent_id=epic.id)
        db.close_issue(child.id, status="wont_fix")
        summary = generate_summary(db)
        # Epic should show 1/1 completion
        assert "1/1" in summary

    def test_needs_attention_section(self, db: FiligreeDB) -> None:
        """Bug in 'fixing' missing root_cause → appears in Needs Attention."""
        bug = db.create_issue("Missing fields bug", type="bug")
        db.update_issue(bug.id, status="confirmed")
        # Move to fixing WITHOUT setting root_cause (which is required_at fixing)
        db.update_issue(bug.id, status="fixing")
        summary = generate_summary(db)
        assert "## Needs Attention" in summary
        assert "root_cause" in summary

    def test_needs_attention_absent_when_clean(self, db: FiligreeDB) -> None:
        """All required fields populated → no Needs Attention section."""
        bug = db.create_issue("Clean bug", type="bug")
        db.update_issue(bug.id, status="confirmed")
        db.update_issue(bug.id, status="fixing", fields={"root_cause": "identified"})
        summary = generate_summary(db)
        assert "## Needs Attention" not in summary

    def test_recent_activity_shows_old_value_only_detail(self, db: FiligreeDB) -> None:
        """Events with only old_value should still render detail in Recent Activity."""
        issue = db.create_issue("Old value event source")
        created_at = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
        db.conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, old_value, new_value, comment, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (issue.id, "field_cleared", "tester", "legacy-value", None, "", created_at),
        )
        db.conn.commit()

        summary = generate_summary(db)
        assert "FIELD CLEARED" in summary
        assert "legacy-value\u2192" in summary


class TestWriteSummary:
    def test_atomic_write(self, db: FiligreeDB, tmp_path: Path) -> None:
        output = tmp_path / "context.md"
        write_summary(db, output)
        assert output.exists()
        content = output.read_text()
        assert "Project Pulse" in content

    def test_overwrites_existing(self, db: FiligreeDB, tmp_path: Path) -> None:
        output = tmp_path / "context.md"
        output.write_text("old content")
        write_summary(db, output)
        content = output.read_text()
        assert "old content" not in content
        assert "Project Pulse" in content

    def test_no_temp_file_left(self, db: FiligreeDB, tmp_path: Path) -> None:
        output = tmp_path / "context.md"
        write_summary(db, output)
        tmp_file = output.with_suffix(".tmp")
        assert not tmp_file.exists()

    def test_temp_file_is_unique_per_call(self, db: FiligreeDB, tmp_path: Path) -> None:
        """Temp filenames must be unique to avoid races between concurrent writers."""
        import os
        from unittest.mock import patch

        output = tmp_path / "context.md"
        temp_paths: list[str] = []

        original_replace = os.replace

        def capture_replace(src: str, dst: str) -> None:
            temp_paths.append(src)
            return original_replace(src, dst)

        with patch("filigree.summary.os.replace", side_effect=capture_replace):
            write_summary(db, output)
            write_summary(db, output)

        # Two calls must use different temp paths
        assert len(temp_paths) == 2
        assert temp_paths[0] != temp_paths[1]

    def test_fd_closed_when_fdopen_fails(self, db: FiligreeDB, tmp_path: Path) -> None:
        """If os.fdopen fails, write_summary must close the raw fd and clean temp file."""
        import os
        from unittest.mock import patch

        output = tmp_path / "context.md"
        original_close = os.close
        closed_fds: list[int] = []

        def tracking_close(fd: int) -> None:
            closed_fds.append(fd)
            original_close(fd)

        with (
            patch("filigree.summary.os.fdopen", side_effect=OSError("fdopen failed")),
            patch("filigree.summary.os.close", side_effect=tracking_close),
            pytest.raises(OSError, match="fdopen failed"),
        ):
            write_summary(db, output)

        assert closed_fds, "Expected os.close to be called for leaked fd cleanup"
        leftovers = list(tmp_path.glob(".context_*.tmp"))
        assert leftovers == []


# ---------------------------------------------------------------------------
# Bug fixes: summary timezone handling, WIP limit
# (consolidated from test_analytics_templates_fixes.py)
# ---------------------------------------------------------------------------


class TestSummaryTimezoneHandling:
    def test_naive_datetime_gets_utc(self) -> None:
        """Naive datetime should get UTC attached via replace."""
        result = summary_parse_iso("2026-01-15T10:00:00")
        assert isinstance(result, datetime)
        assert result.tzinfo is not None
        assert result.tzinfo == UTC

    def test_aware_datetime_converted_to_utc(self) -> None:
        """Aware datetime with non-UTC timezone should be converted to UTC."""
        # +05:00 timezone
        result = summary_parse_iso("2026-01-15T15:00:00+05:00")
        assert isinstance(result, datetime)
        assert result.tzinfo is not None
        # 15:00 +05:00 should be 10:00 UTC
        assert result.hour == 10

    def test_utc_datetime_unchanged(self) -> None:
        """UTC datetime should remain unchanged."""
        result = summary_parse_iso("2026-01-15T10:00:00+00:00")
        assert isinstance(result, datetime)
        assert result.hour == 10
        assert result.tzinfo is not None

    def test_stale_detection_with_aware_datetimes(self, db: FiligreeDB) -> None:
        """Stale detection should work correctly with timezone-aware timestamps."""
        issue = db.create_issue("Stale task")
        db.update_issue(issue.id, status="in_progress")
        # Backdate to 5 days ago using a non-UTC timezone
        old_ts = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        db.conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (old_ts, issue.id))
        db.conn.commit()
        summary = generate_summary(db)
        assert "Stale" in summary
        assert "Stale task" in summary


class TestSummaryWipLimit:
    def test_list_issues_called_with_high_limit(self, db: FiligreeDB) -> None:
        """Verify that list_issues is called with a high limit in generate_summary."""
        # We patch list_issues to track calls with limit parameter
        original_list_issues = db.list_issues
        call_limits: list[int | None] = []

        def tracking_list_issues(**kwargs):  # type: ignore[no-untyped-def]
            call_limits.append(kwargs.get("limit"))
            return original_list_issues(**kwargs)

        with patch.object(db, "list_issues", side_effect=tracking_list_issues):
            generate_summary(db)

        # All calls should have limit=10000 (not the default 100)
        for limit in call_limits:
            assert limit == 10000, f"list_issues called with limit={limit}, expected 10000"


class TestSanitizeTitle:
    """Direct parametrized tests for the _sanitize_title security boundary."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("normal text", "normal text"),
            ("", ""),
            ("a\x00b", "ab"),
            ("\x07\x08\x0b\x0c\x7f", ""),
            ("a\r\nb", "a b"),
            ("a\rb", "a b"),
            ("a\nb", "a b"),
            ("a  b", "a b"),
            ("  leading  trailing  ", "leading trailing"),
            ("a" * 200, "a" * 200),
            ("a" * 201, "a" * 197 + "..."),
        ],
        ids=[
            "passthrough",
            "empty",
            "null-byte",
            "only-control-chars",
            "crlf-to-space",
            "cr-to-space",
            "lf-to-space",
            "multi-space-collapse",
            "leading-trailing-whitespace",
            "exactly-200-no-truncation",
            "over-200-truncated",
        ],
    )
    def test_sanitize_title(self, raw: str, expected: str) -> None:
        assert _sanitize_title(raw) == expected


class TestObservationsInSummary:
    def test_no_observations_no_mention(self, db: FiligreeDB) -> None:
        summary = generate_summary(db)
        assert "OBSERVATION" not in summary.upper()

    def test_fresh_observations_gentle_nudge(self, db: FiligreeDB) -> None:
        db.create_observation("Something to check")
        summary = generate_summary(db)
        assert "OBSERVATIONS:" in summary
        assert "1 pending" in summary
        assert "list_observations" in summary

    def test_stale_observations_warning(self, db: FiligreeDB) -> None:
        obs = db.create_observation("Old thing")
        db.conn.execute(
            "UPDATE observations SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (obs["id"],),
        )
        db.conn.commit()
        summary = generate_summary(db)
        assert "STALE OBSERVATIONS" in summary
        assert "1 observation(s) older than 48 hours" in summary

    def test_expiring_soon_mention(self, db: FiligreeDB) -> None:
        obs = db.create_observation("About to expire")
        soon = (datetime.now(UTC) + timedelta(hours=12)).isoformat()
        db.conn.execute(
            "UPDATE observations SET expires_at = ? WHERE id = ?",
            (soon, obs["id"]),
        )
        db.conn.commit()
        summary = generate_summary(db)
        assert "expiring within 24h" in summary

    def test_observation_stats_failure_is_silent(self, db: FiligreeDB) -> None:
        """If observation_stats() raises OperationalError (pre-v7 DB), summary still generates."""
        import sqlite3

        with patch.object(db, "observation_stats", side_effect=sqlite3.OperationalError("no such table: observations")):
            summary = generate_summary(db)
        assert "Project Pulse" in summary
        assert "OBSERVATION" not in summary.upper()
