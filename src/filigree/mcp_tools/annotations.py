"""MCP tools for shared file annotations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from mcp.types import TextContent, Tool

from filigree.core import (
    VALID_ANNOTATION_INTENTS,
    VALID_ANNOTATION_RELATIONSHIPS,
    VALID_ANNOTATION_STATUSES,
    VALID_ANNOTATION_TARGET_TYPES,
)
from filigree.mcp_tools.common import (
    _list_response,
    _parse_args,
    _registry_error_text,
    _text,
    _validate_actor,
    _validate_int_range,
    _validate_str,
    get_db,
    refresh_summary,
)
from filigree.registry import RegistryResolutionError, RegistryUnavailableError
from filigree.types.api import ErrorCode, ErrorResponse
from filigree.types.inputs import (
    AnnotateFileArgs,
    CarryForwardAnnotationArgs,
    GetAnnotationArgs,
    GetFileAnnotationsArgs,
    GetIssueAnnotationsArgs,
    LinkAnnotationArgs,
    ListAnnotationsArgs,
    ListAttentionAnnotationsArgs,
    PromoteAnnotationArgs,
    ResolveAnnotationArgs,
    SupersedeAnnotationArgs,
    UnlinkAnnotationArgs,
    UpdateAnnotationArgs,
)

_ANCHOR_STATES = ["current", "line_drifted", "content_changed_anchor_found", "stale", "file_missing"]


def _annotation_response_detail(raw: Any) -> tuple[str, list[TextContent] | None]:
    if raw is None:
        return "summary", None
    if raw in {"summary", "full"}:
        return raw, None
    return "", _text(ErrorResponse(error="response_detail must be 'summary' or 'full'", code=ErrorCode.VALIDATION))


def _limit_offset(args: dict[str, Any], default: int = 100) -> tuple[int, int, list[TextContent] | None]:
    limit = args.get("limit", default)
    offset = args.get("offset", 0)
    for err in (
        _validate_int_range(limit, "limit", min_val=1, max_val=10000),
        _validate_int_range(offset, "offset", min_val=0),
    ):
        if err is not None:
            return 0, 0, err
    return int(limit), int(offset), None


def _common_annotation_tools() -> list[Tool]:
    link_schema = {
        "type": "object",
        "properties": {
            "target_type": {"type": "string", "enum": sorted(VALID_ANNOTATION_TARGET_TYPES)},
            "target_id": {"type": "string"},
            "relationship": {"type": "string", "enum": sorted(VALID_ANNOTATION_RELATIONSHIPS)},
        },
        "required": ["target_type", "target_id", "relationship"],
    }
    list_filters = {
        "file_path": {"type": "string"},
        "file_id": {"type": "string"},
        "issue_id": {"type": "string"},
        "target_type": {"type": "string", "enum": sorted(VALID_ANNOTATION_TARGET_TYPES)},
        "target_id": {"type": "string"},
        "actor": {"type": "string"},
        "intent": {"type": "string", "enum": sorted(VALID_ANNOTATION_INTENTS)},
        "critical": {"type": "boolean"},
        "status": {"type": "string", "enum": sorted(VALID_ANNOTATION_STATUSES)},
        "anchor_state": {"type": "string", "enum": _ANCHOR_STATES},
        "relationship": {"type": "string", "enum": sorted(VALID_ANNOTATION_RELATIONSHIPS)},
        "response_detail": {"type": "string", "enum": ["summary", "full"], "default": "summary"},
        "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 10000},
        "offset": {"type": "integer", "default": 0, "minimum": 0},
    }
    return [
        Tool(
            name="annotate_file",
            description="Create a shared project annotation anchored to a file with provenance capture.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "note": {"type": "string"},
                    "line_start": {"type": "integer", "minimum": 1},
                    "line_end": {"type": "integer", "minimum": 1},
                    "context_summary": {"type": "string"},
                    "intent": {"type": "string", "enum": sorted(VALID_ANNOTATION_INTENTS), "default": "breadcrumb"},
                    "critical": {"type": "boolean", "default": False},
                    "links": {"type": "array", "items": link_schema},
                    "actor": {"type": "string"},
                    "session_ref": {"type": "string"},
                },
                "required": ["file_path", "note"],
            },
        ),
        Tool(
            name="list_annotations",
            description="List shared file annotations with optional filters. Summary detail is the default.",
            inputSchema={"type": "object", "properties": list_filters},
        ),
        Tool(
            name="get_annotation",
            description="Get a single annotation by annotation_id, including provenance, links, and audit events.",
            inputSchema={
                "type": "object",
                "properties": {"annotation_id": {"type": "string"}},
                "required": ["annotation_id"],
            },
        ),
        Tool(
            name="update_annotation",
            description="Update annotation note, context, intent, critical flag, or lifecycle status.",
            inputSchema={
                "type": "object",
                "properties": {
                    "annotation_id": {"type": "string"},
                    "note": {"type": "string"},
                    "context_summary": {"type": "string"},
                    "intent": {"type": "string", "enum": sorted(VALID_ANNOTATION_INTENTS)},
                    "critical": {"type": "boolean"},
                    "status": {"type": "string", "enum": sorted(VALID_ANNOTATION_STATUSES)},
                    "actor": {"type": "string"},
                },
                "required": ["annotation_id"],
            },
        ),
        Tool(
            name="resolve_annotation",
            description="Resolve an active annotation while preserving audit trail.",
            inputSchema={
                "type": "object",
                "properties": {"annotation_id": {"type": "string"}, "reason": {"type": "string"}, "actor": {"type": "string"}},
                "required": ["annotation_id"],
            },
        ),
        Tool(
            name="supersede_annotation",
            description="Mark an annotation superseded by another annotation.",
            inputSchema={
                "type": "object",
                "properties": {
                    "annotation_id": {"type": "string"},
                    "replacement_annotation_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "actor": {"type": "string"},
                },
                "required": ["annotation_id", "replacement_annotation_id"],
            },
        ),
        Tool(
            name="promote_annotation",
            description="Promote an annotation narrowly to an issue or observation and link the new target.",
            inputSchema={
                "type": "object",
                "properties": {
                    "annotation_id": {"type": "string"},
                    "target_type": {"type": "string", "enum": ["issue", "observation"], "default": "issue"},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "keep_active": {"type": "boolean", "default": True},
                    "actor": {"type": "string"},
                },
                "required": ["annotation_id"],
            },
        ),
        Tool(
            name="carry_forward_annotation",
            description="Carry an active critical annotation forward to another issue and acknowledge the old target warning.",
            inputSchema={
                "type": "object",
                "properties": {
                    "annotation_id": {"type": "string"},
                    "from_target_id": {"type": "string"},
                    "to_target_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "actor": {"type": "string"},
                },
                "required": ["annotation_id", "from_target_id", "to_target_id", "reason"],
            },
        ),
        Tool(
            name="link_annotation",
            description="Link an annotation to an issue, file, finding, or observation.",
            inputSchema={
                "type": "object",
                "properties": {
                    "annotation_id": {"type": "string"},
                    "target_type": {"type": "string", "enum": sorted(VALID_ANNOTATION_TARGET_TYPES)},
                    "target_id": {"type": "string"},
                    "relationship": {"type": "string", "enum": sorted(VALID_ANNOTATION_RELATIONSHIPS)},
                    "actor": {"type": "string"},
                },
                "required": ["annotation_id", "target_type", "target_id", "relationship"],
            },
        ),
        Tool(
            name="unlink_annotation",
            description="Unlink an annotation from a target. relationship narrows which link to remove.",
            inputSchema={
                "type": "object",
                "properties": {
                    "annotation_id": {"type": "string"},
                    "target_type": {"type": "string", "enum": sorted(VALID_ANNOTATION_TARGET_TYPES)},
                    "target_id": {"type": "string"},
                    "relationship": {"type": "string", "enum": sorted(VALID_ANNOTATION_RELATIONSHIPS)},
                    "actor": {"type": "string"},
                },
                "required": ["annotation_id", "target_type", "target_id"],
            },
        ),
        Tool(
            name="get_file_annotations",
            description="List annotations for a file path. Active critical annotations sort first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "response_detail": {"type": "string", "enum": ["summary", "full"], "default": "summary"},
                    "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 10000},
                    "offset": {"type": "integer", "default": 0, "minimum": 0},
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="get_issue_annotations",
            description="List annotations linked to an issue or epic. Summary detail is the default.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string"},
                    "response_detail": {"type": "string", "enum": ["summary", "full"], "default": "summary"},
                    "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 10000},
                    "offset": {"type": "integer", "default": 0, "minimum": 0},
                },
                "required": ["issue_id"],
            },
        ),
        Tool(
            name="list_attention_annotations",
            description="List active critical must-consider annotations for a target or file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "file_path": {"type": "string"},
                    "critical": {"type": "boolean", "default": True},
                    "status": {"type": "string", "enum": sorted(VALID_ANNOTATION_STATUSES), "default": "active"},
                    "response_detail": {"type": "string", "enum": ["summary", "full"], "default": "summary"},
                    "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 10000},
                    "offset": {"type": "integer", "default": 0, "minimum": 0},
                },
            },
        ),
    ]


def register() -> tuple[list[Tool], dict[str, Callable[..., Any]]]:
    tools = _common_annotation_tools()
    return tools, {
        "annotate_file": _handle_annotate_file,
        "list_annotations": _handle_list_annotations,
        "get_annotation": _handle_get_annotation,
        "update_annotation": _handle_update_annotation,
        "resolve_annotation": _handle_resolve_annotation,
        "supersede_annotation": _handle_supersede_annotation,
        "promote_annotation": _handle_promote_annotation,
        "carry_forward_annotation": _handle_carry_forward_annotation,
        "link_annotation": _handle_link_annotation,
        "unlink_annotation": _handle_unlink_annotation,
        "get_file_annotations": _handle_get_file_annotations,
        "get_issue_annotations": _handle_get_issue_annotations,
        "list_attention_annotations": _handle_list_attention_annotations,
    }


def _db_error(exc: Exception, fallback: str) -> list[TextContent]:
    if isinstance(exc, (RegistryResolutionError, RegistryUnavailableError)):
        return _registry_error_text(exc, action="creating annotation")
    if isinstance(exc, KeyError):
        return _text(ErrorResponse(error=f"Not found: {exc.args[0]}", code=ErrorCode.NOT_FOUND))
    return _text(ErrorResponse(error=str(exc) or fallback, code=ErrorCode.VALIDATION))


async def _handle_annotate_file(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, AnnotateFileArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    links = args.get("links", [])
    if not isinstance(links, list):
        return _text(ErrorResponse(error="links must be a list", code=ErrorCode.VALIDATION))
    try:
        result = get_db().annotate_file(
            args["file_path"],
            args["note"],
            line_start=args.get("line_start"),
            line_end=args.get("line_end"),
            context_summary=args.get("context_summary", ""),
            intent=args.get("intent", "breadcrumb"),
            critical=args.get("critical", False),
            links=cast(list[dict[str, str]], links),
            actor=actor,
            session_ref=args.get("session_ref", ""),
        )
        refresh_summary()
        return _text(result)
    except (KeyError, RegistryResolutionError, RegistryUnavailableError, ValueError) as exc:
        return _db_error(exc, "Could not create annotation")


async def _handle_list_annotations(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, ListAnnotationsArgs)
    response_detail, detail_err = _annotation_response_detail(args.get("response_detail"))
    if detail_err is not None:
        return detail_err
    limit, offset, page_err = _limit_offset(arguments)
    if page_err is not None:
        return page_err
    try:
        return _text(
            get_db().list_annotations(
                file_path=args.get("file_path"),
                file_id=args.get("file_id"),
                issue_id=args.get("issue_id"),
                target_type=args.get("target_type"),
                target_id=args.get("target_id"),
                actor=args.get("actor"),
                intent=args.get("intent"),
                critical=args.get("critical"),
                status=args.get("status"),
                anchor_state=args.get("anchor_state"),
                relationship=args.get("relationship"),
                response_detail=response_detail,
                limit=limit,
                offset=offset,
            )
        )
    except (KeyError, ValueError) as exc:
        return _db_error(exc, "Could not list annotations")


async def _handle_get_annotation(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, GetAnnotationArgs)
    try:
        return _text(get_db().get_annotation(args["annotation_id"]))
    except (KeyError, ValueError) as exc:
        return _db_error(exc, "Could not get annotation")


async def _handle_update_annotation(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, UpdateAnnotationArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    try:
        result = get_db().update_annotation(
            args["annotation_id"],
            note=args.get("note"),
            context_summary=args.get("context_summary"),
            intent=args.get("intent"),
            critical=args.get("critical"),
            status=args.get("status"),
            actor=actor,
        )
        refresh_summary()
        return _text(result)
    except (KeyError, ValueError) as exc:
        return _db_error(exc, "Could not update annotation")


async def _handle_resolve_annotation(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, ResolveAnnotationArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    try:
        result = get_db().resolve_annotation(args["annotation_id"], reason=args.get("reason", ""), actor=actor)
        refresh_summary()
        return _text(result)
    except (KeyError, ValueError) as exc:
        return _db_error(exc, "Could not resolve annotation")


async def _handle_supersede_annotation(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, SupersedeAnnotationArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    try:
        result = get_db().supersede_annotation(
            args["annotation_id"],
            replacement_annotation_id=args["replacement_annotation_id"],
            reason=args.get("reason", ""),
            actor=actor,
        )
        refresh_summary()
        return _text(result)
    except (KeyError, ValueError) as exc:
        return _db_error(exc, "Could not supersede annotation")


async def _handle_promote_annotation(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, PromoteAnnotationArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    keep_active = args.get("keep_active", True)
    if not isinstance(keep_active, bool):
        return _text(ErrorResponse(error="keep_active must be a boolean", code=ErrorCode.VALIDATION))
    try:
        result = get_db().promote_annotation(
            args["annotation_id"],
            target_type=args.get("target_type", "issue"),
            title=args.get("title"),
            reason=args.get("reason", ""),
            keep_active=keep_active,
            actor=actor,
        )
        refresh_summary()
        return _text(result)
    except (KeyError, ValueError) as exc:
        return _db_error(exc, "Could not promote annotation")


async def _handle_carry_forward_annotation(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, CarryForwardAnnotationArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    try:
        result = get_db().carry_forward_annotation(
            args["annotation_id"],
            from_target_id=args["from_target_id"],
            to_target_id=args["to_target_id"],
            reason=args["reason"],
            actor=actor,
        )
        refresh_summary()
        return _text(result)
    except (KeyError, ValueError) as exc:
        return _db_error(exc, "Could not carry forward annotation")


async def _handle_link_annotation(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, LinkAnnotationArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    try:
        result = get_db().link_annotation(
            args["annotation_id"],
            target_type=args["target_type"],
            target_id=args["target_id"],
            relationship=args["relationship"],
            actor=actor,
        )
        refresh_summary()
        return _text(result)
    except (KeyError, ValueError) as exc:
        return _db_error(exc, "Could not link annotation")


async def _handle_unlink_annotation(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, UnlinkAnnotationArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    try:
        result = get_db().unlink_annotation(
            args["annotation_id"],
            target_type=args["target_type"],
            target_id=args["target_id"],
            relationship=args.get("relationship"),
            actor=actor,
        )
        refresh_summary()
        return _text(result)
    except (KeyError, ValueError) as exc:
        return _db_error(exc, "Could not unlink annotation")


async def _handle_get_file_annotations(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, GetFileAnnotationsArgs)
    response_detail, detail_err = _annotation_response_detail(args.get("response_detail"))
    if detail_err is not None:
        return detail_err
    limit, offset, page_err = _limit_offset(arguments)
    if page_err is not None:
        return page_err
    if (err := _validate_str(args.get("file_path"), "file_path")) is not None:
        return err
    try:
        return _text(get_db().get_file_annotations(args["file_path"], response_detail=response_detail, limit=limit, offset=offset))
    except (KeyError, ValueError) as exc:
        return _db_error(exc, "Could not list file annotations")


async def _handle_get_issue_annotations(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, GetIssueAnnotationsArgs)
    response_detail, detail_err = _annotation_response_detail(args.get("response_detail"))
    if detail_err is not None:
        return detail_err
    limit, offset, page_err = _limit_offset(arguments)
    if page_err is not None:
        return page_err
    try:
        return _text(get_db().get_issue_annotations(args["issue_id"], response_detail=response_detail, limit=limit, offset=offset))
    except (KeyError, ValueError) as exc:
        return _db_error(exc, "Could not list issue annotations")


async def _handle_list_attention_annotations(arguments: dict[str, Any]) -> list[TextContent]:

    args = _parse_args(arguments, ListAttentionAnnotationsArgs)
    response_detail, detail_err = _annotation_response_detail(args.get("response_detail"))
    if detail_err is not None:
        return detail_err
    limit, offset, page_err = _limit_offset(arguments)
    if page_err is not None:
        return page_err
    critical = args.get("critical", True)
    if not isinstance(critical, bool):
        return _text(ErrorResponse(error="critical must be a boolean", code=ErrorCode.VALIDATION))
    try:
        result = get_db().list_attention_annotations(
            target_id=args.get("target_id"),
            file_path=args.get("file_path"),
            critical=critical,
            status=args.get("status", "active"),
            response_detail=response_detail,
            limit=limit,
            offset=offset,
        )
        return _text(_list_response(result["items"], has_more=result["has_more"], next_offset=result.get("next_offset")))
    except (KeyError, ValueError) as exc:
        return _db_error(exc, "Could not list attention annotations")
