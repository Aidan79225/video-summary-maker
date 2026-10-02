"""替基礎文章（已完成、單獨發言、有摘要卡）分政策領域，存成 Topic。

    python manage.py classify_topics               # 只分還沒有 Topic 的，上限 TOPIC_DAILY_LIMIT
    python manage.py classify_topics --limit 50
    python manage.py classify_topics --reclassify  # 連已經有的也重分（換模型或提示詞之後）

排程（run_scheduler）每晚匯入之後會接著跑一次。分出來的 Topic 要等該來源的評估通過
（eval_topics）才會算進議題分布；換了模型的話，先評估、通過之後再 --reclassify。
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from articles.gpu_client import GpuApiClient
from articles.topics import classify_topics


class Command(BaseCommand):
    help = "替基礎文章分政策領域（送 GPU 的 topic 工作），存成 Topic"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--limit", type=int, default=settings.TOPIC_DAILY_LIMIT,
                            help="這次最多分幾篇（預設 TOPIC_DAILY_LIMIT）")
        parser.add_argument("--reclassify", action="store_true",
                            help="連已經有 Topic 的也重分（換模型或提示詞之後用）；"
                                 "沒有的先、再來是分得最久的")

    def handle(self, *args, **options) -> None:
        client = GpuApiClient(settings.GPU_API_BASE, settings.GPU_API_KEY)
        report = classify_topics(client, limit=options["limit"], reclassify=options["reclassify"])
        self.stdout.write(str(report))
        for error in report.errors[:10]:
            self.stderr.write(f"  ! {error}")
