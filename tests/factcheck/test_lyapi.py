"""LYAPI 客戶端：查詢參數與錯誤轉譯。"""
from __future__ import annotations

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


def test_a_non_object_response_is_source_unavailable():
    with pytest.raises(SourceUnavailable):
        LyApi(fetch=lambda url: ["not", "a", "dict"]).bill("1")


def test_url_encodes_chinese_parameters():
    url = LyApi(base="https://ly.govapi.tw/v2").url("/bills", {"屆": 11})
    assert url == "https://ly.govapi.tw/v2/bills?%E5%B1%86=11"
