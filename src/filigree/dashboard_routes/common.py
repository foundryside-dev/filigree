"""Shared helpers and constants for dashboard route modules."""

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi.responses import JSONResponse
    from starlette.requests import Request

from filigree.core import FILIGREE_DIR_NAME, FiligreeDB, read_config
from filigree.types.api import ErrorCode, ErrorResponse, parse_response_detail
from filigree.validation import sanitize_actor as _sanitize_actor

logger = logging.getLogger(__name__)
_MISSING = object()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GRAPH_MODE_VALUES = frozenset({"legacy", "v2"})
_GRAPH_STATUS_CATEGORIES = frozenset({"open", "wip", "done"})
_BOOL_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_BOOL_FALSE_VALUES = frozenset({"0", "false", "no", "off"})

# Pagination caps. Routes that overfetch by `limit + 1` must stay safely
# under SQLite's signed-int64 bind limit (2**63 - 1); _MAX_PAGINATION_LIMIT
# is also a product cap that prevents runaway response sizes. Offset stops
# one short of int64 max so any future +1 arithmetic still binds.
_MAX_PAGINATION_LIMIT = 10_000
_MAX_PAGINATION_OFFSET = 9_223_372_036_854_775_806  # 2**63 - 2

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error_response(
    error: str,
    code: ErrorCode,
    status_code: int,
    details: dict[str, Any] | None = None,
    *,
    exc_info: bool | None = None,
) -> JSONResponse:
    """Return a flat 2.0 ErrorResponse and log the error.

    Shape: ``{"error": str, "code": ErrorCode, "details"?: dict}``.
    Construction goes through the ErrorResponse TypedDict so mypy gates
    the wire shape at every call site; the cast to dict is only to
    satisfy JSONResponse's content-type annotation.
    """
    from fastapi.responses import JSONResponse

    # 5xx means a server-side problem we should be able to investigate —
    # log at error level. 4xx is client-caused (bad input, missing id,
    # conflict) — warning is enough. By default we only attach traceback
    # info for 5xx responses when an exception is actually active; callers
    # that already logged the traceback can force exc_info=False.
    log = logger.error if status_code >= 500 else logger.warning
    if exc_info is None:
        exc_info = status_code >= 500 and sys.exc_info()[0] is not None
    log("API error [%s] %s: %s", status_code, code, error, exc_info=exc_info)

    body: ErrorResponse = {"error": error, "code": code, "details": details} if details is not None else {"error": error, "code": code}
    # JSONResponse accepts any JSON-serializable mapping; StrEnum values
    # round-trip correctly because ErrorCode inherits from str.
    return JSONResponse(dict(body), status_code=status_code)


def _check_read_prefix_in_server_mode(db: FiligreeDB, issue_id: str) -> JSONResponse | None:
    """2.1.0 §1.3: in server mode, reject cross-project reads at the route boundary.

    Returns a 404 envelope using :attr:`WrongProjectError.safe_message`
    when the dashboard is running in multi-project server mode
    (``filigree.dashboard._project_store`` is set) AND the requested
    *issue_id* has a foreign prefix. Returns ``None`` in every other
    case — including ethereal (single-project) mode, where the
    documented read-tolerance of ``db.get_issue`` for foreign IDs is
    preserved so CLI / MCP / migration tooling round-tripping through
    the data layer keeps working.

    Read enforcement deliberately lives at the route layer, not in
    ``db_issues.get_issue``: the data-layer call is shared by tools
    that operate cross-prefix on purpose (jsonl import, migration,
    ``filigree doctor``). Putting the check in the route preserves
    that tolerance while closing the cross-project probe surface that
    only matters when the same process serves multiple projects.

    The error envelope uses ``NOT_FOUND``/404 rather than ``VALIDATION``/400
    so a probe ("does project alpha exist on this server?") cannot
    distinguish "no such issue here" from "wrong project" — both look
    identical to the caller.
    """
    from filigree.core import WrongProjectError

    if not bool(getattr(db, "_dashboard_server_mode", False)):
        return None
    try:
        db._check_id_prefix(issue_id)
    except WrongProjectError as exc:
        return _error_response(exc.safe_message, ErrorCode.NOT_FOUND, 404)
    return None


async def _parse_json_body(request: Request) -> dict[str, Any] | JSONResponse:
    """Parse and validate a JSON object body, returning 400 on failure."""
    import json

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return _error_response("Invalid JSON body", ErrorCode.VALIDATION, 400)
    if not isinstance(body, dict):
        return _error_response("Request body must be a JSON object", ErrorCode.VALIDATION, 400)
    return body


