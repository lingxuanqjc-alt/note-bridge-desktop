"""Independent rendering checks; set NODE_BINARY and MARKED_MODULE_PATH to use Marked."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from lxml import html

from note_bridge.models import Span
from note_bridge.richtext import markdown_span


@pytest.fixture(scope="module")
def render_markdown():
    module = os.environ.get("MARKED_MODULE_PATH")
    node = os.environ.get("NODE_BINARY")
    if not module or not node:
        pytest.skip("Independent Markdown rendering requires NODE_BINARY and MARKED_MODULE_PATH")
    module_path = Path(module).resolve()
    assert module_path.is_file(), "The configured independent Markdown parser must exist"
    script = (
        'import fs from "node:fs"; const {marked} = await import('
        + json.dumps(module_path.as_uri())
        + '); process.stdout.write(JSON.stringify(marked.parse('
        + 'JSON.parse(fs.readFileSync(0, "utf8")), {gfm:true})));'
    )

    def render(source):
        result = subprocess.run(
            [node, "--input-type=module", "-e", script],
            input=json.dumps(source), text=True, encoding="utf-8", capture_output=True,
            check=True, timeout=20,
        )
        return html.fragment_fromstring(json.loads(result.stdout), create_parent="div")

    return render


@pytest.mark.parametrize("text", ["正文 ", " 正文", "\t正文\n末尾\t", "  "])
@pytest.mark.parametrize("marks", [
    {"bold": True}, {"italic": True}, {"bold": True, "italic": True}, {"strike": True},
])
def test_emphasis_keeps_original_boundary_whitespace(render_markdown, text, marks):
    span = Span(text=text, **marks)
    tree = render_markdown("before" + markdown_span(span) + "after")
    assert tree[0].text_content() == "before" + text + "after", "Fixing emphasis must not trim source text"
    for mark, tag in (("bold", "strong"), ("italic", "em"), ("strike", "del")):
        nodes = tree.xpath(".//" + tag)
        assert len(nodes) == int(marks.get(mark, False))
        if nodes:
            assert nodes[0].text_content() == text, "All original characters must retain their emphasis"


@pytest.mark.parametrize("code", [False, True])
def test_spaced_emphasis_keeps_combined_marks_safe_text_and_link(render_markdown, code):
    text = "  <tag>& *literal* [label] `x`  "
    url = "https://example.com/notes?q=one&lang=zh"
    span = Span(text=text, bold=True, italic=True, strike=True, underline=True,
                highlight=True, code=code, link=url)
    tree = render_markdown("before" + markdown_span(span) + "after")
    assert tree[0].text_content() == "before" + text + "after"
    assert not tree.xpath(".//tag"), "Literal HTML must remain text inside combined style wrappers"
    for tag in ("strong", "em", "del", "u", "mark", "a", *(("code",) if code else ())):
        nodes = tree.xpath(".//" + tag)
        assert len(nodes) == 1 and nodes[0].text_content() == text, "Combined styles must cover the same text"
    assert tree.xpath(".//a/@href") == [url]


def test_independent_parser_exposes_original_trailing_space_loss(render_markdown):
    broken = render_markdown("before**正文 **after")
    assert not broken.xpath(".//strong"), "This regression must exercise a real Markdown delimiter failure"
    fixed = render_markdown("before" + markdown_span(Span(text="正文 ", bold=True)) + "after")
    assert fixed.xpath(".//strong/text()") == ["正文 "]
    assert not render_markdown("before~~正文 ~~after").xpath(".//del")
    fixed_strike = render_markdown("before" + markdown_span(Span(text="正文 ", strike=True)) + "after")
    assert fixed_strike.xpath(".//del/text()") == ["正文 "]
