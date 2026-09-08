"""Synthetic HTTP tests reject any request outside receipt-selected note IDs."""

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import requests

from note_bridge.errors import BridgeError, Cancelled
from note_bridge.models import Block, NoteDocument, Span, TaskReport, account_fingerprint
from note_bridge.providers.transport import Transport
from note_bridge.providers.vivo import VivoProvider
from note_bridge.storage import Store
from note_bridge.tasks import TaskContext, now

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from vivo_scoped_readback import ScopedReadback, read_notes, verify_account  # noqa: E402


class Endpoint:
    def __init__(self):
        self.exact_ids = ("synthetic-selected-a", "synthetic-selected-b")
        self.calls = []
        self.session_data = {"vivo_account_cookie_iqoo_openid": "synthetic-openid",
                             "vivo_account_cookie_iqoo_vivotoken": "synthetic-token"}
        self.user_data = {"userId": "synthetic-owner"}
        self.details = {identity: {"guid": identity, "userId": "synthetic-owner", "deleted": 1, "type": 1,
                                   "encryptType": 0, "title": identity, "noteBookGuid": "selected-group",
                                   "createTime": 1700000000000, "updateTime": 1700000001000, "resources": []}
                        for identity in self.exact_ids}
        self.details[self.exact_ids[0]]["resources"] = [{
            "guid": "synthetic-image", "noteGuid": self.exact_ids[0], "userId": "synthetic-owner",
            "name": "synthetic.png", "mime": "image/png", "resourceSize": 4,
            "domainAddr": "https://clouddisk-fixture.vivo.com.cn", "resourceKey": "selected-image",
        }]
        self.bodies = {self.exact_ids[0]: '<p>选中的合成正文</p><vnote-image guid="synthetic-image"></vnote-image>',
                       self.exact_ids[1]: '<h2>第二条合成正文</h2>'}
        self.folders = [{"guid": "selected-group", "name": "合成验收组"},
                        {"guid": "private-unselected-group", "name": "Never emitted"}]
        self.file_status = 200
        self.file_bytes = b"data"
        self.on_request = lambda *_: None

    def request(self, session, method, url, **kwargs):
        path = urlsplit(url).path
        data = kwargs.get("json", {})
        if "jvq_param" in data:
            data = json.loads(data["jvq_param"])
        guid = data.get("guid")
        self.calls.append((method, path, guid))
        # Fail at the HTTP boundary even if a caller accidentally broadens acquisition.
        assert "getAllNote" not in path and "statistics" not in path and "sync" not in path
        assert not any(part in path for part in ("create", "upload", "update", "delete"))
        if "/note/get" in path:
            assert guid in self.exact_ids, "A private/unselected note must never be requested"
        self.on_request(path, guid)
        if path.endswith("/account/getUserCookie"):
            assert method == "GET"
            value = self.session_data
        elif path.endswith("/account/getUserInfo"):
            assert method == "POST" and data == {"openId": "synthetic-openid"}
            value = self.user_data
        elif path.endswith("/noteBook/getList"):
            assert method == "POST"
            value = self.folders
        elif path.endswith("/note/getIncludeItem/v2"):
            assert method == "POST" and data == {"guid": guid, "syncProtocolVersion": 200}
            value = self.details[guid]
        elif path.endswith("/note/getContent/v2"):
            assert method == "POST" and data == {"guid": guid}
            value = self.bodies[guid]
        elif path.endswith("/getStsToken.do"):
            assert method == "POST" and data == {"tokenType": 1}
            value = {"stsToken": "synthetic-file-token"}
        elif path == "/api/file/webdisk/source/selected-image":
            assert method == "GET" and not session.cookies and "token" not in session.headers
            response = requests.Response()
            response.status_code, response._content = self.file_status, self.file_bytes
            response._content_consumed = True
            return response
        else:
            pytest.fail("Request outside fixed synthetic routes")
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"code": 0, "data": value}).encode()
        response._content_consumed = True
        return response


def setup(tmp_path, monkeypatch):
    endpoint = Endpoint()
    monkeypatch.setattr(requests.Session, "request", lambda session, *args, **kw: endpoint.request(session, *args, **kw))
    provider = VivoProvider(Transport("https://pc.vivo.com.cn", ("vivo.com.cn",)), tmp_path / "resources")
    # Wire encryption itself has dedicated tests; keep these HTTP scope assertions readable.
    provider._wire.close()
    provider._wire = SimpleNamespace(encrypt=lambda value: value, close=lambda: None)
    provider.account_id = account_fingerprint("vivo", "synthetic-owner")
    store = Store(tmp_path / "test.sqlite")
    private = NoteDocument(platform="vivo", account_id=provider.account_id, source_id="private-unselected",
                           blocks=[Block(spans=[Span(text="Unselected synthetic sentinel")])])
    store.replace_snapshot("vivo", provider.account_id, [private], False)
    report = TaskReport(id="synthetic-read-task", operation="scoped-read", started_at=now())
    context = TaskContext(report, store, lambda _: None)
    return endpoint, provider, store, context


