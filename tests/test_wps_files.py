import base64
import hashlib
import io
import os
from types import SimpleNamespace

import pytest
from botocore.exceptions import EndpointConnectionError
from PIL import Image

from note_bridge.errors import BridgeError, WriteUncertain
from note_bridge.models import Attachment, Block, NoteDocument, PlatformId, Span, TaskStatus
from note_bridge.operations import migrate, receipt_key
from note_bridge.providers.wps import WpsCodec, WpsProvider, decode_body
from note_bridge.storage import Store
from note_bridge.tasks import TaskRunner


class Body:
    def __init__(self, data):
        self.data = data

    def iter_chunks(self, size):
        yield self.data

    def close(self):
        pass


def setup(tmp_path):
    stream = io.BytesIO()
    Image.new("RGB", (30, 20), (30, 50, 200)).save(stream, "PNG")
    data = stream.getvalue()
    (tmp_path / "source.png").write_bytes(data)
    asset = Attachment(id="source-image", name="source.png", kind="image", local_path="source.png",
                       size=len(data), sha256=hashlib.sha256(data).hexdigest())
    note = NoteDocument(platform=PlatformId.XIAOMI, account_id="source", source_id="source-note",
        title="测试", attachments=[asset], blocks=[Block(spans=[Span(text="before", bold=True)]),
          Block(kind="attachment", attachment_id=asset.id), Block(spans=[Span(text="after")])])
    provider = WpsProvider(SimpleNamespace(), None, tmp_path, SimpleNamespace())
    provider._codec = SimpleNamespace(encrypt=lambda value: value)
    provider.account_id = "test-account"
    provider.write_supported = provider.images_supported = True
    return provider, note, data


@pytest.mark.parametrize("failure", [None, "put_unknown", "bad_bytes", "body_unknown", "long"])
def test_object_writes_and_note_association_keep_source_intact_and_block_unknown_retries(tmp_path, failure):
    provider, note, data = setup(tmp_path)
    if failure == "long":
        note.blocks.append(Block(spans=[Span(text="长😀" * 4000)]))
        provider._codec = WpsCodec.__new__(WpsCodec)
        provider._codec._key = b"0123456789abcdef"
    writes, objects, content = [], {}, []
    def drive(method, path, **kwargs):
        if method == "GET":
            return {"id": 1}
        writes.append(path)
        return {"id": "new-cloud-file"}
    def note_json(method, path, **kwargs):
        writes.append(path)
        if path == "set/noteinfo":
            return {"infoVersion": 1}
        if failure == "body_unknown":
            raise WriteUncertain()
        content.append(kwargs["json"])
        return {"contentVersion": 1}
    def put(**kwargs):
        writes.append("put")
        if failure == "put_unknown":
            raise EndpointConnectionError(endpoint_url="https://synthetic.invalid")
        assert kwargs["Bucket"] == "synthetic-bucket" and kwargs["Key"].startswith("account/")
        objects[kwargs["Key"]] = kwargs["Body"]
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}
    def get(**kwargs):
        result = b"bad" if failure == "bad_bytes" else objects[kwargs["Key"]]
        return {"Body": Body(result), "ContentLength": len(result)}
    client = SimpleNamespace(put_object=put, get_object=get)
    provider.drive.json, provider._json = drive, note_json
    provider._files._client = lambda write=False: (client, "synthetic-bucket", "account/")
    store = Store(tmp_path / "tasks.sqlite")
    runner = TaskRunner(store)
    before = note.fingerprint()
    runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
    runner.join(3)
    assert note.fingerprint() == before
    key = receipt_key(note, provider)
    if failure and failure != "long":
        assert runner.current().status == TaskStatus.NEEDS_REVIEW
        count = len(writes)
        runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
        runner.join(3)
        assert len(writes) == count and runner.current().status == TaskStatus.NEEDS_REVIEW
        assert all(row["status"] != "linked" for row in store.resource_receipts(key))
        return
    assert runner.current().status == TaskStatus.SUCCEEDED
    assert content[0]["noteId"] != note.source_id
    assert all(row["status"] == "linked" for row in store.resource_receipts(key))
    restored_body = content[0]["body"]
    if failure == "long":
        assert content[0]["bodyType"] == 1
        object_key = provider._codec.decrypt(restored_body)
        restored_body = provider._codec.decrypt(base64.b64encode(objects[object_key]).decode("ascii"))
        assert "长😀" * 4000 in restored_body, "The object must contain the full body, not its shortened preview."
    blocks, assets = decode_body(restored_body)
    runner.start("readback", lambda ctx: provider._files.download(assets[0], assets[0].download_url, ctx))
    runner.join(3)
    assert blocks[0] == note.blocks[0] and blocks[2] == note.blocks[2]
    assert assets[0].sha256 == note.attachments[0].sha256


@pytest.mark.parametrize("failure", ["changed", "outside", "missing_reference", "duplicate", "nonimage"])
def test_invalid_or_unsupported_sources_cannot_create_a_cloud_file(tmp_path, failure):
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


def test_s3_uses_only_wps_credentials_and_disables_machine_profiles_endpoints_and_retries(tmp_path, monkeypatch):
    from note_bridge.providers import wps_files
    provider, _, _ = setup(tmp_path)
    provider.transport.json = lambda *args, **kwargs: {"region": "cn-north-1", "bucket": "synthetic-bucket", "keyPrefix": "account/",
        "accessKeyId": "synthetic-id", "secretAccessKey": "synthetic-secret", "sessionToken": "synthetic-session"}
    provider._codec.decrypt = lambda value: value
    def session(*, botocore_session):
        assert botocore_session.get_config_variable("config_file") == os.devnull
        assert botocore_session.get_config_variable("credentials_file") == os.devnull
        assert botocore_session.get_credentials().access_key == "synthetic-id"
        return SimpleNamespace(client=client)
    def client(service, **kwargs):
        assert service == "s3" and kwargs["aws_access_key_id"] == "synthetic-id"
        assert kwargs["config"].ignore_configured_endpoint_urls
        assert kwargs["config"].retries["total_max_attempts"] == 1
        return SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(wps_files.boto3, "Session", session)
    provider._files._client(True)
    provider._files.close()
    assert not provider._files.clients


def test_long_body_download_cannot_use_another_accounts_key_prefix(tmp_path):
    provider, _, _ = setup(tmp_path)
    provider._files._client = lambda: (SimpleNamespace(), "synthetic-bucket", "this-account/")
    with pytest.raises(BridgeError, match="当前账号"):
        provider._files.download_body("other-account/" + "a" * 32 + ".encrypted", None)
