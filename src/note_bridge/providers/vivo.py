"""vivo PC suite read protocol, independently decoded from its public web client."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import ExitStack, closing
from pathlib import Path
from urllib.parse import quote, urlsplit

from lxml import html

from ..errors import BridgeError, WriteUncertain
from ..exporter import stamp
from ..models import Attachment, NoteDocument, PlatformId, account_fingerprint
from ..paths import confined, safe_filename
from ..richtext import DEFAULT_LAYOUT_WARNING, block_html, numbered_blocks, parse_html, span_html
from ..tasks import TaskContext
from .base import SPECS, CreatedNote, Provider, Snapshot
from .transport import Transport
from .vivo_files import VivoFiles, image_data
from .vivo_wire import VivoWire
from .xiaomi import timestamp

DIVIDER_DOWNGRADE_WARNING = "vivo 装饰分隔线已转换为普通分隔线。"


def parse_entry(entry: dict, body: str, account: str, folders: dict[str, str], assets=None) -> NoteDocument:
    if not isinstance(entry, dict) or not entry.get("guid") or not isinstance(body, str):
        raise BridgeError("protocol_changed", "vivo 笔记正文结构无法识别。")
    if entry.get("encryptType") not in (None, 0):
        raise BridgeError("encrypted_note", "这条 vivo 笔记已加密，尚未完成解锁读取验证。")
    # vivo uses 1 for active records and 0 for deletion, opposite to several other vendors.
    if entry.get("deleted") != 1:
        raise BridgeError("snapshot_changed", "这条 vivo 笔记在读取时已被移入回收站。")
    if entry.get("type") != 1:
        raise BridgeError("proprietary_content", "vivo 手写或其他专有笔记尚未完成转换验证。")
    if not entry.get("userId") or account_fingerprint(PlatformId.VIVO, str(entry["userId"])) != account:
        raise BridgeError("account_mismatch", "vivo 笔记归属与当前账号不一致，已停止保存。")
    assets = assets or []
    tree = html.fragment_fromstring(body or "", create_parent="div")
    media_warnings = []
    for node in tree.iter():
        if not isinstance(node.tag, str) or not node.tag.startswith("vnote-"):
            continue
        if node.tag == "vnote-todo":
            node.tag = "ul"
            for item in node.iterchildren("todo-item"):
                item.tag = "li"
                if item.get("done") not in ("true", "false"):
                    media_warnings.append("vivo 待办项的完成状态无法识别，需要核对原笔记。")
                item.set("data-checked", "true" if item.get("done") == "true" else "false")
        elif node.tag == "vnote-divider":
            node.tag = "hr"
            media_warnings.append(DIVIDER_DOWNGRADE_WARNING)
        elif node.tag in ("vnote-image", "vnote-audio", "vnote-audio2", "vnote-video", "vnote-doc"):
            filename = node.get("filename", "")
            matching = [
                a for a in assets if a.id == node.get("guid") or a.id in filename or a.name == filename
            ]
            if len(matching) == 1:
                node.tag = "img"
                node.set("src", "asset:" + matching[0].id)
            else:
                node.tag = "p"
                node.text = "[vivo 附件未获取]"
                media_warnings.append("vivo 正文中的附件引用未能与资源列表匹配。")
        else:
            media_warnings.append("vivo 专有内容块暂按可读取文本保留，需要核对原笔记。")
    blocks, warnings = parse_html(
        html.tostring(tree, encoding="unicode"), {"asset:" + a.id: a.id for a in assets}
    )
    warnings = list(dict.fromkeys(warnings + media_warnings))
    if entry.get("resources") and (not assets or any(a.local_path is None for a in assets)):
        warnings.append("这条 vivo 笔记包含尚未下载的附件，当前正文可能不完整。")
    folder = entry.get("noteBookGuid")
    return NoteDocument(
        platform=PlatformId.VIVO,
        account_id=account,
        source_id=entry["guid"],
        title=entry.get("title") or "",
        blocks=blocks,
        attachments=assets,
        warnings=warnings,
        source_folder_id=folder,
        source_folder_name=folders.get(folder),
        created_at=timestamp(entry.get("createTime")),
        updated_at=timestamp(entry.get("updateTime")),
    )


class VivoProvider(Provider):
    spec = SPECS[PlatformId.VIVO]
    read_supported = True
    # Controlled write/readback supports the PNG/JPEG developer preview.
    write_supported = True
    images_supported = True

    def __init__(self, transport: Transport, resources: Path):
        self.transport, self.resources = transport, resources
        self._wire = VivoWire()
        self._openid = None
        self._sts = None
        self._files = VivoFiles(transport, resources)
        self._uploaded_resources = {}

    def _json(self, method, path, data=None, encrypted=False, **kwargs):
        if data is not None:
            request = (
                {"jvq_param": self._wire.encrypt(json.dumps(data, ensure_ascii=False, separators=(",", ":")))}
                if encrypted
                else data.copy()
            )
            kwargs["json"] = request
        payload = self.transport.json(method, "/note-api" + path, **kwargs)
        if payload.get("jvq_response"):
            try:
                payload = json.loads(self._wire.decrypt(payload["jvq_response"]))
            except (ValueError, BridgeError):
                if kwargs.get("write"):
                    raise WriteUncertain() from None
                raise BridgeError("protocol_changed", "vivo 解码后的响应不是有效结构。") from None
        if not isinstance(payload, dict) or payload.get("code") != 0 or "data" not in payload:
            if kwargs.get("write"):
                raise WriteUncertain()
            raise BridgeError("platform_response", "vivo 未确认请求成功，请检查账号、笔记入口及验证状态。")
        return payload["data"]

    def probe(self):
        session = self._json("GET", "/account/getUserCookie")
        if not isinstance(session, dict):
            raise BridgeError("login_incomplete", "vivo 会话尚未建立。")
        openid = session.get("vivo_account_cookie_iqoo_openid")
        token = session.get("vivo_account_cookie_iqoo_vivotoken")
        if not isinstance(openid, str) or not openid or not isinstance(token, str) or not token:
            raise BridgeError("login_incomplete", "请在 vivo 官方窗口完成登录并进入笔记。")
        self._openid = openid
        self.transport.session.headers.update({"token": token, "openId": openid, "source": "1"})
        user = self._json("POST", "/account/getUserInfo", {"openId": openid})
        if not isinstance(user, dict) or not user.get("userId"):
            raise BridgeError("login_incomplete", "vivo 账号身份未能确认。")
        self._json("POST", "/statistics/note", {"type": 0, "encryptType": None})
        self.account_id = account_fingerprint(self.spec.id, str(user["userId"]))
        return self.account_id

    def _statistics(self, **kwargs):
        stats = self._json("POST", "/statistics/note", {"type": 0, "encryptType": None}, **kwargs)
        if not isinstance(stats, dict) or any(
            type(stats.get(key)) is not int or stats[key] < 0 for key in ("totalNotes", "encryptedNotes")
        ):
            raise BridgeError("protocol_changed", "vivo 笔记统计结构无法识别。")
        return stats

    def _body_reader(self):
        # Each reader owns its connection pool, cookie objects and envelope keys.
        # RequestsCookieJar.update copies Cookie objects; credentials stay in RAM.
        transport = Transport(self.transport.origin, self.transport.domains)
        try:
            transport.session.headers.clear()
            transport.session.headers.update(self.transport.session.headers)
            transport.session.cookies.update(self.transport.session.cookies)
            return VivoProvider(transport, self.resources)
        except BaseException:
            transport.session.headers.clear()
            transport.close()
            raise

    def _read_bodies(self, rows, context):
        if not rows:
            return
        kwargs = {"check_cancel": context.check_cancel, "wait": context.cancelled.wait}
        with ExitStack() as stack:
            readers = []
            for _ in range(min(2, len(rows))):
                context.check_cancel()
                reader = self._body_reader()
                stack.callback(reader.transport.session.headers.clear)
                stack.callback(reader.close)
                readers.append(reader)
            # Registered last: all in-flight reads finish before credentials are cleared.
            executor = stack.enter_context(ThreadPoolExecutor(max_workers=2, thread_name_prefix="vivo-read"))
            for offset in range(0, len(rows), 2):
                batch = rows[offset:offset + 2]
                futures = []
                for reader, row in zip(readers, batch, strict=False):
                    context.check_cancel()
                    futures.append(executor.submit(
                        reader._json, "POST", "/note/getContent/v2", {"guid": row["guid"]},
                        encrypted=True, **kwargs,
                    ))
                # No detail/asset request may overlap these body requests. Consume in
                # listing order on the task thread, including per-note failures.
                wait(futures)
                context.check_cancel()
                yield from zip(batch, futures, strict=True)

    def _attachment(self, resource, note_id, context):
        if (
            not isinstance(resource, dict)
            or not resource.get("guid")
            or resource.get("noteGuid") != note_id
            or account_fingerprint(self.spec.id, str(resource.get("userId"))) != self.account_id
        ):
            raise BridgeError("attachment_mismatch", "vivo 附件归属与当前笔记不一致。")
        mime = resource.get("mime") or "bin"
        mime = mime if "/" in mime else mimetypes.guess_type("file." + mime)[0] or "application/octet-stream"
        kind = mime.split("/")[0]
        return Attachment(
            id=resource["guid"],
            name=resource.get("name") or resource["guid"],
            mime=mime,
            kind=kind if kind in ("image", "audio", "video") else "file",
        )

    def _download_resource(self, resource, attachment, context):
        kwargs = {"check_cancel": context.check_cancel, "wait": context.cancelled.wait}
        if not self._sts:
            token = self.transport.json(
                "POST", "/clouddisk-api/api/suite/web/meta/getStsToken.do", json={"tokenType": 1}, **kwargs
            )
            if (
                token.get("code") != 0
                or not isinstance(token.get("data"), dict)
                or not token["data"].get("stsToken")
            ):
                raise BridgeError("attachment_session", "vivo 文件读取会话未能建立。")
            self._sts = token["data"]["stsToken"]
        domain = urlsplit(resource.get("domainAddr", ""))
        if (
            domain.scheme not in ("https", "http")
            or not domain.hostname
            or not domain.hostname.startswith("clouddisk-")
            or not domain.hostname.endswith(".vivo.com.cn")
            or domain.username
            or domain.port
            or domain.path not in ("", "/")
            or domain.query
            or domain.fragment
            or not resource.get("resourceKey")
        ):
            raise BridgeError("unsafe_attachment_url", "vivo 返回的资源域名无法确认。")
        # Fresh, cookie-free transport: only the scoped cloud-file headers go to the vendor's file host.
        transport = Transport("https://" + domain.hostname, (domain.hostname,))
        directory = confined(self.resources, f"{self.spec.id}/{self.account_id}")
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / (uuid.uuid4().hex + ".part")
        digest = hashlib.sha256()
        try:
            # The official reader loads the original object through source/<metaId>.
            # Its size is checked against resourceSize, so a thumbnail cannot pass as the original.
            with transport.request(
                "GET", "/api/file/webdisk/source/" + quote(resource["resourceKey"], safe=""),
                params={"stsToken": self._sts}, stream=True, **kwargs,
            ) as response:
                if response.headers.get("Content-Type", "").startswith(("application/json", "text/html")):
                    raise BridgeError("attachment_response", "vivo 文件服务返回了错误页面。")
                with temporary.open("wb") as stream:
                    for chunk in response.iter_content(1024 * 1024):
                        context.check_cancel()
                        stream.write(chunk)
                        digest.update(chunk)
            size = temporary.stat().st_size
            if size <= 0 or (resource.get("resourceSize") and size != resource["resourceSize"]):
                raise BridgeError("attachment_incomplete", "vivo 附件大小与原始记录不符，未接收不完整文件。")
            suffix = Path(attachment.name).suffix or mimetypes.guess_extension(attachment.mime) or ".bin"
            target = directory / (digest.hexdigest() + suffix)
            os.replace(temporary, target)
            attachment.local_path = target.relative_to(self.resources).as_posix()
            attachment.sha256, attachment.size = digest.hexdigest(), size
            attachment.name = safe_filename(attachment.name)
        finally:
            temporary.unlink(missing_ok=True)
            transport.close()

    def fetch(self, context: TaskContext):
        if not self.account_id:
            raise BridgeError("login_required", "请先登录 vivo 账号。")
        kwargs = {"check_cancel": context.check_cancel, "wait": context.cancelled.wait}
        stats = self._statistics(**kwargs)
        folders_data = self._json("POST", "/noteBook/getList", {"maxEntries": 100000}, **kwargs)
        if (
            not isinstance(folders_data, list)
            or any(not isinstance(row, dict) or not row.get("guid") for row in folders_data)
            or len(folders_data) >= 100000
        ):
            raise BridgeError("protocol_changed", "vivo 分组列表结构无法识别或未完整返回。")
        folders = {row["guid"]: row.get("nameNew") or row.get("name", "") for row in folders_data}
        rows, seen, cursor = [], set(), None
        context.update(total=stats["totalNotes"], stage="正在获取 vivo 笔记目录。")
        for page in range(1, 10001):
            data = {"maxEntries": 50, "pageNum": page, "sortField": 0, "syncProtocolVersion": 200}
            if page > 1:
                data["beforeTimeMs"] = cursor
            payload = self._json("POST", "/note/getAllNote/v2", data, encrypted=True, **kwargs)
            if not isinstance(payload, dict) or not isinstance(payload.get("notes"), list):
                raise BridgeError("protocol_changed", "vivo 笔记目录结构无法识别。")
            batch = payload["notes"]
            for row in batch:
                if not isinstance(row, dict) or not row.get("guid") or row["guid"] in seen:
                    raise BridgeError("pagination_stalled", "vivo 分页返回了重复或无效的笔记标识。")
                seen.add(row["guid"])
                rows.append(row)
            if len(batch) < 50:
                break
            next_cursor = payload.get("chunkLowTime")
            if type(next_cursor) is not int or next_cursor == cursor:
                raise BridgeError("pagination_stalled", "vivo 分页游标未继续前进。")
            cursor = next_cursor
        else:
            raise BridgeError("pagination_limit", "vivo 分页超出安全上限，未保存不完整目录。")
        if len(rows) != stats["totalNotes"]:
            raise BridgeError("pagination_incomplete", "vivo 目录条数与平台总数不一致，未保存不完整目录。")
        context.update(stage="正在读取 vivo 笔记正文。")
        notes, complete = [], True
        with closing(self._read_bodies(rows, context)) as bodies:
            for index, (row, future) in enumerate(bodies):
                context.check_cancel()
                try:
                    body = future.result()
                    assets = []
                    if not isinstance(body, str):
                        raise BridgeError("protocol_changed", "vivo 笔记正文不是预期的富文本结构。")
                    if row.get("resources") or "vnote-" in body or "<img" in body:
                        detail = self._json(
                            "POST",
                            "/note/getIncludeItem/v2",
                            {"guid": row["guid"], "syncProtocolVersion": 200},
                            encrypted=True,
                            **kwargs,
                        )
                        if not isinstance(detail, dict) or detail.get("guid") != row["guid"]:
                            raise BridgeError("detail_mismatch", "vivo 资源详情与当前笔记不一致。")
                        row = {**row, "resources": detail.get("resources")}
                        for resource in detail.get("resources") or []:
                            attachment = self._attachment(resource, row["guid"], context)
                            try:
                                self._download_resource(resource, attachment, context)
                            except BridgeError as error:
                                if error.code == "cancelled":
                                    raise
                                complete = False
                                context.issue(error.code, error.message, row["guid"])
                            assets.append(attachment)
                    note = parse_entry(row, body, self.account_id, folders, assets)
                    # Only these verified layout conversions preserve acquisition coverage.
                    # Unknown/content-loss warnings still block migration; all warnings stay in the report.
                    complete = complete and all(
                        warning in (DEFAULT_LAYOUT_WARNING, DIVIDER_DOWNGRADE_WARNING)
                        for warning in note.warnings
                    )
                    notes.append(note)
                except BridgeError as error:
                    if error.code in ("cancelled", "account_mismatch", "session_expired", "request_forbidden", "rate_limited", "network_error"):
                        raise
                    complete = False
                    context.issue(error.code, error.message, row["guid"])
                context.update(completed=index + 1, succeeded=len(notes))
        original_account = self.account_id
        if self.probe() != original_account:
            raise BridgeError("account_changed", "读取期间 vivo 账号发生变化，已停止保存。")
        final_stats = self._statistics(**kwargs)
        if (final_stats["totalNotes"], final_stats["encryptedNotes"]) != (
            stats["totalNotes"],
            stats["encryptedNotes"],
        ):
            raise BridgeError("snapshot_changed", "读取过程中 vivo 笔记数量发生变化，请重新获取。")
        if stats["encryptedNotes"]:
            context.issue("encrypted_notes_pending", "账号中存在加密笔记，需另行核对是否完整读取。")
            complete = False
        return Snapshot(notes, complete)

    def validate_notes(self, notes: list[NoteDocument]):
        super().validate_notes(notes)
        for note in notes:
            identities = {asset.id for asset in note.attachments}
            references = {block.attachment_id for block in note.blocks if block.kind == "attachment"}
            if len(identities) != len(note.attachments) or not references.issubset(identities):
                raise BridgeError("attachment_mismatch", "图片标识重复或正文引用缺失，未开始上传。")
            for attachment in note.attachments:
                if not attachment.local_path:
                    raise BridgeError("attachment_missing", "图片尚未下载，未开始上传。")
                image_data(attachment, Path(attachment.local_path), self.resources)

    def upload_attachment(self, attachment: Attachment, path: Path, context: TaskContext) -> str:
        if not self.images_supported or not self.account_id:
            raise BridgeError("attachment_upload_pending", "vivo 图片上传尚未完成正式验证。")
        resource = self._files.upload(attachment, path, context)
        self._uploaded_resources[resource["guid"]] = resource
        return resource["guid"]

    def create(self, note: NoteDocument, context: TaskContext) -> CreatedNote:
        self.preflight([note])
        if not self.account_id:
            raise BridgeError("login_required", "请先登录目标 vivo 账号。")
        from .vivo_groups import target_group
        group_id, group_warnings = target_group(self, note, context)
        current, guid = int(time.time() * 1000), uuid.uuid4().hex
        context.record_remote_ids([guid])
        resources, names = [], {}
        try:
            for attachment in note.attachments:
                resource_id = self.upload_attachment(attachment, Path(attachment.local_path), context)
                resource = self._uploaded_resources.pop(resource_id)
                resource.update(noteGuid=guid, sort=len(resources))
                resources.append(resource)
                names[attachment.id] = resource["name"]
            result = self._create_uploaded(note, context, current, guid, resources, names, group_id)
            result.warnings.extend(group_warnings)
            return result
        except BridgeError:
            # A rejected note request can still leave uploaded objects that must not be uploaded again.
            if resources:
                raise WriteUncertain() from None
            raise

    def _create_uploaded(self, note, context, current, guid, resources, names, group_id="0"):
        from html import escape

        markup, warnings = [], []
        referenced = set()
        for block, ordinal in numbered_blocks(note.blocks):
            text = "".join(span_html(span) for span in block.spans)
            if block.kind == "attachment":
                markup.append('<vnote-image filename="' + escape(names[block.attachment_id], quote=True)
                              + '"></vnote-image>')
                referenced.add(block.attachment_id)
            elif block.kind == "todo":
                state = "true" if block.checked else "false"
                markup.append(f'<vnote-todo><todo-item done="{state}">{text}</todo-item></vnote-todo>')
            elif block.kind == "heading":
                markup.append(f"<h{block.level}>{text}</h{block.level}>")
            else:
                markup.append(block_html(block, {}, ordinal))
        for attachment_id, name in names.items():
            if attachment_id not in referenced:
                markup.append('<vnote-image filename="' + escape(name, quote=True) + '"></vnote-image>')
                warnings.append("未在正文定位的图片已附在末尾，请核对原笔记。")

        markup.append("<p>来源笔记时间<br>" + escape(stamp(note)).replace("\n", "<br>") + "</p>")
        if note.source_folder_name:
            markup.append("<p>来源文件夹：" + escape(note.source_folder_name) + "</p>")
        kwargs = {"check_cancel": context.check_cancel, "wait": context.cancelled.wait}
        sync = self._json("POST", "/sync/getSyncState", {"type": 0}, **kwargs)
        if not isinstance(sync, dict) or type(sync.get("updateCount")) is not int:
            raise BridgeError("protocol_changed", "vivo 同步版本未能确认，未执行创建。")
        entry = {
            "guid": guid, "title": note.display_title, "contentDigest": note.plain_text[:60],
            "content": "".join(markup), "originContent": "", "conflictTime": None,
            "createTime": current, "updateTime": current, "contentUpdateTime": current, "attrUpdateTime": current,
            "stickTop": 0, "importantLevel": 0, "noteBookGuid": group_id, "tags": [], "dirty": 1,
            "deleted": 1, "type": 1, "encryptType": 0, "symbolCnf": "", "paperTexture": "0",
            "bgColor": 101, "pageMargins": "[0,16,0,16]", "syncProtocolVersion": 0, "syncLock": 0,
        }
        response = self._json("POST", "/sync/createSync/v2", {
            "type": 0, "lastUpdateCount": sync["updateCount"], "noteBooks": [], "notes": [entry],
            "tags": [], "resources": resources,
        }, encrypted=True, write=True, **kwargs)
        # The official createSync client acknowledges the client's new GUID through updateCount.
        if (not isinstance(response, dict) or type(response.get("updateCount")) is not int
                or response["updateCount"] <= sync["updateCount"]):
            raise WriteUncertain()
        return CreatedNote([guid], warnings)

    def close(self):
        self._openid = None
        self._sts = None
        self._files.close()
        self._uploaded_resources.clear()
        self._wire.close()
        self.transport.session.headers.pop("token", None)
        self.transport.session.headers.pop("openId", None)
        self.transport.close()
        super().close()
