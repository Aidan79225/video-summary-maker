"""領域層例外。"""
from __future__ import annotations


class OperationCancelled(Exception):
    """使用者主動取消操作。"""


class NoSubtitlesAvailable(Exception):
    """影片沒有任何可用的字幕軌。"""


class SummarizerUnavailable(Exception):
    """摘要服務連不上，或指定的模型不存在。"""


class SummarizerOutputInvalid(Exception):
    """模型輸出重試後仍不符合要求。"""
