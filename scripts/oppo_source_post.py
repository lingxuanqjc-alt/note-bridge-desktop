"""One final F2/OR1 read shared by the six fixed OPPO outbound directions."""
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace

from huawei_bi4_readback import verify_target as verify_bi4_target
from oppo_scoped_source import MANIFESTS, capture, digest, require, verify_source
from xiaomi_browser_scope import _ReadOnlyStore

from note_bridge.errors import BridgeError
from note_bridge.operations import receipt_key

BATCHES = (("wps", "BI2"), ("huawei", "BI4"), ("xiaomi", "BI6"),
           ("honor", "BI8"), ("meizu", "BI13"), ("vivo", "BI12"))
OPERATION = "oppo_BI2_BI12_source_post"
OUTPUT = ".private/evidence/oppo-BI2-BI12-source-post.json"


def aborted_meizu(root, replacement, reference):
    """BI10 failed before the mandatory baseline/migrate step; BI13 is a new batch."""
    identifier = "matrix-20260907-oppo-meizu-BI10"
    paths = {"manifest": ".private/checkpoints/oppo-meizu-BI10-job.json",
        "consumed": f".private/checkpoints/{identifier}-consumed-job.json",
        "evidence": f".private/evidence/{identifier}.json"}
    raw = {key: (root / path).read_bytes() for key, path in paths.items()}
    documents = {key: json.loads(value) for key, value in raw.items()}
    require(documents["manifest"] == documents["consumed"] == {**replacement, "id": identifier}
        and not (root / f".private/checkpoints/{identifier}-before.json").exists()
        and documents["evidence"] == {
            "kind": "cloud-matrix-batch", "batch_id": identifier, "formal_acceptance": False,
            "source": "oppo", "target": "meizu", "status": "needs_review",
            "source_post_readback": "pending", "official_rendering": "pending", "items": [],
            "source_scope": "fixed_oppo_capture", "source_capture": reference,
            "whole_source_account_snapshot": False, "oppo_scope": "OR1_fixed_direct_12", "code": "session_expired"})
    return {"batch_id": identifier, "status": "failed_before_target_baseline_and_migration",
        "replacement_batch_id": replacement["id"],
        "files": {key: {"path": paths[key], "sha256": digest(value)} for key, value in raw.items()}}


def audit(root, store, bi4_reference):
    """All six confirmed outputs must refer to the same original two-note capture."""
    reference, notes, records = None, None, []
    for target, label in BATCHES:
        identifier = f"matrix-20260907-oppo-{target}-{label}"
        name = f"oppo-{target}-{label}-job.json"
        paths = {"manifest": ".private/checkpoints/" + name,
            "consumed": ".private/checkpoints/" + identifier + "-consumed-job.json",
            "evidence": ".private/evidence/" + identifier + ".json",
            "baseline": ".private/checkpoints/" + identifier + "-before.json"}
        raw = {key: (root / path).read_bytes() for key, path in paths.items()}
        files = {key: json.loads(value) for key, value in raw.items()}
        job, evidence, baseline = files["manifest"], files["evidence"], files["baseline"]
        require(job == files["consumed"] and job.get("kind") == "cloud-matrix-batch"
            and job.get("id") == identifier and job.get("target") == target and job.get("armed") is False
            and job.get("source_policy") == "direct_seed_only" and job.get("oppo_scope") == "OR1_fixed_direct_12"
            and re.fullmatch(r"[0-9a-f]{64}", str(job.get("expected_account", "")))
            and isinstance(job.get("sources"), list) and len(job["sources"]) == 2)
        origins = [entry.get("from_cloud_fixture", {}) for entry in job["sources"]]
        require(tuple(origin.get("manifest") for origin in origins) == MANIFESTS
            and all(origin.get("platform") == "oppo" and "batch_manifest" not in origin for origin in origins)
            and origins[0].get("scoped_proof") == origins[1].get("scoped_proof"))
        if reference is None:
            reference = origins[0].get("scoped_proof")
            notes = verify_source(reference, store, root)
        require(origins[0].get("scoped_proof") == reference)
        abandoned = aborted_meizu(root, job, reference) if target == "meizu" else None
        fingerprints = {note.source_id: note.fingerprint() for note in notes}
        require(evidence.get("kind") == job["kind"] and evidence.get("batch_id") == identifier
            and evidence.get("source") == "oppo" and evidence.get("target") == target
            and evidence.get("formal_acceptance") is False and evidence.get("source_scope") == "fixed_oppo_capture"
            and evidence.get("source_capture") == reference and evidence.get("whole_source_account_snapshot") is False
            and evidence.get("oppo_scope") == job["oppo_scope"] and evidence.get("baseline_file") == paths["baseline"]
            and evidence.get("succeeded") == 2 and evidence.get("skipped") == 0
            and baseline.get("source") == fingerprints and baseline.get("source_capture") == reference
            and baseline.get("source_scope") == "fixed_oppo_capture"
            and baseline.get("whole_source_account_snapshot") is False)
        recovery = None
        if target == "huawei":
            # The original full read failed. Its separate exact-two proof does not
            # retrospectively verify the original 22 bodies or replace that report.
            recovery = verify_bi4_target(bi4_reference, store, root)
            require(evidence.get("status") == "needs_review" and evidence.get("items") == []
                and len(recovery) == 2)
        else:
            require(evidence.get("status") == "api_verified" and isinstance(evidence.get("items"), list)
                and len(evidence["items"]) == 2)
            fields = (("synthetic_scope_originals_unchanged", "synthetic_scope_only_confirmed_additions")
                      if target == "vivo" else ("original_target_notes_unchanged", "only_confirmed_additions"))
            require(all(evidence.get(field) is True for field in fields))
        if target == "vivo":
            require(evidence.get("whole_account_snapshot") is False
                and evidence.get("comparison_scope") == "confirmed_tool_created_ids_only"
                and baseline.get("target_scope") == "confirmed_tool_created_ids_only")
        destination = SimpleNamespace(spec=SimpleNamespace(id=target), account_id=job["expected_account"])
        receipts = []
        for index, (entry, origin, note) in enumerate(zip(job["sources"], origins, notes, strict=True)):
            require((origin.get("account"), origin.get("source_id"), origin.get("fingerprint"), entry.get("title")) ==
                (note.account_id, note.source_id, note.fingerprint(), note.display_title)
                and entry.get("with_images") is True and entry.get("with_group") is bool(note.source_folder_name))
            if recovery is None:
                item = evidence["items"][index]
                require(item.get("source_id") == note.source_id and item.get("source_fingerprint") == note.fingerprint()
                    and item.get("status") == "api_verified" and item.get("receipt_status") == "confirmed"
                    and item.get("content_verified") is True and item.get("group_verified") is True)
            key = receipt_key(note, destination)
            receipt = store.receipt(key)
            with store.connection() as db:
                resources = [list(row) for row in db.execute(
                    "SELECT resource_id,status FROM resource_receipts WHERE receipt_key=? ORDER BY resource_id", (key,))]
            require(receipt and receipt["status"] == "confirmed" and len(receipt["remote_ids"]) == 1
                and isinstance(receipt["remote_ids"][0], str) and receipt["remote_ids"][0]
                and len(resources) >= 2 and all(row[1] == "linked" for row in resources))
            if recovery is not None:
                actual = recovery[index]
                require((actual.platform, actual.account_id, actual.source_id) ==
                    (target, job["expected_account"], receipt["remote_ids"][0]))
            receipts.append({"key": key, "receipt": receipt, "resources": resources})
        require(len({row["receipt"]["remote_ids"][0] for row in receipts}) == 2)
        record = {"batch_id": identifier, "manifest": name, "file_sha256": {key: digest(value) for key, value in raw.items()},
                  "receipts": receipts}
        if abandoned is not None:
            record["abandoned_attempt"] = abandoned
        if recovery is not None:
            record.update(target_readback=bi4_reference, target_note_count=2, target_image_count=4,
                original_evidence_status="needs_review", original_22_body_integrity="not_rechecked",
                original_22_ids_present=True, original_22_metadata_stable_during_readback=True,
                prewrite_listing_etags_available=False)
        records.append(record)
    return {"source_capture": reference, "batches": records,
            "source_fingerprints": {note.source_id: note.fingerprint() for note in notes}}


