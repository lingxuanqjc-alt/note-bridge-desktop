"""Independent OPPO web image uploads: prepare, small file or parts, then merge.

The official client sends these file endpoints without the note encryption envelope.
"""

import hashlib
import io
import os
import re
import uuid

from PIL import Image

from ..errors import BridgeError, WriteUncertain
from ..models import Attachment
from ..paths import confined, safe_filename


def image_bytes(asset, resources):
    if asset.kind != "image" or not asset.local_path:
        raise BridgeError("unsupported_image", "OPPO 当前仅支持已下载的图片上传。")
    path = confined(resources, asset.local_path)
    if not path.is_file() or not 0 < path.stat().st_size < 104857600:
        raise BridgeError("unsupported_image", "图片缺失或达到 OPPO 网页的 100 MB 上限。")
    data = path.read_bytes()
    if len(data) != asset.size or hashlib.sha256(data).hexdigest() != asset.sha256:
        raise BridgeError("attachment_changed", "图片与缓存的大小或摘要不符，未执行上传。")
    try:
        with Image.open(io.BytesIO(data)) as picture:
            fmt = picture.format
            picture.verify()
        if fmt not in ("PNG", "JPEG"):
            raise ValueError()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise BridgeError("unsupported_image", "本版 OPPO 迁入仅接受校验通过的 PNG、JPEG 图片；其他格式可导出到本地。") from None
    return data, "image/" + {"PNG": "png", "JPEG": "jpeg", "GIF": "gif", "WEBP": "webp"}[fmt]


class OppoFiles:
    multipart_supported = False  # The multipart wire contract has only synthetic coverage.
    prefix = "/owork-server/web/note/v2/"

    def __init__(self, provider):
        self.provider = provider

    def _write(self, path, context, **kwargs):
        response = self.provider.transport.json("POST", self.prefix + path, write=True,
            headers={"Origin": "https://cloud.oppo.com", "Referer": "https://cloud.oppo.com/"},
            check_cancel=context.check_cancel, wait=context.cancelled.wait, **kwargs)
        if response.get("code") == 9530:
            raise BridgeError("cloud_space_full", "OPPO 云空间不足。")
        if response.get("code") != 0 or "data" not in response:
            raise WriteUncertain()
        return response["data"]

    def upload(self, asset, context):
        data, mime = image_bytes(asset, self.provider.resources)
        md5 = hashlib.md5(data).hexdigest()  # Vendor wire checksum; local integrity uses SHA256.
        marker = "prepare/" + uuid.uuid4().hex
        context.record_resource(marker, "allocated")
        prepared = self._write("prepare-file-upload", context, json={"fileSize": len(data), "fileMd5": md5})
        if not isinstance(prepared, dict):
            raise WriteUncertain()
        if prepared.get("alert"):
            raise BridgeError("cloud_space_full", "OPPO 提示云空间不足，未继续上传。")
        result = prepared
        if not prepared.get("existingFile"):
            apply_id, threshold = prepared.get("applyId"), prepared.get("smallFileThreshold")
            if not isinstance(apply_id, str) or not apply_id or type(threshold) is not int or threshold < 0:
                raise WriteUncertain()
            context.record_resource("apply/" + apply_id, "allocated")
            if len(data) <= threshold:
                result = self._write("small-file-upload", context,
                    data={"applyId": apply_id, "fileMd5": md5}, files={"file": (safe_filename(asset.name), data, mime)})
            else:
                if not self.multipart_supported:
                    raise BridgeError("multipart_upload_pending",
                        "OPPO 返回该图片需要分片上传，本版尚未支持；未发送笔记新增请求，已分配的上传资源保留待核对。")
                part_size = prepared.get("sliceSize") or 2097152
                if type(part_size) is not int or not 1 <= part_size <= 104857600:
                    raise WriteUncertain()
                self._write("big-file-part-init-upload", context, json={"totalSize": len(data), "applyId": apply_id})
                for start in range(0, len(data), part_size):
                    part = data[start:start + part_size]
                    self._write("big-file-part-upload", context, files={"file": ("blob", part, mime)},
                        data={"totalSize": len(data), "partSize": part_size, "partNumber": start // part_size + 1,
                              "chunkSize": len(part), "applyId": apply_id, "partMd5": hashlib.md5(part).hexdigest()})
                result = self._write("big-file-part-merge", context, json={"totalSize": len(data),
                    "totalSlices": (len(data) + part_size - 1) // part_size, "sliceSize": part_size,
                    "lastSliceSize": (len(data) - 1) % part_size + 1, "fileMd5": md5, "applyId": apply_id})
        if not isinstance(result, dict):
            raise WriteUncertain()
        cloud_id, identity, check = result.get("ocloudId"), result.get("id"), result.get("checkPayload")
        if (not isinstance(cloud_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", cloud_id)
                or not isinstance(identity, (str, int)) or isinstance(identity, bool) or not identity or not check):
            raise WriteUncertain()
        context.record_resource(cloud_id, "allocated")
        restored = Attachment(id=str(identity), name=asset.name, kind="image", mime=mime)
        self.download(restored, cloud_id, context, expected=asset)
        context.record_resource(cloud_id, "uploaded")
        context.record_resource(marker, "uploaded")
        if prepared.get("applyId"):
            context.record_resource("apply/" + str(prepared["applyId"]), "uploaded")
        return {"id": identity, "type": 0, "checkPayload": check, "url": "/" + cloud_id}

    def download(self, asset, cloud_id, context, expected=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", cloud_id):
            raise BridgeError("attachment_mismatch", "OPPO 文件标识无法确认。")
        directory = self.provider.resources / "oppo" / self.provider.account_id
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / (uuid.uuid4().hex + ".part")
        try:
            with self.provider.transport.request("GET", self.prefix + "file-download/" + cloud_id,
                    stream=True, check_cancel=context.check_cancel, wait=context.cancelled.wait) as response, temporary.open("xb") as stream:
                mime = response.headers.get("Content-Type", "").split(";")[0]
                if mime in ("text/html", "application/json"):
                    raise BridgeError("attachment_response", "OPPO 附件端点未返回文件。")
                size, checksum = 0, hashlib.sha256()
                for chunk in response.iter_content(65536):
                    context.check_cancel()
                    size += len(chunk)
                    if size >= 104857600 or (expected and size > expected.size):
                        raise BridgeError("attachment_changed", "OPPO 附件大小超过校验范围。")
                    stream.write(chunk)
                    checksum.update(chunk)
            if not size or (expected and (size != expected.size or checksum.hexdigest() != expected.sha256)):
                raise BridgeError("attachment_changed", "OPPO 附件大小或摘要校验失败。")
            if asset.kind == "image":
                try:
                    with Image.open(temporary) as picture:
                        picture.verify()
                except (OSError, ValueError, Image.DecompressionBombError):
                    raise BridgeError("attachment_response", "OPPO 图片无法解码。") from None
            target = directory / (checksum.hexdigest() + "-" + safe_filename(asset.name))
            os.replace(temporary, target)
            asset.local_path = target.relative_to(self.provider.resources).as_posix()
            asset.size, asset.sha256 = size, checksum.hexdigest()
            if mime:
                asset.mime = mime
            return asset
        finally:
            temporary.unlink(missing_ok=True)
