"""CLI commands for observations (agent scratchpad): observe, list, dismiss, promote, batch operations."""

from __future__ import annotations

import copy
import json as json_mod
import sqlite3
import sys
from typing import Any

import click

from filigree.cli_common import add_hidden_flat_alias, get_db, refresh_summary
from filigree.issue_payloads import issue_to_public
from filigree.mcp_tools.payloads import observation_link_to_mcp, observation_to_mcp
from filigree.models import Issue
from filigree.registry import RegistryResolutionError, RegistryUnavailableError
from filigree.registry_errors import registry_error_response
from filigree.types.api import BatchFailure, ErrorCode

_MAX_SQLITE_OFFSET = 9_223_372_036_854_775_807
_MAX_SQLITE_OVERFETCH_LIMIT = _MAX_SQLITE_OFFSET - 1
_MAX_OBSERVATION_OLDER_THAN_HOURS = 8760


def _slim_issue(issue: Issue) -> dict[str, Any]:
    return {
        "issue_id": issue.id,
        "title": issue.title,
        "status": issue.status,
        "priority": issue.priority,
        "type": issue.type,
    }


def _emit_validation_error(msg: str, *, as_json: bool) -> None:
    """Emit a 2.0 envelope (or plain text) for a numeric-range failure and exit 1.

    Run inside the command body — not as a Click ``IntRange`` type — because
    the JSON envelope contract requires ``as_json`` to be parsed before the
    error is shaped. Click rejects ``IntRange`` violations before the body
    runs, which would emit a stderr usage error with exit 2 instead.
    """
    _emit_error(msg, ErrorCode.VALIDATION, as_json=as_json)


def _emit_error(msg: str, code: ErrorCode, *, as_json: bool, details: dict[str, object] | None = None) -> None:
    if as_json:
        envelope: dict[str, object] = {"error": msg, "code": code}
        if details:
            envelope["details"] = details
        click.echo(json_mod.dumps(envelope))
    else:
        click.echo(f"Error: {msg}", err=True)
    sys.exit(1)


def _validate_priority(priority: int | None, *, as_json: bool) -> None:
    if priority is not None and not 0 <= priority <= 4:
        _emit_validation_error(
            f"Priority must be between 0 and 4, got {priority}",
            as_json=as_json,
        )


def _validate_line(line: int | None, *, as_json: bool) -> None:
    if line is not None and line < 0:
        _emit_validation_error(f"Line must be >= 0, got {line}", as_json=as_json)


def _validate_int_range(value: int | None, name: str, *, min_val: int, max_val: int, as_json: bool) -> None:
    if value is not None and not min_val <= value <= max_val:
        _emit_validation_error(f"{name} must be between {min_val} and {max_val}, got {value}", as_json=as_json)


def _validate_limit(limit: int, *, as_json: bool) -> None:
    _validate_int_range(limit, "Limit", min_val=1, max_val=_MAX_SQLITE_OVERFETCH_LIMIT, as_json=as_json)


def _validate_offset(offset: int, *, as_json: bool) -> None:
    _validate_int_range(offset, "Offset", min_val=0, max_val=_MAX_SQLITE_OFFSET, as_json=as_json)


