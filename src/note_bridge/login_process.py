"""Isolated private login windows; sessions cross only an inherited memory pipe."""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import threading
import uuid
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import urlparse

from .errors import BridgeError
from .models import PLATFORM_NAMES, PlatformId
from .providers.base import SPECS
from .providers.transport import host_matches

START_TIMEOUT = 30
COMMAND_TIMEOUT = 15
CLOSE_GRACE = 5
CHILD_EXIT_GRACE = 8
MAX_MESSAGE = 4 * 1024 * 1024
ERRORS = {
    "login_incomplete": "请先在官方窗口完成登录并进入笔记页面。",
    "login_window_closed": "登录窗口已关闭，请重新打开官方登录。",
    "login_start_failed": "官方登录窗口启动失败，请确认 WebView2 Runtime 可用后重试。",
    "login_timeout": "登录窗口响应超时，已关闭本次窗口，请重新打开官方登录。",
    "login_protocol_error": "登录窗口通信未完成，已清除本次会话，请重新登录。",
    "login_capture_failed": "未能读取官方登录状态，请进入笔记页面后重试。",
}


def _error(code):
    code = code if code in ERRORS else "login_protocol_error"
    return BridgeError(code, ERRORS[code])


def _send(connection, message):
    connection.send_bytes(json.dumps(message, ensure_ascii=True).encode("utf-8"))


def _receive(connection):
    result = json.loads(connection.recv_bytes(MAX_MESSAGE))
    if not isinstance(result, dict):
        raise ValueError("invalid_login_message")
    return result


def _snapshot(window, platform):
    """Return only this official platform's cookies, never a page URL or browser dump."""
    spec = SPECS[platform]
    url = urlparse(window.get_current_url() or "")
    if url.scheme != "https" or not host_matches(url.hostname or "", spec.domains):
        raise _error("login_incomplete")
    jars = window.get_cookies()
    try:
        rows = []
        for jar in jars:
            for morsel in jar.values():
                domain = morsel["domain"]
                if domain and host_matches(domain.lstrip("."), spec.domains):
                    rows.append({"name": morsel.key, "value": morsel.value, "domain": domain,
                                 "path": morsel["path"] or "/", "secure": bool(morsel["secure"])})
        return {"origin": f"https://{url.hostname}", "cookies": rows}
    finally:
        for jar in jars:
            jar.clear()
        jars.clear()


def _login_menu(window, platform, stopped):
    from webview.menu import Menu, MenuAction

    def navigate(reopen=False):
        if stopped.is_set():
            return
        try:
            if reopen:
                window.load_url(SPECS[platform].login_url)
            else:
                window.run_js("window.location.reload();")
        except Exception:
            # Native exceptions may contain a private page URL; show fixed text only.
            try:
                window.run_js("window.alert('页面操作未完成，请关闭此窗口后重新点击对应平台的登录。');")
            except Exception:
                pass  # A closing window no longer has a page on which to display feedback.

    return [Menu("页面", [MenuAction("刷新当前页面", navigate),
                         MenuAction("重新打开官网", lambda: navigate(reopen=True))])]


