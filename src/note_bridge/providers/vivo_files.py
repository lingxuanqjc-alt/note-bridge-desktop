"""Vivo note file upload from official client modules 192 and 324.

Only vendor-scoped, in-memory file credentials reach its cloud-file host. Allocated
objects stay in the task journal even when the note has not yet been created.
"""

import hashlib
import io
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image

from ..errors import BridgeError, WriteUncertain
from ..models import Attachment
from ..paths import confined
from ..tasks import TaskContext
from .transport import Transport


def file_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme in ("http", "https") and parsed.hostname
                and parsed.hostname.startswith("clouddisk-") and parsed.hostname.endswith(".vivo.com.cn")
                and not parsed.username and not parsed.port and parsed.path in ("", "/")
                and not parsed.query and not parsed.fragment):
            return "https://" + parsed.hostname
    except (ValueError, TypeError):
        pass
    raise BridgeError("unsafe_attachment_url", "vivo 返回的资源域名无法确认。")


def image_data(attachment: Attachment, path: Path, resources: Path):
    path = confined(resources, path)
    if attachment.kind != "image" or not path.is_file() or not 0 < path.stat().st_size <= 209715200:
        raise BridgeError("unsupported_image", "图片不存在或超出 vivo 200 MB 限制，未开始上传。")
    data = path.read_bytes()
    if not attachment.sha256 or hashlib.sha256(data).hexdigest() != attachment.sha256:
        raise BridgeError("attachment_changed", "本地图片与缓存摘要不符，未开始上传。")
    if attachment.size != len(data):
        raise BridgeError("attachment_changed", "本地图片大小与缓存不符，未开始上传。")
    try:
        with Image.open(io.BytesIO(data)) as picture:
            size, format = picture.size, picture.format
            picture.verify()
        if format not in ("PNG", "JPEG"):
            raise ValueError("unverified_image_format")
    except (OSError, ValueError, Image.DecompressionBombError):
        raise BridgeError("unsupported_image", "本版 vivo 迁入仅接受校验通过的 PNG、JPEG 图片；其他格式可导出到本地。") from None
    extension = {"PNG": "png", "JPEG": "jpg"}[format]
    return data, size, extension


class VivoFiles:
    def __init__(self, transport: Transport, resources: Path):
        self.transport, self.resources = transport, resources
        self.sts = None

    def _metadata(self, name: str, data: dict, context: TaskContext, *, write=False):
        payload = self.transport.json("POST", f"/clouddisk-api/api/suite/web/meta/{name}.do",
                                      json=data, write=write, check_cancel=context.check_cancel,
                                      wait=context.cancelled.wait)
        requires_data = name in ("getStsToken", "preUpload")
        if payload.get("code") != 0 or (requires_data and not isinstance(payload.get("data"), dict)):
            if name == "preUpload" and payload.get("code") == 23000:
                raise BridgeError("cloud_space_full", "vivo 云空间不足，图片尚未上传。")
            if write:
                raise WriteUncertain()
            raise BridgeError("attachment_session", "vivo 文件会话无法确认。")
        return payload.get("data")

    def upload(self, attachment: Attachment, path: Path, context: TaskContext) -> dict:
        data, (width, height), extension = image_data(attachment, path, self.resources)
        if not self.sts:
            self.sts = self._metadata("getStsToken", {"tokenType": 1}, context).get("stsToken")
        openid = self.transport.session.headers.get("openId")
        if not isinstance(self.sts, str) or not self.sts or not openid:
            raise BridgeError("attachment_session", "vivo 文件会话未建立，未开始上传。")
        guid, current = uuid.uuid4().hex, int(time.time() * 1000)
        name, checksum = f"IMG_{guid}.{extension}", hashlib.md5(data).hexdigest()
        mime = "image/jpeg" if extension == "jpg" else "image/" + extension
        prepared = self._metadata("preUpload", {
            "name": name, "fileSize": len(data), "checkSum": checksum, "clientCreateTime": current,
            "width": width, "height": height, "metaType": 1, "rotate": 0, "duration": 0,
            "mimeType": mime, "source": "SKETCH", "category": "NOTES",
            "requestFrom": "SKETCH_NOTES", "relateFlag": 1, "checkSumVersion": "2",
        }, context, write=True)
        # A successful preUpload may allocate an object even before any file bytes are sent.
        transport = None
        try:
            meta_id = prepared.get("metaId")
            if not isinstance(meta_id, str) or not meta_id:
                raise WriteUncertain()
            context.record_resource(meta_id, "allocated")
            origin = file_origin(prepared.get("uploadUrl", ""))
            if type(prepared.get("needUpload")) is not bool or type(prepared.get("needAsPart")) is not bool:
                raise WriteUncertain()
            transport = Transport(origin, (urlsplit(origin).hostname,))

            def send(payload: bytes, endpoint: str, mode: int, part_index=None, *, preview=False):
                part_checksum = hashlib.md5(payload).hexdigest()
                headers = {"x-yun-ststoken": self.sts, "x-yun-openid": openid, "x-yun-metaid": meta_id,
                           "x-yun-checksum": part_checksum, "x-yun-checksumVersion": "2",
                           "Origin": "https://pc.vivo.com.cn", "Referer": "https://pc.vivo.com.cn/"}
                if part_index is not None:
                    headers.update({"x-yun-partidx": str(part_index), "x-yun-length": str(len(payload))})
                result = transport.json("POST", "/api/file/webdisk/" + endpoint + ".do",
                                        files={"file": ("IMG_Thumb.png" if preview else name, payload,
                                                        "image/png" if preview else mime)}, headers=headers,
                                        write=True, check_cancel=context.check_cancel)
                if result.get("code") == 25999:
                    self._metadata("confirmUpload", {"metaId": meta_id, "partIdx": part_index if part_index is not None else "",
                                                       "mode": mode, "checkSum": part_checksum, "checkSumVersion": 2},
                                   context, write=True)
                elif result.get("code") != 0:
                    raise WriteUncertain()
                return part_checksum

            thumbnail = prepared.get("thumbnail")
            if isinstance(thumbnail, dict) and thumbnail.get("shouldCreate"):
                # The official preview is a bounded PNG. The source upload retains its original bytes.
                with Image.open(io.BytesIO(data)) as picture:
                    picture.thumbnail((540, 540))
                    preview = io.BytesIO()
                    picture.convert("RGBA").save(preview, format="PNG")
                send(preview.getvalue(), "thumbUpload", 3, preview=True)
            if prepared["needUpload"]:
                if prepared["needAsPart"]:
                    part_size = prepared.get("partSize")
                    if type(part_size) is not int or part_size <= 0:
                        raise WriteUncertain()
                    checksums = [send(data[offset:offset + part_size], "rangeUpload", 2, index)
                                 for index, offset in enumerate(range(0, len(data), part_size))]
                    self._metadata("confirmRangeUpload", {"metaId": meta_id, "fileCheckSum": checksum,
                                                          "checkSumList": checksums}, context, write=True)
                else:
                    send(data, "upload", 1)
            context.record_resource(meta_id, "uploaded")
            return {"guid": guid, "name": name, "mime": extension, "resourceKey": meta_id,
                    "domainAddr": origin, "resourceSize": len(data), "resType": 1,
                    "createTime": current, "updateTime": current, "fileID": meta_id,
                    "dirty": 1, "deleted": 1, "sort": 0, "category": 3}
        except BridgeError as error:
            if not isinstance(error, WriteUncertain):
                context.issue(error.code, error.message)
            raise WriteUncertain() from None
        finally:
            if transport:
                transport.close()

    def close(self):
        self.sts = None
