"""One fresh original-source read must cover all six already confirmed routes."""
import importlib
import json
from types import SimpleNamespace

import pytest
from test_fixed_oppo_sources import oppo, setup

from note_bridge.errors import BridgeError
from note_bridge.models import PlatformId, account_fingerprint
from note_bridge.operations import receipt_key

post = importlib.import_module("oppo_source_post")


def completed_batches(tmp_path, monkeypatch):
    ctx = setup(tmp_path, monkeypatch, oppo)
    initial = oppo.capture([], tmp_path)
    notes = oppo.verify_source(initial["proof"], ctx.store, tmp_path)
    reference = {"path": ".private/evidence/huawei-BI4-readback-" + "a" * 32 + "/proof.json", "sha256": "b" * 64}
    recovered = []
    for target, label in post.BATCHES:
        account = account_fingerprint(target, "synthetic-target-account")
        destination = SimpleNamespace(spec=SimpleNamespace(id=target), account_id=account)
        identifier = f"matrix-20260907-oppo-{target}-{label}"
        entries, items = [], []
        for index, (manifest, note) in enumerate(zip(oppo.MANIFESTS, notes, strict=True)):
            key = receipt_key(note, destination)
            remote_id = f"synthetic-{target}-{index}"
            ctx.store.save_receipt(key, "sending", [remote_id])
            for image in range(2):
                ctx.store.save_resource_receipt(key, f"synthetic-{target}-{index}-image-{image}", "uploaded")
            ctx.store.save_receipt(key, "confirmed", [remote_id])
            entries.append({"title": note.display_title, "with_images": True, "with_group": bool(note.source_folder_name),
                "from_cloud_fixture": {"platform": "oppo", "account": note.account_id,
                    "manifest": manifest, "source_id": note.source_id, "fingerprint": note.fingerprint(),
                    "scoped_proof": initial["proof"]}})
            items.append({"source_id": note.source_id, "source_fingerprint": note.fingerprint(),
                "status": "api_verified", "receipt_status": "confirmed", "content_verified": True, "group_verified": True})
            if target == "huawei":
                recovered.append(note.model_copy(update={"platform": PlatformId.HUAWEI, "account_id": account, "source_id": remote_id}))
        job = {"kind": "cloud-matrix-batch", "id": identifier, "target": target, "expected_account": account,
            "armed": False, "source_policy": "direct_seed_only", "oppo_scope": "OR1_fixed_direct_12", "sources": entries}
        baseline_path = f".private/checkpoints/{identifier}-before.json"
        baseline = {"source": {n.source_id: n.fingerprint() for n in notes}, "source_capture": initial["proof"],
            "source_scope": "fixed_oppo_capture", "whole_source_account_snapshot": False}
        evidence = {"kind": job["kind"], "batch_id": identifier, "source": "oppo", "target": target,
            "status": "api_verified", "formal_acceptance": False, "source_scope": "fixed_oppo_capture",
            "source_capture": initial["proof"], "whole_source_account_snapshot": False, "oppo_scope": job["oppo_scope"],
            "baseline_file": baseline_path, "succeeded": 2, "skipped": 0,
            "items": items, "original_target_notes_unchanged": True, "only_confirmed_additions": True}
        if target == "huawei":
            evidence.update(status="needs_review", items=[], code="matrix_read_incomplete")
            evidence.pop("original_target_notes_unchanged")
            evidence.pop("only_confirmed_additions")
        elif target == "vivo":
            evidence.update(whole_account_snapshot=False, comparison_scope="confirmed_tool_created_ids_only",
                synthetic_scope_originals_unchanged=True, synthetic_scope_only_confirmed_additions=True)
            evidence.pop("original_target_notes_unchanged")
            evidence.pop("only_confirmed_additions")
            baseline["target_scope"] = "confirmed_tool_created_ids_only"
        for name in (f"oppo-{target}-{label}-job.json", identifier + "-consumed-job.json"):
            (ctx.private / "checkpoints" / name).write_text(json.dumps(job), "utf-8")
        if target == "meizu":
            old_id = "matrix-20260907-oppo-meizu-BI10"
            old_job = {**job, "id": old_id}
            for name in ("oppo-meizu-BI10-job.json", old_id + "-consumed-job.json"):
                (ctx.private / "checkpoints" / name).write_text(json.dumps(old_job), "utf-8")
            old = {key: value for key, value in evidence.items()
                if key not in ("baseline_file", "succeeded", "skipped", "original_target_notes_unchanged", "only_confirmed_additions")}
            old.update(batch_id=old_id, status="needs_review", source_post_readback="pending",
                official_rendering="pending", items=[], code="session_expired")
            (ctx.private / "evidence" / (old_id + ".json")).write_text(json.dumps(old), "utf-8")
        (tmp_path / baseline_path).write_text(json.dumps(baseline), "utf-8")
        (ctx.private / "evidence" / (identifier + ".json")).write_text(json.dumps(evidence), "utf-8")
    checks = []
    def verify_bi4(actual, store, root):
        # The real helper's body/resource/receipt proof validation has its own tests.
        # This seam ensures source-post cannot bypass that verifier or choose latest.
        assert actual == reference and root == tmp_path
        checks.append(actual)
        return recovered
    monkeypatch.setattr(post, "verify_bi4_target", verify_bi4)
    return ctx, reference, checks


