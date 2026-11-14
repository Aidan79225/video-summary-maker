"""每日匯入的編排：立法院清單 → GPU 摘要 → 文章。

分成「發現」與「處理」兩步，兩步都以 ivod_id 為準做 upsert。所以排程重跑、
手動補跑、失敗重試都不會產生重複的文章，也不會重做已經完成的事。
"""
from __future__ import annotations

import base64
import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from datetime import date, timedelta
from uuid import uuid4

from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.db import DatabaseError, transaction
from django.db.models import Count, F, Q, QuerySet
from django.utils import timezone

from .followups import forget_article
from .gpu_client import GpuApiClient, GpuApiError, JobField, JobFailed, JobStatus, SpeakerMode
from .ivod_source import IvodClip, SourceUnavailable
from .law_source import LawSource, LawUnavailable
from .members_sync import link_article
from .models import COUNCIL_SOURCES, Article, ArticleSource, ArticleStatus, Slide, Topic
from .profiles import attach_session
from .speaker_result import (SlideResult, check_single_result, check_speaker_result, clean_diarization,  # noqa: F401
                             clean_points, clean_speakers, requested_speaker_count, with_pi_fields)
from .video_range import RangeInvalid, VideoRange, range_length, range_of
from .youtube_source import (canonical_url, clean_speaker, clean_text, is_video_id, ivod_id_for, placeholder_title,
                             slide_range, video_id_from_ivod_id)

logger = logging.getLogger(__name__)

# 同一篇最多重試幾次。一篇永遠失敗的文章（例如影片已下架）會每天排在隊
# 首、每次燒掉幾分鐘 GPU，而且永遠不會放棄。超過就跳過，admin 可以把
# attempts 歸零重來。
MAX_ATTEMPTS = 5

# 幾倍的單篇上限之後就視為卡住。用倍數而不是絕對秒數，調大
# GPU_JOB_TIMEOUT_SECONDS 時才不會反而讓還在正常等待的文章被搶走。
STALE_FACTOR = 2
STUCK_JOB_FACTOR = 2

RESUMABLE_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.DONE})


class ArticleDeleted(Exception):
    """YouTube 影片在處理途中被下架（remove_video 刪了那一列）：GPU 的結果不能寫回去。

    Model.save() 在列不見時會 INSERT 同一個 pk，下架的影片就會自己回到網站上。這不是失敗，也沒有地方
    可以記錄（列已經不在了）：呼叫端把它算成「已下架」，結果丟掉。
    """


class DeckField(StrEnum):
    """GPU 回傳的成品欄位名。協定的一部分，Pi 上沒有 slidebox，各留一份。"""
    TITLE = "title"
    SOURCE_NOTE = "source_note"
    TRANSCRIPT = "transcript_text"
    SLIDES = "slides"
    BRIEF = "brief"


class SlideField(StrEnum):
    TITLE = "title"
    BULLETS = "bullets"
    DETAIL = "detail"
    TIMESTAMP = "timestamp"
    IMAGE = "image_base64"


def clean_brief(raw: object) -> dict | None:
    """把 GPU 回傳的摘要卡整理成前端能安全用的形狀；不成形就當作沒有。

    形狀在這裡釘死一次，前端就不必再對每個欄位做防禦。一句話是空的視同
    沒有卡片——沒有一句話的卡片只是幾個孤立的數字。
    """
    if not isinstance(raw, dict):
        return None
    one_liner = raw.get("one_liner")
    if not isinstance(one_liner, str) or not one_liner.strip():
        return None

    def text(item: dict, key: str) -> str:
        value = item.get(key)
        return value.strip() if isinstance(value, str) else ""

    numbers = [
        {"value": text(n, "value"), "unit": text(n, "unit"),
         "label": text(n, "label"), "quote": text(n, "quote"),
         "law": text(n, "law"), "article": text(n, "article"),
         # 條文來源由 attach_citations 填；重新清理時保留已經抓到的
         "sources": [x for x in (n.get("sources") or []) if isinstance(x, dict)]}
        for n in (raw.get("key_numbers") or []) if isinstance(n, dict)
    ]
    asks = [
        {"request": text(a, "request"), "deadline": text(a, "deadline"),
         "response": text(a, "response")}
        for a in (raw.get("asks") or []) if isinstance(a, dict)
    ]
    return {
        "one_liner": one_liner.strip(),
        "key_numbers": [n for n in numbers if n["value"] and n["label"]],
        "asks": [a for a in asks if a["request"]],
    }


