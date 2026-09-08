"""Independent Xiaomi KSS metadata encoding; preview upload uses one PNG/JPEG block.

The official desktop editor request was intercepted before transmission using a
synthetic image. See docs/XIAOMI-UPLOAD-PROTOCOL.md for the observed contract.
"""

import hashlib
import io
import json
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from PIL import Image

from ..errors import BridgeError, WriteUncertain
from ..paths import confined, safe_filename
from .transport import Transport, host_matches

BLOCK_BYTES = 4 * 1024 * 1024
UPLOAD_HOSTS = ("xmssdn.micloud.mi.com",)


def upload_filename(name, mime):
    """Give opaque cloud image names the extension of their verified bytes."""
    extensions = {"image/png": (".png",), "image/jpeg": (".jpg", ".jpeg"), "image/gif": (".gif",)}[mime]
    filename = safe_filename(name, limit=80)
    suffix = Path(filename).suffix
    if suffix.lower() not in extensions:
        filename += extensions[0]
    elif suffix != suffix.lower():
        filename = filename[:-len(suffix)] + suffix.lower()
    return filename


@contextmanager
def download_response(provider, asset, context):
    """Follow the observed file redirect without forwarding the account session."""
    response = provider.transport.request("GET", "/file/full", params={"type": "note_img", "fileid": asset.id},
        return_redirects=True, stream=True, check_cancel=context.check_cancel, wait=context.cancelled.wait)
    separate = None
    try:
        if 300 <= response.status_code < 400:
            location = response.headers.get("Location", "")
            try:
                url = urlsplit(urljoin(provider.transport.origin + "/file/full", location))
                safe = (url.scheme == "https" and url.hostname and host_matches(url.hostname, UPLOAD_HOSTS)
                        and url.port in (None, 443) and not url.username and not url.password
                        and not url.fragment and url.path.startswith("/") and not url.path.startswith("//"))
            except ValueError:
                safe = False
            if not safe:
                raise BridgeError("unsafe_attachment_url", "小米图片下载跳转地址未通过校验。")
            response.close()
            separate = Transport("https://" + url.hostname, UPLOAD_HOSTS)
            response = separate.request("GET", url.path + ("?" + url.query if url.query else ""),
                stream=True, check_cancel=context.check_cancel, wait=context.cancelled.wait)
        yield response
    finally:
        response.close()
        if separate:
            separate.close()


def upload_metadata(data: bytes, filename: str, mime: str) -> dict:
    """Describe unencrypted bytes without changing the image or making requests."""
    if not isinstance(data, bytes) or not data:
        raise BridgeError("attachment_empty", "图片为空，未开始小米上传。")
    if not isinstance(filename, str) or not filename or not isinstance(mime, str) or not mime.startswith("image/"):
        raise BridgeError("unsupported_image", "小米图片名称或类型不正确，未开始上传。")
    blocks = []
    for offset in range(0, len(data), BLOCK_BYTES):
        block = data[offset:offset + BLOCK_BYTES]
        blocks.append({
            "blob": {},  # Browser Blob JSON representation; bytes travel separately.
            "size": len(block),
            "md5": hashlib.md5(block, usedforsecurity=False).hexdigest(),
            "sha1": hashlib.sha1(block, usedforsecurity=False).hexdigest(),
        })
    return {
        "type": "note_img",
        "storage": {
            "filename": filename,
            "size": len(data),
            "sha1": hashlib.sha1(data, usedforsecurity=False).hexdigest(),
            "mimeType": mime,
            "kss": {"block_infos": blocks},
        },
    }


def image_bytes(asset, resources):
    if asset.kind != "image" or not asset.local_path:
        raise BridgeError("unsupported_image", "本版小米迁入仅支持已下载的 PNG、JPEG 图片；其他格式可导出到本地。")
    path = confined(resources, asset.local_path)
    if not path.is_file() or not 0 < path.stat().st_size <= BLOCK_BYTES:
        raise BridgeError("unsupported_image", "本版小米迁入仅支持 4 MiB 以内的单块图片；更大图片的多块上传尚未实测，可导出到本地。")
    data = path.read_bytes()
    if len(data) != asset.size or hashlib.sha256(data).hexdigest() != asset.sha256:
        raise BridgeError("attachment_changed", "图片与缓存摘要不符，未开始小米上传。")
    try:
        with Image.open(io.BytesIO(data)) as picture:
            fmt = picture.format
            picture.verify()
        mime = {"PNG": "image/png", "JPEG": "image/jpeg"}[fmt]
    except (OSError, SyntaxError, ValueError, KeyError, Image.DecompressionBombError):
        raise BridgeError("unsupported_image", "本版小米迁入仅接受校验通过的 PNG、JPEG；GIF 等格式迁入尚未实测，可导出到本地。") from None
    return data, mime


