"""Analytics, graph, and metrics route handlers."""

from __future__ import annotations

import logging
from collections import deque
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import TYPE_CHECKING, Any, get_args

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

from starlette.requests import Request

from filigree.core import FiligreeDB
from filigree.dashboard_routes.common import (
    _GRAPH_STATUS_CATEGORIES,
    _coerce_graph_mode,
    _error_response,
    _get_bool_param,
    _parse_csv_param,
    _resolve_graph_runtime,
    _safe_bounded_int,
)
from filigree.models import Issue
from filigree.types.api import ErrorCode, StatsWithPrefix
from filigree.types.events import EventType

logger = logging.getLogger(__name__)

# Page size used when streaming every issue into the graph endpoint. Exposed
# at module scope so tests can shrink it to exercise pagination boundaries.
_GRAPH_LIST_PAGE_SIZE = 1000


def _fetch_all_issues(db: FiligreeDB) -> list[Issue]:
    """Return every issue in the DB by paginating list_issues.

    The graph endpoint previously called list_issues(limit=10000), which
    silently truncated large projects and caused scope_root false-404s for
    any issue beyond the cap. Pagination removes the hidden ceiling.
    """
    all_issues: list[Issue] = []
    offset = 0
    while True:
        page = db.list_issues(limit=_GRAPH_LIST_PAGE_SIZE, offset=offset)
        all_issues.extend(page)
        if len(page) < _GRAPH_LIST_PAGE_SIZE:
            break
        offset += _GRAPH_LIST_PAGE_SIZE
    return all_issues


# ---------------------------------------------------------------------------
# Graph v2 helpers
# ---------------------------------------------------------------------------


class _GraphV2Params:
    """Validated parameters for a graph v2 query."""

    __slots__ = (
        "assignee_filter",
        "blocked_only",
        "critical_path_only",
        "edge_limit",
        "include_done",
        "node_limit",
        "ready_only",
        "scope_radius",
        "scope_root",
        "status_filter",
        "type_filter",
        "window_cutoff",
        "window_days",
    )

    def __init__(self) -> None:
        self.include_done: bool = True
        self.blocked_only: bool = False
        self.ready_only: bool = False
        self.critical_path_only: bool = False
        self.node_limit: int = 600
        self.edge_limit: int = 2000
        self.scope_root: str | None = None
        self.scope_radius: int = 0
        self.type_filter: set[str] = set()
        self.status_filter: set[str] = set()
        self.assignee_filter: str | None = None
        self.window_days: int | None = None
        self.window_cutoff: datetime | None = None


