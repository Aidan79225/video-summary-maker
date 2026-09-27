"""法條原文：用實測存下來的 LYAPI 回應當假資料（醫療法）。"""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
from datetime import date
from pathlib import Path

from django.test import SimpleTestCase

from articles.law_source import (
    LawSource,
    LawUnavailable,
    LyApi,
    article_number,
    official_law_url,
    to_chinese,
    version_on,
)

FIXTURES = Path(__file__).parent / "fixtures"
SPEECH_DAY = date(2026, 8, 25)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeFetch:
    """依路徑回應。值可以是 dict，或吃 query 回 dict 的函式。"""

    def __init__(self, routes: dict):
        self.routes = routes
        self.urls: list[str] = []

    def __call__(self, url: str):
        self.urls.append(url)
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.removeprefix("/v2")
        if path not in self.routes:
            raise OSError(f"沒有這個路徑的假資料：{path}")
        route = self.routes[path]
        if callable(route):
            query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            return route(query)
        return route


def _source():
    fetch = FakeFetch({
        "/laws": load("laws_search_醫療法.json"),
        "/laws/02533/versions": load("law_versions_02533.json"),
        "/law_contents": load("law_contents_02533_現行_subset.json"),
    })
    return LawSource(LyApi(fetch=fetch, sleep=lambda s: None)), fetch


class LawSourceTests(SimpleTestCase):
    def test_the_cited_article_comes_first_then_the_penalty_article_that_references_it(self):
        """委員說第 24 條，罰鍰其實在第 106 條（「違反第二十四條第二項規定者…」）。
        只附第 24 條的話，讀者看不到那個 3 萬到 5 萬。"""
        sources = _source()[0].find("醫療法", "第24條", SPEECH_DAY)
        self.assertEqual(sources[0]["article"], "第二十四條")
        self.assertTrue(any(s["article"] == "第一百零六條" for s in sources))
        self.assertTrue(any("三萬元以上五萬元以下" in s["excerpt"] for s in sources))

    def test_a_source_names_the_version_and_links_to_official_and_api_pages(self):
        first = _source()[0].find("醫療法", "第24條", SPEECH_DAY)[0]
        self.assertEqual(first["law"], "醫療法")
        self.assertIn("2026-05-08", first["title"])
        self.assertEqual(first["official_url"], official_law_url("醫療法"))
        self.assertTrue(first["official_url"].startswith("https://law.moj.gov.tw/"))
        self.assertTrue(first["api_url"].startswith("https://ly.govapi.tw/v2/law_contents?"))

    def test_the_version_in_force_on_the_speech_day_is_used(self):
        versions = load("law_versions_02533.json")["lawversions"]
        self.assertEqual(version_on(versions, date(2026, 8, 25))["版本編號"],
                         "02533:2026-05-08-修正")
        self.assertEqual(version_on(versions, date(2025, 1, 1))["版本編號"],
                         "02533:2023-05-30-修正")
        self.assertIsNone(version_on(versions, date(1980, 1, 1)))

    def test_unknown_laws_and_missing_articles_yield_nothing(self):
        source = _source()[0]
        self.assertEqual(source.find("不存在的法", "第24條", SPEECH_DAY), [])
        self.assertEqual(source.find("醫療法", "", SPEECH_DAY), [])
        self.assertEqual(source.find("醫療法", "第0條", SPEECH_DAY), [])

    def test_contents_are_fetched_once_per_version(self):
        source, fetch = _source()
        source.find("醫療法", "第24條", SPEECH_DAY)
        source.find("醫療法", "第106條", SPEECH_DAY)
        self.assertEqual(sum("/law_contents" in u for u in fetch.urls), 1)

    def test_law_search_wraps_the_name_in_quotes(self):
        """攔的 bug：q 不加引號是模糊搜尋，實測搜「醫療法」會先回所得稅法。"""
        fetch = FakeFetch({"/laws": load("laws_search_醫療法.json")})
        LyApi(fetch=fetch).laws_by_name("醫療法")
        self.assertIn("q=%22%E9%86%AB%E7%99%82%E6%B3%95%22", fetch.urls[0])

    def test_law_contents_walk_every_page(self):
        pages = {
            "1": {"total_page": 2, "lawcontents": [{"條號": "第一條"}]},
            "2": {"total_page": 2, "lawcontents": [{"條號": "第二條"}]},
        }
        fetch = FakeFetch({"/law_contents": lambda q: pages[q["page"]]})
        rows = LyApi(fetch=fetch).law_contents("02533:2026-05-08-修正")
        self.assertEqual([r["條號"] for r in rows], ["第一條", "第二條"])

    def test_rate_limits_are_retried_and_other_failures_become_unavailable(self):
        calls = {"n": 0}

        def flaky(url):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
            return load("laws_search_醫療法.json")

        api = LyApi(fetch=flaky, sleep=lambda s: None)
        self.assertEqual(api.laws_by_name("醫療法")[0]["名稱"], "醫療法")

        def broken(url):
            raise http.client.IncompleteRead(b"")

        with self.assertRaises(LawUnavailable):
            LyApi(fetch=broken).laws_by_name("醫療法")
        with self.assertRaises(LawUnavailable):
            LyApi(fetch=lambda url: ["not", "a", "dict"]).laws_by_name("醫療法")

    def test_article_helpers(self):
        self.assertEqual(article_number("第106條"), 106)
        self.assertEqual(article_number("第24條"), 24)
        self.assertIsNone(article_number(""))
        self.assertEqual(to_chinese(106), "一百零六")
        self.assertEqual(to_chinese(110), "一百一十")
        self.assertEqual(to_chinese(24), "二十四")
