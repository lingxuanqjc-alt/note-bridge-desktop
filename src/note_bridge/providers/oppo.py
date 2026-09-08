"""OPPO Connect note protocol from the official September 2026 web client."""

from html import escape
from pathlib import Path

from lxml import html as lhtml

from ..errors import BridgeError, WriteUncertain
from ..exporter import stamp
from ..models import Attachment, NoteDocument, PlatformId, account_fingerprint
from ..richtext import block_html, numbered_blocks, parse_html, span_html
from ..tasks import TaskContext
from .base import SPECS, CreatedNote, Provider, Snapshot
from .oppo_files import OppoFiles, image_bytes
from .oppo_groups import target_group
from .oppo_wire import OppoRequest
from .transport import Transport
from .xiaomi import timestamp


def parse_entry(entry: dict, account: str, folders: dict[str, str], assets=None):
    if not isinstance(entry, dict) or not entry.get("recordId") or not isinstance(entry.get("rawText"), str):
        raise BridgeError("protocol_changed", "OPPO 笔记正文结构无法识别。")
    if entry.get("status") != 0:
        raise BridgeError("snapshot_changed", "这条 OPPO 笔记在读取时已被移入回收站。")
    assets = assets or []
    root = lhtml.fragment_fromstring(entry["rawText"] or "<p></p>", create_parent="div")
    identities = {asset.id for asset in assets}
    for node in root.iter("img"):
        identity = node.get("attachid") if node.get("attachid") in identities else node.get("src")
        if identity in identities:
            node.set("src", "cid:" + identity)
            node.attrib.pop("data-origin-src", None)
    blocks, warnings = parse_html(lhtml.tostring(root, encoding="unicode"), {"cid:" + a.id: a.id for a in assets})
    if (entry.get("attachments") and not assets) or entry.get("attachmentExtra") not in (None, "", "[]", []):
        warnings.append("这条 OPPO 笔记包含尚未下载的附件，当前正文可能不完整。")
    if entry.get("speechLog") or entry.get("callLog") or entry.get("extra"):
        warnings.append("OPPO 扩展内容尚未完成转换验证，需要核对原笔记。")
    folder = entry.get("groupGuid")
    return NoteDocument(
        platform=PlatformId.OPPO,
        account_id=account,
        source_id=entry["recordId"],
        title=entry.get("rawTitle") or "",
        blocks=blocks,
        attachments=assets,
        warnings=warnings,
        source_folder_id=folder,
        source_folder_name=folders.get(folder),
        created_at=timestamp(entry.get("createTime")),
        updated_at=timestamp(entry.get("updateTime")),
    )


