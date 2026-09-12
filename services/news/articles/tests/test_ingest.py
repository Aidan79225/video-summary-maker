"""匯入流程：用假的來源與假的 GPU 客戶端，不碰網路。"""
from __future__ import annotations

import base64
import tempfile
from datetime import date

from django.test import TestCase, override_settings

from articles.gpu_client import GpuApiError, JobFailed
from articles.ingest import (
    discover,
    imageless,
    ingest_day,
    process_pending,
    retry_imageless,
    save_result,
)
from articles.ivod_source import IvodClip, IvodUnavailable
from articles.models import FAILED, PENDING, READY, Article

IMAGE = b"\x00\x01fake-webp\xff"
MEDIA = tempfile.mkdtemp(prefix="news_media_")


def _clip(ivod_id="171180", speaker="洪毓祥"):
    return IvodClip(
        ivod_id=ivod_id, date="2026-08-27", speaker=speaker,
        meeting="第11屆第5會期第23次會議", duration_seconds=197,
        ivod_url=f"https://ivod.ly.gov.tw/Play/Clip/1M/{ivod_id}",
        has_transcript=True,
    )


def _payload(slides=2, with_image=True):
    return {
        "video_id": "171180",
        "title": "2026-08-27 洪毓祥－第11屆第5會期第23次會議",
        "source_note": "逐字稿由立法院 AI 自動產生，可能有辨識錯誤",
        "transcript_text": "00:00 主席 各位同仁",
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
    def __init__(self, result=None, error=None):
        self._result = result if result is not None else _payload()
        self._error = error
        self.submitted = []

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
        article = Article.objects.get(ivod_id="171180")
        self.assertEqual(article.status, PENDING)
        self.assertEqual(article.slug, "2026-08-27-171180")
        self.assertEqual(article.speaker, "洪毓祥")

    def test_running_twice_does_not_duplicate(self):
        discover(date(2026, 8, 27), FakeSource())
        found, created = discover(date(2026, 8, 27), FakeSource())
        self.assertEqual((found, created), (1, 0))
        self.assertEqual(Article.objects.count(), 1)

    def test_a_finished_article_is_not_knocked_back_to_pending(self):
        """攔的 bug：每次排程都 update 一遍，已完成的文章會被打回待處理，
        於是每天重做一次同樣的事、也重花一次 GPU 時間。"""
        discover(date(2026, 8, 27), FakeSource())
        article = Article.objects.get(ivod_id="171180")
        save_result(article, _payload())
        discover(date(2026, 8, 27), FakeSource())
        self.assertEqual(Article.objects.get(ivod_id="171180").status, READY)


@override_settings(MEDIA_ROOT=MEDIA)
class ProcessTests(TestCase):
    def setUp(self):
        discover(date(2026, 8, 27), FakeSource())

    def test_a_processed_article_gets_its_slides_and_images(self):
        report = process_pending(FakeClient(), limit=10, timeout=60)
        self.assertEqual(report.processed, 1)
        article = Article.objects.get(ivod_id="171180")
        self.assertEqual(article.status, READY)
        self.assertEqual(article.slides.count(), 2)
        first = article.slides.first()
        self.assertEqual(first.bullets, ["重點 1"])
        self.assertIn("完整敘述", first.detail)
        self.assertTrue(first.image)
        self.assertEqual(first.image.read(), IMAGE)
        self.assertTrue(article.published_at)

    def test_the_detailed_mode_is_always_requested(self):
        """使用者要的是「有詳細內容的摘要」——條列在新聞頁上太單薄。"""
        client = FakeClient()
        process_pending(client, limit=10, timeout=60)
        self.assertEqual(client.submitted[0][1], True)

    def test_a_page_without_a_screenshot_is_still_saved(self):
        process_pending(FakeClient(result=_payload(with_image=False)), limit=10,
                        timeout=60)
        article = Article.objects.get(ivod_id="171180")
        self.assertEqual(article.slides.count(), 2)
        self.assertFalse(article.slides.first().image)

    def test_a_failed_job_keeps_the_reason_and_can_be_retried_later(self):
        # assertLogs 同時確認失敗有被記到日誌，並讓測試輸出保持乾淨
        with self.assertLogs("articles.ingest", level="WARNING"):
            report = process_pending(FakeClient(error=JobFailed("還沒有逐字稿")),
                                     limit=10, timeout=60)
        self.assertEqual(report.failed, 1)
        article = Article.objects.get(ivod_id="171180")
        self.assertEqual(article.status, FAILED)
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
        self.assertEqual(Article.objects.filter(status=PENDING).count(), 3)

    def test_reprocessing_replaces_the_old_slides_instead_of_piling_up(self):
        process_pending(FakeClient(), limit=10, timeout=60)
        article = Article.objects.get(ivod_id="171180")
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
        self.assertEqual(Article.objects.get(ivod_id="171180").status, READY)

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
        article = Article.objects.get(ivod_id="171180")
        self.assertTrue(article.slides.first().image)

    def test_an_article_that_already_has_screenshots_is_left_alone(self):
        """攔的 bug：把有圖的也重跑，每篇白花幾分鐘 GPU 時間。"""
        process_pending(FakeClient(), limit=10, timeout=60)
        client = FakeClient()
        self.assertEqual(retry_imageless(client, limit=10, timeout=60).processed, 0)
        self.assertEqual(client.submitted, [])

    def test_unfinished_articles_are_not_swept_up_by_the_retry(self):
        self.assertEqual(list(imageless(10)), [])
