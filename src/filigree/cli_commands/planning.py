"""CLI commands for planning: ready, blocked, plan, deps, critical-path, create-plan, changes."""

from __future__ import annotations

import contextlib
import json as json_mod
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, get_args

import click

from filigree.cli_common import get_db, refresh_summary
from filigree.issue_payloads import issue_to_ready
from filigree.types.api import ErrorCode
from filigree.types.events import EventType


def _emit_error(message: str, code: ErrorCode, as_json: bool) -> NoReturn:
    """Emit a CLI error in the right format for the caller's mode and exit 1.

    JSON mode uses the flat ``{error, code}`` envelope shared with the MCP
    server and dashboard; plain mode keeps the legacy ``Error: ...`` line so
    interactive output stays unchanged.
    """
    if as_json:
        click.echo(json_mod.dumps({"error": message, "code": code}))
    else:
        click.echo(f"Error: {message}", err=True)
    sys.exit(1)


def _normalize_iso_timestamp(raw: str, as_json: bool) -> str:
    """Normalize user-supplied ISO-8601 to the form stored in the DB.

    Stored timestamps use ``datetime.now(UTC).isoformat()`` which always
    emits ``+00:00``. SQLite compares TEXT lexically, so any non-UTC
    offset (or trailing ``Z``) would miscompare against ``+00:00`` rows.
    Naive input is treated as UTC (matching the CLI's stored convention).
    Rejects unparseable input with a SystemExit+stderr (or JSON envelope
    if ``as_json``) message — not a silent miscomparison.
    """
    candidate = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except (ValueError, TypeError):
        _emit_error(
            f"Invalid ISO timestamp: {raw!r}. Expected format: 2026-01-15T10:30:00 or 2026-01-15T10:30:00+00:00",
            ErrorCode.VALIDATION,
            as_json,
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _validate_plan_dep_refs(deps: object, label: str) -> str | None:
    """Reject dep values that ``db_planning.create_plan`` would silently misinterpret.

    The DB layer does ``str(dep_ref)`` and treats any string containing ``"."``
    as ``"phase_idx.step_idx"``. A JSON float like ``0.1`` would resolve to
    phase 0 step 1; a bool would hit ``int('True')`` and raise. Mirrors the
    MCP-layer rules in ``mcp_tools/planning.py::_validate_plan_deps``.
    """
    if not isinstance(deps, list):
        return f"{label} 'deps' must be a list, got {type(deps).__name__}"
    for k, dep in enumerate(deps):
        ref = f"{label}, dep[{k}]"
        if isinstance(dep, bool):
            return f"{ref} must be integer or 'P.S' string, not bool"
        if isinstance(dep, int):
            if dep < 0:
                return f"{ref} must be >= 0, got {dep}"
            continue
        if isinstance(dep, str):
            parts = dep.split(".")
            if len(parts) > 2 or any(not p.lstrip("-").isdigit() for p in parts):
                return f"{ref} must be 'N' or 'P.S' with integer components, got {dep!r}"
            continue
        return f"{ref} must be integer or 'P.S' string, got {type(dep).__name__}"
    return None


def _parent_titles_by_id(db: Any, issues: list[Any]) -> dict[str, str]:
    parent_ids = sorted({i.parent_id for i in issues if i.parent_id})
    titles: dict[str, str] = {}
    for parent_id in parent_ids:
        with contextlib.suppress(KeyError):
            titles[parent_id] = db.get_issue(parent_id).title
    return titles


def _ready_impl(as_json: bool, include_context: bool) -> None:
    with get_db() as db:
        issues = db.get_ready()

        if as_json:
            parent_titles = _parent_titles_by_id(db, issues) if include_context else {}
            click.echo(
                json_mod.dumps(
                    {
                        "items": [
                            issue_to_ready(
                                i,
                                include_context=include_context,
                                parent_title=parent_titles.get(i.parent_id or ""),
                            )
                            for i in issues
                        ],
                        "has_more": False,
                    },
                    indent=2,
                    default=str,
                )
            )
            return

        for issue in issues:
            parent_ctx = ""
            if issue.parent_id:
                try:
                    parent = db.get_issue(issue.parent_id)
                    parent_ctx = f" ({parent.title})"
                except KeyError:
                    pass
            click.echo(f'P{issue.priority} {issue.id} [{issue.type}] "{issue.title}"{parent_ctx}')
        click.echo(f"\n{len(issues)} ready")


@click.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("--include-context", is_flag=True, help="Include parent_issue_id and parent_title in JSON output")
def ready(as_json: bool, include_context: bool) -> None:
    """Show issues ready to work on (no blockers)."""
    _ready_impl(as_json, include_context)


@click.command("get-ready")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("--include-context", is_flag=True, help="Include parent_issue_id and parent_title in JSON output")
def get_ready(as_json: bool, include_context: bool) -> None:
    """Show issues ready to work on (no blockers). Alias for `ready`."""
    _ready_impl(as_json, include_context)


def _blocked_impl(as_json: bool) -> None:
    with get_db() as db:
        issues = db.get_blocked()

        if as_json:
            click.echo(
                json_mod.dumps(
                    {
                        "items": [
                            {
                                "issue_id": i.id,
                                "title": i.title,
                                "status": i.status,
                                "priority": i.priority,
                                "type": i.type,
                                "blocked_by": i.blocked_by,
                            }
                            for i in issues
                        ],
                        "has_more": False,
                    },
                    indent=2,
                    default=str,
                )
            )
            return

        for issue in issues:
            blockers = ", ".join(issue.blocked_by)
            click.echo(f'P{issue.priority} {issue.id} [{issue.type}] "{issue.title}" <- {blockers}')
        click.echo(f"\n{len(issues)} blocked")


@click.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def blocked(as_json: bool) -> None:
    """Show blocked issues."""
    _blocked_impl(as_json)


@click.command("get-blocked")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def get_blocked(as_json: bool) -> None:
    """Show blocked issues. Alias for `blocked`."""
    _blocked_impl(as_json)


def _plan_impl(milestone_id: str, as_json: bool) -> None:
    with get_db() as db:
        try:
            p = db.get_plan(milestone_id)
        except KeyError:
            _emit_error(f"Not found: {milestone_id}", ErrorCode.NOT_FOUND, as_json)

        if as_json:
            click.echo(json_mod.dumps(p, indent=2, default=str))
            return

        ms = p["milestone"]
        total = p["total_steps"]
        done = p["completed_steps"]
        click.echo(f"Milestone: {ms['title']} ({done}/{total} steps complete)")
        click.echo()

        for phase_data in p["phases"]:
            phase = phase_data["phase"]
            p_total = phase_data["total"]
            p_done = phase_data["completed"]

            if p_done == p_total and p_total > 0:
                marker = "[DONE]"
            elif phase["status_category"] == "wip":
                marker = "[WIP] "
            else:
                marker = "[    ]"

            click.echo(f"  {marker} {phase['title']} ({p_done}/{p_total})")

            # Markers keyed on status_category so custom packs and the built-in
            # planning workflow (pending/in_progress/completed) both render correctly.
            status_icon = {"open": " ", "wip": ">", "done": "x"}
            for step_dict in phase_data["steps"]:
                icon = status_icon.get(step_dict["status_category"], "?")
                ready_mark = " *" if step_dict["is_ready"] else ""
                click.echo(f"    [{icon}] {step_dict['id']} {step_dict['title']}{ready_mark}")


@click.command()
@click.argument("milestone_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def plan(milestone_id: str, as_json: bool) -> None:
    """Show milestone plan tree with progress."""
    _plan_impl(milestone_id, as_json)


@click.command("get-plan")
@click.argument("milestone_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def get_plan(milestone_id: str, as_json: bool) -> None:
    """Show milestone plan tree with progress. Alias for `plan`."""
    _plan_impl(milestone_id, as_json)


@click.command("add-dep")
@click.argument("issue_id")
@click.argument("depends_on_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def add_dep(ctx: click.Context, issue_id: str, depends_on_id: str, as_json: bool) -> None:
    """Add dependency: issue_id depends on depends_on_id."""
    with get_db() as db:
        try:
            added = db.add_dependency(issue_id, depends_on_id, actor=ctx.obj["actor"])
            status = "added" if added else "already_exists"
            if as_json:
                click.echo(json_mod.dumps({"from_id": issue_id, "to_id": depends_on_id, "status": status}))
            else:
                if added:
                    click.echo(f"Added: {issue_id} depends on {depends_on_id}")
                else:
                    click.echo(f"Already exists: {issue_id} depends on {depends_on_id}")
        except KeyError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Not found: {e}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Not found: {e}", err=True)
            sys.exit(1)
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        refresh_summary(db)


@click.command("remove-dep")
@click.argument("issue_id")
@click.argument("depends_on_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def remove_dep(ctx: click.Context, issue_id: str, depends_on_id: str, as_json: bool) -> None:
    """Remove dependency."""
    with get_db() as db:
        try:
            removed = db.remove_dependency(issue_id, depends_on_id, actor=ctx.obj["actor"])
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        status = "removed" if removed else "not_found"
        if as_json:
            click.echo(json_mod.dumps({"from_id": issue_id, "to_id": depends_on_id, "status": status}))
        else:
            if removed:
                click.echo(f"Removed: {issue_id} no longer depends on {depends_on_id}")
            else:
                click.echo(f"No dependency found: {issue_id} -> {depends_on_id}")
        refresh_summary(db)


def _critical_path_impl(as_json: bool) -> None:
    with get_db() as db:
        path = db.get_critical_path()

    if as_json:
        click.echo(json_mod.dumps({"path": path, "length": len(path)}, indent=2))
        return

    if not path:
        click.echo("No dependency chains found.")
        return

    click.echo(f"Critical path ({len(path)} issues):")
    for i, item in enumerate(path):
        prefix = "  -> " if i > 0 else "  "
        click.echo(f'{prefix}P{item["priority"]} {item["id"]} [{item["type"]}] "{item["title"]}"')


@click.command("critical-path")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
def critical_path(as_json: bool) -> None:
    """Show the longest dependency chain among open issues."""
    _critical_path_impl(as_json)


@click.command("get-critical-path")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
def get_critical_path(as_json: bool) -> None:
    """Show the longest dependency chain among open issues. Alias for `critical-path`."""
    _critical_path_impl(as_json)


@click.command("create-plan")
@click.option("--file", "file_path", default=None, type=click.Path(), help="JSON file (stdin if omitted)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def create_plan(ctx: click.Context, file_path: str | None, as_json: bool) -> None:
    """Create a milestone/phase/step hierarchy from JSON.

    Reads JSON from --file or stdin. Structure:
    {"milestone": {"title": "..."}, "phases": [{"title": "...", "steps": [...]}]}
    """
    try:
        raw = Path(file_path).read_text() if file_path else click.get_text_stream("stdin").read()
    except (OSError, UnicodeDecodeError) as e:
        _emit_error(f"reading file: {e}", ErrorCode.IO, as_json)

    try:
        data = json_mod.loads(raw)
    except json_mod.JSONDecodeError as e:
        _emit_error(f"Invalid JSON: {e}", ErrorCode.VALIDATION, as_json)

    if not isinstance(data, dict):
        _emit_error("JSON must be an object, not a list or scalar", ErrorCode.VALIDATION, as_json)

    if "milestone" not in data or "phases" not in data:
        _emit_error("JSON must contain 'milestone' and 'phases' keys", ErrorCode.VALIDATION, as_json)

    if not isinstance(data["milestone"], dict):
        _emit_error("'milestone' must be an object with at least a 'title' key", ErrorCode.VALIDATION, as_json)

    if not isinstance(data["phases"], list):
        _emit_error("'phases' must be a list of phase objects", ErrorCode.VALIDATION, as_json)

    # Title must be a string at every level — db_planning calls .strip() on it,
    # which would otherwise raise AttributeError (e.g. for JSON numbers/bools).
    if not isinstance(data["milestone"].get("title"), str):
        _emit_error(
            f"Milestone 'title' must be a string, got {type(data['milestone'].get('title')).__name__}",
            ErrorCode.VALIDATION,
            as_json,
        )

    for i, phase in enumerate(data["phases"]):
        if not isinstance(phase, dict):
            _emit_error(
                f"Phase {i + 1} must be an object, got {type(phase).__name__}",
                ErrorCode.VALIDATION,
                as_json,
            )
        if not isinstance(phase.get("title"), str):
            _emit_error(
                f"Phase {i + 1} 'title' must be a string, got {type(phase.get('title')).__name__}",
                ErrorCode.VALIDATION,
                as_json,
            )
        steps = phase.get("steps", [])
        if not isinstance(steps, list):
            _emit_error(
                f"Phase {i + 1} 'steps' must be a list, got {type(steps).__name__}",
                ErrorCode.VALIDATION,
                as_json,
            )
        for j, step in enumerate(steps):
            if not isinstance(step, dict):
                _emit_error(
                    f"Phase {i + 1}, Step {j + 1} must be an object, got {type(step).__name__}",
                    ErrorCode.VALIDATION,
                    as_json,
                )
            if not isinstance(step.get("title"), str):
                _emit_error(
                    f"Phase {i + 1}, Step {j + 1} 'title' must be a string, got {type(step.get('title')).__name__}",
                    ErrorCode.VALIDATION,
                    as_json,
                )
            err = _validate_plan_dep_refs(step.get("deps", []), f"Phase {i + 1}, Step {j + 1}")
            if err is not None:
                _emit_error(err, ErrorCode.VALIDATION, as_json)

    with get_db() as db:
        try:
            result = db.create_plan(data["milestone"], data["phases"], actor=ctx.obj["actor"])  # type: ignore[arg-type]
        except (ValueError, TypeError) as e:
            # Narrowed from a 4-exception tuple that included IndexError and
            # AttributeError: both of those indicate a bug (missing fields
            # would raise KeyError before reaching create_plan; attribute
            # access on validated JSON dicts is programmer error). Let them
            # crash so the bug is visible, rather than being misclassified
            # as a validation error.
            _emit_error(str(e), ErrorCode.VALIDATION, as_json)

        if as_json:
            click.echo(json_mod.dumps(result, indent=2, default=str))
        else:
            ms = result["milestone"]
            click.echo(f"Created plan: {ms['title']} ({ms['id']})")
            for phase_data in result["phases"]:
                phase = phase_data["phase"]
                step_count = len(phase_data["steps"])
                click.echo(f"  Phase: {phase['title']} ({step_count} steps)")
        refresh_summary(db)


def _validate_changes_filters(label: str | None, event_type: str | None, as_json: bool) -> None:
    if label is not None:
        label = label.strip()
        if not label:
            _emit_error("label cannot be empty", ErrorCode.VALIDATION, as_json)
        if any(ord(c) < 32 or c == "\x7f" for c in label):
            _emit_error("label contains control characters", ErrorCode.VALIDATION, as_json)
    if event_type is not None and event_type not in get_args(EventType):
        _emit_error(f"Invalid event type: {event_type}", ErrorCode.VALIDATION, as_json)


def _changes_impl(
    since: str,
    limit: int,
    after_event_id: int | None,
    as_json: bool,
    *,
    actor: str | None = None,
    issue_id: str | None = None,
    label: str | None = None,
    event_type: str | None = None,
    include_heartbeats: bool = False,
) -> None:
    since = _normalize_iso_timestamp(since, as_json)
    _validate_changes_filters(label, event_type, as_json)
    label_filter = label.strip() if label is not None else None
    exclude_types: list[str] = []
    if not include_heartbeats and event_type != "heartbeat":
        exclude_types.append("heartbeat")
    with get_db() as db:
        # Overfetch by 1 to detect has_more without an offset param.
        raw = db.get_events_since(
            since,
            after_event_id=after_event_id,
            limit=limit + 1 if limit > 0 else limit,
            actor=actor,
            issue_id=issue_id,
            label=label_filter,
            event_type=event_type,
            exclude_types=exclude_types or None,
        )
        has_more = limit > 0 and len(raw) > limit
        events = raw[:limit] if has_more else raw

        if as_json:
            payload = {
                "items": events,
                "has_more": has_more,
                "next_since": events[-1]["created_at"] if events else since,
                "next_event_id": events[-1]["id"] if events else after_event_id,
            }
            click.echo(json_mod.dumps(payload, indent=2, default=str))
            return

        if not events:
            click.echo("No events since that timestamp.")
            return

        for ev in events:
            title = ev.get("issue_title", "")
            actor_str = f" by {ev['actor']}" if ev.get("actor") else ""
            click.echo(f"  {ev['created_at']}  {ev['event_type']:<12} {ev['issue_id']}  {title}{actor_str}")
        click.echo(f"\n{len(events)} events")


@click.command("changes")
@click.option("--since", required=True, help="ISO timestamp to get events after")
@click.option(
    "--limit",
    default=100,
    type=click.IntRange(min=1),
    help="Max events (default 100, must be >= 1)",
)
@click.option("--after-event-id", default=None, type=click.IntRange(min=0), help="Resume after this event id when --since ties")
@click.option("--actor", default=None, help="Only include events written by this actor")
@click.option("--issue-id", default=None, help="Only include events for this issue")
@click.option("--label", default=None, help="Only include events for issues currently carrying this label")
@click.option("--type", "event_type", default=None, help="Only include events of this event type")
@click.option("--include-heartbeats", is_flag=True, help="Include heartbeat events (excluded by default)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def changes(
    since: str,
    limit: int,
    after_event_id: int | None,
    actor: str | None,
    issue_id: str | None,
    label: str | None,
    event_type: str | None,
    include_heartbeats: bool,
    as_json: bool,
) -> None:
    """Get events since a timestamp (for session resumption)."""
    _changes_impl(
        since,
        limit,
        after_event_id,
        as_json,
        actor=actor,
        issue_id=issue_id,
        label=label,
        event_type=event_type,
        include_heartbeats=include_heartbeats,
    )


@click.command("get-changes")
@click.option("--since", required=True, help="ISO timestamp to get events after")
@click.option(
    "--limit",
    default=100,
    type=click.IntRange(min=1),
    help="Max events (default 100, must be >= 1)",
)
@click.option("--after-event-id", default=None, type=click.IntRange(min=0), help="Resume after this event id when --since ties")
@click.option("--actor", default=None, help="Only include events written by this actor")
@click.option("--issue-id", default=None, help="Only include events for this issue")
@click.option("--label", default=None, help="Only include events for issues currently carrying this label")
@click.option("--type", "event_type", default=None, help="Only include events of this event type")
@click.option("--include-heartbeats", is_flag=True, help="Include heartbeat events (excluded by default)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def get_changes(
    since: str,
    limit: int,
    after_event_id: int | None,
    actor: str | None,
    issue_id: str | None,
    label: str | None,
    event_type: str | None,
    include_heartbeats: bool,
    as_json: bool,
) -> None:
    """Get events since a timestamp (for session resumption). Alias for `changes`."""
    _changes_impl(
        since,
        limit,
        after_event_id,
        as_json,
        actor=actor,
        issue_id=issue_id,
        label=label,
        event_type=event_type,
        include_heartbeats=include_heartbeats,
    )


def register(cli: click.Group) -> None:
    """Register planning commands with the CLI group."""
    cli.add_command(ready)
    cli.add_command(get_ready)
    cli.add_command(blocked)
    cli.add_command(get_blocked)
    cli.add_command(plan)
    cli.add_command(get_plan)
    cli.add_command(add_dep)
    cli.add_command(remove_dep)
    cli.add_command(critical_path)
    cli.add_command(get_critical_path)
    cli.add_command(create_plan)
    cli.add_command(changes)
    cli.add_command(get_changes)
