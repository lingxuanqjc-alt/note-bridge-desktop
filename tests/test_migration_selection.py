"""Selection must preserve an explicit scope without weakening the write boundary."""

import hashlib

import pytest
from PIL import Image

from note_bridge.bridge import Bridge
from note_bridge.errors import BridgeError
from note_bridge.models import Attachment, Block, NoteDocument, PlatformId, Span, TaskStatus
from note_bridge.operations import receipt_key
from note_bridge.paths import AppPaths
from note_bridge.providers.base import SPECS, CreatedNote, Provider, Snapshot
from note_bridge.providers.factory import create_provider


class LocalProvider(Provider):
    read_supported = write_supported = images_supported = True

    def __init__(self, platform, notes=()):
        self.spec = SPECS[platform]
        self.account_id = platform + "-synthetic-account"
        self.notes = list(notes)
        self.calls = []

    def probe(self):
        self.calls.append("probe")
        return self.account_id

    def fetch(self, context):
        self.calls.append("fetch")
        return Snapshot(self.notes, True)

    def create(self, note, context):
        self.calls.append(("create", note.source_id))
        return CreatedNote(["created-" + note.source_id])


@pytest.fixture
def selection_app(tmp_path):
    paths = AppPaths(tmp_path)
    paths.prepare()
    bridge = Bridge(paths)
    source = LocalProvider(PlatformId.VIVO)
    source.notes = [NoteDocument(platform=source.spec.id, account_id=source.account_id, source_id=key,
                                 title=title, blocks=[Block(spans=[Span(text="正文 " + key)])])
                    for key, title in [("one", "同名"), ("two", "同名"), ("audio", "音频笔记")]]
    source.notes[-1].attachments = [Attachment(id="audio", name="audio.wav", kind="audio")]
    target = LocalProvider(PlatformId.MEIZU)
    bridge._providers[source.spec.id] = source
    bridge._providers[target.spec.id] = target
    bridge._store.replace_snapshot(source.spec.id, source.account_id, source.notes, True)
    direction = {"source": "vivo", "target": "meizu"}
    return bridge, source, target, direction


def prepare(app):
    bridge, _, _, direction = app
    result = bridge.preview_migration(direction)
    assert result["ok"]
    return result["data"]


def run_selection(app, preview, ids):
    bridge, _, _, direction = app
    return bridge.migrate_notes({**direction, "preview_token": preview["token"], "note_ids": ids})


def item(preview, identifier):
    return next(row for row in preview["items"] if row["id"] == identifier)


def test_preview_is_read_only_and_identifies_nonimage_without_stripping_it(selection_app):
    bridge, source, target, _ = selection_app
    preview = prepare(selection_app)
    assert preview["total"] == 3
    assert {row["id"]: row["compatible"] for row in preview["items"]} == {"one": True, "two": True, "audio": False}
    assert item(preview, "audio")["code"] == "unsupported_attachment"
    assert "音频" in item(preview, "audio")["reason"]
    assert source.calls == target.calls == []
    assert bridge._store.recent_tasks() == []
    assert next(note for note in bridge._store.notes(source.spec.id, source.account_id)
                if note.source_id == "audio").attachments[0].kind == "audio"
    assert "account" not in preview and "source_account" not in preview


def test_only_explicit_ids_are_created_and_exclusions_cannot_look_successful(selection_app):
    bridge, source, target, _ = selection_app
    original = {note.source_id: note.fingerprint() for note in source.notes}
    preview = prepare(selection_app)
    result = run_selection(selection_app, preview, ["two"])
    assert result["ok"]
    bridge._runner.join(3)
    report = bridge._runner.current()
    assert source.calls == ["probe"], "Selected migration must not repeat the full source fetch."
    assert target.calls == ["probe", ("create", "two")]
    assert report.status == TaskStatus.PARTIAL
    assert (report.succeeded, report.skipped, report.total) == (1, 0, 1)
    assert {item.note_id for item in report.issues if item.code == "not_selected"} == {"one", "audio"}
    assert any("音频" in item.message for item in report.issues)
    assert {note.source_id: note.fingerprint() for note in bridge._store.notes(source.spec.id, source.account_id)} == original
    assert not run_selection(selection_app, preview, ["two"])["ok"], "Consumed selections cannot launch again."


