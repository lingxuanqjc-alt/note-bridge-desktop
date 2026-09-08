"""Vivo notebook creation and durable migration mappings."""
import hashlib
import json
import math
import re
import time
import uuid

from ..errors import BridgeError, WriteUncertain


def folder_key(note, account):
    identity = [note.platform, note.account_id, note.source_folder_id or note.source_folder_name, "vivo", account]
    return "folder:" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def parse_books(rows):
    if (not isinstance(rows, list) or len(rows) >= 100000 or any(not isinstance(r, dict)
            or not isinstance(r.get("guid"), str) or not r["guid"]
            or not isinstance(r.get("nameNew") or r.get("name"), str)
            or type(r.get("deleted")) is not int for r in rows)
            or len({r["guid"] for r in rows}) != len(rows)):
        raise BridgeError("protocol_changed", "vivo 文件夹目录无法识别，已停止迁移。")
    return [r for r in rows if r["deleted"] == 1 and r["guid"] not in ("-1", "-2", "0")]


def available_name(original, occupied):
    # The official dialog rejects emoji; the original name remains in the note annotation.
    base = re.sub(r"[\x00-\x1f\U0001f000-\U0001faff\u2600-\u27bf\ufe0f]", "_", original).strip() or "导入笔记"
    occupied = occupied | {"全部笔记", "未分类"}
    for number in range(len(occupied) + 2):
        suffix = "" if number == 0 else f" ({number + 1})"
        stem = base
        while len((stem + suffix).encode("utf-16-le")) // 2 > 56:
            stem = stem[:-1]
        if stem + suffix not in occupied:
            return stem + suffix
    raise BridgeError("group_name_conflict", "无法分配不重名的 vivo 文件夹名称。")


def target_group(provider, note, context):
    if not note.source_folder_name:
        return "0", []
    key = folder_key(note, provider.account_id)
    receipt = context.store.receipt(key)
    if receipt and receipt["status"] in ("sending", "uncertain"):
        raise WriteUncertain()
    options = {"check_cancel": context.check_cancel, "wait": context.cancelled.wait}
    def listing():
        return parse_books(provider._json("POST", "/noteBook/getList", {"maxEntries": 100000}, **options))
    rows = listing()
    if receipt and receipt["status"] == "confirmed":
        matches = [r for r in rows if r["guid"] in receipt["remote_ids"]]
        if len(receipt["remote_ids"]) != 1 or len(matches) != 1:
            raise BridgeError("target_group_changed", "已映射的 vivo 文件夹不可用，请核对目标目录。")
        group = matches[0]
    else:
        name = available_name(note.source_folder_name, {r.get("nameNew") or r["name"] for r in rows})
        roots = [r for r in rows if r.get("parentGuid") in (None, "-1")]
        sorts = [r.get("sort") for r in roots]
        if any(type(s) not in (int, float) or not math.isfinite(s) for s in sorts):
            raise BridgeError("protocol_changed", "vivo 文件夹排序无法识别，未执行创建。")
        sort = max(sorts, default=0) + 1
        if sort > 1e20 or (sorts and sort <= max(sorts)):
            raise BridgeError("group_sort_pending", "vivo 文件夹需要重排，未修改现有目录。")
        sync = provider._json("POST", "/sync/getSyncState", {"type": 0}, **options)
        if not isinstance(sync, dict) or type(sync.get("updateCount")) is not int:
            raise BridgeError("protocol_changed", "vivo 同步版本无法确认，未执行创建。")
        now, group_id = int(time.time() * 1000), uuid.uuid4().hex
        if group_id in {r["guid"] for r in rows}:
            raise BridgeError("group_id_conflict", "vivo 文件夹标识冲突，未执行创建。")
        group = {"guid": group_id, "deleted": 1, "name": name, "nameNew": name, "iconColor": "yellow",
                 "createTime": now, "updateTime": now, "defaultNoteBook": 0, "parentGuid": "-1", "sort": sort, "count": 0}
        context.store.save_receipt(key, "sending", [group_id])
        try:
            result = provider._json("POST", "/noteBook/create", {"lastUpdateCount": sync["updateCount"], "syncUp": [group]},
                                    write=True, **options)
            if (not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict)
                    or type(result[0].get("updateSequenceNum")) is not int
                    or result[0]["updateSequenceNum"] <= sync["updateCount"]):
                raise WriteUncertain()
            matches = [r for r in listing() if r["guid"] == group_id and (r.get("nameNew") or r["name"]) == name]
            if len(matches) != 1:
                raise WriteUncertain()
            group = matches[0]
            context.store.save_receipt(key, "confirmed", [group_id])
        except Exception:
            context.store.save_receipt(key, "uncertain")
            raise WriteUncertain() from None
    warnings = [] if (group.get("nameNew") or group["name"]) == note.source_folder_name else ["来源分组因目标重名、名称长度或 Emoji 限制使用了调整后的名称，原名称已附注正文。"]
    return group["guid"], warnings
