"""An interrupted seed becomes reusable only after independently bound readback."""
import hashlib
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError
from note_bridge.models import Attachment, Block, ItemIssue, NoteDocument, Span, TaskReport
from note_bridge.operations import receipt_key
from note_bridge.providers.huawei_groups import folder_key
from note_bridge.storage import Store

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from cloud_fixture_source import select_source  # noqa: E402
from direct_fixture_readback import FIXTURE_ID, MANIFEST, recompute_readback, verify_readback  # noqa: E402
from fetch_scope import write_fetch_scope  # noqa: E402


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")


def setup(tmp_path):
    for name in ("live-fixture-job.py", "live-matrix-job.py"):
        (tmp_path / "scripts").mkdir(exist_ok=True)
        shutil.copyfile(SCRIPTS / name, tmp_path / "scripts" / name)
    base = tmp_path / ".private/checkpoints"
    job = {"id": FIXTURE_ID, "kind": "independent-cloud-write-smoke", "target": "huawei",
           "expected_account": "a" * 64, "armed": False, "content_case": "complex",
           "with_images": True, "with_group": True, "title": "笔记互迁验收 · 固定同名种子"}
    write(base / MANIFEST, job)
    write(base / "huawei-complex-BD2-prepared.json", job)
    seed = NoteDocument(platform="xiaomi", account_id="synthetic-fixture-source", source_id=FIXTURE_ID,
                        title=job["title"], source_folder_id="synthetic-huawei-folder-K", source_folder_name="固定分组",
                        created_at="2026-08-15T08:00:00+00:00", updated_at="2026-08-16T09:00:00+00:00",
                        blocks=[Block(spans=[Span(text="保留文字", bold=True, strike=True)]),
                                Block(kind="list", ordered=True, spans=[Span(text="中文 English 😀")])])
    for index in range(2):
        path = tmp_path / f".private/session-lab/resources/image-{index}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        content = f"synthetic-original-{index}".encode()
        path.write_bytes(content)
        seed.attachments.append(Attachment(id=f"local-{index}", name=path.name, kind="image", mime="image/png",
                                           local_path=path.name, size=len(content), sha256=hashlib.sha256(content).hexdigest()))
        seed.blocks.append(Block(kind="attachment", attachment_id=f"local-{index}"))
    actual = seed.model_copy(deep=True)
    actual.platform, actual.account_id, actual.source_id = "huawei", job["expected_account"], "confirmed-new-note"
    actual.source_folder_id = "confirmed-native-group"
    # Independently written native readback: unsupported strike and list structure
    # are lost, but text, supported bold, order, images and time annotation remain.
    actual.blocks = [Block(spans=[Span(text="保留文字", bold=True)]),
                     Block(spans=[Span(text="1. "), Span(text="中文 English 😀")]),
                     Block(kind="attachment", attachment_id="remote-0"),
                     Block(kind="attachment", attachment_id="remote-1"),
                     Block(spans=[Span(text="2026-08-15 / 2026-08-16")])]
    for index, asset in enumerate(actual.attachments):
        asset.id = f"remote-{index}"
    original = NoteDocument(platform="huawei", account_id=job["expected_account"], source_id="original",
                            title="Existing untouched note", blocks=[Block(spans=[Span(text="Old body")])])
    store = Store(tmp_path / ".private/session-lab/notes.sqlite")
    store.replace_snapshot("huawei", job["expected_account"], [original, actual], True)
    key = receipt_key(seed, SimpleNamespace(spec=SimpleNamespace(id="huawei"), account_id=job["expected_account"]))
    store.save_receipt(key, "sending")
    for index in range(4):
        store.save_resource_receipt(key, f"resource-{index}", "uploaded")
    store.save_receipt(key, "confirmed", [actual.source_id])
    store.save_receipt(folder_key(seed, job["expected_account"]), "confirmed", [actual.source_folder_id])
    write(base / (FIXTURE_ID + "-before.json"), {"full_fingerprints": True, "count": 1,
                                                "target": {original.source_id: original.fingerprint()}})
    pending = tmp_path / ".private/evidence" / (FIXTURE_ID + ".json")
    write(pending, {"kind": job["kind"], "fixture_id": FIXTURE_ID, "platform": "huawei", "formal_acceptance": False,
                    "status": "readback_pending", "receipt_confirmed": True, "acknowledged_writes": 1,
                    "issue_codes": ["format_downgrade"], "cloud_resource_receipt_states": ["linked"] * 4})
    migrate = TaskReport(id="c" * 32, operation="migrate", status="partial", total=1, succeeded=1, completed=1,
                         started_at="2026-09-07T00:00:00+00:00", finished_at="2026-09-07T00:01:00+00:00",
                         issues=[ItemIssue(note_id=seed.source_id, code="format_downgrade", message=message) for message in (
                             "华为删除线、高亮、代码字体或链接已保留文本，样式未保留。", "华为列表已转换为带编号或符号的普通段落。")])
    store.save_task(migrate)
    fetch = TaskReport(id="d" * 32, operation="fetch", status="succeeded", total=2, succeeded=2, completed=2,
                       started_at="2026-09-07T01:00:00+00:00", finished_at="2026-09-07T01:01:00+00:00")
    store.save_task(fetch)
    scope = write_fetch_scope(tmp_path, store, "huawei", job["expected_account"], fetch)
    entry = {"target": "wps", "title": actual.title, "with_images": True, "with_group": True,
             "from_cloud_fixture": {"manifest": MANIFEST, "platform": "huawei", "account": job["expected_account"],
                                    "source_id": actual.source_id, "fingerprint": actual.fingerprint()}}
    return SimpleNamespace(root=tmp_path, store=store, seed=seed, original=original, actual=actual, key=key,
                           job=job, pending=pending, scope=scope, fetch=fetch, migrate=migrate, entry=entry,
                           builder=lambda _: seed, proof=tmp_path / ".private/evidence" / (FIXTURE_ID + "-readback.json"))


