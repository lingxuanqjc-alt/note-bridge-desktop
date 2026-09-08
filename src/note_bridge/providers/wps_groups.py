"""WPS note groups, separate from the drive container used for .wpsnote files."""
import hashlib
import json
import re
import time
import uuid

from ..errors import BridgeError, WriteUncertain


def folder_key(note, account):
    identity = [note.platform, note.account_id, note.source_folder_id or note.source_folder_name, "wps", account]
    return "folder:" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def available_name(original, occupied):
    base = re.sub(r"[\x00-\x1f]", "_", original).strip() or "导入笔记"
    for number in range(len(occupied) + 2):
        suffix = "" if number == 0 else f" ({number + 1})"
        stem = base
        # The official group-name input has maxlength=30 (UTF-16 units).
        while len((stem + suffix).encode("utf-16-le")) // 2 > 30:
            stem = stem[:-1]
        if stem + suffix not in occupied:
            return stem + suffix
    raise BridgeError("group_name_conflict", "无法分配不重名的 WPS 分组名称。")


def target_group(provider, note, context):
    if not note.source_folder_name:
        return None, []
    key = folder_key(note, provider.account_id)
    receipt = context.store.receipt(key)
    if receipt and receipt["status"] in ("sending", "uncertain"):
        raise WriteUncertain()
    options = {"check_cancel": context.check_cancel, "wait": context.cancelled.wait}

    def listing():
        data = provider._json("POST", "get/notegroup", json={"lastRequestTime": 0, "excludeInValid": True}, **options)
        rows = data.get("noteGroups")
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get("groupId"), str)
                or not r["groupId"] or not isinstance(r.get("groupName"), str)
                or type(r.get("valid")) is not int or r["valid"] not in (0, 1) for r in rows):
            raise BridgeError("protocol_changed", "WPS 分组目录无法识别，未继续迁移。")
        return [r for r in rows if r["valid"] == 1]

    rows = listing()
    if receipt and receipt["status"] == "confirmed":
        matches = [r for r in rows if r["groupId"] in receipt["remote_ids"]]
        if len(receipt["remote_ids"]) != 1 or len(matches) != 1:
            raise BridgeError("target_group_changed", "已映射的 WPS 分组不可用，请核对目标目录。")
        group = matches[0]
    else:
        name = available_name(note.source_folder_name, {r["groupName"] for r in rows})
        group_id = uuid.uuid4().hex
        if group_id in {r["groupId"] for r in rows}:
            raise BridgeError("group_id_conflict", "WPS 分组标识冲突，未执行创建。")
        context.store.save_receipt(key, "sending", [group_id])
        try:
            # Official generateNoteGroupInfo -> set/notegroup; never upsert an existing user group.
            result = provider._json("POST", "set/notegroup", json={"groupId": group_id, "groupName": name,
                "valid": 1, "updateTime": int(time.time() * 1000), "isNewGroup": True}, write=True, **options)
            if type(result.get("updateTime")) is not int or result["updateTime"] <= 0:
                raise WriteUncertain()
            matches = [r for r in listing() if r["groupId"] == group_id and r["groupName"] == name]
            if len(matches) != 1:
                raise WriteUncertain()
            group = matches[0]
            context.store.save_receipt(key, "confirmed", [group_id])
        except Exception:
            context.store.save_receipt(key, "uncertain")
            raise WriteUncertain() from None
    warnings = [] if group["groupName"] == note.source_folder_name else ["来源分组因目标重名或名称长度限制使用了调整后的名称，原名称已附注正文。"]
    return group["groupId"], warnings
