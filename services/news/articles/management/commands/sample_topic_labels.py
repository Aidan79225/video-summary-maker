"""替議題分類器抽標註集：每個來源從基礎文章裡隨機抽，建立空白的 TopicLabel。

    python manage.py sample_topic_labels                 # 每個來源補到 20 篇，種子 0
    python manage.py sample_topic_labels --per-source 30 --seed 7

抽完到 admin 的「議題標註」頁標主領域，再跑 eval_topics。已經抽過的不重抽，只補到每個
來源 N 篇；同樣的資料、同樣的種子抽到同樣的文章。
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from articles.topics import TOPIC_MIN_LABELS, sample_labels


class Command(BaseCommand):
    help = "每個來源從基礎文章裡隨機抽文章，建立空白的議題標註（之後在 admin 標）"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--per-source", type=int, default=TOPIC_MIN_LABELS,
                            help=f"每個來源補到幾篇（預設 {TOPIC_MIN_LABELS}，也是評估通過的最少篇數）")
        parser.add_argument("--seed", type=int, default=0, help="亂數種子（預設 0），可重現")

    def handle(self, *args, **options) -> None:
        if options["per_source"] < 1:
            raise CommandError("--per-source 至少要 1")
        report = sample_labels(per_source=options["per_source"], seed=options["seed"])
        self.stdout.write(str(report))
        self.stdout.write("到 admin 的「議題標註」頁標主領域，標完跑 python manage.py eval_topics")
