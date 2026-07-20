"""領域層例外。"""
from __future__ import annotations


class OperationCancelled(Exception):
    """使用者主動取消操作（例如中止下載）。"""
