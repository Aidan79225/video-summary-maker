"""下載影片／音樂的 use case。"""
from __future__ import annotations

from ..domain.entities import DownloadRequest
from ..domain.ports import CancelCheck, ProgressCallback, VideoDownloader


class DownloadVideoUseCase:
    def __init__(self, downloader: VideoDownloader):
        self._downloader = downloader

    def execute(
        self,
        request: DownloadRequest,
        progress: ProgressCallback | None = None,
        is_cancelled: CancelCheck | None = None,
    ) -> str:
        cb: ProgressCallback = progress or (lambda frac, status: None)
        cancel: CancelCheck = is_cancelled or (lambda: False)
        return self._downloader.download(request, cb, cancel)
