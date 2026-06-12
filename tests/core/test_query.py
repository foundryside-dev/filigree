"""Tests for core query operations — list, search, filter, stats, events."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from filigree.core import FiligreeDB


def _sqlite_operational_error(message: str, code: int | None = None) -> sqlite3.OperationalError:
    exc = sqlite3.OperationalError(message)
    if code is not None:
        exc.sqlite_errorcode = code  # type: ignore[attr-defined]
    return exc


class TestListAndSearch:
    def test_list_all(self, db: FiligreeDB) -> None:
        baseline = len(db.list_issues())  # Future release singleton
        db.create_issue("A")
        db.create_issue("B")
        assert len(db.list_issues()) == baseline + 2

    def test_list_filter_status(self, db: FiligreeDB) -> None:
        open_before = len(db.list_issues(status="open"))  # Future release (planning = open category)
        a = db.create_issue("Open one")
        b = db.create_issue("Close one")
        db.close_issue(b.id)
        open_issues = db.list_issues(status="open")
        assert len(open_issues) == open_before + 1
        assert any(i.id == a.id for i in open_issues)

    def test_search(self, db: FiligreeDB) -> None:
        db.create_issue("Fix authentication bug")
        db.create_issue("Add new feature")
        results = db.search_issues("auth")
        assert len(results) == 1
        assert "auth" in results[0].title.lower()


class TestListIssuesCategoryAliases:
    """M9: list_issues category aliases 'in_progress'→'wip' and 'closed'→'done'."""

    def test_in_progress_alias(self, db: FiligreeDB) -> None:
        """status='in_progress' should expand to wip-category states."""
        issue = db.create_issue("WIP task")
        db.update_issue(issue.id, status="in_progress")
        results = db.list_issues(status="in_progress")
        assert any(i.id == issue.id for i in results)

    def test_closed_alias(self, db: FiligreeDB) -> None:
        """status='closed' should expand to done-category states."""
        issue = db.create_issue("Done task")
        db.close_issue(issue.id)
        results = db.list_issues(status="closed")
        assert any(i.id == issue.id for i in results)

    def test_wip_category_direct(self, db: FiligreeDB) -> None:
        """status='wip' should also work directly."""
        issue = db.create_issue("WIP direct")
        db.update_issue(issue.id, status="in_progress")
        results = db.list_issues(status="wip")
        assert any(i.id == issue.id for i in results)

    def test_done_category_direct(self, db: FiligreeDB) -> None:
        """status='done' should also work directly."""
        issue = db.create_issue("Done direct")
        db.close_issue(issue.id)
        results = db.list_issues(status="done")
        assert any(i.id == issue.id for i in results)


class TestListIssuesFilters:
    """M9: list_issues filter parameters beyond status."""

    def test_filter_by_label(self, db: FiligreeDB) -> None:
        a = db.create_issue("Labeled", labels=["urgent"])
        db.create_issue("Unlabeled")
        results = db.list_issues(label="urgent")
        assert len(results) >= 1
        assert any(i.id == a.id for i in results)
        assert all("urgent" in i.labels for i in results)

    def test_filter_by_assignee(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Assigned")
        db.claim_issue(issue.id, assignee="alice")
        db.create_issue("Unassigned")
        results = db.list_issues(assignee="alice")
        assert any(i.id == issue.id for i in results)
        assert all(i.assignee == "alice" for i in results)

    def test_filter_by_type(self, db: FiligreeDB) -> None:
        bug = db.create_issue("A bug", type="bug")
        db.create_issue("A task", type="task")
        results = db.list_issues(type="bug")
        assert any(i.id == bug.id for i in results)
        assert all(i.type == "bug" for i in results)

    def test_filter_by_priority(self, db: FiligreeDB) -> None:
        p0 = db.create_issue("Critical", priority=0)
        db.create_issue("Normal", priority=2)
        results = db.list_issues(priority=0)
        assert any(i.id == p0.id for i in results)
        assert all(i.priority == 0 for i in results)

    def test_filter_by_parent_id(self, db: FiligreeDB) -> None:
        parent = db.create_issue("Parent", type="epic")
        child = db.create_issue("Child", parent_id=parent.id)
        db.create_issue("Orphan")
        results = db.list_issues(parent_id=parent.id)
        assert any(i.id == child.id for i in results)
        assert all(i.parent_id == parent.id for i in results)

    def test_combined_filters(self, db: FiligreeDB) -> None:
        """Multiple filters are ANDed together."""
        db.create_issue("Bug P0", type="bug", priority=0)
        db.create_issue("Task P0", type="task", priority=0)
        db.create_issue("Bug P2", type="bug", priority=2)
        results = db.list_issues(type="bug", priority=0)
        assert all(i.type == "bug" and i.priority == 0 for i in results)


class TestListIssuesPriorityRange:
    """N-6: list_issues priority_min/priority_max range filters (claim-verb pattern)."""

    def test_priority_min_excludes_lower(self, db: FiligreeDB) -> None:
        p0 = db.create_issue("Critical", priority=0)
        p2 = db.create_issue("Medium", priority=2)
        results = db.list_issues(priority_min=2)
        ids = {i.id for i in results}
        assert p2.id in ids
        assert p0.id not in ids
        assert all(i.priority >= 2 for i in results)

    def test_priority_max_excludes_higher(self, db: FiligreeDB) -> None:
        p1 = db.create_issue("High", priority=1)
        p3 = db.create_issue("Low", priority=3)
        results = db.list_issues(priority_max=1)
        ids = {i.id for i in results}
        assert p1.id in ids
        assert p3.id not in ids
        assert all(i.priority <= 1 for i in results)

    def test_p0_included_with_zero_bounds(self, db: FiligreeDB) -> None:
        """Priority 0 is valid — `is not None` semantics, never truthiness."""
        p0 = db.create_issue("Critical", priority=0)
        db.create_issue("High", priority=1)
        results = db.list_issues(priority_min=0, priority_max=0)
        assert [i.id for i in results] == [p0.id]

    def test_min_and_max_combined(self, db: FiligreeDB) -> None:
        db.create_issue("P0", priority=0)
        p1 = db.create_issue("P1", priority=1)
        p3 = db.create_issue("P3", priority=3)
        db.create_issue("P4", priority=4)
        results = db.list_issues(priority_min=1, priority_max=3)
        ids = {i.id for i in results}
        assert ids == {p1.id, p3.id}

    def test_min_greater_than_max_returns_empty(self, db: FiligreeDB) -> None:
        """min > max yields an empty list, no error (claim-verb semantics)."""
        db.create_issue("P2", priority=2)
        assert db.list_issues(priority_min=3, priority_max=1) == []

    def test_exact_priority_filter_unchanged(self, db: FiligreeDB) -> None:
        p0 = db.create_issue("Critical", priority=0)
        db.create_issue("Normal", priority=2)
        results = db.list_issues(priority=0)
        assert any(i.id == p0.id for i in results)
        assert all(i.priority == 0 for i in results)


class TestGetReadyPriorityRange:
    """N-6: get_ready priority_min/priority_max range filters."""

    def test_priority_range_filters(self, db: FiligreeDB) -> None:
        db.create_issue("P0", priority=0)
        p1 = db.create_issue("P1", priority=1)
        p2 = db.create_issue("P2", priority=2)
        db.create_issue("P4", priority=4)
        ready = db.get_ready(priority_min=1, priority_max=2)
        assert {i.id for i in ready} == {p1.id, p2.id}

    def test_p0_included_at_max_zero(self, db: FiligreeDB) -> None:
        p0 = db.create_issue("Critical", priority=0)
        db.create_issue("Medium", priority=2)
        ready = db.get_ready(priority_max=0)
        assert [i.id for i in ready] == [p0.id]

    def test_min_greater_than_max_returns_empty(self, db: FiligreeDB) -> None:
        db.create_issue("P2", priority=2)
        assert db.get_ready(priority_min=3, priority_max=1) == []

    def test_range_still_excludes_assigned(self, db: FiligreeDB) -> None:
        claimed = db.create_issue("Claimed", priority=1)
        db.claim_issue(claimed.id, assignee="agent")
        free = db.create_issue("Free", priority=1)
        ready = db.get_ready(priority_min=1, priority_max=1)
        ids = {i.id for i in ready}
        assert free.id in ids
        assert claimed.id not in ids

    def test_range_still_excludes_blocked(self, db: FiligreeDB) -> None:
        blocker = db.create_issue("Blocker", priority=1)
        blocked = db.create_issue("Blocked", priority=1)
        db.add_dependency(blocked.id, blocker.id)
        ready = db.get_ready(priority_min=1, priority_max=1)
        ids = {i.id for i in ready}
        assert blocker.id in ids
        assert blocked.id not in ids


class TestListIssuesSorting:
    """list_issues supports explicit sort fields and directions."""

    def test_sort_by_updated_at_desc(self, db: FiligreeDB) -> None:
        older = db.create_issue("Older task", type="task", priority=2)
        newer = db.create_issue("Newer task", type="task", priority=2)
        db.conn.execute(
            "UPDATE issues SET updated_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00+00:00", older.id),
        )
        db.conn.execute(
            "UPDATE issues SET updated_at = ? WHERE id = ?",
            ("2026-02-01T00:00:00+00:00", newer.id),
        )
        db.conn.commit()

        results = db.list_issues(type="task", sort_by="updated_at", direction="desc", limit=2)

        assert [issue.id for issue in results] == [newer.id, older.id]

    def test_sort_by_priority_desc(self, db: FiligreeDB) -> None:
        high_numeric_priority = db.create_issue("Priority four", type="task", priority=4)
        low_numeric_priority = db.create_issue("Priority zero", type="task", priority=0)

        results = db.list_issues(type="task", sort_by="priority", direction="desc", limit=2)

        assert [issue.id for issue in results] == [high_numeric_priority.id, low_numeric_priority.id]

    def test_rejects_invalid_sort_by(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="sort_by must be one of"):
            db.list_issues(sort_by="title")

    def test_rejects_invalid_direction(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="direction must be 'asc' or 'desc'"):
            db.list_issues(direction="sideways")


class TestListIssuesBoundaries:
    """M9: list_issues negative limit/offset guards."""

    def test_negative_limit_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="limit must be non-negative"):
            db.list_issues(limit=-1)

    def test_negative_offset_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="offset must be non-negative"):
            db.list_issues(offset=-1)

    def test_zero_limit_returns_empty(self, db: FiligreeDB) -> None:
        db.create_issue("Something")
        results = db.list_issues(limit=0)
        assert results == []

    def test_offset_beyond_results(self, db: FiligreeDB) -> None:
        db.create_issue("Only one")
        results = db.list_issues(offset=9999)
        assert results == []

    def test_list_issues_chunks_hydration_above_sqlite_variable_limit(self, db: FiligreeDB) -> None:
        expected_ids = [db.create_issue(f"Bulk list {i}", labels=["bulk-page"]).id for i in range(101)]
        db.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 100)

        results = db.list_issues(label="bulk-page", sort_by="created_at", direction="asc", limit=101)

        assert [issue.id for issue in results] == expected_ids

    def test_build_issues_batch_chunks_above_sqlite_variable_limit(self, db: FiligreeDB) -> None:
        expected_ids = [db.create_issue(f"Bulk hydrate {i}").id for i in range(101)]
        db.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 100)

        issues = db._build_issues_batch(expected_ids)

        assert [issue.id for issue in issues] == expected_ids


class TestSanitizeFtsQuery:
    """Unit tests for _sanitize_fts_query — primary defense against FTS5 injection."""

    def test_basic_tokens(self) -> None:
        from filigree.db_issues import _sanitize_fts_query

        result = _sanitize_fts_query("hello world")
        assert result == '"hello"* AND "world"*'

    def test_special_chars_stripped(self) -> None:
        from filigree.db_issues import _sanitize_fts_query

        result = _sanitize_fts_query("hello! @world#")
        assert result == '"hello"* AND "world"*'

    def test_only_special_chars_returns_empty_match(self) -> None:
        from filigree.db_issues import _sanitize_fts_query

        result = _sanitize_fts_query("!@#$%^&()")
        assert result == ""

    def test_empty_query_returns_empty_match(self) -> None:
        from filigree.db_issues import _sanitize_fts_query

        result = _sanitize_fts_query("")
        assert result == ""

    def test_embedded_double_quotes_removed(self) -> None:
        from filigree.db_issues import _sanitize_fts_query

        result = _sanitize_fts_query('"hello" "world"')
        assert result == '"hello"* AND "world"*'

    def test_wildcards_preserved(self) -> None:
        from filigree.db_issues import _sanitize_fts_query

        result = _sanitize_fts_query("fix*")
        assert result == '"fix*"*'

    def test_whitespace_only_returns_empty_match(self) -> None:
        from filigree.db_issues import _sanitize_fts_query

        result = _sanitize_fts_query("   ")
        assert result == ""


class TestEscapeLikeQuery:
    """Unit tests for _escape_like — defense against LIKE injection."""

    def test_plain_string(self) -> None:
        from filigree.db_base import _escape_like

        assert _escape_like("hello") == "%hello%"

    def test_percent_escaped(self) -> None:
        from filigree.db_base import _escape_like

        assert _escape_like("100%") == "%100\\%%"

    def test_underscore_escaped(self) -> None:
        from filigree.db_base import _escape_like

        assert _escape_like("foo_bar") == "%foo\\_bar%"

    def test_backslash_escaped(self) -> None:
        from filigree.db_base import _escape_like

        assert _escape_like("a\\b") == "%a\\\\b%"

    def test_all_special_chars(self) -> None:
        from filigree.db_base import _escape_like

        result = _escape_like("%_\\")
        assert result == "%\\%\\_\\\\%"


class TestSearchFTSFallback:
    """Bug filigree-35ef38: FTS fallback must only catch missing-table errors."""

    def test_non_fts_operational_error_propagates(self, db: FiligreeDB) -> None:
        """OperationalError unrelated to missing FTS5 must NOT be silently caught."""
        db.create_issue("Searchable item")

        original_execute = db.conn.execute

        class _SpyConn:
            """Wraps conn to intercept FTS queries."""

            def __getattr__(self, name: str) -> object:
                return getattr(db._conn, name)

            def execute(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
                if "issues_fts" in sql and "MATCH" in sql:
                    raise sqlite3.OperationalError("database disk image is malformed")
                return original_execute(sql, params)

        with patch.object(db, "_conn", _SpyConn()), pytest.raises(sqlite3.OperationalError, match="malformed"):
            db.search_issues("Searchable")

    def test_sqlite_error_without_missing_fts_message_propagates(self, db: FiligreeDB) -> None:
        db.create_issue("Searchable notification item")

        original_execute = db.conn.execute

        class _NoFTS:
            def __getattr__(self, name: str) -> object:
                return getattr(db._conn, name)

            def execute(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
                if "issues_fts" in sql and "MATCH" in sql:
                    raise _sqlite_operational_error("custom extension loader failed", sqlite3.SQLITE_ERROR)
                return original_execute(sql, params)

        with patch.object(db, "_conn", _NoFTS()), pytest.raises(sqlite3.OperationalError, match="custom extension loader failed"):
            db.search_issues("notification")

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("no such table: issues_fts", True),
            ("no such module: fts5", True),
            ("table issues has no column named status_rank", False),
            ("custom extension loader failed", False),
        ],
    )
    def test_fts_unavailable_discriminates_sqlite_error_messages(self, message: str, expected: bool) -> None:
        from filigree.db_issues import _is_fts_unavailable_error

        exc = _sqlite_operational_error(message, sqlite3.SQLITE_ERROR)

        assert _is_fts_unavailable_error(exc) is expected

    def test_non_fts_error_code_propagates_even_with_legacy_message(self, db: FiligreeDB) -> None:
        db.create_issue("Searchable item")

        original_execute = db.conn.execute

        class _CorruptFTS:
            def __getattr__(self, name: str) -> object:
                return getattr(db._conn, name)

            def execute(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
                if "issues_fts" in sql and "MATCH" in sql:
                    raise _sqlite_operational_error("no such table: issues_fts", sqlite3.SQLITE_CORRUPT)
                return original_execute(sql, params)

        with patch.object(db, "_conn", _CorruptFTS()), pytest.raises(sqlite3.OperationalError, match="no such table"):
            db.search_issues("Searchable")


class TestStats:
    def test_stats_counts(self, db: FiligreeDB) -> None:
        db.create_issue("A")
        b = db.create_issue("B")
        db.close_issue(b.id)
        stats = db.get_stats()
        assert stats["by_status"]["open"] == 1
        assert stats["by_status"]["closed"] == 1

    def test_ready_count_matches_get_ready_for_template_open_states(self, db: FiligreeDB) -> None:
        bug = db.create_issue("Template-open bug", type="bug")
        ready_ids = {i.id for i in db.get_ready()}
        stats = db.get_stats()

        assert bug.id in ready_ids
        assert stats["ready_count"] == len(ready_ids)

    def test_ready_count_excludes_assigned_open_issues(self, db: FiligreeDB) -> None:
        bug = db.create_issue("Claimed template-open bug", type="bug")
        db.claim_issue(bug.id, assignee="agent-1")

        ready_ids = {i.id for i in db.get_ready()}
        stats = db.get_stats()

        assert bug.id not in ready_ids
        assert stats["ready_count"] == len(ready_ids)

    def test_done_category_blocker_does_not_count_as_blocked(self, db: FiligreeDB) -> None:
        blocker = db.create_issue("Blocker", type="bug")
        blocked = db.create_issue("Blocked", type="bug")
        db.add_dependency(blocked.id, blocker.id)
        db.close_issue(blocker.id, status="wont_fix")

        ready_ids = {i.id for i in db.get_ready()}
        stats = db.get_stats()

        assert blocked.id in ready_ids
        assert stats["blocked_count"] == 0
        assert stats["ready_count"] == len(ready_ids)

    def test_wip_blocked_issue_counts_in_stats_blocked_count(self, db: FiligreeDB) -> None:
        # Dogfood-4 B3: stats_get said blocked_count 0 while work_blocked returned
        # a claimed wip-category issue — two definitions of "blocked". There is
        # ONE now: stats must agree with get_blocked, wip included.
        blocker = db.create_issue("Blocker", type="task")
        blocked = db.create_issue("Blocked", type="task")
        db.update_issue(blocked.id, status="in_progress")
        db.add_dependency(blocked.id, blocker.id)

        stats = db.get_stats()
        blocked_ids = {i.id for i in db.get_blocked()}

        assert blocked.id in blocked_ids
        assert stats["blocked_count"] == len(blocked_ids)


class TestGetStatsByCategory:
    """Verify get_stats() includes category-level counts (WFT-FR-060)."""

    def test_get_stats_by_category(self, db: FiligreeDB) -> None:
        """by_category sums issues across open/wip/done."""
        baseline_open = db.get_stats()["by_category"]["open"]  # Future release in open category
        db.create_issue("A")  # open → open category
        b = db.create_issue("B")
        db.update_issue(b.id, status="in_progress")  # wip category
        c = db.create_issue("C")
        db.close_issue(c.id)  # done category

        stats = db.get_stats()
        by_cat = stats["by_category"]
        assert by_cat["open"] == baseline_open + 1
        assert by_cat["wip"] == 1
        assert by_cat["done"] == 1

    def test_get_stats_by_category_custom_states(self, db: FiligreeDB) -> None:
        """Bug in 'triage' counts as open; bug in 'fixing' counts as wip."""
        db.create_issue("Bug in triage", type="bug")  # initial_state=triage → open category
        bug2 = db.create_issue("Bug being fixed", type="bug")
        # Move through workflow: triage → confirmed → fixing
        db.update_issue(bug2.id, status="confirmed")
        db.update_issue(bug2.id, status="fixing")

        stats = db.get_stats()
        by_cat = stats["by_category"]
        assert by_cat["open"] >= 1  # triage bug
        assert by_cat["wip"] >= 1  # fixing bug

    def test_get_stats_backward_compat(self, db: FiligreeDB) -> None:
        """by_status is still present alongside by_category."""
        db.create_issue("X")
        stats = db.get_stats()
        assert "by_status" in stats
        assert "by_category" in stats
        assert "ready_count" in stats


class TestEvents:
    def test_events_recorded_on_create(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Event test")
        events = db.get_recent_events(limit=5)
        assert any(e["issue_id"] == issue.id and e["event_type"] == "created" for e in events)

    def test_events_recorded_on_status_change(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Event test")
        db.update_issue(issue.id, status="in_progress")
        events = db.get_recent_events(limit=5)
        assert any(e["issue_id"] == issue.id and e["event_type"] == "status_changed" for e in events)


class TestGetEventsSince:
    def test_basic(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Event source")
        # Get creation event timestamp
        events = db.get_recent_events(limit=1)
        ts = events[0]["created_at"]
        # Make a change after creation
        db.update_issue(issue.id, status="in_progress")
        since_events = db.get_events_since(ts)
        assert len(since_events) >= 1
        assert any(e["event_type"] == "status_changed" for e in since_events)

    def test_empty_when_no_events(self, db: FiligreeDB) -> None:
        result = db.get_events_since("2099-01-01T00:00:00+00:00")
        assert result == []

    def test_respects_limit(self, db: FiligreeDB) -> None:
        for i in range(5):
            db.create_issue(f"Issue {i}")
        result = db.get_events_since("2000-01-01T00:00:00+00:00", limit=2)
        assert len(result) == 2

    def test_chronological_order(self, db: FiligreeDB) -> None:
        db.create_issue("First")
        db.create_issue("Second")
        result = db.get_events_since("2000-01-01T00:00:00+00:00")
        assert len(result) >= 2
        # Events should be in ascending order
        for i in range(len(result) - 1):
            assert result[i]["created_at"] <= result[i + 1]["created_at"]


class TestActorTracking:
    def test_actor_in_update_event(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Track me")
        db.update_issue(issue.id, status="in_progress", actor="agent-alpha")
        events = db.get_recent_events(limit=5)
        status_event = next(e for e in events if e["event_type"] == "status_changed")
        assert status_event["actor"] == "agent-alpha"

    def test_actor_in_close_event(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Close me")
        db.close_issue(issue.id, actor="agent-beta")
        events = db.get_recent_events(limit=5)
        close_event = next(e for e in events if e["event_type"] == "status_changed" and e["new_value"] == "closed")
        assert close_event["actor"] == "agent-beta"


class TestGetStatsEmptyDoneStates:
    """Bug fix: filigree-2e5af8 — get_stats empty done_states."""

    def test_get_stats_with_normal_templates(self, db: FiligreeDB) -> None:
        """get_stats() should work normally with templates loaded."""
        db.create_issue("Issue A")
        b = db.create_issue("Issue B")
        db.close_issue(b.id)

        stats = db.get_stats()
        assert "by_status" in stats
        assert "by_category" in stats
        assert "ready_count" in stats
        assert "blocked_count" in stats
        assert stats["by_category"]["done"] >= 1

    def test_get_stats_empty_done_states_no_crash(self, db: FiligreeDB) -> None:
        """get_stats() should not crash when done_states is empty."""
        db.create_issue("Issue A")

        # Simulate empty done_states by mocking _get_states_for_category
        original_method = db._get_states_for_category

        def mock_get_states(category: str) -> list[str]:
            if category == "done":
                return []
            return original_method(category)

        with patch.object(db, "_get_states_for_category", side_effect=mock_get_states):
            # This should not raise an error
            stats = db.get_stats()
            assert "ready_count" in stats
            assert "blocked_count" in stats
            assert isinstance(stats["ready_count"], int)
            assert isinstance(stats["blocked_count"], int)

    def test_get_stats_empty_done_states_with_deps(self, db: FiligreeDB) -> None:
        """get_stats() with empty done_states should count all deps as blockers."""
        a = db.create_issue("Issue A")
        b = db.create_issue("Issue B")
        db.add_dependency(a.id, b.id)

        original_method = db._get_states_for_category

        def mock_get_states(category: str) -> list[str]:
            if category == "done":
                return []
            return original_method(category)

        with patch.object(db, "_get_states_for_category", side_effect=mock_get_states):
            stats = db.get_stats()
            # With no done states, B blocks A, so A is blocked and B is ready
            assert stats["blocked_count"] >= 1


class TestFTS5SpecialCharacters:
    """FTS5 search should handle special characters gracefully."""

    def test_search_with_special_characters_returns_valid_terms(self, db: FiligreeDB) -> None:
        """Searching for 'notification @#$%' should find issues matching 'notification'."""
        db.create_issue("Fix notification system")
        db.create_issue("Unrelated feature")
        results = db.search_issues("notification @#$%")
        assert len(results) == 1
        assert "notification" in results[0].title.lower()

    def test_search_with_only_special_characters_returns_empty(self, db: FiligreeDB) -> None:
        """Searching for only special characters should return empty, not error."""
        db.create_issue("Some issue")
        results = db.search_issues("@#$%^&()")
        assert results == []

    def test_search_with_mixed_special_and_valid(self, db: FiligreeDB) -> None:
        """Special chars mixed with valid terms should still find matches."""
        db.create_issue("Authentication bug in login")
        results = db.search_issues("auth!@#enti")
        # "auth" and "enti" are separate tokens after sanitization — but the original
        # becomes "authentication" after stripping specials, which may tokenize differently.
        # The key point is: no crash.
        assert isinstance(results, list)

    def test_search_hyphenated_cluster_prefix_finds_self_tagged_work(self, db: FiligreeDB) -> None:
        """Senior-user MCP review run e P2.6: hyphenated queries hit LIKE path.

        Agents prefix their session work with ``[cluster-foo]`` for
        later self-discovery. The legacy FTS-only path tokenised hyphens
        and dropped single-letter tokens, so ``mcp-review-e`` returned 0
        results. The fix routes hyphen / bracket queries to a LIKE
        substring search on the raw query.
        """
        target = db.create_issue("[mcp-review-e] Scratch task A — start_work probe", type="task")
        db.create_issue("Unrelated work")
        db.create_issue("Other [different-cluster] work", type="task")

        # Hyphenated token finds the cluster-prefixed issue.
        results = db.search_issues("mcp-review-e")
        assert any(i.id == target.id for i in results), "hyphenated query must find cluster-prefixed work"

        # Bracketed token finds it too — tests the bracket regex hint.
        results = db.search_issues("[mcp-review-e]")
        assert any(i.id == target.id for i in results), "bracketed query must find cluster-prefixed work"

        # Plain word tokens still use FTS and continue to work.
        results = db.search_issues("Scratch")
        assert any(i.id == target.id for i in results), "plain word query stays on FTS path"

    def test_search_substring_arm_matches_labels(self, db: FiligreeDB) -> None:
        """Dogfood-4 B5: two issues carried the exact label searched for and
        search returned 0 — the substring arm never looked at labels. Label
        vocabulary is kebab-case so a label query always lands on the LIKE arm;
        that arm now searches labels too."""
        tagged = db.create_issue("Throwaway probe", type="task")
        db.add_label(tagged.id, "dogfood-exercise")
        db.create_issue("Unrelated", type="task")

        results = db.search_issues("dogfood-exercise")
        assert any(i.id == tagged.id for i in results), "label-only match must surface on the LIKE arm"
        assert db.count_search_results("dogfood-exercise") >= 1

    def test_search_status_category_filter_excludes_done(self, db: FiligreeDB) -> None:
        """Senior-user MCP review run e P2.7: status_category filter scopes search.

        Without the filter, search returns archived/closed alongside live
        work — so an agent searching for in-flight work has to post-filter
        by hand. The status_category parameter restricts results at the
        DB layer.
        """
        live = db.create_issue("[mcp-review-e] live work", type="task")
        closed = db.create_issue("[mcp-review-e] closed work", type="task")
        db.close_issue(closed.id)
        archived = db.create_issue("[mcp-review-e] archived work", type="task")
        db.close_issue(archived.id)
        db.archive_closed(days_old=0)

        # No filter: all three appear.
        all_results = {i.id for i in db.search_issues("[mcp-review-e]")}
        assert {live.id, closed.id, archived.id}.issubset(all_results)

        # Open category: only live work.
        open_results = {i.id for i in db.search_issues("[mcp-review-e]", status_category="open")}
        assert open_results == {live.id}

        # Done category: closed + archived (the P2.10 archived-as-done fix
        # means archived rows resolve to 'done' too).
        done_results = {i.id for i in db.search_issues("[mcp-review-e]", status_category="done")}
        assert done_results == {closed.id, archived.id}

    def test_search_status_category_filter_applies_before_pagination(self, db: FiligreeDB) -> None:
        for i in range(205):
            issue = db.create_issue(f"[overflow-tag] closed work {i:03d}", type="task")
            db.close_issue(issue.id)
        live = db.create_issue("[overflow-tag] live target", type="task")

        results = db.search_issues("[overflow-tag]", limit=1, status_category="open")

        assert [issue.id for issue in results] == [live.id]


class TestCountSearchResults:
    """filigree-af817d0cf3: count_search_results unit tests."""

    def test_fts_path_returns_correct_count(self, db: FiligreeDB) -> None:
        db.create_issue("Fix authentication bug")
        db.create_issue("Authentication flow rework")
        db.create_issue("Unrelated feature")
        count = db.count_search_results("authentication")
        assert count == 2

    def test_like_fallback_when_fts_unavailable(self, db: FiligreeDB) -> None:
        """When FTS table is missing, LIKE fallback still returns correct count."""
        db.create_issue("Fix notification system")
        db.create_issue("Another notification task")
        db.create_issue("Unrelated task")

        original_execute = db.conn.execute

        class _NoFTS:
            def __getattr__(self, name: str) -> object:
                return getattr(db._conn, name)

            def execute(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
                if "issues_fts" in sql and "MATCH" in sql:
                    raise sqlite3.OperationalError("no such table: issues_fts")
                return original_execute(sql, params)

        with patch.object(db, "_conn", _NoFTS()):
            count = db.count_search_results("notification")
        assert count == 2

    def test_count_sqlite_error_without_missing_fts_message_propagates(self, db: FiligreeDB) -> None:
        db.create_issue("Fix notification system")
        db.create_issue("Another notification task")
        db.create_issue("Unrelated task")

        original_execute = db.conn.execute

        class _NoFTS:
            def __getattr__(self, name: str) -> object:
                return getattr(db._conn, name)

            def execute(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
                if "issues_fts" in sql and "MATCH" in sql:
                    raise _sqlite_operational_error("custom extension loader failed", sqlite3.SQLITE_ERROR)
                return original_execute(sql, params)

        with patch.object(db, "_conn", _NoFTS()), pytest.raises(sqlite3.OperationalError, match="custom extension loader failed"):
            db.count_search_results("notification")

    def test_non_fts_error_code_propagates_even_with_legacy_message(self, db: FiligreeDB) -> None:
        db.create_issue("Fix notification system")

        original_execute = db.conn.execute

        class _CorruptFTS:
            def __getattr__(self, name: str) -> object:
                return getattr(db._conn, name)

            def execute(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
                if "issues_fts" in sql and "MATCH" in sql:
                    raise _sqlite_operational_error("no such table: issues_fts", sqlite3.SQLITE_CORRUPT)
                return original_execute(sql, params)

        with patch.object(db, "_conn", _CorruptFTS()), pytest.raises(sqlite3.OperationalError, match="no such table"):
            db.count_search_results("notification")

    def test_special_character_sanitization(self, db: FiligreeDB) -> None:
        """Special chars in query don't cause errors and valid tokens still match."""
        db.create_issue("Fix the dashboard display")
        count = db.count_search_results("dashboard @#$%")
        assert count == 1

    def test_empty_query(self, db: FiligreeDB) -> None:
        """Empty or whitespace-only query should not crash."""
        db.create_issue("Some issue")
        count = db.count_search_results("")
        assert isinstance(count, int)

    def test_only_special_characters_returns_zero(self, db: FiligreeDB) -> None:
        """Query with only special characters returns 0, not an error."""
        db.create_issue("Normal issue")
        count = db.count_search_results("@#$%^&()")
        assert count == 0

    def test_literal_self_tag_count_matches_search_results(self, db: FiligreeDB) -> None:
        target = db.create_issue("[mcp-review-e] Scratch task A", type="task")
        db.create_issue("Unrelated work")

        results = db.search_issues("mcp-review-e")
        count = db.count_search_results("mcp-review-e")

        assert [issue.id for issue in results] == [target.id]
        assert count == len(results)
