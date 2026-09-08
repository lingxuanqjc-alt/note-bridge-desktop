import hashlib
import io
import json
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from PIL import Image

from note_bridge.errors import BridgeError, WriteUncertain
from note_bridge.models import Attachment, Block, NoteDocument, PlatformId, Span, TaskStatus
from note_bridge.operations import migrate, receipt_key
from note_bridge.providers.huawei import HuaweiProvider, parse_entry
from note_bridge.providers.huawei_transport import HuaweiTransport
from note_bridge.providers.transport import Transport
from note_bridge.storage import Store
from note_bridge.tasks import TaskRunner


def setup(tmp_path):
    stream = io.BytesIO()
    Image.new("RGB", (30, 20), (40, 80, 200)).save(stream, "PNG")
    data = stream.getvalue()
    (tmp_path / "source.png").write_bytes(data)
    asset = Attachment(id="source-image", name="source.png", kind="image", local_path="source.png",
                       size=len(data), sha256=hashlib.sha256(data).hexdigest())
    note = NoteDocument(platform=PlatformId.XIAOMI, account_id="source", source_id="original",
        title="测试", attachments=[asset], blocks=[Block(spans=[Span(text="before", bold=True)]),
         Block(kind="attachment", attachment_id=asset.id), Block(spans=[Span(text="after")])])
    provider = HuaweiProvider(SimpleNamespace(cookie=lambda key: None), tmp_path)
    provider.account_id = "target-account"
    provider.write_supported = provider.images_supported = True
    return provider, note, data


@pytest.mark.parametrize("usage", ["foreign_30_20_1700000000000.png", "prefix_31_20_1700000000000.png",
                                   "prefix_30_20_1700000000000.jpg", "../prefix_30_20_1700000000000.png"])
def test_recovery_cannot_reuse_a_foreign_or_different_image_name(tmp_path, usage):
    provider, note, _ = setup(tmp_path)
    calls = []
    provider._files.configuration = lambda: calls.append("network")
    with pytest.raises(BridgeError) as error:
        provider._files.upload(note.attachments[0], "note", "prefix", None, usage=usage)
    assert error.value.code == "recovery_resource_mismatch" and not calls


@pytest.mark.parametrize("failure", [None, "upload_unknown", "session_expired", "bad_bytes", "update_unknown", "bad_signature", "directory_missing"])
@pytest.mark.parametrize("empty", [False, True])
def test_new_note_only_and_unknown_writes_never_retry_or_report_success(tmp_path, failure, empty):
    provider, note, data = setup(tmp_path)
    writes, content, versions = [], [], []
    def listing(**kwargs):
        rows = [] if empty else [{"guid": "existing", "kind": "note"}]
        if content and failure != "directory_missing":
            rows.append({"guid": "new-note", "kind": "note"})
        return {"startCursor": "listing-cursor"}, rows
    provider._listing = listing
    def metadata(path, payload, **kwargs):
        if path == "notetag/query":
            return {}
        if path == "note/query":
            return {"rspInfo": {"guid": payload["guid"], "etag": "new-version",
                    "data": json.dumps({"guid": "legacy-body-guid", "content": {"version": "19"}})}}
        writes.append(path)
        if path == "note/create":
            assert payload["guid"] != "existing"
            versions.append(json.loads(payload["reqInfo"]["data"])["currentNotePadVersion"])
            return {"rspInfo": {"guid": "new-note", "uuid": "new-prefix", "luid": "new-luid"}}
        assert payload["reqInfo"]["guid"] == "new-note"
        assert payload["startCursor"] == "listing-cursor", "A detail response without a cursor must retain the last directory cursor."
        if failure == "update_unknown":
            raise WriteUncertain()
        content.append(payload["reqInfo"]["data"])
        assert json.loads(content[-1])["currentNotePadVersion"] != versions[0]
        assert json.loads(content[-1])["guid"] == payload["reqInfo"]["guid"], "Native updates bind body and envelope to the newly allocated cloud record."
        return {"rspInfo": {}}
    def files(method, path, **kwargs):
        if path == "/html/getAboutGet":
            return {"code": 0, "dataSwitch": 1, "dataVersion": "dataVersion=2.0"}
        if path == "/html/getCommonParam":
            return {"code": 0, "fileProxyGrayStrategyVersionValue": "synthetic-version"}
        if path == "/html/getHomeData":
            return {"code": 0, "notepadRecycleBinOnSwitch": True}
        if path.endswith("preProcess"):
            return {"code": "0"}
        writes.append(path)
        if "preUpload" in path:
            return {"code": "0", "requestTimeStamp": "1", "sign": "" if failure == "bad_signature" else "synthetic-sign", "dataSyncUserLock": "synthetic-lock"}
        if "afterUpload" in path:
            return {"code": "0"}
        if failure == "session_expired":
            raise BridgeError("session_expired", "Synthetic login expiry")
        assert "/record/new-note/" in unquote(path)
        assert kwargs["files"]["file"][1] == data
        if failure == "upload_unknown":
            raise WriteUncertain()
        return {"id": "new-asset", "versionId": "revision"}
    def download(method, path, **kwargs):
        assert "/record/new-note/" in unquote(path)
        return SimpleNamespace(iter_content=lambda size: [b"broken" if failure == "bad_bytes" else data], close=lambda: None)
    provider._json, provider.transport.json, provider.transport.request = metadata, files, download
    store, before = Store(tmp_path / "tasks.sqlite"), note.fingerprint()
    runner = TaskRunner(store)
    runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
    runner.join(3)
    assert note.fingerprint() == before
    key = receipt_key(note, provider)
    if failure:
        assert runner.current().status == TaskStatus.NEEDS_REVIEW
        if failure == "session_expired":
            assert any(issue.code == "session_expired" for issue in runner.current().issues), "A partial write must retain the actionable login cause without becoming safe to retry."
        count = len(writes)
        runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
        runner.join(3)
        assert len(writes) == count and runner.current().status == TaskStatus.NEEDS_REVIEW
        return
    assert runner.current().status == TaskStatus.SUCCEEDED
    assert all(r["status"] == "linked" for r in store.resource_receipts(key))
    restored = json.loads(content[0])
    asset = note.attachments[0].model_copy(update={"name": restored["fileList"][0]["name"]})
    parsed = parse_entry({"guid": "new-note", "kind": "note", "data": content[0]}, "target-account", {}, [asset])
    assert parsed.blocks[1:4] == note.blocks, "Image position and adjacent formatting must survive the native body dialect."
    assert not parsed.warnings


