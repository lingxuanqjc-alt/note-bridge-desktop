"""Flyme web-note protocol, independently implemented from the official 2026-09-05 page.

The browser's body is a JSON block list, with UTF-16 style offsets (not HTML).
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path

from ..errors import BridgeError, WriteUncertain
from ..exporter import stamp
from ..models import Attachment, Block, NoteDocument, PlatformId, Span, account_fingerprint
from ..richtext import safe_link
from ..tasks import TaskContext
from .base import SPECS, CreatedNote, Provider, Snapshot
from .meizu_files import MeizuFiles, image_bytes
from .transport import Transport
from .xiaomi import timestamp


def decode_spans(text: str, encoded: str | None, warnings: list[str]) -> list[Span]:
    if not isinstance(text, str):
        raise BridgeError("protocol_changed", "魅族笔记文本无法识别。")
    try:
        ranges = json.loads(encoded) if encoded else []
    except (ValueError, TypeError):
        raise BridgeError("protocol_changed", "魅族文字样式无法识别。") from None
    if not isinstance(ranges, list):
        raise BridgeError("protocol_changed", "魅族文字样式不是区间列表。")
    # JavaScript offsets count surrogate pairs as two units. Work only at whole-character boundaries.
    positions = [0]
    for char in text:
        positions.append(positions[-1] + len(char.encode("utf-16-le")) // 2)
    boundaries = set(positions)
    formats = {1: "bold", 7: "italic", 6: "underline", 18: "strike", 19: "highlight"}
    parsed = []
    for item in ranges:
        if not isinstance(item, dict) or not all(type(item.get(k)) is int for k in ("span", "start", "end")):
            raise BridgeError("protocol_changed", "魅族文字样式区间缺少字段。")
        start, end = item["start"], item["end"]
        if start not in boundaries or end not in boundaries or end < start:
            raise BridgeError("protocol_changed", "魅族文字样式区间不完整，已停止转换。")
        code, param = item["span"], item.get("param")
        if code == 1 and param in (0, 2, 3):
            if param:
                warnings.append("段落对齐已转换为默认排版。")
            continue
        if code in formats:
            parsed.append((start, end, formats[code], True))
        elif code == 17:
            link = safe_link(param) if isinstance(param, str) else None
            if link:
                parsed.append((start, end, "link", link))
            else:
                warnings.append("不安全或无法识别的链接已转为文字。")
        else:
            warnings.append("部分字号、颜色或未知文字样式已转换为默认排版。")
    cuts = sorted({0, len(text), *(positions.index(p) for r in parsed for p in r[:2])})
    spans = []
    for start, end in zip(cuts, cuts[1:]):
        flags = {flag: value for left, right, flag, value in parsed if left <= positions[start] < right}
        spans.append(Span(text=text[start:end], **flags))
    return spans


def parse_entry(
    entry: dict, account: str, folder_id: str | None = None, folder_name: str | None = None
) -> NoteDocument:
    if not isinstance(entry, dict) or not entry.get("uuid") or not isinstance(entry.get("body"), str):
        raise BridgeError("protocol_changed", "魅族笔记正文结构无法识别。")
    if entry.get("encrypt") not in (None, 0, "0"):
        raise BridgeError("encrypted_note", "这条魅族笔记已加密，尚未验证解密。")
    try:
        body = json.loads(entry["body"] or "[]")
    except ValueError:
        raise BridgeError("protocol_changed", "魅族正文不是可识别的区块数据。") from None
    warnings, blocks, assets = [], [], {}
    files = entry.get("files") or {}
    if not isinstance(files, dict):
        raise BridgeError("protocol_changed", "魅族附件目录无法识别。")

    def visit(items, level=1, ordered=None):
        if not isinstance(items, list) or level > 6:
            raise BridgeError("proprietary_content", "魅族笔记的列表嵌套或区块结构无法完整转换。")
        for item in items:
            if not isinstance(item, dict) or type(item.get("state")) is not int:
                raise BridgeError("protocol_changed", "魅族笔记区块缺少类型。")
            state = item["state"]
            if state in (50, 51):
                visit(item.get("children"), level + (ordered is not None), state == 50)
            elif state == 53:
                visit(item.get("children"), level, ordered)
            elif state in (0, 1, 2, 60):
                kind = (
                    "heading"
                    if state == 60
                    else "todo"
                    if state in (1, 2)
                    else "list"
                    if ordered is not None
                    else "paragraph"
                )
                heading_level = item.get("level", 1) if state == 60 else level
                if type(heading_level) is not int or not 1 <= heading_level <= 6:
                    raise BridgeError("protocol_changed", "魅族标题层级无法识别。")
                blocks.append(
                    Block(
                        kind=kind,
                        spans=decode_spans(item.get("text", ""), item.get("span"), warnings),
                        checked=state == 2,
                        ordered=bool(ordered),
                        level=heading_level,
                    )
                )
            elif state in (3, 4, 5):
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    raise BridgeError("protocol_changed", "魅族附件缺少标识。")
                url = files.get(name)
                assets[name] = Attachment(
                    id=name,
                    name=name,
                    kind={3: "image", 4: "audio", 5: "file"}[state],
                    mime=mimetypes.guess_type(name)[0] or "application/octet-stream",
                    download_url=url if isinstance(url, str) else None,
                )
                blocks.append(Block(kind="attachment", attachment_id=name))
            else:
                raise BridgeError("proprietary_content", "这条魅族笔记包含尚未验证的专有区块，已停止转换。")

    visit(body)
    for name, url in files.items():
        if name not in assets:
            assets[name] = Attachment(id=name, name=name, download_url=url if isinstance(url, str) else None)
            warnings.append("附件目录中存在正文未引用的文件，已单独保留。")
    return NoteDocument(
        platform=PlatformId.MEIZU,
        account_id=account,
        source_id=str(entry["uuid"]),
        title=entry.get("title") or "",
        blocks=blocks,
        attachments=list(assets.values()),
        source_folder_id=folder_id,
        source_folder_name=folder_name,
        created_at=timestamp(entry.get("createTime")),
        updated_at=timestamp(entry.get("modifyTime")),
        warnings=list(dict.fromkeys(warnings)),
    )


def encode_blocks(note: NoteDocument, images: dict[str, str] | None = None) -> tuple[list[dict], list[str]]:
    rows, warnings = [], []
    if images is not None and (set(images) != {a.id for a in note.attachments}
                              or set(images) != {b.attachment_id for b in note.blocks if b.kind == "attachment"}):
        raise BridgeError("attachment_mismatch", "魅族图片与正文引用不一致，未开始上传。")
    codes = {"bold": 1, "italic": 7, "underline": 6, "strike": 18, "highlight": 19, "link": 17}
    for block in note.blocks:
        if block.kind == "attachment":
            if images is None:
                raise BridgeError("attachment_upload_pending", "魅族附件上传尚未完成验证。")
            rows.append({"state": 3, "name": images[block.attachment_id]})
            continue
        state = (2 if block.checked else 1) if block.kind == "todo" else 60 if block.kind == "heading" else 0
        row, ranges, offset = {"state": state, "text": block.text}, [], 0
        for span in block.spans:
            length = len(span.text.encode("utf-16-le")) // 2
            for flag, code in codes.items():
                value = getattr(span, flag)
                if flag == "link":
                    value = safe_link(value)
                if value and length:
                    ranges.append({"span": code, "start": offset, "end": offset + length,
                                   "param": value if flag == "link" else {1: 1, 7: 2}.get(code, "")})
            if span.code:
                warnings.append("行内代码字体已转换为普通字体。")
            offset += length
        if ranges:
            row["span"] = json.dumps(ranges, ensure_ascii=False, separators=(",", ":"))
        if block.kind == "heading":
            row["level"] = block.level
        if block.kind == "table":
            row["text"] = "\n".join("\t".join(cells) for cells in block.rows)
            warnings.append("表格转换为制表符分隔文本。")
        elif block.kind == "divider":
            row["text"] = "────────────"
            warnings.append("分隔线转换为普通文字。")
        elif block.kind in ("quote", "code"):
            warnings.append("引用或代码块转换为普通段落。")
        if block.kind == "list":
            list_state = 50 if block.ordered else 51
            if block.level > 1:
                warnings.append("嵌套列表转换为单层列表。")
            if not rows or rows[-1]["state"] != list_state:
                rows.append({"state": list_state, "children": []})
            rows[-1]["children"].append({"state": 53, "children": [row]})
        else:
            rows.append(row)
    rows.append({"state": 0, "text": "来源笔记时间\n" + stamp(note)})
    if note.source_folder_name:
        rows.append({"state": 0, "text": "来源文件夹：" + note.source_folder_name})
    return rows, list(dict.fromkeys(warnings))


class MeizuProvider(Provider):
    spec = SPECS[PlatformId.MEIZU]
    read_supported = True
    # Controlled write/readback supports the PNG/JPEG developer preview.
    write_supported = True
    images_supported = True

    def __init__(self, transport: Transport, resources: Path):
        self.transport, self.resources = transport, resources
        self._uid = None
        self._files = MeizuFiles(self)

    def _json(self, path, *, mapping=True, **kwargs):
        payload = self.transport.json(
            "POST",
            "/c/browser/note/" + path,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            **kwargs,
        )
        if payload.get("returnCode") in (401, 302):
            raise BridgeError("session_expired", "魅族登录已失效，请重新登录。")
        if payload.get("returnCode") != 200 or (mapping and not isinstance(payload.get("returnValue"), dict)):
            if kwargs.get("write"):
                raise WriteUncertain()
            raise BridgeError("platform_response", "魅族云笔记未确认请求成功。")
        return payload.get("returnValue")

    def probe(self):
        data = self._json("gettags")
        uid = data.get("userId")
        if (
            not isinstance(uid, (str, int))
            or isinstance(uid, bool)
            or not uid
            or not isinstance(data.get("data"), list)
        ):
            raise BridgeError("login_incomplete", "请在魅族官方窗口进入云笔记后检查账号。")
        self._uid = str(uid)
        self.account_id = account_fingerprint(self.spec.id, self._uid)
        return self.account_id

    def fetch(self, context: TaskContext):
        if not self.account_id:
            raise BridgeError("login_required", "请先登录魅族账号。")
        from .meizu_groups import parse_tags
        tag_data = self._json("gettags", check_cancel=context.check_cancel, wait=context.cancelled.wait)
        if str(tag_data.get("userId")) != self._uid:
            raise BridgeError("account_changed", "读取期间魅族账号发生变化，已停止保存。")
        tags = parse_tags(tag_data)
        folders = {r["id"]: r["name"] for r in tags}
        notes, seen, complete, offset, total = [], set(), True, 0, None
        context.update(stage="正在读取魅族笔记。")
        for _ in range(10000):
            data = self._json(
                "getnotegroups",
                params={"start": offset, "length": 200, "groupUuid": "-1"},
                check_cancel=context.check_cancel,
                wait=context.cancelled.wait,
            )
            rows, count = data.get("content"), data.get("count")
            if not isinstance(rows, list) or type(count) is not int or count < 0:
                raise BridgeError("protocol_changed", "魅族分页结构无法识别。")
            if total is not None and total != count:
                raise BridgeError("snapshot_changed", "读取过程中魅族笔记数量发生变化，请重新获取。")
            total = count
            context.update(total=total)
            for row in rows:
                # The current web response uses 0 for a redacted owner. Bind it to the
                # authenticated gettags identity, checked again after the final page.
                if not isinstance(row, dict) or not row.get("uuid"):
                    raise BridgeError("protocol_changed", "魅族笔记缺少标识。")
                if row.get("userId") not in (0, "0") and str(row.get("userId")) != self._uid:
                    raise BridgeError("account_changed", "魅族笔记归属不一致，已停止获取。")
                key = str(row["uuid"])
                if key in seen:
                    raise BridgeError("pagination_repeated", "魅族分页返回重复笔记，已停止获取。")
                seen.add(key)
                try:
                    folder_id = row.get("groupStatus")
                    if folder_id is not None and not isinstance(folder_id, str):
                        raise BridgeError("protocol_changed", "魅族笔记分组标识无法识别。")
                    if folder_id not in (None, "", "-1") and folder_id not in folders:
                        raise BridgeError("group_missing", "魅族笔记所属分组不在当前目录，已停止转换。")
                    note = parse_entry(row, self.account_id, folder_id if folder_id in folders else None, folders.get(folder_id))
                    for asset in note.attachments:
                        try:
                            self._files.download(asset, context)
                        except BridgeError as error:
                            if error.code in ("cancelled", "session_expired", "request_forbidden", "network_error", "rate_limited"):
                                raise
                            complete = False
                            context.issue(error.code, error.message, key)
                            note.warnings.append("魅族附件下载未完成。")
                    notes.append(note)
                except BridgeError as error:
                    if error.code in ("cancelled", "session_expired", "request_forbidden", "network_error", "rate_limited"):
                        raise
                    complete = False
                    context.issue(error.code, error.message, key)
                context.update(completed=len(seen), succeeded=len(notes))
            offset += len(rows)
            if offset == total:
                break
            if not rows or offset > total:
                raise BridgeError("pagination_incomplete", "魅族返回数量与分页总数不一致。")
        else:
            raise BridgeError("pagination_limit", "魅族分页超过上限，已停止获取。")
        original_account = self.account_id
        if self.probe() != original_account:
            raise BridgeError("account_changed", "读取期间魅族账号发生变化，已停止保存。")
        final_tags = parse_tags(self._json("gettags", check_cancel=context.check_cancel, wait=context.cancelled.wait))
        if {r["id"]: r["name"] for r in final_tags} != folders:
            raise BridgeError("snapshot_changed", "读取期间魅族分组发生变化，请重新获取。")
        return Snapshot(notes, complete)

    def validate_notes(self, notes):
        super().validate_notes(notes)
        for note in notes:
            identities = {a.id: a.id for a in note.attachments}
            if len(identities) != len(note.attachments):
                raise BridgeError("attachment_mismatch", "魅族图片标识重复，未开始上传。")
            encode_blocks(note, identities if note.attachments else None)
            for asset in note.attachments:
                image_bytes(asset, self.resources)

    def create(self, note: NoteDocument, context: TaskContext) -> CreatedNote:
        self.preflight([note])
        if not self.account_id:
            raise BridgeError("login_required", "请先登录目标魅族账号。")
        from .meizu_groups import target_group
        group_id, group_warnings = target_group(self, note, context)
        initial = note.model_copy(update={"attachments": [], "blocks": [b for b in note.blocks if b.kind != "attachment"]})
        blocks, warnings = encode_blocks(initial)
        # Omitting uuid is the official client's new-note operation. Never reuse the source id.
        result = self._json("updatenote", data={
            "title": note.display_title, "topdate": 0, "fontSize": 18, "remind": "", "groupUuid": group_id,
            "body": json.dumps(blocks, ensure_ascii=False, separators=(",", ":")),
        }, write=True, check_cancel=context.check_cancel)
        remote_id = result.get("uuid")
        if not isinstance(remote_id, str) or not remote_id:
            raise WriteUncertain()
        if note.attachments:
            context.record_remote_ids([remote_id])
            try:
                images = {asset.id: self._files.upload(asset, remote_id, context) for asset in note.attachments}
                blocks, warnings = encode_blocks(note, images)
                result = self._json("updatenote", data={
                    "uuid": remote_id, "title": note.display_title, "topdate": 0, "fontSize": 18,
                    "remind": "", "groupUuid": group_id, "body": json.dumps(blocks, ensure_ascii=False, separators=(",", ":")),
                }, write=True, check_cancel=context.check_cancel)
                if result.get("uuid") != remote_id:
                    raise WriteUncertain()
            except BridgeError as error:
                if not isinstance(error, WriteUncertain):
                    context.issue(error.code, error.message, note.source_id)
                raise WriteUncertain() from None
        return CreatedNote([remote_id], warnings + group_warnings)

    def close(self):
        self._uid = None
        self.transport.close()
        super().close()
