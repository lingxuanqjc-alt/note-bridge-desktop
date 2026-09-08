"""Durable source-folder mapping using the official OPPO add-group contract."""
import hashlib
import json
import re

from ..errors import BridgeError, WriteUncertain

DEFAULT_GROUP = "00000000_0000_0000_0000_000000000000"


def folder_key(note, account):
    identity = [note.platform, note.account_id, note.source_folder_id or note.source_folder_name, "oppo", account]
    return "folder:" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def available_name(original, occupied):
    base = re.sub(r'[\\/:?*"<>|\x00-\x1f]', "_", original).strip() or "导入笔记"
    for number in range(len(occupied) + 2):
        suffix = "" if number == 0 else f" ({number + 1})"
        stem = base
        while len((stem + suffix).encode("utf-16-le")) // 2 > 50:
            stem = stem[:-1]
        candidate = stem + suffix
        if candidate not in occupied:
            return candidate
    raise BridgeError("group_name_conflict", "无法分配不重名的目标分组。")


def target_group(provider, note, context):
    if not note.source_folder_name:
        return DEFAULT_GROUP, []
    key = folder_key(note, provider.account_id)
    receipt = context.store.receipt(key)
    if receipt and receipt["status"] in ("sending", "uncertain"):
        raise WriteUncertain()
    options = {"check_cancel": context.check_cancel, "wait": context.cancelled.wait}

    def listing():
        rows = provider._json("/web/note/v2/group-list-new", **options)
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get("groupGuid"), str)
                                             or not isinstance(r.get("groupName"), str) for r in rows):
            raise BridgeError("protocol_changed", "OPPO 分组目录结构无法识别，未继续迁移。")
        return rows

    rows = listing()
    if receipt and receipt["status"] == "confirmed":
        ids = receipt["remote_ids"]
        matches = [r for r in rows if r["groupGuid"] in ids]
        if len(ids) != 1 or len(matches) != 1:
            raise BridgeError("target_group_changed", "已映射的 OPPO 分组已不可用，请核对目标目录。")
        group = matches[0]
    else:
        name = available_name(note.source_folder_name, {r["groupName"] for r in rows})
        context.store.save_receipt(key, "sending")
        try:
            # Official GroupDialog: groupName plus JSON extra containing pureCover and cover.
            result = provider._json("/web/note/v2/add-group", {"groupName": name,
                "extra": json.dumps({"pureCover": "img_cover_yellow", "cover": "img_cover_1"})}, write=True, **options)
            if not isinstance(result, dict) or not isinstance(result.get("groupGuid"), str) or not result["groupGuid"]:
                raise WriteUncertain()
            group_id = result["groupGuid"]
            context.store.save_receipt(key, "sending", [group_id])
            if group_id in {r["groupGuid"] for r in rows}:
                raise WriteUncertain()
            matches = [r for r in listing() if r["groupGuid"] == group_id and r["groupName"] == name]
            if len(matches) != 1:
                raise WriteUncertain()
            group = matches[0]
            context.store.save_receipt(key, "confirmed", [group_id])
        except Exception:
            context.store.save_receipt(key, "uncertain")
            raise WriteUncertain() from None
    warnings = [] if group["groupName"] == note.source_folder_name else ["来源分组因目标重名或名称限制使用了调整后的名称，原名称已附注正文。"]
    return group["groupGuid"], warnings
