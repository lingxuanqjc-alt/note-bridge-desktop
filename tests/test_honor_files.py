import hashlib
import io
import json
from threading import Event
from types import SimpleNamespace

import pytest
from PIL import Image

from note_bridge.errors import BridgeError, WriteUncertain
from note_bridge.models import Attachment, Block, NoteDocument, PlatformId, Span, TaskStatus
from note_bridge.operations import migrate, receipt_key
from note_bridge.providers.honor import HonorProvider, parse_entry
from note_bridge.storage import Store
from note_bridge.tasks import TaskRunner


def setup_image(tmp_path):
    image = io.BytesIO()
    Image.new("RGB", (80, 50), (100, 40, 200)).save(image, "PNG")
    data = image.getvalue()
    (tmp_path / "image.png").write_bytes(data)
    asset = Attachment(id="source-image", name="image.png", kind="image", mime="image/png",
                       local_path="image.png", sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    note = NoteDocument(platform=PlatformId.XIAOMI, account_id="source", source_id="source-note",
                        title="中文😀", attachments=[asset], blocks=[Block(spans=[Span(text="before", bold=True)]),
                        Block(kind="attachment", attachment_id=asset.id), Block(spans=[Span(text="after")])])
    transport = SimpleNamespace(cookie=lambda _: "synthetic-csrf", close=lambda: None)
    provider = HonorProvider(transport, tmp_path)
    provider.account_id = "synthetic-account"
    provider.write_supported = provider.images_supported = True
    return provider, note, data


class Download:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, size):
        yield self.data


@pytest.mark.parametrize("failure", [None, "precreate", "upload", "final_save", "existing_file", "wrong_existing_file"])
def test_pair_upload_readback_and_partial_note_receipts_prevent_duplicate_retry(tmp_path, failure):
    provider, note, data = setup_image(tmp_path)
    store = Store(tmp_path / "tasks.sqlite")
    runner = TaskRunner(store)
    saves, uploads = [], {}

    def note_json(method, path, **options):
        assert options["write"]
        if path == "notepad/file/preCreateFile":
            if failure == "precreate":
                raise BridgeError("cloud_space_full", "synthetic quota")
            if failure in ("existing_file", "wrong_existing_file"):
                return {"existing_file": True}
            return None
        assert path == "notepad/noteSave"
        entry = json.loads(json.dumps(options["json"][0]))
        assert "has_attach" in entry and "has_attachment" not in entry
        for attachment in entry.get("attachments", []):
            assert attachment["attach_type"] == 2 and "attachment_type" not in attachment
        saves.append(entry)
        if failure == "final_save" and entry["update"]:
            raise WriteUncertain()
        return [{"uuid": entry["uuid"]}]

    def upload(method, path, **options):
        assert method == "POST" and path == "/portal/notepad/file/upload" and options["write"]
        if failure == "upload":
            raise WriteUncertain()
        assert options["headers"]["Origin"] == "https://cloud.honor.com"
        metadata = json.loads(options["files"]["attachment"][1])
        assert metadata["parent_uuid"] == saves[0]["uuid"] != note.source_id
        content = options["files"]["file"][1]
        assert content == data and hashlib.sha256(content).hexdigest() == metadata["hash"]
        uploads[options["params"]["cloudPath"]] = content
        return {"code": 0, "data": None}

    provider._json = note_json
    provider._files.transport = SimpleNamespace(json=upload,
        request=lambda method, path, **kw: Download(
            b"wrong" if failure == "wrong_existing_file" else
            data if failure == "existing_file" else uploads[kw["params"]["cloudPath"]]))
    original = note.fingerprint()
    runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
    runner.join(3)
    assert note.fingerprint() == original, "Target encoding must not alter the cached source."
    key = receipt_key(note, provider)
    receipt = store.receipt(key)
    if failure and failure != "existing_file":
        assert runner.current().status == TaskStatus.NEEDS_REVIEW
        assert receipt["status"] == "uncertain" and receipt["remote_ids"] == [saves[0]["uuid"]]
        count = len(saves)
        runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
        runner.join(3)
        assert len(saves) == count and runner.current().status == TaskStatus.NEEDS_REVIEW
        assert all(r["status"] != "linked" for r in store.resource_receipts(key))
        return
    assert runner.current().status == TaskStatus.SUCCEEDED
    assert len(saves) == 2 and len(uploads) == 2 and saves[1]["update"]
    assert receipt["status"] == "confirmed" and all(r["status"] == "linked" for r in store.resource_receipts(key))
    found = []
    runner.start("readback", lambda ctx: found.extend(provider._files.download(saves[1], ctx)))
    runner.join(3)
    restored = parse_entry(saves[1], provider.account_id, {}, found)
    assert not restored.warnings and len(restored.attachments) == 1
    assert restored.attachments[0].sha256 == note.attachments[0].sha256
    assert [b.kind for b in restored.blocks[:3]] == ["paragraph", "attachment", "paragraph"]
    assert restored.blocks[0] == note.blocks[0] and restored.blocks[2] == note.blocks[2]
    assert restored.blocks[1].attachment_id == found[0].id


