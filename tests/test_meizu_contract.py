"""Synthetic edge cases protect the real Flyme format from silent loss and account mixing."""

import json

import pytest

from note_bridge.errors import BridgeError, WriteUncertain
from note_bridge.models import Block, NoteDocument, PlatformId, Span
from note_bridge.providers.meizu import MeizuProvider, encode_blocks, parse_entry
from note_bridge.storage import Store
from note_bridge.tasks import TaskRunner


def entry(body, **changes):
    return {
        "uuid": "fixture",
        "userId": 0,
        "body": json.dumps(body, ensure_ascii=False),
        "createTime": 1720000000000,
        "modifyTime": 1720000001000,
        "encrypt": 0,
        **changes,
    }


def test_divider_degradation_is_visible_in_migration_result():
    note = NoteDocument(platform=PlatformId.VIVO, account_id="source", source_id="divider",
                        blocks=[Block(kind="divider")])
    encoded, warnings = encode_blocks(note)
    restored = parse_entry(entry(encoded), "account")
    assert restored.blocks[0].kind == "paragraph"
    assert "分隔线转换为普通文字。" in warnings, "Text substitutes must not silently claim the original structure survived."


def test_emoji_does_not_shift_following_style_ranges():
    note = parse_entry(
        entry(
            [
                {
                    "state": 0,
                    "text": "😀标题",
                    "span": json.dumps(
                        [
                            {"span": 1, "param": 1, "start": 2, "end": 4},
                            {"span": 7, "param": 2, "start": 3, "end": 4},
                        ]
                    ),
                }
            ]
        ),
        "account",
    )
    assert note.plain_text == "😀标题"
    assert not note.blocks[0].spans[0].bold
    assert note.blocks[0].spans[-1].text == "题"
    assert note.blocks[0].spans[-1].bold and note.blocks[0].spans[-1].italic


def test_nested_lists_and_todos_preserve_structure():
    note = parse_entry(
        entry(
            [
                {"state": 2, "text": "已完成"},
                {
                    "state": 50,
                    "children": [
                        {
                            "state": 53,
                            "children": [
                                {"state": 0, "text": "第一项"},
                                {"state": 51, "children": [{"state": 0, "text": "子项"}]},
                            ],
                        }
                    ],
                },
            ]
        ),
        "account",
    )
    assert note.blocks[0].checked
    assert note.blocks[1].ordered and note.blocks[1].level == 1
    assert not note.blocks[2].ordered and note.blocks[2].level == 2


@pytest.mark.parametrize(
    "body",
    [
        [{"state": 99, "text": "未知"}],
        [{"state": 0, "text": "😀", "span": '[{"span":1,"param":1,"start":1,"end":2}]'}],
    ],
)
def test_unsupported_blocks_and_broken_unicode_ranges_cannot_be_reported_as_success(body):
    with pytest.raises(BridgeError):
        parse_entry(entry(body), "account")


def test_session_identity_is_rechecked_when_server_redacts_note_owners(tmp_path):
    provider = MeizuProvider(None, tmp_path)
    probe_calls = []

    def response(path, **kwargs):
        if path == "gettags":
            probe_calls.append(1)
            return {"userId": 123 if len(probe_calls) == 1 else 456, "data": []}
        return {"count": 1, "content": [entry([{"state": 0, "text": "测试"}])]}

    provider._json = response
    provider.probe()
    runner = TaskRunner(Store(tmp_path / "test.sqlite"))
    runner.start("fetch", provider.fetch)
    runner.join()
    assert any(issue.code == "account_changed" for issue in runner.current().issues)
    assert len(probe_calls) == 2


def test_new_note_encoding_preserves_emoji_style_offsets_and_both_todo_states():
    source = NoteDocument(platform=PlatformId.XIAOMI, account_id="synthetic", source_id="source-id",
                          blocks=[Block(spans=[Span(text="😀"), Span(text="中文", bold=True, italic=True, underline=True)]),
                                  Block(kind="todo", checked=True, spans=[Span(text="done")]),
                                  Block(kind="todo", spans=[Span(text="pending")]),
                                  Block(kind="list", ordered=True, spans=[Span(text="first")]),
                                  Block(kind="list", ordered=True, spans=[Span(text="second")])])
    body, warnings = encode_blocks(source)
    styles = json.loads(body[0]["span"])
    assert {(s["span"], s["param"]) for s in styles} == {(1, 1), (7, 2), (6, "")}
    assert all(s["start"] == 2 and s["end"] == 4 for s in styles)
    assert body[1]["state"] == 2 and body[2]["state"] == 1
    assert len(body[3]["children"]) == 2, "Adjacent numbered items must remain one list."
    restored = parse_entry(entry(body), "target")
    assert restored.plain_text.startswith(source.plain_text)
    assert not warnings


def test_missing_create_receipt_is_uncertain_and_never_sent_twice(tmp_path):
    from types import SimpleNamespace

    provider = MeizuProvider(None, tmp_path)
    provider.account_id, provider.write_supported = "synthetic-target", True
    requests = []

    def response(path, **kwargs):
        requests.append((path, kwargs))
        return {}

    provider._json = response
    note = NoteDocument(platform=PlatformId.XIAOMI, account_id="source", source_id="must-not-update-this-id")
    with pytest.raises(WriteUncertain):
        provider.create(note, SimpleNamespace(check_cancel=lambda: None))
    assert len(requests) == 1 and requests[0][1]["write"] is True
    assert "uuid" not in requests[0][1]["data"], "A source identifier must never update a target's existing note."
