"""工作執行緒：把佇列裡的工作真的跑起來。

執行內容由外面注入，所以這個迴圈本身能用假的執行函式測試，不需要
Ollama、不需要網路。
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace

from slidebox.domain.entities import Settings
from slidebox.domain.errors import OperationCancelled
from slidebox.usecases.build_deck import BuildDeckUseCase
from slidebox.usecases.queue import video_key
from slidebox.usecases.sources import ivod_id

from .jobs import Job, JobStore
from .payload import deck_payload

ProgressCallback = Callable[[float | None, str], None]
CancelCheck = Callable[[], bool]
Executor = Callable[[Job, ProgressCallback, CancelCheck], dict]

DEFAULT_POLL_SECONDS = 0.2


class JobWorker:
    """單一背景執行緒，一次跑一個工作。

    刻意只有一條：兩個生成會互搶 Ollama 與 ffmpeg，而 GPU 主機只有一張卡。
    """

    def __init__(self, store: JobStore, execute: Executor,
                 poll_seconds: float = DEFAULT_POLL_SECONDS):
        self._store = store
        self._execute = execute
        self._poll = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        # clear 是必要的：stop 之後再 start，沒清旗標的話迴圈會立刻退出，
        # 而且是靜悄悄地什麼都不處理。
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="slidebox-worker",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        # 一併要求正在跑的工作停下來，否則關機時會把跑到一半的 pipeline
        # 丟著讓行程死掉，暫存檔也不會被清。
        running = self._store.running
        if running is not None:
            running.cancel_requested.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def run_once(self) -> bool:
        """跑一個工作；沒有可跑的回 False。測試直接用這個，不必開執行緒。"""
        job = self._store.take_next()
        if job is None:
            return False
        if job.cancel_requested.is_set():
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


def video_id_of(url: str) -> str:
    """新聞服務用來當主鍵的識別碼。

    IVOD 給裸的數字 id（Django 那邊的 ivod_id 就是它），其他來源退回佇列
    用的那把鑰匙，至少保證同一支影片得到同一個值。
    """
    return ivod_id(url) or video_key(url)


class SlideboxExecutor:
    """用 slidebox 跑完整條 pipeline。

    use case 從外面傳進來，而且整個行程只建一次：它持有語音辨識器的快取
    （模型一旦載入就留在記憶體與 VRAM 裡），每個工作重建一次等於把那份
    快取丟掉。
    """

    def __init__(self, usecase: BuildDeckUseCase, settings: Settings):
        self._usecase = usecase
        self._settings = settings
        # 基準值另外留一份：每個工作都要從它重設，否則有人送過一次
        # min_slides=3 之後，後面每一篇都會是 3 頁——而送那一次的人早就
        # 離開了。
        self._base = replace(settings)

    def __call__(self, job: Job, progress: ProgressCallback,
                 is_cancelled: CancelCheck) -> dict:
        settings = self._apply(job)
        result = self._usecase.execute(job.url, settings, progress, is_cancelled)
        return deck_payload(result.deck, video_id_of(job.url))

    def _apply(self, job: Job) -> Settings:
        """就地改那個共用的 Settings 實例，不複製。

        composition 組出來的 summarizer 持有它的參照、每次生成都重讀——
        複製一份的話，工作自己指定的模型不會生效。
        """
        overrides = {
            "detailed": job.detailed,
            "min_slides": job.min_slides,
            "max_slides": job.max_slides,
            "model": job.model,
        }
        for name, value in overrides.items():
            setattr(self._settings, name,
                    value if value is not None else getattr(self._base, name))
        return self._settings
