"""Honor web folder creation with persisted source/account identity mappings."""
import hashlib
import json
import re
import uuid

from ..errors import BridgeError, WriteUncertain

DEFAULT_FOLDER = "eda801b5$0e24$4835$9288$cad0a537274a"


def folder_key(note, account):
    identity = [note.platform, note.account_id, note.source_folder_id or note.source_folder_name, "honor", account]
    return "folder:" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def parse_folders(rows):
    if (not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get("uuid"), str)
            or not r["uuid"] or not isinstance(r.get("display_name"), str)
            or type(r.get("type")) is not int for r in rows)
            or len({r["uuid"] for r in rows}) != len(rows)):
        raise BridgeError("protocol_changed", "荣耀文件夹目录无法识别，已停止迁移。")
    return [r for r in rows if r["type"] == 1 and r.get("delete_flag", 0) == 0]


def available_name(original, occupied):
    base = re.sub(r"[\x00-\x1f]", "_", original).strip() or "导入笔记"
    for number in range(len(occupied) + 2):
        suffix = "" if number == 0 else f" ({number + 1})"
        stem = base
        while len((stem + suffix).encode("utf-16-le")) // 2 > 50:
            stem = stem[:-1]
        if stem + suffix not in occupied:
            return stem + suffix
    raise BridgeError("group_name_conflict", "无法分配不重名的荣耀文件夹名称。")


def target_group(provider, note, context):
    if not note.source_folder_name:
        return DEFAULT_FOLDER, []
    key = folder_key(note, provider.account_id)
    receipt = context.store.receipt(key)
    if receipt and receipt["status"] in ("sending", "uncertain"):
        raise WriteUncertain()
    options = {"check_cancel": context.check_cancel, "wait": context.cancelled.wait}
    def listing():
        return parse_folders(provider._json("POST", "notepad/note/initDataBase/getFolderList", **options))
    rows = listing()
    if receipt and receipt["status"] == "confirmed":
        matches = [r for r in rows if r["uuid"] in receipt["remote_ids"] and r["uuid"] != DEFAULT_FOLDER]
        if len(receipt["remote_ids"]) != 1 or len(matches) != 1:
            raise BridgeError("target_group_changed", "已映射的荣耀文件夹不可用，请核对目标目录。")
        group = matches[0]
    else:
        name = available_name(note.source_folder_name, {r["display_name"] for r in rows})
        orders = [r.get("user_order") for r in rows if r.get("parent_uuid") == "presetFolder"]
        if any(type(order) is not int for order in orders):
            raise BridgeError("protocol_changed", "荣耀文件夹排序无法识别，未执行创建。")
        group_id = uuid.uuid4().hex
        if group_id in {r["uuid"] for r in rows}:
            raise BridgeError("group_id_conflict", "荣耀文件夹标识冲突，未执行创建。")
        group = {"uuid": group_id, "display_name": name, "color": "#fff2b500", "parent_uuid": "presetFolder",
                 "user_order": max(orders, default=-1) + 1, "type": 1}
        context.store.save_receipt(key, "sending", [group_id])
        try:
            # Official saveFolder's web branch removes timestamps and posts a new UUID.
            provider._json("POST", "notepad/note/folder/update", json=[group], write=True, **options)
            matches = [r for r in listing() if r["uuid"] == group_id and r["display_name"] == name]
            if len(matches) != 1:
                raise WriteUncertain()
            group = matches[0]
            context.store.save_receipt(key, "confirmed", [group_id])
        except Exception:
            context.store.save_receipt(key, "uncertain")
            raise WriteUncertain() from None
    warnings = [] if group["display_name"] == note.source_folder_name else ["来源分组因目标重名或名称长度限制使用了调整后的名称，原名称已附注正文。"]
    return group["uuid"], warnings
