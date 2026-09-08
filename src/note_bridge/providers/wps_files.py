"""WPS-scoped S3 objects; all credentials are supplied by the logged-in WPS service.

https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/put_object.html
https://docs.aws.amazon.com/boto3/latest/guide/configuration.html
"""

import hashlib
import io
import os
import re
import uuid

import boto3
import botocore.session
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image

from ..errors import BridgeError, WriteUncertain
from ..paths import confined


def image_bytes(asset, resources):
    if asset.kind != "image" or not asset.local_path:
        raise BridgeError("unsupported_image", "WPS 当前仅支持已下载的图片上传。")
    path = confined(resources, asset.local_path)
    if not path.is_file() or not 0 < path.stat().st_size <= 209715200:
        raise BridgeError("unsupported_image", "图片缺失或超过本地 200 MB 上限。")
    data = path.read_bytes()
    if len(data) != asset.size or hashlib.sha256(data).hexdigest() != asset.sha256:
        raise BridgeError("attachment_changed", "图片与缓存的大小或摘要不符，未执行上传。")
    try:
        with Image.open(io.BytesIO(data)) as picture:
            fmt, size = picture.format, picture.size
            picture.verify()
        if fmt not in ("PNG", "JPEG"):
            raise ValueError()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise BridgeError("unsupported_image", "本版 WPS 迁入仅接受校验通过的 PNG、JPEG 图片；其他格式可导出到本地。") from None
    return data, size


