"""依影片來源分派到對應的 adapter。

BuildDeckUseCase 持有一組 gateway。要支援第二個來源，最小又不破壞分層的
作法就是讓 gateway 自己分派——摘要、渲染、設定、取消、佇列全部維持單一條
pipeline，只有「去哪裡拿字幕／片段」這件事分岔。
"""
from __future__ import annotations

from collections.abc import Sequence

from ..domain.entities import Transcript
from ..domain.ports import (
    CancelCheck,
    ProgressCallback,
    SubtitleGateway,
    VideoSectionGateway,
)
from ..usecases.sources import ivod_id


class BySourceSubtitleGateway:
    def __init__(self, default: SubtitleGateway, ivod: SubtitleGateway):
        self._default = default
        self._ivod = ivod

    def fetch(self, url: str, langs: Sequence[str]) -> Transcript:
        chosen = self._ivod if ivod_id(url) else self._default
        return chosen.fetch(url, langs)


class BySourceSectionGateway:
    def __init__(self, default: VideoSectionGateway, ivod: VideoSectionGateway):
        self._default = default
        self._ivod = ivod

    def download_sections(
        self,
        url: str,
        timestamps: Sequence[float],
        max_height: int | None,
        dest_dir: str,
        progress: ProgressCallback,
        is_cancelled: CancelCheck,
    ) -> list[str | None]:
        chosen = self._ivod if ivod_id(url) else self._default
        return chosen.download_sections(
            url, timestamps, max_height, dest_dir, progress, is_cancelled)

    def cleanup(self, dest_dir: str) -> None:
        """兩邊都清。

        cleanup 的簽章裡沒有 url（它在 finally 裡被呼叫，那時可能連網址都
        還沒用到），所以無從判斷該清哪一邊。兩個實作都是「刪掉這個目錄，
        不存在也不報錯」，呼叫兩次是安全的；猜錯邊才會把暫存片段留下來。
        """
        self._default.cleanup(dest_dir)
        self._ivod.cleanup(dest_dir)
