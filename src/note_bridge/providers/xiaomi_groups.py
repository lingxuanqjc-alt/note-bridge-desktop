"""Xiaomi legacy folder creation with account-scoped durable mappings."""
import hashlib
import json
import re
import time

from ..errors import BridgeError, WriteUncertain


def folder_key(note, account):
    identity = [note.platform, note.account_id, note.source_folder_id or note.source_folder_name, "xiaomi", account]
    return "folder:" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def folder_listing(provider, **options):
    folders, cursor, cursors = {}, None, set()
    for _ in range(10000):
        page = provider._page(cursor, **options)
        rows = page.get("folders", [])
        if not isinstance(rows, list):
            raise BridgeError("protocol_changed", "小米文件夹目录无法识别。")
        for row in rows:
            if (not isinstance(row, dict) or type(row.get("id")) not in (str, int)
                    or not str(row["id"]) or not isinstance(row.get("subject"), str)
                    or row.get("status", "normal") not in ("normal", "alive", "deleted", "purged")):
                raise BridgeError("protocol_changed", "小米文件夹信息无法识别。")
            key = str(row["id"])
            value = {"id": key, "subject": row["subject"], "status": row.get("status", "normal")}
            if key in folders and folders[key] != value:
                raise BridgeError("snapshot_changed", "读取期间小米文件夹发生变化。")
            folders[key] = value
        if page["lastPage"]:
            return [r for r in folders.values() if r["status"] in ("normal", "alive") and r["id"] != "0"]
        cursor = page.get("syncTag")
        if cursor is None or str(cursor) in cursors:
            raise BridgeError("paging_stalled", "小米文件夹分页未继续前进。")
        cursors.add(str(cursor))
    raise BridgeError("paging_limit", "小米文件夹分页超过保护上限。")


def target_group(provider, note, context):
    if not note.source_folder_name:
        return "0", []
    key = folder_key(note, provider.account_id)
    receipt = context.store.receipt(key)
    if receipt and receipt["status"] in ("sending", "uncertain"):
        raise WriteUncertain()
    options = {"check_cancel": context.check_cancel, "wait": context.cancelled.wait}
    rows = folder_listing(provider, **options)
    if receipt and receipt["status"] == "confirmed":
        matches = [r for r in rows if r["id"] in receipt["remote_ids"]]
        if len(receipt["remote_ids"]) != 1 or len(matches) != 1:
            raise BridgeError("target_group_changed", "已映射的小米文件夹不可用，请核对目标目录。")
        group = matches[0]
    else:
        base = re.sub(r"[\x00-\x1f]", "_", note.source_folder_name).strip() or "导入笔记"
        occupied = {r["subject"] for r in rows}
        name = base
        number = 2
        while name in occupied:
            name, number = f"{base} ({number})", number + 1
        token = provider.transport.cookie("serviceToken")
        if not token:
            raise BridgeError("login_incomplete", "小米当前写入会话缺失，未执行建组。")
        current = int(time.time() * 1000)
        context.store.save_receipt(key, "sending", [])
        try:
            result = provider._json("POST", "/note/folder", data={"entry": json.dumps({"subject": name,
                "createDate": current, "modifyDate": current}, ensure_ascii=False), "serviceToken": token}, write=True, **options)
            group_id = result.get("entry", {}).get("id")
            if type(group_id) not in (str, int) or not str(group_id) or str(group_id) in {"0", *(r["id"] for r in rows)}:
                raise WriteUncertain()
            context.store.save_receipt(key, "sending", [str(group_id)])
            matches = [r for r in folder_listing(provider, **options) if r["id"] == str(group_id) and r["subject"] == name]
            if len(matches) != 1:
                raise WriteUncertain()
            group = matches[0]
            context.store.save_receipt(key, "confirmed", [str(group_id)])
        except Exception:
            context.store.save_receipt(key, "uncertain")
            raise WriteUncertain() from None
    warnings = [] if group["subject"] == note.source_folder_name else ["来源分组因目标重名或名称字符调整使用了新名称，原名称已附注正文。"]
    return group["id"], warnings
