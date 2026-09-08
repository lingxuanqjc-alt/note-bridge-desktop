from threading import Event
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError, WriteUncertain
from note_bridge.models import NoteDocument, PlatformId
from note_bridge.operations import migrate
from note_bridge.providers.wps import WpsProvider
from note_bridge.providers.wps_groups import available_name, folder_key, target_group
from note_bridge.storage import Store
from note_bridge.tasks import TaskRunner


def setup(tmp_path, failure=None):
    rows, writes = [{"groupId": "existing", "groupName": "工作", "valid": 1}], []
    def request(method, endpoint, **kwargs):
        assert method == "POST"
        if endpoint == "get/notegroup":
            return {"noteGroups": list(rows)}
        assert endpoint == "set/notegroup" and kwargs["write"]
        data = kwargs["json"]
        assert data["isNewGroup"] and data["valid"] == 1 and data["groupId"] != "existing"
        writes.append(data)
        if failure == "unknown":
            raise WriteUncertain()
        if failure != "missing":
            rows.append(dict(data))
        return {"updateTime": 100}
    provider = SimpleNamespace(account_id="target", _json=request)
    context = SimpleNamespace(store=Store(tmp_path / "state.sqlite"), check_cancel=lambda: None, cancelled=Event())
    note = NoteDocument(platform=PlatformId.HUAWEI, account_id="source", source_id="note-1",
                        source_folder_id="folder-1", source_folder_name="工作")
    return provider, note, context, rows, writes


def test_confirmed_mapping_survives_new_notes_and_does_not_merge_equal_folder_names(tmp_path):
    provider, note, context, rows, writes = setup(tmp_path)
    group, warnings = target_group(provider, note, context)
    assert warnings and rows[0]["groupName"] == "工作" and writes[0]["groupName"] == "工作 (2)"
    assert target_group(provider, note.model_copy(update={"source_id": "next"}), context)[0] == group
    assert len(writes) == 1
    other = note.model_copy(update={"source_folder_id": "other-folder"})
    assert target_group(provider, other, context)[0] != group
    assert folder_key(note, "other-target") != folder_key(note, "target")
    assert folder_key(note.model_copy(update={"account_id": "other-source"}), "target") != folder_key(note, "target")


@pytest.mark.parametrize("failure", ["unknown", "missing"])
def test_unconfirmed_upsert_never_runs_again_for_another_note_in_the_same_folder(tmp_path, failure):
    provider, note, context, _, writes = setup(tmp_path, failure)
    for source_id in ("first", "second"):
        with pytest.raises(WriteUncertain):
            target_group(provider, note.model_copy(update={"source_id": source_id}), context)
    assert len(writes) == 1
    receipt = context.store.receipt(folder_key(note, provider.account_id))
    assert receipt["status"] == "uncertain" and receipt["remote_ids"] == [writes[0]["groupId"]]


@pytest.mark.parametrize("still_listed", [False, True])
def test_deleted_target_group_is_not_silently_recreated(tmp_path, still_listed):
    provider, note, context, rows, writes = setup(tmp_path)
    group, _ = target_group(provider, note, context)
    if still_listed:
        next(r for r in rows if r["groupId"] == group)["valid"] = 0
    else:
        rows[:] = [r for r in rows if r["groupId"] != group]
    with pytest.raises(BridgeError, match="分组"):
        target_group(provider, note, context)
    assert len(writes) == 1


def test_ungrouped_notes_and_utf16_names_follow_the_official_input_contract(tmp_path):
    provider, note, context, _, writes = setup(tmp_path)
    assert target_group(provider, note.model_copy(update={"source_folder_name": None}), context) == (None, [])
    assert not writes
    assert available_name("工作/资料", set()) == "工作/资料"
    name = available_name("😀" * 20, {"😀" * 15})
    assert len(name.encode("utf-16-le")) // 2 <= 30 and name.endswith(" (2)")


def test_noteinfo_uses_note_group_while_drive_container_remains_separate(tmp_path):
    fake, note, context, _, writes = setup(tmp_path)
    info_requests, drive_requests = [], []
    def drive(method, endpoint, **kwargs):
        drive_requests.append((method, endpoint))
        if method == "GET":
            return {"id": 100}
        assert kwargs["json"]["groupid"] == 100
        return {"id": 200 + len(drive_requests)}
    provider = WpsProvider(SimpleNamespace(), SimpleNamespace(), tmp_path,
                           SimpleNamespace(json=drive))
    provider.account_id, provider.write_supported = "target", True
    provider._codec = SimpleNamespace(encrypt=lambda text: "synthetic-ciphertext")
    def request(method, endpoint, **kwargs):
        if endpoint == "set/noteinfo":
            info_requests.append(kwargs["json"])
            return {"infoVersion": 1}
        if endpoint == "set/notecontent":
            return {"contentVersion": 1}
        return fake._json(method, endpoint, **kwargs)
    provider._json = request
    runner = TaskRunner(context.store)
    runner.start("migrate", lambda ctx: migrate([note, note.model_copy(update={"source_id": "note-2"})], provider, context.store, ctx))
    runner.join(3)
    assert runner.current().succeeded == 2
    assert len(writes) == 1 and len(info_requests) == 2
    assert {r["groupId"] for r in info_requests} == {writes[0]["groupId"]}
