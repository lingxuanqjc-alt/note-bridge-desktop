"""Huawei memo dataVersion 2.0 file proxy, independently implemented from its web client."""

import hashlib
import io
import os
import re
import secrets
import time
import uuid
from contextlib import contextmanager
from urllib.parse import quote, urlencode

from PIL import Image

from ..errors import BridgeError, WriteUncertain
from ..paths import confined


def trace_id():
    return "03133_02_" + str(int(time.time())) + "_" + "".join(str(1 + secrets.randbelow(9)) for _ in range(8))


def image_bytes(asset, resources):
    if asset.kind != "image" or not asset.local_path:
        raise BridgeError("unsupported_image", "华为当前只处理已下载的图片。")
    path = confined(resources, asset.local_path)
    if not path.is_file() or not 0 < path.stat().st_size <= 10485760:
        raise BridgeError("unsupported_image", "图片缺失或超过华为网页的 10 MB 上限。")
    data = path.read_bytes()
    if len(data) != asset.size or hashlib.sha256(data).hexdigest() != asset.sha256:
        raise BridgeError("attachment_changed", "图片大小或摘要发生变化，未开始上传。")
    try:
        with Image.open(io.BytesIO(data)) as picture:
            fmt, size = picture.format, picture.size
            picture.verify()
        if fmt not in ("PNG", "JPEG"):
            raise ValueError()
    except (ValueError, OSError, Image.DecompressionBombError):
        raise BridgeError("unsupported_image", "华为当前仅验证 PNG/JPEG 原件上传。") from None
    return data, size, "png" if fmt == "PNG" else "jpg"


