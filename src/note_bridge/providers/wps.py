"""Independent WPS web codec and read API, based on official index.37c12598.js.

The bootstrap constant and IV below are public protocol constants, not user credentials.
Account-specific key material from get/web-config is held in this object's memory only.
"""

from __future__ import annotations

import base64
import hashlib
import re
import uuid
from pathlib import Path
from urllib.parse import quote, unquote

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from ..errors import BridgeError, WriteUncertain
from ..exporter import stamp
from ..models import Attachment, Block, NoteDocument, PlatformId, Span, account_fingerprint
from ..richtext import safe_link
from ..tasks import TaskContext
from .base import SPECS, CreatedNote, Provider, Snapshot
from .transport import Transport
from .wps_files import WpsFiles, image_bytes
from .xiaomi import timestamp

_IV = b"5150956153345366"
_BOOTSTRAP = "H9n&S@oGohGpV6d7*"


class WpsCodec:
    def __init__(self, encrypted_password: str):
        if not isinstance(encrypted_password, str) or not encrypted_password:
            raise BridgeError("protocol_changed", "WPS 未提供正文解码配置。")
        password = self._decrypt(unquote(encrypted_password), hashlib.md5(_BOOTSTRAP.encode()).digest())
        if not password:
            raise BridgeError("decode_failed", "WPS 正文解码配置无法确认。")
        self._key = hashlib.md5(password.encode("utf-8")).digest()

    @staticmethod
    def _decrypt(encoded: str, key: bytes) -> str:
        try:
            data = base64.b64decode(encoded, validate=True)
            return unpad(AES.new(key, AES.MODE_CBC, _IV).decrypt(data), 16).decode("utf-8")
        except (ValueError, UnicodeError, TypeError):
            raise BridgeError("decode_failed", "WPS 正文未能正确解码，已停止转换。") from None

    def decrypt(self, encoded: str) -> str:
        return self._decrypt(encoded, self._key) if encoded else ""

    def encrypt(self, text: str) -> str:
        return base64.b64encode(
            AES.new(self._key, AES.MODE_CBC, _IV).encrypt(pad(text.encode("utf-8"), 16))
        ).decode("ascii")

    def close(self):
        self._key = b""


def inline_spans(text: str, flags=None) -> list[Span]:
    """Parse WPS's escaped star/tilde dialect; tilde denotes underline here."""
    flags = flags or {}
    tokens = list(re.finditer(r"\\.|\*{1,3}|~", text))
    spans, cursor, index = [], 0, 0
    while index < len(tokens):
        token = tokens[index]
        if token.start() < cursor:
            index += 1
            continue
        if token.start() > cursor:
            spans.append(Span(text=text[cursor : token.start()], **flags))
        mark = token.group()
        if mark.startswith("\\"):
            spans.append(Span(text=mark[1:], **flags))
            cursor = token.end()
            index += 1
            continue
        end_index = next((i for i in range(index + 1, len(tokens)) if tokens[i].group() == mark), None)
        if end_index is None:
            spans.append(Span(text=mark, **flags))
            cursor = token.end()
            index += 1
            continue
        added = (
            {"bold": True, "italic": True}
            if mark == "***"
            else {{"**": "bold", "*": "italic", "~": "underline"}[mark]: True}
        )
        spans.extend(inline_spans(text[token.end() : tokens[end_index].start()], {**flags, **added}))
        cursor = tokens[end_index].end()
        index = end_index + 1
    if cursor < len(text):
        spans.append(Span(text=text[cursor:], **flags))
    return spans


def decode_body(body: str) -> tuple[list[Block], list[Attachment]]:
    blocks, assets = [], []
    lines = body.split("\n")
    if lines and not lines[-1]:
        lines.pop()
    for line in lines:
        image = re.fullmatch(r"!\[.*\]\(([^)]*)\)\{w:[0-9]+;h:[0-9]+\}", line)
        audio = re.fullmatch(r"!\[audio\]\(([^)]*)\)\{[^}]*\}", line)
        if image or audio:
            key = (image or audio)[1].replace("-", "/")
            asset_id = hashlib.sha256(key.encode()).hexdigest()
            assets.append(
                Attachment(id=asset_id, name=Path(key).name or asset_id, kind="image" if image else "audio", download_url=key)
            )
            blocks.append(Block(kind="attachment", attachment_id=asset_id))
            continue
        if re.match(r"!\[.*\]\(.*\)\{", line):
            raise BridgeError("proprietary_content", "WPS 存在尚未识别的附件区块。")
        kind, level, ordered, checked = "paragraph", 1, False, False
        if line.startswith(("- [ ] ", "- [x] ")):
            kind, checked, line = "todo", line.startswith("- [x] "), line[6:]
        if line.startswith("### "):
            kind, level, line = "heading", 3, line[4:]
        elif line.startswith("## "):
            kind, level, line = "heading", 2, line[3:]
        elif line.startswith("0. "):
            kind, ordered, line = "list", True, line[3:]
        elif line.startswith("* "):
            kind, line = "list", line[2:]
        blocks.append(
            Block(kind=kind, level=level, ordered=ordered, checked=checked, spans=inline_spans(line))
        )
    return blocks, assets


