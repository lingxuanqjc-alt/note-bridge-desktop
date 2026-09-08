"""Account-bound selection of an explicitly loaded snapshot; preview never writes."""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from .errors import BridgeError
from .models import NoteDocument, PlatformId
from .operations import receipt_key
from .providers.base import Provider
from .storage import Store


def complete_notes(store: Store, source: Provider) -> list[NoteDocument]:
    snapshot = store.snapshot(source.spec.id, source.account_id)
    notes = store.notes(source.spec.id, source.account_id)
    if not snapshot["complete"] or snapshot["count"] != len(notes):
        raise BridgeError("source_incomplete", "来源未完整获取，请先解决读取提示后重新预检。")
    if not notes:
        raise BridgeError("empty_notes", "来源中没有可迁移的笔记。")
    if any(note.platform != source.spec.id or note.account_id != source.account_id for note in notes):
        raise BridgeError("account_changed", "缓存与来源账号不一致，请重新获取。")
    return notes


def fingerprints(notes: list[NoteDocument]) -> dict[str, tuple[str, tuple[str, ...]]]:
    return {note.source_id: (note.fingerprint(), tuple(note.warnings)) for note in notes}


@dataclass
class MigrationSelection:
    token: str
    source: PlatformId
    source_account: str
    target: PlatformId
    target_account: str
    snapshot: dict[str, tuple[str, tuple[str, ...]]]
    items: list[dict]
    write_available: bool

    @classmethod
    def prepare(cls, source: Provider, target: Provider, store: Store) -> MigrationSelection:
        notes = complete_notes(store, source)
        items = []
        for note in notes:
            receipt = store.receipt(receipt_key(note, target))
            confirmed = bool(receipt and receipt["status"] == "confirmed")
            reason, code = "", ""
            try:
                store.check_migration_write(note, target)
                if not confirmed:
                    target.validate_notes([note])
                    if note.attachments and not target.images_supported:
                        raise BridgeError("attachment_upload_pending", "该平台的图片迁入尚未开放。")
            except BridgeError as error:
                reason, code = error.message, error.code
            items.append({"id": note.source_id, "title": note.display_title,
                          "summary": note.plain_text.replace("\n", " ")[:120],
                          "compatible": not code, "reason": reason, "code": code,
                          "already_migrated": confirmed, "warnings": list(note.warnings)})
        return cls(secrets.token_urlsafe(32), source.spec.id, source.account_id,
                   target.spec.id, target.account_id, fingerprints(notes), items, target.write_supported)

    def public(self) -> dict:
        return {"token": self.token, "source": self.source, "target": self.target,
                "total": len(self.items), "items": self.items, "write_available": self.write_available,
                "blocked_reason": "" if self.write_available else "该平台的迁入功能尚未开放；预检和选择不会执行写入。"}

    def select(self, token: str, ids: list[str], source: Provider, target: Provider,
               store: Store) -> tuple[list[NoteDocument], list[dict]]:
        if not secrets.compare_digest(token, self.token):
            raise BridgeError("selection_expired", "预检记录已失效，请重新预检。")
        if (source.spec.id, source.account_id, target.spec.id, target.account_id) != (
                self.source, self.source_account, self.target, self.target_account):
            raise BridgeError("account_changed", "迁移双方账号已变化，请重新读取并预检。")
        notes = complete_notes(store, source)
        if fingerprints(notes) != self.snapshot:
            raise BridgeError("snapshot_changed", "已加载的笔记发生变化，请重新预检后选择。")
        chosen = set(ids)
        if not ids or len(chosen) != len(ids) or not chosen.issubset(self.snapshot):
            raise BridgeError("invalid_selection", "所选笔记为空、重复或不属于当前账号的预检范围。")
        if any(not item["compatible"] for item in self.items if item["id"] in chosen):
            raise BridgeError("incompatible_selection", "所选笔记包含不兼容或待核对项，请先调整选择。")
        return ([note for note in notes if note.source_id in chosen],
                [item for item in self.items if item["id"] not in chosen])
