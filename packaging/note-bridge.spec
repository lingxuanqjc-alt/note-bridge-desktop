# Only explicit project deliverables are bundled. Never collect the workspace recursively.
from pathlib import Path

root = Path(SPECPATH).parent
a = Analysis(
    [str(root / "packaging" / "entry.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=[
        (str(root / "ui" / "dist"), "ui"),
        (str(root / "LICENSE"), "."),
        (str(root / "docs" / "GUIDE.md"), "docs"),
        (str(root / "docs" / "COMPATIBILITY.md"), "docs"),
        (str(root / "licenses"), "licenses"),
    ],
    hiddenimports=["webview.platforms.winforms", "webview.platforms.edgechromium"],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "cefpython3", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="NoteBridge", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          icon=str(root / "assets" / "note-bridge.ico"),
          version=str(root / "packaging" / "windows-version.txt"))
collect = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="NoteBridge")