def _parse_graph_v2_params(
    params: Mapping[str, str],
    issues: list[Issue],
    issue_map: dict[str, Issue],
    registered_types: set[str],
) -> _GraphV2Params | JSONResponse:
    """Parse and validate all graph v2 query parameters.

    Returns a ``_GraphV2Params`` on success or a ``JSONResponse`` on the
    first validation failure.
    """
    gp = _GraphV2Params()

    include_done = _get_bool_param(params, "include_done", True)
    if not isinstance(include_done, bool):
        return include_done
    gp.include_done = include_done

    blocked_only = _get_bool_param(params, "blocked_only", False)
    if not isinstance(blocked_only, bool):
        return blocked_only
    gp.blocked_only = blocked_only

    ready_only = _get_bool_param(params, "ready_only", False)
    if not isinstance(ready_only, bool):
        return ready_only
    gp.ready_only = ready_only

    critical_path_only = _get_bool_param(params, "critical_path_only", False)
    if not isinstance(critical_path_only, bool):
        return critical_path_only
    gp.critical_path_only = critical_path_only

    if gp.ready_only and gp.blocked_only:
        return _error_response(
            "ready_only and blocked_only cannot both be true.",
            ErrorCode.VALIDATION,
            422,
            {"param": "ready_only,blocked_only"},
        )

    # Limits
    node_limit_raw = params.get("node_limit")
    if node_limit_raw is not None:
        node_limit_value = _safe_bounded_int(node_limit_raw, name="node_limit", min_value=50, max_value=2000)
        if not isinstance(node_limit_value, int):
            return node_limit_value
        gp.node_limit = node_limit_value

    edge_limit_raw = params.get("edge_limit")
    if edge_limit_raw is not None:
        edge_limit_value = _safe_bounded_int(edge_limit_raw, name="edge_limit", min_value=50, max_value=5000)
        if not isinstance(edge_limit_value, int):
            return edge_limit_value
        gp.edge_limit = edge_limit_value

    # Scope
    gp.scope_root = params.get("scope_root") or None
    if gp.scope_root and gp.scope_root not in issue_map:
        return _error_response(
            f"Unknown scope_root issue id: {gp.scope_root}",
            ErrorCode.VALIDATION,
            404,
            {"param": "scope_root", "value": gp.scope_root},
        )

    gp.scope_radius = 2 if gp.scope_root else 0
    scope_radius_raw = params.get("scope_radius")
    if scope_radius_raw is not None:
        scope_radius_value = _safe_bounded_int(scope_radius_raw, name="scope_radius", min_value=0, max_value=6)
        if not isinstance(scope_radius_value, int):
            return scope_radius_value
        gp.scope_radius = scope_radius_value
        if not gp.scope_root:
            return _error_response(
                "scope_radius requires scope_root.",
                ErrorCode.VALIDATION,
                422,
                {"param": "scope_radius", "value": scope_radius_raw},
            )

    # Type filter — validate against the workflow template registry, not the
    # set of types in the current issue list.  A registered type with zero
    # current issues is still a valid filter (yields an empty node list);
    # rejecting it conflated "registered" with "currently observed".
    # (filigree-68c24cee62)
    type_filter_raw = params.get("types")
    gp.type_filter = set(_parse_csv_param(type_filter_raw)) if type_filter_raw else set()
    if gp.type_filter:
        unknown_types = sorted(gp.type_filter - registered_types)
        if unknown_types:
            return _error_response(
                f"Unknown types: {', '.join(unknown_types)}",
                ErrorCode.VALIDATION,
                400,
                {"param": "types", "value": type_filter_raw},
            )

    # Status category filter
    status_filter_raw = params.get("status_categories")
    gp.status_filter = set(_parse_csv_param(status_filter_raw)) if status_filter_raw else set()
    if gp.status_filter:
        unknown_cats = sorted(gp.status_filter - _GRAPH_STATUS_CATEGORIES)
        if unknown_cats:
            return _error_response(
                f"Unknown status_categories: {', '.join(unknown_cats)}",
                ErrorCode.VALIDATION,
                400,
                {"param": "status_categories", "value": status_filter_raw},
            )

    gp.assignee_filter = params.get("assignee")

    # Time window
    window_days_raw = params.get("window_days")
    if window_days_raw is not None:
        window_days_value = _safe_bounded_int(window_days_raw, name="window_days", min_value=0, max_value=3650)
        if not isinstance(window_days_value, int):
            return window_days_value
        gp.window_days = window_days_value
    gp.window_cutoff = datetime.now(UTC) - timedelta(days=gp.window_days) if gp.window_days and gp.window_days > 0 else None

    return gp


def _issue_updated_at_utc(issue: Issue) -> datetime | None:
    """Parse an issue's updated_at (or created_at) into a UTC datetime."""
    raw = issue.updated_at or issue.created_at
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _graph_status_category(issue: Issue) -> str:
    """Status category for graph filtering and serialization.

    archive_closed() preserves closed_at but writes status='archived',
    which is not a workflow done state — so the raw status_category
    resolves to 'open' (filigree-42045dd065). For graph queries that
    surface terminal-state filters (include_done, status_categories,
    blocker counts), an archived issue must read as 'done'.
    """
    if issue.status == "archived":
        return "done"
    return issue.status_category


