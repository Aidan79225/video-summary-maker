"""每日匯入：立法院清單 → GPU 摘要 → 文章。

    python manage.py ingest_ivod                # 預設抓昨天
    python manage.py ingest_ivod --date 2026-08-27
    python manage.py ingest_ivod --days 7       # 補跑最近七天
    python manage.py ingest_ivod --discover-only
"""
from __future__ import annotations

from datetime import date, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from articles.gpu_client import GpuApiClient
from articles.ingest import discover_days, process_pending, retry_imageless
from articles.ivod_source import IvodDailySource


class Command(BaseCommand):
    help = "抓立法院某一天的質詢片段，送去 GPU 主機產生詳細摘要"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--date", help="YYYY-MM-DD，預設昨天")
        parser.add_argument("--days", type=int, default=1, help="往前補跑幾天")
        parser.add_argument("--limit", type=int, default=settings.INGEST_DAILY_LIMIT)
        parser.add_argument("--discover-only", action="store_true",
                            help="只登記，不送去產生摘要")
        parser.add_argument("--process-only", action="store_true",
                            help="不查立法院，只把待處理的送出去")
        parser.add_argument("--retry-imageless", action="store_true",
                            help="重跑「一張截圖都沒有」的文章（立法院的影片 CDN "
                                 "會間歇性掛掉）。會連摘要一起重做，很花 GPU 時間。")

    def handle(self, *args, **options) -> None:
        client = GpuApiClient(settings.GPU_API_BASE, settings.GPU_API_KEY)
        timeout = settings.GPU_JOB_TIMEOUT_SECONDS
        limit = options["limit"]

        if options["retry_imageless"]:
            self._report(retry_imageless(client, limit=limit, timeout=timeout))
            return

        if options["process_only"]:
            self._report(process_pending(client, limit=limit, timeout=timeout))
            return

        days = self._days(options)
        source = IvodDailySource(settings.IVOD_API_BASE)
        self.stdout.write(f"=== {days[-1]} ～ {days[0]} ===")

        # 先把所有天的清單登記完，再跑**一次**處理。一天一次 process_pending
        # 的話，--days 3 會讓當晚的處理上限悄悄變成三倍——GPU 主機是使用者
        # 的桌機，那會一路跑進上班時間。
        report = discover_days(days, source)
        if options["discover_only"]:
            self.stdout.write(f"發現 {report.discovered}、新增 {report.created}")
            return

        processed = process_pending(client, limit=limit, timeout=timeout)
        report.processed = processed.processed
        report.failed = processed.failed
        report.pending = processed.pending
        report.errors.extend(processed.errors)
        self._report(report)

    def _days(self, options) -> list[date]:
        if options["date"]:
            try:
                start = date.fromisoformat(options["date"])
            except ValueError as e:
                raise CommandError("--date 要是 YYYY-MM-DD 的格式") from e
        else:
            # 預設昨天：立法院的 AI 逐字稿不是即時產生的，當天去抓通常是空的
            start = timezone.localdate() - timedelta(days=1)
        return [start - timedelta(days=i) for i in range(max(1, options["days"]))]

    def _report(self, report) -> None:
        self.stdout.write(str(report))
        for error in report.errors[:10]:
            self.stderr.write(f"  ! {error}")
