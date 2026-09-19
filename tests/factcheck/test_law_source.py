"""法條取證：用實測的醫療法資料。"""
from __future__ import annotations

from datetime import date

from factcheck.domain.entities import ClaimKind, ExtractedClaim, SourceKind
from factcheck.infrastructure.law_source import LawSource, official_law_url, version_on
from factcheck.infrastructure.lyapi import LyApi

from .lyapi_fakes import FakeFetch, load

SPEECH_DAY = date(2026, 8, 25)


def _claim(law="醫療法", article="第24條"):
    return ExtractedClaim(quote="他的行政罰鍰從現行的3萬到5萬", kind=ClaimKind.LAW_ARTICLE,
                          statement="醫療法現行罰鍰 3 萬到 5 萬", figures=("3萬到5萬",),
                          law=law, article=article)


def _source():
    fetch = FakeFetch({
        "/laws": load("laws_search_醫療法.json"),
        "/laws/02533/versions": load("law_versions_02533.json"),
        "/law_contents": load("law_contents_02533_現行_subset.json"),
    })
    return LawSource(LyApi(fetch=fetch)), fetch


def test_the_cited_article_comes_first_then_articles_that_reference_it():
    """委員說第 24 條，罰鍰其實在第 106 條（「違反第二十四條第二項規定者…」）。
    只取第 24 條的話，數字永遠對不上。"""
    evidence = _source()[0].find(_claim(), SPEECH_DAY)
    titles = [e.title for e in evidence]
    assert titles[0].startswith("醫療法 第二十四條")
    assert any("第一百零六條" in t for t in titles)
    assert any("三萬元以上五萬元以下" in e.excerpt for e in evidence)


def test_evidence_names_the_version_and_links_to_official_and_api_sources():
    evidence = _source()[0].find(_claim(), SPEECH_DAY)
    first = evidence[0]
    assert first.source == SourceKind.LAW
    assert "2026-05-08" in first.title
    assert first.official_url == official_law_url("醫療法")
    assert first.official_url.startswith("https://law.moj.gov.tw/")
    assert first.api_url.startswith("https://ly.govapi.tw/v2/law_contents?")


def test_the_version_in_force_on_the_speech_day_is_used():
    versions = load("law_versions_02533.json")["lawversions"]
    assert version_on(versions, date(2026, 8, 25))["版本編號"] == "02533:2026-05-08-修正"
    assert version_on(versions, date(2025, 1, 1))["版本編號"] == "02533:2023-05-30-修正"
    assert version_on(versions, date(1980, 1, 1)) is None


def test_a_law_that_does_not_exist_yields_nothing():
    assert _source()[0].find(_claim(law="不存在的法"), SPEECH_DAY) == []


def test_a_claim_without_an_article_number_yields_nothing():
    """沒有條號時不去整部法律裡找「剛好有這個數字」的條文——那等於拿答案找題目，
    只會讓「相符」變得廉價。"""
    assert _source()[0].find(_claim(article=""), SPEECH_DAY) == []


def test_contents_are_fetched_once_per_version():
    source, fetch = _source()
    source.find(_claim(), SPEECH_DAY)
    source.find(_claim(article="第106條"), SPEECH_DAY)
    assert sum("/law_contents" in u for u in fetch.urls) == 1


def test_penalty_articles_are_prioritized_over_non_penalty_referencing_articles():
    """罰則條文在後面但有具體數字，應該優先於前面的敘述條文。"""
    fetch = FakeFetch({
        "/laws": load("laws_search_醫療法.json"),
        "/laws/02533/versions": load("law_versions_02533.json"),
        "/law_contents": {
            "total_page": 1,
            "lawcontents": [
                {"條號": "第二十四條", "內容": "醫療人員應當…"},
                {"條號": "第五十條", "內容": "違反第二十四條…得予警告"},  # non-penalty reference
                {"條號": "第七十五條", "內容": "違反第二十四條…得予警告"},  # non-penalty reference
                {"條號": "第一百零六條", "內容": "違反第二十四條第二項…處三萬元以上五萬元以下罰鍰"},  # penalty
            ]
        }
    })
    evidence = LawSource(LyApi(fetch=fetch)).find(_claim(), SPEECH_DAY)
    titles = [e.title for e in evidence]
    # 第 24 條優先
    assert titles[0].startswith("醫療法 第二十四條")
    # 第 106 條（罰則）應該在第 50 條或 75 條之前
    idx_106 = next(i for i, t in enumerate(titles) if "第一百零六條" in t)
    idx_50 = next((i for i, t in enumerate(titles) if "第五十條" in t), -1)
    if idx_50 >= 0:
        assert idx_106 < idx_50


def test_an_invalid_article_number_yields_nothing():
    """第0條或超出範圍的條號不會造成 ValueError。"""
    assert _source()[0].find(_claim(article="第0條"), SPEECH_DAY) == []
