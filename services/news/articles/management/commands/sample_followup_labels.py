"""替追問判斷器抽標註集：從「通過程式篩選的候選對」抽，建立空白的 FollowUpLabel。

    python manage.py sample_followup_labels              # 補到 30 對，種子 0
    python manage.py sample_followup_labels --pairs 40 --seed 7

一半取那項要求重疊最高的那一篇、一半在它的候選裡隨機取。抽完到 admin 的「追問標註」頁標
有沒有追問，再跑 eval_followups。已經抽過的要求不重抽，只補到 N 對；同樣的資料、同樣的
種子抽到同樣的對。
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from articles.followups import FOLLOWUP_MIN_LABELS, sample_labels


class Command(BaseCommand):
    help = "從通過程式篩選的候選對裡抽，建立空白的追問標註（之後在 admin 標）"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--pairs", type=int, default=FOLLOWUP_MIN_LABELS,
                            help=f"補到幾對（預設 {FOLLOWUP_MIN_LABELS}，也是評估通過的最少對數）")
        parser.add_argument("--seed", type=int, default=0, help="亂數種子（預設 0），可重現")

    def handle(self, *args, **options) -> None:
        if options["pairs"] < 1:
            raise CommandError("--pairs 至少要 1")
        report = sample_labels(pairs=options["pairs"], seed=options["seed"])
        self.stdout.write(str(report))
        self.stdout.write("到 admin 的「追問標註」頁標有沒有追問，標完跑 python manage.py eval_followups")
