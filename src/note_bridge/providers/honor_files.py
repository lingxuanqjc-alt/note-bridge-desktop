"""Honor's official web file protocol: preCreateFile, multipart upload, singleFileDownstream.

Protocol fields come from the official editor, not copied implementation code.
Multipart API: https://requests.readthedocs.io/en/latest/user/quickstart/#post-a-multipart-encoded-file
Image verification: https://pillow.readthedocs.io/en/stable/reference/Image.html#PIL.Image.Image.verify
"""

import hashlib
import io
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image

from ..errors import BridgeError, WriteUncertain
from ..models import Attachment
from ..paths import confined, safe_filename
from .transport import Transport, host_matches


def image_data(asset, resources):
    if asset.kind != "image" or not asset.local_path:
        raise BridgeError("unsupported_image", "荣耀当前仅支持已下载的图片上传。")
    path = confined(resources, asset.local_path)
    if not path.is_file() or not 0 < path.stat().st_size <= 209715200:
        raise BridgeError("unsupported_image", "图片缺失或超过本地 200 MB 处理上限。")
    data = path.read_bytes()
    if len(data) != asset.size or hashlib.sha256(data).hexdigest() != asset.sha256:
        raise BridgeError("attachment_changed", "图片与缓存的大小或摘要不符，未执行上传。")
    try:
        with Image.open(io.BytesIO(data)) as picture:
            size, fmt = picture.size, picture.format
            picture.verify()
        if fmt not in ("PNG", "JPEG"):
            raise ValueError()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise BridgeError("unsupported_image", "本版荣耀迁入仅接受校验通过的 PNG、JPEG 图片；其他格式可导出到本地。") from None
    return data, size, {"PNG": "png", "JPEG": "jpeg"}[fmt]


