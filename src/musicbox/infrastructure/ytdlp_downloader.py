"""VideoDownloader 的 yt-dlp 實作。"""
from __future__ import annotations

import os

import yt_dlp

from ..domain.entities import DownloadFormat, DownloadRequest
from ..domain.errors import OperationCancelled
from ..domain.ports import CancelCheck, ProgressCallback
from .ffmpeg import get_ffmpeg_dir

try:
    from yt_dlp.utils import DownloadCancelled as _YtDownloadCancelled
except Exception:  # 舊版 yt-dlp 沒有此類別時的後備
    class _YtDownloadCancelled(Exception):
        pass


class YtDlpDownloader:
    def __init__(self, ffmpeg_dir: str | None = None):
        self._ffmpeg_dir = ffmpeg_dir or get_ffmpeg_dir()

    def download(
        self,
        request: DownloadRequest,
        progress: ProgressCallback,
        is_cancelled: CancelCheck | None = None,
    ) -> str:
        os.makedirs(request.output_dir, exist_ok=True)
        cancelled: CancelCheck = is_cancelled or (lambda: False)
        temp_files: set[str] = set()

        def hook(d: dict) -> None:
            # 先記下當前的暫存檔（通常是 .part），取消時才知道要清哪些
            tmp = d.get("tmpfilename") or d.get("filename")
            if tmp:
                temp_files.add(tmp)
            if cancelled():
                raise _YtDownloadCancelled("使用者取消")
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes") or 0
                frac = (downloaded / total) if total else None
                pct = (d.get("_percent_str") or "").strip()
                progress(frac, f"下載中 {pct}".strip())
            elif status == "finished":
                progress(None, "處理中（轉檔／合併）…")

        opts = self._build_opts(request)
        opts["progress_hooks"] = [hook]

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(request.url, download=True)
                path = self._resolve_path(ydl, info, request.fmt)
        except _YtDownloadCancelled as e:
            self._cleanup_partials(temp_files)
            raise OperationCancelled() from e

        progress(1.0, "完成")
        return path

    @staticmethod
    def _cleanup_partials(temp_files: set[str]) -> None:
        """取消後刪掉殘留的暫存檔（.part 及其對應的 .ytdl 索引檔）。"""
        for tmp in temp_files:
            for candidate in (tmp, tmp + ".part", tmp + ".ytdl", os.path.splitext(tmp)[0] + ".ytdl"):
                try:
                    os.remove(candidate)
                except OSError:
                    pass  # 檔案不存在或無法刪除都忽略

    def _build_opts(self, request: DownloadRequest) -> dict:
        outtmpl = os.path.join(request.output_dir, "%(title)s.%(ext)s")
        opts: dict = {
            "outtmpl": outtmpl,
            "ffmpeg_location": self._ffmpeg_dir,
            "noplaylist": True,          # 只下單一影片
            "windowsfilenames": True,    # 清掉 Windows 非法字元
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        if request.fmt == DownloadFormat.MP3:
            opts.update({
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            })
        else:  # MP4
            opts.update({
                "format": self._mp4_format(request.max_height),
                "merge_output_format": "mp4",
            })
        return opts

    @staticmethod
    def _mp4_format(max_height: int | None) -> str:
        if max_height:
            h = max_height
            return (
                f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
            )
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"

    @staticmethod
    def _resolve_path(ydl, info: dict, fmt: DownloadFormat) -> str:
        requested = info.get("requested_downloads")
        if requested:
            last = requested[-1]
            return last.get("filepath") or ydl.prepare_filename(info)
        base = ydl.prepare_filename(info)
        if fmt == DownloadFormat.MP3:
            return os.path.splitext(base)[0] + ".mp3"
        return os.path.splitext(base)[0] + ".mp4"
