import json
from threading import Event
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError, WriteUncertain
from note_bridge.models import NoteDocument, PlatformId
from note_bridge.providers.huawei_groups import available_name, folder_key, parse_tags, target_group
from note_bridge.storage import Store


def setup(tmp_path, failure=None):
    rows = [{"uuid": "existing", "name": "工作", "type": 2, "delete_flag": 0, "user_order": 7}]
    writes = []
    def request(endpoint, data, **kwargs):
        if endpoint == "notetag/query":
            return {"rspInfo": {"noteList": [{"data": json.dumps({"content": r})} for r in rows]}, "ctagNoteTag": "tags"}
        assert endpoint == "notetag/create" and kwargs["write"]
        assert data["ctagNoteInfo"] == "notes" and data["ctagNoteTag"] == "tags" and data["startCursor"] == "latest"
        content = json.loads(data["reqInfo"]["data"])["content"]
        assert content["uuid"] == 0 and content["type"] == 2 and content["version"] == "8"
        writes.append(content)
        if failure == "unknown":
            raise WriteUncertain()
        group_id = "created-" + str(len(writes))
        if failure != "missing":
            rows.append({**content, "uuid": group_id})
        return {"rspInfo": {"uuid": group_id}}
    provider = SimpleNamespace(account_id="target", _json=request,
        _listing=lambda **kwargs: ({"ctagNoteInfo": "notes", "startCursor": "latest"}, []))
    context = SimpleNamespace(store=Store(tmp_path / "state.sqlite"), check_cancel=lambda: None, cancelled=Event())
    note = NoteDocument(platform=PlatformId.XIAOMI, account_id="source", source_id="first",
                        source_folder_id="folder", source_folder_name="工作")
    return provider, note, context, rows, writes


def test_preserves_existing_group_and_reuses_only_confirmed_source_folder_mapping(tmp_path):
    provider, note, context, rows, writes = setup(tmp_path)
    group, warnings = target_group(provider, note, context)
    assert warnings and writes[0]["name"] == "工作 (2)" and writes[0]["user_order"] == 8
    assert rows[0]["name"] == "工作"
    assert target_group(provider, note.model_copy(update={"source_id": "second"}), context)[0] == group
    assert len(writes) == 1
    assert target_group(provider, note.model_copy(update={"source_folder_id": "other"}), context)[0] != group
    assert folder_key(note, "other-account") != folder_key(note, "target")
    assert folder_key(note.model_copy(update={"account_id": "other-source"}), "target") != folder_key(note, "target")


@pytest.mark.parametrize("failure", ["unknown", "missing"])
def test_unknown_write_blocks_other_notes_in_folder_without_retry(tmp_path, failure):
    provider, note, context, _, writes = setup(tmp_path, failure)
    for source_id in ("first", "second"):
        with pytest.raises(WriteUncertain):
            target_group(provider, note.model_copy(update={"source_id": source_id}), context)
    assert len(writes) == 1
    assert context.store.receipt(folder_key(note, "target"))["status"] == "uncertain"


@pytest.mark.parametrize("change", ["removed", "deleted", "task"])
def test_missing_or_changed_mapping_never_creates_replacement(tmp_path, change):
    provider, note, context, rows, writes = setup(tmp_path)
    target_group(provider, note, context)
    if change == "removed":
        rows.pop()
    elif change == "deleted":
        rows[-1]["delete_flag"] = 1
    else:
        rows[-1]["type"] = 3
    with pytest.raises(BridgeError, match="分组"):
        target_group(provider, note, context)
    assert len(writes) == 1


def test_name_limit_and_default_group_and_empty_directory_order(tmp_path):
    provider, note, context, rows, writes = setup(tmp_path)
    assert target_group(provider, note.model_copy(update={"source_folder_name": None}), context) == ("", [])
    assert not writes
    rows.clear()
    target_group(provider, note, context)
    assert writes[0]["user_order"] == 6
    assert available_name("工作/资料", set()) == "工作/资料"
    name = available_name("😀" * 80, {"😀" * 64})
    assert len(name.encode("utf-16-le")) // 2 <= 128 and name.endswith(" (2)")


def test_malformed_directory_never_starts_write(tmp_path):
    provider, note, context, rows, writes = setup(tmp_path)
    rows[0].pop("user_order")
    with pytest.raises(BridgeError):
        target_group(provider, note, context)
    assert not writes and context.store.receipt(folder_key(note, "target")) is None


def test_cloud_content_wrapper_and_legacy_flat_group_preserve_the_same_identity():
    tag = {"uuid": "category-id", "name": "工作", "type": 2, "delete_flag": 0, "user_order": 6}
    for data in (tag, {"content": tag}):
        assert parse_tags({"rspInfo": {"noteList": [{"data": json.dumps(data)}]}}) == [tag]
    with pytest.raises(BridgeError):
        parse_tags({"rspInfo": {"noteList": [{"data": json.dumps({"content": None})}]}})