def test_all_selected_compatible_notes_can_complete_successfully(selection_app):
    bridge, source, target, _ = selection_app
    source.notes = source.notes[:2]
    bridge._store.replace_snapshot(source.spec.id, source.account_id, source.notes, True)
    assert run_selection(selection_app, prepare(selection_app), ["one", "two"])["ok"]
    bridge._runner.join(3)
    report = bridge._runner.current()
    assert report.status == TaskStatus.SUCCEEDED and report.succeeded == 2
    assert not report.issues
    assert target.calls == ["probe", ("create", "one"), ("create", "two")]


def test_legacy_full_account_path_refreshes_target_capability_after_fetch_once(selection_app):
    bridge, source, target, direction = selection_app
    source.notes = source.notes[:2]
    events = []
    fetch = source.fetch
    def fetch_once(ctx):
        events.append("fetch")
        return fetch(ctx)
    source.fetch = fetch_once
    target.prepare_migration = lambda: events.append("capability")
    assert bridge.migrate_notes(direction)["ok"]
    bridge._runner.join(3)
    assert events == ["fetch", "capability"]
    assert bridge._runner.current().status == TaskStatus.SUCCEEDED


@pytest.mark.parametrize("ids", [[], ["one", "one"], ["other-account-note"], ["audio"]])
def test_empty_duplicate_foreign_or_incompatible_selection_never_writes(selection_app, ids):
    bridge, source, target, _ = selection_app
    assert not run_selection(selection_app, prepare(selection_app), ids)["ok"]
    assert source.calls == target.calls == []
    assert bridge._store.recent_tasks() == []


@pytest.mark.parametrize("change", ["source_account", "target_account", "unselected_content", "warnings", "incomplete"])
def test_preview_cannot_survive_account_or_whole_snapshot_changes(selection_app, change):
    bridge, source, target, _ = selection_app
    preview = prepare(selection_app)
    if change == "source_account":
        source.account_id = "another-source"
    elif change == "target_account":
        target.account_id = "another-target"
    else:
        notes = [note.model_copy(deep=True) for note in source.notes]
        if change == "unselected_content":
            notes[-1].title = "Changed after preview"
        if change == "warnings":
            notes[0].warnings.append("新的内容缺失提示")
        bridge._store.replace_snapshot(source.spec.id, source.account_id, notes, change != "incomplete")
    assert not run_selection(selection_app, preview, ["one"])["ok"]
    assert source.calls == target.calls == []
    assert bridge._store.recent_tasks() == []


def test_token_and_ids_are_required_together_and_latest_preview_wins(selection_app):
    bridge, _, target, direction = selection_app
    old = prepare(selection_app)
    latest = prepare(selection_app)
    assert old["token"] != latest["token"]
    for extra in [{"note_ids": ["one"]}, {"preview_token": latest["token"]},
                  {"preview_token": "invalid", "note_ids": ["one"]},
                  {"preview_token": old["token"], "note_ids": ["one"]}]:
        assert not bridge.migrate_notes({**direction, **extra})["ok"]
    assert target.calls == []


def test_preview_does_not_enable_a_production_write_gate(selection_app):
    bridge, source, target, _ = selection_app
    target.write_supported = False
    preview = prepare(selection_app)
    assert not preview["write_available"] and preview["blocked_reason"]
    assert item(preview, "one")["compatible"], "Local compatibility is separate from release readiness."
    result = run_selection(selection_app, preview, ["one"])
    assert result["error"]["code"] == "protocol_pending"
    assert source.calls == target.calls == []
    assert not target.write_supported


