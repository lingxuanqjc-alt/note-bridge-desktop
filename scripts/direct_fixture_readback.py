"""Recompute an interrupted direct seed's independent, read-only completion proof."""
import hashlib
import importlib.util
import json
import re
from datetime import datetime
from types import SimpleNamespace

from fetch_scope import snapshot_binding, task_summary

from note_bridge.errors import BridgeError
from note_bridge.models import NoteDocument, TaskReport
from note_bridge.operations import receipt_key
from note_bridge.paths import confined
from note_bridge.providers.huawei_groups import folder_key

MANIFEST = "huawei-complex-BD2-job.json"
FIXTURE_ID = "fixture-20260907-huawei-complex-BD2"
XIAOMI_MANIFEST = "xiaomi-image-AC6-job.json"
XIAOMI_FIXTURE_ID = "fixture-20260907-xiaomi-image-AC6"


def fail():
    raise BridgeError("invalid_fixture_source", "直接种子的独立回读证明未通过身份、完整快照或内容校验。")


def recompute_readback(root, store, fixture_builder, manifest, fetch_scope_file, *, require_current_scope=False):
    if manifest != MANIFEST or not re.fullmatch(r"\.private/evidence/fetch-[0-9a-f]{32}-scope\.json", str(fetch_scope_file)):
        fail()
    paths = {"prepared": ".private/checkpoints/huawei-complex-BD2-prepared.json",
             "manifest": ".private/checkpoints/" + MANIFEST,
             "original_evidence": ".private/evidence/" + FIXTURE_ID + ".json",
             "baseline": ".private/checkpoints/" + FIXTURE_ID + "-before.json",
             "fetch_scope": fetch_scope_file}
    try:
        raw = {key: (root / path).read_bytes() for key, path in paths.items()}
        files = {key: json.loads(value) for key, value in raw.items()}
        job, evidence, baseline, scope = (files[key] for key in ("manifest", "original_evidence", "baseline", "fetch_scope"))
        if (job != files["prepared"] or job.get("id") != FIXTURE_ID or job.get("target") != "huawei"
                or job.get("kind") != "independent-cloud-write-smoke" or job.get("content_case") != "complex"
                or job.get("armed") is not False or job.get("from_cloud_fixture") is not None
                or job.get("with_images") is not True or job.get("with_group") is not True
                or not re.fullmatch(r"[0-9a-f]{64}", job.get("expected_account", ""))):
            fail()
        if (evidence.get("kind") != job["kind"] or evidence.get("fixture_id") != FIXTURE_ID
                or evidence.get("platform") != "huawei" or evidence.get("formal_acceptance") is not False
                or evidence.get("status") != "readback_pending" or evidence.get("receipt_confirmed") is not True
                or type(evidence.get("acknowledged_writes")) is not int or evidence["acknowledged_writes"] != 1
                or evidence.get("issue_codes") != ["format_downgrade"]
                or evidence.get("cloud_resource_receipt_states") != ["linked"] * 4):
            fail()
        before = baseline["target"]
        if (baseline.get("full_fingerprints") is not True or type(baseline.get("count")) is not int
                or not isinstance(before, dict) or not before or baseline["count"] != len(before)
                or any(not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for key, value in before.items())):
            fail()
        account = job["expected_account"]
        seed = fixture_builder(job)
        destination = SimpleNamespace(spec=SimpleNamespace(id="huawei"), account_id=account)
        key = receipt_key(seed, destination)
        receipt = store.receipt(key)
        resources = store.resource_receipts(key)
        if (not receipt or receipt["status"] != "confirmed" or len(receipt["remote_ids"]) != 1
                or len(resources) != 4 or any(r["status"] != "linked" for r in resources)):
            fail()
        remote_id = receipt["remote_ids"][0]
        if not remote_id or remote_id in before:
            fail()
        task_id = scope["task"]["id"]
        if (fetch_scope_file != f".private/evidence/fetch-{task_id}-scope.json"
                or not re.fullmatch(r"[0-9a-f]{32}", task_id)
                or scope.get("kind") != "completed-fetch-scope" or scope.get("platform") != "huawei"
                or scope.get("account") != account or scope.get("formal_acceptance") is not False
                or type(scope.get("cloud_writes")) is not int or scope["cloud_writes"] != 0):
            fail()
        fetch_task = store.task(task_id)
        if (fetch_task is None or scope["task"] != task_summary(fetch_task) or fetch_task.operation != "fetch"
                or fetch_task.status not in ("succeeded", "partial") or not fetch_task.finished_at
                or {i.code for i in fetch_task.issues} - {"source_warning"}
                or not fetch_task.completed == fetch_task.succeeded == fetch_task.total == len(before) + 1):
            fail()
        scoped_notes = scope["notes"]
        if (not isinstance(scoped_notes, dict) or set(scoped_notes) - set(before) != {remote_id}
                or not set(before).issubset(scoped_notes) or any(scoped_notes[n] != fp for n, fp in before.items())):
            fail()
        current = snapshot_binding(root, store, "huawei", account)
        if require_current_scope and any(scope[name] != current[name] for name in current):
            fail()
        # Later confirmed additions may extend the cache; the original completed
        # fetch still has exactly this one new note and all its notes must be unchanged.
        if any(current["notes"].get(n) != fp for n, fp in scoped_notes.items()):
            fail()
        expected_references = {n: current["attachment_references"][n] for n in scoped_notes}
        paths_in_scope = {ref["path"] for values in expected_references.values() for ref in values}
        if (scope["attachment_references"] != expected_references
                or scope["resources"] != {p: current["resources"][p] for p in paths_in_scope}):
            fail()
        with store.connection() as db:
            reports = [TaskReport.model_validate_json(row[0]) for row in db.execute(
                "SELECT report FROM tasks WHERE json_extract(report,'$.operation')='migrate' "
                "AND EXISTS (SELECT 1 FROM json_each(report,'$.issues') AS issue "
                "WHERE json_extract(issue.value,'$.note_id')=?)", (seed.source_id,)).fetchall()]
        if len(reports) != 1:
            fail()
        written = reports[0]
        if (written.status != "partial" or not written.finished_at or written.skipped != 0
                or not written.completed == written.succeeded == written.total == 1 or not written.issues
                or any(i.code != "format_downgrade" or i.note_id != seed.source_id for i in written.issues)):
            fail()
        completed = datetime.fromisoformat(written.finished_at)
        started = datetime.fromisoformat(fetch_task.started_at)
        if completed.tzinfo is None or started.tzinfo is None or started <= completed:
            fail()
        warnings = [i.message for i in written.issues]
        actual = next(n for n in store.notes("huawei", account) if n.source_id == remote_id)
        group = store.receipt(folder_key(seed, account))
        if (len(seed.attachments) != 2 or len(actual.attachments) != 2 or actual.warnings
                or any(a.kind != "image" for a in [*seed.attachments, *actual.attachments])
                or not seed.source_folder_id or not actual.source_folder_id
                or actual.source_folder_name != seed.source_folder_name
                or group != {"status": "confirmed", "remote_ids": [actual.source_folder_id]}):
            fail()
        spec = importlib.util.spec_from_file_location("direct_seed_comparison", root / "scripts/live-fixture-job.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not module.compare_complex_readback(seed, actual, warnings):
            fail()
        return {"kind": "direct-fixture-independent-readback", "fixture_id": FIXTURE_ID,
                "manifest": MANIFEST, "formal_acceptance": False, "cloud_writes": 0,
                "file_sha256": {name: hashlib.sha256(value).hexdigest() for name, value in raw.items()},
                "fetch_scope_file": fetch_scope_file, "fetch_task": task_summary(fetch_task),
                "migration_task": task_summary(written), "receipt_key": key, "resources": resources,
                "seed_fingerprint": seed.fingerprint(), "target": {"platform": "huawei", "account": account,
                    "source_id": remote_id, "fingerprint": actual.fingerprint()},
                "before_count": len(before), "after_count": len(scoped_notes),
                "content_verified": True, "group_verified": True, "attachment_originals_verified": True,
                "original_notes_unchanged": True, "only_confirmed_addition": True}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, StopIteration):
        fail()