@click.command("observe")
@click.argument("summary")
@click.option("--detail", default="", help="Longer explanation or context")
@click.option(
    "--file-path",
    "--file",
    "file_path",
    default="",
    help="File path (relative to project root)",
)
@click.option("--line", default=None, type=int, help="Line number in file (1-indexed)")
@click.option("--source-issue-id", default="", help="Issue ID that prompted this observation")
@click.option(
    "--priority",
    "-p",
    default=2,  # CLI default is 2; MCP default is 3 — intentional per-surface divergence
    type=int,
    help="Priority 0-4 (default 2)",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def observe_cmd(
    ctx: click.Context,
    summary: str,
    detail: str,
    file_path: str,
    line: int | None,
    source_issue_id: str,
    priority: int,
    as_json: bool,
) -> None:
    """Record an observation (agent scratchpad note, fire-and-forget)."""
    _validate_line(line, as_json=as_json)
    _validate_priority(priority, as_json=as_json)
    with get_db() as db:
        try:
            obs = db.create_observation(
                summary,
                detail=detail,
                file_path=file_path,
                line=line,
                source_issue_id=source_issue_id,
                priority=priority,
                actor=ctx.obj["actor"],
            )
        except (RegistryResolutionError, RegistryUnavailableError) as e:
            response = registry_error_response(e, action="recording observation")
            _emit_error(response["error"], response["code"], as_json=as_json, details=response.get("details"))
        except ValueError as e:
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
            click.echo(json_mod.dumps(observation_to_mcp(obs), indent=2, default=str))
        else:
            click.echo(f"Observed {obs['id']}: {obs['summary']}")
        refresh_summary(db)


@click.command("list-observations")
@click.option("--limit", default=50, type=int, help="Max results (default 50)")
@click.option("--offset", default=0, type=int, help="Skip first N results")
@click.option("--no-limit", "no_limit", is_flag=True, help="Return all results without cap")
@click.option("--file-path", default="", help="Filter by substring in file path")
@click.option("--file-id", default="", help="Filter by exact file ID")
@click.option("--actor", default="", help="Filter by exact actor (e.g. your agent name)")
@click.option("--source-issue-id", default="", help="Filter by source issue ID")
@click.option("--priority-min", type=int, default=None, help="Only observations with priority >= this value")
@click.option("--priority-max", type=int, default=None, help="Only observations with priority <= this value")
@click.option("--older-than-hours", type=int, default=None, help="Only observations created more than N hours ago")
@click.option(
    "--sort-by",
    type=click.Choice(["priority", "created_at", "expires_at"]),
    default="priority",
    help="Sort field (default: priority)",
)
@click.option(
    "--direction",
    type=click.Choice(["asc", "desc"]),
    default="asc",
    help="Sort direction (default: asc)",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_observations_cmd(
    limit: int,
    offset: int,
    no_limit: bool,
    file_path: str,
    file_id: str,
    actor: str,
    source_issue_id: str,
    priority_min: int | None,
    priority_max: int | None,
    older_than_hours: int | None,
    sort_by: str,
    direction: str,
    as_json: bool,
) -> None:
    """List pending observations with optional filtering."""
    _validate_limit(limit, as_json=as_json)
    _validate_offset(offset, as_json=as_json)
    _validate_int_range(priority_min, "priority_min", min_val=0, max_val=4, as_json=as_json)
    _validate_int_range(priority_max, "priority_max", min_val=0, max_val=4, as_json=as_json)
    _validate_int_range(
        older_than_hours,
        "older_than_hours",
        min_val=0,
        max_val=_MAX_OBSERVATION_OLDER_THAN_HOURS,
        as_json=as_json,
    )
    with get_db() as db:
        effective_limit = limit if not no_limit else 10_000_000
        try:
            observations = db.list_observations(
                limit=effective_limit + 1,
                offset=offset,
                file_path=file_path,
                file_id=file_id,
                actor=actor,
                source_issue_id=source_issue_id,
                priority_min=priority_min,
                priority_max=priority_max,
                older_than_hours=older_than_hours,
                sort_by=sort_by,
                direction=direction,
            )
        except ValueError as e:
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
        has_more = len(observations) > effective_limit
        if has_more:
            observations = observations[:effective_limit]
        next_offset = offset + len(observations) if has_more else None

        if as_json:
            payload: dict[str, Any] = {
                "items": [observation_to_mcp(obs) for obs in observations],
                "has_more": has_more,
            }
            if has_more and next_offset is not None:
                payload["next_offset"] = next_offset
            click.echo(json_mod.dumps(payload, indent=2, default=str))
            return

        if not observations:
            click.echo("No observations.")
            return
        for obs in observations:
            loc = f" {obs['file_path']}" if obs.get("file_path") else ""
            if loc and obs.get("line") is not None:
                loc += f":{obs['line']}"
            click.echo(f"P{obs['priority']} {obs['id']}{loc}  {obs['summary']}")
        click.echo(f"\n{len(observations)} observation(s)")


@click.command("dismiss-observation")
@click.argument("observation_id")
@click.option("--reason", default="", help="Reason for dismissal")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def dismiss_observation_cmd(
    ctx: click.Context,
    observation_id: str,
    reason: str,
    as_json: bool,
) -> None:
    """Dismiss a single observation."""
    with get_db() as db:
        try:
            db.dismiss_observation(
                observation_id,
                actor=ctx.obj["actor"],
                reason=reason,
            )
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.NOT_FOUND}))
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
            click.echo(json_mod.dumps({"status": "dismissed", "observation_id": observation_id}))
        else:
            click.echo(f"Dismissed {observation_id}")
        refresh_summary(db)


