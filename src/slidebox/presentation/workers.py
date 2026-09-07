"""QThread worker：把 use case 跑在背景執行緒，透過 signal 回報。"""
from __future__ import annotations

import threading

from PySide6.QtCore import QThread, Signal

from ..domain.entities import Settings
from ..domain.errors import OperationCancelled
from ..usecases.build_deck import BuildDeckUseCase


class BuildDeckWorker(QThread):
    progress = Signal(object, str)   # (fraction: float|None, status: str)
    finished_ok = Signal(object)     # DeckResult
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, usecase: BuildDeckUseCase, url: str, settings: Settings):
        super().__init__()
        self._usecase = usecase
        self._url = url
        self._settings = settings
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            result = self._usecase.execute(
                self._url,
                self._settings,
                lambda frac, status: self.progress.emit(frac, status),
                self._cancel.is_set,
            )
            self.finished_ok.emit(result)
        except OperationCancelled:
            self.cancelled.emit()
        except Exception as e:  # noqa: BLE001 回報給 UI，不讓執行緒崩潰
            self.failed.emit(str(e))
