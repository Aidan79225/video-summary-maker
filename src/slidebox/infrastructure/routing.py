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

        **本 router 要求每個分支的 cleanup 都是無副作用的刪目錄**——它會
        對沒跑過的那一邊也呼叫一次。若哪天某個實作的 cleanup 帶了副作用
        （例如殺掉自己的 subprocess），這個假設就不成立了。
        """
        for gateway in (self._default, self._ivod):
            try:
                gateway.cleanup(dest_dir)
            except Exception:  # noqa: BLE001 port 契約說不可 raise；萬一違約，
                pass           # 也不能讓前一個的失敗害後一個不被清理
