"""追問判斷的純邏輯：schema、提示與回應解析，不碰網路也不碰 Ollama。"""
from __future__ import annotations

import json

import pytest

from slidebox.domain.entities import FollowUpPair
from slidebox.domain.errors import SummarizerOutputInvalid
from slidebox.usecases import followups
from slidebox.usecases.followups import (
    PROMPT_VERSION,
    classifier_name,
    followup_schema,
    followup_system_prompt,
    followup_user_prompt,
    parse_followup_response,
    prompt_fingerprint,
)

PAIR = FollowUpPair(
    request="要求衛福部一個月內提出長照人力補助方案",
    response="部長允諾一個月內提出",
    card="一句話：追問長照人力補助方案進度\n要求：\n- 下週提出補助方案（下週）\n各段小標：\n- 長照人力缺口",
    excerpt="委員：上次要求的長照人力補助方案到現在還沒看到，部長答應的一個月早就過了。",
)


def _reply(followed_up, quote="") -> str:
    return json.dumps({"followed_up": followed_up, "quote": quote}, ensure_ascii=False)


# --- schema ---


def test_the_schema_has_exactly_a_boolean_verdict_and_a_quote():
    assert followup_schema()["properties"] == {
        "followed_up": {"type": "boolean"},
        "quote": {"type": "string"},
    }


def test_both_fields_are_required_so_the_model_cannot_skip_the_quote():
    """小模型會省略選填欄位；「沒有引用」要它明講空字串，不是漏寫。"""
    assert set(followup_schema()["required"]) == {"followed_up", "quote"}


def test_the_verdict_comes_before_the_quote():
    """模型照 properties 的順序生成：先判斷，再去逐字稿找證據。"""
    assert list(followup_schema()["properties"]) == ["followed_up", "quote"]


# --- 回應解析 ---


def test_a_follow_up_comes_back_with_its_quote():
    quote = "上次要求的長照人力補助方案到現在還沒看到"
    assert parse_followup_response(_reply(True, quote)) == (True, quote)


def test_no_follow_up_comes_back_without_a_quote():
    assert parse_followup_response(_reply(False)) == (False, "")


def test_a_quote_attached_to_a_no_is_dropped():
    """提示詞要求沒有追問就留空，但模型偶爾照抄一句。留著的話，新聞服務那邊
    「沒有追問」的那一對旁邊會掛著一句看起來像證據的話。"""
    assert parse_followup_response(_reply(False, "部長答應的一個月早就過了")) == (False, "")


def test_a_yes_without_a_quote_is_passed_through_for_the_grounding_check():
    """攔的 bug：在這裡把它改成「沒有追問」，新聞服務的落地檢查就看不到它，
    ungrounded 的計數永遠是零——評估少了「模型說有、卻拿不出證據」這個訊號。"""
    assert parse_followup_response(_reply(True, "")) == (True, "")


def test_surrounding_whitespace_in_the_quote_is_trimmed():
    assert parse_followup_response(_reply(True, "  還沒看到 \n")) == (True, "還沒看到")


def test_a_quote_that_is_not_a_string_counts_as_no_quote():
    payload = json.dumps({"followed_up": True, "quote": None})
    assert parse_followup_response(payload) == (True, "")


@pytest.mark.parametrize("verdict", ["true", "false", 1, 0, None, "是"])
def test_a_verdict_that_is_not_a_boolean_fails_instead_of_guessing(verdict):
    """判斷就是這一個欄位：把 "true"、1 解讀成有追問，等於替模型做決定。"""
    with pytest.raises(SummarizerOutputInvalid):
        parse_followup_response(_reply(verdict))


def test_a_missing_verdict_fails():
    with pytest.raises(SummarizerOutputInvalid):
        parse_followup_response(json.dumps({"quote": "還沒看到"}, ensure_ascii=False))


@pytest.mark.parametrize("payload", ["不是 JSON", "", "[]", "true", None])
def test_a_reply_that_is_not_an_object_fails(payload):
    with pytest.raises(SummarizerOutputInvalid):
        parse_followup_response(payload)


