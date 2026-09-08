"""Flyme category identity and durable source-to-target mappings."""
import hashlib
import json
import re

from ..errors import BridgeError, WriteUncertain


def folder_key(note, account):
    identity = [note.platform, note.account_id, note.source_folder_id or note.source_folder_name, "meizu", account]
    return "folder:" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def parse_tags(data):
    rows = data.get("data")
    if (not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get("id"), str)
            or not r["id"] or not isinstance(r.get("name"), str) for r in rows)
            or len({r["id"] for r in rows}) != len(rows)):
        raise BridgeError("protocol_changed", "魅族分组目录无法识别，已停止迁移。")
    # The official menu reserves all-notes, recycle-bin and encrypted-note categories.
    return [r for r in rows if r["id"] not in ("-1", "-2", "-3")]


def available_name(original, occupied):
    base = re.sub(r"[\x00-\x1f]", "_", original).strip() or "导入笔记"
    for number in range(len(occupied) + 2):
        suffix = "" if number == 0 else f" ({number + 1})"
        stem = base
        while len((stem + suffix).encode("utf-16-le")) // 2 > 16:
            stem = stem[:-1]
        if stem + suffix not in occupied:
            return stem + suffix
    raise BridgeError("group_name_conflict", "无法分配不重名的魅族分组名称。")


def target_group(provider, note, context):
    if not note.source_folder_name:
        return "-1", []
    key = folder_key(note, provider.account_id)
    receipt = context.store.receipt(key)
    if receipt and receipt["status"] in ("sending", "uncertain"):
        raise WriteUncertain()
    options = {"check_cancel": context.check_cancel, "wait": context.cancelled.wait}
    rows = parse_tags(provider._json("gettags", **options))
    if receipt and receipt["status"] == "confirmed":
        matches = [r for r in rows if r["id"] in receipt["remote_ids"]]
        if len(receipt["remote_ids"]) != 1 or len(matches) != 1:
            raise BridgeError("target_group_changed", "已映射的魅族分组不可用，请核对目标目录。")
        group = matches[0]
    else:
        name = available_name(note.source_folder_name, {r["name"] for r in rows})
        context.store.save_receipt(key, "sending", [])
        try:
            # The official client ignores addNodeTag's value and refreshes gettags.
            provider._json("addNodeTag", params={"name": name}, mapping=False, write=True, **options)
            fresh = parse_tags(provider._json("gettags", **options))
            matches = [r for r in fresh if r["id"] not in {old["id"] for old in rows} and r["name"] == name]
            if len(matches) != 1:
                raise WriteUncertain()
            group = matches[0]
            context.store.save_receipt(key, "confirmed", [group["id"]])
        except Exception:
            context.store.save_receipt(key, "uncertain")
            raise WriteUncertain() from None
    warnings = [] if group["name"] == note.source_folder_name else ["来源分组因目标重名或名称长度限制使用了调整后的名称，原名称已附注正文。"]
    return group["id"], warnings
