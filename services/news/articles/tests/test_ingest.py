"""匯入流程：用假的來源與假的 GPU 客戶端，不碰網路。"""
from __future__ import annotations

import atexit
import base64
import time
import inspect
import shutil
import tempfile
from datetime import date

from django.core.files.storage import default_storage
from django.test import SimpleTestCase, TestCase, override_settings

from articles.gpu_client import GpuApiClient, GpuApiError, JobFailed
from articles.ingest import (
    clean_brief,
    discover,
    discover_days,
    imageless,
    ingest_day,
    process_pending,
    retry_imageless,
    save_result,
)
from articles.ivod_source import IvodClip, IvodUnavailable
from articles.models import Article, ArticleStatus

IMAGE = b"\x00\x01fake-webp\xff"
MEDIA = tempfile.mkdtemp(prefix="news_media_")
atexit.register(shutil.rmtree, MEDIA, ignore_errors=True)


def _clip(ivod_id="900001", speaker="範例一"):
    return IvodClip(
        ivod_id=ivod_id, date="2026-08-27", speaker=speaker,
        meeting="第11屆第5會期第23次會議", duration_seconds=197,
        ivod_url=f"https://ivod.ly.gov.tw/Play/Clip/1M/{ivod_id}",
        has_transcript=True,
    )


BRIEF = {
    "one_liner": "國防部三年編 82.4 億買無人機，交到部隊的不到一半",
    "key_numbers": [
        {"value": "82.4", "unit": "億元", "label": "三年累計編列", "quote": "累計編列八十二點四億元"},
    ],
    "asks": [{"request": "提出交機時程清冊", "deadline": "一個月內", "response": "部長允諾"}],
}


def _payload(slides=2, with_image=True, brief=BRIEF):
    return {
        "video_id": "900001",
        "title": "2026-08-27 範例一－第11屆第5會期第23次會議",
        "source_note": "逐字稿由立法院 AI 自動產生，可能有辨識錯誤",
        "transcript_text": "00:00 主席 各位同仁",
        "brief": brief,
        "slides": [{
            "index": i,
            "title": f"第 {i} 段",
            "bullets": [f"重點 {i}"],
            "detail": f"第 {i} 段的完整敘述",
            "timestamp": float(i * 30),
            "image_base64": base64.b64encode(IMAGE).decode() if with_image else None,
        } for i in range(1, slides + 1)],
    }


class FakeSource:
    def __init__(self, clips=None, error=None):
        self._clips = clips if clips is not None else [_clip()]
        self._error = error

    def clips_for(self, day, only_with_transcript=True):
        if self._error is not None:
            raise self._error
        return list(self._clips)


class FakeClient:
    def __init__(self, result=None, error=None, known_jobs=None):
        self._result = result if result is not None else _payload()
        self._error = error
        self._known_jobs = known_jobs or {}
        self.submitted = []
        self.cancelled = []

    def job(self, job_id):
        return self._known_jobs.get(job_id)

    def cancel(self, job_id):
        self.cancelled.append(job_id)
        self._known_jobs.pop(job_id, None)

    def submit(self, url, detailed=True, min_slides=None, max_slides=None):
        self.submitted.append((url, detailed))
        if isinstance(self._error, GpuApiError):
            raise self._error
        return "job-1"

    def wait(self, job_id, timeout, poll_seconds=3.0, on_progress=None):
        if self._error is not None:
            raise self._error
        return self._result


@override_settings(MEDIA_ROOT=MEDIA)
class DiscoverTests(TestCase):
    def test_a_clip_becomes_a_pending_article(self):
        found, created = discover(date(2026, 8, 27), FakeSource())
        self.assertEqual((found, created), (1, 1))
        article = Article.objects.get(ivod_id="900001")
        self.assertEqual(article.status, ArticleStatus.PENDING)
        self.assertEqual(article.slug, "2026-08-27-900001")
        self.assertEqual(article.speaker, "範例一")

    def test_running_twice_does_not_duplicate(self):
        discover(date(2026, 8, 27), FakeSource())
        found, created = discover(date(2026, 8, 27), FakeSource())
        self.assertEqual((found, created), (1, 0))
        self.assertEqual(Article.objects.count(), 1)

    def test_a_finished_article_is_not_knocked_back_to_pending(self):
        """攔的 bug：每次排程都 update 一遍，已完成的文章會被打回待處理，
        於是每天重做一次同樣的事、也重花一次 GPU 時間。"""
        discover(date(2026, 8, 27), FakeSource())
        article = Article.objects.get(ivod_id="900001")
        save_result(article, _payload())
        discover(date(2026, 8, 27), FakeSource())
        self.assertEqual(Article.objects.get(ivod_id="900001").status, ArticleStatus.READY)


