"""File tracking and scan findings route handlers."""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

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
)
from filigree.registry import REGISTRY_BACKEND_FEATURES, RegistryFileNotFoundError, RegistryResolutionError, RegistryUnavailableError
from filigree.types.api import ErrorCode
from filigree.types.core import AssocType, FindingStatus, Severity

logger = logging.getLogger(__name__)

_MAX_MIN_FINDINGS = 2_147_483_647


def _registry_resolution_error_response(exc: RegistryResolutionError) -> JSONResponse:
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
        try:
            result = db.process_scan_results(**parsed)
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
        try:
            result = db.process_scan_results(**parsed)
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
        ``scan_source``, ``scan_run_id``, ``file_id``, ``issue_id``.
        Drops MCP's ``total`` field per the unified envelope.
        """
        params = request.query_params
        pagination = _parse_pagination(params)
        if isinstance(pagination, JSONResponse):
            return pagination
        limit, offset = pagination
        filters: dict[str, Any] = {}
        for key in ("severity", "status", "scan_source", "scan_run_id", "file_id", "issue_id"):
            val = params.get(key)
            if val is not None:
                filters[key] = val
        try:
            result = db.list_findings_global(limit=limit, offset=offset, **filters)
        except ValueError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        items = [scan_finding_to_loom(f) for f in result["findings"]]
        return JSONResponse(list_response(items, limit=limit, offset=offset, total=result["total"]))

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
        try:
            result = db.process_scan_results(**parsed)
        except RegistryResolutionError as e:
            return _registry_resolution_error_response(e)
        except RegistryUnavailableError as e:
            return _error_response(str(e), ErrorCode.REGISTRY_UNAVAILABLE, 503)
        except ValueError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)
        return JSONResponse(scan_ingest_result_to_loom(result))

    return router
