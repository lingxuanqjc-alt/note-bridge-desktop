"""Select only a confirmed, unchanged cloud test note for a direction smoke test."""
import hashlib
import json
import re
from types import SimpleNamespace

from matrix_expectations import expected_blocks
from migration_content_check import contains_content

from note_bridge.errors import BridgeError
from note_bridge.operations import receipt_key
from note_bridge.paths import confined
from note_bridge.providers.huawei_groups import folder_key as huawei_folder_key


def select_source(job, store, root, fixture_builder, _seen=()):
    source = job.get("from_cloud_fixture")
    if not isinstance(source, dict) or source.get("platform") == job.get("target"):
        raise BridgeError("invalid_fixture_source", "真实来源测试必须使用不同的平台。")
    if "scoped_proof" in source:
        if source.get("platform") == "huawei" and job.get("target") in ("wps", "vivo", "xiaomi", "honor", "meizu", "oppo"):
            from huawei_scoped_source import MANIFESTS, require, verify_source
        elif source.get("platform") == "oppo" and job.get("target") in ("wps", "vivo", "xiaomi", "honor", "meizu", "huawei"):
            from oppo_scoped_source import MANIFESTS, require, verify_source
        elif source.get("platform") == "vivo" and job.get("target") == "oppo":
            from vivo_fixed_source import MANIFESTS, require, verify_source
        else:
            raise BridgeError("invalid_fixture_source", "固定来源证明不属于本次平台范围。")
        require(source.get("manifest") in MANIFESTS and "batch_manifest" not in source)
        notes = verify_source(source["scoped_proof"], store, root)
        note = notes[MANIFESTS.index(source["manifest"])]
        require((note.account_id, note.source_id, note.fingerprint(), note.display_title) ==
            (source.get("account"), source.get("source_id"), source.get("fingerprint"), job.get("title"))
            and job.get("with_images") is bool(note.attachments)
            and job.get("with_group") is bool(note.source_folder_name))
        return note
    if "batch_manifest" in source:
        return _batch_source(job, store, root, fixture_builder, _seen)
    name = source.get("manifest", "")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9-]+-job\.json", name):
        raise BridgeError("invalid_fixture_source", "来源验收记录路径不符合范围。")
    original = json.loads((root / ".private/checkpoints" / name).read_text("utf-8"))
    identifier = original.get("id", "")
    if (original.get("armed") is not False or original.get("target") != source.get("platform")
            or original.get("expected_account") != source.get("account")
            or not re.fullmatch(r"fixture-\d{8}-[A-Za-z0-9-]{1,30}", identifier)):
        raise BridgeError("invalid_fixture_source", "来源账号或原始测试记录不匹配。")
    evidence = json.loads((root / ".private/evidence" / (identifier + ".json")).read_text("utf-8"))
    if evidence.get("status") not in ("verified", "verified_with_degradation"):
        from direct_fixture_readback import verify_readback
        verify_readback(name, store, root, fixture_builder)
    target = SimpleNamespace(spec=SimpleNamespace(id=source["platform"]), account_id=source["account"])
    receipt = store.receipt(receipt_key(fixture_builder(original), target))
    return _cached_source(job, store, root, receipt, original.get("title"))


