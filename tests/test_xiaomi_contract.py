"""Synthetic protocol-contract checks, awaiting authenticated official-site evidence."""

import json

import pytest

from note_bridge.errors import BridgeError
from note_bridge.providers.xiaomi import parse_entry


def test_legacy_payload_preserves_title_time_style_and_media_references():
    entry = {
        "id": "example",
        "content": '<b>中文 😀</b>\n<img fileid="image"/>',
        "createDate": 1720000000000,
        "modifyDate": 1720000001000,
        "folderId": "folder",
        "extraInfo": json.dumps({"title": "标题"}),
        "setting": {"data": [{"fileId": "image", "mimeType": "image/png", "fileName": "图.png"}]},
    }
    note = parse_entry(entry, "opaque-test-account", {"folder": "分组"})
    assert note.title == "标题" and note.created_at.tzinfo is not None
    assert note.source_folder_name == "分组"
    assert note.blocks[0].spans[0].bold and note.blocks[-1].attachment_id == "image"


@pytest.mark.parametrize(
    "change",
    [{"encryptInfo": {"version": 1}}, {"extraInfo": '{"note_content_type":"mind"}'}, {"content": None}],
)
def test_unknown_or_encrypted_content_must_not_become_an_empty_success(change):
    with pytest.raises(BridgeError):
        parse_entry({"id": "example", "content": "text", **change}, "account", {})


def test_each_legacy_line_keeps_its_own_checkbox_and_text_style():
    note = parse_entry(
        {
            "id": "fixture",
            "content": '<input type="checkbox"/><b>第一项</b>\n<input type="checkbox" checked="true"/><i>第二项</i>\n<bullet/>第三项',
        },
        "account",
        {},
    )
    assert [(b.kind, b.checked, b.text) for b in note.blocks] == [
        ("todo", False, "第一项"),
        ("todo", True, "第二项"),
        ("list", False, "第三项"),
    ]
    assert note.blocks[0].spans[-1].bold and note.blocks[1].spans[-1].italic


def test_created_legacy_note_has_valid_checkboxes_and_source_time_at_the_end(tmp_path):
    from types import SimpleNamespace

    from note_bridge.models import Block, NoteDocument, PlatformId, Span, account_fingerprint
    from note_bridge.providers.xiaomi import XiaomiProvider

    provider = XiaomiProvider(SimpleNamespace(cookie=lambda _: "synthetic-session",
        json=lambda *a, **k: {"code": 0, "data": {"e2eeStatus": "close"}}), tmp_path)
    provider.account_id, provider.write_supported = account_fingerprint(PlatformId.XIAOMI, "synthetic-session"), True
    provider.refresh_write_mode()
    captured = []

    def response(method, path, **kwargs):
        captured.append(json.loads(kwargs["data"]["entry"]))
        assert kwargs["write"] is True and path == "/note/note"
        return {"entry": {"id": "new-target-id"}}

    provider._json = response
    source = NoteDocument(platform=PlatformId.MEIZU, account_id="source", source_id="source-id",
                          title="new note", blocks=[Block(kind="todo", checked=True, spans=[Span(text="done")]),
                                                     Block(kind="todo", spans=[Span(text="pending")])])
    result = provider.create(source, SimpleNamespace(check_cancel=lambda: None))
    parsed = parse_entry({**captured[0], "id": result.remote_ids[0]}, "target", {})
    tasks = [b for b in parsed.blocks if b.kind == "todo"]
    assert tasks == source.blocks
    assert parsed.plain_text.index("来源笔记时间") > parsed.plain_text.index("pending")
    assert 'checked="true"' in captured[0]["content"] and "chr(34)" not in captured[0]["content"]
    assert "id" not in captured[0], "Creation cannot reuse or overwrite a target note id."


@pytest.mark.parametrize("newlines,empty_paragraphs", [("\n", 0), ("\n\n", 1), ("\n\n\n", 2)])
def test_image_line_separator_is_not_an_extra_paragraph_but_intentional_blank_lines_survive(newlines, empty_paragraphs):
    note = parse_entry({"id": "fixture", "content": '<b>before</b>\n<img fileid="image"/>' + newlines + '<i>after</i>',
                        "setting": {"data": [{"fileId": "image", "mimeType": "image/png"}]}}, "account", {})
    assert sum(b.kind == "paragraph" and not b.spans for b in note.blocks) == empty_paragraphs
    assert note.blocks[0].spans[0].bold and note.blocks[-1].spans[0].italic
    assert [b.text for b in note.blocks if b.spans] == ["before", "after"]


@pytest.mark.parametrize("value,expected", [('', False), (' checked', False),
    (' checked="checked"', False), (' checked="false"', False), (' checked="True"', False),
    (' checked="true"', True)])
def test_checkbox_uses_native_string_contract_instead_of_html_boolean_presence(value, expected):
    note = parse_entry({"id": "fixture", "content": f'<input type="checkbox"{value}/>item'}, "account", {})
    assert note.blocks[0].kind == "todo"
    assert note.blocks[0].checked is expected, "Wrong completion can silently change the meaning of a migrated task."


def test_encoder_uses_official_native_marks_not_generic_html_aliases():
    from lxml import html

    from note_bridge.models import Span
    from note_bridge.providers.xiaomi import legacy_span

    markup = legacy_span(Span(text='<script>& 😀', bold=True, italic=True, underline=True,
                              strike=True, highlight=True))
    root = html.fragment_fromstring(markup)
    assert {node.tag for node in root.iter()} == {'background', 'delete', 'u', 'i', 'b'}
    assert root.get('color') == '#9affe8af'
    assert ''.join(root.itertext()) == '<script>& 😀'


def test_reader_accepts_native_combined_marks_from_independent_contract_sample():
    note = parse_entry({'id': 'fixture', 'content':
        '<background color="#9affe8af"><delete><u><i><b>原生样式</b></i></u></delete></background>'}, 'account', {})
    span = note.blocks[0].spans[0]
    assert span.text == '原生样式'
    assert span.bold and span.italic and span.underline and span.strike and span.highlight


@pytest.mark.parametrize('tag', ['strong', 'em', 's', 'strike', 'mark', 'code'])
def test_generic_html_aliases_cannot_falsely_pass_native_style_readback(tag):
    note = parse_entry({'id': 'fixture', 'content': f'<{tag}>text</{tag}>'}, 'account', {})
    span = note.blocks[0].spans[0]
    assert span.text == 'text'
    assert not any((span.bold, span.italic, span.strike, span.highlight, span.code))
    assert note.warnings
