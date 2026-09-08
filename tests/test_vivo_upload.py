"""Prevent lost, repeated, foreign-host, or unlinked image uploads during migration."""

import hashlib
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from note_bridge.errors import BridgeError
from note_bridge.models import (
    Attachment,
    Block,
    NoteDocument,
    PlatformId,
    Span,
    TaskStatus,
    account_fingerprint,
)
from note_bridge.operations import migrate, receipt_key
from note_bridge.providers.vivo import VivoProvider, parse_entry
from note_bridge.providers.vivo_files import file_origin
from note_bridge.storage import Store
from note_bridge.tasks import TaskRunner


def image_note(tmp_path):
    path = tmp_path / "synthetic.png"
    Image.new("RGB", (640, 240), "purple").save(path)
    data = path.read_bytes()
    return NoteDocument(platform=PlatformId.XIAOMI, account_id="source", source_id="source-note",
                        blocks=[Block(spans=[Span(text="before")]),
                                Block(kind="attachment", attachment_id="source-image"),
                                Block(spans=[Span(text="after")])],
                        attachments=[Attachment(id="source-image", name="original.png", kind="image",
                                                mime="image/png", local_path=path.name, size=len(data),
                                                sha256=hashlib.sha256(data).hexdigest())])


def configured_provider(tmp_path, monkeypatch, *, part=False, confirm=False, fail_at=None):
    metadata, files, notes = [], [], []

    def meta_request(method, path, **kwargs):
        metadata.append((path, kwargs))
        if path.endswith("/getStsToken.do"):
            return {"code": 0, "data": {"stsToken": "synthetic-scoped-session"}}
        if path.endswith("/preUpload.do"):
            if fail_at == "space":
                return {"code": 23000, "data": {"remainSpace": 0}}
            return {"code": 0, "data": {"metaId": "allocated-image", "uploadUrl": "http://clouddisk-fixture.vivo.com.cn",
                                        "needUpload": True, "needAsPart": part, "partSize": 200,
                                        "thumbnail": {"shouldCreate": True}}}
        assert path.endswith(("/confirmUpload.do", "/confirmRangeUpload.do"))
        return {"code": 0, "data": None}

    def file_transport(origin, domains):
        assert origin == "https://clouddisk-fixture.vivo.com.cn"
        assert domains == ("clouddisk-fixture.vivo.com.cn",)

        def request(method, path, **kwargs):
            files.append((path, kwargs))
            if fail_at == "file":
                raise BridgeError("rate_limited", "synthetic file rejection")
            return {"code": 25999 if confirm else 0}

        return SimpleNamespace(json=request, close=lambda: None)

    monkeypatch.setattr("note_bridge.providers.vivo_files.Transport", file_transport)
    provider = VivoProvider(SimpleNamespace(json=meta_request,
                                           session=SimpleNamespace(headers={"openId": "synthetic-account"})), tmp_path)
    provider.account_id = account_fingerprint(PlatformId.VIVO, "fixture-user")
    provider.write_supported = provider.images_supported = True

    def note_request(method, path, data, **kwargs):
        if path == "/sync/getSyncState":
            assert files, "The sync cursor is read after upload, so it cannot precede resource allocation."
            return {"updateCount": 41}
        assert path == "/sync/createSync/v2" and kwargs["write"]
        notes.append(data)
        if fail_at == "note":
            raise BridgeError("rate_limited", "synthetic note rejection")
        return {"updateCount": 42}

    provider._json = note_request
    return provider, metadata, files, notes


