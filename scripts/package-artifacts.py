"""Package explicit deliverables only; keep reference files and private data out."""
import hashlib
import json
import zipfile
from pathlib import Path

from note_bridge import __release_channel__, __version__

root = Path(__file__).resolve().parents[1]
dist = root / "dist"
stage = dist / "NoteBridge"
assert (stage / "NoteBridge.exe").is_file(), "Build the application first"
payload = sorted(file for file in stage.rglob("*") if file.is_file())
assert not any(file.is_symlink() for file in payload)
manifest = [f'Delete "$INSTDIR\\{str(file.relative_to(stage)).replace(chr(36), chr(36)*2)}"' for file in payload]
directories = sorted((file for file in stage.rglob("*") if file.is_dir()), key=lambda p: len(p.parts), reverse=True)
manifest += [f'RMDir "$INSTDIR\\{str(directory.relative_to(stage)).replace(chr(36), chr(36)*2)}"' for directory in directories]
(root / "build" / "uninstall-files.nsh").write_text("\n".join(manifest), encoding="utf-8-sig")
with zipfile.ZipFile(dist / f"NoteBridge-{__version__}-windows-x64.zip", "w", zipfile.ZIP_DEFLATED) as archive:
    for file in payload:
        archive.write(file, "NoteBridge/" + file.relative_to(stage).as_posix())
source_roots = ["src", "ui/src", "scripts", "tests", "docs", "packaging", "assets", "licenses", ".github"]
source_files = ["pyproject.toml", "uv.lock", "LICENSE", ".gitignore", "README.md", "README-development.md", "CONTRIBUTING.md", "SECURITY.md", "启动笔记互迁.cmd", "ui/package.json", "ui/package-lock.json", "ui/tsconfig.json", "ui/vite.config.ts", "ui/index.html"]
with zipfile.ZipFile(dist / f"note-bridge-desktop-{__version__}-source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
    files = [root / name for name in source_files]
    files += [file for directory in source_roots for file in (root / directory).rglob("*") if file.is_file() and "__pycache__" not in file.parts]
    for file in sorted(set(files)):
        assert not file.is_symlink() and file.resolve().is_relative_to(root)
        archive.write(file, "note-bridge-desktop/" + file.relative_to(root).as_posix())
checksums = {file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in dist.iterdir() if file.suffix in (".exe", ".zip")}
(dist / "SHA256SUMS.txt").write_text("\n".join(f"{digest}  {name}" for name, digest in checksums.items()) + "\n", encoding="utf8")
(dist / "build-manifest.json").write_text(json.dumps({"version": __version__, "release_channel": __release_channel__, "formal_release_ready": False, "sha256": checksums}, indent=2), encoding="utf8")
print("Packaged source, portable ZIP and uninstall allowlist.")