def recompute_xiaomi_readback(root, store, fixture_builder):
    """Bind AC6's existing repair to its original receipt and exact current cache.

    AG hashes are raw server-entry hashes, not NoteDocument fingerprints. The
    recorded expected content hash is independently rebuilt, then the complete
    parsed blocks and original image bytes are checked. No other note is read.
    """
    from note_bridge.providers.xiaomi import encode_content
    from note_bridge.providers.xiaomi_groups import folder_key as xiaomi_folder_key
    from note_bridge.providers.xiaomi_markup import parse_legacy

    paths = {
        "manifest": ".private/checkpoints/" + XIAOMI_MANIFEST,
        "prepared": ".private/checkpoints/xiaomi-image-AC6-prepared.json",
        "original_evidence": ".private/evidence/" + XIAOMI_FIXTURE_ID + ".json",
        "original_readback": ".private/evidence/xiaomi-AC6-reconciliation.json",
        "plan": ".private/checkpoints/xiaomi-AF-readonly-plan.json",
        "af_attempt": ".private/evidence/xiaomi-AF-AC6-content-repair.json",
        "af_readback": ".private/evidence/xiaomi-AF-AC6-readonly-reconciliation.json",
        "ag_repair": ".private/evidence/xiaomi-AG-AC6-content-repair.json",
        "native": ".private/evidence/xiaomi-AG-native-verification.json",
        "images": ".private/evidence/xiaomi-image-browser-AC6.json",
    }

    def require(condition):
        if not condition:
            fail()

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    try:
        raw = {name: (root / path).read_bytes() for name, path in paths.items()}
        files = {name: json.loads(value) for name, value in raw.items()}
        job, initial, first, plan, af, af_read, ag, native, images = (
            files[name] for name in ("manifest", "original_evidence", "original_readback", "plan",
                                    "af_attempt", "af_readback", "ag_repair", "native", "images"))
        require({k: v for k, v in job.items() if k != "consumed_at"} == files["prepared"]
            and isinstance(job.get("consumed_at"), str)
            and job.get("kind") == "independent-cloud-write-smoke" and job.get("id") == XIAOMI_FIXTURE_ID
            and job.get("target") == "xiaomi" and job.get("armed") is False
            and job.get("from_cloud_fixture") is None and job.get("content_case") is None
            and job.get("with_images") is True and job.get("with_group") is True
            and re.fullmatch(r"[0-9a-f]{64}", str(job.get("expected_account", ""))))
        require(initial.get("kind") == job["kind"] and initial.get("fixture_id") == job["id"]
            and initial.get("platform") == "xiaomi" and initial.get("status") == "needs_review"
            and initial.get("formal_acceptance") is False and initial.get("acknowledged_writes") == 1
            and initial.get("issue_codes") == ["format_downgrade"]
            and initial.get("cloud_resource_receipt_states") == ["linked"] * 5
            and all(initial.get(k) is True for k in ("unencrypted_account_scope_verified", "receipt_confirmed",
                "group_mapping_verified", "image_bytes_verified", "original_notes_unchanged"))
            and initial.get("expected_images") == initial.get("restored_images") == 2
            and initial.get("before_count") == 4 and initial.get("after_count") == 5)
        require(first.get("kind") == "xiaomi-AC6-readonly-reconciliation"
            and first.get("status") == "verified_with_degradation" and first.get("formal_acceptance") is False
            and first.get("cloud_writes") == 0 and first.get("images_verified") == 2
            and first.get("original_notes_unchanged") == 4 and first.get("target_notes") == 5
            and all(first.get(k) is True for k in ("content_and_style_verified", "group_verified",
                "only_confirmed_additions", "original_evidence_preserved")))
        require(plan.get("kind") == "xiaomi-AF-scoped-readonly-inspection" and plan.get("armed") is False
            and plan.get("account") == job["expected_account"])
        items = [item for item in plan["items"] if item.get("index") == 3]
        require(len(items) == 1)
        item = items[0]
        identity = item["target_id"]
        require(isinstance(identity, str) and re.fullmatch(r"[0-9]+", identity)
            and len(plan["target_raw_hashes"]) == 8
            and plan["target_raw_hashes"].get(identity) == item["raw_sha256"]
            and re.fullmatch(r"[0-9a-f]{64}", str(item["raw_sha256"])))
        for label, outcome, status in (("AF", af, "write_uncertain"), ("AG", ag, "content_repaired_readback_verified")):
            require(outcome.get("kind") == f"xiaomi-{label}-AC6-content-repair"
                and outcome.get("status") == status and outcome.get("formal_acceptance") is False
                and outcome.get("creates") == outcome.get("uploads") == 0 and outcome.get("update_requests") == 1
                and outcome.get("target_id") == identity and outcome.get("before_sha256") == item["raw_sha256"])
        require(af_read.get("kind") == "xiaomi-AF-AC6-readonly-reconciliation"
            and af_read.get("status") == "old_entry_unchanged" and af_read.get("formal_acceptance") is False
            and af_read.get("cloud_writes") == 0 and af_read.get("new_content_present") is False
            and af_read.get("other_notes_unchanged") is True and af_read.get("old_entry_unchanged") is True)
        require(ag.get("response_code") == 0 and ag.get("response_has_data_object") is True
            and ag.get("other_notes_unchanged") == 7 and ag.get("image_references_unchanged") is True
            and ag.get("source_time_and_group_unchanged") is True
            and re.fullmatch(r"[0-9a-f]{64}", str(ag.get("after_sha256", "")))
            and ag["after_sha256"] != ag["before_sha256"])
        require(native.get("kind") == "xiaomi-AG-AC6-native-verification" and native.get("status") == "verified"
            and native.get("formal_acceptance") is False and native.get("cloud_writes") == 0
            and native.get("images_decoded_visible") == 2 and all(native.get(k) is True for k in
                ("bold", "italic", "underline", "checked_and_unchecked_tasks", "ordered_list_structure")))
        require(images.get("kind") == "xiaomi-official-image-rendering" and images.get("fixture") == "AC6"
            and images.get("status") == "verified" and images.get("formal_acceptance") is False
            and images.get("cloud_writes") == 0 and images.get("fixture_title_in_editor") is True
            and len(images["decoded_images"]) == 2 and all(all(image.get(k) is True for k in
                ("complete", "visible", "insideEditor")) and image.get("width") == 640
                and image.get("height") == 240 for image in images["decoded_images"]))
        # The ordinary fixture builder cannot create files on this read-only route.
        resources_root = root / ".private/session-lab/resources"
        require(all((resources_root / f"fixtures/vivo-upload-synthetic-{i}.{ext}").is_file()
                    for i, ext in enumerate(("png", "jpg"))))
        seed = fixture_builder(job)
        require(seed.account_id == "synthetic-fixture-source" and seed.source_id == XIAOMI_FIXTURE_ID
            and seed.fingerprint() == item["source_fingerprint"] and len(seed.attachments) == 2
            and set(item["images"]) == {a.id for a in seed.attachments}
            and len(set(item["images"].values())) == 2)
        key = receipt_key(seed, SimpleNamespace(spec=SimpleNamespace(id="xiaomi"), account_id=job["expected_account"]))
        receipt = store.receipt(key)
        with store.connection() as db:
            resources = [[row[0], row[1]] for row in db.execute(
                "SELECT resource_id,status FROM resource_receipts WHERE receipt_key=? ORDER BY resource_id", (key,))]
            row = db.execute("SELECT document FROM notes WHERE platform=? AND account=? AND source_id=?",
                             ("xiaomi", job["expected_account"], identity)).fetchone()
        require(receipt == {"status": "confirmed", "remote_ids": [identity]} and len(resources) == 5
            and all(state == "linked" for _, state in resources) and row is not None
            and store.snapshot("xiaomi", job["expected_account"]).get("complete") is True)
        actual = NoteDocument.model_validate_json(row[0])
        content, warnings = encode_content(seed, {a: {"fileId": b} for a, b in item["images"].items()})
        blocks, parse_warnings, _ = parse_legacy(content)
        require(ag.get("expected_content_sha256") == af.get("expected_content_sha256") == digest(content)
            and ag.get("warnings") == af.get("warnings") == warnings == []
            and (actual.platform, actual.account_id, actual.source_id, actual.display_title) ==
                ("xiaomi", job["expected_account"], identity, seed.display_title)
            and actual.blocks == blocks and actual.warnings == parse_warnings
            and actual.source_folder_name == seed.source_folder_name
            and store.receipt(xiaomi_folder_key(seed, job["expected_account"])) ==
                {"status": "confirmed", "remote_ids": [actual.source_folder_id]})
        require(len(actual.attachments) == 2 and {a.id for a in actual.attachments} == set(item["images"].values()))
        for source in seed.attachments:
            restored = next(a for a in actual.attachments if a.id == item["images"][source.id])
            require(source.kind == restored.kind == "image" and source.mime == restored.mime
                and (source.size, source.sha256) == (restored.size, restored.sha256))
            for asset in (source, restored):
                data = confined(resources_root, asset.local_path or "").read_bytes()
                require(len(data) == asset.size and hashlib.sha256(data).hexdigest() == asset.sha256)
        return {"kind": "xiaomi-AC6-independent-source-readback", "manifest": XIAOMI_MANIFEST,
            "fixture_id": XIAOMI_FIXTURE_ID, "formal_acceptance": False, "cloud_writes": 0,
            "scope": "exact_cached_AC6_and_bound_historical_repair", "raw_response_replayed": False,
            "file_sha256": {name: hashlib.sha256(value).hexdigest() for name, value in raw.items()},
            "receipt_key": key, "resources": resources, "seed_fingerprint": seed.fingerprint(),
            "target": {"platform": "xiaomi", "account": actual.account_id, "source_id": identity,
                       "fingerprint": actual.fingerprint()},
            "content_verified": True, "group_verified": True, "attachment_originals_verified": True}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, StopIteration):
        fail()


def verify_readback(manifest, store, root, fixture_builder):
    if manifest == XIAOMI_MANIFEST:
        try:
            proof = json.loads((root / ".private/evidence" / (XIAOMI_FIXTURE_ID + "-readback.json")).read_text("utf-8"))
            if proof != recompute_xiaomi_readback(root, store, fixture_builder):
                fail()
            return
        except (OSError, ValueError, TypeError, KeyError):
            fail()
    if manifest != MANIFEST:
        fail()
    try:
        proof = json.loads((root / ".private/evidence" / (FIXTURE_ID + "-readback.json")).read_text("utf-8"))
        expected = recompute_readback(root, store, fixture_builder, manifest, proof["fetch_scope_file"])
        if proof != expected:
            fail()
    except (OSError, ValueError, TypeError, KeyError):
        fail()
