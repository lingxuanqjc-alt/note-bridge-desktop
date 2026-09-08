"""Collect installed dependency notices without reading configuration or private data."""
import importlib.metadata
import json
import shutil
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
destination = root / "licenses"
destination.mkdir(exist_ok=True)
records = []
for distribution in sorted(importlib.metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
    name = distribution.metadata["Name"]
    if name == "note-bridge-desktop":
        continue
    copied = []
    for file in distribution.files or []:
        value = str(file).lower()
        if ".dist-info/" not in value or not any(word in file.name.lower() for word in ("license", "copying", "notice")):
            continue
        source = Path(distribution.locate_file(file))
        if not source.is_file():
            continue
        target = destination / name / file.name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(source, target)
        copied.append(target.relative_to(destination).as_posix())
    records.append({"name": name, "version": distribution.version,
                    "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License", "Unspecified in wheel metadata"),
                    "sources": distribution.metadata.get_all("Project-URL", []) or [distribution.metadata.get("Home-page", "")], "license_files": copied})
for candidate in [Path(sys.base_prefix) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE"]:
    if candidate.is_file():
        shutil.copyfile(candidate, destination / "Python-LICENSE.txt")
        break
(destination / "dependencies.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf8")
frontend = []
lock = json.loads((root / "ui/package-lock.json").read_text("utf-8"))
for package_path, locked in lock["packages"].items():
    if not package_path:
        continue
    package_root = root / "ui" / package_path
    name = package_path.rsplit("node_modules/", 1)[-1]
    copied = []
    if package_root.is_dir():
        for file in package_root.iterdir():
            if file.is_file() and file.name.lower().split(".")[0] in ("license", "licence", "copying", "notice"):
                target = destination / "npm" / name.replace("/", "_") / file.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, target)
                copied.append(target.relative_to(destination).as_posix())
    frontend.append({"name": name, "version": locked.get("version"), "license": locked.get("license"),
                     "source": locked.get("resolved"), "license_files": copied,
                     "installed_on_this_platform": package_root.is_dir()})
(destination / "frontend-dependencies.json").write_text(json.dumps(frontend, ensure_ascii=False, indent=2), "utf-8")
(destination / "README.md").write_text("# 第三方依赖\n\n本目录记录当前锁定环境中的运行和构建依赖及其分发许可。自写代码采用 MIT；各第三方组件遵守各自许可。\n\nWebView2 Runtime 由微软独立分发，本安装包不包含该运行时。pywebview 中的 Microsoft WebView2 SDK 加载组件遵守微软 SDK 许可：https://www.nuget.org/packages/Microsoft.Web.WebView2/ 。\n\n字体使用 Windows 系统字体，不随软件分发。平台名称使用文本识别，没有复制原包的标志图片。主图标由 scripts/make-icon.py 原创生成。\n", encoding="utf8")
print(f"Collected dependency notices: Python {len(records)}, npm lock entries {len(frontend)}")
