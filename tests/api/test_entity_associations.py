"""HTTP route tests for entity_associations (ADR-029, Clarion B.7 / WP9-A).

Mirrors the MCP-layer test surface against the FastAPI routes — same
shapes, same idempotency, same error semantics. Federation §5 audit
tests live in ``tests/test_entity_associations_federation.py``.
"""

from __future__ import annotations

from httpx import AsyncClient, Response

from filigree.core import WrongProjectError
from filigree.types.api import ErrorCode
from tests.conftest import PopulatedDB


def _assert_wrong_project_response(resp: Response) -> None:
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == ErrorCode.VALIDATION
    assert body["error"] == WrongProjectError.SAFE_MESSAGE
    assert "other" not in body["error"]


class TestListEntityAssociationsHTTP:
    async def test_empty_for_unattached_issue(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        issue_id = dashboard_db.ids["a"]
        resp = await client.get(f"/api/issue/{issue_id}/entity-associations")
        assert resp.status_code == 200
        assert resp.json() == {"associations": []}

    async def test_returns_attached_rows(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        issue_id = dashboard_db.ids["a"]
        # Attach two via the data layer (HTTP attach tested separately).
        dashboard_db.db.add_entity_association(issue_id, "py:func:a", content_hash="h1")
        dashboard_db.db.add_entity_association(issue_id, "py:func:b", content_hash="h2")

        resp = await client.get(f"/api/issue/{issue_id}/entity-associations")
        assert resp.status_code == 200
        body = resp.json()
        ids = {row["clarion_entity_id"] for row in body["associations"]}
        assert ids == {"py:func:a", "py:func:b"}

    async def test_missing_issue_returns_404(self, client: AsyncClient) -> None:
        # dashboard_db prefix is "test"; same-prefix missing ID is the
        # only configuration where "not found" is the right answer (a
        # foreign-prefix ID is a project-routing error, not a typo).
        resp = await client.get("/api/issue/test-nonexistent/entity-associations")
        assert resp.status_code == 404

    async def test_list_foreign_prefix_returns_400(self, client: AsyncClient) -> None:
        """A foreign-prefix issue_id surfaces as VALIDATION via
        WrongProjectError, not a misleading NOT_FOUND."""
        resp = await client.get("/api/issue/other-1234567890/entity-associations")
        _assert_wrong_project_response(resp)


class TestAddEntityAssociationHTTP:
    async def test_attach_returns_201(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        issue_id = dashboard_db.ids["a"]
        resp = await client.post(
            f"/api/issue/{issue_id}/entity-associations",
            json={
                "entity_id": "py:func:tokenize",
                "content_hash": "hash-a",
                "actor": "alice",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["clarion_entity_id"] == "py:func:tokenize"
        assert body["content_hash_at_attach"] == "hash-a"
        assert body["attached_by"] == "alice"

    async def test_attach_idempotent_refreshes_hash(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        issue_id = dashboard_db.ids["a"]
        await client.post(
            f"/api/issue/{issue_id}/entity-associations",
            json={"entity_id": "py:func:foo", "content_hash": "h1", "actor": "alice"},
        )
        resp = await client.post(
            f"/api/issue/{issue_id}/entity-associations",
            json={"entity_id": "py:func:foo", "content_hash": "h2", "actor": "bob"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["content_hash_at_attach"] == "h2"
        assert body["attached_by"] == "alice"  # preserved

    async def test_attach_missing_issue_returns_404(self, client: AsyncClient) -> None:
        # Same prefix as the test DB ("test") so this is a real "issue
        # doesn't exist" case rather than a cross-project routing error.
        resp = await client.post(
            "/api/issue/test-nonexistent/entity-associations",
            json={"entity_id": "py:func:foo", "content_hash": "h"},
        )
        assert resp.status_code == 404

    async def test_attach_missing_entity_id_returns_400(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        issue_id = dashboard_db.ids["a"]
        resp = await client.post(
            f"/api/issue/{issue_id}/entity-associations",
            json={"content_hash": "h"},
        )
        assert resp.status_code == 400

    async def test_attach_missing_content_hash_returns_400(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        issue_id = dashboard_db.ids["a"]
        resp = await client.post(
            f"/api/issue/{issue_id}/entity-associations",
            json={"entity_id": "py:func:foo"},
        )
        assert resp.status_code == 400

    async def test_attach_foreign_prefix_returns_400(self, client: AsyncClient) -> None:
        """Other write routes surface foreign-prefix IDs as 400 VALIDATION
        via WrongProjectError, not 404. The pre-existence check that
        masked this is intentionally removed.
        """
        resp = await client.post(
            "/api/issue/other-1234567890/entity-associations",
            json={"entity_id": "py:func:foo", "content_hash": "h"},
        )
        _assert_wrong_project_response(resp)

    async def test_attach_rejects_whitespace_actor(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        """Match other write routes: actor goes through _validate_actor
        so whitespace/control-character values can't reach attached_by.
        """
        issue_id = dashboard_db.ids["a"]
        resp = await client.post(
            f"/api/issue/{issue_id}/entity-associations",
            json={"entity_id": "py:func:foo", "content_hash": "h", "actor": "   "},
        )
        assert resp.status_code == 400

    async def test_attach_defaults_actor_when_omitted(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        """Omitting actor uses the dashboard default rather than empty
        string, so audit rows always have a non-empty attached_by.
        """
        issue_id = dashboard_db.ids["a"]
        resp = await client.post(
            f"/api/issue/{issue_id}/entity-associations",
            json={"entity_id": "py:func:default-actor", "content_hash": "h"},
        )
        assert resp.status_code == 201
        assert resp.json()["attached_by"]  # non-empty


class TestListAssociationsByEntityHTTP:
    """Reverse lookup — the route Clarion's issues_for (B.6) calls."""

    async def test_returns_empty_for_unbound_entity(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        resp = await client.get("/api/entity-associations", params={"entity_id": "py:func:never"})
        assert resp.status_code == 200
        assert resp.json() == {"associations": []}

    async def test_returns_every_issue_bound_to_entity(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        a_id = dashboard_db.ids["a"]
        b_id = dashboard_db.ids["b"]
        target = "py:func:parser.tokenize"
        dashboard_db.db.add_entity_association(a_id, target, content_hash="h1")
        dashboard_db.db.add_entity_association(b_id, target, content_hash="h2")
        # An unrelated binding that must not appear in the result.
        dashboard_db.db.add_entity_association(a_id, "py:func:other", content_hash="h3")

        resp = await client.get("/api/entity-associations", params={"entity_id": target})
        assert resp.status_code == 200
        body = resp.json()
        assert {row["issue_id"] for row in body["associations"]} == {a_id, b_id}
        assert all(row["clarion_entity_id"] == target for row in body["associations"])

    async def test_foreign_looking_entity_id_is_opaque_lookup_key(self, client: AsyncClient) -> None:
        resp = await client.get("/api/entity-associations", params={"entity_id": "other-1234567890"})
        assert resp.status_code == 200
        assert resp.json() == {"associations": []}

    async def test_missing_entity_id_returns_400(self, client: AsyncClient) -> None:
        resp = await client.get("/api/entity-associations")
        assert resp.status_code == 400

    async def test_whitespace_entity_id_returns_400(self, client: AsyncClient) -> None:
        resp = await client.get("/api/entity-associations", params={"entity_id": "   "})
        assert resp.status_code == 400


class TestRemoveEntityAssociationHTTP:
    async def test_remove_existing_returns_true(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        issue_id = dashboard_db.ids["a"]
        dashboard_db.db.add_entity_association(issue_id, "py:func:foo", content_hash="h")

        resp = await client.delete(
            f"/api/issue/{issue_id}/entity-associations",
            params={"entity_id": "py:func:foo"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"removed": True}

    async def test_remove_missing_returns_false(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        issue_id = dashboard_db.ids["a"]
        resp = await client.delete(
            f"/api/issue/{issue_id}/entity-associations",
            params={"entity_id": "py:func:never-attached"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"removed": False}

    async def test_remove_without_entity_id_returns_400(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        issue_id = dashboard_db.ids["a"]
        resp = await client.delete(f"/api/issue/{issue_id}/entity-associations")
        assert resp.status_code == 400

    async def test_remove_foreign_prefix_returns_400(self, client: AsyncClient) -> None:
        """A foreign-prefix issue_id surfaces as VALIDATION via
        WrongProjectError, matching the other write routes (POST/GET).
        Without this, a cross-project routing error could masquerade as
        an idempotent ``{"removed": false}`` no-op.
        """
        resp = await client.delete(
            "/api/issue/other-1234567890/entity-associations",
            params={"entity_id": "py:func:foo"},
        )
        _assert_wrong_project_response(resp)


class TestFullLifecycleViaHTTP:
    async def test_attach_list_reattach_remove(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        issue_id = dashboard_db.ids["a"]

        # Attach
        await client.post(
            f"/api/issue/{issue_id}/entity-associations",
            json={
                "entity_id": "py:func:lifecycle",
                "content_hash": "v1",
                "actor": "alice",
            },
        )
        listed = (await client.get(f"/api/issue/{issue_id}/entity-associations")).json()
        assert len(listed["associations"]) == 1

        # Re-attach (drift refresh) — same row, new hash, preserved actor
        await client.post(
            f"/api/issue/{issue_id}/entity-associations",
            json={
                "entity_id": "py:func:lifecycle",
                "content_hash": "v2",
                "actor": "bob",
            },
        )
        listed = (await client.get(f"/api/issue/{issue_id}/entity-associations")).json()
        assert len(listed["associations"]) == 1
        assert listed["associations"][0]["content_hash_at_attach"] == "v2"
        assert listed["associations"][0]["attached_by"] == "alice"

        # Remove
        removed = (
            await client.delete(
                f"/api/issue/{issue_id}/entity-associations",
                params={"entity_id": "py:func:lifecycle"},
            )
        ).json()
        assert removed == {"removed": True}

        # List is empty
        listed = (await client.get(f"/api/issue/{issue_id}/entity-associations")).json()
        assert listed == {"associations": []}
