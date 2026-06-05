"""MCP-layer tests for entity_associations (ADR-029, Loomweave B.7 / WP9-A).

Exercises the three tools via call_tool() — the same in-process MCP
shape every other MCP test uses. Federation §5 audit tests live in
``test_entity_associations_federation.py``.
"""

from __future__ import annotations

import pytest

from filigree.core import FiligreeDB, WrongProjectError
from filigree.mcp_server import call_tool  # type: ignore[attr-defined]
from filigree.types.api import ErrorCode
from tests.mcp._helpers import _parse

# Mark the entire module as async — every test calls call_tool().
pytestmark = pytest.mark.asyncio


def _assert_wrong_project_payload(result: dict[str, object]) -> None:
    assert result["code"] == ErrorCode.VALIDATION
    assert result["error"] == WrongProjectError.SAFE_MESSAGE
    assert "other" not in str(result["error"])


class TestAddEntityAssociationMCP:
    async def test_attach_returns_row(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Refactor parser", priority=2)
        result = _parse(
            await call_tool(
                "add_entity_association",
                {
                    "issue_id": issue.id,
                    "entity_id": "py:func:parser.tokenize",
                    "content_hash": "abc123",
                    "actor": "alice",
                },
            )
        )
        assert result["issue_id"] == issue.id
        assert result["entity_id"] == "py:func:parser.tokenize"
        assert result["loomweave_entity_id"] == "py:func:parser.tokenize"
        assert result["content_hash_at_attach"] == "abc123"
        assert result["attached_by"] == "alice"

    async def test_list_surfaces_signature_and_signoff_seq(self, mcp_db: FiligreeDB) -> None:
        """B1 parity: the MCP list tool echoes signature/signoff_seq, so the
        response shape matches the HTTP surface reported to Legis."""
        issue = mcp_db.create_issue("Governed", priority=2)
        mcp_db.add_entity_association(issue.id, "sei:gov", content_hash="h1", actor="legis", signature="deadbeef", signoff_seq=7)
        result = _parse(await call_tool("list_entity_associations", {"issue_id": issue.id}))
        row = result["associations"][0]
        assert row["signature"] == "deadbeef"
        assert row["signoff_seq"] == 7

    async def test_attach_accepts_entity_kind(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Refactor parser", priority=2)
        result = _parse(
            await call_tool(
                "add_entity_association",
                {
                    "issue_id": issue.id,
                    "entity_id": "not-a-loomweave-locator",
                    "content_hash": "abc123",
                    "entity_kind": "function",
                },
            )
        )
        assert result["entity_id"] == "not-a-loomweave-locator"
        assert result["entity_kind"] == "function"

    async def test_attach_idempotent_preserves_attached_by(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("t", priority=2)
        await call_tool(
            "add_entity_association",
            {
                "issue_id": issue.id,
                "entity_id": "py:func:foo",
                "content_hash": "h1",
                "actor": "alice",
            },
        )
        second = _parse(
            await call_tool(
                "add_entity_association",
                {
                    "issue_id": issue.id,
                    "entity_id": "py:func:foo",
                    "content_hash": "h2",
                    "actor": "bob",
                },
            )
        )
        assert second["content_hash_at_attach"] == "h2"
        assert second["attached_by"] == "alice"  # preserved

    async def test_attach_missing_issue_returns_not_found(self, mcp_db: FiligreeDB) -> None:
        # mcp_db prefix is "mcp"; use a properly-prefixed-but-nonexistent id
        result = _parse(
            await call_tool(
                "add_entity_association",
                {
                    "issue_id": "mcp-nonexistent",
                    "entity_id": "py:func:foo",
                    "content_hash": "h",
                },
            )
        )
        assert result["code"] == ErrorCode.NOT_FOUND

    async def test_attach_empty_entity_id_validation(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("t", priority=2)
        result = _parse(
            await call_tool(
                "add_entity_association",
                {
                    "issue_id": issue.id,
                    "entity_id": "",
                    "content_hash": "h",
                },
            )
        )
        assert result["code"] == ErrorCode.VALIDATION

    async def test_attach_foreign_prefix_validation(self, mcp_db: FiligreeDB) -> None:
        result = _parse(
            await call_tool(
                "add_entity_association",
                {
                    "issue_id": "other-1234567890",
                    "entity_id": "py:func:foo",
                    "content_hash": "h",
                },
            )
        )
        _assert_wrong_project_payload(result)


class TestRemoveEntityAssociationMCP:
    async def test_remove_existing(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("t", priority=2)
        await call_tool(
            "add_entity_association",
            {"issue_id": issue.id, "entity_id": "py:func:foo", "content_hash": "h"},
        )
        result = _parse(
            await call_tool(
                "remove_entity_association",
                {"issue_id": issue.id, "entity_id": "py:func:foo"},
            )
        )
        assert result["removed"] is True

    async def test_remove_missing_is_noop(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("t", priority=2)
        result = _parse(
            await call_tool(
                "remove_entity_association",
                {"issue_id": issue.id, "entity_id": "py:func:not-attached"},
            )
        )
        assert result["removed"] is False

    async def test_remove_foreign_prefix_validation(self, mcp_db: FiligreeDB) -> None:
        """A foreign-prefix issue_id surfaces as VALIDATION via
        WrongProjectError, matching add/list. Without this, a routing
        error could quietly resolve to ``removed=False`` no-op.
        """
        result = _parse(
            await call_tool(
                "remove_entity_association",
                {"issue_id": "other-1234567890", "entity_id": "py:func:foo"},
            )
        )
        _assert_wrong_project_payload(result)


class TestListEntityAssociationsMCP:
    async def test_list_empty(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("t", priority=2)
        result = _parse(await call_tool("list_entity_associations", {"issue_id": issue.id}))
        assert result == {"associations": []}

    async def test_list_returns_attached_rows(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("t", priority=2)
        await call_tool(
            "add_entity_association",
            {"issue_id": issue.id, "entity_id": "py:func:a", "content_hash": "h1"},
        )
        await call_tool(
            "add_entity_association",
            {"issue_id": issue.id, "entity_id": "py:func:b", "content_hash": "h2"},
        )
        result = _parse(await call_tool("list_entity_associations", {"issue_id": issue.id}))
        ids = {row["loomweave_entity_id"] for row in result["associations"]}
        assert ids == {"py:func:a", "py:func:b"}

    async def test_list_does_not_compute_drift(self, mcp_db: FiligreeDB) -> None:
        """ADR-029 §"Decision 3": no drift_warning field — caller's job."""
        issue = mcp_db.create_issue("t", priority=2)
        await call_tool(
            "add_entity_association",
            {"issue_id": issue.id, "entity_id": "py:func:foo", "content_hash": "h"},
        )
        result = _parse(await call_tool("list_entity_associations", {"issue_id": issue.id}))
        assert "drift_warning" not in result["associations"][0]

    async def test_list_missing_issue_returns_not_found(self, mcp_db: FiligreeDB) -> None:
        """A typoed or deleted issue_id surfaces as NOT_FOUND rather than
        an empty associations list, matching the get_issue_files pattern.
        """
        result = _parse(await call_tool("list_entity_associations", {"issue_id": "mcp-nonexistent"}))
        assert result["code"] == ErrorCode.NOT_FOUND

    async def test_list_foreign_prefix_validation(self, mcp_db: FiligreeDB) -> None:
        result = _parse(await call_tool("list_entity_associations", {"issue_id": "other-1234567890"}))
        _assert_wrong_project_payload(result)


class TestListAssociationsByEntityMCP:
    """Reverse lookup — the surface Loomweave's issues_for (B.6) calls."""

    async def test_returns_empty_for_unbound_entity(self, mcp_db: FiligreeDB) -> None:
        result = _parse(await call_tool("list_associations_by_entity", {"entity_id": "py:func:never"}))
        assert result == {"associations": []}

    async def test_returns_every_issue_bound_to_entity(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("a", priority=2)
        b = mcp_db.create_issue("b", priority=2)
        c = mcp_db.create_issue("c", priority=2)
        target = "py:func:parser.tokenize"
        for issue in (a, b):
            await call_tool(
                "add_entity_association",
                {"issue_id": issue.id, "entity_id": target, "content_hash": "h"},
            )
        await call_tool(
            "add_entity_association",
            {"issue_id": c.id, "entity_id": "py:func:other", "content_hash": "h"},
        )

        result = _parse(await call_tool("list_associations_by_entity", {"entity_id": target}))
        issue_ids = {row["issue_id"] for row in result["associations"]}
        assert issue_ids == {a.id, b.id}
        assert all(row["entity_id"] == target for row in result["associations"])

    async def test_rejects_blank_entity_id(self, mcp_db: FiligreeDB) -> None:
        result = _parse(await call_tool("list_associations_by_entity", {"entity_id": "   "}))
        assert result["code"] == ErrorCode.VALIDATION

    async def test_foreign_looking_entity_id_is_opaque_lookup_key(self, mcp_db: FiligreeDB) -> None:
        result = _parse(await call_tool("list_associations_by_entity", {"entity_id": "other-1234567890"}))
        assert result == {"associations": []}


class TestRoundTrip:
    async def test_full_lifecycle_via_mcp(self, mcp_db: FiligreeDB) -> None:
        """Integration: attach → list → re-attach (refresh hash) → remove → list empty."""
        issue = mcp_db.create_issue("Lifecycle test", priority=2)

        # Attach
        await call_tool(
            "add_entity_association",
            {
                "issue_id": issue.id,
                "entity_id": "py:func:lifecycle",
                "content_hash": "v1",
                "actor": "alice",
            },
        )
        listed = _parse(await call_tool("list_entity_associations", {"issue_id": issue.id}))
        assert len(listed["associations"]) == 1
        assert listed["associations"][0]["content_hash_at_attach"] == "v1"

        # Re-attach with new hash (drift refresh) — preserves attached_by
        await call_tool(
            "add_entity_association",
            {
                "issue_id": issue.id,
                "entity_id": "py:func:lifecycle",
                "content_hash": "v2",
                "actor": "bob",
            },
        )
        listed = _parse(await call_tool("list_entity_associations", {"issue_id": issue.id}))
        assert len(listed["associations"]) == 1  # still one row
        assert listed["associations"][0]["content_hash_at_attach"] == "v2"
        assert listed["associations"][0]["attached_by"] == "alice"

        # Remove
        removed = _parse(
            await call_tool(
                "remove_entity_association",
                {"issue_id": issue.id, "entity_id": "py:func:lifecycle"},
            )
        )
        assert removed["removed"] is True

        # List is empty
        listed = _parse(await call_tool("list_entity_associations", {"issue_id": issue.id}))
        assert listed == {"associations": []}
