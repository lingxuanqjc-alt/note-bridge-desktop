"""Flyme note attachments, observed in the official notes.flyme.cn client.

addFileToTemp receives file, uuid and type=1 for images. The body references the
basename of returnValue.tempPath; a subsequent updatenote associates that file.
"""

import hashlib
import io
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image

from ..errors import BridgeError, WriteUncertain
from ..paths import confined, safe_filename
from .transport import Transport, host_matches


def image_bytes(asset, resources):
    if asset.kind != "image" or not asset.local_path:
        raise BridgeError("unsupported_image", "魅族当前仅支持已下载的图片上传。")
    path = confined(resources, asset.local_path)
    if not path.is_file() or not 0 < path.stat().st_size <= 209715200:
        raise BridgeError("unsupported_image", "图片缺失或超过本地 200 MB 处理上限。")
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
        raise BridgeError("unsupported_image", "本版魅族迁入仅接受校验通过的 PNG、JPEG 图片；其他格式可导出到本地。") from None
    return data, "image/" + {"PNG": "png", "JPEG": "jpeg"}[fmt]


class MeizuFiles:
    def __init__(self, provider):
        self.provider = provider

    def download(self, asset, context, expected=None):
        url = urlsplit(asset.download_url or "")
        if (url.scheme not in ("", "https") or url.username or url.password or url.fragment
                or url.port or not url.path.startswith("/") or url.path.startswith("//")
                or (url.netloc and (not url.hostname or not host_matches(url.hostname, self.provider.spec.domains)))
                or (url.netloc and url.scheme != "https")):
            raise BridgeError("unsafe_attachment_url", "魅族附件下载地址无法确认。")
        transport = self.provider.transport
        separate = url.hostname and "https://" + url.hostname != transport.origin
        if separate:
            transport = Transport("https://" + url.hostname, self.provider.spec.domains)
            transport.session.cookies.update(self.provider.transport.session.cookies)
        directory = self.provider.resources / "meizu" / self.provider.account_id
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / (uuid.uuid4().hex + ".part")
        try:
            path = url.path + ("?" + url.query if url.query else "")
            with transport.request("GET", path, stream=True, check_cancel=context.check_cancel,
                                   wait=context.cancelled.wait) as response, temporary.open("xb") as stream:
                mime = response.headers.get("Content-Type", "").split(";")[0]
                if mime in ("text/html", "application/json"):
                    raise BridgeError("attachment_response", "魅族附件端点返回了错误页面。")
                size, digest = 0, hashlib.sha256()
                for chunk in response.iter_content(65536):
                    context.check_cancel()
                    size += len(chunk)
                    if size > 209715200 or (expected and size > expected.size):
                        raise BridgeError("attachment_changed", "魅族附件大小超过校验范围。")
                    stream.write(chunk)
                    digest.update(chunk)
            if not size or (expected and (size != expected.size or digest.hexdigest() != expected.sha256)):
                raise BridgeError("attachment_changed", "魅族附件大小或摘要校验失败。")
            if asset.kind == "image":
                try:
                    with Image.open(temporary) as picture:
                        picture.verify()
                except (OSError, ValueError, Image.DecompressionBombError):
                    raise BridgeError("attachment_response", "魅族图片无法解码。") from None
            target = directory / (digest.hexdigest() + "-" + safe_filename(asset.name))
            os.replace(temporary, target)
            asset.local_path = target.relative_to(self.provider.resources).as_posix()
            asset.size, asset.sha256 = size, digest.hexdigest()
            if mime:
                asset.mime = mime
            return asset
        finally:
            temporary.unlink(missing_ok=True)
            if separate:
                transport.close()

    def upload(self, asset, note_id, context):
        data, mime = image_bytes(asset, self.provider.resources)
        # The official renderer identifies images by filename suffix. Cloud sources
        # such as WPS can supply opaque names; use the verified bytes, not input MIME.
        extensions = {"image/png": (".png",), "image/jpeg": (".jpg", ".jpeg"),
                      "image/gif": (".gif",), "image/webp": (".webp",)}[mime]
        filename = safe_filename(asset.name, limit=80)
        suffix = Path(filename).suffix
        if suffix.lower() not in extensions:
            filename += extensions[0]
        else:
            filename = filename[:-len(suffix)] + suffix.lower()
        marker = note_id + "/upload/" + uuid.uuid4().hex
        context.record_resource(marker, "allocated")
        result = self.provider.transport.json("POST", "/c/browser/note/addFileToTemp", write=True,
            data={"uuid": note_id, "type": "1"},
            files={"file": (filename, data, mime)},
            headers={"Origin": "https://notes.flyme.cn", "Referer": "https://notes.flyme.cn/notes"},
            check_cancel=context.check_cancel, wait=context.cancelled.wait)
        returned = result.get("returnValue")
        if result.get("returnCode") != 200 or not isinstance(returned, dict):
            raise WriteUncertain()
        path = returned.get("tempPath")
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise WriteUncertain()
        url = urlsplit(path)
        name = Path(url.path).name
        if not name or name in (".", "..") or url.query or url.fragment:
            raise WriteUncertain()
        context.record_resource(path, "allocated")
        temporary_asset = asset.model_copy(update={"id": name, "name": name, "download_url": path})
        self.download(temporary_asset, context, expected=asset)
        context.record_resource(path, "uploaded")
        context.record_resource(marker, "uploaded")
        return name
