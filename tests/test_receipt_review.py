"""Local review must neither disclose another account nor resolve an unknown write."""

import json
import sqlite3
from contextlib import contextmanager

import pytest

from note_bridge.bridge import Bridge
from note_bridge.models import Block, NoteDocument, PlatformId, Span
from note_bridge.operations import receipt_key
from note_bridge.paths import AppPaths
from note_bridge.providers.base import SPECS, Provider
from note_bridge.receipt_identity import migration_identity


class OfflineProvider(Provider):
    def __init__(self, platform):
        self.spec = SPECS[platform]
        self.account_id = platform + "-private-account"

    def probe(self):
        raise AssertionError("Receipt review must not check a cloud session")

    def fetch(self, context):
        raise AssertionError("Receipt review must not fetch notes")

    def prepare_migration(self):
        raise AssertionError("Receipt review must not prepare a write")

    def create(self, note, context):
        raise AssertionError("Receipt review must not write notes")


@pytest.fixture
def review_app(tmp_path):
    paths = AppPaths(tmp_path)
    paths.prepare()
    bridge = Bridge(paths)
    source, target = OfflineProvider(PlatformId.VIVO), OfflineProvider(PlatformId.HUAWEI)
    bridge._providers.update({source.spec.id: source, target.spec.id: target})
    note = NoteDocument(platform=source.spec.id, account_id=source.account_id, source_id="source-note",
                        title="Synthetic review note", blocks=[Block(spans=[Span(text="body-must-stay-private")])])
    bridge._store.replace_snapshot(source.spec.id, source.account_id, [note], True)
    key = receipt_key(note, target)
    bridge._store.save_receipt(key, "sending", ["target-note"])
    bridge._store.save_resource_receipt(key, "upload-session-must-stay-private", "allocated")
    bridge._store.save_resource_receipt(key, "asset-must-stay-private", "uploaded")
    bridge._store.save_receipt(key, "uncertain")
    cached = note.model_copy(update={"platform": target.spec.id, "account_id": target.account_id, "source_id": "target-note"})
    bridge._store.replace_snapshot(target.spec.id, target.account_id, [cached], True)
    return bridge, source, target, note, key


def review(bridge):
    return bridge.review_migration({"source": "vivo", "target": "huawei"})


@pytest.mark.parametrize("version", ["legacy", "current", "previous", "source_missing"])
def test_review_is_sql_read_only_and_cache_presence_cannot_confirm_receipt(review_app, monkeypatch, version):
    bridge, source, target, note, key = review_app
    if version != "legacy":
        bridge._store.bind_receipt_context(key, migration_identity(note, target))
    if version == "previous":
        changed = note.model_copy(update={"title": "Current source title", "blocks": [Block(spans=[Span(text="edited")])]})
        bridge._store.replace_snapshot(source.spec.id, source.account_id, [changed], True)
    elif version == "source_missing":
        bridge._store.replace_snapshot(source.spec.id, source.account_id, [], True)
    original = bridge._store.connection
    with original() as db:
        before = list(db.iterdump())

    @contextmanager
    def read_only():
        with original() as db:
            db.set_authorizer(lambda action, *args: sqlite3.SQLITE_OK if action in (
                sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION) else sqlite3.SQLITE_DENY)
            yield db

    monkeypatch.setattr(bridge._store, "connection", read_only)
    result = review(bridge)
    assert result["ok"]
    data, item = result["data"], result["data"]["items"][0]
    assert data["cloud_checked"] is False and item["status"] == "uncertain"
    assert item["version"] == ("current" if version == "legacy" else version)
    assert len(data["items"]) == 1 and data["unmatched_cached_notes"] == 0
    if version == "source_missing":
        assert item["title"] == "" and item["id"] == note.source_id and data["cached_notes"] == 0
    elif version == "previous":
        assert item["title"] == "Current source title", "A saved fingerprint is not an archived title."
    assert item["remote_ids"] == item["cached_remote_ids"] == ["target-note"]
    assert sorted(item["resource_states"]) == ["allocated", "uploaded"]
    serialized = json.dumps(data)
    assert all(value not in serialized for value in [source.account_id, target.account_id,
        "body-must-stay-private", "upload-session-must-stay-private", "asset-must-stay-private", key])
    with original() as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("side", ["source", "target"])
