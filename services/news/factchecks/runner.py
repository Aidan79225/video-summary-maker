"""查核的編排：挑文章 → 送 GPU → 落地 → 決定哪些要人工審核。

沿用匯入流程的規則：以文章為單位、可重跑、嘗試次數有上限、斷線後先接回
既有的工作。多一條規則：重跑不能洗掉人工審核。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from articles.gpu_client import GpuApiClient, GpuApiError, JobField, JobFailed
from articles.ingest import RESUMABLE_STATUSES
from articles.models import Article, ArticleStatus

from .models import (
    HUMAN_REVIEW_STATUSES,
    Claim,
    ClaimKind,
    Evidence,
    FactCheckRun,
    Method,
    ReviewStatus,
    RunStatus,
    Verdict,
)

logger = logging.getLogger(__name__)

# 比匯入少：查核失敗通常是模型或 LYAPI 的問題，多試幾天也不會變好
MAX_ATTEMPTS = 3
STALE_FACTOR = 2
# GPU 端把錯誤寫成「例外類別: 訊息」。模型連不上是服務層級的，不是這篇的問題
_MODEL_UNAVAILABLE = "ModelUnavailable"


class ClaimField(StrEnum):
    """GPU 回傳的欄位名。協定的一部分，Pi 上沒有 factcheck，各留一份。"""
    QUOTE = "quote"
    TIMESTAMP = "timestamp"
    KIND = "kind"
    STATEMENT = "statement"
    SUBJECT = "subject"
    VERDICT = "verdict"
    METHOD = "method"
    RATIONALE = "rationale"
    EVIDENCE = "evidence"


class EvidenceField(StrEnum):
    SOURCE = "source"
    TITLE = "title"
    OFFICIAL_URL = "official_url"
    API_URL = "api_url"
    EXCERPT = "excerpt"


@dataclass
class FactCheckReport:
    checked: int = 0
    claims: int = 0
    pending_review: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (f"查核 {self.checked} 篇、主張 {self.claims} 則（待審核 {self.pending_review}）、"
                f"失敗 {self.failed}、略過 {self.skipped}")


class ReviewedMeanwhile(Exception):
    """等 GPU 的那幾分鐘裡有人審核了這篇：新的結果不能蓋掉人工審核。"""


@dataclass(frozen=True)
class SavedClaims:
    total: int
    pending: int


def _claimable(timeout: float) -> Q:
    """可以被接手的查核：還沒做、做壞了、或卡在查核中太久。"""
    stale_before = timezone.now() - timedelta(seconds=STALE_FACTOR * timeout)
    return (Q(status__in=[RunStatus.PENDING, RunStatus.FAILED])
            | Q(status=RunStatus.PROCESSING, updated_at__lt=stale_before))


def candidates(limit: int, timeout: float) -> list[Article]:
    runs = FactCheckRun.objects.filter(_claimable(timeout), attempts__lt=MAX_ATTEMPTS)
    return list(Article.objects
                .filter(status=ArticleStatus.READY)
                .exclude(transcript_text="")
                .filter(Q(factcheck_run__isnull=True)
                        | Q(factcheck_run__in=runs))
                .order_by("-date", "ivod_id")[:limit])


def has_human_review(article: Article) -> bool:
    return article.claims.filter(review_status__in=HUMAN_REVIEW_STATUSES).exists()


def check_articles(client: GpuApiClient, limit: int, timeout: float) -> FactCheckReport:
    report = FactCheckReport()
    for article in candidates(limit, timeout):
        if has_human_review(article):
            report.skipped += 1
            continue
        if not _check_one(article, client, timeout, report):
            break
    return report


def check_article(article: Article, client: GpuApiClient, timeout: float,
                  force: bool = False) -> FactCheckReport:
    """手動查核一篇。已經有人工審核的，除非 force 否則不動。"""
    report = FactCheckReport()
    if has_human_review(article) and not force:
        report.skipped += 1
        return report
    run, _ = FactCheckRun.objects.get_or_create(article=article)
    run.status, run.attempts = RunStatus.PENDING, 0
    run.save(update_fields=["status", "attempts", "updated_at"])
    _check_one(article, client, timeout, report, force=force)
    return report


def _check_one(article: Article, client: GpuApiClient, timeout: float,
               report: FactCheckReport, force: bool = False) -> bool:
    """查核一篇；回傳這一輪還要不要繼續（服務掛了就不要）。"""
    run = _claim(article, timeout)
    if run is None:
        report.skipped += 1
        return True
    try:
        payload = client.wait(_resume_or_submit(article, run, client), timeout=timeout,
                              on_progress=lambda job: logger.info(
                                  "查核 %s：%s", article.ivod_id,
                                  job.get(JobField.PROGRESS) or job.get(JobField.STATUS)))
        saved = save_claims(article, payload, force=force)
    except ReviewedMeanwhile:
        # 人工審核優先：這次的結果丟掉，保留審核過的那一批，查核視為完成
        run.status, run.error, run.gpu_job_id = RunStatus.DONE, "", ""
        run.save(update_fields=["status", "error", "gpu_job_id", "updated_at"])
        report.skipped += 1
        return True
    except JobFailed as e:
        if not str(e).startswith(_MODEL_UNAVAILABLE):
            _fail(run, e, report)
            return True
        # 模型掛了對後面每一篇都一樣。退回這次的嘗試次數：不然每一輪每篇都秒失敗，
        # 三輪就把整批積壓燒到上限、永久放棄
        run.attempts -= 1
        run.save(update_fields=["attempts"])
        _fail(run, e, report)
        return False
    except GpuApiError as e:
        _fail(run, e, report)
        # 服務層級的問題對後面每一篇都一樣，繼續送只是把整批燒成失敗
        return False
    except Exception as e:  # noqa: BLE001 一篇的怪資料不該讓整批停擺
        logger.exception("查核 %s 時發生預期外的錯誤", article.ivod_id)
        _fail(run, e, report)
        return True
    report.checked += 1
    report.claims += saved.total
    report.pending_review += saved.pending
    return True


def _claim(article: Article, timeout: float) -> FactCheckRun | None:
    """用一次條件式 UPDATE 宣告所有權：排程與手動指令同時跑時，才不會把同一
    篇送去 GPU 兩次。"""
    run, _ = FactCheckRun.objects.get_or_create(article=article)
    claimed = (FactCheckRun.objects
               .filter(pk=run.pk)
               .filter(_claimable(timeout))
               .filter(attempts__lt=MAX_ATTEMPTS)
               .update(status=RunStatus.PROCESSING, error="",
                       attempts=F("attempts") + 1, updated_at=timezone.now()))
    if not claimed:
        return None
    run.refresh_from_db()
    return run


def _resume_or_submit(article: Article, run: FactCheckRun, client: GpuApiClient) -> str:
    if run.gpu_job_id:
        job = client.job(run.gpu_job_id)
        if job is not None and job.get(JobField.STATUS) in RESUMABLE_STATUSES:
            logger.info("查核 %s 接回既有工作 %s", article.ivod_id, run.gpu_job_id)
            return run.gpu_job_id
    job_id = client.submit_factcheck(article.ivod_url, article.speaker, article.date,
                                     article.meeting, article.transcript_text)
    run.gpu_job_id = job_id
    run.save(update_fields=["gpu_job_id", "updated_at"])
    logger.info("查核 %s 已送出，工作 %s", article.ivod_id, job_id)
    return job_id


def _fail(run: FactCheckRun, error: Exception, report: FactCheckReport) -> None:
    run.status = RunStatus.FAILED
    run.error = f"{type(error).__name__}: {error}"[:2000]
    if isinstance(error, JobFailed):
        # 工作本身跑完了、結論是失敗；留著 id 只會讓下一輪重讀同一個失敗
        run.gpu_job_id = ""
    run.save(update_fields=["status", "error", "gpu_job_id", "updated_at"])
    report.failed += 1
    report.errors.append(f"{run.article.ivod_id}: {error}")
    logger.warning("查核 %s 失敗：%s", run.article.ivod_id, error)


def _safe_url(value: object) -> str:
    """只收 http(s)：這個網址會變成網站上可點的連結。"""
    url = str(value or "").strip()
    return url[:1000] if url.startswith(("http://", "https://")) else ""


def _float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@transaction.atomic
def save_claims(article: Article, payload: dict, force: bool = False) -> SavedClaims:
    """整批換掉，不做增量合併。

    已經有人工審核的，除非 force 否則丟 ReviewedMeanwhile、什麼都不動。呼叫端
    在送 GPU 前檢查過，但等結果要好幾分鐘，這段期間可能有人審核——所以鎖住這篇
    的查核紀錄後再檢查一次。
    """
    run, _ = FactCheckRun.objects.select_for_update().get_or_create(article=article)
    if not force and has_human_review(article):
        raise ReviewedMeanwhile(article.ivod_id)
    article.claims.all().delete()
    total = pending = 0
    for raw in payload.get("claims") or []:
        if not isinstance(raw, dict):
            continue
        verdict = str(raw.get(ClaimField.VERDICT) or "")
        kind = str(raw.get(ClaimField.KIND) or "")
        quote = str(raw.get(ClaimField.QUOTE) or "").strip()
        if verdict not in Verdict.values or kind not in ClaimKind.values or not quote:
            continue
        method = str(raw.get(ClaimField.METHOD) or "")
        subject = raw.get(ClaimField.SUBJECT)
        review = (ReviewStatus.PENDING if verdict == Verdict.CONTRADICTED
                  else ReviewStatus.AUTO)
        total += 1
        claim = Claim.objects.create(
            article=article, index=total, quote=quote[:1000],
            timestamp=_float(raw.get(ClaimField.TIMESTAMP)), kind=kind,
            statement=str(raw.get(ClaimField.STATEMENT) or quote)[:1000],
            subject={k: str(v) for k, v in subject.items()} if isinstance(subject, dict) else {},
            verdict=verdict,
            method=method if method in Method.values else Method.NONE,
            rationale=str(raw.get(ClaimField.RATIONALE) or "")[:2000],
            review_status=review,
        )
        pending += review == ReviewStatus.PENDING
        for position, item in enumerate(raw.get(ClaimField.EVIDENCE) or [], start=1):
            if not isinstance(item, dict):
                continue
            Evidence.objects.create(
                claim=claim, position=position,
                source=str(item.get(EvidenceField.SOURCE) or "")[:16],
                title=str(item.get(EvidenceField.TITLE) or "")[:300],
                official_url=_safe_url(item.get(EvidenceField.OFFICIAL_URL)),
                api_url=_safe_url(item.get(EvidenceField.API_URL)),
                excerpt=str(item.get(EvidenceField.EXCERPT) or "")[:4000],
            )

    run.status = RunStatus.DONE
    run.error = ""
    run.gpu_job_id = ""
    run.model = str(payload.get("model") or "")[:100]
    run.checked_at = timezone.now()
    run.save()
    return SavedClaims(total=total, pending=pending)
