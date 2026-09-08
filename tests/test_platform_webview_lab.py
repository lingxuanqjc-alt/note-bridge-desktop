"""A platform login host must not broaden scope, expose sessions, or interrupt writes."""
import importlib.util
import io
import json
import subprocess
import sys
import threading
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace

import pytest
from webview.menu import Menu

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("platform_webview_lab", ROOT / "scripts/platform-webview-lab.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class Window:
    def __init__(self, jars=None):
        self.jars = jars if jars is not None else []
        self.events = SimpleNamespace(closing=Event(), closed=Event())
        self.shown = self.destroyed = self.captures = 0
        self.navigation = []
        self.url = "https://cloud.oppo.com/"

    def get_current_url(self):
        return self.url

    def get_cookies(self):
        self.captures += 1
        return self.jars

    def show(self):
        self.shown += 1

    def run_js(self, script):
        self.navigation.append(("script", script))

    def load_url(self, url):
        self.navigation.append(("url", url))

    def destroy(self):
        assert all(handler() is not False for handler in self.events.closing.handlers)
        self.destroyed += 1
        for handler in self.events.closed.handlers:
            handler()


def lab_for(tmp_path, jars=None, platform="oppo"):
    window, output = Window(jars), io.StringIO()
    window.url = module.SPECS[platform].login_url
    lab = module.SessionLab(window, platform, tmp_path, output)
    window.events.closing += lab.on_closing
    window.events.closed += lab.on_closed
    return lab, window, output


def command(lab, action, platform="oppo"):
    lab.handle(json.dumps({"action": action, "platform": platform}))


def events(output):
    return [json.loads(line) for line in output.getvalue().splitlines()]


def cookie(name, domain):
    jar = SimpleCookie()
    jar[name] = "synthetic-secret-never-log"
    jar[name]["domain"] = domain
    jar[name]["path"] = "/notes"
    jar[name]["secure"] = True
    return jar


@pytest.mark.parametrize("platform", module.SPECS)
def test_product_engine_and_official_url_are_used_without_js_bridge_or_automatic_probe(monkeypatch, platform):
    window, created, started = Window(), [], []
    fake = SimpleNamespace(
        create_window=lambda *args, **kwargs: (created.append((args, kwargs)) or window),
        start=lambda **kwargs: started.append(kwargs),
    )
    monkeypatch.setitem(sys.modules, "webview", fake)
    assert module.main(["--platform", platform]) == 0
    name = "OPPO" if platform == "oppo" else module.PLATFORM_NAMES[platform]
    assert created == [((f"笔记互迁 · {name} 测试登录",), {
        "url": module.SPECS[platform].login_url, "js_api": None, "width": 1100, "height": 780,
        "min_size": (800, 600), "text_select": True})]
    options = started[0]
    assert options["gui"] == "edgechromium" and options["debug"] is False
    assert options["private_mode"] is True
    assert Path(options["storage_path"]).parent == ROOT / ".private" / f"webview-{platform}-session"
    assert options["func"].__self__.platform == platform
    assert options["func"].__self__.worker_thread is None and window.captures == 0
    assert isinstance(window.menu[0], Menu) and len(window.menu[0].items) == 2
    assert len(window.events.closing.handlers) == len(window.events.closed.handlers) == 1


@pytest.mark.parametrize("args", [[], ["--platform"], ["--platform", "unknown"], ["oppo"],
                                 ["--platform", "oppo", "--platform", "oppo"], ["--other", "oppo"],
                                 ["--platform", "all"], ["--platform", "oppo,vivo"],
                                 ["--platform", "oppo", "vivo"], ["--platform", "OPPO"],
                                 ["--platform", "../oppo"], ["--platform", ["oppo"]]])
def test_invalid_startup_cannot_import_or_open_a_browser(args, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(
        create_window=lambda *args, **kwargs: pytest.fail("invalid scope opened a browser")))
    assert module.main(args) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "invalid_arguments"


def test_each_host_gets_a_unique_private_directory_even_for_the_same_platform(monkeypatch):
    starts = []
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(
        create_window=lambda *a, **k: Window(), start=lambda **kwargs: starts.append(kwargs)))
    for platform in module.SPECS:
        for _ in range(2):
            assert module.main(["--platform", platform]) == 0
    paths = [Path(start["storage_path"]) for start in starts]
    assert len(paths) == len(set(paths)) == 14
    for start, path in zip(starts, paths, strict=True):
        platform = start["func"].__self__.platform
        assert path.parent == ROOT / ".private" / f"webview-{platform}-session"
        assert start["private_mode"] is True


