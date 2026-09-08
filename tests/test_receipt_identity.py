"""Unknown writes belong to a stable source, even after its content is edited."""
import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError, WriteUncertain
from note_bridge.migration_selection import MigrationSelection
from note_bridge.models import Attachment, Block, NoteDocument, Span, TaskReport
from note_bridge.operations import migrate, receipt_key
from note_bridge.providers.base import CreatedNote, Provider, Snapshot
from note_bridge.receipt_identity import identity_key, migration_identity
from note_bridge.storage import SCHEMA_VERSION, Store
from note_bridge.tasks import TaskContext


class LocalTarget(Provider):
    write_supported = images_supported = True

    def __init__(self, platform="wps", account="target-account"):
        self.spec = SimpleNamespace(id=platform)
        self.account_id = account
        self.created = []

    def probe(self):
        return self.account_id

    def fetch(self, context):
        return Snapshot([], True)

    def create(self, note, context):
        self.preflight([note])
        self.created.append(note.source_id)
        return CreatedNote(["new-" + str(len(self.created))])


def note():
    return NoteDocument(platform="vivo", account_id="source-account", source_id="stable-id", title="Synthetic",
                        blocks=[Block(spans=[Span(text="original version")])])


def context(store):
    return TaskContext(TaskReport(id="local-test", operation="migrate", started_at="2026-09-07T00:00:00+00:00"),
                       store, lambda _: None)


def changed(original):
    result = original.model_copy(deep=True)
    result.blocks[0].spans[0].text = "edited source body"
    return result


def test_original_key_serialization_is_preserved_for_existing_archived_receipts():
    original, target = note(), LocalTarget()
    old_key = hashlib.sha256(json.dumps([original.platform, original.account_id, original.source_id,
        original.fingerprint(), target.spec.id, target.account_id], ensure_ascii=False).encode()).hexdigest()
    assert receipt_key(original, target) == identity_key(migration_identity(original, target)) == old_key


@pytest.mark.parametrize("status", ["sending", "uncertain"])
def test_source_edit_cannot_bypass_unknown_version_before_validation_or_writes(tmp_path, status):
    store, target, original = Store(tmp_path / "notes.sqlite"), LocalTarget(), note()
    key = store.begin_migration_write(original, target)
    store.save_receipt(key, status, ["possibly-existing-target"])
    current = changed(original)
    target.preflight = lambda _: pytest.fail("Unknown versions must block before content validation")
    with pytest.raises(WriteUncertain):
        migrate([current], target, store, context(store))
    with pytest.raises(WriteUncertain):
        store.begin_migration_write(current, target)
    assert target.created == [] and store.receipt(receipt_key(current, target)) is None
    assert store.receipt(key) == {"status": status, "remote_ids": ["possibly-existing-target"]}


def test_already_confirmed_same_version_skips_but_an_intentional_new_version_can_create(tmp_path):
    store, target, original = Store(tmp_path / "notes.sqlite"), LocalTarget(), note()
    key = store.begin_migration_write(original, target)
    store.save_receipt(key, "confirmed", ["first-target"])
    skip = context(store)
    migrate([original], target, store, skip)
    assert skip.report.skipped == 1 and target.created == []
    migrate([changed(original)], target, store, context(store))
    assert target.created == [original.source_id]
    assert store.receipt(key) == {"status": "confirmed", "remote_ids": ["first-target"]}


@pytest.mark.parametrize("different", ["source-platform", "source-account", "source-id", "target-platform", "target-account"])
def test_bound_unknown_does_not_lock_an_unrelated_source_or_account(tmp_path, different):
    store, target, original = Store(tmp_path / "notes.sqlite"), LocalTarget(), note()
    key = store.begin_migration_write(original, target)
    store.save_receipt(key, "uncertain", ["old-target"])
    current, other = changed(original), LocalTarget()
    if different == "source-platform":
        current.platform = "meizu"
    elif different == "source-account":
        current.account_id = "other-source-account"
    elif different == "source-id":
        current.source_id = "different-note"
    elif different == "target-platform":
        other.spec.id = "honor"
    else:
        other.account_id = "other-target-account"
    migrate([current], other, store, context(store))
    assert other.created == [current.source_id] and store.receipt(key)["status"] == "uncertain"


def test_old_unbound_unknown_is_closed_until_exact_archived_identity_is_backfilled(tmp_path):
    store, target, original = Store(tmp_path / "notes.sqlite"), LocalTarget(), note()
    key, archived = receipt_key(original, target), migration_identity(original, target)
    store.save_receipt(key, "sending", ["old-target"])
    store.save_resource_receipt(key, "old-object", "uploaded")
    store.save_receipt(key, "uncertain")
    unrelated = original.model_copy(update={"source_id": "unrelated-source"})
    with pytest.raises(WriteUncertain, match="缺少来源关联"):
        store.begin_migration_write(unrelated, target)
    assert store.bind_receipt_context(key, archived) is True
    assert store.bind_receipt_context(key, archived) is False
    assert store.begin_migration_write(unrelated, target)
    with pytest.raises(WriteUncertain):
        store.begin_migration_write(changed(original), target)
    assert store.receipt(key) == {"status": "uncertain", "remote_ids": ["old-target"]}
    assert store.resource_receipts(key) == [{"resource_id": "old-object", "status": "uploaded"}]


