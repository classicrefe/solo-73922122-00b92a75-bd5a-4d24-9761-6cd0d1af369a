"""领域异常。"""

from __future__ import annotations

from collections.abc import Iterable


class ValidationError(ValueError):
    """用户输入不符合格式或规则。"""

    def __init__(self, reasons: Iterable[str]):
        self.reasons = list(reasons)
        super().__init__("；".join(self.reasons) or "输入无效")


class StorageError(RuntimeError):
    """数据库写入失败；调用方应报告原因并保持原状。"""
