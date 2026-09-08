"""The sole native API exposed to the bundled UI. Remote login windows have no API."""

from __future__ import annotations

import functools
import json
import os
import threading
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from . import __release_channel__, __version__
from .errors import BridgeError
from .exporter import Exporter, package_export
from .login_process import LoginProcess
from .login_process import _error as login_error
from .migration_selection import MigrationSelection
from .models import Model, PlatformId
from .operations import fetch_snapshot, migrate
from .paths import AppPaths
from .providers.base import SPECS, PendingProvider
from .providers.factory import create_provider
from .receipt_review import review_receipts
from .storage import Store
from .tasks import TaskContext, TaskRunner


class FetchRequest(Model):
    platform: PlatformId


class ExportRequest(FetchRequest):
    format: Literal["txt", "md", "html", "docx"]
    multi_file: bool = False


class MigrationDirection(Model):
    source: PlatformId
    target: PlatformId


class MigrateRequest(MigrationDirection):
    note_ids: list[str] | None = None
    preview_token: str | None = None


def api(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            data = function(*args, **kwargs)
            if hasattr(data, "model_dump"):
                data = data.model_dump(mode="json")
            return {"ok": True, "data": data}
        except BridgeError as error:
            return {"ok": False, "error": {"code": error.code, "message": error.message}}
        except (ValidationError, ValueError, TypeError):
            return {
                "ok": False,
                "error": {"code": "invalid_request", "message": "请求参数不正确，请重新选择。"},
            }
        except Exception as error:
            return {
                "ok": False,
                "error": {
                    "code": "internal_error",
                    "message": f"操作未完成（{type(error).__name__}）。请查看任务记录。",
                },
            }

    return wrapped


class Bridge:
    def __init__(self, paths: AppPaths):
        self._paths = paths
        self._store = Store(paths.database)
        self._runner = TaskRunner(self._store, self._emit)
        self._window = None
        self._providers = {key: PendingProvider(spec) for key, spec in SPECS.items()}
        self._login_windows = {}
        self._login_errors = {}
        self._checking = set()
        self._mutex = threading.RLock()
        self._maximized = False
        self._closed = False
        self._migration_selection: MigrationSelection | None = None
        self._last_export = next(
            (
                Path(r.output_path)
                for r in self._store.recent_tasks(100)
                if r.operation == "export" and r.output_path and Path(r.output_path).is_dir()
            ),
            None,
        )

    def _bind(self, window):
        self._window = window

    def _emit(self, report: dict):
        # Reports intentionally contain no HTTP responses, headers or exception reprs.
        target = self._paths.reports / f"{report['id']}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
        if self._window and not self._closed:
            self._window.run_js(f"window.onNoteBridgeTask?.({json.dumps(report, ensure_ascii=True)})")

    def _ensure_idle(self):
        if self._runner.busy:
            raise BridgeError("task_busy", "请先等待当前任务结束或取消任务。")

    def _provider(self, key: PlatformId):
        if key in self._checking:
            raise BridgeError("login_busy", "正在检查登录状态，请等待账号检查完成。")
        provider = self._providers[key]
        if not provider.account_id:
            raise BridgeError("login_required", "请先登录并完成账号检查。")
        return provider

    def _choose_directory(self) -> Path | None:
        import webview

        selected = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return Path(selected[0]).resolve() if selected else None

    @api
    def get_app_state(self):
        with self._mutex:
            states = []
            for key, provider in self._providers.items():
                snapshot = (
                    self._store.snapshot(key, provider.account_id)
                    if provider.account_id
                    else {"count": 0, "complete": False}
                )
                states.append(
                    {
                        "id": key,
                        "logged_in": bool(provider.account_id),
                        "logging_in": key in self._checking,
                        "login_open": key in self._login_windows,
                        **snapshot,
                        "capability": "experimental" if provider.read_supported else "pending",
                        "message": self._login_errors.get(key, provider.spec.note),
                    }
                )
            current = self._runner.current()
            return {
                "name": "笔记互迁",
                "version": __version__,
                "release_channel": __release_channel__,
                "platforms": states,
                "current_task": current.model_dump(mode="json") if current else None,
                "has_export": bool(self._last_export and self._last_export.is_dir()),
                # Preserve the original full-matrix acceptance status independently of branding.
                "release_ready": False,
            }

    @api
    def login_platform(self, platform: str):
        key = PlatformId(platform)
        with self._mutex:
            self._ensure_idle()
            if self._closed:
                raise BridgeError("application_closed", "软件已关闭，请重新启动。")
            existing = self._login_windows.get(key)
            if not existing:
                def closed(window, code):
                    with self._mutex:
                        if self._login_windows.get(key) is window:
                            self._login_windows.pop(key, None)
                            if code:
                                self._login_errors[key] = login_error(code).message

                # Each native process owns a private profile; opening another platform cannot clear it.
                self._login_windows[key] = LoginProcess(key, self._paths.root, on_closed=closed)
                self._login_errors.pop(key, None)
        if existing:
            existing.show()
        return {"started": True}

    @api
    def complete_login(self, platform: str):
        key = PlatformId(platform)
        with self._mutex:
            self._ensure_idle()
            window = self._login_windows.get(key)
            if not window:
                raise BridgeError("login_cancelled", "登录窗口已关闭，请重新打开官方登录。")
            if key in self._checking:
                raise BridgeError("login_busy", "正在检查登录状态。")
            self._checking.add(key)
            # Clear the old identity before collecting a possibly different account's session.
            self._providers[key].close()
        provider = None
        accepted = False
        jars = []
        try:
            jars = window.capture_session()
            provider = create_provider(key, jars, self._paths.resources)
            account = provider.probe()
            provider.account_id = account
            window.hide()
            with self._mutex:
                if self._closed:
                    raise BridgeError("application_closed", "窗口已经关闭，本次会话已清除。")
                if self._login_windows.get(key) is not window or window.closed:
                    raise login_error("login_window_closed")
                self._providers[key] = provider
                self._login_errors.pop(key, None)
                accepted = True
            return {"logged_in": True}
        except BridgeError as error:
            with self._mutex:
                self._login_errors[key] = error.message
            raise
        finally:
            for jar in jars:
                jar.clear()
            jars.clear()
            if provider is not None and not accepted:
                provider.close()
            with self._mutex:
                self._checking.discard(key)

    @api
    def cancel_login(self, platform: str):
        key = PlatformId(platform)
        with self._mutex:
            self._ensure_idle()
            if key in self._checking:
                raise BridgeError("login_busy", "请等待当前账号检查结束。")
            window = self._login_windows.get(key)
            self._providers[key].close()
        if window:
            window.destroy()
        return {"cancelled": True}

    @api
    def fetch_notes(self, payload: dict):
        request = FetchRequest.model_validate(payload)
        with self._mutex:
            self._ensure_idle()
            provider = self._provider(request.platform)
            return self._runner.start("fetch", lambda ctx: fetch_snapshot(provider, self._store, ctx))

    @api
    def export_notes(self, payload: dict):
        request = ExportRequest.model_validate(payload)
        with self._mutex:
            self._ensure_idle()
            provider = self._provider(request.platform)
            notes = self._store.notes(request.platform, provider.account_id)
            if not notes:
                raise BridgeError("empty_notes", "请先获取笔记。")
            destination = self._choose_directory()
            if destination is None:
                return None

            def work(ctx: TaskContext):
                ctx.update(stage="正在生成导出文件。", total=len(notes))
                result = Exporter(self._paths.resources).export(
                    notes,
                    destination,
                    request.format,
                    request.multi_file,
                    progress=lambda done, total: ctx.update(completed=done, total=total),
                    check_cancel=ctx.check_cancel,
                )
                self._last_export = result.path
                ctx.update(output_path=str(result.path), succeeded=result.note_count, issues=result.issues)

            return self._runner.start("export", work)

    @api
    def preview_migration(self, payload: dict):
        request = MigrationDirection.model_validate(payload)
        if request.source == request.target:
            raise BridgeError("same_platform", "请选择两个不同的平台。")
        with self._mutex:
            self._ensure_idle()
            source, target = self._provider(request.source), self._provider(request.target)
            target.prepare_migration()
            self._migration_selection = MigrationSelection.prepare(source, target, self._store)
            return self._migration_selection.public()

    @api
    def review_migration(self, payload: dict):
        request = MigrationDirection.model_validate(payload)
        if request.source == request.target:
            raise BridgeError("same_platform", "请选择两个不同的平台。")
        with self._mutex:
            self._ensure_idle()
            source, target = self._provider(request.source), self._provider(request.target)
            return review_receipts(source, target, self._store)

    @api
    def migrate_notes(self, payload: dict):
        request = MigrateRequest.model_validate(payload)
        if request.source == request.target:
            raise BridgeError("same_platform", "请选择两个不同的平台。")
        if (request.note_ids is None) != (request.preview_token is None):
            raise BridgeError("invalid_selection", "所选笔记需要对应的完整预检记录。")
        with self._mutex:
            self._ensure_idle()
            source, target = self._provider(request.source), self._provider(request.target)
            if not target.write_supported:
                target.preflight([])
            if request.note_ids is not None:
                selection = self._migration_selection
                if selection is None:
                    raise BridgeError("selection_expired", "预检记录已失效，请重新预检。")
                notes, excluded = selection.select(
                    request.preview_token, request.note_ids, source, target, self._store
                )
                # No second source fetch: this task migrates exactly the loaded, bound snapshot.
                def selected_work(ctx: TaskContext):
                    ctx.update(stage="正在确认所选笔记的账号。")
                    if source.probe() != selection.source_account or target.probe() != selection.target_account:
                        raise BridgeError("account_changed", "迁移账号发生变化，尚未开始写入。")
                    for item in excluded:
                        detail = (item["reason"] or "未选择").rstrip("。")
                        ctx.issue("not_selected", f"“{item['title']}”未迁移：{detail}。", item["id"])
                    for note in notes:
                        for warning in note.warnings:
                            ctx.issue("source_warning", warning, note.source_id)
                    migrate(notes, target, self._store, ctx)

                report = self._runner.start("migrate", selected_work)
                self._migration_selection = None
                return report

            def work(ctx: TaskContext):
                try:
                    fetch_snapshot(source, self._store, ctx)
                finally:
                    # Reading source notes does not count as successful target writes.
                    ctx.update(completed=0, succeeded=0)
                if not self._store.snapshot(source.spec.id, source.account_id)["complete"]:
                    raise BridgeError("source_incomplete", "来源未完整获取，迁移尚未开始。请先解决读取提示。")
                notes = self._store.notes(source.spec.id, source.account_id)
                target.prepare_migration()
                migrate(notes, target, self._store, ctx)

            return self._runner.start("migrate", work)

    @api
    def package_notes(self, payload: dict):
        Model.model_validate(payload)
        with self._mutex:
            self._ensure_idle()
            source = self._last_export
            if not source or not source.is_dir():
                raise BridgeError("export_required", "请先完成一次导出。")
            destination = self._choose_directory()
            if destination is None:
                return None

            def work(ctx: TaskContext):
                ctx.update(stage="正在打包导出目录及附件。", total=1)
                target = package_export(source, destination, ctx.check_cancel)
                ctx.update(completed=1, succeeded=1, output_path=str(target))

            return self._runner.start("package", work)

    @api
    def get_task(self, task_id: str):
        report = self._store.task(task_id)
        if not report:
            raise BridgeError("task_missing", "未找到任务记录。")
        return report

    @api
    def cancel_task(self, task_id: str):
        self._runner.cancel(task_id)
        return {"requested": True}

    @api
    def open_directory(self, kind: str):
        paths = {
            "resources": self._paths.resources,
            "logs": self._paths.reports,
            "exports": self._last_export,
        }
        path = paths.get(kind)
        if not path or not path.is_dir():
            raise BridgeError("directory_missing", "目录尚未生成。")
        os.startfile(str(path))
        return {"opened": True}

    @api
    def open_task_output(self, task_id: str):
        report = self._store.task(task_id)
        if not report or not report.output_path:
            raise BridgeError("output_missing", "该任务还没有生成输出文件。")
        path = Path(report.output_path)
        if not path.exists():
            raise BridgeError("output_missing", "输出文件已移动或删除。")
        os.startfile(str(path if path.is_dir() else path.parent))
        return {"opened": True}

    @api
    def window_action(self, action: str):
        if action == "minimize":
            self._window.minimize()
        elif action == "maximize":
            self._window.restore() if self._maximized else self._window.maximize()
            self._maximized = not self._maximized
        elif action == "close":
            self._window.destroy()
        else:
            raise BridgeError("invalid_action", "未知窗口操作。")
        return {"done": True}

    def _closing(self):
        if self._checking:
            self._window.run_js("alert('正在检查账号，请等待检查结束后再关闭。')")
            return False
        if self._runner.busy:
            # A native close is reversible until the user decides how to handle the running task.
            self._window.run_js("alert('任务仍在运行。请先在任务窗口取消，等待结果返回后再关闭。')")
            return False
        return True

    def _shutdown(self):
        with self._mutex:
            self._closed = True
            for provider in self._providers.values():
                provider.close()
            windows = list(self._login_windows.values())
            self._login_windows.clear()
        for window in windows:
            try:
                window.destroy()
            except Exception:
                pass
