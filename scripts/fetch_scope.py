"""Bind a completed lab fetch to its actual account, task and verified local files."""
import hashlib
import json
import re
from collections import Counter

from note_bridge.errors import BridgeError
from note_bridge.paths import confined


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def snapshot_binding(root, store, platform, account):
    snapshot = store.snapshot(platform, account)
    notes = store.notes(platform, account)
    if not snapshot.get("complete") or snapshot.get("count") != len(notes):
        raise BridgeError("fetch_scope_incomplete", "完整快照未确认，未建立读取范围凭据。")
    fingerprints, resources, references = {}, {}, {}
    for note in notes:
        if note.platform != platform or note.account_id != account or note.source_id in fingerprints:
            raise BridgeError("fetch_scope_mismatch", "读取快照的账号或身份不一致。")
        fingerprints[note.source_id] = note.fingerprint()
        references[note.source_id] = []
        for asset in note.attachments:
            if not asset.local_path or not re.fullmatch(r"[0-9a-f]{64}", asset.sha256 or ""):
                raise BridgeError("fetch_scope_resource", "快照附件缺少原件范围凭据。")
            resource = {"sha256": asset.sha256, "size": asset.size}
            if asset.local_path in resources:
                if resources[asset.local_path] != resource:
                    raise BridgeError("fetch_scope_resource", "同一路径附件的完整性记录冲突。")
            else:
                path = confined(root / ".private/session-lab/resources", asset.local_path)
                if (not path.is_file() or path.stat().st_size != asset.size
                        or hashlib.sha256(path.read_bytes()).hexdigest() != asset.sha256):
                    raise BridgeError("fetch_scope_resource", "读取快照的附件原件校验失败。")
                resources[asset.local_path] = resource
            references[note.source_id].append({"attachment_id": asset.id, "path": asset.local_path})
    return {"notes": fingerprints, "resources": resources, "attachment_references": references}


def task_summary(report):
    return {"id": report.id, "operation": report.operation, "status": report.status,
            "started_at": report.started_at, "finished_at": report.finished_at,
            "completed": report.completed, "total": report.total, "succeeded": report.succeeded,
            "issue_counts": dict(Counter(i.code for i in report.issues)),
            "report_sha256": digest(report.model_dump(mode="json"))}


def write_fetch_scope(root, store, platform, account, report):
    # Called only after provider.fetch completed in the worker that owns this account.
    if (report.operation != "fetch" or report.status not in ("succeeded", "partial")
            or not report.finished_at or {i.code for i in report.issues} - {"source_warning"}
            or not re.fullmatch(r"[0-9a-f]{32}", report.id)):
        raise BridgeError("fetch_scope_incomplete", "读取任务未完整结束，未建立范围凭据。")
    saved = store.task(report.id)
    if saved is None or saved.model_dump(mode="json") != report.model_dump(mode="json"):
        raise BridgeError("fetch_scope_mismatch", "读取任务与持久化结果不一致。")
    binding = snapshot_binding(root, store, platform, account)
    if not report.total == report.completed == report.succeeded == len(binding["notes"]):
        raise BridgeError("fetch_scope_mismatch", "读取任务数量与当前完整快照不一致。")
    scope = {"kind": "completed-fetch-scope", "platform": platform, "account": account,
             "formal_acceptance": False, "cloud_writes": 0, "task": task_summary(report), **binding}
    path = root / ".private/evidence" / ("fetch-" + report.id + "-scope.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(scope, stream, ensure_ascii=False, indent=2)
    return path.relative_to(root).as_posix()