@dataclass
class IngestReport:
    discovered: int = 0
    created: int = 0
    processed: int = 0
    failed: int = 0
    skipped: int = 0
    pending: int = 0
    # 處理途中被下架的影片（只有 YouTube 有 remove_video）：結果丟掉了，既不算完成也不算失敗
    removed: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        summary = (f"發現 {self.discovered}、新增 {self.created}、"
                   f"完成 {self.processed}、失敗 {self.failed}、略過 {self.skipped}")
        if self.removed:
            summary += f"、已下架 {self.removed}"
        # 積壓要講出來：一天的上限是 20 篇，會期日可能有 50～100 段，剩下
        # 的若沒有人報告，就會無聲地一直排在那裡。
        return summary + (f"、仍待處理 {self.pending}" if self.pending else "")


def _slug(clip_date: str, ivod_id: str) -> str:
    return f"{clip_date}-{ivod_id}" if clip_date else ivod_id


def discover(day: date, source) -> tuple[int, int]:
    """把某一天的片段登記成待處理的文章。回傳 (發現數, 新增數)。

    已經存在的一律不動——尤其不能把已完成的文章打回待處理，那會讓每次
    排程都重做一遍同樣的事。
    """
    clips = source.clips_for(day)
    created = 0
    for clip in clips:
        _, made = _upsert(clip, day)
        created += 1 if made else 0
    return len(clips), created


def discover_days(days: Sequence[date], sources: Sequence) -> IngestReport:
    """登記多天、多來源的片段。某一天或某個來源失敗不影響其他。

    每個來源有 `name` 與 `clips_for(day)`；清單拿不到時丟 SourceUnavailable。
    """
    report = IngestReport()
    for source in sources:
        for day in days:
            try:
                found, created = discover(day, source)
            except SourceUnavailable as e:
                report.errors.append(f"{source.name} {day}: {e}")
                logger.warning("%s清單取得失敗（%s）：%s", source.name, day, e)
                continue
            report.discovered += found
            report.created += created
    return report


def _as_date(value) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def _upsert(clip: IvodClip, day: date) -> tuple[Article, bool]:
    article, created = Article.objects.get_or_create(
        ivod_id=clip.ivod_id,
        defaults={
            "slug": _slug(clip.date, clip.ivod_id),
            "title": clip.title,
            "speaker": clip.speaker,
            "meeting": clip.meeting,
            # 來源給的是字串（"2026-08-27"）。存進去之前先轉成 date：剛建立的 article 物件
            # 不會重讀資料庫，date 會一直是字串，後面拿去跟任期日期比就會 TypeError。
            "date": _as_date(clip.date) or day,
            "duration_seconds": clip.duration_seconds,
            "ivod_url": clip.ivod_url,
            "source": clip.source,
            "status": ArticleStatus.PENDING,
        },
    )
    if created:
        # 會期從會議名稱解析，登記時就掛上，不必等每晚的側寫重算
        attach_session(article, save=False)
        # 講者「當時」的政黨：登記那一刻就依日期查任期填好，之後換黨不回溯
        link_article(article, save=False)
        article.save(update_fields=["session", "party", "membership", "updated_at"])
    return article, created


def register_video(video_id: str, *, title: str, speaker: str, day: date,
                   duration_seconds: int = 0, speaker_count: int | None = None,
                   video_range: VideoRange | None = None) -> tuple[Article, bool]:
    """登記一支手動加入的 YouTube 影片（add_video）。回傳 (文章, 這次是不是新建的)。

    跟 _upsert 刻意分開：影片不是議會的片段。來源明確寫成 youtube；`meeting` 一律留空——非空的
    會議名稱可能被 parse_session 解析成會期；也不掛會期、不對任期（沒有名冊，講者是頻道名稱，
    頻道叫「王立」不該因為立法院有一位王立就掛上他的政黨）。

    ivod_id 是 yt-<11 碼影片 id>，slug 是 <日期>-yt-<id>，ivod_url 一律存正規網址：同一支影片
    用什麼寫法貼都是同一篇，GPU 的檢查點（以網址為鍵）正規化之後重試才接得回去。已經存在的
    不動——標題、講者、日期都留著原樣，已完成的也不會被打回待處理。

    speaker_count：None＝預設的多人模式、人數由 GPU 自動判斷；1＝操作者聲明這支影片只有一個人（跳過
    區分）；2 以上＝指定人數。已經存在的影片不改（要改人數用 add_video --rerun --speakers N）。

    video_range：只處理影片的一段（原影片的整數秒；見 video_range.py）。None＝整支影片。範圍只有多人模式
    有意義：單人（speaker_count == 1）加上範圍丟 ValueError，不悄悄收下——送出時範圍會被丟掉、處理整支影片。
    已經存在的影片同樣不改（要改範圍用 add_video --rerun --start/--end）。duration_seconds 仍是整支影片的長度。
    """
    if not is_video_id(video_id):
        raise ValueError(f"不是 11 碼的影片 id：{video_id[:40]!r}")
    if video_range is not None and speaker_count == 1:
        raise ValueError("處理範圍只有多人模式可以指定（--speakers 1 跳過說話者區分，沒有範圍這回事）")
    ivod_id = ivod_id_for(video_id)
    return Article.objects.get_or_create(
        ivod_id=ivod_id,
        defaults={
            "slug": _slug(day.isoformat(), ivod_id),
            # 標題與頻道名稱是上傳者寫的：控制字元、雙向覆寫與換行不進資料庫（見 clean_text）
            "title": clean_text(title)[:300],
            "speaker": clean_speaker(speaker),
            "meeting": "",
            "date": day,
            "duration_seconds": max(0, duration_seconds),
            "ivod_url": canonical_url(video_id),
            "source": ArticleSource.YOUTUBE,
            "status": ArticleStatus.PENDING,
            "speaker_count": speaker_count,
            "range_start": None if video_range is None else video_range.start,
            "range_end": None if video_range is None else video_range.end,
        },
    )


