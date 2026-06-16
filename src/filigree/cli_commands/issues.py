"""CLI commands for issue CRUD: create, show, list, update, close, reopen, claim, undo."""

from __future__ import annotations

import json as json_mod
import logging
import sqlite3
import sys
from typing import Any

import click

from filigree import governance
from filigree.cli_common import ActorCommand, explicit_actor, get_db, refresh_summary
from filigree.core import WrongProjectError
from filigree.issue_payloads import issue_to_public, public_issue_with
from filigree.types.api import (
    AmbiguousTransitionError,
    ClaimConflictError,
    ErrorCode,
    InvalidTransitionError,
    IssueDeletionRefusedError,
    claim_conflict_envelope,
    classify_release_claim_error,
    classify_value_error,
)
from filigree.validation import sanitize_actor


def _resolve_and_sanitize_actor(actor: str | None, assignee: str, *, as_json: bool) -> str:
    """Default actor to assignee, then sanitize. Exit 1 on validation failure.

    The group-level ``cli --actor`` already runs through ``sanitize_actor``
    (cli.py), but composed subcommands like ``start-work`` / ``start-next-work``
    own their own ``--actor`` option and bypass that path. Without this re-run,
    blank/control/overlong values reach the audit trail unchecked.
    """
    cleaned, err = sanitize_actor(actor if actor is not None else assignee)
    if err:
        if as_json:
            click.echo(json_mod.dumps({"error": err, "code": ErrorCode.VALIDATION}))
            sys.exit(1)
        raise click.BadParameter(err, param_hint="'--actor'")
    return cleaned


_CLAIM_IDENTITY_MISSING_CLI = "provide --assignee or --actor: claim verbs need a caller identity"


def _resolve_cli_claim_assignee(
    ctx: click.Context,
    assignee: str | None,
    *,
    as_json: bool,
    post_verb_actor: str | None = None,
) -> str:
    """Default an omitted ``--assignee`` from an *explicitly provided* actor (FIL-3).

    Precedence: explicit ``--assignee`` > the command's own post-verb
    ``--actor`` (start-work / start-next-work, sanitized here) > the
    group-level explicit ``filigree --actor X`` (already sanitized by the
    group callback). The implicit group default ("cli") never satisfies the
    identity — accidental anonymous claims keep failing, but the error now
    names both options. Exits 1 with a VALIDATION envelope when no identity
    was provided.
    """
    if assignee is not None:
        return assignee
    if post_verb_actor is not None:
        return _resolve_and_sanitize_actor(post_verb_actor, "", as_json=as_json)
    actor = explicit_actor(ctx)
    if actor is not None:
        return actor
    if as_json:
        click.echo(json_mod.dumps({"error": _CLAIM_IDENTITY_MISSING_CLI, "code": ErrorCode.VALIDATION}))
    else:
        click.echo(f"Error: {_CLAIM_IDENTITY_MISSING_CLI}", err=True)
    sys.exit(1)


logger = logging.getLogger(__name__)

_MAX_SQLITE_OFFSET = 9_223_372_036_854_775_807
_MAX_SQLITE_OVERFETCH_LIMIT = _MAX_SQLITE_OFFSET - 1


def _log_transition_enrichment_failure(issue_id: str, exc: Exception) -> None:
    if isinstance(exc, KeyError):
        logger.debug("Issue %s disappeared while enriching invalid-transition payload", issue_id, exc_info=True)
        return
    logger.warning("Failed to enrich invalid-transition payload for %s", issue_id, exc_info=True)


def _transition_error_payload(db: Any, issue_id: str, exc: BaseException) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": str(exc), "code": ErrorCode.INVALID_TRANSITION}
    if isinstance(exc, AmbiguousTransitionError):
        details: dict[str, Any] = {
            "type": exc.type_name,
            "candidates": exc.candidates,
        }
        if exc.current_status is not None:
            details["current_status"] = exc.current_status
        payload["details"] = details
        return payload
    if isinstance(exc, InvalidTransitionError) and exc.valid_transitions is not None:
        payload["valid_transitions"] = exc.valid_transitions
        payload["hint"] = "Use 'filigree transitions <id>' to see allowed state changes"
        return payload
    try:
        transitions = db.get_valid_transitions(issue_id)
        payload["valid_transitions"] = [{"to": t.to, "category": t.category, "ready": t.ready} for t in transitions]
        payload["hint"] = "Use 'filigree transitions <id>' to see allowed state changes"
    except Exception as enrich_exc:
        _log_transition_enrichment_failure(issue_id, enrich_exc)
    return payload


def _range_check_int(value: int | None, name: str, *, min_val: int, max_val: int, as_json: bool) -> None:
    """Validate that ``value`` (when not ``None``) sits in ``[min_val, max_val]``.

    Run in the command body — not as a Click callback or ``click.IntRange``
    type — because the 2.0 envelope emission depends on ``as_json``, which
    is only reliably available after all options have been parsed. Click
    callbacks fire in cmdline order (``--priority 99 --json`` processes
    priority first), so at callback time ``as_json`` may not yet be in
    ``ctx.params``; running the check here is honest about that ordering
    constraint and lets ``--json`` invocations emit the unified envelope
    (Phase E §9) rather than Click's stderr usage error.

    On failure, either emits the 2.0 envelope (``--json``) or a plain
    error (default) and exits 1. ``None`` values are treated as
    "filter unset" and pass through unchanged.
    """
    if value is None:
        return
    if not min_val <= value <= max_val:
        msg = f"{name} must be between {min_val} and {max_val}, got {value}"
        if as_json:
            click.echo(json_mod.dumps({"error": msg, "code": ErrorCode.VALIDATION}))
        else:
            click.echo(f"Error: {msg}", err=True)
        sys.exit(1)


def _range_check_priority(priority: int, *, as_json: bool) -> None:
    """Validate required ``--priority`` is in the 0..4 range.

    Thin wrapper over ``_range_check_int`` for the create-style commands
    where ``--priority`` is a non-optional ``int``. The wrapper preserves
    the existing error wording (``"Priority must be ..."``) that callers
    and the boundary-validation tests pin.
    """
    _range_check_int(priority, "Priority", min_val=0, max_val=4, as_json=as_json)


def _min_check_int(value: int, name: str, *, min_val: int, as_json: bool) -> None:
    """Validate an integer lower bound inside the command body.

    Like ``_range_check_int``, this is intentionally not a Click callback so
    JSON callers receive the unified envelope instead of Click's usage error.
    """
    if value < min_val:
        msg = f"{name} must be >= {min_val}, got {value}"
        if as_json:
            click.echo(json_mod.dumps({"error": msg, "code": ErrorCode.VALIDATION}))
        else:
            click.echo(f"Error: {msg}", err=True)
        sys.exit(1)