@pytest.mark.parametrize("platform", module.SPECS)
def test_ready_only_starts_command_reader_without_capturing_credentials_or_arming_jobs(tmp_path, monkeypatch,
                                                                                    platform):
    lab, window, output = lab_for(tmp_path, platform=platform)
    started = []
    monkeypatch.setattr(module.threading, "Thread", lambda **kwargs: SimpleNamespace(
        start=lambda: started.append(kwargs)))
    lab.start()
    assert started == [{"target": lab.read_commands, "daemon": True}]
    assert events(output) == [{"event": "ready", "platforms": [platform], "credentials_saved": False}]
    assert window.captures == 0 and lab.worker_thread is None and not list(tmp_path.iterdir())


@pytest.mark.parametrize("platform", module.SPECS)
def test_other_platform_commands_cannot_capture_close_or_route_through_this_host(tmp_path, platform):
    lab, window, output = lab_for(tmp_path, platform=platform)
    for other in module.SPECS:
        if other != platform:
            for action in ("status", "open", "probe", "fetch", "stop"):
                command(lab, action, other)
    assert len(events(output)) == 30
    assert all(item == {"event": "command_error", "code": "invalid_command"} for item in events(output))
    assert window.captures == window.shown == window.destroyed == 0
    assert lab.worker_thread is None and not lab.busy and not lab.closed


def test_vivo_full_account_fetch_is_rejected_before_capturing_a_session(tmp_path, monkeypatch):
    lab, window, output = lab_for(tmp_path, platform="vivo")
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: pytest.fail("vivo full fetch reached worker"))
    command(lab, "fetch", "vivo")
    assert events(output) == [{"event": "test_result", "platform": "vivo", "status": "blocked",
                               "code": "synthetic_scope_required"}]
    assert window.captures == 0 and lab.worker_thread is None and not lab.busy


def test_window_start_failure_returns_only_a_code(monkeypatch, capsys):
    def fail(**kwargs):
        raise RuntimeError("synthetic-secret-never-log")

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=lambda *a, **k: Window(), start=fail))
    assert module.main(["--platform", "oppo"]) == 1
    assert events(io.StringIO(capsys.readouterr().out)) == [{"event": "start_failed", "code": "webview_start"}]


@pytest.mark.parametrize("platform", module.SPECS)
def test_only_selected_domains_enter_worker_stdin_never_arguments_environment_or_logs(tmp_path, monkeypatch,
                                                                                     platform):
    domains = module.SPECS[platform].domains
    approved_names = [f"official_{index}" for index in range(len(domains))] + ["subdomain"]
    jars = [cookie(f"official_{index}", "." + domain) for index, domain in enumerate(domains)]
    jars += [cookie("subdomain", "id." + domains[0]), cookie("unrelated", "unrelated.invalid"),
             cookie("suffix_attack", domains[0] + ".evil.invalid")]
    retained_jars = list(jars)
    lab, window, output = lab_for(tmp_path, jars, platform)
    calls, received = [], []

    class Worker:
        returncode = 0

        def communicate(self, payload):
            received.append(json.loads(payload))
            return json.dumps({"status": "authenticated_read_probe_passed", "platform": platform}), None

        def wait(self):
            return 0

    def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        assert "synthetic-secret-never-log" not in repr((args, kwargs))
        return Worker()

    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    command(lab, "probe", platform)
    lab.join_worker()
    args, options = calls[0]
    assert args == ([str(tmp_path / ".venv/Scripts/python.exe"), str(tmp_path / "scripts/probe-session.py")],)
    assert options["stdin"] == options["stdout"] == subprocess.PIPE
    assert options["stderr"] == subprocess.DEVNULL and "timeout" not in options
    assert options["creationflags"] == subprocess.CREATE_NO_WINDOW
    assert options["encoding"] == "utf-8" and options["env"]["PYTHONUTF8"] == "1"
    assert received[0]["action"] == "probe" and received[0]["platform"] == platform
    assert [item["name"] for item in received[0]["cookies"]] == approved_names
    assert all(item["path"] == "/notes" and item["secure"] for item in received[0]["cookies"])
    assert jars == [] and all(not jar for jar in retained_jars)
    assert "synthetic-secret-never-log" not in output.getvalue() and not list(tmp_path.iterdir())
    assert not lab.busy and window.destroyed == 0