@pytest.mark.parametrize("bound", [False, True])
def test_switching_either_account_cannot_reveal_old_receipts(review_app, side, bound):
    bridge, source, target, note, key = review_app
    if bound:
        bridge._store.bind_receipt_context(key, migration_identity(note, target))
    (source if side == "source" else target).account_id = "different-private-account"
    result = review(bridge)
    assert result["ok"] and result["data"]["items"] == []
    assert "target-note" not in json.dumps(result)
    assert bridge._store.receipt(key)["status"] == "uncertain"


def test_changed_source_without_saved_identity_cannot_guess_its_old_receipt(review_app):
    bridge, source, _, note, key = review_app
    changed = note.model_copy(update={"title": "Changed version"})
    bridge._store.replace_snapshot(source.spec.id, source.account_id, [changed], False)
    data = review(bridge)["data"]
    assert data["cached_notes"] == data["unmatched_cached_notes"] == 1 and data["items"] == []
    assert data["source_cache_complete"] is False
    assert bridge._store.receipt(key) == {"status": "uncertain", "remote_ids": ["target-note"]}


def test_multiple_versions_and_uncached_sources_count_distinct_cached_notes(review_app):
    bridge, source, target, note, key = review_app
    store = bridge._store
    store.bind_receipt_context(key, migration_identity(note, target))
    current = note.model_copy(update={"title": "Current title"})
    current_key = receipt_key(current, target)
    store.save_receipt(current_key, "confirmed", ["confirmed-current-target"])
    store.bind_receipt_context(current_key, migration_identity(current, target))
    missing = note.model_copy(update={"source_id": "no-current-cache", "title": "Unstored historical title"})
    missing_key = receipt_key(missing, target)
    store.save_receipt(missing_key, "uncertain", ["unknown-missing-source-target"])
    store.bind_receipt_context(missing_key, migration_identity(missing, target))
    unmatched = note.model_copy(update={"source_id": "never-migrated"})
    store.replace_snapshot(source.spec.id, source.account_id, [current, unmatched], True)
    data = review(bridge)["data"]
    assert data["cached_notes"] == 2 and data["unmatched_cached_notes"] == 1
    assert len(data["items"]) == 3
    assert sorted(item["version"] for item in data["items"]) == ["current", "previous", "source_missing"]
    assert "Unstored historical title" not in json.dumps(data)
    assert store.receipt(key)["status"] == store.receipt(missing_key)["status"] == "uncertain"


@pytest.mark.parametrize("status", ["confirmed", "rejected"])
def test_extension_only_adds_old_versions_that_still_need_review(review_app, status):
    bridge, source, target, note, key = review_app
    bridge._store.save_receipt(key, status)
    bridge._store.bind_receipt_context(key, migration_identity(note, target))
    bridge._store.replace_snapshot(source.spec.id, source.account_id, [note.model_copy(update={"title": "Changed"})], True)
    data = review(bridge)["data"]
    assert data["items"] == [] and data["unmatched_cached_notes"] == 1
    assert bridge._store.receipt(key)["status"] == status


def test_inconsistent_stored_identity_cannot_reassign_a_receipt_to_another_source(review_app):
    bridge, _, target, note, key = review_app
    bridge._store.bind_receipt_context(key, migration_identity(note, target))
    with bridge._store.connection() as db:
        db.execute("UPDATE receipt_contexts SET source_id='other-source-id' WHERE key=?", (key,))
    result = review(bridge)
    assert result["error"]["code"] == "receipt_context_mismatch"
    assert "target-note" not in json.dumps(result)
    assert bridge._store.receipt(key)["status"] == "uncertain"


@pytest.mark.parametrize("status", ["sending", "confirmed", "rejected"])
def test_statuses_are_presented_without_mutation_even_when_target_cache_is_empty(review_app, status):
    bridge, _, target, _, key = review_app
    bridge._store.save_receipt(key, status)
    bridge._store.replace_snapshot(target.spec.id, target.account_id, [], False)
    data = review(bridge)["data"]
    assert data["items"][0]["status"] == status and data["items"][0]["cached_remote_ids"] == []
    assert data["target_cache_complete"] is False and data["cloud_checked"] is False
    assert bridge._store.receipt(key)["status"] == status


def test_native_entry_requires_current_accounts_and_rejects_caller_account_injection(review_app):
    bridge, source, _, _, _ = review_app
    result = bridge.review_migration({"source": "vivo", "target": "huawei", "source_account": "other"})
    assert result["error"]["code"] == "invalid_request"
    source.account_id = ""
    assert review(bridge)["error"]["code"] == "login_required"
