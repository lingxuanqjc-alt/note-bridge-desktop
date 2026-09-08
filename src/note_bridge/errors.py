"""Explicit, safe-to-display failures; raw HTTP details are never UI messages."""


class BridgeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Cancelled(BridgeError):
    def __init__(self):
        super().__init__("cancelled", "操作已取消，已经成功写入的笔记会保留。")


class WriteUncertain(BridgeError):
    def __init__(self, message="目标端可能已完成写入。请先在目标平台核对，再决定是否重试。"):
        super().__init__("write_uncertain", message)
