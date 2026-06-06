"""CLI commands for metadata: comments, labels, stats, search, events, batch ops."""

from __future__ import annotations

import json as json_mod
import sqlite3
import sys
from typing import Any

import click

from filigree.cli_common import ActorCommand, get_db, refresh_summary
from filigree.core import WrongProjectError
from filigree.issue_payloads import issue_to_public
from filigree.label_payloads import label_namespace_from_public, label_namespace_item_to_public, label_namespace_to_public
from filigree.mcp_tools.payloads import comment_to_mcp, event_to_mcp
from filigree.types.api import ClaimConflictError, ErrorCode, ErrorResponse, claim_conflict_envelope


def _value_error_envelope(exc: ValueError) -> ErrorResponse:
    if isinstance(exc, ClaimConflictError):
        return claim_conflict_envelope(exc)
    return ErrorResponse(error=str(exc), code=ErrorCode.VALIDATION)


@click.command("add-comment", cls=ActorCommand)
@click.argument("issue_id")
@click.argument("text")
@click.option("--expected-assignee", default=None, help="Expected current holder for coordinator writes")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def add_comment(ctx: click.Context, issue_id: str, text: str, expected_assignee: str | None, as_json: bool) -> None:
    """Add a comment to an issue."""
    with get_db() as db:
        # Prefix check first — db.get_issue() ignores prefix on reads, so a
        # foreign-project ID would otherwise be reported as NOT_FOUND, masking
        # the WrongProjectError the write would raise. See filigree-f8861115a9.
        try:
            db._check_id_prefix(issue_id)
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        try:
            db.get_issue(issue_id)
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Not found: {issue_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Not found: {issue_id}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        try:
            comment_id = db.add_comment(issue_id, text, author=ctx.obj["actor"], expected_assignee=expected_assignee)
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps(_value_error_envelope(e)))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        if as_json:
            try:
                comment = db.get_comment(comment_id)
            except sqlite3.Error as e:
                click.echo(json_mod.dumps({"error": f"Database error: {e}", "code": ErrorCode.IO}))
                sys.exit(1)
            click.echo(
                json_mod.dumps(
                    {
                        "comment_id": comment_id,
                        "issue_id": issue_id,
                        "comment": comment_to_mcp(comment),
                    },
                    default=str,
                )
            )
        else:
            click.echo(f"Added comment {comment_id} to {issue_id}")
        refresh_summary(db)


