"""One durable data task at a time; cancellation never rolls back remote successes."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from .errors import BridgeError, Cancelled, WriteUncertain
from .models import ItemIssue, TaskReport, TaskStatus
from .storage import Store


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskContext:
    def __init__(self, report: TaskReport, store: Store, emit: Callable[[dict], None]):
        self.report = report
        self.store = store
        self.emit = emit
        self.cancelled = threading.Event()
        self.lock = threading.RLock()
        self._write_receipt_key: str | None = None

    def begin_write(self, key: str):
        """Legacy scoped repair only; new-note migrations use the atomic identity API."""
        self.store.save_receipt(key, "sending")
        self._write_receipt_key = key

    def begin_migration_write(self, note, target) -> bool:
        self._write_receipt_key = self.store.begin_migration_write(note, target)
        return self._write_receipt_key is not None

    def record_remote_ids(self, remote_ids: list[str]):
        """Persist provider-assigned IDs before a multi-step remote write begins."""
        if not self._write_receipt_key or not remote_ids or any(not value for value in remote_ids):
            raise ValueError("Remote identifiers require an active write intent")
        self.store.save_receipt(self._write_receipt_key, "sending", remote_ids)

    def record_resource(self, resource_id: str, status: str):
        if not self._write_receipt_key:
            raise ValueError("Cloud resources require an active write intent")
        self.store.save_resource_receipt(self._write_receipt_key, resource_id, status)

    def check_cancel(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def update(self, **changes):
        with self.lock:
            for key, value in changes.items():
                setattr(self.report, key, value)
            self.store.save_task(self.report)
            # Window teardown must never alter a committed operation's result.
            try:
                self.emit(self.report.model_dump(mode="json"))
            except Exception:
                pass

    def issue(self, code: str, message: str, note_id: str = ""):
        with self.lock:
            self.report.issues.append(ItemIssue(code=code, message=message, note_id=note_id))
            self.update()


class TaskRunner:
    def __init__(self, store: Store, emit: Callable[[dict], None] = lambda _: None):
        self.store, self.emit = store, emit
        self._lock = threading.RLock()
        self._current: TaskContext | None = None
        self._thread: threading.Thread | None = None
        store.recover_interrupted()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def current(self) -> TaskReport | None:
        with self._lock:
            if self._current:
                return self.store.task(self._current.report.id)
            return next(iter(self.store.recent_tasks(1)), None)

    def start(self, operation: str, work: Callable[[TaskContext], None]) -> TaskReport:
        with self._lock:
            if self.busy:
                raise BridgeError("task_busy", "已有任务正在执行，请等待完成或先取消。")
            report = TaskReport(id=uuid.uuid4().hex, operation=operation, started_at=now())
            self.store.save_task(report)
            context = self._current = TaskContext(report, self.store, self.emit)

            def execute():
                try:
                    context.check_cancel()
                    context.update(status=TaskStatus.RUNNING)
                    work(context)
                    # A committed export or acknowledged remote write remains successful even
                    # when cancellation arrives just after its final atomic commit.
                    if context.report.status == TaskStatus.RUNNING:
                        context.update(
                            status=TaskStatus.PARTIAL if context.report.issues else TaskStatus.SUCCEEDED,
                            stage="部分完成，请检查结果提示。" if context.report.issues else "任务已完成。",
                        )
                except WriteUncertain as error:
                    context.issue(error.code, error.message)
                    context.update(status=TaskStatus.NEEDS_REVIEW, stage=error.message)
                except Cancelled as error:
                    context.update(status=TaskStatus.CANCELLED, stage=error.message)
                except BridgeError as error:
                    context.issue(error.code, error.message)
                    context.update(
                        status=TaskStatus.PARTIAL if context.report.succeeded else TaskStatus.FAILED,
                        stage=error.message,
                    )
                except Exception as error:
                    # Exception text can contain an authenticated URL, response body or cookie.
                    context.issue(
                        "internal_error", f"内部处理失败（{type(error).__name__}），请保留任务编号。"
                    )
                    context.update(
                        status=TaskStatus.PARTIAL if context.report.succeeded else TaskStatus.FAILED,
                        stage="任务未完成，详情见结果报告。",
                    )
                finally:
                    context.update(finished_at=now())

            self._thread = threading.Thread(target=execute, name=f"task-{report.id}", daemon=True)
            self._thread.start()
            return report.model_copy(deep=True)

    def cancel(self, task_id: str):
        with self._lock:
            if not self._current or self._current.report.id != task_id or not self.busy:
                raise BridgeError("task_finished", "该任务已结束。")
            self._current.cancelled.set()
            self._current.update(stage="正在取消，等待当前请求返回并核对结果。")

    def join(self, timeout: float | None = None):
        thread = self._thread
        if thread:
            thread.join(timeout)
