"""Login isolation must preserve other sessions and fail closed when native IPC ends."""

import ctypes
import json
import multiprocessing
import os
import runpy
import sys
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace

import pytest

from note_bridge import login_process as module
from note_bridge.bridge import Bridge
from note_bridge.errors import BridgeError
from note_bridge.models import PlatformId
from note_bridge.paths import AppPaths
from note_bridge.providers.base import SPECS

REAL_WORKER = module._login_worker


class Event:
    def __init__(self):
        self.flag = threading.Event()
        self.handlers = []

    def __iadd__(self, callback):
        self.handlers.append(callback)
        return self

    def set(self):
        self.flag.set()
        for callback in self.handlers:
            callback()

    def wait(self, timeout=None):
        return self.flag.wait(timeout)


class SyntheticWindow:
    def __init__(self, platform, value="synthetic-private-session"):
        self.platform, self.value = platform, value
        self.url = SPECS[platform].login_url
        self.events = SimpleNamespace(shown=Event(), closed=Event())
        self.closed = self.hidden = False
        self.captures = 0

    def get_current_url(self):
        return self.url

    def get_cookies(self):
        self.captures += 1
        jars = []
        for domain in (*SPECS[self.platform].domains, "unrelated.invalid"):
            jar = SimpleCookie()
            jar["synthetic"] = self.value
            jar["synthetic"]["domain"] = domain
            jar["synthetic"]["path"] = "/"
            jars.append(jar)
        return jars

    def show(self):
        self.hidden = False

    def hide(self):
        self.hidden = True

    def destroy(self):
        self.closed = True
        self.events.closed.set()


def synthetic_worker(platform, profile, connection, cleanup_delay=0):
    """Exercise the production child/IPC with a synthetic window, no GUI or network."""
    from webview.menu import Menu

    window = SyntheticWindow(platform, Path(profile).name)

    def start(func, **options):
        assert options == {"gui": "edgechromium", "debug": False, "private_mode": True,
                           "storage_path": profile}
        assert isinstance(window.menu[0], Menu) and window.menu[0].title == "页面"
        assert [item.title for item in window.menu[0].items] == ["刷新当前页面", "重新打开官网"]
        window.events.shown.set()
        func()
        assert window.events.closed.wait(8)
        if cleanup_delay:
            time.sleep(cleanup_delay)
            marker = Path(profile).parent / (Path(profile).name + ".cleaned")
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("synthetic native cleanup completed", "utf-8")

    def create(title, **options):
        assert options["url"] == SPECS[platform].login_url and options["js_api"] is None
        return window

    sys.modules["webview"] = SimpleNamespace(create_window=create, start=start)
    # Frozen Windows apps need neither a console nor stdout for the session channel.
    sys.stdin = sys.stdout = sys.stderr = None
    REAL_WORKER(platform, profile, connection)


def unresponsive_worker(platform, profile, connection):
    module._send(connection, {"event": "ready", "platform": platform})
    module._receive(connection)
    time.sleep(10)


def delayed_cleanup_worker(platform, profile, connection):
    synthetic_worker(platform, profile, connection, cleanup_delay=3.25)


def startup_stalled_worker(platform, profile, connection):
    time.sleep(10)


def mismatched_reply_worker(platform, profile, connection):
    module._send(connection, {"event": "ready", "platform": platform})
    request = module._receive(connection)
    module._send(connection, {"id": request["id"] + 1, "ok": True,
                              "data": "synthetic-secret-never-accept"})
    time.sleep(10)


def parent_surrogate(root, evidence_connection):
    module._login_worker = synthetic_worker
    host = module.LoginProcess(PlatformId.OPPO, Path(root))
    host.capture_session()
    evidence_connection.send(host._process.pid)
    evidence_connection.recv()
    os._exit(0)  # Intentional abrupt exit of this synthetic parent only.


@pytest.mark.parametrize("platform", SPECS)
def test_snapshot_restricts_origin_and_domains_without_returning_private_url_or_printing(platform, capsys):
    window = SyntheticWindow(platform)
    window.url += "?synthetic-auth-token=must-not-cross"
    snapshot = module._snapshot(window, platform)
    assert "synthetic-auth-token" not in json.dumps(snapshot)
    assert {item["domain"] for item in snapshot["cookies"]} == set(SPECS[platform].domains)
    assert all(item["value"] == "synthetic-private-session" for item in snapshot["cookies"])
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("url", ["http://cloud.oppo.com/", "https://cloud.oppo.com.evil.invalid/",
                               "https://yun.vivo.com.cn/", "about:blank"])
def test_untrusted_page_does_not_read_any_cookie(url):
    window = SyntheticWindow(PlatformId.OPPO)
    window.url = url
    with pytest.raises(BridgeError, match="官方窗口") as error:
        module._snapshot(window, PlatformId.OPPO)
    assert error.value.code == "login_incomplete" and window.captures == 0


