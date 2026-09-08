"""Huawei Cloud memo protocol; the separate Huawei Notes service is outside this adapter."""

from __future__ import annotations

import json
import time
import uuid
from html import escape
from pathlib import Path

from lxml import html

from ..errors import BridgeError, WriteUncertain
from ..exporter import stamp
from ..models import Attachment, NoteDocument, PlatformId, account_fingerprint
from ..richtext import numbered_blocks, parse_html
from ..tasks import TaskContext
from .base import SPECS, CreatedNote, Provider, Snapshot
from .huawei_files import HuaweiFiles, image_bytes, trace_id
from .transport import Transport
from .xiaomi import timestamp


def encode_body(note: NoteDocument, images=None):
    if images is None and (note.attachments or any(block.kind == "attachment" for block in note.blocks)):
        raise BridgeError("attachment_upload_pending", "华为图片分组上传尚未完成验证，未执行创建。")
    if images is not None and ({a.id for a in note.attachments} != set(images)
            or {b.attachment_id for b in note.blocks if b.kind == "attachment"} != set(images)):
        raise BridgeError("attachment_mismatch", "华为图片引用与本次附件不一致。")
    if "<>><><<<" in note.plain_text or "<>><><<<" in note.display_title:
        raise BridgeError("legacy_delimiter", "正文含华为旧格式分隔符，尚未验证无损编码，未执行创建。")
    plain, markup, warnings = [], [], []

    def line(text, rich, kind="Text", checked=False):
        marker = str(int(checked)) if kind == "Bullet" else ""
        plain.append(kind + "|" + marker + text)
        markup.append('<element type="' + kind + '">' + marker + '<hw_font size="1.0">' + rich + '</hw_font></element>')

    # The official editor prepends the explicitly edited title using its Y4 serializer.
    line(note.display_title, escape(note.display_title))
    for block, ordinal in numbered_blocks(note.blocks):
        if block.kind == "attachment":
            path = "/data/user/0/com.example.android.notepad/images/" + images[block.attachment_id]
            plain.append("Attachment|" + path)
            markup.append('<element type="Attachment">' + escape(path) + '</element>')
            continue
        rich = ""
        for span in block.spans:
            text = escape(span.text).replace("\n", "<br/>")
            for flag, tag in ((span.bold, "b"), (span.italic, "i"), (span.underline, "u")):
                if flag:
                    text = f"<{tag}>{text}</{tag}>"
            rich += text
            if span.strike or span.highlight or span.code or span.link:
                warnings.append("华为删除线、高亮、代码字体或链接已保留文本，样式未保留。")
        text = block.text
        if block.kind == "list":
            prefix = f"{ordinal}. " if block.ordered else "• "
            text, rich = prefix + text, prefix + rich
            warnings.append("华为列表已转换为带编号或符号的普通段落。")
        elif block.kind not in ("paragraph", "todo"):
            if block.kind == "table":
                text = "\n".join("\t".join(row) for row in block.rows)
                rich = escape(text).replace("\n", "<br/>")
            elif block.kind == "divider":
                text = rich = "——"
            warnings.append("华为标题层级、引用、代码块、表格或分隔线已转换为普通段落。")
        line(text, rich, "Bullet" if block.kind == "todo" else "Text", block.checked)
    for text in ("来源笔记时间\n" + stamp(note), "来源文件夹：" + note.source_folder_name if note.source_folder_name else ""):
        if text:
            line(text, escape(text).replace("\n", "<br/>"))
    return "<>><><<<".join(plain), "<note>" + "".join(markup) + "</note>", list(dict.fromkeys(warnings))


