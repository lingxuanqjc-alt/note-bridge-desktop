"""One consumed lab batch, one real source account, one target comparison pair.

Only previously confirmed synthetic cloud fixtures can enter this harness. It
reports API evidence, never marks a complete migration direction accepted.
"""
import hashlib
import importlib.util
import json
import re
from types import SimpleNamespace

from cloud_fixture_source import select_source
from lab_store import LabStore as Store
from matrix_expectations import expected_blocks
from migration_content_check import contains_content

from note_bridge.errors import BridgeError, WriteUncertain
from note_bridge.operations import fetch_snapshot, migrate, receipt_key
from note_bridge.paths import AppPaths, confined
from note_bridge.providers.factory import create_provider
from note_bridge.receipt_identity import identity_key
from note_bridge.tasks import TaskRunner

OPPO_SCOPE = "OR1_fixed_direct_12"
OPPO_DIRECT_SEEDS = {
    "oppo": ("fixture-20260906-oppo-image-F2-consumed-job.json", "oppo-complex-OR1-job.json"),
    "wps": ("wps-group-J2-job.json", "wps-complex-BD1-job.json"),
    "huawei": ("huawei-group-K2-job.json", "huawei-complex-BD2-job.json"),
    "xiaomi": ("xiaomi-group-P2-job.json", "xiaomi-image-AC6-job.json"),
    "honor": ("honor-group-N2-job.json", "honor-complex-BD5-job.json"),
    "meizu": ("meizu-group-L2-job.json", "meizu-complex-BD6-job.json"),
    "vivo": ("vivo-group-M2-job.json", "vivo-complex-V1-job.json"),
}


