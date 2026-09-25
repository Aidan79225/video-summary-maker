"""質詢卡：模型回應解析、schema 與提示組裝。純函式，不碰網路。"""
from __future__ import annotations

import pytest

from slidebox.domain.entities import Slide
from slidebox.domain.errors import SummarizerOutputInvalid
from slidebox.infrastructure.ollama_summarizer import (
    _BRIEF_SYSTEM,
    _build_brief_prompt,
    brief_schema,
    parse_brief_response,
)
from slidebox.usecases.brief import slides_digest

GOOD = """{
  "one_liner": "國防部三年編 82.4 億買無人機，交到部隊的不到一半",
  "key_numbers": [
    {"value": "82.4", "unit": "億元", "label": "三年累計編列",
     "quote": "累計編列八十二點四億元"}
  ],
  "asks": [
    {"request": "提出分機種交機時程清冊", "deadline": "一個月內", "response": "部長允諾"}
  ]
}"""


def test_parses_every_field():
    brief = parse_brief_response(GOOD)
    assert brief.one_liner.startswith("國防部")
    assert brief.key_numbers[0].value == "82.4"
    assert brief.key_numbers[0].unit == "億元"
    assert brief.key_numbers[0].quote == "累計編列八十二點四億元"
    assert brief.asks[0].deadline == "一個月內"
    assert brief.asks[0].response == "部長允諾"


def test_missing_fields_degrade_to_empty_instead_of_raising():
    """欄位缺失交給 validate／ground 決定，解析只做型別轉換。"""
    brief = parse_brief_response('{"one_liner": "一句話"}')
    assert brief.key_numbers == ()
    assert brief.asks == ()
    brief = parse_brief_response('{"one_liner": 5, "key_numbers": [{"value": 82.4}], "asks": ["x"]}')
    assert brief.one_liner == ""
    assert brief.key_numbers[0].value == ""


def test_non_json_and_non_object_responses_raise():
    with pytest.raises(SummarizerOutputInvalid):
        parse_brief_response("這不是 JSON")
    with pytest.raises(SummarizerOutputInvalid):
        parse_brief_response("[1, 2]")


def test_schema_requires_every_field_so_the_model_cannot_skip_them():
    schema = brief_schema()
    assert set(schema["required"]) == {"one_liner", "key_numbers", "asks"}
    number = schema["properties"]["key_numbers"]["items"]
    assert set(number["required"]) == {"value", "unit", "label", "quote"}
    ask = schema["properties"]["asks"]["items"]
    assert set(ask["required"]) == {"request", "deadline", "response"}


def test_prompt_carries_the_digest_and_the_retry_hint():
    digest = slides_digest((Slide(1, "第一段", ("重點一",), 0.0, detail="完整敘述"),))
    assert "第一段" in _build_brief_prompt(digest)
    assert "請修正" not in _build_brief_prompt(digest)
    hint = "上一次的輸出有這些問題，請修正後重新產出：one_liner 太短"
    assert hint in _build_brief_prompt(digest, hint)


def test_system_prompt_names_every_field_the_schema_expects():
    """模型只會產出提示裡指名的欄位；欄位名沒寫進提示，schema 也救不回內容。"""
    for name in ("one_liner", "key_numbers", "value", "unit", "label", "quote",
                 "asks", "request", "deadline", "response"):
        assert name in _BRIEF_SYSTEM
