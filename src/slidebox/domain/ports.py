"""領域介面（port）：由 infrastructure 實作，usecases 依賴這些抽象。"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Callable, Protocol

from .entities import Deck, Slide, Transcript

# 進度回報：fraction 為 0..1，None 表示不確定；status 為文字說明。
ProgressCallback = Callable[[float | None, str], None]

# 取消判斷：回傳 True 表示使用者已要求取消。
CancelCheck = Callable[[], bool]


class SubtitleGateway(Protocol):
    def fetch(self, url: str, langs: Sequence[str]) -> Transcript:
        """抓字幕並解析。找不到任何可用字幕時 raise NoSubtitlesAvailable。"""
        ...


class Summarizer(Protocol):
    def summarize(
        self,
        compressed: str,
        duration: float,
        min_slides: int,
        max_slides: int,
        hint: str,
        progress: ProgressCallback,
    ) -> tuple[Slide, ...]:
        """回傳 image_path 皆為 None 的 Slide 序列。

        compressed 是 compress_cues() 的產出——壓縮是純邏輯，不是
        summarizer 的責任。hint 為空字串表示首次嘗試，非空時是上一次
        的驗證錯誤，會附進提示裡要求模型修正。
        """
        ...


class VideoSectionGateway(Protocol):
    def download_sections(
        self,
        url: str,
        timestamps: Sequence[float],
        max_height: int | None,
        dest_dir: str,
        progress: ProgressCallback,
        is_cancelled: CancelCheck,
    ) -> list[str | None]:
        """每個時間點回傳一個本地片段檔路徑；該點失敗則該位置為 None。

        自行建立 dest_dir。回傳長度必須等於 timestamps 長度。
        """
        ...

    def cleanup(self, dest_dir: str) -> None:
        """刪除暫存片段目錄。由 use case 在 finally 裡呼叫。

        **必須容忍目錄不存在，且絕不可 raise。** 取消發生在摘要階段時，
        這個目錄根本還沒被建立；此時若拋出例外，會從 finally 取代掉正在
        傳播的 OperationCancelled，讓使用者的「取消」變成檔案系統錯誤。
        """
        ...


class FrameExtractor(Protocol):
    def extract(self, section_path: str, dest_path: str, width: int) -> None:
        """取片段中間那一格，縮到 width，存成 WebP。失敗時 raise。

        自行建立 dest_path 的上層目錄——use case 只做路徑字串運算。
        """
        ...


class DeckRenderer(Protocol):
    def render(self, deck: Deck, dest_path: str) -> None:
        """把 deck 寫成單一 HTML 檔。自行建立上層目錄。"""
        ...


class ModelCatalog(Protocol):
    def list_models(self) -> list[str]:
        """列出本機可用模型，供 UI 下拉。連不上時回傳空陣列，不 raise。"""
        ...
