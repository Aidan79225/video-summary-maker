"""AudioGateway 的 yt-dlp 實作：下載純音訊給語音辨識用。

不做任何後製，所以不需要 ffmpeg——faster-whisper 透過 PyAV 直接解碼 webm／m4a。
"""
from __future__ import annotations

import os
import shutil

import yt_dlp

from ..domain.entities import AudioClip
from ..domain.errors import NoSubtitlesAvailable, OperationCancelled
from ..domain.ports import CancelCheck, ProgressCallback

try:
    from yt_dlp.utils import DownloadCancelled as _YtDownloadCancelled
except Exception:  # 舊版 yt-dlp 沒有此類別時的後備
    class _YtDownloadCancelled(Exception):
        pass


class YtDlpAudioGateway:
    def download_audio(
        self,
        url: str,
        dest_dir: str,
        progress: ProgressCallback,
        is_cancelled: CancelCheck,
    ) -> AudioClip:
        os.makedirs(dest_dir, exist_ok=True)

        def hook(d: dict) -> None:
            # 比照 musicbox 的下載器：在進度 hook 裡檢查取消，下載中途即可中止
            if is_cancelled():
                raise _YtDownloadCancelled("使用者取消")
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes") or 0
                progress(done / total if total else None, "下載音訊…")

        opts = {
            "format": "bestaudio/best",
            # 檔名帶影片 id：yt-dlp 預設不覆寫既有檔案，固定檔名時上一次中斷
            # 留下的舊檔會被直接拿來用，下一支影片就轉錄到別支影片的聲音。
            "outtmpl": os.path.join(dest_dir, "%(id)s.%(ext)s"),
            "progress_hooks": [hook],
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True) or {}
        except _YtDownloadCancelled as e:
            raise OperationCancelled() from e
        except yt_dlp.utils.DownloadError as e:
            cause = str(e).removeprefix("ERROR: ").strip()[:160]
            raise NoSubtitlesAvailable(f"這部影片沒有字幕，音訊也下載失敗：{cause}") from e

        path = self._downloaded_path(info)
        if path is None:
            raise NoSubtitlesAvailable("這部影片沒有字幕，音訊下載後找不到檔案")
        return AudioClip(
            path=path,
            video_id=info.get("id") or "video",
            title=info.get("title") or "未命名影片",
            duration=float(info.get("duration") or 0.0),
        )

    def cleanup(self, dest_dir: str) -> None:
        shutil.rmtree(dest_dir, ignore_errors=True)

    @staticmethod
    def _downloaded_path(info: dict) -> str | None:
        """yt-dlp 在 requested_downloads 回報實際寫出的檔案，副檔名由選到的格式決定。"""
        for item in reversed(info.get("requested_downloads") or []):
            path = item.get("filepath")
            if path and os.path.exists(path):
                return path
        return None
