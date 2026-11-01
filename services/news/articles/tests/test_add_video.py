"""add_video 與 remove_video：預檢、登記、處理、已存在的每一種狀態、安全閥、下架。

假的 GPU 客戶端（patch 掉指令裡的 GpuApiClient）、假的影片資料（patch 掉 fetch_metadata），
不碰網路。解析與預檢本身在 test_youtube_source.py，登記與處理的細節在 test_youtube_ingest.py。
"""
from __future__ import annotations

import base64
import io
import shutil
import tempfile
import time
from datetime import UTC, date, datetime, timedelta
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from articles.gpu_client import GpuApiError, JobFailed
from articles.ingest import MAX_ATTEMPTS
from articles.models import Article, ArticleStatus
from articles.youtube_source import MetadataUnavailable, VideoMeta

GPU = "articles.management.commands.add_video.GpuApiClient"
FETCH = "articles.management.commands.add_video.fetch_metadata"
ZOO, STREAM = "jNQXAC9IVRw", "f4r2yWk4LV0"
ZOO_URL = f"https://www.youtube.com/watch?v={ZOO}"
IMAGE = b"\x00\x01fake-webp\xff"


def _payload(title="GPU 從影片拿到的標題", slides=2):
    return {
        "video_id": ZOO, "title": title, "source_note": "使用 YouTube 自動字幕，品質可能較差",
        "transcript_text": "00:00 大家好", "brief": {"one_liner": "一句話", "key_numbers": [], "asks": []},
        "slides": [{"index": i, "title": f"第 {i} 段", "bullets": [f"重點 {i}"], "detail": f"第 {i} 段的敘述",
                    "timestamp": float(i * 10), "image_base64": base64.b64encode(IMAGE).decode()}
                   for i in range(1, slides + 1)],
    }


class Gpu:
    """假的 GPU 客戶端：記下每一次 submit／job／wait。"""

    def __init__(self, result=None, error=None, jobs=None, job_error=None):
        self._result = result if result is not None else _payload()
        self._error = error
        self._jobs = jobs or {}
        self._job_error = job_error
        self.submits: list[dict] = []
        self.waits: list[tuple[str, float]] = []
        self.looked_up: list[str] = []

    def job(self, job_id):
        self.looked_up.append(job_id)
        if self._job_error is not None:
            raise self._job_error
        return self._jobs.get(job_id)

    def cancel(self, job_id):
        self._jobs.pop(job_id, None)

    def submit(self, url, detailed=True, min_slides=None, max_slides=None, speech_hint=None):
        self.submits.append({"url": url, "detailed": detailed, "min_slides": min_slides,
                             "max_slides": max_slides, "speech_hint": speech_hint})
        if isinstance(self._error, GpuApiError):
            raise self._error
        return f"job-{len(self.submits)}"

    def wait(self, job_id, timeout, poll_seconds=3.0, on_progress=None):
        self.waits.append((job_id, timeout))
        if self._error is not None:
            raise self._error
        return self._result


def _meta(**kw) -> VideoMeta:
    base = dict(video_id=ZOO, title="Me at the zoo", channel="jawed", channel_url="https://www.youtube.com/@jawed",
                date=date(2005, 4, 24), duration_seconds=19, playable=True, page_read=True, unlisted=False,
                category="Film & Animation", shorts_eligible=False)
    base.update(kw)
    return VideoMeta(**base)


class CommandTestCase(TestCase):
    """每個測試自己的截圖資料夾，加上跑指令的小工具。"""

    def setUp(self):
        media = tempfile.mkdtemp(prefix="news_add_video_")
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        self.enterContext(override_settings(MEDIA_ROOT=media, GPU_JOB_TIMEOUT_SECONDS=1800))

    def run_cmd(self, *args, gpu=None, meta=None, fetch_error=None, expect_gpu=True):
        """跑 add_video。回傳 (stdout, stderr, gpu, fetch)。"""
        out, err = io.StringIO(), io.StringIO()
        gpu = gpu if gpu is not None else Gpu()
        fetch = mock.Mock(return_value=meta or _meta(), side_effect=fetch_error)
        with mock.patch(GPU, return_value=gpu) as client_class, mock.patch(FETCH, fetch):
            try:
                call_command("add_video", *args, stdout=out, stderr=err)
            finally:
                self.last_client_class = client_class
        return out.getvalue(), err.getvalue(), gpu, fetch

    def fails(self, *args, **kw):
        """跑 add_video 並預期 CommandError；回傳 (錯誤訊息, stdout, stderr, gpu, fetch)。"""
        out, err = io.StringIO(), io.StringIO()
        gpu = kw.pop("gpu", None) or Gpu()
        fetch = mock.Mock(return_value=kw.pop("meta", None) or _meta(), side_effect=kw.pop("fetch_error", None))
        with mock.patch(GPU, return_value=gpu) as client_class, mock.patch(FETCH, fetch):
            with self.assertRaises(CommandError) as caught:
                call_command("add_video", *args, stdout=out, stderr=err)
            self.last_client_class = client_class
        return str(caught.exception), out.getvalue(), err.getvalue(), gpu, fetch

    def article(self, video_id=ZOO, **fields):
        defaults = dict(ivod_id=f"yt-{video_id}", slug=f"2005-04-24-yt-{video_id}", source="youtube",
                        title="Me at the zoo", speaker="jawed", date=date(2005, 4, 24), duration_seconds=19,
                        ivod_url=f"https://www.youtube.com/watch?v={video_id}")
        return Article.objects.create(**(defaults | fields))

    def refresh(self, article=None):
        return Article.objects.get(ivod_id=f"yt-{ZOO}") if article is None else Article.objects.get(pk=article.pk)


