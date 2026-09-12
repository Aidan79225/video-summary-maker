"""每日匯入的編排：立法院清單 → GPU 摘要 → 文章。

分成「發現」與「處理」兩步，兩步都以 ivod_id 為準做 upsert。所以排程重跑、
手動補跑、失敗重試都不會產生重複的文章，也不會重做已經完成的事。
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import date

from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from .gpu_client import GpuApiClient, GpuApiError, JobFailed
from .ivod_source import IvodClip, IvodDailySource, IvodUnavailable
from .models import FAILED, PENDING, PROCESSING, READY, Article, Slide

logger = logging.getLogger(__name__)


@dataclass
class IngestReport:
    discovered: int = 0
    created: int = 0
    processed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    def __str__(self) -> str:
        return (f"發現 {self.discovered}、新增 {self.created}、完成 {self.processed}、"
                f"失敗 {self.failed}、略過 {self.skipped}")


def _slug(clip_date: str, ivod_id: str) -> str:
    return f"{clip_date}-{ivod_id}" if clip_date else ivod_id


def discover(day: date, source: IvodDailySource) -> tuple[int, int]:
    """把某一天的片段登記成待處理的文章。回傳 (發現數, 新增數)。

    已經存在的一律不動——尤其不能把已完成的文章打回待處理，那會讓每次
    排程都重做一遍同樣的事。
    """
    clips = source.clips_for(day)
    created = 0
    for clip in clips:
        _, made = _upsert(clip)
        created += 1 if made else 0
    return len(clips), created


def _upsert(clip: IvodClip) -> tuple[Article, bool]:
    article, created = Article.objects.get_or_create(
        ivod_id=clip.ivod_id,
        defaults={
            "slug": _slug(clip.date, clip.ivod_id),
            "title": clip.title,
            "speaker": clip.speaker,
            "meeting": clip.meeting,
            "date": clip.date or timezone.localdate(),
            "duration_seconds": clip.duration_seconds,
            "ivod_url": clip.ivod_url,
            "status": PENDING,
        },
    )
    return article, created


def process_pending(client: GpuApiClient, limit: int, timeout: float) -> IngestReport:
    """把待處理（與先前失敗）的文章送去 GPU 主機，一次一篇。

    失敗的會留在 failed 狀態並附上原因，下一次排程再試——立法院的逐字稿
    有時晚幾小時才出現，隔天重試通常就好了。
    """
    queryset = (Article.objects
                .filter(status__in=[PENDING, FAILED, PROCESSING])
                .order_by("-date", "ivod_id")[:limit])
    return _process(queryset, client, timeout)


def imageless(limit: int) -> QuerySet:
    """已完成、但一張截圖都沒有的文章。

    立法院的影片 CDN 會間歇性回 5xx——實測同一批片段前一小時還好好的，
    下一小時三個全部連不上。那種時候摘要照樣產得出來（逐字稿走的是另一個
    端點），只有畫面會全缺。
    """
    return (Article.objects
            .filter(status=READY)
            .annotate(images=Count("slides", filter=~Q(slides__image="")))
            .filter(images=0)
            .order_by("-date", "ivod_id")[:limit])


def retry_imageless(client: GpuApiClient, limit: int, timeout: float) -> IngestReport:
    """重跑那些沒有任何截圖的文章。

    刻意不放進每日排程：重跑會連摘要一起重做，每篇要花幾分鐘的 GPU 時間，
    而 CDN 什麼時候恢復沒人知道。要用的時候手動跑。
    """
    return _process(imageless(limit), client, timeout)


def _process(queryset: QuerySet, client: GpuApiClient, timeout: float) -> IngestReport:
    report = IngestReport()
    for article in list(queryset):
        try:
            _process_one(article, client, timeout)
        except (GpuApiError, JobFailed) as e:
            article.status = FAILED
            article.error = str(e)[:2000]
            article.save(update_fields=["status", "error", "updated_at"])
            report.failed += 1
            report.errors.append(f"{article.ivod_id}: {e}")
            logger.warning("文章 %s 處理失敗：%s", article.ivod_id, e)
            if isinstance(e, GpuApiError):
                # 服務層級的問題（連不上、逾時）對後面每一篇都一樣，
                # 繼續送只是把整批燒成失敗。
                logger.warning("摘要 API 不可用，這一輪提前結束")
                break
        else:
            report.processed += 1
    return report


def _process_one(article: Article, client: GpuApiClient, timeout: float) -> None:
    article.status = PROCESSING
    article.error = ""
    article.save(update_fields=["status", "error", "updated_at"])

    job_id = client.submit(article.ivod_url, detailed=True)
    logger.info("文章 %s 已送出，工作 %s", article.ivod_id, job_id)
    result = client.wait(job_id, timeout=timeout)
    save_result(article, result)


@transaction.atomic
def save_result(article: Article, payload: dict) -> Article:
    """把 GPU 回傳的材料落地。整批換掉，不做增量合併。

    重跑同一篇是正當需求（換了模型、或上次的逐字稿還不完整），此時舊的
    段落必須整批消失，否則會留下前一次的殘骸。
    """
    for slide in article.slides.all():
        if slide.image:
            slide.image.delete(save=False)
    article.slides.all().delete()

    article.title = payload.get("title") or article.title
    article.source_note = payload.get("source_note") or ""
    article.transcript_text = payload.get("transcript_text") or ""
    article.status = READY
    article.error = ""
    article.published_at = article.published_at or timezone.now()
    article.save()

    for raw in payload.get("slides") or []:
        _save_slide(article, raw)
    return article


def _save_slide(article: Article, raw: dict) -> None:
    index = int(raw.get("index") or 0)
    slide = Slide(
        article=article,
        index=index,
        title=str(raw.get("title") or ""),
        bullets=[str(b) for b in (raw.get("bullets") or [])],
        detail=str(raw.get("detail") or ""),
        timestamp=float(raw.get("timestamp") or 0.0),
    )
    encoded = raw.get("image_base64")
    if encoded:
        try:
            content = base64.b64decode(encoded)
        except (ValueError, TypeError):
            content = b""
        if content:
            # 檔名帶 ivod_id：同一篇重跑會覆蓋同一批檔案，不會在 media 裡
            # 越積越多。
            slide.image.save(f"{article.ivod_id}/{index:02d}.webp",
                             ContentFile(content), save=False)
    slide.save()


def ingest_day(day: date, source: IvodDailySource, client: GpuApiClient,
               limit: int, timeout: float) -> IngestReport:
    """一天份的完整流程。"""
    report = IngestReport()
    try:
        report.discovered, report.created = discover(day, source)
    except IvodUnavailable as e:
        report.errors.append(str(e))
        logger.warning("立法院清單取得失敗：%s", e)
        return report
    processed = process_pending(client, limit=limit, timeout=timeout)
    report.processed = processed.processed
    report.failed = processed.failed
    report.errors.extend(processed.errors)
    return report