def oppo_batch_scope(job):
    """The reopened lab gate is only for these twelve routes and their original pair."""
    entries = job.get("sources", [])
    origins = [entry.get("from_cloud_fixture", {}) for entry in entries if isinstance(entry, dict)]
    platforms = {origin.get("platform") for origin in origins if isinstance(origin, dict)}
    involved = job.get("target") == "oppo" or "oppo" in platforms
    if not involved and "oppo_scope" not in job:
        return False
    source = next(iter(platforms)) if len(platforms) == 1 else None
    target = job.get("target")
    if (not involved or job.get("oppo_scope") != OPPO_SCOPE or job.get("source_policy") != "direct_seed_only"
            or job.get("kind") != "cloud-matrix-batch" or source not in OPPO_DIRECT_SEEDS
            or target not in OPPO_DIRECT_SEEDS or (source == "oppo") == (target == "oppo")
            or len(entries) != 2 or len(origins) != 2 or any(not isinstance(origin, dict) for origin in origins)
            or tuple(origin.get("manifest") for origin in origins) != OPPO_DIRECT_SEEDS[source]
            or any("batch_manifest" in origin for origin in origins)
            or not re.fullmatch(r"matrix-\d{8}-[A-Za-z0-9-]{1,30}", str(job.get("id", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(job.get("expected_account", "")))):
        raise BridgeError("platform_deferred", "OPPO 仅恢复本轮固定原始种子的 12 个实验方向。")
    scoped = source in ("oppo", "huawei", "vivo")
    if (any(("scoped_proof" in origin) != scoped for origin in origins)
            or (scoped and origins[0]["scoped_proof"] != origins[1]["scoped_proof"])):
        raise BridgeError("invalid_fixture_source", "这对固定来源必须绑定同一次已验证的精确捕获。")
    return True


def configure_candidate(provider, notes, root):
    if provider.spec.id == "xiaomi":
        try:
            provider.require_unencrypted_mode()
        except BridgeError:
            path = root / ".private/evidence/xiaomi-upload-account-scope.json"
            scope = json.loads(path.read_text("utf-8")) if path.exists() else {}
            provider.confirm_upload_contract(scope)
    provider.write_supported = True
    provider.images_supported = True


def select_batch(job, store, root, fixture_builder):
    entries = job.get("sources")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 30:
        raise BridgeError("invalid_matrix_job", "批次必须明确列出 1 至 30 条已验证的云端样例。")
    if job.get("source_policy") not in (None, "direct_seed_only"):
        raise BridgeError("invalid_fixture_source", "来源策略无法识别，未执行迁入。")
    scoped_oppo_batch = oppo_batch_scope(job)
    if not scoped_oppo_batch and any("scoped_proof" in entry.get("from_cloud_fixture", {}) for entry in entries):
        from huawei_scoped_source import MANIFESTS, require
        origins = [entry.get("from_cloud_fixture", {}) for entry in entries]
        require(job.get("source_policy") == "direct_seed_only" and len(origins) == 2
            and tuple(origin.get("manifest") for origin in origins) == MANIFESTS
            and all(origin.get("platform") == "huawei" for origin in origins)
            and origins[0].get("scoped_proof") == origins[1].get("scoped_proof"))
    if job.get("source_policy") == "direct_seed_only":
        for entry in entries:
            source = entry.get("from_cloud_fixture")
            if (not isinstance(source, dict) or "batch_manifest" in source
                    or not re.fullmatch(r"[A-Za-z0-9-]+-job\.json", str(source.get("manifest", "")))):
                raise BridgeError("invalid_fixture_source", "本批次只允许平台直接创建的固定种子，不能再次迁出迁入笔记。")
            original = json.loads((root / ".private/checkpoints" / source["manifest"]).read_text("utf-8"))
            if (original.get("kind") != "independent-cloud-write-smoke" or original.get("from_cloud_fixture") is not None
                    or original.get("target") != source.get("platform")
                    or original.get("expected_account") != source.get("account")):
                raise BridgeError("invalid_fixture_source", "固定种子的原始记录、平台或账号不匹配。")
    if not scoped_oppo_batch and any(isinstance(entry.get("from_cloud_fixture"), dict)
           and entry["from_cloud_fixture"].get("platform") == "vivo" for entry in entries):
        raise BridgeError("vivo_source_scope_required", "vivo 测试来源已停用全账号缓存选择；请仅核对固定工具样例。")
    notes = [select_source({**entry, "target": job["target"]}, store, root, fixture_builder) for entry in entries]
    if len({(n.platform, n.account_id) for n in notes}) != 1:
        raise BridgeError("mixed_accounts", "一个批次必须属于同一来源平台和账号。")
    if (notes[0].platform == "oppo" or job["target"] == "oppo") and not scoped_oppo_batch:
        raise BridgeError("platform_deferred", "OPPO 按用户指令暂缓，未执行批次。")
    if len({n.source_id for n in notes}) != len(notes):
        raise BridgeError("duplicate_source", "批次样例标识重复，未执行写入。")
    if scoped_oppo_batch:
        target = SimpleNamespace(spec=SimpleNamespace(id=job["target"]), account_id=job["expected_account"])
        for entry, note in zip(entries, notes, strict=True):
            origin = entry["from_cloud_fixture"]
            image_count = 0 if origin["platform"] == "xiaomi" and origin["manifest"] == OPPO_DIRECT_SEEDS["xiaomi"][0] else 2
            if ((note.platform, note.account_id, note.source_id, note.fingerprint(), note.display_title)
                    != (origin["platform"], origin.get("account"), origin.get("source_id"),
                        origin.get("fingerprint"), entry.get("title"))
                    or entry.get("with_images") is not bool(image_count)
                    or entry.get("with_group") is not bool(note.source_folder_name)
                    or len(note.attachments) != image_count or any(asset.kind != "image" for asset in note.attachments)):
                raise BridgeError("invalid_fixture_source", "OPPO 本轮固定来源必须保留原始图片数量；小米 P2 为零，其余各两张。")
            receipt = store.check_migration_write(note, target)
            with store.connection() as db:
                resource = db.execute("SELECT 1 FROM resource_receipts WHERE receipt_key=? LIMIT 1",
                                      (receipt_key(note, target),)).fetchone()
            if receipt or resource:
                raise BridgeError("matrix_receipt_exists", "已有迁入回执或资源，不能换批次编号重复执行。")
            if note.source_folder_name:
                groups = __import__("note_bridge.providers." + job["target"] + "_groups", fromlist=["folder_key"])
                group = store.receipt(groups.folder_key(note, target.account_id))
                if group and group["status"] in ("sending", "uncertain"):
                    raise WriteUncertain("来源分组已有待核对的目标回执，尚未开始新的迁入。")
    return notes


def compare_note(source, restored, warnings=()):
    policy = expected_blocks(source, restored.platform, warnings)
    if (restored.platform == "xiaomi" and policy is None
            and any(span.link for block in source.blocks for span in block.spans)):
        return False
    expected, paragraph_breaks = policy or ([block.model_copy(deep=True) for block in source.blocks], False)
    mapping = {}
    for asset in source.attachments:
        matches = [item for item in restored.attachments if
                   (item.kind, item.size, item.sha256) == (asset.kind, asset.size, asset.sha256)]
        if len(matches) != 1 or not asset.sha256:
            return False
        mapping[asset.id] = matches[0].id
    if len(mapping) != len(restored.attachments):
        return False
    for block in expected:
        if block.kind == "attachment":
            block.attachment_id = mapping.get(block.attachment_id)
    return (source.display_title == restored.display_title
            and contains_content(restored.blocks, expected, paragraph_breaks=paragraph_breaks)
            and all(value.date().isoformat() in restored.plain_text
                    for value in (source.created_at, source.updated_at) if value))


def confirmed_tool_binding(name, index, store, root, fixture_builder, seen=()):
    """Resolve archived synthetic ancestry without reading any account's note cache."""
    if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9-]+-job\.json", name)
            or name in seen or len(seen) >= 8):
        raise BridgeError("invalid_synthetic_scope", "工具样例来源记录路径或谱系无法确认。")
    try:
        payload = (root / ".private/checkpoints" / name).read_bytes()
        original = json.loads(payload)
        kind = original.get("kind")
        prefix = "fixture" if kind == "independent-cloud-write-smoke" else "matrix"
        if (kind not in ("independent-cloud-write-smoke", "cloud-matrix-batch")
                or original.get("armed") is not False
                or not re.fullmatch(prefix + r"-\d{8}-[A-Za-z0-9-]{1,30}", original.get("id", ""))
                or not re.fullmatch(r"[0-9a-f]{64}", original.get("expected_account", ""))):
            raise ValueError()
        if kind == "cloud-matrix-batch":
            entries = original["sources"]
            if type(index) is not int or not isinstance(entries, list) or not 0 <= index < len(entries):
                raise ValueError()
            entry = entries[index]
        else:
            if index is not None:
                raise ValueError()
            entry = original
        target = SimpleNamespace(spec=SimpleNamespace(id=original["target"]),
                                 account_id=original["expected_account"])
        origin = entry.get("from_cloud_fixture")
        lineage = []
        if origin is None and kind == "independent-cloud-write-smoke":
            seed = fixture_builder(original)
            if seed.account_id != "synthetic-fixture-source" or seed.source_id != original["id"]:
                raise ValueError()
            key = receipt_key(seed, target)
        else:
            if not isinstance(origin, dict) or ("manifest" in origin) == ("batch_manifest" in origin):
                raise ValueError()
            previous = confirmed_tool_binding(origin.get("manifest", origin.get("batch_manifest")),
                origin.get("entry_index") if "batch_manifest" in origin else None,
                store, root, fixture_builder, (*seen, name))
            if (previous["platform"], previous["account"], previous["remote_id"]) != (
                    origin["platform"], origin["account"], origin["source_id"]):
                raise ValueError()
            key = identity_key({"source_platform": origin["platform"], "source_account": origin["account"],
                "source_id": origin["source_id"], "source_fingerprint": origin["fingerprint"],
                "target_platform": target.spec.id, "target_account": target.account_id})
            lineage = [previous]
        receipt = store.receipt(key)
        if (not receipt or receipt["status"] != "confirmed" or len(receipt["remote_ids"]) != 1
                or not isinstance(receipt["remote_ids"][0], str) or not receipt["remote_ids"][0]):
            raise BridgeError("synthetic_receipt_unconfirmed", "仅有唯一已确认回执的工具样例可进入读取范围。")
        return {"manifest": name, "manifest_sha256": hashlib.sha256(payload).hexdigest(), "entry_index": index,
                "platform": target.spec.id, "account": target.account_id, "receipt_key": key,
                "remote_id": receipt["remote_ids"][0], "lineage": lineage}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise BridgeError("invalid_synthetic_scope", "工具样例原记录、账号或精确回执身份无法确认。") from None


