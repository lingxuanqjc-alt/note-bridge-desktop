"""Transactional account-scoped snapshots and durable migration receipts."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import BridgeError, WriteUncertain
from .models import NoteDocument, PlatformId, TaskReport, TaskStatus
from .receipt_identity import FIELDS, SCOPE_FIELDS, identity_key, migration_identity

SCHEMA_VERSION = 2


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise BridgeError("newer_database", "缓存由更新版本创建，请使用更新版本打开，当前数据保持不变。")
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS notes (
                    platform TEXT NOT NULL, account TEXT NOT NULL, source_id TEXT NOT NULL,
                    document TEXT NOT NULL, PRIMARY KEY (platform, account, source_id)
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    platform TEXT NOT NULL, account TEXT NOT NULL, complete INTEGER NOT NULL,
                    count INTEGER NOT NULL, PRIMARY KEY (platform, account)
                );
                CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, report TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts (
                    key TEXT PRIMARY KEY, status TEXT NOT NULL, remote_ids TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS resource_receipts (
                    receipt_key TEXT NOT NULL, resource_id TEXT NOT NULL, status TEXT NOT NULL,
                    PRIMARY KEY (receipt_key, resource_id)
                );
                CREATE TABLE IF NOT EXISTS receipt_contexts (
                    key TEXT PRIMARY KEY, source_platform TEXT NOT NULL, source_account TEXT NOT NULL,
                    source_id TEXT NOT NULL, source_fingerprint TEXT NOT NULL,
                    target_platform TEXT NOT NULL, target_account TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS receipt_context_scope ON receipt_contexts
                    (source_platform, source_account, source_id, target_platform, target_account);
                PRAGMA user_version=2;
            """)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def replace_snapshot(
        self, platform: PlatformId, account: str, notes: list[NoteDocument], complete: bool
    ) -> None:
        if any(n.platform != platform or n.account_id != account for n in notes):
            raise ValueError("A snapshot cannot mix accounts or platforms")
        if len({n.source_id for n in notes}) != len(notes):
            raise ValueError("Duplicate source ids in a snapshot")
        with self.connection() as db:
            db.execute("DELETE FROM notes WHERE platform=? AND account=?", (platform, account))
            db.executemany(
                "INSERT INTO notes VALUES (?,?,?,?)",
                [(platform, account, note.source_id, note.model_dump_json()) for note in notes],
            )
            db.execute(
                "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?)",
                (platform, account, int(complete), len(notes)),
            )

    def notes(self, platform: PlatformId, account: str) -> list[NoteDocument]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT document FROM notes WHERE platform=? AND account=? ORDER BY source_id",
                (platform, account),
            ).fetchall()
        notes = [NoteDocument.model_validate_json(row[0]) for row in rows]
        return sorted(notes, key=lambda n: (n.created_at.isoformat() if n.created_at else "", n.source_id))

    def snapshot(self, platform: PlatformId, account: str) -> dict:
        with self.connection() as db:
            row = db.execute(
                "SELECT complete,count FROM snapshots WHERE platform=? AND account=?", (platform, account)
            ).fetchone()
        return {"complete": bool(row[0]), "count": row[1]} if row else {"complete": False, "count": 0}

    def save_task(self, report: TaskReport) -> None:
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO tasks VALUES (?,?)", (report.id, report.model_dump_json()))

    def task(self, task_id: str) -> TaskReport | None:
        with self.connection() as db:
            row = db.execute("SELECT report FROM tasks WHERE id=?", (task_id,)).fetchone()
        return TaskReport.model_validate_json(row[0]) if row else None

    def recent_tasks(self, limit: int = 20) -> list[TaskReport]:
        with self.connection() as db:
            rows = db.execute("SELECT report FROM tasks ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        return [TaskReport.model_validate_json(row[0]) for row in rows]

    def recover_interrupted(self) -> None:
        with self.connection() as db:
            rows = db.execute("SELECT id,report FROM tasks").fetchall()
            for row in rows:
                report = TaskReport.model_validate_json(row[1])
                if report.status in (TaskStatus.QUEUED, TaskStatus.RUNNING):
                    report.status = (
                        TaskStatus.NEEDS_REVIEW if report.operation == "migrate" else TaskStatus.CANCELLED
                    )
                    report.stage = "上次运行中断，请核对已完成的条目。"
                    db.execute("UPDATE tasks SET report=? WHERE id=?", (report.model_dump_json(), row[0]))
            db.execute("UPDATE receipts SET status='uncertain' WHERE status='sending'")

    def receipt(self, key: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT status,remote_ids FROM receipts WHERE key=?", (key,)).fetchone()
        return {"status": row[0], "remote_ids": json.loads(row[1])} if row else None

    @staticmethod
    def _check_migration_write(db, key, identity):
        row = db.execute("SELECT status,remote_ids FROM receipts WHERE key=?", (key,)).fetchone()
        receipt = {"status": row[0], "remote_ids": json.loads(row[1])} if row else None
        # A confirmed same version requires no side effect or old local resources.
        if receipt and receipt["status"] == "confirmed":
            return receipt
        scope = tuple(identity[name] for name in SCOPE_FIELDS)
        unresolved = db.execute(
            "SELECT 1 FROM receipts AS r JOIN receipt_contexts AS c ON r.key=c.key "
            "WHERE c.source_platform=? AND c.source_account=? AND c.source_id=? "
            "AND c.target_platform=? AND c.target_account=? AND r.status IN ('sending','uncertain') LIMIT 1",
            scope,
        ).fetchone()
        if unresolved:
            raise WriteUncertain("这条来源笔记已有未确认的迁入版本，请先核对已有目标；修改正文不能跳过旧回执。")
        # A hash alone cannot identify an old source or either account. Do not guess
        # that an unbound unknown belongs to somebody else. Only canonical note
        # keys belong here; groups and private repair journals have other formats.
        unbound = db.execute(
            "SELECT 1 FROM receipts AS r LEFT JOIN receipt_contexts AS c ON r.key=c.key "
            "WHERE r.status IN ('sending','uncertain') AND c.key IS NULL "
            "AND length(r.key)=64 AND r.key NOT GLOB '*[^0-9a-f]*' LIMIT 1"
        ).fetchone()
        if unbound:
            raise WriteUncertain("存在缺少来源关联的旧未确认回执，请先核对并恢复关联，尚未开始新的迁入。")
        return receipt

    def check_migration_write(self, note, target) -> dict | None:
        """Read-only preflight; the same guard runs again inside the write transaction."""
        identity = migration_identity(note, target)
        key = identity_key(identity)
        with self.connection() as db:
            return self._check_migration_write(db, key, identity)

    @staticmethod
    def _bind_receipt_context(db, key, identity):
        values = tuple(identity[name] for name in FIELDS)
        existing = db.execute(
            "SELECT source_platform,source_account,source_id,source_fingerprint,target_platform,target_account "
            "FROM receipt_contexts WHERE key=?", (key,),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != values:
                raise BridgeError("receipt_context_mismatch", "回执已关联到不同身份，未修改已有记录。")
            return False
        db.execute("INSERT INTO receipt_contexts VALUES (?,?,?,?,?,?,?)", (key, *values))
        return True

    def bind_receipt_context(self, key: str, identity: dict[str, str]) -> bool:
        """Internal historical backfill; exact archived identity only, no status change."""
        if identity_key(identity) != key:
            raise BridgeError("receipt_context_mismatch", "原始身份与回执摘要不匹配，未建立关联。")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM receipts WHERE key=?", (key,)).fetchone():
                raise BridgeError("receipt_context_missing", "回执不存在，不能补建历史写入关联。")
            return self._bind_receipt_context(db, key, identity)

    def begin_migration_write(self, note, target) -> str | None:
        """Atomically guard every version and reserve one pending note across processes."""
        identity = migration_identity(note, target)
        key = identity_key(identity)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            receipt = self._check_migration_write(db, key, identity)
            if receipt and receipt["status"] == "confirmed":
                return None
            self._bind_receipt_context(db, key, identity)
            db.execute(
                "INSERT INTO receipts VALUES (?,'sending','[]') ON CONFLICT(key) DO UPDATE SET "
                "status='sending'", (key,),
            )
        return key

    def save_receipt(self, key: str, status: str, remote_ids: list[str] | None = None) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO receipts VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "status=excluded.status, remote_ids=CASE WHEN ? THEN receipts.remote_ids ELSE excluded.remote_ids END",
                (key, status, json.dumps(remote_ids or []), remote_ids is None),
            )
            if status == "confirmed":
                db.execute("UPDATE resource_receipts SET status='linked' WHERE receipt_key=?", (key,))

    def save_resource_receipt(self, key: str, resource_id: str, status: str):
        if not resource_id or status not in ("allocated", "uploaded"):
            raise ValueError("Invalid cloud resource receipt")
        with self.connection() as db:
            receipt = db.execute("SELECT status FROM receipts WHERE key=?", (key,)).fetchone()
            if not receipt or receipt[0] != "sending":
                raise ValueError("Cloud resources require an active write intent")
            db.execute("INSERT OR REPLACE INTO resource_receipts VALUES (?,?,?)", (key, resource_id, status))

    def resource_receipts(self, key: str) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT resource_id,status FROM resource_receipts WHERE receipt_key=? ORDER BY resource_id", (key,)).fetchall()
        return [dict(row) for row in rows]
