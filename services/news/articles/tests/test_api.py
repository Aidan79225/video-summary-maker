"""news API：Astro 前端讀的就是這些端點。"""
from __future__ import annotations

import base64
import tempfile
from datetime import date

from django.test import TestCase, override_settings

from articles.ingest import save_result
from articles.models import FAILED, PENDING, Article

MEDIA = tempfile.mkdtemp(prefix="news_api_media_")
IMAGE = b"\x00\x01fake-webp\xff"


def _article(ivod_id="171180", speaker="洪毓祥", day="2026-08-27", status=None,
             slides=2, with_image=True):
    article = Article.objects.create(
        ivod_id=ivod_id,
        slug=f"{day}-{ivod_id}",
        title=f"{day} {speaker}－第11屆第5會期第23次會議",
        speaker=speaker,
        meeting="第11屆第5會期第23次會議",
        date=date.fromisoformat(day),
        duration_seconds=197,
        ivod_url=f"https://ivod.ly.gov.tw/Play/Clip/1M/{ivod_id}",
    )
    if status in (PENDING, FAILED):
        article.status = status
        article.save()
        return article
    save_result(article, {
        "title": article.title,
        "source_note": "逐字稿由立法院 AI 自動產生，可能有辨識錯誤",
        "transcript_text": "00:00 主席 各位同仁",
        "slides": [{
            "index": i,
            "title": f"第 {i} 段：國防自主",
            "bullets": [f"重點 {i}"],
            "detail": f"第 {i} 段的完整敘述，講的是無人載具條例。",
            "timestamp": float(i * 30),
            "image_base64": base64.b64encode(IMAGE).decode() if with_image else None,
        } for i in range(1, slides + 1)],
    })
    return article


@override_settings(MEDIA_ROOT=MEDIA)
class ApiTests(TestCase):
    def test_health_works_on_an_empty_database(self):
        body = self.client.get("/api/health").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["articles"], 0)
        self.assertIsNone(body["latest_date"])

    def test_the_listing_returns_cards(self):
        _article()
        body = self.client.get("/api/articles").json()
        self.assertEqual(body["count"], 1)
        card = body["items"][0]
        self.assertEqual(card["speaker"], "洪毓祥")
        self.assertEqual(card["slug"], "2026-08-27-171180")
        self.assertEqual(card["slide_count"], 2)
        self.assertIn("完整敘述", card["teaser"])
        self.assertTrue(card["cover_image_url"].startswith("/media/"))

    def test_unfinished_articles_are_not_news(self):
        """攔的 bug：處理中或失敗的文章是內部狀態，露到前端就會出現
        沒有內容的空白新聞。"""
        _article(ivod_id="1", status=PENDING)
        _article(ivod_id="2", status=FAILED)
        self.assertEqual(self.client.get("/api/articles").json()["count"], 0)

    def test_filtering_by_date_and_speaker_and_text(self):
        _article(ivod_id="1", speaker="洪毓祥", day="2026-08-27")
        _article(ivod_id="2", speaker="徐巧芯", day="2026-08-26")
        cases = [
            ("?date=2026-08-26", 1),
            ("?speaker=洪毓祥", 1),
            ("?q=無人載具", 2),
            ("?q=完全沒有這個字串", 0),
        ]
        for query, expected in cases:
            with self.subTest(query=query):
                body = self.client.get(f"/api/articles{query}").json()
                self.assertEqual(body["count"], expected)

    def test_a_text_search_does_not_return_the_same_article_twice(self):
        """攔的 bug：跨 join 的 OR 查詢每命中一個段落就多一列，同一篇文章
        會在清單上出現好幾次。"""
        _article(slides=3)
        body = self.client.get("/api/articles?q=完整敘述").json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(len(body["items"]), 1)

    def test_pagination_reports_enough_to_build_a_pager(self):
        for i in range(5):
            _article(ivod_id=str(i), day="2026-08-27")
        body = self.client.get("/api/articles?page=2&page_size=2").json()
        self.assertEqual((body["count"], body["page"], body["pages"]), (5, 2, 3))
        self.assertEqual(len(body["items"]), 2)

    def test_an_absurd_page_size_cannot_be_used_to_dump_everything(self):
        for i in range(3):
            _article(ivod_id=str(i))
        body = self.client.get("/api/articles?page_size=100000").json()
        self.assertLessEqual(body["page_size"], 50)

    def test_the_detail_page_has_what_an_article_needs(self):
        _article()
        body = self.client.get("/api/articles/2026-08-27-171180").json()
        self.assertEqual(len(body["slides"]), 2)
        slide = body["slides"][0]
        self.assertEqual(slide["index"], 1)
        self.assertEqual(slide["bullets"], ["重點 1"])
        self.assertIn("無人載具", slide["detail"])
        self.assertTrue(slide["image_url"].startswith("/media/"))
        self.assertIn("AI", body["source_note"])
        self.assertIn("主席", body["transcript_text"])

    def test_an_unknown_article_is_404(self):
        self.assertEqual(self.client.get("/api/articles/沒這篇").status_code, 404)

    def test_an_unfinished_article_is_404_rather_than_half_a_page(self):
        _article(ivod_id="1", status=PENDING)
        self.assertEqual(
            self.client.get("/api/articles/2026-08-27-1").status_code, 404)

    def test_a_page_without_a_screenshot_still_renders(self):
        _article(with_image=False)
        body = self.client.get("/api/articles/2026-08-27-171180").json()
        self.assertIsNone(body["slides"][0]["image_url"])
        self.assertIsNone(body["cover_image_url"])

    def test_speakers_are_listed_with_counts(self):
        _article(ivod_id="1", speaker="洪毓祥", day="2026-08-27")
        _article(ivod_id="2", speaker="洪毓祥", day="2026-08-26")
        _article(ivod_id="3", speaker="徐巧芯", day="2026-08-25")
        items = self.client.get("/api/speakers").json()["items"]
        by_name = {i["name"]: i for i in items}
        self.assertEqual(by_name["洪毓祥"]["count"], 2)
        self.assertEqual(by_name["洪毓祥"]["latest_date"], "2026-08-27")
        self.assertEqual(by_name["徐巧芯"]["count"], 1)

    def test_the_newest_article_comes_first(self):
        _article(ivod_id="1", day="2026-08-25")
        _article(ivod_id="2", day="2026-08-27")
        items = self.client.get("/api/articles").json()["items"]
        self.assertEqual(items[0]["ivod_id"], "2")
