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
