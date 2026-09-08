"""Keep application data separate from installed code and user exports."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import BridgeError


def safe_filename(value: str, fallback: str = "无标题", limit: int = 90) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", value).strip().rstrip(". ")
    value = value[:limit].rstrip(". ") or fallback
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?", value, re.I):
        value = "_" + value
    return value


def confined(root: Path, relative: str | Path) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise BridgeError("unsafe_path", "文件路径超出本次操作目录。")
    return path


@dataclass(frozen=True)
class AppPaths:
    root: Path

    @classmethod
    def default(cls) -> AppPaths:
        override = os.environ.get("NOTE_BRIDGE_DATA_DIR")
        base = (
            Path(override) if override else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "NoteBridge"
        )
        return cls(base.resolve())

    @property
    def database(self) -> Path:
        return self.root / "notes.sqlite"

    @property
    def resources(self) -> Path:
        return self.root / "resources"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    def prepare(self) -> None:
        for path in (self.root, self.resources, self.logs, self.reports):
            path.mkdir(parents=True, exist_ok=True)
