"""評估議題分類器：把已標註的文章用現在的模型與提示詞重分一次，逐來源比對主領域。

    python manage.py eval_topics

每個來源存一筆評估（TopicEvaluation），印出準確率、通過與否，以及每一筆判錯的。通過 =
已標註至少 20 篇、而且準確率至少 80%。某來源有通過的評估，議題分布才會出現在網站上，而且
只算那個分類器分出來的 Topic。

評估不會改動任何文章的 Topic：換了模型的話，先跑這個，通過了再 classify_topics --reclassify。
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from articles.gpu_client import GpuApiClient, GpuApiError
from articles.profiles import compute_profiles, topic_stats_stale
from articles.topics import EvaluationAborted, evaluate, passing_classifiers


class Command(BaseCommand):
    help = "用人工標註集評估議題分類器（逐來源準確率；通過門檻才上線）"

    def handle(self, *args, **options) -> None:
        client = GpuApiClient(settings.GPU_API_BASE, settings.GPU_API_KEY)
        before = passing_classifiers()
        try:
            report = evaluate(client)
        except (EvaluationAborted, GpuApiError) as e:
            raise CommandError(f"評估中止，什麼都沒存：{e}") from e
        self.stdout.write(str(report))
        if passing_classifiers() != before or topic_stats_stale():
            # 上線的分類器換了（或上次重算失敗、側寫裡還是別的版本算的）：證據篩選
            # （/api/articles?topic=）立刻用新條件，不馬上重算的話兩邊要到明早才對得上。
            # 在那之前網站不給議題區塊（API 會比對議題列的分類器），不會掛錯名字。
            compute_profiles()
            self.stdout.write("上線的分類器變了，已經重算人物側寫")
