"""YouTube 影片在 ingest 裡的幾件事：登記、點名處理、頁數範圍與語音辨識提示、標題保留、下架。

假的 GPU 客戶端，不碰網路。指令（add_video／remove_video）在 test_add_video.py。
"""
from __future__ import annotations

import base64
import inspect
import shutil
import tempfile
from datetime import date
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, default_storage
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from articles.gpu_client import GpuApiClient, GpuApiError, JobFailed
from articles.ingest import (
    ArticleDeleted,
    IngestReport,
    process_articles,
    register_video,
    remove_video,
    save_result,
    speech_hint_for,
    video_media_folder,
)
from articles.models import Article, ArticleStatus, Slide

IMAGE = b"\x00\x01fake-webp\xff"
ZOO, STREAM = "jNQXAC9IVRw", "f4r2yWk4LV0"


def _payload(title="GPU 從影片拿到的標題", slides=2):
    return {
        "video_id": ZOO, "title": title, "source_note": "使用 YouTube 自動字幕，品質可能較差",
        "transcript_text": "00:00 大家好", "brief": {"one_liner": "一句話", "key_numbers": [], "asks": []},
        "slides": [{"index": i, "title": f"第 {i} 段", "bullets": [f"重點 {i}"], "detail": f"第 {i} 段的敘述",
                    "timestamp": float(i * 10), "image_base64": base64.b64encode(IMAGE).decode()}
                   for i in range(1, slides + 1)],
    }


class RecordingClient:
    """記下 submit 收到的每一個參數；其餘同 test_ingest.FakeClient。"""

    def __init__(self, result=None, error=None, known_jobs=None):
        self._result = result if result is not None else _payload()
        self._error = error
        self._known_jobs = known_jobs or {}
        self.submits: list[dict] = []
        self.waits: list[tuple[str, float]] = []

    def job(self, job_id):
        return self._known_jobs.get(job_id)

    def cancel(self, job_id):
        self._known_jobs.pop(job_id, None)

    def submit(self, url, detailed=True, min_slides=None, max_slides=None, speech_hint=None,
               speaker_mode=None, speaker_count=None):
        self.submits.append({"url": url, "detailed": detailed, "min_slides": min_slides,
                             "max_slides": max_slides, "speech_hint": speech_hint,
                             "speaker_mode": speaker_mode, "speaker_count": speaker_count})
        if isinstance(self._error, GpuApiError):
            raise self._error
        return f"job-{len(self.submits)}"

    def wait(self, job_id, timeout, poll_seconds=3.0, on_progress=None):
        self.waits.append((job_id, timeout))
        if self._error is not None:
            raise self._error
        return self._result


class ShapeClient:
    """只記下 submit 是用哪些關鍵字呼叫的：議會的呼叫形狀不能因為影片的功能而變。"""

    def __init__(self):
        self.kwargs: list[dict] = []

    def job(self, job_id):
        return None

    def cancel(self, job_id):
        pass

    def submit(self, url, **kwargs):
        self.kwargs.append(kwargs)
        return "job-1"

    def wait(self, job_id, timeout, poll_seconds=3.0, on_progress=None):
        return _payload()


def _video(video_id=ZOO, **kw):
    """登記一支影片。這個檔案測的是第一部分的單人流程：既有的影片都是 --speakers 1 做的（遷移 0015 也這樣填），
    所以預設 speaker_count=1；多人流程在 test_speaker_ingest.py。"""
    kw = {"title": "Me at the zoo", "speaker": "jawed", "day": date(2005, 4, 24), "duration_seconds": 19,
          "speaker_count": 1} | kw
    return register_video(video_id, **kw)[0]


class IsolatedMedia(TestCase):
    """每個測試自己的截圖資料夾：資料庫會跟著測試回滾，檔案不會——共用的話，上一個測試留下的
    檔案會讓「資料夾空了」「刪了幾個檔」這種斷言對不上。"""

    def setUp(self):
        super().setUp()
        media = tempfile.mkdtemp(prefix="news_video_media_")
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        self.enterContext(override_settings(MEDIA_ROOT=media))