def save_proof(case):
    result = recompute_readback(case.root, case.store, case.builder, MANIFEST, case.scope, require_current_scope=True)
    write(case.proof, result)
    return result


def test_independent_readback_unlocks_exact_confirmed_seed_without_rewriting_failure_or_receipts(tmp_path):
    case = setup(tmp_path)
    before = case.pending.read_bytes(), case.store.receipt(case.key), case.store.resource_receipts(case.key)
    proof = save_proof(case)
    selected = select_source(case.entry, case.store, case.root, case.builder)
    assert selected.fingerprint() == case.actual.fingerprint()
    assert proof["before_count"] == 1 and proof["after_count"] == 2
    assert proof["cloud_writes"] == 0 and proof["formal_acceptance"] is False
    assert (case.pending.read_bytes(), case.store.receipt(case.key), case.store.resource_receipts(case.key)) == before


def test_later_unrelated_addition_does_not_require_repeating_a_verified_original_seed_fetch(tmp_path):
    case = setup(tmp_path)
    save_proof(case)
    added = case.original.model_copy(update={"source_id": "later-unrelated-note"})
    case.store.replace_snapshot("huawei", case.job["expected_account"], [case.original, case.actual, added], True)
    assert select_source(case.entry, case.store, case.root, case.builder).source_id == case.actual.source_id
    # Proof creation must bind the exact just-completed snapshot, not an older scope.
    with pytest.raises(BridgeError):
        recompute_readback(case.root, case.store, case.builder, MANIFEST, case.scope, require_current_scope=True)


@pytest.mark.parametrize("fault", ["no-proof", "status-only", "file-hash", "prepared-identity", "pending-evidence",
                                  "wrong-scope-account", "wrong-scope-platform", "wrong-scope-note", "changed-task",
                                  "old-fetch", "uncertain", "sending", "rejected", "multiple-ids", "unlinked-resource",
                                  "changed-original", "extra-scoped-note", "changed-image", "wrong-group", "lost-body",
                                  "missing-warning", "ambiguous-migration-task", "wrong-receipt-source", "wrong-source-id"])
