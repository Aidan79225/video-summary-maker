"""VideoSectionGateway 的 yt-dlp 實作：只下載需要的幾秒片段。"""
from __future__ import annotations

import os
import shutil
from collections.abc import Sequence

import yt_dlp
from yt_dlp.utils import download_range_func

from ..domain.errors import OperationCancelled
from ..domain.ports import CancelCheck, ProgressCallback
from .ffmpeg import get_ffmpeg_dir


class YtDlpSectionGateway:
    """每個時間點跑一次獨立的 yt-dlp 呼叫。

    選擇一點一呼叫而非單次多區間，是為了失敗隔離：單點失敗只讓該頁沒圖，
    符合「部分截圖失敗仍然出片」的策略。代價是重複的 info extraction。
    """

    def __init__(self, clip_seconds: float = 4.0):
        self._clip_seconds = clip_seconds
        self._ffmpeg_dir = get_ffmpeg_dir()
        # yt-dlp 的 download_ranges 前置檢查（FFmpegFD.available()）只查 PATH、
        # 完全無視 ffmpeg_location（其原始碼留有 TODO: Fix path for ffmpeg），
        # 所以 PATH 上找不到 ffmpeg 時整個分段下載會直接中止。
        #
        # 但 PATH 只用來過這個布林檢查——真正執行的仍是 ffmpeg_location 指定的
        # 內建版本。因此使用者已經有 ffmpeg 時就不必動環境變數，避免永久遮蔽
        # 他自己的安裝。
        if shutil.which("ffmpeg") is None:
            os.environ["PATH"] = self._ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

    def download_sections(
        self,
        url: str,
        timestamps: Sequence[float],
        max_height: int | None,
        dest_dir: str,
        progress: ProgressCallback,
        is_cancelled: CancelCheck,
    ) -> list[str | None]:
        os.makedirs(dest_dir, exist_ok=True)
        total = max(1, len(timestamps))
        results: list[str | None] = []

        for i, start in enumerate(timestamps):
            if is_cancelled():
                raise OperationCancelled()
            progress(i / total, f"下載片段 {i + 1}/{total}")
            results.append(self._one(url, start, max_height, dest_dir, i))

        progress(1.0, f"片段下載完成（{sum(r is not None for r in results)}/{total}）")
        return results

    def cleanup(self, dest_dir: str) -> None:
        shutil.rmtree(dest_dir, ignore_errors=True)

    def _one(self, url: str, start: float, max_height: int | None,
             dest_dir: str, index: int) -> str | None:
        stem = os.path.join(dest_dir, f"clip{index:03d}")
        opts = {
            # 只要視訊，不要音訊也不要合併——較快且檔案較小
            "format": (f"bestvideo[height<={max_height}]/bestvideo/best"
                       if max_height else "bestvideo/best"),
            "download_ranges": download_range_func(
                None, [(start, start + self._clip_seconds)]
            ),
            # 強制對齊關鍵影格要重編碼、很慢；切點落在前一個關鍵影格
            # 對「取代表畫面」毫無影響。
            "force_keyframes_at_cuts": False,
            "outtmpl": stem + ".%(ext)s",
            "ffmpeg_location": self._ffmpeg_dir,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception:  # noqa: BLE001 單點失敗只讓該頁沒圖
            return None
        for name in sorted(os.listdir(dest_dir)):
            if name.startswith(os.path.basename(stem)):
                return os.path.join(dest_dir, name)
        return None