class FirstLineTests(CommandTestCase):
    def test_the_first_line_is_the_parsed_id_and_the_canonical_url(self):
        """網址沒加引號時 bash 會在 & 截斷：id 還在，但要讓人看得出來。"""
        out, *_ = self.run_cmd(f"https://youtu.be/{ZOO}?t=30&si=abc", "--speakers", "1", "--dry-run")
        first = out.splitlines()[0]
        self.assertIn(ZOO, first)
        self.assertIn(ZOO_URL, first)

    def test_it_is_the_first_line_in_every_mode(self):
        for extra in (["--dry-run"], ["--speakers", "1"]):
            with self.subTest(extra=extra):
                Article.objects.all().delete()
                out, *_ = self.run_cmd(ZOO_URL, *extra)
                self.assertTrue(out.splitlines()[0].startswith(f"影片 {ZOO}"))
        # 連被安全閥擋下的那一次也是
        Article.objects.all().delete()
        _, out, *_ = self.fails(ZOO_URL)
        self.assertTrue(out.splitlines()[0].startswith(f"影片 {ZOO}"))

    def test_a_playlist_that_comes_with_the_video_is_called_out(self):
        out, *_ = self.run_cmd(f"{ZOO_URL}&list=PLabcdefghijklmnop", "--speakers", "1", "--dry-run")
        self.assertIn("播放清單", out)


class UrlTests(CommandTestCase):
    def test_a_url_that_is_not_a_video_stops_everything_with_the_reason(self):
        message, out, _, gpu, fetch = self.fails("https://www.youtube.com/@KC-Huang", "--speakers", "1")
        self.assertIn("頻道", message)
        self.assertFalse(Article.objects.exists())
        fetch.assert_not_called()
        self.assertEqual(gpu.submits, [])

    def test_one_bad_url_in_a_batch_means_none_of_them_is_done(self):
        message, out, *_ , fetch = self.fails(ZOO_URL, "https://www.youtube.com/results?q=x", "--speakers", "1")
        self.assertIn("搜尋", message)
        self.assertFalse(Article.objects.exists())
        fetch.assert_not_called()
        self.assertTrue(out.splitlines()[0].startswith(f"影片 {ZOO}"))

    def test_two_shapes_of_the_same_video_are_one_video(self):
        out, _, gpu, fetch = self.run_cmd(ZOO_URL, f"https://youtu.be/{ZOO}", "--speakers", "1")
        self.assertEqual(Article.objects.count(), 1)
        self.assertEqual(len(gpu.submits), 1)
        self.assertIn("同一支影片", out)

    def test_a_bare_id_that_starts_with_a_dash_works_with_or_without_the_double_dash(self):
        for args in (["-abcdefghij", "--speakers", "1", "--dry-run"],
                     ["--speakers", "1", "--dry-run", "--", "-abcdefghij"]):
            with self.subTest(args=args):
                out, *_ = self.run_cmd(*args)
                first = out.splitlines()[0]
                self.assertTrue(first.startswith("影片 -abcdefghij"))
                self.assertIn("https://www.youtube.com/watch?v=-abcdefghij", first)

    def test_the_overrides_belong_to_one_video(self):
        for flag, value in (("--title", "t"), ("--speaker", "s"), ("--date", "2026-10-30")):
            with self.subTest(flag=flag):
                message, *_ = self.fails(ZOO_URL, f"https://youtu.be/{STREAM}", flag, value, "--speakers", "1")
                self.assertIn("一個網址", message)
        self.assertFalse(Article.objects.exists())

    def test_a_bad_date_is_refused_before_anything_happens(self):
        message, *_ , fetch = self.fails(ZOO_URL, "--date", "昨天", "--speakers", "1")
        self.assertIn("YYYY-MM-DD", message)
        fetch.assert_not_called()


