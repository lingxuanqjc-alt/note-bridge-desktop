"""Account encryption evidence must gate writes without disabling independent reads."""

import time
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError
from note_bridge.models import Attachment, Block, NoteDocument, PlatformId, Span, account_fingerprint
from note_bridge.providers.xiaomi import XiaomiProvider
from note_bridge.providers.xiaomi_files import XiaomiFiles


class LocalTransport:
    def __init__(self):
        self.cookies = {"userId": "synthetic-user", "serviceToken": "synthetic-session"}
        self.session = object()
        self.mode = {"code": 0, "data": {"e2eeStatus": "close"}}
        self.during_mode = lambda: None
        self.calls = []

    def cookie(self, key):
        return self.cookies.get(key)

    def json(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        assert method == "GET" and not kwargs.get("write"), "These tests must never send cloud writes."
        if path == "/note/full/page":
            return {"code": 0, "data": {"entries": [], "folders": [], "lastPage": True}}
        assert path == "/mic/keybag/v1/getEncInfo"
        assert kwargs["params"]["hsid"] == 2 and kwargs["params"]["appId"] == "micloud"
        assert set(kwargs["params"]) == {"hsid", "appId", "ts"}
        self.during_mode()
        if isinstance(self.mode, Exception):
            raise self.mode
        return self.mode

    def close(self):
        self.cookies.clear()


@pytest.fixture
def provider(tmp_path):
    return XiaomiProvider(LocalTransport(), tmp_path)


def text_note():
    return NoteDocument(platform=PlatformId.VIVO, account_id="source", source_id="one",
                        blocks=[Block(spans=[Span(text="正文")])])


def blocked(provider, code=None):
    before = len(provider.transport.calls)
    for note in [text_note(), text_note().model_copy(update={
            "attachments": [Attachment(id="image", name="image.png", kind="image")]
    })]:
        with pytest.raises(BridgeError) as caught:
            provider.validate_notes([note])
        if code:
            assert caught.value.code == code
    assert len(provider.transport.calls) == before, "Per-note compatibility checks must stay local."


def test_preview_write_requires_official_mode_confirmation_then_stays_local(provider):
    with pytest.raises(BridgeError):
        provider.preflight([text_note()])
    assert provider.probe() == account_fingerprint(PlatformId.XIAOMI, "synthetic-user")
    provider.validate_notes([text_note()])
    provider.require_unencrypted_mode()
    assert [path for _, path, _ in provider.transport.calls] == [
        "/note/full/page", "/mic/keybag/v1/getEncInfo"]
    assert provider.write_supported and provider.images_supported
    provider.preflight([text_note()])
    assert len(provider.transport.calls) == 2, "Enabling preview does not add cloud writes or per-note mode reads."


@pytest.mark.parametrize("payload,code", [
    ({"code": 0, "data": {"e2eeStatus": "open"}}, "encrypted_account"),
    ({"code": 0, "data": {}}, "encryption_status_invalid"),
    ({"code": 0, "data": {"e2eeStatus": "CLOSE"}}, "encryption_status_invalid"),
    ({"code": 0, "data": {"e2eeStatus": False}}, "encryption_status_invalid"),
    ({"code": 1, "data": {"e2eeStatus": "close"}}, "encryption_status_invalid"),
    ({"code": False, "data": {"e2eeStatus": "close"}}, "encryption_status_invalid"),
    ({"code": 0.0, "data": {"e2eeStatus": "close"}}, "encryption_status_invalid"),
    ({"e2eeStatus": "close"}, "encryption_status_invalid"),
    ({"code": 0, "data": []}, "encryption_status_invalid"),
    (None, "encryption_status_invalid"),
    (BridgeError("session_expired", "private-token"), "encryption_status_unavailable"),
    (BridgeError("network_error", "private-token"), "encryption_status_unavailable"),
    (RuntimeError("private-token"), "encryption_status_unavailable"),
])
def test_failed_or_encrypted_mode_does_not_revoke_read_login_but_blocks_all_writes(provider, payload, code):
    provider.transport.mode = payload
    assert provider.probe(), "Read account verification succeeds independently of encryption capability."
    assert provider._page()["entries"] == [], "Independent reading remains usable."
    blocked(provider, code)
    assert "private-token" not in repr(provider._write_mode_error)
    provider.write_supported = provider.images_supported = True
    before = len(provider.transport.calls)
    with pytest.raises(BridgeError):
        provider.create(text_note(), SimpleNamespace())
    with pytest.raises(BridgeError):
        XiaomiFiles(provider).upload(SimpleNamespace(), SimpleNamespace())
    with pytest.raises(BridgeError):
        provider._json("POST", "/note/note", write=True)
    assert len(provider.transport.calls) == before


@pytest.mark.parametrize("change", ["account", "uid", "token", "session", "transport"])
def test_confirmed_scope_cannot_be_reused_with_another_account_or_backend_session(provider, change):
    provider.probe()
    if change == "account":
        provider.account_id = "another-account"
    elif change == "uid":
        provider.transport.cookies["userId"] = "another-user"
    elif change == "token":
        provider.transport.cookies["serviceToken"] = "another-session"
    elif change == "session":
        provider.transport.session = object()
    else:
        provider.transport = LocalTransport()
    blocked(provider, "account_changed")
    assert provider._write_scope is None


@pytest.mark.parametrize("change", ["uid", "token", "session"])
def test_mode_response_is_discarded_if_its_account_or_session_changes_during_the_read(provider, change):
    def replace():
        if change == "session":
            provider.transport.session = object()
        else:
            key = "userId" if change == "uid" else "serviceToken"
            provider.transport.cookies[key] = "changed"
    provider.transport.during_mode = replace
    if change == "uid":
        with pytest.raises(BridgeError, match="账号"):
            provider.probe()
        assert provider.account_id is None
    else:
        assert provider.probe(), "Token rotation does not invalidate a successful independent read login."
    assert provider._write_scope is None
    blocked(provider, "account_changed")


@pytest.mark.parametrize("elapsed", [600, 601, -1])
def test_expiry_or_monotonic_clock_anomaly_stops_before_any_remote_allocation(provider, monkeypatch, elapsed):
    clock = [1000.0]
    monkeypatch.setattr("note_bridge.providers.xiaomi.time.monotonic", lambda: clock[0])
    provider.probe()
    clock[0] += elapsed
    blocked(provider, "encryption_scope_expired")
    assert provider._write_scope is None


def test_failed_refresh_and_exit_clear_previously_confirmed_scope(provider):
    provider.probe()
    provider.transport.mode = BridgeError("request_forbidden", "private-response")
    provider.prepare_migration()
    blocked(provider, "encryption_status_unavailable")
    provider.transport.mode = {"code": 0, "data": {"e2eeStatus": "close"}}
    provider.prepare_migration()
    provider.require_unencrypted_mode()
    provider.close()
    assert provider._write_scope is None and provider.account_id is None


def contract(provider):
    return {"kind": "xiaomi-nonencrypted-upload-scope", "account_id": provider.account_id,
            "metadata_verified": True, "formal_acceptance": False, "cloud_writes": 0,
            "verified_at": time.time()}


def test_explicit_lab_contract_reuses_same_bound_guard_and_bare_flag_does_not(provider):
    provider.account_id = account_fingerprint(PlatformId.XIAOMI, "synthetic-user")
    provider.unencrypted_upload_verified = True
    blocked(provider, "encrypted_upload_pending")
    provider.confirm_upload_contract(contract(provider))
    provider.require_unencrypted_mode()
    assert provider.transport.calls == []
    provider.transport.cookies["serviceToken"] = "changed"
    blocked(provider, "account_changed")


@pytest.mark.parametrize("change", [{"account_id": "different"}, {"metadata_verified": 1},
    {"formal_acceptance": True}, {"cloud_writes": False}, {"cloud_writes": 1},
    {"verified_at": float("nan")}, {"verified_at": float("inf")}, {"verified_at": True},
    {"verified_at": 0}, {"kind": "other"}])
def test_lab_contract_requires_existing_explicit_evidence_shape_and_age(provider, change):
    provider.account_id = account_fingerprint(PlatformId.XIAOMI, "synthetic-user")
    with pytest.raises(BridgeError):
        provider.confirm_upload_contract({**contract(provider), **change})
    assert provider._write_scope is None


def test_old_lab_contract_cannot_override_explicit_server_encrypted_state(provider):
    provider.transport.mode = {"code": 0, "data": {"e2eeStatus": "open"}}
    provider.probe()
    with pytest.raises(BridgeError) as caught:
        provider.confirm_upload_contract(contract(provider))
    assert caught.value.code == "encrypted_account"
    assert provider._write_scope is None


def test_contract_keeps_original_age_instead_of_starting_a_new_600_second_window(provider, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("note_bridge.providers.xiaomi.time.time", lambda: now[0])
    monkeypatch.setattr("note_bridge.providers.xiaomi.time.monotonic", lambda: now[0])
    provider.account_id = account_fingerprint(PlatformId.XIAOMI, "synthetic-user")
    provider.confirm_upload_contract({**contract(provider), "verified_at": 401.0})
    provider.require_unencrypted_mode()
    now[0] = 1001.0
    blocked(provider, "encryption_scope_expired")


@pytest.mark.parametrize("mode,expected", [("close", ""), ("open", "encrypted_account"),
                                        (None, "encryption_status_invalid")])
def test_preview_refreshes_once_for_the_whole_list_and_displays_capability_reason(provider, tmp_path, mode, expected):
    from note_bridge.bridge import Bridge
    from note_bridge.paths import AppPaths
    from note_bridge.providers.base import SPECS

    provider.probe()
    provider.transport.mode = {"code": 0, "data": {"e2eeStatus": mode}}
    paths = AppPaths(tmp_path / "app")
    paths.prepare()
    bridge = Bridge(paths)
    notes = [text_note().model_copy(update={"source_id": str(index)}) for index in range(3)]
    bridge._providers[PlatformId.XIAOMI] = provider
    bridge._providers[PlatformId.VIVO] = SimpleNamespace(spec=SPECS[PlatformId.VIVO], account_id="source")
    bridge._store.replace_snapshot(PlatformId.VIVO, "source", notes, True)
    before = len(provider.transport.calls)
    result = bridge.preview_migration({"source": "vivo", "target": "xiaomi"})
    assert result["ok"] and len(result["data"]["items"]) == 3
    assert len(provider.transport.calls) == before + 1
    assert {row["code"] for row in result["data"]["items"]} == {expected}
    assert all(row["reason"] for row in result["data"]["items"]) is bool(expected)
    assert "synthetic-session" not in str(result) and "synthetic-user" not in str(result)
    assert provider.account_id and result["data"]["write_available"]
    assert all(row["compatible"] for row in result["data"]["items"]) is not bool(expected)


def test_selected_migration_rechecks_mode_after_preview_and_never_fetches_source_again(provider, tmp_path):
    from note_bridge.bridge import Bridge
    from note_bridge.operations import receipt_key
    from note_bridge.paths import AppPaths
    from note_bridge.providers.base import SPECS

    provider.probe()
    paths = AppPaths(tmp_path / "app")
    paths.prepare()
    bridge = Bridge(paths)
    source_calls = []
    def source_probe():
        source_calls.append("probe")
        return "source"
    bridge._providers[PlatformId.VIVO] = SimpleNamespace(
        spec=SPECS[PlatformId.VIVO], account_id="source", probe=source_probe)
    bridge._providers[PlatformId.XIAOMI] = provider
    note = text_note()
    bridge._store.replace_snapshot(PlatformId.VIVO, "source", [note], True)
    preview = bridge.preview_migration({"source": "vivo", "target": "xiaomi"})["data"]
    assert preview["items"][0]["compatible"]
    provider.transport.mode = {"code": 0, "data": {"e2eeStatus": "open"}}
    result = bridge.migrate_notes({"source": "vivo", "target": "xiaomi",
                                   "preview_token": preview["token"], "note_ids": [note.source_id]})
    assert result["ok"]
    bridge._runner.join(3)
    report = bridge._runner.current()
    assert report.status == "failed" and report.succeeded == 0
    assert any(issue.code == "encrypted_account" for issue in report.issues)
    assert bridge._store.receipt(receipt_key(note, provider)) is None
    assert source_calls == ["probe"]
    assert all(method == "GET" for method, _, _ in provider.transport.calls)
