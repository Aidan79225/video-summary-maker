"""測試用的 in-memory 假實作（不碰真實磁碟／網路）。"""
from __future__ import annotations

from musicbox.domain.entities import DownloadRequest
from musicbox.domain.errors import OperationCancelled
from musicbox.domain.ports import CancelCheck, ProgressCallback


class FakeGateway:
    """記憶體版檔案系統；rename 會模擬真實的覆蓋衝突（dst 已存在則報錯）。"""

    def __init__(self, files: dict[str, list[str]]):
        self._files = {folder: list(names) for folder, names in files.items()}

    def list_files(self, folder: str) -> list[str]:
        return list(self._files.get(folder, []))

    def rename(self, folder: str, src_name: str, dst_name: str) -> None:
        names = self._files[folder]
        if src_name not in names:
            raise FileNotFoundError(src_name)
        if dst_name in names:
            raise FileExistsError(dst_name)  # 天真的直接改名會撞到這裡
        names[names.index(src_name)] = dst_name

    def names(self, folder: str) -> set[str]:
        return set(self._files.get(folder, []))


class FakeDownloader:
    """記錄呼叫參數；可設定成尊重取消旗標。"""

    def __init__(self, respect_cancel: bool = False):
        self.respect_cancel = respect_cancel
        self.calls: list[DownloadRequest] = []
        self.progress_events: list[tuple] = []

    def download(
        self,
        request: DownloadRequest,
        progress: ProgressCallback,
        is_cancelled: CancelCheck | None = None,
    ) -> str:
        self.calls.append(request)
        progress(0.0, "開始")
        if self.respect_cancel and is_cancelled and is_cancelled():
            raise OperationCancelled()
        progress(1.0, "完成")
        self.progress_events = [(0.0, "開始"), (1.0, "完成")]
        return f"{request.output_dir}/video.{request.fmt.value}"