def _batch_source(job, store, root, fixture_builder, seen):
    source = job["from_cloud_fixture"]
    name, index = source.get("batch_manifest"), source.get("entry_index")
    if ("manifest" in source or not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9-]+-job\.json", name)
            or name in seen or len(seen) >= 6 or type(index) is not int):
        raise BridgeError("invalid_fixture_source", "批次来源路径、层数或样例索引无效。")
    original = json.loads((root / ".private/checkpoints" / name).read_text("utf-8"))
    identifier = original.get("id", "")
    entries = original.get("sources", [])
    if (original.get("kind") != "cloud-matrix-batch" or original.get("armed") is not False
            or original.get("target") != source.get("platform")
            or original.get("expected_account") != source.get("account")
            or not re.fullmatch(r"matrix-\d{8}-[A-Za-z0-9-]{1,30}", identifier)
            or not isinstance(entries, list) or not 0 <= index < len(entries)):
        raise BridgeError("invalid_fixture_source", "批次来源的账号或记录无法核对。")
    evidence = json.loads((root / ".private/evidence" / (identifier + ".json")).read_text("utf-8"))
    if evidence.get("batch_id") != identifier or evidence.get("target") != source["platform"]:
        raise BridgeError("invalid_fixture_source", "来源批次身份不匹配。")
    if (evidence.get("status") not in ("api_verified", "api_verified_with_degradation")
            or evidence.get("original_target_notes_unchanged") is not True
            or evidence.get("only_confirmed_additions") is not True):
        return _recovered_huawei_source(job, original, evidence, store, root, fixture_builder, seen)
    prior = select_source({**entries[index], "target": original["target"]}, store, root, fixture_builder, (*seen, name))
    matches = [item for item in evidence.get("items", []) if item.get("source_id") == prior.source_id
               and item.get("source_fingerprint") == prior.fingerprint()]
    if len(matches) != 1 or not all(matches[0].get(key) is True for key in ("content_verified", "group_verified")):
        raise BridgeError("invalid_fixture_source", "这条来源笔记未通过批次逐条核对。")
    target = SimpleNamespace(spec=SimpleNamespace(id=source["platform"]), account_id=source["account"])
    return _cached_source(job, store, root, store.receipt(receipt_key(prior, target)), entries[index].get("title"))