class SpeakersValveTests(CommandTestCase):
    """第二部分（用聲音區分說話的人）完成之前的安全閥：沒有聲明單人影片就不登記、不送出。"""

    def test_without_the_declaration_nothing_is_registered_or_sent(self):
        message, out, _, gpu, fetch = self.fails(ZOO_URL)
        self.assertIn("--speakers 1", message)
        self.assertIn("不只一個人", message)
        self.assertFalse(Article.objects.exists())
        fetch.assert_not_called()
        self.last_client_class.assert_not_called()
        self.assertEqual(gpu.submits, [])
        # 第一行還是先印出 id 與網址
        self.assertTrue(out.splitlines()[0].startswith(f"影片 {ZOO}"))

    def test_the_declaration_lets_it_through(self):
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "1")
        self.assertEqual(len(gpu.submits), 1)
        self.assertEqual(self.refresh().status, ArticleStatus.READY)

    def test_only_one_speaker_can_be_declared_for_now(self):
        for value in ("2", "1,2", "甲,乙", "0", ""):
            with self.subTest(value=value):
                message, *_ = self.fails(ZOO_URL, "--speakers", value)
                self.assertIn("--speakers 現在只接受 1", message)
        self.assertFalse(Article.objects.exists())

    def test_a_dry_run_is_not_blocked(self):
        out, _, gpu, fetch = self.run_cmd(ZOO_URL, "--dry-run")
        fetch.assert_called_once()
        self.assertFalse(Article.objects.exists())
        self.assertEqual(gpu.submits, [])

    def test_an_already_published_video_is_not_blocked(self):
        self.article(status=ArticleStatus.READY)
        out, *_ = self.run_cmd(ZOO_URL)
        self.assertIn("已發佈", out)

    def test_a_rerun_is_blocked_like_any_other_submission(self):
        self.article(status=ArticleStatus.READY)
        message, _, _, gpu, _ = self.fails(ZOO_URL, "--rerun")
        self.assertIn("--speakers 1", message)
        self.assertEqual(gpu.submits, [])

    def test_a_failed_video_is_blocked_too(self):
        self.article(status=ArticleStatus.FAILED, error="x")
        message, *_ = self.fails(ZOO_URL)
        self.assertIn("--speakers 1", message)
        self.assertEqual(self.refresh().status, ArticleStatus.FAILED)

    def test_waiting_for_somebody_elses_run_is_not_blocked(self):
        self.article(status=ArticleStatus.PROCESSING)
        out, *_ = self.run_cmd(ZOO_URL)
        self.assertIn("另一個行程正在處理", out)

    def test_in_a_batch_one_submission_blocks_all_of_it(self):
        self.article(status=ArticleStatus.READY)
        self.fails(ZOO_URL, f"https://youtu.be/{STREAM}")
        self.assertEqual(Article.objects.count(), 1)

    def test_the_declaration_is_recorded_nowhere_and_changes_nothing_else(self):
        self.run_cmd(ZOO_URL, "--speakers", "1")
        self.assertEqual(self.refresh().speakers, [])