@pytest.mark.parametrize("phase", ["capture", "spawn", "result", "exit_unknown"])
def test_failure_clears_captured_credentials_and_does_not_falsely_confirm_writes(tmp_path, monkeypatch, phase):
    jars = [cookie("official", ".oppo.com")]
    retained = jars[0]
    lab, window, output = lab_for(tmp_path, jars)
    waited = []

    class Worker:
        returncode = 0

        def communicate(self, payload):
            return "synthetic-secret-never-log", None

        def wait(self):
            waited.append(True)
            if phase == "exit_unknown":
                raise OSError("synthetic-secret-never-log")
            return 0

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic-secret-never-log")

    monkeypatch.setattr(module.subprocess, "Popen", fail if phase == "spawn" else lambda *a, **k: Worker())
    if phase == "capture":
        monkeypatch.setattr(window, "get_cookies", fail)
    command(lab, "fetch")
    lab.join_worker()
    report = events(output)[-1]
    assert report["status"] == ("needs_review" if phase in ("result", "exit_unknown") else "failed")
    assert "synthetic-secret-never-log" not in output.getvalue()
    if phase != "capture":
        assert jars == [] and not retained
    assert lab.busy is (phase == "exit_unknown")
    if phase in ("result", "exit_unknown"):
        assert waited == [True]
    if phase == "exit_unknown":
        assert lab.on_closing() is False and window.destroyed == 0


def test_close_eof_and_duplicate_work_cannot_interrupt_a_running_write(tmp_path, monkeypatch):
    lab, window, output = lab_for(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Worker:
        returncode = 0

        def communicate(self, payload):
            calls.append(json.loads(payload)["action"])
            entered.set()
            assert release.wait(5)
            return json.dumps({"status": "uncertain", "receipt_confirmed": False}), None

        def wait(self):
            assert release.is_set()

    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: Worker())
    command(lab, "probe")
    assert entered.wait(5)
    try:
        assert lab.on_closing() is False
        command(lab, "stop")
        command(lab, "fetch")
        command(lab, "status")
        lab.on_eof()
        assert lab.busy and window.destroyed == 0 and calls == ["probe"]
    finally:
        release.set()
        lab.join_worker()
    assert window.destroyed == 1 and lab.closed and not lab.busy
    reports = events(output)
    assert reports[-2] == {"event": "test_result", "status": "uncertain", "receipt_confirmed": False}
    assert reports[-1]["event"] == "session_closed"


def test_invalid_commands_do_not_start_work_and_eof_closes_only_the_owned_window(tmp_path, monkeypatch):
    lab, window, output = lab_for(tmp_path)
    for request in ["bad", "[]", '{"action":"probe"}', '{"platform":"oppo","action":"arm"}']:
        lab.handle(request)
    command(lab, "probe", "vivo")
    assert window.captures == 0 and lab.worker_thread is None
    assert all(item["code"] == "invalid_command" for item in events(output))
    chunks = iter([b'{"platform":"oppo","action":"open"}\n', b''])
    monkeypatch.setattr(module.os, "read", lambda *a: next(chunks))
    monkeypatch.setattr(module.sys, "stdin", SimpleNamespace(fileno=lambda: 0))
    lab.read_commands()
    assert window.shown == window.destroyed == 1 and lab.closed


