"""One-use completion of the identified H2 test draft; never creates or uploads anything."""

import importlib.util
import json
import time
import uuid

from lab_store import LabStore as Store

from note_bridge.errors import BridgeError
from note_bridge.operations import fetch_snapshot, receipt_key
from note_bridge.providers.factory import create_provider
from note_bridge.providers.huawei import encode_body
from note_bridge.tasks import TaskRunner


def run(jars, root):
    intent_path = root / ".private/lab-repair-job.json"
    intent = json.loads(intent_path.read_text("utf-8"))
    manifest = json.loads((root / ".private/lab-write-job.json").read_text("utf-8"))
    if (intent.get("armed") is not True or intent.get("fixture_id") not in ("fixture-20260906-huawei-image-H2", "fixture-20260906-huawei-image-H3")
            or manifest.get("id") != intent["fixture_id"] or manifest.get("armed") or manifest.get("target") != "huawei"):
        raise ValueError("repair_scope")
    intent["armed"] = False
    intent_path.write_text(json.dumps(intent, indent=2), "utf-8")
    spec = importlib.util.spec_from_file_location("huawei_fixture_source", root / "scripts/live-fixture-job.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    note = module.fixture(manifest)
    store = Store(root / ".private/session-lab/notes.sqlite")
    provider = create_provider("huawei", jars, root / ".private/session-lab/resources")
    runner = TaskRunner(store)
    fixture_label = intent["fixture_id"].rsplit("-", 1)[1]
    report = {"kind": "huawei-" + fixture_label + "-scoped-completion", "formal_acceptance": False, "status": "not_started", "creates": 0, "uploads": 0}
    try:
        if provider.probe() != manifest["expected_account"]:
            raise ValueError("account_changed")
        key = receipt_key(note, provider)
        receipt = store.receipt(key)
        if not receipt or receipt["status"] != "uncertain" or len(receipt["remote_ids"]) != 1:
            raise ValueError("receipt_scope")
        cloud_id = receipt["remote_ids"][0]
        original = {n.source_id: n.fingerprint() for n in store.notes("huawei", provider.account_id) if n.source_id != cloud_id}
        listing, rows = provider._listing()
        if len(rows) != len(original) + 1 or cloud_id not in {r["guid"] for r in rows}:
            raise ValueError("snapshot_changed")
        detail = provider._json("note/query", {"guid": cloud_id, "kind": "note", "ctagNoteInfo": listing.get("ctagNoteInfo", ""), "startCursor": listing.get("startCursor", "")})["rspInfo"]
        encoded = json.loads(detail["data"])
        if encoded["content"].get("title") != note.title or detail["guid"] != cloud_id:
            raise ValueError("fixture_title_mismatch")
        (root / (".private/checkpoints/huawei-" + fixture_label + "-preimage.json")).write_text(json.dumps({"fixture_body": encoded, "original_fingerprints": original}), "utf-8")
        resources = detail.get("attachments") or []
        if len(resources) != len(note.attachments) or len(resources) != 2:
            raise ValueError("fixture_resources_mismatch")
        images = {}
        def verify_assets(context):
            for metadata in resources:
                matching = []
                for asset in note.attachments:
                    if metadata.get("resourceLength") != asset.size:
                        continue
                    provider._files.download(asset.model_copy(), cloud_id, metadata, context, expected=asset)
                    matching.append(asset)
                if len(matching) != 1:
                    raise BridgeError("fixture_resources_mismatch", "测试图片无法唯一核对，未修改正文。")
                images[matching[0].id] = metadata["usage"]
        runner.start("fixture_verify_assets", verify_assets)
        runner.join()
        if runner.current().status != "succeeded" or len(images) != 2:
            raise ValueError("fixture_asset_verification_failed")
        plain, body, warnings = encode_body(note, images)
        encoded["fileList"] = [{"name": name} for name in images.values()]
        encoded["currentNotePadVersion"] = uuid.uuid4().hex[:4] + "-" + str(int(time.time() * 1000)) + "-" + str(uuid.uuid4().int % 100000).zfill(5)
        encoded["content"].update(content=plain, html_content=body, prefix_uuid=detail["uuid"], unstruct_uuid=detail["luid"],
            modified=int(time.time() * 1000), has_attachment=1, first_attach_name=next(iter(images.values())),
            unstructure=json.dumps(encoded["fileList"]))
        tags = provider._json("notetag/query", {"index": 0})
        original_request = provider.transport.session.request
        def observe_update(method, url, **kwargs):
            response = original_request(method, url, **kwargs)
            if method == "POST" and url.endswith("/note/update"):
                report["http_status"] = response.status_code
                try:
                    result = response.json().get("Result", {})
                    code = result.get("code")
                    report["response_code"] = code if str(code).isdigit() else "other"
                    description = str(result.get("desc", ""))
                    report["recognized_flags"] = [word for word in ("etag", "version", "guid", "参数", "错误", "冲突", "数据", "token") if word in description]
                except (ValueError, AttributeError):
                    report["json"] = False
            return response
        provider.transport.session.request = observe_update
        def complete(context):
            context.begin_write(key + ":completion-" + fixture_label)
            context.record_remote_ids([cloud_id])
            payload = {"ctagNoteInfo": listing.get("ctagNoteInfo", ""), "ctagNoteTag": tags.get("ctagNoteTag", ""),
                "startCursor": listing.get("startCursor", ""), "reqInfo": {"guid": cloud_id, "kind": "note", "etag": detail["etag"],
                "simpleNote": "", "data": json.dumps(encoded, ensure_ascii=False)}}
            provider._json("note/update", payload, write=True)
            context.update(succeeded=1)
        runner.start("fixture_complete", complete)
        runner.join()
        report["write_status"] = runner.current().status
        report["issue_codes"] = [i.code for i in runner.current().issues]
        if runner.current().status != "succeeded":
            store.save_receipt(key + ":completion-" + fixture_label, "uncertain")
            report["status"] = "needs_review"
            return report
        runner.start("fixture_readback", lambda ctx: fetch_snapshot(provider, store, ctx))
        runner.join()
        after = {n.source_id: n for n in store.notes("huawei", provider.account_id)}
        actual = after.get(cloud_id)
        preserved = all(k in after and after[k].fingerprint() == value for k, value in original.items())
        if actual is None:
            report.update(status="needs_review", directory_missing=True, after_count=len(after), original_notes_unchanged=preserved)
            store.save_receipt(key + ":completion-" + fixture_label, "uncertain")
            return report
        expected_blocks = [b.model_copy(deep=True) for b in note.blocks]
        ordinal = 0
        for block in expected_blocks:
            if block.kind == "attachment":
                source = next(a for a in note.attachments if a.id == block.attachment_id)
                matches = [a for a in actual.attachments if a.sha256 == source.sha256 and a.size == source.size] if actual else []
                if len(matches) != 1:
                    raise ValueError("readback_asset_mismatch")
                block.attachment_id = matches[0].id
            if block.kind == "list":
                ordinal += 1
                block.kind, block.ordered = "paragraph", False
                block.spans[0].text = str(ordinal) + ". " + block.spans[0].text
            else:
                ordinal = 0
        matched = actual and any(actual.blocks[i:i + len(expected_blocks)] == expected_blocks for i in range(len(actual.blocks)))
        report.update(original_notes_unchanged=preserved, content_verified=bool(matched), image_count=len(actual.attachments) if actual else 0,
                      warnings=warnings, after_count=len(after))
        if runner.current().status == "succeeded" and preserved and matched:
            store.save_receipt(key, "confirmed", [cloud_id])
            store.save_receipt(key + ":completion-" + fixture_label, "confirmed", [cloud_id])
            report["status"] = "verified_with_degradation"
        else:
            report["status"] = "needs_review"
        return report
    finally:
        provider.close()
        (root / (".private/evidence/huawei-" + fixture_label + "-completion.json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
