"""Dashboard route input-validation cluster tests.

Covers:
- filigree-719f0abbb5: POST issues/comments/claims reject non-string fields
- filigree-6c21f57786: POST /files/{id}/associations type-checks issue_id/assoc_type
- filigree-2b756a5a44: PATCH /api/issue honors parent_id
- filigree-237bbad946: /api/activity normalizes since to UTC before SQL compare
- filigree-6e6411daba: graph_v2_enabled config value uses strict bool parse, not Python truthiness
- filigree-37c95a7e51: malformed graph env vars fall back to config, not hardcoded False
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from filigree.core import FiligreeDB, WrongProjectError
from tests._db_factory import make_db

# ---------------------------------------------------------------------------
# filigree-719f0abbb5 — non-string body fields must 400, not 500
# ---------------------------------------------------------------------------


class TestPostNonStringFields:
    async def test_create_issue_rejects_non_string_title(self, client: AsyncClient) -> None:
        resp = await client.post("/api/issues", json={"title": 123})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_create_issue_rejects_list_title(self, client: AsyncClient) -> None:
        resp = await client.post("/api/issues", json={"title": ["oops"]})
        assert resp.status_code == 400

    async def test_add_comment_rejects_non_string_text(self, client: AsyncClient) -> None:
        resp = await client.post("/api/issues", json={"title": "Target"})
        issue_id = resp.json()["id"]
        resp = await client.post(f"/api/issue/{issue_id}/comments", json={"text": {"bad": True}})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_claim_rejects_non_string_assignee(self, client: AsyncClient) -> None:
        resp = await client.post("/api/issues", json={"title": "Target"})
        issue_id = resp.json()["id"]
        resp = await client.post(f"/api/issue/{issue_id}/claim", json={"assignee": 42})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_claim_next_rejects_non_string_assignee(self, client: AsyncClient) -> None:
        resp = await client.post("/api/claim-next", json={"assignee": False})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"


# ---------------------------------------------------------------------------
# filigree-4e0f3d7a3b — create/patch validate scalar string fields
# ---------------------------------------------------------------------------


class TestIssueScalarStringValidation:
    @pytest.mark.parametrize(
        ("field", "value", "expected_error"),
        [
            ("parent_id", 123, "parent_id must be a string or null"),
            ("description", {"bad": True}, "description must be a string"),
            ("notes", ["oops"], "notes must be a string"),
        ],
    )
    async def test_create_issue_rejects_invalid_stringish_fields(
        self,
        client: AsyncClient,
        field: str,
        value: object,
        expected_error: str,
    ) -> None:
        resp = await client.post("/api/issues", json={"title": "Target", field: value})
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert body["error"] == expected_error

    @pytest.mark.parametrize(
        ("field", "value", "expected_error"),
        [
            ("title", {"bad": True}, "title must be a string"),
            ("description", ["oops"], "description must be a string"),
            ("notes", 123, "notes must be a string"),
        ],
    )
    async def test_patch_issue_rejects_invalid_stringish_fields(
        self,
        client: AsyncClient,
        field: str,
        value: object,
        expected_error: str,
    ) -> None:
        issue = (await client.post("/api/issues", json={"title": "Target"})).json()
        resp = await client.patch(f"/api/issue/{issue['id']}", json={field: value})
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert body["error"] == expected_error


# ---------------------------------------------------------------------------
# filigree-6c21f57786 — POST associations type-checks
# ---------------------------------------------------------------------------


class TestAssociationsTypeCheck:
    async def _make_file(self, client: AsyncClient) -> str:
        """Register a file record via the files API and return its id."""
        resp = await client.post(
            "/api/v1/scan-results",
            json={
                "scan_source": "test-scanner",
                "findings": [
                    {
                        "path": "src/foo.py",
                        "rule_id": "R001",
                        "message": "probe",
                        "severity": "info",
                    }
                ],
            },
        )
        assert resp.status_code in (200, 202)
        listing = await client.get("/api/files", params={"q": "src/foo.py"})
        assert listing.status_code == 200
        rows = listing.json().get("files") or listing.json().get("results") or listing.json()
        # Some endpoints return {"files": [...]}, others a bare list
        if isinstance(rows, dict):
            rows = rows.get("files", [])
        assert rows, f"expected a file row, got {listing.json()!r}"
        return str(rows[0]["id"])

    async def test_rejects_non_string_issue_id(self, client: AsyncClient) -> None:
        file_id = await self._make_file(client)
        resp = await client.post(
            f"/api/files/{file_id}/associations",
            json={"issue_id": 123, "assoc_type": "bug_in"},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_rejects_non_string_assoc_type(self, client: AsyncClient) -> None:
        file_id = await self._make_file(client)
        resp = await client.post(
            f"/api/files/{file_id}/associations",
            json={"issue_id": "does-not-exist", "assoc_type": {"evil": True}},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"


# ---------------------------------------------------------------------------
# filigree-2b756a5a44 — PATCH honors parent_id
# ---------------------------------------------------------------------------


class TestPatchParentId:
    async def test_patch_sets_parent_id(self, client: AsyncClient) -> None:
        parent = (await client.post("/api/issues", json={"title": "Parent"})).json()
        child = (await client.post("/api/issues", json={"title": "Child"})).json()

        resp = await client.patch(f"/api/issue/{child['id']}", json={"parent_id": parent["id"]})
        assert resp.status_code == 200
        assert resp.json()["parent_id"] == parent["id"]

    async def test_patch_clears_parent_id(self, client: AsyncClient) -> None:
        parent = (await client.post("/api/issues", json={"title": "Parent"})).json()
        child = (await client.post("/api/issues", json={"title": "Child", "parent_id": parent["id"]})).json()
        assert child["parent_id"] == parent["id"]

        resp = await client.patch(f"/api/issue/{child['id']}", json={"parent_id": ""})
        assert resp.status_code == 200
        assert resp.json()["parent_id"] in (None, "")

    async def test_patch_rejects_non_string_parent_id(self, client: AsyncClient) -> None:
        issue = (await client.post("/api/issues", json={"title": "Target"})).json()
        resp = await client.patch(f"/api/issue/{issue['id']}", json={"parent_id": 123})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_patch_parent_id_foreign_prefix_uses_safe_message(self, client: AsyncClient) -> None:
        issue = (await client.post("/api/issues", json={"title": "Target"})).json()
        resp = await client.patch(f"/api/issue/{issue['id']}", json={"parent_id": "foreign-123abc"})
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert body["error"] == WrongProjectError.SAFE_MESSAGE


# ---------------------------------------------------------------------------
# filigree-237bbad946 — /api/activity normalizes since to UTC
# ---------------------------------------------------------------------------


class TestActivitySinceNormalization:
    async def test_offset_bearing_since_matches_utc_equivalent(self, client: AsyncClient) -> None:
        """An offset-bearing timestamp must return the same events as its UTC equivalent.

        Stored timestamps use UTC offset. SQLite does text compare on the column,
        so `2026-04-17T00:00:00-05:00` and `2026-04-17T05:00:00+00:00` must
        produce the same result set — that's only true if the route normalizes
        to UTC isoformat before calling the DB.
        """
        # Create an issue so the DB has at least one event
        await client.post("/api/issues", json={"title": "Probe"})

        utc = await client.get("/api/activity", params={"since": "2020-01-01T05:00:00+00:00"})
        offset = await client.get("/api/activity", params={"since": "2020-01-01T00:00:00-05:00"})
        assert utc.status_code == 200
        assert offset.status_code == 200
        assert utc.json() == offset.json()

    async def test_naive_since_treated_as_utc(self, client: AsyncClient) -> None:
        """Naive timestamps (no offset) must be treated as UTC, not rejected."""
        await client.post("/api/issues", json={"title": "Probe"})
        resp = await client.get("/api/activity", params={"since": "2020-01-01T00:00:00"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# filigree-6e6411daba — graph_v2_enabled config uses strict bool parse
# ---------------------------------------------------------------------------


@pytest.fixture
def graph_db_unit(tmp_path: Path) -> Generator[FiligreeDB, None, None]:
    """FiligreeDB for graph runtime unit tests."""
    d = make_db(tmp_path)
    yield d
    d.close()


class TestGraphConfigBoolCoercion:
    """String values in config.json must parse via strict bool, not Python truthiness."""

    def test_config_string_false_disables_v2(self, graph_db_unit: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        from filigree.dashboard_routes.common import _resolve_graph_runtime

        monkeypatch.delenv("FILIGREE_GRAPH_V2_ENABLED", raising=False)
        monkeypatch.delenv("FILIGREE_GRAPH_API_MODE", raising=False)
        with patch(
            "filigree.dashboard_routes.common._read_graph_runtime_config",
            return_value={"graph_v2_enabled": "false"},
        ):
            result = _resolve_graph_runtime(graph_db_unit)
        assert result["v2_enabled"] is False

    def test_config_string_zero_disables_v2(self, graph_db_unit: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        from filigree.dashboard_routes.common import _resolve_graph_runtime

        monkeypatch.delenv("FILIGREE_GRAPH_V2_ENABLED", raising=False)
        monkeypatch.delenv("FILIGREE_GRAPH_API_MODE", raising=False)
        with patch(
            "filigree.dashboard_routes.common._read_graph_runtime_config",
            return_value={"graph_v2_enabled": "0"},
        ):
            result = _resolve_graph_runtime(graph_db_unit)
        assert result["v2_enabled"] is False

    def test_config_string_true_enables_v2(self, graph_db_unit: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        from filigree.dashboard_routes.common import _resolve_graph_runtime

        monkeypatch.delenv("FILIGREE_GRAPH_V2_ENABLED", raising=False)
        monkeypatch.delenv("FILIGREE_GRAPH_API_MODE", raising=False)
        with patch(
            "filigree.dashboard_routes.common._read_graph_runtime_config",
            return_value={"graph_v2_enabled": "true"},
        ):
            result = _resolve_graph_runtime(graph_db_unit)
        assert result["v2_enabled"] is True

    def test_config_garbage_string_disables_v2_with_warning(self, graph_db_unit: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        from filigree.dashboard_routes.common import _resolve_graph_runtime

        monkeypatch.delenv("FILIGREE_GRAPH_V2_ENABLED", raising=False)
        monkeypatch.delenv("FILIGREE_GRAPH_API_MODE", raising=False)
        with patch(
            "filigree.dashboard_routes.common._read_graph_runtime_config",
            return_value={"graph_v2_enabled": "banana"},
        ):
            result = _resolve_graph_runtime(graph_db_unit)
        assert result["v2_enabled"] is False


# ---------------------------------------------------------------------------
# filigree-37c95a7e51 — malformed env falls back to config, not hardcoded False
# ---------------------------------------------------------------------------


class TestGraphEnvFallback:
    """Malformed env vars must fall back to config, not silently coerce to False/legacy."""

    def test_malformed_enabled_env_falls_back_to_config_true(self, graph_db_unit: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        from filigree.dashboard_routes.common import _resolve_graph_runtime

        monkeypatch.setenv("FILIGREE_GRAPH_V2_ENABLED", "not-a-bool")
        monkeypatch.delenv("FILIGREE_GRAPH_API_MODE", raising=False)
        with patch(
            "filigree.dashboard_routes.common._read_graph_runtime_config",
            return_value={"graph_v2_enabled": True},
        ):
            result = _resolve_graph_runtime(graph_db_unit)
        assert result["v2_enabled"] is True

    def test_malformed_api_mode_env_falls_back_to_config(self, graph_db_unit: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        from filigree.dashboard_routes.common import _resolve_graph_runtime

        monkeypatch.setenv("FILIGREE_GRAPH_API_MODE", "v3-garbage")
        monkeypatch.delenv("FILIGREE_GRAPH_V2_ENABLED", raising=False)
        with patch(
            "filigree.dashboard_routes.common._read_graph_runtime_config",
            return_value={"graph_api_mode": "v2", "graph_v2_enabled": True},
        ):
            result = _resolve_graph_runtime(graph_db_unit)
        assert result["configured_mode"] == "v2"
        assert result["compatibility_mode"] == "v2"
