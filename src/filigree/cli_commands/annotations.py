"""CLI commands for shared file annotations."""

from __future__ import annotations

import json as json_mod
import sqlite3
import sys

import click

from filigree.cli_common import get_db, refresh_summary
from filigree.core import (
    VALID_ANNOTATION_INTENTS,
    VALID_ANNOTATION_RELATIONSHIPS,
    VALID_ANNOTATION_STATUSES,
    VALID_ANNOTATION_TARGET_TYPES,
)
from filigree.types.api import ErrorCode

_ANCHOR_STATES = ("current", "line_drifted", "content_changed_anchor_found", "stale", "file_missing")
_MAX_SQLITE_OFFSET = 9_223_372_036_854_775_807
_MAX_SQLITE_LIMIT = _MAX_SQLITE_OFFSET - 1


def _emit_error(message: str, code: ErrorCode, *, as_json: bool) -> None:
    if as_json:
        click.echo(json_mod.dumps({"error": message, "code": code}))
    else:
        click.echo(f"Error: {message}", err=True)
    sys.exit(1)


def _validate_choice(value: str | None, name: str, choices: set[str] | frozenset[str] | tuple[str, ...], *, as_json: bool) -> str | None:
    if value is None or value in choices:
        return value
    _emit_error(f"{name} must be one of: {', '.join(sorted(choices))}", ErrorCode.VALIDATION, as_json=as_json)
    raise AssertionError("unreachable")


def _validate_min_int(value: int, name: str, minimum: int, *, as_json: bool, maximum: int | None = None) -> int:
    if value >= minimum and (maximum is None or value <= maximum):
        return value
    if maximum is None:
        _emit_error(f"{name} must be >= {minimum}, got {value}", ErrorCode.VALIDATION, as_json=as_json)
    _emit_error(f"{name} must be between {minimum} and {maximum}, got {value}", ErrorCode.VALIDATION, as_json=as_json)
    raise AssertionError("unreachable")


def _annotation_detail(value: str, *, as_json: bool) -> str:
    return _validate_choice(value, "detail", ("summary", "full"), as_json=as_json) or "summary"


def _parse_links(raw_links: tuple[str, ...], *, as_json: bool) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for raw in raw_links:
        parts = raw.split(":", 2)
        if len(parts) != 3:
            _emit_error(
                f"Invalid link {raw!r}; expected target_type:target_id:relationship",
                ErrorCode.VALIDATION,
                as_json=as_json,
            )
        target_type, target_id, relationship = parts
        links.append({"target_type": target_type, "target_id": target_id, "relationship": relationship})
    return links


def _handle_annotation_exception(exc: Exception, *, as_json: bool) -> None:
    if isinstance(exc, KeyError):
        _emit_error(f"Not found: {exc.args[0]}", ErrorCode.NOT_FOUND, as_json=as_json)
    if isinstance(exc, sqlite3.Error):
        _emit_error(str(exc), ErrorCode.IO, as_json=as_json)
    _emit_error(str(exc), ErrorCode.VALIDATION, as_json=as_json)


@click.command("annotate-file")
@click.argument("file_path")
@click.argument("note")
@click.option("--line", "line_start", default=None, type=int, help="1-based line number")
@click.option("--line-end", default=None, type=int, help="1-based ending line number")
@click.option("--context-summary", default="", help="What you were doing when making the note")
@click.option("--intent", default="breadcrumb")
@click.option("--critical", is_flag=True, help="Mark as critical attention context")
@click.option("--link", "links", multiple=True, help="target_type:target_id:relationship")
@click.option("--session-ref", default="", help="Opaque session/run provenance")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def annotate_file_cmd(
    ctx: click.Context,
    file_path: str,
    note: str,
    line_start: int | None,
    line_end: int | None,
    context_summary: str,
    intent: str,
    critical: bool,
    links: tuple[str, ...],
    session_ref: str,
    as_json: bool,
) -> None:
    """Create a shared annotation on a project file."""
    intent = _validate_choice(intent, "intent", VALID_ANNOTATION_INTENTS, as_json=as_json) or "breadcrumb"
    parsed_links = _parse_links(links, as_json=as_json)
    with get_db() as db:
        try:
            annotation = db.annotate_file(
                file_path,
                note,
                line_start=line_start,
                line_end=line_end,
                context_summary=context_summary,
                intent=intent,
                critical=critical,
                links=parsed_links,
                actor=ctx.obj["actor"],
                session_ref=session_ref,
            )
        except (KeyError, ValueError, sqlite3.Error) as exc:
            _handle_annotation_exception(exc, as_json=as_json)
        if as_json:
            click.echo(json_mod.dumps(annotation, indent=2, default=str))
        else:
            click.echo(f"Annotated {annotation['file_path']}: {annotation['annotation_id']}")
        refresh_summary(db)