def encode_body(note: NoteDocument, images=None) -> tuple[str, list[str]]:
    """Encode the official WPS line dialect; unsupported styles remain explicit losses."""
    lines, warnings = [], []
    if images is not None and (set(images) != {a.id for a in note.attachments}
                              or set(images) != {b.attachment_id for b in note.blocks if b.kind == "attachment"}):
        raise BridgeError("attachment_mismatch", "WPS 图片与正文引用不一致，未开始上传。")

    def escaped(text):
        return re.sub(r"([\\*~#\[\]0])", r"\\\1", text)

    for block in note.blocks:
        if block.kind == "attachment" and images is not None:
            key, (width, height) = images[block.attachment_id]
            lines.append(f"![]({key}){{w:{width};h:{height}}}")
            continue
        text = ""
        for span in block.spans:
            value = escaped(span.text)
            for enabled, mark in ((span.bold, "**"), (span.italic, "*"), (span.underline, "~")):
                if enabled:
                    value = mark + value + mark
            if span.strike or span.highlight or span.code:
                warnings.append("WPS 便签不保留删除线、高亮、行内代码或超链接样式，已保留文字。")
            if span.link:
                address = safe_link(span.link)
                if address is None:
                    raise BridgeError("unsupported_link", "WPS 链接地址无法安全转换，未执行迁入。")
                if span.text != address:
                    value += escaped(" (" + address + ")")
                warnings.append("WPS 超链接已转换为链接文字及明文地址，原链接样式未保留。")
            text += value
        prefix = ""
        if block.kind == "todo":
            prefix = "- [x] " if block.checked else "- [ ] "
        elif block.kind == "list":
            prefix = "0. " if block.ordered else "* "
            if block.level != 1:
                warnings.append("WPS 多层列表已转换为单层列表。")
        elif block.kind == "heading":
            level = 2 if block.level <= 2 else 3
            prefix = "#" * level + " "
            if level != block.level:
                warnings.append("WPS 标题级别已调整为二级或三级标题。")
        elif block.kind == "table":
            text = escaped("\n".join("\t".join(row) for row in block.rows))
            warnings.append("WPS 表格已按行和制表符转换为文字。")
        elif block.kind == "divider":
            text = "────────"
            warnings.append("WPS 分隔线已转换为文字。")
        elif block.kind in ("quote", "code"):
            warnings.append("WPS 引用或代码区块已转换为普通文字。")
        elif block.kind == "attachment":
            raise BridgeError("attachment_upload_pending", "WPS 附件上传尚未完成验证，未执行创建。")
        if "\n" in text:
            warnings.append("WPS 区块内换行已转换为独立段落，跨行样式需核对。")
        lines.append(prefix + text)
    lines.extend(["来源笔记时间", escaped(stamp(note))])
    if note.source_folder_name:
        lines.append("来源文件夹：" + escaped(note.source_folder_name))
    return "\n".join(lines) + "\n", list(dict.fromkeys(warnings))