def test_only_exact_ids_original_images_and_groups_are_read(tmp_path, monkeypatch):
    endpoint, provider, store, context = setup(tmp_path, monkeypatch)
    try:
        result = read_notes(provider, endpoint.exact_ids, context)
        assert isinstance(result, ScopedReadback) and result.complete
        assert result.exact_ids == endpoint.exact_ids
        assert [note.source_id for note in result.notes] == list(endpoint.exact_ids)
        assert {note.account_id for note in result.notes} == {result.account_id}
        assert all(note.source_folder_name == "合成验收组" for note in result.notes)
        asset = result.notes[0].attachments[0]
        assert asset.sha256 == hashlib.sha256(b"data").hexdigest() and asset.size == 4
        assert result.resource_keys == {endpoint.exact_ids[0]: {"synthetic-image": "selected-image"},
                                        endpoint.exact_ids[1]: {}}
        counts = Counter(path.rsplit("/", 1)[-1] for _, path, _ in endpoint.calls)
        assert counts["getList"] == 1 and counts["v2"] == 4 and counts["selected-image"] == 1
        assert {guid for _, _, guid in endpoint.calls if guid} == set(endpoint.exact_ids)
        # The existing incomplete whole-account cache must stay untouched.
        assert store.snapshot("vivo", provider.account_id) == {"complete": False, "count": 1}
        assert [note.source_id for note in store.notes("vivo", provider.account_id)] == ["private-unselected"]
    finally:
        provider.close()


def test_empty_scope_reads_nothing(tmp_path, monkeypatch):
    endpoint, provider, _, context = setup(tmp_path, monkeypatch)
    try:
        result = read_notes(provider, (), context)
        assert result.complete and result.notes == [] and result.exact_ids == ()
        assert endpoint.calls == []
    finally:
        provider.close()


def test_official_uncategorized_id_does_not_need_a_server_notebook_row(tmp_path, monkeypatch):
    endpoint, provider, store, context = setup(tmp_path, monkeypatch)
    identity = endpoint.exact_ids[0]
    endpoint.details[identity]["noteBookGuid"] = "0"
    try:
        result = read_notes(provider, endpoint.exact_ids, context)
        assert result.complete and len(result.notes) == 2
        note = result.notes[0]
        assert note.source_id == identity and note.source_folder_id == "0" and note.source_folder_name is None
        assert note.attachments[0].size == 4
        assert {guid for _, _, guid in endpoint.calls if guid} == set(endpoint.exact_ids)
        assert not store.snapshot("vivo", provider.account_id)["complete"]
    finally:
        provider.close()


@pytest.mark.parametrize("unknown_group", ["-1", "-2", "00", "unknown-group"])
def test_functional_views_and_unknown_groups_are_not_default_notebook_aliases(tmp_path, monkeypatch, unknown_group):
    endpoint, provider, _, context = setup(tmp_path, monkeypatch)
    endpoint.details[endpoint.exact_ids[0]]["noteBookGuid"] = unknown_group
    try:
        with pytest.raises(BridgeError) as error:
            read_notes(provider, endpoint.exact_ids, context)
        assert error.value.code == "group_mismatch"
        assert error.value.message == "指定测试笔记的分组未在当前目录中确认。"
        assert not any("getContent" in path or "webdisk/source" in path for _, path, _ in endpoint.calls)
    finally:
        provider.close()


@pytest.mark.parametrize("ids", [None, "synthetic-selected-a", [""], ["  x"], [1], [["x"]], ["x", "x"]])
def test_bad_scope_never_makes_a_request(tmp_path, monkeypatch, ids):
    endpoint, provider, _, context = setup(tmp_path, monkeypatch)
    try:
        with pytest.raises(BridgeError, match="限定读取"):
            read_notes(provider, ids, context)
        assert endpoint.calls == []
    finally:
        provider.close()


@pytest.mark.parametrize(("field", "value", "code"), [
    ("guid", "private-unselected", "detail_mismatch"), ("userId", "different-owner", "account_mismatch"),
    ("deleted", 0, "snapshot_changed"), ("type", 2, "proprietary_content"),
    ("encryptType", 1, "encrypted_note"), ("noteBookGuid", "missing-group", "group_mismatch"),
])
def test_wrong_note_state_cannot_trigger_body_or_resource_reads(tmp_path, monkeypatch, field, value, code):
    endpoint, provider, _, context = setup(tmp_path, monkeypatch)
    endpoint.details[endpoint.exact_ids[0]][field] = value
    try:
        with pytest.raises(BridgeError) as error:
            read_notes(provider, endpoint.exact_ids, context)
        assert error.value.code == code
        assert not any("getContent" in path or "webdisk/source" in path for _, path, _ in endpoint.calls)
    finally:
        provider.close()