@override_settings(MEDIA_ROOT=MEDIA)
class ProcessTests(TestCase):
    def setUp(self):
        discover(date(2026, 8, 27), FakeSource())

    def test_a_processed_article_gets_its_slides_and_images(self):
        report = process_pending(FakeClient(), limit=10, timeout=60)
        self.assertEqual(report.processed, 1)
        article = Article.objects.get(ivod_id="900001")
        self.assertEqual(article.status, ArticleStatus.READY)
        self.assertEqual(article.slides.count(), 2)
        first = article.slides.first()
        self.assertEqual(first.bullets, ["重點 1"])
        self.assertIn("完整敘述", first.detail)
        self.assertTrue(first.image)
        self.assertEqual(first.image.read(), IMAGE)
        self.assertTrue(article.published_at)

    def test_the_brief_is_stored_with_the_article(self):
        process_pending(FakeClient(), limit=10, timeout=60)
        article = Article.objects.get(ivod_id="900001")
        self.assertEqual(article.brief["one_liner"], BRIEF["one_liner"])
        self.assertEqual(article.brief["key_numbers"][0]["value"], "82.4")
        self.assertEqual(article.brief["asks"][0]["deadline"], "一個月內")
        self.assertEqual(article.teaser, BRIEF["one_liner"])

    def test_an_article_without_a_brief_falls_back_to_the_first_detail(self):
        """GPU 端產不出卡片時是 null，導言退回第一段的完整敘述。"""
        process_pending(FakeClient(result=_payload(brief=None)), limit=10, timeout=60)
        article = Article.objects.get(ivod_id="900001")
        self.assertIsNone(article.brief)
        self.assertIn("完整敘述", article.teaser)

    def test_the_detailed_mode_is_always_requested(self):
        """使用者要的是「有詳細內容的摘要」——條列在新聞頁上太單薄。"""
        client = FakeClient()
        process_pending(client, limit=10, timeout=60)
        self.assertEqual(client.submitted[0][1], True)

    def test_a_page_without_a_screenshot_is_still_saved(self):
        process_pending(FakeClient(result=_payload(with_image=False)), limit=10,
                        timeout=60)
        article = Article.objects.get(ivod_id="900001")
        self.assertEqual(article.slides.count(), 2)
        self.assertFalse(article.slides.first().image)

    def test_a_failed_job_keeps_the_reason_and_can_be_retried_later(self):
        # assertLogs 同時確認失敗有被記到日誌，並讓測試輸出保持乾淨
        with self.assertLogs("articles.ingest", level="WARNING"):
            report = process_pending(FakeClient(error=JobFailed("還沒有逐字稿")),
                                     limit=10, timeout=60)
        self.assertEqual(report.failed, 1)
        article = Article.objects.get(ivod_id="900001")
        self.assertEqual(article.status, ArticleStatus.FAILED)
        self.assertIn("逐字稿", article.error)
        # 失敗的下一輪還會被撿起來——立法院的逐字稿有時晚幾小時才出現
        report = process_pending(FakeClient(), limit=10, timeout=60)
        self.assertEqual(report.processed, 1)

    def test_a_broken_gpu_service_stops_the_batch_instead_of_burning_it(self):
        """攔的 bug：GPU 主機關機時，一整天的 20 段會在幾秒內全部變成失敗，
        而原因對每一篇都一樣。停下來，下一輪再試。"""
        for i in range(2, 5):
            Article.objects.create(
                ivod_id=str(i), slug=f"s{i}", title="t", speaker="s",
                date=date(2026, 8, 27), ivod_url="https://ivod/x")
        client = FakeClient(error=GpuApiError("連線被拒"))
        with self.assertLogs("articles.ingest", level="WARNING"):
            report = process_pending(client, limit=10, timeout=60)
        self.assertEqual(report.failed, 1)
        self.assertEqual(len(client.submitted), 1)
        self.assertEqual(Article.objects.filter(status=ArticleStatus.PENDING).count(), 3)

    def test_reprocessing_replaces_the_old_slides_instead_of_piling_up(self):
        process_pending(FakeClient(), limit=10, timeout=60)
        article = Article.objects.get(ivod_id="900001")
        save_result(article, _payload(slides=3))
        self.assertEqual(article.slides.count(), 3)

    def test_the_limit_is_respected(self):
        for i in range(2, 6):
            Article.objects.create(
                ivod_id=str(i), slug=f"s{i}", title="t", speaker="s",
                date=date(2026, 8, 27), ivod_url="https://ivod/x")
        client = FakeClient()
        process_pending(client, limit=2, timeout=60)
        self.assertEqual(len(client.submitted), 2)


