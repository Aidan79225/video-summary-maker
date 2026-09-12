"""工作執行緒：把佇列裡的工作真的跑起來。

執行內容由外面注入（`execute`），所以這個迴圈本身能用假的執行函式測試，
不需要 Ollama、不需要網路。
"""
from __future__ import annotations

import os
import threading
from collections.abc import Callable

from slidebox.composition import build_usecase
from slidebox.domain.entities import Settings
from slidebox.domain.errors import OperationCancelled
from slidebox.usecases.queue import video_key
from slidebox.usecases.sources import ivod_id

from .jobs import Job, JobStore
from .payload import deck_payload

# 一個工作要幾分鐘，輪詢間隔不必短
_POLL_SECONDS = 0.2

Executor = Callable[[Job, Callable[[float | None, str], None], Callable[[], bool]], dict]


class JobWorker:
    """單一背景執行緒，一次跑一個工作。

    刻意只有一條：兩個生成會互搶 Ollama 與 ffmpeg，而 GPU 主機只有一張卡。
    """

    def __init__(self, store: JobStore, execute: Executor,
                 poll_seconds: float = _POLL_SECONDS):
        self._store = store
        self._execute = execute
        self._poll = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="slidebox-worker",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def run_once(self) -> bool:
        """跑一個工作；沒有可跑的回 False。測試直接用這個，不必開執行緒。"""
        job = self._store.take_next()
        if job is None:
            return False
        if job.cancel_requested.is_set():
            # 排隊期間被取消了，但已經被 take_next 標成執行中
            self._store.cancelled(job.id)
            return True

        def progress(fraction: float | None, status: str) -> None:
            self._store.progress(job.id, fraction, status)

        try:
            result = self._execute(job, progress, job.cancel_requested.is_set)
        except OperationCancelled:
            self._store.cancelled(job.id)
        except Exception as e:  # noqa: BLE001 任何失敗都要變成這個工作的錯誤
            self._store.fail(job.id, f"{type(e).__name__}: {e}"[:800])
        else:
            self._store.finish(job.id, result)
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                self._stop.wait(self._poll)


def _video_id(url: str) -> str:
    """新聞服務用來當主鍵的識別碼。

    IVOD 給裸的數字 id（Django 那邊的 `ivod_id` 就是它），其他來源退回
    佇列用的那把鑰匙，至少保證同一支影片得到同一個值。
    """
    return ivod_id(url) or video_key(url)


def build_executor(base_settings: Settings) -> Executor:
    """真正的執行函式：跑完整條 slidebox pipeline。

    每個工作複製一份設定再套用它自己的參數——`Settings` 是可變物件，
    直接改會讓同時進來的請求互相污染。
    """
    def execute(job: Job, progress, is_cancelled) -> dict:
        from dataclasses import replace

        settings = replace(
            base_settings,
            detailed=job.detailed,
            min_slides=job.min_slides or base_settings.min_slides,
            max_slides=job.max_slides or base_settings.max_slides,
            model=job.model or base_settings.model,
        )
        usecase = build_usecase(settings)
        result = usecase.execute(job.url, settings, progress, is_cancelled)
        payload = deck_payload(result.deck, _video_id(job.url))
        payload["html_path"] = os.path.abspath(result.html_path)
        return payload

    return execute
