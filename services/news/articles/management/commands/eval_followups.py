"""評估追問判斷器：把已標註的對用現在的模型與提示詞重判一次（含落地檢查），比對人工的答案。

    python manage.py eval_followups

存一筆評估（FollowUpEvaluation），印出準確率、通過與否，以及每一筆判錯的。通過 = 已標註至少
30 對、而且準確率至少 85%。有通過的評估，追問率才會出現在網站上，而且只算那個判斷器判的。

評估不會改動任何要求的判斷：換了模型的話，先跑這個；通過之後，舊版本判的要求每晚的 check_followups
會在上限內重判（重判完之前那些人的追問率不給），想一次補完就跑 check_followups --recheck。
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from articles.followups import evaluate, passing_judge
from articles.gpu_client import GpuApiClient, GpuApiError
from articles.profiles import compute_profiles, followup_stats_stale
from articles.topics import EvaluationAborted


class Command(BaseCommand):
    help = "用人工標註集評估追問判斷器（準確率通過門檻才上線）"

    def handle(self, *args, **options) -> None:
        client = GpuApiClient(settings.GPU_API_BASE, settings.GPU_API_KEY)
        before = passing_judge()
        try:
            report = evaluate(client)
        except (EvaluationAborted, GpuApiError) as e:
            raise CommandError(f"評估中止，什麼都沒存：{e}") from e
        self.stdout.write(str(report))
        if passing_judge() != before or followup_stats_stale():
            # 上線的判斷器換了（或上次重算失敗）：清單的狀態立刻用新條件，不馬上重算的話比率要到
            # 明早才跟清單對得上。在那之前 API 不給比率（會比對追問率列的判斷器），不會掛錯名字。
            compute_profiles()
            self.stdout.write("上線的判斷器變了，已經重算人物側寫")