@override_settings(MEDIA_ROOT=MEDIA)
class IngestDayTests(TestCase):
    def test_the_whole_day_runs_end_to_end(self):
        report = ingest_day(date(2026, 8, 27), FakeSource(), FakeClient(),
                            limit=10, timeout=60)
        self.assertEqual((report.discovered, report.created, report.processed),
                         (1, 1, 0 + 1))
        self.assertEqual(Article.objects.get(ivod_id="900001").status, ArticleStatus.READY)

    def test_an_unreachable_legislature_api_does_not_crash_the_run(self):
        """Pi 半夜跑排程，立法院那邊偶爾就是連不上。下一輪會再試。"""
        with self.assertLogs("articles.ingest", level="WARNING"):
            report = ingest_day(date(2026, 8, 27),
                                FakeSource(error=IvodUnavailable("逾時")),
                                FakeClient(), limit=10, timeout=60)
        self.assertEqual(report.discovered, 0)
        self.assertTrue(report.errors)


@override_settings(MEDIA_ROOT=MEDIA)
class RetryImagelessTests(TestCase):
    """立法院的影片 CDN 會間歇性回 5xx：摘要產得出來，畫面全缺。"""

    def setUp(self):
        discover(date(2026, 8, 27), FakeSource())

    def test_an_article_with_no_screenshots_at_all_can_be_re_run(self):
        process_pending(FakeClient(result=_payload(with_image=False)), limit=10,
                        timeout=60)
        client = FakeClient()
        report = retry_imageless(client, limit=10, timeout=60)
        self.assertEqual(report.processed, 1)
        article = Article.objects.get(ivod_id="900001")
        self.assertTrue(article.slides.first().image)

    def test_an_article_that_already_has_screenshots_is_left_alone(self):
        """攔的 bug：把有圖的也重跑，每篇白花幾分鐘 GPU 時間。"""
        process_pending(FakeClient(), limit=10, timeout=60)
        client = FakeClient()
        self.assertEqual(retry_imageless(client, limit=10, timeout=60).processed, 0)
        self.assertEqual(client.submitted, [])

    def test_unfinished_articles_are_not_swept_up_by_the_retry(self):
        self.assertEqual(list(imageless(10)), [])


