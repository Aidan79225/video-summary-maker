"""處理範圍在 ingest 裡：登記時記下、送去 GPU 時帶上、頁數照範圍的長度算、記錯的範圍不悄悄送出去、結果落地。

假的 GPU 客戶端，不碰網路、不碰 GPU。核對 GPU 結果的每一條規則本身在 test_speaker_result_range.py；
這裡測的是它接在 ingest 裡之後的後果。
"""
from __future__ import annotations

from datetime import date

from articles.gpu_client import JobFailed
from articles.ingest import process_articles, register_video, save_result
from articles.models import Article, ArticleStatus
from articles.tests import test_youtube_ingest as yt
from articles.tests.speaker_support import CONTRACT_VIDEO, diarized_result, ranged_result
from articles.video_range import VideoRange


def register(video_range=None, speaker_count=None, video_id=CONTRACT_VIDEO, duration=3600):
    article, _ = register_video(video_id, title="辯論", speaker="示範頻道", day=date(2026, 11, 14),
                                duration_seconds=duration, speaker_count=speaker_count, video_range=video_range)
    return article


class RegisterTests(yt.IsolatedMedia):
    def test_a_range_is_stored_with_the_video(self):
        article = register(VideoRange(600, 3480))
        stored = Article.objects.get(pk=article.pk)
        self.assertEqual((stored.range_start, stored.range_end), (600, 3480))

    def test_an_open_end_is_stored_as_a_start_and_no_end(self):
        stored = Article.objects.get(pk=register(VideoRange(600, None)).pk)
        self.assertEqual((stored.range_start, stored.range_end), (600, None))

    def test_a_start_of_zero_is_stored_as_zero_not_as_nothing(self):
        stored = Article.objects.get(pk=register(VideoRange(0, 1800)).pk)
        self.assertEqual((stored.range_start, stored.range_end), (0, 1800))

    def test_no_range_stores_nothing(self):
        stored = Article.objects.get(pk=register(None).pk)
        self.assertEqual((stored.range_start, stored.range_end), (None, None))

    def test_the_video_length_stays_the_whole_videos(self):
        """duration_seconds 是整支影片的長度（影片頁給的），不是處理的那一段的：頁面顯示的長度不能因範圍而變。"""
        self.assertEqual(Article.objects.get(pk=register(VideoRange(600, 1200)).pk).duration_seconds, 3600)

    def test_an_existing_video_keeps_the_range_it_has(self):
        """跟標題、講者、日期一樣：已經登記的不動。要改範圍是 add_video --rerun --start/--end，不經過這裡。"""
        first = register(VideoRange(600, 3480))
        again = register(VideoRange(0, 100))
        self.assertEqual(again.pk, first.pk)
        stored = Article.objects.get(pk=first.pk)
        self.assertEqual((stored.range_start, stored.range_end), (600, 3480))

    def test_a_range_for_a_single_speaker_video_is_refused_not_dropped(self):
        """範圍只在多人模式有意義：單人（--speakers 1）影片收下範圍的話，之後送出時範圍會被悄悄丟掉、處理整支影片。"""
        with self.assertRaises(ValueError) as caught:
            register(VideoRange(600, 3480), speaker_count=1)
        self.assertIn("多人", str(caught.exception))
        self.assertFalse(Article.objects.exists())


