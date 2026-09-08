"""Native grammar samples independent of the application's writer."""
from note_bridge.providers.xiaomi_markup import parse_legacy


def test_native_empty_heading_markers_do_not_swallow_following_paragraphs():
    blocks, _, _ = parse_legacy('<new-format/><h2/><b>Heading</b>\nbody\n<h3/>subheading\nend')
    assert [(b.kind, b.level, b.text) for b in blocks] == [
        ('heading', 2, 'Heading'), ('paragraph', 1, 'body'),
        ('heading', 3, 'subheading'), ('paragraph', 1, 'end')]
    assert blocks[0].spans[0].bold


def test_native_order_and_indent_are_not_downgraded_to_flat_bullets():
    blocks, _, _ = parse_legacy('<order indent="1" inputNumber="1"/>first\n'
        '<order indent="2" inputNumber="1"/><i>nested</i>\n<bullet indent="3"/>bullet')
    assert [(b.kind, b.ordered, b.level, b.text) for b in blocks] == [
        ('list', True, 1, 'first'), ('list', True, 2, 'nested'), ('list', False, 3, 'bullet')]
    assert blocks[1].spans[0].italic


def test_quote_and_rule_are_structural_without_extra_blank_paragraphs():
    blocks, _, _ = parse_legacy('before\n<quote><text indent="1"><b>quote</b></text></quote>\n<hr/>\nafter')
    assert [(b.kind, b.text) for b in blocks] == [
        ('paragraph', 'before'), ('quote', 'quote'), ('divider', ''), ('paragraph', 'after')]
    assert blocks[1].spans[0].bold


def test_literal_check_symbols_do_not_turn_ordinary_text_into_tasks():
    blocks, _, _ = parse_legacy('☑ literal\n• literal\n<del>old completed task</del>')
    assert [(b.kind, b.checked, b.text) for b in blocks] == [
        ('paragraph', False, '☑ literal'), ('paragraph', False, '• literal'),
        ('todo', True, 'old completed task')]


def test_list_wrappers_keep_nested_depth_and_following_paragraph():
    blocks, _, _ = parse_legacy('<ol><li>first<ul><li>nested</li></ul></li></ol>\nafter')
    assert [(b.kind, b.ordered, b.level, b.text) for b in blocks] == [
        ('list', True, 1, 'first'), ('list', False, 2, 'nested'), ('paragraph', False, 1, 'after')]


def test_active_elements_cannot_execute_or_contribute_script_text():
    blocks, warnings, _ = parse_legacy('<script>bad()<script>nested()</script></script><b>safe</b>')
    assert [b.text for b in blocks] == ['safe']
    assert blocks[0].spans[0].bold and warnings


def test_native_link_is_lifted_after_paragraph_and_uses_plain_card_title():
    blocks, warnings, _ = parse_legacy('<b>before</b><a href="https://example.com/"><i>title</i></a>after')
    assert [b.text for b in blocks] == ['beforeafter', 'title']
    assert blocks[0].spans[0].bold
    assert blocks[1].spans[0].link == 'https://example.com/'
    assert not blocks[1].spans[0].italic and warnings


def test_old_html_break_cannot_falsely_certify_native_line_preservation():
    blocks, warnings, _ = parse_legacy('one<br/>two')
    assert [b.text for b in blocks] == ['onetwo']
    assert warnings


def test_writer_preserves_native_structures_and_literal_newlines(tmp_path):
    import json
    from types import SimpleNamespace

    from note_bridge.models import Block, NoteDocument, PlatformId, Span, account_fingerprint
    from note_bridge.providers.xiaomi import XiaomiProvider, parse_entry

    note = NoteDocument(platform=PlatformId.VIVO, account_id='source', source_id='sample', title='title', blocks=[
        Block(kind='heading', level=2, spans=[Span(text='heading', bold=True)]),
        Block(kind='list', ordered=True, level=2, spans=[Span(text='nested')]),
        Block(kind='quote', spans=[Span(text='quote', strike=True)]), Block(kind='divider'),
        Block(kind='todo', checked=True, spans=[Span(text='done')]),
        Block(spans=[Span(text='first\n\nlast', italic=True)])])
    provider = XiaomiProvider(SimpleNamespace(cookie=lambda _: 'synthetic',
        json=lambda *a, **k: {'code': 0, 'data': {'e2eeStatus': 'close'}}), tmp_path)
    provider.account_id, provider.write_supported = account_fingerprint(PlatformId.XIAOMI, 'synthetic'), True
    provider.refresh_write_mode()
    captured = []

    def response(method, path, **kwargs):
        assert method == 'POST' and path == '/note/note'
        captured.append(json.loads(kwargs['data']['entry']))
        return {'entry': {'id': 'new'}}

    provider._json = response
    provider.create(note, SimpleNamespace(check_cancel=lambda: None))
    content = captured[0]['content']
    assert content.startswith('<new-format/>')
    assert '<h2/>' in content and '<order indent="2" inputNumber="1"/>' in content
    assert '<quote><delete>quote</delete></quote>' in content and '<hr/>' in content
    assert 'checked="true"' in content and '<br' not in content
    restored = parse_entry({'id': 'new', **captured[0]}, 'target', {})
    assert restored.blocks[1:6] == note.blocks[:5]
    assert [b.text for b in restored.blocks[6:9]] == ['first', '', 'last']
