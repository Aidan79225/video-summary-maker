"""每日匯入：立法院、臺中市議會與新北市議會的清單 → GPU 摘要 → 文章。

    python manage.py ingest_ivod                # 預設抓昨天
    python manage.py ingest_ivod --date 2026-08-27
    python manage.py ingest_ivod --days 7       # 補跑最近七天
    python manage.py ingest_ivod --discover-only
    python manage.py ingest_ivod --source tccc --date 2026-09-24 --days 45 --discover-only
    python manage.py ingest_ivod --source ntpc --date 2026-09-17 --days 400 --discover-only
"""
from __future__ import annotations

from datetime import date, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from articles.gpu_client import GpuApiClient
from articles.ingest import discover_days, process_pending, rerun_ready, retry_imageless
from articles.ivod_source import IvodDailySource
from articles.law_source import LawSource, LyApi
from articles.members_sync import MembersUnavailable, NtpcMemberSource, sync
from articles.models import ArticleSource, Membership
from articles.ntpc_source import NtpcDailySource, NtpcRoster
from articles.tccc_source import TcccDailySource


class Command(BaseCommand):
    help = "抓立法院、臺中市議會與新北市議會某一天的質詢片段，送去 GPU 主機產生詳細摘要"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--date", help="YYYY-MM-DD，預設昨天")
        parser.add_argument("--days", type=int, default=1, help="往前補跑幾天")
        parser.add_argument("--limit", type=int, default=settings.INGEST_DAILY_LIMIT)
        parser.add_argument("--discover-only", action="store_true",
                            help="只登記，不送去產生摘要")
        parser.add_argument("--source", choices=["ly", "tccc", "ntpc"],
                            help="只查這個來源的清單（ly=立法院、tccc=臺中市議會、"
                                 "ntpc=新北市議會）；不給就查所有啟用的來源。"
                                 "只影響查清單，處理積壓不分來源。")
        parser.add_argument("--process-only", action="store_true",
                            help="不查立法院，只把待處理的送出去")
        parser.add_argument("--retry-imageless", action="store_true",
                            help="重跑「一張截圖都沒有」的文章（立法院的影片 CDN "
                                 "會間歇性掛掉）。會連摘要一起重做，很花 GPU 時間。")

        parser.add_argument("--rerun-ready", action="store_true",
                            help="把已完成的文章整批重產（提示詞或模型改了的時候用）。"
                                 "摘要與截圖都重做，每篇要花幾分鐘 GPU 時間。")

    def handle(self, *args, **options) -> None:
        client = GpuApiClient(settings.GPU_API_BASE, settings.GPU_API_KEY)
        timeout = settings.GPU_JOB_TIMEOUT_SECONDS
        limit = options["limit"]
        law_source = LawSource(LyApi(settings.LYAPI_BASE)) if settings.CITE_LAWS else None

        if options["rerun_ready"]:
            self._report(rerun_ready(client, limit=limit, timeout=timeout))
            return

        if options["retry_imageless"]:
            self._report(retry_imageless(client, limit=limit, timeout=timeout))
            return

        if options["process_only"]:
            self._report(process_pending(client, limit=limit, timeout=timeout,
                                         law_source=law_source))
            return

        days = self._days(options)
        sources = self._sources(options.get("source"))
        names = "、".join(s.name for s in sources)
        self.stdout.write(f"=== {days[-1]} ～ {days[0]}（{names}）===")

        # 先把所有天的清單登記完，再跑**一次**處理。一天一次 process_pending
        # 的話，--days 3 會讓當晚的處理上限悄悄變成三倍——GPU 主機是使用者
        # 的桌機，那會一路跑進上班時間。
        report = discover_days(days, sources)
        if options["discover_only"]:
            self.stdout.write(f"發現 {report.discovered}、新增 {report.created}")
            return

        processed = process_pending(client, limit=limit, timeout=timeout,
                                    law_source=law_source)
        report.processed = processed.processed
        report.failed = processed.failed
        report.pending = processed.pending
        report.errors.extend(processed.errors)
        self._report(report)

    def _sources(self, only: str | None) -> list:
        """要查哪些來源。--source 明確指定時連 TCCC_ENABLED／NTPC_ENABLED 都不看——
        使用者都指名要臺中了，再被環境變數擋掉只會讓人困惑。"""
        if only == "ly":
            return [IvodDailySource(settings.IVOD_API_BASE)]
        if only == "tccc":
            return [TcccDailySource(settings.TCCC_VOD_BASE)]
        if only == "ntpc":
            return [s for s in (self._ntpc_source(),) if s is not None]
        sources = [IvodDailySource(settings.IVOD_API_BASE)]
        if settings.TCCC_ENABLED:
            sources.append(TcccDailySource(settings.TCCC_VOD_BASE))
        if settings.NTPC_ENABLED:
            ntpc = self._ntpc_source()
            if ntpc is not None:
                sources.append(ntpc)
        return sources

    def _ntpc_source(self) -> NtpcDailySource | None:
        """新北的講者規則要靠名冊（誰是議長、誰屬於哪個黨團）。名冊還是空的（第一次
        部署、或排程還沒跑到週日的同步）就先同步一次；同步失敗就**這次不查新北**。

        不能用空名冊繼續：文章的講者在登記那一刻就定了、之後不會重算，空名冊登記的
        片段會永遠把議長、副議長與別黨團的召集人列成講者。晚一天查不會少任何片段。
        """
        if not Membership.objects.filter(source=ArticleSource.NTPC).exists():
            self.stdout.write("新北市議會的名冊是空的，先同步一次…")
            try:
                report = sync(NtpcMemberSource(settings.NTPC_WEB_BASE).fetch())
                self.stdout.write(f"新北市議會：{report}")
            except MembersUnavailable as e:
                self.stderr.write(f"  ! 新北市議會的名冊同步失敗，這次不查新北：{e}")
                return None
        return NtpcDailySource(settings.NTPC_VOD_BASE, roster=NtpcRoster.from_db(),
                               include_mixed=settings.NTPC_INCLUDE_MIXED)

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