class SubmitTests(yt.IsolatedMedia):
    def test_the_range_goes_to_the_gpu(self):
        client = yt.RecordingClient(result=ranged_result(start=600, end=1500))
        process_articles(register(VideoRange(600, 1500)), client, timeout=60)
        sent = client.submits[0]
        self.assertEqual((sent["speaker_mode"], sent["speaker_count"], sent["range_start"], sent["range_end"]),
                         ("diarize", None, 600, 1500))

    def test_an_open_end_goes_as_no_end(self):
        client = yt.RecordingClient(result=ranged_result(start=600, end=3600))
        process_articles(register(VideoRange(600, None)), client, timeout=60)
        self.assertEqual((client.submits[0]["range_start"], client.submits[0]["range_end"]), (600, None))

    def test_a_fixed_count_and_a_range_go_together(self):
        client = yt.RecordingClient(result=ranged_result(start=600, end=1500, requested=3))
        process_articles(register(VideoRange(600, 1500), speaker_count=3), client, timeout=60)
        sent = client.submits[0]
        self.assertEqual((sent["speaker_count"], sent["range_start"], sent["range_end"]), (3, 600, 1500))

    def kwargs_sent(self, article):
        """用只記關鍵字的客戶端送一次，回傳 submit 收到的關鍵字（它交回的結果不合多人契約，這一輪會失敗，不重要）。"""
        client = yt.ShapeClient()
        with self.assertLogs("articles.ingest", level="WARNING"):
            process_articles(article, client, timeout=60)
        return list(client.kwargs[0]), client.kwargs[0]

    def test_a_video_without_a_range_is_called_exactly_as_before(self):
        """沒有範圍的影片，呼叫 submit 的關鍵字跟加範圍之前一樣：連 range 這兩個鍵都不傳。"""
        names, _ = self.kwargs_sent(register(None))
        self.assertEqual(names, ["detailed", "min_slides", "max_slides", "speech_hint", "speaker_mode", "speaker_count"])

    def test_with_a_range_the_two_keys_come_after_the_speaker_keys(self):
        names, kwargs = self.kwargs_sent(register(VideoRange(600, 1500)))
        self.assertEqual(names, ["detailed", "min_slides", "max_slides", "speech_hint", "speaker_mode",
                                 "speaker_count", "range_start", "range_end"])
        self.assertEqual((kwargs["range_start"], kwargs["range_end"]), (600, 1500))

    def test_a_council_article_never_carries_a_range(self):
        council = Article.objects.create(
            ivod_id="900001", slug="2026-08-27-900001", title="質詢", speaker="範例一", source="ly",
            meeting="第11屆第5會期第23次會議", date=date(2026, 8, 27), duration_seconds=197,
            ivod_url="https://ivod.ly.gov.tw/Play/Clip/1M/900001", range_start=600, range_end=1200)
        client = yt.ShapeClient()
        process_articles(council, client, timeout=60)
        self.assertEqual(list(client.kwargs[0]), ["detailed", "speech_hint"])


class PageCountTests(yt.IsolatedMedia):
    """頁數照要處理的那一段的長度算：一小時影片裡的十五分鐘，頁數該跟十五分鐘的影片一樣多。"""

    def pages(self, video_range, result, duration=3600, slides=None):
        client = yt.RecordingClient(result=result)
        process_articles(register(video_range, duration=duration), client, timeout=60, slides=slides)
        return client.submits[0]["min_slides"], client.submits[0]["max_slides"]

    def test_a_range_is_counted_by_its_own_length(self):
        # 900 秒 → 最少 clamp(⌈900/360⌉=3, 3, 8)=3，最多 clamp(⌈900/120⌉=8, 6, 50)=8
        self.assertEqual(self.pages(VideoRange(600, 1500), ranged_result(start=600, end=1500)), (3, 8))

    def test_the_whole_video_is_counted_by_the_whole_length(self):
        self.assertEqual(self.pages(None, diarized_result(CONTRACT_VIDEO)), (8, 30))

    def test_an_open_end_runs_to_the_end_of_the_video(self):
        # 3600 − 600 = 3000 秒 → 最少 clamp(9, 3, 8)=8，最多 clamp(25, 6, 50)=25
        self.assertEqual(self.pages(VideoRange(600, None), ranged_result(start=600, end=3600)), (8, 25))

    def test_an_end_beyond_the_video_is_cut_at_its_end(self):
        self.assertEqual(self.pages(VideoRange(600, 9000), ranged_result(start=600, end=3600)), (8, 25))

    def test_the_operators_own_numbers_win(self):
        self.assertEqual(self.pages(VideoRange(600, 1500), ranged_result(start=600, end=1500), slides=(2, 4)), (2, 4))

    def test_an_unknown_video_length_with_an_open_end_leaves_the_pages_to_the_gpu(self):
        self.assertEqual(self.pages(VideoRange(600, None), ranged_result(start=600, end=1500), duration=0),
                         (None, None))

    def test_an_unknown_video_length_with_an_end_still_knows_the_length(self):
        self.assertEqual(self.pages(VideoRange(600, 1500), ranged_result(start=600, end=1500), duration=0), (3, 8))


