"""Dashboard API tests — graph serialization, frontend contracts, and bounded int validation."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from filigree.dashboard import STATIC_DIR
from tests.conftest import PopulatedDB


class TestGraphFrontendContracts:
    def test_issue_detail_fetches_issue_files_contract(self) -> None:
        detail_js = (STATIC_DIR / "js" / "views" / "detail.js").read_text()
        api_js = (STATIC_DIR / "js" / "api.js").read_text()
        assert "fetchIssueFiles" in api_js
        assert "fetchIssueFiles" in detail_js
        assert "Promise.all([" in detail_js

    def test_issue_detail_renders_associated_files_section(self) -> None:
        detail_js = (STATIC_DIR / "js" / "views" / "detail.js").read_text()
        assert "Associated Files" in detail_js
        assert "switchView('files');setTimeout(()=>openFileDetail(" in detail_js
        assert "issueFilesData.length" in detail_js

    def test_project_refresh_falls_back_when_selected_project_is_removed(self) -> None:
        app_js = (STATIC_DIR / "js" / "app.js").read_text()
        assert "const currentMissing =" in app_js
        assert "setProject(fallbackKey);" in app_js
        assert "setProject(fallbackKey, { keepDetail: true });" not in app_js
        assert "Selected project was removed. Switched to an available project." in app_js

    def test_graph_zoom_floor_contract(self) -> None:
        graph_js = (STATIC_DIR / "js" / "views" / "graph.js").read_text()
        assert "function computeGraphMinZoom(nodeCount)" in graph_js
        assert "function enforceReadableZoomBounds(nodeCount)" in graph_js
        assert "state.cy.minZoom(floor);" in graph_js
        assert "state.cy.maxZoom(GRAPH_MAX_ZOOM);" in graph_js
        assert "minZoom: graphMinZoom" in graph_js
        assert "minZoom: 0.1" not in graph_js
        assert "fitGraphWithCaps()" in graph_js
        assert "GRAPH_FIT_ZOOM_CAP = 1.5" in graph_js

    def test_files_finding_actions_contract(self) -> None:
        files_js = (STATIC_DIR / "js" / "views" / "files.js").read_text()
        ui_js = (STATIC_DIR / "js" / "ui.js").read_text()
        assert "Create Ticket" in files_js
        assert "Close Finding" in files_js
        assert "function closeFinding()" in files_js
        assert "dataset.findingFileId" in files_js
        assert "dataset.findingId" in files_js
        assert "patchFileFinding(" in files_js
        assert "patchFileFinding(" in ui_js

    def test_graph_overlay_hierarchy_contract(self) -> None:
        graph_js = (STATIC_DIR / "js" / "views" / "graph.js").read_text()
        assert "applyCriticalPathStyles();" in graph_js

    def test_graph_notice_uses_icon_not_color_only(self) -> None:
        graph_js = (STATIC_DIR / "js" / "views" / "graph.js").read_text()
        html = (STATIC_DIR / "dashboard.html").read_text()
        assert 'id="graphNotice"' in html
        assert 'role="status"' in html
        assert 'aria-live="polite"' in html
        assert 'icon.textContent = "\\u26A0 "' in graph_js or 'icon.textContent = "\u26a0 "' in graph_js
        assert 'icon.setAttribute("aria-hidden", "true")' in graph_js

    def test_hover_traversal_uses_outgoers_not_full_edge_scan(self) -> None:
        graph_js = (STATIC_DIR / "js" / "views" / "graph.js").read_text()
        start = graph_js.index('state.cy.on("mouseover", "node"')
        end = graph_js.index('state.cy.on("mouseout", "node"', start)
        hover_block = graph_js[start:end]
        assert 'curNode.outgoers("edge")' in hover_block
        assert "state.cy.edges().forEach" not in hover_block

    def test_topology_change_reuses_positions_only_when_all_nodes_have_positions(self) -> None:
        graph_js = (STATIC_DIR / "js" / "views" / "graph.js").read_text()
        assert "const canReusePositions =" in graph_js
        assert "cyNodes.every((n) => Object.hasOwn(previousPositions, n.data.id))" in graph_js
        assert "positions: (node) => previousPositions[node.id()]," in graph_js
        assert "previousPositions[node.id()] || { x: 0, y: 0 }" not in graph_js

    def test_graph_perf_state_is_in_bottom_diagnostics_bar(self) -> None:
        html = (STATIC_DIR / "dashboard.html").read_text()
        assert 'id="graphDiagnosticsBar"' in html
        diagnostics_idx = html.index('id="graphDiagnosticsBar"')
        perf_idx = html.index('id="graphPerfState"')
        cy_idx = html.index('id="cy"')
        assert cy_idx < diagnostics_idx
        assert diagnostics_idx < perf_idx

    def test_graph_perf_state_user_facing_text_and_tooltip_timings(self) -> None:
        graph_js = (STATIC_DIR / "js" / "views" / "graph.js").read_text()
        assert 'document.getElementById("graphNodeEdgeCount")' in graph_js
        assert "countEl.textContent = `${nodeCount} nodes, ${edgeCount} edges`;" in graph_js
        assert "el.textContent = `Query ${queryMs}ms | Render ${renderMs}ms`;" in graph_js
        assert "Perf q:" not in graph_js


class TestGraphSidebarContracts:
    def test_graph_sidebar_html_structure(self) -> None:
        html = (STATIC_DIR / "dashboard.html").read_text()
        assert 'id="graphSidebar"' in html
        assert 'id="graphSidebarList"' in html
        assert 'id="graphSidebarTypeFilter"' in html
        assert 'id="graphSidebarStatus"' in html
        assert 'role="listbox"' in html
        assert 'aria-live="polite"' in html

    def test_graph_sidebar_module_exists(self) -> None:
        sidebar_js = (STATIC_DIR / "js" / "views" / "graphSidebar.js").read_text()
        assert "export function rebuildTreeIndex()" in sidebar_js
        assert "export function renderGraphSidebar()" in sidebar_js
        assert "export function resolveGraphScope()" in sidebar_js
        assert "export function toggleGraphSidebarItem(" in sidebar_js
        assert "export function handleGhostClick(" in sidebar_js

    def test_graph_sidebar_safety_and_sanitization(self) -> None:
        sidebar_js = (STATIC_DIR / "js" / "views" / "graphSidebar.js").read_text()
        assert "escHtml" in sidebar_js, "Must import escHtml for XSS prevention"
        assert "data-sidebar-item" in sidebar_js, "Must use data attributes instead of inline onclick"
        assert "data-sidebar-type" in sidebar_js, "Must use data attributes for type filter"
        assert "addEventListener" in sidebar_js, "Must use delegated event listeners"
        assert "onclick" not in sidebar_js, "Must not use inline onclick handlers"
        assert "escHtml(issue.title)" in sidebar_js, "Issue titles must be HTML-escaped"
        assert "checkNodeCap(" in sidebar_js, "Node cap must be checked before adding selections"
        assert "confirmNodeCap(" in sidebar_js, "Node cap must trigger user confirmation"

    def test_graph_ghost_node_style_defined(self) -> None:
        graph_js = (STATIC_DIR / "js" / "views" / "graph.js").read_text()
        assert "isGhost" in graph_js
        assert "dashed" in graph_js

    def test_graph_sidebar_wired_in_app(self) -> None:
        app_js = (STATIC_DIR / "js" / "app.js").read_text()
        assert "rebuildTreeIndex" in app_js
        assert "renderGraphSidebar" in app_js
        assert "attachSidebarListeners" in app_js, "Must call attachSidebarListeners during init"
        assert "graphSidebarSelectAll" in app_js
        assert "graphSidebarClearAll" in app_js

    def test_graph_sidebar_state_model(self) -> None:
        state_js = (STATIC_DIR / "js" / "state.js").read_text()
        assert "graphSidebarSelections" in state_js
        assert "graphSidebarTypeFilter" in state_js


class TestAnalyticsContracts:
    """Contract tests for analytics.js — health score and impact score computation."""

    def test_analytics_module_exports(self) -> None:
        analytics_js = (STATIC_DIR / "js" / "analytics.js").read_text()
        assert "export function computeImpactScores()" in analytics_js
        assert "export function computeHealthScore()" in analytics_js

    def test_impact_scores_uses_bfs_traversal(self) -> None:
        analytics_js = (STATIC_DIR / "js" / "analytics.js").read_text()
        assert "const visited = new Set();" in analytics_js
        assert "const queue = [" in analytics_js
        assert "queue.shift()" in analytics_js
        assert "visited.has(" in analytics_js
        assert "state.impactScores" in analytics_js

    def test_health_score_has_four_weighted_components(self) -> None:
        analytics_js = (STATIC_DIR / "js" / "analytics.js").read_text()
        assert "blockedScore" in analytics_js
        assert "freshScore" in analytics_js
        assert "readyScore" in analytics_js
        assert "balanceScore" in analytics_js
        assert "blockedScore + freshScore + readyScore + balanceScore" in analytics_js

    def test_health_score_division_by_zero_guards(self) -> None:
        analytics_js = (STATIC_DIR / "js" / "analytics.js").read_text()
        assert "openIssues.length ?" in analytics_js
        assert "wipIssues.length ?" in analytics_js

    def test_health_score_stores_breakdown_in_state(self) -> None:
        analytics_js = (STATIC_DIR / "js" / "analytics.js").read_text()
        assert "state._healthBreakdown = {" in analytics_js
        for key in ["blocked", "freshness", "ready", "balance"]:
            assert f'"{key}"' in analytics_js or f"{key}:" in analytics_js

    def test_analytics_wired_in_app(self) -> None:
        app_js = (STATIC_DIR / "js" / "app.js").read_text()
        assert "computeImpactScores" in app_js
        assert "computeHealthScore" in app_js


class TestRouterAliasContracts:
    """Contract tests for router.js deprecated tab aliases."""

    def test_aliases_map_exists(self) -> None:
        router_js = (STATIC_DIR / "js" / "router.js").read_text()
        assert "const ALIASES = {" in router_js
        assert 'health: "files"' in router_js
        assert 'activity: "insights"' in router_js
        assert 'workflow: "kanban"' in router_js

    def test_switch_view_applies_aliases(self) -> None:
        router_js = (STATIC_DIR / "js" / "router.js").read_text()
        fn_start = router_js.index("export function switchView(")
        fn_end = router_js.index("export function switchKanbanMode(")
        switch_block = router_js[fn_start:fn_end]
        assert "ALIASES[view]" in switch_block

    def test_parse_hash_applies_aliases(self) -> None:
        router_js = (STATIC_DIR / "js" / "router.js").read_text()
        fn_start = router_js.index("export function parseHash()")
        parse_block = router_js[fn_start:]
        assert "ALIASES[view]" in parse_block


class TestGraphSidebarRenderingContracts:
    """Contract tests for sidebar-scoped graph rendering model."""

    def test_resolve_graph_scope_returns_nodes_edges_ghosts(self) -> None:
        sidebar_js = (STATIC_DIR / "js" / "views" / "graphSidebar.js").read_text()
        fn_start = sidebar_js.index("export function resolveGraphScope()")
        scope_block = sidebar_js[fn_start:]
        assert "nodes:" in scope_block
        assert "edges:" in scope_block
        assert "ghostIds" in scope_block

    def test_render_graph_uses_resolve_graph_scope(self) -> None:
        graph_js = (STATIC_DIR / "js" / "views" / "graph.js").read_text()
        assert "resolveGraphScope()" in graph_js
        assert "ghostIds" in graph_js

    def test_sidebar_type_filter_exported(self) -> None:
        sidebar_js = (STATIC_DIR / "js" / "views" / "graphSidebar.js").read_text()
        assert "export function toggleGraphSidebarType(" in sidebar_js

    def test_graph_renders_blank_state_when_no_selections(self) -> None:
        graph_js = (STATIC_DIR / "js" / "views" / "graph.js").read_text()
        assert "data-graph-blank" in graph_js
        assert "graphSidebarSelections.size === 0" in graph_js

    def test_graph_applies_status_pill_filters_to_scoped_nodes(self) -> None:
        graph_js = (STATIC_DIR / "js" / "views" / "graph.js").read_text()
        render_start = graph_js.index("export function renderGraph()")
        render_block = graph_js[render_start:]
        assert "statusPills.open" in render_block
        assert "statusPills.active" in render_block
        assert "statusPills.done" in render_block

    def test_graph_ghost_nodes_have_reduced_opacity(self) -> None:
        graph_js = (STATIC_DIR / "js" / "views" / "graph.js").read_text()
        assert "isGhost ? 0.45" in graph_js or "isGhost: isGhost" in graph_js


class TestSafeBoundedInt:
    """Bug filigree-2c3119: _safe_bounded_int must pass through _safe_int's error response."""

    def test_non_integer_returns_safe_int_error_code(self) -> None:
        """When _safe_int fails, its error response (VALIDATION_ERROR) must propagate, not be replaced."""
        import json

        from starlette.responses import JSONResponse

        from filigree.dashboard import _safe_bounded_int

        result = _safe_bounded_int("abc", name="window_days", min_value=1, max_value=365)
        assert isinstance(result, JSONResponse)

        assert isinstance(result.body, bytes)
        body = json.loads(result.body.decode())
        # Must use _safe_int's VALIDATION, not the replaced GRAPH_INVALID_PARAM
        assert body["code"] == "VALIDATION"


