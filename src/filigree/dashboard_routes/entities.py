"""Entity-association HTTP routes (ADR-029, Clarion B.7 / WP9-A).

Mirrors the four MCP tools on the HTTP surface so cross-product
callers (notably Clarion's ``issues_for`` MCP tool, which runs on the
Clarion side and reaches into Filigree via HTTP) can read and write
the binding without going through MCP.

Routes:

- ``GET    /api/issue/{issue_id}/entity-associations`` — list rows
- ``GET    /api/entity-associations?entity_id=…`` — reverse lookup
- ``POST   /api/issue/{issue_id}/entity-associations`` — attach (body)
- ``DELETE /api/issue/{issue_id}/entity-associations?entity_id=…`` — remove

The ``entity_id`` contains colons (``py:func:foo``); to keep it out of
URL path parameters it travels in request bodies (POST) and query
strings (DELETE), URL-encoded by the client.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import APIRouter

from starlette.requests import Request

from filigree.core import FiligreeDB, WrongProjectError
from filigree.dashboard_routes.common import (
    _check_read_prefix_in_server_mode,
    _error_response,
    _parse_json_body,
    _validate_actor,
)
from filigree.types.api import ErrorCode

logger = logging.getLogger(__name__)


def create_classic_router() -> APIRouter:
    """Build the APIRouter for the entity_associations endpoints.

    All handlers are async despite doing synchronous SQLite I/O so DB
    access stays serialised on the event loop thread (matching the
    rest of ``dashboard_routes``).
    """
    from fastapi import APIRouter, Depends
    from fastapi.responses import JSONResponse

    from filigree.dashboard import _get_db

    router = APIRouter()

    @router.get("/issue/{issue_id}/entity-associations")
    async def api_list_entity_associations(issue_id: str, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Return all entity_associations for *issue_id*.

        Returns raw rows; drift detection is the caller's job per
        ADR-029 §"Decision 3".
        """
        # 2.1.0 §1.3: server-mode reads are 404'd at the route boundary
        # so cross-project probes can't distinguish "wrong project" from
        # "no such issue". Ethereal mode falls through to the data-layer
        # WrongProjectError (→ 400 VALIDATION) — preserves the documented
        # error code for single-project CLI / MCP via the dashboard.
        err = _check_read_prefix_in_server_mode(db, issue_id)
        if err is not None:
            return err
        # Mirror the MCP handler: list first (prefix-enforcing →
        # WrongProjectError → 400), then probe existence only when
        # empty so a typoed or deleted issue surfaces as 404 rather
        # than an empty-result false negative. get_issue is a read
        # path and does not enforce prefix, so doing it first would
        # mask cross-project errors as 404.
        try:
            rows = db.list_entity_associations(issue_id)
        except WrongProjectError as exc:
            return _error_response(exc.safe_message, ErrorCode.VALIDATION, 400)
        if not rows:
            try:
                db.get_issue(issue_id)
            except KeyError:
                return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)
        return JSONResponse({"associations": [dict(row) for row in rows]})

    @router.get("/entity-associations")
    async def api_list_associations_by_entity(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Reverse lookup: return every issue in this project bound to *entity_id*.

        The companion to ``GET /api/issue/{issue_id}/entity-associations``;
        the entity_id lives in the query string (URL-encoded) because
        Clarion entity IDs contain colons. Project isolation is by DB
        file. Drift detection is the consumer's job per ADR-029
        §"Decision 3".
        """
        entity_id = request.query_params.get("entity_id", "")
        if not isinstance(entity_id, str) or not entity_id.strip():
            return _error_response("entity_id query parameter is required", ErrorCode.VALIDATION, 400)
        try:
            rows = db.list_associations_by_entity(entity_id)
        except WrongProjectError as exc:
            return _error_response(exc.safe_message, ErrorCode.VALIDATION, 400)
        except ValueError as exc:
            return _error_response(str(exc), ErrorCode.VALIDATION, 400)
        return JSONResponse({"associations": [dict(row) for row in rows]})

    @router.post("/issue/{issue_id}/entity-associations", status_code=201)
    async def api_add_entity_association(issue_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Attach a Clarion entity to *issue_id*. Idempotent on the composite
        key — re-attach refreshes ``content_hash_at_attach`` and ``attached_at``
        while preserving the original ``attached_by``.

        Body: ``{"entity_id": str, "content_hash": str, "actor": str?}``.
        """
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        entity_id = body.get("entity_id", "")
        content_hash = body.get("content_hash", "")
        if not isinstance(entity_id, str) or not entity_id.strip():
            return _error_response("entity_id is required", ErrorCode.VALIDATION, 400)
        if not isinstance(content_hash, str) or not content_hash.strip():
            return _error_response("content_hash is required", ErrorCode.VALIDATION, 400)
        actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
        if actor_err:
            return actor_err
        # No pre-existence check: the data layer enforces prefix
        # (WrongProjectError → 400 VALIDATION) and existence (ValueError
        # "Issue not found" → 404 NOT_FOUND) in the correct order. A
        # pre-check via get_issue() would surface foreign-prefix IDs as
        # 404, contradicting the other write routes.
        try:
            row = db.add_entity_association(issue_id, entity_id, content_hash, actor=actor)
        except WrongProjectError as exc:
            return _error_response(exc.safe_message, ErrorCode.VALIDATION, 400)
        except KeyError:
            return _error_response(f"Issue not found: {issue_id}", ErrorCode.NOT_FOUND, 404)
        except ValueError as exc:
            return _error_response(str(exc), ErrorCode.VALIDATION, 400)
        return JSONResponse(dict(row), status_code=201)

    @router.delete("/issue/{issue_id}/entity-associations")
    async def api_remove_entity_association(issue_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Remove the binding identified by ``(issue_id, entity_id)``.

        The entity_id comes through as a query parameter (URL-encoded)
        because it contains colons that would foul a path parameter.
        Idempotent — returns ``{"removed": false}`` if no row existed.
        """
        entity_id = request.query_params.get("entity_id", "")
        if not isinstance(entity_id, str) or not entity_id.strip():
            return _error_response("entity_id query parameter is required", ErrorCode.VALIDATION, 400)
        actor, actor_err = _validate_actor(request.query_params.get("actor", "dashboard"))
        if actor_err:
            return actor_err
        try:
            removed = db.remove_entity_association(issue_id, entity_id, actor=actor)
        except WrongProjectError as exc:
            return _error_response(exc.safe_message, ErrorCode.VALIDATION, 400)
        except ValueError as exc:
            return _error_response(str(exc), ErrorCode.VALIDATION, 400)
        return JSONResponse({"removed": removed})

    return router