def parse_entry(entry: dict, account: str, folders: dict[str, str], assets=None) -> NoteDocument:
    if not isinstance(entry, dict) or not entry.get("guid") or entry.get("kind") != "note":
        raise BridgeError("proprietary_content", "这条华为记录不是已验证的普通备忘录。")
    try:
        data = json.loads(entry["data"])
        content = data["content"]
    except (ValueError, KeyError, TypeError):
        raise BridgeError("protocol_changed", "华为备忘录正文结构无法识别。") from None
    if not isinstance(content, dict) or not isinstance(content.get("html_content"), str):
        raise BridgeError("protocol_changed", "华为备忘录缺少已验证的富文本正文。")
    # The embedded legacy client GUID has a different namespace. fetch() validates the
    # response envelope's cloud GUID against the requested directory record instead.
    if content.get("delete_flag") not in (None, 0):
        raise BridgeError("snapshot_changed", "这条华为备忘录在读取时已被移入回收站。")
    root = html.fragment_fromstring(content["html_content"] or "", create_parent="div")
    warnings = []
    empty_fonts = set()
    by_name = {a.name: a for a in assets or []}
    for node in root.iter():
        if node.tag == "element":
            kind = node.get("type", "Text")
            if kind == "Bullet":
                marker = node.text or ""
                if not marker.startswith(("0", "1")):
                    raise BridgeError("protocol_changed", "华为待办标记无法识别。")
                node.tag = "li"
                node.set("data-checked", "true" if marker[0] == "1" else "false")
                node.text = marker[1:]
            elif kind == "Text":
                node.tag = "p"
                # An explicit empty legacy row must survive the generic HTML parser.
                if (len(node) == 1 and node[0].tag == "hw_font" and not len(node[0])
                        and not (node.text or node[0].text or node[0].tail)):
                    empty_fonts.add(node[0])
            elif kind == "Attachment" and (node.text or "").rsplit("/", 1)[-1] in by_name:
                asset = by_name[(node.text or "").rsplit("/", 1)[-1]]
                node.tag, node.text = "img", None
                node.set("src", "cid:" + asset.id)
                node.set("alt", asset.name)
            else:
                node.tag = "p"
                warnings.append("华为专有内容块暂按可读取文本保留，需要核对原笔记。")
        elif node.tag == "hw_font":
            if node.get("size", "1.0") != "1.0" or node.get("color"):
                warnings.append("华为文字字号或颜色已转换为默认排版。")
            node.tag = "br" if node in empty_fonts else "span"
        elif node.tag == "tab_span":
            node.tag = "span"
            warnings.append("华为自定义列表缩进已转换为普通段落。")
    blocks, clean_warnings = parse_html(html.tostring(root, encoding="unicode"),
                                       {"cid:" + a.id: a.id for a in assets or []})
    if assets is not None and {b.attachment_id for b in blocks if b.kind == "attachment"} != {a.id for a in assets}:
        warnings.append("华为云端附件与正文引用不一致，当前笔记需要核对。")
    if assets is None and (data.get("fileList") or entry.get("attachments") or content.get("has_attachment")):
        warnings.append("这条华为备忘录包含尚未下载的附件，当前正文可能不完整。")
    folder = str(content.get("tag_id", ""))
    title = content.get("title") or ""
    # The official list title also contains a body preview; data5 holds the explicitly edited title.
    try:
        title_data = json.loads(content.get("data5") or "{}")
        if isinstance(title_data, dict) and title_data.get("data2") == "edit" and isinstance(title_data.get("data1"), str):
            title = title_data["data1"]
    except (TypeError, ValueError):
        warnings.append("华为标题元数据无法解析，已保留目录标题。")
    return NoteDocument(
        platform=PlatformId.HUAWEI,
        account_id=account,
        source_id=entry["guid"],
        title=title,
        blocks=blocks,
        attachments=assets or [],
        source_folder_id=folder,
        source_folder_name=folders.get(folder),
        created_at=timestamp(content.get("created")),
        updated_at=timestamp(content.get("modified")),
        warnings=list(dict.fromkeys(warnings + clean_warnings)),
    )