def vivo_tool_scope(store, root, fixture_builder, account):
    """Select confirmed tool-created IDs; titles and the full note cache are never consulted."""
    bindings, excluded = {}, []
    for path in sorted((root / ".private/checkpoints").glob("*-job.json")):
        try:
            original = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if (not isinstance(original, dict) or original.get("target") != "vivo"
                or original.get("expected_account") != account or original.get("armed") is not False):
            continue
        if original.get("kind") == "cloud-matrix-batch":
            entries = original.get("sources")
            if not isinstance(entries, list) or not 1 <= len(entries) <= 30:
                excluded.append({"manifest": path.name, "entry_index": None, "code": "invalid_synthetic_scope"})
                continue
            indices = range(len(entries))
        else:
            indices = [None]
        for index in indices:
            try:
                binding = confirmed_tool_binding(path.name, index, store, root, fixture_builder)
                if not re.fullmatch(r"[a-f0-9]{32}", binding["remote_id"]):
                    raise BridgeError("invalid_synthetic_scope", "vivo 工具样例回执不是已验证的 GUID。")
            except BridgeError as error:
                excluded.append({"manifest": path.name, "entry_index": index, "code": error.code})
                continue
            prior = bindings.get(binding["remote_id"])
            if prior and prior["receipt_key"] != binding["receipt_key"]:
                raise BridgeError("invalid_synthetic_scope", "同一 vivo 工具样例存在不同写入身份，需先核对。")
            bindings[binding["remote_id"]] = binding
    return {"kind": "confirmed-tool-created-vivo-scope", "account_id": account,
            "exact_ids": tuple(sorted(bindings)), "bindings": [bindings[key] for key in sorted(bindings)],
            "excluded": excluded, "whole_account_snapshot": False}


