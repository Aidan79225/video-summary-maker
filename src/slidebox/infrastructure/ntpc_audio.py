"""新北市議會影片的音訊 gateway：接上 NtpcClient 的 HlsAudioGateway。

新北的會議紀錄要兩個多月後才上線、而且沒有時間戳，走語音辨識。跟臺中不同的是
串流在 Wowza 上，網址加 `wowzaaudioonly=true` 就只給音訊：兩小時的片段約 58 MB，
不必把整支 SD 影片（約 1.6 Mbps）拉下來再丟掉影像。

實測（2026-09-29）：純音訊串流是 AAC LC 44.1 kHz 立體聲約 63 kbps，ffmpeg 用預設的
User-Agent 就抓得到；前 20 秒 1 秒抓完（每段約 10 秒，一個請求約 0.25 秒），
兩小時約 720 段估計三分鐘上下，逾時沿用通用的 15 分鐘。
"""
from __future__ import annotations

import subprocess

from ..domain.errors import NoSubtitlesAvailable
from ..usecases.sources import ntpc_clip
from .hls_audio import DEFAULT_TIMEOUT_SECONDS, HlsAudioGateway, Resolver
from .ntpc_api import NtpcClient


def _ntpc_source(client: NtpcClient) -> Resolver:
    def resolve(url: str) -> tuple[str, str, str]:
        ref = ntpc_clip(url)
        if ref is None:
            # 走到這裡表示路由接錯了；不碰網路，直接說清楚。
            raise NoSubtitlesAvailable(f"這不是新北市議會的影片網址（{url[:60]}）")
        record = client.record(ref)
        # video_id 用 GUID：新聞服務的文章主鍵是檔案 key，但那只有清單頁看得到，
        # 而 Pi 不檢查 GPU 回傳的這個值。
        return f"ntpc-{ref.asset_id}", record.title, client.audio_url(ref)
    return resolve


class NtpcAudioGateway(HlsAudioGateway):
    def __init__(self, client: NtpcClient, runner=subprocess.run,
                 ffmpeg_exe: str | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        super().__init__(_ntpc_source(client), runner, ffmpeg_exe, timeout)
        # 留著參照，composition 的測試才看得到接上的是哪個 client
        self._client = client
