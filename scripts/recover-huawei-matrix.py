"""Inspect, then complete an explicitly listed interrupted test draft; never create a note."""
import importlib.util
import json
import re
import time
import uuid

from cloud_fixture_source import select_source
from lab_store import LabStore as Store

from note_bridge.errors import BridgeError
from note_bridge.operations import fetch_snapshot, receipt_key
from note_bridge.providers.factory import create_provider
from note_bridge.providers.huawei import encode_body
from note_bridge.providers.huawei_files import image_bytes
from note_bridge.tasks import TaskRunner


def recovery_scope(intent):
    label = intent.get("batch", "W4")
    batches = {"W4": ("vivo-huawei-W4-job.json", "matrix-20260906-vivo-huawei-W4"),
               "AA2": ("meizu-huawei-AA2-job.json", "matrix-20260906-meizu-huawei-AA2"),
               "AA6": ("honor-huawei-AA6-job.json", "matrix-20260906-honor-huawei-AA6"),
               "BC3": ("xiaomi-huawei-BC3-job.json", "matrix-20260907-xiaomi-huawei-BC3")}
    if not isinstance(label, str) or label not in batches:
        raise BridgeError("recovery_scope", "该中断批次不在已审查的草稿恢复范围内。")
    if label == "BC3" and (type(intent.get("entry_index")) is not int or intent["entry_index"] != 2):
        raise BridgeError("recovery_scope", "BC3 恢复仅限已记录的第三条草稿。")
    return label, *batches[label]


def recovery_identity(detail, encoded):
    content = encoded.get("content", {})
    prefix, luid = content.get("prefix_uuid"), content.get("unstruct_uuid")
    if (not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_$-]{1,150}", v) for v in (prefix, luid))
            or detail.get("luid") != luid or (detail.get("uuid") not in (None, "", prefix))):
        raise BridgeError("recovery_scope", "草稿正文中的附件身份与详情不一致。")
    return prefix, luid


def plan_images(source, draft, metadata, resource_receipts, specs, prefix):
    if len(source.attachments) != 2 or not 1 <= len(draft.attachments) <= 2 or len(metadata) != len(draft.attachments):
        raise BridgeError("recovery_scope", "草稿附件数量不在本次恢复范围内。")
    images, used = {}, set()
    for asset in draft.attachments:
        matches = [item for item in source.attachments if (item.sha256, item.size) == (asset.sha256, asset.size)]
        rows = [row for row in metadata if row.get("assetId") == asset.id and row.get("usage") == asset.name]
        if len(matches) != 1 or len(rows) != 1 or matches[0].id in images:
            raise BridgeError("recovery_scope", "已有图片无法唯一匹配来源原件。")
        width, height, extension = specs[matches[0].id]
        if not re.fullmatch(re.escape(f"{prefix}_{width}_{height}_") + r"\d{13}\." + extension, asset.name):
            raise BridgeError("recovery_scope", "已有图片名称不属于草稿正文记录的附件身份。")
        images[matches[0].id] = asset.name
        used.update((asset.id, asset.name))
    remaining = [item for item in resource_receipts if item["resource_id"] not in used]
    missing = [asset for asset in source.attachments if asset.id not in images]
    if not missing:
        if remaining:
            raise BridgeError("recovery_scope", "存在未能解释的旧资源回执。")
        return images, None, None
    if len(missing) != 1 or len(remaining) != 1 or remaining[0]["status"] != "allocated":
        raise BridgeError("recovery_scope", "缺失图片与中断资源回执不一致。")
    asset = missing[0]
    width, height, extension = specs[asset.id]
    usage = remaining[0]["resource_id"]
    if not re.fullmatch(re.escape(f"{prefix}_{width}_{height}_") + r"\d{13}\." + extension, usage):
        raise BridgeError("recovery_scope", "中断资源名称不属于这条草稿或这张图片。")
    return images, asset, usage


def scoped_image_plan(label, source, draft, metadata, resources, specs, prefix):
    images, missing, usage = plan_images(source, draft, metadata, resources, specs, prefix)
    if label == "BC3":
        expected = {value for asset in draft.attachments for value in (asset.id, asset.name)}
        if (missing is not None or len(images) != 2 or len(expected) != 4 or len(resources) != 4
                or {item["resource_id"] for item in resources} != expected
                or any(item["status"] != "uploaded" for item in resources)):
            raise BridgeError("recovery_scope", "BC3 必须复用两张已上传图片，不允许新增或重新上传。")
    return images, missing, usage


