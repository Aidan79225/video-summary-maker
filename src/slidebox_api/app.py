"""FastAPI 應用：摘要、分類與追問判斷工作的提交、查詢與取消。

只做 HTTP。排隊規則在 jobs.py、執行在 runner.py、成品格式在 payload.py，
三者都不知道 FastAPI 的存在。

所有相依都從外面傳進來，這個模組不讀環境變數、也不 new 任何東西——組裝
是 serve_api.py 的事。
"""
from __future__ import annotations

import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from slidebox.domain.entities import FollowUpPair, TopicLabel

from .jobs import Job, JobKind, JobStore
from .runner import JobWorker

MAX_SLIDES = 50
ALLOWED_SCHEMES = ("http", "https")
# 分類的輸入是摘要卡的一句話加各段小標，幾百字就夠。上限擋的是誤把整份逐字稿
# 送進來的請求：準確率是在短輸入上量的，長輸入分出來的結果沒有人評估過。
MAX_TOPIC_TEXT = 4000
# 少於兩個就沒有東西可選；二十個是替新聞服務的十二個領域留的餘裕，同時不讓
# 一份亂塞的清單把提示詞灌爆。
MIN_TOPIC_LABELS = 2
MAX_TOPIC_LABELS = 20
# 追問判斷的輸入。逐字稿片段的上限是設計定的：新聞服務挑的是一段視窗，不是整份
# 逐字稿，長輸入判出來的結果沒有人評估過。要求與回應在摘要卡裡只有二三十字、
# 摘要卡本身幾百字；這些上限只擋誤送整份逐字稿的請求，不會擋到正常的資料。
MAX_FOLLOWUP_EXCERPT = 1500
MAX_FOLLOWUP_REQUEST = 500
MAX_FOLLOWUP_RESPONSE = 500
MAX_FOLLOWUP_CARD = 4000


class TopicLabelIn(BaseModel):
    """分類工作的一個可選領域。長度上限同樣是為了不讓提示詞被灌爆。"""
    key: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=200)


class JobRequest(BaseModel):
    # 沒帶 kind 的舊客戶端就是摘要工作，行為跟加這個欄位之前一樣
    kind: JobKind = JobKind.DECK
    # 摘要工作必填（_validate 檢查）；分類工作不需要
    url: str = ""
    # 新聞用途一律要詳細內容：條列在網頁上讀起來太單薄
    detailed: bool = True
    min_slides: int | None = Field(default=None, ge=1, le=MAX_SLIDES)
    max_slides: int | None = Field(default=None, ge=1, le=MAX_SLIDES)
    model: str | None = None
    # 語音辨識的專有名詞提示（講者姓名、機關名），走 Whisper 的來源才用得到
    speech_hint: str | None = Field(default=None, max_length=200)
    # 以下只有分類工作用得到：要分類的文字與可選的領域
    text: str = Field(default="", max_length=MAX_TOPIC_TEXT)
    labels: list[TopicLabelIn] = Field(default_factory=list)
    # 以下只有追問工作用得到：舊的要求、官員當時的回應（可空）、後來那篇的摘要卡、
    # 後來那篇逐字稿裡挑出來的一段（可空）
    request: str = Field(default="", max_length=MAX_FOLLOWUP_REQUEST)
    response: str = Field(default="", max_length=MAX_FOLLOWUP_RESPONSE)
    card: str = Field(default="", max_length=MAX_FOLLOWUP_CARD)
    excerpt: str = Field(default="", max_length=MAX_FOLLOWUP_EXCERPT)


class JobView(BaseModel):
    id: str
    kind: str
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


class HealthView(BaseModel):
    ok: bool
    model: str
    ollama_host: str
    ollama_reachable: bool
    queued: int
    busy: bool


