"""MCP tools for dependencies, ready/blocked queries, plans, and critical path."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from typing import Any, cast

from mcp.types import TextContent, Tool

from filigree.issue_payloads import issue_to_public
from filigree.mcp_tools.common import (
    _list_response,
    _parse_args,
    _ready_issue,
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
    AddPlanStepArgs,
    CreatePlanArgs,
    CreatePlanFromFileArgs,
    GetPlanArgs,
    GetReadyArgs,
    LabelPlanTreeArgs,
    LabelSubtreeArgs,
    MilestoneInput,
    MovePlanStepArgs,
    PhaseInput,
    RemoveDependencyArgs,
    RetargetPlanDependencyArgs,
)


def register() -> tuple[list[Tool], dict[str, Callable[..., Any]]]:
    """Return (tool_definitions, handler_map) for planning-domain tools."""
    tools = [
        Tool(
            name="add_dependency",
            description="Add dependency: from_issue_id depends on to_issue_id (to_issue_id blocks from_issue_id). Returns the flat updated PublicIssue for from_issue_id plus dependency metadata.",
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
            description="Remove a dependency between two issues. Returns the flat updated PublicIssue for from_issue_id plus dependency metadata.",
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
            description=(
                "Get all unassigned issues in the open category with no open blockers, sorted by priority. "
                "Pass include_context=true to add parent_issue_id and parent_title while preserving the slim default."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "include_context": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include parent_issue_id and parent_title on each ready item.",
                    },
                },
            },
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
                            "labels": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Labels to apply to the milestone and all descendants",
                            },
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
                                "labels": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Additional labels to apply to this phase and its steps",
                                },
                                "steps": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "title": {"type": "string"},
                                            "priority": {"type": "integer", "default": 2, "minimum": 0, "maximum": 4},
                                            "description": {"type": "string", "default": ""},
                                            "labels": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                                "description": "Additional labels to apply to this step",
                                            },
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
            name="create_plan_from_file",
            description=(
                "Create a full milestone->phase->step hierarchy from a project-relative JSON file. "
                "Uses the same JSON structure as create_plan."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Project-relative path to a plan JSON file"},
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="add_plan_step",
            description=(
                "Add a step to an existing phase in one call. Inherits labels from the phase "
                "and accepts dependency issue IDs so agents do not have to compose create_issue "
                "plus add_dependency manually."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "phase_id": {"type": "string", "description": "Phase issue ID to receive the new step"},
                    "title": {"type": "string", "description": "Step title"},
                    "priority": {"type": "integer", "default": 2, "minimum": 0, "maximum": 4},
                    "description": {"type": "string", "default": ""},
                    "notes": {"type": "string", "default": ""},
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional labels; phase labels are inherited",
                    },
                    "deps": {"type": "array", "items": {"type": "string"}, "description": "Issue IDs this new step depends on"},
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                },
                "required": ["phase_id", "title"],
            },
        ),
        Tool(
            name="retarget_plan_dependency",
            description=(
                "Replace one dependency edge on a plan step. This wraps remove_dependency + "
                "add_dependency so agents do not have to hand-edit the plan graph."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "step_id": {"type": "string", "description": "Plan step whose dependency should change"},
                    "old_depends_on_id": {"type": "string", "description": "Current blocker to remove"},
                    "new_depends_on_id": {"type": "string", "description": "Replacement blocker to add"},
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                },
                "required": ["step_id", "old_depends_on_id", "new_depends_on_id"],
            },
        ),
        Tool(
            name="move_plan_step",
            description="Move an existing plan step under another phase without raw parent_id surgery.",
            inputSchema={
                "type": "object",
                "properties": {
                    "step_id": {"type": "string", "description": "Step issue ID to move"},
                    "phase_id": {"type": "string", "description": "Destination phase issue ID"},
                    "actor": {"type": "string", "description": "Agent/user identity for audit trail"},
                },
                "required": ["step_id", "phase_id"],
            },
        ),
        Tool(
            name="label_plan_tree",
            description="Apply one label to a milestone and every phase/step beneath it.",
            inputSchema={
                "type": "object",
                "properties": {
                    "milestone_id": {"type": "string", "description": "Milestone issue ID whose plan tree should be labeled"},
                    "label": {"type": "string", "description": "Label to apply"},
                    "response_detail": {
                        "type": "string",
                        "enum": ["slim", "full"],
                        "default": "slim",
                        "description": "'slim' returns issue ID strings; 'full' returns full PublicIssue records.",
                    },
                },
                "required": ["milestone_id", "label"],
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
        "create_plan_from_file": _handle_create_plan_from_file,
        "add_plan_step": _handle_add_plan_step,
        "retarget_plan_dependency": _handle_retarget_plan_dependency,
        "move_plan_step": _handle_move_plan_step,
        "label_plan_tree": _handle_label_plan_tree,
        "label_subtree": _handle_label_subtree,
        "get_critical_path": _handle_get_critical_path,
    }

    return tools, handlers


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _parent_titles_by_id(tracker: Any, issues: list[Any]) -> dict[str, str]:
    parent_ids = sorted({i.parent_id for i in issues if i.parent_id})
    titles: dict[str, str] = {}
    for parent_id in parent_ids:
        with contextlib.suppress(KeyError):
            titles[parent_id] = tracker.get_issue(parent_id).title
    return titles


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
    issue = tracker.get_issue(args["from_issue_id"])
    response: dict[str, Any] = dict(issue_to_public(issue))
    response["dependency_result"] = status
    response["dependency"] = {"from_issue_id": args["from_issue_id"], "to_issue_id": args["to_issue_id"]}
    return _text(cast(DependencyActionResponse, response))


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
    issue = tracker.get_issue(args["from_issue_id"])
    response: dict[str, Any] = dict(issue_to_public(issue))
    response["dependency_result"] = status
    response["dependency"] = {"from_issue_id": args["from_issue_id"], "to_issue_id": args["to_issue_id"]}
    return _text(cast(DependencyActionResponse, response))


async def _handle_get_ready(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db

    args = _parse_args(arguments, GetReadyArgs)
    include_context = args.get("include_context", False)
    if not isinstance(include_context, bool):
        return _text(ErrorResponse(error="include_context must be a boolean", code=ErrorCode.VALIDATION))

    tracker = _get_db()
    issues = tracker.get_ready()
    parent_titles = _parent_titles_by_id(tracker, issues) if include_context else {}
    items = [_ready_issue(i, include_context=include_context, parent_title=parent_titles.get(i.parent_id or "")) for i in issues]
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


def _validate_plan_payload_shape(arguments: dict[str, Any]) -> list[TextContent] | None:
    if "milestone" not in arguments or "phases" not in arguments:
        return _text(ErrorResponse(error="JSON must contain 'milestone' and 'phases' keys", code=ErrorCode.VALIDATION))

    milestone = arguments["milestone"]
    if not isinstance(milestone, dict):
        return _text(ErrorResponse(error="'milestone' must be an object with at least a 'title' key", code=ErrorCode.VALIDATION))
    if not isinstance(milestone.get("title"), str):
        return _text(
            ErrorResponse(
                error=f"Milestone 'title' must be a string, got {type(milestone.get('title')).__name__}", code=ErrorCode.VALIDATION
            )
        )

    phases = arguments["phases"]
    if not isinstance(phases, list):
        return _text(ErrorResponse(error="'phases' must be a list of phase objects", code=ErrorCode.VALIDATION))
    for pi, phase in enumerate(phases):
        if not isinstance(phase, dict):
            return _text(ErrorResponse(error=f"Phase {pi + 1} must be an object, got {type(phase).__name__}", code=ErrorCode.VALIDATION))
        if not isinstance(phase.get("title"), str):
            return _text(
                ErrorResponse(
                    error=f"Phase {pi + 1} 'title' must be a string, got {type(phase.get('title')).__name__}", code=ErrorCode.VALIDATION
                )
            )
        steps = phase.get("steps", [])
        if not isinstance(steps, list):
            return _text(
                ErrorResponse(error=f"Phase {pi + 1} 'steps' must be a list, got {type(steps).__name__}", code=ErrorCode.VALIDATION)
            )
        for si, step in enumerate(steps):
            if not isinstance(step, dict):
                return _text(
                    ErrorResponse(
                        error=f"Phase {pi + 1}, Step {si + 1} must be an object, got {type(step).__name__}", code=ErrorCode.VALIDATION
                    )
                )
            if not isinstance(step.get("title"), str):
                return _text(
                    ErrorResponse(
                        error=f"Phase {pi + 1}, Step {si + 1} 'title' must be a string, got {type(step.get('title')).__name__}",
                        code=ErrorCode.VALIDATION,
                    )
                )
    return None


async def _create_plan_from_payload(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    shape_err = _validate_plan_payload_shape(arguments)
    if shape_err:
        return shape_err
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


async def _handle_create_plan(arguments: dict[str, Any]) -> list[TextContent]:
    return await _create_plan_from_payload(arguments)


async def _handle_create_plan_from_file(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _safe_path

    args = _parse_args(arguments, CreatePlanFromFileArgs)
    try:
        plan_path = _safe_path(args["file_path"])
        raw = plan_path.read_text()
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    except (OSError, UnicodeDecodeError) as e:
        return _text(ErrorResponse(error=f"reading file: {e}", code=ErrorCode.IO))

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return _text(ErrorResponse(error=f"Invalid JSON: {e}", code=ErrorCode.VALIDATION))

    if not isinstance(data, dict):
        return _text(ErrorResponse(error="JSON must be an object, not a list or scalar", code=ErrorCode.VALIDATION))

    payload = dict(data)
    payload.pop("actor", None)
    if "actor" in args:
        payload["actor"] = args["actor"]
    return await _create_plan_from_payload(payload)


async def _handle_add_plan_step(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, AddPlanStepArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    priority_err = _validate_int_range(args.get("priority"), "priority", min_val=0, max_val=4)
    if priority_err:
        return priority_err

    tracker = _get_db()
    try:
        step = tracker.add_plan_step(
            args["phase_id"],
            args["title"],
            priority=args.get("priority", 2),
            description=args.get("description", ""),
            notes=args.get("notes", ""),
            labels=args.get("labels"),
            deps=args.get("deps"),
            actor=actor,
        )
    except KeyError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.NOT_FOUND))
    except (TypeError, ValueError) as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    _refresh_summary()
    return _text(issue_to_public(step))


async def _handle_retarget_plan_dependency(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, RetargetPlanDependencyArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    tracker = _get_db()
    try:
        issue = tracker.retarget_plan_dependency(
            args["step_id"],
            args["old_depends_on_id"],
            args["new_depends_on_id"],
            actor=actor,
        )
    except KeyError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.NOT_FOUND))
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    _refresh_summary()
    response: dict[str, Any] = dict(issue_to_public(issue))
    response["dependency_result"] = "retargeted"
    response["dependency"] = {
        "from_issue_id": args["step_id"],
        "old_to_issue_id": args["old_depends_on_id"],
        "to_issue_id": args["new_depends_on_id"],
    }
    return _text(response)


async def _handle_move_plan_step(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, MovePlanStepArgs)
    actor, actor_err = _validate_actor(args.get("actor", "mcp"))
    if actor_err:
        return actor_err
    tracker = _get_db()
    try:
        issue = tracker.move_plan_step(args["step_id"], args["phase_id"], actor=actor)
    except KeyError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.NOT_FOUND))
    except ValueError as e:
        return _text(ErrorResponse(error=str(e), code=ErrorCode.VALIDATION))
    _refresh_summary()
    response: dict[str, Any] = dict(issue_to_public(issue))
    response["move_result"] = "moved"
    response["changed_fields"] = ["parent_id"]
    return _text(response)


async def _handle_label_plan_tree(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.mcp_server import _get_db, _refresh_summary

    args = _parse_args(arguments, LabelPlanTreeArgs)
    detail = parse_response_detail(args.get("response_detail"))
    if isinstance(detail, dict):
        return _text(detail)
    tracker = _get_db()
    try:
        milestone = tracker.get_issue(args["milestone_id"])
        if milestone.type != "milestone":
            return _text(
                ErrorResponse(
                    error=f"milestone_id must reference a milestone issue, got {milestone.type!r}: {args['milestone_id']}",
                    code=ErrorCode.VALIDATION,
                )
            )
        succeeded, failed = tracker.label_subtree(args["milestone_id"], label=args["label"])
    except KeyError:
        return _text(ErrorResponse(error=f"Issue not found: {args['milestone_id']}", code=ErrorCode.NOT_FOUND))
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
    response = CriticalPathResponse(path=path, length=len(path))
    if not path:
        response["note"] = "no open dependency chains"
    return _text(response)
