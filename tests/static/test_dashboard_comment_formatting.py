"""Issue-detail comments preserve plain-text formatting without rendering HTML."""

from __future__ import annotations

import json
import subprocess
import textwrap
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMENTS = [
    "First paragraph.\n\nNext steps:\n- First item\n- Second item",
    "Windows lines\r\n\r\n  Indented line\r\n\tTabbed line",
    '<img src=x onerror="alert(1)">\n<script>alert(2)</script> & plain text',
]


class CommentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[dict[str, str | None]] = []
        self.text_nodes: list[tuple[str, dict[str, str | None]]] = []
        self.tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        # Only divs can contain the comment text; ignore unrelated void elements.
        if tag == "div":
            self.stack.append(dict(attrs))

    def handle_endtag(self, tag: str) -> None:
        if tag == "div":
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        if self.stack:
            self.text_nodes.append((data, self.stack[-1]))


@pytest.fixture(scope="module")
def rendered_comments() -> CommentParser:
    script = textwrap.dedent(
        """
        import { openDetail } from "./src/filigree/static/js/views/detail.js";

        const comments = COMMENTS.map((text) => ({ text, author: "tester", created_at: "2026-01-01T12:00:00Z" }));
        const issue = {
          id: "test-comment", title: "Comment formatting", type: "task",
          status: "open", status_category: "open", priority: 2,
          fields: {}, comments, events: [], blocked_by: [], blocks: [],
        };
        const nodes = Object.fromEntries(["detailPanel", "detailHeader", "detailContent"].map((id) => [id, {
          innerHTML: "", classList: { remove() {} }, querySelectorAll() { return []; },
        }]));
        globalThis.document = { getElementById(id) { return nodes[id] || null; } };
        globalThis.history = { replaceState() {} };
        globalThis.fetch = async (url) => ({
          ok: true,
          json: async () => url.endsWith("/issue/test-comment") ? issue : [],
        });
        await openDetail(issue.id);
        console.log(JSON.stringify(nodes.detailContent.innerHTML));
        """
    ).replace("COMMENTS", json.dumps(COMMENTS))
    result = subprocess.run(
        ["node", "--input-type=module"],
        input=script,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    parser = CommentParser()
    parser.feed(json.loads(result.stdout))
    return parser


def test_comment_bodies_preserve_line_breaks_and_indentation(rendered_comments: CommentParser) -> None:
    for comment in COMMENTS:
        containers = [attrs for text, attrs in rendered_comments.text_nodes if text == comment]
        assert len(containers) == 1
        classes = (containers[0].get("class") or "").split()
        assert "whitespace-pre-wrap" in classes
        assert "leading-relaxed" in classes


def test_comment_html_remains_inert_text(rendered_comments: CommentParser) -> None:
    assert "img" not in rendered_comments.tags
    assert "script" not in rendered_comments.tags
    assert any(text == COMMENTS[2] for text, _attrs in rendered_comments.text_nodes)