def _view(job: Job, include_result: bool = True) -> JobView:
    return JobView(
        id=job.id, kind=job.kind, url=job.url, status=job.status, detailed=job.detailed,
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
            max_slides=request.max_slides, model=request.model,
            speech_hint=request.speech_hint, kind=request.kind, text=request.text,
            labels=[TopicLabel(key=item.key, label=item.label,
                               description=item.description)
                    for item in request.labels],
            followup=_followup_pair(request)))

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
    if request.kind == JobKind.TOPIC:
        _validate_topic(request)
    elif request.kind == JobKind.FOLLOWUP:
        _validate_followup(request)
    else:
        _validate_deck(request)


def _validate_deck(request: JobRequest) -> None:
    if request.min_slides and request.max_slides \
            and request.min_slides > request.max_slides:
        raise HTTPException(status_code=422, detail="min_slides 不可大於 max_slides")
    _validate_url(request.url)


def _validate_url(url: str) -> None:
    # 這個網址會被交給 yt-dlp。限定 scheme 才不會讓 file:// 之類的東西
    # 變成讀取 GPU 主機本機檔案的管道。
    if urlparse(url).scheme not in ALLOWED_SCHEMES:
        raise HTTPException(status_code=422, detail="網址必須是 http 或 https")


def _validate_topic(request: JobRequest) -> None:
    """檢查的是模型拿得到什麼、選得出什麼。

    網址可以不帶（分類用不到，帶了只是讓工作清單看得出是哪一篇）；帶了就照
    摘要工作的規矩檢查——哪天有人把它拿去用，file:// 不能是現成的漏洞。
    """
    if request.url:
        _validate_url(request.url)
    if not request.text.strip():
        raise HTTPException(status_code=422, detail="分類工作必須帶 text")
    labels = request.labels
    if not MIN_TOPIC_LABELS <= len(labels) <= MAX_TOPIC_LABELS:
        raise HTTPException(
            status_code=422,
            detail=f"labels 必須有 {MIN_TOPIC_LABELS}～{MAX_TOPIC_LABELS} 個")
    # min_length 只看長度，"  " 會過。只有空白的名稱去掉空白就是空的，對回
    # 代碼時會跟「模型回 null」混在一起；只有空白的 key 存進資料庫也認不出是
    # 哪個領域。
    if any(not item.key.strip() or not item.label.strip() for item in labels):
        raise HTTPException(status_code=422, detail="labels 的 key 與名稱不可空白")
    if _has_duplicates(item.key for item in labels):
        raise HTTPException(status_code=422, detail="labels 的 key 不可重複")
    # 模型選的是名稱，再由名稱對回 key：名稱重複就對不回唯一的 key。
    if _has_duplicates(item.label.strip() for item in labels):
        raise HTTPException(status_code=422, detail="labels 的名稱不可重複")


def _has_duplicates(values) -> bool:
    items = list(values)
    return len(set(items)) != len(items)


def _validate_followup(request: JobRequest) -> None:
    """沒有舊的要求就沒有東西可追；沒有後來那篇的摘要卡，模型只能憑一段逐字稿猜。

    回應與逐字稿片段可以空：官員當場可能沒回應，逐字稿也可能挑不出片段，那一對
    照樣要判（沒有片段就抄不出引用，新聞服務的落地檢查會把它記下來）。網址同分類
    工作：用不到，帶了就照摘要工作的規矩檢查。
    """
    if request.url:
        _validate_url(request.url)
    if not request.request.strip():
        raise HTTPException(status_code=422, detail="追問工作必須帶 request")
    if not request.card.strip():
        raise HTTPException(status_code=422, detail="追問工作必須帶 card")


def _followup_pair(request: JobRequest) -> FollowUpPair | None:
    """只有追問工作才帶這一對；其他種類就算送了這些欄位，工作上也不會多一份。"""
    if request.kind != JobKind.FOLLOWUP:
        return None
    return FollowUpPair(request=request.request, response=request.response,
                        card=request.card, excerpt=request.excerpt)
