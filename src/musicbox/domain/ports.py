"""領域介面（port）：由 infrastructure 層實作，usecases 依賴這些抽象。"""
from __future__ import annotations

from typing import Callable, Protocol

from .entities import DownloadRequest, Settings

# 進度回報：fraction 為 0..1 的完成比例，None 表示不確定（顯示忙碌狀態）；status 為文字說明。
ProgressCallback = Callable[[float | None, str], None]

# 取消判斷：回傳 True 表示使用者已要求取消。
CancelCheck = Callable[[], bool]


class VideoDownloader(Protocol):
    def download(
        self,
        request: DownloadRequest,
        progress: ProgressCallback,
        is_cancelled: CancelCheck | None = None,
    ) -> str:
        """下載影片／音樂，回傳最終檔案路徑。失敗時 raise 例外。

        若 is_cancelled() 在下載途中回傳 True，應中止並 raise OperationCancelled。
        """
        ...


class FileSystemGateway(Protocol):
    def list_files(self, folder: str) -> list[str]:
        """列出資料夾裡的檔案名稱（不含子資料夾）。"""
        ...

    def rename(self, folder: str, src_name: str, dst_name: str) -> None:
        """把 folder 裡的 src_name 改名為 dst_name。"""
        ...


class SettingsRepository(Protocol):
    def load(self, default: Settings) -> Settings:
        """讀取設定；檔案不存在或損毀時回傳 default。"""
        ...

    def save(self, settings: Settings) -> None:
        """寫入設定。"""
        ...
