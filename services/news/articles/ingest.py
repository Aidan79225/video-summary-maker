"""每日匯入的編排：立法院清單 → GPU 摘要 → 文章。

分成「發現」與「處理」兩步，兩步都以 ivod_id 為準做 upsert。所以排程重跑、
手動補跑、失敗重試都不會產生重複的文章，也不會重做已經完成的事。
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from uuid import uuid4

from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.db import transaction
from django.db.models import Count, F, Q, QuerySet
from django.utils import timezone

from .gpu_client import GpuApiClient, GpuApiError, JobFailed
from .ivod_source import IvodClip, IvodDailySource, IvodUnavailable
from .models import Article, ArticleStatus, Slide

logger = logging.getLogger(__name__)

# 同一篇最多重試幾次。一篇永遠失敗的文章（例如影片已下架）會每天排在隊
# 首、每次燒掉幾分鐘 GPU，而且永遠不會放棄。超過就跳過，admin 可以把
# attempts 歸零重來。
MAX_ATTEMPTS = 5

# PROCESSING 超過這麼久就視為卡住（處理途中斷電或被 kill）。取兩倍的單篇
# 上限，正常情況不可能誤判。
STALE_PROCESSING_SECONDS = 2 * 60 * 60


@dataclass
class IngestReport:
    discovered: int = 0
    created: int = 0
    processed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

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
            "status": ArticleStatus.PENDING,
        },
    )
    return article, created


def process_pending(client: GpuApiClient, limit: int, timeout: float) -> IngestReport:
    """把待處理（與先前失敗）的文章送去 GPU 主機，一次一篇。

    失敗的會留在 failed 狀態並附上原因，下一次排程再試——立法院的逐字稿
    有時晚幾小時才出現，隔天重試通常就好了。
    """
    queryset = (Article.objects
                .filter(_claimable())
                .filter(attempts__lt=MAX_ATTEMPTS)
                .order_by("-date", "ivod_id")[:limit])
    return _process(queryset, client, timeout)


def imageless(limit: int) -> QuerySet:
    """已完成、但一張截圖都沒有的文章。

    立法院的影片 CDN 會間歇性回 5xx——實測同一批片段前一小時還好好的，
    下一小時三個全部連不上。那種時候摘要照樣產得出來（逐字稿走的是另一個
    端點），只有畫面會全缺。
    """
    return (Article.objects
            .filter(status=ArticleStatus.READY)
            .annotate(images=Count("slides", filter=~Q(slides__image="")))
            .filter(images=0)
            .order_by("-date", "ivod_id")[:limit])


def retry_imageless(client: GpuApiClient, limit: int, timeout: float) -> IngestReport:
    """重跑那些沒有任何截圖的文章。

    刻意不放進每日排程：重跑會連摘要一起重做，每篇要花幾分鐘的 GPU 時間，
    而 CDN 什麼時候恢復沒人知道。要用的時候手動跑。

    失敗時不降級：這些文章已經發佈而且內容完好，把它們打成 FAILED 等於
    讓它們立刻從新聞站上消失。
    """
    return _process(imageless(limit), client, timeout,
                    demote_on_failure=False, claim=False)


def _process(queryset: QuerySet, client: GpuApiClient, timeout: float,
             demote_on_failure: bool = True, claim: bool = True) -> IngestReport:
    """逐篇處理。

    demote_on_failure=False 時失敗只記錄原因、不改狀態：重跑已發佈文章的
    路徑（例如補截圖）必須這樣，把一篇內容完好、只是缺圖的文章打成 FAILED
    等於讓它立刻從新聞站上消失。

    claim=False 時不搶所有權、也不把狀態改成 PROCESSING——同樣是為了讓
    已發佈的文章在重跑期間繼續留在站上。那條路是手動指令，不需要跟排程
    互斥；最壞的情況只是白跑一次 GPU，不會弄壞資料。
    """
    report = IngestReport()
    for article in list(queryset):
        if claim and not _claim(article):
            # 另一個行程（排程與手動指令同時跑）已經接手這一篇
            report.skipped += 1
            continue
        try:
            _process_one(article, client, timeout)
        except (GpuApiError, JobFailed) as e:
            _record_failure(article, e, report, demote_on_failure)
            if isinstance(e, GpuApiError):
                # 服務層級的問題（連不上、逾時）對後面每一篇都一樣，
                # 繼續送只是把整批燒成失敗。
                logger.warning("摘要 API 不可用，這一輪提前結束")
                break
        except Exception as e:  # noqa: BLE001
            # 資料庫鎖住、SD 卡寫滿、上游給了怪資料……這些若冒出去，
            # 那天剩下的文章全部不會被處理，而 run_scheduler 會把
            # traceback 吞進 log——維運者只會看到「新聞停更了」。
            logger.exception("文章 %s 處理時發生預期外的錯誤", article.ivod_id)
            _record_failure(article, e, report, demote_on_failure)
        else:
            report.processed += 1
    return report


def _record_failure(article: Article, error: Exception, report: IngestReport,
                    demote: bool) -> None:
    article.error = f"{type(error).__name__}: {error}"[:2000]
    fields = ["error", "updated_at"]
    if demote:
        article.status = ArticleStatus.FAILED
        fields.append("status")
    else:
        # 只是重跑失敗，原本的成品還在——狀態留著，文章繼續在站上
        article.status = ArticleStatus.READY
        fields.append("status")
    article.save(update_fields=fields)
    report.failed += 1
    report.errors.append(f"{article.ivod_id}: {error}")
    logger.warning("文章 %s 處理失敗：%s", article.ivod_id, error)


def _claimable():
    """可以被接手的條件：還沒做、做壞了、或卡在處理中太久。

    卡住的判準是時間：處理途中斷電或被 kill 的文章會永遠停在 PROCESSING，
    沒有這條就再也沒人會碰它。
    """
    stale_before = timezone.now() - timedelta(seconds=STALE_PROCESSING_SECONDS)
    return (Q(status__in=[ArticleStatus.PENDING, ArticleStatus.FAILED])
            | Q(status=ArticleStatus.PROCESSING, updated_at__lt=stale_before))


def _claim(article: Article) -> bool:
    """用一次條件式 UPDATE 宣告所有權；回傳有沒有搶到。

    PROCESSING 這個狀態本身不是鎖——排程與手動指令同時跑時，兩邊都會選
    到同一批文章，然後把同一支影片送去 GPU 兩次、對同一篇文章各自做一次
    「刪光再寫」。要靠資料庫的原子更新才擋得住。
    """
    claimed = (Article.objects
               .filter(pk=article.pk)
               .filter(_claimable())
               .update(status=ArticleStatus.PROCESSING, error="",
                       attempts=F("attempts") + 1, updated_at=timezone.now()))
    if claimed:
        article.refresh_from_db()
    return bool(claimed)


def _process_one(article: Article, client: GpuApiClient, timeout: float) -> None:
    """送一篇去 GPU 主機並等它跑完。呼叫前必須已經 _claim 成功。"""
    job_id = _resume_or_submit(article, client)
    result = client.wait(job_id, timeout=timeout,
                         on_progress=lambda job: logger.info(
                             "文章 %s：%s", article.ivod_id,
                             job.get("progress_status") or job.get("status")))
    save_result(article, result)
    article.gpu_job_id = ""
    article.save(update_fields=["gpu_job_id", "updated_at"])


def _resume_or_submit(article: Article, client: GpuApiClient) -> str:
    """有上一輪留下的工作就接回去，否則送一個新的。

    等待途中 Pi 的網路斷個幾十秒就會讓這一輪失敗，而 GPU 那邊其實還在跑
    ——重送等於把幾分鐘的成品丟掉、整支影片再跑一遍。
    """
    if article.gpu_job_id and client.job(article.gpu_job_id) is not None:
        logger.info("文章 %s 接回既有工作 %s", article.ivod_id, article.gpu_job_id)
        return article.gpu_job_id

    job_id = client.submit(article.ivod_url, detailed=True)
    article.gpu_job_id = job_id
    article.save(update_fields=["gpu_job_id", "updated_at"])
    logger.info("文章 %s 已送出，工作 %s", article.ivod_id, job_id)
    return job_id


@transaction.atomic
def save_result(article: Article, payload: dict) -> Article:
    """把 GPU 回傳的材料落地。整批換掉，不做增量合併。

    重跑同一篇是正當需求（換了模型、或上次的逐字稿還不完整），此時舊的
    段落必須整批消失，否則會留下前一次的殘骸。

    舊檔案**不在交易裡刪**。檔案系統不會跟著 DB 回滾：先刪檔再讓交易失敗
    的話，Slide 列會被救回來但檔案已經沒了，成品變成一排永久破圖——而且
    `imageless()` 看的是「image 欄位是不是空的」，欄位非空所以永遠不會被
    重試撿到，`status` 也還是 READY，沒有任何路徑會修好它。
    留下孤兒檔比留下破圖便宜得多。

    新檔案寫進一個新的子資料夾，所以在交易提交之前，舊圖片仍然完好可用。
    """
    # 用欄位自己的 storage 而不是全域的 default_storage：override_settings
    # 或換過 storage 的部署，刪除才會落在正確的地方。
    old_files = [(s.image.storage, s.image.name) for s in article.slides.all() if s.image]
    article.slides.all().delete()
    folder = f"{article.ivod_id}/{uuid4().hex[:8]}"

    article.title = payload.get("title") or article.title
    article.source_note = payload.get("source_note") or ""
    article.transcript_text = payload.get("transcript_text") or ""
    article.status = ArticleStatus.READY
    article.error = ""
    article.published_at = article.published_at or timezone.now()
    article.save()

    for raw in payload.get("slides") or []:
        _save_slide(article, raw, folder)

    # 交易成功之後才刪舊檔。失敗的話什麼都沒動，舊成品原封不動還在。
    transaction.on_commit(lambda: _delete_files(old_files))
    return article


def _delete_files(files: list[tuple[Storage, str]]) -> None:
    for storage, name in files:
        try:
            storage.delete(name)
        except OSError as e:  # noqa: PERF203 刪不掉只是留下孤兒檔，不該中斷
            logger.warning("刪不掉舊截圖 %s：%s", name, e)


def _save_slide(article: Article, raw: dict, folder: str) -> None:
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
            # 每次重跑用一個新的子資料夾，而不是覆寫同名檔案：Django 的
            # FileSystemStorage 從不覆寫，撞名會自動加隨機後綴——舊作法看
            # 起來像覆寫，其實只是因為先刪掉了。新資料夾讓「舊圖還在、新圖
            # 也寫好了」同時成立，交易才有回頭路。
            slide.image.save(f"{folder}/{index:02d}.webp",
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