@pytest.mark.parametrize("platform", SPECS)
def test_native_menu_refreshes_only_current_page_or_fixed_official_entry(platform, capsys):
    calls = []
    stopped = threading.Event()
    window = SimpleNamespace(run_js=lambda script: calls.append(("js", script)),
                             load_url=lambda url: calls.append(("url", url)))
    menu = module._login_menu(window, platform, stopped)[0]
    assert menu.title == "页面" and [item.title for item in menu.items] == ["刷新当前页面", "重新打开官网"]
    assert not calls, "Building the menu must not navigate or inspect an account."
    menu.items[0].function()
    menu.items[1].function()
    assert calls == [("js", "window.location.reload();"), ("url", SPECS[platform].login_url)]
    stopped.set()
    for item in menu.items:
        item.function()
    assert len(calls) == 2, "Closed login windows must never be reopened by a queued menu action."
    assert capsys.readouterr() == ("", "")


def test_native_menu_failure_shows_fixed_feedback_without_exposing_private_url(capsys):
    shown = []
    def failed(url):
        raise RuntimeError("https://official.invalid/?synthetic-private-token")
    window = SimpleNamespace(run_js=shown.append, load_url=failed)
    menu = module._login_menu(window, PlatformId.OPPO, threading.Event())[0]
    menu.items[1].function()
    assert len(shown) == 1 and "页面操作未完成" in shown[0]
    assert "synthetic-private-token" not in shown[0] and capsys.readouterr() == ("", "")


@pytest.mark.parametrize("failure", ["origin", "domain", "type", "cookie_name"])
def test_parent_rejects_cross_platform_or_malformed_session_and_clears_reply(failure):
    host = object.__new__(module.LoginProcess)
    host.platform = PlatformId.OPPO
    row = {"name": "synthetic", "value": "synthetic-secret", "domain": "oppo.com",
           "path": "/", "secure": True}
    snapshot = {"origin": "https://cloud.oppo.com", "cookies": [row]}
    if failure == "origin":
        snapshot["origin"] = "https://yun.vivo.com.cn"
    elif failure == "domain":
        row["domain"] = "oppo.com.evil.invalid"
    elif failure == "type":
        snapshot["origin"] = 123
    else:
        row["name"] = "invalid cookie name"
    destroyed = []
    host._request = lambda action: snapshot
    host.destroy = lambda: destroyed.append(True)
    with pytest.raises(BridgeError) as error:
        host.capture_session()
    assert error.value.code == "login_protocol_error"
    assert "synthetic-secret" not in str(error.value)
    assert snapshot == {} and destroyed == [True]


