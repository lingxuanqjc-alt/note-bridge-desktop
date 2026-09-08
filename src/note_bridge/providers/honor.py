"""Current Honor cloud notebook read protocol, observed on the official web app.

Evidence: portal/note/main.2b996175.js and assets/login-NwG2TXMn.js, 2026-09-05.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from ..errors import BridgeError, WriteUncertain
from ..models import NoteDocument, PlatformId, account_fingerprint
from ..richtext import parse_html
from ..tasks import TaskContext
from .base import SPECS, CreatedNote, Provider, Snapshot
from .honor_files import HonorFiles, image_data
from .honor_html import encode, normalize
from .transport import Transport
from .xiaomi import timestamp


def parse_entry(entry: dict, account: str, folders: dict[str, str], assets=None) -> NoteDocument:
    if not isinstance(entry, dict) or not entry.get("uuid") or not isinstance(entry.get("html_content"), str):
        raise BridgeError("protocol_changed", "荣耀笔记正文结构无法识别。")
    if entry.get("lock_status") not in (None, 0):
        raise BridgeError("encrypted_note", "这条荣耀笔记已锁定，尚未完成解锁读取验证。")
    if entry.get("delete_flag") not in (None, 0):
        raise BridgeError("snapshot_changed", "这条荣耀笔记在读取时已被移入回收站。")
    # Official NoteType.SUPER = 2; type 1 is proprietary handwriting, not text.
    if entry.get("type") != 2:
        raise BridgeError("proprietary_content", "荣耀手写或证件笔记尚未完成转换验证。")
    markup, warnings = normalize(entry["html_content"])
    assets = assets or []
    blocks, clean_warnings = parse_html(markup, {"cid:" + asset.id: asset.id for asset in assets})
    warnings = list(dict.fromkeys(warnings + clean_warnings))
    if (entry.get("attachments") or entry.get("has_attach") or entry.get("has_attachment") or entry.get("first_attach_uuid")) and not assets:
        warnings.append("这条荣耀笔记包含尚未下载的附件，当前正文可能不完整。")
    folder = entry.get("folder_uuid")
    return NoteDocument(
        platform=PlatformId.HONOR,
        account_id=account,
        source_id=str(entry["uuid"]),
        title=entry.get("title") or "",
        blocks=blocks,
        attachments=assets,
        warnings=warnings,
        source_folder_id=folder,
        source_folder_name=folders.get(folder),
        created_at=timestamp(entry.get("create_time")),
        updated_at=timestamp(entry.get("modify_time")),
    )


class HonorProvider(Provider):
    spec = SPECS[PlatformId.HONOR]
    read_supported = True
    # Controlled write/readback supports the PNG/JPEG developer preview.
    write_supported = True
    images_supported = True

    def __init__(self, transport: Transport, resources: Path):
        self.transport, self.resources = transport, resources
        self._files = HonorFiles(self)

    def _json(self, method, path, **kwargs):
        csrf = self.transport.cookie("hc_CSRF_Token")
        if not csrf:
            raise BridgeError("login_incomplete", "请在荣耀官方窗口完成登录并进入笔记。")
        payload = self.transport.json(
            method,
            "/portal/" + path,
            headers={"Csrftoken": csrf, "x-hn-traceId": uuid.uuid4().hex, "Device-Type": "1"},
            **kwargs,
        )
        if path == "notepad/file/preCreateFile" and method == "POST" and kwargs.get("write"):
            if payload.get("code") in (0, 40053):
                # 40053's already-present content is independently checked before upload/association.
                return {"existing_file": payload["code"] == 40053}
        if payload.get("code") != 0 or "data" not in payload:
            if kwargs.get("write"):
                if payload.get("code") == 40050:
                    raise BridgeError("cloud_space_full", "荣耀云空间不足，未确认创建笔记。")
                raise WriteUncertain()
            raise BridgeError("platform_response", "荣耀云服务未确认请求成功，请检查验证状态。")
        return payload["data"]

    def probe(self):
        data = self._json("GET", "user/info")
        if not isinstance(data, dict) or not isinstance(data.get("userId"), (str, int)) or not data["userId"]:
            raise BridgeError("login_incomplete", "荣耀账号身份未能确认。")
        self._json("GET", "notepad/note/count")
        self.account_id = account_fingerprint(self.spec.id, str(data["userId"]))
        return self.account_id

    def fetch(self, context: TaskContext):
        if not self.account_id:
            raise BridgeError("login_required", "请先登录荣耀账号。")
        kwargs = {"check_cancel": context.check_cancel, "wait": context.cancelled.wait}
        folders_data = self._json("POST", "notepad/note/initDataBase/getFolderList", **kwargs)
        if not isinstance(folders_data, list) or any(
            not isinstance(row, dict) or not row.get("uuid") for row in folders_data
        ):
            raise BridgeError("protocol_changed", "荣耀分组列表结构无法识别。")
        folders = {row["uuid"]: row.get("display_name", "") for row in folders_data}
        count = self._json("GET", "notepad/note/count", **kwargs)
        if not isinstance(count, dict) or type(count.get("total")) is not int or count["total"] < 0:
            raise BridgeError("protocol_changed", "荣耀笔记总数无法识别。")
        rows = self._json(
            "POST",
            "notepad/note/util/getNoteList",
            json={"queryType": 1, "category": "total", "searchContent": ""},
            **kwargs,
        )
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not row.get("uuid") for row in rows
        ):
            raise BridgeError("protocol_changed", "荣耀笔记目录结构无法识别。")
        keys = [row["uuid"] for row in rows]
        if len(set(keys)) != len(keys) or len(keys) != count["total"]:
            raise BridgeError("pagination_incomplete", "荣耀目录条数与平台总数不一致，未保存不完整目录。")
        context.update(total=len(keys), stage="正在读取荣耀笔记。")
        notes, complete = [], True
        for index, key in enumerate(keys):
            details = self._json("POST", "notepad/noteDetail", json={"noteIds": [key]}, **kwargs)
            if (
                not isinstance(details, list)
                or len(details) != 1
                or not isinstance(details[0], dict)
                or details[0].get("uuid") != key
            ):
                raise BridgeError("detail_mismatch", "荣耀笔记详情与请求标识不一致。")
            try:
                assets = []
                try:
                    assets = self._files.download(details[0], context)
                except BridgeError as error:
                    if error.code in ("cancelled", "session_expired", "request_forbidden", "network_error", "rate_limited"):
                        raise
                    complete = False
                    context.issue(error.code, error.message, key)
                note = parse_entry(details[0], self.account_id, folders, assets)
                if note.warnings:
                    complete = False
                notes.append(note)
            except BridgeError as error:
                if error.code in ("cancelled", "session_expired", "request_forbidden", "network_error", "rate_limited"):
                    raise
                complete = False
                context.issue(error.code, error.message, key)
            context.update(completed=index + 1, succeeded=len(notes))
        original_account = self.account_id
        if self.probe() != original_account:
            raise BridgeError("account_changed", "读取期间荣耀账号发生变化，已停止保存。")
        final_count = self._json("GET", "notepad/note/count", **kwargs)
        if final_count.get("total") != len(keys):
            raise BridgeError("snapshot_changed", "读取过程中荣耀笔记数量发生变化，请重新获取。")
        return Snapshot(notes, complete)

    def validate_notes(self, notes: list[NoteDocument]):
        super().validate_notes(notes)
        for note in notes:
            identities = {asset.id: asset.id for asset in note.attachments}
            if len(identities) != len(note.attachments):
                raise BridgeError("attachment_mismatch", "荣耀图片标识重复，未开始上传。")
            encode(note, identities if note.attachments else None)
            for asset in note.attachments:
                image_data(asset, self.resources)

    def create(self, note: NoteDocument, context: TaskContext):
        self.preflight([note])
        if not self.account_id:
            raise BridgeError("login_required", "请先登录目标荣耀账号。")
        body, warnings = encode(note, {a.id: a.id for a in note.attachments} if note.attachments else None)
        from .honor_groups import target_group
        group_id, group_warnings = target_group(self, note, context)
        current, guid = int(time.time() * 1000), uuid.uuid4().hex
        entry = {"uuid": guid, "folder_uuid": group_id, "type": 2,
                 "title": note.display_title, "title_type": "edit", "html_content": body,
                 "summary": note.plain_text[:100], "search_content": note.plain_text,
                 "create_time": current, "modify_time": current, "slate_modify_time": current,
                 "slate_content": "", "is_top": 0, "lock_status": 0, "favorite": 0, "background": 0,
                 "has_attach": 0, "first_attach_uuid": "", "has_todo": int(any(b.kind == "todo" for b in note.blocks)),
                 "dirty": 1, "update": False}
        context.record_remote_ids([guid])
        def save():
            result = self._json("POST", "notepad/noteSave", json=[entry], write=True,
                                check_cancel=context.check_cancel, wait=context.cancelled.wait)
            if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict) or result[0].get("uuid") != guid:
                raise WriteUncertain()
            if isinstance(result[0].get("guid"), str) and result[0]["guid"]:
                entry["guid"] = result[0]["guid"]
        if not note.attachments:
            save()
            return CreatedNote([guid], warnings + group_warnings)
        self._files._transport()
        skeleton = note.model_copy(update={"attachments": [], "blocks": [b for b in note.blocks if b.kind != "attachment"]})
        entry["html_content"] = encode(skeleton)[0]
        save()
        # From here a new target note exists; failures must retain its ID and block retries.
        try:
            container, images, records = uuid.uuid4().hex, {}, []
            for asset in note.attachments:
                images[asset.id], uploaded = self._files.upload(asset, guid, container, context)
                records.extend(uploaded)
            body, warnings = encode(note, images)
            entry.update(html_content=body, attachments=records, unstruct_guid=container,
                         has_attach=1, first_attach_uuid=records[0]["uuid"],
                         first_attach_filename=records[0]["filename"], update=True)
            save()
        except BridgeError as error:
            if not isinstance(error, WriteUncertain):
                context.issue(error.code, error.message, note.source_id)
            raise WriteUncertain() from None
        return CreatedNote([guid], warnings + group_warnings)

    def close(self):
        self._files.close()
        self.transport.close()
        super().close()
