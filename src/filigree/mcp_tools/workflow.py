"""MCP tools for workflow templates, types, packs, transitions, and validation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.types import TextContent, Tool

from filigree.mcp_tools.common import _list_response, _parse_args, _text
from filigree.types.api import (
    ErrorCode,
    ErrorResponse,
    InboundTransitionInfo,
    OutboundTransitionInfo,
    PackListItem,
    SchemaResponse,
    StatusExplanation,
    TransitionDetail,
    ValidationResult,
    WorkflowGuideResponse,
    WorkflowStatusesResponse,
)
from filigree.types.inputs import (
    ExplainStatusArgs,
    GetTemplateArgs,
    GetTypeInfoArgs,
    GetValidTransitionsArgs,
    GetWorkflowGuideArgs,
    ValidateIssueArgs,
)
from filigree.types.workflow import (
    StateInfo,
    TypeListItem,
)


def register() -> tuple[list[Tool], dict[str, Callable[..., Any]]]:
    """Return (tool_definitions, handler_map) for workflow-domain tools."""
    tools = [
        Tool(
            name="get_template",
            description=(
                "canonical full workflow definition for an issue type: pack, states, transitions, "
                "initial state, and fields schema. Prefer this for workflow discovery."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "description": "Issue type (bug, task, feature, epic, milestone, phase, step, requirement)",
                    },
                },
                "required": ["type"],
            },
        ),
        Tool(
            name="get_workflow_statuses",
            description="Return workflow statuses by category (open/wip/done) from enabled templates.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_schema",
            description="Return MCP schema/discovery metadata including entity ID prefixes and the tools that accept each ID family.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_mcp_status",
            description="Read-only MCP server health and schema-compatibility diagnostic. Safe in schema-mismatch mode.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="list_types",
            description="List all registered issue types with their workflow info (states, pack, description).",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_type_info",
            description=("compatibility alias for get_template; returns the same canonical full workflow definition."),
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": "Issue type name (e.g. 'bug', 'task', 'feature')"},
                },
                "required": ["type"],
            },
        ),
        Tool(
            name="list_packs",
            description="List all enabled workflow packs with their types and metadata.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_valid_transitions",
            description="Get valid next states for an issue with readiness indicators. Shows which fields are needed before each transition.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID"},
                },
                "required": ["issue_id"],
            },
        ),
        Tool(
            name="validate_issue",
            description="Validate an issue against its type template. Returns warnings for missing recommended fields. Call get_valid_transitions first to see allowed state changes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID"},
                },
                "required": ["issue_id"],
            },
        ),
        Tool(
            name="get_workflow_guide",
            description="Get the workflow guide for a pack: state diagram, overview, tips, common mistakes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pack": {"type": "string", "description": "Pack name (e.g. 'core', 'planning', 'engineering')"},
                },
                "required": ["pack"],
            },
        ),
        Tool(
            name="explain_status",
            description="Explain a status within a type's workflow: its category, inbound/outbound transitions, and fields required at this status.",
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": "Issue type name"},
                    "status": {"type": "string", "description": "Status name to explain"},
                },
                "required": ["type", "status"],
            },
        ),
        Tool(
            name="reload_templates",
            description="Reload workflow templates from disk. Use after editing .filigree/templates/ or .filigree/packs/ files.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]

    handlers: dict[str, Callable[..., Any]] = {
        "get_template": _handle_get_template,
        "get_workflow_statuses": _handle_get_workflow_statuses,
        "get_schema": _handle_get_schema,
        "get_mcp_status": _handle_get_mcp_status,
        "list_types": _handle_list_types,
        "get_type_info": _handle_get_type_info,
        "list_packs": _handle_list_packs,
        "get_valid_transitions": _handle_get_valid_transitions,
        "validate_issue": _handle_validate_issue,
        "get_workflow_guide": _handle_get_workflow_guide,
        "explain_status": _handle_explain_status,
        "reload_templates": _handle_reload_templates,
    }

    return tools, handlers


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _handle_get_template(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, GetTemplateArgs)
    tracker = _get_db()
    tpl = tracker.get_template(args["type"])
    if tpl is None:
        return _text(ErrorResponse(error=f"Unknown template: {args['type']}", code=ErrorCode.NOT_FOUND))
    return _text(tpl)


async def _handle_get_workflow_statuses(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    tracker = _get_db()
    return _text(
        WorkflowStatusesResponse(
            statuses={
                "open": tracker._get_states_for_category("open"),
                "wip": tracker._get_states_for_category("wip"),
                "done": tracker._get_states_for_category("done"),
            }
        )
    )


async def _handle_get_schema(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    tracker = _get_db()
    prefix = tracker.prefix
    return _text(
        SchemaResponse(
            project_prefix=prefix,
            entity_id_prefixes={
                "issue": {
                    "entity": "issue",
                    "prefix": f"{prefix}-",
                    "primary_key": "issue_id",
                    "example": f"{prefix}-<hash>",
                    "accepted_by_tools": [
                        "get_issue",
                        "list_issues",
                        "update_issue",
                        "close_issue",
                        "reopen_issue",
                        "start_work",
                        "claim_issue",
                        "release_claim",
                        "heartbeat_work",
                        "get_stale_claims",
                        "reclaim_issue",
                        "add_comment",
                        "get_comments",
                        "add_label",
                        "remove_label",
                        "get_issue_events",
                        "get_issue_files",
                        "get_valid_transitions",
                        "validate_issue",
                        "add_dependency",
                        "remove_dependency",
                        "add_file_association",
                        "get_issue_annotations",
                        "list_attention_annotations",
                    ],
                },
                "observation": {
                    "entity": "observation",
                    "prefix": f"{prefix}-obs-",
                    "primary_key": "observation_id",
                    "example": f"{prefix}-obs-<hash>",
                    "accepted_by_tools": [
                        "dismiss_observation",
                        "promote_observation",
                        "batch_dismiss_observations",
                        "batch_promote_observations",
                    ],
                },
                "scan_finding": {
                    "entity": "scan_finding",
                    "prefix": f"{prefix}-sf-",
                    "primary_key": "finding_id",
                    "example": f"{prefix}-sf-<hash>",
                    "accepted_by_tools": [
                        "get_finding",
                        "update_finding",
                        "batch_update_findings",
                        "promote_finding",
                        "dismiss_finding",
                    ],
                },
                "file_record": {
                    "entity": "file_record",
                    "prefix": f"{prefix}-f-",
                    "primary_key": "file_id",
                    "example": f"{prefix}-f-<hash>",
                    "accepted_by_tools": [
                        "get_file",
                        "delete_file_record",
                        "get_file_timeline",
                        "get_issue_files",
                        "add_file_association",
                        "list_findings",
                        "list_observations",
                        "annotate_file",
                        "get_file_annotations",
                    ],
                },
                "annotation": {
                    "entity": "annotation",
                    "prefix": f"{prefix}-ann-",
                    "primary_key": "annotation_id",
                    "example": f"{prefix}-ann-<hash>",
                    "accepted_by_tools": [
                        "get_annotation",
                        "update_annotation",
                        "resolve_annotation",
                        "supersede_annotation",
                        "promote_annotation",
                        "carry_forward_annotation",
                        "link_annotation",
                        "unlink_annotation",
                        "list_annotations",
                    ],
                },
            },
        )
    )


async def _handle_get_mcp_status(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import get_mcp_status_payload

    return _text(get_mcp_status_payload())


async def _handle_list_types(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    tracker = _get_db()
    types_list: list[TypeListItem] = []
    for tt in tracker.templates.list_types():
        types_list.append(
            TypeListItem(
                type=tt.type,
                display_name=tt.display_name,
                description=tt.description,
                pack=tt.pack,
                states=[StateInfo(name=s.name, category=s.category) for s in tt.states],
                initial_state=tt.initial_state,
            )
        )
    items = sorted(types_list, key=lambda t: str(t["type"]))
    return _text(_list_response(list(items), has_more=False))


async def _handle_get_type_info(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, GetTypeInfoArgs)
    tracker = _get_db()
    tpl = tracker.get_template(args["type"])
    if tpl is None:
        return _text(ErrorResponse(error=f"Unknown type: {args['type']}", code=ErrorCode.NOT_FOUND))
    return _text(tpl)


async def _handle_list_packs(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    tracker = _get_db()
    packs_list: list[PackListItem] = []
    for pack in tracker.templates.list_packs():
        packs_list.append(
            PackListItem(
                pack=pack.pack,
                version=pack.version,
                display_name=pack.display_name,
                description=pack.description,
                types=sorted(pack.types.keys()),
                requires_packs=list(pack.requires_packs),
            )
        )
    items = sorted(packs_list, key=lambda p: str(p["pack"]))
    return _text(_list_response(list(items), has_more=False))


async def _handle_get_valid_transitions(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, GetValidTransitionsArgs)
    tracker = _get_db()
    try:
        transitions = tracker.get_valid_transitions(args["issue_id"])
        return _text(
            [
                TransitionDetail(
                    to=t.to,
                    category=t.category,
                    enforcement=t.enforcement or "",
                    requires_fields=list(t.requires_fields),
                    missing_fields=list(t.missing_fields),
                    ready=t.ready,
                )
                for t in transitions
            ]
        )
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['issue_id']}", code=ErrorCode.NOT_FOUND))


async def _handle_validate_issue(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, ValidateIssueArgs)
    tracker = _get_db()
    try:
        val_result = tracker.validate_issue(args["issue_id"])
        return _text(
            ValidationResult(
                valid=val_result.valid,
                warnings=list(val_result.warnings),
                errors=list(val_result.errors),
            )
        )
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['issue_id']}", code=ErrorCode.NOT_FOUND))


async def _handle_get_workflow_guide(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, GetWorkflowGuideArgs)
    tracker = _get_db()
    wf_pack = tracker.templates.get_pack(args["pack"])
    note: str | None = None

    # Try resolving as a type name if not a direct pack match
    if wf_pack is None:
        type_tpl = tracker.templates.get_type(args["pack"])
        if type_tpl is not None:
            wf_pack = tracker.templates.get_pack(type_tpl.pack)
            note = f"Resolved type '{args['pack']}' to pack '{wf_pack.pack}'" if wf_pack else None

    if wf_pack is None:
        return _text(
            ErrorResponse(
                error=f"Unknown pack: '{args['pack']}'. Use list_packs to see available packs, or list_types to see types.",
                code=ErrorCode.NOT_FOUND,
            )
        )

    if wf_pack.guide is None:
        return _text(WorkflowGuideResponse(pack=wf_pack.pack, guide=None, message="No guide available for this pack"))

    result = WorkflowGuideResponse(pack=wf_pack.pack, guide=dict(wf_pack.guide))
    if note:
        result["note"] = note
    return _text(result)


async def _handle_explain_status(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, ExplainStatusArgs)
    tracker = _get_db()
    type_tpl = tracker.templates.get_type(args["type"])
    if type_tpl is None:
        return _text(ErrorResponse(error=f"Unknown type: {args['type']}", code=ErrorCode.NOT_FOUND))
    status_name = args["status"]
    status_def = None
    for s in type_tpl.states:
        if s.name == status_name:
            status_def = s
            break
    if status_def is None:
        return _text(
            ErrorResponse(
                error=f"Unknown status {status_name!r} for type {args['type']!r}",
                code=ErrorCode.NOT_FOUND,
            )
        )
    inbound: list[InboundTransitionInfo] = [
        InboundTransitionInfo(**{"from": td.from_state, "enforcement": td.enforcement})
        for td in type_tpl.transitions
        if td.to_state == status_name
    ]
    outbound: list[OutboundTransitionInfo] = [
        OutboundTransitionInfo(to=td.to_state, enforcement=td.enforcement, requires_fields=list(td.requires_fields))
        for td in type_tpl.transitions
        if td.from_state == status_name
    ]
    required_fields = [fd.name for fd in type_tpl.fields_schema if status_name in fd.required_at]
    return _text(
        StatusExplanation(
            status=status_name,
            category=status_def.category,
            type=args["type"],
            inbound_transitions=inbound,
            outbound_transitions=outbound,
            required_fields=required_fields,
        )
    )


async def _handle_reload_templates(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    tracker = _get_db()
    try:
        tracker.reload_templates()
        # Force the new registry to materialise before regenerating context.md;
        # _refresh_summary reads template-derived sections.
        tracker.templates.list_types()
    except ValueError as exc:
        return _text(ErrorResponse(error=str(exc), code=ErrorCode.VALIDATION))
    _refresh_summary()
    return _text({"status": "ok"})