class NewVideoTests(CommandTestCase):
    def test_a_new_video_is_registered_summarised_and_published(self):
        out, err, gpu, fetch = self.run_cmd(ZOO_URL, "--speakers", "1")
        article = self.refresh()
        self.assertEqual((article.status, article.source, article.ivod_url, article.slug),
                         (ArticleStatus.READY, "youtube", ZOO_URL, f"2005-04-24-yt-{ZOO}"))
        self.assertEqual((article.title, article.speaker, article.date, article.duration_seconds, article.meeting),
                         ("Me at the zoo", "jawed", date(2005, 4, 24), 19, ""))
        self.assertEqual(article.attempts, 1)
        self.assertEqual(article.slides.count(), 2)
        self.assertEqual(gpu.submits, [{"url": ZOO_URL, "detailed": True, "min_slides": 3, "max_slides": 6,
                                        "speech_hint": "jawed。Me at the zoo"}])
        fetch.assert_called_once_with(ZOO)

    def test_the_output_prints_relative_paths_not_a_domain(self):
        out, *_ = self.run_cmd(ZOO_URL, "--speakers", "1")
        self.assertIn(f"/article/2005-04-24-yt-{ZOO}", out)
        self.assertIn("/videos", out)
        self.assertNotIn("http://", out.replace(ZOO_URL, ""))
        self.assertNotIn("news.aidan.tw", out)
        self.assertIn("使用 YouTube 自動字幕", out)

    def test_the_data_that_will_be_registered_is_printed(self):
        out, *_ = self.run_cmd(ZOO_URL, "--speakers", "1", "--dry-run", meta=_meta(duration_seconds=5715))
        for text in ("標題：Me at the zoo", "頻道（講者）：jawed", "日期：2005-04-24（台北時間）", "長度：1:35:15",
                     "頁數：8～48 頁"):
            self.assertIn(text, out)

    def test_waiting_uses_the_configured_timeout_and_there_is_no_flag_to_change_it(self):
        _, _, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "1")
        self.assertEqual(gpu.waits, [("job-1", 1800)])
        with override_settings(GPU_JOB_TIMEOUT_SECONDS=2700):
            Article.objects.all().delete()
            _, _, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "1")
        self.assertEqual(gpu.waits, [("job-1", 2700)])
        with self.assertRaises(CommandError):
            call_command("add_video", ZOO_URL, "--speakers", "1", "--timeout", "9999",
                         stdout=io.StringIO(), stderr=io.StringIO())

    def test_the_channel_name_cannot_split_into_two_speakers(self):
        self.run_cmd(ZOO_URL, "--speakers", "1", meta=_meta(channel="甲、乙頻道"))
        self.assertEqual(self.refresh().speaker, "甲・乙頻道")

    def test_no_channel_at_all_is_a_channel_called_youtube(self):
        self.run_cmd(ZOO_URL, "--speakers", "1", meta=_meta(channel=None))
        self.assertEqual(self.refresh().speaker, "YouTube")

    def test_no_title_at_all_is_registered_with_the_placeholder_and_replaced_by_the_gpus(self):
        self.run_cmd(ZOO_URL, "--speakers", "1", meta=_meta(title=None))
        self.assertEqual(self.refresh().title, "GPU 從影片拿到的標題")

    def test_the_overrides_win_and_the_title_survives_processing(self):
        self.run_cmd(ZOO_URL, "--speakers", "1", "--title", "我取的標題", "--speaker", "固定的頻道名",
                     "--date", "2026-10-30")
        article = self.refresh()
        self.assertEqual((article.title, article.speaker, article.date, article.slug),
                         ("我取的標題", "固定的頻道名", date(2026, 10, 30), f"2026-10-30-yt-{ZOO}"))

    def test_an_unknown_date_falls_back_to_today_in_taipei_with_a_loud_warning(self):
        with mock.patch("django.utils.timezone.now",
                        return_value=datetime(2026, 10, 31, 17, 0, tzinfo=UTC)):   # 台北已經是 11-01
            out, err, *_ = self.run_cmd(ZOO_URL, "--speakers", "1", meta=_meta(date=None))
        self.assertEqual(self.refresh().date, date(2026, 11, 1))
        self.assertIn("抓不到影片的日期", err)
        self.assertIn("--date", err)

    def test_the_slide_range_can_be_overridden_and_a_contradiction_is_caught_before_registering(self):
        _, _, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "1", "--min-slides", "2", "--max-slides", "4")
        self.assertEqual((gpu.submits[0]["min_slides"], gpu.submits[0]["max_slides"]), (2, 4))
        Article.objects.all().delete()
        for args in (["--min-slides", "9", "--max-slides", "4"], ["--max-slides", "99"], ["--min-slides", "0"]):
            with self.subTest(args=args):
                self.fails(ZOO_URL, "--speakers", "1", *args)
                self.assertFalse(Article.objects.exists())
        # 長影片的下限（8）比只給的 --max-slides 還大
        message, *_ = self.fails(ZOO_URL, "--speakers", "1", "--max-slides", "5", meta=_meta(duration_seconds=3600))
        self.assertIn("矛盾", message)
        self.assertFalse(Article.objects.exists())

    def test_an_unknown_length_leaves_the_pages_to_the_gpu(self):
        _, _, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "1", meta=_meta(duration_seconds=0))
        self.assertEqual((gpu.submits[0]["min_slides"], gpu.submits[0]["max_slides"]), (None, None))

    def test_a_dry_run_registers_nothing_and_never_touches_the_gpu(self):
        out, err, gpu, fetch = self.run_cmd(ZOO_URL, "--speakers", "1", "--dry-run")
        self.assertFalse(Article.objects.exists())
        self.assertEqual((gpu.submits, gpu.waits, gpu.looked_up), ([], [], []))
        self.assertIn("--dry-run", out)
        self.assertIn(f"/article/2005-04-24-yt-{ZOO}", out)

    def test_a_dry_run_that_would_be_refused_says_so_and_fails(self):
        message, *_ = self.fails(ZOO_URL, "--dry-run", meta=_meta(unlisted=True))
        self.assertIn("--allow-unlisted", message)

    def test_a_failed_job_is_a_failed_article_and_a_failed_command(self):
        gpu = Gpu(error=JobFailed("影片已下架"))
        with self.assertLogs("articles.ingest", level="WARNING"):
            message, *_ = self.fails(ZOO_URL, "--speakers", "1", gpu=gpu)
        self.assertIn("影片已下架", message)
        self.assertIn("再執行同一個指令", message)
        article = self.refresh()
        self.assertEqual((article.status, article.attempts), (ArticleStatus.FAILED, 1))

    def test_a_gpu_that_is_off_is_a_failed_command_that_can_be_retried(self):
        gpu = Gpu(error=GpuApiError("連線被拒"))
        with self.assertLogs("articles.ingest", level="WARNING"):
            message, *_ = self.fails(ZOO_URL, "--speakers", "1", gpu=gpu)
        self.assertIn("連線被拒", message)
        self.assertEqual(self.refresh().status, ArticleStatus.FAILED)
        # GPU 開了之後同一個指令就好
        out, *_ = self.run_cmd(ZOO_URL, "--speakers", "1")
        self.assertEqual(self.refresh().status, ArticleStatus.READY)

    def test_every_video_in_a_batch_is_tried_and_the_failures_are_listed_at_the_end(self):
        class Selective(Gpu):
            def wait(self, job_id, timeout, poll_seconds=3.0, on_progress=None):
                if job_id == "job-1":
                    raise JobFailed("第一支壞了")
                return super().wait(job_id, timeout, poll_seconds, on_progress)

        with self.assertLogs("articles.ingest", level="WARNING"):
            message, out, err, gpu, _ = self.fails(ZOO_URL, f"https://youtu.be/{STREAM}", "--speakers", "1",
                                                   gpu=Selective())
        self.assertIn(ZOO, message)
        self.assertNotIn(STREAM, message)
        self.assertIn("第一支壞了", err)
        self.assertEqual(Article.objects.get(ivod_id=f"yt-{STREAM}").status, ArticleStatus.READY)
        self.assertEqual(Article.objects.get(ivod_id=f"yt-{ZOO}").status, ArticleStatus.FAILED)


