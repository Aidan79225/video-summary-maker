"""Ollama 實作：提示詞、schema 與回應解析。HTTP 用假的 chat 函式替換。"""
from __future__ import annotations

import json
from datetime import date

import pytest

from factcheck.domain.entities import ClaimKind, Evidence, SourceKind, Speech, Verdict
from factcheck.domain.errors import ModelOutputInvalid
from factcheck.infrastructure.ollama import (
    EXTRACT_SCHEMA,
    MAX_CLAIMS,
    OllamaClaimExtractor,
    OllamaTextJudge,
    build_judge_messages,
    parse_claims,
    parse_judgement,
)

SPEECH = Speech(speaker="邱慧洳", date=date(2026, 8, 25), meeting="第11屆第5會期第23次會議",
                transcript="00:32 他的行政罰鍰從現行的3萬到5萬提升到5到25萬")


def _claim(**overrides):
    item = {"quote": "他的行政罰鍰從現行的3萬到5萬", "kind": "law_article",
            "statement": "醫療法現行罰鍰為 3 萬到 5 萬元", "figures": ["3萬到5萬"],
            "law": "醫療法", "article": "第24條", "bill_keywords": "", "proposer": ""}
    item.update(overrides)
    return item


def test_claims_are_parsed():
    claims = parse_claims(json.dumps({"claims": [_claim()]}))
    assert len(claims) == 1
    claim = claims[0]
    assert claim.kind == ClaimKind.LAW_ARTICLE
    assert claim.figures == ("3萬到5萬",)
    assert claim.law == "醫療法"


def test_claims_with_an_unknown_kind_are_dropped():
    assert parse_claims(json.dumps({"claims": [_claim(kind="opinion")]})) == []


def test_claims_without_a_quote_are_dropped():
    assert parse_claims(json.dumps({"claims": [_claim(quote="  ")]})) == []


def test_a_missing_statement_falls_back_to_the_quote():
    claim = parse_claims(json.dumps({"claims": [_claim(statement="")]}))[0]
    assert claim.statement == claim.quote


def test_at_most_max_claims_are_kept():
    payload = json.dumps({"claims": [_claim() for _ in range(MAX_CLAIMS + 3)]})
    assert len(parse_claims(payload)) == MAX_CLAIMS


def test_broken_json_is_model_output_invalid():
    with pytest.raises(ModelOutputInvalid):
        parse_claims("not json")
    with pytest.raises(ModelOutputInvalid):
        parse_claims(json.dumps({"items": []}))


@pytest.mark.parametrize("raw, verdict", [
    ("supported", Verdict.SUPPORTED),
    ("contradicted", Verdict.CONTRADICTED),
    ("insufficient", Verdict.UNVERIFIABLE),
    ("maybe", Verdict.UNVERIFIABLE),
])
def test_judgements_map_to_verdicts(raw, verdict):
    judgement = parse_judgement(json.dumps(
        {"verdict": raw, "evidence_quote": "處新臺幣三萬元以上", "reason": "理由"}))
    assert judgement.verdict == verdict
    assert judgement.evidence_quote == "處新臺幣三萬元以上"


def test_the_extractor_sends_a_constrained_deterministic_request():
    seen = {}

    def chat(host, body, timeout):
        seen.update(host=host, body=body)
        return json.dumps({"claims": [_claim()]})

    claims = OllamaClaimExtractor("http://gpu:11434", "qwen3.5:9b", 32768,
                                  chat=chat).extract(SPEECH)
    assert claims[0].law == "醫療法"
    body = seen["body"]
    assert body["model"] == "qwen3.5:9b"
    assert body["format"] == EXTRACT_SCHEMA
    assert body["stream"] is False
    assert body["options"] == {"num_ctx": 32768, "temperature": 0}
    prompt = body["messages"][-1]["content"]
    assert "邱慧洳" in prompt and "3萬到5萬" in prompt


def test_the_judge_numbers_its_evidence():
    evidence = [Evidence(SourceKind.LAW, "醫療法 第一百零六條", "https://a", "https://b",
                         "處新臺幣三萬元以上五萬元以下罰鍰")]
    prompt = build_judge_messages("罰鍰 3 萬到 5 萬", evidence)[-1]["content"]
    assert "[1] 醫療法 第一百零六條" in prompt
    assert "處新臺幣三萬元以上五萬元以下罰鍰" in prompt

    judge = OllamaTextJudge("http://gpu:11434", "qwen3.5:9b", 32768, chat=lambda h, b, t: json.dumps(
        {"verdict": "supported", "evidence_quote": "三萬元以上", "reason": "相同"}))
    assert judge.judge("罰鍰 3 萬到 5 萬", evidence).verdict == Verdict.SUPPORTED
