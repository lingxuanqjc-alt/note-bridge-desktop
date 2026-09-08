import json
from threading import Event
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError, WriteUncertain
from note_bridge.models import NoteDocument, PlatformId, account_fingerprint
from note_bridge.providers.xiaomi_groups import folder_key, folder_listing, target_group
from note_bridge.storage import Store


def setup(tmp_path, failure=None):
    rows = [{"id": "old", "subject": "工作", "status": "normal"}]
    writes = []
    def request(method, path, **kwargs):
        assert method == "POST" and path == "/note/folder" and kwargs["write"]
        data = json.loads(kwargs["data"]["entry"])
        assert set(data) == {"subject", "createDate", "modifyDate"}
        writes.append(data)
        if failure == "unknown":
            raise WriteUncertain()
        new_id = str(len(writes))
        if failure != "missing":
            rows.append({"id": new_id, "subject": data["subject"]})
        return {"entry": {"id": new_id}}
    provider = SimpleNamespace(account_id="target", _json=request, transport=SimpleNamespace(cookie=lambda _: "test-session"),
        _page=lambda cursor=None, **kwargs: {"folders": list(rows), "lastPage": True})
    note = NoteDocument(platform=PlatformId.MEIZU, account_id="source", source_id="first",
                        source_folder_id="folder", source_folder_name="工作")
    context = SimpleNamespace(store=Store(tmp_path / "state.sqlite"), check_cancel=lambda: None, cancelled=Event())
    return provider, note, context, rows, writes


def test_new_folder_does_not_merge_user_content_and_reuses_only_source_identity(tmp_path):
    provider, note, context, rows, writes = setup(tmp_path)
    group, warnings = target_group(provider, note, context)
    assert warnings and writes[0]["subject"] == "工作 (2)" and rows[0]["subject"] == "工作"
    assert target_group(provider, note.model_copy(update={"source_id": "next"}), context)[0] == group
    assert len(writes) == 1
    assert target_group(provider, note.model_copy(update={"source_folder_id": "other"}), context)[0] != group
    assert folder_key(note, "other-target") != folder_key(note, "target")
    assert folder_key(note.model_copy(update={"account_id": "other-source"}), "target") != folder_key(note, "target")


@pytest.mark.parametrize("failure", ["unknown", "missing"])
def test_unknown_creation_blocks_later_notes_without_retry(tmp_path, failure):
    provider, note, context, _, writes = setup(tmp_path, failure)
    for source in ("first", "second"):
        with pytest.raises(WriteUncertain):
            target_group(provider, note.model_copy(update={"source_id": source}), context)
    assert len(writes) == 1


def test_deleted_folder_is_not_recreated(tmp_path):
    provider, note, context, rows, writes = setup(tmp_path)
    target_group(provider, note, context)
    rows[-1]["status"] = "deleted"
    with pytest.raises(BridgeError):
        target_group(provider, note, context)
    assert len(writes) == 1


def test_folder_pagination_accepts_identical_repeats_but_rejects_changing_directory():
    def page(cursor=None, **kwargs):
        return {"folders": [{"id": "a", "subject": "work"}], "lastPage": cursor is not None, "syncTag": "next"}
    assert len(folder_listing(SimpleNamespace(_page=page))) == 1
    def changed(cursor=None, **kwargs):
        return {"folders": [{"id": "a", "subject": "changed" if cursor else "work"}], "lastPage": bool(cursor), "syncTag": "next"}
    with pytest.raises(BridgeError):
        folder_listing(SimpleNamespace(_page=changed))
    with pytest.raises(BridgeError):
        folder_listing(SimpleNamespace(_page=lambda *args, **kwargs: {"folders": [], "lastPage": False, "syncTag": "stuck"}))


def test_created_note_uses_confirmed_folder_without_overwriting_source_or_existing_notes(tmp_path):
    from note_bridge.providers.xiaomi import XiaomiProvider, parse_entry

    fake, note, context, rows, writes = setup(tmp_path)
    provider = XiaomiProvider(fake.transport, tmp_path)
    provider.account_id, provider.write_supported = account_fingerprint(PlatformId.XIAOMI, "test-session"), True
    provider.transport.json = lambda *a, **k: {"code": 0, "data": {"e2eeStatus": "close"}}
    provider.refresh_write_mode()
    provider._page = fake._page
    created = []

    def request(method, path, **kwargs):
        if path == "/note/folder":
            return fake._json(method, path, **kwargs)
        assert method == "POST" and path == "/note/note" and kwargs["write"]
        entry = json.loads(kwargs["data"]["entry"])
        assert "id" not in entry
        created.append(entry)
        return {"entry": {"id": "new-note"}}

    provider._json = request
    result = provider.create(note, context)
    assert result.remote_ids == ["new-note"] and len(writes) == 1
    assert created[0]["folderId"] == rows[-1]["id"] != "old"
    parsed = parse_entry({**created[0], "id": "new-note"}, "target", {rows[-1]["id"]: rows[-1]["subject"]})
    assert parsed.source_folder_name == "工作 (2)"
    assert "来源文件夹：工作" in parsed.plain_text
    assert note.source_folder_id == "folder" and note.source_folder_name == "工作"
