"""Independent server-side envelope checks and loss/account/pagination protections."""

import base64
import struct
import zlib
from types import SimpleNamespace

import pytest
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad

from note_bridge.bridge import Bridge
from note_bridge.errors import BridgeError
from note_bridge.models import Attachment, PlatformId, TaskStatus, account_fingerprint
from note_bridge.operations import fetch_snapshot
from note_bridge.paths import AppPaths
from note_bridge.providers.base import SPECS, CreatedNote
from note_bridge.providers.transport import Transport
from note_bridge.providers.vivo import VivoProvider, parse_entry
from note_bridge.providers.vivo_wire import VivoWire
from note_bridge.storage import Store
from note_bridge.tasks import TaskRunner


def test_server_can_unwrap_client_key_and_return_unicode_without_shared_private_key():
    server = RSA.generate(2048)
    wire = VivoWire(server.public_key().export_key())
    payload = "中文😀 / test"
    raw = base64.urlsafe_b64decode(wire.encrypt(payload) + "==")
    header_size = int.from_bytes(raw[:2], "big")
    assert int.from_bytes(raw[2:10], "big") == zlib.crc32(raw[10:header_size])
    assert struct.unpack(">HHH", raw[10:16]) == (32, 203, 1)
    token_len = raw[17]
    assert raw[18 : 18 + token_len] == b"com.android.notes"
    iv = raw[18 + token_len : 34 + token_len]
    key_start = 36 + token_len
    key = PKCS1_v1_5.new(server).decrypt(raw[key_start:header_size], b"invalid")
    assert unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(raw[header_size:]), 16).decode() == payload
    response = raw[:header_size] + AES.new(key, AES.MODE_CBC, iv).encrypt(pad("测试响应".encode(), 16))
    assert wire.decrypt(base64.urlsafe_b64encode(response).decode()) == "测试响应"
    corrupted = bytearray(response)
    corrupted[15] ^= 1
    with pytest.raises(BridgeError, match="解码"):
        wire.decrypt(base64.urlsafe_b64encode(corrupted).decode())
    wire.close()


def note_row(**changes):
    return {
        "guid": "fixture",
        "userId": "fixture-user",
        "type": 1,
        "deleted": 1,
        "encryptType": 0,
        "createTime": 1720000000000,
        "updateTime": 1720000001000,
        "noteBookGuid": "folder",
        **changes,
    }


def test_regular_html_retains_text_style_folder_and_missing_media_warning():
    account = account_fingerprint(PlatformId.VIVO, "fixture-user")
    note = parse_entry(
        note_row(), '<p><b>中文😀</b></p><img src="resource-guid">', account, {"folder": "学习"}
    )
    assert note.blocks[0].spans[0].bold
    assert note.blocks[0].text == "中文😀" and note.source_folder_name == "学习"
    assert note.warnings, "An unread image must prevent a misleading complete export."


@pytest.mark.parametrize(
    "changes", [{"userId": "another-user"}, {"type": 2}, {"encryptType": 1}, {"deleted": 0}]
)
def test_foreign_locked_or_proprietary_notes_are_not_treated_as_ordinary_text(changes):
    with pytest.raises(BridgeError):
        parse_entry(
            note_row(**changes), "<p>text</p>", account_fingerprint(PlatformId.VIVO, "fixture-user"), {}
        )


def test_directory_must_match_official_count_before_any_content_is_read(tmp_path):
    provider = VivoProvider(None, tmp_path)
    provider.account_id = account_fingerprint(PlatformId.VIVO, "fixture-user")
    calls = []

    def response(method, path, data=None, **kwargs):
        calls.append(path)
        return {
            "/statistics/note": {"totalNotes": 2, "encryptedNotes": 0},
            "/noteBook/getList": [],
            "/note/getAllNote/v2": {"notes": [note_row()]},
        }[path]

    provider._json = response
    runner = TaskRunner(Store(tmp_path / "tasks.sqlite"))
    runner.start("fetch", provider.fetch)
    runner.join()
    assert runner.current().issues[0].code == "pagination_incomplete"
    assert "/note/getContent/v2" not in calls


