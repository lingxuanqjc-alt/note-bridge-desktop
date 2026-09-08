"""Reopening OPPO must only admit the twelve fixed direct-seed directions."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError
from note_bridge.models import Attachment, Block, NoteDocument, PlatformId, Span
from note_bridge.operations import receipt_key
from note_bridge.providers.base import CreatedNote, Snapshot
from note_bridge.providers.oppo import OppoProvider
from note_bridge.storage import Store

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("oppo_matrix_scope_test", SCRIPTS / "live-matrix-job.py")
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)
PEERS = ("wps", "huawei", "xiaomi", "honor", "meizu", "vivo")


def pair(tmp_path, monkeypatch, source="wps", target="oppo"):
    (tmp_path / ".private/checkpoints").mkdir(parents=True)
    (tmp_path / ".private/evidence").mkdir()
    store = Store(tmp_path / ".private/session-lab/notes.sqlite")
    notes, entries = [], []
    for index, name in enumerate(matrix.OPPO_DIRECT_SEEDS[source]):
        image_count = 0 if source == "xiaomi" and index == 0 else 2
        note = NoteDocument(platform=source, account_id="a" * 64, source_id=f"source-{index}",
            title=f"笔记互迁验收 · 原始{index}", source_folder_id=f"source-group-{index}",
            source_folder_name="笔记互迁分组验收 T" if index else None,
            blocks=[Block(spans=[Span(text=f"原文{index}")])], attachments=[
                Attachment(id=f"image-{asset}", name=f"image-{asset}.png", kind="image", size=1,
                           sha256=str(asset) * 64) for asset in range(image_count)])
        notes.append(note)
        original = {"id": f"fixture-20260907-{source}-seed-{index}", "kind": "independent-cloud-write-smoke",
                    "target": source, "armed": False, "expected_account": note.account_id}
        (tmp_path / ".private/checkpoints" / name).write_text(json.dumps(original), "utf8")
        origin = {"platform": source, "account": note.account_id, "source_id": note.source_id,
                  "fingerprint": note.fingerprint(), "manifest": name}
        if source in ("oppo", "vivo", "huawei"):
            origin["scoped_proof"] = {"path": ".private/evidence/exact-pair/proof.json", "sha256": "c" * 64}
        entries.append({"title": note.title, "with_images": bool(note.attachments), "with_group": bool(note.source_folder_name),
                        "from_cloud_fixture": origin})
    job = {"id": f"matrix-20260907-{source}-{target}-BI1", "kind": "cloud-matrix-batch", "target": target,
           "expected_account": "b" * 64, "source_policy": "direct_seed_only", "oppo_scope": matrix.OPPO_SCOPE,
           "armed": False, "sources": entries}
    monkeypatch.setattr(matrix, "select_source", lambda entry, *_: next(
        note for note in notes if note.source_id == entry["from_cloud_fixture"]["source_id"]))
    return job, notes, store


@pytest.mark.parametrize(("source", "target"), [(source, target) for peer in PEERS
                         for source, target in ((peer, "oppo"), ("oppo", peer))])
def test_exact_twelve_routes_keep_original_image_counts_and_individual_group_semantics(tmp_path, monkeypatch, source, target):
    job, notes, store = pair(tmp_path, monkeypatch, source, target)
    assert matrix.select_batch(job, store, tmp_path, None) == notes
    assert OppoProvider.write_supported and OppoProvider.images_supported


@pytest.mark.parametrize(("source", "index", "count"), [("xiaomi", 0, 2), ("xiaomi", 1, 0), ("wps", 0, 0)])
def test_only_the_original_xiaomi_p2_may_have_zero_images(tmp_path, monkeypatch, source, index, count):
    job, notes, store = pair(tmp_path, monkeypatch, source, "oppo")
    notes[index].attachments = [Attachment(id=f"replacement-{i}", name=f"image-{i}.png", kind="image",
        size=1, sha256=str(i) * 64) for i in range(count)]
    job["sources"][index]["with_images"] = bool(count)
    job["sources"][index]["from_cloud_fixture"]["fingerprint"] = notes[index].fingerprint()
    with pytest.raises(BridgeError):
        matrix.select_batch(job, store, tmp_path, None)


@pytest.mark.parametrize("damage", ["marker", "other_route", "reversed", "derived", "wrong_seed", "only_one",
                                   "missing_capture", "mixed_capture", "group_flag", "source_identity", "missing_image"])
def test_scope_tampering_never_reaches_a_provider(tmp_path, monkeypatch, damage):
    job, notes, store = pair(tmp_path, monkeypatch, "oppo", "wps")
    if damage == "marker":
        job.pop("oppo_scope")
    elif damage == "other_route":
        for entry in job["sources"]:
            entry["from_cloud_fixture"]["platform"] = "honor"
    elif damage == "reversed":
        job["sources"].reverse()
    elif damage == "derived":
        job["sources"][0]["from_cloud_fixture"]["batch_manifest"] = "forwarded-job.json"
    elif damage == "wrong_seed":
        job["sources"][0]["from_cloud_fixture"]["manifest"] = "other-direct-job.json"
    elif damage == "only_one":
        job["sources"].pop()
    elif damage == "missing_capture":
        job["sources"][0]["from_cloud_fixture"].pop("scoped_proof")
    elif damage == "mixed_capture":
        job["sources"][0]["from_cloud_fixture"]["scoped_proof"] = {"path": "different"}
    elif damage == "group_flag":
        job["sources"][0]["with_group"] = True
    elif damage == "source_identity":
        notes[0].account_id = "other"
    else:
        notes[0].attachments.pop()
        job["sources"][0]["from_cloud_fixture"]["fingerprint"] = notes[0].fingerprint()
    with pytest.raises(BridgeError):
        matrix.select_batch(job, store, tmp_path, None)


@pytest.mark.parametrize("state", ["confirmed", "rejected", "sending", "uncertain", "old_version", "group_unknown"])
def test_existing_or_unknown_target_receipts_cannot_be_retried_by_renaming_the_batch(tmp_path, monkeypatch, state):
    from note_bridge.providers.oppo_groups import folder_key

    job, notes, store = pair(tmp_path, monkeypatch)
    target = SimpleNamespace(spec=SimpleNamespace(id="oppo"), account_id=job["expected_account"])
    if state == "group_unknown":
        store.save_receipt(folder_key(notes[1], target.account_id), "uncertain")
    elif state == "old_version":
        store.begin_migration_write(notes[0], target)
        notes[0].blocks[0].spans[0].text = "changed"
        job["sources"][0]["from_cloud_fixture"]["fingerprint"] = notes[0].fingerprint()
    else:
        store.save_receipt(receipt_key(notes[0], target), state, ["old-target"])
    job["id"] = "matrix-20260907-wps-oppo-BI999"
    with pytest.raises(BridgeError):
        matrix.select_batch(job, store, tmp_path, None)


@pytest.mark.parametrize(("source", "target"), [("wps", "oppo"), ("oppo", "wps")])
def test_wps_oppo_use_existing_one_shot_migration_and_two_target_reads(tmp_path, monkeypatch, source, target):
    job, notes, store = pair(tmp_path, monkeypatch, source, target)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/live-fixture-job.py").write_bytes((SCRIPTS / "live-fixture-job.py").read_bytes())
    if source == "wps":
        store.replace_snapshot(source, notes[0].account_id, notes, True)
    old_notes = store.notes

    def guarded_notes(platform, account):
        assert platform != "oppo" or platform == target, "OPPO capture must not be replaced by whole-account cache."
        return old_notes(platform, account)

    monkeypatch.setattr(store, "notes", guarded_notes)
    monkeypatch.setattr(matrix, "Store", lambda _: store)
    job["armed"] = True
    control = tmp_path / ".private/lab-matrix-job.json"
    control.write_text(json.dumps(job), "utf8")
    rows, calls = [], []

    def fetch(context):
        calls.append("read")
        return Snapshot(list(rows), True)

    def create(note, context):
        assert json.loads(control.read_text("utf8"))["armed"] is False
        calls.append("write")
        identifier = "target-" + note.source_id
        restored = note.model_copy(update={"platform": PlatformId(target), "account_id": job["expected_account"],
                                          "source_id": identifier, "source_folder_id": None})
        if note.source_folder_name:
            groups = __import__("note_bridge.providers." + target + "_groups", fromlist=["folder_key"])
            restored.source_folder_id = "target-group"
            context.store.save_receipt(groups.folder_key(note, job["expected_account"]), "confirmed", ["target-group"])
        rows.append(restored)
        return CreatedNote([identifier])

    provider = SimpleNamespace(spec=SimpleNamespace(id=target), account_id=job["expected_account"],
        probe=lambda: job["expected_account"], preflight=lambda _: None, fetch=fetch, create=create, close=lambda: None)
    monkeypatch.setattr(matrix, "create_provider", lambda *args: provider)
    result = matrix.run_armed_job(target, [], tmp_path)
    assert result["status"] == "api_verified" and result["only_confirmed_additions"]
    assert all(item["content_verified"] and item["group_verified"] for item in result["items"])
    assert result["source_post_readback"] == "pending" and result["formal_acceptance"] is False
    assert calls == ["read", "write", "write", "read"]
    assert matrix.run_armed_job(target, [], tmp_path) is None
    assert calls == ["read", "write", "write", "read"]


def test_oppo_pair_still_acquires_both_platform_locks_and_the_global_writer_guard(tmp_path, monkeypatch):
    from lab_platform_lock import foreground_scope

    job, _, _ = pair(tmp_path, monkeypatch)
    job["armed"] = True
    (tmp_path / ".private/lab-matrix-job.json").write_text(json.dumps(job), "utf8")
    scope, _ = foreground_scope(tmp_path, "oppo", "probe")
    assert scope == {"wps", "oppo"}
    (tmp_path / ".private/lab-write-job.json").write_text(json.dumps({"armed": True, "target": "honor"}), "utf8")
    with pytest.raises(BridgeError) as error:
        matrix.run_armed_job("oppo", [], tmp_path)
    assert error.value.code == "conflicting_write_jobs"


@pytest.mark.parametrize(("platform", "target", "helper"), [
    ("oppo", "wps", "oppo_scoped_source"), ("vivo", "oppo", "vivo_fixed_source"),
    ("huawei", "oppo", "huawei_scoped_source")])
def test_fixed_proof_selection_is_identity_bound_and_never_reads_account_cache(tmp_path, monkeypatch, platform, target, helper):
    from cloud_fixture_source import select_source

    job, notes, _ = pair(tmp_path, monkeypatch, platform, target)
    checked = []

    def require(condition):
        if not condition:
            raise BridgeError("invalid_fixture_source", "scope")

    def verify(reference, store, root):
        checked.append(reference)
        return notes

    monkeypatch.setitem(sys.modules, helper, SimpleNamespace(
        MANIFESTS=matrix.OPPO_DIRECT_SEEDS[platform], require=require, verify_source=verify))
    store = SimpleNamespace(notes=lambda *a: pytest.fail("Never read all private account notes"),
                            snapshot=lambda *a: pytest.fail("Fixed capture is not an account snapshot"))
    entry = {**job["sources"][0], "target": target}
    assert select_source(entry, store, tmp_path, None) == notes[0]
    assert len(checked) == 1
    entry["from_cloud_fixture"]["fingerprint"] = "f" * 64
    with pytest.raises(BridgeError):
        select_source(entry, store, tmp_path, None)