class HuaweiFiles:
    def __init__(self, provider):
        self.provider = provider
        self.config = None
        self._download_active = False

    def headers(self):
        result = {"Origin": "https://cloud.huawei.com", "Referer": "https://cloud.huawei.com/home",
                  "x-hw-trace-id": trace_id()}
        csrf = self.provider.transport.cookie("CSRFToken")
        if csrf:
            result["CSRFToken"] = csrf
        return result

    def json(self, path, data, *, write=False):
        headers = self.headers()
        headers["Content-Type"] = "application/json;charset=utf-8"
        return self.provider.transport.json("POST", path, json=data, headers=headers, write=write)

    def configuration(self):
        if self.config is None:
            switch = self.json("/html/getAboutGet", {"moduleType": "notepad", "traceId": trace_id()})
            config = self.json("/html/getCommonParam", {"traceId": trace_id()})
            if (switch.get("code") != 0 or switch.get("dataSwitch") != 1
                    or switch.get("dataVersion") != "dataVersion=2.0" or config.get("code") != 0
                    or not isinstance(config.get("fileProxyGrayStrategyVersionValue"), str)):
                raise BridgeError("legacy_attachment_pending", "华为当前账号未确认使用已实现的新版附件协议。")
            self.config = config["fileProxyGrayStrategyVersionValue"]
        return self.config

    def upload(self, asset, note_id, prefix_uuid, context, *, usage=None):
        data, (width, height), extension = image_bytes(asset, self.provider.resources)
        if not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_$-]{1,150}", value) for value in (note_id, prefix_uuid)):
            raise BridgeError("protocol_changed", "华为新笔记附件标识无法确认。")
        if usage is not None and (not isinstance(usage, str) or not re.fullmatch(
                re.escape(f"{prefix_uuid}_{width}_{height}_") + r"\d{13}\." + extension, usage)):
            raise BridgeError("recovery_resource_mismatch", "恢复附件名称与这条笔记及图片规格不一致，未开始上传。")
        version = self.configuration()
        name = usage or f"{prefix_uuid}_{width}_{height}_{int(time.time() * 1000)}.{extension}"
        path = "/proxy/v1/upload/" + quote(f"/v2/1001/note/record/{note_id}/usage/{name}/assets", safe="") + "?uploadType=content&fields=*"
        context.record_resource(name, "allocated")
        context.check_cancel()
        prepared = self.json("/driveFileProxy/preUploadAttachmentProcess",
            {"needToSignUrl": path, "httpMethod": "POST", "generateSignFlag": True, "firstUploadFileFlag": True}, write=True)
        if prepared.get("code") != "0" or any(not prepared.get(key) for key in ("requestTimeStamp", "sign", "dataSyncUserLock")):
            raise WriteUncertain()
        mime = "image/png" if extension == "png" else "image/jpeg"
        headers = self.headers()
        headers.update({"x-hw-lock": prepared["dataSyncUserLock"], "x-hw-signature": prepared["sign"],
                        "x-hw-properties": urlencode({"fileName": name, "mimeType": mime}, quote_via=quote),
                        "x-hw-app-version": "17000300", "version": version})
        try:
            context.check_cancel()
            result = self.provider.transport.json("POST", path, params={"timeStamp": prepared["requestTimeStamp"]},
                headers=headers, files={"file": (name, data, mime)}, write=True)
        finally:
            # Release the server upload lock even when the upload result is unknown.
            self.json("/driveFileProxy/afterUploadAttachmentProcess", {}, write=True)
        if not all(isinstance(result.get(key), str) and result[key] for key in ("id", "versionId")):
            raise WriteUncertain()
        metadata = {"usage": name, "assetId": result["id"], "versionId": result["versionId"], "resourceLength": len(data)}
        context.record_resource(result["id"], "allocated")
        self.download(asset.model_copy(), note_id, metadata, context, expected=asset)
        context.record_resource(name, "uploaded")
        context.record_resource(result["id"], "uploaded")
        return metadata

    def download(self, asset, note_id, metadata, context, expected=None):
        # Upload verification and other individual callers prepare independently.
        with self.note_download_scope(note_id) as download:
            return download(asset, metadata, context, expected=expected)

    @contextmanager
    def note_download_scope(self, note_id):
        """Official getAttachmentUrl prepares once, then reads this note's images."""
        if self._download_active:
            raise BridgeError("attachment_scope_active", "已有一条华为笔记正在读取附件，请先结束该读取范围。")
        self._download_active = True
        active, version, account = True, None, self.provider.account_id

        def download(asset, metadata, context, expected=None):
            nonlocal active, version
            if not active:
                raise BridgeError("attachment_scope_closed", "本条笔记的附件读取范围已结束或失败，请重新准备。")
            try:
                context.check_cancel()
                if self.provider.account_id != account:
                    raise BridgeError("account_changed", "附件读取期间华为账号发生变化，已停止读取。")
                values = [note_id, metadata.get("assetId"), metadata.get("versionId")]
                if not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_$-]{1,150}", value) for value in values):
                    raise BridgeError("protocol_changed", "华为附件的笔记或版本标识无法确认。")
                if version is None:
                    version = self._prepare_download()
                return self._download(asset, note_id, metadata, context, expected, version)
            except BaseException:
                # Even if a caller catches the failure inside the with block,
                # it cannot reuse this preparation for another request/retry.
                active, version = False, None
                raise

        try:
            yield download
        finally:
            active, version = False, None
            self._download_active = False

    def _prepare_download(self):
        version = self.configuration()
        prepared = self.json("/proxyserver/driveFileProxy/preProcess", {"needToSignUrl": "", "httpMethod": "GET", "generateSignFlag": False})
        if prepared.get("code") != "0":
            raise BridgeError("attachment_session", "华为附件下载会话未确认。")
        return version

    def _download(self, asset, note_id, metadata, context, expected, version):
        route = "/v2/dataSync/callback/v1/1001/kind/note/record/" + quote(note_id, safe="")
        route += "/assets/" + quote(metadata["assetId"], safe="") + "/revisions/" + quote(metadata["versionId"], safe="")
        directory = self.provider.resources / "huawei" / self.provider.account_id
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / (uuid.uuid4().hex + ".part")
        context.check_cancel()
        try:
            response = self.provider.transport.request("GET", "/proxy/v1/download/" + quote(route, safe=""),
                params={"version": version}, headers={**self.headers(), "version": version}, stream=True)
            size, checksum = 0, hashlib.sha256()
            try:
                with temporary.open("xb") as output:
                    for chunk in response.iter_content(65536):
                        context.check_cancel()
                        size += len(chunk)
                        if size > 209715200 or (expected and size > expected.size):
                            raise BridgeError("attachment_changed", "华为附件超过本次校验范围。")
                        output.write(chunk)
                        checksum.update(chunk)
            finally:
                response.close()
            if not size or (expected and (size != expected.size or checksum.hexdigest() != expected.sha256)):
                raise BridgeError("attachment_changed", "华为附件大小或摘要不符。")
            if type(metadata.get("resourceLength")) is int and metadata["resourceLength"] != size:
                raise BridgeError("attachment_changed", "华为附件未达到记录大小。")
            try:
                with Image.open(temporary) as picture:
                    fmt = picture.format
                    picture.verify()
                extension = {"PNG": ".png", "JPEG": ".jpg", "GIF": ".gif", "WEBP": ".webp"}[fmt]
            except (ValueError, OSError, KeyError, Image.DecompressionBombError):
                raise BridgeError("attachment_response", "华为图片无法解码。") from None
            target = directory / (checksum.hexdigest() + extension)
            os.replace(temporary, target)
            asset.local_path = target.relative_to(self.provider.resources).as_posix()
            asset.size, asset.sha256 = size, checksum.hexdigest()
            asset.mime = "image/jpeg" if fmt == "JPEG" else "image/" + fmt.lower()
            return asset
        finally:
            temporary.unlink(missing_ok=True)
