"""FastAPI 應用：摘要工作的提交、查詢與取消。

只做 HTTP。排隊規則在 jobs.py、執行在 runner.py、成品格式在 payload.py，
三者都不知道 FastAPI 的存在。

所有相依都從外面傳進來，這個模組不讀環境變數、也不 new 任何東西——組裝
是 serve_api.py 的事。
"""
from __future__ import annotations

import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date as date_type
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .jobs import Job, JobKind, JobStore
from .runner import JobWorker

MAX_SLIDES = 50
MAX_TRANSCRIPT = 200_000
ALLOWED_SCHEMES = ("http", "https")


class JobRequest(BaseModel):
    url: str = Field(min_length=4)
    # 新聞用途一律要詳細內容：條列在網頁上讀起來太單薄
    detailed: bool = True
    min_slides: int | None = Field(default=None, ge=1, le=MAX_SLIDES)
    max_slides: int | None = Field(default=None, ge=1, le=MAX_SLIDES)
    model: str | None = None


class FactCheckRequest(BaseModel):
    # 只用來識別與回溯，不會拿去下載——但一樣限定 http(s)
    source_url: str = Field(min_length=4)
    speaker: str = Field(min_length=1, max_length=100)
    date: date_type
    meeting: str = Field(default="", max_length=300)
    transcript_text: str = Field(min_length=1, max_length=MAX_TRANSCRIPT)


class JobView(BaseModel):
    id: str
    url: str
    kind: str = JobKind.DECK.value
    status: str
    detailed: bool
    progress_fraction: float | None = None
    progress_status: str = ""
    error: str = ""
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    result: dict | None = None


class HealthView(BaseModel):
    ok: bool
    model: str
    ollama_host: str
    ollama_reachable: bool
    queued: int
    busy: bool


def _view(job: Job, include_result: bool = True) -> JobView:
    return JobView(
        id=job.id, url=job.url, kind=job.kind, status=job.status, detailed=job.detailed,
        progress_fraction=job.progress_fraction, progress_status=job.progress_status,
        error=job.error, created_at=job.created_at, started_at=job.started_at,
        finished_at=job.finished_at,
        # 清單不帶 result：每篇成品有幾百 KB 的 base64 圖片
        result=job.result if include_result else None,
    )


def create_app(
    store: JobStore,
    worker: JobWorker,
    api_key: str,
    model: str,
    ollama_host: str,
    probe_ollama: Callable[[], bool],
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker.start()
        try:
            yield
        finally:
            worker.stop()

    app = FastAPI(title="SlideBox 摘要 API", version="1.0", lifespan=lifespan)

    def require_key(x_api_key: str = Header(default="")) -> None:
        # 沒設金鑰就不驗：區網自用時多一道設定只會擋住自己。一旦設了就強制。
        if not api_key:
            return
        # compare_digest 而不是 ==：後者會在第一個不同的位元組就返回，
        # 回應時間會洩漏金鑰的前綴。比 bytes 而不是 str：compare_digest 對
        # 非 ASCII 的 str 會丟 TypeError，那會變成 500 而不是 401。
        if not secrets.compare_digest(x_api_key.encode(), api_key.encode()):
            raise HTTPException(status_code=401, detail="X-API-Key 不正確")

    @app.get("/health", response_model=HealthView)
    def health() -> HealthView:
        queued, running = store.counts()
        return HealthView(
            ok=True, model=model, ollama_host=ollama_host,
            ollama_reachable=probe_ollama(), queued=queued, busy=bool(running),
        )

    @app.post("/jobs", status_code=202, dependencies=[Depends(require_key)])
    def submit(request: JobRequest) -> JobView:
        _validate(request)
        return _view(store.submit(
            request.url, detailed=request.detailed, min_slides=request.min_slides,
            max_slides=request.max_slides, model=request.model))

    @app.post("/factchecks", status_code=202, dependencies=[Depends(require_key)])
    def submit_factcheck(request: FactCheckRequest) -> JobView:
        if urlparse(request.source_url).scheme not in ALLOWED_SCHEMES:
            raise HTTPException(status_code=422, detail="網址必須是 http 或 https")
        return _view(store.submit(request.source_url, kind=JobKind.FACTCHECK, params={
            "speaker": request.speaker,
            "date": request.date.isoformat(),
            "meeting": request.meeting,
            "transcript_text": request.transcript_text,
        }))

    @app.get("/jobs", dependencies=[Depends(require_key)])
    def recent(limit: int = 20) -> list[JobView]:
        return [_view(job, include_result=False)
                for job in store.recent_snapshots(limit)]

    @app.get("/jobs/{job_id}", dependencies=[Depends(require_key)])
    def detail(job_id: str) -> JobView:
        return _view(_require_job(job_id))

    @app.delete("/jobs/{job_id}", dependencies=[Depends(require_key)])
    def cancel(job_id: str) -> JobView:
        if not store.cancel(job_id):
            raise HTTPException(status_code=404, detail="沒有這個工作")
        return _view(_require_job(job_id), include_result=False)

    def _require_job(job_id: str) -> Job:
        job = store.snapshot(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="沒有這個工作")
        return job

    return app


def _validate(request: JobRequest) -> None:
    if request.min_slides and request.max_slides \
            and request.min_slides > request.max_slides:
        raise HTTPException(status_code=422, detail="min_slides 不可大於 max_slides")
    # 這個網址會被交給 yt-dlp。限定 scheme 才不會讓 file:// 之類的東西
    # 變成讀取 GPU 主機本機檔案的管道。
    if urlparse(request.url).scheme not in ALLOWED_SCHEMES:
        raise HTTPException(status_code=422, detail="網址必須是 http 或 https")
