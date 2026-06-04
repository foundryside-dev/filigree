from __future__ import annotations

import json
import subprocess
import textwrap
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent


class FragmentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def _module_url(rel: str) -> str:
    return f"file://{(ROOT / rel).as_posix()}"


def _render_js(script: str) -> dict[str, Any]:
    result = subprocess.run(
        ["node", "--input-type=module"],
        input=textwrap.dedent(script),
        capture_output=True,
        check=True,
        text=True,
        cwd=ROOT,
    )
    return json.loads(result.stdout)


def _parse_fragment(html: str) -> FragmentParser:
    parser = FragmentParser()
    parser.feed(html)
    return parser


def _assert_no_injected_attrs(parser: FragmentParser) -> None:
    for _tag, attrs in parser.tags:
        assert "autofocus" not in attrs
        assert "onmouseover" not in attrs
        assert "formaction" not in attrs


def test_issue_id_chip_renders_payload_as_inert_text_and_expected_handlers() -> None:
    payload = "issue\" autofocus=\"true' onmouseover='alert(1)<>&\n\tspace"
    data = _render_js(
        f"""
        import {{ issueIdChip }} from {json.dumps(_module_url("src/filigree/static/js/ui.js"))};

        const payload = {json.dumps(payload)};
        console.log(JSON.stringify({{ html: issueIdChip(payload) }}));
        """
    )
    parser = _parse_fragment(data["html"])
    tag, attrs = parser.tags[0]

    assert tag == "span"
    assert attrs["role"] == "button"
    assert attrs["tabindex"] == "0"
    assert attrs["onclick"].startswith("copyIssueId('")
    assert attrs["onclick"].endswith("', event)")
    assert attrs["onkeydown"].startswith("if(event.key==='Enter')copyIssueId('")
    assert payload in "".join(parser.text)
    _assert_no_injected_attrs(parser)


def test_kanban_card_renders_issue_id_and_labels_as_inert_dom_attributes() -> None:
    payload = "filigree-\" autofocus=\"true' onmouseover='alert(1)<>&"
    data = _render_js(
        f"""
        import {{ renderCard }} from {json.dumps(_module_url("src/filigree/static/js/views/kanban.js"))};
        import {{ state }} from {json.dumps(_module_url("src/filigree/static/js/state.js"))};

        state.multiSelectMode = false;
        state.changedIds.clear();
        state.impactScores = {{}};
        const issue = {{
          id: {json.dumps(payload)},
          title: "Injected <title> should be text",
          type: "bug",
          priority: 1,
          status: "triage\\\" autofocus=\\\"true",
          status_category: "open",
          blocked_by: [],
          is_ready: true,
          updated_at: new Date().toISOString(),
        }};
        console.log(JSON.stringify({{ html: renderCard(issue) }}));
        """
    )
    parser = _parse_fragment(data["html"])
    root_tag, root_attrs = parser.tags[0]

    assert root_tag == "div"
    assert "card" in (root_attrs["class"] or "")
    assert root_attrs["data-id"] == payload
    assert root_attrs["onclick"].startswith("openDetail('")
    assert root_attrs["onclick"].endswith("')")
    assert payload in "".join(parser.text)
    _assert_no_injected_attrs(parser)


def test_attribute_safe_js_escape_survives_browser_style_attribute_parsing() -> None:
    payload = "id\" formaction=\"https://evil.invalid' onmouseover='alert(1)<>&"
    data = _render_js(
        f"""
        import {{ escJsSingleAttr }} from {json.dumps(_module_url("src/filigree/static/js/ui.js"))};

        const payload = {json.dumps(payload)};
        const escaped = escJsSingleAttr(payload);
        console.log(JSON.stringify({{ html: `<button onclick="openDetail('${{escaped}}')">Open</button>` }}));
        """
    )
    parser = _parse_fragment(data["html"])
    tag, attrs = parser.tags[0]

    assert tag == "button"
    assert set(attrs) == {"onclick"}
    assert attrs["onclick"].startswith("openDetail('")
    assert attrs["onclick"].endswith("')")
    _assert_no_injected_attrs(parser)