class TestGraphAdvancedAPI:
    async def test_graph_combined_filters(self, client: AsyncClient) -> None:
        resp = await client.get(
            "/api/graph?mode=v2&include_done=false&status_categories=open,wip&types=task&ready_only=false&blocked_only=false",
        )
        assert resp.status_code == 200
        data = resp.json()
        for node in data["nodes"]:
            assert node["status_category"] in {"open", "wip"}
            assert node["type"] == "task"

    async def test_graph_v2_and_legacy_edge_direction_consistency(
        self,
        client: AsyncClient,
        dashboard_db: PopulatedDB,
    ) -> None:
        ids = dashboard_db.ids
        legacy = (await client.get("/api/graph?mode=legacy")).json()
        v2 = (await client.get("/api/graph?mode=v2")).json()
        expected_source = ids["b"]  # blocker
        expected_target = ids["a"]  # blocked
        assert any(e["source"] == expected_source and e["target"] == expected_target for e in legacy["edges"])
        assert any(e["source"] == expected_source and e["target"] == expected_target for e in v2["edges"])

    async def test_graph_critical_path_only_subset(self, client: AsyncClient) -> None:
        full = await client.get("/api/graph?mode=v2")
        crit = await client.get("/api/graph?mode=v2&critical_path_only=true")
        assert full.status_code == 200
        assert crit.status_code == 200
        full_nodes = full.json()["nodes"]
        crit_nodes = crit.json()["nodes"]
        assert len(crit_nodes) <= len(full_nodes)
        assert all(n["id"] for n in crit_nodes)

    async def test_graph_truncation_semantics_metadata(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        for i in range(80):
            dashboard_db.db.create_issue(title=f"Graph cap issue {i}", type="task", priority=2)
        resp = await client.get("/api/graph?mode=v2&node_limit=50")
        assert resp.status_code == 200
        data = resp.json()
        assert data["limits"]["truncated"] is True
        assert data["telemetry"]["total_nodes_before_limit"] >= len(data["nodes"])
        assert data["telemetry"]["total_nodes_before_limit"] > 50

    async def test_graph_blocked_only_returns_only_currently_blocked_nodes(
        self,
        client: AsyncClient,
        dashboard_db: PopulatedDB,
    ) -> None:
        ids = dashboard_db.ids
        resp = await client.get("/api/graph?mode=v2&blocked_only=true")
        assert resp.status_code == 200
        data = resp.json()
        node_ids = {n["id"] for n in data["nodes"]}
        assert ids["a"] in node_ids
        assert all(n["blocked_by_open_count"] > 0 for n in data["nodes"])
        assert data["query"]["blocked_only"] is True

    async def test_graph_assignee_filter_behavior(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        dashboard_db.db.update_issue(ids["a"], assignee="alice")
        resp = await client.get("/api/graph?mode=v2&assignee=alice")
        assert resp.status_code == 200
        data = resp.json()
        assert data["query"]["assignee"] == "alice"
        assert len(data["nodes"]) >= 1
        assert all(n["assignee"] == "alice" for n in data["nodes"])
        assert ids["a"] in {n["id"] for n in data["nodes"]}

    async def test_graph_scope_radius_behavior(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        root = ids["a"]
        near = ids["b"]

        r0 = await client.get(f"/api/graph?mode=v2&scope_root={root}&scope_radius=0")
        r1 = await client.get(f"/api/graph?mode=v2&scope_root={root}&scope_radius=1")
        assert r0.status_code == 200
        assert r1.status_code == 200

        r0_data = r0.json()
        r1_data = r1.json()
        r0_nodes = {n["id"] for n in r0_data["nodes"]}
        r1_nodes = {n["id"] for n in r1_data["nodes"]}
        assert r0_nodes == {root}
        assert root in r1_nodes
        assert near in r1_nodes
        assert len(r1_nodes) >= len(r0_nodes)

    async def test_graph_edge_limit_trimming_behavior(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        target = dashboard_db.db.create_issue("Edge fan-in target", type="task", priority=2)
        blockers: list[str] = []
        for i in range(60):
            blocker = dashboard_db.db.create_issue(f"Edge blocker {i}", type="task", priority=2)
            blockers.append(blocker.id)
            dashboard_db.db.add_dependency(target.id, blocker.id)

        resp = await client.get("/api/graph?mode=v2&edge_limit=50")
        assert resp.status_code == 200
        data = resp.json()
        assert data["limits"]["edge_limit"] == 50
        assert data["limits"]["truncated"] is True
        assert len(data["edges"]) == 50
        assert data["telemetry"]["total_edges_before_limit"] > 50

    async def test_graph_v2_paginates_beyond_single_list_page(
        self,
        client: AsyncClient,
        dashboard_db: PopulatedDB,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: /api/graph must paginate through all issues, not cap silently.

        Previously the handler called list_issues(limit=10000); projects with
        more issues silently lost graph nodes and could false-404 on a valid
        scope_root past the cap.
        """
        from filigree.dashboard_routes import analytics as analytics_routes

        monkeypatch.setattr(analytics_routes, "_GRAPH_LIST_PAGE_SIZE", 3)

        created_ids: list[str] = []
        for i in range(7):
            new_issue = dashboard_db.db.create_issue(f"Beyond cap {i}", type="task", priority=2)
            created_ids.append(new_issue.id)

        resp = await client.get("/api/graph?mode=v2&node_limit=2000")
        assert resp.status_code == 200
        data = resp.json()
        node_ids = {n["id"] for n in data["nodes"]}
        for issue_id in created_ids:
            assert issue_id in node_ids, f"Issue {issue_id} missing — pagination failed"

    async def test_graph_v2_scope_root_accepted_when_beyond_preload_page(
        self,
        client: AsyncClient,
        dashboard_db: PopulatedDB,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """scope_root must validate against the full DB, not a preload-truncated subset."""
        from filigree.dashboard_routes import analytics as analytics_routes

        monkeypatch.setattr(analytics_routes, "_GRAPH_LIST_PAGE_SIZE", 3)

        far_ids: list[str] = []
        for i in range(6):
            far_issue = dashboard_db.db.create_issue(f"Far issue {i}", type="task", priority=2)
            far_ids.append(far_issue.id)

        # scope_root is the last-created issue, which under the old non-paginated
        # 10000-limit code was inside the preload, but the bug class the test
        # guards against is: any issue not in the preload must still validate.
        # The pagination fix ensures issue_map is complete even when the underlying
        # list_issues default page size is tiny.
        resp = await client.get(f"/api/graph?mode=v2&scope_root={far_ids[-1]}&scope_radius=0")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        node_ids = {n["id"] for n in data["nodes"]}
        assert far_ids[-1] in node_ids

    async def test_graph_window_days_filters_stale_nodes(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        stale = dashboard_db.db.create_issue("Old graph issue", type="task", priority=2)
        fresh = dashboard_db.db.create_issue("Fresh graph issue", type="task", priority=2)
        dashboard_db.db.conn.execute(
            "UPDATE issues SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", stale.id),
        )
        dashboard_db.db.conn.commit()

        resp = await client.get("/api/graph?mode=v2&types=task&window_days=7")
        assert resp.status_code == 200
        data = resp.json()
        node_ids = {n["id"] for n in data["nodes"]}
        assert stale.id not in node_ids
        assert fresh.id in node_ids
        assert data["query"]["window_days"] == 7


class TestGraphAPI:
    async def test_graph_structure(self, client: AsyncClient) -> None:
        resp = await client.get("/api/graph")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data
        assert isinstance(data["nodes"], list)
        assert isinstance(data["edges"], list)

    async def test_graph_nodes_have_required_fields(self, client: AsyncClient) -> None:
        resp = await client.get("/api/graph")
        data = resp.json()
        for node in data["nodes"]:
            assert "id" in node
            assert "title" in node
            assert "status" in node
            assert "priority" in node
            assert "type" in node

    async def test_graph_edges_from_dependencies(self, client: AsyncClient) -> None:
        resp = await client.get("/api/graph")
        data = resp.json()
        # populated_db has A depends on B, so there should be an edge
        assert len(data["edges"]) >= 1

    async def test_graph_v2_mode_shape(self, client: AsyncClient) -> None:
        resp = await client.get("/api/graph?mode=v2")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "v2"
        assert "query" in data
        assert "limits" in data
        assert "telemetry" in data
        assert isinstance(data["nodes"], list)
        assert isinstance(data["edges"], list)
        if data["nodes"]:
            node = data["nodes"][0]
            assert "blocked_by_open_count" in node
            assert "blocks_open_count" in node
            assert "is_ready" in node

    async def test_graph_invalid_mode(self, client: AsyncClient) -> None:
        resp = await client.get("/api/graph?mode=nope")
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"

    async def test_graph_invalid_ready_blocked_combo(self, client: AsyncClient) -> None:
        resp = await client.get("/api/graph?mode=v2&ready_only=true&blocked_only=true")
        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == "VALIDATION"

    async def test_graph_scope_root_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/graph?mode=v2&scope_root=missing")
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == "VALIDATION"

    async def test_graph_include_done_false_excludes_done_nodes(self, client: AsyncClient) -> None:
        resp = await client.get("/api/graph?mode=v2&include_done=false")
        assert resp.status_code == 200
        data = resp.json()
        assert all(node["status_category"] != "done" for node in data["nodes"])

    async def test_graph_mode_defaults_to_v2_when_enabled(self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FILIGREE_GRAPH_V2_ENABLED", "1")
        resp = await client.get("/api/graph")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "v2"

    async def test_graph_mode_legacy_shape_when_requested(self, client: AsyncClient) -> None:
        resp = await client.get("/api/graph?mode=legacy")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data
        assert "mode" not in data

    async def test_graph_invalid_boolean_param(self, client: AsyncClient) -> None:
        resp = await client.get("/api/graph?mode=v2&include_done=maybe")
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert body["details"]["param"] == "include_done"

    async def test_graph_invalid_status_category(self, client: AsyncClient) -> None:
        resp = await client.get("/api/graph?mode=v2&status_categories=open,wat")
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert body["details"]["param"] == "status_categories"

    async def test_graph_invalid_type_filter(self, client: AsyncClient) -> None:
        resp = await client.get("/api/graph?mode=v2&types=task,notatype")
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert body["details"]["param"] == "types"

    async def test_graph_scope_radius_requires_scope_root(self, client: AsyncClient) -> None:
        resp = await client.get("/api/graph?mode=v2&scope_radius=2")
        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert body["details"]["param"] == "scope_radius"

    async def test_graph_limit_validation(self, client: AsyncClient) -> None:
        resp = await client.get("/api/graph?mode=v2&node_limit=10")
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert body["details"]["param"] == "node_limit"

    async def test_graph_window_days_validation(self, client: AsyncClient) -> None:
        resp = await client.get("/api/graph?mode=v2&window_days=-1")
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert body["details"]["param"] == "window_days"

    async def test_graph_window_days_zero_is_noop(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        """window_days=0 should not filter any nodes — it's a no-op."""
        stale = dashboard_db.db.create_issue("Very old issue", type="task")
        dashboard_db.db.conn.execute(
            "UPDATE issues SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", stale.id),
        )
        dashboard_db.db.conn.commit()

        resp = await client.get("/api/graph?mode=v2&window_days=0")
        assert resp.status_code == 200
        data = resp.json()
        node_ids = {n["id"] for n in data["nodes"]}
        assert stale.id in node_ids
        assert data["query"]["window_days"] == 0

    async def test_graph_mode_query_override_beats_compat_mode(self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FILIGREE_GRAPH_API_MODE", "legacy")
        resp = await client.get("/api/graph?mode=v2")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "v2"

    async def test_graph_compat_mode_env_legacy_default(self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FILIGREE_GRAPH_V2_ENABLED", "1")
        monkeypatch.setenv("FILIGREE_GRAPH_API_MODE", "legacy")
        resp = await client.get("/api/graph")
        assert resp.status_code == 200
        data = resp.json()
        assert "mode" not in data

    async def test_graph_v2_node_limit_truncation(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        for i in range(60):
            dashboard_db.db.create_issue(title=f"Graph load issue {i}", type="task", priority=2)
        resp = await client.get("/api/graph?mode=v2&node_limit=50")
        assert resp.status_code == 200
        data = resp.json()
        assert data["limits"]["truncated"] is True
        assert len(data["nodes"]) == 50
        assert "query_ms" in data["telemetry"]

    async def test_graph_critical_path_only_marks_only_adjacent_edges(
        self,
        client: AsyncClient,
        dashboard_db: PopulatedDB,
    ) -> None:
        """is_critical_path must mark only adjacent edges in the ordered chain.

        Regression: the route collapsed db.get_critical_path() into a node-id
        set, so any edge whose endpoints both lay on the path was flagged
        critical — including shortcut edges that skip a path node.
        Build chain x1->x2->x3->x4 with shortcut x1->x4 and assert the
        shortcut is NOT marked critical even though both endpoints are on
        the path. (filigree-c9b08d1363)
        """
        db = dashboard_db.db
        x1 = db.create_issue("CP root", type="task", priority=2)
        x2 = db.create_issue("CP mid1", type="task", priority=2)
        x3 = db.create_issue("CP mid2", type="task", priority=2)
        x4 = db.create_issue("CP tail", type="task", priority=2)
        # Chain: x1 blocks x2 blocks x3 blocks x4 (so x4 dep x3, x3 dep x2, x2 dep x1)
        db.add_dependency(x2.id, x1.id)
        db.add_dependency(x3.id, x2.id)
        db.add_dependency(x4.id, x3.id)
        # Shortcut: x4 also depends directly on x1 (skipping x2, x3)
        db.add_dependency(x4.id, x1.id)

        resp = await client.get("/api/graph?mode=v2&critical_path_only=true")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        node_ids = {n["id"] for n in data["nodes"]}
        # Longest path is 4 nodes; shortcut alone would only give 2 — DP wins.
        assert {x1.id, x2.id, x3.id, x4.id} <= node_ids

        edges_by_pair = {(e["source"], e["target"]): e for e in data["edges"]}
        # Adjacent edges in the chain are critical
        assert edges_by_pair[(x1.id, x2.id)]["is_critical_path"] is True
        assert edges_by_pair[(x2.id, x3.id)]["is_critical_path"] is True
        assert edges_by_pair[(x3.id, x4.id)]["is_critical_path"] is True
        # The shortcut x1->x4 must NOT be marked critical
        assert edges_by_pair[(x1.id, x4.id)]["is_critical_path"] is False

    async def test_graph_v2_archived_issues_excluded_when_include_done_false(
        self,
        client: AsyncClient,
        dashboard_db: PopulatedDB,
    ) -> None:
        """include_done=false must also exclude archived issues.

        Regression: archive_closed() preserves closed_at but historically
        the 'archived' status was not in any workflow's done category,
        so archived issues' status_category resolved to 'open'. The graph
        route filtered only on status_category=='done', leaking archived
        issues. (filigree-b6cacfce72) Senior-user MCP review run e P2.10
        fixed the root cause: 'archived' is now resolved to 'done' at the
        category layer.
        """
        db = dashboard_db.db
        target = db.create_issue("To be archived", type="task", priority=2)
        db.update_issue(target.id, status="in_progress")
        db.close_issue(target.id)
        db.archive_closed(days_old=0)
        archived = db.get_issue(target.id)
        assert archived.status == "archived"
        assert archived.status_category == "done", "archived now resolves to 'done' category (P2.10)"

        resp = await client.get("/api/graph?mode=v2&include_done=false")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        node_ids = {n["id"] for n in data["nodes"]}
        assert target.id not in node_ids, "archived issue must be excluded by include_done=false"

    async def test_graph_v2_archived_issues_excluded_by_status_categories_open(
        self,
        client: AsyncClient,
        dashboard_db: PopulatedDB,
    ) -> None:
        """status_categories=open must not match archived issues.

        Senior-user MCP review run e P2.10: archived is now resolved to
        'done' at the category layer, so the open filter naturally
        excludes archived rows and the node payload reports 'done'.
        (filigree-b6cacfce72)
        """
        db = dashboard_db.db
        target = db.create_issue("Archived but open-cat", type="task", priority=2)
        db.update_issue(target.id, status="in_progress")
        db.close_issue(target.id)
        db.archive_closed(days_old=0)

        resp = await client.get("/api/graph?mode=v2&status_categories=open")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        node_ids = {n["id"] for n in data["nodes"]}
        assert target.id not in node_ids
        # Any node serialized must surface a normalized category — archived → 'done'
        for n in data["nodes"]:
            assert n["status_category"] != "done", "open filter must not yield 'done' nodes"

    async def test_graph_v2_archived_dependents_do_not_increment_blocks_open_count(
        self,
        client: AsyncClient,
        dashboard_db: PopulatedDB,
    ) -> None:
        """Archived dependents must count as done for blocks_open_count.

        Regression: _open_blocks_count compared against
        status_category != 'done', and the .blocks list (unlike
        .blocked_by) is unfiltered upstream — so an archived dependent
        was still counted as an open blocked-issue. (filigree-b6cacfce72)
        """
        db = dashboard_db.db
        blocker = db.create_issue("Blocker with archived dependent", type="task", priority=2)
        dependent = db.create_issue("Will be archived dependent", type="task", priority=2)
        db.add_dependency(dependent.id, blocker.id)
        db.update_issue(dependent.id, status="in_progress")
        db.close_issue(dependent.id)
        db.archive_closed(days_old=0)
        # Sanity: archived dependent retained in .blocks (unfiltered upstream)
        assert dependent.id in db.get_issue(blocker.id).blocks

        resp = await client.get("/api/graph?mode=v2")
        assert resp.status_code == 200
        data = resp.json()
        target_node = next(n for n in data["nodes"] if n["id"] == blocker.id)
        assert target_node["blocks_open_count"] == 0, "archived dependent must not count toward blocks_open_count"

    async def test_graph_v2_types_filter_accepts_registered_but_absent_type(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        """Filter validation must use registered template types, not the set
        of types currently held by issues.  ``release`` is a registered type
        in the default packs but the populated_db fixture seeds none — the
        request should still validate (returning an empty node list), not
        400 with "Unknown types: release".  (filigree-68c24cee62)
        """
        registered = {t.type for t in dashboard_db.db.templates.list_types()}
        observed = {i.type for i in dashboard_db.db.list_issues()}
        unused = sorted(registered - observed)
        assert unused, "test fixture changed: no registered-but-unused type to exercise"
        target = unused[0]

        resp = await client.get(f"/api/graph?mode=v2&types={target}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # No issue currently has this type, so the node list is empty.
        assert data["nodes"] == []