@pytest.mark.parametrize("fault", ["wrong-account", "wrong-version", "missing-field", "extra-field", "missing-receipt", "bound-conflict"])
def test_backfill_cannot_guess_an_identity_or_rebind_a_receipt(tmp_path, fault):
    store, target, original = Store(tmp_path / "notes.sqlite"), LocalTarget(), note()
    key, identity = receipt_key(original, target), migration_identity(original, target)
    if fault != "missing-receipt":
        store.save_receipt(key, "uncertain", ["preserve"])
    if fault == "wrong-account":
        identity["target_account"] = "different"
    elif fault == "wrong-version":
        identity["source_fingerprint"] = "0" * 64
    elif fault == "missing-field":
        identity.pop("source_id")
    elif fault == "extra-field":
        identity["credentials"] = "must-never-be-accepted"
    elif fault == "bound-conflict":
        assert store.bind_receipt_context(key, identity)
        with store.connection() as db:
            db.execute("UPDATE receipt_contexts SET source_id='inconsistent-binding' WHERE key=?", (key,))
    with store.connection() as db:
        before = list(db.iterdump())
    with pytest.raises((BridgeError, ValueError)):
        store.bind_receipt_context(key, identity)
    with store.connection() as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("different_versions", [False, True])
def test_two_store_instances_can_reserve_only_one_concurrent_version(tmp_path, different_versions):
    first, second = Store(tmp_path / "notes.sqlite"), Store(tmp_path / "notes.sqlite")
    original, target = note(), LocalTarget()
    barrier = threading.Barrier(2)

    def begin(store, document):
        barrier.wait(timeout=3)
        try:
            return bool(store.begin_migration_write(document, target))
        except WriteUncertain:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = [executor.submit(begin, first, original),
                    executor.submit(begin, second, changed(original) if different_versions else original)]
        assert sorted(result.result(timeout=5) for result in attempts) == [False, True]
    with first.connection() as db:
        assert db.execute("SELECT count(*) FROM receipts WHERE status='sending'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM receipt_contexts").fetchone()[0] == 1


def test_binding_and_sending_roll_back_together_when_reservation_fails(tmp_path):
    store, original, target = Store(tmp_path / "notes.sqlite"), note(), LocalTarget()
    with store.connection() as db:
        db.executescript("CREATE TRIGGER fail_insert BEFORE INSERT ON receipts BEGIN SELECT RAISE(ABORT,'stop'); END;")
    with pytest.raises(sqlite3.IntegrityError):
        store.begin_migration_write(original, target)
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM receipt_contexts").fetchone()[0] == 0


def test_known_rejection_restarts_without_erasing_persisted_ids_or_resources(tmp_path):
    store, original, target = Store(tmp_path / "notes.sqlite"), note(), LocalTarget()
    key = store.begin_migration_write(original, target)
    store.save_receipt(key, "sending", ["known-draft-id"])
    store.save_resource_receipt(key, "known-object", "allocated")
    store.save_receipt(key, "rejected")
    assert store.begin_migration_write(original, target) == key
    assert store.receipt(key) == {"status": "sending", "remote_ids": ["known-draft-id"]}
    assert store.resource_receipts(key) == [{"resource_id": "known-object", "status": "allocated"}]


def test_exact_confirmed_restore_is_not_blocked_by_unrelated_legacy_unknown(tmp_path):
    store, target, original = Store(tmp_path / "notes.sqlite"), LocalTarget(), note()
    original.attachments = [Attachment(id="old", name="missing.png", kind="image", local_path="missing.png")]
    store.save_receipt(receipt_key(original, target), "confirmed", ["already-target"])
    store.save_receipt("f" * 64, "uncertain", ["unbound-target"])
    target.validate_notes = lambda notes: pytest.fail("Confirmed restores need no old files") if notes else None
    ctx = context(store)
    migrate([original], target, store, ctx)
    assert ctx.report.skipped == 1 and target.created == []


def test_private_repair_and_group_keys_are_not_misclassified_as_canonical_migrations(tmp_path):
    store, original, target = Store(tmp_path / "notes.sqlite"), note(), LocalTarget()
    store.save_receipt("folder:" + "a" * 64, "uncertain")
    store.save_receipt("b" * 64 + ":independent-private-repair", "uncertain")
    assert store.begin_migration_write(original, target)


def test_preflight_cannot_offer_changed_unknown_as_compatible_or_mutate_receipts(tmp_path):
    store, original, target = Store(tmp_path / "notes.sqlite"), note(), LocalTarget()
    key = store.begin_migration_write(original, target)
    store.save_receipt(key, "uncertain", ["existing-target"])
    current = changed(original)
    current.attachments = [Attachment(id="missing", name="missing.png", kind="image")]
    store.replace_snapshot(current.platform, current.account_id, [current], True)
    source = SimpleNamespace(spec=SimpleNamespace(id=current.platform), account_id=current.account_id)
    target.validate_notes = lambda _: pytest.fail("Unknown identity must be evaluated first")
    with store.connection() as db:
        before = list(db.iterdump())
    preview = MigrationSelection.prepare(source, target, store).public()
    assert not preview["items"][0]["compatible"] and preview["items"][0]["code"] == "write_uncertain"
    with store.connection() as db:
        assert list(db.iterdump()) == before


def test_schema_upgrade_preserves_legacy_unknown_without_inventing_context(tmp_path):
    path = tmp_path / "notes.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE receipts(key TEXT PRIMARY KEY,status TEXT,remote_ids TEXT); PRAGMA user_version=1;")
        db.execute("INSERT INTO receipts VALUES (?,'uncertain','[\"legacy-target\"]')", ("e" * 64,))
    store = Store(path)
    assert store.receipt("e" * 64) == {"status": "uncertain", "remote_ids": ["legacy-target"]}
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.execute("SELECT count(*) FROM receipt_contexts").fetchone()[0] == 0
    with pytest.raises(WriteUncertain):
        store.begin_migration_write(note(), LocalTarget())