def test_unconfirmed_or_mismatched_evidence_cannot_be_promoted_by_a_success_string(tmp_path, fault):
    case = setup(tmp_path)
    proof = save_proof(case)
    if fault == "no-proof":
        case.proof.unlink()
    elif fault == "status-only":
        write(case.proof, {"status": "verified", "content_verified": True})
    elif fault == "file-hash":
        proof["file_sha256"]["manifest"] = "0" * 64
        write(case.proof, proof)
    elif fault == "prepared-identity":
        job = {**case.job, "expected_account": "b" * 64}
        write(case.root / ".private/checkpoints/huawei-complex-BD2-prepared.json", job)
    elif fault == "pending-evidence":
        data = json.loads(case.pending.read_text("utf-8"))
        data["fixture_id"] = "another-fixture"
        write(case.pending, data)
    elif fault in ("wrong-scope-account", "wrong-scope-platform", "wrong-scope-note", "extra-scoped-note"):
        path = case.root / case.scope
        data = json.loads(path.read_text("utf-8"))
        if fault == "wrong-scope-account":
            data["account"] = "b" * 64
        elif fault == "wrong-scope-platform":
            data["platform"] = "wps"
        elif fault == "wrong-scope-note":
            data["notes"]["different-note"] = data["notes"].pop(case.actual.source_id)
        else:
            data["notes"]["unexplained-extra"] = "e" * 64
        write(path, data)
    elif fault in ("changed-task", "old-fetch"):
        if fault == "old-fetch":
            case.fetch.started_at = "2026-09-06T01:00:00+00:00"
        else:
            case.fetch.total = 3
        case.store.save_task(case.fetch)
    elif fault in ("uncertain", "sending", "rejected", "multiple-ids"):
        case.store.save_receipt(case.key, "confirmed" if fault == "multiple-ids" else fault,
                                [case.actual.source_id, "second-note"] if fault == "multiple-ids" else [case.actual.source_id])
    elif fault == "unlinked-resource":
        with case.store.connection() as db:
            db.execute("UPDATE resource_receipts SET status='uploaded' WHERE receipt_key=?", (case.key,))
    elif fault in ("changed-original", "wrong-group", "lost-body"):
        if fault == "changed-original":
            case.original.blocks[0].spans[0].text = "Altered existing body"
        elif fault == "wrong-group":
            case.actual.source_folder_id = "different-native-group"
        else:
            case.actual.blocks[0].spans[0].text = "Truncated body"
        case.store.replace_snapshot("huawei", case.job["expected_account"], [case.original, case.actual], True)
    elif fault == "changed-image":
        (case.root / ".private/session-lab/resources/image-0.png").write_bytes(b"altered-original")
    elif fault == "missing-warning":
        case.migrate.issues.pop()
        case.store.save_task(case.migrate)
    elif fault == "ambiguous-migration-task":
        case.store.save_task(case.migrate.model_copy(update={"id": "e" * 32}))
    elif fault == "wrong-receipt-source":
        case.seed.source_id = "different-source-seed"
    else:
        case.entry["from_cloud_fixture"]["source_id"] = "different-confirmed-note"
    state = case.store.receipt(case.key), case.store.resource_receipts(case.key), case.pending.read_bytes()
    with pytest.raises(BridgeError):
        select_source(case.entry, case.store, case.root, case.builder)
    assert (case.store.receipt(case.key), case.store.resource_receipts(case.key), case.pending.read_bytes()) == state


@pytest.mark.parametrize("fault", ["missing-warning", "old-fetch", "lost-body", "wrong-group", "changed-original"])
def test_proof_generation_itself_recomputes_content_and_scope_instead_of_only_binding_hashes(tmp_path, fault):
    case = setup(tmp_path)
    if fault == "missing-warning":
        case.migrate.issues.pop()
        case.store.save_task(case.migrate)
    elif fault == "old-fetch":
        case.migrate.finished_at = "2026-09-07T02:00:00+00:00"
        case.store.save_task(case.migrate)
    else:
        if fault == "lost-body":
            case.actual.blocks[0].spans[0].text = "lost"
        elif fault == "wrong-group":
            case.actual.source_folder_id = "wrong"
        else:
            case.original.blocks[0].spans[0].text = "changed"
        case.store.replace_snapshot("huawei", case.job["expected_account"], [case.original, case.actual], True)
        case.fetch.id = "f" * 32
        case.store.save_task(case.fetch)
        case.scope = write_fetch_scope(case.root, case.store, "huawei", case.job["expected_account"], case.fetch)
    with pytest.raises(BridgeError):
        save_proof(case)
    assert not case.proof.exists()


def test_unrelated_failed_seed_remains_closed_even_with_a_bd2_proof(tmp_path):
    case = setup(tmp_path)
    save_proof(case)
    with pytest.raises(BridgeError):
        verify_readback("huawei-complex-unrelated-job.json", case.store, case.root, case.builder)
