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
        # yt-dlp 在啟用 download_ranges（片段下載）時，會先用「ffmpeg 是否在
        # PATH 上」做前置檢查，這個檢查不會讀取 ffmpeg_location 選項（yt-dlp
        # 已知限制，見其原始碼 downloader/external.py 的 TODO 註解）。若使用者
        # 電腦沒有另外安裝 ffmpeg，僅設定 ffmpeg_location 並不夠，必須把內建
        # ffmpeg 所在資料夾加進 PATH，否則片段下載一律回報「ffmpeg 未安裝」。
        if self._ffmpeg_dir not in os.environ.get("PATH", "").split(os.pathsep):
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
