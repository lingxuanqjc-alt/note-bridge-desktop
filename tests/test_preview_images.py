"""Preview availability must not expand the verified upload scope or restrict exports."""

import hashlib
import io

import pytest
from PIL import Image

from note_bridge.errors import BridgeError
from note_bridge.exporter import Exporter
from note_bridge.models import Attachment, Block, NoteDocument, PlatformId, TaskStatus, account_fingerprint
from note_bridge.operations import migrate, receipt_key
from note_bridge.providers.factory import create_provider
from note_bridge.providers.xiaomi_files import BLOCK_BYTES
from note_bridge.storage import Store
from note_bridge.tasks import TaskRunner

PREVIEW_TARGETS = [PlatformId.XIAOMI, PlatformId.VIVO, PlatformId.HONOR, PlatformId.MEIZU, PlatformId.WPS, PlatformId.HUAWEI, PlatformId.OPPO]


def picture_note(tmp_path, fmt, *, size=None):
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "purple").save(stream, format=fmt)
    data = stream.getvalue()
    if size is not None:
        data += b"\0" * (size - len(data))
    path = tmp_path / "opaque-cloud-image"
    path.write_bytes(data)
    # Cloud names and metadata may be opaque or wrong: validate the original bytes.
    asset = Attachment(id="picture", name="misleading.png", kind="image", mime="image/png",
                       local_path=path.name, size=len(data), sha256=hashlib.sha256(data).hexdigest())
    note = NoteDocument(platform=PlatformId.HUAWEI, account_id="synthetic-source", source_id="original",
                        blocks=[Block(kind="attachment", attachment_id=asset.id)], attachments=[asset])
    return note, data


def local_provider(platform, tmp_path, monkeypatch):
    monkeypatch.setattr("note_bridge.providers.transport.Transport.request",
                        lambda *a, **k: pytest.fail("Local preflight must not send any HTTP request"))
    provider = create_provider(platform, [], tmp_path)
    provider.account_id = "synthetic-target"
    if platform == PlatformId.XIAOMI:
        cookies = {"userId": "synthetic-user", "serviceToken": "synthetic-session"}
        monkeypatch.setattr(provider.transport, "cookie", cookies.get)
        provider.account_id = account_fingerprint(platform, cookies["userId"])

        def mode(method, path, **kwargs):
            assert method == "GET" and path == "/mic/keybag/v1/getEncInfo" and not kwargs.get("write")
            return {"code": 0, "data": {"e2eeStatus": "close"}}

        monkeypatch.setattr(provider.transport, "json", mode)
        provider.refresh_write_mode()
    return provider


@pytest.mark.parametrize("platform", PREVIEW_TARGETS)
@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
def test_verified_image_bytes_are_eligible_without_trusting_filename_or_mime(platform, fmt, tmp_path, monkeypatch):
    note, data = picture_note(tmp_path, fmt)
    provider = local_provider(platform, tmp_path, monkeypatch)
    try:
        provider.preflight([note])
        assert (tmp_path / note.attachments[0].local_path).read_bytes() == data
    finally:
        provider.close()


@pytest.mark.parametrize("platform", PREVIEW_TARGETS)
@pytest.mark.parametrize("fmt", ["GIF", "WEBP"])
def test_unverified_formats_stop_before_sending_intent_group_or_upload(platform, fmt, tmp_path, monkeypatch):
    note, data = picture_note(tmp_path, fmt)
    provider = local_provider(platform, tmp_path, monkeypatch)
    store = Store(tmp_path / "state.sqlite")
    try:
        # Direct provider calls must honor the same scope before using any context or network.
        with pytest.raises(BridgeError) as error:
            provider.create(note, None)
        assert error.value.code == "unsupported_image" and all(fmt in error.value.message for fmt in ("PNG", "JPEG"))
        runner = TaskRunner(store)
        runner.start("migration", lambda context: migrate([note], provider, store, context))
        runner.join(3)
        report = runner.current()
        assert report.status == TaskStatus.FAILED and report.succeeded == 0
        assert store.receipt(receipt_key(note, provider)) is None
        assert any(issue.code == "unsupported_image" for issue in report.issues)
        assert (tmp_path / note.attachments[0].local_path).read_bytes() == data
    finally:
        provider.close()


@pytest.mark.parametrize("size", [BLOCK_BYTES - 1, BLOCK_BYTES, BLOCK_BYTES + 1])
def test_xiaomi_preview_stops_before_unverified_second_block(tmp_path, monkeypatch, size):
    note, data = picture_note(tmp_path, "PNG", size=size)
    provider = local_provider(PlatformId.XIAOMI, tmp_path, monkeypatch)
    try:
        if size <= BLOCK_BYTES:
            provider.preflight([note])
        else:
            with pytest.raises(BridgeError) as error:
                provider.create(note, None)
            assert error.value.code == "unsupported_image" and "多块上传尚未实测" in error.value.message
        assert (tmp_path / note.attachments[0].local_path).read_bytes() == data
    finally:
        provider.close()


@pytest.mark.parametrize("fmt", ["GIF", "WEBP"])
def test_migration_only_restrictions_preserve_original_local_export(tmp_path, fmt):
    note, data = picture_note(tmp_path, fmt)
    result = Exporter(tmp_path).export([note], tmp_path, "html")
    assert result.note_count == 1 and not result.issues
    assert [path.read_bytes() for path in (result.path / "resources").iterdir()] == [data]
