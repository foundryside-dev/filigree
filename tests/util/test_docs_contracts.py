"""Documentation contract checks for public surface counts and defaults."""

from __future__ import annotations

from pathlib import Path

from filigree.mcp_server import _all_tools

ROOT = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_public_docs_mcp_tool_count_matches_registry() -> None:
    count = len(_all_tools)
    docs = {
        "README.md": _read("README.md"),
        "docs/README.md": _read("docs/README.md"),
        "docs/getting-started.md": _read("docs/getting-started.md"),
    }

    for path, text in docs.items():
        assert "71 tools" not in text and "71 MCP tools" not in text, path
        assert f"{count} tools" in text or f"{count} MCP tools" in text, path


def test_api_reference_documents_default_release_pack() -> None:
    text = _read("docs/api-reference.md")

    assert '["core", "planning", "release"]' in text
    assert 'defaults to `["core", "planning"]`' not in text
    assert '"enabled_packs": ["core", "planning"]' not in text
