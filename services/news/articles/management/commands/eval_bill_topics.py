"""評估議案分類器：把已標註的議案用現在的模型與提示詞重分一次，比對主領域。

    python manage.py eval_bill_topics

存一筆評估（BillTopicEvaluation），印出準確率、通過與否，以及每一筆判錯的。通過 = 已標註至少
20 件、而且準確率至少 80%。提案與質詢一致率要這裡通過、立法院的議題分類（eval_topics）也通過才算。

評估不會改動任何議案的 BillTopic：換了模型的話，先跑這個，通過了再 classify_bill_topics --reclassify。
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from articles.bill_topics import evaluate, passing_classifier
from articles.chamber import alignment_stats_stale
from articles.gpu_client import GpuApiClient, GpuApiError
from articles.profiles import compute_profiles
from articles.topics import EvaluationAborted


class Command(BaseCommand):
    help = "用人工標註集評估議案分類器（準確率通過門檻才上線）"

    def handle(self, *args, **options) -> None:
        client = GpuApiClient(settings.GPU_API_BASE, settings.GPU_API_KEY)
        before = passing_classifier()
        try:
            report = evaluate(client)
        except (EvaluationAborted, GpuApiError) as e:
            raise CommandError(f"評估中止，什麼都沒存：{e}") from e
        self.stdout.write(str(report))
        if passing_classifier() != before or alignment_stats_stale():
            # 上線的議案分類器換了（或上次重算失敗）：主提案清單的領域立刻用新版本標，不馬上重算的話
            # 一致率要到明早才跟清單對得上。在那之前 API 不給這張卡（會比對一致率列的分類器）。
            compute_profiles()
            self.stdout.write("上線的議案分類器變了，已經重算人物側寫")
