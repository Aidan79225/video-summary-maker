"""重算人物側寫（投入量、具體度）的快取。

    python manage.py compute_profiles

排程（run_scheduler）每晚匯入之後會接著跑一次；手動跑是為了部署後第一次回填、
或改了名冊（合併人物、補任期日期）之後想立刻看到結果。整批重算，重跑是安全的。
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from articles.profiles import compute_profiles


class Command(BaseCommand):
    help = "替文章掛上會期，重算每個人每個會期的側寫指標（印出各會期人數與對不到任期的講者）"

    def handle(self, *args, **options) -> None:
        self.stdout.write(str(compute_profiles()))