@override_settings(MEDIA_ROOT=MEDIA)
class SaveResultDurabilityTests(TestCase):
    """攔的 bug：在交易裡刪檔案。檔案系統不會跟著 DB 回滾。"""

    def setUp(self):
        discover(date(2026, 8, 27), FakeSource())
        self.article = Article.objects.get(ivod_id="900001")
        save_result(self.article, _payload())

    def _files(self):
        return [s.image.name for s in self.article.slides.all() if s.image]

    def test_a_failed_rewrite_leaves_the_old_article_completely_intact(self):
        """回滾之後不該留下「欄位有值、檔案不存在」的破圖——那種文章
        imageless() 撿不到（欄位非空）、process_pending 也碰不到（狀態還是
        READY），沒有任何路徑會修好它。"""
        before = self._files()
        self.assertTrue(before)

        broken = _payload(slides=2)
        broken["slides"][1]["timestamp"] = "不是數字"   # 上游給了壞資料
        with self.assertRaises(ValueError):
            save_result(self.article, broken)

        self.article.refresh_from_db()
        self.assertEqual(self._files(), before)
        for name in before:
            with self.subTest(name=name):
                self.assertTrue(default_storage.exists(name), f"{name} 不見了")

    def test_a_successful_rewrite_cleans_up_the_old_files(self):
        """反過來：成功時舊檔案要真的被刪掉，不能在 SD 卡上越積越多。

        刪除掛在 transaction.on_commit 上，而 TestCase 把每個測試包在會回滾
        的交易裡——所以要用 captureOnCommitCallbacks 才跑得到，這也正好說明
        了「只有提交成功才刪」這個機制。
        """
        before = self._files()
        with self.captureOnCommitCallbacks(execute=True):
            save_result(self.article, _payload(slides=3))
        after = self._files()
        self.assertNotEqual(set(before), set(after))
        for name in before:
            with self.subTest(name=name):
                self.assertFalse(default_storage.exists(name))
        for name in after:
            self.assertTrue(default_storage.exists(name))

    def test_rewriting_does_not_accumulate_random_suffixes(self):
        """攔的 bug：Django 的 storage 從不覆寫，撞名會加隨機後綴。"""
        save_result(self.article, _payload())
        for name in self._files():
            self.assertRegex(name, r"articles/900001/[0-9a-f]{8}/\d{2}\.webp$")



class ClientContractTests(SimpleTestCase):
    """攔的 bug：測試用的假客戶端跟真的分頭演化。

    真實 client 多了一個方法，ingest 開始呼叫它，而所有 ingest 測試仍然
    全綠——因為它們用的是那個舊的假物件。這種漂移只有簽章比對抓得到。
    """

    def test_the_fake_offers_every_method_ingest_uses(self):
        for name in ("submit", "wait", "job", "cancel"):
            with self.subTest(method=name):
                self.assertTrue(hasattr(FakeClient, name))
                self.assertEqual(
                    list(inspect.signature(getattr(FakeClient, name)).parameters),
                    list(inspect.signature(getattr(GpuApiClient, name)).parameters),
                )


@override_settings(MEDIA_ROOT=MEDIA)
class ResumeTests(TestCase):
    """斷線之後接回 GPU 上的工作。"""

    def setUp(self):
        discover(date(2026, 8, 27), FakeSource())
        self.article = Article.objects.get(ivod_id="900001")

    def _run(self, client):
        return process_pending(client, limit=10, timeout=60)

    def test_an_unfinished_job_is_picked_back_up_instead_of_resubmitted(self):
        """等待途中 Pi 斷線幾十秒，GPU 那邊其實還在跑。重送等於把幾分鐘的
        成品丟掉、整支影片再跑一遍。"""
        self.article.gpu_job_id = "J"
        self.article.save(update_fields=["gpu_job_id"])
        client = FakeClient(known_jobs={"J": {"status": "running"}})
        self._run(client)
        self.assertEqual(client.submitted, [])

    def test_a_failed_job_is_not_picked_back_up(self):
        """攔的 bug：只檢查「這個 id 還在不在」就接回去，會每天重讀同一個
        失敗結論、從來不重送——重試機制靜悄悄地整個失效，而 attempts 照樣
        每天加一，幾天後永久放棄。"""
        self.article.gpu_job_id = "J"
        self.article.save(update_fields=["gpu_job_id"])
        client = FakeClient(known_jobs={"J": {"status": "failed", "error": "壞了"}})
        self._run(client)
        self.assertEqual(len(client.submitted), 1)

    def test_a_finished_job_keeps_its_reason_but_drops_the_id(self):
        """失敗之後那個 id 不該留著，否則下一輪又會接回去。"""
        with self.assertLogs("articles.ingest", level="WARNING"):
            self._run(FakeClient(error=JobFailed("還沒有逐字稿")))
        self.article.refresh_from_db()
        self.assertEqual(self.article.gpu_job_id, "")

    def test_a_job_stuck_for_far_too_long_is_cancelled_and_resubmitted(self):
        """GPU 某一步假死時，接回去只是每天白等一輪完整的逾時。"""
        self.article.gpu_job_id = "J"
        self.article.save(update_fields=["gpu_job_id"])
        client = FakeClient(known_jobs={
            "J": {"status": "running", "started_at": time.time() - 10_000}})
        self._run(client)
        self.assertEqual(client.cancelled, ["J"])
        self.assertEqual(len(client.submitted), 1)