class StoredRangeProblemTests(yt.IsolatedMedia):
    """資料列上記著的範圍不能悄悄送出去，也不能悄悄丟掉：寧可這支影片失敗、說清楚。"""

    def failed(self, article, **fields):
        Article.objects.filter(pk=article.pk).update(**fields)
        client = yt.RecordingClient(result=ranged_result())
        with self.assertLogs("articles.ingest", level="WARNING"):
            report = process_articles(Article.objects.get(pk=article.pk), client, timeout=60)
        stored = Article.objects.get(pk=article.pk)
        self.assertEqual((report.failed, stored.status), (1, ArticleStatus.FAILED))
        self.assertEqual(client.submits, [], "不該送出任何請求")
        return stored.error

    def test_a_single_speaker_video_that_somehow_has_a_range_fails_before_anything_is_sent(self):
        error = self.failed(register(None), speaker_count=1, range_start=600, range_end=1500)
        self.assertIn("範圍", error)
        self.assertIn("單人", error)

    def test_a_range_that_makes_no_sense_fails_before_anything_is_sent(self):
        error = self.failed(register(None), range_start=600, range_end=600)
        self.assertIn("範圍", error)


class ResumeTests(yt.IsolatedMedia):
    def test_a_job_still_running_is_picked_up_and_the_range_is_not_sent_again(self):
        article = register(VideoRange(600, 1500))
        Article.objects.filter(pk=article.pk).update(gpu_job_id="job-old")
        client = yt.RecordingClient(result=ranged_result(start=600, end=1500),
                                    known_jobs={"job-old": {"status": "running"}})
        process_articles(Article.objects.get(pk=article.pk), client, timeout=60)
        self.assertEqual((client.submits, client.waits[0][0]), ([], "job-old"))
        self.assertEqual(Article.objects.get(pk=article.pk).status, ArticleStatus.READY)


class PublishTests(yt.IsolatedMedia):
    def test_a_ranged_result_is_published_with_the_original_timeline_and_the_range(self):
        article = register(VideoRange(600, 1500))
        save_result(article, ranged_result(start=600, end=1500))
        stored = Article.objects.get(pk=article.pk)
        self.assertEqual(stored.status, ArticleStatus.READY)
        self.assertEqual((stored.diarization["range_start"], stored.diarization["range_end"]), (600.0, 1500.0))
        self.assertEqual((stored.range_start, stored.range_end), (600, 1500))
        self.assertTrue(stored.transcript_text.startswith("10:05 [[S1]]："))
        self.assertEqual(stored.slides.order_by("index").first().points[0]["start"], 605.0)

    def test_a_result_that_ignored_the_range_is_not_published(self):
        """舊版 GPU 悄悄處理整支影片：結果沒有範圍，不發佈、什麼都不寫。"""
        article = register(VideoRange(600, 1500))
        with self.assertRaises(JobFailed) as caught:
            save_result(article, diarized_result(CONTRACT_VIDEO))
        self.assertIn("還沒更新", str(caught.exception))
        stored = Article.objects.get(pk=article.pk)
        self.assertEqual((stored.status, stored.transcript_text, stored.speakers), (ArticleStatus.PENDING, "", []))

    def test_a_video_without_a_range_publishes_exactly_as_before(self):
        article = register(None)
        save_result(article, diarized_result(CONTRACT_VIDEO))
        stored = Article.objects.get(pk=article.pk)
        self.assertEqual(stored.status, ArticleStatus.READY)
        self.assertNotIn("range_start", stored.diarization)
