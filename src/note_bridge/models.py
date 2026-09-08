"""The provider-independent document and task contract. No credentials belong here."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PlatformId(StrEnum):
    XIAOMI = "xiaomi"
    OPPO = "oppo"
    VIVO = "vivo"
    HUAWEI = "huawei"
    HONOR = "honor"
    MEIZU = "meizu"
    WPS = "wps"


PLATFORM_NAMES = {
    PlatformId.XIAOMI: "小米笔记",
    PlatformId.OPPO: "OPPO笔记",
    PlatformId.VIVO: "vivo笔记",
    PlatformId.HUAWEI: "华为备忘录",
    PlatformId.HONOR: "荣耀笔记",
    PlatformId.MEIZU: "魅族笔记",
    PlatformId.WPS: "WPS便签",
}


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Span(Model):
    text: str = ""
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    highlight: bool = False
    code: bool = False
    link: str | None = None


class Block(Model):
    kind: Literal[
        "paragraph", "heading", "list", "todo", "quote", "code", "divider", "table", "attachment"
    ] = "paragraph"
    spans: list[Span] = Field(default_factory=list)
    level: int = Field(default=1, ge=1, le=6)
    ordered: bool = False
    checked: bool = False
    rows: list[list[str]] = Field(default_factory=list)
    attachment_id: str | None = None

    @property
    def text(self) -> str:
        return "".join(span.text for span in self.spans)


class Attachment(Model):
    id: str
    name: str
    kind: Literal["image", "audio", "video", "file"] = "file"
    mime: str = "application/octet-stream"
    local_path: str | None = None
    sha256: str | None = None
    size: int = Field(default=0, ge=0)
    # Signed download URLs only live during acquisition, never in cache or reports.
    download_url: str | None = Field(default=None, exclude=True, repr=False)


class NoteDocument(Model):
    platform: PlatformId
    account_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    title: str = ""
    source_folder_id: str | None = None
    source_folder_name: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    blocks: list[Block] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("Timestamps must include a timezone; do not guess the source timezone")
        return value

    @property
    def plain_text(self) -> str:
        return "\n".join(
            "\n".join("\t".join(row) for row in block.rows) if block.kind == "table" else block.text
            for block in self.blocks
            if block.kind != "attachment"
        )

    @property
    def display_title(self) -> str:
        return self.title.strip() or next(
            (line.strip()[:40] for line in self.plain_text.splitlines() if line.strip()), "无标题"
        )

    def fingerprint(self) -> str:
        data = self.model_dump(mode="json", exclude={"account_id", "warnings"})
        for attachment in data["attachments"]:
            attachment.pop("local_path", None)
        return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_REVIEW = "needs_review"


class ItemIssue(Model):
    note_id: str = ""
    code: str
    message: str


class TaskReport(Model):
    id: str
    operation: str
    status: TaskStatus = TaskStatus.QUEUED
    stage: str = "等待开始"
    completed: int = 0
    total: int = 0
    succeeded: int = 0
    skipped: int = 0
    issues: list[ItemIssue] = Field(default_factory=list)
    output_path: str | None = None
    started_at: str
    finished_at: str | None = None


def account_fingerprint(platform: PlatformId, stable_user_id: str) -> str:
    return hashlib.sha256(f"{platform}\0{stable_user_id}".encode()).hexdigest()
