"""同步立法院的院內紀錄（院會與委員會的出席、委員提案、記名表決）。

    python manage.py sync_ly_records               # 這一屆（LY_TERM）的全部會期
    python manage.py sync_ly_records --session 5   # 只同步第 5 會期
    python manage.py sync_ly_records --term 11

排程（run_scheduler）每週日在 sync_members 之後跑一次，接著重算人物側寫。抓的過程中任何一頁失敗，
整次失敗、資料庫不動（以非零結束，cron 看得到）；重跑是安全的。
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from articles.ly_records import RecordsUnavailable, sync_ly_records


class Command(BaseCommand):
    help = "從 LYAPI 同步立法院的會議出席、委員提案與記名表決（人物側寫的院內紀錄）"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--term", type=int, default=None,
                            help="第幾屆（預設 LY_TERM）")
        parser.add_argument("--session", type=int, default=None,
                            help="只同步這個會期；不給就全部會期")

    def handle(self, *args, **options) -> None:
        term = options["term"] if options["term"] is not None else settings.LY_TERM
        try:
            report = sync_ly_records(term=term, session=options["session"])
        except RecordsUnavailable as e:
            raise CommandError(f"立法院院內紀錄這次沒有同步（資料庫沒有改動）：{e}") from e
        self.stdout.write(str(report))