@pytest.mark.parametrize("part,confirm", [(False, False), (False, True), (True, False), (True, True)])
def test_uploaded_image_keeps_its_bytes_position_and_durable_note_link(tmp_path, monkeypatch, part, confirm):
    note = image_note(tmp_path)
    provider, metadata, files, notes = configured_provider(tmp_path, monkeypatch, part=part, confirm=confirm)
    store, runner = Store(tmp_path / "tasks.sqlite"), None
    runner = TaskRunner(store)
    runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
    runner.join()
    assert runner.current().status == TaskStatus.SUCCEEDED
    assert len(notes) == 1
    uploaded = notes[0]["resources"][0]
    row = notes[0]["notes"][0]
    assert uploaded["noteGuid"] == row["guid"] != note.source_id
    assert uploaded["guid"] != note.attachments[0].id
    key = receipt_key(note, provider)
    assert store.resource_receipts(key) == [{"resource_id": "allocated-image", "status": "linked"}]
    restored_asset = note.attachments[0].model_copy(update={"id": uploaded["guid"], "name": uploaded["name"]})
    restored = parse_entry({**row, "userId": "fixture-user", "resources": [uploaded]}, row["content"],
                           provider.account_id, {}, [restored_asset])
    assert [b.kind for b in restored.blocks[:3]] == ["paragraph", "attachment", "paragraph"]
    assert restored.blocks[1].attachment_id == restored_asset.id
    assert restored.blocks[0].text == "before" and restored.blocks[2].text == "after"
    originals = [kwargs["files"]["file"][1] for path, kwargs in files if not path.endswith("/thumbUpload.do")]
    assert hashlib.sha256(b"".join(originals)).hexdigest() == note.attachments[0].sha256
    for _, kwargs in files:
        assert kwargs["write"] is True
        assert kwargs["headers"]["Origin"] == "https://pc.vivo.com.cn"
        assert kwargs["headers"]["x-yun-checksum"] == hashlib.md5(kwargs["files"]["file"][1]).hexdigest()
    preview = files[0][1]["files"]["file"]
    with Image.open(io.BytesIO(preview[1])) as picture:
        assert picture.format == "PNG" and max(picture.size) <= 540
    assert preview[0] == "IMG_Thumb.png" and preview[2] == "image/png"
    confirmations = [kwargs["json"] for path, kwargs in metadata if path.endswith("/confirmUpload.do")]
    assert len(confirmations) == (len(files) if confirm else 0), "Confirm unknown upload acknowledgements; do not resend bytes."
    if part:
        completion = next(kwargs["json"] for path, kwargs in metadata if path.endswith("/confirmRangeUpload.do"))
        assert completion["fileCheckSum"] == hashlib.md5(b"".join(originals)).hexdigest()
        assert completion["checkSumList"] == [hashlib.md5(chunk).hexdigest() for chunk in originals]


@pytest.mark.parametrize("fail_at,resource_status", [("file", "allocated"), ("note", "uploaded")])
def test_partial_upload_is_not_retried_and_keeps_orphan_object_receipt(tmp_path, monkeypatch, fail_at, resource_status):
    note = image_note(tmp_path)
    provider, metadata, _, _ = configured_provider(tmp_path, monkeypatch, fail_at=fail_at)
    store = Store(tmp_path / "tasks.sqlite")
    runner = TaskRunner(store)
    for _ in range(2):
        runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
        runner.join()
        assert runner.current().status == TaskStatus.NEEDS_REVIEW
    key = receipt_key(note, provider)
    assert store.receipt(key)["status"] == "uncertain"
    assert len(store.receipt(key)["remote_ids"]) == 1
    assert store.resource_receipts(key) == [{"resource_id": "allocated-image", "status": resource_status}]
    assert sum(path.endswith("/preUpload.do") for path, _ in metadata) == 1


def test_space_rejection_allocates_no_file_and_never_reports_success(tmp_path, monkeypatch):
    note = image_note(tmp_path)
    provider, _, files, notes = configured_provider(tmp_path, monkeypatch, fail_at="space")
    store = Store(tmp_path / "tasks.sqlite")
    runner = TaskRunner(store)
    runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
    runner.join()
    assert runner.current().status == TaskStatus.FAILED and not files and not notes
    assert runner.current().issues[0].code == "cloud_space_full"
    assert store.receipt(receipt_key(note, provider))["status"] == "rejected"


@pytest.mark.parametrize("invalid", ["modified", "missing_reference", "duplicate_id", "non_image", "outside"])
def test_all_images_are_checked_before_any_remote_side_effect(tmp_path, monkeypatch, invalid):
    note = image_note(tmp_path)
    provider, metadata, files, notes = configured_provider(tmp_path, monkeypatch)
    if invalid == "modified":
        (tmp_path / note.attachments[0].local_path).write_bytes(b"changed after download")
    elif invalid == "missing_reference":
        note.blocks[1].attachment_id = "not-downloaded"
    elif invalid == "duplicate_id":
        note.attachments.append(note.attachments[0].model_copy())
    elif invalid == "non_image":
        note.attachments[0].kind = "file"
    else:
        note.attachments[0].local_path = "../outside.png"
    with pytest.raises(BridgeError):
        provider.preflight([note])
    assert not metadata and not files and not notes