@click.command("list-annotations")
@click.option("--file", "file_path", default=None, help="Filter by file path")
@click.option("--file-id", default=None, help="Filter by file ID")
@click.option("--issue-id", default=None, help="Filter by linked issue ID")
@click.option("--target-type", default=None)
@click.option("--target-id", default=None)
@click.option("--relationship", default=None)
@click.option("--actor", default=None)
@click.option("--intent", default=None)
@click.option("--critical/--not-critical", default=None)
@click.option("--status", default=None)
@click.option("--anchor-state", default=None)
@click.option("--detail", default="summary")
@click.option("--limit", default=100, type=int)
@click.option("--offset", default=0, type=int)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_annotations_cmd(
    file_path: str | None,
    file_id: str | None,
    issue_id: str | None,
    target_type: str | None,
    target_id: str | None,
    relationship: str | None,
    actor: str | None,
    intent: str | None,
    critical: bool | None,
    status: str | None,
    anchor_state: str | None,
    detail: str,
    limit: int,
    offset: int,
    as_json: bool,
) -> None:
    """List annotations."""
    target_type = _validate_choice(target_type, "target_type", VALID_ANNOTATION_TARGET_TYPES, as_json=as_json)
    relationship = _validate_choice(relationship, "relationship", VALID_ANNOTATION_RELATIONSHIPS, as_json=as_json)
    intent = _validate_choice(intent, "intent", VALID_ANNOTATION_INTENTS, as_json=as_json)
    status = _validate_choice(status, "status", VALID_ANNOTATION_STATUSES, as_json=as_json)
    anchor_state = _validate_choice(anchor_state, "anchor_state", _ANCHOR_STATES, as_json=as_json)
    detail = _annotation_detail(detail, as_json=as_json)
    limit = _validate_min_int(limit, "limit", 1, as_json=as_json, maximum=_MAX_SQLITE_LIMIT)
    offset = _validate_min_int(offset, "offset", 0, as_json=as_json, maximum=_MAX_SQLITE_OFFSET)
    with get_db() as db:
        try:
            result = db.list_annotations(
                file_path=file_path,
                file_id=file_id,
                issue_id=issue_id,
                target_type=target_type,
                target_id=target_id,
                relationship=relationship,
                actor=actor,
                intent=intent,
                critical=critical,
                status=status,
                anchor_state=anchor_state,
                response_detail=detail,
                limit=limit,
                offset=offset,
            )
        except (KeyError, ValueError, sqlite3.Error) as exc:
            _handle_annotation_exception(exc, as_json=as_json)
        if as_json:
            click.echo(json_mod.dumps(result, indent=2, default=str))
            return
        for item in result["items"]:
            marker = "!" if item["critical"] else "-"
            click.echo(f"{marker} {item['annotation_id']} {item['file_path']} [{item['status']}] {item['note']}")
        click.echo(f"\n{len(result['items'])} annotation(s)")


@click.command("get-annotation")
@click.argument("annotation_id")
@click.option("--detail", default="full")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def get_annotation_cmd(annotation_id: str, detail: str, as_json: bool) -> None:
    """Get an annotation."""
    detail = _annotation_detail(detail, as_json=as_json)
    with get_db() as db:
        try:
            annotation = db.get_annotation(annotation_id, response_detail=detail)
        except (KeyError, ValueError, sqlite3.Error) as exc:
            _handle_annotation_exception(exc, as_json=as_json)
        if as_json:
            click.echo(json_mod.dumps(annotation, indent=2, default=str))
        else:
            click.echo(f"{annotation['annotation_id']} {annotation['file_path']} [{annotation['status']}]")
            click.echo(annotation["note"])


@click.command("resolve-annotation")
@click.argument("annotation_id")
@click.option("--reason", default="", help="Resolution reason")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def resolve_annotation_cmd(ctx: click.Context, annotation_id: str, reason: str, as_json: bool) -> None:
    """Resolve an annotation."""
    with get_db() as db:
        try:
            annotation = db.resolve_annotation(annotation_id, reason=reason, actor=ctx.obj["actor"])
        except (KeyError, ValueError, sqlite3.Error) as exc:
            _handle_annotation_exception(exc, as_json=as_json)
        if as_json:
            click.echo(json_mod.dumps(annotation, indent=2, default=str))
        else:
            click.echo(f"Resolved {annotation['annotation_id']}")
        refresh_summary(db)


@click.command("carry-forward-annotation")
@click.argument("annotation_id")
@click.option("--from", "from_target_id", required=True, help="Old issue target ID")
@click.option("--to", "to_target_id", required=True, help="New issue target ID")
@click.option("--reason", required=True, help="Carry-forward reason")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def carry_forward_annotation_cmd(
    ctx: click.Context,
    annotation_id: str,
    from_target_id: str,
    to_target_id: str,
    reason: str,
    as_json: bool,
) -> None:
    """Carry an annotation forward to another issue."""
    with get_db() as db:
        try:
            result = db.carry_forward_annotation(
                annotation_id,
                from_target_id=from_target_id,
                to_target_id=to_target_id,
                reason=reason,
                actor=ctx.obj["actor"],
            )
        except (KeyError, ValueError, sqlite3.Error) as exc:
            _handle_annotation_exception(exc, as_json=as_json)
        if as_json:
            click.echo(json_mod.dumps(result, indent=2, default=str))
        else:
            click.echo(f"Carried {annotation_id} forward to {to_target_id}")
        refresh_summary(db)


def register(cli: click.Group) -> None:
    cli.add_command(annotate_file_cmd)
    cli.add_command(list_annotations_cmd)
    cli.add_command(get_annotation_cmd)
    cli.add_command(resolve_annotation_cmd)
    cli.add_command(carry_forward_annotation_cmd)
