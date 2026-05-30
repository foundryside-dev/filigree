"""MCP tests for shared file annotation tools."""

from __future__ import annotations

from pathlib import Path

from filigree.core import FiligreeDB
from filigree.mcp_server import call_tool
from filigree.registry import RegistryUnavailableError, ResolvedFile
from filigree.types.api import ErrorCode
from tests.mcp._helpers import _parse


def _project_root(db: FiligreeDB) -> Path:
    assert db.project_root is not None
    return db.project_root


class TestAnnotationMcpTools:
    async def test_annotate_file_registry_unavailable_returns_error_response(self, mcp_db: FiligreeDB) -> None:
        class UnavailableRegistry:
            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                raise RegistryUnavailableError(
                    "Clarion registry unavailable for test",
                    url="http://clarion.test/api/v1/files?path=src%2Fmcp_ann.py",
                    path=path,
                    cause_kind="network",
                )

            def is_displaced(self) -> bool:
                return False

        root = _project_root(mcp_db)
        source = root / "src" / "mcp_ann.py"
        source.parent.mkdir()
        source.write_text("alpha\n")
        mcp_db.registry = UnavailableRegistry()

        result = await call_tool("annotate_file", {"file_path": "src/mcp_ann.py", "note": "note"})
        data = _parse(result)

        assert data["code"] == ErrorCode.REGISTRY_UNAVAILABLE
        assert data["details"]["cause"] == "registry_unavailable"
        assert data["details"]["cause_kind"] == "network"
        assert data["details"]["path"] == "src/mcp_ann.py"
        assert data["details"]["url"] == "http://clarion.test/api/v1/files?path=src%2Fmcp_ann.py"

    async def test_annotate_file_returns_public_full_payload(self, mcp_db: FiligreeDB) -> None:
        root = _project_root(mcp_db)
        source = root / "src" / "mcp_ann.py"
        source.parent.mkdir()
        source.write_text("alpha\nbeta\n")
        issue = mcp_db.create_issue("Linked issue")

        result = await call_tool(
            "annotate_file",
            {
                "file_path": "src/mcp_ann.py",
                "note": "Future agents should read beta.",
                "line_start": 2,
                "intent": "warning",
                "critical": True,
                "links": [{"target_type": "issue", "target_id": issue.id, "relationship": "must_consider"}],
                "actor": "mcp-test",
            },
        )
        data = _parse(result)

        assert data["annotation_id"].startswith("mcp-ann-")
        assert "id" not in data
        assert data["file_path"] == "src/mcp_ann.py"
        assert data["anchor_state"] == "current"
        assert data["provenance"]["file_checksum"]
        assert data["links"][0]["annotation_link_id"].startswith("mcp-annlink-")

    async def test_list_annotations_defaults_to_summary_envelope_and_paginates(self, mcp_db: FiligreeDB) -> None:
        root = _project_root(mcp_db)
        (root / "a.py").write_text("a\n")
        (root / "b.py").write_text("b\n")
        first = mcp_db.annotate_file("a.py", "critical", critical=True)
        second = mcp_db.annotate_file("b.py", "regular", critical=False)

        result = await call_tool("list_annotations", {"limit": 1})
        data = _parse(result)

        assert set(data) == {"items", "has_more", "next_offset"}
        assert data["items"][0]["annotation_id"] == first["annotation_id"]
        assert "provenance" not in data["items"][0]
        assert data["has_more"] is True
        result2 = await call_tool("list_annotations", {"limit": 10, "response_detail": "full"})
        data2 = _parse(result2)
        assert {item["annotation_id"] for item in data2["items"]} == {first["annotation_id"], second["annotation_id"]}
        assert "provenance" in data2["items"][0]

    async def test_close_issue_returns_annotation_warnings(self, mcp_db: FiligreeDB) -> None:
        root = _project_root(mcp_db)
        (root / "close.py").write_text("x = 1\n")
        issue = mcp_db.create_issue("Close me")
        annotation = mcp_db.annotate_file(
            "close.py",
            "Must be handled before close.",
            critical=True,
            links=[{"target_type": "issue", "target_id": issue.id, "relationship": "must_consider"}],
        )

        result = await call_tool("close_issue", {"issue_id": issue.id, "reason": "done"})
        data = _parse(result)

        assert data["issue_id"] == issue.id
        assert data["annotation_warnings"][0]["annotation_id"] == annotation["annotation_id"]
        assert data["annotation_warnings"][0]["relationship"] == "must_consider"

    async def test_validation_errors_use_flat_envelope(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("annotate_file", {"file_path": "missing.py", "note": ""})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert set(data) >= {"error", "code"}


class TestAnnotationMcpMutationTools:
    async def test_resolve_supersede_promote_and_carry_forward(self, mcp_db: FiligreeDB) -> None:
        root = _project_root(mcp_db)
        (root / "flow.py").write_text("one\ntwo\n")
        old_issue = mcp_db.create_issue("Old")
        new_issue = mcp_db.create_issue("New")
        original = mcp_db.annotate_file(
            "flow.py",
            "Original",
            line_start=1,
            critical=True,
            links=[{"target_type": "issue", "target_id": old_issue.id, "relationship": "must_consider"}],
        )
        replacement = mcp_db.annotate_file("flow.py", "Replacement", line_start=2)

        supersede = _parse(
            await call_tool(
                "supersede_annotation",
                {
                    "annotation_id": original["annotation_id"],
                    "replacement_annotation_id": replacement["annotation_id"],
                    "reason": "newer note",
                },
            )
        )
        assert supersede["status"] == "superseded"

        resolved = _parse(await call_tool("resolve_annotation", {"annotation_id": replacement["annotation_id"], "reason": "done"}))
        assert resolved["status"] == "resolved"

        promoted = _parse(
            await call_tool(
                "promote_annotation",
                {
                    "annotation_id": replacement["annotation_id"],
                    "target_type": "observation",
                    "title": "Promoted note",
                    "reason": "triage candidate",
                },
            )
        )
        assert promoted["target_type"] == "observation"
        assert promoted["target_id"].startswith("mcp-obs-")

        carried = _parse(
            await call_tool(
                "carry_forward_annotation",
                {
                    "annotation_id": original["annotation_id"],
                    "from_target_id": old_issue.id,
                    "to_target_id": new_issue.id,
                    "reason": "phase two",
                },
            )
        )
        assert carried["link"]["target_id"] == new_issue.id

    async def test_carry_forward_requires_active_source_link(self, mcp_db: FiligreeDB) -> None:
        root = _project_root(mcp_db)
        (root / "flow.py").write_text("one\n")
        linked_issue = mcp_db.create_issue("Linked")
        unrelated_issue = mcp_db.create_issue("Unrelated")
        new_issue = mcp_db.create_issue("New")
        annotation = mcp_db.annotate_file(
            "flow.py",
            "Original",
            line_start=1,
            critical=True,
            links=[{"target_type": "issue", "target_id": linked_issue.id, "relationship": "must_consider"}],
        )

        result = await call_tool(
            "carry_forward_annotation",
            {
                "annotation_id": annotation["annotation_id"],
                "from_target_id": unrelated_issue.id,
                "to_target_id": new_issue.id,
                "reason": "phase two",
            },
        )

        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "not actively linked" in data["error"]
