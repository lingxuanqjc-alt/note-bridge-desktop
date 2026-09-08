"""Windows entry point. Only the installed, trusted UI receives a Python bridge."""

from __future__ import annotations

import argparse
import ctypes
import logging
import multiprocessing
import sys
import threading
import time
from pathlib import Path

from .bridge import Bridge
from .paths import AppPaths


def frontend_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "ui" / "index.html"
    return Path(__file__).resolve().parents[2] / "ui" / "dist" / "index.html"


def main():
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="笔记互迁")
    parser.add_argument("--check", action="store_true", help="检查本地安装资源，不启动窗口")
    parser.add_argument("--smoke-test", action="store_true", help="验证真实窗口和桥接后退出，不进行平台登录")
    args = parser.parse_args()
    index = frontend_path()
    if args.check:
        print("frontend=present" if index.is_file() else "frontend=missing")
        return 0 if index.is_file() else 1
    if sys.platform != "win32":
        raise SystemExit("当前桌面版本支持 Windows 10／11。")
    if not index.is_file():
        ctypes.windll.user32.MessageBoxW(
            None,
            "缺少界面资源。源码运行请先执行 npm run build --prefix ui；安装版本请重新安装。",
            "笔记互迁",
            0x10,
        )
        return 1
    import webview

    logging.getLogger("pywebview").setLevel(logging.CRITICAL)
    paths = AppPaths.default()
    paths.prepare()
    from .instance import InstanceLock

    instance = InstanceLock(paths.root)
    if instance.already_running:
        instance.close()
        ctypes.windll.user32.MessageBoxW(None, "笔记互迁已在运行，请切回已打开的窗口。", "笔记互迁", 0x40)
        return 0
    bridge = Bridge(paths)
    window = webview.create_window(
        "笔记互迁",
        str(index),
        js_api=bridge,
        width=1200,
        height=850,
        min_size=(900, 650),
        frameless=True,
        easy_drag=False,
        background_color="#f4f5f7",
        text_select=True,
    )
    bridge._bind(window)
    window.events.closing += bridge._closing
    window.events.closed += bridge._shutdown
    smoke = {"ok": False}

    def smoke_test():
        try:
            if not window.events.loaded.wait(30):
                raise RuntimeError("window_not_loaded")
            for _ in range(30):
                if window.evaluate_js("Boolean(window.pywebview?.api?.get_app_state)"):
                    break
                time.sleep(0.2)
            completed = threading.Event()

            def receive(result):
                smoke["ok"] = bool(
                    result and result.get("ok") and len(result.get("data", {}).get("platforms", [])) == 7
                )
                completed.set()

            window.evaluate_js("window.pywebview.api.get_app_state()", callback=receive)
            completed.wait(15)
            print("native_bridge=" + ("passed" if smoke["ok"] else "failed"), flush=True)
        except Exception as error:
            print("native_bridge_error=" + type(error).__name__, flush=True)
        finally:
            window.destroy()

    try:
        webview.start(
            func=smoke_test if args.smoke_test else None,
            gui="edgechromium",
            debug=False,
            private_mode=True,
            storage_path=str(paths.root / "browser-session"),
        )
    except Exception as error:
        ctypes.windll.user32.MessageBoxW(
            None,
            f"桌面窗口启动失败（{type(error).__name__}）。请确认已安装 Microsoft Edge WebView2 Runtime。官方地址：https://developer.microsoft.com/microsoft-edge/webview2/",
            "笔记互迁",
            0x10,
        )
        return 1
    finally:
        bridge._shutdown()
        instance.close()
    return 0 if not args.smoke_test or smoke["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
