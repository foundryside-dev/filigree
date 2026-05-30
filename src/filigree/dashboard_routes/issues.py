"""Issue, workflow, and dependency route handlers.

CONNECTION INVARIANT (CONTRACT-E, see dashboard_routes/files.py): the write
handlers here (``update_issue``, ``close_issue``, ``add_comment``,
``batch_update``, ``claim_*``, dependency edits, …) run synchronous DB work on
the shared event-loop connection. They MUST stay plain ``async def`` with no
``await`` mid-transaction, so they run to completion on the event-loop thread
and stay cooperatively serialised. Do NOT move one onto a worker thread
(``asyncio.to_thread``/executor) without giving it its own connection via
``FiligreeDB.borrow_for_worker_thread`` — otherwise it races the worker-thread
paths at the ``sqlite3.Connection`` level.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, overload

from starlette.requests import Request

if TYPE_CHECKING:
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

from filigree.core import FiligreeDB, WrongProjectError
from filigree.dashboard_routes.common import (
    _MAX_PAGINATION_OFFSET,
    _check_read_prefix_in_server_mode,
    _error_response,
    _parse_json_body,
    _safe_int,
    _validate_actor,
    _validate_priority_field,
)
from filigree.db_planning import NotAMilestoneError
from filigree.models import Issue
from filigree.types.api import (
    ClaimConflictError,
    DepDetail,
    EnrichedIssueDetail,
    ErrorCode,
    InvalidTransitionError,
    IssueDetailEvent,
    claim_conflict_details,
    classify_value_error,
    errorcode_to_http_status,
    invalid_transition_details,
)
from filigree.types.api import (
    classify_issue_write_error as _classify_issue_write_error,
)
from filigree.types.api import (
    classify_release_claim_error as _classify_release_claim_error,
)
from filigree.types.core import ISOTimestamp
from filigree.types.planning import CommentRecord

logger = logging.getLogger(__name__)

# Page size used when streaming every issue into the dashboard preload.
# Exposed at module scope so tests can shrink it to exercise pagination.
_ISSUES_LIST_PAGE_SIZE = 1000
_MISSING = object()


def _invalid_transition_details(exc: BaseException) -> dict[str, Any] | None:
    return invalid_transition_details(exc)


def _issue_write_error_details(exc: BaseException) -> dict[str, Any] | None:
    if isinstance(exc, ClaimConflictError):
        return claim_conflict_details(exc)
    return _invalid_transition_details(exc)


def _fetch_all_issues(db: FiligreeDB) -> list[Issue]:
    """Return every issue in the DB by paginating list_issues.

    The dashboard preload previously called ``list_issues(limit=10000)``,
    which silently truncated large projects. Pagination removes that hidden
    ceiling while preserving the response shape.
    """
    all_issues: list[Issue] = []
    offset = 0
    while True:
        page = db.list_issues(limit=_ISSUES_LIST_PAGE_SIZE, offset=offset)
        all_issues.extend(page)
        if len(page) < _ISSUES_LIST_PAGE_SIZE:
            break
        offset += _ISSUES_LIST_PAGE_SIZE
    return all_issues


@overload
def _validate_body_string_field(
    body: dict[str, object],
    field: str,
    *,
    allow_null: bool = ...,
    default: str,
) -> str | JSONResponse: ...


@overload
def _validate_body_string_field(
    body: dict[str, object],
    field: str,
    *,
    allow_null: bool = ...,
    default: None = ...,
) -> str | None | JSONResponse: ...


def _validate_body_string_field(
    body: dict[str, object],
    field: str,
    *,
    allow_null: bool = False,
    default: str | None = None,
) -> str | None | JSONResponse:
    """Validate a JSON body field expected to be a string.

    Missing fields fall back to ``default`` so callers can preserve existing
    create/patch semantics. Present fields must be strings, except when
    ``allow_null`` is set, in which case explicit ``null`` is accepted.

    Overloads narrow the return when ``default`` is a non-None ``str`` —
    ``None`` is unreachable in that case, so callers that pass ``default=""``
    can use the result without an extra ``None`` guard.
    """
    value = body.get(field, _MISSING)
    if value is _MISSING:
        return default
    if value is None:
        if allow_null:
            return None
        return _error_response(f"{field} must be a string", ErrorCode.VALIDATION, 400)
    if not isinstance(value, str):
        suffix = " or null" if allow_null else ""
        return _error_response(f"{field} must be a string{suffix}", ErrorCode.VALIDATION, 400)
    return value


def _parse_batch_update_body(body: dict[str, Any]) -> dict[str, Any] | JSONResponse:
    """Validate the batch-update request body.

    Shared by classic ``POST /api/batch/update`` and loom
    ``POST /api/loom/batch/update``; both generations accept the same
    request shape (``issue_ids``, ``status``, ``priority``, ``assignee``,
    ``fields``, ``actor``). Returns the kwargs dict for
    ``db.batch_update`` on success, or a 400 ``JSONResponse`` on error.
    """
    issue_ids = body.get("issue_ids")
    if not isinstance(issue_ids, list):
        return _error_response("issue_ids must be a JSON array", ErrorCode.VALIDATION, 400)
    if not all(isinstance(i, str) for i in issue_ids):
        return _error_response("All issue_ids must be strings", ErrorCode.VALIDATION, 400)
    actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
    if actor_err:
        return actor_err
    expected_assignee = body.get("expected_assignee")
    if expected_assignee is not None and not isinstance(expected_assignee, str):
        return _error_response("expected_assignee must be a string", ErrorCode.VALIDATION, 400)
    priority = _validate_priority_field(body)
    if not isinstance(priority, int) and priority is not None:
        return priority  # JSONResponse error
    # See _parse_batch_close_body's reason-narrowing comment: JSONResponse is
    # only TYPE_CHECKING-imported at module scope, so we narrow on the success
    # types instead. ``status`` is ``str | None | JSONResponse``; a missing
    # ``status`` falls through to ``None`` (no change).
    status = _validate_body_string_field(body, "status", default=None)
    if status is not None and not isinstance(status, str):
        return status
    expected_assignee = body.get("expected_assignee")
    if expected_assignee is not None and not isinstance(expected_assignee, str):
        return _error_response("expected_assignee must be a string", ErrorCode.VALIDATION, 400)
    assignee = body.get("assignee")
    if assignee is not None and not isinstance(assignee, str):
        return _error_response("assignee must be a string", ErrorCode.VALIDATION, 400)
    fields = body.get("fields")
    if fields is not None and not isinstance(fields, dict):
        return _error_response("fields must be a dict", ErrorCode.VALIDATION, 400)
    return {
        "issue_ids": issue_ids,
        "status": status,
        "priority": priority,
        "assignee": assignee,
        "fields": fields,
        "actor": actor,
        "expected_assignee": expected_assignee,
    }


def _parse_batch_close_body(body: dict[str, Any]) -> dict[str, Any] | JSONResponse:
    """Validate the batch-close request body.

    Shared by classic ``POST /api/batch/close`` and loom
    ``POST /api/loom/batch/close``. Returns kwargs for ``db.batch_close``
    on success, or a 400 ``JSONResponse`` on error.

    Accepts optional ``force`` (bool) to use the template reverse/escape
    transition on every item — matches the CLI ``--force`` and MCP
    ``force`` argument on ``batch_close``. Use only for cleanup flows
    that intentionally leave the normal workflow (e.g. a planning batch in
    its initial state where the type's default done state is not reachable).
    """
    issue_ids = body.get("issue_ids")
    if not isinstance(issue_ids, list):
        return _error_response("issue_ids must be a JSON array", ErrorCode.VALIDATION, 400)
    if not all(isinstance(i, str) for i in issue_ids):
        return _error_response("All issue_ids must be strings", ErrorCode.VALIDATION, 400)
    # default="" means the helper returns str on success and JSONResponse on
    # type error — so the not-a-str branch carries the validation response.
    # (JSONResponse is only TYPE_CHECKING-imported at module scope; we can't
    # name it for isinstance() here without forcing a runtime import.)
    reason = _validate_body_string_field(body, "reason", default="")
    if not isinstance(reason, str):
        return reason
    actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
    if actor_err:
        return actor_err
    expected_assignee = body.get("expected_assignee")
    if expected_assignee is not None and not isinstance(expected_assignee, str):
        return _error_response("expected_assignee must be a string", ErrorCode.VALIDATION, 400)
    force = body.get("force", False)
    if not isinstance(force, bool):
        return _error_response("force must be a boolean", ErrorCode.VALIDATION, 400)
    if force:
        # 2.1.0 §1.1: HTTP callers can't use the workflow escape lane
        # unless the dashboard was started with
        # ``--allow-http-force-close``. Runtime import avoids forming a
        # cycle at module load (filigree.dashboard already imports from
        # the routes package).
        from filigree.dashboard import _get_allow_http_force_close

        if not _get_allow_http_force_close():
            return _error_response(
                "force=true is not permitted on HTTP batch-close. "
                "Start the dashboard with --allow-http-force-close to opt in, "
                "or use the CLI / MCP ``batch_close`` with force=true.",
                ErrorCode.VALIDATION,
                400,
            )
    return {
        "issue_ids": issue_ids,
        "reason": reason,
        "actor": actor,
        "expected_assignee": expected_assignee,
        "force": force,
    }


def _parse_release_claim_body(body: dict[str, Any]) -> dict[str, Any] | JSONResponse:
    """Validate optional release-claim body fields shared by classic and loom."""
    if_held = body.get("if_held", False)
    if not isinstance(if_held, bool):
        return _error_response("if_held must be a boolean", ErrorCode.VALIDATION, 400)
    expected_assignee = body.get("expected_assignee")
    if expected_assignee is not None and not isinstance(expected_assignee, str):
        return _error_response("expected_assignee must be a string", ErrorCode.VALIDATION, 400)
    reason = _validate_body_string_field(body, "reason", default="")
    if not isinstance(reason, str):
        return reason
    return {"if_held": if_held, "expected_assignee": expected_assignee, "reason": reason}


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_classic_router() -> APIRouter:
    """Build the classic-generation APIRouter for issue, workflow, and
    dependency endpoints.

    NOTE: All handlers are intentionally async despite doing synchronous
    SQLite I/O. This serializes DB access on the event loop thread,
    avoiding concurrent multi-thread access to the shared DB connection.
    """
    from fastapi import APIRouter, Depends
    from fastapi.responses import JSONResponse

    from filigree.dashboard import _get_db

    router = APIRouter()

    @router.get("/issues")
    async def api_issues(db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        issues = _fetch_all_issues(db)
        return JSONResponse([i.to_dict() for i in issues])

    @router.get("/ready")
    async def api_ready(db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Issues with no open blockers, sorted by priority."""
        issues = db.get_ready()
        return JSONResponse([i.to_dict() for i in issues])

    @router.get("/issue/{issue_id}")
    async def api_issue_detail(issue_id: str, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Full issue detail with dependency details, events, and comments."""
        err = _check_read_prefix_in_server_mode(db, issue_id)
        if err is not None:
            return err
        try:
            issue = db.get_issue(issue_id)
        except KeyError:
            return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)

        # Resolve dep details for blocks and blocked_by in a single query
        dep_ids = list(set(issue.blocks + issue.blocked_by))
        dep_details: dict[str, DepDetail] = {}
        if dep_ids:
            placeholders = ",".join("?" * len(dep_ids))
            rows = db.conn.execute(
                f"SELECT id, title, status, type, priority FROM issues WHERE id IN ({placeholders})",
                dep_ids,
            ).fetchall()
            found = {r["id"]: r for r in rows}
            for did in dep_ids:
                if did in found:
                    r = found[did]
                    dep_details[did] = DepDetail(
                        title=r["title"],
                        status=r["status"],
                        status_category=db._resolve_status_category(r["type"], r["status"]),
                        priority=r["priority"],
                    )
                else:
                    logger.warning("Dangling dependency reference %s in issue %s", did, issue_id)
                    dep_details[did] = DepDetail(
                        title=f"[Deleted: {did}]",
                        status="deleted",
                        status_category="done",
                        priority=4,
                    )

        # Events — NOTE: SQL column list must stay in sync with IssueDetailEvent fields.
        # IssueDetailEvent is a slim 5-column projection — NOT full EventRecord.
        events = db.conn.execute(
            "SELECT event_type, actor, old_value, new_value, created_at FROM events WHERE issue_id = ? ORDER BY created_at DESC LIMIT 20",
            (issue_id,),
        ).fetchall()
        event_list: list[IssueDetailEvent] = [
            IssueDetailEvent(
                event_type=e["event_type"],
                actor=e["actor"],
                old_value=e["old_value"],
                new_value=e["new_value"],
                created_at=e["created_at"],
            )
            for e in events
        ]

        result = EnrichedIssueDetail(
            **issue.to_dict(),
            dep_details=dep_details,
            events=event_list,
            comments=db.get_comments(issue_id),
        )
        return JSONResponse(result)

    @router.get("/dependencies")
    async def api_dependencies(db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        deps = db.get_all_dependencies()
        return JSONResponse(deps)

    @router.get("/type/{type_name}")
    async def api_type_template(type_name: str, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Workflow template for a given issue type."""
        tpl = db.templates.get_type(type_name)
        if tpl is None:
            # Unknown type_name is input validation (rejected-enum-value),
            # not resource-lookup, so VALIDATION + 400 matches the pattern
            # used everywhere else in this file for rejected enum values.
            valid_types = [t.type for t in db.templates.list_types()]
            return _error_response(
                f'Unknown type "{type_name}". Valid types: {", ".join(valid_types)}',
                ErrorCode.VALIDATION,
                400,
                {"param": "type_name", "value": type_name, "valid_types": valid_types},
            )
        return JSONResponse(
            {
                "type": tpl.type,
                "display_name": tpl.display_name,
                "states": [{"name": s.name, "category": s.category} for s in tpl.states],
                "initial_state": tpl.initial_state,
                "transitions": [{"from": t.from_state, "to": t.to_state, "enforcement": t.enforcement} for t in tpl.transitions],
                "reverse_transitions": [
                    {"from": t.from_state, "to": t.to_state, "enforcement": t.enforcement} for t in tpl.reverse_transitions
                ],
            }
        )

    @router.get("/issue/{issue_id}/transitions")
    async def api_issue_transitions(issue_id: str, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Valid next states for an issue."""
        err = _check_read_prefix_in_server_mode(db, issue_id)
        if err is not None:
            return err
        try:
            transitions = db.get_valid_transitions(issue_id)
        except KeyError:
            return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)
        return JSONResponse(
            [
                {
                    "to": t.to,
                    "category": t.category,
                    "enforcement": t.enforcement,
                    "ready": t.ready,
                    "missing_fields": list(t.missing_fields),
                    "requires_fields": list(t.requires_fields),
                }
                for t in transitions
            ]
        )

    @router.get("/issue/{issue_id}/files")
    async def api_issue_files(issue_id: str, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Files associated with an issue."""
        err = _check_read_prefix_in_server_mode(db, issue_id)
        if err is not None:
            return err
        try:
            db.get_issue(issue_id)
        except KeyError:
            return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)
        files = db.get_issue_files(issue_id)
        return JSONResponse(files)

    @router.get("/issue/{issue_id}/findings")
    async def api_issue_findings(issue_id: str, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Scan findings related to an issue."""
        err = _check_read_prefix_in_server_mode(db, issue_id)
        if err is not None:
            return err
        try:
            db.get_issue(issue_id)
        except KeyError:
            return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)
        findings = db.get_issue_findings(issue_id)
        return JSONResponse([f.to_dict() for f in findings])

    @router.patch("/issue/{issue_id}")
    async def api_update_issue(issue_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Update issue fields (status, priority, assignee, etc.)."""
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
        if actor_err:
            return actor_err
        expected_assignee = body.get("expected_assignee")
        if expected_assignee is not None and not isinstance(expected_assignee, str):
            return _error_response("expected_assignee must be a string", ErrorCode.VALIDATION, 400)
        force_overwrite_corrupt = body.get("force_overwrite_corrupt", False)
        if not isinstance(force_overwrite_corrupt, bool):
            return _error_response("force_overwrite_corrupt must be a boolean", ErrorCode.VALIDATION, 400)
        body.pop("actor", None)
        priority = _validate_priority_field(body)
        if isinstance(priority, JSONResponse):
            return priority
        title = _validate_body_string_field(body, "title", default=None)
        if isinstance(title, JSONResponse):
            return title
        description = _validate_body_string_field(body, "description", default=None)
        if isinstance(description, JSONResponse):
            return description
        notes = _validate_body_string_field(body, "notes", default=None)
        if isinstance(notes, JSONResponse):
            return notes
        parent_id = _validate_body_string_field(body, "parent_id", default=None)
        if isinstance(parent_id, JSONResponse):
            return parent_id
        status = _validate_body_string_field(body, "status", default=None)
        if isinstance(status, JSONResponse):
            return status
        try:
            issue = db.update_issue(
                issue_id,
                status=status,
                priority=priority,
                assignee=body.get("assignee"),
                title=title,
                description=description,
                notes=notes,
                parent_id=parent_id,
                fields=body.get("fields"),
                actor=actor,
                expected_assignee=expected_assignee,
                force_overwrite_corrupt=force_overwrite_corrupt,
            )
        except KeyError:
            return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)
        except WrongProjectError as e:
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        except TypeError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        except ValueError as e:
            code = _classify_issue_write_error(e)
            return _error_response(str(e), code, errorcode_to_http_status(code), _issue_write_error_details(e))
        return JSONResponse(issue.to_dict())

    @router.post("/issue/{issue_id}/close")
    async def api_close_issue(issue_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Close an issue."""
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
        if actor_err:
            return actor_err
        expected_assignee = body.get("expected_assignee")
        if expected_assignee is not None and not isinstance(expected_assignee, str):
            return _error_response("expected_assignee must be a string", ErrorCode.VALIDATION, 400)
        # default="" with allow_null=False means the helper returns str on
        # success and JSONResponse on type error; narrow on `not isinstance
        # str` so mypy keeps the `str` branch through the close call.
        reason = _validate_body_string_field(body, "reason", default="")
        if not isinstance(reason, str):
            return reason
        status_field = body.get("status")
        if status_field is not None and not isinstance(status_field, str):
            return _error_response("status must be a string", ErrorCode.VALIDATION, 400)
        fields = body.get("fields")
        try:
            annotation_warnings = db.get_annotation_closeout_warnings(issue_id)
            issue = db.close_issue(
                issue_id,
                reason=reason,
                status=status_field,
                actor=actor,
                fields=fields,
                expected_assignee=expected_assignee,
            )
        except KeyError:
            return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)
        except WrongProjectError as e:
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        except TypeError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        except ValueError as e:
            code = _classify_issue_write_error(e)
            return _error_response(str(e), code, errorcode_to_http_status(code), _issue_write_error_details(e))
        result: dict[str, Any] = dict(issue.to_dict())
        if annotation_warnings:
            result["annotation_warnings"] = annotation_warnings
        return JSONResponse(result)

    @router.post("/issue/{issue_id}/reopen")
    async def api_reopen_issue(issue_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Reopen a closed issue."""
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
        if actor_err:
            return actor_err
        try:
            issue = db.reopen_issue(issue_id, actor=actor)
        except KeyError:
            return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)
        except WrongProjectError as e:
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        except ValueError as e:
            code = classify_value_error(str(e))
            return _error_response(str(e), code, errorcode_to_http_status(code))
        return JSONResponse(issue.to_dict())

    @router.post("/issue/{issue_id}/comments", status_code=201)
    async def api_add_comment(issue_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Add a comment to an issue."""
        # Prefix check first — db.get_issue() ignores prefix on reads, so a
        # foreign-project ID would otherwise be reported as NOT_FOUND, masking
        # the WrongProjectError the write would raise. Mirrors meta CLI's
        # add-comment fix; see filigree-25b44e65e2.
        try:
            db._check_id_prefix(issue_id)
        except WrongProjectError as e:
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        try:
            db.get_issue(issue_id)
        except KeyError:
            return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        text = body.get("text", "")
        if not isinstance(text, str):
            return _error_response("text must be a string", ErrorCode.VALIDATION, 400)
        author, author_err = _validate_actor(body.get("author", "dashboard"))
        if author_err:
            return author_err
        expected_assignee = body.get("expected_assignee")
        if expected_assignee is not None and not isinstance(expected_assignee, str):
            return _error_response("expected_assignee must be a string", ErrorCode.VALIDATION, 400)
        try:
            comment_id = db.add_comment(issue_id, text, author=author, expected_assignee=expected_assignee)
        except ValueError as e:
            code = _classify_issue_write_error(e)
            return _error_response(str(e), code, errorcode_to_http_status(code), _issue_write_error_details(e))
        # Fetch just the single comment to get the real created_at timestamp
        row = db.conn.execute("SELECT created_at FROM comments WHERE id = ?", (comment_id,)).fetchone()
        if row is None:
            logger = logging.getLogger(__name__)
            logger.error("Comment %d not found immediately after INSERT for issue %s", comment_id, issue_id)
            return _error_response("Internal error: comment created but not retrievable", ErrorCode.INTERNAL, 500)
        created_at = ISOTimestamp(row["created_at"])
        return JSONResponse(
            CommentRecord(id=comment_id, author=author, text=text, created_at=created_at),
            status_code=201,
        )

    @router.get("/search")
    async def api_search(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Full-text search across issues."""
        params = request.query_params
        # Parse without min/max so out-of-range values still clamp (pinned by
        # tests/api/test_api.py::TestBoundaryClamping); we only want to reject
        # malformed inputs (non-integer) with the flat 400 envelope.
        limit_raw = _safe_int(params.get("limit", "50"), "limit")
        if not isinstance(limit_raw, int):
            return limit_raw
        offset_raw = _safe_int(params.get("offset", "0"), "offset")
        if not isinstance(offset_raw, int):
            return offset_raw
        limit = min(max(limit_raw, 1), 1000)
        offset = max(offset_raw, 0)
        # Reject offsets that won't bind into SQLite's signed-int64 OFFSET.
        # Lower-bound clamping is intentionally lenient (pinned by tests),
        # but the upper bound has no clamp-friendly meaning — a typoed
        # 10**22 is a client bug, not a request to skip everything.
        if offset > _MAX_PAGINATION_OFFSET:
            return _error_response(
                f"offset must be at most {_MAX_PAGINATION_OFFSET}, got {offset}",
                ErrorCode.VALIDATION,
                400,
            )
        q = params.get("q", "")
        if not q.strip():
            return JSONResponse({"results": [], "total": 0})
        total = db.count_search_results(q)
        page = db.search_issues(q, limit=limit, offset=offset)
        return JSONResponse({"results": [i.to_dict() for i in page], "total": total})

    @router.get("/plan/{milestone_id}")
    async def api_plan(milestone_id: str, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Milestone plan tree."""
        err = _check_read_prefix_in_server_mode(db, milestone_id)
        if err is not None:
            return err
        try:
            plan = db.get_plan(milestone_id)
        except KeyError:
            return _error_response(f"Issue not found: {milestone_id}", ErrorCode.NOT_FOUND, 404)
        except NotAMilestoneError as e:
            return _error_response(str(e), ErrorCode.NOT_FOUND, 404)
        return JSONResponse(plan)

    @router.post("/batch/update")
    async def api_batch_update(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Batch update issues."""
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        parsed = _parse_batch_update_body(body)
        if isinstance(parsed, JSONResponse):
            return parsed
        issue_ids = parsed.pop("issue_ids")
        try:
            updated, errors = db.batch_update(issue_ids, **parsed)
        except WrongProjectError as e:
            # 2.1.0 §0.4: a foreign-prefix id in a batch aborts envelope-
            # level rather than producing N misleading per-item errors.
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        except TypeError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        return JSONResponse(
            {
                "updated": [i.to_dict() for i in updated],
                "errors": errors,
            }
        )

    @router.post("/batch/close")
    async def api_batch_close(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Batch close issues."""
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        parsed = _parse_batch_close_body(body)
        if isinstance(parsed, JSONResponse):
            return parsed
        issue_ids = parsed.pop("issue_ids")
        try:
            closed, errors = db.batch_close(issue_ids, **parsed)
        except WrongProjectError as e:
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        return JSONResponse(
            {
                "closed": [i.to_dict() for i in closed],
                "errors": errors,
            }
        )

    @router.get("/types")
    async def api_types_list(db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """List all registered issue types."""
        types = db.templates.list_types()
        return JSONResponse(
            [
                {
                    "type": t.type,
                    "display_name": t.display_name,
                    "pack": t.pack,
                    "initial_state": t.initial_state,
                }
                for t in types
            ]
        )

    @router.post("/issues", status_code=201)
    async def api_create_issue(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Create a new issue."""
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        title = body.get("title", "")
        if not isinstance(title, str):
            return _error_response("title must be a string", ErrorCode.VALIDATION, 400)
        actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
        if actor_err:
            return actor_err
        priority = _validate_priority_field(body, default=2)
        if isinstance(priority, JSONResponse):
            return priority
        if priority is None:
            priority = 2
        parent_id = _validate_body_string_field(body, "parent_id", allow_null=True, default=None)
        if isinstance(parent_id, JSONResponse):
            return parent_id
        description = _validate_body_string_field(body, "description", default="")
        if isinstance(description, JSONResponse):
            return description
        if description is None:
            description = ""
        notes = _validate_body_string_field(body, "notes", default="")
        if isinstance(notes, JSONResponse):
            return notes
        if notes is None:
            notes = ""
        try:
            issue = db.create_issue(
                title,
                type=body.get("type", "task"),
                priority=priority,
                parent_id=parent_id,
                assignee=body.get("assignee", ""),
                description=description,
                notes=notes,
                fields=body.get("fields"),
                labels=body.get("labels"),
                deps=body.get("deps"),
                actor=actor,
            )
        except TypeError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        except WrongProjectError as e:
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        except ValueError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        return JSONResponse(issue.to_dict(), status_code=201)

    @router.post("/issue/{issue_id}/claim")
    async def api_claim_issue(issue_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Claim an issue."""
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        assignee = body.get("assignee", "")
        if not isinstance(assignee, str):
            return _error_response("assignee must be a string", ErrorCode.VALIDATION, 400)
        if not assignee or not assignee.strip():
            return _error_response("assignee is required and cannot be empty", ErrorCode.VALIDATION, 400)
        actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
        if actor_err:
            return actor_err
        try:
            issue = db.claim_issue(issue_id, assignee=assignee, actor=actor)
        except KeyError:
            return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)
        except WrongProjectError as e:
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        except ClaimConflictError as e:
            return _error_response(str(e), ErrorCode.CONFLICT, 409, claim_conflict_details(e))
        except ValueError as e:
            code = classify_value_error(str(e))
            return _error_response(str(e), code, errorcode_to_http_status(code))
        return JSONResponse(issue.to_dict())

    @router.post("/issue/{issue_id}/release")
    async def api_release_claim(issue_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Release a claimed issue."""
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
        if actor_err:
            return actor_err
        release_options = _parse_release_claim_body(body)
        if isinstance(release_options, JSONResponse):
            return release_options
        try:
            issue = db.release_claim(issue_id, actor=actor, **release_options)
        except KeyError:
            return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)
        except WrongProjectError as e:
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        except InvalidTransitionError as e:
            return _error_response(
                str(e),
                ErrorCode.INVALID_TRANSITION,
                errorcode_to_http_status(ErrorCode.INVALID_TRANSITION),
                _invalid_transition_details(e),
            )
        except ClaimConflictError as e:
            return _error_response(str(e), ErrorCode.CONFLICT, 409, claim_conflict_details(e))
        except ValueError as e:
            code = _classify_release_claim_error(issue_id, e)
            return _error_response(str(e), code, errorcode_to_http_status(code), _issue_write_error_details(e))
        return JSONResponse(issue.to_dict())

    @router.post("/claim-next")
    async def api_claim_next(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Claim the highest-priority ready issue."""
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        assignee = body.get("assignee", "")
        if not isinstance(assignee, str):
            return _error_response("assignee must be a string", ErrorCode.VALIDATION, 400)
        if not assignee or not assignee.strip():
            return _error_response("assignee is required and cannot be empty", ErrorCode.VALIDATION, 400)
        actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
        if actor_err:
            return actor_err
        try:
            issue = db.claim_next(assignee, actor=actor)
        except ClaimConflictError as e:
            return _error_response(str(e), ErrorCode.CONFLICT, 409, claim_conflict_details(e))
        except ValueError as e:
            code = classify_value_error(str(e))
            return _error_response(str(e), code, errorcode_to_http_status(code))
        if issue is None:
            return _error_response("No ready issues to claim", ErrorCode.NOT_FOUND, 404)
        return JSONResponse(issue.to_dict())

    @router.post("/issue/{issue_id}/dependencies")
    async def api_add_dependency(issue_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Add a dependency: issue_id depends on depends_on."""
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        depends_on = body.get("depends_on", "")
        if not depends_on or not isinstance(depends_on, str) or not depends_on.strip():
            return _error_response("depends_on is required and must be a non-empty string", ErrorCode.VALIDATION, 400)
        actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
        if actor_err:
            return actor_err
        try:
            added = db.add_dependency(issue_id, depends_on, actor=actor)
        except KeyError as e:
            return _error_response(str(e), ErrorCode.NOT_FOUND, 404)
        except WrongProjectError as e:
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        except ValueError as e:
            return _error_response(str(e), ErrorCode.CONFLICT, 409)
        return JSONResponse({"added": added})

    @router.delete("/issue/{issue_id}/dependencies/{dep_id}")
    async def api_remove_dependency(
        issue_id: str, dep_id: str, actor: str = "dashboard", db: FiligreeDB = Depends(_get_db)
    ) -> JSONResponse:
        """Remove a dependency."""
        clean_actor, actor_err = _validate_actor(actor)
        if actor_err:
            return actor_err
        try:
            removed = db.remove_dependency(issue_id, dep_id, actor=clean_actor)
        except KeyError as e:
            return _error_response(str(e), ErrorCode.NOT_FOUND, 404)
        except WrongProjectError as e:
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        return JSONResponse({"removed": removed})

    return router


def create_loom_router() -> APIRouter:
    """Build the loom-generation APIRouter for issue, workflow, and
    dependency endpoints.

    Phase C2 mounts the batch endpoints (``/batch/update``,
    ``/batch/close``); Phase C3 adds the single-issue surface (GET,
    create, update, close, reopen, claim, release, claim-next,
    comments, dependencies). See ADR-002 for the generation framing
    and ``tests/fixtures/contracts/loom/`` for the response-shape
    pins.

    Path conventions: loom uses ``/issues/{issue_id}`` (plural,
    symmetric with the ``/issues`` collection); classic uses
    ``/issue/{issue_id}`` (singular). The two never collide so
    living-surface aliases at ``/api/issues/{issue_id}`` could land
    later — they are deliberately not added in C3 to keep the loom
    surface as the single recommended entry point until federation
    consumers stabilise.
    """
    from fastapi import APIRouter, Depends
    from fastapi.responses import JSONResponse

    from filigree.dashboard import _get_db
    from filigree.dashboard_routes.common import _get_bool_param, _parse_pagination, _parse_response_detail
    from filigree.generations.loom.adapters import (
        blocked_issue_to_loom,
        comment_record_to_loom,
        file_assoc_to_loom,
        issue_event_to_loom,
        issue_to_loom,
        list_response,
        pack_to_loom,
        slim_issue_to_loom,
        type_template_to_loom,
    )
    from filigree.generations.loom.types import CommentRecordLoom

    router = APIRouter()

    @router.get("/issues")
    async def api_loom_list_issues(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """List issues — ``ListResponse[IssueLoom]`` with real pagination.

        Loom adds ``?limit=&offset=`` (default limit=100). Classic
        ``GET /api/issues`` returns every row in one shot and stays
        unchanged. The loom variant overfetches by 1 to detect
        ``has_more`` without a separate COUNT query.
        """
        params = request.query_params
        pagination = _parse_pagination(params)
        if isinstance(pagination, JSONResponse):
            return pagination
        limit, offset = pagination
        try:
            page = db.list_issues(limit=limit + 1, offset=offset)
        except ValueError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        has_more = len(page) > limit
        if has_more:
            page = page[:limit]
        items = [issue_to_loom(i) for i in page]
        return JSONResponse(list_response(items, limit=limit, offset=offset, has_more=has_more))

    @router.get("/ready")
    async def api_loom_ready(db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Issues ready to work (no open blockers) — ``ListResponse[IssueLoom]``.

        Returns the full result set — ``get_ready()`` is unbounded today
        and ``has_more`` is always ``false``.
        """
        issues = db.get_ready()
        items = [issue_to_loom(i) for i in issues]
        return JSONResponse(list_response(items, limit=len(items), offset=0, has_more=False))

    @router.get("/blocked")
    async def api_loom_blocked(db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Issues with at least one open blocker — ``ListResponse[BlockedIssueLoom]``.

        Loom-only (no classic counterpart). Returns the full result set
        — ``get_blocked()`` is unbounded today and ``has_more`` is
        always ``false``.
        """
        issues = db.get_blocked()
        items = [blocked_issue_to_loom(i) for i in issues]
        return JSONResponse(list_response(items, limit=len(items), offset=0, has_more=False))

    @router.get("/search")
    async def api_loom_search(
        request: Request,
        db: FiligreeDB = Depends(_get_db),
    ) -> JSONResponse:
        """Full-text search — ``ListResponse[IssueLoom]``.

        Classic ``GET /api/search`` returns ``{results, total}``. Loom
        drops the running total to keep the unified envelope (consumers
        needing total can hit ``/api/stats``).
        """
        params = request.query_params
        # See classic /api/search: parse without min/max so clamp tests still
        # pass; only malformed (non-integer) values produce 400.
        limit_raw = _safe_int(params.get("limit", "50"), "limit")
        if not isinstance(limit_raw, int):
            return limit_raw
        offset_raw = _safe_int(params.get("offset", "0"), "offset")
        if not isinstance(offset_raw, int):
            return offset_raw
        limit = min(max(limit_raw, 1), 1000)
        offset = max(offset_raw, 0)
        # Mirror classic /api/search: reject offsets that won't bind into
        # SQLite's signed-int64 OFFSET. See filigree-0ad97ea6e0.
        if offset > _MAX_PAGINATION_OFFSET:
            return _error_response(
                f"offset must be at most {_MAX_PAGINATION_OFFSET}, got {offset}",
                ErrorCode.VALIDATION,
                400,
            )
        q = params.get("q", "")
        if not q.strip():
            return JSONResponse(list_response([], limit=limit, offset=offset, has_more=False))
        total = db.count_search_results(q)
        page = db.search_issues(q, limit=limit, offset=offset)
        items = [issue_to_loom(i) for i in page]
        return JSONResponse(list_response(items, limit=limit, offset=offset, total=total))

    @router.get("/types")
    async def api_loom_list_types(db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """List registered issue types — ``ListResponse[TypeSummaryLoom]``."""
        types = db.templates.list_types()
        items = [type_template_to_loom(t) for t in types]
        return JSONResponse(list_response(items, limit=len(items), offset=0, has_more=False))

    @router.get("/packs")
    async def api_loom_list_packs(db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """List enabled workflow packs — ``ListResponse[PackLoom]``.

        Loom-only (no classic dashboard counterpart). Mirrors MCP's
        ``list_packs`` projection.
        """
        packs = sorted(db.templates.list_packs(), key=lambda p: p.pack)
        items = [pack_to_loom(p) for p in packs]
        return JSONResponse(list_response(items, limit=len(items), offset=0, has_more=False))

    @router.get("/issues/{issue_id}/comments")
    async def api_loom_get_comments(issue_id: str, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Comments for an issue — ``ListResponse[CommentRecordLoom]``.

        Validates the issue exists (404 ``NOT_FOUND`` if not) so missing
        issues do not silently return an empty list. ``db.get_comments``
        itself returns ``[]`` for unknown ids; the parent check matches
        the pattern used by ``GET /api/issue/{id}/files`` and
        ``POST /api/issue/{id}/comments``.
        """
        err = _check_read_prefix_in_server_mode(db, issue_id)
        if err is not None:
            return err
        try:
            db.get_issue(issue_id)
        except KeyError:
            return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)
        comments = db.get_comments(issue_id)
        items = [comment_record_to_loom(c) for c in comments]
        return JSONResponse(list_response(items, limit=len(items), offset=0, has_more=False))

    @router.get("/issues/{issue_id}/events")
    async def api_loom_get_issue_events(issue_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Event history for an issue — ``ListResponse[IssueEventLoom]``.

        ``db.get_issue_events`` validates the issue exists (raises
        ``KeyError``) and accepts a ``limit``. Default 50 to match MCP
        defaults; classic dashboard has no counterpart for this endpoint.
        """
        err = _check_read_prefix_in_server_mode(db, issue_id)
        if err is not None:
            return err
        params = request.query_params
        pagination = _parse_pagination(params, default_limit=50)
        if isinstance(pagination, JSONResponse):
            return pagination
        limit, offset = pagination
        try:
            events = db.get_issue_events(issue_id, limit=limit + 1, offset=offset)
        except KeyError:
            return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)
        has_more = len(events) > limit
        if has_more:
            events = events[:limit]
        items = [issue_event_to_loom(e) for e in events]
        return JSONResponse(list_response(items, limit=limit, offset=offset, has_more=has_more))

    @router.get("/issues/{issue_id}/files")
    async def api_loom_get_issue_files(issue_id: str, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """File associations for an issue — ``ListResponse[FileAssocLoom]``.

        Validates the issue exists (404 ``NOT_FOUND`` if not) — the
        underlying ``db.get_issue_files`` does not. Matches the classic
        ``GET /api/issue/{id}/files`` validation pattern.
        """
        err = _check_read_prefix_in_server_mode(db, issue_id)
        if err is not None:
            return err
        try:
            db.get_issue(issue_id)
        except KeyError:
            return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)
        assocs = db.get_issue_files(issue_id)
        items = [file_assoc_to_loom(a) for a in assocs]
        return JSONResponse(list_response(items, limit=len(items), offset=0, has_more=False))

    @router.post("/batch/update")
    async def api_loom_batch_update(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Batch update issues — loom envelope.

        ``response_detail=slim`` (default) keeps the historical C2 shape
        (``BatchResponse[SlimIssueLoom]``); ``response_detail=full``
        upgrades ``succeeded[]`` items to full ``IssueLoom``. Validation
        of the query param runs before body parsing so a malformed
        ``response_detail`` returns 400 even on a malformed body.
        """
        detail = _parse_response_detail(request.query_params)
        if isinstance(detail, JSONResponse):
            return detail
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        parsed = _parse_batch_update_body(body)
        if isinstance(parsed, JSONResponse):
            return parsed
        issue_ids = parsed.pop("issue_ids")
        try:
            updated, errors = db.batch_update(issue_ids, **parsed)
        except WrongProjectError as e:
            # 2.1.0 §0.4: envelope-level abort on foreign-prefix.
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        except TypeError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        project = issue_to_loom if detail == "full" else slim_issue_to_loom
        return JSONResponse(
            {
                "succeeded": [project(i) for i in updated],
                "failed": errors,
            }
        )

    @router.post("/batch/close")
    async def api_loom_batch_close(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Batch close issues — loom envelope.

        ``response_detail=slim`` (default) returns ``SlimIssueLoom`` in
        ``succeeded[]``; ``response_detail=full`` returns ``IssueLoom``.
        ``newly_unblocked[]`` stays ``SlimIssueLoom`` regardless — it
        represents *secondary* state (consumers branch on its presence
        to decide whether to refetch); upgrading would inflate the
        response without buying federation consumers anything new. See
        ``docs/federation/contracts.md`` for the locked C5 rule.

        Includes ``newly_unblocked`` (omitted when empty) computed the
        same way MCP ``batch_close`` does it: diff ``get_ready()``
        before vs. after. Classic ``/api/batch/close`` does not surface
        ``newly_unblocked`` and stays unchanged.
        """
        detail = _parse_response_detail(request.query_params)
        if isinstance(detail, JSONResponse):
            return detail
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        parsed = _parse_batch_close_body(body)
        if isinstance(parsed, JSONResponse):
            return parsed
        issue_ids = parsed.pop("issue_ids")
        ready_before = {i.id for i in db.get_ready()}
        try:
            closed, errors = db.batch_close(issue_ids, **parsed)
        except WrongProjectError as e:
            # 2.1.0 §0.4: envelope-level abort on foreign-prefix.
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        ready_after = db.get_ready()
        newly_unblocked = [i for i in ready_after if i.id not in ready_before]
        project = issue_to_loom if detail == "full" else slim_issue_to_loom
        response: dict[str, Any] = {
            "succeeded": [project(i) for i in closed],
            "failed": errors,
        }
        if newly_unblocked:
            response["newly_unblocked"] = [slim_issue_to_loom(i) for i in newly_unblocked]
        return JSONResponse(response)

    @router.get("/issues/{issue_id}")
    async def api_loom_get_issue(issue_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Get a single issue — IssueLoom (or IssueLoomWithFiles when
        ``include_files=true``).

        Loom defaults ``include_files`` to ``False`` (federation
        consumers usually want a clean issue projection without the
        file-association payload). Classic ``GET /api/issue/{id}`` does
        not expose ``include_files`` and continues to return its
        existing ``EnrichedIssueDetail`` envelope.
        """
        err = _check_read_prefix_in_server_mode(db, issue_id)
        if err is not None:
            return err
        include_files = _get_bool_param(request.query_params, "include_files", default=False)
        if isinstance(include_files, JSONResponse):
            return include_files
        try:
            issue = db.get_issue(issue_id)
        except KeyError:
            return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)
        body: dict[str, Any] = dict(issue_to_loom(issue))
        if include_files:
            body["files"] = [file_assoc_to_loom(a) for a in db.get_issue_files(issue_id)]
        return JSONResponse(body)

    @router.post("/issues", status_code=201)
    async def api_loom_create_issue(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Create an issue — returns ``IssueLoom``."""
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        title = body.get("title", "")
        if not isinstance(title, str):
            return _error_response("title must be a string", ErrorCode.VALIDATION, 400)
        actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
        if actor_err:
            return actor_err
        priority = _validate_priority_field(body, default=2)
        if isinstance(priority, JSONResponse):
            return priority
        if priority is None:
            priority = 2
        parent_id = _validate_body_string_field(body, "parent_id", allow_null=True, default=None)
        if isinstance(parent_id, JSONResponse):
            return parent_id
        description = _validate_body_string_field(body, "description", default="")
        if isinstance(description, JSONResponse):
            return description
        if description is None:
            description = ""
        notes = _validate_body_string_field(body, "notes", default="")
        if isinstance(notes, JSONResponse):
            return notes
        if notes is None:
            notes = ""
        try:
            issue = db.create_issue(
                title,
                type=body.get("type", "task"),
                priority=priority,
                parent_id=parent_id,
                assignee=body.get("assignee", ""),
                description=description,
                notes=notes,
                fields=body.get("fields"),
                labels=body.get("labels"),
                deps=body.get("deps"),
                actor=actor,
            )
        except WrongProjectError as e:
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        except (TypeError, ValueError) as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        return JSONResponse(issue_to_loom(issue), status_code=201)

    @router.patch("/issues/{issue_id}")
    async def api_loom_update_issue(issue_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Update issue fields — returns ``IssueLoom``."""
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
        if actor_err:
            return actor_err
        expected_assignee = body.get("expected_assignee")
        if expected_assignee is not None and not isinstance(expected_assignee, str):
            return _error_response("expected_assignee must be a string", ErrorCode.VALIDATION, 400)
        force_overwrite_corrupt = body.get("force_overwrite_corrupt", False)
        if not isinstance(force_overwrite_corrupt, bool):
            return _error_response("force_overwrite_corrupt must be a boolean", ErrorCode.VALIDATION, 400)
        body.pop("actor", None)
        priority = _validate_priority_field(body)
        if isinstance(priority, JSONResponse):
            return priority
        title = _validate_body_string_field(body, "title", default=None)
        if isinstance(title, JSONResponse):
            return title
        description = _validate_body_string_field(body, "description", default=None)
        if isinstance(description, JSONResponse):
            return description
        notes = _validate_body_string_field(body, "notes", default=None)
        if isinstance(notes, JSONResponse):
            return notes
        parent_id = _validate_body_string_field(body, "parent_id", default=None)
        if isinstance(parent_id, JSONResponse):
            return parent_id
        status = _validate_body_string_field(body, "status", default=None)
        if isinstance(status, JSONResponse):
            return status
        try:
            issue = db.update_issue(
                issue_id,
                status=status,
                priority=priority,
                assignee=body.get("assignee"),
                title=title,
                description=description,
                notes=notes,
                parent_id=parent_id,
                fields=body.get("fields"),
                actor=actor,
                expected_assignee=expected_assignee,
                force_overwrite_corrupt=force_overwrite_corrupt,
            )
        except KeyError:
            return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)
        except WrongProjectError as e:
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        except TypeError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        except ValueError as e:
            code = _classify_issue_write_error(e)
            return _error_response(str(e), code, errorcode_to_http_status(code), _issue_write_error_details(e))
        return JSONResponse(issue_to_loom(issue))

    @router.post("/issues/{issue_id}/close")
    async def api_loom_close_issue(issue_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Close an issue — returns ``IssueLoom`` (or
        ``IssueLoomWithUnblocked`` when at least one issue became ready
        as a result, mirroring MCP ``close_issue``).
        """
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
        if actor_err:
            return actor_err
        expected_assignee = body.get("expected_assignee")
        if expected_assignee is not None and not isinstance(expected_assignee, str):
            return _error_response("expected_assignee must be a string", ErrorCode.VALIDATION, 400)
        # See classic close handler: narrow on `not isinstance str`
        # to keep mypy's str-branch through the close call.
        reason = _validate_body_string_field(body, "reason", default="")
        if not isinstance(reason, str):
            return reason
        status_field = body.get("status")
        if status_field is not None and not isinstance(status_field, str):
            return _error_response("status must be a string", ErrorCode.VALIDATION, 400)
        fields = body.get("fields")
        ready_before = {i.id for i in db.get_ready()}
        try:
            annotation_warnings = db.get_annotation_closeout_warnings(issue_id)
            issue = db.close_issue(
                issue_id,
                reason=reason,
                status=status_field,
                actor=actor,
                fields=fields,
                expected_assignee=expected_assignee,
            )
        except KeyError:
            return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)
        except WrongProjectError as e:
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        except TypeError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        except ValueError as e:
            code = _classify_issue_write_error(e)
            return _error_response(str(e), code, errorcode_to_http_status(code), _issue_write_error_details(e))
        ready_after = db.get_ready()
        newly_unblocked = [i for i in ready_after if i.id not in ready_before]
        result: dict[str, Any] = dict(issue_to_loom(issue))
        if annotation_warnings:
            result["annotation_warnings"] = annotation_warnings
        if newly_unblocked:
            result["newly_unblocked"] = [slim_issue_to_loom(i) for i in newly_unblocked]
        return JSONResponse(result)

    @router.post("/issues/{issue_id}/reopen")
    async def api_loom_reopen_issue(issue_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Reopen a closed issue — returns ``IssueLoom``."""
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
        if actor_err:
            return actor_err
        try:
            issue = db.reopen_issue(issue_id, actor=actor)
        except KeyError:
            return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)
        except WrongProjectError as e:
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        except ValueError as e:
            code = classify_value_error(str(e))
            return _error_response(str(e), code, errorcode_to_http_status(code))
        return JSONResponse(issue_to_loom(issue))

    @router.post("/issues/{issue_id}/claim")
    async def api_loom_claim_issue(issue_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Claim an issue — returns ``IssueLoom``."""
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        assignee = body.get("assignee", "")
        if not isinstance(assignee, str):
            return _error_response("assignee must be a string", ErrorCode.VALIDATION, 400)
        if not assignee or not assignee.strip():
            return _error_response("assignee is required and cannot be empty", ErrorCode.VALIDATION, 400)
        actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
        if actor_err:
            return actor_err
        try:
            issue = db.claim_issue(issue_id, assignee=assignee, actor=actor)
        except KeyError:
            return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)
        except WrongProjectError as e:
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        except ClaimConflictError as e:
            return _error_response(str(e), ErrorCode.CONFLICT, 409, claim_conflict_details(e))
        except ValueError as e:
            code = classify_value_error(str(e))
            return _error_response(str(e), code, errorcode_to_http_status(code))
        return JSONResponse(issue_to_loom(issue))

    @router.post("/issues/{issue_id}/release")
    async def api_loom_release_claim(issue_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Release a claimed issue — returns ``IssueLoom``."""
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
        if actor_err:
            return actor_err
        release_options = _parse_release_claim_body(body)
        if isinstance(release_options, JSONResponse):
            return release_options
        try:
            issue = db.release_claim(issue_id, actor=actor, **release_options)
        except KeyError:
            return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)
        except WrongProjectError as e:
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        except InvalidTransitionError as e:
            return _error_response(
                str(e),
                ErrorCode.INVALID_TRANSITION,
                errorcode_to_http_status(ErrorCode.INVALID_TRANSITION),
                _invalid_transition_details(e),
            )
        except ClaimConflictError as e:
            return _error_response(str(e), ErrorCode.CONFLICT, 409, claim_conflict_details(e))
        except ValueError as e:
            code = _classify_release_claim_error(issue_id, e)
            return _error_response(str(e), code, errorcode_to_http_status(code), _issue_write_error_details(e))
        return JSONResponse(issue_to_loom(issue))

    @router.post("/claim-next")
    async def api_loom_claim_next(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Claim the highest-priority ready issue — returns ``IssueLoom``,
        or 404 ``ErrorCode.NOT_FOUND`` when nothing is ready (matching
        classic).
        """
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        assignee = body.get("assignee", "")
        if not isinstance(assignee, str):
            return _error_response("assignee must be a string", ErrorCode.VALIDATION, 400)
        if not assignee or not assignee.strip():
            return _error_response("assignee is required and cannot be empty", ErrorCode.VALIDATION, 400)
        actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
        if actor_err:
            return actor_err
        try:
            issue = db.claim_next(assignee, actor=actor)
        except ClaimConflictError as e:
            return _error_response(str(e), ErrorCode.CONFLICT, 409, claim_conflict_details(e))
        except ValueError as e:
            code = classify_value_error(str(e))
            return _error_response(str(e), code, errorcode_to_http_status(code))
        if issue is None:
            return _error_response("No ready issues to claim", ErrorCode.NOT_FOUND, 404)
        return JSONResponse(issue_to_loom(issue))

    @router.post("/issues/{issue_id}/comments", status_code=201)
    async def api_loom_add_comment(issue_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Add a comment — returns ``CommentRecordLoom`` (``comment_id``
        replaces classic's ``id``).
        """
        # See classic add-comment: prefix check first so foreign-project IDs
        # surface as 400 VALIDATION instead of 404 NOT_FOUND.
        try:
            db._check_id_prefix(issue_id)
        except WrongProjectError as e:
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        try:
            db.get_issue(issue_id)
        except KeyError:
            return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        text = body.get("text", "")
        if not isinstance(text, str):
            return _error_response("text must be a string", ErrorCode.VALIDATION, 400)
        author, author_err = _validate_actor(body.get("author", "dashboard"))
        if author_err:
            return author_err
        expected_assignee = body.get("expected_assignee")
        if expected_assignee is not None and not isinstance(expected_assignee, str):
            return _error_response("expected_assignee must be a string", ErrorCode.VALIDATION, 400)
        try:
            comment_id = db.add_comment(issue_id, text, author=author, expected_assignee=expected_assignee)
        except ValueError as e:
            code = _classify_issue_write_error(e)
            return _error_response(str(e), code, errorcode_to_http_status(code), _issue_write_error_details(e))
        row = db.conn.execute("SELECT created_at FROM comments WHERE id = ?", (comment_id,)).fetchone()
        if row is None:
            logger.error("Comment %d not found immediately after INSERT for issue %s", comment_id, issue_id)
            return _error_response("Internal error: comment created but not retrievable", ErrorCode.INTERNAL, 500)
        return JSONResponse(
            CommentRecordLoom(
                comment_id=comment_id,
                author=author,
                text=text,
                created_at=ISOTimestamp(row["created_at"]),
            ),
            status_code=201,
        )

    @router.post("/issues/{issue_id}/dependencies")
    async def api_loom_add_dependency(issue_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Add a dependency. Body: ``{depends_on: str}``. Response:
        ``{added: bool}`` (matches classic; the loom envelope adds no
        rename here because there are no entity primary keys to relabel).
        """
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        depends_on = body.get("depends_on", "")
        if not depends_on or not isinstance(depends_on, str) or not depends_on.strip():
            return _error_response("depends_on is required and must be a non-empty string", ErrorCode.VALIDATION, 400)
        actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
        if actor_err:
            return actor_err
        try:
            added = db.add_dependency(issue_id, depends_on, actor=actor)
        except KeyError as e:
            return _error_response(str(e), ErrorCode.NOT_FOUND, 404)
        except WrongProjectError as e:
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        except ValueError as e:
            return _error_response(str(e), ErrorCode.CONFLICT, 409)
        return JSONResponse({"added": added})

    @router.delete("/issues/{issue_id}/dependencies/{dep_issue_id}")
    async def api_loom_remove_dependency(
        issue_id: str,
        dep_issue_id: str,
        actor: str = "dashboard",
        db: FiligreeDB = Depends(_get_db),
    ) -> JSONResponse:
        """Remove a dependency. Path uses ``dep_issue_id`` (loom
        vocabulary); classic ``DELETE /api/issue/{id}/dependencies/{dep_id}``
        keeps its ``dep_id`` parameter name unchanged.

        Response: ``{removed: bool, issue_found: bool}``. ``removed`` is the
        classic boolean (an edge was actually deleted). ``issue_found`` is a
        loom-only addition (wire-compatible per the contract's stability
        clause): ``false`` exactly when a referenced issue does not exist, so
        a caller can distinguish a typo'd id from a genuinely-absent edge
        without parsing the server log.

        Idempotent at the wire layer per the loom contract
        (tests/fixtures/contracts/loom/issues-dep-remove.json). Two distinct
        cases both reach 200 ``{"removed": false}``, and they differ in kind:

        - **Edge absent, both issues exist:** ``remove_dependency`` returns
          ``False`` without raising → ``{removed: false, issue_found: true}``.
          This is the genuine retry-safety case — a DELETE replayed after a
          network glitch finds nothing to remove — and ``removed`` matches
          classic, which returns the same 200.
        - **Issue itself missing:** ``remove_dependency`` raises ``KeyError``
          (it ``get_issue``-s both ids first; db_planning.py:285-286) →
          ``{removed: false, issue_found: false}``. The wire layer absorbs the
          ``KeyError`` to satisfy the frozen fixture, but it is a deliberate
          tradeoff, *not* behaviour shared with classic: classic and the
          CLI/MCP surfaces convert the same ``KeyError`` to NOT_FOUND.
          Absorbing it here can mask a typo'd ``issue_id``, so the catch both
          sets ``issue_found: false`` and logs a warning — the caller still
          receives the idempotent 200, but the masked typo is now both
          machine-detectable and traceable in the server log.
        """
        clean_actor, actor_err = _validate_actor(actor)
        if actor_err:
            return actor_err
        try:
            removed = db.remove_dependency(issue_id, dep_issue_id, actor=clean_actor)
        except KeyError:
            # KeyError fires only when an issue itself is missing (the absent-edge
            # retry-safety case returns False without raising), so this branch is
            # exactly the typo-masking path: flag it via issue_found=false, log
            # it, then honour the frozen idempotent contract.
            logger.warning(
                "Loom dependency DELETE absorbed missing-issue KeyError (issue_id=%s dep_issue_id=%s); returning idempotent removed=false",
                issue_id,
                dep_issue_id,
            )
            return JSONResponse({"removed": False, "issue_found": False})
        except WrongProjectError as e:
            return _error_response(e.safe_message, ErrorCode.VALIDATION, 400)
        # No KeyError ⇒ remove_dependency resolved both issues, so they exist.
        return JSONResponse({"removed": removed, "issue_found": True})

    return router
