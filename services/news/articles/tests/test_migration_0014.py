"""遷移 0014（YouTube 來源）：SQLite 加欄位會把 article 表整個重建一次，既有的文章與指向它的段落都要原樣帶過去。

Pi 上跑的是真的資料庫，不是空的測試庫：用 0013 的歷史模型先寫幾筆資料，再遷移到 0014 看資料還在。
（也在真實資料庫的副本上手動跑過一次，見 PR 說明。）
"""
from __future__ import annotations

from datetime import date

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [("articles", "0013_bill_topics")]
AFTER = [("articles", "0014_youtube_source")]


class MigrationTests(TransactionTestCase):
    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(BEFORE)
        old = executor.loader.project_state(BEFORE).apps
        Article, Slide = old.get_model("articles", "Article"), old.get_model("articles", "Slide")
        article = Article.objects.create(
            ivod_id="900001", slug="2026-08-27-900001", title="質詢", speaker="範例一", source="ly",
            meeting="第11屆第5會期第23次會議", date=date(2026, 8, 27), duration_seconds=197,
            ivod_url="https://ivod.ly.gov.tw/Play/Clip/1M/900001", status="ready",
            brief={"one_liner": "一句話", "key_numbers": [], "asks": []}, transcript_text="逐字稿")
        Slide.objects.create(article=article, index=1, title="第一段", bullets=["重點"], detail="敘述",
                             timestamp=12.5, image="articles/900001/aaaaaaaa/01.webp")
        self.addCleanup(self._back_to_the_latest)

    @staticmethod
    def _back_to_the_latest():
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_existing_articles_and_their_slides_survive_and_get_an_empty_speakers_list(self):
        executor = MigrationExecutor(connection)
        executor.migrate(AFTER)
        new = executor.loader.project_state(AFTER).apps
        Article, Slide = new.get_model("articles", "Article"), new.get_model("articles", "Slide")
        article = Article.objects.get(ivod_id="900001")
        self.assertEqual((article.title, article.source, article.status, article.duration_seconds,
                          article.transcript_text), ("質詢", "ly", "ready", 197, "逐字稿"))
        self.assertEqual(article.brief["one_liner"], "一句話")
        self.assertEqual(article.speakers, [])
        slide = Slide.objects.get(article=article)
        self.assertEqual((slide.index, slide.title, slide.bullets, slide.timestamp, slide.image.name),
                         (1, "第一段", ["重點"], 12.5, "articles/900001/aaaaaaaa/01.webp"))

    def test_a_video_can_be_stored_after_the_migration(self):
        executor = MigrationExecutor(connection)
        executor.migrate(AFTER)
        Article = executor.loader.project_state(AFTER).apps.get_model("articles", "Article")
        video = Article.objects.create(
            ivod_id="yt-jNQXAC9IVRw", slug="2005-04-24-yt-jNQXAC9IVRw", source="youtube", title="Me at the zoo",
            speaker="jawed", date=date(2005, 4, 24), ivod_url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
            speakers=[{"id": "S1"}])
        video.refresh_from_db()
        self.assertEqual((video.source, video.speakers), ("youtube", [{"id": "S1"}]))