@click.command(cls=ActorCommand)
@click.argument("title")
@click.option(
    "--type",
    "issue_type",
    default="task",
    help=(
        "Issue type. Core/planning examples: task, bug, feature, epic, milestone, phase, step; requirement requires the requirements pack."
    ),
)
@click.option("--priority", "-p", default=2, type=int, help="Priority 0-4 (0=critical)")
@click.option("--parent", "--parent-issue-id", "parent", default=None, help="Parent issue ID")
@click.option("--assignee", default="", help="Assignee")
@click.option("--description", "-d", default="", help="Description")
@click.option("--notes", default="", help="Notes")
@click.option("--label", "-l", multiple=True, help="Labels (repeatable)")
@click.option("--dep", multiple=True, help="Depends on issue IDs (repeatable)")
@click.option("--field", "-f", multiple=True, help="Custom field as key=value (repeatable)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def create(
    ctx: click.Context,
    title: str,
    issue_type: str,
    priority: int,
    parent: str | None,
    assignee: str,
    description: str,
    notes: str,
    label: tuple[str, ...],
    dep: tuple[str, ...],
    field: tuple[str, ...],
    as_json: bool,
) -> None:
    """Create a new issue."""
    _range_check_priority(priority, as_json=as_json)
    fields = {}
    for f in field:
        if "=" not in f:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Invalid field format: {f} (expected key=value)", "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Invalid field format: {f} (expected key=value)", err=True)
            sys.exit(1)
        k, v = f.split("=", 1)
        fields[k] = v

    with get_db() as db:
        try:
            issue = db.create_issue(
                title,
                type=issue_type,
                priority=priority,
                parent_id=parent,
                assignee=assignee,
                description=description,
                notes=notes,
                labels=list(label) if label else None,
                deps=list(dep) if dep else None,
                fields=fields or None,
                actor=ctx.obj["actor"],
            )
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        if as_json:
            click.echo(json_mod.dumps(issue_to_public(issue), indent=2, default=str))
        else:
            click.echo(f"Created {issue.id}: {issue.title}")
            click.echo("Next: filigree ready")
        refresh_summary(db)