@pytest.mark.parametrize("invalid", ["changed", "outside", "missing_reference", "duplicate", "nonimage"])
def test_all_source_images_are_validated_before_remote_note_creation(tmp_path, invalid):
    provider, note, _ = setup_image(tmp_path)
    if invalid == "changed":
        (tmp_path / "image.png").write_bytes(b"changed")
    elif invalid == "outside":
        note.attachments[0].local_path = "../image.png"
    elif invalid == "missing_reference":
        note.blocks[1].attachment_id = "missing"
    elif invalid == "duplicate":
        note.attachments.append(note.attachments[0].model_copy())
    else:
        note.attachments[0].kind = "file"
    provider._json = lambda *a, **kw: pytest.fail("Invalid source must not create even a placeholder note.")
    with pytest.raises(BridgeError):
        provider.preflight([note])


@pytest.mark.parametrize("field,value", [("parent_uuid", "another-note"), ("filename", "../secret"), ("hash", "bad")])
def test_download_checks_scope_and_integrity_before_contacting_file_service(tmp_path, field, value):
    provider, _, data = setup_image(tmp_path)
    row = {"uuid": "image", "parent_uuid": "note", "filename": "image.png", "mimetype": "image/png",
           "size": len(data), "hash": hashlib.sha256(data).hexdigest(), field: value}
    with pytest.raises(BridgeError):
        provider._files.download({"uuid": "note", "unstruct_guid": "folder", "attachments": [row]}, None)


def test_existing_blob_code_is_not_a_general_write_success(tmp_path):
    provider, _, _ = setup_image(tmp_path)
    provider.transport.json = lambda *a, **kw: {"code": 40053, "desc": "synthetic"}
    assert provider._json("POST", "notepad/file/preCreateFile", write=True) == {"existing_file": True}
    with pytest.raises(WriteUncertain):
        provider._json("POST", "notepad/noteSave", write=True)


def cached_download_setup(tmp_path):
    provider, note, data = setup_image(tmp_path)
    row = {"uuid": "current-image", "parent_uuid": "current-note", "filename": "current.png",
           "mimetype": "image/png", "size": len(data), "hash": hashlib.sha256(data).hexdigest()}
    entry = {"uuid": "current-note", "unstruct_guid": "current-container", "attachments": [row]}
    target = tmp_path / "honor" / provider.account_id / (row["hash"] + ".png")
    target.parent.mkdir(parents=True)
    target.write_bytes(data)
    requests = []

    def download(method, path, **kwargs):
        requests.append(kwargs["params"]["cloudPath"])
        return Download(data)

    provider._files.transport = SimpleNamespace(request=download)
    context = SimpleNamespace(check_cancel=lambda: None, cancelled=Event(), record_resource=lambda *args: None)
    return provider, note, data, row, entry, target, requests, context


