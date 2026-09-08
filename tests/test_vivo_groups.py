from threading import Event
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError, WriteUncertain
from note_bridge.models import NoteDocument, PlatformId
from note_bridge.providers.vivo_groups import available_name, folder_key, target_group
from note_bridge.storage import Store


def setup(tmp_path, failure=None):
    rows = [{"guid": "existing", "name": "工作", "deleted": 1, "parentGuid": "-1", "sort": 2}]
    writes = []
    def request(method, path, data, **kwargs):
        assert method == "POST"
        if path == "/noteBook/getList":
            return list(rows)
        if path == "/sync/getSyncState":
            return {"updateCount": 10}
        assert path == "/noteBook/create" and not kwargs.get("encrypted") and kwargs["write"]
        assert data["lastUpdateCount"] == 10
        group = data["syncUp"][0]
        assert group["parentGuid"] == "-1" and group["deleted"] == 1
        writes.append(group)
        if failure == "unknown":
            raise WriteUncertain()
        if failure != "missing":
            rows.append(dict(group))
        return [{"updateSequenceNum": 11}]
    provider = SimpleNamespace(account_id="target", _json=request)
    note = NoteDocument(platform=PlatformId.XIAOMI, account_id="source", source_id="first",
                        source_folder_id="folder", source_folder_name="工作")
    context = SimpleNamespace(store=Store(tmp_path / "state.sqlite"), cancelled=Event(), check_cancel=lambda: None)
    return provider, note, context, rows, writes


def test_reuse_preserves_existing_user_groups_and_separates_source_folder_identities(tmp_path):
    provider, note, context, rows, writes = setup(tmp_path)
    group, warnings = target_group(provider, note, context)
    assert warnings and writes[0]["name"] == "工作 (2)" and writes[0]["sort"] == 3
    assert rows[0]["guid"] == "existing" and rows[0]["name"] == "工作"
    assert target_group(provider, note.model_copy(update={"source_id": "next"}), context)[0] == group
    assert len(writes) == 1
    assert target_group(provider, note.model_copy(update={"source_folder_id": "other"}), context)[0] != group
    assert folder_key(note, "other-account") != folder_key(note, "target")
    assert folder_key(note.model_copy(update={"account_id": "other"}), "target") != folder_key(note, "target")


@pytest.mark.parametrize("failure", ["unknown", "missing"])
def test_uncertain_creation_blocks_next_note_without_retry(tmp_path, failure):
    provider, note, context, _, writes = setup(tmp_path, failure)
    for source_id in ("first", "next"):
        with pytest.raises(WriteUncertain):
            target_group(provider, note.model_copy(update={"source_id": source_id}), context)
    assert len(writes) == 1
    assert context.store.receipt(folder_key(note, "target"))["remote_ids"] == [writes[0]["guid"]]


def test_deleted_mapping_is_not_recreated(tmp_path):
    provider, note, context, rows, writes = setup(tmp_path)
    target_group(provider, note, context)
    rows[-1]["deleted"] = 2
    with pytest.raises(BridgeError):
        target_group(provider, note, context)
    assert len(writes) == 1


def test_names_and_ungrouped_notes(tmp_path):
    provider, note, context, _, writes = setup(tmp_path)
    assert target_group(provider, note.model_copy(update={"source_folder_name": None}), context) == ("0", [])
    assert not writes
    name = available_name("工" * 70 + "😀", {"工" * 56})
    assert len(name.encode("utf-16-le")) // 2 <= 56 and name.endswith(" (2)")
    assert available_name("工作😀", set()) == "工作_"


def test_extreme_sort_does_not_reorder_original_folders(tmp_path):
    provider, note, context, rows, writes = setup(tmp_path)
    rows[0]["sort"] = 1e20
    with pytest.raises(BridgeError):
        target_group(provider, note, context)
    assert not writes and rows[0]["sort"] == 1e20
