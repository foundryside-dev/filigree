"""Release management route handlers."""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import APIRouter

    from filigree.types.planning import ReleaseSummaryItem

from starlette.requests import Request

from filigree.core import FiligreeDB
from filigree.dashboard_routes.common import (
    _check_read_prefix_in_server_mode,
    _error_response,
    _get_bool_param,
)
from filigree.db_planning import NotAReleaseError
from filigree.types.api import ErrorCode

logger = logging.getLogger(__name__)

_SEMVER_STRICT_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_SEMVER_LOOSE_RE = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?")

# Tagged sort keys: a leading "kind" discriminator guarantees that no valid
# semver tuple can collide with a sentinel, regardless of version magnitude.
# 0 = semver, 1 = non-semver, 2 = Future — so the ordering contract
# (semver < non-semver < Future) holds without relying on numeric headroom.
_SemverSortKey = tuple[int, int, int, int]
_NON_SEMVER_KEY: _SemverSortKey = (1, 0, 0, 0)
_FUTURE_KEY: _SemverSortKey = (2, 0, 0, 0)


def _match_to_semver_key(match: re.Match[str]) -> _SemverSortKey | None:
    try:
        return (0, int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))
    except (OverflowError, ValueError):
        return None


def _semver_sort_key(release: ReleaseSummaryItem) -> _SemverSortKey:
    """Extract a tagged sort key ``(kind, major, minor, patch)`` from a release.

    Priority (each step independent — failure to parse falls through to the next):
      1. ``version`` strip-equals ``"Future"`` → FUTURE.
      2. Strict semver on ``version``.
      3. Loose semver on ``version``.
      4. ``title`` (case-insensitive, whitespace-stripped) equals ``"future"`` → FUTURE.
      5. Loose semver on ``title``.
      6. Otherwise non-semver.

    Non-empty-but-unparseable ``version`` does NOT block title fallbacks: it
    must yield to title-based Future detection (step 4) and title-based loose
    semver (step 5). Whitespace-only ``version`` is treated as absent.

    Non-string ``version``/``title`` values (possible via ``import_jsonl``,
    which stores ``fields`` verbatim) are treated as absent rather than being
    passed through to ``re.match``.
    """
    version_raw = release.get("version")
    version = version_raw.strip() if isinstance(version_raw, str) else ""
    title_raw = release.get("title", "")
    title = title_raw if isinstance(title_raw, str) else ""

    # 1. Exact "Future" on version field
    if version == "Future":
        return _FUTURE_KEY

    # 2-3. Try parsing version: strict 3-part, then loose
    if version:
        m = _SEMVER_STRICT_RE.match(version)
        if m:
            key = _match_to_semver_key(m)
            if key is not None:
                return key
        m = _SEMVER_LOOSE_RE.search(version)
        if m:
            key = _match_to_semver_key(m)
            if key is not None:
                return key

    # 4. Title-based Future detection (backward compat)
    if title.strip().lower() == "future":
        return _FUTURE_KEY

    # 5. Loose semver on title
    if title:
        m = _SEMVER_LOOSE_RE.search(title)
        if m:
            key = _match_to_semver_key(m)
            if key is not None:
                return key

    # 6. Non-semver fallback (between semver and Future)
    return _NON_SEMVER_KEY


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_classic_router() -> APIRouter:
    """Build the classic-generation APIRouter for release endpoints.

    NOTE: All handlers are intentionally async despite doing synchronous
    SQLite I/O. This serializes DB access on the event loop thread,
    avoiding concurrent multi-thread access to the shared DB connection.

    Classic routes live at their existing unprefixed paths. See ADR-002
    for the generation naming and lifecycle rules.
    """
    from fastapi import APIRouter, Depends
    from fastapi.responses import JSONResponse

    from filigree.dashboard import _get_db

    router = APIRouter()

    @router.get("/releases")
    async def api_releases(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """List releases with progress rollups."""
        include_released = _get_bool_param(request.query_params, "include_released", False)
        if not isinstance(include_released, bool):
            return include_released  # propagate the 400 error response

        try:
            releases = db.get_releases_summary(include_released=include_released)
            # Sort is a UI concern — applied here, not in the DB layer.
            # Kept inside the try block so a corrupt release row (e.g.
            # non-string version from import_jsonl) surfaces as a structured
            # error instead of an uncaught exception.
            releases.sort(key=_semver_sort_key)
        except sqlite3.Error:
            logger.exception("Database error loading releases summary")
            return _error_response("Database error loading releases", ErrorCode.IO, 500, exc_info=False)
        except Exception:
            logger.exception("BUG: Unexpected error loading releases summary")
            return _error_response("Internal error loading releases", ErrorCode.INTERNAL, 500, exc_info=False)

        return JSONResponse({"releases": releases})

    @router.get("/release/{release_id}/tree")
    async def api_release_tree(release_id: str, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Release hierarchy tree with progress rollups."""
        err = _check_read_prefix_in_server_mode(db, release_id)
        if err is not None:
            return err
        try:
            tree = db.get_release_tree(release_id)
        except KeyError:
            return _error_response(f"Release not found: {release_id}", ErrorCode.NOT_FOUND, 404)
        except NotAReleaseError as e:
            # Asking for a /release/<id>/tree on an id that exists but is not
            # a release is still a "not a release of that id" — matching the
            # 404 status with NOT_FOUND keeps the envelope internally
            # consistent (status and code agree).
            return _error_response(str(e), ErrorCode.NOT_FOUND, 404)
        except sqlite3.Error:
            logger.exception("Database error loading release tree for %s", release_id)
            return _error_response("Database error loading release tree", ErrorCode.IO, 500, exc_info=False)
        except Exception:
            # Includes bare ValueError from corrupt imported data (e.g. Issue.__post_init__)
            # — that is data corruption, not a release-type mismatch.
            logger.exception("BUG: Unexpected error loading release tree for %s", release_id)
            return _error_response("Internal error loading release tree", ErrorCode.INTERNAL, 500, exc_info=False)
        return JSONResponse(tree)

    return router


def create_weft_router() -> APIRouter:
    """Build the loom-generation APIRouter for release endpoints.

    Empty in Phase B of the 2.0 federation work package; Phase C fills
    loom release endpoints as they are implemented. See ADR-002 for the
    generation framing and docs/federation/contracts.md for the stability
    guarantee.
    """
    from fastapi import APIRouter

    return APIRouter()
