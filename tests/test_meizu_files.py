import hashlib
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from note_bridge.errors import BridgeError, WriteUncertain
from note_bridge.models import Attachment, Block, NoteDocument, PlatformId, Span, TaskStatus
from note_bridge.operations import migrate, receipt_key
from note_bridge.providers.meizu import MeizuProvider, parse_entry
from note_bridge.storage import Store
from note_bridge.tasks import TaskRunner


def setup(tmp_path):
    stream = io.BytesIO()
    Image.new("RGB", (24, 20), (120, 20, 200)).save(stream, "PNG")
    data = stream.getvalue()
    (tmp_path / "source.png").write_bytes(data)
    asset = Attachment(id="image", name="source.png", kind="image", mime="image/png", local_path="source.png",
                       sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    note = NoteDocument(platform=PlatformId.XIAOMI, account_id="source", source_id="source-note",
                        title="测试", attachments=[asset], blocks=[Block(spans=[Span(text="😀before", bold=True)]),
                          Block(kind="attachment", attachment_id="image"), Block(spans=[Span(text="after")])])
    provider = MeizuProvider(SimpleNamespace(origin="https://notes.flyme.cn"), tmp_path)
    provider.account_id = "test-account"
    provider.write_supported = provider.images_supported = True
    return provider, note, data


@pytest.mark.parametrize("name,expected", [("opaque", "opaque.png"), ("wrong.jpg", "wrong.jpg.png"),
                                         ("中文.png", "中文.png"), ("upper.PNG", "upper.png")])
def test_upload_filename_identifies_verified_image_without_changing_source(tmp_path, name, expected):
    provider, note, data = setup(tmp_path)
    asset = note.attachments[0]
    asset.name, asset.mime = name, "image/jpeg"  # Deliberately stale source metadata.
    before = asset.model_dump()
    uploads = []
    def upload(method, path, **kwargs):
        uploads.append(kwargs["files"]["file"])
        return {"returnCode": 200, "returnValue": {"tempPath": "/temp/server.png"}}
    provider.transport.json = upload
    provider._files.download = lambda *args, **kwargs: None
    context = SimpleNamespace(record_resource=lambda *args: None, check_cancel=lambda: None,
                              cancelled=SimpleNamespace(wait=lambda *args: None))
    assert provider._files.upload(asset, "new-target", context) == "server.png"
    assert uploads == [(expected, data, "image/png")]
    assert asset.model_dump() == before, "Target filename normalization must not mutate source identity or bytes."


class Download:
    headers = {"Content-Type": "image/png"}

    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, size):
        yield self.data


@pytest.mark.parametrize("failure", [None, "upload", "corrupt", "final_save", "foreign_path"])
def test_image_association_preserves_source_and_unknown_results_never_duplicate_notes(tmp_path, failure):
    provider, note, data = setup(tmp_path)
    saved = []
    def save(path, **kwargs):
        row = kwargs["data"]
        saved.append(row.copy())
        assert row.get("uuid") != note.source_id, "An update may only target the newly created target note."
        if failure == "final_save" and row.get("uuid"):
            raise WriteUncertain()
        return {"uuid": "new-target"}
    def upload(method, path, **kwargs):
        assert path == "/c/browser/note/addFileToTemp" and kwargs["data"] == {"uuid": "new-target", "type": "1"}
        if failure == "upload":
            raise WriteUncertain()
        return {"returnCode": 200, "returnValue": {"tempPath": "//foreign.invalid/image.png" if failure == "foreign_path" else "/temp/server.png"}}
    provider._json = save
    provider.transport.json = upload
    provider.transport.request = lambda *args, **kwargs: Download(b"invalid" if failure == "corrupt" else data)
    store = Store(tmp_path / "tasks.sqlite")
    runner = TaskRunner(store)
    before = note.fingerprint()
    runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
    runner.join(3)
    key = receipt_key(note, provider)
    assert note.fingerprint() == before
    if failure:
        assert runner.current().status == TaskStatus.NEEDS_REVIEW
        assert store.receipt(key)["remote_ids"] == ["new-target"]
        count = len(saved)
        runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
        runner.join(3)
        assert len(saved) == count and runner.current().status == TaskStatus.NEEDS_REVIEW
        return
    assert runner.current().status == TaskStatus.SUCCEEDED and len(saved) == 2
    assert all(row["status"] == "linked" for row in store.resource_receipts(key))
    restored = parse_entry({"uuid": "new-target", "body": saved[1]["body"], "files": {"server.png": "/file/server.png"}}, "test-account")
    runner.start("download", lambda ctx: provider._files.download(restored.attachments[0], ctx))
    runner.join(3)
    assert restored.attachments[0].sha256 == note.attachments[0].sha256
    assert restored.blocks[0] == note.blocks[0] and restored.blocks[2] == note.blocks[2]
    assert restored.blocks[1].attachment_id == "server.png"


@pytest.mark.parametrize("failure", ["changed", "outside", "missing_reference", "duplicate", "nonimage"])
def test_invalid_images_cannot_create_even_a_partial_remote_note(tmp_path, failure):
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


@pytest.mark.parametrize("url", ["https://foreign.invalid/file", "//foreign.invalid/file", "http://notes.flyme.cn/file", "file:///private", ""])
def test_remote_attachment_urls_cannot_escape_vendor_scope(tmp_path, url):
    provider, note, _ = setup(tmp_path)
    note.attachments[0].download_url = url
    with pytest.raises(BridgeError):
        provider._files.download(note.attachments[0], None)