class OppoProvider(Provider):
    spec = SPECS[PlatformId.OPPO]
    read_supported = True
    # Controlled PNG/JPEG write and native readback support the developer preview.
    write_supported = True
    images_supported = True

    def __init__(self, transport: Transport, resources: Path):
        self.transport, self.resources = transport, resources
        self.transport.session.headers["Referer"] = "https://cloud.oppo.com/"
        self._files = OppoFiles(self)

    @staticmethod
    def _data(payload):
        if not isinstance(payload, dict) or payload.get("code") not in (0, "0") or "data" not in payload:
            raise BridgeError("platform_response", "OPPO 未确认请求成功，请检查笔记入口及设备验证状态。")
        return payload["data"]

    def _json(self, path, data=None, encrypted_response=True, **kwargs):
        request = OppoRequest(data or {})
        try:
            payload = self.transport.json("POST", "/owork-server" + path, json=request.payload, **kwargs)
            try:
                result = self._data(payload)
                return self._data(request.decrypt(result)) if encrypted_response else result
            except BridgeError:
                # Once sent, an unfamiliar envelope cannot prove that no note was created.
                if kwargs.get("write"):
                    raise WriteUncertain() from None
                raise
        finally:
            request.close()

    def probe(self):
        user = self._data(self.transport.json("GET", "/owork-server/web/account/v1/userInfo"))
        if not isinstance(user, dict) or not isinstance(user.get("ssoId"), (str, int)) or not user["ssoId"]:
            raise BridgeError("login_incomplete", "OPPO 账号身份未能确认。")
        self._json("/web/note/v2/group-list-new")
        self.account_id = account_fingerprint(self.spec.id, str(user["ssoId"]))
        return self.account_id

    def _listing(self, page=1, **kwargs):
        data = self._json(
            "/web/note/v2/list",
            {
                "status": 0,
                "queryFields": [],
                "pageNo": page,
                "pageSize": 50,
                "recordType": "hypertext_item_info",
                "sortFieldList": [
                    {"field": "topTime", "sortType": "desc"},
                    {"field": "updateTime", "sortType": "desc"},
                ],
            },
            **kwargs,
        )
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("records"), list)
            or type(data.get("totalCount")) is not int
            or data["totalCount"] < 0
            or type(data.get("hasMore")) is not bool
        ):
            raise BridgeError("protocol_changed", "OPPO 笔记分页结构无法识别。")
        return data

    def fetch(self, context: TaskContext):
        if not self.account_id:
            raise BridgeError("login_required", "请先登录 OPPO 账号。")
        kwargs = {"check_cancel": context.check_cancel, "wait": context.cancelled.wait}
        groups = self._json("/web/note/v2/group-list-new", **kwargs)
        if not isinstance(groups, list) or any(
            not isinstance(g, dict) or not isinstance(g.get("groupGuid"), str) for g in groups
        ):
            raise BridgeError("protocol_changed", "OPPO 笔记分组结构无法识别。")
        # The system-wide "all notes" view legitimately has an empty group GUID.
        folders = {g["groupGuid"]: g.get("groupName", "") for g in groups if g["groupGuid"]}
        rows, seen, total = [], set(), None
        for page in range(1, 10001):
            listing = self._listing(page, **kwargs)
            if total is not None and listing["totalCount"] != total:
                raise BridgeError("snapshot_changed", "获取目录时 OPPO 笔记总数发生变化，请重新获取。")
            total = listing["totalCount"]
            for row in listing["records"]:
                if not isinstance(row, dict) or not row.get("recordId") or row["recordId"] in seen:
                    raise BridgeError("pagination_stalled", "OPPO 分页返回重复或无效笔记。")
                seen.add(row["recordId"])
                rows.append(row)
            if not listing["hasMore"]:
                break
            if not listing["records"]:
                raise BridgeError("pagination_stalled", "OPPO 分页没有继续前进。")
        else:
            raise BridgeError("pagination_limit", "OPPO 分页超出安全上限，未保存不完整目录。")
        if len(rows) != total:
            raise BridgeError("pagination_incomplete", "OPPO 目录条数与平台统计不一致。")
        handwritten = self._json(
            "/web/paint_note/v2/list",
            {
                "status": 0,
                "pageNo": 1,
                "pageSize": 1,
                "recordType": "paint_note_item_info",
                "queryFields": [{"field": "sysVersion", "comparatorType": "greater_than", "value": "0"}],
                "sortFieldList": [{"field": "sysVersion", "sortType": "desc"}],
            },
            **kwargs,
        )
        if not isinstance(handwritten, dict) or type(handwritten.get("totalCount")) is not int:
            raise BridgeError("protocol_changed", "OPPO 手写笔记统计无法识别。")
        complete = handwritten["totalCount"] == 0
        if not complete:
            context.issue("proprietary_content", "账号中存在尚未转换的 OPPO 手写笔记。")
        context.update(total=total + handwritten["totalCount"], stage="正在读取 OPPO 笔记正文。")
        notes = []
        for index, row in enumerate(rows):
            # An empty version requests complete content, including when the list is already current.
            data = {"recordId": row["recordId"], "status": 0, "version": ""}
            detail = self._json(
                "/web/note/v2/info", data, params={"recordId": row["recordId"], "version": ""}, **kwargs
            )
            if not isinstance(detail, dict) or detail.get("recordId") != row["recordId"]:
                raise BridgeError("detail_mismatch", "OPPO 正文与请求笔记不一致。")
            if detail.get("version") != row.get("version"):
                raise BridgeError("snapshot_changed", "读取时 OPPO 笔记正文发生变化，请重新获取。")
            try:
                assets = []
                attachments = detail.get("attachments") or []
                if not isinstance(attachments, list):
                    raise BridgeError("protocol_changed", "OPPO 附件列表结构无法识别。")
                for item in attachments:
                    if not isinstance(item, dict) or not item.get("id") or item.get("type") != 0 or not isinstance(item.get("url"), str):
                        raise BridgeError("attachment_download_pending", "这类 OPPO 附件尚未完成读取验证。")
                    cloud_id = item["url"].removeprefix("/")
                    asset = Attachment(id=str(item["id"]), name=cloud_id, kind="image")
                    self._files.download(asset, cloud_id, context)
                    assets.append(asset)
                note = parse_entry(detail, self.account_id, folders, assets)
                complete = complete and not note.warnings
                notes.append(note)
            except BridgeError as error:
                if error.code in ("cancelled", "session_expired", "request_forbidden", "network_error", "rate_limited"):
                    raise
                complete = False
                context.issue(error.code, error.message, row["recordId"])
            context.update(completed=index + 1, succeeded=len(notes))
        original_account = self.account_id
        if self.probe() != original_account:
            raise BridgeError("account_changed", "读取期间 OPPO 账号发生变化，已停止保存。")
        if self._listing(**kwargs)["totalCount"] != total:
            raise BridgeError("snapshot_changed", "读取期间 OPPO 笔记总数发生变化，请重新获取。")
        return Snapshot(notes, complete)

    def validate_notes(self, notes):
        super().validate_notes(notes)
        for note in notes:
            ids = {asset.id for asset in note.attachments}
            if len(ids) != len(note.attachments) or ids != {b.attachment_id for b in note.blocks if b.kind == "attachment"}:
                raise BridgeError("attachment_mismatch", "OPPO 图片与正文引用不一致，未开始上传。")
            for asset in note.attachments:
                image_bytes(asset, self.resources)

    def create(self, note: NoteDocument, context: TaskContext) -> CreatedNote:
        self.preflight([note])
        if not self.account_id:
            raise BridgeError("login_required", "请先登录目标 OPPO 账号。")
        group_id, group_warnings = target_group(self, note, context)
        images = {}
        try:
            for asset in note.attachments:
                images[asset.id] = self._files.upload(asset, context)
        except BridgeError as error:
            if not isinstance(error, WriteUncertain):
                context.issue(error.code, error.message, note.source_id)
            if error.code == "multipart_upload_pending":
                raise WriteUncertain(error.message) from None
            raise WriteUncertain() from None
        markup, warnings = [], list(group_warnings)
        for block, ordinal in numbered_blocks(note.blocks):
            text = "".join(span_html(span) for span in block.spans)
            if block.kind == "attachment":
                attachment = images[block.attachment_id]
                # The official renderer resolves raw src as an attachment ID, not a download URL.
                src = str(attachment["id"])
                markup.append(f'<img src="{escape(src, quote=True)}" attachid="{escape(str(attachment["id"]), quote=True)}">')
            elif block.kind == "todo":
                state = "true" if block.checked else "false"
                markup.append('<ul data-type="taskList"><li data-type="taskItem" '
                              f'data-checked="{state}"><div><p>{text}</p></div></li></ul>')
            elif block.kind == "heading":
                markup.append(f"<h{block.level}>{text}</h{block.level}>")
            elif block.kind == "divider":
                # The official divider parser rejects HR without a supported line style.
                markup.append('<hr class="hr-style-solid">')
            else:
                markup.append(block_html(block, {}, ordinal))
        if any(block.kind == "code" for block in note.blocks):
            warnings.append("OPPO 官网将代码块显示为普通文字；代码正文及原始标记仍保留，可导出到本地。")
        if any(span.code for block in note.blocks for span in block.spans):
            warnings.append("OPPO 官网将行内代码显示为普通文字；文字及原始标记仍保留，可导出到本地。")
        markup.append("<p>来源笔记时间<br>" + escape(stamp(note)).replace("\n", "<br>") + "</p>")
        if note.source_folder_name:
            markup.append("<p>来源文件夹：" + escape(note.source_folder_name) + "</p>")
        # OPPO reserves EM for a separate normal-font mark; its italic parser accepts I.
        raw_text = "".join(markup).replace("<em>", "<i>").replace("</em>", "</i>")
        # The class selects OPPO's yellow highlight; inline color preserves API/export semantics.
        raw_text = raw_text.replace("<mark>", '<span class="highlight_color_yellow" style="background-color: rgba(255, 226, 39, 0.4)">').replace("</mark>", "</span>")
        # Official client modules f220 (add) and d291 (default group), September 2026.
        # Leave recordId/version absent: this endpoint creates a fresh record in one write.
        entry = self._json("/web/note/v2/add", {
            "category": 2, "groupGuid": group_id,
            "rawTitle": note.display_title, "rawText": raw_text,
            "attachments": list(images.values()), "attachmentExtra": "",
        }, write=True, check_cancel=context.check_cancel, wait=context.cancelled.wait)
        if not isinstance(entry, dict) or not isinstance(entry.get("recordId"), str) or not entry["recordId"]:
            raise WriteUncertain()
        return CreatedNote([entry["recordId"]], warnings)

    def close(self):
        self.transport.close()
        super().close()