@click.command("promote-observation")
@click.argument("observation_id")
@click.option(
    "--type",
    "issue_type",
    default="task",
    help="Issue type (bug, task, feature; requirement requires the requirements pack)",
)
@click.option(
    "--priority",
    "-p",
    default=None,
    type=int,
    help="Override priority (default: observation priority)",
)
@click.option("--title", default=None, help="Override title (default: observation summary)")
@click.option("--description", default="", help="Extra description to prepend")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def promote_observation_cmd(
    ctx: click.Context,
    observation_id: str,
    issue_type: str,
    priority: int | None,
    title: str | None,
    description: str,
    as_json: bool,
) -> None:
    """Promote an observation to a real issue."""
    _validate_priority(priority, as_json=as_json)
    with get_db() as db:
        try:
            result = db.promote_observation(
                observation_id,
                issue_type=issue_type,
                priority=priority,
                title=title,
                extra_description=description,
                actor=ctx.obj["actor"],
            )
            issue = db.get_issue(result["issue"].id)
        except ValueError as e:
            msg = str(e)
            err_code = ErrorCode.NOT_FOUND if "not found" in msg.lower() else ErrorCode.VALIDATION
            if as_json:
                click.echo(json_mod.dumps({"error": msg, "code": err_code}))
            else:
                click.echo(f"Error: {msg}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        resp: dict[str, Any] = dict(issue_to_public(issue))
        if result.get("warnings"):
            resp["warnings"] = result["warnings"]
        if as_json:
            click.echo(json_mod.dumps(resp, indent=2, default=str))
        else:
            issue = result["issue"]
            click.echo(f"Promoted {observation_id} → {issue.id}: {issue.title}")
            if result.get("warnings"):
                for w in result["warnings"]:
                    click.echo(f"  Warning: {w}", err=True)
        refresh_summary(db)


@click.command("batch-dismiss-observations")
@click.argument("observation_ids", nargs=-1, required=True)
@click.option("--reason", default="", help="Reason for dismissal")
@click.option(
    "--detail",
    "response_detail",
    type=click.Choice(["slim", "full"]),
    default="slim",
    help="JSON shape for succeeded[]: 'slim' (default, observation ID strings) or 'full' (pre-dismissal ObservationDict records).",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def batch_dismiss_observations_cmd(
    ctx: click.Context,
    observation_ids: tuple[str, ...],
    reason: str,
    response_detail: str,
    as_json: bool,
) -> None:
    """Dismiss multiple observations in one call."""
    with get_db() as db:
        raw_ids = list(observation_ids)
        # Snapshot pre-dismissal records for full mode — rows are deleted by
        # batch_dismiss_observations so the fetch must happen first.
        full_records: list[dict[str, Any]] = []
        try:
            if response_detail == "full":
                full_records = [observation_to_mcp(rec) for rec in db.get_observations_by_ids(raw_ids)]
            result = db.batch_dismiss_observations(
                raw_ids,
                actor=ctx.obj["actor"],
                reason=reason,
            )
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        # Mirror MCP: compute succeeded as unique inputs minus not_found, preserving order
        not_found_set = set(result["not_found"])
        succeeded_ids = [oid for oid in dict.fromkeys(raw_ids) if oid not in not_found_set]
        failed: list[BatchFailure] = [
            BatchFailure(id=oid, error=f"Observation not found: {oid}", code=ErrorCode.NOT_FOUND) for oid in result["not_found"]
        ]

        if as_json:
            succeeded_payload: list[Any] = full_records if response_detail == "full" else succeeded_ids
            click.echo(
                json_mod.dumps(
                    {"succeeded": succeeded_payload, "failed": list(failed)},
                    indent=2,
                    default=str,
                )
            )
        else:
            for oid in succeeded_ids:
                click.echo(f"  Dismissed {oid}")
            for f_item in failed:
                click.echo(f"  Error {f_item['id']}: {f_item['error']}", err=True)
            click.echo(f"Dismissed {len(succeeded_ids)}/{len(observation_ids)} observations")
        refresh_summary(db)
        if failed:
            sys.exit(1)


@click.command("batch-promote-observations")
@click.argument("observation_ids", nargs=-1, required=True)
@click.option(
    "--type",
    "issue_type",
    default="task",
    help="Issue type (bug, task, feature; requirement requires the requirements pack)",
)
@click.option(
    "--priority",
    "-p",
    default=None,
    type=int,
    help="Override priority for all created issues (default: each observation priority)",
)
@click.option(
    "--detail",
    "response_detail",
    type=click.Choice(["slim", "full"]),
    default="slim",
    help="JSON shape for succeeded[]: 'slim' (default, 5-key SlimIssue) or 'full' (PublicIssue records).",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def batch_promote_observations_cmd(
    ctx: click.Context,
    observation_ids: tuple[str, ...],
    issue_type: str,
    priority: int | None,
    response_detail: str,
    as_json: bool,
) -> None:
    """Promote multiple observations to issues in one call."""
    _validate_priority(priority, as_json=as_json)
    with get_db() as db:
        try:
            promoted, failed = db.batch_promote_observations(
                list(observation_ids),
                issue_type=issue_type,
                priority=priority,
                actor=ctx.obj["actor"],
            )
            issues = [db.get_issue(result["issue"].id) for result in promoted]
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        failed_ids = {f_item["id"] for f_item in failed}
        succeeded_obs_ids = [oid for oid in dict.fromkeys(observation_ids) if oid not in failed_ids]
        if as_json:
            if response_detail == "full":
                succeeded_payload: list[Any] = [dict(issue_to_public(issue)) for issue in issues]
            else:
                succeeded_payload = [_slim_issue(issue) for issue in issues]
            click.echo(
                json_mod.dumps(
                    {"succeeded": succeeded_payload, "failed": failed},
                    indent=2,
                    default=str,
                )
            )
        else:
            for obs_id, issue in zip(succeeded_obs_ids, issues, strict=False):
                click.echo(f"  Promoted {obs_id} -> {issue.id}: {issue.title}")
            for f_item in failed:
                click.echo(f"  Error {f_item['id']}: {f_item['error']}", err=True)
            click.echo(f"Promoted {len(issues)}/{len(observation_ids)} observations")
        refresh_summary(db)
        if failed:
            sys.exit(1)


@click.command("link-observation")
@click.argument("observation_id")
@click.argument("issue_id")
@click.option(
    "--disposition",
    type=click.Choice(["evidence", "duplicate", "superseded", "related"]),
    default="evidence",
    help="Triage disposition for this link.",
)
@click.option("--reason", default="", help="Reason or operator note for the link")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def link_observation_cmd(
    ctx: click.Context,
    observation_id: str,
    issue_id: str,
    disposition: str,
    reason: str,
    as_json: bool,
) -> None:
    """Link an observation to an existing issue and clear it from the queue."""
    with get_db() as db:
        try:
            link = db.link_observation_to_issue(
                observation_id,
                issue_id,
                disposition=disposition,
                reason=reason,
                actor=ctx.obj["actor"],
            )
        except KeyError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except ValueError as e:
            msg = str(e)
            err_code = ErrorCode.NOT_FOUND if "not found" in msg.lower() else ErrorCode.VALIDATION
            if as_json:
                click.echo(json_mod.dumps({"error": msg, "code": err_code}))
            else:
                click.echo(f"Error: {msg}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        if as_json:
            click.echo(json_mod.dumps(observation_link_to_mcp(link), indent=2, default=str))
        else:
            click.echo(f"Linked {observation_id} -> {issue_id} as {disposition}")
        refresh_summary(db)


@click.command("batch-link-observations")
@click.argument("issue_id")
@click.argument("observation_ids", nargs=-1, required=True)
@click.option(
    "--disposition",
    type=click.Choice(["evidence", "duplicate", "superseded", "related"]),
    default="evidence",
    help="Triage disposition for every link.",
)
@click.option("--reason", default="", help="Reason or operator note for every link")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def batch_link_observations_cmd(
    ctx: click.Context,
    issue_id: str,
    observation_ids: tuple[str, ...],
    disposition: str,
    reason: str,
    as_json: bool,
) -> None:
    """Link multiple observations to an existing issue."""
    with get_db() as db:
        try:
            linked, failed = db.batch_link_observations_to_issue(
                list(observation_ids),
                issue_id,
                disposition=disposition,
                reason=reason,
                actor=ctx.obj["actor"],
            )
        except KeyError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except (TypeError, ValueError) as e:
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

        succeeded_payload = [observation_link_to_mcp(item) for item in linked]
        payload = {
            "succeeded": succeeded_payload,
            "failed": failed,
        }
        if as_json:
            click.echo(json_mod.dumps(payload, indent=2, default=str))
        else:
            for item in succeeded_payload:
                click.echo(f"  Linked {item['observation_id']} -> {item['issue_id']} as {item['disposition']}")
            for f_item in failed:
                click.echo(f"  Error {f_item['id']}: {f_item['error']}", err=True)
            click.echo(f"Linked {len(linked)}/{len(observation_ids)} observations")
        refresh_summary(db)
        if failed:
            sys.exit(1)


@click.command("promote-observations-to-issue")
@click.argument("observation_ids", nargs=-1, required=True)
@click.option("--type", "issue_type", default="task", help="Issue type for the created issue")
@click.option(
    "--priority",
    "-p",
    default=None,
    type=int,
    help="Override priority (default: highest priority among observations)",
)
@click.option("--title", default=None, help="Override title (default: first observation summary)")
@click.option("--description", default="", help="Extra description to prepend")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def promote_observations_to_issue_cmd(
    ctx: click.Context,
    observation_ids: tuple[str, ...],
    issue_type: str,
    priority: int | None,
    title: str | None,
    description: str,
    as_json: bool,
) -> None:
    """Promote multiple observations into one issue."""
    _validate_priority(priority, as_json=as_json)
    with get_db() as db:
        try:
            result = db.promote_observations_to_issue(
                list(observation_ids),
                issue_type=issue_type,
                priority=priority,
                title=title,
                extra_description=description,
                actor=ctx.obj["actor"],
            )
            issue = db.get_issue(result["issue"].id)
        except (TypeError, ValueError) as e:
            msg = str(e)
            err_code = ErrorCode.NOT_FOUND if "not found" in msg.lower() else ErrorCode.VALIDATION
            if as_json:
                click.echo(json_mod.dumps({"error": msg, "code": err_code}))
            else:
                click.echo(f"Error: {msg}", err=True)
            sys.exit(1)
        except sqlite3.Error as e:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Database error: {e}", "code": ErrorCode.IO}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        resp: dict[str, Any] = dict(issue_to_public(issue))
        if result.get("warnings"):
            resp["warnings"] = result["warnings"]
        if as_json:
            click.echo(json_mod.dumps(resp, indent=2, default=str))
        else:
            click.echo(f"Promoted {len(observation_ids)} observations -> {issue.id}: {issue.title}")
            if result.get("warnings"):
                for w in result["warnings"]:
                    click.echo(f"  Warning: {w}", err=True)
        refresh_summary(db)


@click.group("observation", invoke_without_command=True)
@click.pass_context
def observation_group(ctx: click.Context) -> None:
    """Manage observations (the agent scratchpad) — list, dismiss, promote, link.

    Grouped form of the flat observation-management verbs (which still resolve as
    hidden back-compat aliases). Note: ``observe`` (recording a new observation)
    stays a flat, visible top-level verb (filigree-03303d6c5a); ``observation
    create`` is a visible alias of it (filigree-ce3bfae865).
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit(0)


def register(cli: click.Group) -> None:
    """Register observation commands with the CLI group."""
    # ``observe`` stays flat and visible — it is a common everyday verb.
    cli.add_command(observe_cmd)

    observation_group.add_command(list_observations_cmd, "list")
    observation_group.add_command(dismiss_observation_cmd, "dismiss")
    observation_group.add_command(promote_observation_cmd, "promote")
    observation_group.add_command(link_observation_cmd, "link")
    observation_group.add_command(promote_observations_to_issue_cmd, "promote-to-issue")
    observation_group.add_command(batch_dismiss_observations_cmd, "batch-dismiss")
    observation_group.add_command(batch_link_observations_cmd, "batch-link")
    observation_group.add_command(batch_promote_observations_cmd, "batch-promote")

    # ``observation create`` — visible in-group alias of the flat ``observe``
    # verb (filigree-ce3bfae865, dogfood N-7). Clone for the same reason
    # ``add_hidden_flat_alias`` clones: the Command object is shared, so
    # alias-specific attrs (name/help) must be set on a copy, never on the
    # original. copy.copy is shallow — the clone shares observe_cmd's params
    # and callback (no fork); mutate attrs only, never params.
    create_alias = copy.copy(observe_cmd)
    create_alias.name = "create"
    create_alias.short_help = "Alias of `filigree observe` — record an observation."
    create_alias.help = f"{observe_cmd.help}\n\nAlias of `filigree observe`, the canonical flat verb."
    observation_group.add_command(create_alias, "create")

    cli.add_command(observation_group)

    add_hidden_flat_alias(cli, list_observations_cmd, "list-observations")
    add_hidden_flat_alias(cli, dismiss_observation_cmd, "dismiss-observation")
    add_hidden_flat_alias(cli, promote_observation_cmd, "promote-observation")
    add_hidden_flat_alias(cli, link_observation_cmd, "link-observation")
    add_hidden_flat_alias(cli, promote_observations_to_issue_cmd, "promote-observations-to-issue")
    add_hidden_flat_alias(cli, batch_dismiss_observations_cmd, "batch-dismiss-observations")
    add_hidden_flat_alias(cli, batch_link_observations_cmd, "batch-link-observations")
    add_hidden_flat_alias(cli, batch_promote_observations_cmd, "batch-promote-observations")
