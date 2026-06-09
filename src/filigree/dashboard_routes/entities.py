"""Entity-association HTTP routes (ADR-029, Loomweave B.7 / WP9-A).

Mirrors the four MCP tools on the HTTP surface so cross-product
callers (notably Loomweave's ``issues_for`` MCP tool, which runs on the
Loomweave side and reaches into Filigree via HTTP) can read and write
the binding without going through MCP.

Routes:

- ``GET    /api/issue/{issue_id}/entity-associations`` — list rows
- ``GET    /api/entity-associations?entity_id=…`` — reverse lookup
- ``POST   /api/issue/{issue_id}/entity-associations`` — attach (body)
- ``DELETE /api/issue/{issue_id}/entity-associations?entity_id=…`` — remove

The ``entity_id`` contains colons (``py:func:foo``); to keep it out of
URL path parameters it travels in request bodies (POST) and query
strings (DELETE), URL-encoded by the client.

CONNECTION INVARIANT (CONTRACT-E, see dashboard_routes/files.py): the write
handlers here (``add_entity_association``, ``remove_entity_association``) do
synchronous DB work on the shared event-loop connection and MUST stay plain
``async def`` with no ``await`` mid-transaction. Do NOT move one onto a worker
thread without its own connection via ``FiligreeDB.borrow_for_worker_thread``.
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
from filigree.types.core import make_content_hash, make_issue_id, make_loomweave_entity_id

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
            rows = db.list_entity_associations(make_issue_id(issue_id))
        except WrongProjectError as exc:
            return _error_response(exc.safe_message, ErrorCode.VALIDATION, 400)
        except ValueError as exc:
            return _error_response(str(exc), ErrorCode.VALIDATION, 400)
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
        Loomweave entity IDs contain colons. Project isolation is by DB
        file. Drift detection is the consumer's job per ADR-029
        §"Decision 3".
        """
        entity_id = request.query_params.get("entity_id", "")
        current_content_hash = request.query_params.get("current_content_hash")
        if not isinstance(entity_id, str) or not entity_id.strip():
            return _error_response("entity_id query parameter is required", ErrorCode.VALIDATION, 400)
        if current_content_hash is not None and (not isinstance(current_content_hash, str) or not current_content_hash.strip()):
            return _error_response("current_content_hash must be a non-empty string when provided", ErrorCode.VALIDATION, 400)
        try:
            rows = db.list_associations_by_entity(
                make_loomweave_entity_id(entity_id),
                current_content_hash=make_content_hash(current_content_hash) if current_content_hash is not None else None,
            )
        except ValueError as exc:
            return _error_response(str(exc), ErrorCode.VALIDATION, 400)
        return JSONResponse({"associations": [dict(row) for row in rows]})

    @router.post("/issue/{issue_id}/entity-associations", status_code=201)
    async def api_add_entity_association(issue_id: str, request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Attach a Loomweave entity to *issue_id*. Idempotent on the composite
        key — re-attach refreshes ``content_hash_at_attach`` and ``attached_at``
        while preserving the original ``attached_by``.

        Body: ``{"entity_id": str, "content_hash": str, "entity_kind": str?,
        "actor": str?, "signature": str?, "signoff_seq": int?}``.

        ``signature``/``signoff_seq`` carry Legis's governed sign-off (v25/B1).
        This classic surface is transport-open (ADR-012: not enforced; transport
        is the boundary — only ``/api/weft/*`` is loom-scoped). The sign-off is
        stored **verbatim and never verified here** — Filigree holds no key; Legis
        is the sole verifier. Their semantic effect is functional, not a security
        gate: a *present* (non-null) ``signature`` flips the binding to
        ``governed``, which makes it non-removable via the delete route and makes
        a governed close fail closed when Legis is unreachable. A fabricated
        sign-off therefore grants no privilege — it only makes the binding
        stickier and closes stricter; deconfliction (cooperating callers) is the
        boundary, which a route-level check could not improve.
        """
        body = await _parse_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        entity_id = body.get("entity_id", "")
        content_hash = body.get("content_hash", "")
        entity_kind = body.get("entity_kind", body.get("external_entity_kind"))
        if not isinstance(entity_id, str) or not entity_id.strip():
            return _error_response("entity_id is required", ErrorCode.VALIDATION, 400)
        if not isinstance(content_hash, str) or not content_hash.strip():
            return _error_response("content_hash is required", ErrorCode.VALIDATION, 400)
        if entity_kind is not None and not isinstance(entity_kind, str):
            return _error_response("entity_kind must be a string", ErrorCode.VALIDATION, 400)
        # v25 (B1): opaque Legis governed-sign-off binding fields. Optional on
        # the wire — Legis omits them when no key is configured — so missing →
        # None, stored verbatim. Validate type when present; reject bool for
        # signoff_seq since bool is an int subclass.
        signature = body.get("signature")
        if signature is not None and not isinstance(signature, str):
            return _error_response("signature must be a string", ErrorCode.VALIDATION, 400)
        signoff_seq = body.get("signoff_seq")
        if signoff_seq is not None and (not isinstance(signoff_seq, int) or isinstance(signoff_seq, bool)):
            return _error_response("signoff_seq must be an integer", ErrorCode.VALIDATION, 400)
        actor, actor_err = _validate_actor(body.get("actor", "dashboard"))
        if actor_err:
            return actor_err
        # No pre-existence check: the data layer enforces prefix
        # (WrongProjectError → 400 VALIDATION) and existence (ValueError
        # "Issue not found" → 404 NOT_FOUND) in the correct order. A
        # pre-check via get_issue() would surface foreign-prefix IDs as
        # 404, contradicting the other write routes.
        try:
            row = db.add_entity_association(
                make_issue_id(issue_id),
                make_loomweave_entity_id(entity_id),
                make_content_hash(content_hash),
                actor=actor,
                entity_kind=entity_kind,
                signature=signature,
                signoff_seq=signoff_seq,
            )
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
            removed = db.remove_entity_association(
                make_issue_id(issue_id),
                make_loomweave_entity_id(entity_id),
                actor=actor,
            )
        except WrongProjectError as exc:
            return _error_response(exc.safe_message, ErrorCode.VALIDATION, 400)
        except ValueError as exc:
            return _error_response(str(exc), ErrorCode.VALIDATION, 400)
        return JSONResponse({"removed": removed})

    return router