class PreflightIntegrationTests(CommandTestCase):
    """預檢沒過就不登記：沒有救的影片不佔一個重試次數。"""

    CASES = (
        ("not found", dict(playable=False, playability_reason="影片不存在或已被移除（oEmbed 回 404）"), [], "不存在"),
        ("live", dict(is_live=True), ["--force", "--allow-unlisted", "--allow-music"], "直播中"),
        ("upcoming", dict(is_upcoming=True, playable=False, playability_reason="x"), ["--force"], "尚未首播"),
        ("short", dict(shorts_eligible=True), ["--force", "--allow-music"], "Shorts"),
        ("unlisted", dict(unlisted=True), [], "--allow-unlisted"),
        ("music", dict(category="Music"), [], "--allow-music"),
        ("long", dict(duration_seconds=181 * 60), [], "--max-minutes"),
        ("just ended", dict(is_post_live=True), [], "--force"),
    )

    def test_each_refusal_stops_before_the_database(self):
        for name, changes, _, word in self.CASES:
            with self.subTest(name=name):
                message, _, _, gpu, _ = self.fails(ZOO_URL, "--speakers", "1", meta=_meta(**changes))
                self.assertIn("預檢沒過", message)
                self.assertIn(word, message)
                self.assertFalse(Article.objects.exists())
                self.assertEqual(gpu.submits, [])

    def test_the_refusals_that_have_no_flag_stay_refused_whatever_flags_are_given(self):
        for name in ("live", "upcoming", "short", "not found"):
            changes, flags = next((c, f) for n, c, f, _ in self.CASES if n == name)
            with self.subTest(name=name):
                self.fails(ZOO_URL, "--speakers", "1", "--force", "--allow-unlisted", "--allow-music",
                           "--max-minutes", "9999", meta=_meta(**changes))
                self.assertFalse(Article.objects.exists())

    def test_the_named_flag_lets_each_overridable_one_through(self):
        for changes, flags in ((dict(unlisted=True), ["--allow-unlisted"]),
                               (dict(category="Music"), ["--allow-music"]),
                               (dict(duration_seconds=181 * 60), ["--max-minutes", "200"]),
                               (dict(is_post_live=True), ["--force"])):
            with self.subTest(flags=flags):
                Article.objects.all().delete()
                _, err, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "1", *flags, meta=_meta(**changes))
                self.assertEqual(self.refresh().status, ArticleStatus.READY)

    def test_the_forced_stream_carries_a_warning(self):
        _, err, *_ = self.run_cmd(ZOO_URL, "--speakers", "1", "--force", meta=_meta(is_post_live=True))
        self.assertIn("轉檔", err)

    def test_a_page_that_could_not_be_read_is_a_loud_warning_not_a_silent_pass(self):
        meta = VideoMeta(video_id=ZOO, title="Me at the zoo", channel="jawed",
                         problems=("影片頁連不上（逾時）",))
        _, err, *_ = self.run_cmd(ZOO_URL, "--speakers", "1", meta=meta)
        self.assertIn("沒有讀到影片頁", err)
        self.assertIn("影片頁連不上", err)
        article = self.refresh()
        self.assertEqual(article.date, timezone.localdate())
        self.assertEqual(article.duration_seconds, 0)

    def test_when_youtube_cannot_be_read_at_all_the_command_says_how_to_go_on(self):
        message, *_ = self.fails(ZOO_URL, "--speakers", "1", fetch_error=MetadataUnavailable("oEmbed 連不上"))
        self.assertIn("oEmbed 連不上", message)
        self.assertIn("--title", message)
        self.assertFalse(Article.objects.exists())

    def test_with_title_speaker_and_date_given_it_goes_on_without_reading_anything(self):
        out, err, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "1", "--title", "t", "--speaker", "s",
                                        "--date", "2026-10-30", fetch_error=MetadataUnavailable("連不上"))
        article = self.refresh()
        self.assertEqual((article.status, article.title, article.duration_seconds), (ArticleStatus.READY, "t", 0))
        self.assertIn("連不上", err)
        self.assertEqual((gpu.submits[0]["min_slides"], gpu.submits[0]["max_slides"]), (None, None))