@pytest.mark.parametrize("body,complete", [
    ('<p style="font-family:sans-serif;font-size:16px">正文</p><vnote-divider></vnote-divider>', True),
    ('<p>正文</p><img src="missing.png">', False),
    ('<vnote-doc filename="missing.pdf"></vnote-doc>', False),
    ('<vnote-todo><todo-item done="unknown">正文</todo-item></vnote-todo>', False),
    ('<vnote-handwriting>正文</vnote-handwriting>', False),
    ('<table><tr><td><img src="missing.png">正文</td></tr></table>', False),
    ('<p>正文</p><iframe src="https://example.invalid"></iframe>', False),
])
def test_layout_only_downgrades_do_not_claim_missing_notes_or_hide_content_loss(
    tmp_path, monkeypatch, body, complete,
):
    provider = VivoProvider(Transport("https://pc.vivo.com.cn", ("vivo.com.cn",)), tmp_path)
    provider.account_id = account_fingerprint(PlatformId.VIVO, "fixture-user")
    provider.probe = lambda: provider.account_id
    responses = {
        "/statistics/note": {"totalNotes": 1, "encryptedNotes": 0},
        "/noteBook/getList": [],
        "/note/getAllNote/v2": {"notes": [note_row()]},
        "/note/getContent/v2": body,
        "/note/getIncludeItem/v2": {"guid": "fixture", "resources": []},
    }
    monkeypatch.setattr(VivoProvider, "_json", lambda self, method, path, data=None, **kwargs: responses[path])
    paths = AppPaths(tmp_path / "app")
    paths.prepare()
    bridge = Bridge(paths)
    store, runner = bridge._store, bridge._runner
    runner.start("fetch", lambda ctx: fetch_snapshot(provider, store, ctx))
    runner.join(3)
    report = runner.current()
    assert store.snapshot(PlatformId.VIVO, provider.account_id) == {"complete": complete, "count": 1}
    assert report.status == TaskStatus.PARTIAL, "Even permitted layout downgrades must remain visible."
    assert any(issue.code == "source_warning" for issue in report.issues)

    assert any(issue.code == "incomplete_snapshot" for issue in report.issues) is not complete
    assert store.notes(PlatformId.VIVO, provider.account_id)[0].warnings

    created = []

    def create(note, context):
        created.append(note.source_id)
        return CreatedNote(["synthetic-target-note"])

    bridge._providers[PlatformId.VIVO] = provider
    bridge._providers[PlatformId.WPS] = SimpleNamespace(
        spec=SPECS[PlatformId.WPS], account_id="synthetic-target-account",
        write_supported=True, preflight=lambda notes: None, prepare_migration=lambda: None, create=create,
    )
    assert bridge.migrate_notes({"source": "vivo", "target": "wps"})["ok"]
    runner.join(3)
    report = runner.current()
    assert created == (["fixture"] if complete else [])
    assert report.succeeded == int(complete)
    assert report.status == (TaskStatus.PARTIAL if complete else TaskStatus.FAILED)
    assert any(issue.code == "source_incomplete" for issue in report.issues) is not complete
    assert any(issue.code == "source_warning" for issue in report.issues)
    provider.close()


def test_custom_media_and_todos_keep_their_position_and_completion_state():
    account = account_fingerprint(PlatformId.VIVO, "fixture-user")
    note = parse_entry(
        note_row(resources=True),
        '<p>before</p><vnote-image filename="prefix-asset-a.png"></vnote-image>'
        '<vnote-todo><todo-item done="true"><b>done</b></todo-item>'
        '<todo-item done="false">pending</todo-item></vnote-todo>'
        '<vnote-divider></vnote-divider><p>after</p>',
        account, {}, [Attachment(id="asset-a", name="image.png", kind="image", local_path="fixture.png")],
    )
    assert [b.kind for b in note.blocks] == ["paragraph", "attachment", "todo", "todo", "divider", "paragraph"]
    assert note.blocks[1].attachment_id == "asset-a"
    assert note.blocks[2].checked and not note.blocks[3].checked
    assert note.blocks[2].spans[0].bold
    assert note.blocks[-1].text == "after"
    assert note.warnings == ["vivo 装饰分隔线已转换为普通分隔线。"]


def test_unknown_todo_state_and_unmatched_media_cannot_be_silent_successes():
    note = parse_entry(note_row(), '<vnote-todo><todo-item done="new-state">task</todo-item></vnote-todo>'
                       '<vnote-doc filename="unavailable.pdf"></vnote-doc>',
                       account_fingerprint(PlatformId.VIVO, "fixture-user"), {})
    assert len(note.warnings) == 2
    assert "task" in note.plain_text and "附件未获取" in note.plain_text


@pytest.mark.parametrize("field,value", [("userId", "different-user"), ("noteGuid", "different-note")])
def test_attachment_ownership_is_checked_before_any_download(tmp_path, field, value):
    provider = VivoProvider(None, tmp_path)
    provider.account_id = account_fingerprint(PlatformId.VIVO, "fixture-user")
    resource = {"guid": "asset", "userId": "fixture-user", "noteGuid": "fixture", field: value}
    with pytest.raises(BridgeError) as error:
        provider._attachment(resource, "fixture", None)
    assert error.value.code == "attachment_mismatch"


def test_create_uses_a_fresh_guid_and_roundtrips_style_and_todo_state(tmp_path):
    import threading
    from types import SimpleNamespace

    from note_bridge.models import Block, NoteDocument, Span

    provider = VivoProvider(None, tmp_path)
    provider.account_id = account_fingerprint(PlatformId.VIVO, "fixture-user")
    provider.write_supported = True
    captured = []

    def response(method, path, data, **kwargs):
        if path == "/sync/getSyncState":
            return {"updateCount": 7}
        assert path == "/sync/createSync/v2" and kwargs["write"] and kwargs["encrypted"]
        captured.append(data)
        return {"updateCount": 8}

    provider._json = response
    source = NoteDocument(platform=PlatformId.MEIZU, account_id="source", source_id="never-reuse",
                          blocks=[Block(kind="heading", level=1, spans=[Span(text="中文😀", bold=True)]),
                                  Block(kind="todo", checked=True, spans=[Span(text="done")]),
                                  Block(kind="todo", spans=[Span(text="pending")])])
    intents = []
    created = provider.create(source, SimpleNamespace(check_cancel=lambda: None, cancelled=threading.Event(),
                                                      record_remote_ids=intents.append))
    row = captured[0]["notes"][0]
    assert row["guid"] == created.remote_ids[0] and row["guid"] != source.source_id and len(row["guid"]) == 32
    assert intents == [[row["guid"]]]
    assert captured[0]["noteBooks"] == captured[0]["resources"] == captured[0]["tags"] == []
    decoded = parse_entry({**row, "userId": "fixture-user"}, row["content"], provider.account_id, {})
    assert decoded.blocks[:3] == source.blocks
