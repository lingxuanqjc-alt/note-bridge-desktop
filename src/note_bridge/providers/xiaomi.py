"""Independent implementation of the public Xiaomi legacy-note web protocol.

Evidence: i.mi.com/note/h5, main.3ec79d08 and vendor.bb3b9b48, inspected 2026-09-05.
Live verification is still required. Encrypted and proprietary payloads fail explicitly.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import mimetypes
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from ..errors import BridgeError, WriteUncertain
from ..exporter import stamp
from ..models import Attachment, NoteDocument, PlatformId, Span, account_fingerprint
from ..paths import confined, safe_filename
from ..richtext import numbered_blocks, safe_link
from ..tasks import TaskContext
from .base import SPECS, CreatedNote, Provider, Snapshot
from .transport import Transport


def legacy_span(span: Span) -> str:
    """Encode native Xiaomi marks, not visually similar generic HTML tags."""
    text = html.escape(span.text)
    for enabled, tag in ((span.bold, "b"), (span.italic, "i"), (span.underline, "u"),
                         (span.strike, "delete"), (span.highlight, "background")):
        if enabled:
            attrs = ' color="#9affe8af"' if tag == "background" else ""
            text = f"<{tag}{attrs}>{text}</{tag}>"
    link = safe_link(span.link)
    if link:
        text = f'<a href="{html.escape(link, quote=True)}">{text}</a>'
    return text


def encode_content(note: NoteDocument, images: dict) -> tuple[str, list[str]]:
    lines = [html.escape(note.display_title)]
    warnings = []
    for block, ordinal in numbered_blocks(note.blocks):
        if any("\n" in span.text for span in block.spans):
            warnings.append("小米区块内换行已转换为独立段落，跨行样式需核对。")
        if block.attachment_id:
            lines.append('<img fileid="' + html.escape(images[block.attachment_id]["fileId"], quote=True) + '"/>')
        elif block.kind == "table":
            lines.extend(html.escape("\t".join(row)) for row in block.rows)
            warnings.append("表格转换为制表符分隔文本。")
        elif block.kind == "divider":
            lines.append("<hr/>")
        else:
            content = "".join(legacy_span(span) for span in block.spans)
            if any(span.code for span in block.spans):
                warnings.append("小米行内代码已保留文本，等宽字体未保留。")
            if any(safe_link(span.link) for span in block.spans):
                warnings.append("小米超链接使用独立链接卡片，原段落内位置和样式需核对。")
            if block.kind == "todo":
                checked = ' checked="true"' if block.checked else ""
                content = f'<input type="checkbox" indent="{block.level}" level="-1"{checked}/>' + content
            elif block.kind == "list":
                marker = (f'<order indent="{block.level}" inputNumber="{ordinal}"/>' if block.ordered
                          else f'<bullet indent="{block.level}"/>')
                content = marker + content
            elif block.kind == "heading":
                content = f'<h{min(3, block.level)}/>' + content
                if block.level > 3:
                    warnings.append("小米仅支持三级标题，更深标题已调整为三级。")
            elif block.kind == "quote":
                content = "<quote>" + content + "</quote>"
            elif block.kind == "code":
                warnings.append("部分段落样式转换为普通文本。")
            lines.append(content)
    referenced = {block.attachment_id for block in note.blocks if block.attachment_id}
    for identity, image in images.items():
        if identity not in referenced:
            lines.append('<img fileid="' + html.escape(image["fileId"], quote=True) + '"/>')
            warnings.append("未在正文定位的图片已附加到正文末尾。")
    lines.extend(["来源笔记时间", html.escape(stamp(note))])
    if note.source_folder_name:
        lines.append("来源文件夹：" + html.escape(note.source_folder_name))
    return "<new-format/>" + "\n".join(lines), list(dict.fromkeys(warnings))


def timestamp(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BridgeError("protocol_changed", "笔记时间字段无法识别，已停止转换。")
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise BridgeError("protocol_changed", "笔记时间字段超出有效范围。") from None


def parse_entry(entry: dict, account: str, folders: dict[str, str]) -> NoteDocument:
    if not isinstance(entry, dict) or not entry.get("id") or not isinstance(entry.get("content"), str):
        raise BridgeError("protocol_changed", "笔记正文结构无法识别。")
    if entry.get("encryptInfo"):
        raise BridgeError("encrypted_note", "这条笔记启用了端到端加密，当前解码尚未完成验证。")
    try:
        extra = json.loads(entry.get("extraInfo") or "{}")
    except (ValueError, TypeError):
        raise BridgeError("protocol_changed", "笔记附加信息无法识别。") from None
    if not isinstance(extra, dict) or extra.get("note_content_type", "common") != "common":
        raise BridgeError("proprietary_content", "思维导图或专有笔记格式尚未完成转换验证。")
    raw = entry["content"]
    if re.search(r"^☺ ", raw, re.M):
        raise BridgeError("legacy_attachment", "旧版小米图片编码尚未完成转换验证，已保留原始云端笔记。")
    setting = entry.get("setting") or {}
    metadata = setting.get("data", []) if isinstance(setting, dict) else None
    if not isinstance(metadata, list):
        raise BridgeError("protocol_changed", "笔记附件列表无法识别。")
    assets = []
    for data in metadata:
        if not isinstance(data, dict) or not data.get("fileId"):
            raise BridgeError("protocol_changed", "附件标识缺失。")
        file_id = str(data["fileId"])
        mime = data.get("mimeType") or "application/octet-stream"
        if not isinstance(mime, str):
            raise BridgeError("protocol_changed", "附件类型无法识别。")
        kind = mime.split("/")[0]
        name = str(data.get("fileName") or file_id + (mimetypes.guess_extension(mime) or ".bin"))
        assets.append(
            Attachment(
                id=file_id, name=name, mime=mime, kind=kind if kind in ("image", "audio", "video") else "file"
            )
        )
    from .xiaomi_markup import parse_legacy

    blocks, warnings, media = parse_legacy(raw)
    for identity, kind in media:
        if identity not in {asset.id for asset in assets}:
            assets.append(Attachment(id=identity, name=identity, kind=kind))
            warnings.append("正文引用的附件缺少元数据，下载及类型识别可能受限。")
    folder_id = str(entry.get("folderId", "0"))
    return NoteDocument(
        platform=PlatformId.XIAOMI,
        account_id=account,
        source_id=str(entry["id"]),
        title=str(extra.get("title") or ""),
        source_folder_id=folder_id,
        source_folder_name=folders.get(folder_id),
        created_at=timestamp(entry.get("createDate")),
        updated_at=timestamp(entry.get("modifyDate")),
        blocks=blocks,
        attachments=assets,
        warnings=list(dict.fromkeys(warnings)),
    )


class XiaomiProvider(Provider):
    spec = SPECS[PlatformId.XIAOMI]
    read_supported = True
    # Developer preview after controlled write/readback; account encryption stays guarded.
    write_supported = True
    images_supported = True

    def __init__(self, transport: Transport, resources: Path):
        self.transport, self.resources = transport, resources
        self._clear_write_mode()

    def _clear_write_mode(self, code="encrypted_upload_pending", message=None):
        self._write_scope = None
        self._write_mode_error = (code, message or "尚未确认小米账号的加密模式，不能迁入文本或图片；读取和导出仍可使用。")

    def _write_binding(self):
        uid = self.transport.cookie("userId")
        if not self.account_id or not uid or account_fingerprint(self.spec.id, uid) != self.account_id:
            raise BridgeError("account_changed", "小米账号与加密检查范围不一致，请重新登录并预检。")
        token = self.transport.cookie("serviceToken")
        if not token:
            raise BridgeError("login_incomplete", "小米写入会话缺失，请重新登录并预检。")
        # This digest never leaves RAM. A replaced transport, session or token needs new evidence.
        return (id(self.transport), id(getattr(self.transport, "session", self.transport)),
                self.account_id, hashlib.sha256(token.encode()).digest())

    def require_unencrypted_mode(self):
        """Purely local guard, including the last confirmed account and backend session."""
        try:
            current = self._write_binding()
            if self._write_scope is None:
                raise BridgeError(*self._write_mode_error)
            binding, checked, expires = self._write_scope
            if current != binding:
                raise BridgeError("account_changed", "小米账号或会话已变化，请重新预检加密状态。")
            if not checked <= time.monotonic() < expires:
                raise BridgeError("encryption_scope_expired", "小米加密检查已超过 600 秒，请重新预检；尚未继续写入。")
        except BridgeError as error:
            self._clear_write_mode(error.code, error.message)
            raise

    def refresh_write_mode(self):
        """Official GET only; capability failure never defaults to unencrypted mode."""
        self._clear_write_mode()
        try:
            binding = self._write_binding()
            # Official note/vendor 7xhp.f and home/vendor 30164.li use this same GET.
            payload = self.transport.json("GET", "/mic/keybag/v1/getEncInfo",
                params={"hsid": 2, "appId": "micloud", "ts": int(time.time() * 1000)})
            if self._write_binding() != binding:
                raise BridgeError("account_changed", "小米账号或会话在加密检查期间变化，请重新预检。")
            if (not isinstance(payload, dict) or type(payload.get("code")) is not int
                    or payload["code"] != 0 or not isinstance(payload.get("data"), dict)):
                raise BridgeError("encryption_status_invalid", "小米未明确确认加密状态，迁入已阻断；读取和导出仍可使用。")
            status = payload["data"].get("e2eeStatus")
            if status == "open":
                raise BridgeError("encrypted_account", "小米账号已启用端到端加密，当前不能迁入文本或图片。")
            if status != "close":
                raise BridgeError("encryption_status_invalid", "小米加密状态字段无法识别，迁入已阻断；读取和导出仍可使用。")
            checked = time.monotonic()
            self._write_scope = (binding, checked, checked + 600)
        except BridgeError as error:
            if error.code in {"account_changed", "login_incomplete", "encrypted_account", "encryption_status_invalid"}:
                self._clear_write_mode(error.code, error.message)
            else:
                reasons = {"network_error": "网络请求失败", "session_expired": "登录或二次验证失效",
                           "request_forbidden": "平台拒绝访问", "rate_limited": "请求受到限流",
                           "server_error": "平台服务不可用", "protocol_changed": "响应格式无法识别"}
                reason = reasons.get(error.code, "接口未确认成功")
                self._clear_write_mode("encryption_status_unavailable",
                    f"小米加密状态检查未完成（{reason}），不能迁入；读取和导出仍可使用。")
        except Exception:
            # Do not leak response bodies or a transport exception into the preview.
            self._clear_write_mode("encryption_status_unavailable",
                "小米加密状态检查未完成，不能迁入；读取和导出仍可使用。")

    def confirm_upload_contract(self, scope: dict):
        """Lab-only bridge for the existing intercepted, non-encrypted upload contract.

        Production never reads a scope file or calls this method. Bare booleans cannot
        grant a scope, nor can an older contract override an explicitly encrypted account.
        """
        if self._write_mode_error[0] == "encrypted_account":
            self.require_unencrypted_mode()
        self._clear_write_mode()
        binding = self._write_binding()
        stamp = scope.get("verified_at") if isinstance(scope, dict) else None
        if (not isinstance(scope, dict) or scope.get("kind") != "xiaomi-nonencrypted-upload-scope"
                or scope.get("account_id") != self.account_id or scope.get("metadata_verified") is not True
                or scope.get("formal_acceptance") is not False or type(scope.get("cloud_writes")) is not int
                or scope["cloud_writes"] != 0 or type(stamp) not in (int, float) or not math.isfinite(stamp)):
            raise BridgeError(*self._write_mode_error)
        age = time.time() - stamp
        if not 0 <= age < 600:
            raise BridgeError(*self._write_mode_error)
        checked = time.monotonic() - age
        self._write_scope = (binding, checked, checked + 600)

    def prepare_migration(self):
        self.refresh_write_mode()

    def _json(self, method: str, path: str, **kwargs) -> dict:
        if kwargs.get("write"):
            self.require_unencrypted_mode()
        payload = self.transport.json(method, path, **kwargs)
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            if kwargs.get("write"):
                # Unknown vendor error codes do not prove that a write had no effect.
                error = WriteUncertain()
                if type(payload.get("code")) is int:
                    error.vendor_code = payload["code"]
                raise error
            raise BridgeError(
                "platform_response", "小米云服务未确认请求成功，请检查登录、二次验证及笔记版本。"
            )
        return payload["data"]

    def _page(self, cursor=None, **kwargs):
        params = {"limit": 200, "ts": int(time.time() * 1000)}
        if cursor is not None:
            params["syncTag"] = cursor
        data = self._json("GET", "/note/full/page", params=params, **kwargs)
        if not isinstance(data.get("entries"), list) or not isinstance(data.get("lastPage"), bool):
            raise BridgeError("protocol_changed", "小米分页结果结构发生变化，已停止获取。")
        return data

    def probe(self) -> str:
        self._clear_write_mode()
        uid = self.transport.cookie("userId")
        if not uid:
            raise BridgeError("login_incomplete", "请在小米官方窗口完成登录并进入笔记页面，再检查登录。")
        self._page()
        self.account_id = account_fingerprint(self.spec.id, uid)
        self.refresh_write_mode()
        if self.transport.cookie("userId") != uid:
            self._clear_write_mode()
            self.account_id = None
            raise BridgeError("account_changed", "小米账号在检查期间发生变化，请重新登录。")
        return self.account_id

    def fetch(self, context: TaskContext) -> Snapshot:
        if not self.account_id:
            raise BridgeError("login_required", "请先登录小米账号。")
        from .xiaomi_groups import folder_listing

        account = self.account_id
        def check_account():
            uid = self.transport.cookie("userId")
            if not uid or account_fingerprint(self.spec.id, uid) != account:
                raise BridgeError("account_changed", "读取期间小米账号发生变化，已有缓存保持不变。")

        check_account()
        options = {"check_cancel": context.check_cancel, "wait": context.cancelled.wait}
        folders = {row["id"]: row["subject"] for row in folder_listing(self, **options)}
        entries, cursors, seen = [], set(), set()
        cursor = None
        context.update(stage="正在获取小米笔记目录。")
        for _ in range(10000):
            context.check_cancel()
            page = self._page(cursor, check_cancel=context.check_cancel, wait=context.cancelled.wait)
            for entry in page["entries"]:
                if not isinstance(entry, dict) or "id" not in entry:
                    raise BridgeError("protocol_changed", "笔记标识缺失。")
                if entry.get("status") in ("deleted", "purged") or entry.get("type") == "folder":
                    continue
                key = str(entry["id"])
                if key in seen:
                    raise BridgeError("paging_duplicate", "分页返回重复笔记，已停止以免遗漏或重复。")
                seen.add(key)
                entries.append(entry)
            if page["lastPage"]:
                break
            cursor = page.get("syncTag")
            if cursor is None or str(cursor) in cursors:
                raise BridgeError("paging_stalled", "平台分页没有继续前进，已有缓存保持不变。")
            cursors.add(str(cursor))
        else:
            raise BridgeError("paging_limit", "分页数量超过保护上限，已有缓存保持不变。")
        notes, complete = [], True
        context.update(total=len(entries), stage="正在读取正文和附件。")
        for index, entry in enumerate(entries):
            context.check_cancel()
            key = str(entry["id"])
            try:
                data = self._json(
                    "GET",
                    f"/note/note/{quote(key, safe='')}/",
                    check_cancel=context.check_cancel,
                    wait=context.cancelled.wait,
                )
                detail = data.get("entry")
                if not isinstance(detail, dict) or str(detail.get("id")) != key:
                    raise BridgeError("detail_mismatch", "笔记详情标识与目录不一致，未保存该条内容。")
                note = parse_entry(detail, account, folders)
                if note.source_folder_id not in ("0", None) and note.source_folder_id not in folders:
                    complete = False
                    note.warnings.append("来源文件夹缺失，已保留其标识，归属尚待核对。")
                    context.issue("source_folder_missing", "笔记所属文件夹未在目录中找到。", key)
                for attachment in note.attachments:
                    try:
                        self._download(attachment, context)
                    except BridgeError as error:
                        if error.code == "cancelled":
                            raise
                        complete = False
                        note.warnings.append("附件下载未完成：" + attachment.name)
                        context.issue("attachment_failed", "附件下载未完成，请保留源笔记后重试。", key)
                notes.append(note)
            except BridgeError as error:
                if error.code in ("cancelled", "session_expired", "request_forbidden", "rate_limited", "network_error"):
                    raise
                complete = False
                context.issue(error.code, error.message, key)
            context.update(completed=index + 1, succeeded=len(notes))
        final_folders = {row["id"]: row["subject"] for row in folder_listing(self, **options)}
        check_account()
        if self.account_id != account:
            raise BridgeError("account_changed", "读取期间小米账号发生变化，已有缓存保持不变。")
        if final_folders != folders:
            raise BridgeError("snapshot_changed", "读取期间小米文件夹发生变化，请重新获取。")
        return Snapshot(notes, complete)

    def _download(self, attachment: Attachment, context: TaskContext):
        from .xiaomi_files import download_response

        directory = confined(self.resources, f"{self.spec.id}/{self.account_id}")
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / (uuid.uuid4().hex + ".part")
        digest = hashlib.sha256()
        try:
            with download_response(self, attachment, context) as response:
                if response.headers.get("Content-Type", "").startswith(("text/html", "application/json")):
                    raise BridgeError("attachment_response", "附件端点返回了错误页面。")
                with temporary.open("wb") as stream:
                    for chunk in response.iter_content(1024 * 1024):
                        context.check_cancel()
                        stream.write(chunk)
                        digest.update(chunk)
            if temporary.stat().st_size == 0:
                raise BridgeError("attachment_empty", "平台返回了空附件。")
            target = directory / (digest.hexdigest()[:16] + "-" + safe_filename(attachment.name))
            os.replace(temporary, target)
            attachment.local_path = target.relative_to(self.resources).as_posix()
            attachment.sha256, attachment.size = digest.hexdigest(), target.stat().st_size
        finally:
            temporary.unlink(missing_ok=True)

    def validate_notes(self, notes: list[NoteDocument]):
        super().validate_notes(notes)
        from .xiaomi_files import image_bytes

        if notes:
            self.require_unencrypted_mode()
        for note in notes:
            identities = {asset.id for asset in note.attachments}
            if len(identities) != len(note.attachments):
                raise BridgeError("duplicate_attachment", "来源图片标识重复，未开始迁移。")
            if any(block.attachment_id and block.attachment_id not in identities for block in note.blocks):
                raise BridgeError("attachment_missing", "正文引用的图片缺失，未开始迁移。")
            for asset in note.attachments:
                image_bytes(asset, self.resources)

    def create(self, note: NoteDocument, context: TaskContext) -> CreatedNote:
        self.preflight([note])
        if not self.account_id:
            raise BridgeError("login_required", "请先登录目标小米账号。")
        from .xiaomi_files import XiaomiFiles

        try:
            images = {asset.id: XiaomiFiles(self).upload(asset, context) for asset in note.attachments}
            return self._create_with_images(note, context, images)
        except BridgeError as error:
            if note.attachments:
                if not isinstance(error, WriteUncertain):
                    context.issue(error.code, error.message, note.source_id)
                raise WriteUncertain() from None
            raise

    def _create_with_images(self, note: NoteDocument, context: TaskContext, images: dict) -> CreatedNote:
        self.preflight([note])
        if not self.account_id:
            raise BridgeError("login_required", "请先登录目标小米账号。")
        content, warnings = encode_content(note, images)
        token = self.transport.cookie("serviceToken")
        if not token:
            raise BridgeError("login_incomplete", "目标账号缺少当前写入会话，请重新登录。")
        from .xiaomi_groups import target_group
        group_id, group_warnings = target_group(self, note, context)
        warnings.extend(group_warnings)
        current = int(time.time() * 1000)
        entry = {
            "content": content,
            "folderId": group_id,
            "colorId": 0,
            "createDate": current,
            "modifyDate": current,
        }
        if images:
            entry["setting"] = {"data": list({image["fileId"]: image for image in images.values()}.values())}
        response = self._json(
            "POST",
            "/note/note",
            data={"entry": json.dumps(entry, ensure_ascii=False), "serviceToken": token},
            write=True,
            check_cancel=context.check_cancel,
        )
        remote = response.get("entry", {}).get("id")
        if not remote:
            raise WriteUncertain()
        if images:
            context.record_remote_ids([str(remote)])
            detail = self._json("GET", f"/note/note/{quote(str(remote), safe='')}/",
                                check_cancel=context.check_cancel, wait=context.cancelled.wait)
            actual = parse_entry(detail.get("entry"), self.account_id, {})
            expected = {image["fileId"] for image in images.values()}
            if ({asset.id for asset in actual.attachments} != expected
                    or {block.attachment_id for block in actual.blocks if block.attachment_id} != expected):
                raise WriteUncertain()
        return CreatedNote([str(remote)], list(dict.fromkeys(warnings)))

    def close(self):
        self._clear_write_mode()
        self.transport.close()
        super().close()
