"""统一的 422 错误类型。

任何输入证据缺失、类型矛盾、签名不可桥接、ELF 非法或架构不符都抛出
``BridgeError``，由 HTTP 层转换为 422，且不会留下可下载的半成品。
"""

from __future__ import annotations


class BridgeError(Exception):
    """整单拒绝（HTTP 422）。"""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field

    def detail(self) -> dict[str, str]:
        d = {"error": self.message}
        if self.field:
            d["field"] = self.field
        return d
