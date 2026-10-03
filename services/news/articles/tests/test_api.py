"""news API：Astro 前端讀的就是這些端點。"""
from __future__ import annotations

import atexit
import base64
import itertools
import shutil
import tempfile
from datetime import date, datetime
from urllib.parse import parse_qsl, unquote, urlsplit

from django.test import TestCase, override_settings
from django.utils import timezone

from articles.ingest import save_result
from articles.models import (Article, ArticleStatus, FollowUp, Membership, Person, ProfileStat, Session,
                             Topic, TopicEvaluation)
from articles.profiles import assign_sessions, compute_profiles

MEDIA = tempfile.mkdtemp(prefix="news_api_media_")
atexit.register(shutil.rmtree, MEDIA, ignore_errors=True)
IMAGE = b"\x00\x01fake-webp\xff"


BRIEF = {
    "one_liner": "國防部三年編 82.4 億買無人機，交到部隊的不到一半",
    "key_numbers": [
        {"value": "82.4", "unit": "億元", "label": "三年累計編列", "quote": "累計編列八十二點四億元"},
        {"value": "3", "unit": "萬元", "label": "現行罰鍰下限", "quote": "罰鍰從現行的3萬到5萬",
         "law": "醫療法", "article": "第106條",
         "sources": [{"law": "醫療法", "article": "第一百零六條",
                      "title": "醫療法 第一百零六條（2026-05-08 修正版）",
                      "excerpt": "違反第二十四條第二項規定者，處新臺幣三萬元以上五萬元以下罰鍰",
                      "official_url": "https://law.moj.gov.tw/x",
                      "api_url": "https://ly.govapi.tw/v2/x"}]},
    ],
    "asks": [{"request": "提出交機時程清冊", "deadline": "一個月內", "response": "部長允諾"}],
}


