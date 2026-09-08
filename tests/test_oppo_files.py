import hashlib
import io
from types import SimpleNamespace

import pytest
from lxml import html
from PIL import Image

from note_bridge.errors import BridgeError, WriteUncertain
from note_bridge.models import Attachment, Block, NoteDocument, PlatformId, Span, TaskStatus
from note_bridge.operations import migrate, receipt_key
from note_bridge.providers.oppo import OppoProvider, parse_entry
from note_bridge.storage import Store
from note_bridge.tasks import TaskRunner


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


def setup(tmp_path):
    stream = io.BytesIO()
    Image.new("RGB", (20, 30), (20, 60, 100)).save(stream, "PNG")
    data = stream.getvalue()
    (tmp_path / "source.png").write_bytes(data)
    asset = Attachment(id="source-image", name="source.png", kind="image", local_path="source.png",
                       size=len(data), sha256=hashlib.sha256(data).hexdigest())
    note = NoteDocument(platform=PlatformId.XIAOMI, account_id="source-account", source_id="source-note",
        attachments=[asset], blocks=[Block(spans=[Span(text="before", italic=True)]),
          Block(kind="attachment", attachment_id=asset.id), Block(spans=[Span(text="after")])])
    provider = OppoProvider(SimpleNamespace(origin="https://owork-api-cn.oppo.com", session=SimpleNamespace(headers={})), tmp_path)
    provider.account_id = "target-account"
    provider.write_supported = provider.images_supported = True
    return provider, note, data


@pytest.mark.parametrize("mode", ["small", "parts", "existing", "unknown_upload", "bad_bytes", "unknown_add"])
def test_upload_bytes_and_receipts_protect_against_incomplete_or_duplicate_migrations(tmp_path, mode):
    provider, note, data = setup(tmp_path)
    # Exercise the implemented wire algorithm separately; production preview stays disabled.
    provider._files.multipart_supported = mode == "parts"
    paths, parts, added = [], [], []
    metadata = {"id": "new-image", "ocloudId": "oc_new_image", "checkPayload": "synthetic"}
    def upload(method, path, **kwargs):
        step = path.rsplit("/", 1)[-1]
        paths.append(step)
        assert kwargs["write"] and "encryptContent" not in kwargs.get("json", {})
        if step == "prepare-file-upload":
            assert kwargs["json"]["fileMd5"] == hashlib.md5(data).hexdigest()
            return {"code": 0, "data": {**metadata, "applyId": "new-apply", "existingFile": mode == "existing",
                                        "smallFileThreshold": 0 if mode == "parts" else 10000, "sliceSize": 37}}
        if mode == "unknown_upload":
            raise WriteUncertain()
        if step == "big-file-part-upload":
            part = kwargs["files"]["file"][1]
            assert kwargs["data"]["partNumber"] == len(parts) + 1
            assert kwargs["data"]["partMd5"] == hashlib.md5(part).hexdigest()
            parts.append(part)
        if step == "big-file-part-merge":
            assert b"".join(parts) == data and kwargs["json"]["totalSlices"] == len(parts)
            assert kwargs["json"]["lastSliceSize"] == len(parts[-1])
        return {"code": 0, "data": metadata}
    def add(path, entry, **kwargs):
        added.append(entry)
        assert "recordId" not in entry, "Source or cached target IDs must never be reused for creation."
        if mode == "unknown_add":
            raise WriteUncertain()
        return {"recordId": "new-note"}
    provider.transport.json = upload
    provider.transport.request = lambda *args, **kwargs: Download(b"bad" if mode == "bad_bytes" else data)
    provider._json = add
    store = Store(tmp_path / "tasks.sqlite")
    runner = TaskRunner(store)
    before = note.fingerprint()
    runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
    runner.join(3)
    assert note.fingerprint() == before
    key = receipt_key(note, provider)
    if mode in ("unknown_upload", "bad_bytes", "unknown_add"):
        assert runner.current().status == TaskStatus.NEEDS_REVIEW
        count = len(paths)
        runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
        runner.join(3)
        assert len(paths) == count and runner.current().status == TaskStatus.NEEDS_REVIEW
        assert all(row["status"] != "linked" for row in store.resource_receipts(key))
        return
    assert runner.current().status == TaskStatus.SUCCEEDED
    native_image = html.fragment_fromstring(added[0]["rawText"], create_parent="div").find("img")
    assert native_image.get("src") == added[0]["attachments"][0]["id"], "The vendor renderer resolves src through attachment metadata; a raw download URL is discarded."
    assert all(row["status"] == "linked" for row in store.resource_receipts(key))
    if mode == "existing":
        assert paths == ["prepare-file-upload"], "Existing bytes still require local checksum verification."
    downloaded = note.attachments[0].model_copy(update={"id": "new-image"})
    restored = parse_entry({**added[0], "recordId": "new-note", "status": 0}, "target", {}, [downloaded])
    assert not restored.warnings and restored.blocks[0] == note.blocks[0] and restored.blocks[2] == note.blocks[2]
    assert restored.blocks[1].attachment_id == "new-image"


@pytest.mark.parametrize("failure", ["changed", "outside", "missing_reference", "duplicate", "nonimage"])
def test_invalid_source_images_are_rejected_before_allocating_cloud_resources(tmp_path, failure):
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


def test_native_image_id_without_editor_attachid_still_preserves_the_image(tmp_path):
    _, note, _ = setup(tmp_path)
    entry = {"recordId": "native", "status": 0, "rawText": '<p>before</p><img src="source-image"><p>after</p>'}
    parsed = parse_entry(entry, "target", {}, note.attachments)
    assert not parsed.warnings and parsed.blocks[1].attachment_id == "source-image"
