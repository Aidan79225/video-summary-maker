"""Ollama 回應解析：正常、缺欄位、型別不對、空內容。"""
from __future__ import annotations

import json

import pytest

from slidebox.domain.errors import SummarizerOutputInvalid
from slidebox.infrastructure.ollama_summarizer import (
    slides_schema,
    parse_summary_response,
)


def test_parses_a_well_formed_payload():
    payload = json.dumps({"slides": [
        {"title": "開場", "bullets": ["打招呼", "介紹主題"], "timestamp": 0},
        {"title": "重點", "bullets": ["核心論點"], "timestamp": 125.5},
    ]})
    slides = parse_summary_response(payload)
    assert len(slides) == 2
    assert slides[0].index == 1
    assert slides[1].index == 2
    assert slides[0].title == "開場"
    assert slides[0].bullets == ("打招呼", "介紹主題")
    assert slides[1].timestamp == 125.5
    assert all(s.image_path is None for s in slides)


def test_accepts_timestamp_given_as_string():
    """schema 要求 number，但小模型偶爾仍給字串。"""
    payload = json.dumps({"slides": [
        {"title": "A", "bullets": ["x"], "timestamp": "90"},
    ]})
    assert parse_summary_response(payload)[0].timestamp == 90.0


def test_unparsable_timestamp_becomes_zero():
    payload = json.dumps({"slides": [
        {"title": "A", "bullets": ["x"], "timestamp": "第三分鐘"},
    ]})
    assert parse_summary_response(payload)[0].timestamp == 0.0


def test_missing_fields_fall_back_to_empty():
    """欄位缺失不在這裡 raise——交給 validate_slides 判斷是否要重試。"""
    payload = json.dumps({"slides": [{"title": "只有標題"}]})
    slide = parse_summary_response(payload)[0]
    assert slide.title == "只有標題"
    assert slide.bullets == ()
    assert slide.timestamp == 0.0


def test_bullets_given_as_a_single_string_is_wrapped():
    payload = json.dumps({"slides": [
        {"title": "A", "bullets": "只有一句", "timestamp": 5},
    ]})
    assert parse_summary_response(payload)[0].bullets == ("只有一句",)


def test_non_string_bullets_are_coerced():
    payload = json.dumps({"slides": [
        {"title": "A", "bullets": ["good", 42, None], "timestamp": 5},
    ]})
    assert parse_summary_response(payload)[0].bullets == ("good", "42")


def test_empty_slides_array_returns_empty_tuple():
    assert parse_summary_response(json.dumps({"slides": []})) == ()


def test_invalid_json_raises():
    with pytest.raises(SummarizerOutputInvalid):
        parse_summary_response("{ 這不是 JSON")


def test_missing_slides_key_raises():
    with pytest.raises(SummarizerOutputInvalid):
        parse_summary_response(json.dumps({"chapters": []}))


def test_schema_requires_the_three_fields():
    item = slides_schema(False)["properties"]["slides"]["items"]
    assert set(item["required"]) == {"title", "bullets", "timestamp"}
    assert item["properties"]["timestamp"]["type"] == "number"


def test_detail_is_carried_through_when_the_model_supplies_it():
    slides = parse_summary_response(
        '{"slides":[{"title":"章","bullets":["點"],"timestamp":0,"detail":"完整敘述"}]}'
    )
    assert slides[0].detail == "完整敘述"


def test_missing_or_non_string_detail_degrades_to_empty():
    """一般模式的回應本來就沒有 detail；缺欄位不該讓整批摘要作廢。"""
    slides = parse_summary_response(
        '{"slides":[{"title":"章","bullets":["點"],"timestamp":0},'
        '{"title":"章","bullets":["點"],"timestamp":0,"detail":123}]}'
    )
    assert slides[0].detail == ""
    assert slides[1].detail == ""


# --- 第三人稱改寫 ---


def test_rewrite_keeps_index_title_and_timestamp_from_the_original():
    from slidebox.domain.entities import Slide
    from slidebox.infrastructure.ollama_summarizer import parse_rewrite_response

    original = Slide(index=3, title="標題", bullets=("我們要求",), timestamp=90.0, detail="我們認為")
    slide = parse_rewrite_response(
        '{"bullets": ["講者要求"], "detail": "講者認為", "title": "被改掉的標題", "timestamp": 1}',
        original)
    assert (slide.index, slide.title, slide.timestamp) == (3, "標題", 90.0)
    assert slide.bullets == ("講者要求",)
    assert slide.detail == "講者認為"


def test_rewrite_in_normal_mode_never_gains_a_detail():
    from slidebox.domain.entities import Slide
    from slidebox.infrastructure.ollama_summarizer import parse_rewrite_response

    original = Slide(index=1, title="t", bullets=("我們",), timestamp=0.0, detail="")
    slide = parse_rewrite_response('{"bullets": ["講者"], "detail": "模型硬寫的一段"}', original)
    assert slide.detail == ""


def test_rewrite_with_empty_bullets_keeps_the_original_bullets():
    from slidebox.domain.entities import Slide
    from slidebox.infrastructure.ollama_summarizer import parse_rewrite_response

    original = Slide(index=1, title="t", bullets=("我們",), timestamp=0.0)
    assert parse_rewrite_response('{"bullets": [], "detail": ""}', original).bullets == ("我們",)


def test_rewrite_rejects_non_json():
    import pytest

    from slidebox.domain.entities import Slide
    from slidebox.domain.errors import SummarizerOutputInvalid
    from slidebox.infrastructure.ollama_summarizer import parse_rewrite_response

    with pytest.raises(SummarizerOutputInvalid):
        parse_rewrite_response("not json", Slide(1, "t", ("x",), 0.0))


def test_rewrite_prompt_tells_the_model_to_use_the_third_person():
    from slidebox.domain.entities import Slide
    from slidebox.infrastructure.ollama_summarizer import _REWRITE_SYSTEM, _build_rewrite_prompt

    assert "第三人稱" in _REWRITE_SYSTEM and "講者" in _REWRITE_SYSTEM
    prompt = _build_rewrite_prompt(Slide(1, "t", ("我們要求",), 0.0), "第 1 頁用了第一人稱「我們」")
    assert "我們要求" in prompt and "第一人稱" in prompt and "detail 請輸出空字串" in prompt
