"""常駐排程：每天固定時間跑一次匯入。

    python manage.py run_scheduler

想用系統排程的人可以不要這個指令，直接用 cron 或 systemd timer 跑
`manage.py ingest_ivod`——兩邊跑的是同一段程式碼。
"""
from __future__ import annotations

import logging
import signal

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

DEFAULT_BACKFILL_DAYS = 3


class Command(BaseCommand):
    help = "每天固定時間自動匯入立法院當日的質詢摘要"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--hour", type=int, default=settings.INGEST_HOUR)
        parser.add_argument("--minute", type=int, default=10)
        parser.add_argument("--backfill-days", type=int, default=DEFAULT_BACKFILL_DAYS,
                            help="啟動時回補前幾天（0 表示不回補）")

    def handle(self, *args, **options) -> None:
        from apscheduler.schedulers.blocking import BlockingScheduler

        scheduler = BlockingScheduler(timezone=settings.TIME_ZONE)
        hour, minute = options["hour"], options["minute"]

        def job(days: int = options["backfill_days"] or 1) -> None:
            # 包起來：例外若冒出排程，APScheduler 會把這個工作移除，之後
            # 就再也不會跑——而使用者不會發現，只會覺得「新聞停更了」。
            try:
                call_command("ingest_ivod", days=days)
            except Exception:  # noqa: BLE001
                logger.exception("每日匯入失敗，排程繼續")

        # 每天也跑回補而不只查昨天：立法院的 AI 逐字稿有時晚幾小時才出現，
        # 而 discover 只收「已經有逐字稿」的片段。晚到排程時間之後的那些，
        # 沒有回補就再也不會被查到，而且沒有任何訊號。
        scheduler.add_job(job, "cron", hour=hour, minute=minute, id="ingest_ivod",
                          max_instances=1, coalesce=True, misfire_grace_time=3600)
        signal.signal(signal.SIGTERM, lambda *_: scheduler.shutdown(wait=False))

        self.stdout.write(
            f"排程已啟動：每天 {hour:02d}:{minute:02d}（{settings.TIME_ZONE}）")

        # 排程是記憶體裡的：行程沒開的那次執行根本不存在，不會補跑。Pi 停電
        # 跨過排程時間，那天的質詢就永遠不會被發現，而且沒有任何地方會報出
        # 這個洞。啟動時回補幾天很便宜——discover 每天只是一次 HTTP，而且
        # 整條流程以 ivod_id 為準做 upsert，重跑不會產生重複。
        backfill = options["backfill_days"]
        if backfill > 0:
            self.stdout.write(f"啟動回補最近 {backfill} 天…")
            job(days=backfill)
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            self.stdout.write("排程停止")
