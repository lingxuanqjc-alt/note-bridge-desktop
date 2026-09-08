import json
from threading import Event
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError, WriteUncertain
from note_bridge.models import NoteDocument, PlatformId
from note_bridge.providers.meizu import MeizuProvider
from note_bridge.providers.meizu_groups import available_name, folder_key, target_group
from note_bridge.storage import Store
from note_bridge.tasks import TaskRunner


def setup(tmp_path, failure=None):
    rows = [{"id": "-1", "name": "全部"}, {"id": "old", "name": "工作"}]
    writes = []
    def request(path, **kwargs):
        if path == "gettags":
            return {"data": list(rows)}
        assert path == "addNodeTag" and kwargs["write"] and not kwargs["mapping"]
        writes.append(kwargs["params"])
        if failure == "unknown":
            raise WriteUncertain()
        if failure != "missing":
            rows.append({"id": "created-" + str(len(writes)), "name": writes[-1]["name"]})
        return None
    provider = SimpleNamespace(account_id="target", _json=request)
    note = NoteDocument(platform=PlatformId.XIAOMI, account_id="source", source_id="first",
                        source_folder_id="folder", source_folder_name="工作")
    context = SimpleNamespace(store=Store(tmp_path / "state.sqlite"), cancelled=Event(), check_cancel=lambda: None)
    return provider, note, context, rows, writes


def test_group_mapping_reused_without_merging_existing_user_group_or_another_source_folder(tmp_path):
    provider, note, context, rows, writes = setup(tmp_path)
    group, warnings = target_group(provider, note, context)
    assert warnings and writes[0]["name"] == "工作 (2)" and rows[1]["name"] == "工作"
    assert target_group(provider, note.model_copy(update={"source_id": "second"}), context)[0] == group
    assert len(writes) == 1
    assert target_group(provider, note.model_copy(update={"source_folder_id": "other"}), context)[0] != group
    assert folder_key(note, "other-account") != folder_key(note, "target")
    assert folder_key(note.model_copy(update={"account_id": "other-source"}), "target") != folder_key(note, "target")


@pytest.mark.parametrize("failure", ["unknown", "missing"])
def test_uncertain_group_write_blocks_following_notes_without_retry(tmp_path, failure):
    provider, note, context, _, writes = setup(tmp_path, failure)
    for source_id in ("first", "next"):
        with pytest.raises(WriteUncertain):
            target_group(provider, note.model_copy(update={"source_id": source_id}), context)
    assert len(writes) == 1


def test_deleted_mapping_is_not_recreated(tmp_path):
    provider, note, context, rows, writes = setup(tmp_path)
    target_group(provider, note, context)
    rows.pop()
    with pytest.raises(BridgeError):
        target_group(provider, note, context)
    assert len(writes) == 1


def test_ungrouped_notes_and_utf16_name_limits(tmp_path):
    provider, note, context, _, writes = setup(tmp_path)
    assert target_group(provider, note.model_copy(update={"source_folder_name": None}), context) == ("-1", [])
    assert not writes
    name = available_name("😀" * 12, {"😀" * 8})
    assert len(name.encode("utf-16-le")) // 2 <= 16 and name.endswith(" (2)")


def test_fetch_keeps_group_identity_and_detects_missing_group(tmp_path):
    for missing in (False, True):
        provider = MeizuProvider(None, tmp_path)
        def request(path, **kwargs):
            if path == "gettags":
                return {"userId": "123", "data": [] if missing else [{"id": "folder", "name": "工作"}]}
            return {"count": 1, "content": [{"uuid": "note", "userId": 0, "groupStatus": "folder", "body": "[]"}]}
        provider._json = request
        provider.probe()
        runner = TaskRunner(Store(tmp_path / (str(missing) + ".sqlite")))
        snapshots = []
        runner.start("fetch", lambda ctx: snapshots.append(provider.fetch(ctx)))
        runner.join()
        assert len(snapshots) == 1
        if missing:
            assert not snapshots[0].complete and not snapshots[0].notes
        else:
            note = snapshots[0].notes[0]
            assert note.source_folder_id == "folder" and note.source_folder_name == "工作"


def test_create_uses_new_group_and_accepts_empty_success_value_for_group_only(tmp_path, monkeypatch):
    from note_bridge.providers import meizu_groups
    monkeypatch.setattr(meizu_groups, "target_group", lambda *args: ("new-group", []))
    sent = []
    def request(method, path, **kwargs):
        sent.append(kwargs)
        if path.endswith("addNodeTag"):
            return {"returnCode": 200, "returnValue": None}
        assert kwargs["data"]["groupUuid"] == "new-group"
        assert isinstance(json.loads(kwargs["data"]["body"]), list)
        return {"returnCode": 200, "returnValue": {"uuid": "new-note"}}
    provider = MeizuProvider(SimpleNamespace(json=request), tmp_path)
    provider.account_id, provider.write_supported = "target", True
    assert provider._json("addNodeTag", mapping=False, write=True) is None
    context = SimpleNamespace(check_cancel=lambda: None)
    result = provider.create(NoteDocument(platform=PlatformId.XIAOMI, account_id="source", source_id="note"), context)
    assert result.remote_ids == ["new-note"]