def read_audit(root, bi4_reference):
    with closing(sqlite3.connect((root / ".private/session-lab/notes.sqlite").resolve().as_uri() + "?mode=ro",
                                 uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        return audit(root, _ReadOnlyStore(db), bi4_reference)


def run(jars, root, bi4_reference):
    before = read_audit(root, bi4_reference)  # Pending or uncertain directions cause no cloud request.
    output_path = root / OUTPUT
    with output_path.open("x", encoding="utf-8") as output:
        report = {"kind": OPERATION, "status": "started", "formal_acceptance": False,
            "whole_account_snapshot": False, "scope_complete": False, "covered_batches": 6,
            "source_notes": 2, "source_images": 4, "cloud_writes": 0, "cache_writes": 0, "receipt_writes": 0,
            "batch_ids": [batch["batch_id"] for batch in before["batches"]],
            "huawei_target_original_22_body_integrity": "not_rechecked",
            "started_at": datetime.now(timezone.utc).isoformat(), "before": before}
        json.dump(report, output, ensure_ascii=False)
        output.flush()
        try:
            fresh = capture(jars, root)
            report["fresh_capture"] = fresh
            require(fresh.get("status") == "captured" and fresh.get("scope_complete") is True)
            require(read_audit(root, bi4_reference) == before, "source_post_binding_changed")
            with closing(sqlite3.connect((root / ".private/session-lab/notes.sqlite").resolve().as_uri() + "?mode=ro",
                                         uri=True, timeout=15)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                actual = verify_source(fresh["proof"], _ReadOnlyStore(db), root)
            report["source_fingerprints_unchanged"] = {n.source_id: n.fingerprint() for n in actual} == before["source_fingerprints"]
            old_proof = json.loads((root / before["source_capture"]["path"]).read_text("utf-8"))
            new_proof = json.loads((root / fresh["proof"]["path"]).read_text("utf-8"))
            require(fresh["proof"]["path"] != before["source_capture"]["path"]
                and datetime.fromisoformat(new_proof["captured_started_at"]) >= datetime.fromisoformat(report["started_at"]),
                "source_post_not_fresh")
            report["source_versions_unchanged"] = old_proof["versions"] == new_proof["versions"]
            require(report["source_fingerprints_unchanged"] and report["source_versions_unchanged"], "source_post_changed")
            report.update(status="verified", scope_complete=True)
        except BridgeError as error:
            report.update(status="needs_review", code=error.code)
        except Exception as error:
            report.update(status="failed", code="source_post_error", error_type=type(error).__name__)
        finally:
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            output.seek(0)
            output.truncate()
            json.dump(report, output, ensure_ascii=False, indent=2)
    return {key: report[key] for key in ("kind", "status", "scope_complete", "covered_batches", "source_notes",
        "source_images", "batch_ids", "whole_account_snapshot", "huawei_target_original_22_body_integrity",
        "cloud_writes", "cache_writes", "receipt_writes")} | {"evidence": OUTPUT}
