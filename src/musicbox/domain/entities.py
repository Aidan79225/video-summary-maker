"""領域實體：不依賴任何框架或 IO，純資料與規則。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DownloadFormat(Enum):
    """下載格式。"""
    MP4 = "mp4"   # 影片
    MP3 = "mp3"   # 音樂（抽音訊）


@dataclass(frozen=True)
class DownloadRequest:
    url: str
    fmt: DownloadFormat
    output_dir: str
    max_height: int | None = None   # MP4 畫質上限（像素高度）；None = 最高畫質


class RenameMode(Enum):
    """重新編號的模式。"""
    RENUMBER = "renumber"   # 連續重編，填補空缺（01,02,03...）
    KEEP = "keep"           # 保留原號，只統一格式


@dataclass(frozen=True)
class RenameOptions:
    separator: str = "-"                      # 號碼與歌名之間的分隔符
    mode: RenameMode = RenameMode.RENUMBER
    padding: int = 2                          # 補零位數（至少）
    extensions: frozenset[str] = frozenset({".mp3"})


@dataclass(frozen=True)
class RenameItem:
    old_name: str
    new_name: str

    @property
    def changed(self) -> bool:
        return self.old_name != self.new_name


@dataclass(frozen=True)
class RenamePlan:
    folder: str
    items: tuple[RenameItem, ...]

    @property
    def changed_count(self) -> int:
        return sum(1 for it in self.items if it.changed)


@dataclass
class Settings:
    """使用者設定，持久化於 settings.json。"""
    output_dir: str
    rename_folder: str
    fmt: DownloadFormat = DownloadFormat.MP4
    max_height: int | None = None
    separator: str = "-"
    mode: RenameMode = RenameMode.RENUMBER
    padding: int = 2