class ExistingVideoTests(CommandTestCase):
    def test_a_published_video_is_left_alone_and_the_command_says_where_it_is(self):
        self.article(status=ArticleStatus.READY)
        out, _, gpu, fetch = self.run_cmd(ZOO_URL, "--speakers", "1")
        self.assertIn(f"已發佈在 /article/2005-04-24-yt-{ZOO}", out)
        self.assertIn("--rerun", out)
        self.assertEqual((gpu.submits, gpu.looked_up), ([], []))
        fetch.assert_not_called()
        self.assertEqual(self.refresh().status, ArticleStatus.READY)

    def test_the_existing_row_is_not_re_read_from_youtube(self):
        """預檢是第一次登記時做的；重跑不該因為後來 YouTube 上的狀態變了而被擋下。"""
        self.article(status=ArticleStatus.FAILED, error="x")
        _, _, _, fetch = self.run_cmd(ZOO_URL, "--speakers", "1")
        fetch.assert_not_called()

    def test_a_rerun_regenerates_while_the_video_stays_online(self):
        self.article(status=ArticleStatus.READY, title="我取的標題", attempts=1)
        seen = []

        class Watching(Gpu):
            def wait(inner, job_id, timeout, poll_seconds=3.0, on_progress=None):
                seen.append(self.refresh().status)
                return super().wait(job_id, timeout, poll_seconds, on_progress)

        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "1", "--rerun", gpu=Watching(result=_payload(slides=3)))
        article = self.refresh()
        self.assertEqual(seen, [ArticleStatus.READY])
        self.assertEqual((article.status, article.slides.count(), article.attempts, article.title),
                         (ArticleStatus.READY, 3, 1, "我取的標題"))
        self.assertIn("留在網站上", out)
        self.assertEqual((gpu.submits[0]["min_slides"], gpu.submits[0]["max_slides"]), (3, 6))

    def test_a_failed_rerun_keeps_the_video_online_and_says_so(self):
        self.article(status=ArticleStatus.READY)
        with self.assertLogs("articles.ingest", level="WARNING"):
            message, *_ = self.fails(ZOO_URL, "--speakers", "1", "--rerun", gpu=Gpu(error=JobFailed("boom")))
        self.assertIn("boom", message)
        self.assertIn("仍然留在網站上", message)
        self.assertEqual(self.refresh().status, ArticleStatus.READY)

    def test_a_failed_video_is_retried_with_the_attempts_reset(self):
        self.article(status=ArticleStatus.FAILED, error="JobFailed: 還沒有逐字稿\n第二行", attempts=3)
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "1")
        article = self.refresh()
        self.assertEqual((article.status, article.attempts, article.error), (ArticleStatus.READY, 1, ""))
        self.assertIn("上一次處理失敗", out)
        self.assertIn("還沒有逐字稿", out)
        self.assertNotIn("第二行", out)
        self.assertEqual(len(gpu.submits), 1)

    def test_a_video_that_used_up_the_automatic_retries_is_run_again_by_a_human(self):
        """MAX_ATTEMPTS 擋的是夜間排程的無限重試；人為重跑不受它限制。"""
        self.article(status=ArticleStatus.FAILED, error="x", attempts=MAX_ATTEMPTS)
        out, *_ = self.run_cmd(ZOO_URL, "--speakers", "1")
        self.assertIn(f"{MAX_ATTEMPTS} 次都用完了", out)
        self.assertEqual(self.refresh().status, ArticleStatus.READY)

    def test_a_registered_but_unprocessed_video_is_processed(self):
        self.article(status=ArticleStatus.PENDING)
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "1")
        self.assertEqual((len(gpu.submits), self.refresh().status), (1, ArticleStatus.READY))

    def test_the_overrides_do_not_rewrite_an_existing_row_and_the_command_says_so(self):
        self.article(status=ArticleStatus.FAILED, error="x")
        _, err, *_ = self.run_cmd(ZOO_URL, "--speakers", "1", "--title", "新標題", "--speaker", "新人")
        article = self.refresh()
        self.assertEqual((article.title, article.speaker), ("Me at the zoo", "jawed"))
        self.assertIn("不會改", err)

    def test_the_slide_overrides_do_apply_to_a_rerun(self):
        self.article(status=ArticleStatus.FAILED, error="x", duration_seconds=3600)
        _, _, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "1", "--min-slides", "4", "--max-slides", "9")
        self.assertEqual((gpu.submits[0]["min_slides"], gpu.submits[0]["max_slides"]), (4, 9))

    def test_a_dry_run_on_an_existing_video_changes_nothing(self):
        for status in (ArticleStatus.READY, ArticleStatus.FAILED, ArticleStatus.PROCESSING):
            with self.subTest(status=status):
                Article.objects.all().delete()
                job = "J" if status == ArticleStatus.PROCESSING else ""
                article = self.article(status=status, attempts=2, gpu_job_id=job)
                out, _, gpu, _ = self.run_cmd(ZOO_URL, "--dry-run", "--rerun")
                after = self.refresh()
                self.assertEqual((after.status, after.attempts, after.gpu_job_id),
                                 (article.status, article.attempts, article.gpu_job_id))
                self.assertEqual((gpu.submits, gpu.looked_up), ([], []))
                self.assertIn("--dry-run", out)


