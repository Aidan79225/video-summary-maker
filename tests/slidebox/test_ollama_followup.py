"""追問判斷器的 Ollama 呼叫：用假的 HTTP 回應，不碰網路也不碰 Ollama。"""
from __future__ import annotations

import json

import pytest

from slidebox.domain.entities import FollowUpPair, FollowUpResult
from slidebox.domain.errors import OperationCancelled, SummarizerOutputInvalid
from slidebox.infrastructure import ollama_summarizer as mod
from slidebox.usecases.followups import classifier_name, followup_schema

PAIR = FollowUpPair(
    request="要求衛福部一個月內提出長照人力補助方案",
    response="部長允諾一個月內提出",
    card="一句話：追問長照人力補助方案進度\n各段小標：\n- 長照人力缺口",
    excerpt="委員：上次要求的長照人力補助方案到現在還沒看到。",
)
QUOTE = "上次要求的長照人力補助方案到現在還沒看到"


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


def _reply(followed_up, quote="") -> str:
    return json.dumps({"followed_up": followed_up, "quote": quote}, ensure_ascii=False)


@pytest.fixture
def sent(monkeypatch):
    """記下送給 Ollama 的請求，回一個預設的答案；答案可以在測試裡換。"""
    record = {"reply": _reply(True, QUOTE)}

    def fake_post(url, body, timeout):
        record.update(url=url, body=body, timeout=timeout)
        record["response"] = FakeResponse(_stream(record["reply"]))
        return record["response"]

    monkeypatch.setattr(mod, "_post_json", fake_post)
    return record


def _judge(model="qwen3.5:9b", is_cancelled=None, num_ctx=32768):
    return mod.OllamaFollowUpJudge("http://gpu:11434/", model, num_ctx).judge(
        PAIR, lambda frac, status: None, is_cancelled)


def test_the_result_carries_the_verdict_the_quote_and_the_classifier(sent):
    assert _judge() == FollowUpResult(True, QUOTE, classifier_name("qwen3.5:9b"))


def test_the_classifier_name_is_the_model_that_actually_ran(sent):
    """名稱跟著這次真的用的模型走：評估通過的是哪個模型，就只有它判的算數。"""
    assert _judge(model="llama3").classifier.startswith("llama3#followup-v1#")


def test_the_request_goes_to_the_chat_endpoint_of_the_configured_model(sent):
    _judge(model="llama3")
    assert sent["url"] == "http://gpu:11434/api/chat"
    assert sent["body"]["model"] == "llama3"


def test_the_temperature_is_zero(sent):
    """同一對每次都要得到同一個判斷：標註集量到的準確率才代表上線後的行為。"""
    _judge()
    assert sent["body"]["options"]["temperature"] == 0


def test_thinking_is_turned_off(sent):
    """攔的 bug：qwen3.5 預設會先想幾千個 token 才回幾十個 token 的答案，
    一對從幾秒變成一分多鐘，每晚 200 對就是幾個小時佔著唯一的佇列。"""
    _judge()
    assert sent["body"]["think"] is False


def test_the_context_size_is_the_one_it_was_given(sent):
    """Ollama 換 num_ctx 會重新載入模型；判斷跟摘要排同一個佇列交錯跑。"""
    _judge(num_ctx=32768)
    assert sent["body"]["options"]["num_ctx"] == 32768


def test_the_format_is_the_followup_schema(sent):
    _judge()
    assert sent["body"]["format"] == followup_schema()


def test_the_pair_reaches_the_model(sent):
    _judge()
    system, user = sent["body"]["messages"]
    assert system["role"] == "system" and user["role"] == "user"
    assert "同一件具體的事" in system["content"]
    for part in (PAIR.request, PAIR.response, PAIR.card, PAIR.excerpt):
        assert part in user["content"]


def test_no_follow_up_comes_back_without_a_quote(sent):
    sent["reply"] = _reply(False, "部長答應的一個月早就過了")
    assert _judge() == FollowUpResult(False, "", classifier_name("qwen3.5:9b"))


def test_a_verdict_that_is_not_a_boolean_fails_the_judgment(sent):
    sent["reply"] = _reply("true", QUOTE)
    with pytest.raises(SummarizerOutputInvalid):
        _judge()


def test_cancelling_stops_the_stream(sent):
    with pytest.raises(OperationCancelled):
        _judge(is_cancelled=lambda: True)
    assert sent["response"].closed
