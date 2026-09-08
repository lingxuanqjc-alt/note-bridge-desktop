from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import BridgeError
from ..models import Attachment, NoteDocument, PlatformId
from ..tasks import TaskContext


@dataclass(frozen=True)
class PlatformSpec:
    id: PlatformId
    login_url: str
    domains: tuple[str, ...]
    note: str


SPECS = {
    PlatformId.XIAOMI: PlatformSpec(
        PlatformId.XIAOMI,
        "https://i.mi.com/note/h5",
        ("mi.com", "xiaomi.com"),
        "本版支持普通笔记及 PNG/JPEG 迁入；单张最多 4 MiB，写入前检查非加密账号。",
    ),
    PlatformId.OPPO: PlatformSpec(
        PlatformId.OPPO,
        "https://cloud.oppo.com/",
        ("oppo.com", "heytap.com", "heytapmobi.com"),
        "本版支持 Connect v2 笔记、分组及 PNG/JPEG；分片上传未开放，代码显示差异会报告，专有手写仍待验证。",
    ),
    PlatformId.VIVO: PlatformSpec(
        PlatformId.VIVO,
        "https://yun.vivo.com.cn/",
        ("vivo.com", "vivo.com.cn"),
        "已验证当前网页笔记分页和正文读取；格式降级与未获取附件会进入报告。",
    ),
    PlatformId.HUAWEI: PlatformSpec(
        PlatformId.HUAWEI,
        "https://cloud.huawei.com/home",
        ("huawei.com", "hicloud.com", "huaweicloud.com"),
        "本版支持华为备忘录、分组及 PNG/JPEG；华为笔记不是同一项服务。",
    ),
    PlatformId.HONOR: PlatformSpec(
        PlatformId.HONOR,
        "https://cloud.honor.com/",
        ("honor.com", "hihonor.com"),
        "本版支持新荣耀笔记、分组及 PNG/JPEG；旧账号入口和专有手写仍待验证。",
    ),
    PlatformId.MEIZU: PlatformSpec(
        PlatformId.MEIZU,
        "https://cloud.flyme.cn/browser/main.jsp",
        ("flyme.cn", "meizu.com"),
        "本版支持 Flyme 笔记、分组及 PNG/JPEG；格式差异会进入报告。",
    ),
    PlatformId.WPS: PlatformSpec(
        PlatformId.WPS,
        "https://note.wps.cn/",
        ("wps.cn", "wps.com", "kdocs.cn"),
        "本版支持 WPS 长正文、分组及 PNG/JPEG；链接等格式差异会进入报告。",
    ),
}


@dataclass
class Snapshot:
    notes: list[NoteDocument]
    complete: bool


@dataclass
class CreatedNote:
    remote_ids: list[str]
    warnings: list[str] = field(default_factory=list)


class Provider(ABC):
    spec: PlatformSpec
    account_id: str | None = None
    read_supported = False
    write_supported = False
    images_supported = False

    @abstractmethod
    def probe(self) -> str:
        """Return an opaque, stable account identity after a real permission check."""

    @abstractmethod
    def fetch(self, context: TaskContext) -> Snapshot: ...

    def preflight(self, notes: list[NoteDocument]):
        if not self.write_supported:
            raise BridgeError("protocol_pending", "该平台的写入协议尚未完成验证，当前不能开始迁移。")
        if any(note.attachments for note in notes) and not self.images_supported:
            raise BridgeError("attachment_upload_pending", "该目标的附件上传尚未完成验证，迁移尚未开始。")
        self.validate_notes(notes)

    def prepare_migration(self):
        """Refresh optional read-only account capabilities once before local preflight."""

    def validate_notes(self, notes: list[NoteDocument]):
        """Read-only local content checks; this never grants permission to write."""
        if any(asset.kind != "image" for note in notes for asset in note.attachments):
            raise BridgeError(
                "unsupported_attachment",
                "含音频、视频或其他文件附件，当前迁入仅支持图片。可先将这条笔记导出到本地。",
            )

    def upload_attachment(self, attachment: Attachment, path: Path, context: TaskContext) -> str:
        raise BridgeError("attachment_upload_pending", "该平台的附件上传尚未完成验证。")

    def create(self, note: NoteDocument, context: TaskContext) -> CreatedNote:
        raise BridgeError("protocol_pending", "该平台的笔记创建尚未完成验证。")

    def close(self):
        self.account_id = None


class PendingProvider(Provider):
    """Deliberately unavailable until a protocol has evidence; never returns synthetic notes."""

    def __init__(self, spec: PlatformSpec):
        self.spec = spec

    def probe(self) -> str:
        raise BridgeError("protocol_pending", self.spec.note + "当前登录入口可用，账号读取适配尚未完成。")

    def fetch(self, context: TaskContext) -> Snapshot:
        self.probe()
        raise AssertionError("Unreachable")