@pytest.mark.parametrize("change", ["note_owner", "account_owner", "duplicate", "bad_list"])
def test_all_resources_are_checked_before_any_body_or_image_request(tmp_path, monkeypatch, change):
    endpoint, provider, _, context = setup(tmp_path, monkeypatch)
    resources = endpoint.details[endpoint.exact_ids[0]]["resources"]
    if change == "note_owner":
        resources[0]["noteGuid"] = "private-unselected"
    elif change == "account_owner":
        resources[0]["userId"] = "different-owner"
    elif change == "duplicate":
        resources.append(dict(resources[0]))
    else:
        endpoint.details[endpoint.exact_ids[0]]["resources"] = {}
    try:
        with pytest.raises(BridgeError):
            read_notes(provider, endpoint.exact_ids, context)
        assert not any("getContent" in path or "webdisk/source" in path for _, path, _ in endpoint.calls)
    finally:
        provider.close()


def test_asset_failure_never_claims_complete_or_updates_snapshot(tmp_path, monkeypatch):
    endpoint, provider, store, context = setup(tmp_path, monkeypatch)
    endpoint.file_bytes = b"truncated-and-wrong-size"
    try:
        with pytest.raises(BridgeError) as error:
            read_notes(provider, endpoint.exact_ids, context)
        assert error.value.code == "attachment_incomplete"
        assert not store.snapshot("vivo", provider.account_id)["complete"]
        assert not any(guid == endpoint.exact_ids[1] for _, _, guid in endpoint.calls)
    finally:
        provider.close()


def test_unsupported_body_stays_explicitly_incomplete(tmp_path, monkeypatch):
    endpoint, provider, _, context = setup(tmp_path, monkeypatch)
    endpoint.bodies[endpoint.exact_ids[1]] = '<vnote-unknown>synthetic</vnote-unknown>'
    try:
        result = read_notes(provider, endpoint.exact_ids, context)
        assert not result.complete and len(result.notes) == 2
        assert "incomplete_scoped_read" in {issue.code for issue in context.report.issues}
    finally:
        provider.close()


def test_cancelled_scope_has_no_requests(tmp_path, monkeypatch):
    endpoint, provider, _, context = setup(tmp_path, monkeypatch)
    context.cancelled.set()
    try:
        with pytest.raises(Cancelled):
            read_notes(provider, endpoint.exact_ids, context)
        assert endpoint.calls == []
    finally:
        provider.close()


def test_account_switch_stops_before_next_body(tmp_path, monkeypatch):
    endpoint, provider, _, context = setup(tmp_path, monkeypatch)
    endpoint.on_request = lambda path, guid: setattr(provider, "account_id", "changed") if "getIncludeItem" in path else None
    try:
        with pytest.raises(BridgeError) as error:
            read_notes(provider, endpoint.exact_ids, context)
        assert error.value.code == "account_changed"
        assert not any("getContent" in path for _, path, _ in endpoint.calls)
    finally:
        provider.close()


def test_account_confirmation_uses_only_existing_two_login_endpoints(tmp_path, monkeypatch):
    endpoint, provider, _, _ = setup(tmp_path, monkeypatch)
    provider.account_id = None
    try:
        account = verify_account(provider)
        assert account == account_fingerprint("vivo", "synthetic-owner")
        assert provider.account_id == account
        assert [path for _, path, _ in endpoint.calls] == [
            "/note-api/account/getUserCookie", "/note-api/account/getUserInfo"]
    finally:
        provider.close()


@pytest.mark.parametrize("failure", ["session_shape", "token_missing", "openid_missing", "user_missing", "account_changed"])
def test_unconfirmed_or_changed_identity_clears_ram_and_stops(tmp_path, monkeypatch, failure):
    endpoint, provider, _, _ = setup(tmp_path, monkeypatch)
    if failure == "session_shape":
        endpoint.session_data = []
    elif failure == "token_missing":
        endpoint.session_data.pop("vivo_account_cookie_iqoo_vivotoken")
    elif failure == "openid_missing":
        endpoint.session_data["vivo_account_cookie_iqoo_openid"] = 123
    elif failure == "user_missing":
        endpoint.user_data = {}
    else:
        endpoint.user_data = {"userId": "different-owner"}
    provider.transport.session.cookies.set("synthetic", "opaque")
    with pytest.raises(BridgeError) as error:
        verify_account(provider)
    assert error.value.code == ("account_changed" if failure == "account_changed" else "login_incomplete")
    assert provider.account_id is None and provider._openid is None and not provider.transport.session.cookies
    assert "token" not in provider.transport.session.headers and "openId" not in provider.transport.session.headers