def _recovered_huawei_source(job, original, evidence, store, root, fixture_builder, seen):
    """Recompute two reviewed recoveries; their original failed evidence stays failed.

    The fixed huawei-{label}-source-lineage.json proof binds file hashes and every
    permitted entry's source/target identity and receipt key. It is only a binding
    record: receipts, complete snapshots, content, groups and assets are rechecked.
    """
    source = job["from_cloud_fixture"]
    name, index = source["batch_manifest"], source["entry_index"]
    scopes = {
        "meizu-huawei-AA2-job.json": ("AA2", "matrix-20260906-meizu-huawei-AA2", "meizu", (0, 1, 2), 2),
        "honor-huawei-AA6-job.json": ("AA6", "matrix-20260906-honor-huawei-AA6", "honor", (0,), 0),
    }
    if name not in scopes:
        raise BridgeError("invalid_fixture_source", "来源批次尚未通过云端回读。")
    label, batch_id, platform, indices, repaired_index = scopes[name]
    baseline_name = f".private/checkpoints/{batch_id}-before.json"
    if (source["platform"] != "huawei" or original["id"] != batch_id or index not in indices
            or evidence.get("kind") != "cloud-matrix-batch" or evidence.get("source") != platform
            or evidence.get("status") != "needs_review" or evidence.get("baseline_file") != baseline_name
            or len(original["sources"]) <= max(indices)):
        raise BridgeError("invalid_fixture_source", "该来源不属于已限定的华为恢复批次。")
    paths = {
        "manifest": f".private/checkpoints/{name}",
        "original_evidence": f".private/evidence/{batch_id}.json",
        "baseline": baseline_name,
        "recovery_evidence": f".private/evidence/huawei-{label}-recovery-repair.json",
        "recovery_baseline": f".private/checkpoints/huawei-{label}-recovery-before.json",
    }
    try:
        payloads = {key: (root / value).read_bytes() for key, value in paths.items()}
        baseline = json.loads(payloads["baseline"])
        recovery = json.loads(payloads["recovery_evidence"])
        recovery_before = json.loads(payloads["recovery_baseline"])
        proof = json.loads((root / f".private/evidence/huawei-{label}-source-lineage.json").read_text("utf-8"))
        hashes = {key: hashlib.sha256(value).hexdigest() for key, value in payloads.items()}
        if (proof.get("kind") != "huawei-recovered-source-lineage" or proof.get("batch_id") != batch_id
                or proof.get("file_sha256") != hashes or proof.get("formal_acceptance") is not False
                or type(proof.get("cloud_writes")) is not int or proof["cloud_writes"] != 0):
            raise ValueError()
        bindings = proof["items"]
        if (not isinstance(bindings, list) or len(bindings) != len(indices)
                or any(not isinstance(item, dict) or type(item.get("entry_index")) is not int for item in bindings)
                or {item["entry_index"] for item in bindings} != set(indices)):
            raise ValueError()
        if (recovery.get("kind") != f"huawei-{label}-scoped-recovery" or recovery.get("mode") != "repair"
                or recovery.get("status") != "verified_with_degradation"
                or recovery.get("content_verified") is not True
                or recovery.get("original_other_notes_unchanged") is not True
                or any(type(recovery.get(key)) is not int or recovery[key] != value
                       for key, value in {"creates": 0, "uploads": 0, "updates": 1,
                                          "existing_images": 2, "missing_images": 0}.items())):
            raise ValueError()
        before, source_before = baseline["target"], baseline["source"]
        fingerprints = recovery_before["fingerprints"]
        for values in (before, source_before, fingerprints):
            if (not isinstance(values, dict) or not values
                    or any(not isinstance(key, str) or not isinstance(value, str)
                           or not re.fullmatch(r"[0-9a-f]{64}", value) for key, value in values.items())):
                raise ValueError()
        if type(evidence.get("before_count")) is not int or evidence["before_count"] != len(before):
            raise ValueError()
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise BridgeError("invalid_fixture_source", "华为恢复的独立身份凭据或原始证据不完整。") from None

    target = SimpleNamespace(spec=SimpleNamespace(id="huawei"), account_id=original["expected_account"])
    if not store.snapshot("huawei", target.account_id).get("complete"):
        raise BridgeError("invalid_fixture_source", "华为恢复来源需要完整快照。")
    current = {note.source_id: note for note in store.notes("huawei", target.account_id)}
    if not all(key in current and current[key].fingerprint() == value for key, value in before.items()):
        raise BridgeError("fixture_source_changed", "华为恢复批次的原有笔记基线不一致。")
    selected, recovered_ids, source_accounts = {}, set(), set()
    for entry_index in indices:
        entry = original["sources"][entry_index]
        prior = select_source({**entry, "target": "huawei"}, store, root, fixture_builder, (*seen, name))
        source_accounts.add(prior.account_id)
        if prior.platform != platform or source_before.get(prior.source_id) != prior.fingerprint():
            raise BridgeError("fixture_source_changed", "华为恢复的原来源与迁入前基线不一致。")
        if len(source_accounts) != 1 or len(prior.attachments) != 2 or any(a.kind != "image" for a in prior.attachments):
            raise BridgeError("invalid_fixture_source", "华为恢复仅接受同一来源账号的已限定双图片样例。")
        key = receipt_key(prior, target)
        receipt = store.receipt(key)
        if not receipt or receipt["status"] != "confirmed" or len(receipt["remote_ids"]) != 1:
            raise BridgeError("invalid_fixture_source", "华为恢复来源尚无唯一确认回执。")
        remote_id = receipt["remote_ids"][0]
        actual = current.get(remote_id)
        binding = next(item for item in bindings if item["entry_index"] == entry_index)
        if (actual is None or actual.platform != "huawei" or actual.account_id != target.account_id
                or actual.warnings or remote_id in before or remote_id in recovered_ids
                or binding.get("source") != _source_identity(prior)
                or binding.get("target") != _source_identity(actual) or binding.get("receipt_key") != key):
            raise BridgeError("invalid_fixture_source", "华为恢复凭据的笔记身份或回执不匹配。")
        recovered_ids.add(remote_id)
        scope = {"platform": "huawei", "account": target.account_id, "source_id": remote_id,
                 "fingerprint": binding["target"]["fingerprint"]}
        _cached_source({"from_cloud_fixture": scope, "title": entry["title"],
                        "with_images": bool(prior.attachments), "with_group": bool(prior.source_folder_name)},
                       store, root, receipt, entry["title"])
        if entry_index == repaired_index:
            repair_receipt = store.receipt(key + f":{label}-scoped-recovery")
            if repair_receipt != {"status": "confirmed", "remote_ids": [remote_id]} or remote_id not in fingerprints:
                raise BridgeError("invalid_fixture_source", "华为正文修复与原始目标回执未绑定。")
            warnings = recovery.get("warnings")
        else:
            if fingerprints.get(remote_id) != actual.fingerprint():
                raise BridgeError("fixture_source_changed", "未修复条目与恢复前的指纹不一致。")
            warnings = [issue.get("message") for issue in evidence.get("issues", [])
                        if issue.get("note_id") == prior.source_id and issue.get("code") == "format_downgrade"]
        if not isinstance(warnings, list) or any(not isinstance(value, str) for value in warnings):
            raise BridgeError("invalid_fixture_source", "华为恢复的格式降级记录缺失。")
        group = store.receipt(huawei_folder_key(prior, target.account_id))
        if (not prior.source_folder_id or not actual.source_folder_id
                or group != {"status": "confirmed", "remote_ids": [actual.source_folder_id]}
                or not _huawei_recovered_content(prior, actual, warnings)):
            raise BridgeError("fixture_source_changed", "华为恢复正文、附件或分组未通过当前复核。")
        selected[entry_index] = (actual, receipt)
    if (set(fingerprints) - set(before) != recovered_ids or not set(before).issubset(fingerprints)
            or any(fingerprints[key] != value for key, value in before.items())):
        raise BridgeError("invalid_fixture_source", "华为恢复前新增范围与原批次回执不一致。")
    # Apply the caller's original requested identity, scope and asset checks too.
    _, receipt = selected[index]
    return _cached_source(job, store, root, receipt,
                          original["sources"][index]["title"])


