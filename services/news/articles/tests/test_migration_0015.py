"""遷移 0015（多人說話者區分）：既有的資料列帶過去、YouTube 影片算單人、新的影片預設多人。

跟 0014 一樣用真的遷移：先用 0014 的歷史模型寫資料，再遷移到 0015 看資料還在。SQLite 加欄位會把
資料表重建，既有的文章與段落都要原樣帶過去。
"""
from __future__ import annotations

from datetime import date

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase

from articles.models import Article as LiveArticle, ArticleSource, Slide as LiveSlide

BEFORE = [("articles", "0014_youtube_source")]
AFTER = [("articles", "0015_multi_speaker")]


class MigrationTests(TransactionTestCase):
    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(BEFORE)
        old = executor.loader.project_state(BEFORE).apps
        Article, Slide = old.get_model("articles", "Article"), old.get_model("articles", "Slide")
        video = Article.objects.create(
            ivod_id="yt-jNQXAC9IVRw", slug="2005-04-24-yt-jNQXAC9IVRw", source="youtube", title="Me at the zoo",
            speaker="jawed", date=date(2005, 4, 24), duration_seconds=19, status="ready",
            ivod_url="https://www.youtube.com/watch?v=jNQXAC9IVRw", transcript_text="00:00 大家好")
        council = Article.objects.create(
            ivod_id="900001", slug="2026-08-27-900001", title="質詢", speaker="範例一", source="ly",
            meeting="第11屆第5會期第23次會議", date=date(2026, 8, 27), duration_seconds=197,
            ivod_url="https://ivod.ly.gov.tw/Play/Clip/1M/900001", status="ready",
            brief={"one_liner": "一句話", "key_numbers": [], "asks": []}, transcript_text="逐字稿")
        Slide.objects.create(article=video, index=1, title="動物園", bullets=["大象"], detail="敘述",
                             timestamp=3.0, image="articles/yt-jNQXAC9IVRw/aaaaaaaa/01.webp")
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

    def test_an_existing_video_stays_single_speaker(self):
        """既有的影片都是 --speakers 1 做的：不補這個 1 的話，它們會被當成多人影片，下一次 --rerun 就變了模式。"""
        Article, _ = self._migrate()
        video = Article.objects.get(ivod_id="yt-jNQXAC9IVRw")
        self.assertEqual(video.speaker_count, 1)
        self.assertEqual((video.speakers, video.diarization), ([], None))

    def test_a_council_row_comes_through_untouched(self):
        Article, Slide = self._migrate()
        council = Article.objects.get(ivod_id="900001")
        self.assertEqual((council.title, council.source, council.status, council.duration_seconds,
                          council.transcript_text, council.speakers, council.diarization),
                         ("質詢", "ly", "ready", 197, "逐字稿", [], None))
        self.assertEqual(council.brief["one_liner"], "一句話")
        slide = Slide.objects.get(article=council)
        self.assertEqual((slide.index, slide.title, slide.bullets, slide.detail, slide.timestamp, slide.image.name),
                         (1, "第一段", ["重點"], "敘述", 12.5, "articles/900001/aaaaaaaa/01.webp"))

    def test_existing_slides_get_no_points(self):
        _, Slide = self._migrate()
        self.assertEqual([slide.points for slide in Slide.objects.all()], [[], []])

    def test_a_video_registered_after_the_migration_is_multi_speaker_by_default(self):
        """遷移的第二步把預設改回 None：只有既有的資料列是 1，新的影片沒有指定人數就是自動多人。"""
        Article, _ = self._migrate()
        new = Article.objects.create(
            ivod_id="yt-abcdefghijk", slug="2026-10-30-yt-abcdefghijk", source="youtube", title="新影片",
            speaker="頻道", date=date(2026, 10, 30), ivod_url="https://www.youtube.com/watch?v=abcdefghijk")
        new.refresh_from_db()
        self.assertIsNone(new.speaker_count)
        self.assertEqual((new.speakers, new.diarization), ([], None))

    def test_the_new_fields_can_be_stored_and_read_back(self):
        Article, Slide = self._migrate()
        video = Article.objects.get(ivod_id="yt-jNQXAC9IVRw")
        video.speaker_count = 3
        video.diarization = {"status": "ok", "warnings": []}
        video.speakers = [{"id": "S1", "label": "發言者 A"}]
        video.save()
        slide = Slide.objects.get(article=video)
        slide.points = [{"speaker": "S1", "turn": 0, "start": 1.5, "text": "說了一件事", "evidence": "原話"}]
        slide.save()
        video.refresh_from_db()
        slide.refresh_from_db()
        self.assertEqual((video.speaker_count, video.diarization["status"], video.speakers[0]["id"]),
                         (3, "ok", "S1"))
        self.assertEqual(slide.points[0]["speaker"], "S1")


class LiveModelDefaultTests(SimpleTestCase):
    """上面測的是遷移當下的欄位；這裡測的是現在的模型本身：新的影片沒有指定人數就是自動多人（None）。
    預設錯成 1 的話，每一支新登記的影片都會悄悄變成「單人」，而且遷移的歷史檢查之外沒有人發現。"""

    def test_a_new_video_is_multi_speaker_unless_somebody_says_otherwise(self):
        video = LiveArticle(source=ArticleSource.YOUTUBE)
        self.assertIsNone(video.speaker_count)
        self.assertEqual((video.speakers, video.diarization), ([], None))

    def test_a_new_slide_has_no_points(self):
        self.assertEqual(LiveSlide().points, [])

    def test_the_defaults_are_not_shared_between_rows(self):
        first, second = LiveArticle(), LiveArticle()
        first.speakers.append({"id": "S1"})
        self.assertEqual(second.speakers, [])
        slide, other = LiveSlide(), LiveSlide()
        slide.points.append({"speaker": "S1"})
        self.assertEqual(other.points, [])