@click.command("get-comments")
@click.argument("issue_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def get_comments(issue_id: str, as_json: bool) -> None:
    """List comments on an issue."""
    with get_db() as db:
        try:
            db.get_issue(issue_id)
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Not found: {issue_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Not found: {issue_id}", err=True)
            sys.exit(1)
        result = db.get_comments(issue_id)
        if as_json:
            # Phase E1: list --json wraps items in ListResponse[T] ({items, has_more}).
            # Mirrors mcp_tools/meta.py::_handle_get_comments which uses _list_response().
            click.echo(json_mod.dumps({"items": [comment_to_mcp(c) for c in result], "has_more": False}, indent=2, default=str))
            return
        if not result:
            click.echo("No comments.")
            return
        for c in result:
            click.echo(f"[{c['created_at']}] {c['author']}: {c['text']}")


@click.command("add-label", cls=ActorCommand)
@click.argument("label_name")
@click.argument("issue_id")
@click.option("--expected-assignee", default=None, help="Expected current holder for coordinator writes")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def add_label(ctx: click.Context, label_name: str, issue_id: str, expected_assignee: str | None, as_json: bool) -> None:
    """Add a label to an issue. Usage: add-label <label> <issue_id>"""
    with get_db() as db:
        # Prefix check before existence precheck — see filigree-f8861115a9.
        try:
            db._check_id_prefix(issue_id)
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        try:
            db.get_issue(issue_id)
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Not found: {issue_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Not found: {issue_id}", err=True)
            sys.exit(1)
        try:
            added, canonical, replaced = db.add_label(
                issue_id,
                label_name,
                actor=ctx.obj["actor"],
                expected_assignee=expected_assignee,
            )
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps(_value_error_envelope(e)))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        # Mutual-exclusivity displacement (e.g. review:* namespace) used to be
        # silent; surface the replaced label so callers can audit
        # (filigree-cb980eee0d, P2.7).
        status = "replaced" if replaced else ("added" if added else "already_exists")
        if as_json:
            payload: dict[str, Any] = {"issue_id": issue_id, "label": canonical, "status": status}
            if replaced:
                payload["replaced_labels"] = replaced
            click.echo(json_mod.dumps(payload))
        else:
            if replaced:
                click.echo(f"Added label '{canonical}' to {issue_id} (replaced: {', '.join(replaced)})")
            elif added:
                click.echo(f"Added label '{canonical}' to {issue_id}")
            else:
                click.echo(f"Label '{canonical}' already on {issue_id}")
        refresh_summary(db)


@click.command("remove-label", cls=ActorCommand)
@click.argument("issue_id")
@click.argument("label_name")
@click.option("--expected-assignee", default=None, help="Expected current holder for coordinator writes")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def remove_label(ctx: click.Context, issue_id: str, label_name: str, expected_assignee: str | None, as_json: bool) -> None:
    """Remove a label from an issue."""
    with get_db() as db:
        # Prefix check before existence precheck — see filigree-f8861115a9.
        try:
            db._check_id_prefix(issue_id)
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        try:
            db.get_issue(issue_id)
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Not found: {issue_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Not found: {issue_id}", err=True)
            sys.exit(1)
        try:
            removed, canonical = db.remove_label(
                issue_id,
                label_name,
                actor=ctx.obj["actor"],
                expected_assignee=expected_assignee,
            )
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps(_value_error_envelope(e)))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        status = "removed" if removed else "not_found"
        if as_json:
            click.echo(json_mod.dumps({"issue_id": issue_id, "label": canonical, "status": status}))
        else:
            if removed:
                click.echo(f"Removed label '{canonical}' from {issue_id}")
            else:
                click.echo(f"Label '{canonical}' not found on {issue_id}")
        refresh_summary(db)


@click.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def stats(as_json: bool) -> None:
    """Show project statistics."""
    with get_db() as db:
        s = db.get_stats()

        if as_json:
            click.echo(json_mod.dumps(s, indent=2, default=str))
            return

        click.echo("Status names:")
        for status, count in sorted(s["by_status"].items()):
            click.echo(f"  {status}: {count}")
        click.echo("\nStatus categories:")
        for category, count in sorted(s["by_category"].items()):
            click.echo(f"  {category}: {count}")
        click.echo("\nTypes:")
        for t, count in sorted(s["by_type"].items()):
            click.echo(f"  {t}: {count}")
        click.echo(f"\nReady: {s['ready_count']}")
        click.echo(f"Blocked: {s['blocked_count']}")
        click.echo(f"Dependencies: {s['total_dependencies']}")


@click.command("mcp-status")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def mcp_status(as_json: bool) -> None:
    """Show MCP/runtime status (CLI counterpart of the get_mcp_status MCP tool).

    Produces the same payload as the MCP ``get_mcp_status`` tool: schema
    compatibility, DB-init state, and runtime/install diagnostics. Like the MCP
    tool, this is intentionally read-only and safe in degraded modes (schema
    mismatch, registry mismatch, DB-open failure) — it reports *why* the
    connector is degraded rather than tracebacking.
    """
    # Lazy import: mcp_server pulls in mcp.server + starlette, which we do not
    # want to load on every CLI invocation. Mirror the MCP handler, which also
    # imports get_mcp_status_payload lazily (mcp_tools/workflow.py).
    from filigree.core import FILIGREE_DIR_NAME, ProjectNotInitialisedError, find_filigree_anchor
    from filigree.mcp_server import _attempt_startup, get_mcp_status_payload

    # Resolve the project anchor exactly as the MCP server's _run() does, then
    # populate the mcp_server module globals via _attempt_startup so the payload
    # reflects real project state (calling get_mcp_status_payload cold would
    # report "not_initialized" off the uninitialized globals).
    try:
        project_root, conf_path = find_filigree_anchor()
    except ProjectNotInitialisedError as exc:
        from filigree.cli_common import _emit_startup_failure

        _emit_startup_failure(exc, ErrorCode.NOT_INITIALIZED)
        sys.exit(1)

    _attempt_startup(project_root / FILIGREE_DIR_NAME, conf_path=conf_path)
    payload = get_mcp_status_payload()

    if as_json:
        # Same serializer the MCP _text() helper uses, so the two surfaces
        # emit byte-identical payloads (default=str renders StrEnum codes).
        click.echo(json_mod.dumps(payload, indent=2, default=str))
        return

    click.echo(f"Status: {payload['status']}")
    click.echo(f"DB initialized: {payload['db_initialized']}")
    click.echo(f"Schema compatible: {payload['schema_compatible']}")
    click.echo(f"Installed schema version: {payload['installed_schema_version']}")
    click.echo(f"Database schema version: {payload['database_schema_version']}")
    click.echo(f"Project dir: {payload['filigree_dir']}")
    if payload.get("code"):
        click.echo(f"Code: {payload['code']}")
    if payload.get("error"):
        click.echo(f"Error: {payload['error']}")
    if payload.get("guidance"):
        click.echo(f"Guidance: {payload['guidance']}")
    runtime = payload.get("runtime") or {}
    if runtime:
        click.echo("Runtime:")
        for key, value in runtime.items():
            click.echo(f"  {key}: {value}")


@click.command()
@click.argument("query")
@click.option("--limit", default=100, type=click.IntRange(min=0), help="Max results (default 100)")
@click.option("--offset", default=0, type=click.IntRange(min=0), help="Skip first N results")
@click.option(
    "--status-category",
    "status_category",
    type=click.Choice(["open", "wip", "done"]),
    default=None,
    help="Filter results by template status category. Default returns all categories.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def search(query: str, limit: int, offset: int, status_category: str | None, as_json: bool) -> None:
    """Search issues by title/description."""
    with get_db() as db:
        issues = db.search_issues(
            query,
            limit=limit + 1 if limit > 0 else limit,
            offset=offset,
            status_category=status_category,  # type: ignore[arg-type]
        )
        has_more = limit > 0 and len(issues) > limit
        issues = issues[:limit] if has_more else issues

        if as_json:
            search_payload: dict[str, Any] = {
                "items": [{"issue_id": i.id, "title": i.title, "status": i.status, "priority": i.priority, "type": i.type} for i in issues],
                "has_more": has_more,
            }
            if has_more:
                search_payload["next_offset"] = offset + len(issues)
            click.echo(json_mod.dumps(search_payload, indent=2, default=str))
            return

        for issue in issues:
            click.echo(f"P{issue.priority} {issue.id} [{issue.type}] {issue.status:<12} {issue.title}")
        click.echo(f"\n{len(issues)} results")


def _events_impl(issue_id: str, limit: int, as_json: bool) -> None:
    with get_db() as db:
        try:
            # Overfetch by 1 to detect has_more without an offset param.
            raw_events = db.get_issue_events(issue_id, limit=limit + 1 if limit > 0 else limit)
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Not found: {issue_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Not found: {issue_id}", err=True)
            sys.exit(1)

        has_more = limit > 0 and len(raw_events) > limit
        event_list = raw_events[:limit] if has_more else raw_events
        if as_json:
            events_payload: dict[str, Any] = {"items": [event_to_mcp(ev) for ev in event_list], "has_more": has_more}
            click.echo(json_mod.dumps(events_payload, indent=2, default=str))
            return

        if not event_list:
            click.echo(f"No events for {issue_id}.")
            return

        for ev in event_list:
            old_val = ev.get("old_value", "")
            new_val = ev.get("new_value", "")
            detail = ""
            if old_val or new_val:
                detail = f" ({old_val} -> {new_val})" if old_val else f" ({new_val})"
            actor_str = f" by {ev['actor']}" if ev.get("actor") else ""
            click.echo(f"  #{ev['id']}  {ev['created_at']}  {ev['event_type']}{detail}{actor_str}")
        click.echo(f"\n{len(event_list)} events")


@click.command("events")
@click.argument("issue_id")
@click.option("--limit", default=50, type=click.IntRange(min=0), help="Max events (default 50)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def events_cmd(issue_id: str, limit: int, as_json: bool) -> None:
    """Get event history for a specific issue, newest first."""
    _events_impl(issue_id, limit, as_json)


@click.command("get-issue-events")
@click.argument("issue_id")
@click.option("--limit", default=50, type=click.IntRange(min=0), help="Max events (default 50)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def get_issue_events_cmd(issue_id: str, limit: int, as_json: bool) -> None:
    """Get event history for a specific issue, newest first. Alias for `events`."""
    _events_impl(issue_id, limit, as_json)


@click.command("batch-update", cls=ActorCommand)
@click.argument("issue_ids", nargs=-1, required=True)
@click.option("--status", default=None, help="New status")
@click.option("--priority", "-p", default=None, type=click.IntRange(0, 4), help="New priority")
@click.option("--assignee", default=None, help="New assignee")
@click.option("--field", "-f", multiple=True, help="Custom field as key=value (repeatable)")
@click.option("--expected-assignee", default=None, help="Expected current holder for coordinator writes")
@click.option(
    "--detail",
    "response_detail",
    type=click.Choice(["slim", "full"]),
    default="slim",
    help="JSON shape for succeeded[]: 'slim' (default, 5-key SlimIssue) or 'full' (PublicIssue records).",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def batch_update(
    ctx: click.Context,
    issue_ids: tuple[str, ...],
    status: str | None,
    priority: int | None,
    assignee: str | None,
    field: tuple[str, ...],
    expected_assignee: str | None,
    response_detail: str,
    as_json: bool,
) -> None:
    """Update multiple issues with the same changes."""
    fields = None
    if field:
        fields = {}
        for f in field:
            if "=" not in f:
                if as_json:
                    click.echo(json_mod.dumps({"error": f"Invalid field format: {f} (expected key=value)", "code": ErrorCode.VALIDATION}))
                else:
                    click.echo(f"Invalid field format: {f}", err=True)
                sys.exit(1)
            k, v = f.split("=", 1)
            fields[k] = v

    with get_db() as db:
        try:
            results, errors = db.batch_update(
                list(issue_ids),
                status=status,
                priority=priority,
                assignee=assignee,
                fields=fields,
                actor=ctx.obj["actor"],
                expected_assignee=expected_assignee,
            )
        except WrongProjectError as e:
            # 2.1.0 §0.4: foreign-prefix id in the batch aborts envelope-level.
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        if as_json:
            if response_detail == "full":
                succeeded_payload: list[dict[str, Any]] = [dict(issue_to_public(i)) for i in results]
            else:
                succeeded_payload = [
                    {"issue_id": i.id, "title": i.title, "status": i.status, "priority": i.priority, "type": i.type} for i in results
                ]
            click.echo(
                json_mod.dumps(
                    {
                        "succeeded": succeeded_payload,
                        "failed": errors,
                    },
                    indent=2,
                    default=str,
                )
            )
        else:
            for issue in results:
                click.echo(f"  Updated {issue.id}: {issue.title}")
            for err in errors:
                click.echo(f"  Error {err['id']}: {err['error']}", err=True)
                if "valid_transitions" in err:
                    valid = ", ".join(t["to"] for t in err["valid_transitions"])
                    click.echo(f"    Valid transitions: {valid}", err=True)
            click.echo(f"Updated {len(results)} issues")
        refresh_summary(db)
        if errors:
            sys.exit(1)


@click.command("batch-close", cls=ActorCommand)
@click.argument("issue_ids", nargs=-1, required=True)
@click.option("--reason", default="", help="Close reason")
@click.option(
    "--detail",
    "response_detail",
    type=click.Choice(["slim", "full"]),
    default="slim",
    help="JSON shape for succeeded[]: 'slim' (default, 5-key SlimIssue) or 'full' (PublicIssue records). newly_unblocked stays slim.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help=("Use the template reverse/escape transition on every item. Use only for cleanup flows that leave the normal workflow."),
)
@click.option("--expected-assignee", default=None, help="Expected current holder for coordinator writes")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def batch_close(
    ctx: click.Context,
    issue_ids: tuple[str, ...],
    reason: str,
    response_detail: str,
    force: bool,
    expected_assignee: str | None,
    as_json: bool,
) -> None:
    """Close multiple issues with per-item error reporting."""
    with get_db() as db:
        ready_before_batch = {i.id for i in db.get_ready()} if as_json else set()
        try:
            closed, errors = db.batch_close(
                list(issue_ids),
                reason=reason,
                actor=ctx.obj["actor"],
                expected_assignee=expected_assignee,
                force=force,
            )
        except WrongProjectError as e:
            # 2.1.0 §0.4.
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        if as_json:
            ready_after_batch = db.get_ready() if as_json else []
            newly_unblocked_batch = [
                {"issue_id": i.id, "title": i.title, "status": i.status, "priority": i.priority, "type": i.type}
                for i in ready_after_batch
                if i.id not in ready_before_batch
            ]
            if response_detail == "full":
                succeeded_payload: list[dict[str, Any]] = [dict(issue_to_public(i)) for i in closed]
            else:
                succeeded_payload = [
                    {"issue_id": i.id, "title": i.title, "status": i.status, "priority": i.priority, "type": i.type} for i in closed
                ]
            # BatchResponse contract: newly_unblocked is NotRequired and must be
            # OMITTED when empty (not emitted as []). Mirrors
            # mcp_tools/issues.py::_handle_batch_close.
            payload: dict[str, Any] = {
                "succeeded": succeeded_payload,
                "failed": errors,
            }
            if newly_unblocked_batch:
                payload["newly_unblocked"] = newly_unblocked_batch
            click.echo(json_mod.dumps(payload, indent=2, default=str))
        else:
            for issue in closed:
                click.echo(f"  Closed {issue.id}: {issue.title}")
            for err in errors:
                click.echo(f"  Error {err['id']}: {err['error']}", err=True)
                if "valid_transitions" in err:
                    valid = ", ".join(t["to"] for t in err["valid_transitions"])
                    click.echo(f"    Valid transitions: {valid}", err=True)
            click.echo(f"Closed {len(closed)}/{len(issue_ids)} issues")
        refresh_summary(db)
        if errors:
            sys.exit(1)


@click.command("batch-add-label", cls=ActorCommand)
@click.argument("label_name")
@click.argument("issue_ids", nargs=-1, required=True)
@click.option(
    "--detail",
    "response_detail",
    type=click.Choice(["slim", "full"]),
    default="slim",
    help="JSON shape for succeeded[]: 'slim' (default, issue ID strings) or 'full' (PublicIssue records).",
)
@click.option("--expected-assignee", default=None, help="Expected current holder for coordinator writes")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def batch_add_label(
    ctx: click.Context,
    label_name: str,
    issue_ids: tuple[str, ...],
    response_detail: str,
    expected_assignee: str | None,
    as_json: bool,
) -> None:
    """Add the same label to multiple issues."""
    with get_db() as db:
        try:
            labeled, errors = db.batch_add_label(
                list(issue_ids),
                label=label_name,
                actor=ctx.obj["actor"],
                expected_assignee=expected_assignee,
            )
            if response_detail == "full":
                succeeded_payload: list[Any] = [dict(issue_to_public(db.get_issue(row["id"]))) for row in labeled]
            else:
                succeeded_payload = [row["id"] for row in labeled]
        except WrongProjectError as e:
            # 2.1.0 §0.4.
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        if as_json:
            click.echo(
                json_mod.dumps(
                    {
                        "succeeded": succeeded_payload,
                        "failed": errors,
                    },
                    indent=2,
                    default=str,
                )
            )
        else:
            for row in labeled:
                if row["status"] == "added":
                    click.echo(f"  Added label '{label_name}' to {row['id']}")
                else:
                    click.echo(f"  Label '{label_name}' already on {row['id']}")
            for err in errors:
                click.echo(f"  Error {err['id']}: {err['error']}", err=True)
            click.echo(f"Labeled {len(labeled)}/{len(issue_ids)} issues")
        refresh_summary(db)
        if errors:
            sys.exit(1)


@click.command("batch-remove-label", cls=ActorCommand)
@click.argument("label_name")
@click.argument("issue_ids", nargs=-1, required=True)
@click.option(
    "--detail",
    "response_detail",
    type=click.Choice(["slim", "full"]),
    default="slim",
    help="JSON shape for succeeded[]: 'slim' (default, issue ID strings) or 'full' (PublicIssue records).",
)
@click.option("--expected-assignee", default=None, help="Expected current holder for coordinator writes")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def batch_remove_label(
    ctx: click.Context,
    label_name: str,
    issue_ids: tuple[str, ...],
    response_detail: str,
    expected_assignee: str | None,
    as_json: bool,
) -> None:
    """Remove the same label from multiple issues."""
    with get_db() as db:
        try:
            removed, errors = db.batch_remove_label(
                list(issue_ids),
                label=label_name,
                actor=ctx.obj["actor"],
                expected_assignee=expected_assignee,
            )
            if response_detail == "full":
                succeeded_payload: list[Any] = [dict(issue_to_public(db.get_issue(row["id"]))) for row in removed]
            else:
                succeeded_payload = [row["id"] for row in removed]
        except WrongProjectError as e:
            # 2.1.0 §0.4.
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        if as_json:
            click.echo(
                json_mod.dumps(
                    {
                        "succeeded": succeeded_payload,
                        "failed": errors,
                    },
                    indent=2,
                    default=str,
                )
            )
        else:
            for row in removed:
                if row["status"] == "removed":
                    click.echo(f"  Removed label '{label_name}' from {row['id']}")
                else:
                    click.echo(f"  Label '{label_name}' not found on {row['id']}")
            for err in errors:
                click.echo(f"  Error {err['id']}: {err['error']}", err=True)
            click.echo(f"Removed label from {len(removed)}/{len(issue_ids)} issues")
        refresh_summary(db)
        if errors:
            sys.exit(1)


@click.command("batch-add-comment", cls=ActorCommand)
@click.argument("text")
@click.argument("issue_ids", nargs=-1, required=True)
@click.option(
    "--detail",
    "response_detail",
    type=click.Choice(["slim", "full"]),
    default="slim",
    help="JSON shape for succeeded[]: 'slim' (default, issue ID strings) or 'full' (PublicIssue records of the commented-on issues).",
)
@click.option("--expected-assignee", default=None, help="Expected current holder for coordinator writes")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def batch_add_comment(
    ctx: click.Context,
    text: str,
    issue_ids: tuple[str, ...],
    response_detail: str,
    expected_assignee: str | None,
    as_json: bool,
) -> None:
    """Add the same comment to multiple issues."""
    with get_db() as db:
        try:
            commented, errors = db.batch_add_comment(
                list(issue_ids),
                text=text,
                author=ctx.obj["actor"],
                expected_assignee=expected_assignee,
            )
            if response_detail == "full":
                succeeded_payload: list[Any] = [dict(issue_to_public(db.get_issue(str(row["id"])))) for row in commented]
            else:
                succeeded_payload = [str(row["id"]) for row in commented]
        except WrongProjectError as e:
            # 2.1.0 §0.4.
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        if as_json:
            click.echo(
                json_mod.dumps(
                    {
                        "succeeded": succeeded_payload,
                        "failed": errors,
                    },
                    indent=2,
                    default=str,
                )
            )
        else:
            for row in commented:
                click.echo(f"  Added comment {row['comment_id']} to {row['id']}")
            for err in errors:
                click.echo(f"  Error {err['id']}: {err['error']}", err=True)
            click.echo(f"Commented on {len(commented)}/{len(issue_ids)} issues")
        refresh_summary(db)
        if errors:
            sys.exit(1)


def _list_labels_impl(namespace: str | None, top: int, as_json: bool) -> None:
    with get_db() as db:
        result = db.list_labels(namespace=label_namespace_from_public(namespace), top=top)
        if as_json:
            items = [label_namespace_item_to_public(ns, ns_data) for ns, ns_data in result["namespaces"].items()]
            click.echo(json_mod.dumps({"items": items, "has_more": False}, indent=2))
            return
        for ns_name, ns_data in sorted(result["namespaces"].items()):
            writable = "rw" if ns_data["writable"] else "ro"
            display_name = label_namespace_to_public(ns_name)
            click.echo(f"\n{display_name}: ({ns_data['type']}, {writable})")
            for item in ns_data["labels"]:
                click.echo(f"  {item['label']}  ({item['count']})")
            if ns_data.get("truncated"):
                click.echo(f"  ... showing {len(ns_data['labels'])} of {ns_data['total']}; use --top 0 for all")


@click.command("labels")
@click.option("--namespace", "-n", default=None, help="Filter to a namespace")
@click.option("--top", default=10, type=click.IntRange(min=0), help="Max labels per namespace (0 for unlimited)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_labels_cmd(namespace: str | None, top: int, as_json: bool) -> None:
    """List all labels grouped by namespace with counts."""
    _list_labels_impl(namespace, top, as_json)


@click.command("list-labels")
@click.option("--namespace", "-n", default=None, help="Filter to a namespace")
@click.option("--top", default=10, type=click.IntRange(min=0), help="Max labels per namespace (0 for unlimited)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_labels_alias_cmd(namespace: str | None, top: int, as_json: bool) -> None:
    """List all labels grouped by namespace with counts. Alias for `labels`."""
    _list_labels_impl(namespace, top, as_json)


def _taxonomy_impl(as_json: bool) -> None:
    with get_db() as db:
        result = db.get_label_taxonomy()
        if as_json:
            click.echo(json_mod.dumps(result, indent=2))
            return
        for section, data in result.items():
            click.echo(f"\n== {section} ==")
            if isinstance(data, dict) and "suggested" in data:
                click.echo(f"  {', '.join(data['suggested'])}")
            elif isinstance(data, dict):
                for ns, info in data.items():
                    vals = info.get("values") or info.get("examples") or [info.get("example", "")]
                    click.echo(f"  {ns}: {info['description']}  [{', '.join(str(v) for v in vals)}]")


@click.command("taxonomy")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def taxonomy_cmd(as_json: bool) -> None:
    """Show the label taxonomy vocabulary."""
    _taxonomy_impl(as_json)


@click.command("get-label-taxonomy")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def get_label_taxonomy_cmd(as_json: bool) -> None:
    """Show the label taxonomy vocabulary. Alias for `taxonomy`."""
    _taxonomy_impl(as_json)


@click.command("reconciliation-debt")
@click.option("--limit", default=50, type=int, help="Max results (default 50)")
@click.option("--offset", default=0, type=int, help="Skip first N results")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def reconciliation_debt_cmd(limit: int, offset: int, as_json: bool) -> None:
    """List issues carrying reconciliation debt (deferred governed cascade closes).

    Design A defers a governed finding→issue auto-close that Legis blocks or
    cannot confirm, recording the deferral as reconciliation debt. This lists
    the issues that carry it so the deferral can be actioned.
    """
    with get_db() as db:
        try:
            rows = db.list_reconciliation_debt(limit=limit + 1, offset=offset)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]
        next_offset = offset + len(rows) if has_more else None

        if as_json:
            payload: dict[str, Any] = {"items": rows, "has_more": has_more}
            if has_more and next_offset is not None:
                payload["next_offset"] = next_offset
            click.echo(json_mod.dumps(payload, indent=2, default=str))
            return

        if not rows:
            click.echo("No reconciliation debt.")
            return
        for row in rows:
            click.echo(f"{row['issue_id']}  {row['debt_count']} debt  (latest {row['latest']})")
        click.echo(f"\n{len(rows)} issue(s) with reconciliation debt")


def register(cli: click.Group) -> None:
    """Register metadata commands with the CLI group."""
    cli.add_command(add_comment)
    cli.add_command(get_comments)
    cli.add_command(add_label)
    cli.add_command(remove_label)
    cli.add_command(list_labels_cmd)
    cli.add_command(list_labels_alias_cmd)
    cli.add_command(taxonomy_cmd)
    cli.add_command(get_label_taxonomy_cmd)
    cli.add_command(stats)
    cli.add_command(mcp_status)
    cli.add_command(search)
    cli.add_command(events_cmd)
    cli.add_command(get_issue_events_cmd)
    cli.add_command(batch_update)
    cli.add_command(batch_close)
    cli.add_command(batch_add_label)
    cli.add_command(batch_remove_label)
    cli.add_command(batch_add_comment)
    cli.add_command(reconciliation_debt_cmd)
