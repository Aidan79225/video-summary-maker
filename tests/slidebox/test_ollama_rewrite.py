"""第三人稱改寫的 Ollama 呼叫：用假的 HTTP 回應，不碰網路也不碰 Ollama。"""
from __future__ import annotations

import json

import pytest

from slidebox.domain.entities import Slide
from slidebox.domain.errors import SummarizerOutputInvalid
from slidebox.infrastructure import ollama_summarizer as mod

SLIDE = Slide(3, "公車服務", ("我們要求一個月內提出改善計畫",), 600.0)


class FakeResponse:
    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for line in self._lines:
            yield line.encode("utf-8")


@pytest.fixture
def sent(monkeypatch):
    record = {"reply": json.dumps({"bullets": ["講者要求一個月內提出改善計畫"], "detail": ""},
                                  ensure_ascii=False)}

    def fake_post(url, body, timeout):
        record.update(body=body)
        lines = [json.dumps({"message": {"content": record["reply"]}}),
                 json.dumps({"message": {"content": ""}, "done": True})]
        return FakeResponse(lines)

    monkeypatch.setattr(mod, "_post_json", fake_post)
    return record


def _rewrite():
    return mod.OllamaSlideRewriter("http://gpu:11434", "qwen3.5:9b", 32768).rewrite(
        SLIDE, "第 3 頁用了第一人稱「我們」", lambda frac, status: None)


def test_thinking_is_turned_off(sent):
    """攔的 bug：qwen3.5 預設先「思考」，改寫偶爾整段都進了 thinking、content 是空的，
    回來的是空字串（「改寫的回應不是合法的 JSON：Expecting value」）。改寫一頁幾百字不需要思考。"""
    _rewrite()
    assert sent["body"]["think"] is False


def test_the_rewritten_bullets_replace_the_old_ones(sent):
    assert _rewrite().bullets == ("講者要求一個月內提出改善計畫",)


def test_an_empty_reply_is_reported_as_invalid_output(sent):
    """use case 靠這個例外型別決定「這頁沒改掉，往下走」，不能變成別的例外讓整個工作停掉。"""
    sent["reply"] = ""
    with pytest.raises(SummarizerOutputInvalid):
        _rewrite()
