"""KSS block boundaries must not omit or duplicate any image bytes."""

import hashlib
import json

import pytest

from note_bridge.errors import BridgeError
from note_bridge.providers.xiaomi_files import BLOCK_BYTES, upload_metadata


def test_small_file_matches_independent_known_digest_vectors_and_json_shape():
    value = upload_metadata(b"abc", "fixture.png", "image/png")
    assert value == {"type": "note_img", "storage": {
        "filename": "fixture.png", "size": 3, "mimeType": "image/png",
        "sha1": "a9993e364706816aba3e25717850c26c9cd0d89d",
        "kss": {"block_infos": [{"blob": {}, "size": 3,
            "md5": "900150983cd24fb0d6963f7d28e17f72",
            "sha1": "a9993e364706816aba3e25717850c26c9cd0d89d"}]},
    }}
    assert json.loads(json.dumps(value)) == value
    assert "encryptInfo" not in value, "This encoder must not imply encrypted-account support."


@pytest.mark.parametrize("size", [BLOCK_BYTES - 1, BLOCK_BYTES, BLOCK_BYTES + 1, 2 * BLOCK_BYTES + 17])
def test_every_byte_is_represented_once_at_official_four_mib_boundaries(size):
    data = (bytes(range(251)) * (size // 251 + 1))[:size]
    storage = upload_metadata(data, "fixture.png", "image/png")["storage"]
    blocks = storage["kss"]["block_infos"]
    assert len(blocks) == (size + BLOCK_BYTES - 1) // BLOCK_BYTES
    assert sum(block["size"] for block in blocks) == size
    assert storage["sha1"] == hashlib.sha1(data, usedforsecurity=False).hexdigest()
    offset = 0
    for block in blocks:
        part = data[offset:offset + block["size"]]
        assert block["md5"] == hashlib.md5(part, usedforsecurity=False).hexdigest()
        assert block["sha1"] == hashlib.sha1(part, usedforsecurity=False).hexdigest()
        offset += block["size"]
    assert offset == len(data)


@pytest.mark.parametrize("data,name,mime", [(b"", "f.png", "image/png"), (b"a", "", "image/png"), (b"a", "f", "text/plain")])
def test_invalid_metadata_cannot_reach_the_cloud(data, name, mime):
    with pytest.raises(BridgeError):
        upload_metadata(data, name, mime)


def upload_setup(tmp_path, monkeypatch, failure=None, reused=False, node_suffix=""):
    import io
    from types import SimpleNamespace

    from PIL import Image

    from note_bridge.models import Attachment, PlatformId, account_fingerprint
    from note_bridge.providers import xiaomi_files
    from note_bridge.providers.xiaomi import XiaomiProvider

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "purple").save(buffer, format="PNG")
    data = buffer.getvalue()
    (tmp_path / "fixture.png").write_bytes(data)
    asset = Attachment(id="source", name="fixture.png", kind="image", mime="image/png",
                       local_path="fixture.png", size=len(data), sha256=hashlib.sha256(data).hexdigest())
    calls, receipts, issues = [], [], []
    context = SimpleNamespace(check_cancel=lambda: None, record_resource=lambda *row: receipts.append(row),
                              issue=lambda *row: issues.append(row))
    completed = {"fileId": "file-1", "digest": hashlib.sha1(data, usedforsecurity=False).hexdigest()}

    def api(method, path, **kwargs):
        assert method == "POST" and kwargs["write"]
        calls.append(path)
        if path.endswith("request_upload_file"):
            if failure == "request":
                raise xiaomi_files.WriteUncertain()
            if reused:
                return completed
            return {"storage": {"uploadId": "upload-1", "kss": {
                "node_urls": ["https://evil.invalid" if failure == "host" else "https://tos.xmssdn.micloud.mi.com" + node_suffix],
                "file_meta": "synthetic-meta", "block_metas": [{"is_existed": 0, "block_meta": "synthetic-block"}],
            }}}
        assert path.endswith("/commit")
        commit = json.loads(kwargs["data"]["commit"])
        assert commit["storage"]["kss"]["commit_metas"] == [{"commit_meta": "synthetic-commit"}]
        return completed

    class FileTransport:
        def __init__(self, origin, domains):
            assert origin == "https://tos.xmssdn.micloud.mi.com" and domains == ("xmssdn.micloud.mi.com",)

        def json(self, method, path, **kwargs):
            calls.append(path)
            assert kwargs["data"] == data and kwargs["write"]
            assert not ({"Cookie", "Authorization"} & set(kwargs["headers"]))
            assert "serviceToken" not in kwargs["params"]
            if failure == "block":
                raise xiaomi_files.WriteUncertain()
            return {"commit_meta": "synthetic-commit"}

        def close(self):
            pass

    def download(actual, ctx):
        calls.append("download")
        actual.size = asset.size
        actual.sha256 = "mismatch" if failure == "download" else asset.sha256

    provider = XiaomiProvider(SimpleNamespace(cookie=lambda _: "synthetic-session",
        json=lambda *a, **k: {"code": 0, "data": {"e2eeStatus": "close"}}), tmp_path)
    provider.account_id = account_fingerprint(PlatformId.XIAOMI, "synthetic-session")
    provider.refresh_write_mode()
    provider._json, provider._download = api, download
    monkeypatch.setattr(xiaomi_files, "Transport", FileTransport)
    return xiaomi_files.XiaomiFiles(provider), asset, context, calls, receipts, issues


@pytest.mark.parametrize("reused", [False, True])
def test_uploaded_or_deduplicated_file_requires_original_byte_readback(tmp_path, monkeypatch, reused):
    files, asset, context, calls, receipts, _ = upload_setup(tmp_path, monkeypatch, reused=reused)
    assert files.upload(asset, context)["fileId"] == "file-1"
    assert calls == (["/file/v2/user/request_upload_file", "download"] if reused else
                     ["/file/v2/user/request_upload_file", "/upload_block_chunk", "/file/v2/user/commit", "download"])
    assert ("xiaomi-file/file-1", "uploaded") in receipts


@pytest.mark.parametrize('name,expected', [('opaque-cloud-id', 'opaque-cloud-id.png'),
    ('image.PNG', 'image.png'), ('wrong.jpg', 'wrong.jpg.png')])
def test_upload_names_follow_verified_image_bytes_even_for_opaque_cloud_names(tmp_path, monkeypatch, name, expected):
    files, asset, context, _, _, _ = upload_setup(tmp_path, monkeypatch, reused=True)
    asset.name = name
    original = files.provider._json

    def request(method, path, **kwargs):
        metadata = json.loads(kwargs['data']['data'])['storage']
        assert metadata['filename'] == expected and metadata['mimeType'] == 'image/png'
        assert metadata['size'] == asset.size
        return original(method, path, **kwargs)

    files.provider._json = request
    files.upload(asset, context)


@pytest.mark.parametrize('code', [765432, 'sensitive-response-value', True])
def test_uncertain_response_keeps_only_numeric_diagnostic_and_never_retries(tmp_path, monkeypatch, code):
    from note_bridge.errors import WriteUncertain
    from note_bridge.providers.xiaomi import XiaomiProvider

    files, asset, context, _, _, issues = upload_setup(tmp_path, monkeypatch)
    requests = []
    def response(*args, **kwargs):
        requests.append(args)
        return {'code': code, 'message': 'private-session-value'}

    files.provider.transport.json = response
    files.provider._json = XiaomiProvider._json.__get__(files.provider)
    with pytest.raises(WriteUncertain):
        files.upload(asset, context)
    assert len(requests) == 1
    assert bool(issues) is (type(code) is int)
    assert 'private-session-value' not in str(issues) and 'sensitive-response-value' not in str(issues)


def test_file_node_path_prefix_is_preserved_and_default_https_port_is_allowed(tmp_path, monkeypatch):
    files, asset, context, calls, _, _ = upload_setup(tmp_path, monkeypatch, node_suffix=":443/service/path")
    files.upload(asset, context)
    assert "/service/path/upload_block_chunk" in calls


@pytest.mark.parametrize("suffix", [":8443", "/../escape", "/%2e%2e/escape", "/%5cescape", "?token=value", "#fragment"])
def test_node_authority_and_path_cannot_redirect_upload_outside_the_verified_contract(tmp_path, monkeypatch, suffix):
    from note_bridge.errors import WriteUncertain

    files, asset, context, calls, _, _ = upload_setup(tmp_path, monkeypatch, node_suffix=suffix)
    with pytest.raises(WriteUncertain):
        files.upload(asset, context)
    assert calls == ["/file/v2/user/request_upload_file"]


@pytest.mark.parametrize("failure", ["request", "host", "block", "download"])
def test_unknown_upload_stops_without_retry_or_falsely_confirming_a_resource(tmp_path, monkeypatch, failure):
    from note_bridge.errors import WriteUncertain

    files, asset, context, calls, receipts, _ = upload_setup(tmp_path, monkeypatch, failure)
    with pytest.raises(WriteUncertain):
        files.upload(asset, context)
    assert all(calls.count(call) == 1 for call in calls)
    assert not any(status == "uploaded" for _, status in receipts)
    if failure in ("request", "host", "block"):
        assert "/file/v2/user/commit" not in calls


def test_encryption_scope_and_changed_source_stop_before_any_allocation(tmp_path, monkeypatch):
    files, asset, context, calls, receipts, _ = upload_setup(tmp_path, monkeypatch)
    files.provider._clear_write_mode()
    with pytest.raises(BridgeError, match="加密"):
        files.upload(asset, context)
    files.provider.refresh_write_mode()
    (tmp_path / "fixture.png").write_bytes(b"changed")
    with pytest.raises(BridgeError):
        files.upload(asset, context)
    assert calls == receipts == []


@pytest.mark.parametrize("missing_reference", [False, True])
def test_note_creation_requires_both_body_and_metadata_association_before_success(tmp_path, monkeypatch, missing_reference):
    from threading import Event

    from note_bridge.errors import WriteUncertain
    from note_bridge.models import Block, NoteDocument, PlatformId
    from note_bridge.providers.xiaomi import XiaomiProvider

    files, asset, context, calls, _, _ = upload_setup(tmp_path, monkeypatch)
    provider = XiaomiProvider(files.provider.transport, tmp_path)
    provider.account_id = files.provider.account_id
    provider.write_supported = provider.images_supported = True
    provider.refresh_write_mode()
    provider._download = files.provider._download
    captured = []
    recorded = []
    context.record_remote_ids = lambda ids: recorded.extend(ids)
    context.cancelled = Event()

    def api(method, path, **kwargs):
        if path.startswith("/file/"):
            return files.provider._json(method, path, **kwargs)
        if method == "POST":
            assert path == "/note/note" and kwargs["write"]
            entry = json.loads(kwargs["data"]["entry"])
            assert "id" not in entry
            captured.append(entry)
            return {"entry": {"id": "new-note"}}
        assert path == "/note/note/new-note/" and recorded == ["new-note"]
        entry = {**captured[0], "id": "new-note"}
        if missing_reference:
            entry["content"] = "server lost image reference"
        return {"entry": entry}

    provider._json = api
    note = NoteDocument(platform=PlatformId.VIVO, account_id="synthetic-source", source_id="source-note",
                        title="fixture", attachments=[asset], blocks=[Block(kind="attachment", attachment_id=asset.id)])
    if missing_reference:
        with pytest.raises(WriteUncertain):
            provider.create(note, context)
    else:
        assert provider.create(note, context).remote_ids == ["new-note"]
    assert len(captured) == 1 and '<img fileid="file-1"/>' in captured[0]["content"]
    assert captured[0]["setting"]["data"][0]["fileId"] == "file-1"
    assert calls.count("/upload_block_chunk") == 1


@pytest.mark.parametrize("location", ["https://ali.xmssdn.micloud.mi.com/file?signature=synthetic",
                                      "https://evil.invalid/file", "https://account.xiaomi.com/login"])
def test_file_redirect_uses_a_new_connection_without_account_cookies(monkeypatch, location):
    from threading import Event
    from types import SimpleNamespace

    from note_bridge.providers import xiaomi_files

    closed, connections = [], []
    initial = SimpleNamespace(status_code=302, headers={"Location": location}, close=lambda: closed.append("initial"))
    final = SimpleNamespace(status_code=200, close=lambda: closed.append("file"))

    class FileTransport:
        def __init__(self, origin, domains):
            connections.append(origin)
            assert origin == "https://ali.xmssdn.micloud.mi.com"

        def request(self, method, path, **kwargs):
            assert method == "GET" and path == "/file?signature=synthetic"
            assert "headers" not in kwargs and "cookies" not in kwargs
            return final

        def close(self):
            closed.append("transport")

    monkeypatch.setattr(xiaomi_files, "Transport", FileTransport)
    provider = SimpleNamespace(transport=SimpleNamespace(origin="https://i.mi.com", request=lambda *a, **k: initial))
    ctx = SimpleNamespace(check_cancel=lambda: None, cancelled=Event())
    if "ali.xmssdn" in location:
        with xiaomi_files.download_response(provider, SimpleNamespace(id="file"), ctx) as result:
            assert result is final
        assert "file" in closed and "transport" in closed
    else:
        with pytest.raises(BridgeError):
            with xiaomi_files.download_response(provider, SimpleNamespace(id="file"), ctx):
                pytest.fail("Unverified redirect must not be fetched")
        assert connections == []


def test_redirect_inspection_does_not_follow_redirects_or_apply_to_writes():
    from types import SimpleNamespace

    import requests

    from note_bridge.providers.transport import Transport

    sent = []
    response = SimpleNamespace(status_code=302, close=lambda: None)
    def request(*args, **kwargs):
        sent.append(kwargs)
        assert kwargs["allow_redirects"] is False
        return response
    session = SimpleNamespace(headers={}, cookies=requests.cookies.RequestsCookieJar(), request=request)
    transport = Transport("https://i.mi.com", ("mi.com",), session=session)
    assert transport.request("GET", "/file/full", return_redirects=True) is response
    with pytest.raises(BridgeError) as error:
        transport.request("GET", "/file/full")
    assert error.value.code == "session_expired", "Default behavior must remain unchanged for other platforms."
    with pytest.raises(ValueError):
        transport.request("POST", "/note/note", write=True, return_redirects=True)
    assert len(sent) == 2