def _login_worker(platform, profile, connection):
    """Child entry point: no provider, application API, stdout session or cloud job."""
    # These overrides could otherwise select a shared/existing browser profile.
    for name in ("WEBVIEW2_USER_DATA_FOLDER", "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"):
        os.environ.pop(name, None)
    stopped = threading.Event()
    send_lock = threading.Lock()
    window = None
    exit_timer = None

    def emit(message):
        with send_lock:
            _send(connection, message)

    def close():
        nonlocal exit_timer
        if stopped.is_set():
            return
        stopped.set()
        # This process never performs a data task. A stuck native shutdown may be ended safely.
        # pywebview waits up to 3 s for WebView2 before removing this private profile.
        exit_timer = threading.Timer(CHILD_EXIT_GRACE, lambda: os._exit(0))
        exit_timer.daemon = True
        exit_timer.start()
        if window is not None:
            window.destroy()

    def watch_parent():
        parent = multiprocessing.parent_process()
        while not stopped.wait(0.5):
            if parent is not None and not parent.is_alive():
                close()
                return

    def commands():
        try:
            emit({"event": "ready", "platform": platform})
            while not stopped.is_set():
                request = _receive(connection)
                if (set(request) != {"id", "platform", "action"}
                        or type(request["id"]) is not int or request["platform"] != platform
                        or request["action"] not in ("show", "hide", "snapshot", "stop")):
                    raise ValueError("invalid_login_command")
                action = request["action"]
                if action == "stop":
                    close()
                    return
                result = None
                try:
                    if action == "snapshot":
                        result = _snapshot(window, platform)
                    elif action == "show":
                        window.show()
                    else:
                        window.hide()
                    emit({"id": request["id"], "ok": True, "data": result})
                except BridgeError as error:
                    emit({"id": request["id"], "ok": False, "code": error.code})
                except Exception:
                    emit({"id": request["id"], "ok": False, "code": "login_capture_failed"})
                finally:
                    if isinstance(result, dict):
                        result.clear()
                    result = None
        except (EOFError, OSError, ValueError):
            close()

    def ready():
        if not window.events.shown.wait(START_TIMEOUT):
            emit({"event": "failed", "code": "login_start_failed"})
            close()
            return
        threading.Thread(target=commands, daemon=True).start()

    try:
        platform = PlatformId(platform)
        if Path(profile).exists():
            raise ValueError("login_profile_must_be_new")
        import webview

        logging.getLogger("pywebview").setLevel(logging.CRITICAL)
        window = webview.create_window(f"{PLATFORM_NAMES[platform]} · 官方登录",
                                       url=SPECS[platform].login_url, js_api=None,
                                       width=1100, height=780, min_size=(800, 600), text_select=True)
        window.menu = _login_menu(window, platform, stopped)
        window.events.closed += stopped.set
        threading.Thread(target=watch_parent, daemon=True).start()
        webview.start(func=ready, gui="edgechromium", debug=False, private_mode=True,
                      storage_path=profile)
    except Exception:
        try:
            emit({"event": "failed", "code": "login_start_failed"})
        except (OSError, ValueError):
            pass
    finally:
        stopped.set()
        connection.close()
        if exit_timer is not None:
            exit_timer.cancel()