def _show_impl(issue_id: str, as_json: bool, with_files: bool) -> None:
    with get_db() as db:
        try:
            issue = db.get_issue(issue_id)
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Not found: {issue_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Not found: {issue_id}", err=True)
            sys.exit(1)

        file_assocs = db.get_issue_files(issue_id) if with_files else []

        if as_json:
            out: dict[str, Any] = dict(issue_to_public(issue))
            if with_files:
                out["files"] = file_assocs
            click.echo(json_mod.dumps(out, indent=2, default=str))
            return

        click.echo(f"ID:       {issue.id}")
        click.echo(f"Title:    {issue.title}")
        click.echo(f"Status:   {issue.status}")
        click.echo(f"Priority: P{issue.priority}")
        click.echo(f"Type:     {issue.type}")
        if issue.parent_id:
            click.echo(f"Parent:   {issue.parent_id}")
        if issue.assignee:
            click.echo(f"Assignee: {issue.assignee}")
        click.echo(f"Created:  {issue.created_at}")
        if issue.closed_at:
            click.echo(f"Closed:   {issue.closed_at}")
        if issue.labels:
            click.echo(f"Labels:   {', '.join(issue.labels)}")
        if issue.is_ready:
            click.echo("Ready:    YES (no blockers)")
        if issue.blocked_by:
            click.echo(f"Blocked by: {', '.join(issue.blocked_by)}")
        if issue.blocks:
            click.echo(f"Blocks:   {', '.join(issue.blocks)}")
        if issue.children:
            click.echo(f"Children: {', '.join(issue.children)}")
        if issue.description:
            click.echo(f"\n--- Description ---\n{issue.description}")
        if issue.notes:
            click.echo(f"\n--- Notes ---\n{issue.notes}")
        if issue.fields:
            click.echo("\n--- Fields ---")
            for k, v in issue.fields.items():
                click.echo(f"  {k}: {v}")
        if with_files and file_assocs:
            click.echo("\n--- Files ---")
            for assoc in file_assocs:
                click.echo(f"  [{assoc['assoc_type']}] {assoc['file_path']} (file_id={assoc['file_id']})")


@click.command()
@click.argument("issue_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "--with-files/--no-files",
    "with_files",
    default=False,
    help="Include file associations (default: off)",
)
def show(issue_id: str, as_json: bool, with_files: bool) -> None:
    """Show issue details."""
    _show_impl(issue_id, as_json, with_files)


@click.command("get-issue")
@click.argument("issue_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "--with-files/--no-files",
    "with_files",
    default=False,
    help="Include file associations (default: off)",
)
def get_issue_cmd(issue_id: str, as_json: bool, with_files: bool) -> None:
    """Show issue details. Alias for `show`."""
    _show_impl(issue_id, as_json, with_files)


def _list_issues_impl(
    status: str | None,
    issue_type: str | None,
    priority: int | None,
    priority_min: int | None,
    priority_max: int | None,
    parent: str | None,
    assignee: str | None,
    label: tuple[str, ...],
    label_prefix: str | None,
    not_label: str | None,
    sort_by: str,
    direction: str,
    limit: int,
    offset: int,
    as_json: bool,
) -> None:
    _range_check_int(priority, "priority", min_val=0, max_val=4, as_json=as_json)
    _range_check_int(priority_min, "priority_min", min_val=0, max_val=4, as_json=as_json)
    _range_check_int(priority_max, "priority_max", min_val=0, max_val=4, as_json=as_json)
    if priority is not None and (priority_min is not None or priority_max is not None):
        msg = "--priority is exact-match and cannot be combined with --priority-min/--priority-max"
        if as_json:
            click.echo(json_mod.dumps({"error": msg, "code": ErrorCode.VALIDATION}))
        else:
            click.echo(f"Error: {msg}", err=True)
        sys.exit(1)
    _range_check_int(limit, "limit", min_val=0, max_val=_MAX_SQLITE_OVERFETCH_LIMIT, as_json=as_json)
    _range_check_int(offset, "offset", min_val=0, max_val=_MAX_SQLITE_OFFSET, as_json=as_json)
    with get_db() as db:
        label_filter = list(label) if label else None
        try:
            issues = db.list_issues(
                status=status,
                type=issue_type,
                priority=priority,
                priority_min=priority_min,
                priority_max=priority_max,
                parent_id=parent,
                assignee=assignee,
                label=label_filter,
                label_prefix=label_prefix,
                not_label=not_label,
                sort_by=sort_by,
                direction=direction,
                limit=limit + 1 if limit > 0 else limit,
                offset=offset,
            )
        except ValueError as e:
            # Validation errors reach here from db_issues for unknown virtual
            # labels and malformed label_prefix. JSON callers expect the 2.0
            # envelope shape; only the plain-text path keeps Click's default.
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
                sys.exit(1)
            raise click.ClickException(str(e)) from e

        has_more = limit > 0 and len(issues) > limit
        issues = issues[:limit] if has_more else issues

        if as_json:
            list_payload: dict[str, Any] = {"items": [issue_to_public(i) for i in issues], "has_more": has_more}
            if has_more:
                list_payload["next_offset"] = offset + len(issues)
            click.echo(json_mod.dumps(list_payload, indent=2, default=str))
            return

        for issue in issues:
            ready_marker = " *" if issue.is_ready else ""
            click.echo(f"P{issue.priority} {issue.id} [{issue.type}] {issue.status:<12} {issue.title}{ready_marker}")

        click.echo(f"\n{len(issues)} issues")


@click.command("list")
@click.option("--status", default=None, help="Filter by status")
@click.option("--type", "issue_type", default=None, help="Filter by type")
@click.option("--priority", "-p", default=None, type=int, help="Filter by priority")
@click.option("--priority-min", default=None, type=int, help="Minimum priority (0=critical)")
@click.option("--priority-max", default=None, type=int, help="Maximum priority")
@click.option("--parent", "--parent-issue-id", "parent", default=None, help="Filter by parent issue ID")
@click.option("--assignee", default=None, help="Filter by assignee")
@click.option("--label", "-l", multiple=True, help="Filter by label (repeatable, AND logic). Supports virtuals.")
@click.option("--label-prefix", default=None, help="Filter by label namespace prefix (include trailing colon)")
@click.option("--not-label", default=None, help="Exclude issues with this label")
@click.option("--sort-by", default="priority", help="Sort by priority, created_at, or updated_at")
@click.option("--direction", default="asc", help="Sort direction: asc or desc")
@click.option("--limit", default=100, type=int, help="Max results (default 100)")
@click.option("--offset", default=0, type=int, help="Skip first N results")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_cmd(
    status: str | None,
    issue_type: str | None,
    priority: int | None,
    priority_min: int | None,
    priority_max: int | None,
    parent: str | None,
    assignee: str | None,
    label: tuple[str, ...],
    label_prefix: str | None,
    not_label: str | None,
    sort_by: str,
    direction: str,
    limit: int,
    offset: int,
    as_json: bool,
) -> None:
    """List issues with optional filters."""
    _list_issues_impl(
        status,
        issue_type,
        priority,
        priority_min,
        priority_max,
        parent,
        assignee,
        label,
        label_prefix,
        not_label,
        sort_by,
        direction,
        limit,
        offset,
        as_json,
    )


@click.command("list-issues")
@click.option("--status", default=None, help="Filter by status")
@click.option("--type", "issue_type", default=None, help="Filter by type")
@click.option("--priority", "-p", default=None, type=int, help="Filter by priority")
@click.option("--priority-min", default=None, type=int, help="Minimum priority (0=critical)")
@click.option("--priority-max", default=None, type=int, help="Maximum priority")
@click.option("--parent", "--parent-issue-id", "parent", default=None, help="Filter by parent issue ID")
@click.option("--assignee", default=None, help="Filter by assignee")
@click.option("--label", "-l", multiple=True, help="Filter by label (repeatable, AND logic). Supports virtuals.")
@click.option("--label-prefix", default=None, help="Filter by label namespace prefix (include trailing colon)")
@click.option("--not-label", default=None, help="Exclude issues with this label")
@click.option("--sort-by", default="priority", help="Sort by priority, created_at, or updated_at")
@click.option("--direction", default="asc", help="Sort direction: asc or desc")
@click.option("--limit", default=100, type=int, help="Max results (default 100)")
@click.option("--offset", default=0, type=int, help="Skip first N results")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_issues_cmd(
    status: str | None,
    issue_type: str | None,
    priority: int | None,
    priority_min: int | None,
    priority_max: int | None,
    parent: str | None,
    assignee: str | None,
    label: tuple[str, ...],
    label_prefix: str | None,
    not_label: str | None,
    sort_by: str,
    direction: str,
    limit: int,
    offset: int,
    as_json: bool,
) -> None:
    """List issues with optional filters. Alias for `list`."""
    _list_issues_impl(
        status,
        issue_type,
        priority,
        priority_min,
        priority_max,
        parent,
        assignee,
        label,
        label_prefix,
        not_label,
        sort_by,
        direction,
        limit,
        offset,
        as_json,
    )


def _update_impl(
    actor: str,
    issue_id: str,
    status: str | None,
    priority: int | None,
    title: str | None,
    assignee: str | None,
    description: str | None,
    notes: str | None,
    parent: str | None,
    design: str | None,
    field: tuple[str, ...],
    expected_assignee: str | None,
    as_json: bool,
) -> None:
    _range_check_int(priority, "priority", min_val=0, max_val=4, as_json=as_json)
    fields = None
    # Truthiness gates would silently drop `--design=` (empty-string clear);
    # see filigree-613e9f5f66.  Distinguish unset (None) from cleared ("").
    if field or design is not None:
        fields = {}
        for f in field:
            if "=" not in f:
                if as_json:
                    click.echo(json_mod.dumps({"error": f"Invalid field format: {f}", "code": ErrorCode.VALIDATION}))
                else:
                    click.echo(f"Invalid field format: {f}", err=True)
                sys.exit(1)
            k, v = f.split("=", 1)
            fields[k] = v
        if design is not None:
            fields["design"] = design

    with get_db() as db:
        # C1: `update --status <done>` reaches the same close as the `close`
        # command (close routes through update_issue) — gate it the same way.
        gate = governance.evaluate_status_change_gate(db, issue_id, status)
        if not gate.allowed:
            code = ErrorCode.INTERNAL if gate.outcome is governance.GateOutcome.INTEGRITY_FAILURE else ErrorCode.CONFLICT
            if as_json:
                click.echo(json_mod.dumps({"error": gate.reason, "code": code}))
            else:
                click.echo(gate.reason, err=True)
            sys.exit(1)
        try:
            issue = db.update_issue(
                issue_id,
                status=status,
                priority=priority,
                title=title,
                assignee=assignee,
                description=description,
                notes=notes,
                parent_id=parent,
                fields=fields,
                actor=actor,
                expected_assignee=expected_assignee,
            )
            if as_json:
                click.echo(json_mod.dumps(issue_to_public(issue), indent=2, default=str))
            else:
                click.echo(f"Updated {issue.id}: {issue.title} [{issue.status}]")
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Not found: {issue_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Not found: {issue_id}", err=True)
            sys.exit(1)
        except InvalidTransitionError as e:
            if as_json:
                click.echo(json_mod.dumps(_transition_error_payload(db, issue_id, e)))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except ValueError as e:
            if as_json:
                msg = str(e)
                if isinstance(e, ClaimConflictError):
                    click.echo(json_mod.dumps(claim_conflict_envelope(e)))
                elif classify_value_error(msg) == ErrorCode.INVALID_TRANSITION:
                    click.echo(json_mod.dumps(_transition_error_payload(db, issue_id, e)))
                else:
                    click.echo(json_mod.dumps({"error": msg, "code": classify_value_error(msg)}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        refresh_summary(db)


@click.command(cls=ActorCommand)
@click.argument("issue_id")
@click.option("--status", default=None, help="New status")
@click.option("--priority", "-p", default=None, type=int, help="New priority")
@click.option("--title", default=None, help="New title")
@click.option("--assignee", default=None, help="New assignee")
@click.option("--description", "-d", default=None, help="New description")
@click.option("--notes", default=None, help="New notes")
@click.option("--parent", "--parent-issue-id", "parent", default=None, help="New parent issue ID (empty string to clear)")
@click.option("--design", default=None, help="New design field")
@click.option("--field", "-f", multiple=True, help="Custom field as key=value (repeatable)")
@click.option("--expected-assignee", default=None, help="Expected current holder for coordinator writes")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def update(
    ctx: click.Context,
    issue_id: str,
    status: str | None,
    priority: int | None,
    title: str | None,
    assignee: str | None,
    description: str | None,
    notes: str | None,
    parent: str | None,
    design: str | None,
    field: tuple[str, ...],
    expected_assignee: str | None,
    as_json: bool,
) -> None:
    """Update an issue."""
    _update_impl(
        ctx.obj["actor"],
        issue_id,
        status,
        priority,
        title,
        assignee,
        description,
        notes,
        parent,
        design,
        field,
        expected_assignee,
        as_json,
    )


@click.command("update-issue", cls=ActorCommand)
@click.argument("issue_id")
@click.option("--status", default=None, help="New status")
@click.option("--priority", "-p", default=None, type=int, help="New priority")
@click.option("--title", default=None, help="New title")
@click.option("--assignee", default=None, help="New assignee")
@click.option("--description", "-d", default=None, help="New description")
@click.option("--notes", default=None, help="New notes")
@click.option("--parent", "--parent-issue-id", "parent", default=None, help="New parent issue ID (empty string to clear)")
@click.option("--design", default=None, help="New design field")
@click.option("--field", "-f", multiple=True, help="Custom field as key=value (repeatable)")
@click.option("--expected-assignee", default=None, help="Expected current holder for coordinator writes")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def update_issue_cmd(
    ctx: click.Context,
    issue_id: str,
    status: str | None,
    priority: int | None,
    title: str | None,
    assignee: str | None,
    description: str | None,
    notes: str | None,
    parent: str | None,
    design: str | None,
    field: tuple[str, ...],
    expected_assignee: str | None,
    as_json: bool,
) -> None:
    """Update an issue. Alias for `update`."""
    _update_impl(
        ctx.obj["actor"],
        issue_id,
        status,
        priority,
        title,
        assignee,
        description,
        notes,
        parent,
        design,
        field,
        expected_assignee,
        as_json,
    )


@click.command(cls=ActorCommand)
@click.argument("issue_ids", nargs=-1, required=True)
@click.option("--reason", default="", help="Close reason")
@click.option(
    "--status",
    default=None,
    help=(
        "Target done-category status. Optional; defaults to the first done state for the type "
        "(e.g. closed). Use this to land in an alternate done state (wont_fix, not_a_bug, "
        "cancelled) when the default isn't reachable from the current status."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help=(
        "Use the template reverse/escape transition and close from any state. Use only "
        "for cleanup flows that intentionally leave the normal workflow."
    ),
)
@click.option("--expected-assignee", default=None, help="Expected current holder for coordinator writes")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def close(
    ctx: click.Context,
    issue_ids: tuple[str, ...],
    reason: str,
    status: str | None,
    force: bool,
    expected_assignee: str | None,
    as_json: bool,
) -> None:
    """Close one or more issues."""
    with get_db() as db:
        succeeded: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        ready_before = {i.id for i in db.get_ready()} if as_json else set()
        for issue_id in issue_ids:
            try:
                # B5: a governed issue cannot be closed without verified Legis
                # binding evidence (DECISION 3 — report + skip, don't abort).
                gate = governance.evaluate_closure_gate(db, issue_id)
                if not gate.allowed:
                    code = ErrorCode.INTERNAL if gate.outcome is governance.GateOutcome.INTEGRITY_FAILURE else ErrorCode.CONFLICT
                    errors.append({"id": issue_id, "error": gate.reason, "code": code})
                    if not as_json:
                        click.echo(gate.reason, err=True)
                    continue
                annotation_warnings = db.get_annotation_closeout_warnings(issue_id)
                issue = db.close_issue(
                    issue_id,
                    reason=reason,
                    status=status,
                    actor=ctx.obj["actor"],
                    expected_assignee=expected_assignee,
                    force=force,
                )
                if as_json:
                    item: dict[str, Any] = {
                        "issue_id": issue.id,
                        "title": issue.title,
                        "status": issue.status,
                        "priority": issue.priority,
                        "type": issue.type,
                    }
                    if annotation_warnings:
                        item["annotation_warnings"] = annotation_warnings
                    succeeded.append(item)
                else:
                    click.echo(f"Closed {issue.id}: {issue.title}")
                    for warning in annotation_warnings:
                        click.echo(
                            f"Annotation warning: {warning['annotation_id']} must be considered for {warning['file_path']}",
                            err=True,
                        )
            except KeyError:
                errors.append({"id": issue_id, "error": f"Not found: {issue_id}", "code": ErrorCode.NOT_FOUND})
                if not as_json:
                    click.echo(f"Not found: {issue_id}", err=True)
            except ValueError as e:
                msg = str(e)
                code = ErrorCode.CONFLICT if isinstance(e, ClaimConflictError) else classify_value_error(msg)
                error_item: dict[str, Any] = {"id": issue_id, "error": msg, "code": code}
                if isinstance(e, ClaimConflictError):
                    envelope = claim_conflict_envelope(e)
                    error_item["details"] = envelope["details"]
                elif isinstance(e, InvalidTransitionError):
                    # Surface the structured next-step hint like update/release do,
                    # instead of dropping it in the generic ValueError arm.
                    enriched = _transition_error_payload(db, issue_id, e)
                    for key in ("valid_transitions", "hint", "details"):
                        if key in enriched:
                            error_item[key] = enriched[key]
                errors.append(error_item)
                if not as_json:
                    click.echo(msg, err=True)
        if as_json:
            # Stage 2B task 2b.3c: when the call was ``close <id>`` with a
            # single id and it failed, emit the flat 2.0 envelope instead
            # of the batch-shape wrapper. ``filigree close a b --json``
            # keeps the batch shape because batching is the documented
            # behaviour for N≥2.
            if len(issue_ids) == 1 and errors and not succeeded:
                err = errors[0]
                error_payload: dict[str, Any] = {"error": err["error"], "code": err["code"]}
                for key in ("details", "valid_transitions", "hint"):
                    if key in err:
                        error_payload[key] = err[key]
                click.echo(json_mod.dumps(error_payload))
                refresh_summary(db)
                sys.exit(1)
            # Only issues that became ready *after* the close (per docs/cli.md).
            ready = db.get_ready()
            newly_unblocked = [
                {"issue_id": i.id, "title": i.title, "status": i.status, "priority": i.priority, "type": i.type}
                for i in ready
                if i.id not in ready_before
            ]
            # Batch envelope contract: ``newly_unblocked`` is OMITTED when
            # empty (matches MCP ``batch_close`` at mcp_tools/issues.py).
            payload: dict[str, Any] = {"succeeded": succeeded, "failed": errors}
            if newly_unblocked:
                payload["newly_unblocked"] = newly_unblocked
            click.echo(json_mod.dumps(payload, indent=2, default=str))
        refresh_summary(db)
        if errors:
            sys.exit(1)


@click.command(cls=ActorCommand)
@click.argument("issue_ids", nargs=-1, required=True)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def reopen(ctx: click.Context, issue_ids: tuple[str, ...], as_json: bool) -> None:
    """Reopen one or more closed issues to their last non-done statuses."""
    with get_db() as db:
        reopened: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for issue_id in issue_ids:
            try:
                issue = db.reopen_issue(issue_id, actor=ctx.obj["actor"])
                if as_json:
                    reopened.append(
                        {
                            "issue_id": issue.id,
                            "title": issue.title,
                            "status": issue.status,
                            "priority": issue.priority,
                            "type": issue.type,
                        }
                    )
                else:
                    click.echo(f"Reopened {issue.id}: {issue.title} [{issue.status}]")
            except KeyError:
                errors.append({"id": issue_id, "error": f"Not found: {issue_id}", "code": ErrorCode.NOT_FOUND})
                if not as_json:
                    click.echo(f"Not found: {issue_id}", err=True)
            except ValueError as e:
                msg = str(e)
                code = ErrorCode.CONFLICT if isinstance(e, ClaimConflictError) else classify_value_error(msg)
                error_item: dict[str, Any] = {"id": issue_id, "error": msg, "code": code}
                if isinstance(e, ClaimConflictError):
                    envelope = claim_conflict_envelope(e)
                    error_item["details"] = envelope["details"]
                errors.append(error_item)
                if not as_json:
                    click.echo(f"Error reopening {issue_id}: {e}", err=True)
        if as_json:
            payload: dict[str, Any] = {"succeeded": reopened, "failed": errors}
            click.echo(json_mod.dumps(payload, indent=2, default=str))
        refresh_summary(db)
        if errors:
            sys.exit(1)


@click.command(cls=ActorCommand)
@click.argument("issue_id")
@click.option("--assignee", default=None, help="Who is claiming (agent name); defaults to an explicitly provided --actor")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def claim(ctx: click.Context, issue_id: str, assignee: str | None, as_json: bool) -> None:
    """Atomically claim an open issue or released in-progress handoff."""
    # FIL-3: --assignee defaults from an explicit --actor (group-level or
    # post-verb); with neither, fail naming both options.
    assignee = _resolve_cli_claim_assignee(ctx, assignee, as_json=as_json)
    # Mirror the MCP handler's assignee pre-validation (mcp_tools/issues.py
    # lines 603-604) so a blank value surfaces as VALIDATION, not CONFLICT.
    if not assignee.strip():
        if as_json:
            click.echo(json_mod.dumps({"error": "assignee must be a non-empty string", "code": ErrorCode.VALIDATION}))
        else:
            click.echo("Error: assignee must be a non-empty string", err=True)
        sys.exit(1)
    with get_db() as db:
        try:
            issue = db.claim_issue(issue_id, assignee=assignee, actor=ctx.obj["actor"])
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Not found: {issue_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Not found: {issue_id}", err=True)
            sys.exit(1)
        except WrongProjectError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except ValueError as e:
            msg = str(e)
            if as_json:
                if isinstance(e, ClaimConflictError):
                    click.echo(json_mod.dumps(claim_conflict_envelope(e)))
                else:
                    code = classify_value_error(msg)
                    if code == ErrorCode.INVALID_TRANSITION:
                        click.echo(json_mod.dumps(_transition_error_payload(db, issue_id, e)))
                    else:
                        click.echo(json_mod.dumps({"error": msg, "code": code}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        if as_json:
            click.echo(json_mod.dumps(issue_to_public(issue), indent=2, default=str))
        else:
            click.echo(f"Claimed {issue.id}: {issue.title} [{issue.status}] -> {assignee}")
        refresh_summary(db)


@click.command("claim-next", cls=ActorCommand)
@click.option("--assignee", default=None, help="Who is claiming; defaults to an explicitly provided --actor")
@click.option("--type", "type_filter", default=None, help="Filter by issue type")
@click.option("--priority-min", default=None, type=int, help="Minimum priority (0=critical)")
@click.option("--priority-max", default=None, type=int, help="Maximum priority")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def claim_next(
    ctx: click.Context,
    assignee: str | None,
    type_filter: str | None,
    priority_min: int | None,
    priority_max: int | None,
    as_json: bool,
) -> None:
    """Claim the highest-priority ready issue matching filters."""
    # FIL-3: --assignee defaults from an explicit --actor (group-level or
    # post-verb); with neither, fail naming both options.
    assignee = _resolve_cli_claim_assignee(ctx, assignee, as_json=as_json)
    # Mirror the MCP handler (mcp_tools/issues.py lines 646-647): blank assignee
    # is bad user input, not a race. The only ValueError db.claim_next propagates
    # is "Assignee cannot be empty" (inner claim_issue errors are swallowed).
    if not assignee.strip():
        if as_json:
            click.echo(json_mod.dumps({"error": "assignee must be a non-empty string", "code": ErrorCode.VALIDATION}))
        else:
            click.echo("Error: assignee must be a non-empty string", err=True)
        sys.exit(1)
    _range_check_int(priority_min, "priority_min", min_val=0, max_val=4, as_json=as_json)
    _range_check_int(priority_max, "priority_max", min_val=0, max_val=4, as_json=as_json)
    with get_db() as db:
        try:
            issue = db.claim_next(
                assignee,
                type_filter=type_filter,
                priority_min=priority_min,
                priority_max=priority_max,
                actor=ctx.obj["actor"],
            )
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        if issue is None:
            if as_json:
                # Mirror the MCP ClaimNextEmptyResponse shape (types/api.py:266).
                click.echo(json_mod.dumps({"status": "empty", "reason": "No ready issues matching filters"}))
            else:
                click.echo("No issues available")
        else:
            if as_json:
                # Mirror the MCP ClaimNextResponse shape (types/api.py:140) — emit
                # the issue dict plus selection_reason via the shared formatter.
                payload = public_issue_with(issue, selection_reason=issue.format_claim_next_reason())
                click.echo(json_mod.dumps(payload, indent=2, default=str))
            else:
                click.echo(f"Claimed {issue.id}: {issue.title} [{issue.status}] -> {assignee}")
        refresh_summary(db)


def _release_impl(
    actor: str,
    issue_id: str,
    as_json: bool,
    *,
    if_held: bool = False,
    expected_assignee: str | None = None,
    reason: str = "",
) -> None:
    with get_db() as db:
        try:
            issue = db.release_claim(issue_id, actor=actor, if_held=if_held, expected_assignee=expected_assignee, reason=reason)
            if as_json:
                click.echo(json_mod.dumps(issue_to_public(issue), indent=2, default=str))
            else:
                click.echo(f"Released {issue.id}: {issue.title} [{issue.status}]")
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Not found: {issue_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Not found: {issue_id}", err=True)
            sys.exit(1)
        except WrongProjectError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except InvalidTransitionError as e:
            if as_json:
                payload: dict[str, Any] = {"error": str(e), "code": ErrorCode.INVALID_TRANSITION}
                if e.valid_transitions is not None:
                    payload["valid_transitions"] = e.valid_transitions
                    payload["hint"] = "Use 'filigree transitions <id>' to see allowed state changes"
                else:
                    try:
                        transitions = db.get_valid_transitions(issue_id)
                        payload["valid_transitions"] = [{"to": t.to, "category": t.category, "ready": t.ready} for t in transitions]
                        payload["hint"] = "Use 'filigree transitions <id>' to see allowed state changes"
                    except Exception as enrich_exc:
                        _log_transition_enrichment_failure(issue_id, enrich_exc)
                click.echo(json_mod.dumps(payload))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except ClaimConflictError as e:
            if as_json:
                click.echo(json_mod.dumps(claim_conflict_envelope(e)))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except ValueError as e:
            if as_json:
                code = classify_release_claim_error(issue_id, e)
                click.echo(json_mod.dumps({"error": str(e), "code": code}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        refresh_summary(db)


@click.command("release", cls=ActorCommand)
@click.argument("issue_id")
@click.option(
    "--if-held",
    is_flag=True,
    help="Idempotently release only if held by --expected-assignee or the global --actor; no-op if unassigned.",
)
@click.option("--expected-assignee", default=None, help="Expected current assignee for --if-held coordinator flows.")
@click.option("--reason", default="", help="Audit reason for releasing the claim.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def release(
    ctx: click.Context,
    issue_id: str,
    if_held: bool,
    expected_assignee: str | None,
    reason: str,
    as_json: bool,
) -> None:
    """Release a claimed issue by clearing its assignee."""
    _release_impl(ctx.obj["actor"], issue_id, as_json, if_held=if_held, expected_assignee=expected_assignee, reason=reason)


@click.command("release-claim", cls=ActorCommand)
@click.argument("issue_id")
@click.option(
    "--if-held",
    is_flag=True,
    help="Idempotently release only if held by --expected-assignee or the global --actor; no-op if unassigned.",
)
@click.option("--expected-assignee", default=None, help="Expected current assignee for --if-held coordinator flows.")
@click.option("--reason", default="", help="Audit reason for releasing the claim.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def release_claim_cmd(
    ctx: click.Context,
    issue_id: str,
    if_held: bool,
    expected_assignee: str | None,
    reason: str,
    as_json: bool,
) -> None:
    """Release a claimed issue by clearing its assignee. Alias for `release`."""
    _release_impl(ctx.obj["actor"], issue_id, as_json, if_held=if_held, expected_assignee=expected_assignee, reason=reason)


@click.command("release-my-claims", cls=ActorCommand)
@click.option("--label", default=None, help="Restrict to issues carrying this exact label.")
@click.option("--label-prefix", default=None, help="Restrict to issues with a label starting with this prefix (must end with ':').")
@click.option("--dry-run", is_flag=True, help="List the issues that would be released without making changes.")
@click.option(
    "--no-revert-status",
    "no_revert_status",
    is_flag=True,
    help="Do NOT revert wip-category issues back to an open predecessor on release.",
)
@click.option("--reason", default="", help="Audit reason recorded on each release event.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def release_my_claims_cmd(
    ctx: click.Context,
    label: str | None,
    label_prefix: str | None,
    dry_run: bool,
    no_revert_status: bool,
    reason: str,
    as_json: bool,
) -> None:
    """Bulk-release every live claim held by --actor.

    Designed for end-of-session cleanup. Tag scratch with --label or --label-prefix
    to restrict; pair with the cluster:* convention to release just one session's
    worth of claims at a time.
    """
    actor = ctx.obj.get("actor") or ""
    if not actor.strip():
        if as_json:
            click.echo(json_mod.dumps({"error": "release-my-claims requires --actor", "code": ErrorCode.VALIDATION}))
        else:
            click.echo("Error: release-my-claims requires --actor", err=True)
        sys.exit(1)
    with get_db() as db:
        try:
            released, failures = db.release_my_claims(
                actor=actor,
                label=label,
                label_prefix=label_prefix,
                dry_run=dry_run,
                revert_status=not no_revert_status,
                reason=reason,
            )
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        if as_json:
            payload: dict[str, Any] = {
                "succeeded": [issue_to_public(i) for i in released],
                "failed": list(failures),
            }
            if dry_run:
                payload["dry_run"] = True
            click.echo(json_mod.dumps(payload, indent=2, default=str))
            if not dry_run:
                refresh_summary(db)
            if failures:
                sys.exit(1)
            return
        verb = "would release" if dry_run else "released"
        click.echo(f"{verb} {len(released)} claim(s) for actor {actor!r}")
        for issue in released:
            click.echo(f"  {issue.id} [{issue.type}] {issue.title}")
        if failures:
            click.echo(f"{len(failures)} failure(s):", err=True)
            for fail in failures:
                click.echo(f"  {fail['id']}: {fail['error']}", err=True)
        if not dry_run:
            refresh_summary(db)
        if failures:
            sys.exit(1)


@click.command("heartbeat-work", cls=ActorCommand)
@click.argument("issue_id")
@click.option("--expected-assignee", default=None, help="Expected current assignee; defaults to global --actor.")
@click.option("--lease-hours", default=48, type=int, help="Lease duration from this heartbeat.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def heartbeat_work_cmd(
    ctx: click.Context,
    issue_id: str,
    expected_assignee: str | None,
    lease_hours: int,
    as_json: bool,
) -> None:
    """Refresh claim liveness metadata for the current holder."""
    _range_check_int(lease_hours, "lease_hours", min_val=1, max_val=8760, as_json=as_json)
    with get_db() as db:
        try:
            issue = db.heartbeat_work(
                issue_id,
                actor=ctx.obj["actor"],
                expected_assignee=expected_assignee,
                lease_hours=lease_hours,
            )
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Not found: {issue_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Not found: {issue_id}", err=True)
            sys.exit(1)
        except WrongProjectError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except ValueError as e:
            if as_json:
                if isinstance(e, ClaimConflictError):
                    click.echo(json_mod.dumps(claim_conflict_envelope(e)))
                else:
                    click.echo(json_mod.dumps({"error": str(e), "code": classify_value_error(str(e))}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        if as_json:
            click.echo(json_mod.dumps(issue_to_public(issue), indent=2, default=str))
        else:
            click.echo(f"Heartbeat recorded for {issue.id}: assignee={issue.assignee}")
        refresh_summary(db)


@click.command("stale-claims")
@click.option("--stale-after-hours", default=48, type=int, help="Legacy assignment age threshold.")
@click.option(
    "--expires-within-hours",
    type=int,
    default=None,
    help="Also include active explicit leases expiring within this many hours.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def stale_claims_cmd(stale_after_hours: int, expires_within_hours: int | None, as_json: bool) -> None:
    """List assigned issues whose claim liveness is stale."""
    _range_check_int(stale_after_hours, "stale_after_hours", min_val=1, max_val=8760, as_json=as_json)
    _range_check_int(expires_within_hours, "expires_within_hours", min_val=1, max_val=8760, as_json=as_json)
    with get_db() as db:
        try:
            issues = db.get_stale_claims(
                stale_after_hours=stale_after_hours,
                expires_within_hours=expires_within_hours,
            )
        except ValueError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        if as_json:
            click.echo(
                json_mod.dumps(
                    {"items": [issue_to_public(issue) for issue in issues], "has_more": False},
                    indent=2,
                    default=str,
                )
            )
        elif not issues:
            click.echo("No stale claims")
        else:
            for issue in issues:
                click.echo(f'P{issue.priority} {issue.id} [{issue.type}] "{issue.title}" -> {issue.assignee}')


@click.command("reclaim", cls=ActorCommand)
@click.argument("issue_id")
@click.option("--assignee", required=True, help="New assignee")
@click.option("--expected-assignee", required=True, help="Current assignee expected by the caller")
@click.option("--reason", required=True, help="Why the claim is being reclaimed")
@click.option("--lease-hours", default=48, type=int, help="Lease duration for the new assignee.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def reclaim_cmd(
    ctx: click.Context,
    issue_id: str,
    assignee: str,
    expected_assignee: str,
    reason: str,
    lease_hours: int,
    as_json: bool,
) -> None:
    """Safely transfer a claim when the observed holder matches."""
    if not assignee.strip():
        if as_json:
            click.echo(json_mod.dumps({"error": "assignee must be a non-empty string", "code": ErrorCode.VALIDATION}))
        else:
            click.echo("Error: assignee must be a non-empty string", err=True)
        sys.exit(1)
    if not expected_assignee.strip():
        if as_json:
            click.echo(json_mod.dumps({"error": "expected_assignee must be a non-empty string", "code": ErrorCode.VALIDATION}))
        else:
            click.echo("Error: expected_assignee must be a non-empty string", err=True)
        sys.exit(1)
    if not reason.strip():
        if as_json:
            click.echo(json_mod.dumps({"error": "reason must be a non-empty string", "code": ErrorCode.VALIDATION}))
        else:
            click.echo("Error: reason must be a non-empty string", err=True)
        sys.exit(1)
    _range_check_int(lease_hours, "lease_hours", min_val=1, max_val=8760, as_json=as_json)
    with get_db() as db:
        try:
            issue = db.reclaim_issue(
                issue_id,
                assignee=assignee,
                expected_assignee=expected_assignee,
                reason=reason,
                actor=ctx.obj["actor"],
                lease_hours=lease_hours,
            )
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Not found: {issue_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Not found: {issue_id}", err=True)
            sys.exit(1)
        except WrongProjectError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except ValueError as e:
            if as_json:
                if isinstance(e, ClaimConflictError):
                    click.echo(json_mod.dumps(claim_conflict_envelope(e)))
                else:
                    click.echo(json_mod.dumps({"error": str(e), "code": classify_value_error(str(e))}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        if as_json:
            click.echo(json_mod.dumps(issue_to_public(issue), indent=2, default=str))
        else:
            click.echo(f"Reclaimed {issue.id}: {expected_assignee} -> {issue.assignee}")
        refresh_summary(db)


def _undo_impl(actor: str, issue_id: str, as_json: bool) -> None:
    with get_db() as db:
        try:
            result = db.undo_last(issue_id, actor=actor)
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Not found: {issue_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Not found: {issue_id}", err=True)
            sys.exit(1)

        if as_json:
            click.echo(json_mod.dumps(result, indent=2, default=str))
            if not result["undone"]:
                sys.exit(1)
        else:
            if result["undone"]:
                click.echo(f"Undone {result['event_type']} (event #{result['event_id']}) on {issue_id}")
            else:
                click.echo(f"Cannot undo: {result['reason']}", err=True)
                sys.exit(1)
        refresh_summary(db)


@click.command(cls=ActorCommand)
@click.argument("issue_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def undo(ctx: click.Context, issue_id: str, as_json: bool) -> None:
    """Undo the most recent reversible action on an issue."""
    _undo_impl(ctx.obj["actor"], issue_id, as_json)


@click.command("undo-last", cls=ActorCommand)
@click.argument("issue_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def undo_last_cmd(ctx: click.Context, issue_id: str, as_json: bool) -> None:
    """Undo the most recent reversible action on an issue. Alias for `undo`."""
    _undo_impl(ctx.obj["actor"], issue_id, as_json)


@click.command("delete-issue", cls=ActorCommand)
@click.argument("issue_id")
@click.option(
    "--force",
    is_flag=True,
    help="Delete a non-terminal issue; orphan children and cascade inbound dependencies",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def delete_issue_cmd(ctx: click.Context, issue_id: str, force: bool, as_json: bool) -> None:
    """Hard-delete an issue and all of its dependent rows. IRREVERSIBLE.

    Destroys the issue's events, so `undo-last` cannot reverse it. Writes a
    deletion tombstone so federation consumers see an issue_deleted record on
    /changes. Refuses unless the issue is in a done-category status or
    'archived', has no children, and has no other issues blocked by it. Pass
    --force to delete anyway: children orphan and inbound dependencies cascade.
    """
    with get_db() as db:
        try:
            result = db.delete_issue(issue_id, force=force, actor=ctx.obj["actor"])
            refresh_summary(db)
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Not found: {issue_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Error: issue {issue_id} not found", err=True)
            sys.exit(1)
        except IssueDeletionRefusedError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.CONFLICT}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
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
            click.echo(json_mod.dumps(result, indent=2, default=str))
        else:
            click.echo(
                f"Deleted {issue_id} "
                f"({result['deleted_events']} event(s), "
                f"{result['orphaned_children']} child(ren) orphaned, "
                f"{result['deleted_dependencies_in']} inbound dep(s) cascaded)"
            )


@click.command("start-work")
@click.argument("issue_id")
@click.option("--assignee", default=None, help="Who is starting work (agent name); defaults to an explicitly provided --actor")
@click.option("--target-status", default=None, help="Override wip status (defaults to reachable wip target)")
@click.option("--actor", default=None, help="Actor for audit trail (defaults to --assignee; also fills an omitted --assignee)")
@click.option("--advance", is_flag=True, help="Walk soft transitions to the nearest wip state (e.g. triage->confirmed->fixing)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def start_work(
    ctx: click.Context,
    issue_id: str,
    assignee: str | None,
    target_status: str | None,
    actor: str | None,
    advance: bool,
    as_json: bool,
) -> None:
    """Atomically claim an issue and transition it to its wip status."""
    # FIL-3: --assignee defaults from the command's own --actor, else from a
    # group-level explicit --actor; with neither, fail naming both options.
    assignee = _resolve_cli_claim_assignee(ctx, assignee, as_json=as_json, post_verb_actor=actor)
    # Mirror MCP: blank assignee is bad user input, not a race.
    if not assignee.strip():
        if as_json:
            click.echo(json_mod.dumps({"error": "assignee must be a non-empty string", "code": ErrorCode.VALIDATION}))
        else:
            click.echo("Error: assignee must be a non-empty string", err=True)
        sys.exit(1)
    # Mirror MCP: actor defaults to assignee when not specified, and is
    # sanitized through the same validator the group-level --actor uses.
    resolved_actor = _resolve_and_sanitize_actor(actor, assignee, as_json=as_json)
    with get_db() as db:
        try:
            issue = db.start_work(
                issue_id,
                assignee=assignee,
                target_status=target_status,
                actor=resolved_actor,
                advance=advance,
            )
        except KeyError:
            if as_json:
                click.echo(json_mod.dumps({"error": f"Issue not found: {issue_id}", "code": ErrorCode.NOT_FOUND}))
            else:
                click.echo(f"Error: Issue not found: {issue_id}", err=True)
            sys.exit(1)
        except (AmbiguousTransitionError, InvalidTransitionError) as e:
            if as_json:
                click.echo(json_mod.dumps(_transition_error_payload(db, issue_id, e)))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except WrongProjectError as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.VALIDATION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except ClaimConflictError as e:
            # Optimistic-lock conflict — distinct error code so JSON
            # consumers can branch on CONFLICT vs VALIDATION; mirrors the
            # ``claim``, ``release``, and ``reclaim`` CLI paths.
            if as_json:
                click.echo(json_mod.dumps(claim_conflict_envelope(e)))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except ValueError as e:
            msg = str(e)
            code = classify_value_error(msg)
            if as_json:
                if code == ErrorCode.INVALID_TRANSITION:
                    # Build enriched transition error (best-effort).
                    payload: dict[str, Any] = {"error": msg, "code": ErrorCode.INVALID_TRANSITION}
                    if isinstance(e, InvalidTransitionError) and e.valid_transitions is not None:
                        payload["valid_transitions"] = e.valid_transitions
                        payload["hint"] = "Use 'filigree transitions <id>' to see allowed state changes"
                    else:
                        try:
                            transitions = db.get_valid_transitions(issue_id)
                            payload["valid_transitions"] = [{"to": t.to, "category": t.category, "ready": t.ready} for t in transitions]
                            payload["hint"] = "Use 'filigree transitions <id>' to see allowed state changes"
                        except Exception as enrich_exc:
                            # Enrichment is best-effort — must never mask the original error.
                            _log_transition_enrichment_failure(issue_id, enrich_exc)
                    click.echo(json_mod.dumps(payload))
                else:
                    click.echo(json_mod.dumps({"error": msg, "code": code}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        if as_json:
            click.echo(json_mod.dumps(issue_to_public(issue), indent=2, default=str))
        else:
            click.echo(f"Started work on {issue.id}: status={issue.status}, assignee={issue.assignee}")
        refresh_summary(db)


@click.command("start-next-work")
@click.option("--assignee", default=None, help="Who is starting work (agent name); defaults to an explicitly provided --actor")
@click.option("--type", "type_filter", default=None, help="Filter by issue type")
@click.option("--priority-min", default=None, type=int, help="Minimum priority (0=critical)")
@click.option("--priority-max", default=None, type=int, help="Maximum priority")
@click.option("--target-status", default=None, help="Override wip status (defaults to reachable wip target)")
@click.option("--actor", default=None, help="Actor for audit trail (defaults to --assignee; also fills an omitted --assignee)")
@click.option("--advance", is_flag=True, help="Walk soft transitions to wip so multi-hop types (e.g. triage bugs) become startable")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def start_next_work(
    ctx: click.Context,
    assignee: str | None,
    type_filter: str | None,
    priority_min: int | None,
    priority_max: int | None,
    target_status: str | None,
    actor: str | None,
    advance: bool,
    as_json: bool,
) -> None:
    """Claim and start the highest-priority ready issue matching filters."""
    # FIL-3: --assignee defaults from the command's own --actor, else from a
    # group-level explicit --actor; with neither, fail naming both options.
    assignee = _resolve_cli_claim_assignee(ctx, assignee, as_json=as_json, post_verb_actor=actor)
    # Mirror MCP: blank assignee is bad user input, not a race.
    if not assignee.strip():
        if as_json:
            click.echo(json_mod.dumps({"error": "assignee must be a non-empty string", "code": ErrorCode.VALIDATION}))
        else:
            click.echo("Error: assignee must be a non-empty string", err=True)
        sys.exit(1)
    _range_check_int(priority_min, "priority_min", min_val=0, max_val=4, as_json=as_json)
    _range_check_int(priority_max, "priority_max", min_val=0, max_val=4, as_json=as_json)
    # Mirror MCP: actor defaults to assignee when not specified, and is
    # sanitized through the same validator the group-level --actor uses.
    resolved_actor = _resolve_and_sanitize_actor(actor, assignee, as_json=as_json)
    with get_db() as db:
        try:
            claimed = db.start_next_work(
                assignee=assignee,
                type_filter=type_filter,
                priority_min=priority_min,
                priority_max=priority_max,
                target_status=target_status,
                actor=resolved_actor,
                advance=advance,
            )
        except (AmbiguousTransitionError, InvalidTransitionError) as e:
            if as_json:
                click.echo(json_mod.dumps({"error": str(e), "code": ErrorCode.INVALID_TRANSITION}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except ValueError as e:
            # Parity with start-work: classify status/transition/state errors
            # as INVALID_TRANSITION rather than the generic VALIDATION code.
            # No issue_id is available here (start-next-work claims one), so
            # valid_transitions enrichment is not applicable — bare envelope.
            msg = str(e)
            code = classify_value_error(msg)
            if as_json:
                click.echo(json_mod.dumps({"error": msg, "code": code}))
            else:
                click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        if claimed is None:
            if as_json:
                click.echo(json_mod.dumps({"status": "empty", "reason": "No ready issues matching filters"}, indent=2))
            else:
                click.echo("No ready issues matching filters")
            # Empty is not an error — exit 0.
            return

        if as_json:
            click.echo(json_mod.dumps(issue_to_public(claimed), indent=2, default=str))
        else:
            click.echo(f"Started work on {claimed.id}: status={claimed.status}, assignee={claimed.assignee}")
        refresh_summary(db)


def register(cli: click.Group) -> None:
    """Register issue commands with the CLI group."""
    cli.add_command(create)
    cli.add_command(show)
    cli.add_command(get_issue_cmd)
    cli.add_command(list_cmd)
    cli.add_command(list_issues_cmd)
    cli.add_command(update)
    cli.add_command(update_issue_cmd)
    cli.add_command(close)
    cli.add_command(reopen)
    cli.add_command(delete_issue_cmd)
    cli.add_command(claim)
    cli.add_command(claim_next)
    cli.add_command(release)
    cli.add_command(release_claim_cmd)
    cli.add_command(release_my_claims_cmd)
    cli.add_command(heartbeat_work_cmd)
    cli.add_command(stale_claims_cmd)
    cli.add_command(stale_claims_cmd, "get-stale-claims")
    cli.add_command(reclaim_cmd)
    cli.add_command(reclaim_cmd, "reclaim-issue")
    cli.add_command(undo)
    cli.add_command(undo_last_cmd)
    cli.add_command(start_work)
    cli.add_command(start_next_work)