def test_one_fresh_read_covers_six_routes_without_upgrading_bi4_original_body_claim(tmp_path, monkeypatch):
    ctx, reference, checks = completed_batches(tmp_path, monkeypatch)
    database = ctx.private / "session-lab/notes.sqlite"
    before = database.read_bytes()
    originals = {p: p.read_bytes() for p in (ctx.private / "evidence").glob("matrix-*.json")}
    result = post.run([], tmp_path, reference)
    assert result["status"] == "verified" and result["covered_batches"] == 6
    assert result["batch_ids"] == [f"matrix-20260907-oppo-{target}-{label}" for target, label in post.BATCHES]
    assert "matrix-20260907-oppo-meizu-BI13" in result["batch_ids"]
    assert "matrix-20260907-oppo-meizu-BI10" not in result["batch_ids"]
    assert result["source_notes"] == 2 and result["source_images"] == 4
    assert result["whole_account_snapshot"] is False
    assert result["huawei_target_original_22_body_integrity"] == "not_rechecked"
    assert len(ctx.calls) == 24, "Exactly one additional 12-request source capture covers six outbound migrations."
    assert len(checks) == 2, "Explicit recovery is checked both before and after the single fresh capture."
    assert database.read_bytes() == before and all(p.read_bytes() == raw for p, raw in originals.items())
    report = json.loads((tmp_path / post.OUTPUT).read_text("utf-8"))
    recovery = report["before"]["batches"][1]
    assert recovery["original_evidence_status"] == "needs_review"
    assert recovery["original_22_body_integrity"] == "not_rechecked"
    assert recovery["prewrite_listing_etags_available"] is False
    assert report["before"]["batches"][4]["abandoned_attempt"]["status"] == "failed_before_target_baseline_and_migration"
    with pytest.raises(FileExistsError):
        post.run([], tmp_path, reference)
    assert len(ctx.calls) == 24, "Consumed source-post must not silently recapture."


@pytest.mark.parametrize("change", ["baseline", "success", "uncertain", "different_pair"])
def test_meizu_replacement_is_only_for_the_known_prewrite_failure(tmp_path, monkeypatch, change):
    ctx, reference, _ = completed_batches(tmp_path, monkeypatch)
    old_id = "matrix-20260907-oppo-meizu-BI10"
    if change == "baseline":
        (ctx.private / "checkpoints" / (old_id + "-before.json")).write_text("{}", "utf-8")
    elif change == "different_pair":
        path = ctx.private / "checkpoints" / (old_id + "-consumed-job.json")
        old = json.loads(path.read_text("utf-8"))
        old["sources"][0]["from_cloud_fixture"]["source_id"] = "another-note"
        path.write_text(json.dumps(old), "utf-8")
    else:
        path = ctx.private / "evidence" / (old_id + ".json")
        old = json.loads(path.read_text("utf-8"))
        old.update(status="api_verified" if change == "success" else "uncertain", succeeded=2)
        path.write_text(json.dumps(old), "utf-8")
    with pytest.raises(BridgeError):
        post.run([], tmp_path, reference)
    assert len(ctx.calls) == 12 and not (tmp_path / post.OUTPUT).exists(), "Never turn a written/unknown old attempt into a replacement."


