"""FastAPI 應用：摘要工作的提交、查詢與取消。

這一層只做 HTTP——排隊規則在 jobs.py，執行在 runner.py，成品格式在
payload.py，三者都不知道 FastAPI 的存在。
"""
from __future__ import annotations

import os
import urllib.request
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from slidebox.domain.entities import Settings

from .jobs import Job, JobStore
from .runner import JobWorker, build_executor

DEFAULT_OUTPUT_DIR = os.environ.get(
    "SLIDEBOX_API_OUTPUT_DIR",
    os.path.join(os.path.expanduser("~"), "slidebox_api_output"),
)


class JobRequest(BaseModel):
    url: str = Field(min_length=4)
    # 新聞用途一律要詳細內容：條列在網頁上讀起來太單薄
    detailed: bool = True
    min_slides: int | None = Field(default=None, ge=1, le=50)
    max_slides: int | None = Field(default=None, ge=1, le=50)
    model: str | None = None


class JobView(BaseModel):
    id: str
    url: str
    status: str
    detailed: bool
    progress_fraction: float | None = None
    progress_status: str = ""
    error: str = ""
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    result: dict | None = None


def _view(job: Job, include_result: bool = True) -> JobView:
    return JobView(
        id=job.id, url=job.url, status=job.status, detailed=job.detailed,
        progress_fraction=job.progress_fraction, progress_status=job.progress_status,
        error=job.error, created_at=job.created_at, started_at=job.started_at,
        finished_at=job.finished_at,
        # 清單不帶 result：每篇成品有幾百 KB 的 base64 圖片
        result=job.result if include_result else None,
    )


def _default_settings() -> Settings:
    """API 的預設設定。可用環境變數覆蓋，讓部署不必改程式碼。"""
    settings = Settings(output_dir=DEFAULT_OUTPUT_DIR)
    settings.model = os.environ.get("SLIDEBOX_MODEL", settings.model)
    settings.ollama_host = os.environ.get("SLIDEBOX_OLLAMA_HOST", settings.ollama_host)
    settings.detailed = True
    return settings


def _ollama_reachable(host: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{host.rstrip('/')}/api/tags", timeout=timeout):
            return True
    except Exception:  # noqa: BLE001 健康檢查失敗就是 False，不要冒泡
        return False


def create_app(store: JobStore | None = None, worker: JobWorker | None = None,
               api_key: str | None = None, settings: Settings | None = None) -> FastAPI:
    """組裝 app。所有相依都可注入，測試才不需要 Ollama 與網路。"""
    settings = settings or _default_settings()
    store = store or JobStore()
    worker = worker or JobWorker(store, build_executor(settings))
    # 沒設金鑰就不驗：區網自用時多一道設定只會擋住自己。一旦設了就強制。
    expected_key = api_key if api_key is not None else os.environ.get("SLIDEBOX_API_KEY", "")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker.start()
        try:
            yield
        finally:
            worker.stop()

    app = FastAPI(title="SlideBox 摘要 API", version="1.0", lifespan=lifespan)

    def require_key(x_api_key: str = Header(default="")) -> None:
        if expected_key and x_api_key != expected_key:
            raise HTTPException(status_code=401, detail="X-API-Key 不正確")

    @app.get("/health")
    def health() -> dict:
        queued, running = store.counts()
        return {
            "ok": True,
            "model": settings.model,
            "ollama_host": settings.ollama_host,
            "ollama_reachable": _ollama_reachable(settings.ollama_host),
            "queued": queued,
            "busy": bool(running),
        }

    @app.post("/jobs", status_code=202, dependencies=[Depends(require_key)])
    def submit(request: JobRequest) -> JobView:
        if request.min_slides and request.max_slides and \
                request.min_slides > request.max_slides:
            raise HTTPException(status_code=422, detail="min_slides 不可大於 max_slides")
        job = store.submit(request.url, detailed=request.detailed,
                           min_slides=request.min_slides, max_slides=request.max_slides,
                           model=request.model)
        return _view(job)

    @app.get("/jobs", dependencies=[Depends(require_key)])
    def recent(limit: int = 20) -> list[JobView]:
        return [_view(j, include_result=False) for j in store.recent(limit)]

    @app.get("/jobs/{job_id}", dependencies=[Depends(require_key)])
    def detail(job_id: str) -> JobView:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="沒有這個工作")
        return _view(job)

    @app.delete("/jobs/{job_id}", dependencies=[Depends(require_key)])
    def cancel(job_id: str) -> JobView:
        if not store.cancel(job_id):
            raise HTTPException(status_code=404, detail="沒有這個工作")
        job = store.get(job_id)
        assert job is not None
        return _view(job, include_result=False)

    app.state.store = store
    app.state.worker = worker
    return app