class InterruptedRunTests(CommandTestCase):
    """SSH 斷線、容器重開：row 停在處理中，GPU 那邊的工作可能還在跑、或已經做完。"""

    def processing(self, minutes_ago, job="J", **fields):
        article = self.article(status=ArticleStatus.PROCESSING, gpu_job_id=job, attempts=1, **fields)
        Article.objects.filter(pk=article.pk).update(updated_at=timezone.now() - timedelta(minutes=minutes_ago))
        return article

    def test_a_job_that_finished_on_the_gpu_is_picked_up_without_waiting_for_the_stale_time(self):
        """管理頁的「重新排隊」會把 gpu_job_id 清掉，幾十分鐘的成品就丟了；這個指令先問 GPU。"""
        self.processing(minutes_ago=5)
        gpu = Gpu(jobs={"J": {"status": "done"}})
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "1", gpu=gpu)
        article = self.refresh()
        self.assertEqual((article.status, article.attempts, article.gpu_job_id), (ArticleStatus.READY, 1, ""))
        self.assertEqual((gpu.submits, gpu.waits), ([], [("J", 1800)]))
        self.assertIn("接回去", out)

    def test_a_job_that_is_still_running_is_picked_up_too(self):
        self.processing(minutes_ago=5)
        gpu = Gpu(jobs={"J": {"status": "running", "started_at": time.time() - 60}})
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "1", gpu=gpu)
        self.assertEqual((gpu.submits, self.refresh().status), ([], ArticleStatus.READY))

    def test_a_job_that_is_queued_on_the_gpu_is_picked_up_too(self):
        self.processing(minutes_ago=5)
        gpu = Gpu(jobs={"J": {"status": "queued", "created_at": time.time() - 60}})
        self.run_cmd(ZOO_URL, "--speakers", "1", gpu=gpu)
        self.assertEqual((gpu.submits, self.refresh().status), ([], ArticleStatus.READY))

    def test_a_job_the_gpu_no_longer_knows_is_sent_again_with_the_attempts_reset(self):
        self.processing(minutes_ago=5)
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "1", gpu=Gpu(jobs={}))
        self.assertEqual(len(gpu.submits), 1)
        self.assertEqual((self.refresh().status, self.refresh().attempts), (ArticleStatus.READY, 1))
        self.assertIn("不認得", out)

    def test_a_job_that_failed_on_the_gpu_is_sent_again(self):
        self.processing(minutes_ago=5)
        gpu = Gpu(jobs={"J": {"status": "failed", "error": "壞了"}})
        self.run_cmd(ZOO_URL, "--speakers", "1", gpu=gpu)
        self.assertEqual((len(gpu.submits), self.refresh().status), (1, ArticleStatus.READY))

    def test_a_gpu_that_cannot_answer_is_not_guessed_about(self):
        self.processing(minutes_ago=5)
        message, *_ = self.fails(ZOO_URL, "--speakers", "1", gpu=Gpu(job_error=GpuApiError("連線被拒")))
        self.assertIn("連線被拒", message)
        self.assertEqual(self.refresh().status, ArticleStatus.PROCESSING)

    def test_somebody_else_is_still_running_it_and_the_command_says_when_it_counts_as_stuck(self):
        self.article(status=ArticleStatus.PROCESSING, attempts=1)
        Article.objects.filter(ivod_id=f"yt-{ZOO}").update(updated_at=timezone.now() - timedelta(minutes=10))
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "1")
        # 2 × 1800 秒 = 60 分鐘，已經 10 分鐘 → 還要 50 分鐘
        self.assertIn("另一個行程正在處理", out)
        self.assertIn("約還要 50 分鐘", out)
        self.assertEqual((gpu.submits, gpu.looked_up), ([], []))
        self.assertEqual(self.refresh().status, ArticleStatus.PROCESSING)

    def test_a_run_that_stopped_long_ago_is_taken_over(self):
        self.article(status=ArticleStatus.PROCESSING, attempts=1)
        Article.objects.filter(ivod_id=f"yt-{ZOO}").update(updated_at=timezone.now() - timedelta(minutes=70))
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "1")
        self.assertIn("處理中斷了", out)
        self.assertEqual((len(gpu.submits), self.refresh().status), (1, ArticleStatus.READY))

    def test_a_failure_while_resuming_is_a_failed_video_that_can_be_tried_again(self):
        self.processing(minutes_ago=5)
        gpu = Gpu(jobs={"J": {"status": "done"}}, error=JobFailed("結果壞了"))
        with self.assertLogs("articles.ingest", level="WARNING"):
            message, *_ = self.fails(ZOO_URL, "--speakers", "1", gpu=gpu)
        self.assertIn("結果壞了", message)
        self.assertEqual(self.refresh().status, ArticleStatus.FAILED)


