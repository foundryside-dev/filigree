"""MCP tools for dependencies, ready/blocked queries, plans, and critical path."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from mcp.types import TextContent, Tool

from filigree.issue_payloads import issue_to_public
from filigree.mcp_tools.common import (
    _list_response,
    _parse_args,
    _slim_issue,
    _text,
    _validate_actor,
    _validate_int_range,
)
from filigree.mcp_tools.payloads import critical_path_node_to_mcp, plan_tree_to_mcp
from filigree.types.api import (
    BatchResponse,
    BlockedIssue,
    CriticalPathMcpNode,
    CriticalPathResponse,
    DependencyActionResponse,
    ErrorCode,
    ErrorResponse,
    PlanResponse,
    PublicIssue,
    parse_response_detail,
)
from filigree.types.inputs import (
    AddDependencyArgs,
    CreatePlanArgs,
    GetPlanArgs,
    LabelSubtreeArgs,
    MilestoneInput,
    PhaseInput,
    RemoveDependencyArgs,
)


def register() -> tuple[list[Tool], dict[str, Callable[..., Any]]]:
    """Return (tool_definitions, handler_map) for planning-domain tools."""
    tools = [
        Tool(
            name="add_dependency",
            description="Add dependency: from_issue_id depends on to_issue_id (to_issue_id blocks from_issue_id)",
            inputSchema={
                "type": "object",
                "properties": {
                    "from_issue_id": {"type": "string", "description": "Issue that is blocked"},
                    "to_issue_id": {"type": "string", "description": "Issue that blocks"},
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                },
                "required": ["from_issue_id", "to_issue_id"],
            },
        ),
        Tool(
            name="remove_dependency",
            description="Remove a dependency between two issues",
            inputSchema={
                "type": "object",
                "properties": {
                    "from_issue_id": {"type": "string", "description": "Issue that was blocked"},
                    "to_issue_id": {"type": "string", "description": "Issue that was blocking"},
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                },
                "required": ["from_issue_id", "to_issue_id"],
            },
        ),
        Tool(
            name="get_ready",
            description="Get all issues that are ready to work on (open, no blockers), sorted by priority",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_blocked",
            description="Get all blocked issues with their blocker lists",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_plan",
            description="Get milestone plan tree showing phases, steps, and progress",
            inputSchema={
                "type": "object",
                "properties": {
                    "milestone_id": {"type": "string", "description": "Milestone issue ID"},
                },
                "required": ["milestone_id"],
            },
        ),
        Tool(
            name="create_plan",
            description=(
                "Create a full milestone->phase->step hierarchy in one call. "
                "Returns the plan tree. Step deps use indices: integer for same-phase, "
                "'phase_idx.step_idx' for cross-phase."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "milestone": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "priority": {"type": "integer", "default": 2, "minimum": 0, "maximum": 4},
                            "description": {"type": "string", "default": ""},
                            "labels": {"type": "array", "items": {"type": "string"}, "description": "Labels to apply to the milestone and all descendants"},
                        },
                        "required": ["title"],
                    },
                    "phases": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "priority": {"type": "integer", "default": 2, "minimum": 0, "maximum": 4},
                                "description": {"type": "string", "default": ""},
                                "labels": {"type": "array", "items": {"type": "string"}, "description": "Additional labels to apply to this phase and its steps"},
                                "steps": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "title": {"type": "string"},
                                            "priority": {"type": "integer", "default": 2, "minimum": 0, "maximum": 4},
                                            "description": {"type": "string", "default": ""},
                                            "labels": {"type": "array", "items": {"type": "string"}, "description": "Additional labels to apply to this step"},
                                            "deps": {
                                                "type": "array",
                                                "items": {"type": ["integer", "string"]},
                                                "description": "Step indices (int for same-phase, 'p.s' for cross-phase)",
                                            },
                                        },
                                        "required": ["title"],
                                    },
                                },
                            },
                            "required": ["title"],
                        },
                    },
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                },
                "required": ["milestone", "phases"],
            },
        ),
        Tool(
            name="label_subtree",
            description="Apply one label to a parent issue and every descendant in its subtree.",
            inputSchema={
                "type": "object",
                "properties": {
                    "parent_id": {"type": "string", "description": "Root issue ID whose subtree should be labeled"},
                    "label": {"type": "string", "description": "Label to apply"},
                    "response_detail": {
                        "type": "string",
                        "enum": ["slim", "full"],
                        "default": "slim",
                        "description": "'slim' returns issue ID strings; 'full' returns full PublicIssue records.",
                    },
                },
                "required": ["parent_id", "label"],
            },
        ),
        Tool(
            name="get_critical_path",
            description="Longest dependency chain among open issues. Helps prioritize work that unblocks the most downstream items.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]

    handlers: dict[str, Callable[..., Any]] = {
        "add_dependency": _handle_add_dependency,
        "remove_dependency": _handle_remove_dependency,
        "get_ready": _handle_get_ready,
        "get_blocked": _handle_get_blocked,
        "get_plan": _handle_get_plan,
        "create_plan": _handle_create_plan,
        "label_subtree": _handle_label_subtree,
        "get_critical_path": _handle_get_critical_path,
    }

    return tools, handlers


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _handle_add_dependency(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, AddDependencyArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    tracker = _get_db()
    try:
        added = tracker.add_dependency(
            args["from_issue_id"],
            args["to_issue_id"],
            actor=actor,
        )
    except (ValueError, KeyError) as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    _refresh_summary()
    status = "added" if added else "already_exists"
    return _text(DependencyActionResponse(status=status, from_issue_id=args["from_issue_id"], to_issue_id=args["to_issue_id"]))


async def _handle_remove_dependency(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, RemoveDependencyArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    tracker = _get_db()
    try:
        removed = tracker.remove_dependency(
            args["from_issue_id"],
            args["to_issue_id"],
            actor=actor,
        )
    except (ValueError, KeyError) as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    _refresh_summary()
    status = "removed" if removed else "not_found"
    return _text(DependencyActionResponse(status=status, from_issue_id=args["from_issue_id"], to_issue_id=args["to_issue_id"]))


async def _handle_get_ready(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    tracker = _get_db()
    issues = tracker.get_ready()
    items = [_slim_issue(i) for i in issues]
    return _text(_list_response(items, has_more=False))


async def _handle_get_blocked(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    tracker = _get_db()
    issues = tracker.get_blocked()
    items = [BlockedIssue(**_slim_issue(i), blocked_by=i.blocked_by) for i in issues]
    return _text(_list_response(items, has_more=False))


async def _handle_get_plan(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, GetPlanArgs)
    tracker = _get_db()
    try:
        plan_tree = tracker.get_plan(args["milestone_id"])
        total = plan_tree["total_steps"]
        completed = plan_tree["completed_steps"]
        result = PlanResponse(
            milestone=plan_tree["milestone"],
            phases=plan_tree["phases"],
            total_steps=total,
            completed_steps=completed,
            progress_pct=round(completed / total * 100, 1) if total > 0 else 0.0,
        )
        return _text(plan_tree_to_mcp(result))
    except KeyError:
        return _text(ErrorResponse(error=f"Milestone not found: {args['milestone_id']}", code=ErrorCode.NOT_FOUND))


def _validate_plan_deps(deps: Any, name: str) -> list[TextContent] | None:
    """Reject deps values that db_planning would silently misinterpret.

    ``db_planning.create_plan`` uses ``str(dep_ref)`` and treats any string
    containing ``"."`` as ``"phase_idx.step_idx"``. A JSON float like ``0.1``
    would become ``"0.1"`` and resolve to phase 0 step 1 instead of being
    rejected; a bool would become ``"True"``/``"False"`` and fail with a raw
    ``ValueError`` from ``int()`` (per filigree-e87d310708).
    """
    if not isinstance(deps, list):
        return _text(ErrorResponse(error=f"{name} must be an array", code=ErrorCode.VALIDATION))
    for i, dep in enumerate(deps):
        label = f"{name}[{i}]"
        # Reject bool before int: ``True`` is ``int`` subclass but ``str(True)``
        # hits ``int('True')`` → raw ValueError.
        if isinstance(dep, bool):
            return _text(ErrorResponse(error=f"{label} must be integer or string, not bool", code=ErrorCode.VALIDATION))
        if isinstance(dep, int):
            if dep < 0:
                return _text(ErrorResponse(error=f"{label} must be >= 0", code=ErrorCode.VALIDATION))
            continue
        if isinstance(dep, str):
            parts = dep.split(".")
            if len(parts) > 2 or any(not p.lstrip("-").isdigit() for p in parts):
                return _text(
                    ErrorResponse(
                        error=f"{label} must be 'N' or 'P.S' with integer components, got {dep!r}",
                        code=ErrorCode.VALIDATION,
                    )
                )
            continue
        return _text(
            ErrorResponse(
                error=f"{label} must be integer or 'P.S' string, got {type(dep).__name__}",
                code=ErrorCode.VALIDATION,
            )
        )
    return None


async def _handle_create_plan(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, CreatePlanArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err

    # Validate priority on nested milestone/phase/step objects
    milestone = args["milestone"]
    err = _validate_int_range(milestone.get("priority"), "milestone.priority", min_val=0, max_val=4)
    if err:
        return err
    for pi, phase in enumerate(args["phases"]):
        err = _validate_int_range(phase.get("priority"), f"phases[{pi}].priority", min_val=0, max_val=4)
        if err:
            return err
        for si, step in enumerate(phase.get("steps", [])):
            err = _validate_int_range(step.get("priority"), f"phases[{pi}].steps[{si}].priority", min_val=0, max_val=4)
            if err:
                return err
            dep_err = _validate_plan_deps(step.get("deps", []), f"phases[{pi}].steps[{si}].deps")
            if dep_err:
                return dep_err

    tracker = _get_db()
    try:
        plan = tracker.create_plan(
            cast(MilestoneInput, dict(milestone)),
            [cast(PhaseInput, dict(p)) for p in args["phases"]],
            actor=actor,
        )
        _refresh_summary()
        return _text(plan_tree_to_mcp(plan))
    except (KeyError, IndexError, ValueError) as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))


async def _handle_label_subtree(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, LabelSubtreeArgs)
    detail = parse_response_detail(args.get("response_detail"))
    if isinstance(detail, dict):
        return _text(detail)
    tracker = _get_db()
    try:
        succeeded, failed = tracker.label_subtree(args["parent_id"], label=args["label"])
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['parent_id']}", code=ErrorCode.NOT_FOUND))
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    _refresh_summary()
    if detail == "full":
        full_result: BatchResponse[PublicIssue] = BatchResponse(
            succeeded=[issue_to_public(tracker.get_issue(row["id"])) for row in succeeded],
            failed=failed,
        )
        return _text(full_result)
    result: BatchResponse[str] = BatchResponse(
        succeeded=[row["id"] for row in succeeded],
        failed=failed,
    )
    return _text(result)


async def _handle_get_critical_path(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    tracker = _get_db()
    path = [cast(CriticalPathMcpNode, critical_path_node_to_mcp(node)) for node in tracker.get_critical_path()]
    return _text(CriticalPathResponse(path=path, length=len(path)))
