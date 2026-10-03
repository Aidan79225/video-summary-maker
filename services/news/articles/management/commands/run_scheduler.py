"""常駐排程：每天固定時間跑一次匯入，接著替新文章分政策領域、判斷追問、替委員提案分領域、重算人物側寫。

    python manage.py run_scheduler

想用系統排程的人可以不要這個指令，直接用 cron 或 systemd timer 依序跑
`manage.py ingest_ivod`、`manage.py classify_topics`、`manage.py check_followups`、
`manage.py classify_bill_topics`、`manage.py compute_profiles`——兩邊跑的是同一段程式碼。每週日另外依序跑 `sync_members`、
`sync_ly_records`、`compute_profiles`。
"""
from __future__ import annotations

import logging
import signal

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

DEFAULT_BACKFILL_DAYS = 3


def sources_missing_members() -> list[str]:
    """啟用中、但名單表裡還沒有任何一筆任期的來源。

    原本只在整張表是空的時候才先同步：之後新加一個來源（例如新北）時，立法院與
    臺中早就有資料，新來源的名冊就要等到週日才會有——新北的講者規則靠名冊判斷
    議長與黨團，那一週的文章全都會用退化的規則登記。
    """
    from articles.models import ArticleSource, Membership

    wanted = [ArticleSource.LY]
    if settings.TCCC_ENABLED:
        wanted.append(ArticleSource.TCCC)
    if settings.NTPC_ENABLED:
        wanted.append(ArticleSource.NTPC)
    present = set(Membership.objects.filter(source__in=wanted)
                  .values_list("source", flat=True).distinct())
    return [str(source) for source in wanted if source not in present]


def nightly(days: int) -> None:
    """每晚的工作：匯入 → 分政策領域 → 判斷追問 → 議案分類 → 重算人物側寫。

    每一步各包各的：例外若冒出排程，APScheduler 會把這個工作移除，之後就再也不會跑
    ——而使用者不會發現，只會覺得「新聞停更了」。前一步失敗不拖垮後一步：匯入失敗時
    積壓的文章照樣值得分類，分類失敗時既有的文章仍然值得一份最新的統計。

    分類排在匯入之後：剛做好的文章當晚就分，而且不會跟摘要工作搶 GPU 的佇列。
    排在重算之前：側寫讀的是分好的 Topic。追問判斷同理：剛做好的文章當晚就能當候選，
    側寫的追問率讀的是判斷好的 FollowUp。議案分類也排在重算之前：提案與質詢一致率讀的是分好的
    BillTopic；排在文章的工作之後，是因為議案每週才同步一次，不急著跟當晚的新文章搶 GPU。
    """
    try:
        call_command("ingest_ivod", days=days)
    except Exception:  # noqa: BLE001
        logger.exception("每日匯入失敗，排程繼續")
    try:
        call_command("classify_topics", limit=settings.TOPIC_DAILY_LIMIT)
    except Exception:  # noqa: BLE001
        logger.exception("議題分類失敗，排程繼續")
    try:
        call_command("check_followups", limit=settings.FOLLOWUP_DAILY_LIMIT)
    except Exception:  # noqa: BLE001
        logger.exception("追問判斷失敗，排程繼續")
    try:
        call_command("classify_bill_topics", limit=settings.BILL_TOPIC_DAILY_LIMIT)
    except Exception:  # noqa: BLE001
        logger.exception("議案分類失敗，排程繼續")
    try:
        call_command("compute_profiles")
    except Exception:  # noqa: BLE001
        logger.exception("人物側寫重算失敗，排程繼續")


WEEKLY_STEPS = (("sync_members", "議員名單同步失敗"),
                ("sync_ly_records", "立法院院內紀錄同步失敗"),
                ("compute_profiles", "人物側寫重算失敗"))


def weekly() -> None:
    """每週日的工作：議員名單 → 立法院的院內紀錄（出席、提案、表決）→ 重算人物側寫。

    院內紀錄排在名單之後：黨團、到職與離職日都是名單給的，遞補的人當週就對得上。重算排在最後，
    新的院內紀錄當天就上得了網站，不必等到隔天晚上。三步各包各的（同 nightly）：例外冒出排程，
    APScheduler 會把這個工作移除。
    """
    for name, failure in WEEKLY_STEPS:
        try:
            call_command(name)
        except Exception:  # noqa: BLE001
            logger.exception("%s，排程繼續", failure)


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
            nightly(days)

        # 每天也跑回補而不只查昨天：立法院的 AI 逐字稿有時晚幾小時才出現，
        # 而 discover 只收「已經有逐字稿」的片段。晚到排程時間之後的那些，
        # 沒有回補就再也不會被查到，而且沒有任何訊號。
        scheduler.add_job(job, "cron", hour=hour, minute=minute, id="ingest_ivod",
                          max_instances=1, coalesce=True, misfire_grace_time=3600)

        def sync_members_job() -> None:
            try:
                call_command("sync_members")
            except Exception:  # noqa: BLE001
                logger.exception("議員名單同步失敗，排程繼續")

        # 政黨、選區一週看一次就夠；排在匯入之前，當天新文章才標得到。院內紀錄跟著名單一起跑（weekly）
        scheduler.add_job(weekly, "cron", day_of_week="sun", hour=3, minute=30,
                          id="sync_members", max_instances=1, coalesce=True,
                          misfire_grace_time=3600)
        def stop(*_) -> None:
            # 回補是在 scheduler.start() 之前同步跑的（可能一兩個小時）。
            # 那段期間呼叫 shutdown 會丟 SchedulerNotRunningError，而它會
            # 從匯入迴圈當下的位置冒出來，被當成「這篇文章處理失敗」。
            if scheduler.running:
                scheduler.shutdown(wait=False)
            else:
                raise SystemExit(0)

        signal.signal(signal.SIGTERM, stop)

        self.stdout.write(
            f"排程已啟動：每天 {hour:02d}:{minute:02d}（{settings.TIME_ZONE}）")

        # 排程是記憶體裡的：行程沒開的那次執行根本不存在，不會補跑。Pi 停電
        # 跨過排程時間，那天的質詢就永遠不會被發現，而且沒有任何地方會報出
        # 這個洞。啟動時回補幾天很便宜——discover 每天只是一次 HTTP，而且
        # 整條流程以 ivod_id 為準做 upsert，重跑不會產生重複。
        # 任何一個啟用中的來源還沒有名單（第一次部署、或新開了一個來源）就先同步一次，
        # 否則要等到週日文章才有政黨
        from articles.models import ArticleSource
        missing = sources_missing_members()
        if missing:
            labels = "、".join(ArticleSource(s).label for s in missing)
            self.stdout.write(f"議員名單還沒有{labels}的資料，先同步一次…")
            sync_members_job()

        backfill = options["backfill_days"]
        if backfill > 0:
            self.stdout.write(f"啟動回補最近 {backfill} 天…")
            job(days=backfill)
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            self.stdout.write("排程停止")
