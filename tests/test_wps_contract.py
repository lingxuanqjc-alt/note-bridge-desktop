import base64
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError, WriteUncertain
from note_bridge.models import Block, NoteDocument, PlatformId, Span, TaskStatus
from note_bridge.operations import migrate, receipt_key
from note_bridge.providers.wps import WpsCodec, WpsProvider, decode_body, encode_body
from note_bridge.storage import Store
from note_bridge.tasks import TaskRunner


def source_note():
    return NoteDocument(platform=PlatformId.MEIZU, account_id="source", source_id="existing-source",
                        title="中文😀", blocks=[Block(spans=[Span(text="hello "), Span(text="bold", bold=True),
                        Span(text=" and "), Span(text="both", italic=True, underline=True)]),
                        Block(kind="todo", checked=True, spans=[Span(text="done")]),
                        Block(kind="todo", spans=[Span(text="pending")]),
                        Block(kind="list", ordered=True, spans=[Span(text="first")])])


def test_wps_dialect_preserves_supported_styles_tasks_and_literal_markers():
    note = source_note()
    body, warnings = encode_body(note)
    restored, assets = decode_body(body)
    assert restored[:len(note.blocks)] == note.blocks and not assets and not warnings
    literal = r"## *literal* ~no underline~ 0. [x] C:\notes\file"
    note.blocks = [Block(spans=[Span(text=literal)])]
    body, warnings = encode_body(note)
    restored, _ = decode_body(body)
    assert restored[0].kind == "paragraph" and restored[0].text == literal
    assert all(not span.bold and not span.italic and not span.underline for span in restored[0].spans)


def test_wps_unsupported_structure_is_reported_and_media_is_rejected_before_writing():
    note = source_note()
    note.blocks = [Block(kind="table", rows=[["a", "b"]]),
                   Block(spans=[Span(text="marked", highlight=True)])]
    body, warnings = encode_body(note)
    assert "a\tb" in body and len(warnings) == 2
    note.blocks = [Block(kind="attachment", attachment_id="unavailable")]
    with pytest.raises(BridgeError):
        encode_body(note)


@pytest.mark.parametrize("address", ["https://example.org/a_0?q=*x*#part", "mailto:notes@example.org"])
def test_named_link_keeps_address_and_label_marks_without_marking_the_address(address):
    note = source_note()
    note.blocks = [Block(spans=[Span(text="资料", bold=True, italic=True, underline=True, link=address)])]
    body, warnings = encode_body(note)
    restored, _ = decode_body(body)
    assert restored[0].spans[0] == Span(text="资料", bold=True, italic=True, underline=True)
    address_spans = restored[0].spans[1:]
    assert "".join(span.text for span in address_spans) == " (" + address + ")"
    assert all(span == Span(text=span.text) for span in address_spans)
    assert warnings == ["WPS 超链接已转换为链接文字及明文地址，原链接样式未保留。"]


def test_visible_url_is_not_duplicated_during_link_conversion():
    note = source_note()
    address = "https://example.org/path"
    note.blocks = [Block(spans=[Span(text=address, link=address, bold=True)])]
    restored, _ = decode_body(encode_body(note)[0])
    assert restored[0].spans == [Span(text=address, bold=True)]


@pytest.mark.parametrize("address", ["javascript:alert(1)", "https://example.org/\nunsafe", "file:///private"])
def test_unsafe_link_stops_preflight_before_any_cloud_request(tmp_path, address):
    provider, calls = provider_for_test(tmp_path)
    note = source_note()
    note.blocks = [Block(spans=[Span(text="链接", link=address)])]
    with pytest.raises(BridgeError) as error:
        provider.preflight([note])
    assert error.value.code == "unsupported_link"
    assert calls == []


def provider_for_test(tmp_path, fail_at=None):
    calls = []

    def response(method, path, **kwargs):
        calls.append((path, kwargs))
        if path == fail_at:
            raise BridgeError("rate_limited", "fixture rejection")
        return {
            "/api/v3/groups/special": {"id": 99},
            "/api/v3/groups/99/files/new_empty": {"id": 101},
            "/notesvr/set/noteinfo": {"infoVersion": 1},
            "/notesvr/set/notecontent": {"contentVersion": 1},
        }[path]

    transport = SimpleNamespace(json=response)
    provider = WpsProvider(transport, None, tmp_path, transport)
    provider.account_id, provider.write_supported = "target", True
    provider._codec = SimpleNamespace(encrypt=lambda value: base64.b64encode(value.encode()).decode())
    return provider, calls


def test_wps_three_step_create_keeps_a_durable_new_id_and_never_overwrites_source(tmp_path):
    provider, calls = provider_for_test(tmp_path)
    store = Store(tmp_path / "state.sqlite")
    runner, note = TaskRunner(store), source_note()
    runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
    runner.join()
    assert runner.current().status == TaskStatus.SUCCEEDED
    receipt = store.receipt(receipt_key(note, provider))
    guid = receipt["remote_ids"][0]
    assert receipt["status"] == "confirmed" and len(guid) == 32 and guid != note.source_id
    assert calls[1][1]["json"]["storeid"] == calls[2][1]["json"]["noteId"] == guid
    assert calls[3][1]["json"]["localContentVersion"] == 0
    assert calls[1][1]["headers"]["Origin"] == "https://note.wps.cn"
    assert all(kwargs["write"] for _, kwargs in calls[1:])


@pytest.mark.parametrize("fail_at", ["/notesvr/set/noteinfo", "/notesvr/set/notecontent"])
def test_partial_wps_creation_stays_uncertain_with_its_id_and_is_never_retried(tmp_path, fail_at):
    provider, calls = provider_for_test(tmp_path, fail_at)
    store = Store(tmp_path / "state.sqlite")
    runner, note = TaskRunner(store), source_note()
    for _ in range(2):
        runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
        runner.join()
        assert runner.current().status == TaskStatus.NEEDS_REVIEW
        receipt = store.receipt(receipt_key(note, provider))
        assert receipt["status"] == "uncertain" and len(receipt["remote_ids"][0]) == 32
    assert sum(path.endswith("/new_empty") for path, _ in calls) == 1


def test_process_recovery_preserves_the_prepared_remote_id(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    store.save_receipt("fixture-key", "sending", ["prepared-id"])
    store.recover_interrupted()
    assert store.receipt("fixture-key") == {"status": "uncertain", "remote_ids": ["prepared-id"]}


def test_wps_invalid_write_acknowledgement_is_uncertain(tmp_path):
    provider = WpsProvider(SimpleNamespace(json=lambda *args, **kwargs: {"errorCode": "unknown"}), None, tmp_path)
    with pytest.raises(WriteUncertain):
        provider._json("POST", "set/notecontent", write=True)


def test_wps_cipher_keeps_unicode_and_clears_key_material():
    # Fixed synthetic AES key; no account configuration is embedded in this test.
    codec = WpsCodec.__new__(WpsCodec)
    codec._key = b"fixture-key-1234"
    text = "中文😀 *body* ~underlined~"
    assert codec.decrypt(codec.encrypt(text)) == text
    codec.close()
    assert codec._key == b""
