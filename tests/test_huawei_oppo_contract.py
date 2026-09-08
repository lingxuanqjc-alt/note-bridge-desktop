import base64
import json

import pytest
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad

from note_bridge.errors import BridgeError, WriteUncertain
from note_bridge.providers.huawei import parse_entry as huawei_entry
from note_bridge.providers.oppo import OppoProvider
from note_bridge.providers.oppo import parse_entry as oppo_entry
from note_bridge.providers.oppo_wire import OppoRequest


def test_oppo_response_uses_the_request_key_but_the_server_response_iv():
    server = RSA.generate(2048)
    request = OppoRequest({"fixture": "中文😀"}, server.public_key().export_key())
    key = PKCS1_v1_5.new(server).decrypt(base64.b64decode(request.payload["key"]), b"invalid")
    plaintext = unpad(
        AES.new(key, AES.MODE_CBC, request.payload["iv"].encode()).decrypt(
            base64.b64decode(request.payload["encryptContent"])
        ),
        16,
    )
    assert json.loads(plaintext) == {"fixture": "中文😀"}
    iv = b"server-response!"
    payload = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(b'{"code":0,"data":[]}', 16))
    assert request.decrypt({"iv": iv.decode(), "encryptContent": base64.b64encode(payload).decode()}) == {
        "code": 0,
        "data": [],
    }
    with pytest.raises(BridgeError):
        request.decrypt({"iv": iv.decode(), "encryptContent": "broken"})
    request.close()
    assert request.payload == {}


def test_huawei_cloud_id_remains_distinct_from_legacy_client_id():
    note = huawei_entry(
        {
            "guid": "cloud-fixture",
            "kind": "note",
            "data": json.dumps(
                {
                    "guid": "client-fixture",
                    "content": {
                        "html_content": '<element type="Text"><hw_font size="1.0"><b>标题😀</b></hw_font></element>'
                        '<element type="Bullet">1<hw_font size="1.0">已完成</hw_font></element>',
                        "created": 1720000000000,
                        "modified": 1720000001000,
                        "tag_id": "folder",
                    },
                }
            ),
        },
        "account",
        {"folder": "分组"},
    )
    assert note.source_id == "cloud-fixture" and note.source_folder_name == "分组"
    assert note.blocks[0].spans[0].bold and note.blocks[1].checked
    assert note.blocks[1].text == "已完成"


def test_oppo_media_failure_and_deleted_records_cannot_be_silent_success():
    row = {
        "recordId": "fixture",
        "status": 0,
        "rawText": "<div><b>正文</b></div>",
        "attachments": [{"id": "media"}],
        "groupGuid": "folder",
    }
    note = oppo_entry(row, "account", {"folder": "分组"})
    assert note.blocks[0].spans[0].bold and note.warnings
    with pytest.raises(BridgeError):
        oppo_entry({**row, "status": 1}, "account", {})


def test_editor_wrappers_preserve_task_completion_and_nested_numbered_items():
    from note_bridge.richtext import parse_html

    blocks, warnings = parse_html(
        '<ul data-type="taskList"><li data-type="taskItem" data-checked="true">'
        '<div><p><strong>done</strong></p></div></li><li data-checked="false"><p>pending</p></li></ul>'
        '<ol><li><p>first</p><ol><li><p>nested</p></li></ol></li><li><div>second</div></li></ol>'
    )
    assert not warnings
    assert [(b.kind, b.checked, b.level, b.ordered) for b in blocks] == [
        ("todo", True, 1, False), ("todo", False, 1, False),
        ("list", False, 1, True), ("list", False, 2, True), ("list", False, 1, True),
    ]
    assert blocks[0].spans[0].bold and blocks[2].text == "first"


def test_oppo_create_omits_existing_record_identity_and_roundtrips_rich_text(tmp_path):
    import threading
    from types import SimpleNamespace

    from note_bridge.models import Block, NoteDocument, PlatformId, Span

    transport = SimpleNamespace(session=SimpleNamespace(headers={}))
    provider = OppoProvider(transport, tmp_path)
    provider.account_id, provider.write_supported = "target", True
    calls = []

    def response(path, data, **kwargs):
        assert path == "/web/note/v2/add" and kwargs["write"]
        calls.append(data)
        return {"recordId": "new-target-record"}

    provider._json = response
    source = NoteDocument(platform=PlatformId.MEIZU, account_id="source", source_id="do-not-overwrite",
                          title="中文😀", blocks=[Block(spans=[Span(text="body", bold=True)]),
                          Block(kind="todo", checked=True, spans=[Span(text="done")]),
                          Block(kind="list", ordered=True, spans=[Span(text="first")])])
    created = provider.create(source, SimpleNamespace(check_cancel=lambda: None, cancelled=threading.Event()))
    assert len(calls) == 1 and "recordId" not in calls[0] and "version" not in calls[0]
    assert created.remote_ids == ["new-target-record"] and not created.warnings
    decoded = oppo_entry({**calls[0], "recordId": "new-target-record", "status": 0}, "target", {})
    assert decoded.blocks[:3] == source.blocks and decoded.title == source.title


def test_oppo_unrecognized_write_acknowledgement_is_uncertain_and_not_retryable(tmp_path):
    from types import SimpleNamespace

    calls = []

    def response(*args, **kwargs):
        calls.append(kwargs)
        return {"code": "unknown-vendor-state"}

    provider = OppoProvider(SimpleNamespace(session=SimpleNamespace(headers={}), json=response), tmp_path)
    with pytest.raises(WriteUncertain):
        provider._json("/web/note/v2/add", {"rawTitle": "fixture"}, write=True)
    assert len(calls) == 1 and calls[0]["write"]
