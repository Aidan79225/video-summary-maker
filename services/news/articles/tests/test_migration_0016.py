"""遷移 0016（影片的處理範圍）：既有的資料列帶過去、沒有範圍、新欄位能存能讀。

跟 0015 一樣用真的遷移：先用 0015 的歷史模型寫資料，再遷移到 0016 看資料還在。SQLite 加欄位會把資料表重建，
既有的文章與段落都要原樣帶過去——尤其是「已發佈的影片沒有範圍」：它們都是整支影片處理的，
不能因為欄位新增而變成「有一段範圍」。
"""
from __future__ import annotations

from datetime import date

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase

from articles.models import Article as LiveArticle, ArticleSource

BEFORE = [("articles", "0015_multi_speaker")]
AFTER = [("articles", "0016_video_range")]


class MigrationTests(TransactionTestCase):
    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(BEFORE)
        old = executor.loader.project_state(BEFORE).apps
        Article, Slide = old.get_model("articles", "Article"), old.get_model("articles", "Slide")
        video = Article.objects.create(
            ivod_id="yt-jNQXAC9IVRw", slug="2005-04-24-yt-jNQXAC9IVRw", source="youtube", title="少子化對談",
            speaker="示範頻道", date=date(2005, 4, 24), duration_seconds=900, status="ready", speaker_count=None,
            ivod_url="https://www.youtube.com/watch?v=jNQXAC9IVRw", transcript_text="00:00 [[S1]]：大家好",
            speakers=[{"id": "S1", "label": "發言者 A"}], diarization={"status": "ok", "warnings": []})
        council = Article.objects.create(
            ivod_id="900001", slug="2026-08-27-900001", title="質詢", speaker="範例一", source="ly",
            meeting="第11屆第5會期第23次會議", date=date(2026, 8, 27), duration_seconds=197,
            ivod_url="https://ivod.ly.gov.tw/Play/Clip/1M/900001", status="ready", transcript_text="逐字稿")
        Slide.objects.create(article=video, index=1, title="開場", bullets=["[[S1]]：歡迎"], detail="[[S1]]：歡迎。",
                             timestamp=3.0, points=[{"speaker": "S1", "turn": 0, "start": 3.0, "text": "歡迎",
                                                      "evidence": "歡迎"}],
                             image="articles/yt-jNQXAC9IVRw/aaaaaaaa/01.webp")
        Slide.objects.create(article=council, index=1, title="第一段", bullets=["重點"], detail="敘述",
                             timestamp=12.5, image="articles/900001/aaaaaaaa/01.webp")
        self.addCleanup(self._back_to_the_latest)

    @staticmethod
    def _back_to_the_latest():
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def _migrate(self):
        executor = MigrationExecutor(connection)
        executor.migrate(AFTER)
        new = executor.loader.project_state(AFTER).apps
        return new.get_model("articles", "Article"), new.get_model("articles", "Slide")

    def test_existing_videos_and_council_rows_have_no_range(self):
        """既有的影片都是整支處理的：不補任何範圍，否則下一次 --rerun 會被當成「範圍被改掉了」。"""
        Article, _ = self._migrate()
        for ivod_id in ("yt-jNQXAC9IVRw", "900001"):
            with self.subTest(row=ivod_id):
                row = Article.objects.get(ivod_id=ivod_id)
                self.assertEqual((row.range_start, row.range_end), (None, None))

    def test_a_published_video_comes_through_untouched(self):
        Article, Slide = self._migrate()
        video = Article.objects.get(ivod_id="yt-jNQXAC9IVRw")
        self.assertEqual((video.title, video.source, video.status, video.duration_seconds, video.speaker_count,
                          video.transcript_text, video.diarization["status"], video.speakers[0]["id"]),
                         ("少子化對談", "youtube", "ready", 900, None, "00:00 [[S1]]：大家好", "ok", "S1"))
        slide = Slide.objects.get(article=video)
        self.assertEqual((slide.index, slide.title, slide.timestamp, slide.points[0]["speaker"], slide.image.name),
                         (1, "開場", 3.0, "S1", "articles/yt-jNQXAC9IVRw/aaaaaaaa/01.webp"))

    def test_a_council_row_comes_through_untouched(self):
        Article, Slide = self._migrate()
        council = Article.objects.get(ivod_id="900001")
        self.assertEqual((council.title, council.source, council.status, council.duration_seconds,
                          council.transcript_text), ("質詢", "ly", "ready", 197, "逐字稿"))
        slide = Slide.objects.get(article=council)
        self.assertEqual((slide.index, slide.title, slide.timestamp, slide.image.name),
                         (1, "第一段", 12.5, "articles/900001/aaaaaaaa/01.webp"))

    def test_the_new_fields_can_be_stored_and_read_back(self):
        Article, _ = self._migrate()
        video = Article.objects.get(ivod_id="yt-jNQXAC9IVRw")
        video.range_start, video.range_end = 600, 3480
        video.save()
        video.refresh_from_db()
        self.assertEqual((video.range_start, video.range_end), (600, 3480))
        video.range_end = None
        video.save()
        video.refresh_from_db()
        self.assertEqual((video.range_start, video.range_end), (600, None))

    def test_a_start_of_zero_is_a_value_not_nothing(self):
        """--end 30:00 的範圍是 0～1800：range_start 存的是 0，不是空的（空的＝沒有範圍）。"""
        Article, _ = self._migrate()
        video = Article.objects.get(ivod_id="yt-jNQXAC9IVRw")
        video.range_start, video.range_end = 0, 1800
        video.save()
        video.refresh_from_db()
        self.assertEqual((video.range_start, video.range_end), (0, 1800))
        self.assertIsNotNone(video.range_start)


class LiveModelDefaultTests(SimpleTestCase):
    """上面測的是遷移當下的欄位；這裡測的是現在的模型本身：新登記的影片沒有範圍。"""

    def test_a_new_video_has_no_range(self):
        video = LiveArticle(source=ArticleSource.YOUTUBE)
        self.assertEqual((video.range_start, video.range_end), (None, None))

    def test_the_fields_are_nullable_whole_seconds(self):
        for name in ("range_start", "range_end"):
            with self.subTest(field=name):
                field = LiveArticle._meta.get_field(name)
                self.assertTrue(field.null and field.blank)
                self.assertEqual(field.get_internal_type(), "PositiveIntegerField")
                self.assertIsNone(field.default)