class HuaweiProvider(Provider):
    spec = SPECS[PlatformId.HUAWEI]
    read_supported = True
    # Controlled write/readback supports the PNG/JPEG developer preview.
    write_supported = True
    images_supported = True

    def __init__(self, transport: Transport, resources: Path):
        self.transport, self.resources = transport, resources
        self._files = HuaweiFiles(self)

    def _json(self, path, data, **kwargs):
        headers = {"Referer": "https://cloud.huawei.com/home", "Origin": "https://cloud.huawei.com",
                   "Content-Type": "application/json;charset=utf-8", "Cache-Control": "no-cache"}
        csrf = self.transport.cookie("CSRFToken")
        if csrf:
            headers["CSRFToken"] = csrf
        payload = self.transport.json(
            "POST", "/notepad/" + path, json={**data, "traceId": trace_id()}, headers=headers, **kwargs
        )
        if not isinstance(payload.get("Result"), dict) or payload["Result"].get("code") != "0":
            if kwargs.get("write"):
                raise WriteUncertain()
            raise BridgeError("platform_response", "华为云服务未确认请求成功，请检查备忘录入口及验证状态。")
        if not isinstance(payload.get("rspInfo"), dict):
            if kwargs.get("write"):
                raise WriteUncertain()
            raise BridgeError("protocol_changed", "华为备忘录响应结构无法识别。")
        return payload

    def probe(self):
        uid = self.transport.cookie("userId")
        if not uid:
            raise BridgeError("login_incomplete", "请在华为官方窗口完成登录并进入备忘录。")
        self._json("notetag/query", {"index": 0})
        self.account_id = account_fingerprint(self.spec.id, uid)
        return self.account_id

    def _listing(self, **kwargs):
        payload = self._json("simplenote/query", {"index": 0, "status": 0, "guids": ""}, **kwargs)
        rows = payload["rspInfo"].get("noteList")
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not r.get("guid") for r in rows):
            raise BridgeError("protocol_changed", "华为备忘录目录结构无法识别。")
        if len({r["guid"] for r in rows}) != len(rows):
            raise BridgeError("pagination_incomplete", "华为目录返回重复笔记，已停止保存。")
        return payload, rows

    def fetch(self, context: TaskContext):
        if not self.account_id:
            raise BridgeError("login_required", "请先登录华为账号。")
        kwargs = {"check_cancel": context.check_cancel, "wait": context.cancelled.wait}
        from .huawei_groups import parse_tags
        tags = parse_tags(self._json("notetag/query", {"index": 0}, **kwargs))
        folders = {str(tag["uuid"]): tag["name"] for tag in tags}
        listing, rows = self._listing(**kwargs)
        context.update(total=len(rows), stage="正在读取华为备忘录正文。")
        notes, complete = [], True
        for index, row in enumerate(rows):
            detail = self._json(
                "note/query",
                {
                    "ctagNoteInfo": listing.get("ctagNoteInfo", ""),
                    "startCursor": listing.get("startCursor", ""),
                    "guid": row["guid"],
                    "kind": row["kind"],
                },
                **kwargs,
            )["rspInfo"]
            if detail.get("guid") != row["guid"]:
                raise BridgeError("detail_mismatch", "华为详情与请求笔记不一致。")
            try:
                assets = []
                with self._files.note_download_scope(row["guid"]) as download:
                    for metadata in detail.get("attachments") or []:
                        if not isinstance(metadata, dict) or not isinstance(metadata.get("usage"), str):
                            raise BridgeError("protocol_changed", "华为附件名称无法确认。")
                        asset = Attachment(id=metadata.get("assetId", ""), name=metadata["usage"], kind="image")
                        download(asset, metadata, context)
                        assets.append(asset)
                note = parse_entry(detail, self.account_id, folders, assets if assets else None)
                complete = complete and not note.warnings
                notes.append(note)
            except BridgeError as error:
                if error.code in ("cancelled", "session_expired", "request_forbidden", "network_error", "rate_limited"):
                    raise
                complete = False
                context.issue(error.code, error.message, row["guid"])
            context.update(completed=index + 1, succeeded=len(notes))
        original_account = self.account_id
        if self.probe() != original_account:
            raise BridgeError("account_changed", "读取期间华为账号发生变化，已停止保存。")
        _, final_rows = self._listing(**kwargs)
        if {(r["guid"], r.get("etag")) for r in final_rows} != {(r["guid"], r.get("etag")) for r in rows}:
            raise BridgeError("snapshot_changed", "读取期间华为目录或正文版本发生变化，请重新获取。")
        return Snapshot(notes, complete)

    def validate_notes(self, notes: list[NoteDocument]):
        super().validate_notes(notes)
        for note in notes:
            for asset in note.attachments:
                image_bytes(asset, self.resources)
            if len({a.id for a in note.attachments}) != len(note.attachments):
                raise BridgeError("attachment_mismatch", "华为图片标识重复，未开始上传。")
            encode_body(note, {a.id: "placeholder.jpg" for a in note.attachments} if note.attachments else None)

    def _empty_account_version(self, **kwargs):
        # Official 17.0.0.300 init merges common then home settings; its editor
        # selects format 19 for the recycle-bin switch, otherwise format 12.
        enabled = False
        for path in ("/html/getCommonParam", "/html/getHomeData"):
            headers = {**self._files.headers(), "Content-Type": "application/json;charset=utf-8"}
            config = self.transport.json("POST", path, json={"traceId": headers["x-hw-trace-id"]},
                                         headers=headers, **kwargs)
            if not isinstance(config, dict) or type(config.get("code")) is not int or config["code"] != 0:
                raise BridgeError("format_version_pending", "华为空账号的网页配置未确认格式版本，未执行创建。")
            if "notepadRecycleBinOnSwitch" in config:
                enabled = config["notepadRecycleBinOnSwitch"]
        if type(enabled) not in (bool, int) or enabled not in (False, True):
            raise BridgeError("format_version_pending", "华为空账号的格式开关无法识别，未执行创建。")
        return "19" if enabled else "12"

    def create(self, note: NoteDocument, context: TaskContext):
        self.preflight([note])
        if not self.account_id:
            raise BridgeError("login_required", "请先登录目标华为账号。")
        kwargs = {"check_cancel": context.check_cancel, "wait": context.cancelled.wait}
        tags = self._json("notetag/query", {"index": 0}, **kwargs)
        listing, rows = self._listing(**kwargs)
        sample = next((row for row in rows if row.get("kind") == "note"), None)
        detail = {}
        if sample:
            # Existing accounts still require an actually readable, supported format.
            detail = self._json("note/query", {"ctagNoteInfo": listing.get("ctagNoteInfo", ""),
                                "startCursor": listing.get("startCursor", ""), "guid": sample["guid"], "kind": "note"}, **kwargs)
            try:
                if detail["rspInfo"]["guid"] != sample["guid"]:
                    raise ValueError("detail_mismatch")
                version = json.loads(detail["rspInfo"]["data"])["content"]["version"]
                if version not in ("12", "19"):
                    raise ValueError("unverified_version")
            except (KeyError, TypeError, ValueError):
                raise BridgeError("format_version_pending", "华为当前正文格式版本尚未验证，未执行创建。") from None
        elif rows:
            raise BridgeError("format_version_pending", "华为目录包含未验证的笔记类型，未执行创建。")
        else:
            version = self._empty_account_version(**kwargs)
        if note.attachments:
            self._files.configuration()
        from .huawei_groups import target_group
        group_id, group_warnings = target_group(self, note, context)
        if group_id:
            tags = self._json("notetag/query", {"index": 0}, **kwargs)
            listing, rows = self._listing(**kwargs)
        skeleton = note.model_copy(deep=True)
        skeleton.attachments = []
        skeleton.blocks = [b for b in skeleton.blocks if b.kind != "attachment"]
        plain, body, warnings = encode_body(skeleton)
        current, guid = int(time.time() * 1000), "newNote" + uuid.uuid4().hex
        content = {"filedir": "", "delete_flag": 0, "fold_id": 0, "is_lunar": 0, "need_reminded": 0,
                   "prefix_uuid": "", "unstruct_uuid": "", "created": current, "modified": current,
                   "data6": "0", "data5": json.dumps({"data1": note.display_title, "data2": "edit"}, ensure_ascii=False),
                   "content": plain, "html_content": body, "title": note.display_title, "version": version,
                   "favorite": 0, "first_attach_name": "", "has_attachment": 0,
                   "has_todo": int(any(block.kind == "todo" for block in note.blocks)), "tag_id": group_id}
        encoded = {"guid": guid, "simpleNote": "", "fileList": [], "content": content,
                   "currentNotePadVersion": uuid.uuid4().hex[:4] + "-" + str(current) + "-" + str(uuid.uuid4().int % 100000).zfill(5)}
        context.record_remote_ids([guid])
        result = self._json("note/create", {"guid": guid, "ctagNoteInfo": listing.get("ctagNoteInfo", ""),
                            "ctagNoteTag": tags.get("ctagNoteTag", ""),
                            "startCursor": listing.get("startCursor", "") if group_id else detail.get("startCursor", listing.get("startCursor", "")),
                            "reqInfo": {"kind": "note", "data": json.dumps(encoded, ensure_ascii=False), "simpleNote": ""}},
                            write=True, **kwargs)
        cloud_id = result["rspInfo"].get("guid")
        if (not isinstance(cloud_id, str) or not cloud_id or cloud_id.startswith("newNote")
                or cloud_id in {row["guid"] for row in rows}):
            raise WriteUncertain()
        context.record_remote_ids([cloud_id])
        if note.attachments:
            try:
                created = result["rspInfo"]
                prefix, luid = created.get("uuid"), created.get("luid")
                if not prefix or not luid:
                    raise WriteUncertain()
                resources = [self._files.upload(a, cloud_id, prefix, context) for a in note.attachments]
                plain, body, warnings = encode_body(note, {a.id: r["usage"] for a, r in zip(note.attachments, resources)})
                fresh_listing, _ = self._listing(**kwargs)
                fresh = self._json("note/query", {"ctagNoteInfo": fresh_listing.get("ctagNoteInfo", ""),
                    "startCursor": fresh_listing.get("startCursor", ""), "guid": cloud_id, "kind": "note"}, **kwargs)
                if fresh["rspInfo"].get("guid") != cloud_id or not fresh["rspInfo"].get("etag"):
                    raise WriteUncertain()
                content.update(content=plain, html_content=body, prefix_uuid=prefix, unstruct_uuid=luid,
                    first_attach_name=resources[0]["usage"], has_attachment=1,
                    unstructure=json.dumps([{"name": r["usage"]} for r in resources]), modified=int(time.time() * 1000))
                # Native H4 shows that updates replace the creation's client GUID with the cloud GUID.
                # Existing records can retain a legacy body GUID when read, but that is not the write contract.
                encoded["guid"] = cloud_id
                encoded.update(fileList=[{"name": r["usage"]} for r in resources],
                    currentNotePadVersion=uuid.uuid4().hex[:4] + "-" + str(int(time.time() * 1000)) + "-" + str(uuid.uuid4().int % 100000).zfill(5))
                self._json("note/update", {"ctagNoteInfo": fresh_listing.get("ctagNoteInfo", ""),
                    "ctagNoteTag": tags.get("ctagNoteTag", ""), "startCursor": fresh.get("startCursor", fresh_listing.get("startCursor", "")),
                    "reqInfo": {"guid": cloud_id, "etag": fresh["rspInfo"]["etag"], "kind": "note",
                                "simpleNote": "", "data": json.dumps(encoded, ensure_ascii=False)}}, write=True, **kwargs)
                # An acknowledged update can leave a detail-only record absent from the official directory.
                # Such a record must keep its uncertain receipt, never become a confirmed migration.
                _, visible_rows = self._listing(**kwargs)
                if sum(row.get("guid") == cloud_id for row in visible_rows) != 1:
                    raise WriteUncertain()
            except BridgeError as error:
                if not isinstance(error, WriteUncertain):
                    context.issue(error.code, error.message, note.source_id)
                raise WriteUncertain() from None
        return CreatedNote([cloud_id], warnings + group_warnings)

    def close(self):
        self.transport.close()
        super().close()
