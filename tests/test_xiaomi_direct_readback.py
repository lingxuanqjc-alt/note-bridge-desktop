"""An old failure cannot be erased or bypassed by claiming AC6 was repaired."""
import hashlib
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError
from note_bridge.operations import receipt_key
from note_bridge.providers.xiaomi import encode_content, parse_entry
from note_bridge.providers.xiaomi_groups import folder_key
from note_bridge.storage import Store

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from cloud_fixture_source import select_source  # noqa: E402
from direct_fixture_readback import (  # noqa: E402
    XIAOMI_FIXTURE_ID as FIXTURE_ID,
)
from direct_fixture_readback import (  # noqa: E402
    XIAOMI_MANIFEST as MANIFEST,
)
from direct_fixture_readback import (  # noqa: E402
    recompute_xiaomi_readback,
)
from huawei_scoped_source import fixture  # noqa: E402


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf8")


def setup(tmp_path, monkeypatch):
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(SCRIPTS / "live-fixture-job.py", tmp_path / "scripts/live-fixture-job.py")
    private = tmp_path / ".private"
    base, evidence, resources = private / "checkpoints", private / "evidence", private / "session-lab/resources"
    for i, ext in enumerate(("png", "jpg")):
        path = resources / f"fixtures/vivo-upload-synthetic-{i}.{ext}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"synthetic original {i}".encode())
    job = dict(kind="independent-cloud-write-smoke", id=FIXTURE_ID, target="xiaomi",
               title="笔记互迁验收 · 小米双图片 AC6", expected_account="a" * 64,
               with_images=True, with_group=True, armed=False)
    write(base / "xiaomi-image-AC6-prepared.json", job)
    job = {**job, "consumed_at": "2026-09-07T00:00:00+00:00"}
    write(base / MANIFEST, job)
    builder = lambda value: fixture(tmp_path, value)  # noqa: E731
    seed = builder(job)
    identity = "10003"
    mapping = {asset.id: f"remote-image-{i}" for i, asset in enumerate(seed.attachments)}
    content, warnings = encode_content(seed, {a: {"fileId": b} for a, b in mapping.items()})
    assert not warnings
    data = {"id": identity, "content": content, "folderId": "9000", "createDate": 1000,
            "modifyDate": 2000, "setting": {"data": [
                {"fileId": mapping[a.id], "mimeType": a.mime} for a in seed.attachments]}}
    actual = parse_entry(data, job["expected_account"], {"9000": seed.source_folder_name})
    for asset, original in zip(actual.attachments, seed.attachments, strict=True):
        asset.local_path = "downloaded/" + original.name
        asset.size, asset.sha256 = original.size, original.sha256
        destination = resources / asset.local_path
        destination.parent.mkdir(exist_ok=True)
        destination.write_bytes((resources / original.local_path).read_bytes())
    store = Store(private / "session-lab/notes.sqlite")
    store.replace_snapshot("xiaomi", job["expected_account"], [actual], True)
    target = SimpleNamespace(spec=SimpleNamespace(id="xiaomi"), account_id=job["expected_account"])
    key = receipt_key(seed, target)
    store.save_receipt(key, "sending")
    for i in range(5):
        store.save_resource_receipt(key, f"resource-{i}", "uploaded")
    store.save_receipt(key, "confirmed", [identity])
    group_key = folder_key(seed, job["expected_account"])
    store.save_receipt(group_key, "confirmed", [actual.source_folder_id])
    write(evidence / (FIXTURE_ID + ".json"), dict(kind=job["kind"], fixture_id=FIXTURE_ID,
        platform="xiaomi", status="needs_review", formal_acceptance=False, acknowledged_writes=1,
        issue_codes=["format_downgrade"], cloud_resource_receipt_states=["linked"] * 5,
        unencrypted_account_scope_verified=True, receipt_confirmed=True, group_mapping_verified=True,
        image_bytes_verified=True, original_notes_unchanged=True, expected_images=2, restored_images=2,
        before_count=4, after_count=5, content_and_style_verified=False))
    write(evidence / "xiaomi-AC6-reconciliation.json", dict(kind="xiaomi-AC6-readonly-reconciliation",
        status="verified_with_degradation", formal_acceptance=False, cloud_writes=0, images_verified=2,
        original_notes_unchanged=4, target_notes=5, content_and_style_verified=True,
        group_verified=True, only_confirmed_additions=True, original_evidence_preserved=True))
    plan = dict(kind="xiaomi-AF-scoped-readonly-inspection", armed=False, account=job["expected_account"],
        target_raw_hashes={str(10000 + i): "b" * 64 for i in range(8)}, items=[dict(index=3,
        target_id=identity, raw_sha256="b" * 64, source_fingerprint=seed.fingerprint(), images=mapping)])
    write(base / "xiaomi-AF-readonly-plan.json", plan)
    repair = dict(formal_acceptance=False, creates=0, uploads=0, update_requests=1, target_id=identity,
        before_sha256="b" * 64, expected_content_sha256=hashlib.sha256(
            json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest(), warnings=[])
    write(evidence / "xiaomi-AF-AC6-content-repair.json", dict(repair,
        kind="xiaomi-AF-AC6-content-repair", status="write_uncertain"))
    write(evidence / "xiaomi-AF-AC6-readonly-reconciliation.json", dict(
        kind="xiaomi-AF-AC6-readonly-reconciliation", status="old_entry_unchanged", formal_acceptance=False,
        cloud_writes=0, new_content_present=False, other_notes_unchanged=True, old_entry_unchanged=True))
    write(evidence / "xiaomi-AG-AC6-content-repair.json", dict(repair,
        kind="xiaomi-AG-AC6-content-repair", status="content_repaired_readback_verified",
        response_code=0, response_has_data_object=True, other_notes_unchanged=7,
        image_references_unchanged=True, source_time_and_group_unchanged=True, after_sha256="c" * 64))
    write(evidence / "xiaomi-AG-native-verification.json", dict(kind="xiaomi-AG-AC6-native-verification",
        status="verified", formal_acceptance=False, cloud_writes=0, images_decoded_visible=2,
        bold=True, italic=True, underline=True, checked_and_unchecked_tasks=True, ordered_list_structure=True))
    write(evidence / "xiaomi-image-browser-AC6.json", dict(kind="xiaomi-official-image-rendering",
        fixture="AC6", status="verified", formal_acceptance=False, cloud_writes=0, fixture_title_in_editor=True,
        decoded_images=[dict(width=640, height=240, complete=True, visible=True, insideEditor=True)] * 2))
    proof = evidence / (FIXTURE_ID + "-readback.json")
    result = recompute_xiaomi_readback(tmp_path, store, builder)
    write(proof, result)
    entry = dict(target="oppo", title=seed.title, with_images=True, with_group=True,
        from_cloud_fixture=dict(manifest=MANIFEST, platform="xiaomi", account=job["expected_account"],
                                source_id=identity, fingerprint=actual.fingerprint()))
    return SimpleNamespace(root=tmp_path, store=store, builder=builder, key=key, group_key=group_key,
        seed=seed, actual=actual, entry=entry, proof=proof, evidence=evidence, base=base, resources=resources)


def test_original_repaired_ac6_is_reusable_without_erasing_failures_or_receipts(tmp_path, monkeypatch):
    case = setup(tmp_path, monkeypatch)
    originals = {p: p.read_bytes() for p in (*case.evidence.glob("*.json"), *case.base.glob("*.json"))}
    receipts = case.store.receipt(case.key), case.store.resource_receipts(case.key)
    # Recompute itself must never enumerate other notes; legacy select_source only
    # visits the Xiaomi account, which is separate from Vivo's prohibited cache.
    with monkeypatch.context() as m:
        m.setattr(case.store, "notes", lambda *args: pytest.fail("Do not read unrelated cached bodies"))
        proof = recompute_xiaomi_readback(case.root, case.store, case.builder)
    assert proof["cloud_writes"] == 0 and proof["raw_response_replayed"] is False
    assert proof["target"]["fingerprint"] == case.actual.fingerprint()
    assert select_source(case.entry, case.store, case.root, case.builder) == case.actual
    assert originals == {p: p.read_bytes() for p in originals}
    assert receipts == (case.store.receipt(case.key), case.store.resource_receipts(case.key))


@pytest.mark.parametrize("fault", ["no_proof", "file_changed", "prepared", "uncertain", "missing_resource",
    "unlinked_resource", "wrong_id", "cache_fingerprint", "body", "downloaded_bytes", "original_bytes",
    "native_pending", "native_image_hidden", "AG_content_hash", "AF_not_reconciled", "plan_images", "group"])
def test_independent_proof_never_promotes_an_unbound_or_changed_seed(tmp_path, monkeypatch, fault):
    case = setup(tmp_path, monkeypatch)

    def edit(path, change):
        value = json.loads(path.read_text("utf8"))
        change(value)
        write(path, value)

    if fault == "no_proof":
        case.proof.unlink()
    elif fault == "file_changed":
        edit(case.evidence / "xiaomi-AG-native-verification.json", lambda v: v.update(extra="changed"))
    elif fault == "prepared":
        edit(case.base / "xiaomi-image-AC6-prepared.json", lambda v: v.update(title="Different seed"))
    elif fault in ("uncertain", "wrong_id"):
        case.store.save_receipt(case.key, "uncertain" if fault == "uncertain" else "confirmed", ["other-id"])
    elif fault in ("missing_resource", "unlinked_resource"):
        with case.store.connection() as db:
            db.execute("DELETE FROM resource_receipts WHERE receipt_key=? AND resource_id='resource-0'", (case.key,))
            if fault == "unlinked_resource":
                db.execute("INSERT INTO resource_receipts VALUES (?, 'resource-0', 'uploaded')", (case.key,))
    elif fault in ("cache_fingerprint", "body"):
        if fault == "cache_fingerprint":
            case.actual.created_at = None
        else:
            case.actual.blocks.pop()
        case.store.replace_snapshot("xiaomi", case.actual.account_id, [case.actual], True)
        case.entry["from_cloud_fixture"]["fingerprint"] = case.actual.fingerprint()
    elif fault in ("downloaded_bytes", "original_bytes"):
        asset = (case.actual if fault == "downloaded_bytes" else case.seed).attachments[0]
        (case.resources / asset.local_path).write_bytes(b"changed original image")
    elif fault == "native_pending":
        edit(case.evidence / "xiaomi-AG-native-verification.json", lambda v: v.update(status="pending"))
    elif fault == "native_image_hidden":
        edit(case.evidence / "xiaomi-image-browser-AC6.json", lambda v: v["decoded_images"][0].update(visible=False))
    elif fault == "AG_content_hash":
        edit(case.evidence / "xiaomi-AG-AC6-content-repair.json", lambda v: v.update(expected_content_sha256="d" * 64))
    elif fault == "AF_not_reconciled":
        edit(case.evidence / "xiaomi-AF-AC6-readonly-reconciliation.json", lambda v: v.update(new_content_present=True))
    elif fault == "plan_images":
        edit(case.base / "xiaomi-AF-readonly-plan.json", lambda v: v["items"][0]["images"].update({case.seed.attachments[0].id: "another-image"}))
    else:
        case.store.save_receipt(case.group_key, "confirmed", ["other-group"])
    with pytest.raises(BridgeError):
        select_source(case.entry, case.store, case.root, case.builder)