def require_bc3_inspection(root, scope):
    try:
        report = json.loads((root / ".private/evidence/huawei-BC3-recovery-inspect.json").read_text("utf-8"))
        if (report.get("kind") != "huawei-BC3-scoped-recovery" or report.get("mode") != "inspect"
                or report.get("status") != "plan_verified" or report.get("scope") != scope
                or any(type(report.get(key)) is not int or report[key] != value
                       for key, value in {"creates": 0, "uploads": 0, "updates": 0,
                                          "existing_images": 2, "missing_images": 0}.items())):
            raise ValueError()
    except (OSError, ValueError, TypeError, AttributeError):
        raise BridgeError("recovery_scope", "BC3 修复需要先完成同一草稿、账号及指纹的只读检查。") from None


def run(jars, root):
    intent_path = root / ".private/lab-matrix-recovery.json"
    intent = json.loads(intent_path.read_text("utf-8"))
    label, manifest_name, batch_id = recovery_scope(intent)
    if intent.get("armed") is not True or intent.get("mode") not in ("inspect", "repair"):
        return {"status": "not_armed"}
    for name in ("lab-write-job.json", "lab-matrix-job.json"):
        if json.loads((root / ".private" / name).read_text("utf-8")).get("armed"):
            raise BridgeError("conflicting_write_jobs", "其他写入意图尚未消费。")
    intent["armed"] = False
    intent_path.write_text(json.dumps(intent, indent=2), "utf-8")
    modules = []
    for name in ("live-fixture-job.py", "live-matrix-job.py"):
        spec = importlib.util.spec_from_file_location(name, root / "scripts" / name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules.append(module)
    fixture, matrix = modules
    job = json.loads((root / ".private/checkpoints" / manifest_name).read_text("utf-8"))
    if (job["id"] != batch_id or job["armed"] or job["expected_account"] != intent.get("expected_account")
            or job.get("target") != "huawei"):
        raise BridgeError("recovery_scope", "本次只核对已记录批次中明确限定的华为草稿。")
    store = Store(root / ".private/session-lab/notes.sqlite")
    source = select_source({**job["sources"][0 if label == "AA6" else 2], "target": "huawei"}, store, root, fixture.fixture)
    provider = create_provider("huawei", jars, root / ".private/session-lab/resources")
    # BC3 may only use the already-audited uncertain receipt. Runner startup
    # normalizes sending receipts, so validate this scope before creating it.
    runner = None if label == "BC3" else TaskRunner(store)
    report = {"kind": f"huawei-{label}-scoped-recovery", "formal_acceptance": False,
              "mode": intent["mode"], "status": "not_started", "creates": 0, "uploads": 0, "updates": 0}
    recovery_key = None

    def reread():
        runner.start("recovery_read", lambda ctx: fetch_snapshot(provider, store, ctx))
        runner.join()
        task = runner.current()
        if task.status not in ("succeeded", "partial") or {i.code for i in task.issues} - {"source_warning", "incomplete_snapshot"}:
            raise BridgeError("recovery_read_failed", "恢复前后的完整读取未通过。")
        return {note.source_id: note for note in store.notes("huawei", provider.account_id)}

    def query(cloud_id):
        listing, rows = provider._listing()
        if sum(row.get("guid") == cloud_id for row in rows) != 1:
            raise BridgeError("recovery_scope", "草稿不在当前官方目录中。")
        payload = provider._json("note/query", {"guid": cloud_id, "kind": "note",
            "ctagNoteInfo": listing.get("ctagNoteInfo", ""), "startCursor": listing.get("startCursor", "")})
        detail = payload["rspInfo"]
        report["query_fields"] = {k: {"envelope": bool(payload.get(k)), "detail": bool(detail.get(k))}
                                  for k in ("uuid", "luid", "guid", "startCursor")}
        if detail.get("guid") != cloud_id or not detail.get("etag"):
            raise BridgeError("recovery_scope", "草稿身份或版本无法确认。")
        return listing, detail

    try:
        report["stage"] = "account_and_receipt"
        if provider.probe() != job["expected_account"]:
            raise BridgeError("account_changed", "华为恢复账号不匹配。")
        key = receipt_key(source, provider)
        receipt = store.receipt(key)
        if not receipt or receipt["status"] != "uncertain" or len(receipt["remote_ids"]) != 1:
            raise BridgeError("recovery_scope", "原始不明回执不符合预期。")
        if runner is None:
            runner = TaskRunner(store)
        cloud_id = receipt["remote_ids"][0]
        report["stage"] = "snapshot"
        before = reread()
        draft = before.get(cloud_id)
        if draft is None or draft.fingerprint() != intent.get("target_fingerprint") or source.fingerprint() != intent.get("source_fingerprint"):
            raise BridgeError("recovery_changed", "来源或草稿自只读检查后发生变化，未修改。")
        report["stage"] = "query"
        listing, detail = query(cloud_id)
        encoded = json.loads(detail["data"])
        report["body_fields"] = {k: bool(encoded.get("content", {}).get(k)) for k in ("prefix_uuid", "unstruct_uuid")}
        report["identity_diagnostics"] = {
            "luid_matches_body": detail.get("luid") == encoded["content"].get("unstruct_uuid"),
            "prefix_format_valid": isinstance(encoded["content"].get("prefix_uuid"), str) and bool(re.fullmatch(r"[A-Za-z0-9_$-]{1,150}", encoded["content"]["prefix_uuid"])),
            "body_luid_format_valid": isinstance(encoded["content"].get("unstruct_uuid"), str) and bool(re.fullmatch(r"[A-Za-z0-9_$-]{1,150}", encoded["content"]["unstruct_uuid"])),
            "detail_luid_type": type(detail.get("luid")).__name__,
        }
        report["stage"] = "title"
        if encoded["content"].get("title") != source.display_title:
            raise BridgeError("recovery_scope", "草稿标题与来源验收标记不符。")
        report["stage"] = "body"
        skeleton = source.model_copy(update={"attachments": [], "blocks": [b for b in source.blocks if b.kind != "attachment"]})
        bare_draft = draft.model_copy(update={"attachments": [], "blocks": [b for b in draft.blocks if b.kind != "attachment"]})
        if not matrix.compare_note(skeleton, bare_draft, encode_body(skeleton)[2]):
            raise BridgeError("recovery_changed", "草稿现有正文与来源不一致。")
        report["stage"] = "images"
        prefix, luid = recovery_identity(detail, encoded)
        specs = {}
        for asset in source.attachments:
            _, size, extension = image_bytes(asset, provider.resources)
            specs[asset.id] = (*size, extension)
        report["image_diagnostics"] = {
            "source_count": len(source.attachments), "draft_count": len(draft.attachments),
            "metadata_count": len(detail.get("attachments") or []),
            "prefix_present": bool(detail.get("uuid")),
            "existing_source_matches": [sum((s.sha256, s.size) == (a.sha256, a.size) for s in source.attachments) for a in draft.attachments],
            "existing_metadata_matches": [sum(r.get("assetId") == a.id and r.get("usage") == a.name for r in detail.get("attachments") or []) for a in draft.attachments],
            "resource_statuses": [r["status"] for r in store.resource_receipts(key)],
        }
        images, missing, usage = scoped_image_plan(label, source, draft, detail.get("attachments") or [],
            store.resource_receipts(key), specs, prefix)
        report.update(status="plan_verified", existing_images=len(images), missing_images=int(missing is not None),
                      original_other_notes=len(before) - 1)
        if label == "BC3":
            report["scope"] = {"batch_id": batch_id, "entry_index": 2, "account": provider.account_id,
                               "receipt_key": key, "source_fingerprint": source.fingerprint(),
                               "target_fingerprint": draft.fingerprint()}
            if intent["mode"] == "repair":
                require_bc3_inspection(root, report["scope"])
        fixture.write_json(root / f".private/checkpoints/huawei-{label}-recovery-before.json", {
            "fingerprints": {k: note.fingerprint() for k, note in before.items()}, "draft_data": encoded})
        if intent["mode"] == "inspect":
            return report
        recovery_key = key + f":{label}-scoped-recovery"
        if store.receipt(recovery_key):
            raise BridgeError("recovery_already_attempted", "恢复已有回执，必须先核对，不能重复执行。")

        def complete(context):
            if label == "BC3" and missing is not None:
                raise BridgeError("recovery_scope", "BC3 不允许新增或重新上传图片。")
            context.begin_write(recovery_key)
            context.record_remote_ids([cloud_id])
            if missing:
                report["uploads"] += 1
                uploaded = provider._files.upload(missing, cloud_id, prefix, context, usage=usage)
                images[missing.id] = uploaded["usage"]
            fresh_listing, fresh = query(cloud_id)
            if json.loads(fresh["data"]) != encoded or recovery_identity(fresh, json.loads(fresh["data"])) != (prefix, luid):
                raise BridgeError("recovery_changed", "上传期间草稿正文变化，未覆盖正文。")
            body = json.loads(fresh["data"])
            plain, markup, warnings = encode_body(source, images)
            ordered = [images[a.id] for a in source.attachments]
            body.update(guid=cloud_id, fileList=[{"name": name} for name in ordered],
                        currentNotePadVersion=uuid.uuid4().hex[:4] + "-" + str(int(time.time() * 1000)) + "-" + str(uuid.uuid4().int % 100000).zfill(5))
            body["content"].update(content=plain, html_content=markup, prefix_uuid=prefix, unstruct_uuid=luid,
                modified=int(time.time() * 1000), has_attachment=1, first_attach_name=ordered[0],
                unstructure=json.dumps(body["fileList"]))
            tags = provider._json("notetag/query", {"index": 0})
            report["updates"] += 1
            provider._json("note/update", {"ctagNoteInfo": fresh_listing.get("ctagNoteInfo", ""),
                "ctagNoteTag": tags.get("ctagNoteTag", ""), "startCursor": fresh.get("startCursor", fresh_listing.get("startCursor", "")),
                "reqInfo": {"guid": cloud_id, "kind": "note", "etag": fresh["etag"], "simpleNote": "", "data": json.dumps(body, ensure_ascii=False)}}, write=True)
            report["warnings"] = warnings
            context.update(succeeded=1)

        runner.start("recovery_complete", complete)
        runner.join()
        if runner.current().status != "succeeded":
            store.save_receipt(recovery_key, "uncertain")
            raise BridgeError("recovery_write_review", "恢复步骤未全部确认，请保留回执核对。")
        after = reread()
        actual = after.get(cloud_id)
        preserved = before.keys() == after.keys() and all(
            before[k].fingerprint() == after[k].fingerprint() for k in before if k != cloud_id)
        verified = (actual is not None and not actual.warnings and matrix.compare_note(source, actual, report["warnings"])
                    and actual.source_folder_id == draft.source_folder_id and preserved
                    and store.snapshot("huawei", provider.account_id).get("complete"))
        resources = {value for asset in actual.attachments for value in (asset.id, asset.name)} if actual else set()
        verified = verified and all(item["resource_id"] in resources for item in store.resource_receipts(key) + store.resource_receipts(recovery_key))
        report.update(original_other_notes_unchanged=preserved, content_verified=bool(verified))
        if not verified:
            store.save_receipt(recovery_key, "uncertain")
            raise BridgeError("recovery_readback_review", "恢复后内容或资源仍需核对。")
        store.save_receipt(key, "confirmed", [cloud_id])
        store.save_receipt(recovery_key, "confirmed", [cloud_id])
        report["status"] = "verified_with_degradation"
    except BridgeError as error:
        report.update(status="needs_review", code=error.code)
        if recovery_key and store.receipt(recovery_key) and store.receipt(recovery_key)["status"] == "sending":
            store.save_receipt(recovery_key, "uncertain")
    except Exception as error:
        # Exception text can contain authenticated response data; persist only its type.
        report.update(status="needs_review", code="internal_error", error_type=type(error).__name__)
        if recovery_key and store.receipt(recovery_key) and store.receipt(recovery_key)["status"] == "sending":
            store.save_receipt(recovery_key, "uncertain")
    finally:
        provider.close()
        fixture.write_json(root / (f".private/evidence/huawei-{label}-recovery-" + intent["mode"] + ".json"), report)
    return report