@pytest.mark.parametrize("mode", ["pending", "wrong_capture", "subset", "body_unverified", "wrong_scope",
    "source_changed", "unconfirmed", "resource_unlinked", "unconsumed", "wrong_manifest", "bi4_rejected"])
def test_incomplete_or_changed_bindings_cause_zero_new_cloud_requests(tmp_path, monkeypatch, mode):
    ctx, reference, _ = completed_batches(tmp_path, monkeypatch)
    path = ctx.private / "evidence/matrix-20260907-oppo-vivo-BI12.json"
    evidence = json.loads(path.read_text("utf-8"))
    if mode == "pending":
        evidence["status"] = "needs_review"
    elif mode == "wrong_capture":
        evidence["source_capture"] = {"path": "unrelated", "sha256": "a" * 64}
    elif mode == "subset":
        evidence["items"] = evidence["items"][:1]
    elif mode == "body_unverified":
        evidence["items"][0]["content_verified"] = False
    elif mode == "wrong_scope":
        evidence["whole_account_snapshot"] = True
    elif mode == "source_changed":
        evidence["items"][0]["source_fingerprint"] = "a" * 64
    elif mode in ("unconfirmed", "resource_unlinked"):
        receipt = post.audit(tmp_path, ctx.store, reference)["batches"][-1]["receipts"][0]
        if mode == "unconfirmed":
            ctx.store.save_receipt(receipt["key"], "uncertain")
        else:
            # Normal APIs refuse this downgrade; model a damaged historical row.
            with ctx.store.connection() as db:
                db.execute("UPDATE resource_receipts SET status='uploaded' WHERE receipt_key=? AND resource_id=?",
                    (receipt["key"], receipt["resources"][0][0]))
    elif mode in ("unconsumed", "wrong_manifest"):
        job_path = ctx.private / "checkpoints/oppo-vivo-BI12-job.json"
        job = json.loads(job_path.read_text("utf-8"))
        if mode == "unconsumed":
            job["armed"] = True
        else:
            job["sources"][0]["from_cloud_fixture"]["manifest"] = "derived-batch-job.json"
            consumed = ctx.private / "checkpoints/matrix-20260907-oppo-vivo-BI12-consumed-job.json"
            consumed.write_text(json.dumps(job), "utf-8")
        job_path.write_text(json.dumps(job), "utf-8")
    else:
        def reject(*args):
            raise BridgeError("invalid_BI4_readback_scope", "Synthetic invalid recovery proof")
        monkeypatch.setattr(post, "verify_bi4_target", reject)
    path.write_text(json.dumps(evidence), "utf-8")
    with pytest.raises(BridgeError):
        post.run([], tmp_path, reference)
    assert len(ctx.calls) == 12 and not (tmp_path / post.OUTPUT).exists()


@pytest.mark.parametrize("change", ["version", "receipt_binding", "replayed_capture"])
def test_same_body_cannot_hide_version_change_or_replayed_pre_migration_capture(tmp_path, monkeypatch, change):
    ctx, reference, _ = completed_batches(tmp_path, monkeypatch)
    original = post.capture
    def modified(jars, root):
        if change == "replayed_capture":
            old = post.audit(root, ctx.store, reference)["source_capture"]
            return {"status": "captured", "scope_complete": True, "proof": old}
        result = original(jars, root)
        if change == "receipt_binding":
            path = ctx.private / "evidence/matrix-20260907-oppo-honor-BI8.json"
            data = json.loads(path.read_text("utf-8"))
            data["operator_annotation"] = "synthetic concurrent update"
            path.write_text(json.dumps(data), "utf-8")
        else:
            path = root / result["proof"]["path"]
            proof = json.loads(path.read_text("utf-8"))
            proof["versions"] = {key: "new-version" for key in proof["versions"]}
            path.write_text(json.dumps(proof), "utf-8")
            result["proof"]["sha256"] = oppo.digest(path.read_bytes())
        return result
    monkeypatch.setattr(post, "capture", modified)
    result = post.run([], tmp_path, reference)
    assert result["status"] == "needs_review" and result["scope_complete"] is False
    assert len(ctx.calls) == (12 if change == "replayed_capture" else 24)
    if change == "version":
        report = json.loads((tmp_path / post.OUTPUT).read_text("utf-8"))
        assert report["source_fingerprints_unchanged"] is True and report["source_versions_unchanged"] is False
