"""議題分類器的 Ollama 呼叫：用假的 HTTP 回應，不碰網路也不碰 Ollama。"""
from __future__ import annotations

import json

import pytest

from slidebox.domain.entities import TopicLabel, TopicResult
from slidebox.domain.errors import OperationCancelled, SummarizerOutputInvalid
from slidebox.infrastructure import ollama_summarizer as mod
from slidebox.usecases.topics import topic_schema

LABELS = (
    TopicLabel("defense", "國防外交", "國防、軍事、外交、兩岸、僑務"),
    TopicLabel("welfare", "衛生福利", "醫療、健保、長照、社福、托育、食安"),
)
TEXT = "一句話：要求衛福部說明長照 3.0 的預算\n各段小標：\n- 長照人力缺口"


class FakeResponse:
    """模擬 urlopen 回傳的 NDJSON 串流。"""

    def __init__(self, lines):
        self._lines = lines
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def __iter__(self):
        for line in self._lines:
            yield line.encode("utf-8")


def _stream(payload: str) -> list[str]:
    lines = [json.dumps({"message": {"content": c}}) for c in payload]
    lines.append(json.dumps({"message": {"content": ""}, "done": True}))
    return lines


def _reply(primary, secondary=None) -> str:
    return json.dumps({"primary": primary, "secondary": secondary}, ensure_ascii=False)


@pytest.fixture
def sent(monkeypatch):
    """記下送給 Ollama 的請求，回一個預設的答案；答案可以在測試裡換。"""
    record = {"reply": _reply("衛生福利", "國防外交")}

    def fake_post(url, body, timeout):
        record.update(url=url, body=body, timeout=timeout)
        record["response"] = FakeResponse(_stream(record["reply"]))
        return record["response"]

    monkeypatch.setattr(mod, "_post_json", fake_post)
    return record


def _classify(model="qwen3.5:9b", is_cancelled=None, num_ctx=32768):
    return mod.OllamaTopicClassifier("http://gpu:11434/", model, num_ctx).classify(
        TEXT, LABELS, lambda frac, status: None, is_cancelled)


def test_the_result_maps_names_back_to_keys_and_names_the_classifier(sent):
    assert _classify() == TopicResult("welfare", "defense", "qwen3.5:9b#topic-v1")


def test_the_request_goes_to_the_chat_endpoint_of_the_configured_model(sent):
    _classify(model="llama3")
    assert sent["url"] == "http://gpu:11434/api/chat"
    assert sent["body"]["model"] == "llama3"


def test_the_temperature_is_zero(sent):
    """同一段文字每次都要分到同一個領域：評估集量到的準確率才代表上線後的行為。"""
    _classify()
    assert sent["body"]["options"]["temperature"] == 0


def test_the_context_size_is_the_one_it_was_given(sent):
    """攔的 bug：Ollama 換 num_ctx 會重新載入模型。分類跟摘要排同一個佇列交錯
    跑，兩邊不同的話每次切換都要多等一次載入。"""
    _classify(num_ctx=32768)
    assert sent["body"]["options"]["num_ctx"] == 32768


def test_the_format_is_the_label_schema(sent):
    _classify()
    assert sent["body"]["format"] == topic_schema(LABELS)


def test_the_text_and_the_label_names_reach_the_model(sent):
    _classify()
    system, user = sent["body"]["messages"]
    assert system["role"] == "system" and user["role"] == "user"
    assert "衛生福利" in system["content"]
    assert TEXT in user["content"]


def test_a_secondary_equal_to_the_primary_comes_back_as_null(sent):
    sent["reply"] = _reply("衛生福利", "衛生福利")
    assert _classify().secondary is None


def test_a_primary_outside_the_list_fails_the_classification(sent):
    sent["reply"] = _reply("財政經濟")
    with pytest.raises(SummarizerOutputInvalid):
        _classify()


def test_cancelling_stops_the_stream(sent):
    with pytest.raises(OperationCancelled):
        _classify(is_cancelled=lambda: True)
    assert sent["response"].closed


def test_thinking_is_turned_off(sent):
    """攔的 bug：qwen3.5 預設會先想幾千個 token 才回二十幾個 token 的答案，
    一篇從 2.5 秒變成一分多鐘，每晚 200 篇就是三個多小時佔著唯一的佇列。"""
    _classify()
    assert sent["body"]["think"] is False