@pytest.mark.parametrize("origin", ["https://other.example", "https://clouddisk-fixture.vivo.com.cn.other.example",
                                    "https://user@clouddisk-fixture.vivo.com.cn", "https://clouddisk-fixture.vivo.com.cn:443",
                                    "https://clouddisk-fixture.vivo.com.cn/path", "https://clouddisk-fixture.vivo.com.cn?key=value"])
def test_scoped_file_session_cannot_be_sent_to_an_unverified_host(origin):
    with pytest.raises(BridgeError):
        file_origin(origin)


def test_process_recovery_preserves_unlinked_files_for_manual_reconciliation(tmp_path):
    store = Store(tmp_path / "tasks.sqlite")
    store.save_receipt("write-intent", "sending", ["new-note"])
    store.save_resource_receipt("write-intent", "new-object", "uploaded")
    store.recover_interrupted()
    assert store.receipt("write-intent") == {"status": "uncertain", "remote_ids": ["new-note"]}
    assert store.resource_receipts("write-intent") == [{"resource_id": "new-object", "status": "uploaded"}]


def test_image_note_uses_confirmed_target_folder_in_final_sync(tmp_path, monkeypatch):
    from note_bridge.providers import vivo_groups
    note = image_note(tmp_path)
    note.source_folder_id, note.source_folder_name = "source-folder", "work"
    provider, _, _, notes = configured_provider(tmp_path, monkeypatch)
    monkeypatch.setattr(vivo_groups, "target_group", lambda *args: ("confirmed-target-folder", []))
    store = Store(tmp_path / "groups.sqlite")
    runner = TaskRunner(store)
    runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
    runner.join()
    assert runner.current().succeeded == 1
    assert notes[0]["notes"][0]["noteBookGuid"] == "confirmed-target-folder"
    assert notes[0]["resources"], "Folder assignment must survive image upload and final sync."


def test_table_keeps_empty_cell_coordinates_and_literal_text_beside_image(tmp_path, monkeypatch):
    note = image_note(tmp_path)
    rows = [["列A", "列B", "列C"], ["中文😀", "", "A | B"], ["", "数字 42", "& < >"]]
    note.blocks.append(Block(kind="table", rows=rows))
    provider, _, _, notes = configured_provider(tmp_path, monkeypatch)
    store = Store(tmp_path / "table.sqlite")
    runner = TaskRunner(store)
    runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
    runner.join()
    assert runner.current().succeeded == 1
    entry = notes[0]["notes"][0]
    restored = parse_entry({**entry, "userId": "fixture-user"}, entry["content"],
                           provider.account_id, {})
    tables = [block.rows for block in restored.blocks if block.kind == "table"]
    assert tables == [rows], "Empty cells must not shift values into a different column."
    assert "<table>" in entry["content"] and "&amp; &lt; &gt;" in entry["content"]
    assert notes[0]["resources"], "The table must coexist with the uploaded image."


def test_complex_corpus_keeps_long_body_rich_styles_table_code_and_quote(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("complex_fixture", Path(__file__).resolve().parents[1] / "scripts/live-fixture-job.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    note = module.fixture({"target": "vivo", "id": "fixture-20260906-vivo-complex", "title": "笔记互迁验收 · 综合", "content_case": "complex"})
    note.attachments = image_note(tmp_path).attachments
    provider, _, _, notes = configured_provider(tmp_path, monkeypatch)
    store = Store(tmp_path / "complex.sqlite")
    runner = TaskRunner(store)
    runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
    runner.join()
    assert runner.current().succeeded == 1
    entry = notes[0]["notes"][0]
    restored = parse_entry({**entry, "userId": "fixture-user"}, entry["content"], provider.account_id, {})
    assert restored.blocks[:len(note.blocks)] == note.blocks
    assert len(note.plain_text) > 10000, "The corpus must exercise the long-body storage path."