class ClientContractTests(SimpleTestCase):
    def test_the_recording_fake_offers_every_method_ingest_uses(self):
        """跟 test_ingest 的同名測試一樣：假客戶端不能跟真的分頭演化。"""
        for name in ("submit", "wait", "job", "cancel"):
            with self.subTest(method=name):
                self.assertEqual(list(inspect.signature(getattr(RecordingClient, name)).parameters),
                                 list(inspect.signature(getattr(GpuApiClient, name)).parameters))


class RegisterVideoTests(IsolatedMedia):
    def test_a_new_video_is_a_pending_youtube_article_with_the_canonical_url(self):
        article, created = register_video(ZOO, title="Me at the zoo", speaker="jawed",
                                          day=date(2005, 4, 24), duration_seconds=19)
        self.assertTrue(created)
        article.refresh_from_db()
        self.assertEqual((article.ivod_id, article.source, article.slug, article.status),
                         (f"yt-{ZOO}", "youtube", f"2005-04-24-yt-{ZOO}", ArticleStatus.PENDING))
        self.assertEqual(article.ivod_url, f"https://www.youtube.com/watch?v={ZOO}")
        self.assertEqual((article.title, article.speaker, article.date, article.duration_seconds),
                         ("Me at the zoo", "jawed", date(2005, 4, 24), 19))
        self.assertEqual((article.meeting, article.party, article.membership, article.session, article.speakers),
                         ("", "", None, None, []))
        self.assertEqual((article.attempts, article.error, article.gpu_job_id), (0, "", ""))

    def test_it_never_attaches_a_session_or_links_a_member(self):
        """攔的 bug：走議會的登記路徑會對頻道名稱找任期、對標題找會期。"""
        with mock.patch("articles.ingest.attach_session") as attach:
            with mock.patch("articles.ingest.link_article") as link:
                register_video(ZOO, title="第4屆第8次定期會 市政總質詢 直播", speaker="王立", day=date(2026, 10, 30))
        attach.assert_not_called()
        link.assert_not_called()
        self.assertEqual(Article.objects.get(ivod_id=f"yt-{ZOO}").meeting, "")

    def test_registering_again_changes_nothing(self):
        first = _video()
        again, created = register_video(ZOO, title="別的標題", speaker="別人", day=date(2030, 1, 1),
                                        duration_seconds=999)
        self.assertFalse(created)
        self.assertEqual(again.pk, first.pk)
        again.refresh_from_db()
        self.assertEqual((again.title, again.speaker, again.date, again.duration_seconds),
                         ("Me at the zoo", "jawed", date(2005, 4, 24), 19))
        self.assertEqual(Article.objects.count(), 1)

    def test_a_finished_video_is_not_knocked_back_to_pending(self):
        article = _video()
        save_result(article, _payload())
        register_video(ZOO, title="x", speaker="y", day=date(2030, 1, 1))
        self.assertEqual(Article.objects.get(ivod_id=f"yt-{ZOO}").status, ArticleStatus.READY)

    def test_the_channel_name_cannot_split_into_two_speakers_and_everything_fits_the_columns(self):
        article = _video(speaker="甲、乙頻道", title="題" * 400)
        self.assertEqual(article.speaker, "甲・乙頻道")
        self.assertEqual(len(article.title), 300)
        self.assertEqual(len(_video("abcdefghijk", speaker="長" * 400).speaker), 300)

    def test_an_id_that_could_not_be_a_video_is_refused_before_anything_is_written(self):
        for bad in ("short", "../../etc/pass", "abcdefghijkl", "videoseries", "", "yt-abcdefghi"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                register_video(bad, title="t", speaker="s", day=date(2026, 10, 30))
        self.assertFalse(Article.objects.exists())

    def test_an_id_that_starts_with_a_dash_is_fine(self):
        article = _video("-abcdefghij")
        self.assertEqual((article.ivod_id, article.slug), ("yt--abcdefghij", "2005-04-24-yt--abcdefghij"))

    def test_a_negative_length_is_stored_as_unknown(self):
        self.assertEqual(_video(duration_seconds=-5).duration_seconds, 0)

    def test_what_the_uploader_wrote_is_cleaned_before_it_is_stored(self):
        """標題與頻道名稱是上傳者寫的：控制字元、格式字元（含雙向覆寫）與換行都不進資料庫。"""
        article = _video(title="Line1\nLine2\x1b[2J‮\x00", speaker="Chan\x00nel\t‮、Two\n名")
        article.refresh_from_db()
        self.assertEqual((article.title, article.speaker), ("Line1 Line2[2J", "Channel ・Two 名"))


class ProcessArticlesTests(IsolatedMedia):
    def test_a_registered_video_is_claimed_processed_and_published(self):
        article = _video()
        client = RecordingClient()
        report = process_articles(article, client, timeout=60)
        self.assertEqual((report.processed, report.failed, report.skipped), (1, 0, 0))
        article.refresh_from_db()
        self.assertEqual((article.status, article.attempts, article.gpu_job_id), (ArticleStatus.READY, 1, ""))
        self.assertTrue(article.published_at)
        self.assertEqual(article.slides.count(), 2)
        self.assertEqual(client.waits, [("job-1", 60)])

    def test_a_list_and_a_queryset_work_like_a_single_article(self):
        first, second = _video(), _video("abcdefghijk")
        self.assertEqual(process_articles([first], RecordingClient(), timeout=60).processed, 1)
        queryset = Article.objects.filter(pk=second.pk)
        self.assertEqual(process_articles(queryset, RecordingClient(), timeout=60).processed, 1)

    def test_the_slides_are_stored_under_the_videos_own_folder(self):
        article = _video()
        process_articles(article, RecordingClient(), timeout=60)
        for slide in article.slides.all():
            self.assertRegex(slide.image.name, rf"^articles/yt-{ZOO}/[0-9a-f]{{8}}/\d{{2}}\.webp$")

    def test_the_gpu_gets_a_page_range_that_fits_the_length(self):
        for seconds, expected in ((19, (3, 6)), (5715, (8, 48)), (0, (None, None))):
            with self.subTest(seconds=seconds):
                client = RecordingClient()
                process_articles(_video(f"x{seconds:010d}", duration_seconds=seconds), client, timeout=60)
                self.assertEqual((client.submits[0]["min_slides"], client.submits[0]["max_slides"]), expected)

    def test_the_operators_range_wins(self):
        client = RecordingClient()
        process_articles(_video(), client, timeout=60, slides=(2, 4))
        self.assertEqual((client.submits[0]["min_slides"], client.submits[0]["max_slides"]), (2, 4))

    def test_the_video_is_submitted_by_its_canonical_url_in_detailed_mode(self):
        client = RecordingClient()
        process_articles(_video(), client, timeout=60)
        self.assertEqual((client.submits[0]["url"], client.submits[0]["detailed"]),
                         (f"https://www.youtube.com/watch?v={ZOO}", True))

    def test_the_hint_is_the_channel_and_the_title_cut_to_200_characters(self):
        client = RecordingClient()
        article = _video(speaker="黃國昌", title="【國昌直播】來看看我上週在做什麼_1019")
        process_articles(article, client, timeout=60)
        self.assertEqual(client.submits[0]["speech_hint"], "黃國昌。【國昌直播】來看看我上週在做什麼_1019")
        long = _video("abcdefghijk", speaker="頻" * 150, title="題" * 150)
        self.assertEqual(len(speech_hint_for(long)), 200)
        self.assertEqual(speech_hint_for(long), ("頻" * 150 + "。" + "題" * 150)[:200])

    def test_the_hint_is_the_same_on_every_retry(self):
        """提示一變，GPU 上已存的逐字稿就失效、要重新辨識。"""
        article = _video()
        hints = []
        for _ in range(2):
            client = RecordingClient(error=GpuApiError("連線被拒"))
            with self.assertLogs("articles.ingest", level="WARNING"):
                process_articles(Article.objects.get(pk=article.pk), client, timeout=60)
            hints.append(client.submits[0]["speech_hint"])
            Article.objects.filter(pk=article.pk).update(status=ArticleStatus.PENDING)
        self.assertEqual(hints[0], hints[1])

    def test_the_council_call_to_the_gpu_keeps_its_old_shape(self):
        """攔的 bug：影片的頁數範圍連議會的 submit 也一起帶上，議會的文章每次都被限制頁數。"""
        Article.objects.create(ivod_id="900001", slug="2026-08-27-900001", title="t", speaker="範例一",
                               meeting="第11屆第5會期第23次會議", date=date(2026, 8, 27),
                               duration_seconds=197, ivod_url="https://ivod.ly.gov.tw/Play/Clip/1M/900001")
        client = ShapeClient()
        process_articles(Article.objects.filter(ivod_id="900001"), client, timeout=60)
        self.assertEqual(list(client.kwargs[0]), ["detailed", "speech_hint"])
        self.assertEqual(client.kwargs[0]["speech_hint"], "立法院 第11屆第5會期第23次會議。發言者：範例一")

    def test_a_failed_first_run_is_a_failed_article_with_the_reason(self):
        article = _video()
        with self.assertLogs("articles.ingest", level="WARNING"):
            report = process_articles(article, RecordingClient(error=JobFailed("影片已下架")), timeout=60)
        article.refresh_from_db()
        self.assertEqual((report.failed, article.status), (1, ArticleStatus.FAILED))
        self.assertIn("影片已下架", article.error)

    def test_a_rerun_keeps_the_video_online_and_does_not_spend_an_attempt(self):
        article = _video()
        process_articles(article, RecordingClient(), timeout=60)
        article.refresh_from_db()
        with self.assertLogs("articles.ingest", level="WARNING"):
            report = process_articles(article, RecordingClient(error=JobFailed("boom")), timeout=60,
                                      demote_on_failure=False, claim=False)
        article.refresh_from_db()
        self.assertEqual((report.failed, article.status, article.attempts), (1, ArticleStatus.READY, 1))
        self.assertIn("boom", article.error)

    def test_a_rerun_replaces_the_slides(self):
        article = _video()
        process_articles(article, RecordingClient(), timeout=60)
        process_articles(Article.objects.get(pk=article.pk), RecordingClient(result=_payload(slides=3)),
                         timeout=60, demote_on_failure=False, claim=False)
        self.assertEqual(Article.objects.get(pk=article.pk).slides.count(), 3)

    def test_an_article_somebody_else_is_processing_is_skipped(self):
        article = _video()
        Article.objects.filter(pk=article.pk).update(status=ArticleStatus.PROCESSING)
        client = RecordingClient()
        report = process_articles(Article.objects.get(pk=article.pk), client, timeout=60)
        self.assertEqual((report.skipped, report.processed, client.submits), (1, 0, []))

    def test_an_unfinished_job_is_picked_back_up(self):
        article = _video()
        Article.objects.filter(pk=article.pk).update(gpu_job_id="J")
        client = RecordingClient(known_jobs={"J": {"status": "running"}})
        process_articles(Article.objects.get(pk=article.pk), client, timeout=60)
        self.assertEqual((client.submits, client.waits), ([], [("J", 60)]))


class RegisteredTitleTests(IsolatedMedia):
    def test_the_registered_title_survives_processing(self):
        """使用者用 --title 蓋掉的標題，不會被 GPU 的標題換回去。"""
        article = _video(title="我自己取的標題")
        save_result(article, _payload(title="GPU 從影片拿到的標題"))
        article.refresh_from_db()
        self.assertEqual(article.title, "我自己取的標題")

    def test_the_placeholder_is_replaced_by_the_title_the_gpu_found(self):
        article = _video(title=f"YouTube 影片 {ZOO}")
        save_result(article, _payload(title="GPU 從影片拿到的標題"))
        article.refresh_from_db()
        self.assertEqual(article.title, "GPU 從影片拿到的標題")

    def test_the_title_the_gpu_found_is_cleaned_like_any_title_an_uploader_wrote(self):
        article = _video(title=f"YouTube 影片 {ZOO}")
        save_result(article, _payload(title="GPU\x1b[2J 標題‮\n第二行"))
        article.refresh_from_db()
        self.assertEqual(article.title, "GPU[2J 標題 第二行")
        # 清完什麼都不剩就留著佔位標題
        other = _video("abcdefghijk", title=f"YouTube 影片 abcdefghijk")
        save_result(other, _payload(title="\x1b\x00"))
        other.refresh_from_db()
        self.assertEqual(other.title, "YouTube 影片 abcdefghijk")

    def test_the_placeholder_of_another_video_is_a_real_title(self):
        article = _video(title=f"YouTube 影片 {STREAM}")
        save_result(article, _payload(title="GPU 的標題"))
        article.refresh_from_db()
        self.assertEqual(article.title, f"YouTube 影片 {STREAM}")

    def test_an_empty_gpu_title_keeps_the_placeholder(self):
        article = _video(title=f"YouTube 影片 {ZOO}")
        save_result(article, _payload(title=""))
        article.refresh_from_db()
        self.assertEqual(article.title, f"YouTube 影片 {ZOO}")

    def test_the_councils_still_take_the_gpu_title_and_new_taipei_keeps_its_own(self):
        council = Article.objects.create(ivod_id="900009", slug="2026-08-27-900009", title="舊標題",
                                         speaker="範例一", date=date(2026, 8, 27), ivod_url="https://x/y")
        save_result(council, _payload(title="新標題"))
        ntpc = Article.objects.create(ivod_id="ntpc-1", slug="2026-08-27-ntpc-1", source="ntpc", title="Pi 的標題",
                                      speaker="範例一", date=date(2026, 8, 27), ivod_url="https://x/z")
        save_result(ntpc, _payload(title="GPU 的標題"))
        self.assertEqual((Article.objects.get(pk=council.pk).title, Article.objects.get(pk=ntpc.pk).title),
                         ("新標題", "Pi 的標題"))


class RemoveVideoTests(IsolatedMedia):
    def _published(self, video_id=ZOO):
        article = _video(video_id)
        process_articles(article, RecordingClient(), timeout=60)
        return Article.objects.get(pk=article.pk)

    def _exists(self, name):
        return default_storage.exists(name)

    def test_the_article_its_slides_and_all_of_its_files_go(self):
        article = self._published()
        names = [slide.image.name for slide in article.slides.all()]
        self.assertEqual(len(names), 2)
        report = remove_video(f"yt-{ZOO}")
        self.assertEqual((report.existed, report.slides, report.errors, report.strays), (True, 2, [], []))
        self.assertEqual(sorted(report.files), sorted(names))
        self.assertFalse(Article.objects.filter(ivod_id=f"yt-{ZOO}").exists())
        self.assertFalse(Slide.objects.filter(article_id=article.pk).exists())
        for name in names:
            self.assertFalse(self._exists(name))
        self.assertFalse(self._exists(video_media_folder(f"yt-{ZOO}")))

    def test_orphans_left_by_failed_retries_go_too_and_other_files_stay(self):
        """每次重跑寫進新的子資料夾；失敗的重試留下的檔案不在資料庫裡，但在同一個資料夾底下。"""
        article = self._published()
        orphan = default_storage.save(f"articles/yt-{ZOO}/deadbeef/01.webp", ContentFile(IMAGE))
        other = self._published("abcdefghijk")
        other_names = [s.image.name for s in other.slides.all()]
        council_file = default_storage.save("articles/900001/aaaaaaaa/01.webp",
                                            ContentFile(IMAGE))
        remove_video(f"yt-{ZOO}")
        self.assertFalse(self._exists(orphan))
        self.assertFalse(self._exists(f"articles/yt-{ZOO}"))
        for name in other_names + [council_file]:
            self.assertTrue(self._exists(name), name)
        self.assertTrue(Article.objects.filter(ivod_id="yt-abcdefghijk").exists())
        self.assertTrue(article.pk)

    def test_a_dry_run_changes_nothing_and_says_what_it_would_do(self):
        article = self._published()
        names = [slide.image.name for slide in article.slides.all()]
        report = remove_video(f"yt-{ZOO}", dry_run=True)
        self.assertEqual((report.existed, report.slides, sorted(report.files)), (True, 2, sorted(names)))
        self.assertTrue(Article.objects.filter(ivod_id=f"yt-{ZOO}").exists())
        for name in names:
            self.assertTrue(self._exists(name))

    def test_running_it_twice_is_safe(self):
        self._published()
        remove_video(f"yt-{ZOO}")
        again = remove_video(f"yt-{ZOO}")
        self.assertEqual((again.existed, again.slides, again.files, again.errors), (False, 0, [], []))

    def test_a_video_that_never_got_any_files_is_removed_too(self):
        _video()
        report = remove_video(f"yt-{ZOO}")
        self.assertEqual((report.existed, report.files), (True, []))
        self.assertFalse(Article.objects.exists())

    def test_the_cleanup_finishes_a_removal_that_was_cut_short(self):
        """文章刪了、檔案沒刪完（例如中途斷電）：再跑一次就收尾。"""
        article = self._published()
        names = [slide.image.name for slide in article.slides.all()]
        Article.objects.filter(pk=article.pk).delete()
        self.assertTrue(all(self._exists(name) for name in names))
        report = remove_video(f"yt-{ZOO}")
        self.assertEqual((report.existed, sorted(report.files)), (False, sorted(names)))
        self.assertFalse(any(self._exists(name) for name in names))

    def test_only_a_well_formed_id_can_reach_the_file_system(self):
        for bad in ("yt-../../x", "yt-short", "yt-abcdefghijkl", "900001", "", "yt-", "../articles",
                    "yt-ab/cdefghi.."):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                remove_video(bad)

    def test_an_article_that_is_not_a_video_is_never_touched(self):
        council = Article.objects.create(ivod_id=f"yt-{STREAM}", slug="2026-08-27-x", source="ly", title="t",
                                         speaker="範例一", date=date(2026, 8, 27), ivod_url="https://x/y")
        with self.assertRaises(ValueError):
            remove_video(f"yt-{STREAM}")
        self.assertTrue(Article.objects.filter(pk=council.pk).exists())

    def test_a_file_name_that_points_elsewhere_is_reported_not_deleted(self):
        article = self._published()
        survivor = default_storage.save("articles/900001/aaaaaaaa/01.webp",
                                        ContentFile(IMAGE))
        Slide.objects.filter(article=article, index=1).update(image=survivor)
        report = remove_video(f"yt-{ZOO}")
        self.assertEqual(report.strays, [survivor])
        self.assertTrue(self._exists(survivor))
        self.assertFalse(Article.objects.filter(ivod_id=f"yt-{ZOO}").exists())

    def test_a_file_that_cannot_be_deleted_is_reported_and_the_rest_still_go(self):
        article = self._published()
        names = [slide.image.name for slide in article.slides.all()]
        real_delete = FileSystemStorage.delete

        def flaky(storage, name):
            if name == names[0]:
                raise PermissionError("唯讀")
            return real_delete(storage, name)

        with mock.patch.object(FileSystemStorage, "delete", flaky), \
                self.assertLogs("articles.ingest", level="WARNING"):
            report = remove_video(f"yt-{ZOO}")
        # 那個檔案，與因此刪不掉的兩層資料夾（批次資料夾、影片資料夾）
        self.assertEqual(len(report.errors), 3)
        self.assertIn(names[0], report.errors[0])
        self.assertFalse(Article.objects.filter(ivod_id=f"yt-{ZOO}").exists())
        self.assertFalse(self._exists(names[1]))
        # 再跑一次就收尾
        again = remove_video(f"yt-{ZOO}")
        self.assertEqual((again.errors, self._exists(names[0])), ([], False))

    def test_an_id_that_starts_with_a_dash_works(self):
        article = _video("-abcdefghij")
        process_articles(article, RecordingClient(), timeout=60)
        report = remove_video("yt--abcdefghij")
        self.assertEqual(len(report.files), 2)
        self.assertFalse(self._exists("articles/yt--abcdefghij"))


class Removing(RecordingClient):
    """影片被下架（remove_video 的底層）：at="wait" 是 GPU 還在做的時候，at="submit" 是剛送出、
    Pi 還沒來得及記下工作 id 的那一瞬間。"""

    def __init__(self, *args, at="wait", **kwargs):
        super().__init__(*args, **kwargs)
        self.at = at

    def submit(self, *args, **kwargs):
        if self.at == "submit":
            remove_video(f"yt-{ZOO}")
        return super().submit(*args, **kwargs)

    def wait(self, job_id, timeout, poll_seconds=3.0, on_progress=None):
        if self.at == "wait":
            remove_video(f"yt-{ZOO}")
        return super().wait(job_id, timeout, poll_seconds, on_progress)


class DeletedWhileProcessingTests(IsolatedMedia):
    """影片在 GPU 還在做的時候被下架：結果要丟掉。Model.save() 在列不見時會 INSERT 同一個 pk，
    沒有這道檢查的話，下架之後影片又自己回到網站上。"""

    EXISTS = 'SELECT 1 AS "a" FROM "articles_article" WHERE "articles_article"."id" = '

    def _gone(self, article):
        Article.objects.filter(pk=article.pk).delete()

    def _nothing_came_back(self):
        self.assertFalse(Article.objects.exists())
        self.assertFalse(Slide.objects.exists())
        self.assertFalse(default_storage.exists(f"articles/yt-{ZOO}"))

    def test_a_result_for_a_deleted_video_is_refused_before_anything_is_written(self):
        article = _video()
        self._gone(article)
        with self.assertRaises(ArticleDeleted):
            save_result(article, _payload())
        self._nothing_came_back()

    def test_the_other_videos_are_not_touched_by_the_refusal(self):
        keep = _video("abcdefghijk")
        save_result(keep, _payload())
        article = _video()
        self._gone(article)
        with self.assertRaises(ArticleDeleted):
            save_result(article, _payload(slides=3))
        self.assertEqual((Article.objects.count(), Slide.objects.count()), (1, 2))

    def test_the_check_is_one_cheap_lookup_for_a_video_and_nothing_at_all_for_a_council_article(self):
        """議會的 save_result 逐位元組不變：不多一個查詢。"""
        video = _video()
        with CaptureQueriesContext(connection) as queries:
            save_result(video, _payload())
        self.assertEqual(len([q for q in queries if q["sql"].startswith(self.EXISTS)]), 1)
        council = Article.objects.create(ivod_id="900001", slug="2026-08-27-900001", title="t", speaker="範例一",
                                         meeting="第11屆第5會期第23次會議", date=date(2026, 8, 27),
                                         ivod_url="https://ivod.ly.gov.tw/Play/Clip/1M/900001")
        with CaptureQueriesContext(connection) as queries:
            save_result(council, _payload())
        self.assertEqual([q for q in queries if q["sql"].startswith(self.EXISTS)], [])

    def test_a_video_removed_while_the_gpu_works_is_reported_as_removed(self):
        article = _video()
        with self.assertLogs("articles.ingest", level="WARNING") as logs:
            report = process_articles(Article.objects.get(pk=article.pk), Removing(), timeout=60)
        self.assertEqual((report.removed, report.processed, report.failed, report.skipped), (1, 0, 0, 0))
        self.assertIn(f"yt-{ZOO}", report.errors[0])
        self.assertIn("已下架 1", str(report))
        self.assertFalse([r for r in logs.records if r.exc_info])
        self._nothing_came_back()

    def test_a_rerun_removed_meanwhile_does_not_come_back_either(self):
        article = _video()
        process_articles(article, RecordingClient(), timeout=60)
        with self.assertLogs("articles.ingest", level="WARNING"):
            report = process_articles(Article.objects.get(pk=article.pk), Removing(result=_payload(slides=4)),
                                      timeout=60, demote_on_failure=False, claim=False)
        self.assertEqual((report.removed, report.processed), (1, 0))
        self._nothing_came_back()

    def test_a_video_removed_just_after_the_job_was_submitted_is_a_removal_without_a_traceback(self):
        """送出之後、記下工作 id 之前被下架：UPDATE 沒有列可以更新（DatabaseError）。"""
        article = _video()
        client = Removing(at="submit")
        with self.assertLogs("articles.ingest", level="WARNING") as logs:
            report = process_articles(Article.objects.get(pk=article.pk), client, timeout=60)
        self.assertEqual((report.removed, report.processed, report.failed), (1, 0, 0))
        self.assertFalse([r for r in logs.records if r.exc_info])
        self.assertEqual(client.waits, [])                 # 沒有為一支已經不在的影片等 GPU
        self._nothing_came_back()

    def test_a_failure_for_a_row_that_is_gone_is_a_removal_not_a_crash(self):
        """_record_failure 以前對不見的列 save() 會再丟 DatabaseError，整個批次跟著掛掉。"""
        for error, at in ((JobFailed("影片已下架"), "wait"), (GpuApiError("連線被拒"), "submit"),
                          (RuntimeError("怪事"), "wait")):
            with self.subTest(error=type(error).__name__):
                Article.objects.all().delete()
                article = _video()
                with self.assertLogs("articles.ingest", level="WARNING"):
                    report = process_articles(Article.objects.get(pk=article.pk), Removing(error=error, at=at),
                                              timeout=60)
                self.assertEqual((report.removed, report.failed, report.processed), (1, 0, 0))
                self._nothing_came_back()

    def test_a_failure_with_a_row_that_is_there_is_recorded_as_before(self):
        article = _video()
        with self.assertLogs("articles.ingest", level="WARNING"):
            report = process_articles(article, RecordingClient(error=JobFailed("影片已下架")), timeout=60)
        article.refresh_from_db()
        self.assertEqual((report.removed, report.failed, article.status), (0, 1, ArticleStatus.FAILED))
        self.assertEqual(str(IngestReport()), "發現 0、新增 0、完成 0、失敗 0、略過 0")

    def test_a_database_error_that_is_not_a_missing_row_still_surfaces(self):
        from django.db import DatabaseError

        article = _video()
        with mock.patch.object(Article, "save", side_effect=DatabaseError("database is locked")):
            with self.assertLogs("articles.ingest", level="WARNING"), self.assertRaises(DatabaseError):
                process_articles(article, RecordingClient(error=JobFailed("x")), timeout=60, claim=False)

    def test_a_claim_that_finds_no_row_leaves_the_article_alone(self):
        article = _video()
        self._gone(article)
        client = RecordingClient()
        report = process_articles(article, client, timeout=60)
        self.assertEqual((report.skipped, client.submits), (1, []))
        self._nothing_came_back()
