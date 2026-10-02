"""替基礎文章的要求換算期限、找追問的候選，送 GPU 的 followup 工作判斷有沒有追問。

    python manage.py check_followups              # 只判新的候選對，上限 FOLLOWUP_DAILY_LIMIT
    python manage.py check_followups --limit 50
    python manage.py check_followups --recheck    # 不是通過的判斷器判的要求清掉重判

排程（run_scheduler）每晚議題分類之後會接著跑一次。判斷出來的結果要等判斷器的評估通過
（eval_followups）才會算進追問率；換了模型或提示詞的話，先評估、通過之後再 --recheck。
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from articles.followups import check_followups, passing_judge
from articles.gpu_client import GpuApiClient


class Command(BaseCommand):
    help = "替基礎文章帶期限的要求換算到期日，再送 GPU 的 followup 工作判斷後來有沒有追問"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--limit", type=int, default=settings.FOLLOWUP_DAILY_LIMIT,
                            help="這次最多送幾個判斷（預設 FOLLOWUP_DAILY_LIMIT）")
        parser.add_argument("--recheck", action="store_true",
                            help="先把不是通過的判斷器判的要求清掉重判（新版本評估通過之後用）")

    def handle(self, *args, **options) -> None:
        if options["recheck"] and passing_judge() is None:
            raise CommandError("還沒有通過評估的判斷器，沒有「舊版本」要重判：直接跑 check_followups")
        client = GpuApiClient(settings.GPU_API_BASE, settings.GPU_API_KEY)
        report = check_followups(client, limit=options["limit"], recheck=options["recheck"])
        self.stdout.write(str(report))
        for error in report.errors[:10]:
            self.stderr.write(f"  ! {error}")