def process_pending(client: GpuApiClient, limit: int, timeout: float,
                    law_source: LawSource | None = None) -> IngestReport:
    """把待處理（與先前失敗）的文章送去 GPU 主機，一次一篇。

    失敗的會留在 failed 狀態並附上原因，下一次排程再試——立法院的逐字稿
    有時晚幾小時才出現，隔天重試通常就好了。

    只處理議會的來源：YouTube 影片由 add_video 明確處理，不進夜間排程。一支兩小時的
    影片會占住整個夜間批次，GPU 離線時還會白白燒掉它的重試次數；GPU 是使用者的桌機，
    不該在沒人看的時候自己開一個長工作。
    """
    queryset = _backlog(timeout).order_by("-date", "ivod_id")[:limit]
    return _process(queryset, client, timeout, law_source=law_source)


def _backlog(timeout: float) -> QuerySet:
    """夜間排程要處理的積壓：議會的、可以接手的、還沒用完重試次數的。"""
    return (Article.objects
            .filter(source__in=COUNCIL_SOURCES)
            .filter(_claimable(timeout))
            .filter(attempts__lt=MAX_ATTEMPTS))


def imageless(limit: int) -> QuerySet:
    """已完成、但一張截圖都沒有的文章。

    立法院的影片 CDN 會間歇性回 5xx——實測同一批片段前一小時還好好的，
    下一小時三個全部連不上。那種時候摘要照樣產得出來（逐字稿走的是另一個
    端點），只有畫面會全缺。只看議會的文章：影片可能本來就沒有畫面（只有聲音的影片）。
    """
    return (Article.objects
            .filter(status=ArticleStatus.READY, source__in=COUNCIL_SOURCES)
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


def rerun_ready(client: GpuApiClient, limit: int, timeout: float) -> IngestReport:
    """把已完成的文章整批重產（摘要與截圖都重做）。

    用在提示詞或模型改了、想讓舊文章也換成新寫法的時候。和補截圖一樣：
    手動指令、不降級、不搶所有權，文章在重跑期間繼續留在站上。
    只重產議會的文章；YouTube 影片要重做用 add_video --rerun。
    """
    queryset = (Article.objects
                .filter(status=ArticleStatus.READY, source__in=COUNCIL_SOURCES)
                .order_by("-date", "ivod_id")[:limit])
    return _process(queryset, client, timeout, demote_on_failure=False, claim=False)


def process_articles(articles_or_queryset: Article | Iterable[Article], client: GpuApiClient, timeout: float, *,
                     demote_on_failure: bool = True, claim: bool = True,
                     slides: tuple[int | None, int | None] | None = None) -> IngestReport:
    """處理指定的幾篇（一篇、清單或 queryset）。add_video 用它：影片不進夜間排程，只有明確點名的才處理。

    demote_on_failure 與 claim 的意義同 _process：重做已發佈的影片（add_video --rerun）要兩個都關，
    影片才會在重跑期間留在站上。slides 是 (最少頁, 最多頁)，影片沒給就依它的長度算（見
    youtube_source.slide_range）。不附法條來源：那是議會質詢的功能。

    報告裡的「仍待處理」是夜間排程那份議會的積壓，跟這幾篇無關，呼叫端不要拿來說明它們。
    """
    if isinstance(articles_or_queryset, Article):
        articles_or_queryset = [articles_or_queryset]
    return _process(articles_or_queryset, client, timeout, demote_on_failure=demote_on_failure, claim=claim,
                    slides=slides)


def _process(queryset: QuerySet, client: GpuApiClient, timeout: float,
             demote_on_failure: bool = True, claim: bool = True,
             law_source: LawSource | None = None,
             slides: tuple[int | None, int | None] | None = None) -> IngestReport:
    """逐篇處理。

    demote_on_failure=False 時失敗只記錄原因、不動狀態：重跑已發佈文章的
    路徑（例如補截圖）必須這樣，把一篇內容完好、只是缺圖的文章打成 FAILED
    等於讓它立刻從新聞站上消失。

    claim=False 時不搶所有權、也不把狀態改成 PROCESSING——同樣是為了讓
    已發佈的文章在重跑期間繼續留在站上。那條路是手動指令，不需要跟排程
    互斥；最壞的情況只是白跑一次 GPU，不會弄壞資料。
    """
    report = IngestReport()
    for article in list(queryset):
        if claim and not _claim(article, timeout):
            # 另一個行程（排程與手動指令同時跑）已經接手這一篇
            report.skipped += 1
            continue
        try:
            _process_one(article, client, timeout, law_source, slides)
        except ArticleDeleted as e:
            # 處理途中被下架：不是失敗，也沒有列可以記錄；結果已經丟掉
            _record_removal(article, e, report)
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
    report.pending = _backlog(timeout).count()
    return report


def _record_failure(article: Article, error: Exception, report: IngestReport,
                    demote: bool) -> None:
    article.error = f"{type(error).__name__}: {error}"[:2000]
    fields = ["error", "updated_at"]
    if demote:
        article.status = ArticleStatus.FAILED
        fields.append("status")
    if isinstance(error, JobFailed):
        # 工作本身跑完了、結論是失敗。留著這個 id 只會讓下一輪接回去重讀
        # 同一個失敗結論，永遠不重送。
        article.gpu_job_id = ""
        fields.append("gpu_job_id")
    if not _save_fields(article, fields):
        # 列已經不在了（處理途中被 remove_video 下架）：沒有地方可以記這次失敗，記成已下架就好。
        # 以前這裡再丟一次 DatabaseError，整批剩下的文章都不會被處理
        _record_removal(article, error, report)
        return
    report.failed += 1
    report.errors.append(f"{article.ivod_id}: {error}")
    logger.warning("文章 %s 處理失敗：%s", article.ivod_id, error)


def _record_removal(article: Article, reason: object, report: IngestReport) -> None:
    report.removed += 1
    report.errors.append(f"{article.ivod_id}: 處理途中被下架（{reason}）")
    logger.warning("文章 %s 在處理途中被下架，GPU 的結果已丟棄：%s", article.ivod_id, reason)


def _vanished(article: Article) -> bool:
    """YouTube 影片的資料列已經不在了（remove_video 刪的）。

    只管影片：議會文章沒有下架指令，永遠是 False，連這個查詢都沒有——它們的行為逐位元組維持原樣。
    """
    return article.source == ArticleSource.YOUTUBE and not Article.objects.filter(pk=article.pk).exists()


def _deleted(article: Article) -> ArticleDeleted:
    return ArticleDeleted(f"{article.ivod_id} 的資料列已經不在了")


def _save_fields(article: Article, fields: list[str]) -> bool:
    """article.save(update_fields=fields)。回傳 False＝YouTube 影片的列已經不在（沒有存）。

    先看一眼再存：列不見時 save() 的 UPDATE 沒有列可以更新（DatabaseError），在交易裡還會讓整個交易
    報廢（needs_rollback）。看一眼和存之間被下架的機會很小，真的碰到的話再確認一次。
    議會文章：不查、行為不變（DatabaseError 照舊往外丟）。
    """
    if _vanished(article):
        return False
    try:
        article.save(update_fields=fields)
    except DatabaseError:
        if _vanished(article):
            return False
        raise
    return True


def _claimable(timeout: float):
    """可以被接手的條件：還沒做、做壞了、或卡在處理中太久。

    卡住的判準是時間：處理途中斷電或被 kill 的文章會永遠停在 PROCESSING，
    沒有這條就再也沒人會碰它。
    """
    stale_before = timezone.now() - timedelta(seconds=STALE_FACTOR * timeout)
    return (Q(status__in=[ArticleStatus.PENDING, ArticleStatus.FAILED])
            | Q(status=ArticleStatus.PROCESSING, updated_at__lt=stale_before))


def _claim(article: Article, timeout: float) -> bool:
    """用一次條件式 UPDATE 宣告所有權；回傳有沒有搶到。

    PROCESSING 這個狀態本身不是鎖——排程與手動指令同時跑時，兩邊都會選
    到同一批文章，然後把同一支影片送去 GPU 兩次、對同一篇文章各自做一次
    「刪光再寫」。要靠資料庫的原子更新才擋得住。
    """
    claimed = (Article.objects
               .filter(pk=article.pk)
               .filter(_claimable(timeout))
               # attempts 也要在這裡擋：兩個行程同時跑時，另一個可能剛把它
               # 推到上限，而查詢那一刻還沒到。
               .filter(attempts__lt=MAX_ATTEMPTS)
               .update(status=ArticleStatus.PROCESSING, error="",
                       attempts=F("attempts") + 1, updated_at=timezone.now()))
    if claimed:
        article.refresh_from_db()
    return bool(claimed)


def _process_one(article: Article, client: GpuApiClient, timeout: float,
                 law_source: LawSource | None = None,
                 slides: tuple[int | None, int | None] | None = None) -> None:
    """送一篇去 GPU 主機並等它跑完。呼叫前必須已經 _claim 成功。"""
    job_id = _resume_or_submit(article, client, timeout, slides)
    result = client.wait(job_id, timeout=timeout,
                         on_progress=lambda job: logger.info(
                             "文章 %s：%s", article.ivod_id,
                             job.get(JobField.PROGRESS) or job.get(JobField.STATUS)))
    save_result(article, result)
    # 落地之後才附來源：文章已經是 READY，LYAPI 掛掉只會少了條文連結，
    # 不會讓幾分鐘的 GPU 成品跟著失敗。
    if law_source is not None:
        attach_citations(article, law_source)


def attach_citations(article: Article, source: LawSource) -> int:
    """摘要卡裡講到法條的關鍵數字，抓條文原文附在旁邊。回傳附上幾則。

    只附來源、不判對錯。整篇任何一則取不到就整篇略過並記 log：來源的
    有無應該一致，一半有一半沒有讀者會以為沒連結的那些查無此條。
    """
    brief = article.brief if isinstance(article.brief, dict) else None
    if not brief:
        return 0
    numbers = brief.get("key_numbers") or []
    wanted = [n for n in numbers if n.get("law") and n.get("article")]
    if not wanted:
        return 0
    attached = 0
    try:
        for number in wanted:
            number["sources"] = source.find(number["law"], number["article"], article.date)
            attached += 1 if number["sources"] else 0
    except LawUnavailable as e:
        logger.warning("文章 %s 的條文來源取不到：%s", article.ivod_id, e)
        return 0
    article.brief = brief
    article.save(update_fields=["brief", "updated_at"])
    return attached


def speech_hint_for(article: Article) -> str:
    """給語音辨識的專有名詞提示：議會名、會議名、講者姓名。Whisper 會優先用這些
    寫法（「楊啓邦」而不是同音的「楊啟邦」）。有逐字稿的來源 GPU 端用不到。"""
    if article.source == ArticleSource.YOUTUBE:
        # 影片沒有議會名與會議名：頻道名稱加標題（人名與主題用詞）。由存在資料庫的欄位組成，
        # 每次重試都一樣——提示一變，GPU 上已存的逐字稿就失效、要重新辨識
        return f"{article.speaker}。{article.title}"[:200]
    body = article.get_source_display()
    text = f"{body} {article.meeting}。發言者：{article.speaker}"
    return text[:200]


def _resume_or_submit(article: Article, client: GpuApiClient, timeout: float,
                      slides: tuple[int | None, int | None] | None = None) -> str:
    """有上一輪留下、而且還可能有成果的工作就接回去，否則送一個新的。

    等待途中 Pi 的網路斷個幾十秒就會讓這一輪失敗，而 GPU 那邊其實還在跑
    ——重送等於把幾分鐘的成品丟掉、整支影片再跑一遍。
    """
    resumable = _resumable_job(article, client, timeout)
    if resumable is not None:
        logger.info("文章 %s 接回既有工作 %s", article.ivod_id, resumable)
        return resumable

    if article.source == ArticleSource.YOUTUBE:
        video_range = _stored_range(article)
        # GPU 預設要 8～15 頁：19 秒的影片只產得出 3 頁，會因「少於下限」失敗。依長度算；
        # 只處理一段的話依那一段的長度（一小時影片裡的十分鐘，頁數該跟十分鐘的影片一樣）
        low, high = (slides if slides is not None
                     else slide_range(range_length(article.duration_seconds, video_range)))
        if wants_diarization(article):
            # 預設：多人區分。人數只有指定 2 位以上才送，沒指定就是 None（GPU 自動判斷）。
            # 範圍只有記著的時候才傳：沒有範圍的影片，呼叫的形狀跟加範圍之前一模一樣
            range_arguments = ({} if video_range is None
                               else {"range_start": video_range.start, "range_end": video_range.end})
            job_id = client.submit(article.ivod_url, detailed=True, min_slides=low, max_slides=high,
                                   speech_hint=speech_hint_for(article), speaker_mode=SpeakerMode.DIARIZE,
                                   speaker_count=requested_speaker_count(article), **range_arguments)
        else:
            if video_range is not None:
                # 單人的請求沒有範圍這個概念：悄悄丟掉的話，操作者要的一段會變成整支影片
                raise JobFailed(f"這支影片是單人模式（--speakers 1），資料列上卻記著處理範圍 {video_range.describe()}："
                                "範圍只有多人模式能用，沒有送出。要處理一段請用 add_video --rerun 不加 --speakers 1")
            # --speakers 1：跟第一部分一模一樣的單人請求，不帶任何說話者的鍵
            job_id = client.submit(article.ivod_url, detailed=True, min_slides=low, max_slides=high,
                                   speech_hint=speech_hint_for(article))
    else:
        job_id = client.submit(article.ivod_url, detailed=True, speech_hint=speech_hint_for(article))
    article.gpu_job_id = job_id
    if not _save_fields(article, ["gpu_job_id", "updated_at"]):
        # 送出之後、記下工作 id 之前，影片被下架了
        raise _deleted(article)
    logger.info("文章 %s 已送出，工作 %s", article.ivod_id, job_id)
    return job_id


def _stored_range(article: Article) -> VideoRange | None:
    """資料列上記著的處理範圍；沒有是 None。記著的不合理（沒有人會寫出這樣的資料列）就讓這一支影片失敗：
    不猜，猜錯的範圍會送去 GPU、處理到不是操作者要的那一段。"""
    try:
        return range_of(article)
    except RangeInvalid as e:
        raise JobFailed(f"這支影片記著的處理範圍不合理，沒有送出：{e}") from e


def wants_diarization(article: Article) -> bool:
    """這支影片要不要做多人說話者區分：YouTube 影片除了操作者聲明單人（speaker_count == 1）以外，都要。

    議會的來源永遠不要：一個人質詢，沒有「誰說的」的問題。
    """
    return article.source == ArticleSource.YOUTUBE and article.speaker_count != 1


def _resumable_job(article: Article, client: GpuApiClient, timeout: float,
                   now: Callable[[], float] = time.time) -> str | None:
    """上一輪的工作 id 還值不值得等。

    只接回還可能有成果的狀態。接回一個 failed 的工作等於每天重讀同一個
    失敗結論、從來不重送——重試機制會靜悄悄地整個失效，而 attempts 照樣
    每天加一，五天後永久放棄。

    卡住太久的（GPU 那邊某一步假死）先取消再重送，否則每天都會白等一輪
    完整的逾時。
    """
    if not article.gpu_job_id:
        return None
    job = client.job(article.gpu_job_id)
    if job is None or job.get(JobField.STATUS) not in RESUMABLE_STATUSES:
        return None
    if _stuck(job, timeout, now):
        logger.warning("工作 %s 卡住太久，取消後重送", article.gpu_job_id)
        client.cancel(article.gpu_job_id)
        return None
    return article.gpu_job_id


def _stuck(job: dict, timeout: float, now: Callable[[], float]) -> bool:
    """這個工作是不是卡太久了。

    QUEUED 也要看：卡住的工作被取消之後，重送的那個會排在它後面一直是
    QUEUED——只認 RUNNING 的話，之後每天都會「接回」這個永遠排不到的
    工作，而且再也不會被判定卡住。
    """
    reference = {
        JobStatus.RUNNING: JobField.STARTED_AT,
        JobStatus.QUEUED: JobField.CREATED_AT,
    }.get(job.get(JobField.STATUS))
    since = job.get(reference) if reference else None
    return bool(since) and now() - float(since) > STUCK_JOB_FACTOR * timeout


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

    YouTube 影片：GPU 做的時候影片被下架（remove_video）的話，文章那一列已經不在，下面的 article.save()
    會 INSERT 同一個 pk，把下架的影片連同新的截圖寫回來。所以在交易裡、任何寫入之前先確認那一列還在，
    不在就丟 ArticleDeleted、什麼都不寫（SQLite 用 IMMEDIATE 交易，交易一開始就拿了寫入鎖，確認之後
    別的行程刪不了這一列）。議會文章維持原樣。

    YouTube 影片的說話者：多人模式（預設）先過 check_speaker_result——比下架的檢查還早、在任何寫入之前：
    不合契約就丟 JobFailed，文章不發佈、舊內容（重跑時）原封不動。單人模式（--speakers 1）的結果不該
    長得像多人結果（check_single_result）。通過之後存的是清理過的版本，而且 speakers、diarization 與每一段的
    points 一律重寫（包括空的）：重跑時舊的說話者清單不能留在新的段落旁邊。操作者取的名字不帶過去——
    重跑之後編號可能全變，把舊名字掛到新編號上等於安錯人。
    """
    checked = None
    if article.source == ArticleSource.YOUTUBE:
        if wants_diarization(article):
            checked = check_speaker_result(article, payload)
        else:
            check_single_result(payload)
    if _vanished(article):
        raise _deleted(article)
    # 用欄位自己的 storage 而不是全域的 default_storage：override_settings
    # 或換過 storage 的部署，刪除才會落在正確的地方。
    old_files = [(s.image.storage, s.image.name) for s in article.slides.all() if s.image]
    article.slides.all().delete()
    # 議題分類讀的是一句話與各段小標，重產之後兩者都換了：舊的領域不再對應這篇的內容，
    # 刪掉讓下一輪 classify_topics 重新分。人工標註（TopicLabel）不刪：發言的議題沒變。
    Topic.objects.filter(article=article).delete()
    # 追問也一樣：它當來源的要求可能換了，它當候選的判斷是對舊內容做的，都要重判
    forget_article(article)
    folder = f"{article.ivod_id}/{uuid4().hex[:8]}"

    if not _keeps_registered_title(article):
        title = payload.get(DeckField.TITLE) or article.title
        if article.source == ArticleSource.YOUTUBE:
            # GPU 從 yt-dlp 拿到的標題也是上傳者寫的；清完什麼都不剩就留著原本的（佔位）標題
            title = clean_text(title) or article.title
        article.title = title
    article.source_note = payload.get(DeckField.SOURCE_NOTE) or ""
    if checked is None:
        article.transcript_text = payload.get(DeckField.TRANSCRIPT) or ""
        article.brief = clean_brief(payload.get(DeckField.BRIEF))
    else:
        # 多人模式沒有摘要卡：摘要卡的「要求與回應」不分說話者，會把主持人的前提寫成來賓的數字
        article.transcript_text = checked.transcript_text
        article.brief = None
    if article.source == ArticleSource.YOUTUBE:
        article.speakers = with_pi_fields(checked.speakers) if checked is not None else []
        article.diarization = checked.diarization if checked is not None else None
    article.status = ArticleStatus.READY
    article.error = ""
    # 成品已經落地，那個工作 id 沒有用了；留著只會讓下一輪接回一份
    # 早就取回過的結果。
    article.gpu_job_id = ""
    article.published_at = article.published_at or timezone.now()
    article.save()

    for position, raw in enumerate(payload.get(DeckField.SLIDES) or [], start=1):
        _save_slide(article, raw, folder, position, checked.slides[position - 1] if checked is not None else None)

    # 交易成功之後才刪舊檔。失敗的話什麼都沒動，舊成品原封不動還在。
    transaction.on_commit(lambda: _delete_files(old_files))
    return article


def _keeps_registered_title(article: Article) -> bool:
    """登記時的標題比 GPU 回來的好的來源：處理完成後不換掉它。

    新北的講者是 Pi 依名冊過濾過的（拿掉議長、副議長與別黨團的召集人）；GPU 那邊沒有名冊，
    標題用的是網站的原始名單。蓋過去的話，標題又會把主席列成講者。
    YouTube 影片的標題是登記時讀到的（或使用者用 --title 指定的），不會被 GPU 的標題換回去；
    只有連標題都讀不到時登記的佔位標題，才換成 GPU 從影片拿到的標題。
    """
    if not article.title:
        return False
    if article.source == ArticleSource.NTPC:
        return True
    if article.source == ArticleSource.YOUTUBE:
        return article.title != placeholder_title(video_id_from_ivod_id(article.ivod_id) or "")
    return False


def _delete_files(files: list[tuple[Storage, str]]) -> None:
    for storage, name in files:
        try:
            storage.delete(name)
        except OSError as e:  # noqa: PERF203 刪不掉只是留下孤兒檔，不該中斷
            logger.warning("刪不掉舊截圖 %s：%s", name, e)


@dataclass
class RemovalReport:
    # 資料庫裡有沒有這篇（沒有的話只清檔案：上一次下架做到一半時，再跑一次就能收尾）
    existed: bool = False
    # 文章有幾段（連同截圖的記錄一起刪）
    slides: int = 0
    # 刪掉的檔案（dry-run 是會刪的）
    files: list[str] = field(default_factory=list)
    # 資料庫記著、卻不在這支影片的資料夾底下的檔名：不刪，只提醒
    strays: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def video_media_folder(ivod_id: str) -> str:
    """影片的截圖資料夾（相對於 MEDIA_ROOT）：save_result 寫的是 articles/<ivod_id>/<批次>/NN.webp。"""
    return f"articles/{ivod_id}"


def remove_video(ivod_id: str, dry_run: bool = False) -> RemovalReport:
    """下架一支 YouTube 影片：刪文章（連同段落），並刪它的截圖與整個 articles/<ivod_id>/ 資料夾。

    管理頁刪文章不會刪檔案（Slide.image 只是個欄位），截圖網址還能用；而且把文章改回 FAILED 或
    PENDING 沒有用——夜間排程會把它重做回來。所以要有明確的下架。

    ivod_id 必須是 yt-<11 碼影片 id>（只含英數、底線、減號），才組得出資料夾——使用者打的東西
    不直接拼進路徑。檔案一律走欄位自己的 storage API（先刪檔、再由深到淺刪資料夾），
    不用 os 或 shutil 去碰路徑。資料夾連失敗的重試留下的孤兒檔一起清；文章已經不在也照清，所以
    跑兩次是安全的。dry_run 只回報會刪什麼。
    """
    if video_id_from_ivod_id(ivod_id) is None:
        raise ValueError(f"不是 yt-<11 碼影片 id>：{ivod_id[:40]!r}")
    article = Article.objects.filter(ivod_id=ivod_id).first()
    if article is not None and article.source != ArticleSource.YOUTUBE:
        raise ValueError(f"{ivod_id} 不是 YouTube 影片（來源是{article.get_source_display()}），不處理")

    report = RemovalReport(existed=article is not None)
    storage = Slide._meta.get_field("image").storage
    folder = video_media_folder(ivod_id)
    named: list[str] = []
    if article is not None:
        slides = list(article.slides.all())
        report.slides = len(slides)
        named = [slide.image.name for slide in slides if slide.image]
    report.strays = [name for name in named if not name.startswith(folder + "/")]
    in_folder = [name for name in named if name.startswith(folder + "/")]
    found, folders = _walk(storage, folder)
    report.files = sorted(set(in_folder) | set(found))
    if dry_run:
        return report

    if article is not None:
        with transaction.atomic():
            article.delete()
    for name in report.files:
        _remove(storage, name, report)
    # 由深到淺：資料夾空了才刪得掉
    for directory in reversed(folders):
        _remove(storage, directory, report)
    return report


def _walk(storage: Storage, root: str) -> tuple[list[str], list[str]]:
    """root 底下所有檔案與資料夾（資料夾含 root 自己，上層在前）。root 不存在就是空的。"""
    try:
        directories, files = storage.listdir(root)
    except (FileNotFoundError, NotADirectoryError):
        return [], []
    found, folders = [f"{root}/{name}" for name in files], [root]
    for name in directories:
        inner_files, inner_folders = _walk(storage, f"{root}/{name}")
        found += inner_files
        folders += inner_folders
    return found, folders


def _remove(storage: Storage, name: str, report: RemovalReport) -> None:
    try:
        storage.delete(name)
    except OSError as e:
        report.errors.append(f"刪不掉 {name}：{e}")
        logger.warning("下架時刪不掉 %s：%s", name, e)


def _save_slide(article: Article, raw: dict, folder: str, index: int,
                checked: SlideResult | None = None) -> None:
    """index 用迴圈位置，不用 payload 裡的值。

    上游若少給或給重複的 index，兩筆都會變成 0 而撞上 unique 約束，整篇
    落地失敗。位置一定是唯一且連續的。

    checked：多人影片這一頁清理過的條列、完整敘述與重點（check_speaker_result 的結果）；有它就只存它，
    不再讀 raw 裡的同名欄位。沒有（議會、單人影片）就跟以前一樣，points 是空清單。
    """
    slide = Slide(
        article=article,
        index=index,
        title=checked.title if checked is not None else str(raw.get(SlideField.TITLE) or ""),
        bullets=list(checked.bullets) if checked is not None else [str(b) for b in (raw.get(SlideField.BULLETS) or [])],
        detail=checked.detail if checked is not None else str(raw.get(SlideField.DETAIL) or ""),
        timestamp=float(raw.get(SlideField.TIMESTAMP) or 0.0),
        points=list(checked.points) if checked is not None else [],
    )
    encoded = raw.get(SlideField.IMAGE)
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


def ingest_day(day: date, sources: Sequence, client: GpuApiClient,
               limit: int, timeout: float,
               law_source: LawSource | None = None) -> IngestReport:
    """一天份的完整流程。"""
    report = discover_days([day], sources)
    # 查不到新片段不影響「把先前登記好的積壓送出去」——兩件事的上游不同，
    # 立法院掛掉時 GPU 沒有理由整夜閒著。
    processed = process_pending(client, limit=limit, timeout=timeout,
                                law_source=law_source)
    report.processed = processed.processed
    report.failed = processed.failed
    report.pending = processed.pending
    report.errors.extend(processed.errors)
    return report
