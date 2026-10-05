"""替有立委主提案的委員提案分政策領域（提案與質詢一致率用），存成 BillTopic。

    python manage.py classify_bill_topics               # 只分還沒有 BillTopic 的，上限 BILL_TOPIC_DAILY_LIMIT
    python manage.py classify_bill_topics --limit 50
    python manage.py classify_bill_topics --reclassify  # 連已經有的也重分（換模型或提示詞之後）

送給 GPU 的是「議案名稱：<議案名稱>」，新的會期先分。排程（run_scheduler）每晚在追問判斷之後、重算
之前跑一次。分出來的 BillTopic 要等議案分類的評估通過（eval_bill_topics）才會算進一致率；換了模型
的話，先評估、通過之後再 --reclassify。
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from articles.bill_topics import classify_bill_topics
from articles.gpu_client import GpuApiClient


class Command(BaseCommand):
    help = "替有立委主提案的委員提案分政策領域（送 GPU 的 topic 工作），存成 BillTopic"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--limit", type=int, default=settings.BILL_TOPIC_DAILY_LIMIT,
                            help="這次最多分幾件（預設 BILL_TOPIC_DAILY_LIMIT）")
        parser.add_argument("--reclassify", action="store_true",
                            help="連已經有 BillTopic 的也重分（換模型或提示詞之後用）；"
                                 "沒有的先、再來是分得最久的")

    def handle(self, *args, **options) -> None:
        client = GpuApiClient(settings.GPU_API_BASE, settings.GPU_API_KEY)
        report = classify_bill_topics(client, limit=options["limit"], reclassify=options["reclassify"])
        self.stdout.write(str(report))
        for error in report.errors[:10]:
            self.stderr.write(f"  ! {error}")