def test_fetch_reuses_matching_bytes_but_keeps_the_current_remote_identity(tmp_path):
    provider, _, data, row, entry, target, requests, context = cached_download_setup(tmp_path)
    row["hash"] = row["hash"].upper()
    actual = provider._files.download(entry, context)
    assert not requests, "Fresh server metadata already proves the cached bytes; do not download them again."
    assert len(actual) == 1 and actual[0].id == "current-image" and actual[0].name == "current.png"
    assert actual[0].local_path == target.relative_to(tmp_path).as_posix()
    assert actual[0].sha256 == hashlib.sha256(data).hexdigest() and actual[0].size == len(data)


@pytest.mark.parametrize("cache", ["missing", "truncated", "same_size_corrupt", "unreadable"])
def test_bad_or_missing_cache_falls_back_to_verified_remote_download(tmp_path, monkeypatch, cache):
    provider, _, data, _, entry, target, requests, context = cached_download_setup(tmp_path)
    if cache == "missing":
        target.unlink()
    elif cache == "truncated":
        target.write_bytes(data[:-1])
    elif cache == "same_size_corrupt":
        target.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    else:
        original_open = type(target).open

        def open_file(path, mode="r", *args, **kwargs):
            if path == target and mode == "rb":
                raise PermissionError("synthetic cache read failure")
            return original_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(type(target), "open", open_file)
    actual = provider._files.download(entry, context)
    assert requests == ["/sync/notepad/current-container/current.png"]
    assert actual[0].sha256 == hashlib.sha256(data).hexdigest() and actual[0].size == len(data)
    monkeypatch.undo()
    assert target.read_bytes() == data


def test_another_accounts_cache_cannot_suppress_the_current_accounts_download(tmp_path):
    provider, _, data, _, entry, old_target, requests, context = cached_download_setup(tmp_path)
    provider.account_id = "different-account"
    actual = provider._files.download(entry, context)
    assert len(requests) == 1 and old_target.read_bytes() == data
    assert actual[0].local_path.startswith("honor/different-account/")


@pytest.mark.parametrize("cancel_at", [1, 2])
def test_cache_validation_remains_cancellable(tmp_path, cancel_at):
    provider, _, data, _, entry, target, requests, context = cached_download_setup(tmp_path)
    calls = 0

    def check_cancel():
        nonlocal calls
        calls += 1
        if calls == cancel_at:
            raise BridgeError("cancelled", "synthetic cancellation")

    context.check_cancel = check_cancel
    with pytest.raises(BridgeError) as error:
        provider._files.download(entry, context)
    assert error.value.code == "cancelled" and not requests and target.read_bytes() == data


@pytest.mark.parametrize("preexisting", [False, True])
def test_upload_and_preexisting_checks_cannot_be_satisfied_by_cached_source_bytes(tmp_path, preexisting):
    provider, note, data, _, _, _, requests, context = cached_download_setup(tmp_path)
    provider._json = lambda *args, **kwargs: {"existing_file": preexisting}
    uploads = []

    def upload(*args, **kwargs):
        uploads.append(kwargs["params"]["cloudPath"])
        return {"code": 0}

    def download(*args, **kwargs):
        requests.append(kwargs["params"]["cloudPath"])
        return Download(bytes([data[0] ^ 1]) + data[1:])

    provider._files.transport = SimpleNamespace(request=download, json=upload)
    with pytest.raises(BridgeError) as error:
        provider._files.upload(note.attachments[0], "current-note", "current-container", context)
    assert error.value.code == "attachment_changed" and len(requests) == 1
    assert len(uploads) == (0 if preexisting else 1), (
        "New writes and preexisting remote paths need actual byte readback even when the source is cached."
    )