def _parse_pagination(
    params: Mapping[str, str],
    default_limit: int = 100,
) -> tuple[int, int] | JSONResponse:
    """Extract ``limit`` and ``offset`` from query params with validation.

    Returns ``(limit, offset)`` on success or a 400 ``JSONResponse`` on error.

    Both values are bounded so they (and ``limit + 1`` overfetch) bind safely
    into SQLite's signed-int64 LIMIT/OFFSET. See ``_MAX_PAGINATION_LIMIT`` /
    ``_MAX_PAGINATION_OFFSET``.
    """
    limit = _safe_int(
        params.get("limit", str(default_limit)),
        "limit",
        min_value=1,
        max_value=_MAX_PAGINATION_LIMIT,
    )
    if not isinstance(limit, int):
        return limit
    offset = _safe_int(
        params.get("offset", "0"),
        "offset",
        min_value=0,
        max_value=_MAX_PAGINATION_OFFSET,
    )
    if not isinstance(offset, int):
        return offset
    return limit, offset


def _parse_response_detail(
    params: Mapping[str, str],
) -> Literal["slim", "full"] | JSONResponse:
    """Parse the ``response_detail`` query parameter.

    Thin HTTP wrapper around ``filigree.types.api.parse_response_detail`` —
    converts an ``ErrorResponse`` from the shared parser into a 400
    ``JSONResponse`` for FastAPI route consumption. The shared parser is
    the single source of truth for the slim/full vocabulary; MCP and CLI
    surfaces use it directly.
    """
    parsed = parse_response_detail(params.get("response_detail"))
    if isinstance(parsed, dict):
        return _error_response(parsed["error"], ErrorCode.VALIDATION, 400)
    return parsed


