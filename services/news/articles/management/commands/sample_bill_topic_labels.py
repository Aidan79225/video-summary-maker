"""替議案分類器抽標註集：從已同步、有立委主提案的委員提案隨機抽，建立空白的 BillTopicLabel。

    python manage.py sample_bill_topic_labels                 # 補到 20 件，種子 0
    python manage.py sample_bill_topic_labels --count 30 --seed 7

抽完到 admin 的「議案議題標註」頁標主領域，再跑 eval_bill_topics。已經抽過的不重抽，只補到 N 件；
同樣的資料、同樣的種子抽到同樣的議案。
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from articles.bill_topics import BILL_TOPIC_MIN_LABELS, sample_labels


class Command(BaseCommand):
    help = "從有立委主提案的委員提案裡隨機抽，建立空白的議案議題標註（之後在 admin 標）"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--count", type=int, default=BILL_TOPIC_MIN_LABELS,
                            help=f"補到幾件（預設 {BILL_TOPIC_MIN_LABELS}，也是評估通過的最少件數）")
        parser.add_argument("--seed", type=int, default=0, help="亂數種子（預設 0），可重現")

    def handle(self, *args, **options) -> None:
        if options["count"] < 1:
            raise CommandError("--count 至少要 1")
        report = sample_labels(count=options["count"], seed=options["seed"])
        self.stdout.write(str(report))
        self.stdout.write("到 admin 的「議案議題標註」頁標主領域，標完跑 python manage.py eval_bill_topics")
