"""Huawei memo categories, grounded in the official notetag service and editor."""
import hashlib
import json
import re
import time
import uuid

from ..errors import BridgeError, WriteUncertain


def folder_key(note, account):
    identity = [note.platform, note.account_id, note.source_folder_id or note.source_folder_name, "huawei", account]
    return "folder:" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def available_name(original, occupied):
    base = re.sub(r"[\x00-\x1f]", "_", original).strip() or "导入笔记"
    for number in range(len(occupied) + 2):
        suffix = "" if number == 0 else f" ({number + 1})"
        stem = base
        while len((stem + suffix).encode("utf-16-le")) // 2 > 128:
            stem = stem[:-1]
        if stem + suffix not in occupied:
            return stem + suffix
    raise BridgeError("group_name_conflict", "无法分配不重名的华为分组名称。")


def parse_tags(payload):
    try:
        rows = payload["rspInfo"]["noteList"]
        if not isinstance(rows, list):
            raise ValueError()
        tags = []
        for row in rows:
            tag = json.loads(row["data"])
            # Current cloud responses wrap content; the official local store also uses flat records.
            if isinstance(tag, dict) and "content" in tag:
                tag = tag["content"]
            if (not isinstance(tag, dict) or type(tag.get("uuid")) not in (str, int)
                    or not str(tag["uuid"]) or not isinstance(tag.get("name"), str)
                    or type(tag.get("type")) is not int or type(tag.get("delete_flag")) is not int):
                raise ValueError()
            if tag["type"] == 2 and tag["delete_flag"] == 0:
                tags.append(tag)
        if len({str(t["uuid"]) for t in tags}) != len(tags):
            raise ValueError()
        return tags
    except (KeyError, TypeError, ValueError):
        raise BridgeError("protocol_changed", "华为分组目录无法识别，未继续迁移。") from None


def target_group(provider, note, context):
    if not note.source_folder_name:
        return "", []
    key = folder_key(note, provider.account_id)
    receipt = context.store.receipt(key)
    if receipt and receipt["status"] in ("sending", "uncertain"):
        raise WriteUncertain()
    options = {"check_cancel": context.check_cancel, "wait": context.cancelled.wait}

    def listing():
        payload = provider._json("notetag/query", {"index": 0}, **options)
        return payload, parse_tags(payload)

    tags_payload, tags = listing()
    if receipt and receipt["status"] == "confirmed":
        matches = [t for t in tags if str(t["uuid"]) in receipt["remote_ids"]]
        if len(receipt["remote_ids"]) != 1 or len(matches) != 1:
            raise BridgeError("target_group_changed", "已映射的华为分组不可用，请核对目标目录。")
        group = matches[0]
    else:
        name = available_name(note.source_folder_name, {t["name"] for t in tags})
        orders = [t.get("user_order") for t in tags]
        if any(type(order) is not int for order in orders):
            raise BridgeError("protocol_changed", "华为分组排序值无法识别，未执行创建。")
        order = max(orders) if orders else 5
        order = order if order >= 2147483647 else order + 1
        listing_payload, _ = provider._listing(**options)
        now = int(time.time() * 1000)
        content = {"color": "#fa2a2d", "create_time": now, "delete_flag": 0, "name": name,
            "type": 2, "user_order": order, "guid": "", "sort_key": "", "uuid": 0, "version": "8",
            "last_update_time": now,
            "currentNotePadVersion": uuid.uuid4().hex[:4] + "-" + str(now) + "-" + str(uuid.uuid4().int % 100000).zfill(5)}
        context.store.save_receipt(key, "sending", [])
        try:
            result = provider._json("notetag/create", {"ctagNoteInfo": listing_payload.get("ctagNoteInfo", ""),
                "ctagNoteTag": tags_payload.get("ctagNoteTag", ""), "startCursor": listing_payload.get("startCursor", ""),
                "reqInfo": {"data": json.dumps({"content": content}, ensure_ascii=False)}}, write=True, **options)
            group_id = result["rspInfo"].get("uuid")
            if type(group_id) not in (str, int) or not str(group_id) or str(group_id) == "0" or str(group_id) in {str(t["uuid"]) for t in tags}:
                raise WriteUncertain()
            context.store.save_receipt(key, "sending", [str(group_id)])
            _, fresh = listing()
            matches = [t for t in fresh if str(t["uuid"]) == str(group_id) and t["name"] == name]
            if len(matches) != 1:
                raise WriteUncertain()
            group = matches[0]
            context.store.save_receipt(key, "confirmed", [str(group_id)])
        except Exception:
            context.store.save_receipt(key, "uncertain")
            raise WriteUncertain() from None
    warnings = [] if group["name"] == note.source_folder_name else ["来源分组因目标重名或名称长度限制使用了调整后的名称，原名称已附注正文。"]
    return str(group["uuid"]), warnings
