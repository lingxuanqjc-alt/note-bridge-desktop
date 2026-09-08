"""Prevent concurrent desktop processes from operating on the same local database."""

import ctypes
import hashlib
from pathlib import Path


class InstanceLock:
    def __init__(self, data_directory: Path):
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        self._kernel.CreateMutexW.restype = ctypes.c_void_p
        self._kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        name = (
            "Local\\NoteBridge-"
            + hashlib.sha256(str(data_directory.resolve()).lower().encode()).hexdigest()[:24]
        )
        self._handle = self._kernel.CreateMutexW(None, False, name)
        if not self._handle:
            raise OSError("Unable to create application instance mutex")
        self.already_running = ctypes.get_last_error() == 183

    def close(self):
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None