@pytest.mark.parametrize("failure", ["changed", "outside", "missing_reference", "duplicate", "unsupported"])
def test_invalid_source_is_rejected_before_any_cloud_note_is_created(tmp_path, failure):
    provider, note, _ = setup(tmp_path)
    if failure == "changed":
        (tmp_path / "source.png").write_bytes(b"changed")
    elif failure == "outside":
        note.attachments[0].local_path = "../other.png"
    elif failure == "missing_reference":
        note.blocks[1].attachment_id = "unknown"
    elif failure == "duplicate":
        note.attachments.append(note.attachments[0].model_copy())
    else:
        note.attachments[0].kind = "file"
    with pytest.raises(BridgeError):
        provider.preflight([note])


def test_rotated_csrf_overrides_stale_cookie_and_is_cleared_on_close(monkeypatch):
    sent = []
    def request(self, method, path, **kwargs):
        sent.append(kwargs["headers"].get("CSRFToken"))
        return SimpleNamespace(headers={"CSRFToken": "synthetic-rotated"} if len(sent) == 1 else {})
    monkeypatch.setattr(Transport, "request", request)
    transport = HuaweiTransport("https://cloud.huawei.com", ("huawei.com",))
    transport.request("POST", "/html/getAboutGet", headers={"CSRFToken": "synthetic-old"})
    transport.request("POST", "/notepad/note/create", headers={"CSRFToken": "synthetic-old"}, write=True)
    assert sent == ["synthetic-old", "synthetic-rotated"], "A successful read can rotate the required token before the next write."
    transport.close()
    assert transport._csrf is None


def test_uploaded_images_without_body_references_cannot_appear_complete(tmp_path):
    _, note, _ = setup(tmp_path)
    entry = {"guid": "new-note", "kind": "note", "data": json.dumps({"content": {"html_content": "<note><element type='Text'>draft</element></note>"}})}
    parsed = parse_entry(entry, "target-account", {}, note.attachments)
    assert any("引用不一致" in warning for warning in parsed.warnings)


@pytest.mark.parametrize("title", ["独立标题", ""])
def test_explicit_title_does_not_include_the_directory_body_preview(title):
    content = {"title": title + "\n正文摘要", "data5": json.dumps({"data1": title, "data2": "edit"}),
               "html_content": "<note><element type='Text'>正文摘要</element></note>"}
    note = parse_entry({"guid": "cloud-note", "kind": "note", "data": json.dumps({"content": content})}, "account", {})
    assert note.title == title, "A directory preview must not silently rename a manually titled or untitled note."
    assert any(span.text == "正文摘要" for block in note.blocks for span in block.spans)
