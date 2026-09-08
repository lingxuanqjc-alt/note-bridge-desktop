import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError
from note_bridge.models import Attachment, Block, NoteDocument, PlatformId, Span
from note_bridge.operations import receipt_key
from note_bridge.providers.huawei_groups import folder_key
from note_bridge.storage import Store

scripts = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(scripts))
spec = importlib.util.spec_from_file_location("cloud_fixture_source", scripts / "cloud_fixture_source.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture_scope(tmp_path):
    private = tmp_path / ".private"
    (private / "checkpoints").mkdir(parents=True)
    (private / "evidence").mkdir()
    title = "笔记互迁验收 · 来源"
    original = {"id": "fixture-20260906-source", "target": "meizu", "expected_account": "account", "title": title, "armed": False}
    (private / "checkpoints/source-job.json").write_text(json.dumps(original), "utf-8")
    (private / "evidence/fixture-20260906-source.json").write_text(json.dumps({"status": "verified"}), "utf-8")
    seed = NoteDocument(platform=PlatformId.XIAOMI, account_id="synthetic", source_id="seed", title=title)
    note = seed.model_copy(update={"platform": PlatformId.MEIZU, "account_id": "account", "source_id": "remote"})
    store = Store(private / "notes.sqlite")
    store.replace_snapshot(PlatformId.MEIZU, "account", [note], True)
    target = SimpleNamespace(spec=SimpleNamespace(id="meizu"), account_id="account")
    store.save_receipt(receipt_key(seed, target), "confirmed", ["remote"])
    job = {"target": "wps", "title": title, "from_cloud_fixture": {"manifest": "source-job.json",
        "platform": "meizu", "account": "account", "source_id": "remote", "fingerprint": note.fingerprint()}}
    return job, store, seed, note


def test_only_actual_confirmed_cloud_note_is_used_without_changing_identity(tmp_path):
    job, store, seed, note = fixture_scope(tmp_path)
    selected = module.select_source(job, store, tmp_path, lambda _: seed)
    assert selected == note and selected.platform == PlatformId.MEIZU and selected.source_id == "remote"


def batch_scope(tmp_path):
    job, store, seed, prior = fixture_scope(tmp_path)
    derived = prior.model_copy(update={"platform": PlatformId.WPS, "account_id": "wps-account", "source_id": "wps-remote"})
    store.replace_snapshot(PlatformId.WPS, "wps-account", [derived], True)
    target = SimpleNamespace(spec=SimpleNamespace(id="wps"), account_id="wps-account")
    store.save_receipt(receipt_key(prior, target), "confirmed", [derived.source_id])
    batch = {"kind": "cloud-matrix-batch", "id": "matrix-20260906-chain", "target": "wps",
             "expected_account": "wps-account", "armed": False, "sources": [job]}
    (tmp_path / ".private/checkpoints/chain-job.json").write_text(json.dumps(batch), "utf-8")
    evidence = {"batch_id": batch["id"], "target": "wps", "status": "api_verified",
                "original_target_notes_unchanged": True, "only_confirmed_additions": True,
                "items": [{"source_id": prior.source_id, "source_fingerprint": prior.fingerprint(),
                           "content_verified": True, "group_verified": True}]}
    (tmp_path / ".private/evidence/matrix-20260906-chain.json").write_text(json.dumps(evidence), "utf-8")
    outer = {"target": "honor", "title": derived.display_title, "from_cloud_fixture": {
        "batch_manifest": "chain-job.json", "entry_index": 0, "platform": "wps", "account": "wps-account",
        "source_id": derived.source_id, "fingerprint": derived.fingerprint()}}
    return outer, store, seed, prior, derived


def test_verified_cloud_batch_can_be_next_source_with_its_actual_identity(tmp_path):
    job, store, seed, _, derived = batch_scope(tmp_path)
    assert module.select_source(job, store, tmp_path, lambda _: seed) == derived


@pytest.mark.parametrize("failure", ["ancestor_changed", "unconfirmed", "wrong_index", "ambiguous_path", "unverified_item"])
def test_derived_source_requires_unbroken_account_receipt_and_content_proof(tmp_path, failure):
    job, store, seed, prior, derived = batch_scope(tmp_path)
    if failure == "ancestor_changed":
        prior.title = "changed"
        store.replace_snapshot(prior.platform, prior.account_id, [prior], True)
    elif failure == "unconfirmed":
        target = SimpleNamespace(spec=SimpleNamespace(id="wps"), account_id="wps-account")
        store.save_receipt(receipt_key(prior, target), "uncertain", [derived.source_id])
    elif failure == "wrong_index":
        job["from_cloud_fixture"]["entry_index"] = True
    elif failure == "ambiguous_path":
        job["from_cloud_fixture"]["manifest"] = "source-job.json"
    else:
        path = tmp_path / ".private/evidence/matrix-20260906-chain.json"
        evidence = json.loads(path.read_text("utf-8"))
        evidence["items"][0]["content_verified"] = False
        path.write_text(json.dumps(evidence), "utf-8")
    with pytest.raises(BridgeError):
        module.select_source(job, store, tmp_path, lambda _: seed)


@pytest.mark.parametrize("change", ["account", "source_id", "fingerprint", "manifest"])
def test_wrong_account_original_note_or_changed_content_cannot_be_selected(tmp_path, change):
    job, store, seed, _ = fixture_scope(tmp_path)
    job["from_cloud_fixture"][change] = "../outside" if change == "manifest" else "wrong"
    with pytest.raises(BridgeError):
        module.select_source(job, store, tmp_path, lambda _: seed)


def test_incomplete_source_snapshot_cannot_be_used_for_direction_verification(tmp_path):
    job, store, seed, note = fixture_scope(tmp_path)
    store.replace_snapshot(PlatformId.MEIZU, "account", [note], False)
    with pytest.raises(BridgeError):
        module.select_source(job, store, tmp_path, lambda _: seed)


@pytest.mark.parametrize("changed", ["missing", "different_bytes"])
def test_unavailable_or_changed_image_cannot_be_uploaded_under_verified_note_identity(tmp_path, changed):
    job, store, seed, note = fixture_scope(tmp_path)
    payload = b"original-image"
    note.attachments = [Attachment(id="image", kind="image", name="test.png", local_path="test.png",
                                   size=len(payload), sha256=hashlib.sha256(payload).hexdigest())]
    store.replace_snapshot(PlatformId.MEIZU, "account", [note], True)
    job["with_images"] = True
    job["from_cloud_fixture"]["fingerprint"] = note.fingerprint()
    if changed == "different_bytes":
        path = tmp_path / ".private/session-lab/resources/test.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"modified-image")
    with pytest.raises(BridgeError):
        module.select_source(job, store, tmp_path, lambda _: seed)


def recovered_scope(tmp_path, label="AA2"):
    """A repaired draft plus the unchanged successful neighbours in its old batch."""
    private = tmp_path / ".private"
    for name in ("checkpoints", "evidence", "session-lab/resources"):
        (private / name).mkdir(parents=True, exist_ok=True)
    platform, count, repaired = ("meizu", 3, 2) if label == "AA2" else ("honor", 1, 0)
    batch_id = f"matrix-20260906-{platform}-huawei-{label}"
    name = f"{platform}-huawei-{label}-job.json"
    account = "huawei-account"
    target = SimpleNamespace(spec=SimpleNamespace(id="huawei"), account_id=account)
    store = Store(private / "notes.sqlite")
    seeds, priors, actuals, entries = {}, [], [], []
    warning = "华为列表已转换为带编号或符号的普通段落。"
    for index in range(count):
        title = f"笔记互迁验收 · 来源 {index}"
        seed = NoteDocument(platform=PlatformId.XIAOMI, account_id="synthetic", source_id=f"seed-{index}", title=title)
        prior = NoteDocument(platform=platform, account_id="source-account", source_id=f"source-{index}", title=title,
                             source_folder_id="source-folder", source_folder_name="验收分组",
                             blocks=[Block(spans=[Span(text="中文😀", bold=True)]),
                                     Block(kind="list", ordered=True, spans=[Span(text="第一项")])])
        for asset_index in range(2):
            payload = f"original-{index}-{asset_index}".encode()
            relative = f"source-{index}-{asset_index}.png"
            (private / "session-lab/resources" / relative).write_bytes(payload)
            asset = Attachment(id=f"image-{asset_index}", name=relative, kind="image", local_path=relative,
                               size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
            prior.attachments.append(asset)
            prior.blocks.append(Block(kind="attachment", attachment_id=asset.id))
        actual = prior.model_copy(deep=True, update={"platform": PlatformId.HUAWEI, "account_id": account,
                                                    "source_id": f"remote-{index}", "source_folder_id": "target-folder"})
        actual.blocks[1] = Block(spans=[Span(text="1. "), Span(text="第一项")])
        for asset_index, asset in enumerate(actual.attachments):
            asset.id = f"remote-image-{index}-{asset_index}"
            relative = f"target-{index}-{asset_index}.png"
            payload = (private / "session-lab/resources" / asset.local_path).read_bytes()
            (private / "session-lab/resources" / relative).write_bytes(payload)
            asset.local_path = relative
            actual.blocks[2 + asset_index].attachment_id = asset.id
        fixture_id = f"fixture-20260906-source-{index}"
        original = {"id": fixture_id, "target": platform, "expected_account": "source-account", "title": title, "armed": False}
        (private / f"checkpoints/source-{index}-job.json").write_text(json.dumps(original), "utf-8")
        (private / f"evidence/{fixture_id}.json").write_text(json.dumps({"status": "verified"}), "utf-8")
        seeds[fixture_id] = seed
        prior_target = SimpleNamespace(spec=SimpleNamespace(id=platform), account_id=prior.account_id)
        store.save_receipt(receipt_key(seed, prior_target), "confirmed", [prior.source_id])
        store.save_receipt(receipt_key(prior, target), "confirmed", [actual.source_id])
        store.save_receipt(folder_key(prior, account), "confirmed", [actual.source_folder_id])
        if index == repaired:
            store.save_receipt(receipt_key(prior, target) + f":{label}-scoped-recovery", "confirmed", [actual.source_id])
        entries.append({"title": title, "with_images": True, "with_group": True, "from_cloud_fixture": {
            "manifest": f"source-{index}-job.json", "platform": platform, "account": prior.account_id,
            "source_id": prior.source_id, "fingerprint": prior.fingerprint()}})
        priors.append(prior)
        actuals.append(actual)
    existing = actuals[0].model_copy(deep=True, update={"source_id": "pre-existing", "title": "原有笔记"})
    store.replace_snapshot(platform, "source-account", priors, True)
    store.replace_snapshot("huawei", account, [existing, *actuals], True)
    batch = {"kind": "cloud-matrix-batch", "id": batch_id, "target": "huawei",
             "expected_account": account, "armed": False, "sources": entries}
    baseline_name = f".private/checkpoints/{batch_id}-before.json"
    original_evidence = {"kind": "cloud-matrix-batch", "batch_id": batch_id, "source": platform, "target": "huawei",
                         "status": "needs_review", "before_count": 1, "baseline_file": baseline_name,
                         "items": [], "issues": [{"note_id": n.source_id, "code": "format_downgrade", "message": warning} for n in priors]}
    baseline = {"target": {existing.source_id: existing.fingerprint()},
                "source": {n.source_id: n.fingerprint() for n in priors}}
    recovery = {"kind": f"huawei-{label}-scoped-recovery", "mode": "repair", "status": "verified_with_degradation",
                "creates": 0, "uploads": 0, "updates": 1, "content_verified": True,
                "existing_images": 2, "missing_images": 0,
                "original_other_notes_unchanged": True, "warnings": [warning]}
    recovery_baseline = {"fingerprints": {existing.source_id: existing.fingerprint(),
                                         **{n.source_id: n.fingerprint() for n in actuals}}}
    recovery_baseline["fingerprints"][actuals[repaired].source_id] = "e" * 64
    paths = {"manifest": private / "checkpoints" / name,
             "original_evidence": private / "evidence" / (batch_id + ".json"),
             "baseline": tmp_path / baseline_name,
             "recovery_evidence": private / "evidence" / f"huawei-{label}-recovery-repair.json",
             "recovery_baseline": private / "checkpoints" / f"huawei-{label}-recovery-before.json"}
    for key, value in {"manifest": batch, "original_evidence": original_evidence, "baseline": baseline,
                       "recovery_evidence": recovery, "recovery_baseline": recovery_baseline}.items():
        paths[key].write_text(json.dumps(value), "utf-8")
    proof_path = private / "evidence" / f"huawei-{label}-source-lineage.json"
    proof = {"kind": "huawei-recovered-source-lineage", "batch_id": batch_id, "formal_acceptance": False, "cloud_writes": 0,
             "file_sha256": {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in paths.items()},
             "items": [{"entry_index": index, "receipt_key": receipt_key(prior, target),
                        "source": {"platform": prior.platform, "account": prior.account_id,
                                   "source_id": prior.source_id, "fingerprint": prior.fingerprint()},
                        "target": {"platform": actual.platform, "account": actual.account_id,
                                   "source_id": actual.source_id, "fingerprint": actual.fingerprint()}}
                       for index, (prior, actual) in enumerate(zip(priors, actuals, strict=True))]}
    proof_path.write_text(json.dumps(proof), "utf-8")
    actual = actuals[repaired]
    job = {"target": "wps", "title": actual.title, "with_images": True, "with_group": True,
           "from_cloud_fixture": {"batch_manifest": name, "entry_index": repaired, "platform": "huawei",
                                  "account": account, "source_id": actual.source_id, "fingerprint": actual.fingerprint()}}
    return SimpleNamespace(job=job, store=store, builder=lambda original: seeds[original["id"]], priors=priors,
                           actuals=actuals, existing=existing, target=target, paths=paths, proof_path=proof_path,
                           proof=proof, repaired=repaired, label=label)


@pytest.mark.parametrize("label,index", [("AA2", 0), ("AA2", 1), ("AA2", 2), ("AA6", 0)])
def test_reconciled_huawei_source_retains_actual_identity_without_rewriting_old_failure(tmp_path, label, index):
    ctx = recovered_scope(tmp_path, label)
    actual = ctx.actuals[index]
    ctx.job.update(title=actual.title)
    ctx.job["from_cloud_fixture"].update(entry_index=index, source_id=actual.source_id, fingerprint=actual.fingerprint())
    original = ctx.paths["original_evidence"].read_bytes()
    assert module.select_source(ctx.job, ctx.store, tmp_path, ctx.builder) == actual
    assert ctx.paths["original_evidence"].read_bytes() == original
    assert json.loads(original)["status"] == "needs_review"


@pytest.mark.parametrize("failure", ["missing_proof", "wrong_file_hash", "wrong_batch", "wrong_index", "duplicate_index",
                                     "wrong_source_account", "wrong_target_account", "wrong_receipt_binding",
                                     "unconfirmed", "wrong_remote_id", "wrong_repair_id", "repair_uncertain",
                                     "incomplete_snapshot", "original_note_changed", "ancestor_changed",
                                     "missing_target_image", "target_image_changed", "group_unconfirmed"])
def test_recovery_status_cannot_authorize_another_account_unknown_write_or_changed_data(tmp_path, failure):
    ctx = recovered_scope(tmp_path)
    prior, actual = ctx.priors[ctx.repaired], ctx.actuals[ctx.repaired]
    key = receipt_key(prior, ctx.target)
    binding = ctx.proof["items"][ctx.repaired]
    if failure == "missing_proof":
        ctx.proof_path.unlink()
    elif failure == "wrong_file_hash":
        ctx.proof["file_sha256"]["manifest"] = "0" * 64
    elif failure == "wrong_batch":
        ctx.proof["batch_id"] = "matrix-20260906-other"
    elif failure == "wrong_index":
        binding["entry_index"] = 3
    elif failure == "duplicate_index":
        binding["entry_index"] = 0
    elif failure == "wrong_source_account":
        binding["source"]["account"] = "another-account"
    elif failure == "wrong_target_account":
        binding["target"]["account"] = "another-account"
    elif failure == "wrong_receipt_binding":
        binding["receipt_key"] = "0" * 64
    elif failure in ("unconfirmed", "wrong_remote_id"):
        ctx.store.save_receipt(key, "uncertain" if failure == "unconfirmed" else "confirmed",
                               [actual.source_id if failure == "unconfirmed" else "another-note"])
    elif failure in ("wrong_repair_id", "repair_uncertain"):
        ctx.store.save_receipt(key + ":AA2-scoped-recovery", "uncertain" if failure == "repair_uncertain" else "confirmed",
                               [actual.source_id if failure == "repair_uncertain" else "another-note"])
    elif failure == "incomplete_snapshot":
        ctx.store.replace_snapshot("huawei", ctx.target.account_id, [ctx.existing, *ctx.actuals], False)
    elif failure == "original_note_changed":
        ctx.existing.title = "changed"
        ctx.store.replace_snapshot("huawei", ctx.target.account_id, [ctx.existing, *ctx.actuals], True)
    elif failure == "ancestor_changed":
        prior.blocks[0].spans[0].text = "changed"
        ctx.store.replace_snapshot(prior.platform, prior.account_id, ctx.priors, True)
    elif failure in ("missing_target_image", "target_image_changed"):
        path = tmp_path / ".private/session-lab/resources" / actual.attachments[0].local_path
        if failure == "missing_target_image":
            path.unlink()
        else:
            path.write_bytes(b"different-image")
    else:
        ctx.store.save_receipt(folder_key(prior, ctx.target.account_id), "uncertain", [actual.source_folder_id])
    if failure != "missing_proof":
        ctx.proof_path.write_text(json.dumps(ctx.proof), "utf-8")
    with pytest.raises(BridgeError):
        module.select_source(ctx.job, ctx.store, tmp_path, ctx.builder)


@pytest.mark.parametrize("failure", ["wrong_original_source", "wrong_original_target", "wrong_source_baseline",
                                     "unexplained_recovery_addition", "repair_not_verified", "repair_created_note",
                                     "missing_downgrade", "recovery_was_not_double_image",
                                     "changed_body", "changed_image_metadata", "neighbour_changed",
                                     "unreviewed_manifest", "unreviewed_entry"])
def test_even_rebound_recovery_proof_must_recompute_scope_content_and_original_baselines(tmp_path, failure):
    ctx = recovered_scope(tmp_path)
    document_key = "original_evidence"
    value = json.loads(ctx.paths[document_key].read_text("utf-8"))
    if failure == "wrong_original_source":
        value["source"] = "vivo"
    elif failure == "wrong_original_target":
        value["target"] = "meizu"
    elif failure == "wrong_source_baseline":
        document_key = "baseline"
        value = json.loads(ctx.paths[document_key].read_text("utf-8"))
        value["source"][ctx.priors[2].source_id] = "0" * 64
    elif failure == "unexplained_recovery_addition":
        document_key = "recovery_baseline"
        value = json.loads(ctx.paths[document_key].read_text("utf-8"))
        value["fingerprints"]["unexpected-note"] = "0" * 64
    elif failure in ("repair_not_verified", "repair_created_note", "missing_downgrade", "recovery_was_not_double_image"):
        document_key = "recovery_evidence"
        value = json.loads(ctx.paths[document_key].read_text("utf-8"))
        field, replacement = {"repair_not_verified": ("content_verified", False),
                              "repair_created_note": ("creates", 1), "missing_downgrade": ("warnings", []),
                              "recovery_was_not_double_image": ("existing_images", 1)}[failure]
        value[field] = replacement
    elif failure in ("changed_body", "changed_image_metadata", "neighbour_changed"):
        index = 0 if failure == "neighbour_changed" else ctx.repaired
        actual = ctx.actuals[index]
        if failure == "changed_image_metadata":
            actual.attachments[0].sha256 = "0" * 64
        else:
            actual.blocks[0].spans[0].text = "missing original body"
        ctx.proof["items"][index]["target"]["fingerprint"] = actual.fingerprint()
        if index == ctx.repaired:
            ctx.job["from_cloud_fixture"]["fingerprint"] = actual.fingerprint()
        ctx.store.replace_snapshot("huawei", ctx.target.account_id, [ctx.existing, *ctx.actuals], True)
    else:
        document_key = "manifest"
        value = json.loads(ctx.paths[document_key].read_text("utf-8"))
        if failure == "unreviewed_manifest":
            path = ctx.paths[document_key].with_name("other-job.json")
            path.write_text(json.dumps(value), "utf-8")
            ctx.job["from_cloud_fixture"]["batch_manifest"] = path.name
        else:
            value["sources"].append(value["sources"][2])
            ctx.job["from_cloud_fixture"]["entry_index"] = 3
    ctx.paths[document_key].write_text(json.dumps(value), "utf-8")
    ctx.proof["file_sha256"] = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in ctx.paths.items()}
    ctx.proof_path.write_text(json.dumps(ctx.proof), "utf-8")
    with pytest.raises(BridgeError):
        module.select_source(ctx.job, ctx.store, tmp_path, ctx.builder)