@override_settings(MEDIA_ROOT=MEDIA)
class UpstreamOutageTests(TestCase):
    def test_the_backlog_is_still_processed_when_the_legislature_api_is_down(self):
        """攔的 bug：discover 失敗就直接 return，跳過整個 process_pending。
        兩者的上游不同——立法院掛掉時 GPU 沒有理由整夜閒著。"""
        discover(date(2026, 8, 27), FakeSource())
        client = FakeClient()
        with self.assertLogs("articles.ingest", level="WARNING"):
            report = ingest_day(date(2026, 8, 28),
                                FakeSource(error=IvodUnavailable("逾時")),
                                client, limit=10, timeout=60)
        self.assertEqual(len(client.submitted), 1)
        self.assertEqual(report.processed, 1)

    def test_one_bad_day_does_not_stop_the_other_days(self):
        class FlakySource:
            def __init__(self):
                self.calls = 0

            def clips_for(self, day, only_with_transcript=True):
                self.calls += 1
                if self.calls == 1:
                    raise IvodUnavailable("逾時")
                return [_clip()]

        source = FlakySource()
        with self.assertLogs("articles.ingest", level="WARNING"):
            report = discover_days([date(2026, 8, 27), date(2026, 8, 26)], source)
        self.assertEqual(report.created, 1)
        self.assertTrue(report.errors)


@override_settings(MEDIA_ROOT=MEDIA)
class StuckJobTests(TestCase):
    def setUp(self):
        discover(date(2026, 8, 27), FakeSource())
        self.article = Article.objects.get(ivod_id="900001")
        self.article.gpu_job_id = "J"
        self.article.save(update_fields=["gpu_job_id"])

    def test_a_job_stuck_in_the_queue_is_also_cancelled_and_resubmitted(self):
        """攔的 bug：只認 RUNNING 的話，被取消後重送的那個會一直是 QUEUED
        ——之後每天都「接回」一個永遠排不到的工作，而且再也不會被判定卡住。"""
        client = FakeClient(known_jobs={
            "J": {"status": "queued", "created_at": time.time() - 10_000}})
        process_pending(client, limit=10, timeout=60)
        self.assertEqual(client.cancelled, ["J"])
        self.assertEqual(len(client.submitted), 1)

    def test_a_job_that_just_started_is_left_alone(self):
        client = FakeClient(known_jobs={
            "J": {"status": "queued", "created_at": time.time()}})
        process_pending(client, limit=10, timeout=60)
        self.assertEqual(client.cancelled, [])
        self.assertEqual(client.submitted, [])


class CleanBriefTests(SimpleTestCase):
    """GPU 回傳的卡片形狀在落地時釘死一次，前端就不必再逐欄防禦。"""

    def test_a_well_formed_brief_passes_through_trimmed(self):
        out = clean_brief({
            "one_liner": " 一句話 ",
            "key_numbers": [{"value": " 82.4 ", "unit": "億元", "label": "編列", "quote": "q"}],
            "asks": [{"request": "清冊", "deadline": "", "response": ""}],
        })
        self.assertEqual(out["one_liner"], "一句話")
        self.assertEqual(out["key_numbers"][0]["value"], "82.4")
        self.assertEqual(out["asks"], [{"request": "清冊", "deadline": "", "response": ""}])

    def test_junk_is_dropped_rather_than_stored(self):
        out = clean_brief({
            "one_liner": "一句話",
            "key_numbers": [{"value": "", "label": "沒數字"}, "x", {"value": "5", "label": ""}],
            "asks": [{"request": ""}, None, {"request": "留下", "deadline": 3}],
        })
        self.assertEqual(out["key_numbers"], [])
        self.assertEqual(out["asks"], [{"request": "留下", "deadline": "", "response": ""}])

    def test_no_one_liner_means_no_brief(self):
        self.assertIsNone(clean_brief(None))
        self.assertIsNone(clean_brief("不是物件"))
        self.assertIsNone(clean_brief({"one_liner": "  ", "key_numbers": [{"value": "1", "label": "x"}]}))
