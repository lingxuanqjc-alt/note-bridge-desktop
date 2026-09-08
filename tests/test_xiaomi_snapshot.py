from types import SimpleNamespace

import pytest

from note_bridge.models import NoteDocument, PlatformId, account_fingerprint
from note_bridge.operations import fetch_snapshot
from note_bridge.providers.xiaomi import XiaomiProvider
from note_bridge.storage import Store
from note_bridge.tasks import TaskRunner


def run_snapshot(tmp_path, change=None):
    state = {"uid": "source-user", "folder": "工作"}
    provider = XiaomiProvider(SimpleNamespace(cookie=lambda _: state["uid"]), tmp_path)
    account = account_fingerprint(PlatformId.XIAOMI, "source-user")
    provider.account_id = account
    store = Store(tmp_path / "state.sqlite")
    old = NoteDocument(platform=PlatformId.XIAOMI, account_id=account, source_id="old", title="old cache")
    store.replace_snapshot(PlatformId.XIAOMI, account, [old], True)

    def page(*args, **kwargs):
        return {"folders": [{"id": "group", "subject": state["folder"]}],
                "entries": [{"id": "note"}], "lastPage": True}

    def detail(method, path, **kwargs):
        assert method == "GET" and path == "/note/note/note/"
        if change == "account":
            state["uid"] = "different-user"
        if change == "folder":
            state["folder"] = "renamed"
        return {"entry": {"id": "wrong-note" if change == "detail" else "note", "content": "正文",
                          "folderId": "missing" if change == "missing" else "group"}}

    provider._page, provider._json = page, detail
    runner = TaskRunner(store)
    runner.start("fetch", lambda ctx: fetch_snapshot(provider, store, ctx))
    runner.join()
    return store.notes(PlatformId.XIAOMI, account), runner.current()


@pytest.mark.parametrize("change,code", [("account", "account_changed"), ("folder", "snapshot_changed")])
def test_changing_account_or_directory_cannot_replace_trusted_cache(tmp_path, change, code):
    notes, report = run_snapshot(tmp_path, change)
    # The runner reports partial progress after reading a note; no snapshot was committed.
    assert report.status == "partial" and code in {i.code for i in report.issues}
    assert [n.source_id for n in notes] == ["old"]


def test_unknown_group_keeps_content_and_identity_but_prevents_complete_snapshot(tmp_path):
    notes, report = run_snapshot(tmp_path, "missing")
    assert report.status == "partial" and "source_folder_missing" in {i.code for i in report.issues}
    assert len(notes) == 1 and notes[0].source_folder_id == "missing" and notes[0].plain_text == "正文"


def test_wrong_detail_never_enters_cache_as_the_requested_note(tmp_path):
    notes, report = run_snapshot(tmp_path, "detail")
    assert report.status == "partial" and "detail_mismatch" in {i.code for i in report.issues}
    assert notes == []


def test_stable_account_and_group_preserve_verified_folder_metadata(tmp_path):
    notes, report = run_snapshot(tmp_path)
    assert report.status == "succeeded" and not report.issues
    assert len(notes) == 1 and notes[0].source_folder_id == "group" and notes[0].source_folder_name == "工作"
