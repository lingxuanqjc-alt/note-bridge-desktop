"""One exact two-note source-post read shared by the five fixed Huawei directions."""
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace

from huawei_scoped_source import MANIFESTS, capture, digest, require, verify_source
from xiaomi_browser_scope import _ReadOnlyStore

from note_bridge.errors import BridgeError
from note_bridge.operations import receipt_key

BATCHES = (("xiaomi", "BD13"), ("wps", "BD14"), ("honor", "BD15"), ("meizu", "BD16"), ("vivo", "BD17"))
OPERATION = "huawei_BD13_BD17_source_post"
OUTPUT = ".private/evidence/huawei-BD13-BD17-source-post.json"


def audit(root, store):
    """No cloud or mutations: all five consumed, confirmed batches must bind one source capture."""
    reference, notes, records = None, None, []
    for target, label in BATCHES:
        identifier = f"matrix-20260907-huawei-{target}-{label}"
        manifest = f"huawei-{target}-{label}-job.json"
        raw = (root / ".private/checkpoints" / manifest).read_bytes()
        evidence_raw = (root / ".private/evidence" / (identifier + ".json")).read_bytes()
        job, evidence = json.loads(raw), json.loads(evidence_raw)
        require(job.get("kind") == "cloud-matrix-batch" and job.get("id") == identifier
            and job.get("target") == target and job.get("armed") is False
            and job.get("source_policy") == "direct_seed_only"
            and re.fullmatch(r"[0-9a-f]{64}", str(job.get("expected_account", "")))
            and isinstance(job.get("sources"), list) and len(job["sources"]) == 2)
        origins = [entry.get("from_cloud_fixture", {}) for entry in job["sources"]]
        require(tuple(o.get("manifest") for o in origins) == MANIFESTS
            and all(o.get("platform") == "huawei" and "batch_manifest" not in o for o in origins)
            and origins[0].get("scoped_proof") == origins[1].get("scoped_proof"))
        if reference is None:
            reference = origins[0].get("scoped_proof")
            notes = verify_source(reference, store, root)
        require(origins[0].get("scoped_proof") == reference)
        require(evidence.get("kind") == job["kind"] and evidence.get("batch_id") == identifier
            and evidence.get("source") == "huawei" and evidence.get("target") == target
            and evidence.get("status") == "api_verified" and evidence.get("formal_acceptance") is False
            and evidence.get("source_scope") == "fixed_huawei_K2_BD2_capture"
            and evidence.get("source_capture") == reference and evidence.get("whole_source_account_snapshot") is False
            and evidence.get("source_original_17_integrity") == "pending"
            and isinstance(evidence.get("items"), list) and len(evidence["items"]) == 2)
        keys = (("synthetic_scope_originals_unchanged", "synthetic_scope_only_confirmed_additions") if target == "vivo"
                else ("original_target_notes_unchanged", "only_confirmed_additions"))
        require(all(evidence.get(k) is True for k in keys))
        if target == "vivo":
            require(evidence.get("whole_account_snapshot") is False
                and evidence.get("comparison_scope") == "confirmed_tool_created_ids_only")
        destination = SimpleNamespace(spec=SimpleNamespace(id=target), account_id=job["expected_account"])
        receipts = []
        for entry, origin, note, item in zip(job["sources"], origins, notes, evidence["items"], strict=True):
            require((origin.get("account"), origin.get("source_id"), origin.get("fingerprint"), entry.get("title")) ==
                (note.account_id, note.source_id, note.fingerprint(), note.display_title)
                and entry.get("with_images") is True and entry.get("with_group") is True
                and item.get("source_id") == note.source_id and item.get("source_fingerprint") == note.fingerprint()
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
            receipts.append({"key": key, "receipt": receipt, "resources": resources})
        require(len({r["receipt"]["remote_ids"][0] for r in receipts}) == 2)
        records.append({"manifest": manifest, "manifest_sha256": digest(raw), "evidence_sha256": digest(evidence_raw),
                        "receipts": receipts})
    return {"source_capture": reference, "batches": records,
            "source_fingerprints": {n.source_id: n.fingerprint() for n in notes}}


def read_audit(root):
    with closing(sqlite3.connect((root / ".private/session-lab/notes.sqlite").resolve().as_uri() + "?mode=ro",
                                 uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        return audit(root, _ReadOnlyStore(db))


def run(jars, root):
    before = read_audit(root)  # Missing or still-running directions make zero cloud requests.
    output_path = root / OUTPUT
    with output_path.open("x", encoding="utf-8") as output:
        report = {"kind": OPERATION, "status": "started", "formal_acceptance": False,
            "whole_account_snapshot": False, "original_17_integrity": "pending", "cloud_writes": 0,
            "cache_writes": 0, "receipt_writes": 0, "scope_complete": False, "covered_batches": 5,
            "source_notes": 2, "source_images": 4, "started_at": datetime.now(timezone.utc).isoformat(), "before": before}
        json.dump(report, output, ensure_ascii=False)
        output.flush()
        try:
            fresh = capture(jars, root)
            report["fresh_capture"] = fresh
            require(fresh.get("status") == "captured" and fresh.get("scope_complete") is True)
            after = read_audit(root)
            require(after == before, "source_post_binding_changed")
            with closing(sqlite3.connect((root / ".private/session-lab/notes.sqlite").resolve().as_uri() + "?mode=ro",
                                         uri=True, timeout=15)) as db:
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                actual = verify_source(fresh["proof"], _ReadOnlyStore(db), root)
            report["source_fingerprints_unchanged"] = {n.source_id: n.fingerprint() for n in actual} == before["source_fingerprints"]
            old_proof = json.loads((root / before["source_capture"]["path"]).read_text("utf-8"))
            new_proof = json.loads((root / fresh["proof"]["path"]).read_text("utf-8"))
            report["source_etags_unchanged"] = old_proof["etags"] == new_proof["etags"]
            require(report["source_fingerprints_unchanged"] and report["source_etags_unchanged"], "source_post_changed")
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
    return {k: report[k] for k in ("kind", "status", "scope_complete", "covered_batches", "source_notes", "source_images",
        "whole_account_snapshot", "original_17_integrity", "cloud_writes", "cache_writes", "receipt_writes")} | {"evidence": OUTPUT}
