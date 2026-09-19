"""查核成品轉 JSON。這是給 Django 的 HTTP 契約。"""
from __future__ import annotations

from factcheck.domain.entities import (
    CheckedClaim, ClaimKind, Evidence, ExtractedClaim, Method, SourceKind, Verdict,
)
from slidebox_api.factcheck_payload import factcheck_payload


def test_payload_shape():
    claim = ExtractedClaim(quote="3萬到5萬", kind=ClaimKind.LAW_ARTICLE, statement="罰鍰",
                           figures=("3萬到5萬",), law="醫療法", article="第24條")
    evidence = Evidence(SourceKind.LAW, "醫療法 第一百零六條", "https://law", "https://api", "三萬元")
    payload = factcheck_payload(
        [CheckedClaim(claim, 32.0, Verdict.SUPPORTED, Method.NUMERIC, "相同", (evidence,))],
        "qwen3.5:9b")
    assert payload["model"] == "qwen3.5:9b"
    [item] = payload["claims"]
    assert item["verdict"] == "supported"
    assert item["method"] == "numeric"
    assert item["kind"] == "law_article"
    assert item["timestamp"] == 32.0
    assert item["subject"] == {"law": "醫療法", "article": "第24條"}
    assert item["evidence"] == [{"source": "law", "title": "醫療法 第一百零六條",
                                 "official_url": "https://law", "api_url": "https://api",
                                 "excerpt": "三萬元"}]
