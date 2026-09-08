"""OPPO must render italics, rather than accepting EM as a normal-font custom mark."""
from threading import Event
from types import SimpleNamespace

from lxml import html

from note_bridge.models import Block, NoteDocument, PlatformId, Span
from note_bridge.providers.oppo import OppoProvider, parse_entry
from note_bridge.richtext import span_html


def test_official_italic_tag_preserves_nested_marks_and_literal_tag_text(tmp_path):
    provider = OppoProvider(SimpleNamespace(session=SimpleNamespace(headers={})), tmp_path)
    provider.account_id = "synthetic-target"
    provider.write_supported = True
    spans = [Span(text="literal <em> <mark> "), Span(text="italic", italic=True, bold=True),
             Span(text=" combined", italic=True, underline=True, highlight=True, link="https://example.com/")]
    note = NoteDocument(platform=PlatformId.WPS, account_id="synthetic-source", source_id="one",
                        blocks=[Block(spans=spans), Block(kind="heading", level=2, spans=spans)])
    sent = []

    def create(path, payload, **kwargs):
        assert path == "/web/note/v2/add" and kwargs["write"] is True
        sent.append(payload)
        return {"recordId": "synthetic-created"}

    provider._json = create
    context = SimpleNamespace(check_cancel=lambda: None, cancelled=Event())
    provider.create(note, context)
    assert len(sent) == 1
    root = html.fragment_fromstring(sent[0]["rawText"], create_parent="div")
    assert not root.xpath(".//em") and len(root.xpath(".//i")) == 4
    assert len(root.xpath(".//i/strong")) == 2 and len(root.xpath('.//a/span[@class="highlight_color_yellow"]/u/i')) == 2
    assert not root.xpath('.//mark') and len(root.xpath('.//span[contains(@style,"background-color")]')) == 2
    assert "literal <em>" in root.text_content(), "Literal user text must not be rewritten as markup."
    restored = parse_entry({"recordId": "synthetic-created", "status": 0, **sent[0]}, "synthetic-target", {})
    for block in restored.blocks[:2]:
        assert any(span.italic and span.bold and span.text == "italic" for span in block.spans)
        assert any(span.italic and span.underline and span.highlight and span.text == " combined" for span in block.spans)
    assert not restored.warnings and "literal <em> <mark>" in root.text_content()
    assert span_html(Span(text="local export", italic=True)) == "<em>local export</em>", "Other platforms and exports keep their existing markup."


def test_divider_has_official_style_and_code_visual_losses_are_reported_without_losing_export_semantics(tmp_path):
    provider = OppoProvider(SimpleNamespace(session=SimpleNamespace(headers={})), tmp_path)
    provider.account_id, provider.write_supported = "synthetic-target", True
    note = NoteDocument(platform=PlatformId.WPS, account_id="synthetic-source", source_id="code",
        blocks=[Block(kind="divider"), Block(kind="code", spans=[Span(text="  a < b\n    <em>literal</em>")]),
                Block(spans=[Span(text="inline", code=True)])])
    sent = []

    def create(path, payload, **kwargs):
        assert path == "/web/note/v2/add" and kwargs["write"] is True
        sent.append(payload)
        return {"recordId": "synthetic-created"}

    provider._json = create
    created = provider.create(note, SimpleNamespace(check_cancel=lambda: None, cancelled=Event()))
    assert len(created.warnings) == 2 and all("普通文字" in warning for warning in created.warnings)
    root = html.fragment_fromstring(sent[0]["rawText"], create_parent="div")
    assert root.xpath('.//hr/@class') == ["hr-style-solid"]
    assert root.xpath('.//pre/code')[0].text_content() == note.blocks[1].text
    restored = parse_entry({"recordId": "synthetic-created", "status": 0, **sent[0]}, "synthetic-target", {})
    assert restored.blocks[:3] == note.blocks, "Cloud display degradation must not erase the raw code semantics used by local exports."