class WpsFiles:
    def __init__(self, provider):
        self.provider = provider
        self.clients = {}

    def _client(self, write=False):
        if write not in self.clients:
            config = self.provider.transport.json("POST", "/s3/request" + ("upload" if write else "download"), json={},
                headers={"Origin": "https://note.wps.cn", "Referer": "https://note.wps.cn/"})
            if (config.get("region") not in ("cn-north-1", "cn-northwest-1")
                    or not isinstance(config.get("bucket"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", config["bucket"])
                    or not isinstance(config.get("keyPrefix"), str) or not re.fullmatch(r"[A-Za-z0-9_/-]{0,200}", config["keyPrefix"])):
                raise BridgeError("protocol_changed", "WPS 文件存储范围无法确认。")
            values = [self.provider._codec.decrypt(config.get(key)) for key in ("accessKeyId", "secretAccessKey", "sessionToken")]
            if not all(values):
                raise BridgeError("session_expired", "WPS 文件会话未能确认。")
            # Do not consult the machine's AWS profiles, credential files or endpoint overrides.
            core = botocore.session.Session()
            core.set_config_variable("config_file", os.devnull)
            core.set_config_variable("credentials_file", os.devnull)
            core.set_config_variable("profile", None)
            core.set_credentials(*values)
            session = boto3.Session(botocore_session=core)
            client = session.client("s3", region_name=config["region"],
                aws_access_key_id=values[0], aws_secret_access_key=values[1], aws_session_token=values[2],
                config=Config(connect_timeout=10, read_timeout=35, retries={"total_max_attempts": 1},
                    signature_version="s3v4", s3={"addressing_style": "virtual"},
                    ignore_configured_endpoint_urls=True, request_checksum_calculation="when_required",
                    response_checksum_validation="when_required"))
            values.clear()
            self.clients[write] = (client, config["bucket"], config["keyPrefix"])
        return self.clients[write]

    def upload(self, asset, context):
        data, size = image_bytes(asset, self.provider.resources)
        client, bucket, prefix = self._client(True)
        key = uuid.uuid4().hex
        context.record_resource(key, "allocated")
        context.check_cancel()
        try:
            response = client.put_object(Bucket=bucket, Key=prefix + key, Body=data)
        except (BotoCoreError, ClientError):
            raise WriteUncertain() from None
        if response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200:
            raise WriteUncertain()
        self.download(asset.model_copy(), key, context, expected=asset)
        context.record_resource(key, "uploaded")
        return key, size

    def upload_body(self, note_id, data, context):
        if not re.fullmatch(r"[a-f0-9]{32}", note_id) or not 0 < len(data) <= 209715200:
            raise BridgeError("invalid_body", "WPS 长正文标识或大小无法确认。")
        client, bucket, prefix = self._client(True)
        key = prefix + note_id + ".encrypted"
        context.record_resource(note_id + ".encrypted", "allocated")
        context.check_cancel()
        try:
            result = client.put_object(Bucket=bucket, Key=key, Body=data)
        except (BotoCoreError, ClientError):
            raise WriteUncertain() from None
        if result.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200:
            raise WriteUncertain()
        if self.download_body(key, context) != data:
            raise WriteUncertain()
        context.record_resource(note_id + ".encrypted", "uploaded")
        return key

    def download_body(self, key, context):
        client, bucket, prefix = self._client()
        if not isinstance(key, str) or not key.startswith(prefix) or not re.fullmatch(r"[a-f0-9]{32}\.encrypted", key[len(prefix):]):
            raise BridgeError("legacy_object_storage", "WPS 长正文不在当前账号的已验证文件范围内。")
        context.check_cancel()
        try:
            result = client.get_object(Bucket=bucket, Key=key)
            parts, size = [], 0
            body = result["Body"]
            try:
                for part in body.iter_chunks(65536):
                    context.check_cancel()
                    size += len(part)
                    if size > 209715200:
                        raise BridgeError("body_too_large", "WPS 长正文超过本地处理上限。")
                    parts.append(part)
            finally:
                body.close()
            if not size or size != result.get("ContentLength"):
                raise BridgeError("body_incomplete", "WPS 长正文未完整下载。")
            return b"".join(parts)
        except (BotoCoreError, ClientError):
            raise BridgeError("body_download_failed", "WPS 长正文下载失败，请检查文件会话。") from None

    def download(self, asset, key, context, expected=None):
        if not re.fullmatch(r"[a-fA-F0-9]{32}", key):
            raise BridgeError("legacy_attachment", "WPS 旧文件映射尚未完成验证，未读取不确定路径。")
        client, bucket, prefix = self._client()
        directory = self.provider.resources / "wps" / self.provider.account_id
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / (uuid.uuid4().hex + ".part")
        context.check_cancel()
        try:
            response = client.get_object(Bucket=bucket, Key=prefix + key)
            stream = response["Body"]
            try:
                size, checksum = 0, hashlib.sha256()
                with temporary.open("xb") as output:
                    for chunk in stream.iter_chunks(65536):
                        context.check_cancel()
                        size += len(chunk)
                        if size > 209715200 or (expected and size > expected.size):
                            raise BridgeError("attachment_changed", "WPS 附件大小超过校验范围。")
                        output.write(chunk)
                        checksum.update(chunk)
            finally:
                stream.close()
            if not size or size != response.get("ContentLength") or (expected and (size != expected.size or checksum.hexdigest() != expected.sha256)):
                raise BridgeError("attachment_changed", "WPS 附件大小或摘要校验失败。")
            suffix = ".bin"
            if asset.kind == "image":
                try:
                    with Image.open(temporary) as picture:
                        fmt = picture.format
                        picture.verify()
                    suffix = {"PNG": ".png", "JPEG": ".jpg", "GIF": ".gif", "WEBP": ".webp"}[fmt]
                    asset.mime = "image/" + ("jpeg" if fmt == "JPEG" else fmt.lower())
                except (OSError, ValueError, KeyError, Image.DecompressionBombError):
                    raise BridgeError("attachment_response", "WPS 图片无法解码。") from None
            target = directory / (checksum.hexdigest() + suffix)
            os.replace(temporary, target)
            asset.local_path = target.relative_to(self.provider.resources).as_posix()
            asset.size, asset.sha256 = size, checksum.hexdigest()
            return asset
        except (BotoCoreError, ClientError):
            raise BridgeError("attachment_download_failed", "WPS 对象存储下载未完成，请检查文件会话。") from None
        finally:
            temporary.unlink(missing_ok=True)

    def close(self):
        for client, _, _ in self.clients.values():
            client.close()
        self.clients.clear()
