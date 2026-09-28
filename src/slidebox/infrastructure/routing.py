"""依影片來源分派到對應的 adapter。

BuildDeckUseCase 持有一組 gateway。要支援第二、第三個來源，最小又不破壞
分層的作法就是讓 gateway 自己分派——摘要、渲染、設定、取消、佇列全部維持
單一條 pipeline，只有「去哪裡拿字幕／片段／音訊」這件事分岔。

三個分支：立法院 IVOD、臺中市議會、其餘（yt-dlp）。臺中分支是選配——
桌面 app 沒接它時退回 default，不必知道這個來源存在。
"""
from __future__ import annotations

from collections.abc import Sequence

from ..domain.entities import AudioClip, Transcript
from ..domain.ports import (
    AudioGateway,
    CancelCheck,
    ProgressCallback,
    SubtitleGateway,
    VideoSectionGateway,
)
from ..usecases.sources import ivod_id, tccc_clip


def _pick(url: str, default, ivod, tccc):
    if ivod_id(url):
        return ivod
    if tccc is not None and tccc_clip(url):
        return tccc
    return default


def _cleanup_all(gateways, dest_dir: str) -> None:
    """每個分支都清一次。

    cleanup 的簽章裡沒有 url（它在 finally 裡被呼叫，那時可能連網址都
    還沒用到），所以無從判斷該清哪一邊。每個實作都是「刪掉這個目錄，
    不存在也不報錯」，多呼叫幾次是安全的；猜錯邊才會把暫存片段留下來。

    **本 router 要求每個分支的 cleanup 都是無副作用的刪目錄**——它會
    對沒跑過的那幾邊也各呼叫一次。若哪天某個實作的 cleanup 帶了副作用
    （例如殺掉自己的 subprocess），這個假設就不成立了。
    """
    for gateway in gateways:
        if gateway is None:
            continue
        try:
            gateway.cleanup(dest_dir)
        except Exception:  # noqa: BLE001 port 契約說不可 raise；萬一違約，
            pass           # 也不能讓前一個的失敗害後一個不被清理


class BySourceSubtitleGateway:
    def __init__(self, default: SubtitleGateway, ivod: SubtitleGateway,
                 tccc: SubtitleGateway | None = None):
        self._default = default
        self._ivod = ivod
        self._tccc = tccc

    def fetch(self, url: str, langs: Sequence[str]) -> Transcript:
        return _pick(url, self._default, self._ivod, self._tccc).fetch(url, langs)


class BySourceSectionGateway:
    def __init__(self, default: VideoSectionGateway, ivod: VideoSectionGateway,
                 tccc: VideoSectionGateway | None = None):
        self._default = default
        self._ivod = ivod
        self._tccc = tccc

    def download_sections(
        self,
        url: str,
        timestamps: Sequence[float],
        max_height: int | None,
        dest_dir: str,
        progress: ProgressCallback,
        is_cancelled: CancelCheck,
    ) -> list[str | None]:
        chosen = _pick(url, self._default, self._ivod, self._tccc)
        return chosen.download_sections(
            url, timestamps, max_height, dest_dir, progress, is_cancelled)

    def cleanup(self, dest_dir: str) -> None:
        _cleanup_all((self._default, self._ivod, self._tccc), dest_dir)


class BySourceAudioGateway:
    """音訊只有兩條路：臺中用 ffmpeg 抓 HLS，其餘交給 yt-dlp。IVOD 不會走到
    這裡（它有逐字稿，不會觸發語音備援）。"""

    def __init__(self, default: AudioGateway, tccc: AudioGateway | None = None):
        self._default = default
        self._tccc = tccc

    def download_audio(
        self,
        url: str,
        dest_dir: str,
        progress: ProgressCallback,
        is_cancelled: CancelCheck,
    ) -> AudioClip:
        chosen = self._tccc if (self._tccc is not None and tccc_clip(url)) else self._default
        return chosen.download_audio(url, dest_dir, progress, is_cancelled)

    def cleanup(self, dest_dir: str) -> None:
        _cleanup_all((self._default, self._tccc), dest_dir)