def test_real_spawned_login_processes_keep_profiles_and_sessions_separate(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(module, "_login_worker", synthetic_worker)
    closed = []
    hosts = []
    try:
        first = module.LoginProcess(PlatformId.OPPO, tmp_path, lambda host, code: closed.append(host.platform))
        hosts.append(first)
        first_session = first.capture_session()
        first_value = first_session[0]["synthetic"].value
        second = module.LoginProcess(PlatformId.HUAWEI, tmp_path)
        hosts.append(second)
        second_session = second.capture_session()
        second_value = second_session[0]["synthetic"].value
        assert first._process.pid != second._process.pid
        assert first_value.startswith("oppo-") and second_value.startswith("huawei-")
        assert first_value != second_value
        assert first.capture_session()[0]["synthetic"].value == first_value
        first.hide()
        first.show()
        first.destroy()
        assert not first._process.is_alive()
        assert second.capture_session()[0]["synthetic"].value == second_value
        reopened = module.LoginProcess(PlatformId.OPPO, tmp_path)
        hosts.append(reopened)
        assert reopened.capture_session()[0]["synthetic"].value != first_value
    finally:
        for host in hosts:
            host.destroy()
    assert all(not host._process.is_alive() for host in hosts)
    assert capsys.readouterr() == ("", "")
    assert not list(tmp_path.rglob("*")), "Synthetic sessions must never be persisted by the IPC layer."


def test_unresponsive_child_is_terminated_without_falsely_reporting_a_session(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_login_worker", unresponsive_worker)
    monkeypatch.setattr(module, "COMMAND_TIMEOUT", 0.05)
    host = module.LoginProcess(PlatformId.OPPO, tmp_path)
    started = time.monotonic()
    try:
        with pytest.raises(BridgeError) as error:
            host.capture_session()
        assert error.value.code == "login_timeout"
        assert host.closed and not host._process.is_alive()
        assert time.monotonic() - started < 10
    finally:
        host.destroy()


def test_startup_timeout_closes_child_even_without_an_account_check(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_login_worker", startup_stalled_worker)
    monkeypatch.setattr(module, "START_TIMEOUT", 0.05)
    callback = threading.Event()
    codes = []

    def closed(host, code):
        codes.append(code)
        callback.set()

    host = module.LoginProcess(PlatformId.OPPO, tmp_path, closed)
    try:
        assert callback.wait(8)
        assert codes == ["login_timeout"] and host.closed and not host._process.is_alive()
    finally:
        host.destroy()


def test_mismatched_ipc_reply_cannot_become_an_account_session(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_login_worker", mismatched_reply_worker)
    host = module.LoginProcess(PlatformId.OPPO, tmp_path)
    try:
        with pytest.raises(BridgeError) as error:
            host.capture_session()
        assert error.value.code == "login_protocol_error"
        assert "synthetic-secret" not in str(error.value) and host.closed
    finally:
        host.destroy()
    assert not host._process.is_alive()


def test_native_profile_cleanup_finishes_before_parent_or_child_hard_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_login_worker", delayed_cleanup_worker)
    host = module.LoginProcess(PlatformId.OPPO, tmp_path)
    host.capture_session()
    try:
        host.destroy()
        assert host._process.exitcode == 0
        markers = list((tmp_path / "browser-login").glob("*.cleaned"))
        assert len(markers) == 1
        assert markers[0].read_text("utf-8") == "synthetic native cleanup completed"
    finally:
        host.destroy()


@pytest.mark.skipif(os.name != "nt", reason="Windows parent-death handle verification")
def test_abrupt_parent_exit_closes_its_owned_synthetic_login_child(tmp_path):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe()
    parent = context.Process(target=parent_surrogate, args=(str(tmp_path), sender))
    parent.start()
    sender.close()
    handle = None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    try:
        assert receiver.poll(8)
        child_pid = receiver.recv()
        handle = kernel.OpenProcess(0x100000, False, child_pid)
        assert handle
        receiver.send("exit")
        parent.join(3)
        assert parent.exitcode == 0
        assert kernel.WaitForSingleObject(handle, 5000) == 0, "The orphan must close its authorized session."
    finally:
        receiver.close()
        if parent.is_alive():
            parent.terminate()
            parent.join(2)
        if handle:
            kernel.CloseHandle(handle)


def test_frozen_entry_dispatches_workers_before_importing_the_main_app(monkeypatch):
    observed = []

    class WorkerDispatched(Exception):
        pass

    def freeze():
        observed.append("freeze_support")
        raise WorkerDispatched

    monkeypatch.setattr(multiprocessing, "freeze_support", freeze)
    monkeypatch.setitem(sys.modules, "note_bridge.app", SimpleNamespace(
        main=lambda: pytest.fail("worker reached UI/instance-lock startup")))
    entry = Path(__file__).resolve().parents[1] / "packaging/entry.py"
    with pytest.raises(WorkerDispatched):
        runpy.run_path(str(entry), run_name="__main__")
    assert observed == ["freeze_support"]


@pytest.mark.parametrize("failure", ["closed_during_probe", "hide_failed"])
def test_bridge_never_accepts_a_session_after_its_window_disappears(tmp_path, monkeypatch, failure):
    paths = AppPaths(tmp_path)
    paths.prepare()
    bridge = Bridge(paths)
    closed = []
    jar = SimpleCookie("synthetic=synthetic-private-session")
    window = SimpleNamespace(capture_session=lambda: [jar], closed=False)

    def probe():
        if failure == "closed_during_probe":
            window.closed = True
            bridge._login_windows.pop(PlatformId.OPPO)
        return "synthetic-account"

    def hide():
        if failure == "hide_failed":
            raise module._error("login_window_closed")

    window.hide = hide
    bridge._login_windows[PlatformId.OPPO] = window
    provider = SimpleNamespace(probe=probe, close=lambda: closed.append(True))
    monkeypatch.setattr("note_bridge.bridge.create_provider", lambda *a: provider)
    result = bridge.complete_login("oppo")
    assert result["ok"] is False and result["error"]["code"] == "login_window_closed"
    assert closed == [True] and not jar and not bridge._checking
    assert bridge._providers[PlatformId.OPPO].account_id is None


def test_bridge_reuses_each_platform_host_and_old_close_cannot_remove_a_replacement(tmp_path, monkeypatch):
    paths = AppPaths(tmp_path)
    paths.prepare()
    bridge = Bridge(paths)
    created, shown = [], []

    def create(platform, root, on_closed):
        window = SimpleNamespace(platform=platform, close_callback=on_closed,
                                 show=lambda: shown.append(platform), destroy=lambda: None)
        created.append(window)
        return window

    monkeypatch.setattr("note_bridge.bridge.LoginProcess", create)
    assert bridge.login_platform("oppo")["ok"]
    assert bridge.login_platform("oppo")["ok"]
    assert bridge.login_platform("huawei")["ok"]
    assert len(created) == 2 and shown == [PlatformId.OPPO]
    old = created[0]
    old.close_callback(old, None)
    assert bridge.login_platform("oppo")["ok"]
    old.close_callback(old, "login_timeout")
    assert bridge._login_windows[PlatformId.OPPO] is created[2]
    bridge._shutdown()
    assert not bridge._login_windows
    assert bridge.login_platform("oppo")["error"]["code"] == "application_closed"
