"""議會文章的 API 回應：多人影片的功能不能讓議會（與單人影片）的任何一個既有欄位、任何一個字動到。

這個檔案刻意只用「多人功能之前就存在」的模型欄位與 API 欄位，所以在還沒有這個功能的程式碼（origin/master）上也要通過：
  * 回應裡既有的每個鍵、每個值逐字比對（手寫的預期值，不從程式算）；
  * 新增的只能是中性預設值（speakers: []、diarization: null、每頁 points: []）——在舊程式碼上這些鍵根本不存在，也算通過；
  * 文字裡剛好有 [[S1]] 這樣的字（議會的逐字稿不會有，但單人影片的標題可能有人這樣寫）原封不動，不被當成說話者記號換掉；
  * 查詢次數跟舊程式碼一樣（Pi 的 SD 卡不能因為這個功能多讀一次）。
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import date

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from articles.models import Article, ArticleStatus, Slide

# 舊程式碼（沒有多人功能）回應裡本來就沒有、新程式碼加上去的鍵，與它們必須有的中性值
NEUTRAL_ARTICLE_KEYS = {"speakers": [], "diarization": None}
NEUTRAL_SLIDE_KEYS = {"points": []}

# 以 origin/master 量到的查詢次數：文章清單（兩篇）與單篇
LIST_QUERIES = 3
DETAIL_QUERIES = 2

# 舊程式碼（還沒有多人功能）的 Article 沒有 speakers 欄位：只依賴它的那一個測試在舊程式碼上跳過
HAS_SPEAKERS = any(field.name == "speakers" for field in Article._meta.get_fields())

PLAIN = "跟說話者無關的一句話。"
LOOKS_LIKE_A_MARKER = "[[S1]]：看起來像說話者記號的字。 [[S2]] 還有 [[S0]]"


def without_neutral(data: dict) -> dict:
    """拿掉新功能加的中性預設值（確認它們真的是中性的），剩下的就是舊程式碼會回的樣子。"""
    data = dict(data)
    for key, neutral in NEUTRAL_ARTICLE_KEYS.items():
        if key in data:
            assert data.pop(key) == neutral, f"{key} 不是中性預設值"
    slides = []
    for slide in data.get("slides", []):
        slide = dict(slide)
        for key, neutral in NEUTRAL_SLIDE_KEYS.items():
            if key in slide:
                assert slide.pop(key) == neutral, f"slides.{key} 不是中性預設值"
        slides.append(slide)
    if "slides" in data:
        data["slides"] = slides
    return data


class CouncilGoldenTests(TestCase):
    def setUp(self):
        media = tempfile.mkdtemp(prefix="news_golden_")
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        self.enterContext(override_settings(MEDIA_ROOT=media))
        brief = {"one_liner": "質詢要求補助", "key_numbers": [], "asks": []}
        self.with_brief = Article.objects.create(
            ivod_id="900001", slug="2026-08-27-900001", source="ly", title="補助質詢", speaker="王委員",
            meeting="第 1 次會議", date=date(2026, 8, 27), duration_seconds=600, ivod_url="https://ivod.example/1",
            source_note="逐字稿取自官方", transcript_text="00:00 王委員：" + LOOKS_LIKE_A_MARKER, brief=brief,
            status=ArticleStatus.READY)
        Slide.objects.create(article=self.with_brief, index=1, title="第一段 [[S1]]",
                             bullets=["[[S1]]：重點一", PLAIN], detail="[[S1]]：完整敘述 " + PLAIN, timestamp=1.5)
        self.no_brief = Article.objects.create(
            ivod_id="tccc-7", slug="2026-08-26-tccc-7", source="tccc", title="教育質詢", speaker="李議員",
            meeting="定期會", date=date(2026, 8, 26), duration_seconds=300, ivod_url="https://tccc.example/7",
            transcript_text=LOOKS_LIKE_A_MARKER, status=ArticleStatus.READY)
        Slide.objects.create(article=self.no_brief, index=1, title="只有一段", bullets=["[[S2]]：條列"],
                             detail=LOOKS_LIKE_A_MARKER + "。" + "長" * 200, timestamp=0.0)
        Slide.objects.create(article=self.no_brief, index=2, title="第二段", bullets=[], detail="", timestamp=9.0)

    def test_the_detail_of_an_article_with_a_brief_is_exactly_what_it_always_was(self):
        data = without_neutral(self.client.get("/api/articles/2026-08-27-900001").json())
        self.assertEqual(data, {
            "slug": "2026-08-27-900001", "ivod_id": "900001", "source": "ly", "party": "",
            "title": "補助質詢", "speaker": "王委員", "meeting": "第 1 次會議", "date": "2026-08-27",
            "duration_seconds": 600, "ivod_url": "https://ivod.example/1", "teaser": "質詢要求補助",
            "cover_image_url": None, "slide_count": 1, "source_note": "逐字稿取自官方",
            "transcript_text": "00:00 王委員：" + LOOKS_LIKE_A_MARKER,
            "brief": {"one_liner": "質詢要求補助", "key_numbers": [], "asks": []},
            "slides": [{"index": 1, "title": "第一段 [[S1]]", "bullets": ["[[S1]]：重點一", PLAIN],
                        "detail": "[[S1]]：完整敘述 " + PLAIN, "timestamp": 1.5, "image_url": None}],
        })

    def test_the_detail_of_an_article_without_a_brief_keeps_its_markers_and_its_teaser_cut(self):
        data = without_neutral(self.client.get("/api/articles/2026-08-26-tccc-7").json())
        self.assertEqual(data["teaser"], (LOOKS_LIKE_A_MARKER + "。" + "長" * 200)[:120])
        self.assertEqual(data["transcript_text"], LOOKS_LIKE_A_MARKER)
        self.assertEqual(data["brief"], None)
        self.assertEqual(data["slides"], [
            {"index": 1, "title": "只有一段", "bullets": ["[[S2]]：條列"], "detail": LOOKS_LIKE_A_MARKER + "。" + "長" * 200,
             "timestamp": 0.0, "image_url": None},
            {"index": 2, "title": "第二段", "bullets": [], "detail": "", "timestamp": 9.0, "image_url": None},
        ])
        self.assertEqual(data["slide_count"], 2)

    def test_the_new_keys_if_present_are_neutral(self):
        for slug in ("2026-08-27-900001", "2026-08-26-tccc-7"):
            with self.subTest(slug=slug):
                raw = self.client.get(f"/api/articles/{slug}").json()
                for key, neutral in NEUTRAL_ARTICLE_KEYS.items():
                    self.assertEqual(raw.get(key, neutral), neutral)
                for slide in raw["slides"]:
                    self.assertEqual(slide.get("points", []), [])

    def test_the_list_is_exactly_what_it_always_was(self):
        data = self.client.get("/api/articles").json()
        self.assertEqual([item["slug"] for item in data["items"]], ["2026-08-27-900001", "2026-08-26-tccc-7"])
        self.assertEqual(sorted(data["items"][0]), sorted([
            "slug", "ivod_id", "source", "party", "title", "speaker", "meeting", "date", "duration_seconds",
            "ivod_url", "teaser", "cover_image_url", "slide_count"]))
        self.assertEqual(data["items"][0]["teaser"], "質詢要求補助")
        self.assertEqual(data["items"][1]["teaser"], (LOOKS_LIKE_A_MARKER + "。" + "長" * 200)[:120])
        self.assertEqual((data["count"], data["page"], data["pages"]), (2, 1, 1))

    def test_the_number_of_queries_is_what_it_was(self):
        with CaptureQueriesContext(connection) as listing:
            self.client.get("/api/articles")
        with CaptureQueriesContext(connection) as detail:
            self.client.get("/api/articles/2026-08-26-tccc-7")
        self.assertLessEqual(len(listing), LIST_QUERIES)
        self.assertLessEqual(len(detail), DETAIL_QUERIES)

    @unittest.skipUnless(HAS_SPEAKERS, "舊程式碼的 Article 沒有 speakers 欄位")
    def test_a_council_row_with_a_speaker_list_by_mistake_is_still_not_touched(self):
        """議會的文章永遠不碰：就算資料庫裡不知怎麼有了說話者清單（壞資料），文字也不換記號。"""
        Article.objects.filter(pk=self.no_brief.pk).update(
            speakers=[{"id": "S1", "label": "發言者 A", "name": "不該出現的名字", "kind": "participant"}])
        data = self.client.get("/api/articles/2026-08-26-tccc-7").json()
        self.assertEqual(data["transcript_text"], LOOKS_LIKE_A_MARKER)
        self.assertEqual(data["slides"][0]["bullets"], ["[[S2]]：條列"])
        self.assertEqual(data["teaser"], (LOOKS_LIKE_A_MARKER + "。" + "長" * 200)[:120])
        self.assertNotIn("不該出現的名字", str(data))