class WpsProvider(Provider):
    spec = SPECS[PlatformId.WPS]
    read_supported = True
    # Controlled write/readback supports the PNG/JPEG developer preview.
    write_supported = True
    images_supported = True

    def __init__(self, transport: Transport, identity: Transport, resources: Path, drive: Transport | None = None):
        self.transport, self.identity, self.resources, self.drive = transport, identity, resources, drive
        self._uid = None
        self._codec = None
        self._files = WpsFiles(self)

    def _json(self, method, path, **kwargs):
        kwargs.setdefault("headers", {"Origin": "https://note.wps.cn", "Referer": "https://note.wps.cn/"})
        data = self.transport.json(method, "/notesvr/" + path, **kwargs)
        if data.get("errorCode") or data.get("result") == "userNotLogin":
            if kwargs.get("write"):
                raise WriteUncertain()
            raise BridgeError("platform_response", "WPS 未确认请求成功，请检查登录与云空间。")
        return data

    def probe(self):
        data = self.identity.json("GET", "/api/v3/mine", params={"attrs": "profile"})
        uid = data.get("userid")
        if not isinstance(uid, (str, int)) or isinstance(uid, bool) or not uid:
            raise BridgeError("login_incomplete", "请在 WPS 官方窗口进入便签后检查账号。")
        self._uid = str(uid)
        self.transport.session.headers["X-user-key"] = self._uid
        config = self._json("POST", "get/web-config", json={"test": "test"})
        self._codec = WpsCodec(config.get("aesPassword"))
        self.account_id = account_fingerprint(self.spec.id, self._uid)
        return self.account_id

    def fetch(self, context: TaskContext):
        if not self.account_id or not self._codec:
            raise BridgeError("login_required", "请先登录 WPS 账号。")
        kwargs = {"check_cancel": context.check_cancel, "wait": context.cancelled.wait}
        group_data = self._json(
            "POST", "get/notegroup", json={"lastRequestTime": 0, "excludeInValid": True}, **kwargs
        )
        groups = group_data.get("noteGroups")
        if not isinstance(groups, list) or any(
            not isinstance(g, dict) or not g.get("groupId") for g in groups
        ):
            raise BridgeError("protocol_changed", "WPS 分组目录无法识别。")
        folders = {str(g["groupId"]): g.get("groupName") or "" for g in groups}
        entries, seen = [], set()
        # Home, group and reminder views partition the current WPS web notebook.
        scopes = [("home", None), *(("group", key) for key in folders), ("remind", None)]
        context.update(stage="正在读取 WPS 便签目录。")
        for scope, group in scopes:
            scope_seen, offset = set(), 0
            for _ in range(10000):
                if scope == "home":
                    data = self._json(
                        "GET",
                        f"v2/user/{quote(self._uid, safe='')}/home/startindex/{offset}/rows/50/notes",
                        **kwargs,
                    )
                else:
                    params = {"uid": self._uid, "rows": 50, "startIndex": offset}
                    if scope == "group":
                        params["groupId"] = group
                    else:
                        params.update(remindStartTime=-62135596800000, remindEndTime=253402300800000)
                    data = self._json("POST", "web/getnotes/" + scope, json=params, **kwargs)
                rows = data.get("webNotes")
                if not isinstance(rows, list):
                    raise BridgeError("protocol_changed", "WPS 分页响应缺少笔记列表。")
                for row in rows:
                    if not isinstance(row, dict) or not row.get("noteId"):
                        raise BridgeError("protocol_changed", "WPS 笔记标识缺失。")
                    key = str(row["noteId"])
                    if key in scope_seen:
                        raise BridgeError("pagination_repeated", "WPS 分页返回重复便签，已停止获取。")
                    scope_seen.add(key)
                    if key not in seen:
                        seen.add(key)
                        entries.append(row)
                if len(rows) < 50:
                    break
                offset += len(rows)
            else:
                raise BridgeError("pagination_limit", "WPS 分页超过上限，已停止获取。")
        notes, complete = [], True
        context.update(total=len(entries), stage="正在读取 WPS 便签正文。")
        for index, row in enumerate(entries):
            key = str(row["noteId"])
            data = self._json("POST", "get/notebody", json={"noteIds": [key]}, **kwargs)
            bodies = data.get("noteBodies")
            if not isinstance(bodies, list) or len(bodies) != 1 or bodies[0].get("noteId") != key:
                raise BridgeError("detail_mismatch", "WPS 便签详情与请求标识不一致。")
            detail = bodies[0]
            try:
                if detail.get("bodyType") not in (0, 1):
                    raise BridgeError(
                        "object_storage_pending", "这条 WPS 便签采用独立对象存储，读取尚未完成验证。"
                    )
                if detail.get("valid") == 2:
                    raise BridgeError("snapshot_changed", "这条 WPS 便签已被移入回收站。")
                title = self._codec.decrypt(row.get("title") or "")
                summary = self._codec.decrypt(row.get("summary") or "")
                body = self._codec.decrypt(detail.get("body") or "")
                if detail["bodyType"] == 1:
                    encoded = self._files.download_body(body, context)
                    body = self._codec.decrypt(base64.b64encode(encoded).decode("ascii"))
                blocks, assets = decode_body(body)
                note = NoteDocument(
                    platform=self.spec.id,
                    account_id=self.account_id,
                    source_id=key,
                    title=title or summary.split("\n", 1)[0],
                    blocks=blocks,
                    attachments=assets,
                    source_folder_id=row.get("groupId"),
                    source_folder_name=folders.get(str(row.get("groupId"))),
                    created_at=timestamp(row.get("createTime")),
                    updated_at=timestamp(detail.get("contentUpdateTime")),
                )
                if assets:
                    for asset in assets:
                        try:
                            self._files.download(asset, asset.download_url, context)
                        except BridgeError as error:
                            if error.code == "cancelled":
                                raise
                            complete = False
                            context.issue(error.code, error.message, key)
                            note.warnings.append("WPS 附件下载未完成。")
                notes.append(note)
            except BridgeError as error:
                if error.code in ("cancelled", "session_expired", "request_forbidden", "network_error", "rate_limited"):
                    raise
                complete = False
                context.issue(error.code, error.message, key)
            context.update(completed=index + 1, succeeded=len(notes))
        original_account = self.account_id
        if self.probe() != original_account:
            raise BridgeError("account_changed", "读取期间 WPS 账号发生变化，已停止保存。")
        return Snapshot(notes, complete)

    def validate_notes(self, notes):
        super().validate_notes(notes)
        for note in notes:
            identities = {asset.id: ("0" * 32, image_bytes(asset, self.resources)[1]) for asset in note.attachments}
            if len(identities) != len(note.attachments):
                raise BridgeError("attachment_mismatch", "WPS 图片标识重复，未开始上传。")
            body, _ = encode_body(note, identities if note.attachments else None)
            if len(body.encode("utf-8")) > 104857600:
                raise BridgeError("body_too_large", "WPS 正文超过本地 100 MB 处理上限，未执行创建。")

    def create(self, note: NoteDocument, context: TaskContext) -> CreatedNote:
        self.preflight([note])
        if not self.account_id or not self._codec or not self.drive:
            raise BridgeError("login_required", "请先登录目标 WPS 账号。")
        from .wps_groups import target_group
        note_group_id, group_warnings = target_group(self, note, context)
        body, warnings = encode_body(note, {a.id: ("0" * 32, (0, 0)) for a in note.attachments} if note.attachments else None)
        kwargs = {"check_cancel": context.check_cancel, "wait": context.cancelled.wait}
        group = self.drive.json("GET", "/api/v3/groups/special", **kwargs)
        if type(group.get("id")) is not int or group["id"] <= 0:
            raise BridgeError("protocol_changed", "WPS 个人目录标识无法确认，未执行创建。")
        guid = uuid.uuid4().hex
        context.record_remote_ids([guid])
        drive_written = False
        try:
            file = self.drive.json("POST", f"/api/v3/groups/{group['id']}/files/new_empty", json={
                "groupid": group["id"], "parentid": 0, "store": "wps_note", "storeid": guid,
                "name": note.display_title + ".wpsnote", "add_name_index": True, "parent_path": ["WPS便签"],
            }, headers={"Origin": "https://note.wps.cn", "Referer": "https://note.wps.cn/"}, write=True, **kwargs)
            drive_written = True
            if not isinstance(file.get("id"), (str, int)) or not file["id"]:
                raise WriteUncertain()
            context.record_resource(str(file["id"]), "uploaded")
            info = self._json("POST", "set/noteinfo", json={
                "noteId": guid, "star": 0, "remindTime": 0, "remindType": 0,
                **({"groupId": note_group_id} if note_group_id else {}),
            }, write=True, **kwargs)
            if type(info.get("infoVersion")) is not int or info["infoVersion"] <= 0:
                raise WriteUncertain()
            if note.attachments:
                images = {asset.id: self._files.upload(asset, context) for asset in note.attachments}
                body, warnings = encode_body(note, images)
            # The official client stores raw AES ciphertext past 10,240 UTF-16 units.
            body_type = int(len(body.encode("utf-16-le")) // 2 >= 10240)
            if body_type:
                encrypted_bytes = base64.b64decode(self._codec.encrypt(body), validate=True)
                body = self._files.upload_body(guid, encrypted_bytes, context)
            content = self._json("POST", "set/notecontent", json={
                "noteId": guid, "title": self._codec.encrypt(""),
                "summary": self._codec.encrypt(note.display_title + "\n" + note.plain_text[:100]),
                "body": self._codec.encrypt(body), "bodyType": body_type, "localContentVersion": 0, "thumbnail": None,
            }, write=True, **kwargs)
            if type(content.get("contentVersion")) is not int or content["contentVersion"] <= 0:
                raise WriteUncertain()
        except BridgeError:
            if drive_written:
                # Even a later 4xx or cancellation can leave the new cloud file behind.
                raise WriteUncertain() from None
            raise
        return CreatedNote([guid], warnings + group_warnings)

    def close(self):
        self._files.close()
        if self._codec:
            self._codec.close()
        self._codec, self._uid = None, None
        self.transport.close()
        self.identity.close()
        if self.drive:
            self.drive.close()
        super().close()