@pytest.mark.parametrize("platform", module.SPECS)
@pytest.mark.parametrize("page", ["suffix_attack", "plain_http", "other_platform"])
def test_nonofficial_page_cannot_capture_a_session_or_start_worker(tmp_path, monkeypatch, platform, page):
    lab, window, output = lab_for(tmp_path, platform=platform)
    if page == "suffix_attack":
        window.url = "https://" + module.urlparse(window.url).hostname + ".evil.invalid/"
    elif page == "plain_http":
        window.url = window.url.replace("https://", "http://")
    else:
        other = "xiaomi" if platform == "oppo" else "oppo"
        window.url = module.SPECS[other].login_url
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: pytest.fail("untrusted page reached worker"))
    command(lab, "probe", platform)
    lab.join_worker()
    assert window.captures == 0 and not lab.busy
    assert events(output)[-1] == {"event": "test_result", "status": "blocked", "code": "login_incomplete"}


@pytest.mark.parametrize("output_error", [BrokenPipeError, ValueError])
def test_disconnected_stdout_does_not_prevent_worker_completion_or_eof_cleanup(tmp_path, monkeypatch, output_error):
    lab, window, _ = lab_for(tmp_path)

    class DisconnectedOutput:
        def write(self, value):
            raise output_error("controller_closed")

    class Worker:
        returncode = 0

        def communicate(self, payload):
            assert lab.on_closing() is False
            assert lab.busy and not lab.closing and window.destroyed == 0
            lab.on_eof()
            return json.dumps({"status": "uncertain"}), None

        def wait(self):
            return 0

    lab.output = DisconnectedOutput()
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: Worker())
    command(lab, "probe")
    lab.join_worker()
    assert not lab.busy and lab.closed and window.destroyed == 1


@pytest.mark.parametrize("platform", module.SPECS)
@pytest.mark.parametrize("action", ["reload", "home"])
def test_navigation_stays_in_selected_window_without_session_capture(tmp_path, platform, action):
    lab, window, output = lab_for(tmp_path, platform=platform)
    command(lab, action, platform)
    expected = ("url", module.SPECS[platform].login_url) if action == "home" else (
        "script", "window.location.reload();")
    assert window.navigation == [expected]
    assert not lab.busy and window.captures == 0 and lab.worker_thread is None
    assert events(output) == [{"event": "navigation_started", "platform": platform, "action": action}]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("state", ["busy", "closed", "closing"])
@pytest.mark.parametrize("action", ["reload", "home"])
def test_refresh_cannot_interrupt_work_or_reopen_closed_session(tmp_path, state, action):
    lab, window, output = lab_for(tmp_path)
    setattr(lab, state, True)
    command(lab, action)
    assert window.navigation == [] and window.captures == 0
    assert events(output)[-1]["code"] == ("worker_busy" if state == "busy" else "session_closed")


def test_native_menu_uses_same_guarded_navigation_without_arbitrary_input(tmp_path):
    lab, window, _ = lab_for(tmp_path, platform="meizu")
    menu = lab.menu()
    assert menu[0].title == "页面"
    assert [item.title for item in menu[0].items] == ["刷新当前页面", "重新打开官网"]
    menu[0].items[0].function()
    menu[0].items[1].function()
    assert window.navigation == [("script", "window.location.reload();"),
                                  ("url", module.SPECS["meizu"].login_url)]


def test_navigation_failure_is_sanitized_and_releases_guard(tmp_path, monkeypatch):
    lab, window, output = lab_for(tmp_path)

    def fail(script):
        assert lab.busy and lab.on_closing() is False
        command(lab, "probe")
        raise RuntimeError("synthetic-secret-never-log")

    monkeypatch.setattr(window, "run_js", fail)
    command(lab, "reload")
    assert not lab.busy and not lab.closing and window.captures == 0
    assert "synthetic-secret-never-log" not in output.getvalue()
    assert events(output)[-1] == {"event": "command_error", "code": "navigation_failed"}