class XiaomiFiles:
    """One-shot upload candidate; callers must establish non-encrypted scope first."""

    def __init__(self, provider):
        self.provider = provider

    def upload(self, asset, context):
        self.provider.require_unencrypted_mode()
        data, mime = image_bytes(asset, self.provider.resources)
        token = self.provider.transport.cookie("serviceToken")
        if not token:
            raise BridgeError("login_incomplete", "小米上传会话缺失，请重新登录。")
        metadata = upload_metadata(data, upload_filename(asset.name, mime), mime)
        marker = "xiaomi-upload/" + uuid.uuid4().hex
        upload_id = None
        context.record_resource(marker, "allocated")
        try:
            result = self.provider._json("POST", "/file/v2/user/request_upload_file",
                data={"data": json.dumps(metadata), "serviceToken": token},
                write=True, check_cancel=context.check_cancel)
            if not result.get("fileId"):
                storage = result.get("storage")
                if not isinstance(storage, dict) or not isinstance(storage.get("uploadId"), str) or not storage["uploadId"]:
                    raise WriteUncertain()
                context.record_resource("xiaomi-upload-id/" + storage["uploadId"], "allocated")
                upload_id = storage["uploadId"]
                kss = storage.get("kss")
                if not isinstance(kss, dict):
                    raise WriteUncertain()
                nodes, blocks = kss.get("node_urls"), kss.get("block_metas")
                if (not isinstance(nodes, list) or not nodes or not isinstance(nodes[0], str)
                        or not isinstance(blocks, list) or len(blocks) != len(metadata["storage"]["kss"]["block_infos"])
                        or not isinstance(kss.get("file_meta"), str) or not kss["file_meta"]):
                    raise WriteUncertain()
                try:
                    url = urlsplit(nodes[0])
                    safe = (url.scheme == "https" and url.hostname and host_matches(url.hostname, UPLOAD_HOSTS)
                            and not url.username and not url.password and url.port in (None, 443) and not url.query
                            and not url.fragment and not url.path.startswith("//")
                            and "\\" not in unquote(url.path)
                            and not any(part in (".", "..") for part in unquote(url.path).split("/")))
                except ValueError:
                    safe = False
                if not safe:
                    raise BridgeError("unsafe_upload_url", "小米返回的文件上传主机尚未验证，已停止上传。")
                commits = []
                # Fresh transport deliberately has no account cookies or authorization.
                transport = Transport("https://" + url.hostname, UPLOAD_HOSTS)
                try:
                    for index, block in enumerate(blocks):
                        context.check_cancel()
                        if not isinstance(block, dict) or type(block.get("is_existed")) is not int or block["is_existed"] not in (0, 1):
                            raise WriteUncertain()
                        if block["is_existed"] == 1:
                            commit_meta = block.get("commit_meta")
                        else:
                            self.provider.require_unencrypted_mode()
                            if not isinstance(block.get("block_meta"), str) or not block["block_meta"]:
                                raise WriteUncertain()
                            response = transport.json("POST", url.path.rstrip("/") + "/upload_block_chunk", write=True,
                                params={"chunk_pos": 0, "file_meta": kss["file_meta"], "block_meta": block["block_meta"]},
                                data=data[index * BLOCK_BYTES:(index + 1) * BLOCK_BYTES],
                                headers={"Content-Type": "application/octet-stream", "Origin": "https://i.mi.com"},
                                check_cancel=context.check_cancel)
                            commit_meta = response.get("commit_meta")
                        if not isinstance(commit_meta, str) or not commit_meta:
                            raise WriteUncertain()
                        commits.append({"commit_meta": commit_meta})
                finally:
                    transport.close()
                commit = {"storage": {"uploadId": storage["uploadId"], "size": len(data),
                    "sha1": metadata["storage"]["sha1"], "kss": {"file_meta": kss["file_meta"], "commit_metas": commits}}}
                result = self.provider._json("POST", "/file/v2/user/commit",
                    data={"commit": json.dumps(commit), "serviceToken": token},
                    write=True, check_cancel=context.check_cancel)
            file_id = result.get("fileId")
            if (not isinstance(file_id, str) or not file_id or result.get("digest") != metadata["storage"]["sha1"]
                    or result.get("mimeType", mime) not in ("", mime)):
                raise WriteUncertain()
            context.record_resource("xiaomi-file/" + file_id, "allocated")
            downloaded = asset.model_copy(update={"id": file_id, "mime": mime, "local_path": None})
            self.provider._download(downloaded, context)
            if downloaded.sha256 != asset.sha256 or downloaded.size != asset.size:
                raise BridgeError("attachment_changed", "小米上传后原件校验不一致，已停止关联笔记。")
            context.record_resource("xiaomi-file/" + file_id, "uploaded")
            if upload_id:
                context.record_resource("xiaomi-upload-id/" + upload_id, "uploaded")
            context.record_resource(marker, "uploaded")
            return {"fileId": file_id, "mimeType": mime, "digest": metadata["storage"]["sha1"]}
        except BridgeError as error:
            if type(getattr(error, "vendor_code", None)) is int:
                context.issue("platform_write_response", f"小米写入响应未确认，平台代码：{error.vendor_code}。")
            if not isinstance(error, WriteUncertain):
                context.issue(error.code, error.message)
            raise WriteUncertain() from None