def _article(ivod_id="900001", speaker="範例一", day="2026-08-27", status=None,
             slides=2, with_image=True, brief=None, source="ly", party="",
             meeting="第11屆第5會期第23次會議", duration=197):
    article = Article.objects.create(
        ivod_id=ivod_id,
        source=source,
        party=party,
        slug=f"{day}-{ivod_id}",
        title=f"{day} {speaker}－{meeting}",
        speaker=speaker,
        meeting=meeting,
        date=date.fromisoformat(day),
        duration_seconds=duration,
        ivod_url=f"https://ivod.ly.gov.tw/Play/Clip/1M/{ivod_id}",
    )
    if status in (ArticleStatus.PENDING, ArticleStatus.FAILED):
        article.status = status
        article.save()
        return article
    save_result(article, {
        "title": article.title,
        "source_note": "逐字稿由立法院 AI 自動產生，可能有辨識錯誤",
        "transcript_text": "00:00 主席 各位同仁",
        "brief": brief,
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
        self.assertEqual(card["speaker"], "範例一")
        self.assertEqual(card["slug"], "2026-08-27-900001")
        self.assertEqual(card["slide_count"], 2)
        self.assertIn("完整敘述", card["teaser"])
        self.assertTrue(card["cover_image_url"].startswith("/media/"))

    def test_unfinished_articles_are_not_news(self):
        """攔的 bug：處理中或失敗的文章是內部狀態，露到前端就會出現
        沒有內容的空白新聞。"""
        _article(ivod_id="1", status=ArticleStatus.PENDING)
        _article(ivod_id="2", status=ArticleStatus.FAILED)
        self.assertEqual(self.client.get("/api/articles").json()["count"], 0)

    def test_filtering_by_date_and_speaker_and_text(self):
        _article(ivod_id="1", speaker="範例一", day="2026-08-27")
        _article(ivod_id="2", speaker="範例二", day="2026-08-26")
        cases = [
            ("?date=2026-08-26", 1),
            ("?speaker=範例一", 1),
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
        body = self.client.get("/api/articles/2026-08-27-900001").json()
        self.assertEqual(len(body["slides"]), 2)
        slide = body["slides"][0]
        self.assertEqual(slide["index"], 1)
        self.assertEqual(slide["bullets"], ["重點 1"])
        self.assertIn("無人載具", slide["detail"])
        self.assertTrue(slide["image_url"].startswith("/media/"))
        self.assertIn("AI", body["source_note"])
        self.assertIn("主席", body["transcript_text"])

    def test_the_detail_page_carries_the_brief_and_the_card_leads_with_it(self):
        _article(brief=BRIEF)
        body = self.client.get("/api/articles/2026-08-27-900001").json()
        self.assertEqual(body["brief"]["one_liner"], BRIEF["one_liner"])
        self.assertEqual(body["brief"]["key_numbers"][0]["unit"], "億元")
        self.assertEqual(body["brief"]["asks"][0]["response"], "部長允諾")
        numbers = body["brief"]["key_numbers"]
        # 舊資料沒有 law／sources 也要出得來
        self.assertEqual(numbers[0]["law"], "")
        self.assertEqual(numbers[0]["sources"], [])
        self.assertEqual(numbers[1]["sources"][0]["article"], "第一百零六條")
        self.assertTrue(numbers[1]["sources"][0]["official_url"].startswith("https://"))
        self.assertEqual(body["teaser"], BRIEF["one_liner"])
        card = self.client.get("/api/articles").json()["items"][0]
        self.assertEqual(card["teaser"], BRIEF["one_liner"])

    def test_an_article_without_a_brief_says_so_with_null(self):
        _article()
        body = self.client.get("/api/articles/2026-08-27-900001").json()
        self.assertIsNone(body["brief"])

    def test_an_unknown_article_is_404(self):
        self.assertEqual(self.client.get("/api/articles/沒這篇").status_code, 404)

    def test_an_unfinished_article_is_404_rather_than_half_a_page(self):
        _article(ivod_id="1", status=ArticleStatus.PENDING)
        self.assertEqual(
            self.client.get("/api/articles/2026-08-27-1").status_code, 404)

    def test_a_page_without_a_screenshot_still_renders(self):
        _article(with_image=False)
        body = self.client.get("/api/articles/2026-08-27-900001").json()
        self.assertIsNone(body["slides"][0]["image_url"])
        self.assertIsNone(body["cover_image_url"])

    def test_speakers_are_listed_with_counts(self):
        _article(ivod_id="1", speaker="範例一", day="2026-08-27")
        _article(ivod_id="2", speaker="範例一", day="2026-08-26")
        _article(ivod_id="3", speaker="範例二", day="2026-08-25")
        items = self.client.get("/api/speakers").json()["items"]
        by_name = {i["name"]: i for i in items}
        self.assertEqual(by_name["範例一"]["count"], 2)
        self.assertEqual(by_name["範例一"]["latest_date"], "2026-08-27")
        self.assertEqual(by_name["範例二"]["count"], 1)

    def test_the_newest_article_comes_first(self):
        _article(ivod_id="1", day="2026-08-25")
        _article(ivod_id="2", day="2026-08-27")
        items = self.client.get("/api/articles").json()["items"]
        self.assertEqual(items[0]["ivod_id"], "2")


@override_settings(MEDIA_ROOT=MEDIA)
class MediaServingTests(TestCase):
    """攔的 bug：清單回的 /media/... 只是字串，沒有任何測試證明它取得到。

    django.conf.urls.static.static() 在 DEBUG=False 時直接回空清單，而本
    專案的 DEBUG 預設就是 False——照文件部署的結果是整站圖片全 404，
    而三邊的測試都照樣綠（只比對字串開頭）。
    """

    def test_the_cover_url_from_the_api_actually_serves_the_image(self):
        _article()
        url = self.client.get("/api/articles").json()["items"][0]["cover_image_url"]
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), IMAGE)

    def test_every_slide_image_url_serves_too(self):
        _article()
        body = self.client.get("/api/articles/2026-08-27-900001").json()
        for slide in body["slides"]:
            with self.subTest(index=slide["index"]):
                self.assertEqual(self.client.get(slide["image_url"]).status_code, 200)

    def test_a_screenshot_is_served_with_a_long_public_cache_header(self):
        """對外流量真正碰到 Django 的是圖片。每次重跑都換資料夾、網址跟著
        換，所以同一個網址的內容永遠不變，可以讓 Cloudflare 邊緣快取。"""
        _article()
        url = self.client.get("/api/articles").json()["items"][0]["cover_image_url"]
        cache = self.client.get(url)["Cache-Control"]
        self.assertIn("public", cache)
        self.assertIn("max-age=86400", cache)
        self.assertIn("immutable", cache)

    def test_a_missing_screenshot_is_not_cached(self):
        """404 被快取一天的話，補上圖片之後讀者還是一整天看到破圖。"""
        response = self.client.get("/media/articles/nope/01.webp")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("max-age=86400", response.get("Cache-Control", ""))

    @override_settings(MEDIA_CACHE_SECONDS=0)
    def test_the_cache_header_can_be_switched_off(self):
        _article()
        url = self.client.get("/api/articles").json()["items"][0]["cover_image_url"]
        self.assertFalse(self.client.get(url).has_header("Cache-Control"))

    def test_paths_outside_the_media_root_are_refused(self):
        for path in ("/media/../../manage.py", "/media/..%2f..%2fmanage.py"):
            with self.subTest(path=path):
                self.assertNotEqual(self.client.get(path).status_code, 200)


@override_settings(MEDIA_ROOT=MEDIA)
class HostileQueryTests(TestCase):
    """訪客網址上的參數會被前端原樣轉手過來，爬蟲什麼都會試。"""

    def test_a_page_number_beyond_any_database_is_refused_not_a_500(self):
        """攔的 bug：超過 int64 的 OFFSET 讓 SQLite 直接 500，而前端會把它
        顯示成「後端掛了」——任何爬蟲都能製造假的故障警報。"""
        response = self.client.get("/api/articles?page=99999999999999999999")
        self.assertEqual(response.status_code, 422)

    def test_a_bad_date_is_the_visitors_problem_not_a_500(self):
        self.assertEqual(self.client.get("/api/articles?date=abc").status_code, 422)
        self.assertEqual(
            self.client.get("/api/articles?date=2026-13-45").status_code, 422)

    def test_a_large_page_size_is_clamped_rather_than_refused(self):
        _article()
        body = self.client.get("/api/articles?page_size=100000").json()
        self.assertLessEqual(body["page_size"], 50)


@override_settings(WHITENOISE_USE_FINDERS=True, WHITENOISE_AUTOREFRESH=True)
class StaticServingTests(TestCase):
    """admin 的 CSS 由 WhiteNoise 服務，gunicorn 底下不再靠 runserver 的 --insecure。

    測試用 finders 直接從 django.contrib.admin 的 static 目錄找，不必先
    collectstatic；正式映像在建置時 collectstatic，走的是同一個 middleware。
    """

    def test_admin_css_is_served_without_the_dev_server(self):
        response = self.client.get("/static/admin/css/base.css")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/css", response["Content-Type"])


@override_settings(MEDIA_ROOT=MEDIA)
class SourceTests(TestCase):
    """兩個議會並列：卡片要說自己是哪裡來的，而且可以只看其中一個。"""

    def test_cards_carry_their_source_and_can_be_filtered_by_it(self):
        _article("900001", speaker="範例一")
        _article("tccc-14833", speaker="楊啓邦", source="tccc")
        res = self.client.get("/api/articles").json()
        self.assertEqual({i["source"] for i in res["items"]}, {"ly", "tccc"})
        res = self.client.get("/api/articles?source=tccc").json()
        self.assertEqual([i["ivod_id"] for i in res["items"]], ["tccc-14833"])
        self.assertEqual(self.client.get("/api/articles?source=nope").status_code, 422)

    def test_new_taipei_is_a_source_too(self):
        _article("900001", speaker="範例一")
        _article("ntpc-0408R1150916020", speaker="周雅玲、林裔綺", source="ntpc")
        res = self.client.get("/api/articles?source=ntpc").json()
        self.assertEqual([i["ivod_id"] for i in res["items"]], ["ntpc-0408R1150916020"])
        self.assertEqual(res["items"][0]["source"], "ntpc")
        items = self.client.get("/api/speakers?source=ntpc").json()["items"]
        self.assertEqual({i["name"] for i in items}, {"周雅玲", "林裔綺"})
        self.assertEqual(self.client.get("/api/parties?source=ntpc").status_code, 200)

    def test_the_detail_carries_the_source_too(self):
        _article("tccc-14833", speaker="楊啓邦", source="tccc")
        res = self.client.get("/api/articles/2026-08-27-tccc-14833").json()
        self.assertEqual(res["source"], "tccc")

    def test_speakers_are_listed_per_source(self):
        _article("900001", speaker="範例一")
        _article("tccc-14833", speaker="楊啓邦", source="tccc")
        items = self.client.get("/api/speakers").json()["items"]
        self.assertEqual({(i["name"], i["source"]) for i in items},
                         {("範例一", "ly"), ("楊啓邦", "tccc")})
        items = self.client.get("/api/speakers?source=tccc").json()["items"]
        self.assertEqual([i["name"] for i in items], ["楊啓邦"])
        self.assertEqual(self.client.get("/api/speakers?source=nope").status_code, 422)


@override_settings(MEDIA_ROOT=MEDIA)
class JointSpeakerTests(TestCase):
    """聯合質詢的講者是「甲、乙、丙」：查任何一位都要命中，清單要拆成三個人。"""

    def test_each_member_of_a_joint_article_can_be_looked_up(self):
        _article("tccc-1", speaker="謝志忠、黃守達、王立任", source="tccc")
        _article("tccc-2", speaker="王立", source="tccc")
        for name in ("謝志忠", "黃守達", "王立任"):
            res = self.client.get(f"/api/articles?speaker={name}").json()
            self.assertEqual([i["ivod_id"] for i in res["items"]], ["tccc-1"], name)
        res = self.client.get("/api/articles?speaker=王立").json()
        self.assertEqual([i["ivod_id"] for i in res["items"]], ["tccc-2"])

    def test_the_speaker_list_splits_joint_articles(self):
        _article("tccc-1", speaker="謝志忠、黃守達", source="tccc", day="2026-09-24")
        _article("tccc-2", speaker="謝志忠", source="tccc", day="2026-09-02")
        items = {(i["name"], i["count"], i["latest_date"])
                 for i in self.client.get("/api/speakers").json()["items"]}
        self.assertEqual(items, {("謝志忠", 2, "2026-09-24"), ("黃守達", 1, "2026-09-24")})


@override_settings(MEDIA_ROOT=MEDIA)
class PartyApiTests(TestCase):
    def test_cards_carry_the_party_and_can_be_filtered_by_it(self):
        _article("1", speaker="甲", party="民主進步黨")
        _article("2", speaker="乙", party="中國國民黨")
        _article("tccc-3", speaker="丙、丁", source="tccc", party="中國國民黨、民主進步黨")
        res = self.client.get("/api/articles?party=民主進步黨").json()
        self.assertEqual({i["ivod_id"] for i in res["items"]}, {"1", "tccc-3"})
        self.assertEqual(res["items"][0]["party"] in ("民主進步黨", "中國國民黨、民主進步黨"), True)

    def test_parties_are_aggregated_per_party(self):
        _article("1", speaker="甲", party="民主進步黨", day="2026-08-27")
        _article("tccc-3", speaker="丙、丁", source="tccc", party="中國國民黨、民主進步黨", day="2026-09-24")
        _article("4", speaker="無", party="")
        items = {(i["name"], i["count"], i["latest_date"]) for i in self.client.get("/api/parties").json()["items"]}
        self.assertEqual(items, {("民主進步黨", 2, "2026-09-24"), ("中國國民黨", 1, "2026-09-24")})
        items = self.client.get("/api/parties?source=ly").json()["items"]
        self.assertEqual([(i["name"], i["count"]) for i in items], [("民主進步黨", 1)])

    def test_speakers_carry_their_current_party_and_district(self):
        from articles.members_sync import MemberRecord, sync
        sync([MemberRecord(source="ly", external_id="1", name="甲", party="民主進步黨", district="臺北市第一選舉區")])
        _article("1", speaker="甲")
        _article("2", speaker="乙")
        items = {i["name"]: i for i in self.client.get("/api/speakers").json()["items"]}
        self.assertEqual((items["甲"]["party"], items["甲"]["district"]), ("民主進步黨", "臺北市第一選舉區"))
        self.assertEqual((items["乙"]["party"], items["乙"]["district"]), ("", ""))


# --- 人物側寫 ---

S5 = "第11屆第5會期第23次會議"
S5_EXTRA = "第11屆第5會期第1次臨時會第2次會議"
S4 = "第11屆第4會期第8次會議"
_speech_ids = itertools.count(1)


def _speech(speaker, meeting=S5, day="2026-03-10", brief=None, source="ly",
            status=None, duration=600):
    """側寫用的文章：一段、不帶截圖，跑得快。"""
    return _article(f"p{next(_speech_ids)}", speaker=speaker, day=day, status=status, slides=1,
                    with_image=False, brief=brief, source=source, meeting=meeting,
                    duration=duration)


def _members(source, *names):
    return {name: Membership.objects.create(person=Person.objects.create(name=name),
                                            source=source, name=name)
            for name in names}


def _evidence_count(client, evidence_url):
    """照網站的做法跟著證據網址走：/speaker/<名字>?… 換成 /api/articles 的查詢。

    網站把 brief 轉成 has_brief（見設計文件「網站」一節），其餘原樣轉手。
    """
    parts = urlsplit(evidence_url)
    assert parts.path.startswith("/speaker/"), evidence_url
    params = dict(parse_qsl(parts.query))
    if params.pop("brief", None) == "1":
        params["has_brief"] = "1"
    params["speaker"] = unquote(parts.path.removeprefix("/speaker/"))
    return client.get("/api/articles", params).json()["count"]


@override_settings(MEDIA_ROOT=MEDIA)
class ProfileApiTests(TestCase):
    """立法院第 11 屆第 5 會期（含臨時會）與第 4 會期，六位立委。"""

    def setUp(self):
        self.m = _members("ly", "王立", "王立任", "甲", "乙", "丙", "丁")
        for day in range(1, 6):
            _speech("王立", day=f"2026-03-0{day}", brief=BRIEF)
        _speech("王立", S5_EXTRA, day="2026-07-15", brief=BRIEF)   # 臨時會併入第 5 會期
        _speech("王立", day="2026-04-01")                          # 沒有摘要卡
        _speech("王立、甲", day="2026-04-02", brief=BRIEF)          # 聯合質詢：不算具體度
        _speech("王立", day="2026-04-03", status=ArticleStatus.PENDING)
        _speech("王立任", day="2026-04-04", brief=BRIEF)            # 名字包含「王立」，不能算給他
        _speech("王立任", day="2026-04-05", brief=BRIEF)
        _speech("甲、王立任、乙", day="2026-04-06", duration=1800)
        _speech("王立", S4, day="2025-10-01")
        _speech("丙", S4, day="2025-10-02")
        _speech("王立", "立法院朝野黨團協商", day="2026-04-07")      # 沒有會期
        compute_profiles()
        self.s5 = Session.objects.get(name="第11屆第5會期")
        self.s4 = Session.objects.get(name="第11屆第4會期")

    def _profile(self, name, **params):
        return self.client.get(f"/api/people/{self.m[name].person_id}/profile", params)

    def _indicators(self, res):
        return {i["key"]: i for block in res["blocks"] for i in block["indicators"]}

    def test_the_profile_has_the_documented_shape(self):
        res = self._profile("王立").json()
        self.assertEqual(res["person"], {"id": self.m["王立"].person_id, "name": "王立"})
        self.assertEqual(res["source"], "ly")
        self.assertEqual(res["session"], {"id": self.s5.id, "source": "ly", "term": "11",
                                          "name": "第11屆第5會期", "start_date": "2026-03-01",
                                          "end_date": "2026-07-15"})
        self.assertEqual([s["name"] for s in res["sessions"]], ["第11屆第5會期", "第11屆第4會期"])
        self.assertEqual(res["min_sample"], 5)
        self.assertTrue(res["computed_at"].endswith("+08:00"), res["computed_at"])
        self.assertEqual([(b["key"], b["title"], [i["key"] for i in b["indicators"]])
                          for b in res["blocks"]],
                         [("volume", "投入量", ["speeches", "speaking_minutes"]),
                          ("specificity", "具體度", ["numbers_per_speech", "deadline_asks_per_speech",
                                                    "sourced_number_share"]),
                          # 追問區塊永遠在（清單不靠模型）；判斷器沒通過就沒有比率
                          ("followup", "追問", [])])
        got = self._indicators(res)
        speeches = got["speeches"]
        # 單獨 5 + 臨時會 1 + 沒有卡 1 + 聯合 1；處理中的與沒有會期的不算
        self.assertEqual((speeches["value"], speeches["n"], speeches["n_unit"], speeches["unit"],
                          speeches["label"], speeches["peers"], speeches["sample_ok"]),
                         (8, 8, "篇", "次", "發言次數", 6, True))
        self.assertIsInstance(speeches["value"], int)
        self.assertIsNotNone(speeches["percentile"])
        # 摘要卡：兩個數字（一個有條文來源）、一項帶期限的要求；基礎文章 6 篇
        self.assertEqual((got["numbers_per_speech"]["value"], got["numbers_per_speech"]["n"]), (2.0, 6))
        self.assertEqual(got["deadline_asks_per_speech"]["value"], 1.0)
        share = got["sourced_number_share"]
        self.assertEqual((share["value"], share["n"], share["n_unit"], share["unit"]),
                         (50.0, 12, "個數字", "%"))
        # 具體度只有他夠樣本（王立任只有 2 篇）：同儕不足，不比較
        self.assertEqual((share["sample_ok"], share["percentile"], share["peers"]), (True, None, 1))

    def test_percentiles_are_passed_through_unrounded(self):
        """頁面自己四捨五入到整數。API 先取到小數一位就是兩次捨入：64.46 → 64.5 → 65。"""
        stat = ProfileStat.objects.get(person=self.m["王立"].person, session=self.s5,
                                       indicator="speeches")
        got = self._indicators(self._profile("王立").json())["speeches"]
        # 第 5 會期的發言次數 0、0、1、2、3、8：比他低的 5 人、同值 1 人
        self.assertEqual(stat.percentile, 5.5 * 100 / 6)
        self.assertEqual(got["percentile"], stat.percentile)

    def test_evidence_urls_are_relative_site_paths_with_the_name_encoded(self):
        got = self._indicators(self._profile("王立").json())
        base = f"/speaker/%E7%8E%8B%E7%AB%8B?source=ly&session={self.s5.id}"
        self.assertEqual(got["speeches"]["evidence_url"], base)
        self.assertEqual(got["speaking_minutes"]["evidence_url"], base)
        for key in ("numbers_per_speech", "deadline_asks_per_speech", "sourced_number_share"):
            self.assertEqual(got[key]["evidence_url"], base + "&solo=1&brief=1")

    def test_every_count_can_be_clicked_back_to_exactly_that_many_articles(self):
        """證據一致性：證據網址查出來的篇數必須等於指標的 n。"""
        checked = 0
        for name in self.m:
            first = self._profile(name).json()
            for session in first["sessions"]:
                res = self._profile(name, session=session["id"]).json()
                for indicator in self._indicators(res).values():
                    if indicator["n_unit"] != "篇":
                        continue
                    self.assertEqual(_evidence_count(self.client, indicator["evidence_url"]),
                                     indicator["n"], (name, session["name"], indicator["key"]))
                    checked += 1
                speeches = self._indicators(res)["speeches"]
                self.assertEqual(speeches["value"], speeches["n"])
        self.assertGreater(checked, 20)

    def test_a_council_member_sees_speaking_time_first(self):
        tccc = _members("tccc", "楊啓邦")
        _speech("楊啓邦", "第4屆第8次定期會 市政總質詢", day="2026-09-01", source="tccc")
        compute_profiles()
        res = self.client.get(f"/api/people/{tccc['楊啓邦'].person_id}/profile").json()
        self.assertEqual(res["source"], "tccc")
        self.assertEqual([i["key"] for i in res["blocks"][0]["indicators"]],
                         ["speaking_minutes", "speeches"])
        self.assertEqual(res["blocks"][0]["indicators"][0]["value"], 10.0)

    def test_the_default_session_is_the_latest_one_he_spoke_in(self):
        # 丙只在第 4 會期發言：第 5 會期比較新，但他在那裡是 0
        self.assertEqual(self._profile("丙").json()["session"]["id"], self.s4.id)
        # 丁從沒發言：就用有統計的最近一個
        res = self._profile("丁").json()
        self.assertEqual(res["session"]["id"], self.s5.id)
        self.assertEqual(self._indicators(res)["speeches"]["value"], 0)

    def test_a_session_can_be_picked(self):
        res = self._profile("王立", session=self.s4.id).json()
        self.assertEqual((res["session"]["name"], res["source"]), ("第11屆第4會期", "ly"))
        self.assertEqual(self._indicators(res)["speeches"]["value"], 1)

    def test_the_default_source_is_that_of_the_latest_session(self):
        person = self.m["王立"].person
        Membership.objects.create(person=person, source="tccc", name="王立")
        _speech("王立", "第4屆第8次定期會 市政總質詢", day="2026-09-01", source="tccc")
        compute_profiles()
        self.assertEqual(self._profile("王立").json()["source"], "tccc")
        res = self._profile("王立", source="ly").json()
        self.assertEqual((res["source"], res["session"]["id"]), ("ly", self.s5.id))
        self.assertEqual([s["source"] for s in res["sessions"]], ["ly", "ly"])

    def test_missing_people_and_statistics_are_404(self):
        self.assertEqual(self.client.get("/api/people/999999/profile").status_code, 404)
        nobody = Person.objects.create(name="沒有任期的人")
        self.assertEqual(self.client.get(f"/api/people/{nobody.id}/profile").status_code, 404)
        self.assertEqual(self._profile("王立", source="tccc").status_code, 404)
        self.assertEqual(self._profile("王立", session=999999).status_code, 404)
        self.assertEqual(self._profile("王立", session=self.s5.id, source="ntpc").status_code, 404)
        self.assertEqual(self._profile("王立", source="nope").status_code, 422)


@override_settings(MEDIA_ROOT=MEDIA)
class EvidenceFilterTests(TestCase):
    """/api/articles 的 session、solo、has_brief：側寫的證據網址靠它們。"""

    def setUp(self):
        _article("1", speaker="王立", brief=BRIEF, slides=1, with_image=False)
        _article("2", speaker="王立、甲", brief=BRIEF, slides=1, with_image=False)
        _article("3", speaker="王立", slides=1, with_image=False)
        _article("4", speaker="王立任", brief=BRIEF, slides=1, with_image=False)
        _article("5", speaker="王立", meeting=S4, day="2025-10-01", slides=1, with_image=False)
        assign_sessions()
        self.s5 = Session.objects.get(name="第11屆第5會期")

    def _ids(self, **params):
        return sorted(i["ivod_id"] for i in self.client.get("/api/articles", params).json()["items"])

    def test_session_keeps_only_that_sessions_articles(self):
        self.assertEqual(self._ids(session=self.s5.id), ["1", "2", "3", "4"])
        self.assertEqual(self._ids(session=999999), [])

    def test_solo_drops_joint_speeches_and_with_a_speaker_means_exactly_that_name(self):
        self.assertEqual(self._ids(solo=1), ["1", "3", "4", "5"])
        self.assertEqual(self._ids(speaker="王立"), ["1", "2", "3", "5"])
        self.assertEqual(self._ids(speaker="王立", solo=1), ["1", "3", "5"])

    def test_has_brief_keeps_only_articles_with_a_brief(self):
        self.assertEqual(self._ids(has_brief=1), ["1", "2", "4"])
        self.assertEqual(self._ids(speaker="王立", session=self.s5.id, solo=1, has_brief=1), ["1"])

    def test_false_means_no_filter_and_junk_is_refused(self):
        self.assertEqual(self._ids(solo=0, has_brief="false"), ["1", "2", "3", "4", "5"])
        self.assertEqual(self.client.get("/api/articles?session=abc").status_code, 422)
        self.assertEqual(self.client.get("/api/articles?solo=maybe").status_code, 422)


@override_settings(MEDIA_ROOT=MEDIA)
class SpeakerPersonTests(TestCase):
    def test_speakers_carry_the_person_behind_the_name(self):
        m = _members("ly", "甲")["甲"]
        _article("1", speaker="甲")
        _article("2", speaker="乙")
        items = {i["name"]: i for i in self.client.get("/api/speakers").json()["items"]}
        self.assertEqual(items["甲"]["person_id"], m.person_id)
        self.assertIsNone(items["乙"]["person_id"])

    def test_the_latest_term_wins_when_a_name_has_several(self):
        old = Person.objects.create(name="甲")
        new = Person.objects.create(name="甲")
        Membership.objects.create(person=new, source="ly", name="甲", start_date=date(2024, 2, 1))
        Membership.objects.create(person=old, source="ly", name="甲", start_date=date(2020, 2, 1),
                                  end_date=date(2024, 1, 31))
        _article("1", speaker="甲")
        items = self.client.get("/api/speakers").json()["items"]
        self.assertEqual([i["person_id"] for i in items], [new.id])


# --- 議題分布 ---

CLASSIFIER = "fake-model#topic-v1"


def _pass(source, classifier=CLASSIFIER, accuracy=0.9, labeled=20):
    return TopicEvaluation.objects.create(
        source=source, classifier=classifier, labeled=labeled, correct=round(labeled * accuracy),
        accuracy=accuracy, passed=True, ran_at=timezone.make_aware(datetime(2026, 10, 3, 9, 30)))


def _topic(article, primary, classifier=CLASSIFIER):
    Topic.objects.create(article=article, primary=primary, classifier=classifier,
                         labeled_at=timezone.now())
    return article


def _topic_speech(speaker, primary, classifier=CLASSIFIER, **kw):
    """一篇基礎文章（單獨發言、有摘要卡）加上它的領域。"""
    kw.setdefault("brief", BRIEF)
    return _topic(_speech(speaker, **kw), primary, classifier)


@override_settings(MEDIA_ROOT=MEDIA)
class TopicProfileApiTests(TestCase):
    """立法院第 11 屆第 5 會期，六位立委，分類器已通過評估。"""

    def setUp(self):
        self.m = _members("ly", "王立", "甲", "乙", "丙", "丁", "戊")
        Membership.objects.filter(name="王立").update(
            committees=["第11屆第5會期：財政委員會", "第11屆第5會期：程序委員會"])
        Membership.objects.exclude(name="王立").update(committees=["第11屆第5會期：內政委員會"])
        # 王立：財經 3、衛福 2、勞動 1，基礎文章 6 篇
        for i, primary in enumerate(("finance", "welfare", "finance", "labor", "welfare", "finance")):
            _topic_speech("王立", primary, day=f"2026-03-0{i + 1}")
        _topic(_speech("王立、甲", day="2026-04-01", brief=BRIEF), "defense")    # 聯合質詢
        _topic(_speech("王立", day="2026-04-02"), "defense")                     # 沒有摘要卡
        _speech("王立", day="2026-04-03", brief=BRIEF)                           # 還沒分類
        _topic_speech("王立", "defense", classifier="new-model#topic-v2", day="2026-04-04")
        _topic_speech("王立", "justice", meeting=S4, day="2025-10-01")                # 上一個會期
        for name, primaries in (("甲", ["interior"] * 4 + ["finance"]),
                                ("乙", ["interior", "education", "transport", "labor", "local"]),
                                ("丙", ["environment"] * 5), ("丁", ["interior", "defense"])):
            for primary in primaries:
                _topic_speech(name, primary)
        self.evaluation = _pass("ly")
        compute_profiles()
        self.s5 = Session.objects.get(name="第11屆第5會期")

    def _profile(self, name, **params):
        return self.client.get(f"/api/people/{self.m[name].person_id}/profile", params).json()

    def _topics(self, res):
        blocks = {b["key"]: b for b in res["blocks"]}
        return blocks.get("topics")

    def test_the_topics_block_has_the_documented_shape(self):
        res = self._profile("王立")
        self.assertEqual([b["key"] for b in res["blocks"]],
                         ["volume", "specificity", "topics", "followup"])
        block = self._topics(res)
        self.assertEqual(block["title"], "議題分布")
        self.assertEqual([i["key"] for i in block["indicators"]],
                         ["topic_focus", "topic_breadth", "committee_alignment"])
        self.assertEqual(block["classifier"], {"name": CLASSIFIER, "accuracy": 0.9, "labeled": 20,
                                               "evaluated_at": "2026-10-03T09:30:00+08:00"})
        # 其他區塊沒有這兩個欄位
        self.assertNotIn("distribution", res["blocks"][0])

    def test_the_distribution_lists_all_twelve_by_count_then_in_table_order(self):
        rows = self._topics(self._profile("王立"))["distribution"]
        self.assertEqual([(r["key"], r["label"], r["count"]) for r in rows[:3]],
                         [("finance", "財政經濟", 3), ("welfare", "衛生福利", 2), ("labor", "勞動", 1)])
        self.assertEqual([r["key"] for r in rows[3:]],
                         ["defense", "interior", "education", "transport", "environment",
                          "justice", "agriculture", "digital", "local"])
        self.assertEqual({r["count"] for r in rows[3:]}, {0})
        self.assertEqual([r["share"] for r in rows[:4]], [50.0, 2 / 6 * 100, 1 / 6 * 100, 0.0])
        self.assertEqual(rows[0]["evidence_url"],
                         f"/speaker/%E7%8E%8B%E7%AB%8B?source=ly&session={self.s5.id}"
                         "&solo=1&brief=1&topic=finance")

    def test_the_indicators(self):
        got = {i["key"]: i for i in self._topics(self._profile("王立"))["indicators"]}
        focus, breadth, aligned = got["topic_focus"], got["topic_breadth"], got["committee_alignment"]
        self.assertEqual((focus["value"], focus["n"], focus["unit"], focus["n_unit"]),
                         (50.0, 6, "%", "篇"))
        self.assertEqual((breadth["value"], breadth["unit"]), (3, "個"))
        self.assertIsInstance(breadth["value"], int)
        # 財政委員會的職掌只有財經：6 篇裡 3 篇
        self.assertEqual((aligned["value"], aligned["n"]), (50.0, 6))
        # 同儕：基礎文章至少 5 篇的王立、甲、乙、丙（丁只有 2 篇）——不到 5 人，不比
        self.assertEqual((focus["peers"], focus["percentile"], focus["sample_ok"]), (4, None, True))
        self.assertEqual(focus["evidence_url"],
                         f"/speaker/%E7%8E%8B%E7%AB%8B?source=ly&session={self.s5.id}"
                         "&solo=1&brief=1&topic=any")

    def test_every_count_can_be_clicked_back_to_exactly_that_many_articles(self):
        """證據一致性：分布每一列的證據網址查出來的篇數等於它的篇數；每個指標等於它的 n。"""
        checked = 0
        for name in self.m:
            res = self._profile(name, session=self.s5.id)
            for row in self._topics(res)["distribution"]:
                self.assertEqual(_evidence_count(self.client, row["evidence_url"]), row["count"],
                                 (name, row["key"]))
                checked += 1
            for block in res["blocks"]:
                for indicator in block["indicators"]:
                    if indicator["n_unit"] == "篇":
                        self.assertEqual(_evidence_count(self.client, indicator["evidence_url"]),
                                         indicator["n"], (name, indicator["key"]))
                        checked += 1
        self.assertGreater(checked, 6 * 12)

    def test_without_a_passing_evaluation_there_is_no_block_and_no_evidence(self):
        TopicEvaluation.objects.all().delete()
        res = self._profile("王立")
        self.assertEqual([b["key"] for b in res["blocks"]], ["volume", "specificity", "followup"])
        for topic in ("finance", "any"):
            self.assertEqual(self.client.get("/api/articles", {"topic": topic}).json()["count"], 0)

    def test_an_evaluation_that_passed_after_the_last_recompute_shows_nothing_yet(self):
        """評估剛通過、側寫還沒重算：沒有分布列，不能給一排 0 篇。"""
        tccc = _members("tccc", "楊啓邦")
        _topic_speech("楊啓邦", "transport", meeting="第4屆第8次定期會 市政總質詢",
                      day="2026-09-01", source="tccc")
        compute_profiles()
        _pass("tccc")
        res = self.client.get(f"/api/people/{tccc['楊啓邦'].person_id}/profile").json()
        self.assertEqual([b["key"] for b in res["blocks"]], ["volume", "specificity", "followup"])
        compute_profiles()
        res = self.client.get(f"/api/people/{tccc['楊啓邦'].person_id}/profile").json()
        block = self._topics(res)
        # 市議員沒有委員會職掌
        self.assertEqual([i["key"] for i in block["indicators"]], ["topic_focus", "topic_breadth"])
        self.assertEqual(block["distribution"][0]["key"], "transport")


@override_settings(MEDIA_ROOT=MEDIA)
class TopicFilterTests(TestCase):
    """/api/articles?topic=：主領域是它，而且是那篇文章的來源通過評估的分類器分的。"""

    def setUp(self):
        self.ly_ok = _topic(_article("1", speaker="王立", brief=BRIEF, slides=1, with_image=False),
                            "finance")
        self.ly_other = _topic(_article("2", speaker="王立", brief=BRIEF, slides=1, with_image=False),
                               "finance", classifier="tccc-model#topic-v1")
        self.ly_labor = _topic(_article("3", speaker="王立", brief=BRIEF, slides=1, with_image=False),
                               "labor")
        _article("4", speaker="王立", brief=BRIEF, slides=1, with_image=False)   # 還沒分類
        self.tccc = _topic(_article("tccc-5", speaker="楊啓邦", source="tccc", brief=BRIEF, slides=1,
                                    with_image=False, meeting="第4屆第8次定期會"),
                           "finance", classifier="tccc-model#topic-v1")
        _pass("ly")

    def _ids(self, **params):
        return sorted(i["ivod_id"] for i in self.client.get("/api/articles", params).json()["items"])

    def test_only_the_passing_classifier_of_each_articles_source_counts(self):
        self.assertEqual(self._ids(topic="finance"), ["1"])
        # 臺中通過的是另一個分類器：那個分類器分的立法院文章仍然不算
        _pass("tccc", classifier="tccc-model#topic-v1")
        self.assertEqual(self._ids(topic="finance"), ["1", "tccc-5"])

    def test_any_means_classified_by_the_passing_version_in_any_area(self):
        self.assertEqual(self._ids(topic="any"), ["1", "3"])
        self.assertEqual(self._ids(topic="labor", speaker="王立", solo=1, has_brief=1), ["3"])

    def test_junk_is_refused(self):
        self.assertEqual(self.client.get("/api/articles?topic=space").status_code, 422)
        self.assertEqual(self.client.get("/api/articles?topic=財政經濟").status_code, 422)


@override_settings(MEDIA_ROOT=MEDIA)
class TopicStatsVersionTests(TestCase):
    """攔的 bug：側寫的議題列沒記是哪個分類器算的。上線的分類器換了、重算又失敗時，頁面把
    A 版算的數字掛上 B 版的名字，證據篩選（用 B 版）點進去是 0 篇。"""

    setUp = TopicProfileApiTests.setUp
    _profile = TopicProfileApiTests._profile
    _topics = TopicProfileApiTests._topics

    def test_topic_rows_record_their_classifier(self):
        rows = ProfileStat.objects.exclude(classifier="")
        self.assertTrue(rows.exists())
        self.assertEqual(set(rows.values_list("classifier", flat=True)), {CLASSIFIER})
        self.assertFalse(ProfileStat.objects.filter(indicator="speeches").exclude(classifier="").exists())

    def test_no_block_when_the_rows_came_from_another_classifier(self):
        from articles.profiles import topic_stats_stale

        self.assertFalse(topic_stats_stale())
        # B 版通過評估，但還沒重算（例如重算時資料庫被鎖住）
        _pass("ly", classifier="new-model#topic-v2")
        self.assertTrue(topic_stats_stale())
        self.assertIsNone(self._topics(self._profile("王立")))
        compute_profiles()
        self.assertFalse(topic_stats_stale())
        block = self._topics(self._profile("王立"))
        self.assertEqual(block["classifier"]["name"], "new-model#topic-v2")

    def test_committee_alignment_says_why_it_has_no_value(self):
        Membership.objects.filter(name="王立").update(committees=[])
        compute_profiles()
        got = {i["key"]: i for i in self._topics(self._profile("王立"))["indicators"]}
        self.assertEqual((got["committee_alignment"]["n"], got["committee_alignment"]["reason"]),
                         (0, "no_committee_data"))
        self.assertEqual(got["topic_focus"]["reason"], "")


# --- 追問 ---

JUDGE = "fake-model#followup-v1#abcd1234"


def _judge(classifier=JUDGE, passed=True):
    from articles.models import FollowUpEvaluation

    return FollowUpEvaluation.objects.create(
        classifier=classifier, labeled=30, correct=27 if passed else 20,
        accuracy=0.9 if passed else 20 / 30, passed=passed,
        ran_at=timezone.make_aware(datetime(2026, 10, 3, 9, 30)))


def _ask_brief(request, deadline="一個月內", response=""):
    return {**BRIEF, "asks": [{"request": request, "deadline": deadline, "response": response}]}


@override_settings(MEDIA_ROOT=MEDIA)
class FollowupProfileApiTests(TestCase):
    """王立在第 11 屆第 5 會期的要求，各種狀態都有（日期以今天往前後推，API 用的是今天）。"""

    def setUp(self):
        from datetime import timedelta

        from articles.models import FollowUp

        self.m = _members("ly", "王立", "甲")
        today = timezone.localdate()
        self.later = _speech("王立", day="2026-03-20", brief=BRIEF)

        def request(name, due, followed_by=None, checked_at=None, classifier=JUDGE, day="2026-03-02",
                    meeting=S5, deadline="一個月內", status=None):
            article = _speech("王立", meeting=meeting, day=day, brief=_ask_brief(name, deadline), status=status)
            return FollowUp.objects.create(
                article=article, ask_index=0, request=name, deadline_text=deadline, due_date=due,
                followed_by=followed_by, quote="清冊還沒給" if followed_by else "", classifier=classifier,
                checked=[followed_by.id] if followed_by else [], checked_at=checked_at)

        day = timedelta(days=1)
        self.followed = request("已追問的要求", today + 5 * day, followed_by=self.later)
        self.not_followed = request("未追問的要求", today - 100 * day, checked_at=timezone.now())
        self.watching = request("觀察中的要求", today - 3 * day)
        self.pending = request("待追蹤的要求", today + 2 * day)
        self.pending_today = request("今天到期的要求", today)
        request("期限換不出來的要求", None, deadline="儘快")
        request("別的會期", today + 9 * day, meeting=S4, day="2025-10-01")
        joint = _speech("王立、甲", day="2026-03-03", brief=_ask_brief("聯合質詢的要求"))
        FollowUp.objects.create(article=joint, ask_index=0, request="聯合質詢的要求", deadline_text="一個月內",
                                due_date=today + day)
        assign_sessions()
        self.s5 = Session.objects.get(name="第11屆第5會期")

    def _block(self, name="王立", **params):
        res = self.client.get(f"/api/people/{self.m[name].person_id}/profile",
                              {"session": self.s5.id, **params}).json()
        blocks = {b["key"]: b for b in res["blocks"]}
        self.assertEqual(list(blocks)[-1], "followup")
        return blocks["followup"]

    def test_without_a_passing_judge_only_pending_requests_and_no_rate(self):
        compute_profiles()
        block = self._block()
        self.assertEqual((block["title"], block["indicators"], block["judge"], block["unparsed"]),
                         ("追問", [], None, 1))
        # 已追問的那項還沒到期：判斷不算數，就是待追蹤；其他要靠判斷的狀態都不給
        self.assertEqual([(a["request"], a["state"], a["followed_by"], a["quote"]) for a in block["asks"]],
                         [("今天到期的要求", "pending", None, ""), ("待追蹤的要求", "pending", None, ""),
                          ("已追問的要求", "pending", None, "")])

    def test_the_block_never_exposes_judged_states_without_a_passing_judge(self):
        _judge(passed=False)
        _judge("other#followup-v0#x")       # 通過的是別的判斷器：這些要求的判斷都不算數
        compute_profiles()
        block = self._block()
        # 都等上線的判斷器重判（待重判），不從清單上消失；舊版本判的已追問、未追問都不給
        self.assertEqual([(a["request"], a["state"]) for a in block["asks"]],
                         [("未追問的要求", "rejudging"), ("觀察中的要求", "rejudging"),
                          ("今天到期的要求", "rejudging"), ("待追蹤的要求", "rejudging"),
                          ("已追問的要求", "rejudging")])
        self.assertEqual({(a["followed_by"], a["quote"]) for a in block["asks"]}, {(None, "")})
        rate, = block["indicators"]
        self.assertEqual((rate["value"], rate["n"], rate["reason"]), (None, 0, "rejudging"))
        self.assertEqual(block["judge"]["name"], "other#followup-v0#x")
        self.assertNotIn("清冊還沒給", str(block))

    def test_with_a_passing_judge_every_state_the_rate_and_the_judge(self):
        _judge()
        compute_profiles()
        block = self._block()
        self.assertEqual(block["judge"], {"name": JUDGE, "accuracy": 0.9, "labeled": 30,
                                          "evaluated_at": "2026-10-03T09:30:00+08:00"})
        # 依到期日排序
        self.assertEqual([(a["request"], a["state"]) for a in block["asks"]],
                         [("未追問的要求", "not_followed"), ("觀察中的要求", "watching"),
                          ("今天到期的要求", "pending"), ("待追蹤的要求", "pending"),
                          ("已追問的要求", "followed")])
        followed = block["asks"][-1]
        self.assertEqual(followed["article"], {"slug": self.followed.article.slug,
                                               "title": self.followed.article.title, "date": "2026-03-02"})
        self.assertEqual((followed["deadline"], followed["due_date"]),
                         ("一個月內", self.followed.due_date.isoformat()))
        self.assertEqual(followed["followed_by"], {"slug": self.later.slug, "title": self.later.title,
                                                   "date": "2026-03-20"})
        self.assertEqual(followed["quote"], "清冊還沒給")
        self.assertEqual((block["asks"][0]["followed_by"], block["asks"][0]["quote"]), (None, ""))
        rate, = block["indicators"]
        self.assertEqual((rate["key"], rate["label"], rate["unit"], rate["value"], rate["n"], rate["n_unit"],
                          rate["sample_ok"], rate["percentile"]),
                         ("followup_rate", "追問率", "%", 50.0, 2, "項", False, None))
        self.assertEqual(rate["evidence_url"],
                         f"/speaker/%E7%8E%8B%E7%AB%8B?source=ly&session={self.s5.id}#followups")

    def test_the_rate_waits_for_a_recompute_with_the_live_judge(self):
        """評估剛換版本、側寫還沒重算：舊版本的數字不掛新版本的名字。"""
        _judge()
        compute_profiles()
        FollowUp.objects.update(classifier="")      # 都是程式篩出來的（不必重判），只差側寫還沒重算
        _judge("new#followup-v2#y")
        block = self._block()
        self.assertEqual(block["indicators"], [])
        self.assertEqual(block["judge"]["name"], "new#followup-v2#y")

    def test_after_a_judge_switch_the_rate_is_withheld_until_his_requests_are_rejudged(self):
        """攔的 bug：新的判斷器通過、側寫重算之後，比率只用新判斷器判完的那幾項算——舊版本找到的
        追問都變成「觀察中」、不在分子裡，追問率掉到 0%，看起來像真的。"""
        _judge()
        FollowUp.objects.filter(pk=self.not_followed.pk).update(classifier="")   # 沒有候選：不必重判
        _judge("new#followup-v2#y")
        compute_profiles()
        block = self._block()
        rate, = block["indicators"]
        self.assertEqual((rate["key"], rate["value"], rate["n"], rate["percentile"], rate["reason"]),
                         ("followup_rate", None, 0, None, "rejudging"))
        self.assertEqual(rate["evidence_url"],
                         f"/speaker/%E7%8E%8B%E7%AB%8B?source=ly&session={self.s5.id}#followups")
        states = {a["request"]: a["state"] for a in block["asks"]}
        self.assertEqual(states["已追問的要求"], "rejudging")
        self.assertEqual(states["未追問的要求"], "not_followed")
        self.assertNotIn("清冊還沒給", str(block))
        stat = ProfileStat.objects.get(person_id=self.m["王立"].person_id, session=self.s5,
                                       indicator="followup_rate")
        self.assertEqual((stat.value, stat.n, stat.percentile), (None, 0, None))
        # 重判完（都出自新的判斷器）就照常給
        FollowUp.objects.exclude(classifier="").update(classifier="new#followup-v2#y")
        compute_profiles()
        rate, = self._block()["indicators"]
        self.assertEqual((rate["value"], rate["n"], rate.get("reason", "")), (50.0, 2, ""))

    def test_only_requests_whose_articles_have_a_page(self):
        _judge()
        compute_profiles()
        Article.objects.filter(pk=self.pending.article_id).update(status=ArticleStatus.FAILED)
        Article.objects.filter(pk=self.later.pk).update(status=ArticleStatus.PROCESSING)
        block = self._block()
        requests = {a["request"]: a for a in block["asks"]}
        self.assertNotIn("待追蹤的要求", requests)
        self.assertEqual((requests["已追問的要求"]["state"], requests["已追問的要求"]["followed_by"]),
                         ("followed", None))

    def test_someone_without_requests_still_gets_the_block(self):
        _judge()
        compute_profiles()
        block = self._block("甲")
        self.assertEqual((block["asks"], block["unparsed"]), ([], 0))
        rate, = block["indicators"]
        self.assertEqual((rate["value"], rate["n"]), (None, 0))
