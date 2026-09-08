"""Business operations; no browser, UI assumptions or guessed provider responses."""

from __future__ import annotations

from .errors import BridgeError, WriteUncertain
from .models import NoteDocument
from .providers.base import Provider
from .receipt_identity import receipt_key as receipt_key
from .storage import Store
from .tasks import TaskContext


def fetch_snapshot(provider: Provider, store: Store, context: TaskContext):
    if not provider.account_id:
        raise BridgeError("login_required", "请先登录账号。")
    account = provider.account_id
    snapshot = provider.fetch(context)
    context.check_cancel()
    if provider.account_id != account:
        raise BridgeError("account_changed", "账号在任务期间发生变化，本次数据没有覆盖已有缓存。")
    store.replace_snapshot(provider.spec.id, account, snapshot.notes, snapshot.complete)
    context.update(
        completed=max(context.report.completed, len(snapshot.notes)),
        total=max(context.report.total, len(snapshot.notes)),
        succeeded=len(snapshot.notes),
    )
    for note in snapshot.notes:
        for warning in note.warnings:
            context.issue("source_warning", warning, note.source_id)
    if not snapshot.complete:
        context.issue("incomplete_snapshot", "本次获取未覆盖全部内容，请检查逐条提示后再导出。")


def migrate(notes: list[NoteDocument], target: Provider, store: Store, context: TaskContext):
    if not notes:
        raise BridgeError("empty_notes", "来源中没有可迁移的笔记。")
    scope = (notes[0].platform, notes[0].account_id)
    if any((note.platform, note.account_id) != scope for note in notes):
        raise BridgeError("mixed_accounts", "同一次迁移不能混用来源平台或账号。")
    if len({note.source_id for note in notes}) != len(notes):
        raise BridgeError("duplicate_source", "来源存在重复笔记标识，迁移尚未开始。")
    if not target.account_id:
        raise BridgeError("login_required", "请先登录目标账号。")
    account = target.account_id
    # A confirmed remote note no longer depends on its old local attachment files.
    # Unknown writes must be reviewed before either validation or new side effects.
    pending = []
    context.update(total=len(notes), stage="正在核对已有迁移记录。")
    for note in notes:
        context.check_cancel()
        try:
            receipt = store.check_migration_write(note, target)
        except WriteUncertain as error:
            context.issue(error.code, error.message, note.source_id)
            raise
        if not receipt or receipt["status"] != "confirmed":
            pending.append(note)
    target.preflight(pending)
    context.update(total=len(notes), stage="正在向目标新增笔记。")
    for index, note in enumerate(notes):
        context.check_cancel()
        if target.account_id != account:
            raise BridgeError("account_changed", "目标账号发生变化，迁移已停止。")
        key = receipt_key(note, target)
        receipt = store.receipt(key)
        if receipt and receipt["status"] == "confirmed":
            context.update(skipped=context.report.skipped + 1, completed=index + 1)
            continue
        # Persist intent before any remote side effect. An interrupted process cannot retry blindly.
        if not context.begin_migration_write(note, target):
            # Another process can confirm this exact version after the first check.
            context.update(skipped=context.report.skipped + 1, completed=index + 1)
            continue
        try:
            created = target.create(note, context)
            if not created.remote_ids or any(not value for value in created.remote_ids):
                raise WriteUncertain()
            store.save_receipt(key, "confirmed", created.remote_ids)
        except WriteUncertain:
            store.save_receipt(key, "uncertain")
            raise
        except BridgeError:
            # A provider may only raise BridgeError when it knows no note was created.
            store.save_receipt(key, "rejected")
            raise
        except Exception:
            store.save_receipt(key, "uncertain")
            raise WriteUncertain() from None
        for warning in created.warnings:
            context.issue("format_downgrade", warning, note.source_id)
        context.update(succeeded=context.report.succeeded + 1, completed=index + 1)