# --- 提示詞 ---


def test_the_prompt_says_only_the_same_concrete_matter_counts():
    """同一個領域的別件事不算：不寫明的話，問過一次長照的人之後每次問長照都
    會被算成追問，追問率只是在量他多常講同一個領域。"""
    prompt = followup_system_prompt()
    assert "同一件具體的事" in prompt
    assert "同一個領域" in prompt and "不算" in prompt


def test_the_prompt_says_the_quote_must_be_verbatim_from_the_excerpt():
    """新聞服務只認逐字稿裡找得到的引用；從摘要卡抄或改寫過的，落地檢查會判不過。"""
    prompt = followup_system_prompt()
    assert "逐字稿片段" in prompt
    assert "原封不動" in prompt
    assert "空字串" in prompt


def test_every_part_of_the_pair_reaches_the_user_message():
    message = followup_user_prompt(PAIR)
    for part in (PAIR.request, PAIR.response, PAIR.card, PAIR.excerpt):
        assert part in message


def test_the_old_request_and_the_new_article_are_labelled_apart():
    """模型要分得出哪段是舊的要求、哪段是後來的發言，否則會從舊的要求裡抄引用。"""
    message = followup_user_prompt(PAIR)
    assert message.index(PAIR.request) < message.index(PAIR.card) < message.index(PAIR.excerpt)
    assert "先前的要求" in message
    assert "逐字稿片段" in message


def test_a_missing_response_is_said_out_loud_not_left_blank():
    """空白的段落會被模型當成格式錯誤或自行腦補；明講「沒有回應」才不會。"""
    message = followup_user_prompt(FollowUpPair(PAIR.request, "  ", PAIR.card, PAIR.excerpt))
    assert "沒有回應" in message


def test_a_missing_excerpt_is_said_out_loud_not_left_blank():
    message = followup_user_prompt(FollowUpPair(PAIR.request, PAIR.response, PAIR.card, ""))
    assert "沒有逐字稿" in message


# --- 判斷器名稱 ---


def test_the_classifier_name_carries_the_model_the_prompt_version_and_a_fingerprint():
    """新聞服務靠這串分辨不同版本判出來的結果：換模型或改提示詞都要變。"""
    assert PROMPT_VERSION == "followup-v1"
    assert classifier_name("qwen3.5:9b") == f"qwen3.5:9b#followup-v1#{prompt_fingerprint()}"
    assert len(prompt_fingerprint()) == 8


def test_a_different_model_is_a_different_classifier():
    assert classifier_name("llama3") != classifier_name("qwen3.5:9b")


def test_the_name_is_not_the_topic_classifiers_name():
    """兩種工作的評估各自存；名稱撞在一起的話，議題分類通過了評估，追問判斷
    就會被當成也通過了。"""
    from slidebox.domain.entities import TopicLabel
    from slidebox.usecases.topics import classifier_name as topic_name
    labels = (TopicLabel("a", "甲"), TopicLabel("b", "乙"))
    assert classifier_name("qwen3.5:9b") != topic_name("qwen3.5:9b", labels)


@pytest.mark.parametrize("name,extra", [
    ("_SYSTEM", "\n- 寧可判成有追問。\n"),
    ("_USER", "\n請仔細判斷。"),
])
def test_editing_a_prompt_template_is_a_different_classifier(monkeypatch, name, extra):
    """改了提示詞範本卻忘了升 PROMPT_VERSION，名稱也要變。"""
    before = classifier_name("qwen3.5:9b")
    monkeypatch.setattr(followups, name, getattr(followups, name) + extra)
    assert classifier_name("qwen3.5:9b") != before


def test_editing_the_schema_is_a_different_classifier(monkeypatch):
    before = classifier_name("qwen3.5:9b")
    original = followups.followup_schema

    def looser():
        schema = original()
        schema["required"] = ["followed_up"]
        return schema

    monkeypatch.setattr(followups, "followup_schema", looser)
    assert classifier_name("qwen3.5:9b") != before
