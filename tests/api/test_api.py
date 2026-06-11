"""Dashboard API tests — core issue endpoints, workflow, batch, and dashboard behavior."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

import filigree.dashboard as dash_module
from filigree.dashboard import STATIC_DIR, create_app
from tests.conftest import PopulatedDB


class TestWeftWireSurface:
    """T1: the named generation is served under /api/weft; legacy /api/loom is gone."""

    async def test_weft_routes_mounted(self, client: AsyncClient) -> None:
        resp = await client.get("/api/weft/ready")
        assert resp.status_code != 404

    async def test_legacy_loom_routes_are_gone(self, client: AsyncClient) -> None:
        resp = await client.get("/api/loom/ready")
        assert resp.status_code == 404


class TestDashboardIndex:
    async def test_serves_html(self, client: AsyncClient) -> None:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Filigree" in resp.text

    def test_html_file_exists(self) -> None:
        assert (STATIC_DIR / "dashboard.html").exists()

    async def test_graph_sidebar_and_toolbar_present(self, client: AsyncClient) -> None:
        resp = await client.get("/")
        assert resp.status_code == 200
        html = resp.text
        assert 'class="graph-toolbar flex flex-wrap items-center' in html
        assert 'id="graphSidebar"' in html
        assert 'id="graphSidebarList"' in html
        assert 'id="graphSidebarTypeFilter"' in html
        assert 'id="graphSidebarStatus"' in html
        assert 'id="btnCritPath"' in html
        assert 'id="graphNotice"' in html
        assert 'role="status"' in html
        assert 'aria-live="polite"' in html
        assert 'id="graphDiagnosticsBar"' in html
        assert 'id="graphPerfState"' in html

    async def test_graph_default_not_epics_only(self, client: AsyncClient) -> None:
        resp = await client.get("/")
        assert resp.status_code == 200
        html = resp.text
        assert 'id="graphEpicsOnly" checked' not in html


class TestDashboardConfigAPI:
    async def test_config_defaults(self, client: AsyncClient) -> None:
        resp = await client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["graph_v2_enabled"] is False
        assert data["graph_api_mode"] == "legacy"
        assert "graph_mode_configured" in data

    async def test_config_reads_env_overrides(self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FILIGREE_GRAPH_V2_ENABLED", "1")
        monkeypatch.setenv("FILIGREE_GRAPH_API_MODE", "v2")
        resp = await client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["graph_v2_enabled"] is True
        assert data["graph_api_mode"] == "v2"


class TestStatsAPI:
    async def test_stats_structure(self, client: AsyncClient) -> None:
        resp = await client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "by_status" in data
        assert "by_category" in data
        assert "by_type" in data
        assert "ready_count" in data
        assert "blocked_count" in data
        assert "total_dependencies" in data
        assert "prefix" in data
        # status_name_counts / status_category_counts removed in 3.0.0
        # (filigree-e4181ae767) — public /api/stats no longer emits the aliases.
        assert "status_name_counts" not in data
        assert "status_category_counts" not in data

    async def test_stats_prefix(self, client: AsyncClient) -> None:
        resp = await client.get("/api/stats")
        data = resp.json()
        assert data["prefix"] == "test"

    async def test_stats_counts(self, client: AsyncClient) -> None:
        resp = await client.get("/api/stats")
        data = resp.json()
        assert data["by_status"]["closed"] == 1
        assert data["total_dependencies"] >= 1


class TestIssuesAPI:
    async def test_list_all_issues(self, client: AsyncClient) -> None:
        resp = await client.get("/api/issues")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 5  # epic + A + B + C + auto-seeded Future release

    async def test_list_all_issues_paginates_beyond_single_page(
        self,
        client: AsyncClient,
        dashboard_db: PopulatedDB,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: /api/issues must paginate instead of truncating silently."""
        from filigree.dashboard_routes import issues as issue_routes

        monkeypatch.setattr(issue_routes, "_ISSUES_LIST_PAGE_SIZE", 3)

        created_ids: list[str] = []
        for i in range(7):
            new_issue = dashboard_db.db.create_issue(f"Beyond preload page {i}", type="task", priority=2)
            created_ids.append(new_issue.id)

        resp = await client.get("/api/issues")
        assert resp.status_code == 200
        data = resp.json()
        issue_ids = {issue["id"] for issue in data}

        for issue_id in created_ids:
            assert issue_id in issue_ids, f"Issue {issue_id} missing — /api/issues pagination failed"

    async def test_issue_structure(self, client: AsyncClient) -> None:
        resp = await client.get("/api/issues")
        data = resp.json()
        issue = data[0]
        # Check expected fields
        for field in ["id", "title", "status", "priority", "type", "blocks", "blocked_by", "is_ready"]:
            assert field in issue

    async def test_update_issue_force_overwrite_corrupt_fields(
        self,
        client: AsyncClient,
        dashboard_db: PopulatedDB,
    ) -> None:
        issue = dashboard_db.db.create_issue("HTTP corrupt fields")
        dashboard_db.db.conn.execute("UPDATE issues SET fields = ? WHERE id = ?", (b"\xff\xfe", issue.id))
        dashboard_db.db.conn.commit()

        resp = await client.patch(
            f"/api/issue/{issue.id}",
            json={"fields": {"restored": True}, "force_overwrite_corrupt": True},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["fields"] == {"restored": True}

    async def test_weft_update_issue_force_overwrite_corrupt_fields(
        self,
        client: AsyncClient,
        dashboard_db: PopulatedDB,
    ) -> None:
        issue = dashboard_db.db.create_issue("Weft corrupt fields")
        dashboard_db.db.conn.execute("UPDATE issues SET fields = ? WHERE id = ?", (b"\xff\xfe", issue.id))
        dashboard_db.db.conn.commit()

        resp = await client.patch(
            f"/api/weft/issues/{issue.id}",
            json={"fields": {"restored": True}, "force_overwrite_corrupt": True},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["fields"] == {"restored": True}


class TestIssueDetailAPI:
    async def test_issue_detail(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.get(f"/api/issue/{ids['a']}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == ids["a"]
        assert data["title"] == "Issue A"

    async def test_issue_detail_includes_deps(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.get(f"/api/issue/{ids['a']}")
        data = resp.json()
        assert "dep_details" in data
        assert "events" in data
        assert "comments" in data

    async def test_issue_detail_blocked_by_details(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.get(f"/api/issue/{ids['a']}")
        data = resp.json()
        # A is blocked by B
        assert ids["b"] in data["blocked_by"]
        assert ids["b"] in data["dep_details"]
        dep = data["dep_details"][ids["b"]]
        assert dep["title"] == "Issue B"
        assert dep["status"] == "open"
        assert "status_category" in dep

    async def test_issue_detail_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/issue/nonexistent")
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == "NOT_FOUND"
        assert "nonexistent" in body["error"]

    async def test_issue_with_comments(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.get(f"/api/issue/{ids['b']}")
        data = resp.json()
        assert len(data["comments"]) == 1
        assert data["comments"][0]["text"] == "Test comment"
        assert data["comments"][0]["author"] == "tester"


class TestDependenciesAPI:
    async def test_dependencies_list(self, client: AsyncClient) -> None:
        resp = await client.get("/api/dependencies")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        dep = data[0]
        assert "from" in dep
        assert "to" in dep
        assert "type" in dep


class TestTypeTemplateAPI:
    """WFT-FR-065: /api/type/{type_name} endpoint."""

    async def test_type_template_endpoint(self, client: AsyncClient) -> None:
        resp = await client.get("/api/type/bug")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "bug"
        assert data["display_name"] == "Bug Report"
        assert len(data["states"]) >= 4
        assert len(data["transitions"]) >= 4
        assert data["initial_state"] == "triage"
        # Each state has name + category
        for state in data["states"]:
            assert "name" in state
            assert "category" in state

    async def test_type_template_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/type/nonexistent")
        # Unknown type_name is rejected-enum-value input validation, so
        # VALIDATION + 400 — same pattern as other unknown-enum-value
        # responses elsewhere in the dashboard API.
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert "nonexistent" in body["error"]
        assert "task" in body["error"]
        assert "bug" in body["error"]
        # Structured details carry the invalid value and the valid set.
        details = body.get("details", {})
        assert details.get("param") == "type_name"
        assert details.get("value") == "nonexistent"
        assert "bug" in details.get("valid_types", [])


class TestWorkflowAwareAPI:
    """Phase 4: API responses include category-level data."""

    async def test_stats_includes_by_category(self, client: AsyncClient) -> None:
        resp = await client.get("/api/stats")
        data = resp.json()
        assert "by_category" in data
        by_cat = data["by_category"]
        assert "open" in by_cat
        assert "wip" in by_cat
        assert "done" in by_cat

    async def test_graph_nodes_include_status_category(self, client: AsyncClient) -> None:
        resp = await client.get("/api/graph")
        data = resp.json()
        for node in data["nodes"]:
            assert "status_category" in node

    async def test_issues_include_status_category(self, client: AsyncClient) -> None:
        resp = await client.get("/api/issues")
        data = resp.json()
        for issue in data:
            assert "status_category" in issue


class TestTransitionsAPI:
    """GET /api/issue/{issue_id}/transitions — valid next states."""

    async def test_transitions_for_open_issue(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.get(f"/api/issue/{ids['b']}/transitions")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        # Each transition has required fields
        for t in data:
            assert "to" in t
            assert "category" in t
            assert "ready" in t

    async def test_transitions_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/issue/nonexistent/transitions")
        assert resp.status_code == 404
        assert "error" in resp.json()


class TestCreateIssueAPI:
    """POST /api/issues — create a new issue."""

    async def test_create_basic_issue(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/issues",
            json={"title": "New test issue"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "New test issue"
        assert "id" in data
        assert data["type"] == "task"
        assert data["priority"] == 2

    async def test_create_with_all_fields(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/issues",
            json={
                "title": "Full issue",
                "type": "bug",
                "priority": 0,
                "description": "A bug report",
                "notes": "Some notes",
                "assignee": "alice",
                "labels": ["critical", "ui"],
                "actor": "api-user",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "Full issue"
        assert data["type"] == "bug"
        assert data["priority"] == 0
        assert data["description"] == "A bug report"
        assert data["assignee"] == "alice"

    async def test_create_rejects_priority_like_labels(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/issues",
            json={"title": "Bad labels", "labels": ["P1"]},
        )

        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert "priority field" in body["error"]

    async def test_weft_create_rejects_priority_like_labels(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/weft/issues",
            json={"title": "Bad labels", "labels": ["priority:1"]},
        )

        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert "priority field" in body["error"]

    async def test_create_preserves_custom_fields(self, release_client: AsyncClient, release_dashboard_db: Any) -> None:
        resp = await release_client.post(
            "/api/issues",
            json={
                "title": "Release candidate",
                "type": "release",
                "fields": {"version": "v1.2.3"},
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["fields"] == {"version": "v1.2.3"}
        stored = release_dashboard_db.get_issue(data["id"])
        assert stored.fields == {"version": "v1.2.3"}

    async def test_create_empty_title_returns_400(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/issues",
            json={"title": ""},
        )
        assert resp.status_code == 400
        assert "error" in resp.json()

    async def test_create_invalid_fields_type_returns_400(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/issues",
            json={"title": "Bad fields", "fields": []},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert "fields must be a dict" in body["error"]

    async def test_create_invalid_type_returns_400(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/issues",
            json={"title": "Bad type", "type": "nonexistent_type"},
        )
        assert resp.status_code == 400
        body = resp.json()
        # Error from core.py includes valid types
        assert "nonexistent_type" in body["error"]
        assert "task" in body["error"]

    async def test_create_with_parent(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            "/api/issues",
            json={"title": "Child issue", "parent_id": ids["epic"]},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["parent_id"] == ids["epic"]

    async def test_create_with_deps(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            "/api/issues",
            json={"title": "Dep issue", "deps": [ids["b"]]},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert ids["b"] in data["blocked_by"]


class TestUpdateAPI:
    """PATCH /api/issue/{issue_id} — update issue fields."""

    async def test_update_priority(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.patch(
            f"/api/issue/{ids['b']}",
            json={"priority": 0},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["priority"] == 0

    async def test_update_assignee(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.patch(
            f"/api/issue/{ids['b']}",
            json={"assignee": "alice"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["assignee"] == "alice"

    async def test_update_status(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.patch(
            f"/api/issue/{ids['b']}",
            json={"status": "in_progress"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "in_progress"

    async def test_weft_update_surfaces_soft_transition_warning(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        issue = dashboard_db.db.create_issue("Warn me", type="bug")

        resp = await client.patch(f"/api/weft/issues/{issue.id}", json={"status": "confirmed"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "confirmed"
        assert data["data_warnings"] == ["Missing recommended fields for 'confirmed': severity"]

    async def test_update_title(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.patch(
            f"/api/issue/{ids['b']}",
            json={"title": "Renamed Issue B"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Renamed Issue B"

    async def test_update_not_found(self, client: AsyncClient) -> None:
        resp = await client.patch(
            "/api/issue/nonexistent",
            json={"priority": 1},
        )
        assert resp.status_code == 404
        assert "error" in resp.json()

    async def test_update_invalid_fields_type_returns_400(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.patch(
            f"/api/issue/{ids['a']}",
            json={"fields": []},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert "fields must be a dict" in body["error"]

    async def test_update_invalid_transition(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        # Trying to transition to an invalid state should 409
        resp = await client.patch(
            f"/api/issue/{ids['b']}",
            json={"status": "totally_bogus_state"},
        )
        assert resp.status_code == 409
        assert "error" in resp.json()

    async def test_update_actor_defaults_to_dashboard(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.patch(
            f"/api/issue/{ids['b']}",
            json={"priority": 3},
        )
        assert resp.status_code == 200
        # Verify actor was recorded (check events)
        detail_resp = await client.get(f"/api/issue/{ids['b']}")
        events = detail_resp.json()["events"]
        # Most recent event should have actor "dashboard"
        assert any(e.get("actor") == "dashboard" for e in events)

    async def test_update_custom_actor(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.patch(
            f"/api/issue/{ids['b']}",
            json={"priority": 1, "actor": "bot-1"},
        )
        assert resp.status_code == 200
        detail_resp = await client.get(f"/api/issue/{ids['b']}")
        events = detail_resp.json()["events"]
        assert any(e.get("actor") == "bot-1" for e in events)


class TestCloseReopenAPI:
    """POST /api/issue/{issue_id}/close and /reopen."""

    async def test_close_issue(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            f"/api/issue/{ids['b']}/close",
            json={"reason": "completed"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status_category"] == "done"

    async def test_classic_close_includes_annotation_warnings(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        source = dashboard_db.db.db_path.parent / "api-close-classic.py"
        source.write_text("x = 1\n")
        issue = dashboard_db.db.create_issue("Classic annotated close")
        annotation = dashboard_db.db.annotate_file(
            source.name,
            "Must be considered before close.",
            critical=True,
            links=[{"target_type": "issue", "target_id": issue.id, "relationship": "must_consider"}],
        )

        resp = await client.post(f"/api/issue/{issue.id}/close", json={"reason": "completed"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["annotation_warnings"][0]["annotation_id"] == annotation["annotation_id"]
        assert data["annotation_warnings"][0]["relationship"] == "must_consider"

    async def test_weft_close_includes_annotation_warnings(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        source = dashboard_db.db.db_path.parent / "api-close-weft.py"
        source.write_text("x = 1\n")
        issue = dashboard_db.db.create_issue("Weft annotated close")
        annotation = dashboard_db.db.annotate_file(
            source.name,
            "Must be considered before close.",
            critical=True,
            links=[{"target_type": "issue", "target_id": issue.id, "relationship": "must_consider"}],
        )

        resp = await client.post(f"/api/weft/issues/{issue.id}/close", json={"reason": "completed"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["annotation_warnings"][0]["annotation_id"] == annotation["annotation_id"]
        assert data["annotation_warnings"][0]["relationship"] == "must_consider"

    async def test_close_already_closed(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        # C is already closed
        resp = await client.post(
            f"/api/issue/{ids['c']}/close",
            json={},
        )
        assert resp.status_code == 409
        assert "error" in resp.json()

    async def test_close_invalid_transition_includes_valid_transitions(
        self,
        client: AsyncClient,
        dashboard_db: PopulatedDB,
    ) -> None:
        bug = dashboard_db.db.create_issue("API invalid close", type="bug")

        resp = await client.post(
            f"/api/issue/{bug.id}/close",
            json={"reason": "cleanup"},
        )

        assert resp.status_code == 409
        data = resp.json()
        assert data["code"] == "INVALID_TRANSITION"
        assert {t["to"] for t in data["details"]["valid_transitions"]} == {"confirmed", "wont_fix", "not_a_bug"}

    async def test_close_not_found(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/issue/nonexistent/close",
            json={},
        )
        assert resp.status_code == 404
        assert "error" in resp.json()

    async def test_reopen_closed_issue(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        # C is closed — reopen it
        resp = await client.post(
            f"/api/issue/{ids['c']}/reopen",
            json={},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status_category"] == "open"

    async def test_weft_reopen_bug_returns_to_last_non_done_status(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        issue = dashboard_db.db.create_issue("Weft bug reopen", type="bug", fields={"severity": "major"})
        dashboard_db.db.update_issue(issue.id, status="confirmed")
        dashboard_db.db.update_issue(issue.id, status="fixing", fields={"root_cause": "bad assumption"})
        dashboard_db.db.update_issue(issue.id, status="verifying", fields={"fix_verification": "regression added"})
        dashboard_db.db.close_issue(issue.id, reason="closed too early")

        resp = await client.post(f"/api/weft/issues/{issue.id}/reopen", json={"actor": "api-test"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "verifying"
        assert data["closed_at"] is None
        assert data["fields"]["root_cause"] == "bad assumption"
        assert data["fields"]["fix_verification"] == "regression added"
        assert "close_reason" not in data["fields"]

    async def test_reopen_not_closed(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        # B is open — can't reopen
        resp = await client.post(
            f"/api/issue/{ids['b']}/reopen",
            json={},
        )
        assert resp.status_code == 409
        assert "error" in resp.json()

    async def test_reopen_not_found(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/issue/nonexistent/reopen",
            json={},
        )
        assert resp.status_code == 404
        assert "error" in resp.json()

    async def test_close_with_actor(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            f"/api/issue/{ids['b']}/close",
            json={"actor": "bot-2"},
        )
        assert resp.status_code == 200

    async def test_reopen_with_actor(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        # C is closed, reopen with actor
        resp = await client.post(
            f"/api/issue/{ids['c']}/reopen",
            json={"actor": "bot-3"},
        )
        assert resp.status_code == 200


class TestClaimAPI:
    async def test_claim_issue(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            f"/api/issue/{ids['a']}/claim",
            json={"assignee": "agent-1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["assignee"] == "agent-1"

    async def test_release_claim(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        dashboard_db.db.claim_issue(ids["a"], assignee="agent-1")
        resp = await client.post(
            f"/api/issue/{ids['a']}/release",
            json={},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["assignee"] == ""

    async def test_release_if_held_unassigned_classic(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            f"/api/issue/{ids['a']}/release",
            json={"actor": "agent-1", "if_held": True},
        )
        assert resp.status_code == 200
        assert resp.json()["assignee"] == ""

    async def test_release_if_held_expected_assignee_weft(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        dashboard_db.db.claim_issue(ids["a"], assignee="agent-1")

        resp = await client.post(
            f"/api/weft/issues/{ids['a']}/release",
            json={"actor": "coordinator", "if_held": True, "expected_assignee": "agent-1"},
        )

        assert resp.status_code == 200
        assert resp.json()["assignee"] == ""

    async def test_release_if_held_rejects_other_assignee_classic(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        dashboard_db.db.claim_issue(ids["a"], assignee="agent-2")

        resp = await client.post(
            f"/api/issue/{ids['a']}/release",
            json={"actor": "agent-1", "if_held": True},
        )

        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == "CONFLICT"
        assert body["details"] == {"issue_id": ids["a"], "observed": "agent-2", "expected": "agent-1"}
        assert dashboard_db.db.get_issue(ids["a"]).assignee == "agent-2"

    async def test_release_claim_invalid_reverse_transition_returns_invalid_transition(
        self,
        client: AsyncClient,
        dashboard_db: PopulatedDB,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from filigree.types.api import InvalidTransitionError, TransitionMode

        ids = dashboard_db.ids
        dashboard_db.db.start_work(ids["a"], assignee="agent-1", actor="agent-1")
        original_validate_transition = dashboard_db.db.templates.validate_transition

        def fail_backward_validation(
            type_name: str,
            from_state: str,
            to_state: str,
            fields: dict[str, Any],
            *,
            mode: TransitionMode = TransitionMode.FORWARD,
        ) -> Any:
            if mode is TransitionMode.BACKWARD:
                raise InvalidTransitionError(type_name, from_state, to_state=to_state, mode=TransitionMode.BACKWARD)
            return original_validate_transition(type_name, from_state, to_state, fields, mode=mode)

        monkeypatch.setattr(dashboard_db.db.templates, "validate_transition", fail_backward_validation)

        resp = await client.post(f"/api/issue/{ids['a']}/release", json={"actor": "agent-1"})

        assert resp.status_code == 409
        assert resp.json()["code"] == "INVALID_TRANSITION"

    async def test_claim_next(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        resp = await client.post(
            "/api/claim-next",
            json={"assignee": "agent-2"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["assignee"] == "agent-2"

    async def test_claim_not_found(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/issue/nonexistent/claim",
            json={"assignee": "x"},
        )
        assert resp.status_code == 404


class TestClaimEmptyAssigneeAPI:
    """Bug filigree-040ddb: dashboard claim endpoints must reject empty assignee."""

    @pytest.mark.parametrize("body", [{"assignee": ""}, {}], ids=["empty", "missing"])
    async def test_claim_issue_rejects_bad_assignee(self, client: AsyncClient, dashboard_db: PopulatedDB, body: dict[str, str]) -> None:
        ids = dashboard_db.ids
        resp = await client.post(f"/api/issue/{ids['a']}/claim", json=body)
        assert resp.status_code == 400
        assert "assignee" in resp.json()["error"].lower()

    @pytest.mark.parametrize("body", [{"assignee": ""}, {}], ids=["empty", "missing"])
    async def test_claim_next_rejects_bad_assignee(self, client: AsyncClient, body: dict[str, str]) -> None:
        resp = await client.post("/api/claim-next", json=body)
        assert resp.status_code == 400
        assert "assignee" in resp.json()["error"].lower()


class TestCommentAPI:
    """POST /api/issue/{issue_id}/comments — add a comment."""

    @pytest.mark.parametrize(
        ("path_template", "id_field"),
        [
            ("/api/issue/{issue_id}/comments", "id"),
            ("/api/weft/issues/{issue_id}/comments", "comment_id"),
        ],
    )
    async def test_add_comment_expected_assignee_allows_coordinator(
        self,
        client: AsyncClient,
        dashboard_db: PopulatedDB,
        path_template: str,
        id_field: str,
    ) -> None:
        ids = dashboard_db.ids
        dashboard_db.db.claim_issue(ids["a"], assignee="alice")

        resp = await client.post(
            path_template.format(issue_id=ids["a"]),
            json={"text": "coordinator note", "author": "coordinator", "expected_assignee": "alice"},
        )

        assert resp.status_code == 201
        assert id_field in resp.json()

    @pytest.mark.parametrize(
        "path_template",
        [
            "/api/issue/{issue_id}/comments",
            "/api/weft/issues/{issue_id}/comments",
        ],
    )
    async def test_add_comment_expected_assignee_must_be_string(
        self,
        client: AsyncClient,
        dashboard_db: PopulatedDB,
        path_template: str,
    ) -> None:
        ids = dashboard_db.ids

        resp = await client.post(
            path_template.format(issue_id=ids["a"]),
            json={"text": "bad expectation", "expected_assignee": ["alice"]},
        )

        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_add_comment(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            f"/api/issue/{ids['b']}/comments",
            json={"text": "A new comment", "author": "alice"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["text"] == "A new comment"
        assert data["author"] == "alice"
        assert "id" in data
        assert "created_at" in data

    async def test_add_comment_default_author(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            f"/api/issue/{ids['a']}/comments",
            json={"text": "No author specified"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["text"] == "No author specified"
        assert data["author"] == "dashboard"

    async def test_add_comment_not_found(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/issue/nonexistent/comments",
            json={"text": "orphan comment"},
        )
        assert resp.status_code == 404
        assert "error" in resp.json()

    async def test_add_comment_empty_text(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            f"/api/issue/{ids['b']}/comments",
            json={"text": ""},
        )
        assert resp.status_code == 400
        assert "error" in resp.json()

    async def test_add_comment_whitespace_text(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            f"/api/issue/{ids['b']}/comments",
            json={"text": "   "},
        )
        assert resp.status_code == 400
        assert "error" in resp.json()


class TestSearchAPI:
    """GET /api/search?q=... — server-side FTS5 search."""

    async def test_search_finds_issue(self, client: AsyncClient) -> None:
        resp = await client.get("/api/search", params={"q": "Issue A"})
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert "total" in data
        assert data["total"] >= 1
        titles = [r["title"] for r in data["results"]]
        assert "Issue A" in titles

    async def test_search_empty_query(self, client: AsyncClient) -> None:
        resp = await client.get("/api/search", params={"q": ""})
        assert resp.status_code == 200
        data = resp.json()
        assert data["results"] == []
        assert data["total"] == 0

    async def test_search_no_results(self, client: AsyncClient) -> None:
        resp = await client.get("/api/search", params={"q": "zzzznonexistentzzzz"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["results"] == []

    async def test_search_with_limit(self, client: AsyncClient) -> None:
        resp = await client.get("/api/search", params={"q": "Issue", "limit": 1})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) <= 1

    async def test_search_with_offset(self, client: AsyncClient) -> None:
        # Get total first
        resp_all = await client.get("/api/search", params={"q": "Issue"})
        total_all = resp_all.json()["total"]
        # Now with offset
        resp = await client.get("/api/search", params={"q": "Issue", "offset": 1})
        data = resp.json()
        assert data["total"] <= total_all


class TestMetricsAPI:
    """GET /api/metrics?days=30 — flow metrics."""

    async def test_metrics_default(self, client: AsyncClient) -> None:
        resp = await client.get("/api/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "period_days" in data
        assert "throughput" in data
        assert "avg_cycle_time_hours" in data
        assert "avg_lead_time_hours" in data
        assert "by_type" in data
        assert data["period_days"] == 30

    async def test_metrics_custom_days(self, client: AsyncClient) -> None:
        resp = await client.get("/api/metrics", params={"days": 7})
        assert resp.status_code == 200
        data = resp.json()
        assert data["period_days"] == 7

    async def test_metrics_throughput(self, client: AsyncClient) -> None:
        # populated_db has 1 closed issue (C), so throughput should be >= 1
        resp = await client.get("/api/metrics", params={"days": 365})
        data = resp.json()
        assert data["throughput"] >= 1


class TestCriticalPathAPI:
    """GET /api/critical-path — longest dependency chain."""

    async def test_critical_path(self, client: AsyncClient) -> None:
        resp = await client.get("/api/critical-path")
        assert resp.status_code == 200
        data = resp.json()
        assert "path" in data
        assert "length" in data
        assert isinstance(data["path"], list)
        assert data["length"] == len(data["path"])

    async def test_critical_path_has_dep_chain(self, client: AsyncClient) -> None:
        # A depends on B, both open, so the critical path should be >= 2
        resp = await client.get("/api/critical-path")
        data = resp.json()
        assert data["length"] >= 2

    async def test_critical_path_node_structure(self, client: AsyncClient) -> None:
        resp = await client.get("/api/critical-path")
        data = resp.json()
        if data["length"] > 0:
            node = data["path"][0]
            assert "id" in node
            assert "title" in node
            assert "priority" in node
            assert "type" in node


class TestActivityAPI:
    """GET /api/activity — recent events across all issues."""

    async def test_activity_default(self, client: AsyncClient) -> None:
        resp = await client.get("/api/activity")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        # populated_db creates events for epic, A, B, C (created, closed, dep_added, comment)
        assert len(data) >= 1

    async def test_activity_has_issue_title(self, client: AsyncClient) -> None:
        resp = await client.get("/api/activity")
        data = resp.json()
        assert len(data) >= 1
        event = data[0]
        assert "issue_id" in event
        assert "event_type" in event
        assert "issue_title" in event
        assert "created_at" in event

    async def test_activity_with_limit(self, client: AsyncClient) -> None:
        resp = await client.get("/api/activity", params={"limit": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) <= 2

    async def test_activity_with_since(self, client: AsyncClient) -> None:
        # Use a very old timestamp to get all events
        resp = await client.get("/api/activity", params={"since": "2020-01-01T00:00:00"})
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    async def test_activity_since_returns_chronological(self, client: AsyncClient) -> None:
        resp = await client.get("/api/activity", params={"since": "2020-01-01T00:00:00"})
        data = resp.json()
        if len(data) >= 2:
            # Chronological: earliest first
            assert data[0]["created_at"] <= data[-1]["created_at"]

    async def test_activity_no_since_returns_newest_first(self, client: AsyncClient) -> None:
        resp = await client.get("/api/activity")
        data = resp.json()
        if len(data) >= 2:
            # Newest-first (no since param)
            assert data[0]["created_at"] >= data[-1]["created_at"]


class TestPlanAPI:
    """GET /api/plan/{milestone_id} — milestone plan tree."""

    async def test_plan_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/plan/nonexistent")
        assert resp.status_code == 404
        assert "error" in resp.json()

    async def test_plan_non_milestone_root_returns_not_found(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        phase = dashboard_db.db.create_issue("Wrong root", type="phase")

        resp = await client.get(f"/api/plan/{phase.id}")

        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == "NOT_FOUND"
        assert "not a milestone" in body["error"]

    async def test_plan_returns_tree(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        # Create a mini milestone -> phase -> step hierarchy
        milestone = dashboard_db.db.create_issue("Test Milestone", type="milestone")
        phase = dashboard_db.db.create_issue("Phase 1", type="phase", parent_id=milestone.id)
        dashboard_db.db.create_issue("Step 1", type="step", parent_id=phase.id)

        resp = await client.get(f"/api/plan/{milestone.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert "milestone" in data
        assert "phases" in data
        assert "total_steps" in data
        assert "completed_steps" in data
        assert data["milestone"]["id"] == milestone.id
        assert len(data["phases"]) == 1
        assert data["total_steps"] == 1
        assert data["completed_steps"] == 0


class TestBatchAPI:
    """POST /api/batch/update and /api/batch/close — batch operations."""

    async def test_batch_update_priority(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            "/api/batch/update",
            json={"issue_ids": [ids["a"], ids["b"]], "priority": 0},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "updated" in data
        assert "errors" in data
        assert len(data["updated"]) == 2
        assert all(i["priority"] == 0 for i in data["updated"])

    async def test_batch_update_with_errors(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            "/api/batch/update",
            json={"issue_ids": [ids["a"], "nonexistent"], "priority": 1},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["updated"]) == 1
        assert len(data["errors"]) == 1
        assert data["errors"][0]["id"] == "nonexistent"

    async def test_batch_update_with_actor(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            "/api/batch/update",
            json={"issue_ids": [ids["b"]], "priority": 3, "actor": "batch-bot"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["updated"]) == 1

    async def test_batch_update_invalid_fields_type_returns_400(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            "/api/batch/update",
            json={"issue_ids": [ids["a"]], "fields": []},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert "fields must be a dict" in body["error"]

    async def test_batch_close(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            "/api/batch/close",
            json={"issue_ids": [ids["b"]], "reason": "batch done"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "closed" in data
        assert "errors" in data
        assert len(data["closed"]) == 1
        assert len(data["errors"]) == 0
        assert data["closed"][0]["id"] == ids["b"]

    async def test_batch_close_already_closed(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        # C is already closed — should report per-item error, not 409
        resp = await client.post(
            "/api/batch/close",
            json={"issue_ids": [ids["c"]]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["closed"]) == 0
        assert len(data["errors"]) == 1
        assert data["errors"][0]["id"] == ids["c"]

    async def test_batch_close_with_actor(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            "/api/batch/close",
            json={"issue_ids": [ids["b"]], "actor": "closer-bot"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["closed"]) == 1

    async def test_http_batch_close_force_returns_400(
        self,
        client: AsyncClient,
        dashboard_db: PopulatedDB,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """2.1.0 §1.1: HTTP batch-close rejects ``force=true`` by default.

        Without ``--allow-http-force-close`` at dashboard startup, passing
        ``force=true`` returns 400/VALIDATION instead of bypassing the
        transition validator. Closes the privilege-escalation footgun
        where any HTTP client could clear the workflow gate.
        """
        import filigree.dashboard as dash_module

        monkeypatch.setattr(dash_module, "_allow_http_force_close", False)
        phase = dashboard_db.db.create_issue("Cleanup phase", type="phase")
        resp = await client.post(
            "/api/batch/close",
            json={"issue_ids": [phase.id], "force": True},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert "force=true" in body["error"]
        # And the weft mirror behaves the same.
        resp_weft = await client.post(
            "/api/weft/batch/close",
            json={"issue_ids": [phase.id], "force": True},
        )
        assert resp_weft.status_code == 400
        assert resp_weft.json()["code"] == "VALIDATION"

    async def test_http_batch_close_force_with_opt_in_works(
        self,
        client: AsyncClient,
        dashboard_db: PopulatedDB,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """2.1.0 §1.1: ``force=true`` is honoured once the operator opts in
        via ``--allow-http-force-close`` (modeled here by flipping the
        module flag the CLI option toggles).
        """
        import filigree.dashboard as dash_module

        monkeypatch.setattr(dash_module, "_allow_http_force_close", True)
        phase = dashboard_db.db.create_issue("Cleanup phase", type="phase")
        # Without force the default 'completed' is unreachable from 'pending'.
        resp = await client.post("/api/batch/close", json={"issue_ids": [phase.id]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["closed"] == []
        assert len(data["errors"]) == 1
        assert data["errors"][0]["code"] == "INVALID_TRANSITION"
        # With force AND the opt-in, the close goes through.
        resp = await client.post(
            "/api/batch/close",
            json={"issue_ids": [phase.id], "force": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["closed"]) == 1
        assert data["closed"][0]["status"] == "completed"
        assert data["errors"] == []

    async def test_batch_close_force_must_be_boolean(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post("/api/batch/close", json={"issue_ids": [ids["b"]], "force": "yes"})
        assert resp.status_code == 400
        assert "force" in resp.json()["error"].lower()


class TestActorLengthCapAtHTTPBoundary:
    """2.1.0 §1.4 / ADR-012: ``sanitize_actor``'s 128-character cap is
    enforced at every HTTP entry point that accepts an actor.

    Pinning each surface guards against a regression where one route
    plumbs the actor directly into the DB without routing through
    ``_validate_actor``. The CLI and MCP cap is already pinned at
    ``tests/cli/test_compose_commands.py`` and
    ``tests/mcp/test_boundary_validation.py``; this is the HTTP arm.
    """

    _OVERLONG = "a" * 129  # exactly one over _MAX_ACTOR_LENGTH

    async def test_claim_overlong_actor_rejected(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            f"/api/issue/{ids['a']}/claim",
            json={"assignee": "alice", "actor": self._OVERLONG},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION", body
        assert "128" in body["error"]

    async def test_release_overlong_actor_rejected(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            f"/api/issue/{ids['a']}/release",
            json={"actor": self._OVERLONG},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "VALIDATION"

    async def test_close_overlong_actor_rejected(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            f"/api/issue/{ids['a']}/close",
            json={"actor": self._OVERLONG},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "VALIDATION"

    async def test_batch_close_overlong_actor_rejected(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            "/api/batch/close",
            json={"issue_ids": [ids["a"]], "actor": self._OVERLONG},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "VALIDATION"

    async def test_add_comment_overlong_author_rejected(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            f"/api/issue/{ids['a']}/comments",
            json={"text": "hi", "author": self._OVERLONG},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "VALIDATION"

    async def test_control_char_actor_rejected(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        """Control characters must be rejected pre-strip so smuggled
        newlines can't reach the audit log.
        """
        ids = dashboard_db.ids
        resp = await client.post(
            f"/api/issue/{ids['a']}/claim",
            json={"assignee": "alice", "actor": "bad\nactor"},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "VALIDATION", body
        assert "control" in body["error"].lower()


class TestNonObjectBodyReturns400:
    """Non-dict JSON bodies (e.g. []) must return 400, not crash with 500."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("PATCH", "/api/issue/test-1"),
            ("POST", "/api/issues"),
            ("POST", "/api/issue/test-1/close"),
            ("POST", "/api/batch/update"),
        ],
        ids=["update", "create", "close", "batch-update"],
    )
    async def test_rejects_array_body(self, client: AsyncClient, method: str, path: str) -> None:
        resp = await client.request(method, path, content="[]")
        assert resp.status_code == 400


class TestBatchAPIInputValidation:
    """Bug filigree-366a6d: null issue_ids crashes with 500 TypeError."""

    @pytest.mark.parametrize(
        ("endpoint", "body"),
        [
            ("/api/batch/update", {"issue_ids": None, "priority": 1}),
            ("/api/batch/close", {"issue_ids": None}),
        ],
        ids=["update-null", "close-null"],
    )
    async def test_null_issue_ids_returns_400(self, client: AsyncClient, endpoint: str, body: dict[str, Any]) -> None:
        resp = await client.post(endpoint, json=body)
        assert resp.status_code == 400
        assert "issue_ids" in resp.json()["error"].lower()

    @pytest.mark.parametrize(
        ("endpoint", "body", "check_message"),
        [
            ("/api/batch/update", {"issue_ids": "not-a-list", "priority": 1}, None),
            ("/api/batch/close", {"issue_ids": "not-a-list"}, None),
            ("/api/batch/update", {"priority": 1}, None),
            ("/api/batch/close", {"reason": "done"}, None),
            ("/api/batch/update", {"issue_ids": [123], "priority": 1}, "string"),
            ("/api/batch/close", {"issue_ids": [123]}, "string"),
        ],
        ids=[
            "update-string",
            "close-string",
            "update-missing",
            "close-missing",
            "update-non-string-elements",
            "close-non-string-elements",
        ],
    )
    async def test_invalid_issue_ids_returns_400(
        self,
        client: AsyncClient,
        endpoint: str,
        body: dict[str, Any],
        check_message: str | None,
    ) -> None:
        resp = await client.post(endpoint, json=body)
        assert resp.status_code == 400
        if check_message:
            assert check_message in resp.json()["error"].lower()


class TestBatchClosePartialMutation:
    """Bug filigree-2cecbb: batch/close partially mutates then returns error."""

    async def test_batch_close_collects_per_item_errors(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        """batch/close with mix of valid and invalid IDs should return 200
        with succeeded and failed lists, not a single error response."""
        ids = dashboard_db.ids
        # ids["a"] is open (closeable), "nonexistent" will fail
        resp = await client.post(
            "/api/batch/close",
            json={"issue_ids": [ids["a"], "nonexistent"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "closed" in data
        assert "errors" in data
        assert len(data["closed"]) == 1
        assert data["closed"][0]["id"] == ids["a"]
        assert len(data["errors"]) == 1
        assert data["errors"][0]["id"] == "nonexistent"

    async def test_batch_close_all_fail_returns_200_with_errors(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        """Even if all items fail, batch/close should return 200 with errors list."""
        resp = await client.post(
            "/api/batch/close",
            json={"issue_ids": ["test-deadbeef00", "test-cafebabe01"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["closed"]) == 0
        assert len(data["errors"]) == 2

    async def test_batch_close_foreign_prefix_aborts_envelope_level(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        """2.1.0 §0.4: foreign-prefix in a batch aborts envelope-level
        rather than producing N misleading per-item NOT_FOUND errors."""
        ids = dashboard_db.ids
        resp = await client.post(
            "/api/batch/close",
            # ids["a"] is local; "foreign-deadbeef01" parses as foreign-prefix
            # (hex 12 chars, candidate prefix "foreign" != local "test").
            json={"issue_ids": [ids["a"], "foreign-deadbeef01"]},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["code"] == "VALIDATION"
        # 2.1.0 §1.2: the HTTP envelope uses ``WrongProjectError.safe_message``,
        # so the foreign prefix is no longer echoed back. The assertion
        # pinning "the error indicates a project-membership problem"
        # moves to the generic safe wording.
        from filigree.core import WrongProjectError

        assert data["error"] == WrongProjectError.SAFE_MESSAGE
        # No partial mutation: the local id must NOT have been closed.
        check = await client.get(f"/api/issue/{ids['a']}")
        assert check.json()["status"] != "closed"

    async def test_core_batch_close_returns_tuple(self, dashboard_db: PopulatedDB) -> None:
        """core.batch_close should return (results, errors) like batch_update."""
        ids = dashboard_db.ids
        result = dashboard_db.db.batch_close([ids["a"], "nonexistent"], reason="test")
        # Should be a tuple of (closed_list, errors_list)
        assert isinstance(result, tuple)
        assert len(result) == 2
        closed, errors = result
        assert len(closed) == 1
        assert len(errors) == 1


class TestDependencyManagementAPI:
    async def test_add_dependency(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.post(
            f"/api/issue/{ids['b']}/dependencies",
            json={"depends_on": ids["c"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["added"] is True

    async def test_remove_dependency(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        dashboard_db.db.add_dependency(ids["a"], ids["b"])
        resp = await client.request(
            "DELETE",
            f"/api/issue/{ids['a']}/dependencies/{ids['b']}",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["removed"] is True

    async def test_add_dep_cycle_detection(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        dashboard_db.db.add_dependency(ids["a"], ids["b"])
        resp = await client.post(
            f"/api/issue/{ids['b']}/dependencies",
            json={"depends_on": ids["a"]},
        )
        assert resp.status_code == 409

    async def test_add_dep_not_found(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/issue/test-nonexistent/dependencies",
            json={"depends_on": "test-also-nonexistent"},
        )
        assert resp.status_code == 404


class TestTypesListAPI:
    """GET /api/types — list all registered issue types."""

    async def test_types_list(self, client: AsyncClient) -> None:
        resp = await client.get("/api/types")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        # Check structure of each type entry
        for t in data:
            assert "type" in t
            assert "display_name" in t
            assert "pack" in t
            assert "initial_state" in t

    async def test_types_includes_task(self, client: AsyncClient) -> None:
        resp = await client.get("/api/types")
        data = resp.json()
        type_names = [t["type"] for t in data]
        assert "task" in type_names

    async def test_types_includes_bug(self, client: AsyncClient) -> None:
        resp = await client.get("/api/types")
        data = resp.json()
        type_names = [t["type"] for t in data]
        assert "bug" in type_names


class TestDashboardConcurrency:
    """Bug filigree-4b8e41: sync handlers run in thread pool, creating races on shared DB."""

    def test_all_route_handlers_are_async(self) -> None:
        """All handlers must be async to avoid thread pool dispatch and shared-DB races.

        FastAPI runs sync handlers in a thread pool (anyio.to_thread). With a single
        shared SQLite connection, this causes concurrent multi-thread access.
        Making all handlers async keeps them on the event loop thread — naturally serialized.
        """
        import asyncio

        app = create_app()
        sync_handlers: list[str] = []
        for route in app.routes:  # type: ignore[attr-defined]
            if hasattr(route, "endpoint") and not asyncio.iscoroutinefunction(route.endpoint):
                sync_handlers.append(f"{route.path} ({route.endpoint.__name__})")
        assert sync_handlers == [], f"Sync handlers run in thread pool, racing on shared DB: {sync_handlers}"

    async def test_concurrent_requests_no_errors(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        """Concurrent reads + writes must all succeed without SQLite threading errors."""
        import asyncio

        ids = dashboard_db.ids

        async def read_issues() -> int:
            resp = await client.get("/api/issues")
            return resp.status_code

        async def read_stats() -> int:
            resp = await client.get("/api/stats")
            return resp.status_code

        async def update_priority(p: int) -> int:
            resp = await client.patch(
                f"/api/issue/{ids['b']}",
                json={"priority": p % 5},
            )
            return resp.status_code

        # Mix 10 reads and 5 writes concurrently
        tasks: list[asyncio.Task[int]] = []
        for i in range(5):
            tasks.append(asyncio.ensure_future(read_issues()))
            tasks.append(asyncio.ensure_future(read_stats()))
            tasks.append(asyncio.ensure_future(update_priority(i)))

        results = await asyncio.gather(*tasks)
        assert all(r == 200 for r in results), f"Got status codes: {results}"


class TestEtherealDashboard:
    async def test_no_register_endpoint(self, client: AsyncClient) -> None:
        """Ethereal mode should not have /api/register."""
        resp = await client.post("/api/register", json={"path": "/foo"})
        assert resp.status_code == 404 or resp.status_code == 405

    async def test_projects_returns_single_entry(self, client: AsyncClient) -> None:
        """Ethereal mode returns a single project with empty key."""
        resp = await client.get("/api/projects")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["key"] == ""

    async def test_no_reload_endpoint(self, client: AsyncClient) -> None:
        """Ethereal mode should not have /api/reload."""
        resp = await client.post("/api/reload")
        assert resp.status_code == 404 or resp.status_code == 405

    async def test_issues_at_root_api(self, client: AsyncClient) -> None:
        """Issues served at /api/issues (no project key prefix)."""
        resp = await client.get("/api/issues")
        assert resp.status_code == 200


class TestMcpEndpoint:
    async def test_mcp_endpoint_gated_off_without_token(self, client: AsyncClient) -> None:
        """The high-privilege /mcp transport is not mounted unless a federation
        bearer token is configured. The default ``client`` fixture sets no token,
        so the endpoint must be absent (404). Mount-when-configured and bearer
        enforcement are covered in tests/api/test_weft_auth.py.
        """
        resp = await client.get("/mcp/")
        assert resp.status_code == 404


class TestHealthAPI:
    async def test_health_returns_ok(self, client: AsyncClient) -> None:
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


class TestEtherealTracerBullet:
    """End-to-end validation that init+dashboard+API works together."""

    async def test_single_project_lifecycle(self, dashboard_db: PopulatedDB) -> None:
        """Create an issue via DB, verify it appears in the API."""
        # Create an issue directly in the DB
        dashboard_db.db.create_issue(title="Tracer bullet test")

        # Wire up dashboard
        dash_module._db = dashboard_db.db
        try:
            app = create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                # Verify issues endpoint returns our issue
                resp = await client.get("/api/issues")
                assert resp.status_code == 200
                issues = resp.json()
                assert any(i["title"] == "Tracer bullet test" for i in issues)

                # Verify /api/projects returns single entry with empty key (ethereal)
                proj_resp = await client.get("/api/projects")
                assert proj_resp.status_code == 200
                proj_data = proj_resp.json()
                assert len(proj_data) == 1
                assert proj_data[0]["key"] == ""

                # Verify server-only endpoints are gone
                assert (await client.post("/api/register", json={})).status_code in (404, 405)
        finally:
            dash_module._db = None


# ===========================================================================
# Dashboard error handling regression tests (from test_error_handling_fixes.py)
# Covers: filigree-4c2fd9 (batch/close + JSON validation),
#         filigree-9e7ed0 (sync-in-async handlers)
# ===========================================================================


class TestDashboardBatchCloseKeyError:
    """POST /api/batch/close with nonexistent ID returns per-item error."""

    async def test_batch_close_nonexistent_id(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/batch/close",
            json={"issue_ids": ["test-deadbeef00"]},
        )
        # Returns 200 with per-item error collection (not fail-fast 404)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["closed"]) == 0
        assert len(data["errors"]) == 1
        assert data["errors"][0]["id"] == "test-deadbeef00"


class TestDashboardMalformedJSON:
    """Endpoints should return 400 for malformed JSON bodies."""

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("PATCH", "/api/issue/test-abc123", b"not valid json{{{"),
            ("POST", "/api/issue/test-abc123/close", b"not valid json"),
            ("POST", "/api/issues", b"{broken"),
            ("POST", "/api/batch/close", b"not json"),
            ("POST", "/api/batch/update", b"not json"),
        ],
        ids=["update", "close", "create", "batch-close", "batch-update"],
    )
    async def test_malformed_json_returns_400(self, client: AsyncClient, method: str, path: str, body: bytes) -> None:
        resp = await client.request(method, path, content=body, headers={"content-type": "application/json"})
        assert resp.status_code == 400
        assert "error" in resp.json()


class TestDashboardHandlersAreAsync:
    """All endpoints must be async to avoid thread pool dispatch and shared-DB races.

    Supersedes the old sync-handler test. See TestDashboardConcurrency in tests/api/test_api.py
    for the full concurrency safety test (filigree-4b8e41).
    """

    def test_all_handlers_are_async(self) -> None:
        """All route handlers must be async def (not plain def)."""
        app = create_app()
        for route in app.routes:  # type: ignore[attr-defined]
            if not hasattr(route, "endpoint"):
                continue
            handler = route.endpoint
            assert inspect.iscoroutinefunction(handler), f"Handler {route.path} must be async def to avoid thread pool dispatch"


class TestCORSMiddleware:
    """CORS middleware should restrict origins to localhost only."""

    async def test_cors_allows_localhost(self, client: AsyncClient) -> None:
        resp = await client.options(
            "/api/issues",
            headers={
                "Origin": "http://localhost:8080",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:8080"

    async def test_cors_allows_127_0_0_1(self, client: AsyncClient) -> None:
        resp = await client.options(
            "/api/issues",
            headers={
                "Origin": "http://127.0.0.1:8377",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.headers.get("access-control-allow-origin") == "http://127.0.0.1:8377"

    async def test_cors_rejects_external_origin(self, client: AsyncClient) -> None:
        resp = await client.options(
            "/api/issues",
            headers={
                "Origin": "http://evil.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert "access-control-allow-origin" not in resp.headers

    async def test_cors_rejects_localhost_subdomain(self, client: AsyncClient) -> None:
        """Ensure regex anchoring prevents localhost.evil.com from matching."""
        resp = await client.options(
            "/api/issues",
            headers={
                "Origin": "http://localhost.evil.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert "access-control-allow-origin" not in resp.headers


class TestBoundaryClamping:
    """Input clamping tests for activity and search endpoints."""

    async def test_activity_extreme_limit_clamped(self, client: AsyncClient) -> None:
        """Limit > 1000 should be clamped, not rejected."""
        resp = await client.get("/api/activity", params={"limit": 999999})
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    async def test_activity_negative_limit_clamped(self, client: AsyncClient) -> None:
        """Negative limit should be clamped to 1, not rejected."""
        resp = await client.get("/api/activity", params={"limit": -5})
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    async def test_search_extreme_limit_clamped(self, client: AsyncClient) -> None:
        resp = await client.get("/api/search", params={"q": "Issue", "limit": 999999})
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data

    async def test_search_negative_offset_clamped(self, client: AsyncClient) -> None:
        resp = await client.get("/api/search", params={"q": "Issue", "offset": -10})
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data

    async def test_search_zero_limit_clamped(self, client: AsyncClient) -> None:
        """Limit of 0 should be clamped to 1."""
        resp = await client.get("/api/search", params={"q": "Issue", "limit": 0})
        assert resp.status_code == 200