def _filter_graph_nodes(
    issues: list[Issue],
    issue_map: dict[str, Issue],
    gp: _GraphV2Params,
    scoped_ids: set[str] | None,
    critical_path_ids: set[str],
) -> list[dict[str, Any]]:
    """Apply all graph v2 filters and return node dicts."""

    def _open_blocker_count(issue_id: str) -> int:
        issue = issue_map[issue_id]
        total = 0
        for blocker_id in issue.blocked_by:
            blocker = issue_map.get(blocker_id)
            if blocker and _graph_status_category(blocker) != "done":
                total += 1
        return total

    def _open_blocks_count(issue_id: str) -> int:
        issue = issue_map[issue_id]
        total = 0
        for blocked_id in issue.blocks:
            blocked_issue = issue_map.get(blocked_id)
            if blocked_issue and _graph_status_category(blocked_issue) != "done":
                total += 1
        return total

    filtered: list[dict[str, Any]] = []
    for issue in issues:
        category = _graph_status_category(issue)
        if scoped_ids is not None and issue.id not in scoped_ids:
            continue
        if not gp.include_done and category == "done":
            continue
        if gp.type_filter and issue.type not in gp.type_filter:
            continue
        if gp.status_filter and category not in gp.status_filter:
            continue
        if gp.assignee_filter is not None and issue.assignee != gp.assignee_filter:
            continue
        if gp.window_cutoff is not None:
            ts = _issue_updated_at_utc(issue)
            if ts is None or ts < gp.window_cutoff:
                continue

        blocker_count = _open_blocker_count(issue.id)
        blocks_count = _open_blocks_count(issue.id)
        if gp.blocked_only and blocker_count == 0:
            continue
        if gp.ready_only and not issue.is_ready:
            continue
        if gp.critical_path_only and issue.id not in critical_path_ids:
            continue

        filtered.append(
            {
                "id": issue.id,
                "title": issue.title,
                "status": issue.status,
                "status_category": category,
                "priority": issue.priority,
                "type": issue.type,
                "assignee": issue.assignee,
                "is_ready": issue.is_ready,
                "blocked_by_open_count": blocker_count,
                "blocks_open_count": blocks_count,
            }
        )
    return filtered


