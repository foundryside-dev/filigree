"""Dashboard API boundary validation tests for priority and actor."""

from __future__ import annotations

from httpx import AsyncClient


class TestDashboardPriorityValidation:
    """Priority range checks in dashboard routes."""

    async def test_update_issue_priority_too_high(self, client: AsyncClient) -> None:
        resp = await client.post("/api/issues", json={"title": "Target"})
        assert resp.status_code == 201
        issue_id = resp.json()["id"]
        resp = await client.patch(f"/api/issue/{issue_id}", json={"priority": 5})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_update_issue_priority_too_low(self, client: AsyncClient) -> None:
        resp = await client.post("/api/issues", json={"title": "Target"})
        issue_id = resp.json()["id"]
        resp = await client.patch(f"/api/issue/{issue_id}", json={"priority": -1})
        assert resp.status_code == 400

    async def test_create_issue_priority_out_of_range(self, client: AsyncClient) -> None:
        resp = await client.post("/api/issues", json={"title": "Bad", "priority": 99})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_create_issue_priority_boundary_0(self, client: AsyncClient) -> None:
        resp = await client.post("/api/issues", json={"title": "P0", "priority": 0})
        assert resp.status_code == 201
        assert resp.json()["priority"] == 0

    async def test_create_issue_priority_boundary_4(self, client: AsyncClient) -> None:
        resp = await client.post("/api/issues", json={"title": "P4", "priority": 4})
        assert resp.status_code == 201
        assert resp.json()["priority"] == 4

    async def test_batch_update_priority_out_of_range(self, client: AsyncClient) -> None:
        resp = await client.post("/api/issues", json={"title": "Target"})
        issue_id = resp.json()["id"]
        resp = await client.post(
            "/api/batch/update",
            json={"issue_ids": [issue_id], "priority": 5},
        )
        assert resp.status_code == 400

    async def test_create_issue_priority_float(self, client: AsyncClient) -> None:
        resp = await client.post("/api/issues", json={"title": "Bad", "priority": 2.5})
        assert resp.status_code == 400

    async def test_create_issue_priority_bool(self, client: AsyncClient) -> None:
        """bool is a subclass of int — should be rejected."""
        resp = await client.post("/api/issues", json={"title": "Bad", "priority": True})
        assert resp.status_code == 400

    async def test_create_issue_priority_null_rejected(self, client: AsyncClient) -> None:
        resp = await client.post("/api/issues", json={"title": "Bad", "priority": None})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_update_issue_priority_null_rejected(self, client: AsyncClient) -> None:
        resp = await client.post("/api/issues", json={"title": "Target"})
        issue_id = resp.json()["id"]
        resp = await client.patch(f"/api/issue/{issue_id}", json={"priority": None})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_batch_update_priority_null_rejected(self, client: AsyncClient) -> None:
        resp = await client.post("/api/issues", json={"title": "Target"})
        issue_id = resp.json()["id"]
        resp = await client.post(
            "/api/batch/update",
            json={"issue_ids": [issue_id], "priority": None},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"


class TestDashboardActorValidation:
    """Actor validation in dashboard routes."""

    async def test_update_issue_empty_actor(self, client: AsyncClient) -> None:
        resp = await client.post("/api/issues", json={"title": "Target"})
        issue_id = resp.json()["id"]
        resp = await client.patch(f"/api/issue/{issue_id}", json={"actor": "", "title": "New"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_close_issue_control_char_actor(self, client: AsyncClient) -> None:
        resp = await client.post("/api/issues", json={"title": "Target"})
        issue_id = resp.json()["id"]
        resp = await client.post(f"/api/issue/{issue_id}/close", json={"actor": "\x00evil"})
        assert resp.status_code == 400

    async def test_create_issue_bom_actor(self, client: AsyncClient) -> None:
        resp = await client.post("/api/issues", json={"title": "Test", "actor": "\ufeff"})
        assert resp.status_code == 400

    async def test_add_comment_control_char_author(self, client: AsyncClient) -> None:
        """Control characters in comment author should be rejected."""
        resp = await client.post("/api/issues", json={"title": "Target"})
        issue_id = resp.json()["id"]
        resp = await client.post(
            f"/api/issue/{issue_id}/comments",
            json={"text": "hello", "author": "\x00evil"},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_add_comment_empty_author(self, client: AsyncClient) -> None:
        """Empty comment author should be rejected."""
        resp = await client.post("/api/issues", json={"title": "Target"})
        issue_id = resp.json()["id"]
        resp = await client.post(
            f"/api/issue/{issue_id}/comments",
            json={"text": "hello", "author": ""},
        )
        assert resp.status_code == 400

    async def test_claim_issue_valid_actor(self, client: AsyncClient) -> None:
        """Valid actor should pass through."""
        resp = await client.post("/api/issues", json={"title": "Target"})
        issue_id = resp.json()["id"]
        resp = await client.post(
            f"/api/issue/{issue_id}/claim",
            json={"assignee": "bot", "actor": "test-agent"},
        )
        assert resp.status_code == 200


class TestPaginationOverflowBoundary:
    """filigree-393cfab62c: pagination params must reject before SQLite bind overflow."""

    async def test_classic_files_rejects_huge_limit_with_400(self, client: AsyncClient) -> None:
        # Without the cap, 'limit + 1' overfetch on int64 max raised an
        # uncaught OverflowError from sqlite3 and surfaced as 500.
        resp = await client.get("/api/files", params={"limit": "9223372036854775807"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_classic_files_rejects_above_max_pagination_limit(self, client: AsyncClient) -> None:
        from filigree.dashboard_routes.common import _MAX_PAGINATION_LIMIT

        resp = await client.get("/api/files", params={"limit": str(_MAX_PAGINATION_LIMIT + 1)})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_classic_files_rejects_huge_offset_with_400(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files", params={"limit": "10", "offset": "99999999999999999999"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_classic_files_rejects_huge_min_findings_with_400(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files", params={"min_findings": "9223372036854775808"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_loom_files_rejects_huge_min_findings_with_400(self, client: AsyncClient) -> None:
        resp = await client.get("/api/loom/files", params={"min_findings": "9223372036854775808"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_hotspots_rejects_huge_limit_with_400(self, client: AsyncClient) -> None:
        # filigree-873962aa58: limit-only endpoints bypassed _parse_pagination
        # and bound 2**63 directly into SQLite, raising OverflowError -> 500.
        resp = await client.get("/api/files/hotspots", params={"limit": "9223372036854775808"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_hotspots_rejects_above_max_pagination_limit(self, client: AsyncClient) -> None:
        from filigree.dashboard_routes.common import _MAX_PAGINATION_LIMIT

        resp = await client.get("/api/files/hotspots", params={"limit": str(_MAX_PAGINATION_LIMIT + 1)})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_scan_runs_rejects_huge_limit_with_400(self, client: AsyncClient) -> None:
        resp = await client.get("/api/scan-runs", params={"limit": "9223372036854775808"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_scan_runs_rejects_above_max_pagination_limit(self, client: AsyncClient) -> None:
        from filigree.dashboard_routes.common import _MAX_PAGINATION_LIMIT

        resp = await client.get("/api/scan-runs", params={"limit": str(_MAX_PAGINATION_LIMIT + 1)})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"
