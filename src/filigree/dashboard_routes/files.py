"""File tracking and scan findings route handlers."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

    from filigree.types.files import CleanStaleResult, ScanIngestResult

from starlette.requests import Request

from filigree.core import (
    VALID_ASSOC_TYPES,
    VALID_FINDING_STATUSES,
    VALID_SEVERITIES,
    FiligreeDB,
)
from filigree.dashboard_routes.common import (
    _MAX_PAGINATION_LIMIT,
    _error_response,
    _parse_json_body,
    _parse_pagination,
    _safe_int,
    _validate_actor,
)
from filigree.registry import (
    REGISTRY_BACKEND_FEATURES,
    RegistryBriefingBlockedError,
    RegistryFileNotFoundError,
    RegistryResolutionError,
    RegistryUnavailableError,
)
from filigree.types.api import ErrorCode
from filigree.types.core import AssocType, FindingStatus, Severity

logger = logging.getLogger(__name__)

_MAX_MIN_FINDINGS = 2_147_483_647

# CONTRACT-E: the worker-thread DB paths (scan-results ingest across the three
# router factories, and the findings/clean-stale sweep) run via
# asyncio.to_thread so the event loop stays responsive during the Clarion HTTP
# wait / bulk write. They do their DB work on a PRIVATE connection obtained from
# FiligreeDB.borrow_for_worker_thread() — NOT the shared event-loop connection.
# That closes the cross-thread shared-connection race for good: the connection
# invariant is connection-scoped, not route-scoped —
#
#   * Handlers that touch the shared connection stay plain ``async def`` and do
#     no ``await`` mid-transaction, so they run to completion on the event-loop
#     thread and are cooperatively serialised against each other.
#   * Any handler that goes off the event-loop thread (to_thread/executor) MUST
#     use its own connection via ``borrow_for_worker_thread`` so it never shares
#     a connection cross-thread.
#
# clean-stale conforms under this invariant despite using to_thread, because it
# borrows a private connection. Writer/writer contention — between two worker
# paths, or a worker and an event-loop handler — is mediated entirely by
# SQLite's own file locking: WAL admits a single writer at a time and
# ``busy_timeout`` (5 s) makes the loser wait for the brief write window rather
# than raise SQLITE_BUSY. No application-level lock is taken, so the worker
# paths run fully in parallel right up to that window — the bulk of each call
# (the Clarion HTTP resolution, which happens BEFORE any write transaction
# opens) overlaps freely. The shared registry's ``httpx.Client`` is safe for
# concurrent use, so two workers may resolve against Clarion simultaneously.


def _ingest_scan_results_on_private_conn(db: FiligreeDB, parsed: dict[str, Any]) -> ScanIngestResult:
    """Run ``process_scan_results`` on a private worker-thread connection.

    Handed to ``asyncio.to_thread``, so the borrowed connection is opened,
    used, committed, and closed entirely on the worker thread (CONTRACT-E /
    ``FiligreeDB.borrow_for_worker_thread``). Never touches the shared
    event-loop connection.
    """
    with db.borrow_for_worker_thread() as worker_db:
        return worker_db.process_scan_results(**parsed)


def _clean_stale_findings_on_private_conn(db: FiligreeDB, *, days: int, scan_source: str, actor: str) -> CleanStaleResult:
    """Run ``clean_stale_findings`` on a private worker-thread connection.

    Same CONTRACT-E rationale as ``_ingest_scan_results_on_private_conn``.
    """
    with db.borrow_for_worker_thread() as worker_db:
        return worker_db.clean_stale_findings(days=days, scan_source=scan_source, actor=actor)


def _promote_finding_on_private_conn(
    db: FiligreeDB,
    *,
    scan_source: str,
    fingerprint: str,
    priority: int | None,
    labels: list[str] | None,
    actor: str,
) -> dict[str, Any] | None:
    """Resolve ``(scan_source, fingerprint)`` → finding and promote it.

    Runs on a private worker-thread connection (CONTRACT-E) — the resolve and
    the promote (an issue write) share the SAME borrowed connection so it is
    never used cross-thread. Returns ``{"issue_id", "created"}`` or ``None``
    when no finding matches the fingerprint.
    """
    with db.borrow_for_worker_thread() as worker_db:
        finding = worker_db.find_finding_by_fingerprint(scan_source, fingerprint)
        if finding is None:
            return None
        result = worker_db.promote_finding_to_issue(finding["id"], priority=priority, labels=labels, actor=actor)
        return {"issue_id": result["issue"].id, "created": result["created"]}


def _parse_promote_priority(raw: Any) -> tuple[int | None, str | None]:
    """Normalize an optional ``"P2"``/``"2"``/``2`` priority to an int 0-4.

    Returns ``(priority, None)`` on success (``priority`` is ``None`` when the
    field was omitted), or ``(None, error_message)`` on a malformed value.
    """
    if raw is None:
        return None, None
    text = str(raw).strip()
    if text[:1] in ("P", "p"):
        text = text[1:]
    try:
        value = int(text)
    except ValueError:
        return None, f"priority must be P0-P4 or 0-4, got {raw!r}"
    if not 0 <= value <= 4:
        return None, f"priority must be in 0-4, got {raw!r}"
    return value, None


def _registry_resolution_error_response(exc: RegistryResolutionError) -> JSONResponse:
    if isinstance(exc, RegistryBriefingBlockedError):
        return _error_response(str(exc), ErrorCode.BRIEFING_BLOCKED, 403)
    if isinstance(exc, RegistryFileNotFoundError):
        return _error_response(str(exc), ErrorCode.NOT_FOUND, 404)
    return _error_response(str(exc), ErrorCode.VALIDATION, 400)


# ---------------------------------------------------------------------------
# Shared request parsing
# ---------------------------------------------------------------------------


def _parse_scan_results_body(body: dict[str, Any]) -> dict[str, Any] | str:
    """Validate the scan-results request body.

    Shared by the classic ``POST /api/v1/scan-results`` handler and the loom
    ``POST /api/loom/scan-results`` handler — both generations accept the
    same request shape; only the response envelope differs (per ADR-002 §6
    and the loom contract fixture). Returns the kwargs dict to splat into
    ``db.process_scan_results`` on success, or an error string on validation
    failure (caller wraps it in a 400 ``ErrorCode.VALIDATION`` response).
    """
    scan_source = body.get("scan_source", "")
    if not isinstance(scan_source, str) or not scan_source:
        return "scan_source is required and must be a string"
    if "findings" not in body:
        return "findings is required (use [] for a clean scan)"
    findings = body["findings"]
    if not isinstance(findings, list):
        return "findings must be a JSON array"
    mark_unseen = body.get("mark_unseen", False)
    if not isinstance(mark_unseen, bool):
        return "mark_unseen must be a boolean"
    create_observations = body.get("create_observations", False)
    if not isinstance(create_observations, bool):
        return "create_observations must be a boolean"
    complete_scan_run = body.get("complete_scan_run", True)
    if not isinstance(complete_scan_run, bool):
        return "complete_scan_run must be a boolean"
    scan_run_id = body.get("scan_run_id", "")
    if not isinstance(scan_run_id, str):
        return "scan_run_id must be a string"
    return {
        "scan_source": scan_source,
        "findings": findings,
        "scan_run_id": scan_run_id,
        "mark_unseen": mark_unseen,
        "create_observations": create_observations,
        "complete_scan_run": complete_scan_run,
    }


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_classic_router() -> APIRouter:
    """Build the classic-generation APIRouter for file tracking and scan
    findings endpoints.

    NOTE: All handlers are intentionally async despite doing synchronous
    SQLite I/O. This serializes DB access on the event loop thread,
    avoiding concurrent multi-thread access to the shared DB connection.

    Route order matters: ``/files/_schema`` must be registered before
    ``/files/{file_id}`` so FastAPI matches the literal path first.
    """
    from fastapi import APIRouter, Depends
    from fastapi.responses import JSONResponse

    from filigree.dashboard import _get_db

    router = APIRouter()

    @router.get("/files")
    async def api_list_files(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """List tracked file records with optional filtering and pagination."""
        params = request.query_params
        pagination = _parse_pagination(params)
        if isinstance(pagination, JSONResponse):
            return pagination
        limit, offset = pagination
        min_findings = _safe_int(params.get("min_findings", "0"), "min_findings", min_value=0, max_value=_MAX_MIN_FINDINGS)
        if isinstance(min_findings, JSONResponse):
            return min_findings
        try:
            result = db.list_files_paginated(
                limit=limit,
                offset=offset,
                language=params.get("language"),
                path_prefix=params.get("path_prefix"),
                min_findings=min_findings if min_findings > 0 else None,
                has_severity=params.get("has_severity"),
                scan_source=params.get("scan_source"),
                sort=params.get("sort", "updated_at"),
                direction=params.get("direction"),
            )
        except ValueError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        return JSONResponse(result, headers={"Cache-Control": "no-cache"})

    @router.get("/files/hotspots")
    async def api_file_hotspots(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Files ranked by weighted finding severity score."""
        params = request.query_params
        limit = _safe_int(params.get("limit", "10"), "limit", min_value=1, max_value=_MAX_PAGINATION_LIMIT)
        if isinstance(limit, JSONResponse):
            return limit
        result = db.get_file_hotspots(limit=limit)
        return JSONResponse(result)

    @router.get("/files/stats")
    async def api_file_stats(db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Global findings severity stats across all files."""
        return JSONResponse(db.get_global_findings_stats())

    @router.get("/files/_schema")
    async def api_files_schema(db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """API discovery: valid enum values and endpoint catalog for file/scan features."""
        schema = {
            "valid_severities": sorted(VALID_SEVERITIES),
            "valid_finding_statuses": sorted(VALID_FINDING_STATUSES),
            "valid_association_types": sorted(VALID_ASSOC_TYPES),
            "valid_file_sort_fields": ["first_seen", "language", "path", "updated_at"],
            "valid_finding_sort_fields": ["severity", "updated_at"],
            "config_flags": {
                "registry_backend": db.registry_backend,
                "registry_backend_features": list(REGISTRY_BACKEND_FEATURES),
                "allow_local_fallback": db.allow_local_fallback,
                "clarion_instance_id": db.clarion_instance_id,
                "clarion_api_version": db.clarion_api_version,
                "clarion_instance_rotated": db.clarion_instance_rotated,
            },
            "endpoints": [
                {
                    "method": "POST",
                    "path": "/api/v1/scan-results",
                    "description": "Ingest scan results",
                    "status": "live",
                    "request_body": {
                        "scan_source": "string (required)",
                        "findings": "array (required)",
                        "scan_run_id": "string (optional)",
                        "mark_unseen": "boolean (optional)",
                        "create_observations": "boolean (optional, default false)",
                        "complete_scan_run": "boolean (optional, default true)",
                    },
                },
                {"method": "GET", "path": "/api/files", "description": "List tracked files", "status": "live"},
                {
                    "method": "GET",
                    "path": "/api/files/{file_id}",
                    "description": "Get file details",
                    "status": "live",
                },
                {
                    "method": "GET",
                    "path": "/api/files/{file_id}/findings",
                    "description": "Findings for a specific file",
                    "status": "live",
                },
                {
                    "method": "PATCH",
                    "path": "/api/files/{file_id}/findings/{finding_id}",
                    "description": "Update finding status/linkage",
                    "status": "live",
                },
                {
                    "method": "GET",
                    "path": "/api/files/{file_id}/timeline",
                    "description": "Merged event timeline for a file",
                    "status": "live",
                },
                {
                    "method": "GET",
                    "path": "/api/files/hotspots",
                    "description": "Files ranked by weighted finding severity",
                    "status": "live",
                },
                {
                    "method": "POST",
                    "path": "/api/files/{file_id}/associations",
                    "description": "Link a file to an issue",
                    "status": "live",
                },
                {
                    "method": "GET",
                    "path": "/api/files/stats",
                    "description": "Global findings severity stats",
                    "status": "live",
                },
                {
                    "method": "GET",
                    "path": "/api/scan-runs",
                    "description": "Scan run history (grouped by scan_run_id)",
                    "status": "live",
                },
                {
                    "method": "GET",
                    "path": "/api/files/_schema",
                    "description": "API discovery (this endpoint)",
                    "status": "live",
                },
            ],
        }
        return JSONResponse(schema, headers={"Cache-Control": "max-age=3600"})

    @router.get("/files/{file_id}")
    async def api_get_file(file_id: str, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Get file record with associations, recent findings, and summary."""
        try:
            data = db.get_file_detail(file_id)
        except KeyError:
            return _error_response(f"File not found: {file_id}", ErrorCode.NOT_FOUND, 404)
        return JSONResponse(data, headers={"Cache-Control": "no-cache"})

    @router.get("/files/{file_id}/findings")
    async def api_get_file_findings(file_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Get scan findings for a file with pagination."""
        try:
            db.get_file(file_id)
        except KeyError:
            return _error_response(f"File not found: {file_id}", ErrorCode.NOT_FOUND, 404)
        params = request.query_params
        pagination = _parse_pagination(params)
        if isinstance(pagination, JSONResponse):
            return pagination
        limit, offset = pagination
        severity_raw = params.get("severity")
        if severity_raw is not None and severity_raw not in VALID_SEVERITIES:
            return _error_response(
                f"Invalid severity '{severity_raw}'. Must be one of: {', '.join(sorted(VALID_SEVERITIES))}",
                ErrorCode.VALIDATION,
                400,
            )
        status_raw = params.get("status")
        if status_raw is not None and status_raw not in VALID_FINDING_STATUSES:
            return _error_response(
                f"Invalid status '{status_raw}'. Must be one of: {', '.join(sorted(VALID_FINDING_STATUSES))}",
                ErrorCode.VALIDATION,
                400,
            )
        try:
            result = db.get_findings_paginated(
                file_id,
                severity=cast(Severity | None, severity_raw),
                status=cast(FindingStatus | None, status_raw),
                sort=params.get("sort", "updated_at"),
                limit=limit,
                offset=offset,
            )
        except ValueError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        return JSONResponse(result, headers={"Cache-Control": "max-age=30"})

    @router.patch("/files/{file_id}/findings/{finding_id}")
    async def api_update_file_finding(
        file_id: str,
        finding_id: str,
        request: Request,
        db: FiligreeDB = Depends(_get_db),
    ) -> JSONResponse:
        """Update finding status and/or linked issue."""
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        status = body.get("status")
        issue_id = body.get("issue_id")
        if status is None and issue_id is None:
            return _error_response("At least one of status or issue_id is required", ErrorCode.VALIDATION, 400)
        if status is not None and not isinstance(status, str):
            return _error_response("status must be a string", ErrorCode.VALIDATION, 400)
        if issue_id is not None and not isinstance(issue_id, str):
            return _error_response("issue_id must be a string", ErrorCode.VALIDATION, 400)
        try:
            finding = db.update_finding(
                finding_id,
                file_id=file_id,
                status=cast(FindingStatus | None, status),
                issue_id=issue_id,
            )
        except KeyError:
            return _error_response(f"Finding not found: {finding_id}", ErrorCode.NOT_FOUND, 404)
        except ValueError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        return JSONResponse(finding)

    @router.get("/files/{file_id}/timeline")
    async def api_get_file_timeline(file_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Get merged timeline of events for a file."""
        params = request.query_params
        pagination = _parse_pagination(params, default_limit=50)
        if isinstance(pagination, JSONResponse):
            return pagination
        limit, offset = pagination
        event_type = params.get("event_type")
        try:
            result = db.get_file_timeline(file_id, limit=limit, offset=offset, event_type=event_type)
        except KeyError:
            return _error_response(f"File not found: {file_id}", ErrorCode.NOT_FOUND, 404)
        except ValueError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        return JSONResponse(result)

    @router.post("/files/{file_id}/associations")
    async def api_add_file_association(file_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Link a file to an issue."""
        try:
            db.get_file(file_id)
        except KeyError:
            return _error_response(f"File not found: {file_id}", ErrorCode.NOT_FOUND, 404)
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        issue_id = body.get("issue_id", "")
        assoc_type = body.get("assoc_type", "")
        if not isinstance(issue_id, str) or not isinstance(assoc_type, str):
            return _error_response("issue_id and assoc_type must be strings", ErrorCode.VALIDATION, 400)
        if not issue_id or not assoc_type:
            return _error_response("issue_id and assoc_type are required", ErrorCode.VALIDATION, 400)
        try:
            db.add_file_association(file_id, issue_id, cast(AssocType, assoc_type))
        except ValueError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        return JSONResponse({"status": "created"}, status_code=201)

    @router.post("/v1/scan-results")
    async def api_scan_results(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Ingest scan results."""
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        parsed = _parse_scan_results_body(body)
        if isinstance(parsed, str):
            return _error_response(parsed, ErrorCode.VALIDATION, 400)
        # CONTRACT-E: process_scan_results does a blocking HTTP round-trip to
        # Clarion (one per CLARION_BATCH_MAX_QUERIES-sized chunk under
        # registry_backend='clarion'). It runs on a worker thread
        # (asyncio.to_thread) using a PRIVATE connection (see
        # _ingest_scan_results_on_private_conn) so it never shares the
        # event-loop connection cross-thread. No app-level lock: concurrent
        # workers overlap their HTTP resolution and serialise only at the WAL
        # write window via busy_timeout (see the module header).
        try:
            result = await asyncio.to_thread(_ingest_scan_results_on_private_conn, db, parsed)
        except RegistryResolutionError as e:
            return _registry_resolution_error_response(e)
        except RegistryUnavailableError as e:
            return _error_response(str(e), ErrorCode.REGISTRY_UNAVAILABLE, 503)
        except ValueError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        return JSONResponse(result)

    @router.get("/scan-runs")
    async def api_scan_runs(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Get scan run history from scan_findings grouped by scan_run_id."""
        params = request.query_params
        limit = _safe_int(params.get("limit", "10"), "limit", min_value=1, max_value=_MAX_PAGINATION_LIMIT)
        if isinstance(limit, JSONResponse):
            return limit
        try:
            runs = db.get_scan_runs(limit=limit)
        except sqlite3.Error:
            logger.exception("Failed to query scan runs")
            return _error_response("Failed to query scan runs", ErrorCode.IO, 500, exc_info=False)
        return JSONResponse({"scan_runs": runs}, headers={"Cache-Control": "no-cache"})

    return router


def create_loom_router() -> APIRouter:
    """Build the loom-generation APIRouter for file tracking and scan
    findings endpoints.

    Phase C1 mounts ``POST /api/loom/scan-results`` per the fixture at
    ``tests/fixtures/contracts/loom/scan-results.json``. Subsequent
    Phase C tasks add the rest of the loom file/findings surface.
    """
    from fastapi import APIRouter, Depends
    from fastapi.responses import JSONResponse

    from filigree.dashboard import _get_db
    from filigree.generations.loom.adapters import (
        file_record_to_loom,
        list_response,
        scan_finding_to_loom,
        scan_ingest_result_to_loom,
        scanner_config_to_loom,
    )
    from filigree.scanners import list_scanners

    router = APIRouter()

    @router.post("/scan-results")
    async def api_loom_scan_results(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Ingest scan results — loom envelope.

        Equivalent to /api/scan-results as of 2026-04-26.
        """
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        parsed = _parse_scan_results_body(body)
        if isinstance(parsed, str):
            return _error_response(parsed, ErrorCode.VALIDATION, 400)
        # CONTRACT-E: process_scan_results does a blocking HTTP round-trip to
        # Clarion (one per CLARION_BATCH_MAX_QUERIES-sized chunk under
        # registry_backend='clarion'). It runs on a worker thread
        # (asyncio.to_thread) using a PRIVATE connection (see
        # _ingest_scan_results_on_private_conn) so it never shares the
        # event-loop connection cross-thread. No app-level lock: concurrent
        # workers overlap their HTTP resolution and serialise only at the WAL
        # write window via busy_timeout (see the module header).
        try:
            result = await asyncio.to_thread(_ingest_scan_results_on_private_conn, db, parsed)
        except RegistryResolutionError as e:
            return _registry_resolution_error_response(e)
        except RegistryUnavailableError as e:
            return _error_response(str(e), ErrorCode.REGISTRY_UNAVAILABLE, 503)
        except ValueError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        return JSONResponse(scan_ingest_result_to_loom(result))

    @router.get("/files")
    async def api_loom_list_files(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """List tracked files — ``ListResponse[FileRecordLoom]``.

        Classic ``GET /api/files`` returns ``PaginatedResult`` with
        ``{results, total, limit, offset, has_more}``. Loom drops
        ``total``, ``limit``, ``offset`` from the envelope per the
        unified ``ListResponse`` contract — consumers paginate via
        ``next_offset``. Filter query params (``language``,
        ``path_prefix``, ``min_findings``, ``has_severity``,
        ``scan_source``, ``sort``, ``direction``) match classic.
        """
        params = request.query_params
        pagination = _parse_pagination(params)
        if isinstance(pagination, JSONResponse):
            return pagination
        limit, offset = pagination
        min_findings = _safe_int(params.get("min_findings", "0"), "min_findings", min_value=0, max_value=_MAX_MIN_FINDINGS)
        if isinstance(min_findings, JSONResponse):
            return min_findings
        try:
            result = db.list_files_paginated(
                limit=limit,
                offset=offset,
                language=params.get("language"),
                path_prefix=params.get("path_prefix"),
                min_findings=min_findings if min_findings > 0 else None,
                has_severity=params.get("has_severity"),
                scan_source=params.get("scan_source"),
                sort=params.get("sort", "updated_at"),
                direction=params.get("direction"),
            )
        except ValueError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        items = [file_record_to_loom(r) for r in result["results"]]
        return JSONResponse(list_response(items, limit=limit, offset=offset, total=result["total"]))

    @router.get("/findings")
    async def api_loom_list_findings(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Project-wide findings list — ``ListResponse[ScanFindingLoom]``.

        Loom-only (no classic dashboard counterpart at this path).
        Mirrors MCP ``list_findings`` filters: ``severity``, ``status``,
        ``scan_source``, ``scan_run_id``, ``file_id``, ``issue_id``, plus
        ``fingerprint`` (lets a scanner confirm a finding's issue link without
        re-promoting). Drops MCP's ``total`` field per the unified envelope.
        """
        params = request.query_params
        pagination = _parse_pagination(params)
        if isinstance(pagination, JSONResponse):
            return pagination
        limit, offset = pagination
        filters: dict[str, Any] = {}
        for key in ("severity", "status", "scan_source", "scan_run_id", "file_id", "issue_id", "fingerprint"):
            val = params.get(key)
            if val is not None:
                filters[key] = val
        try:
            result = db.list_findings_global(limit=limit, offset=offset, **filters)
        except ValueError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        items = [scan_finding_to_loom(f) for f in result["findings"]]
        return JSONResponse(list_response(items, limit=limit, offset=offset, total=result["total"]))

    @router.post("/findings/promote")
    async def api_loom_promote_finding(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Promote a finding to a tracked issue, keyed by ``(scan_source, fingerprint)``.

        This is the HTTP surface Wardline's ``file_finding`` posts to (A2): an
        agent holding only a scanner fingerprint turns one true-positive into a
        tracked issue. Idempotent — re-promoting a fingerprint whose finding
        already links an issue returns that issue with ``created=false`` (no
        duplicate). Returns 404 when the fingerprint was never ingested under
        ``scan_source``. See the 2026-06-02 promote-by-fingerprint brief.

        Request: ``{scan_source (req), fingerprint (req), priority?, labels?}``.
        ``priority`` accepts ``"P2"``/``2``; omit to derive from severity.
        Response: ``{"issue_id": "<id>", "created": true|false}``.

        Concurrency: promote is a WRITE (issue create + finding link), so it
        runs via ``asyncio.to_thread`` on a PRIVATE worker-thread connection
        (see ``_promote_finding_on_private_conn`` and CONTRACT-E in the module
        header), never touching the shared event-loop connection.
        """
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        scan_source = body.get("scan_source", "")
        if not isinstance(scan_source, str) or not scan_source.strip():
            return _error_response("scan_source is required and must be a string", ErrorCode.VALIDATION, 400)
        fingerprint = body.get("fingerprint", "")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            return _error_response("fingerprint is required and must be a string", ErrorCode.VALIDATION, 400)
        priority, priority_err = _parse_promote_priority(body.get("priority"))
        if priority_err is not None:
            return _error_response(priority_err, ErrorCode.VALIDATION, 400)
        labels = body.get("labels")
        if labels is not None and (not isinstance(labels, list) or not all(isinstance(x, str) for x in labels)):
            return _error_response("labels must be a list of strings", ErrorCode.VALIDATION, 400)
        actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
        if actor_err:
            return actor_err
        try:
            result = await asyncio.to_thread(
                _promote_finding_on_private_conn,
                db,
                scan_source=scan_source,
                fingerprint=fingerprint,
                priority=priority,
                labels=labels,
                actor=actor,
            )
        except ValueError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        except sqlite3.Error as e:
            return _error_response(f"database error promoting finding: {e}", ErrorCode.IO, 500)
        if result is None:
            return _error_response("no finding for fingerprint", ErrorCode.NOT_FOUND, 404)
        logger.info(
            "promote-by-fingerprint: issue=%s created=%s (scan_source=%r, actor=%r)",
            result["issue_id"],
            result["created"],
            scan_source,
            actor,
        )
        return JSONResponse(result)

    @router.post("/findings/clean-stale")
    async def api_loom_clean_stale_findings(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Retention sweep — soft-archive stale ``unseen_in_latest`` findings.

        Federation surface over the existing core ``clean_stale_findings``
        (the same operation as CLI ``filigree finding clean-stale``). Moves
        ``unseen_in_latest`` findings older than ``older_than_days`` (default
        30) to ``fixed`` status, scoped to a single ``scan_source``. Soft, not
        a delete: rows persist and a finding that reappears in a later scan
        auto-reopens (``fixed`` → ``open``) with its ``seen_count`` intact.
        See ADR-015 for the retention policy and the scan-run contract.

        Enrich-only (loom.md sec 3-5, ADR-002 sec 7): pure local DB write,
        fully functional with no federation peer present.

        ``scan_source`` is REQUIRED here — it is an *accident-guard*, not an
        auth boundary: the core method treats ``None`` as "all sources", which
        we refuse to expose so a caller cannot accidentally sweep every tool's
        findings. The actual trust boundary is loopback-only binding (there is
        no inbound auth on any route; see ADR-015 §1). ``older_than_days=0`` is
        permitted (sweep the whole current unseen backlog); blast radius is
        bounded because the op is soft and only touches already-unseen rows.

        No tombstone: findings are not federated through a changes feed, and
        this is a soft transition anyway (cf. the issue-deletion tombstone,
        which is for hard-deletes of federated entities).

        Concurrency: this is a bulk write (``UPDATE scan_findings SET
        status='fixed' ...``) that runs via ``asyncio.to_thread`` on a PRIVATE
        worker-thread connection (see ``_clean_stale_findings_on_private_conn``
        and ``FiligreeDB.borrow_for_worker_thread``) — the same idiom as the
        scan-results handlers. Because it never touches the shared event-loop
        connection, it cannot race the plain-async event-loop write handlers
        (e.g. PATCH findings) at the ``sqlite3.Connection`` level. It takes no
        app-level lock against the scan-results worker path; WAL admits one
        writer at a time and ``busy_timeout`` absorbs the brief overlap. See the
        module header for the connection-scoped invariant.
        """
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        scan_source = body.get("scan_source", "")
        if not isinstance(scan_source, str) or not scan_source:
            return _error_response("scan_source is required and must be a string", ErrorCode.VALIDATION, 400)
        older_than_days = body.get("older_than_days", 30)
        # JSON booleans are ints in Python; reject them explicitly so
        # {"older_than_days": true} does not silently become 1.
        if isinstance(older_than_days, bool) or not isinstance(older_than_days, int) or older_than_days < 0:
            return _error_response("older_than_days must be a non-negative integer", ErrorCode.VALIDATION, 400)
        actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
        if actor_err:
            return actor_err
        result = await asyncio.to_thread(
            _clean_stale_findings_on_private_conn,
            db,
            days=older_than_days,
            scan_source=scan_source,
            actor=actor,
        )
        logger.info(
            "clean-stale: %d findings fixed (scan_source=%r, older_than_days=%d, actor=%r)",
            result["findings_fixed"],
            scan_source,
            older_than_days,
            actor,
        )
        return JSONResponse(
            {
                "findings_fixed": result["findings_fixed"],
                "scan_source": scan_source,
                "older_than_days": older_than_days,
            }
        )

    @router.get("/scanners")
    async def api_loom_list_scanners(db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """List registered scanner configs — ``ListResponse[ScannerLoom]``.

        Loom-only (no classic dashboard counterpart). Drops MCP's
        ``errors`` and ``hint`` siblings per the strict envelope —
        scanner load errors are logged at the boundary; consumers that
        need the diagnostic UI remain on the MCP surface. Resolves
        ``scanners/`` against ``project_root / ".filigree"`` so
        ``.filigree.conf`` projects with a relocated ``db = ...`` path
        still find their scanner TOMLs (filigree-641037692a). Falls
        back to ``db.db_path.parent / "scanners"`` only when
        ``project_root`` was not set (bare ``FiligreeDB(...)``
        construction without ``from_filigree_dir`` / ``from_conf``).
        """
        scanners_dir = db.project_root / ".filigree" / "scanners" if db.project_root is not None else db.db_path.parent / "scanners"
        load_errors: list[str] = []
        scanners = list_scanners(scanners_dir, errors=load_errors)
        if load_errors:
            logger.warning("scanner load errors during /api/loom/scanners: %s", load_errors)
        items = [scanner_config_to_loom(s) for s in scanners]
        return JSONResponse(list_response(items, limit=len(items), offset=0, has_more=False))

    return router


def create_living_surface_router() -> APIRouter:
    """Build the living-surface APIRouter for file tracking and scan
    findings endpoints.

    Per ``docs/federation/contracts.md``, the living surface at
    ``/api/*`` (no generation prefix) aliases the current recommended
    generation — as of 2026-04-26 that is loom. Living-surface aliases
    are added per-endpoint in Phase C wherever there is no classic
    counterpart at the same path (so no ambiguity is created for
    pre-2.0 callers).

    Phase C1: ``POST /api/scan-results`` aliases the loom handler.
    Classic publishes ``POST /api/v1/scan-results`` (different path), so
    the alias is unambiguous.
    """
    from fastapi import APIRouter, Depends
    from fastapi.responses import JSONResponse

    from filigree.dashboard import _get_db
    from filigree.generations.loom.adapters import scan_ingest_result_to_loom

    router = APIRouter()

    @router.post("/scan-results")
    async def api_living_scan_results(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Ingest scan results — living surface (loom envelope).

        Equivalent to /api/loom/scan-results as of 2026-04-26.
        """
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        parsed = _parse_scan_results_body(body)
        if isinstance(parsed, str):
            return _error_response(parsed, ErrorCode.VALIDATION, 400)
        # CONTRACT-E: process_scan_results does a blocking HTTP round-trip to
        # Clarion (one per CLARION_BATCH_MAX_QUERIES-sized chunk under
        # registry_backend='clarion'). It runs on a worker thread
        # (asyncio.to_thread) using a PRIVATE connection (see
        # _ingest_scan_results_on_private_conn) so it never shares the
        # event-loop connection cross-thread. No app-level lock: concurrent
        # workers overlap their HTTP resolution and serialise only at the WAL
        # write window via busy_timeout (see the module header).
        try:
            result = await asyncio.to_thread(_ingest_scan_results_on_private_conn, db, parsed)
        except RegistryResolutionError as e:
            return _registry_resolution_error_response(e)
        except RegistryUnavailableError as e:
            return _error_response(str(e), ErrorCode.REGISTRY_UNAVAILABLE, 503)
        except ValueError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        return JSONResponse(scan_ingest_result_to_loom(result))

    return router