class RemoveVideoCommandTests(CommandTestCase):
    def published(self, video_id=ZOO):
        self.run_cmd(f"https://www.youtube.com/watch?v={video_id}", "--speakers", "1",
                     meta=_meta(video_id=video_id))
        return Article.objects.get(ivod_id=f"yt-{video_id}")

    def remove(self, *args):
        out, err = io.StringIO(), io.StringIO()
        call_command("remove_video", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def removal_fails(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with self.assertRaises(CommandError) as caught:
            call_command("remove_video", *args, stdout=out, stderr=err)
        return str(caught.exception), out.getvalue(), err.getvalue()

    def test_dry_run_first_then_for_real_by_any_of_the_three_names(self):
        article = self.published()
        names = [s.image.name for s in article.slides.all()]
        for target in (ZOO_URL, f"https://youtu.be/{ZOO}?t=3", article.slug, f"yt-{ZOO}", ZOO):
            with self.subTest(target=target):
                out, _ = self.remove(target, "--dry-run")
                self.assertTrue(out.splitlines()[0].startswith(f"影片 {ZOO}"))
                self.assertIn("會刪除", out)
                self.assertIn(f"/article/{article.slug}", out)
                self.assertTrue(Article.objects.filter(pk=article.pk).exists())
                self.assertTrue(all(default_storage.exists(n) for n in names))
        out, _ = self.remove(article.slug)
        self.assertIn("已刪除", out)
        self.assertIn("Cloudflare", out)
        self.assertFalse(Article.objects.exists())
        self.assertFalse(any(default_storage.exists(n) for n in names))
        self.assertFalse(default_storage.exists(f"articles/yt-{ZOO}"))

    def test_the_other_videos_files_are_left_alone(self):
        self.published()
        other = self.published(STREAM)
        keep = [s.image.name for s in other.slides.all()]
        self.remove(ZOO_URL)
        self.assertTrue(Article.objects.filter(ivod_id=f"yt-{STREAM}").exists())
        self.assertTrue(all(default_storage.exists(n) for n in keep))

    def test_running_it_twice_is_safe(self):
        self.published()
        self.remove(ZOO_URL)
        out, _ = self.remove(ZOO_URL)
        self.assertIn("沒有這支影片", out)
        self.assertIn("沒有截圖檔案要刪", out)
        self.assertNotIn("已刪除", out)

    def test_a_removal_that_was_cut_short_is_finished_by_running_it_again(self):
        article = self.published()
        names = [s.image.name for s in article.slides.all()]
        Article.objects.filter(pk=article.pk).delete()
        self.remove(ZOO_URL)
        self.assertFalse(any(default_storage.exists(n) for n in names))

    def test_orphan_files_from_old_attempts_are_cleaned_too(self):
        self.published()
        orphan = default_storage.save(f"articles/yt-{ZOO}/deadbeef/01.webp", ContentFile(IMAGE))
        self.remove(ZOO_URL)
        self.assertFalse(default_storage.exists(orphan))

    def test_things_that_are_not_a_video_are_refused_without_touching_anything(self):
        council = Article.objects.create(ivod_id="900001", slug="2026-08-27-900001", title="t", speaker="範例一",
                                         date=date(2026, 8, 27), ivod_url="https://x/y")
        for target in ("2026-08-27-900001", "900001", "https://ivod.ly.gov.tw/Play/Clip/1M/171180",
                       "https://www.youtube.com/@KC-Huang", "../../etc", "yt-../..", ""):
            with self.subTest(target=target):
                message, *_ = self.removal_fails(target)
                self.assertIn("看不出要下架哪一支影片", message)
        self.assertTrue(Article.objects.filter(pk=council.pk).exists())

    def test_a_row_with_a_video_id_but_another_source_is_never_deleted(self):
        impostor = Article.objects.create(ivod_id=f"yt-{ZOO}", slug="2026-08-27-x", source="ly", title="t",
                                          speaker="範例一", date=date(2026, 8, 27), ivod_url="https://x/y")
        message, *_ = self.removal_fails(ZOO_URL)
        self.assertIn("不是 YouTube 影片", message)
        self.assertTrue(Article.objects.filter(pk=impostor.pk).exists())

    def test_a_video_that_is_being_processed_right_now_cannot_be_removed(self):
        """處理完成時 save_result 會把文章寫回來；現在刪只是白刪。"""
        self.article(status=ArticleStatus.PROCESSING)
        message, *_ = self.removal_fails(ZOO_URL)
        self.assertIn("還在處理中", message)
        self.assertTrue(Article.objects.exists())
        Article.objects.update(updated_at=timezone.now() - timedelta(minutes=70))
        out, _ = self.remove(ZOO_URL)
        self.assertFalse(Article.objects.exists())

    def test_files_that_could_not_be_deleted_make_the_command_fail_but_everything_else_goes(self):
        article = self.published()
        names = [s.image.name for s in article.slides.all()]
        real = type(default_storage._wrapped).delete

        def flaky(storage, name):
            if name == names[0]:
                raise PermissionError("唯讀")
            return real(storage, name)

        with mock.patch.object(type(default_storage._wrapped), "delete", flaky), \
                self.assertLogs("articles.ingest", level="WARNING"):
            message, _, err = self.removal_fails(ZOO_URL)
        self.assertIn("再執行一次", message)
        self.assertIn(names[0], err)
        self.assertFalse(Article.objects.exists())

    def test_a_video_with_a_dash_id_can_be_named_with_or_without_the_double_dash(self):
        self.published("-abcdefghij")
        out, _ = self.remove("-abcdefghij", "--dry-run")
        self.assertIn("會刪除", out)
        out, _ = self.remove("--", "-abcdefghij")
        self.assertIn("已刪除", out)
        self.assertFalse(Article.objects.exists())
