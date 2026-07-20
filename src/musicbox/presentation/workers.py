"""QThread worker：把 use case 跑在背景執行緒，透過 signal 回報進度與結果。"""
from __future__ import annotations

import threading

from PySide6.QtCore import QThread, Signal

from ..domain.entities import DownloadRequest, RenamePlan
from ..domain.errors import OperationCancelled
from ..usecases.download_video import DownloadVideoUseCase
from ..usecases.rename_songs import ApplyRenamePlanUseCase


class DownloadWorker(QThread):
    progress = Signal(object, str)   # (fraction: float|None, status: str)
    finished_ok = Signal(str)        # 最終檔案路徑
    failed = Signal(str)             # 錯誤訊息
    cancelled = Signal()             # 使用者取消

    def __init__(self, usecase: DownloadVideoUseCase, request: DownloadRequest):
        super().__init__()
        self._usecase = usecase
        self._request = request
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            path = self._usecase.execute(
                self._request,
                lambda frac, status: self.progress.emit(frac, status),
                self._cancel.is_set,
            )
            self.finished_ok.emit(path or "")
        except OperationCancelled:
            self.cancelled.emit()
        except Exception as e:  # noqa: BLE001 — 回報給 UI，不讓執行緒崩潰
            self.failed.emit(str(e))


class RenameApplyWorker(QThread):
    progress = Signal(object, str)
    finished_ok = Signal(int)        # 實際改名數
    failed = Signal(str)

    def __init__(self, usecase: ApplyRenamePlanUseCase, plan: RenamePlan):
        super().__init__()
        self._usecase = usecase
        self._plan = plan

    def run(self) -> None:
        try:
            count = self._usecase.execute(
                self._plan,
                lambda frac, status: self.progress.emit(frac, status),
            )
            self.finished_ok.emit(count)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))
