from threading import Event
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError, WriteUncertain
from note_bridge.models import NoteDocument, PlatformId
from note_bridge.operations import migrate
from note_bridge.providers.oppo import OppoProvider
from note_bridge.providers.oppo_groups import DEFAULT_GROUP, available_name, folder_key, target_group
from note_bridge.storage import Store
from note_bridge.tasks import TaskRunner


def setup(tmp_path, failure=None):
    rows, writes = [{"groupGuid": "existing", "groupName": "工作"}], []
    def request(path, payload=None, **kwargs):
        if path.endswith("group-list-new"):
            return list(rows)
        assert path.endswith("add-group") and kwargs["write"]
        writes.append(payload)
        if failure == "unknown":
            raise WriteUncertain()
        group = {"groupGuid": f"new-{len(writes)}", "groupName": payload["groupName"]}
        if failure != "directory_missing":
            rows.append(group)
        return group
    provider = SimpleNamespace(account_id="target-account", _json=request)
    context = SimpleNamespace(store=Store(tmp_path / "state.sqlite"), check_cancel=lambda: None, cancelled=Event())
    note = NoteDocument(platform=PlatformId.XIAOMI, account_id="source-account", source_id="note-1",
                        source_folder_id="folder-1", source_folder_name="工作")
    return provider, note, context, rows, writes


def test_folder_mapping_reuses_confirmed_group_without_merging_existing_user_folder(tmp_path):
    provider, note, context, rows, writes = setup(tmp_path)
    first, warnings = target_group(provider, note, context)
    assert first != "existing" and warnings
    assert rows[0] == {"groupGuid": "existing", "groupName": "工作"}
    assert rows[1]["groupName"] == "工作 (2)"
    second, _ = target_group(provider, note.model_copy(update={"source_id": "note-2"}), context)
    assert second == first and len(writes) == 1, "Notes in the same source folder must stay together across resumed tasks."
    different, _ = target_group(provider, note.model_copy(update={"source_folder_id": "folder-2"}), context)
    assert different != first, "Distinct source folders with equal names must not be silently merged."


@pytest.mark.parametrize("failure", ["unknown", "directory_missing"])
def test_uncertain_folder_creation_blocks_other_notes_without_repeating_the_write(tmp_path, failure):
    provider, note, context, _, writes = setup(tmp_path, failure)
    with pytest.raises(WriteUncertain):
        target_group(provider, note, context)
    with pytest.raises(WriteUncertain):
        target_group(provider, note.model_copy(update={"source_id": "another-note"}), context)
    assert len(writes) == 1
    assert context.store.receipt(folder_key(note, provider.account_id))["status"] == "uncertain"


def test_removed_mapping_stops_instead_of_recreating_or_routing_to_default(tmp_path):
    provider, note, context, rows, writes = setup(tmp_path)
    group, _ = target_group(provider, note, context)
    rows[:] = [r for r in rows if r["groupGuid"] != group]
    with pytest.raises(BridgeError, match="分组"):
        target_group(provider, note, context)
    assert len(writes) == 1


def test_group_identity_isolates_both_accounts_and_default_notes_do_not_create_folders(tmp_path):
    provider, note, context, _, writes = setup(tmp_path)
    assert folder_key(note, "account-A") != folder_key(note, "account-B")
    assert folder_key(note, "account-A") != folder_key(note.model_copy(update={"account_id": "other"}), "account-A")
    assert target_group(provider, note.model_copy(update={"source_folder_name": None}), context) == (DEFAULT_GROUP, [])
    assert writes == []


def test_names_follow_official_utf16_limit_without_splitting_emoji():
    result = available_name("😀" * 40 + "/", set())
    assert len(result.encode("utf-16-le")) // 2 <= 50
    assert "/" not in result
    assert available_name("A/B", {"A_B"}) == "A_B (2)"


def test_real_create_routes_each_note_to_the_durable_folder_mapping(tmp_path):
    fake, note, context, _, group_writes = setup(tmp_path)
    provider = OppoProvider(SimpleNamespace(origin="https://owork-api-cn.oppo.com", session=SimpleNamespace(headers={})), tmp_path)
    provider.account_id, provider.write_supported = fake.account_id, True
    entries = []
    def request(path, payload=None, **kwargs):
        if path.endswith("/add"):
            entries.append(payload)
            return {"recordId": f"note-{len(entries)}"}
        return fake._json(path, payload, **kwargs)
    provider._json = request
    runner = TaskRunner(context.store)
    notes = [note, note.model_copy(update={"source_id": "note-2"})]
    runner.start("migrate", lambda ctx: migrate(notes, provider, context.store, ctx))
    runner.join(3)
    assert runner.current().status == "partial", "Only the documented collision rename should require attention."
    assert runner.current().succeeded == 2
    assert len(group_writes) == 1 and len(entries) == 2
    assert {entry["groupGuid"] for entry in entries} == {"new-1"}, "Preserved source groups must affect the cloud write, not just the body annotation."