def _safe_int(
    value: str,
    name: str,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int | JSONResponse:
    """Parse a query-param string to int, returning a 400 error response on failure.

    When *min_value* is set, values below that floor are rejected with 400.
    When *max_value* is set, values above that ceiling are rejected with 400.
    """
    try:
        result = int(value)
    except (ValueError, TypeError):
        return _error_response(
            f'Invalid value for {name}: "{value}". Must be an integer.',
            ErrorCode.VALIDATION,
            400,
        )
    if min_value is not None and result < min_value:
        return _error_response(
            f"Invalid value for {name}: {result}. Must be >= {min_value}.",
            ErrorCode.VALIDATION,
            400,
        )
    if max_value is not None and result > max_value:
        return _error_response(
            f"Invalid value for {name}: {result}. Must be <= {max_value}.",
            ErrorCode.VALIDATION,
            400,
        )
    return result


def _parse_bool_value(raw: str, name: str) -> bool | JSONResponse:
    value = raw.strip().lower()
    if value in _BOOL_TRUE_VALUES:
        return True
    if value in _BOOL_FALSE_VALUES:
        return False
    return _error_response(
        f'Invalid value for {name}: "{raw}". Must be one of true/false, 1/0, yes/no, on/off.',
        ErrorCode.VALIDATION,
        400,
        {"param": name, "value": raw},
    )


def _get_bool_param(params: Mapping[str, str], name: str, default: bool) -> bool | JSONResponse:
    """Extract a boolean query param, returning *default* when absent."""
    raw = params.get(name)
    if raw is None:
        return default
    return _parse_bool_value(raw, name)


def _read_graph_runtime_config(db: FiligreeDB) -> dict[str, Any]:
    """Read graph runtime settings from project config, if available.

    Note: read_config() already handles JSONDecodeError/OSError internally
    and returns defaults, so no outer try/except is needed here.
    """
    # ``read_config`` expects the ``.filigree/`` metadata directory.
    # For ``.filigree.conf`` projects the DB may be relocated (e.g.
    # ``storage/track.db``), so ``db_path.parent`` is not the metadata
    # dir — derive it from ``project_root`` when present, and only fall
    # back to ``db_path.parent`` for the legacy ``.filigree/filigree.db``
    # layout where they coincide.
    config_dir = db.project_root / FILIGREE_DIR_NAME if db.project_root is not None else db.db_path.parent
    return dict(read_config(config_dir))


def _coerce_config_bool(value: Any, name: str) -> bool:
    """Coerce a config-file value to bool using strict parsing.

    Native bool passes through. Strings are parsed via the same allowlist as
    env vars (``_parse_bool_value``) so ``"false"``/``"0"`` read as False
    instead of truthy-non-empty-string. Anything else logs and returns False.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        parsed = _parse_bool_value(value, name)
        if isinstance(parsed, bool):
            return parsed
        logger.warning("Unparseable config value %s=%r; defaulting to False", name, value)
        return False
    logger.warning(
        "Unexpected config value %s=%r (type %s); defaulting to False",
        name,
        value,
        type(value).__name__,
    )
    return False


def _coerce_config_graph_mode(value: Any) -> str:
    """Normalize a config graph_api_mode value to a canonical mode or ``''``."""
    if not isinstance(value, str):
        return ""
    mode = value.strip().lower()
    return mode if mode in _GRAPH_MODE_VALUES else ""


def _resolve_graph_runtime(db: FiligreeDB) -> dict[str, Any]:
    """Resolve graph feature controls from env + project config.

    Precedence: explicit env var (when validly parseable) wins; otherwise the
    project config value is used. Malformed env vars log and fall back to
    config — they do not silently force the feature off.
    """
    config = _read_graph_runtime_config(db)
    config_enabled = _coerce_config_bool(config.get("graph_v2_enabled"), "graph_v2_enabled")

    enabled_raw = os.getenv("FILIGREE_GRAPH_V2_ENABLED")
    if enabled_raw is not None:
        enabled_value = _parse_bool_value(enabled_raw, "FILIGREE_GRAPH_V2_ENABLED")
        if isinstance(enabled_value, bool):
            enabled = enabled_value
        else:
            logger.warning(
                "Unparseable FILIGREE_GRAPH_V2_ENABLED=%r, falling back to config value %s",
                enabled_raw,
                config_enabled,
            )
            enabled = config_enabled
    else:
        enabled = config_enabled

    config_mode = _coerce_config_graph_mode(config.get("graph_api_mode"))
    env_mode_raw = os.getenv("FILIGREE_GRAPH_API_MODE")
    if env_mode_raw is not None and env_mode_raw.strip():
        env_mode = env_mode_raw.strip().lower()
        if env_mode in _GRAPH_MODE_VALUES:
            configured_mode = env_mode
        else:
            logger.warning(
                "Invalid FILIGREE_GRAPH_API_MODE=%r, falling back to config value %r",
                env_mode_raw,
                config_mode or None,
            )
            configured_mode = config_mode
    else:
        configured_mode = config_mode

    compatibility_mode = configured_mode or ("v2" if enabled else "legacy")
    return {
        "v2_enabled": enabled,
        "configured_mode": configured_mode or None,
        "compatibility_mode": compatibility_mode,
    }


def _parse_csv_param(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _safe_bounded_int(raw: str, *, name: str, min_value: int, max_value: int) -> int | JSONResponse:
    value = _safe_int(raw, name)
    if not isinstance(value, int):
        return value  # pass through _safe_int's error response
    if value < min_value or value > max_value:
        return _error_response(
            f'Invalid value for {name}: "{raw}". Must be between {min_value} and {max_value}.',
            ErrorCode.VALIDATION,
            400,
            {"param": name, "value": raw},
        )
    return value


def _coerce_graph_mode(raw: str | None, db: FiligreeDB) -> str | JSONResponse:
    # Explicit `?mode=` wins over compatibility/feature-flag defaults, so skip
    # the runtime-config disk read on the override path. See filigree-1eaf84f2c3.
    if raw is not None:
        mode = raw.strip().lower()
        if mode not in _GRAPH_MODE_VALUES:
            return _error_response(
                f'Invalid value for mode: "{raw}". Must be one of: legacy, v2.',
                ErrorCode.VALIDATION,
                400,
                {"param": "mode", "value": raw},
            )
        return mode
    runtime = _resolve_graph_runtime(db)
    return str(runtime["compatibility_mode"])


def _validate_priority(value: Any, *, required: bool = False) -> int | None | JSONResponse:
    """Validate a priority value from JSON body.

    Returns the validated int, None (if optional and absent), or a JSONResponse error.
    """
    if value is None:
        if required:
            return _error_response("priority is required", ErrorCode.VALIDATION, 400)
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        return _error_response("priority must be an integer between 0 and 4", ErrorCode.VALIDATION, 400)
    if not (0 <= value <= 4):
        return _error_response(f"priority must be between 0 and 4, got {value}", ErrorCode.VALIDATION, 400)
    return value


def _validate_priority_field(
    body: Mapping[str, Any],
    *,
    key: str = "priority",
    default: object = _MISSING,
    required: bool = False,
) -> int | None | JSONResponse:
    """Validate a priority field while distinguishing missing from explicit null.

    Route handlers often need to preserve semantics like "omitted means leave
    unchanged" or "omitted means use default 2". Using ``dict.get()`` erases
    the difference between a missing key and an explicit JSON ``null``; this
    helper preserves that distinction so ``null`` can be rejected cleanly.
    """
    raw = body.get(key, _MISSING)
    if raw is _MISSING:
        if default is not _MISSING:
            return default if isinstance(default, int) else None
        return _validate_priority(None, required=required)
    if raw is None:
        return _error_response("priority must be an integer between 0 and 4", ErrorCode.VALIDATION, 400)
    return _validate_priority(raw, required=required)


def _validate_actor(value: Any) -> tuple[str, JSONResponse | None]:
    """Validate an actor name from JSON body.

    Returns (cleaned_actor, None) on success or ("", JSONResponse) on error.
    """
    cleaned, err = _sanitize_actor(value)
    if err:
        return ("", _error_response(err, ErrorCode.VALIDATION, 400))
    return (cleaned, None)
