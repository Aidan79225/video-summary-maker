"""LYAPI 客戶端：查詢參數與錯誤轉譯。"""
from __future__ import annotations

import http.client
import urllib.error

import pytest

from factcheck.domain.errors import SourceUnavailable
from factcheck.infrastructure.lyapi import LyApi

from .lyapi_fakes import FakeFetch, load


def test_law_search_wraps_the_name_in_quotes():
    """攔的 bug：q 不加引號是模糊搜尋，實測搜「醫療法」會回傳所得稅法。"""
    fetch = FakeFetch({"/laws": load("laws_search_醫療法.json")})
    rows = LyApi(fetch=fetch).laws_by_name("醫療法")
    assert rows[0]["名稱"] == "醫療法"
    assert "q=%22%E9%86%AB%E7%99%82%E6%B3%95%22" in fetch.urls[0]


def test_law_contents_walks_every_page():
    pages = {
        "1": {"total_page": 2, "lawcontents": [{"條號": "第一條"}]},
        "2": {"total_page": 2, "lawcontents": [{"條號": "第二條"}]},
    }
    fetch = FakeFetch({"/law_contents": lambda q: pages[q["page"]]})
    rows = LyApi(fetch=fetch).law_contents("02533:2026-05-08-修正")
    assert [r["條號"] for r in rows] == ["第一條", "第二條"]


def test_bill_detail_returns_the_data_object():
    fetch = FakeFetch({"/bills/201110221870000": load("bill_201110221870000.json")})
    bill = LyApi(fetch=fetch).bill("201110221870000")
    assert bill["提案單位/提案委員"] == "行政院"


def test_connection_errors_become_source_unavailable():
    def broken(url):
        raise OSError("connection reset")
    with pytest.raises(SourceUnavailable):
        LyApi(fetch=broken).laws_by_name("醫療法")


def test_a_truncated_response_is_source_unavailable():
    """攔的 bug：連線中途斷掉是 http.client 的例外，不是 OSError，原本會讓整篇查核失敗。"""
    def truncated(url):
        raise http.client.IncompleteRead(b"")
    with pytest.raises(SourceUnavailable):
        LyApi(fetch=truncated).laws_by_name("醫療法")


def test_a_non_object_response_is_source_unavailable():
    with pytest.raises(SourceUnavailable):
        LyApi(fetch=lambda url: ["not", "a", "dict"]).bill("1")


def test_url_encodes_chinese_parameters():
    url = LyApi(base="https://ly.govapi.tw/v2").url("/bills", {"屆": 11})
    assert url == "https://ly.govapi.tw/v2/bills?%E5%B1%86=11"


def test_a_rate_limited_request_is_retried_with_backoff():
    """實測：打太快會 429（「LYAPI 取不到 /bills/202110224810000：HTTP Error 429」），
    重試幾次通常就過了，不必直接判該則主張無法查證。"""
    calls = {"count": 0}
    sleeps: list[float] = []

    def flaky(url):
        calls["count"] += 1
        if calls["count"] <= 2:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
        return {"bills": []}

    api = LyApi(fetch=flaky, sleep=sleeps.append)
    assert api.get("/bills") == {"bills": []}
    assert sleeps == [2.0, 4.0]


def test_persistent_rate_limiting_becomes_source_unavailable():
    def always_429(url):
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)

    with pytest.raises(SourceUnavailable):
        LyApi(fetch=always_429, sleep=lambda seconds: None).get("/bills")


def test_retry_after_header_is_honoured_and_capped_at_ten_seconds():
    import email.message

    headers = email.message.Message()
    headers["Retry-After"] = "30"
    calls = {"count": 0}
    sleeps: list[float] = []

    def fetch(url):
        calls["count"] += 1
        if calls["count"] == 1:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", headers, None)
        return {"bills": []}

    assert LyApi(fetch=fetch, sleep=sleeps.append).get("/bills") == {"bills": []}
    assert sleeps == [10.0]  # Retry-After: 30，但上限 10 秒
