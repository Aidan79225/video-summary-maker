"""臺中市議會片段的音訊 gateway：接上 TcccClient 的 HlsAudioGateway。

臺中沒有逐字稿可抓（議事錄沒有時間戳），走語音辨識；抓音訊的 ffmpeg 流程與
新北共用，在 hls_audio。`NoSubtitlesGateway` 原本定義在這裡，搬走後保留
re-export，既有的 import 不必改。
"""
from __future__ import annotations

import subprocess

from ..domain.errors import NoSubtitlesAvailable
from ..usecases.sources import tccc_clip
from .hls_audio import DEFAULT_TIMEOUT_SECONDS, HlsAudioGateway, NoSubtitlesGateway, Resolver
from .tccc_api import TcccClient

__all__ = ["NoSubtitlesGateway", "TcccAudioGateway"]


def _tccc_source(client: TcccClient) -> Resolver:
    def resolve(url: str) -> tuple[str, str, str]:
        ref = tccc_clip(url)
        if ref is None:
            # 走到這裡表示路由接錯了；不碰網路，直接說清楚。
            raise NoSubtitlesAvailable(f"這不是臺中市議會的片段網址（{url[:60]}）")
        record = client.record(ref)
        return f"tccc-{ref.ano}", record.title, client.video_url(ref)
    return resolve


class TcccAudioGateway(HlsAudioGateway):
    def __init__(self, client: TcccClient, runner=subprocess.run,
                 ffmpeg_exe: str | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        super().__init__(_tccc_source(client), runner, ffmpeg_exe, timeout)
        # 留著參照，composition 的測試才看得到接上的是哪個 client
        self._client = client