def _source_identity(note):
    return {"platform": note.platform, "account": note.account_id,
            "source_id": note.source_id, "fingerprint": note.fingerprint()}


def _huawei_recovered_content(prior, actual, warnings):
    policy = expected_blocks(prior, "huawei", warnings)
    if policy is None:
        return False
    expected, paragraph_breaks = policy
    mapping = {}
    for asset in prior.attachments:
        matches = [item for item in actual.attachments if
                   (item.kind, item.size, item.sha256) == (asset.kind, asset.size, asset.sha256)]
        if len(matches) != 1 or not asset.sha256:
            return False
        mapping[asset.id] = matches[0].id
    if len(mapping) != len(actual.attachments) or len(set(mapping.values())) != len(mapping):
        return False
    for block in expected:
        if block.kind == "attachment":
            block.attachment_id = mapping.get(block.attachment_id)
    return (prior.display_title == actual.display_title
            and contains_content(actual.blocks, expected, paragraph_breaks=paragraph_breaks)
            and all(value.date().isoformat() in actual.plain_text
                    for value in (prior.created_at, prior.updated_at) if value))


def _cached_source(job, store, root, receipt, title):
    source = job["from_cloud_fixture"]
    if (not receipt or receipt["status"] != "confirmed" or receipt["remote_ids"] != [source.get("source_id")]
            or not store.snapshot(source["platform"], source["account"]).get("complete")):
        raise BridgeError("invalid_fixture_source", "来源回执或完整快照无法确认。")
    notes = [note for note in store.notes(source["platform"], source["account"]) if note.source_id == source["source_id"]]
    if (len(notes) != 1 or notes[0].fingerprint() != source.get("fingerprint")
            or notes[0].display_title != title or notes[0].display_title != job.get("title")
            or not notes[0].display_title.startswith("笔记互迁验收 · ")):
        raise BridgeError("fixture_source_changed", "来源测试笔记已变化或不在本次范围内，未执行迁入。")
    note = notes[0]
    if bool(note.attachments) != bool(job.get("with_images")) or bool(note.source_folder_name) != bool(job.get("with_group")):
        raise BridgeError("invalid_fixture_source", "来源图片或分组与本次验证范围不一致。")
    for asset in note.attachments:
        path = confined(root / ".private/session-lab/resources", asset.local_path or "")
        if not path.is_file() or path.stat().st_size != asset.size or hashlib.sha256(path.read_bytes()).hexdigest() != asset.sha256:
            raise BridgeError("fixture_source_changed", "来源图片原件未通过本地完整性校验。")
    return note