def test_account_change_during_final_probe_stops_before_any_create(selection_app):
    bridge, source, target, _ = selection_app
    preview = prepare(selection_app)
    target.probe = lambda: "different-cloud-account"
    assert run_selection(selection_app, preview, ["one"])["ok"]
    bridge._runner.join(3)
    report = bridge._runner.current()
    assert report.status == TaskStatus.FAILED and report.succeeded == 0
    assert target.calls == [] and source.calls == ["probe"]
    assert any(issue.code == "account_changed" for issue in report.issues)


def test_uncertain_receipt_is_visible_and_cannot_be_selected(selection_app):
    bridge, source, target, _ = selection_app
    bridge._store.save_receipt(receipt_key(source.notes[0], target), "uncertain", ["draft"])
    preview = prepare(selection_app)
    assert item(preview, "one")["code"] == "write_uncertain"
    assert not run_selection(selection_app, preview, ["one"])["ok"]
    assert target.calls == []
    assert bridge._store.receipt(receipt_key(source.notes[0], target))["status"] == "uncertain"


def test_confirmed_receipt_remains_a_skip_and_source_warnings_survive(selection_app):
    bridge, source, target, _ = selection_app
    source.notes[0].warnings = ["排版采用普通段落"]
    bridge._store.replace_snapshot(source.spec.id, source.account_id, source.notes, True)
    bridge._store.save_receipt(receipt_key(source.notes[0], target), "confirmed", ["existing"])
    preview = prepare(selection_app)
    assert item(preview, "one")["already_migrated"]
    assert run_selection(selection_app, preview, ["one"])["ok"]
    bridge._runner.join(3)
    report = bridge._runner.current()
    assert (report.succeeded, report.skipped) == (0, 1)
    assert target.calls == ["probe"]
    assert any(issue.code == "source_warning" for issue in report.issues)


def test_attachment_tampering_after_preview_is_rejected_by_actual_preflight(selection_app, tmp_path):
    bridge, source, _, direction = selection_app
    asset_path = tmp_path / "picture.png"
    Image.new("RGB", (10, 10), "red").save(asset_path)
    data = asset_path.read_bytes()
    note = source.notes[0]
    note.attachments = [Attachment(id="img", name="picture.png", kind="image", local_path="picture.png",
                                   size=len(data), sha256=hashlib.sha256(data).hexdigest())]
    note.blocks.append(Block(kind="attachment", attachment_id="img"))
    bridge._store.replace_snapshot(source.spec.id, source.account_id, source.notes, True)
    target = create_provider(PlatformId.MEIZU, [], tmp_path)
    target.account_id = "synthetic-target-account"
    target.write_supported = target.images_supported = True
    target.probe = lambda: target.account_id
    target.create = lambda *args: pytest.fail("Changed images must be rejected before create")
    bridge._providers[PlatformId.MEIZU] = target
    preview = bridge.preview_migration(direction)["data"]
    assert item(preview, "one")["compatible"]
    asset_path.write_bytes(b"changed")
    assert run_selection(selection_app, preview, ["one"])["ok"]
    bridge._runner.join(3)
    report = bridge._runner.current()
    assert report.succeeded == 0
    assert any(issue.code == "attachment_changed" for issue in report.issues)


@pytest.mark.parametrize("platform", list(PlatformId))
def test_all_real_provider_content_checks_are_local_and_do_not_open_gates(platform, tmp_path):
    provider = create_provider(platform, [], tmp_path)
    capabilities = (provider.write_supported, provider.images_supported)
    note = NoteDocument(platform=PlatformId.VIVO, account_id="synthetic", source_id="note",
                        title="音频", attachments=[Attachment(id="a", name="a.wav", kind="audio")])
    try:
        with pytest.raises(BridgeError) as error:
            provider.validate_notes([note])
        assert error.value.code == "unsupported_attachment"
        assert (provider.write_supported, provider.images_supported) == capabilities
        with pytest.raises(BridgeError) as error:
            provider.preflight([note])
        assert error.value.code == ("unsupported_attachment" if capabilities[0] else "protocol_pending")
    finally:
        provider.close()