def vivo_source_binding(job, store, root, fixture_builder):
    """Bind only the batch's direct original Vivo seeds; no cache-completeness exception."""
    entries = job.get("sources")
    if (job.get("kind") != "cloud-matrix-batch" or job.get("target") in ("vivo", "oppo")
            or not re.fullmatch(r"matrix-\d{8}-[A-Za-z0-9-]{1,30}", job.get("id", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", job.get("expected_account", ""))
            or not isinstance(entries, list) or not 1 <= len(entries) <= 30):
        raise BridgeError("invalid_fixture_source", "vivo 限定来源必须绑定一个明确的工具验收批次。")
    bindings = []
    try:
        for entry in entries:
            origin = entry["from_cloud_fixture"]
            if (origin.get("platform") != "vivo" or "batch_manifest" in origin
                    or not re.fullmatch(r"[a-f0-9]{32}", origin.get("source_id", ""))
                    or not re.fullmatch(r"[a-f0-9]{64}", origin.get("fingerprint", ""))):
                raise ValueError()
            binding = confirmed_tool_binding(origin["manifest"], None, store, root, fixture_builder)
            if (binding["lineage"] or (binding["platform"], binding["account"], binding["remote_id"])
                    != ("vivo", origin["account"], origin["source_id"])):
                raise ValueError()
            original = json.loads((root / ".private/checkpoints" / origin["manifest"]).read_text("utf-8"))
            evidence_path = root / ".private/evidence" / (original["id"] + ".json")
            evidence_bytes = evidence_path.read_bytes()
            evidence = json.loads(evidence_bytes)
            if (original.get("kind") != "independent-cloud-write-smoke"
                    or original.get("from_cloud_fixture") is not None
                    or original.get("title") != entry.get("title")
                    or evidence.get("fixture_id") != original["id"] or evidence.get("platform") != "vivo"
                    or evidence.get("status") not in ("verified", "verified_with_degradation")):
                raise ValueError()
            bindings.append({**binding, "source_fingerprint": origin["fingerprint"],
                             "original_evidence_sha256": hashlib.sha256(evidence_bytes).hexdigest()})
        if len({b["account"] for b in bindings}) != 1 or len({b["remote_id"] for b in bindings}) != len(entries):
            raise ValueError()
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise BridgeError("invalid_fixture_source", "vivo 固定来源的原始样例、账号、回执或历史回读不匹配。") from None
    return {"batch": {key: job.get(key) for key in
                      ("kind", "id", "target", "expected_account", "source_policy", "sources")},
            "account_id": bindings[0]["account"], "exact_ids": [b["remote_id"] for b in bindings],
            "bindings": bindings}


def validate_vivo_source_notes(job, binding, notes, root):
    from vivo_scoped_readback import DIVIDER_DOWNGRADE_WARNING

    from note_bridge.richtext import DEFAULT_LAYOUT_WARNING

    if len(notes) != len(binding["exact_ids"]):
        raise BridgeError("fixture_source_changed", "vivo 限定来源数量不一致。")
    for entry, original, note in zip(job["sources"], binding["bindings"], notes, strict=True):
        if ((note.platform, note.account_id, note.source_id, note.fingerprint())
                != ("vivo", binding["account_id"], original["remote_id"], original["source_fingerprint"])
                or note.display_title != entry.get("title") or not note.display_title.startswith("笔记互迁验收 · ")
                or bool(note.attachments) != bool(entry.get("with_images"))
                or bool(note.source_folder_name) != bool(entry.get("with_group"))
                or set(note.warnings) - {DEFAULT_LAYOUT_WARNING, DIVIDER_DOWNGRADE_WARNING}):
            raise BridgeError("fixture_source_changed", "vivo 限定来源正文、时间、分组或附件引用已变化。")
        for asset in note.attachments:
            path = confined(root / ".private/session-lab/resources", asset.local_path or "")
            if (not path.is_file() or path.stat().st_size != asset.size
                    or hashlib.sha256(path.read_bytes()).hexdigest() != asset.sha256):
                raise BridgeError("fixture_source_changed", "vivo 限定来源附件原件未通过完整性校验。")


def run_vivo_source_scope(jars, root, request):
    """One explicit source-post request for an already verified batch; never selects new writes."""
    from fetch_scope import task_summary
    from vivo_scoped_readback import read_notes, verify_account

    if request.get("armed") is not True:
        return {"kind": "vivo-source-scope", "status": "not_armed", "cloud_writes": 0}
    manifest, purpose = request.get("manifest", ""), request.get("purpose")
    if (request.get("platform") != "vivo" or request.get("operation") != "vivo_source_scope"
            or purpose != "source_post"
            or not re.fullmatch(r"[A-Za-z0-9-]+-job\.json", manifest)):
        raise BridgeError("invalid_fixture_source", "vivo 来源读取请求未限定到已知批次。")
    fixture_spec = importlib.util.spec_from_file_location("vivo_source_fixture", root / "scripts/live-fixture-job.py")
    fixture_module = importlib.util.module_from_spec(fixture_spec)
    fixture_spec.loader.exec_module(fixture_module)
    request["armed"] = False
    fixture_module.write_json(root / ".private/lab-discovery.json", request)
    manifest_bytes = (root / ".private/checkpoints" / manifest).read_bytes()
    job = json.loads(manifest_bytes)
    if job.get("armed") is not False:
        raise BridgeError("invalid_fixture_source", "源端补核对只接受已消费批次。")
    paths = AppPaths(root / ".private/session-lab")
    store = Store(paths.database)
    binding = vivo_source_binding(job, store, root, fixture_module.fixture)
    original_evidence = (root / ".private/evidence" / (job["id"] + ".json")).read_bytes()
    previous = json.loads(original_evidence)
    if (previous.get("batch_id") != job["id"] or previous.get("source") != "vivo"
            or previous.get("target") != job["target"]
            or previous.get("status") not in ("api_verified", "api_verified_with_degradation")
            or len(previous.get("items", [])) != len(binding["exact_ids"])
            or any(item.get("status") != "api_verified" or item.get("receipt_status") != "confirmed"
                   or item.get("content_verified") is not True or item.get("group_verified") is not True
                   or item.get("source_id") != ref["remote_id"]
                   or item.get("source_fingerprint") != ref["source_fingerprint"]
                   for item, ref in zip(previous["items"], binding["bindings"], strict=True))):
        raise BridgeError("invalid_fixture_source", "源端补核对需要对应批次已确认的逐条迁入结果。")
    for ref in binding["bindings"]:
        key = identity_key({"source_platform": "vivo", "source_account": binding["account_id"],
            "source_id": ref["remote_id"], "source_fingerprint": ref["source_fingerprint"],
            "target_platform": job["target"], "target_account": job["expected_account"]})
        receipt = store.receipt(key)
        if not receipt or receipt["status"] != "confirmed" or len(receipt["remote_ids"]) != 1:
            raise BridgeError("invalid_fixture_source", "该历史方向尚无唯一已确认的目标回执。")
    provider = create_provider("vivo", jars, paths.resources)
    runner = TaskRunner(store)
    acquired = []

    def read(context):
        if verify_account(provider) != binding["account_id"]:
            raise BridgeError("account_changed", "vivo 当前账号与固定来源不一致。")
        scope = read_notes(provider, binding["exact_ids"], context)
        if (scope.complete is not True or scope.account_id != binding["account_id"]
                or scope.exact_ids != tuple(binding["exact_ids"])):
            raise BridgeError("matrix_read_incomplete", "vivo 固定来源尚未完整读取。")
        validate_vivo_source_notes(job, binding, scope.notes, root)
        acquired.extend({"source_id": note.source_id, "fingerprint": note.fingerprint(),
            "assets": [{"id": asset.id, "sha256": asset.sha256, "size": asset.size} for asset in note.attachments]}
                        for note in scope.notes)

    try:
        runner.start("vivo_source_" + purpose, read)
        runner.join()
        task = runner.current()
        complete = (task.status in ("succeeded", "partial") and len(acquired) == len(binding["exact_ids"])
                    and task.completed == task.succeeded == task.total == len(binding["exact_ids"])
                    and not ({issue.code for issue in task.issues} - {"source_warning"}))
        proof = {"kind": "vivo-fixed-source-proof", "purpose": purpose, "scope_complete": complete,
            "whole_account_snapshot": False, "formal_acceptance": False, "cloud_writes": 0,
            "manifest": manifest, "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "binding": binding, "task": task_summary(task), "notes": acquired if complete else [],
            "synthetic_source_notes_unchanged": complete,
            "original_migration_evidence_sha256": hashlib.sha256(original_evidence).hexdigest()}
        destination = root / ".private/evidence" / f"vivo-source-{task.id}-proof.json"
        with destination.open("x", encoding="utf-8") as stream:
            json.dump(proof, stream, ensure_ascii=False, indent=2)
        return {"kind": "vivo-source-scope", "status": "verified" if complete else "needs_review",
                "purpose": purpose, "source_notes": len(acquired) if complete else 0, "cloud_writes": 0,
                "whole_account_snapshot": False, "proof_file": destination.relative_to(root).as_posix(),
                "task_id": task.id, "issue_codes": sorted({i.code for i in task.issues})}
    finally:
        provider.close()


def run_armed_job(platform, jars, root):
    path = root / ".private/lab-matrix-job.json"
    if not path.exists():
        return None
    job = json.loads(path.read_text("utf-8"))
    single_path = root / ".private/lab-write-job.json"
    if job.get("armed") is True and single_path.exists() and json.loads(single_path.read_text("utf-8")).get("armed") is True:
        raise BridgeError("conflicting_write_jobs", "单条任务和批量任务不能同时待执行。")
    if job.get("armed") is not True or job.get("target") != platform:
        return None
    if job.get("source_policy") != "direct_seed_only":
        job["armed"] = False
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), "utf-8")
        temporary.replace(path)
        raise BridgeError("invalid_fixture_source", "旧批次只可历史核对；待执行标记已清除，请使用固定原始种子生成新批次。")
    if (job.get("kind") != "cloud-matrix-batch"
            or not re.fullmatch(r"matrix-\d{8}-[A-Za-z0-9-]{1,30}", job.get("id", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", job.get("expected_account", ""))):
        raise BridgeError("invalid_matrix_job", "矩阵批次范围未确认。")
    oppo_batch_scope(job)
    spec = importlib.util.spec_from_file_location("matrix_fixture", root / "scripts/live-fixture-job.py")
    fixture_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture_module)
    job["armed"] = False
    fixture_module.write_json(path, job)
    if "target_before_fetch" in job:
        from bd20_before_scope import validate_route
        validate_route(job)
    paths = AppPaths(root / ".private/session-lab")
    store = Store(paths.database)
    notes = select_batch(job, store, root, fixture_module.fixture)
    provider = create_provider(platform, jars, paths.resources)
    runner = TaskRunner(store)
    evidence = {"kind": job["kind"], "batch_id": job["id"], "formal_acceptance": False,
                "source": notes[0].platform, "target": platform, "status": "not_started",
                "source_post_readback": "pending", "official_rendering": "pending", "items": []}
    output = root / ".private/evidence" / (job["id"] + ".json")
    scoped_vivo = platform == "vivo"
    scoped_huawei_source = notes[0].platform == "huawei" and "scoped_proof" in job["sources"][0]["from_cloud_fixture"]
    scoped_oppo_batch = job.get("oppo_scope") == OPPO_SCOPE
    scoped_other_source = scoped_oppo_batch and notes[0].platform in ("oppo", "vivo")
    if scoped_huawei_source:
        reference = job["sources"][0]["from_cloud_fixture"]["scoped_proof"]
        proof = json.loads((root / reference["path"]).read_text("utf-8"))
        evidence.update(source_scope="fixed_huawei_K2_BD2_capture", source_capture=reference,
            source_capture_time=proof["captured_finished_at"], whole_source_account_snapshot=False,
            source_original_17_integrity="pending")
    if scoped_other_source:
        reference = job["sources"][0]["from_cloud_fixture"]["scoped_proof"]
        evidence.update(source_scope="fixed_" + notes[0].platform + "_capture", source_capture=reference,
                        whole_source_account_snapshot=False)
    if scoped_oppo_batch:
        evidence["oppo_scope"] = OPPO_SCOPE
    if scoped_vivo:
        from vivo_scoped_readback import read_notes, verify_account
        evidence["comparison_scope"] = "confirmed_tool_created_ids_only"
        evidence["whole_account_snapshot"] = False

    def read_snapshot():
        runner.start("matrix_fetch", lambda ctx: fetch_snapshot(provider, store, ctx))
        runner.join()
        report = runner.current()
        if (report.status not in ("succeeded", "partial")
                or {i.code for i in report.issues} - {"source_warning"}
                or not store.snapshot(platform, provider.account_id).get("complete")):
            raise BridgeError("matrix_read_incomplete", "完整读取未确认，矩阵验证停止。")
        return {n.source_id: n for n in store.notes(platform, provider.account_id)}

    def read_vivo_scope(ids, phase, bindings):
        ids = tuple(sorted(ids))
        result = []
        runner.start("matrix_synthetic_fetch", lambda ctx: result.append(read_notes(provider, ids, ctx)))
        runner.join()
        task = runner.current()
        scope = result[0] if len(result) == 1 else None
        complete = (task.status in ("succeeded", "partial")
            and not ({i.code for i in task.issues} - {"source_warning"})
            and scope is not None and scope.complete is True and scope.account_id == job["expected_account"]
            and provider.account_id == job["expected_account"] and scope.exact_ids == ids
            and len(scope.notes) == len(ids) and {n.source_id for n in scope.notes} == set(ids)
            and all(n.platform == "vivo" and n.account_id == job["expected_account"] for n in scope.notes))
        scope_path = root / ".private/evidence" / (job["id"] + "-vivo-" + phase + "-scope.json")
        fixture_module.write_json(scope_path, {"kind": "vivo-synthetic-scoped-task", "batch_id": job["id"],
            "phase": phase, "account_id": job["expected_account"], "exact_ids": ids, "bindings": bindings,
            "scope_complete": bool(complete), "whole_account_snapshot": False, "formal_acceptance": False,
            "task": task.model_dump(mode="json"),
            "notes": [{"source_id": n.source_id, "fingerprint": n.fingerprint(),
                       "assets": [{"id": a.id, "sha256": a.sha256, "size": a.size} for a in n.attachments]}
                      for n in scope.notes] if complete else []})
        evidence[phase + "_scope_file"] = scope_path.relative_to(root).as_posix()
        evidence[phase + "_scope_sha256"] = hashlib.sha256(scope_path.read_bytes()).hexdigest()
        if not complete:
            raise BridgeError("matrix_read_incomplete", "工具样例的精确读取范围未完整确认，矩阵验证停止。")
        return {n.source_id: n for n in scope.notes}

    try:
        if (verify_account(provider) if scoped_vivo else provider.probe()) != job["expected_account"]:
            raise BridgeError("account_changed", "目标账号与批次不符。")
        configure_candidate(provider, notes, root)
        provider.preflight(notes)
        before_scope = vivo_tool_scope(store, root, fixture_module.fixture, provider.account_id) if scoped_vivo else None
        if "target_before_fetch" in job:
            from bd20_before_scope import reuse
            before, reused_before = reuse(job, store, root, provider.account_id)
            evidence["target_before_fetch"] = reused_before
        else:
            before = read_vivo_scope(before_scope["exact_ids"], "before", before_scope) if scoped_vivo else read_snapshot()
        evidence["synthetic_before_count" if scoped_vivo else "before_count"] = len(before)
        baseline_path = root / ".private/checkpoints" / (job["id"] + "-before.json")
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        fixture_module.write_json(baseline_path, {
            "target": {key: note.fingerprint() for key, note in before.items()},
            "source": {note.source_id: note.fingerprint() for note in
                       (notes if scoped_vivo or scoped_huawei_source or scoped_other_source
                        else store.notes(notes[0].platform, notes[0].account_id))},
            **({"target_before_fetch": evidence["target_before_fetch"]} if "target_before_fetch" in job else {}),
            **({"target_scope": "confirmed_tool_created_ids_only", "source_scope": "selected_fixed_direct_seeds"}
               if scoped_vivo else {}),
            **({"source_scope": "fixed_huawei_K2_BD2_capture", "source_capture": evidence["source_capture"],
                "source_capture_time": evidence["source_capture_time"], "whole_source_account_snapshot": False,
                "source_original_17_integrity": "pending"} if scoped_huawei_source else {}),
            **({"source_scope": evidence["source_scope"], "source_capture": evidence["source_capture"],
                "whole_source_account_snapshot": False} if scoped_other_source else {}),
        })
        evidence["baseline_file"] = baseline_path.relative_to(root).as_posix()
        fixture_module.write_json(output, evidence)
        runner.start("matrix_migrate", lambda ctx: migrate(notes, provider, store, ctx))
        runner.join()
        report = runner.current()
        evidence.update(status=report.status, succeeded=report.succeeded, skipped=report.skipped,
                        issues=[{"note_id": i.note_id, "code": i.code, "message": i.message} for i in report.issues])
        # Save the write outcome even if the subsequent network read fails.
        fixture_module.write_json(output, evidence)
        if scoped_vivo:
            additions = []
            for note in notes:
                key = receipt_key(note, provider)
                receipt = store.receipt(key)
                if receipt and receipt["status"] == "confirmed":
                    ids = receipt["remote_ids"]
                    if (len(ids) != 1 or not isinstance(ids[0], str) or not re.fullmatch(r"[a-f0-9]{32}", ids[0])
                            or any(item["remote_id"] == ids[0] for item in additions)):
                        raise BridgeError("invalid_synthetic_scope", "本批次的已确认回执无法绑定唯一工具笔记。")
                    additions.append({"source_id": note.source_id, "source_fingerprint": note.fingerprint(),
                                      "receipt_key": key, "remote_id": ids[0]})
            after_ids = set(before) | {item["remote_id"] for item in additions}
            after = read_vivo_scope(after_ids, "after", {"before_scope": before_scope, "current_batch": additions})
        else:
            after = read_snapshot()
        evidence["synthetic_after_count" if scoped_vivo else "after_count"] = len(after)
        unchanged = all(k in after and n.fingerprint() == after[k].fingerprint() for k, n in before.items())
        evidence["synthetic_scope_originals_unchanged" if scoped_vivo else "original_target_notes_unchanged"] = unchanged
        expected_new = set()
        for note in notes:
            receipt = store.receipt(receipt_key(note, provider))
            ids = receipt["remote_ids"] if receipt and receipt["status"] == "confirmed" else []
            expected_new.update(set(ids) - before.keys())
            restored = after.get(ids[0]) if len(ids) == 1 else None
            warnings = [i.message for i in report.issues if i.note_id == note.source_id and i.code == "format_downgrade"]
            content = restored is not None and compare_note(note, restored, warnings)
            named_group = bool(note.source_folder_name) if scoped_oppo_batch else bool(note.source_folder_id)
            group = not named_group
            if named_group and restored:
                module = __import__("note_bridge.providers." + platform + "_groups", fromlist=["folder_key"])
                group_receipt = store.receipt(module.folder_key(note, provider.account_id))
                group = (group_receipt is not None and group_receipt["status"] == "confirmed"
                         and group_receipt["remote_ids"] == [restored.source_folder_id])
            evidence["items"].append({"source_id": note.source_id, "source_fingerprint": note.fingerprint(),
                "receipt_status": receipt["status"] if receipt else "absent", "content_verified": content,
                "source_title_empty": note.title == "", "target_title_empty": restored.title == "" if restored else None,
                "group_verified": group, "status": "api_verified" if content and group else "needs_review"})
        exact_additions = after.keys() - before.keys() == expected_new
        if "target_before_fetch" in job:
            exact_additions = exact_additions and len(after) == 22 and len(expected_new) == 1
        evidence["synthetic_scope_only_confirmed_additions" if scoped_vivo else "only_confirmed_additions"] = exact_additions
        evidence["status"] = ("api_verified" if unchanged and exact_additions
            and all(item["status"] == "api_verified" for item in evidence["items"]) else "needs_review")
    except BridgeError as error:
        evidence.update(status="needs_review", code=error.code)
    finally:
        provider.close()
        fixture_module.write_json(output, evidence)
    return evidence