class LoginProcess:
    """A single platform window with bounded RPC and independent native lifetime."""

    def __init__(self, platform: PlatformId, root: Path, on_closed=None):
        self.platform = PlatformId(platform)
        self._on_closed = on_closed
        self._ready, self._closed = threading.Event(), threading.Event()
        self._state_lock, self._send_lock = threading.Lock(), threading.Lock()
        self._command_lock, self._close_lock = threading.Lock(), threading.Lock()
        self._pending = None
        self._sequence = 0
        self._failure = None
        context = multiprocessing.get_context("spawn")
        self._connection, child = context.Pipe(duplex=True)
        profile = root.resolve() / "browser-login" / f"{self.platform}-{uuid.uuid4().hex}"
        self._process = context.Process(target=_login_worker, args=(self.platform, str(profile), child),
                                        name=f"NoteBridgeLogin-{self.platform}", daemon=True)
        try:
            self._process.start()
        except Exception:
            self._connection.close()
            raise _error("login_start_failed") from None
        finally:
            child.close()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    @property
    def closed(self):
        return self._closed.is_set()

    def _read(self):
        try:
            if not self._connection.poll(START_TIMEOUT):
                self._failure = "login_timeout"
                return
            while True:
                message = _receive(self._connection)
                if message == {"event": "ready", "platform": self.platform}:
                    self._ready.set()
                elif message.get("event") == "failed":
                    self._failure = "login_start_failed"
                    break
                else:
                    with self._state_lock:
                        pending = self._pending
                        if (pending is None or message.get("id") != pending["id"]
                                or type(message.get("ok")) is not bool):
                            raise ValueError("unexpected_login_response")
                        pending["response"] = message
                        pending["event"].set()
        except ValueError:
            self._failure = "login_protocol_error"
        except (EOFError, OSError):
            pass
        finally:
            self._closed.set()
            self._ready.set()
            with self._state_lock:
                if self._pending is not None:
                    self._pending["event"].set()
            self._connection.close()
            self.destroy()
            if self._on_closed is not None:
                try:
                    self._on_closed(self, self._failure)
                except Exception:
                    pass  # The native process still closes if the application already shut down.

    def _request(self, action):
        if not self._command_lock.acquire(blocking=False):
            raise BridgeError("login_busy", "正在检查登录窗口，请等待当前操作结束。")
        pending = None
        try:
            if not self._ready.wait(START_TIMEOUT):
                self._failure = "login_timeout"
                self.destroy()
                raise _error("login_timeout")
            if self.closed:
                raise _error(self._failure or "login_window_closed")
            with self._state_lock:
                self._sequence += 1
                pending = {"id": self._sequence, "event": threading.Event(), "response": None}
                self._pending = pending
            with self._send_lock:
                _send(self._connection, {"id": pending["id"], "platform": self.platform, "action": action})
            if not pending["event"].wait(COMMAND_TIMEOUT):
                self._failure = "login_timeout"
                self.destroy()
                raise _error("login_timeout")
            response = pending["response"]
            if response is None or self.closed:
                raise _error(self._failure or "login_window_closed")
            if not response["ok"]:
                raise _error(response.get("code"))
            return response.get("data")
        except (EOFError, OSError, ValueError):
            self._failure = "login_protocol_error"
            self.destroy()
            raise _error("login_protocol_error") from None
        finally:
            with self._state_lock:
                self._pending = None
            if pending is not None:
                pending.clear()
            self._command_lock.release()

    def capture_session(self):
        snapshot = self._request("snapshot")
        jars = []
        try:
            if (not isinstance(snapshot, dict) or set(snapshot) != {"origin", "cookies"}
                    or not isinstance(snapshot["origin"], str)):
                raise ValueError("invalid_snapshot")
            spec = SPECS[self.platform]
            origin = urlparse(snapshot["origin"])
            if origin.scheme != "https" or not host_matches(origin.hostname or "", spec.domains):
                raise ValueError("invalid_origin")
            if not isinstance(snapshot["cookies"], list):
                raise ValueError("invalid_cookies")
            for item in snapshot["cookies"]:
                if (not isinstance(item, dict) or set(item) != {"name", "value", "domain", "path", "secure"}
                        or not all(isinstance(item[key], str) for key in ("name", "value", "domain", "path"))
                        or type(item["secure"]) is not bool
                        or not host_matches(item["domain"].lstrip("."), spec.domains)):
                    raise ValueError("invalid_cookie")
                jar = SimpleCookie()
                jar[item["name"]] = item["value"]
                for key in ("domain", "path", "secure"):
                    jar[item["name"]][key] = item[key]
                jars.append(jar)
            return jars
        except (ValueError, TypeError, KeyError, CookieError):
            for jar in jars:
                jar.clear()
            self.destroy()
            raise _error("login_protocol_error") from None
        finally:
            if isinstance(snapshot, dict):
                snapshot.clear()

    def show(self):
        self._request("show")

    def hide(self):
        self._request("hide")

    def destroy(self):
        with self._close_lock:
            if self._process.is_alive():
                try:
                    with self._send_lock:
                        _send(self._connection, {"id": 0, "platform": self.platform, "action": "stop"})
                except (OSError, ValueError):
                    pass
                self._process.join(CLOSE_GRACE)
                if self._process.is_alive():
                    # The child owns only login UI; provider reads/writes remain in the parent.
                    self._process.terminate()
                    self._process.join(1)
            self._closed.set()
            self._connection.close()
