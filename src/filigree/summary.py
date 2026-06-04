"""Pre-computed summary generator for agent context.

Reads the filigree DB and generates a compact markdown summary (~80-120 lines)
that agents can read in a single file read at session start.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from filigree.core import FiligreeDB
from filigree.models import Issue

logger = logging.getLogger(__name__)

STALE_THRESHOLD_DAYS = 3
SUMMARY_FETCH_LIMIT = 100
SUMMARY_RENDER_LIMIT = 12
SUMMARY_BLOCKED_RENDER_LIMIT = 10
SUMMARY_EPIC_RENDER_LIMIT = 10


class _MalformedTimestamp:
    """Sentinel for unparseable timestamps (typed alternative to bare object())."""

    __slots__ = ()


_MALFORMED_TIMESTAMP = _MalformedTimestamp()

# Matches C0/C1 control characters except tab/newline (which we handle separately)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _sanitize_title(text: str) -> str:
    """Sanitize untrusted text for safe markdown interpolation.

    Strips control characters, collapses newlines/carriage returns to spaces,
    and truncates to a reasonable length.
    """
    text = _CONTROL_CHARS_RE.sub("", text)
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    # Collapse multiple spaces
    text = " ".join(text.split())
    # Truncate overly long titles
    if len(text) > 200:
        text = text[:197] + "..."
    return text


def _parse_iso(ts: str) -> datetime | _MalformedTimestamp:
    """Parse an ISO timestamp, handling timezone-aware and naive formats.

    Returns a UTC-aware datetime on success. Naive datetimes get UTC attached;
    aware datetimes are converted to UTC via astimezone (not just replace).
    Returns a malformed sentinel when parsing fails.
    """
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (ValueError, TypeError):
        return _MALFORMED_TIMESTAMP


def generate_summary(db: FiligreeDB) -> str:
    """Generate the context.md summary from current DB state."""
    now = datetime.now(UTC)
    now_iso = now.isoformat(timespec="seconds")
    stats = db.get_stats()
    ready = db.get_ready()
    blocked = db.get_blocked()
    # Use wip category to capture all work-in-progress states (fixing, verifying, etc.)
    in_progress = db.list_issues(status="wip", limit=SUMMARY_FETCH_LIMIT)
    recent = db.get_recent_events(limit=10)

    lines: list[str] = []
    lines.append(f"# Project Pulse (auto-generated {now_iso})")
    lines.append("")

    # Batch-fetch parent titles to avoid N+1 queries in render loops
    parent_ids: set[str] = set()
    for issue in ready:
        if issue.parent_id:
            parent_ids.add(issue.parent_id)
    for issue in in_progress:
        if issue.parent_id:
            parent_ids.add(issue.parent_id)
    parent_titles: dict[str, str] = {}
    if parent_ids:
        # Chunk to stay within SQLite's SQLITE_MAX_VARIABLE_NUMBER limit
        ids_list = list(parent_ids)
        chunk_size = 500
        for i in range(0, len(ids_list), chunk_size):
            chunk = ids_list[i : i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            rows = db.conn.execute(
                f"SELECT id, title FROM issues WHERE id IN ({placeholders})",  # noqa: S608
                chunk,
            ).fetchall()
            for r in rows:
                parent_titles[r["id"]] = _sanitize_title(r["title"])

    # Vitals use category counts (open/wip/done) instead of literal status names
    by_cat = stats.get("by_category", {})
    open_count = by_cat.get("open", 0)
    wip_count = by_cat.get("wip", 0)
    done_count = by_cat.get("done", 0)
    ready_count = stats["ready_count"]
    blocked_count = stats["blocked_count"]

    lines.append("## Vitals")
    lines.append(f"Open: {open_count} | In Progress: {wip_count} | Done: {done_count} | Ready: {ready_count} | Blocked: {blocked_count}")
    lines.append("")

    # -- Active Plans (milestones)
    milestones = db.list_issues(type="milestone", status="open", limit=SUMMARY_FETCH_LIMIT)
    milestones += db.list_issues(type="milestone", status="wip", limit=SUMMARY_FETCH_LIMIT)
    if milestones:
        lines.append("## Active Plans")
        for ms in milestones:
            plan = db.get_plan(ms.id)
            total = plan["total_steps"]
            done = plan["completed_steps"]
            if total > 0:
                bar_filled = int((done / total) * 10)
                bar = "\u2588" * bar_filled + "\u2591" * (10 - bar_filled)
            else:
                bar = "\u2591" * 10
            lines.append(f"### {_sanitize_title(ms.title)} [{bar}] {done}/{total} steps")

            for phase_data in plan["phases"]:
                phase = phase_data["phase"]
                p_total = phase_data["total"]
                p_done = phase_data["completed"]
                p_ready = phase_data["ready"]

                if p_done == p_total and p_total > 0:
                    marker = "\u2713"
                elif phase["status_category"] == "wip":
                    marker = "\u25b6"
                else:
                    marker = "\u25cb"

                ready_note = f", {p_ready} ready" if p_ready > 0 else ""
                lines.append(f"  {marker} {_sanitize_title(phase['title'])} ({p_done}/{p_total} complete{ready_note})")

            lines.append("")

    # -- Ready to Work (limit 12)
    lines.append("## Ready to Work (no blockers, by priority)")
    if ready:
        for issue in ready[:12]:
            parent_ctx = ""
            if issue.parent_id and issue.parent_id in parent_titles:
                parent_ctx = f" ({parent_titles[issue.parent_id]})"
            # Show state in parens when it differs from the default "open"
            state_info = f" ({issue.status})" if issue.status != "open" else ""
            title = _sanitize_title(issue.title)
            lines.append(f'- P{issue.priority} {issue.id} [{issue.type}] "{title}"{state_info}{parent_ctx}')
        if len(ready) > 12:
            lines.append(f"  ...and {len(ready) - 12} more")
    else:
        lines.append("- (none)")
    lines.append("")

    # -- In Progress (all wip-category states)
    lines.append("## In Progress")
    if in_progress:
        for issue in in_progress[:SUMMARY_RENDER_LIMIT]:
            parent_ctx = ""
            if issue.parent_id and issue.parent_id in parent_titles:
                parent_ctx = f" ({parent_titles[issue.parent_id]})"
            # Show state in parens when it differs from the default "in_progress"
            state_info = f" ({issue.status})" if issue.status != "in_progress" else ""
            lines.append(f'- {issue.id} [{issue.type}] "{_sanitize_title(issue.title)}"{state_info}{parent_ctx}')
        omitted_wip = max(wip_count, len(in_progress)) - SUMMARY_RENDER_LIMIT
        if omitted_wip > 0:
            lines.append(f"  ...and {omitted_wip} more in progress. Run `issue_list status=wip` for the full set.")
    else:
        lines.append("- (none)")
    lines.append("")

    # Needs Attention — wip issues with missing required fields
    needs_attention: list[tuple[Issue, list[str]]] = []
    for issue in in_progress:
        missing = db.templates.validate_fields_for_state(issue.type, issue.status, issue.fields)
        if missing:
            needs_attention.append((issue, missing))
    if needs_attention:
        lines.append("## Needs Attention")
        for attn_issue, missing_fields in needs_attention[:8]:
            lines.append(
                f'- {attn_issue.id} [{attn_issue.type}] "{_sanitize_title(attn_issue.title)}" ({attn_issue.status})'
                f" — missing: {', '.join(missing_fields)}"
            )
        if len(needs_attention) > 8:
            lines.append(f"  ...and {len(needs_attention) - 8} more")
        lines.append("")

    # -- Stale (wip-category >3 days with no activity)
    stale_cutoff = now - timedelta(days=STALE_THRESHOLD_DAYS)
    stale: list[tuple[Issue, datetime | _MalformedTimestamp]] = []
    for issue in in_progress:
        parsed_updated = _parse_iso(issue.updated_at)
        if isinstance(parsed_updated, _MalformedTimestamp) or parsed_updated < stale_cutoff:
            stale.append((issue, parsed_updated))
    if stale:
        lines.append("## Stale (in_progress >3 days, no activity)")
        for issue, parsed_updated in stale[:SUMMARY_RENDER_LIMIT]:
            if isinstance(parsed_updated, _MalformedTimestamp):
                marker = _sanitize_title(str(issue.updated_at))
                line = f'- P{issue.priority} {issue.id} [{issue.type}] "{_sanitize_title(issue.title)}" (malformed updated_at: {marker})'
            else:
                days_ago = (now - parsed_updated).days
                line = f'- P{issue.priority} {issue.id} [{issue.type}] "{_sanitize_title(issue.title)}" ({days_ago}d stale)'
            lines.append(line)
        if len(stale) > SUMMARY_RENDER_LIMIT:
            lines.append(f"  ...and {len(stale) - SUMMARY_RENDER_LIMIT} more stale. Run `issue_list status=wip` for the full set.")
        lines.append("")

    # -- Blocked (top 10)
    lines.append("## Blocked (top 10 by priority)")
    if blocked:
        for issue in blocked[:SUMMARY_BLOCKED_RENDER_LIMIT]:
            blockers_str = ", ".join(issue.blocked_by) if issue.blocked_by else "?"
            title = _sanitize_title(issue.title)
            line = f'- P{issue.priority} {issue.id} [{issue.type}] "{title}" \u2190 blocked by: {blockers_str}'
            lines.append(line)
        if len(blocked) > SUMMARY_BLOCKED_RENDER_LIMIT:
            lines.append(f"  ...and {len(blocked) - SUMMARY_BLOCKED_RENDER_LIMIT} more")
    else:
        lines.append("- (none)")
    lines.append("")

    # -- Epic Progress (limit 10; use status_category for done/open checks)
    epics = db.list_issues(type="epic", limit=SUMMARY_FETCH_LIMIT)
    open_epics = [e for e in epics if e.status_category != "done"]
    if open_epics:
        lines.append("## Epic Progress")
        for epic in open_epics[:SUMMARY_EPIC_RENDER_LIMIT]:
            children = db.list_issues(parent_id=epic.id, limit=SUMMARY_FETCH_LIMIT)
            total = len(children)
            done = sum(1 for c in children if c.status_category == "done")
            ready_c = sum(1 for c in children if c.is_ready)
            blocked_c = sum(1 for c in children if not c.is_ready and c.status_category == "open")

            if total > 0:
                bar_filled = int((done / total) * 8)
                bar = "\u2588" * bar_filled + "\u2591" * (8 - bar_filled)
            else:
                bar = "\u2591" * 8

            extra = []
            if ready_c:
                extra.append(f"{ready_c} ready")
            if blocked_c:
                extra.append(f"{blocked_c} blocked")
            extra_str = f" ({', '.join(extra)})" if extra else ""

            lines.append(f"- {_sanitize_title(epic.title):<40} [{bar}] {done}/{total}{extra_str}")
        lines.append("")

    # -- Critical Path
    crit_path = db.get_critical_path()
    if crit_path:
        lines.append(f"## Critical Path ({len(crit_path)} issues)")
        for i, item in enumerate(crit_path):
            arrow = " -> " if i > 0 else ""
            title = _sanitize_title(item["title"])
            lines.append(f'  {arrow}P{item["priority"]} {item["id"]} [{item["type"]}] "{title}"')
        lines.append("")

    # -- Recent Activity
    lines.append("## Recent Activity (last 10 events)")
    if recent:
        for evt in recent:
            evt_type = evt["event_type"].upper().replace("_", " ")
            title = _sanitize_title(evt.get("issue_title", evt["issue_id"]))
            # Sanitize event values — may contain untrusted titles or field data
            old_v = _sanitize_title(evt["old_value"] or "")
            new_v = _sanitize_title(evt["new_value"] or "")
            if len(old_v) > 50:
                old_v = old_v[:47] + "..."
            if len(new_v) > 50:
                new_v = new_v[:47] + "..."
            detail = ""
            if old_v and new_v:
                detail = f" {old_v}\u2192{new_v}"
            elif old_v:
                detail = f" {old_v}\u2192"
            elif new_v:
                detail = f" {new_v}"
            lines.append(f'- {evt_type} {evt["issue_id"]} "{title}"{detail}')
    else:
        lines.append("- (no recent activity)")
    lines.append("")

    # Observations (read-only — sweep=False to avoid write side effects on a read path)
    try:
        obs_stats = db.observation_stats(sweep=False)
        if obs_stats["count"] > 0:
            if obs_stats["stale_count"] > 0:
                stale_n = obs_stats["stale_count"]
                oldest_h = obs_stats["oldest_hours"]
                age_str = f"{oldest_h:.0f}h ago" if oldest_h is not None else "unknown"
                lines.append(f"STALE OBSERVATIONS: {stale_n} observation(s) older than 48 hours (oldest: {age_str})")
                lines.append(f"  Total pending: {obs_stats['count']}. Run `observation_list` to review.")
            else:
                oldest_h = obs_stats["oldest_hours"]
                age_str = f"{oldest_h:.0f}h ago" if oldest_h is not None else "unknown"
                lines.append(f"OBSERVATIONS: {obs_stats['count']} pending (oldest: {age_str})")
                lines.append("  Use `observation_list` to review, `observation_promote` to create issues,")
                lines.append("  or `observation_dismiss` to clear.")
            if obs_stats["expiring_soon_count"] > 0:
                lines.append(f"  ({obs_stats['expiring_soon_count']} expiring within 24h)")
            lines.append("")
    except sqlite3.OperationalError:
        logger.debug("observation stats unavailable in summary", exc_info=True)

    return "\n".join(lines)


def write_summary(db: FiligreeDB, output_path: str | Path) -> None:
    """Generate and write the summary atomically (write-temp then rename)."""
    summary = generate_summary(db)
    output = Path(output_path)
    fd, tmp_name = tempfile.mkstemp(dir=output.parent, suffix=".tmp", prefix=".context_")
    try:
        try:
            f = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        with f:
            f.write(summary)
        os.replace(tmp_name, str(output))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