class HonorFiles:
    def __init__(self, provider):
        self.provider = provider
        self.transport = None

    def _transport(self):
        if self.transport is None:
            config = self.provider._json("GET", "config/web")
            parsed = urlsplit(config.get("viteDownloadDomain", ""))
            if (parsed.scheme != "https" or not parsed.hostname
                    or not host_matches(parsed.hostname, self.provider.spec.domains)
                    or parsed.username or parsed.port or parsed.path not in ("", "/")
                    or parsed.query or parsed.fragment):
                raise BridgeError("unsafe_attachment_url", "荣耀文件服务地址无法确认。")
            self.transport = Transport("https://" + parsed.hostname, self.provider.spec.domains)
            self.transport.session.cookies.update(self.provider.transport.session.cookies)
        return self.transport

    def _headers(self):
        csrf = self.provider.transport.cookie("hc_CSRF_Token")
        if not csrf:
            raise BridgeError("session_expired", "荣耀文件会话已失效。")
        return {"Csrftoken": csrf, "Origin": "https://cloud.honor.com",
                "Referer": "https://cloud.honor.com/", "Device-Type": "1"}

    def upload(self, asset, note_id, container, context):
        data, (width, height), extension = image_data(asset, self.provider.resources)
        transport = self._transport()
        preview_id = str(uuid.uuid4()).replace("-", "$")
        prefix = f"{preview_id}_{width}_{height}_{int(time.time() * 1000)}"
        result = []
        # The preview is a separate object; retaining original bytes avoids recompression loss.
        for original in (True, False):
            name = prefix + ("_original" if original else "") + "." + extension
            record = {"uuid": str(uuid.uuid4()).replace("-", "$") if original else preview_id,
                      "filename": name, "mimetype": "image/" + extension, "parent_uuid": note_id,
                      "size": len(data), "hash": hashlib.sha256(data).hexdigest(), "attach_type": 2}
            resource_key = container + "/" + name
            # Preserve the allocated path before an upload can have an unknown result.
            context.record_resource(resource_key, "allocated")
            prepared = self.provider._json("POST", "notepad/file/preCreateFile", params={"appName": "notepad"},
                                json={"name": name, "uud": container, "size": len(data), "hash": record["hash"]},
                                write=True, check_cancel=context.check_cancel, wait=context.cancelled.wait)
            if prepared and prepared.get("existing_file"):
                self._download_one(record, record["uuid"], container, context)
            payload = transport.json(
                "POST", "/portal/notepad/file/upload", write=True,
                params={"cloudPath": "/sync/notepad/" + resource_key}, headers=self._headers(),
                files={"file": (name, data, record["mimetype"]),
                       "attachment": ("blob", json.dumps(record), "application/json")},
                check_cancel=context.check_cancel, wait=context.cancelled.wait,
            )
            if payload.get("code") != 0:
                raise WriteUncertain()
            # Confirm bytes at this allocated path before claiming an uploaded resource.
            self._download_one(record, record["uuid"], container, context)
            context.record_resource(resource_key, "uploaded")
            result.append(record)
        return preview_id, result

    def download(self, entry, context):
        rows = entry.get("attachments") or []
        if not isinstance(rows, list):
            raise BridgeError("protocol_changed", "荣耀附件列表结构无法识别。")
        container, note_id = entry.get("unstruct_guid"), entry["uuid"]
        if not rows:
            return []
        if not isinstance(container, str) or not re.fullmatch(r"[A-Za-z0-9$-]{1,80}", container):
            raise BridgeError("attachment_mismatch", "荣耀附件目录标识无法确认。")
        for row in rows:
            if (not isinstance(row, dict) or row.get("parent_uuid") != note_id
                    or not isinstance(row.get("filename"), str)
                    or not re.fullmatch(r"[A-Za-z0-9$_.-]{1,200}", row["filename"])
                    or not row.get("uuid") or type(row.get("size")) is not int
                    or not 0 < row["size"] <= 209715200
                    or not re.fullmatch(r"[0-9a-fA-F]{64}", str(row.get("hash", "")))):
                raise BridgeError("attachment_mismatch", "荣耀附件的归属或摘要无法确认。")
        if len({row["uuid"] for row in rows}) != len(rows):
            raise BridgeError("attachment_mismatch", "荣耀附件标识重复。")
        assets, handled = [], set()
        for row in rows:
            if row["uuid"] in handled:
                continue
            selected, identity = row, row["uuid"]
            if row.get("mimetype", "").startswith("image/"):
                pair = [r for r in rows if r["filename"].split("_")[0] == row["filename"].split("_")[0]]
                originals = [r for r in pair if "_original." in r["filename"]]
                previews = [r for r in pair if "_original." not in r["filename"]]
                if len(pair) > 1:
                    if len(originals) != 1 or len(previews) != 1:
                        raise BridgeError("attachment_mismatch", "荣耀原图和预览图未能唯一配对。")
                    selected, identity = originals[0], previews[0]["uuid"]
                    handled.update(r["uuid"] for r in pair)
            handled.add(row["uuid"])
            assets.append(self._download_one(selected, identity, container, context, use_cache=True))
        return assets

    def _download_one(self, row, identity, container, context, *, use_cache=False):
        mime = row.get("mimetype", "application/octet-stream")
        kind = mime.split("/")[0]
        if kind not in ("image", "audio", "video"):
            kind = "file"
        digest = row["hash"].lower()
        directory = self.provider.resources / "honor" / self.provider.account_id
        directory.mkdir(parents=True, exist_ok=True)
        suffix = Path(row["filename"]).suffix[:12]
        target = directory / (digest + suffix)
        asset = Attachment(id=identity, name=safe_filename(row["filename"]), kind=kind, mime=mime,
                           local_path=target.relative_to(self.provider.resources).as_posix(),
                           sha256=digest, size=row["size"])
        if use_cache:
            context.check_cancel()
            try:
                cached = target.resolve()
                if (cached.is_relative_to(directory.resolve()) and cached.is_file()
                        and cached.stat().st_size == row["size"]):
                    size, checksum = 0, hashlib.sha256()
                    with cached.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(65536), b""):
                            context.check_cancel()
                            size += len(chunk)
                            if size > row["size"]:
                                break
                            checksum.update(chunk)
                    if size == row["size"] and checksum.hexdigest() == digest:
                        context.check_cancel()
                        return asset
            except OSError:
                pass  # An unreadable cache does not replace the remote acquisition path.
        temporary = directory / (uuid.uuid4().hex + ".tmp")
        try:
            with self._transport().request(
                "GET", "/portal/notepad/file/singleFileDownstream",
                params={"cloudPath": "/sync/notepad/" + container + "/" + row["filename"]},
                headers=self._headers(), stream=True, check_cancel=context.check_cancel,
                wait=context.cancelled.wait,
            ) as response, temporary.open("xb") as output:
                size, checksum = 0, hashlib.sha256()
                for chunk in response.iter_content(65536):
                    context.check_cancel()
                    size += len(chunk)
                    if size > row["size"]:
                        raise BridgeError("attachment_changed", "荣耀附件大小与记录不符。")
                    output.write(chunk)
                    checksum.update(chunk)
            if size != row["size"] or checksum.hexdigest() != digest:
                raise BridgeError("attachment_changed", "荣耀附件大小或摘要校验失败。")
            os.replace(temporary, target)
            return asset
        finally:
            temporary.unlink(missing_ok=True)

    def close(self):
        if self.transport:
            self.transport.close()
            self.transport = None
