"""Build unarmed fixed-source data and validate it without writing files or cloud data."""
import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from huawei_scoped_source import MANIFESTS, fixture
from xiaomi_browser_scope import _module

from note_bridge.operations import receipt_key

TARGETS = {"wps", "vivo", "xiaomi", "honor", "meizu"}


def _validate_destination(target, account, identifier):
    if (target not in TARGETS or not re.fullmatch(r"[0-9a-f]{64}", str(account))
            or not re.fullmatch(r"matrix-\d{8}-[A-Za-z0-9-]{1,30}", str(identifier))):
        raise ValueError("Invalid fixed Huawei batch destination or identity")


def _preparation(notes, reference):
    fingerprints = {note.source_id: note.fingerprint() for note in notes}
    digest = hashlib.sha256(json.dumps(fingerprints, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"source_scope": "fixed_huawei_K2_BD2_capture", "whole_account_snapshot": False,
            "source_capture": deepcopy(reference), "original_17_integrity": "pending",
            "source_snapshot_count": len(notes), "source_snapshot_sha256": digest,
            "cloud_requests": 0}


def build_prepared_job(notes, scoped_proof, *, target, expected_account, batch_id):
    """Pure assembly only. Call validate_prepared before using the returned unarmed data."""
    _validate_destination(target, expected_account, batch_id)
    if (len(notes) != len(MANIFESTS) or any(note.platform != "huawei" for note in notes)
            or len({note.account_id for note in notes}) != 1):
        raise ValueError("Only the two fixed Huawei sources from one account are allowed")
    entries = [{"title": note.display_title, "with_images": True, "with_group": True,
                "from_cloud_fixture": {"platform": "huawei", "manifest": manifest,
                    "account": note.account_id, "source_id": note.source_id,
                    "fingerprint": note.fingerprint(), "scoped_proof": deepcopy(scoped_proof)}}
               for manifest, note in zip(MANIFESTS, notes, strict=True)]
    return {"kind": "cloud-matrix-batch", "source_policy": "direct_seed_only", "id": batch_id,
            "target": target, "expected_account": expected_account, "sources": entries, "armed": False,
            "preparation": _preparation(notes, scoped_proof)}


def validate_prepared(job, store, root):
    """Read proofs and receipts only; never fall back to the whole-account source cache."""
    entries = job.get("sources")
    if (job.get("kind") != "cloud-matrix-batch" or job.get("source_policy") != "direct_seed_only"
            or job.get("armed") is not False or not isinstance(entries, list) or len(entries) != 2
            or any(not isinstance(entry, dict) for entry in entries)):
        raise ValueError("Invalid prepared fixed-source batch")
    _validate_destination(job.get("target"), job.get("expected_account"), job.get("id"))
    origins = [entry.get("from_cloud_fixture") for entry in entries]
    if (any(not isinstance(origin, dict) for origin in origins)
            or tuple(origin.get("manifest") for origin in origins) != MANIFESTS
            or any(origin.get("platform") != "huawei" or "batch_manifest" in origin for origin in origins)
            or origins[0].get("scoped_proof") is None
            or origins[0]["scoped_proof"] != origins[1].get("scoped_proof")):
        raise ValueError("Only the fixed Huawei pair may share one capture")
    matrix = _module(Path(__file__).resolve().parents[1], "live-matrix-job")
    notes = matrix.select_batch(job, store, root, lambda item: fixture(root, item))
    if job.get("preparation") != _preparation(notes, origins[0]["scoped_proof"]):
        raise ValueError("Scoped Huawei preparation must retain its limited source meaning")
    destination = SimpleNamespace(spec=SimpleNamespace(id=job["target"]), account_id=job["expected_account"])
    for note in notes:
        key = receipt_key(note, destination)
        if store.receipt(key) or store.resource_receipts(key):
            raise ValueError("Existing target receipt/resources require review; changing a batch label cannot retry them")
    return notes