def _filter_graph_edges(
    deps: list[dict[str, Any]],
    visible_ids: set[str],
    critical_path_edges: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Build edge dicts for visible nodes.

    ``critical_path_edges`` is the set of adjacent ``(source, target)``
    pairs along the ordered critical path chain — only those edges may
    be flagged ``is_critical_path``. Marking on node-id membership alone
    incorrectly flagged shortcut edges between non-adjacent path nodes
    (filigree-c9b08d1363).
    """
    return [
        {
            "id": f"{dep['to']}->{dep['from']}",
            "source": dep["to"],
            "target": dep["from"],
            "kind": dep["type"],
            "is_critical_path": (dep["to"], dep["from"]) in critical_path_edges,
        }
        for dep in deps
        if dep["to"] in visible_ids and dep["from"] in visible_ids
    ]


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_classic_router() -> APIRouter:
    """Build the classic-generation APIRouter for analytics, graph, and
    metrics endpoints.

    NOTE: All handlers are intentionally async despite doing synchronous
    SQLite I/O. This serializes DB access on the event loop thread,
    avoiding concurrent multi-thread access to the shared DB connection.
    """
    from fastapi import APIRouter, Depends
    from fastapi.responses import JSONResponse

    from filigree.dashboard import _get_db

    router = APIRouter()

    @router.get("/config")
    async def api_config(db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Dashboard runtime config exposed to frontend consumers."""
        runtime = _resolve_graph_runtime(db)
        return JSONResponse(
            {
                "graph_v2_enabled": runtime["v2_enabled"],
                "graph_api_mode": runtime["compatibility_mode"],
                "graph_mode_configured": runtime["configured_mode"],
            }
        )

    @router.get("/graph")
    async def api_graph(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Graph data API with legacy and v2 compatibility modes."""
        mode = _coerce_graph_mode(request.query_params.get("mode"), db)
        if isinstance(mode, JSONResponse):
            return mode

        issues = _fetch_all_issues(db)
        deps = db.get_all_dependencies()

        # Legacy behavior remains the default compatibility path.
        if mode == "legacy":
            nodes = [
                {
                    "id": i.id,
                    "title": i.title,
                    "status": i.status,
                    "status_category": i.status_category,
                    "priority": i.priority,
                    "type": i.type,
                }
                for i in issues
            ]
            edges = [{"source": d["to"], "target": d["from"]} for d in deps]
            return JSONResponse({"nodes": nodes, "edges": edges})

        # Graph v2 query model
        started = perf_counter()
        issue_map = {i.id: i for i in issues}

        registered_types = {t.type for t in db.templates.list_types()}
        gp = _parse_graph_v2_params(request.query_params, issues, issue_map, registered_types)
        if isinstance(gp, JSONResponse):
            return gp

        critical_path_ids: set[str] = set()
        critical_path_edges: set[tuple[str, str]] = set()
        if gp.critical_path_only:
            ordered_path = [node["id"] for node in db.get_critical_path()]
            critical_path_ids = set(ordered_path)
            # Adjacent (source=blocker, target=blocked) pairs only — shortcut
            # edges between non-adjacent path nodes are not part of the chain
            # and must not be flagged is_critical_path (filigree-c9b08d1363).
            critical_path_edges = {(ordered_path[i], ordered_path[i + 1]) for i in range(len(ordered_path) - 1)}

        # Scope neighborhood (undirected BFS around scope_root)
        scoped_ids: set[str] | None = None
        if gp.scope_root:
            neighbors: dict[str, set[str]] = {}
            for dep in deps:
                neighbors.setdefault(dep["to"], set()).add(dep["from"])
                neighbors.setdefault(dep["from"], set()).add(dep["to"])

            scoped_ids = {gp.scope_root}
            queue: deque[tuple[str, int]] = deque([(gp.scope_root, 0)])
            while queue:
                current, dist = queue.popleft()
                if dist >= gp.scope_radius:
                    continue
                for nxt in neighbors.get(current, set()):
                    if nxt not in scoped_ids:
                        scoped_ids.add(nxt)
                        queue.append((nxt, dist + 1))

        filtered_nodes = _filter_graph_nodes(issues, issue_map, gp, scoped_ids, critical_path_ids)

        total_nodes_before_limit = len(filtered_nodes)
        truncated = False
        if len(filtered_nodes) > gp.node_limit:
            filtered_nodes = filtered_nodes[: gp.node_limit]
            truncated = True

        visible_ids = {node["id"] for node in filtered_nodes}
        filtered_edges = _filter_graph_edges([dict(d) for d in deps], visible_ids, critical_path_edges)

        total_edges_before_limit = len(filtered_edges)
        if len(filtered_edges) > gp.edge_limit:
            filtered_edges = filtered_edges[: gp.edge_limit]
            truncated = True

        query_ms = int((perf_counter() - started) * 1000)
        runtime = _resolve_graph_runtime(db)
        return JSONResponse(
            {
                "mode": "v2",
                "compatibility_mode": runtime["compatibility_mode"],
                "query": {
                    "scope_root": gp.scope_root,
                    "scope_radius": gp.scope_radius if gp.scope_root else None,
                    "include_done": gp.include_done,
                    "types": sorted(gp.type_filter) if gp.type_filter else [],
                    "status_categories": sorted(gp.status_filter) if gp.status_filter else [],
                    "assignee": gp.assignee_filter,
                    "blocked_only": gp.blocked_only,
                    "ready_only": gp.ready_only,
                    "critical_path_only": gp.critical_path_only,
                    "window_days": gp.window_days,
                },
                "limits": {
                    "node_limit": gp.node_limit,
                    "edge_limit": gp.edge_limit,
                    "truncated": truncated,
                },
                "telemetry": {
                    "query_ms": query_ms,
                    "total_nodes_before_limit": total_nodes_before_limit,
                    "total_edges_before_limit": total_edges_before_limit,
                },
                "nodes": filtered_nodes,
                "edges": filtered_edges,
            }
        )

    @router.get("/stats")
    async def api_stats(db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        result = StatsWithPrefix(**db.get_stats(), prefix=db.prefix)
        return JSONResponse(result)

    @router.get("/metrics")
    async def api_metrics(days: int = 30, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Flow metrics: cycle time, lead time, throughput."""
        from filigree.analytics import get_flow_metrics

        days = min(max(days, 1), 3650)
        metrics = get_flow_metrics(db, days=days)
        return JSONResponse(metrics)

    @router.get("/observations/stats")
    async def api_observation_stats(db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Observation count and age stats for dashboard display."""
        import sqlite3

        try:
            stats = db.observation_stats(sweep=False)
        except sqlite3.Error:
            logger.warning("observation_stats unavailable", exc_info=True)
            return _error_response("observation stats unavailable", ErrorCode.IO, 503, exc_info=False)
        return JSONResponse(stats)

    @router.get("/critical-path")
    async def api_critical_path(db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Longest dependency chain among open issues."""
        path = db.get_critical_path()
        return JSONResponse({"path": path, "length": len(path)})

    @router.get("/activity")
    async def api_activity(limit: int = 50, since: str = "", db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Recent events across all issues."""
        limit = min(max(limit, 1), 1000)
        if since:
            from datetime import UTC, datetime

            try:
                parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return _error_response(f"Invalid ISO timestamp: {since!r}", ErrorCode.VALIDATION, 400)
            # Stored timestamps are ISO with explicit UTC offset and SQLite compares as text.
            # Normalize to UTC isoformat so offset-bearing and naive inputs compare correctly.
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            since = parsed.astimezone(UTC).isoformat()
        events = db.get_events_since(since, limit=limit) if since else db.get_recent_events(limit=limit)
        return JSONResponse(events)

    @router.post("/v1/observations")
    async def api_classic_create_observation(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Create a new observation or return an existing duplicate (classic alias)."""
        return await _create_observation_handler(request, db)

    return router


def create_weft_router() -> APIRouter:
    """Build the weft-generation APIRouter for analytics, graph, and
    metrics endpoints.

    Phase C4 mounts the cross-issue ``GET /changes`` and project-wide
    ``GET /observations`` list endpoints; both are weft-only (no
    classic dashboard counterpart) and live behind ``/api/weft/`` plus
    living-surface aliases at ``/api/changes`` and ``/api/observations``
    per the C4 aliasing rule (no classic counterpart → alias).
    """
    from fastapi import APIRouter, Depends
    from fastapi.responses import JSONResponse

    from filigree.dashboard import _get_db
    from filigree.dashboard_routes.common import _MAX_PAGINATION_LIMIT, _parse_pagination
    from filigree.generations.weft.adapters import (
        change_record_to_weft,
        list_response,
        observation_to_weft,
    )

    router = APIRouter()

    @router.get("/changes")
    async def api_weft_changes(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Cross-issue events since a timestamp — ``ListResponse[ChangeRecordWeft]``.

        Weft-only (no classic dashboard counterpart). Mirrors MCP's
        ``get_changes`` semantics: pass ``?since=<ISO timestamp>`` and
        optional ``?limit=`` (default 100). Heartbeat events are excluded
        by default; pass ``?include_heartbeats=true`` or ``?type=heartbeat``
        to include them. Overfetches by 1 to detect ``has_more``. ``offset``
        is not exposed — the cursor is the ``since`` timestamp.
        """
        params = request.query_params
        since = params.get("since", "")
        if not since:
            return _error_response("since query parameter is required", ErrorCode.VALIDATION, 400)
        try:
            parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            return _error_response(
                f"Invalid ISO timestamp: {since!r}. Expected format: 2026-01-15T10:30:00",
                ErrorCode.VALIDATION,
                400,
            )
        # Stored timestamps are ISO with explicit ``+00:00`` offset; SQLite
        # compares them as text. Canonicalize ``since`` to UTC isoformat so
        # offset-bearing and naive inputs compare correctly. Mirrors the
        # classic ``/api/activity`` route. (filigree-d808d8b70f)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        since_normalized = parsed.astimezone(UTC).isoformat()
        # ``offset`` is not part of this endpoint's surface — the cursor is
        # ``since``.  Reject any offset query param rather than silently
        # discarding it. (filigree-f0f47f5b9d)
        if "offset" in params:
            return _error_response(
                "offset is not supported on /api/weft/changes; the cursor is the 'since' timestamp.",
                ErrorCode.VALIDATION,
                400,
                {"param": "offset"},
            )
        after_event_id: int | None = None
        if "after_event_id" in params:
            after_event_id_or_err = _safe_bounded_int(
                params.get("after_event_id", "0"),
                name="after_event_id",
                min_value=0,
                max_value=9223372036854775807,
            )
            if not isinstance(after_event_id_or_err, int):
                return after_event_id_or_err
            after_event_id = after_event_id_or_err
        event_type = params.get("type")
        if event_type is not None and event_type not in get_args(EventType):
            return _error_response(
                f"Invalid event type: {event_type}",
                ErrorCode.VALIDATION,
                400,
                {"param": "type", "value": event_type},
            )
        include_heartbeats = _get_bool_param(params, "include_heartbeats", False)
        if not isinstance(include_heartbeats, bool):
            return include_heartbeats
        exclude_types: list[str] = []
        if not include_heartbeats and event_type != "heartbeat":
            exclude_types.append("heartbeat")
        limit_or_err = _safe_bounded_int(
            params.get("limit", "100"),
            name="limit",
            min_value=1,
            max_value=_MAX_PAGINATION_LIMIT,
        )
        if not isinstance(limit_or_err, int):
            return limit_or_err
        limit = limit_or_err
        events = db.get_events_since(
            since_normalized,
            after_event_id=after_event_id,
            limit=limit + 1,
            event_type=event_type,
            exclude_types=exclude_types or None,
        )
        has_more = len(events) > limit
        if has_more:
            events = events[:limit]
        items = [change_record_to_weft(e) for e in events]
        body: dict[str, Any] = {"items": items, "has_more": has_more}
        body["next_since"] = items[-1]["created_at"] if items else since_normalized
        body["next_event_id"] = items[-1]["event_id"] if items else after_event_id
        return JSONResponse(body)

    @router.get("/observations")
    async def api_weft_list_observations(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """List pending observations — ``ListResponse[ObservationWeft]``.

        Weft-only (no classic dashboard counterpart). Drops MCP's
        ``stats`` sibling per the strict ``ListResponse[T]`` envelope —
        consumers needing aggregate counts hit ``/api/observations/stats``.
        ``?file_path=`` and ``?file_id=`` filters mirror the MCP tool;
        ``?limit=&offset=`` paginate.
        """
        params = request.query_params
        pagination = _parse_pagination(params)
        if isinstance(pagination, JSONResponse):
            return pagination
        limit, offset = pagination
        observations = db.list_observations(
            limit=limit + 1,
            offset=offset,
            file_path=params.get("file_path", ""),
            file_id=params.get("file_id", ""),
            sweep=False,
        )
        has_more = len(observations) > limit
        if has_more:
            observations = observations[:limit]
        items = [observation_to_weft(o) for o in observations]
        return JSONResponse(list_response(items, limit=limit, offset=offset, has_more=has_more))

    @router.post("/observations")
    async def api_weft_create_observation(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Create a new observation or return an existing duplicate (weft generation)."""
        return await _create_observation_handler(request, db)

    return router


def create_living_surface_router() -> APIRouter:
    """Build the living-surface APIRouter for analytics and observations.

    Per ``docs/federation/contracts.md``, the living surface at
    ``/api/*`` (no generation prefix) aliases the current recommended
    generation — as of 2026-04-26 that is weft. Living-surface aliases
    are added per-endpoint in Phase C wherever there is no classic
    counterpart at the same path.
    """
    from fastapi import APIRouter, Depends

    from filigree.dashboard import _get_db

    router = APIRouter()

    @router.post("/observations")
    async def api_living_create_observation(request: Request, db: FiligreeDB = Depends(_get_db)) -> JSONResponse:
        """Ingest observation — living surface (weft envelope).

        Equivalent to /api/weft/observations as of 2026-04-26.
        """
        return await _create_observation_handler(request, db)

    return router


async def _create_observation_handler(request: Request, db: FiligreeDB) -> JSONResponse:
    import sqlite3
    from datetime import UTC, datetime

    from fastapi.responses import JSONResponse

    from filigree.dashboard_routes.common import _error_response, _parse_json_body, _validate_actor
    from filigree.db_files import _is_project_relative_scan_path, _normalize_scan_path
    from filigree.generations.weft.adapters import observation_to_weft

    body = await _parse_json_body(request)
    if isinstance(body, JSONResponse):
        return body

    # 1. Parse and validate summary (required)
    summary = body.get("summary")
    if summary is None:
        return _error_response("summary is required", ErrorCode.VALIDATION, 400)
    if not isinstance(summary, str):
        return _error_response("summary must be a string", ErrorCode.VALIDATION, 400)
    summary_stripped = summary.strip()
    if not summary_stripped:
        return _error_response("Observation summary cannot be empty", ErrorCode.VALIDATION, 400)

    # 2. Parse and validate detail (optional string)
    detail = body.get("detail", "")
    if not isinstance(detail, str):
        return _error_response("detail must be a string", ErrorCode.VALIDATION, 400)

    # 3. Parse and validate file_path (optional string)
    file_path = body.get("file_path", "")
    if not isinstance(file_path, str):
        return _error_response("file_path must be a string", ErrorCode.VALIDATION, 400)

    # 4. Parse and validate line (optional integer)
    line = body.get("line")
    if line is not None:
        if not isinstance(line, int) or isinstance(line, bool):
            return _error_response("line must be an integer", ErrorCode.VALIDATION, 400)
        if line < 0:
            return _error_response("line must be >= 0", ErrorCode.VALIDATION, 400)
        if line > 9223372036854775807:  # MAX_SQLITE_INTEGER
            return _error_response("line is too large", ErrorCode.VALIDATION, 400)

    # 5. Parse and validate source_issue_id & source_finding_id
    source_issue_id = body.get("source_issue_id", "")
    if not isinstance(source_issue_id, str):
        return _error_response("source_issue_id must be a string", ErrorCode.VALIDATION, 400)
    source_finding_id = body.get("source_finding_id", "")
    if not isinstance(source_finding_id, str):
        return _error_response("source_finding_id must be a string", ErrorCode.VALIDATION, 400)

    # 6. Parse and validate priority (optional integer)
    priority_val = body.get("priority", 3)
    if not isinstance(priority_val, int) or isinstance(priority_val, bool) or not (0 <= priority_val <= 4):
        return _error_response("priority must be an integer between 0 and 4", ErrorCode.VALIDATION, 400)

    # 7. Validate actor name
    actor_val = body.get("actor", "dashboard")
    actor, actor_err = _validate_actor(actor_val)
    if actor_err:
        return actor_err

    # 8. Check project-relative file_path validation
    normalized_path = ""
    if file_path:
        try:
            normalized_path = _normalize_scan_path(file_path)
            if not _is_project_relative_scan_path(normalized_path):
                return _error_response("file_path must be project-relative", ErrorCode.VALIDATION, 400)
        except ValueError as e:
            return _error_response(str(e), ErrorCode.VALIDATION, 400)

    # 9. Perform idempotency check: check if live duplicate exists before calling create_observation
    line_cmp = line if line is not None else -1
    now = datetime.now(UTC).isoformat()

    is_duplicate = False
    try:
        existing = db.conn.execute(
            "SELECT * FROM observations WHERE summary = ? AND file_path = ? AND coalesce(line, -1) = ?",
            (summary_stripped, normalized_path, line_cmp),
        ).fetchone()
        if existing and existing["expires_at"] > now:
            is_duplicate = True
    except sqlite3.Error as e:
        return _error_response(f"Database error during duplicate check: {e}", ErrorCode.IO, 500)

    # 10. Call the underlying SQLite DB wrapper to create or replace the observation
    try:
        obs = db.create_observation(
            summary_stripped,
            detail=detail,
            file_path=file_path,
            line=line,
            source_issue_id=source_issue_id,
            source_finding_id=source_finding_id,
            priority=priority_val,
            actor=actor,
        )
    except ValueError as e:
        return _error_response(str(e), ErrorCode.VALIDATION, 400)
    except sqlite3.Error as e:
        return _error_response(f"Database error: {e}", ErrorCode.IO, 500)

    # 11. Map and return
    mapped = observation_to_weft(obs)
    status_code = 200 if is_duplicate else 201
    return JSONResponse(mapped, status_code=status_code)
