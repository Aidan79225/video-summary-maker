"""補立法院文章空白的會議名稱。

    python manage.py backfill_meetings

全院委員會、公聽會的片段沒有「會議資料」，舊版匯入時會議名稱存成空字串，解析不出會期，
這些發言就不計入人物側寫（實測約 6%）。新匯入的片段已經會改用頂層的「會議名稱」；這個
指令是給既有的文章補一次：逐篇向 LYAPI 查原始資料，一秒一個請求。補完再跑一次
compute_profiles，它們就會掛上會期。重跑是安全的：只動會議名稱還是空的文章。
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from articles.ivod_source import IvodDailySource, IvodUnavailable, meeting_title
from articles.models import Article, ArticleSource


class Command(BaseCommand):
    help = "向 LYAPI 補立法院文章空白的會議名稱（補完再跑 compute_profiles）"

    def handle(self, *args, **options) -> None:
        source = IvodDailySource(settings.IVOD_API_BASE)
        missing = list(Article.objects.filter(source=ArticleSource.LY, meeting="")
                       .order_by("date", "id").values_list("id", "ivod_id"))
        filled = still_empty = failed = 0
        for article_id, ivod_id in missing:
            try:
                meeting = meeting_title(source.record(ivod_id))
            except IvodUnavailable as e:
                failed += 1
                self.stderr.write(f"  ! {ivod_id}：{e}")
                continue
            if not meeting:
                still_empty += 1
                continue
            # 用 update 而不是 save：不動 updated_at（判斷「處理中卡太久」的依據）
            Article.objects.filter(id=article_id).update(meeting=meeting)
            filled += 1
        self.stdout.write(f"會議名稱空白的立法院文章 {len(missing)} 篇：補上 {filled}、"
                          f"LYAPI 也沒有 {still_empty}、查詢失敗 {failed}")
        if filled:
            self.stdout.write("接著跑 python manage.py compute_profiles，它們才會掛上會期、計入側寫")
