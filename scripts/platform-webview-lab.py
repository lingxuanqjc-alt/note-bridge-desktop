"""One-platform private WebView2 login for the existing stdin lab worker."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

from note_bridge.models import PLATFORM_NAMES, PlatformId
from note_bridge.providers.base import SPECS
from note_bridge.providers.transport import host_matches

ROOT = Path(__file__).resolve().parents[1]


class SessionLab:
    def __init__(self, window, platform, root=ROOT, output=None):
        self.window, self.root = window, root
        self.platform = PlatformId(platform)
        self.spec = SPECS[self.platform]
        self.output = output if output is not None else sys.stdout
        self.lock, self.output_lock = threading.RLock(), threading.Lock()
        self.busy = self.closing = self.closed = self.eof = False
        self.worker_thread = None

    def emit(self, **result):
        with self.output_lock:
            try:
                print(json.dumps(result, ensure_ascii=True), file=self.output, flush=True)
            except (OSError, ValueError):
                pass  # A disconnected controller must not interrupt worker completion or window cleanup.

    def on_closing(self):
        with self.lock:
            if self.busy:
                self.emit(event="command_error", code="worker_busy")
                return False
            self.closing = True
            return True

    def on_closed(self):
        with self.lock:
            self.closed = True
        self.emit(event="session_closed", platform=self.platform)

    def stop(self):
        with self.lock:
            if self.closed or self.closing:
                return
            if not self.on_closing():
                return
        self.window.destroy()

    def menu(self):
        from webview.menu import Menu, MenuAction

        def command(action):
            self.handle(json.dumps({"platform": self.platform, "action": action}))

        return [Menu("页面", [MenuAction("刷新当前页面", lambda: command("reload")),
                             MenuAction("重新打开官网", lambda: command("home"))])]

    def navigate(self, action):
        try:
            if action == "home":
                self.window.load_url(self.spec.login_url)
            else:
                self.window.run_js("window.location.reload();")
            self.emit(event="navigation_started", platform=self.platform, action=action)
        except Exception:
            self.emit(event="command_error", code="navigation_failed")
        finally:
            with self.lock:
                self.busy = False
                close_after = self.eof
            if close_after:
                self.stop()

    def handle(self, line):
        try:
            request = json.loads(line)
            if (not isinstance(request, dict) or set(request) != {"platform", "action"}
                    or request["platform"] != self.platform
                    or request["action"] not in ("status", "open", "probe", "fetch", "stop", "reload", "home")):
                raise ValueError
        except (ValueError, TypeError):
            self.emit(event="command_error", code="invalid_command")
            return
        action = request["action"]
        with self.lock:
            if self.closed or self.closing:
                self.emit(event="command_error", code="session_closed")
                return
            if action == "status":
                self.emit(event="session_status", open=[self.platform], busy=self.busy)
                return
            if action in ("open", "stop"):
                pass
            elif self.busy:
                self.emit(event="command_error", code="worker_busy")
                return
            elif action in ("reload", "home"):
                self.busy = True
            elif self.platform == PlatformId.VIVO and action == "fetch":
                self.emit(event="test_result", platform=self.platform, status="blocked",
                          code="synthetic_scope_required")
                return
            else:
                self.busy = True
                self.worker_thread = threading.Thread(target=self.run_worker, args=(action,), daemon=False)
                try:
                    self.worker_thread.start()
                except Exception:
                    self.busy = False
                    self.worker_thread = None
                    self.emit(event="test_result", status="failed", code="worker_start")
                return
        # Native window calls can synchronously invoke closing on the GUI thread.
        if action == "stop":
            self.stop()
        elif action in ("reload", "home"):
            self.navigate(action)
        else:
            self.window.show()

    def run_worker(self, action):
        jars, cookies, payload, output, worker = [], [], None, None, None
        exit_known = True
        result = {"status": "failed", "code": "session_capture"}
        try:
            url = urlparse(self.window.get_current_url() or "")
            if url.scheme != "https" or not host_matches(url.hostname or "", self.spec.domains):
                result = {"status": "blocked", "code": "login_incomplete"}
                return
            jars = self.window.get_cookies()
            for jar in jars:
                for morsel in jar.values():
                    domain = morsel["domain"]
                    if domain and host_matches(domain.lstrip("."), self.spec.domains):
                        cookies.append({"name": morsel.key, "value": morsel.value, "domain": domain,
                                        "path": morsel["path"] or "/", "secure": bool(morsel["secure"])})
            payload = json.dumps({"platform": self.platform, "action": action, "cookies": cookies})
            result = {"status": "failed", "code": "worker_start"}
            worker = subprocess.Popen(
                [str(self.root / ".venv/Scripts/python.exe"), str(self.root / "scripts/probe-session.py")],
                cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
                env={**os.environ, "PYTHONUTF8": "1"},
            )
            result = {"status": "needs_review", "code": "worker_result"}
            output, _ = worker.communicate(payload)  # No timeout, kill, or retry of a possible cloud write.
            parsed = json.loads(output)
            if worker.returncode == 0 and isinstance(parsed, dict):
                result = parsed  # probe-session.py owns the existing sanitized result contract.
        except Exception:
            pass  # Only the deterministic phase code is emitted; exception text can contain credentials.
        finally:
            if worker is not None:
                try:
                    worker.wait()  # A pipe failure must not release the window while its worker still runs.
                except Exception:
                    exit_known = False
                    result = {"status": "needs_review", "code": "worker_exit_unknown"}
            for jar in jars:
                jar.clear()
            jars.clear()
            cookies.clear()
            payload = output = None
            self.emit(**{**result, "event": "test_result"})
            with self.lock:
                self.busy = not exit_known
                close_after_result = self.eof and exit_known
            if close_after_result:
                self.stop()

    def on_eof(self):
        with self.lock:
            self.eof = True
            if self.busy:
                self.emit(event="close_deferred", code="worker_busy")
                return
        self.stop()

    def read_commands(self):
        pending = b""
        try:
            while not self.closed and not self.closing:
                chunk = os.read(sys.stdin.fileno(), 4096)
                if not chunk:
                    if pending:
                        self.handle(pending)
                    break
                pending += chunk
                if len(pending) > 16384:
                    self.emit(event="command_error", code="invalid_command")
                    break
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    self.handle(line)
        except Exception:
            self.emit(event="command_error", code="stdin_read")
        finally:
            self.on_eof()

    def start(self):
        self.emit(event="ready", platforms=[self.platform], credentials_saved=False)
        # Raw os.read holds no buffered stdin lock during interpreter shutdown after a window close.
        threading.Thread(target=self.read_commands, daemon=True).start()

    def join_worker(self):
        if self.worker_thread is not None:
            self.worker_thread.join()


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if (len(args) != 2 or args[0] != "--platform" or not isinstance(args[1], str)
            or args[1] not in SPECS):
        print(json.dumps({"event": "start_failed", "code": "invalid_arguments"}), flush=True)
        return 2
    platform = PlatformId(args[1])
    spec = SPECS[platform]
    lab = None
    try:
        import webview

        logging.getLogger("pywebview").setLevel(logging.CRITICAL)
        name = "OPPO" if platform == PlatformId.OPPO else PLATFORM_NAMES[platform]
        window = webview.create_window(f"笔记互迁 · {name} 测试登录", url=spec.login_url, js_api=None,
                                        width=1100, height=780, min_size=(800, 600), text_select=True)
        lab = SessionLab(window, platform)
        window.menu = lab.menu()
        window.events.closing += lab.on_closing
        window.events.closed += lab.on_closed
        webview.start(func=lab.start, gui="edgechromium", debug=False, private_mode=True,
                      storage_path=str(ROOT / ".private" / f"webview-{platform}-session" / uuid.uuid4().hex))
        return 0
    except Exception:
        print(json.dumps({"event": "start_failed", "code": "webview_start"}), flush=True)
        return 1
    finally:
        if lab is not None:
            lab.join_worker()


if __name__ == "__main__":
    raise SystemExit(main())
