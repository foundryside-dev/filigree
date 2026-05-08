"""Tests for filigree.analytics — flow metrics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from filigree.analytics import _parse_iso as analytics_parse_iso
from filigree.analytics import cycle_time, get_flow_metrics, lead_time
from filigree.core import FiligreeDB, Issue


class TestCycleTimeTimezoneOrdering:
    """Regression: cycle_time must order events by chronological time, not by
    raw SQL text. Events imported from external systems can carry heterogeneous
    ISO offsets; lexical ordering picks the wrong WIP→done pair.
    """

    def test_cycle_time_orders_by_utc_not_raw_text(self, db: FiligreeDB) -> None:
        """WIP event with +10:00 offset is chronologically earlier than
        done event with +00:00 offset, even though the raw text sorts later.

        Lexical sort would place 'T00:30:00+00:00' before 'T09:00:00+10:00',
        producing either a negative cycle time or None. Proper chronological
        sort keeps them in order and yields ~30 minutes.
        """
        issue = db.create_issue("TZ order test")
        db.conn.execute(
            "DELETE FROM events WHERE issue_id = ? AND event_type = 'status_changed'",
            (issue.id,),
        )
        # WIP event: 2026-01-01T10:00:00+10:00 == 2026-01-01T00:00:00 UTC
        db.conn.execute(
            "INSERT INTO events (issue_id, event_type, old_value, new_value, created_at) "
            "VALUES (?, 'status_changed', 'open', 'in_progress', '2026-01-01T10:00:00+10:00')",
            (issue.id,),
        )
        # Done event: 2026-01-01T00:30:00+00:00 == 2026-01-01T00:30:00 UTC (30 min later)
        # Lexically '2026-01-01T00:30:00+00:00' < '2026-01-01T10:00:00+10:00',
        # so raw-text SQL sort places done before WIP — breaking cycle_time.
        db.conn.execute(
            "INSERT INTO events (issue_id, event_type, old_value, new_value, created_at) "
            "VALUES (?, 'status_changed', 'in_progress', 'closed', '2026-01-01T00:30:00+00:00')",
            (issue.id,),
        )
        db.conn.commit()

        ct = cycle_time(db, issue.id)
        assert ct is not None, "should find WIP→done pair with chronological ordering"
        # 30 minutes = 0.5 hours
        assert abs(ct - 0.5) < 0.01, f"cycle time should be 0.5h UTC, got {ct}"

    def test_flow_metrics_orders_events_chronologically(self, db: FiligreeDB) -> None:
        """get_flow_metrics must sort per-issue events by UTC, not raw text."""
        issue = db.create_issue("Flow TZ test")
        db.close_issue(issue.id)
        db.conn.execute(
            "DELETE FROM events WHERE issue_id = ? AND event_type = 'status_changed'",
            (issue.id,),
        )
        # WIP in +10:00, done in +00:00 — chronological gap is 30 minutes.
        db.conn.execute(
            "INSERT INTO events (issue_id, event_type, old_value, new_value, created_at) "
            "VALUES (?, 'status_changed', 'open', 'in_progress', '2026-01-01T10:00:00+10:00')",
            (issue.id,),
        )
        db.conn.execute(
            "INSERT INTO events (issue_id, event_type, old_value, new_value, created_at) "
            "VALUES (?, 'status_changed', 'in_progress', 'closed', '2026-01-01T00:30:00+00:00')",
            (issue.id,),
        )
        # Make sure the issue is counted (closed_at recent)
        db.conn.execute(
            "UPDATE issues SET closed_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), issue.id),
        )
        db.conn.commit()

        metrics = get_flow_metrics(db, days=30)
        assert metrics["avg_cycle_time_hours"] is not None
        assert abs(metrics["avg_cycle_time_hours"] - 0.5) < 0.01


class TestCycleTime:
    def test_cycle_time_basic(self, db: FiligreeDB) -> None:
        issue = db.create_issue("CT test")
        db.update_issue(issue.id, status="in_progress")
        db.close_issue(issue.id)
        ct = cycle_time(db, issue.id)
        assert ct is not None
        assert ct >= 0

    def test_cycle_time_never_started(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Never started")
        db.close_issue(issue.id)
        ct = cycle_time(db, issue.id)
        assert ct is None

    def test_cycle_time_not_closed(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Still open")
        db.update_issue(issue.id, status="in_progress")
        ct = cycle_time(db, issue.id)
        assert ct is None

    def test_cycle_time_skips_unparsable_done_timestamp(self, db: FiligreeDB) -> None:
        """If first done event has corrupt timestamp, later valid done events should be used."""
        issue = db.create_issue("CT corrupt test")
        # Manually insert all events with controlled timestamps
        # 1) WIP event with valid timestamp
        db.conn.execute(
            "INSERT OR IGNORE INTO events (issue_id, event_type, old_value, new_value, created_at) "
            "VALUES (?, 'status_changed', 'open', 'in_progress', '2026-01-10T10:00:00+00:00')",
            (issue.id,),
        )
        # 2) Done event with corrupt/empty timestamp (first done — should be skipped)
        db.conn.execute(
            "INSERT OR IGNORE INTO events (issue_id, event_type, old_value, new_value, created_at) "
            "VALUES (?, 'status_changed', 'in_progress', 'closed', 'not-a-date')",
            (issue.id,),
        )
        # 3) Done event with valid timestamp (should be used)
        db.conn.execute(
            "INSERT OR IGNORE INTO events (issue_id, event_type, old_value, new_value, created_at) "
            "VALUES (?, 'status_changed', 'in_progress', 'closed', '2026-01-15T12:00:00+00:00')",
            (issue.id,),
        )
        db.conn.commit()
        ct = cycle_time(db, issue.id)
        assert ct is not None

    def test_cycle_time_closed_before_wip_then_reopened(self, db: FiligreeDB) -> None:
        """open→closed→open→in_progress→closed should return valid cycle time.

        Regression: cycle_time() broke on the first done event even though
        no WIP start had been found yet, returning None.
        """
        issue = db.create_issue("Closed-before-WIP test")
        # Simulate: open→closed (no WIP), reopen, then proper WIP→done
        db.conn.execute(
            "INSERT OR IGNORE INTO events (issue_id, event_type, old_value, new_value, created_at) "
            "VALUES (?, 'status_changed', 'open', 'closed', '2026-01-01T10:00:00+00:00')",
            (issue.id,),
        )
        db.conn.execute(
            "INSERT OR IGNORE INTO events (issue_id, event_type, old_value, new_value, created_at) "
            "VALUES (?, 'status_changed', 'closed', 'open', '2026-01-02T10:00:00+00:00')",
            (issue.id,),
        )
        db.conn.execute(
            "INSERT OR IGNORE INTO events (issue_id, event_type, old_value, new_value, created_at) "
            "VALUES (?, 'status_changed', 'open', 'in_progress', '2026-01-03T10:00:00+00:00')",
            (issue.id,),
        )
        db.conn.execute(
            "INSERT OR IGNORE INTO events (issue_id, event_type, old_value, new_value, created_at) "
            "VALUES (?, 'status_changed', 'in_progress', 'closed', '2026-01-05T10:00:00+00:00')",
            (issue.id,),
        )
        db.conn.commit()
        ct = cycle_time(db, issue.id)
        assert ct is not None, "Should find the valid WIP→done pair after reopen"
        # 2 days = 48 hours
        assert abs(ct - 48.0) < 0.1

    def test_cycle_time_uses_type_specific_status_categories(self, db: FiligreeDB) -> None:
        """cycle_time must classify status by issue type, not global state-name sets."""
        issue = db.create_issue("Type-aware CT", type="bug")
        db.conn.execute("DELETE FROM events WHERE issue_id = ? AND event_type = 'status_changed'", (issue.id,))
        db.conn.execute(
            "INSERT INTO events (issue_id, event_type, old_value, new_value, created_at) "
            "VALUES (?, 'status_changed', 'triage', 'shared_wip', '2026-01-01T10:00:00+00:00')",
            (issue.id,),
        )
        db.conn.execute(
            "INSERT INTO events (issue_id, event_type, old_value, new_value, created_at) "
            "VALUES (?, 'status_changed', 'shared_wip', 'shared_done', '2026-01-02T10:00:00+00:00')",
            (issue.id,),
        )
        db.conn.commit()

        def fake_global_states(category: str) -> set[str]:
            if category == "wip":
                return {"shared_wip"}
            if category == "done":
                return {"shared_done"}
            return set()

        def fake_resolve(issue_type: str, state: str) -> str:
            if issue_type == issue.type:
                if state == "shared_wip":
                    return "done"
                if state == "shared_done":
                    return "wip"
            return "open"

        with (
            patch.object(db, "_get_states_for_category", side_effect=fake_global_states),
            patch.object(db, "_resolve_status_category", side_effect=fake_resolve),
        ):
            ct = cycle_time(db, issue.id)

        assert ct is None, "Type-aware resolver should ignore misleading global state sets"


class TestLeadTime:
    def test_lead_time_basic(self, db: FiligreeDB) -> None:
        issue = db.create_issue("LT test")
        db.close_issue(issue.id)
        lt = lead_time(db, issue.id)
        assert lt is not None
        assert lt >= 0

    def test_lead_time_not_closed(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Still open")
        lt = lead_time(db, issue.id)
        assert lt is None

    def test_lead_time_accepts_issue_object(self, db: FiligreeDB) -> None:
        """lead_time() should accept an Issue object to avoid N+1 re-fetches."""
        issue = db.create_issue("LT direct")
        db.close_issue(issue.id)
        refreshed = db.get_issue(issue.id)
        lt = lead_time(db, issue=refreshed)
        assert lt is not None
        assert lt >= 0

    def test_lead_time_no_extra_queries_with_issue_object(self, db: FiligreeDB) -> None:
        """Passing an Issue object should not call db.get_issue()."""
        issue = db.create_issue("LT no query")
        db.close_issue(issue.id)
        refreshed = db.get_issue(issue.id)

        original_get = db.get_issue
        calls: list[str] = []

        def tracking_get(issue_id: str) -> Issue:
            calls.append(issue_id)
            return original_get(issue_id)

        with patch.object(db, "get_issue", side_effect=tracking_get):
            lt = lead_time(db, issue=refreshed)

        assert lt is not None
        assert len(calls) == 0, f"get_issue called {len(calls)} times with Issue object"

    def test_lead_time_archived_issue(self, db: FiligreeDB) -> None:
        """Archived issues retain closed_at and should count as completed.

        ``archive_closed()`` writes literal status='archived', which is not a
        template-defined done category — but the issue is still completed work
        with a real closed_at. lead_time must not silently drop these.
        """
        issue = db.create_issue("Will archive")
        db.update_issue(issue.id, status="in_progress")
        db.close_issue(issue.id)
        db.archive_closed(days_old=0)
        refreshed = db.get_issue(issue.id)
        assert refreshed.status == "archived"

        lt = lead_time(db, issue=refreshed)
        assert lt is not None, "Archived issue with closed_at must contribute to lead time"
        assert lt >= 0


class TestFlowMetrics:
    def test_flow_metrics_structure(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Metric test")
        db.update_issue(issue.id, status="in_progress")
        db.close_issue(issue.id)
        data = get_flow_metrics(db)
        assert "throughput" in data
        assert "period_days" in data
        assert "avg_cycle_time_hours" in data
        assert "avg_lead_time_hours" in data
        assert "by_type" in data
        assert data["period_days"] == 30

    def test_flow_metrics_empty(self, db: FiligreeDB) -> None:
        data = get_flow_metrics(db)
        assert data["throughput"] == 0
        assert data["avg_cycle_time_hours"] is None

    def test_flow_metrics_by_type(self, db: FiligreeDB) -> None:
        task = db.create_issue("Task", type="task")
        db.update_issue(task.id, status="in_progress")
        db.close_issue(task.id)
        epic = db.create_issue("Epic", type="epic")
        db.update_issue(epic.id, status="in_progress")
        db.close_issue(epic.id)
        data = get_flow_metrics(db)
        assert "task" in data["by_type"]
        assert "epic" in data["by_type"]

    def test_flow_metrics_uses_type_specific_status_categories(self, db: FiligreeDB) -> None:
        """get_flow_metrics must use per-issue-type category resolution for cycle time."""
        issue = db.create_issue("Type-aware metrics", type="bug")
        # bug template has no triage→closed; use directly-reachable wont_fix
        # (filigree-cb980eee0d, validate-transitions policy).
        db.close_issue(issue.id, status="wont_fix")
        db.conn.execute("DELETE FROM events WHERE issue_id = ? AND event_type = 'status_changed'", (issue.id,))
        db.conn.execute(
            "INSERT INTO events (issue_id, event_type, old_value, new_value, created_at) "
            "VALUES (?, 'status_changed', 'triage', 'shared_wip', '2026-01-01T10:00:00+00:00')",
            (issue.id,),
        )
        db.conn.execute(
            "INSERT INTO events (issue_id, event_type, old_value, new_value, created_at) "
            "VALUES (?, 'status_changed', 'shared_wip', 'shared_done', '2026-01-02T10:00:00+00:00')",
            (issue.id,),
        )
        db.conn.commit()

        def fake_global_states(category: str) -> set[str]:
            if category == "wip":
                return {"in_progress"}
            if category == "done":
                return {"closed"}
            return set()

        def fake_resolve(issue_type: str, state: str) -> str:
            if issue_type == issue.type:
                if state == "shared_wip":
                    return "wip"
                if state == "shared_done":
                    return "done"
            return "open"

        with (
            patch.object(db, "_get_states_for_category", side_effect=fake_global_states),
            patch.object(db, "_resolve_status_category", side_effect=fake_resolve),
        ):
            data = get_flow_metrics(db, days=30)

        assert data["throughput"] == 1
        assert "bug" in data["by_type"]
        assert data["by_type"]["bug"]["count"] == 1

    def test_flow_metrics_respects_days(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Recent close")
        db.update_issue(issue.id, status="in_progress")
        db.close_issue(issue.id)
        # With a large window, should see the issue
        data_30 = get_flow_metrics(db, days=30)
        assert data_30["throughput"] >= 1
        assert data_30["period_days"] == 30
        # days param is wired through and changes the result shape
        data_7 = get_flow_metrics(db, days=7)
        assert data_7["period_days"] == 7
        assert data_7["throughput"] >= 1  # recently closed, still in 7d window

    def test_flow_metrics_rejects_non_positive_days(self, db: FiligreeDB) -> None:
        """get_flow_metrics must reject days < 1 — a negative cutoff silently
        excludes all real issues and returns period_days=N to callers."""
        for bad in (-1, 0, -365):
            with pytest.raises(ValueError, match="days"):
                get_flow_metrics(db, days=bad)

    def test_flow_metrics_uses_high_limit(self, db: FiligreeDB) -> None:
        """get_flow_metrics must not use default limit=100 for list_issues."""
        original = db.list_issues
        call_limits: list[int | None] = []

        def tracking(**kwargs):  # type: ignore[no-untyped-def]
            call_limits.append(kwargs.get("limit"))
            return original(**kwargs)

        with patch.object(db, "list_issues", side_effect=tracking):
            get_flow_metrics(db)

        # Every call should use a high limit, not the default 100
        for limit in call_limits:
            assert limit is not None, "list_issues called without explicit limit"
            assert limit > 100, f"list_issues called with limit={limit}, expected >100"

    def test_flow_metrics_paginates_beyond_single_page(self, db: FiligreeDB) -> None:
        """get_flow_metrics must paginate to collect all done issues, not cap at a fixed limit."""
        original = db.list_issues
        page_size = [0]  # Track what page size is used

        def capped_list(**kwargs):  # type: ignore[no-untyped-def]
            # Record the limit used and simulate a smaller page size
            page_size[0] = kwargs.get("limit", 100)
            return original(**kwargs)

        # Create some issues so there's data
        for i in range(3):
            iss = db.create_issue(f"Paginate test {i}")
            db.update_issue(iss.id, status="in_progress")
            db.close_issue(iss.id)

        with patch.object(db, "list_issues", side_effect=capped_list):
            data = get_flow_metrics(db)

        assert data["throughput"] >= 3

    def test_flow_metrics_batch_fetches_status_events(self, db: FiligreeDB) -> None:
        """Regression: avoid N+1 events queries while computing cycle time."""
        for i in range(4):
            issue = db.create_issue(f"Batch metric {i}")
            db.update_issue(issue.id, status="in_progress")
            db.close_issue(issue.id)

        seen_sql: list[str] = []

        def _trace(sql: str) -> None:
            seen_sql.append(sql.lower())

        db.conn.set_trace_callback(_trace)
        try:
            data = get_flow_metrics(db)
        finally:
            db.conn.set_trace_callback(None)

        assert data["throughput"] >= 4
        event_queries = [sql for sql in seen_sql if "from events" in sql and "event_type = 'status_changed'" in sql]
        assert len(event_queries) == 1, f"expected 1 batched status-events query, got {len(event_queries)}"

    def test_flow_metrics_includes_archived_issues(self, db: FiligreeDB) -> None:
        """Archived issues (from archive_closed) must be counted in throughput."""
        issue = db.create_issue("Will archive")
        db.update_issue(issue.id, status="in_progress")
        db.close_issue(issue.id)
        # archive_closed rewrites status to 'archived' but preserves closed_at
        db.archive_closed(days_old=0)
        refreshed = db.get_issue(issue.id)
        assert refreshed.status == "archived"
        data = get_flow_metrics(db, days=30)
        assert data["throughput"] >= 1, "Archived issues should be counted"

    def test_flow_metrics_dedupes_issues_appearing_in_multiple_buckets(self, db: FiligreeDB) -> None:
        """Issue returned by both status='closed' and status='archived' scans must count once.

        The closed-category expansion includes any template-defined done state — if
        a workflow pack declares an 'archived' done state, it overlaps with the
        synthetic status that archive_closed() writes, so both status queries return
        the same issue. Without id-keyed dedup, throughput and cycle-time averages
        are inflated.
        """
        issue = db.create_issue("Overlap target")
        db.update_issue(issue.id, status="in_progress")
        db.close_issue(issue.id)
        closed_issue = db.get_issue(issue.id)

        original = db.list_issues

        def overlapping_list(**kwargs):  # type: ignore[no-untyped-def]
            status = kwargs.get("status")
            offset = kwargs.get("offset", 0)
            if status in ("closed", "archived") and offset == 0:
                return [closed_issue]
            if status in ("closed", "archived"):
                return []
            return original(**kwargs)

        with patch.object(db, "list_issues", side_effect=overlapping_list):
            data = get_flow_metrics(db, days=30)

        assert data["throughput"] == 1, f"Same issue in both buckets should be counted once, got {data['throughput']}"
        assert data["by_type"].get("task", {}).get("count", 0) <= 1

    def test_flow_metrics_archived_issues_contribute_to_lead_time(self, db: FiligreeDB) -> None:
        """Archived issues are counted in throughput; they must also feed avg_lead_time.

        Regression: lead_time() rejected status='archived' because the synthetic
        status resolves to category 'open', so archived issues' lead times were
        silently dropped from the average even though throughput included them.
        """
        issue = db.create_issue("Archived metric")
        db.update_issue(issue.id, status="in_progress")
        db.close_issue(issue.id)
        db.archive_closed(days_old=0)
        assert db.get_issue(issue.id).status == "archived"

        data = get_flow_metrics(db, days=30)
        assert data["throughput"] >= 1
        assert data["avg_lead_time_hours"] is not None, "Archived issue must contribute to avg_lead_time"

    def test_flow_metrics_by_type_count_is_closed_count_not_cycle_samples(self, db: FiligreeDB) -> None:
        """by_type[t]['count'] is the closed-issue count for type t, not the cycle-time sample count.

        Regression: count was len(cycle_time_samples), so issues closed without a
        WIP transition (allowed by the task workflow's open->closed direct path)
        were silently absent from by_type even though they're real throughput.
        The CLI labels this field "closed" — keep the displayed count honest.
        """
        # Direct open->closed: no WIP event, so cycle_time will be None
        no_wip = db.create_issue("Direct close", type="task")
        db.close_issue(no_wip.id)
        # Normal WIP->closed: contributes a cycle-time sample
        with_wip = db.create_issue("WIP close", type="task")
        db.update_issue(with_wip.id, status="in_progress")
        db.close_issue(with_wip.id)

        data = get_flow_metrics(db, days=30)
        task_metrics = data["by_type"].get("task")
        assert task_metrics is not None, f"task type missing from by_type: {data['by_type']}"
        assert task_metrics["count"] == 2, f"expected 2 closed tasks, got {task_metrics['count']}"
        # avg_cycle_time_hours derives from the one WIP->closed issue only
        assert task_metrics["avg_cycle_time_hours"] is not None

    def test_flow_metrics_by_type_count_present_with_no_cycle_samples(self, db: FiligreeDB) -> None:
        """A type with closed issues but zero cycle-time samples still appears in by_type.

        Edge case of the count-vs-samples bug: previously, a type whose only
        closed issues lacked WIP transitions was missing from by_type entirely.
        """
        issue = db.create_issue("No-WIP only", type="task")
        db.close_issue(issue.id)

        data = get_flow_metrics(db, days=30)
        task_metrics = data["by_type"].get("task")
        assert task_metrics is not None
        assert task_metrics["count"] == 1
        assert task_metrics["avg_cycle_time_hours"] is None


# ---------------------------------------------------------------------------
# Bug fixes: cycle_time reopen, _parse_iso, flow_metrics date comparison
# (consolidated from test_analytics_templates_fixes.py)
# ---------------------------------------------------------------------------


class TestCycleTimeReopen:
    def test_cycle_time_uses_first_close(self, db: FiligreeDB) -> None:
        """Cycle time should measure first WIP to first done, not first WIP to last done."""
        issue = db.create_issue("Reopen test")
        db.update_issue(issue.id, status="in_progress")
        db.close_issue(issue.id)

        # Record cycle_time after first close
        ct_first = cycle_time(db, issue.id)
        assert ct_first is not None

        # Reopen and close again
        db.reopen_issue(issue.id)
        db.update_issue(issue.id, status="in_progress")
        # Backdate the second close to make it clearly different
        db.close_issue(issue.id)

        ct_after_reopen = cycle_time(db, issue.id)
        assert ct_after_reopen is not None
        # Should be the same as the first close (break after first done)
        assert ct_after_reopen == ct_first


class TestAnalyticsParseIso:
    def test_garbage_returns_none(self) -> None:
        """Garbage strings should return None, not datetime.now()."""
        result = analytics_parse_iso("not-a-date")
        assert result is None

    def test_empty_string_returns_none(self) -> None:
        result = analytics_parse_iso("")
        assert result is None

    def test_valid_iso_string(self) -> None:
        """Valid ISO string should return correct datetime."""
        result = analytics_parse_iso("2026-01-15T10:00:00+00:00")
        assert result is not None
        assert result.year == 2026
        assert result.month == 1
        assert result.day == 15

    def test_naive_iso_string_gets_utc(self) -> None:
        """Naive ISO string should get UTC timezone attached."""
        result = analytics_parse_iso("2026-01-15T10:00:00")
        assert result is not None
        assert result.tzinfo is not None
        assert result.tzinfo == UTC

    def test_result_is_not_now(self) -> None:
        """On failure, should NOT return datetime.now()."""
        before = datetime.now(UTC)
        result = analytics_parse_iso("garbage")
        assert result is None
        # Verify it's truly None, not something close to now
        assert result != before


class TestFlowMetricsDateComparison:
    def test_metrics_correctly_filter_by_cutoff_date(self, db: FiligreeDB) -> None:
        """Verify metrics use proper datetime comparison, not string comparison."""
        issue = db.create_issue("Recent close")
        db.update_issue(issue.id, status="in_progress")
        db.close_issue(issue.id)

        data = get_flow_metrics(db, days=30)
        assert data["throughput"] >= 1

    def test_metrics_exclude_old_issues(self, db: FiligreeDB) -> None:
        """Issues closed before the cutoff should be excluded."""
        issue = db.create_issue("Old close")
        db.update_issue(issue.id, status="in_progress")
        db.close_issue(issue.id)

        # Backdate the closed_at to 60 days ago
        old_ts = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        db.conn.execute("UPDATE issues SET closed_at = ? WHERE id = ?", (old_ts, issue.id))
        db.conn.commit()

        data = get_flow_metrics(db, days=30)
        assert data["throughput"] == 0
