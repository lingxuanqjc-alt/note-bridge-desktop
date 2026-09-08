import importlib
import json
from types import SimpleNamespace

import pytest
from test_huawei_scoped_source import setup

from note_bridge.errors import BridgeError
from note_bridge.models import account_fingerprint
from note_bridge.operations import receipt_key

post = importlib.import_module("huawei_source_post")
scope = importlib.import_module("huawei_scoped_source")


def completed_batches(tmp_path, monkeypatch):
    ctx = setup(tmp_path, monkeypatch)
    initial = scope.capture([], tmp_path)
    notes = scope.verify_source(initial["proof"], ctx.store, tmp_path)
    for target, label in post.BATCHES:
        account = account_fingerprint(target, "synthetic-target-account")
        destination = SimpleNamespace(spec=SimpleNamespace(id=target), account_id=account)
        identifier = f"matrix-20260907-huawei-{target}-{label}"
        entries, items = [], []
        for index, (manifest, note) in enumerate(zip(scope.MANIFESTS, notes, strict=True)):
            key = receipt_key(note, destination)
            ctx.store.save_receipt(key, "sending", [f"synthetic-{target}-{index}"])
            for image in range(2):
                ctx.store.save_resource_receipt(key, f"synthetic-{target}-{index}-image-{image}", "uploaded")
            ctx.store.save_receipt(key, "confirmed", [f"synthetic-{target}-{index}"])
            entries.append({"title": note.display_title, "with_images": True, "with_group": True,
                "from_cloud_fixture": {"platform": "huawei", "account": note.account_id,
                    "manifest": manifest, "source_id": note.source_id, "fingerprint": note.fingerprint(),
                    "scoped_proof": initial["proof"]}})
            items.append({"source_id": note.source_id, "source_fingerprint": note.fingerprint(),
                "status": "api_verified", "receipt_status": "confirmed", "content_verified": True, "group_verified": True})
        job = {"kind": "cloud-matrix-batch", "id": identifier, "target": target, "expected_account": account,
            "armed": False, "source_policy": "direct_seed_only", "sources": entries}
        evidence = {"kind": job["kind"], "batch_id": identifier, "source": "huawei", "target": target,
            "status": "api_verified", "formal_acceptance": False, "source_scope": "fixed_huawei_K2_BD2_capture",
            "source_capture": initial["proof"], "whole_source_account_snapshot": False, "source_original_17_integrity": "pending",
            "items": items, "original_target_notes_unchanged": True, "only_confirmed_additions": True}
        if target == "vivo":
            evidence.update(whole_account_snapshot=False, comparison_scope="confirmed_tool_created_ids_only",
                synthetic_scope_originals_unchanged=True, synthetic_scope_only_confirmed_additions=True)
            evidence.pop("original_target_notes_unchanged")
            evidence.pop("only_confirmed_additions")
        (ctx.private / "checkpoints" / f"huawei-{target}-{label}-job.json").write_text(json.dumps(job), "utf-8")
        (ctx.private / "evidence" / (identifier + ".json")).write_text(json.dumps(evidence), "utf-8")
    return ctx


def test_one_exact_source_post_covers_five_batches_without_changing_their_old_reports_or_cache(tmp_path, monkeypatch):
    ctx = completed_batches(tmp_path, monkeypatch)
    database = ctx.private / "session-lab/notes.sqlite"
    before = database.read_bytes()
    reports = {p: p.read_bytes() for p in (ctx.private / "evidence").glob("matrix-*.json")}
    result = post.run([], tmp_path)
    assert result["status"] == "verified" and result["covered_batches"] == 5
    assert result["source_notes"] == 2 and result["source_images"] == 4
    assert result["whole_account_snapshot"] is False and result["original_17_integrity"] == "pending"
    assert len(ctx.calls) == 28, "One new 14-request capture covers all five migrations, not five repeated reads."
    assert database.read_bytes() == before and all(p.read_bytes() == raw for p, raw in reports.items())
    assert not ctx.store.snapshot("huawei", ctx.account)["complete"]
    with pytest.raises(FileExistsError):
        post.run([], tmp_path)
    assert len(ctx.calls) == 28, "A consumed post-check cannot silently perform another cloud read."


@pytest.mark.parametrize("mode", ["not_complete", "wrong_capture", "unconfirmed_receipt", "subset", "wrong_scope"])
def test_post_waits_for_all_exact_directions_and_receipts_before_reading_cloud(tmp_path, monkeypatch, mode):
    ctx = completed_batches(tmp_path, monkeypatch)
    path = ctx.private / "evidence/matrix-20260907-huawei-wps-BD14.json"
    evidence = json.loads(path.read_text("utf-8"))
    if mode == "not_complete":
        evidence["status"] = "needs_review"
    elif mode == "wrong_capture":
        evidence["source_capture"] = {"path": "unrelated", "sha256": "a" * 64}
    elif mode == "subset":
        evidence["items"] = evidence["items"][:1]
    elif mode == "wrong_scope":
        evidence["whole_source_account_snapshot"] = True
    else:
        audit = post.audit(tmp_path, ctx.store)
        ctx.store.save_receipt(audit["batches"][1]["receipts"][0]["key"], "uncertain")
    path.write_text(json.dumps(evidence), "utf-8")
    with pytest.raises(BridgeError):
        post.run([], tmp_path)
    assert len(ctx.calls) == 14 and not (tmp_path / post.OUTPUT).exists()


def test_changed_receipt_binding_after_capture_remains_pending_and_never_rewrites_history(tmp_path, monkeypatch):
    ctx = completed_batches(tmp_path, monkeypatch)
    real_capture = post.capture
    def capture_then_change(jars, root):
        result = real_capture(jars, root)
        path = ctx.private / "evidence/matrix-20260907-huawei-honor-BD15.json"
        evidence = json.loads(path.read_text("utf-8"))
        evidence["operator_annotation"] = "synthetic concurrent change"
        path.write_text(json.dumps(evidence), "utf-8")
        return result
    monkeypatch.setattr(post, "capture", capture_then_change)
    result = post.run([], tmp_path)
    assert result["status"] == "needs_review" and result["scope_complete"] is False
    assert len(ctx.calls) == 28


def test_fresh_etag_change_cannot_be_hidden_by_equal_decoded_body(tmp_path, monkeypatch):
    completed_batches(tmp_path, monkeypatch)
    real_capture = post.capture
    def changed_version(jars, root):
        result = real_capture(jars, root)
        path = root / result["proof"]["path"]
        proof = json.loads(path.read_text("utf-8"))
        proof["etags"] = {key: "synthetic-new-version" for key in proof["etags"]}
        path.write_text(json.dumps(proof), "utf-8")
        result["proof"]["sha256"] = scope.digest(path.read_bytes())
        return result
    monkeypatch.setattr(post, "capture", changed_version)
    result = post.run([], tmp_path)
    assert result["status"] == "needs_review" and result["scope_complete"] is False
    report = json.loads((tmp_path / post.OUTPUT).read_text("utf-8"))
    assert report["source_fingerprints_unchanged"] is True and report["source_etags_unchanged"] is False
