from threading import Event
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError, WriteUncertain
from note_bridge.models import NoteDocument, PlatformId
from note_bridge.providers.honor import HonorProvider
from note_bridge.providers.honor_groups import DEFAULT_FOLDER, available_name, folder_key, target_group
from note_bridge.storage import Store


def setup(tmp_path, failure=None):
    rows = [{"uuid": "existing", "display_name": "工作", "type": 1, "parent_uuid": "presetFolder", "user_order": 2}]
    writes = []
    def request(method, path, **kwargs):
        assert method == "POST"
        if path.endswith("getFolderList"):
            return list(rows)
        assert path == "notepad/note/folder/update" and kwargs["write"]
        group = kwargs["json"][0]
        assert "create_time" not in group and "modify_time" not in group
        assert group["parent_uuid"] == "presetFolder" and group["type"] == 1
        writes.append(group)
        if failure == "unknown":
            raise WriteUncertain()
        if failure != "missing":
            rows.append(dict(group))
        return None
    provider = SimpleNamespace(account_id="target", _json=request)
    note = NoteDocument(platform=PlatformId.XIAOMI, account_id="source", source_id="first",
                        source_folder_id="folder", source_folder_name="工作")
    context = SimpleNamespace(store=Store(tmp_path / "state.sqlite"), cancelled=Event(), check_cancel=lambda: None)
    return provider, note, context, rows, writes


def test_reuse_does_not_merge_existing_folder_or_distinct_source_identity(tmp_path):
    provider, note, context, rows, writes = setup(tmp_path)
    group, warnings = target_group(provider, note, context)
    assert warnings and writes[0]["display_name"] == "工作 (2)" and writes[0]["user_order"] == 3
    assert rows[0]["uuid"] == "existing" and rows[0]["display_name"] == "工作"
    assert target_group(provider, note.model_copy(update={"source_id": "next"}), context)[0] == group
    assert len(writes) == 1
    assert target_group(provider, note.model_copy(update={"source_folder_id": "other"}), context)[0] != group
    assert folder_key(note, "other-target") != folder_key(note, "target")
    assert folder_key(note.model_copy(update={"account_id": "other-source"}), "target") != folder_key(note, "target")


@pytest.mark.parametrize("failure", ["unknown", "missing"])
def test_uncertain_group_stops_following_notes_without_retry(tmp_path, failure):
    provider, note, context, _, writes = setup(tmp_path, failure)
    for source_id in ("first", "next"):
        with pytest.raises(WriteUncertain):
            target_group(provider, note.model_copy(update={"source_id": source_id}), context)
    assert len(writes) == 1
    assert context.store.receipt(folder_key(note, "target"))["remote_ids"] == [writes[0]["uuid"]]


@pytest.mark.parametrize("change", ["removed", "deleted", "type"])
def test_changed_mapping_is_not_recreated(tmp_path, change):
    provider, note, context, rows, writes = setup(tmp_path)
    target_group(provider, note, context)
    if change == "removed":
        rows.pop()
    elif change == "deleted":
        rows[-1]["delete_flag"] = 1
    else:
        rows[-1]["type"] = 2
    with pytest.raises(BridgeError):
        target_group(provider, note, context)
    assert len(writes) == 1


def test_default_notes_and_name_limit(tmp_path):
    provider, note, context, _, writes = setup(tmp_path)
    assert target_group(provider, note.model_copy(update={"source_folder_name": None}), context) == (DEFAULT_FOLDER, [])
    assert not writes
    name = available_name("😀" * 40, {"😀" * 25})
    assert len(name.encode("utf-16-le")) // 2 <= 50 and name.endswith(" (2)")


def test_created_note_uses_the_new_folder_not_default(tmp_path, monkeypatch):
    from note_bridge.providers import honor_groups
    monkeypatch.setattr(honor_groups, "target_group", lambda *args: ("new-folder", []))
    provider = HonorProvider(None, tmp_path)
    provider.account_id, provider.write_supported = "target", True
    writes = []
    def request(method, path, **kwargs):
        assert path == "notepad/noteSave"
        row = kwargs["json"][0]
        assert row["folder_uuid"] == "new-folder"
        writes.append(row)
        return [{"uuid": row["uuid"]}]
    provider._json = request
    context = SimpleNamespace(check_cancel=lambda: None, cancelled=Event(), record_remote_ids=lambda ids: None)
    result = provider.create(NoteDocument(platform=PlatformId.XIAOMI, account_id="source", source_id="note"), context)
    assert result.remote_ids == [writes[0]["uuid"]]
