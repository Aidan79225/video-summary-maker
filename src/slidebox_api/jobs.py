"""摘要工作的佇列與狀態機。

純資料與規則：不碰 HTTP、不碰 slidebox、不開執行緒。所以整套排隊與狀態
轉換都能用單元測試釘住，工作執行緒只負責照著它說的做。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_finished(self) -> bool:
        return self in _FINISHED


_FINISHED = frozenset({JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED})

# 預設保留幾筆工作。每筆成品帶著 base64 圖片，這個服務又會連續跑好幾個月，
# 不設上限記憶體只會一路長。
_MAX_JOBS = 50


@dataclass(eq=False)
class Job:
    """一次摘要工作。eq=False：這是 identity 物件，不是值。"""
    id: str
    url: str
    detailed: bool = True
    min_slides: int | None = None
    max_slides: int | None = None
    model: str | None = None

    status: JobStatus = JobStatus.QUEUED
    progress_fraction: float | None = None
    progress_status: str = ""
    error: str = ""
    result: dict | None = None

    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    # 由工作執行緒輪詢；取消不是把狀態改掉就算數，正在跑的那條執行緒必須
    # 自己走到檢查點才停得下來。
    cancel_requested: threading.Event = field(default_factory=threading.Event)


class JobStore:
    """執行緒安全的工作表。

    同一時間只讓一個 job 進入 RUNNING：兩個生成會互搶 Ollama 與 ffmpeg，
    而 GPU 主機只有一張卡。
    """

    def __init__(self, max_jobs: int = _MAX_JOBS):
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._max_jobs = max_jobs

    # --- 查詢 ---

    @property
    def running(self) -> Job | None:
        with self._lock:
            return self._running_unlocked()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def snapshot(self, job_id: str) -> Job | None:
        """鎖內複製一份再回傳。

        Job 是可變的，而 HTTP 層會在鎖外逐一讀它的屬性——沒有快照的話，
        讀到一半工作剛好結束，就會拿到自相矛盾的組合。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            return replace(job) if job is not None else None

    def recent_snapshots(self, limit: int = 20) -> list[Job]:
        with self._lock:
            return [replace(self._jobs[i]) for i in reversed(self._order)][:limit]

    def counts(self) -> tuple[int, int]:
        """(排隊中, 是否有在跑)——給 /health 用。"""
        with self._lock:
            queued = sum(1 for j in self._jobs.values() if j.status == JobStatus.QUEUED)
            return queued, 1 if self._running_unlocked() else 0

    # --- 轉換 ---

    def submit(self, url: str, detailed: bool = True, min_slides: int | None = None,
               max_slides: int | None = None, model: str | None = None) -> Job:
        job = Job(id=uuid.uuid4().hex, url=url, detailed=detailed,
                  min_slides=min_slides, max_slides=max_slides, model=model)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._forget_old_unlocked()
        return job

    def take_next(self) -> Job | None:
        """把最早排隊的工作標成執行中並回傳；沒有可跑的就回 None。"""
        with self._lock:
            if self._running_unlocked() is not None:
                return None
            for job_id in self._order:
                job = self._jobs[job_id]
                if job.status == JobStatus.QUEUED:
                    job.status = JobStatus.RUNNING
                    job.started_at = time.time()
                    return job
            return None

    def progress(self, job_id: str, fraction: float | None, status: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.progress_fraction = fraction
                job.progress_status = status

    def finish(self, job_id: str, result: dict) -> None:
        self._settle(job_id, JobStatus.DONE, result=result)

    def fail(self, job_id: str, error: str) -> None:
        self._settle(job_id, JobStatus.FAILED, error=error)

    def cancelled(self, job_id: str) -> None:
        self._settle(job_id, JobStatus.CANCELLED)

    def cancel(self, job_id: str) -> bool:
        """要求取消；回傳這個 id 存不存在。

        還在排隊的直接標成已取消（它從來沒佔用過 GPU）。已經在跑的只舉旗，
        狀態留給工作執行緒自己收尾——否則執行緒還在跑、GPU 還被佔著，而它
        稍後會把結果寫進一個「已經結束」的工作。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.cancel_requested.set()
            if job.status == JobStatus.QUEUED:
                job.status = JobStatus.CANCELLED
                job.finished_at = time.time()
            return True

    # --- 內部 ---

    def _settle(self, job_id: str, status: JobStatus, result: dict | None = None,
                error: str = "") -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.result = result
            job.error = error
            job.finished_at = time.time()
            if status == JobStatus.DONE:
                job.progress_fraction = 1.0
            job.status = status
            self._forget_old_unlocked()

    def _running_unlocked(self) -> Job | None:
        for job in self._jobs.values():
            if job.status == JobStatus.RUNNING:
                return job
        return None

    def _forget_old_unlocked(self) -> None:
        """超過上限時丟掉最舊的、已經結束的工作。

        還在排隊或執行中的絕不丟——丟掉正在跑的那一筆，呼叫端就再也查不到
        自己的工作，而它其實還在佔著 GPU。
        """
        while len(self._order) > self._max_jobs:
            victim = next(
                (i for i in self._order if self._jobs[i].status.is_finished), None)
            if victim is None:
                return
            self._order.remove(victim)
            self._jobs.pop(victim, None)
