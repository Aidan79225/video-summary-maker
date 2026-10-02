"""議題分類的純邏輯：schema、提示與回應解析，不碰網路也不碰 Ollama。"""
from __future__ import annotations

import json

import pytest

from slidebox.domain.entities import TopicLabel
from slidebox.domain.errors import SummarizerOutputInvalid
from slidebox.usecases.topics import (
    PROMPT_VERSION,
    classifier_name,
    parse_topic_response,
    topic_schema,
    topic_system_prompt,
    topic_user_prompt,
)

LABELS = (
    TopicLabel("defense", "國防外交", "國防、軍事、外交、兩岸、僑務"),
    TopicLabel("welfare", "衛生福利", "醫療、健保、長照、社福、托育、食安"),
    TopicLabel("local", "地方建設／其他", "都市計畫、住宅、區里建設、議事程序、以上都不是的"),
)
NAMES = ["國防外交", "衛生福利", "地方建設／其他"]


def _reply(primary, secondary=None) -> str:
    return json.dumps({"primary": primary, "secondary": secondary}, ensure_ascii=False)


# --- schema ---


def test_the_model_chooses_among_label_names_not_keys():
    assert topic_schema(LABELS)["properties"]["primary"]["enum"] == NAMES


def test_the_secondary_is_a_name_or_null():
    assert topic_schema(LABELS)["properties"]["secondary"]["enum"] == [*NAMES, None]


def test_the_secondary_is_constrained_by_its_enum_not_by_a_type_union():
    """攔的 bug：寫成 type: ["string", "null"] 的話，llama.cpp 把 schema 轉文法時
    走「型別聯集」那條路、enum 被忽略，次領域就能是清單外的任意字串。"""
    assert "type" not in topic_schema(LABELS)["properties"]["secondary"]


def test_both_fields_are_required_so_the_model_cannot_skip_the_secondary():
    """小模型會省略選填欄位；次領域「沒有」要明講 null，不是漏寫。"""
    assert set(topic_schema(LABELS)["required"]) == {"primary", "secondary"}


def test_the_schema_follows_whatever_labels_the_caller_sent():
    """領域清單只有新聞服務那一份；GPU 端寫死的話，那邊加一個領域這邊不會知道。"""
    schema = topic_schema((TopicLabel("a", "甲"), TopicLabel("b", "乙")))
    assert schema["properties"]["primary"]["enum"] == ["甲", "乙"]


# --- 回應解析 ---


def test_names_are_mapped_back_to_keys():
    assert parse_topic_response(_reply("衛生福利", "國防外交"), LABELS) == ("welfare", "defense")


def test_a_null_secondary_stays_null():
    assert parse_topic_response(_reply("國防外交", None), LABELS) == ("defense", None)


def test_a_secondary_equal_to_the_primary_becomes_null():
    """提示詞要求不能相同，但模型偶爾照填——那等於它沒有找到第二個領域。"""
    assert parse_topic_response(_reply("衛生福利", "衛生福利"), LABELS) == ("welfare", None)


@pytest.mark.parametrize("primary", ["經濟", "", None, 3, "welfare"])
def test_a_primary_outside_the_list_fails_instead_of_guessing(primary):
    """主領域是整個指標的依據：對不上時挑「最接近的」等於替模型做決定。
    代碼（welfare）也不收——模型被要求的是名稱，回代碼表示它沒照格式走。"""
    with pytest.raises(SummarizerOutputInvalid):
        parse_topic_response(_reply(primary), LABELS)


def test_a_missing_primary_fails():
    with pytest.raises(SummarizerOutputInvalid):
        parse_topic_response(json.dumps({"secondary": None}), LABELS)


@pytest.mark.parametrize("payload", ["不是 JSON", "", "[]", '"國防外交"'])
def test_a_reply_that_is_not_an_object_fails(payload):
    with pytest.raises(SummarizerOutputInvalid):
        parse_topic_response(payload, LABELS)


def test_an_unknown_secondary_is_dropped_rather_than_failing_the_job():
    """次領域不進任何指標；為它讓整個工作失敗，這篇會每晚重送、每晚失敗，
    永遠進不了基礎文章。"""
    assert parse_topic_response(_reply("國防外交", "經濟"), LABELS) == ("defense", None)


def test_surrounding_whitespace_in_a_name_is_tolerated():
    assert parse_topic_response(_reply(" 衛生福利 ", None), LABELS) == ("welfare", None)


# --- 提示詞 ---


def test_every_label_name_and_description_reaches_the_prompt():
    prompt = topic_system_prompt(LABELS)
    for label in LABELS:
        assert label.label in prompt
        assert label.description in prompt


def test_the_prompt_never_shows_the_keys():
    """模型選的是名稱；提示裡出現代碼，它就可能回代碼，而那會被當成失敗。"""
    prompt = topic_system_prompt(LABELS)
    for label in LABELS:
        assert label.key not in prompt


def test_a_label_without_a_description_is_still_listed():
    prompt = topic_system_prompt((TopicLabel("a", "甲"), TopicLabel("b", "乙")))
    assert "甲" in prompt and "乙" in prompt


def test_the_prompt_states_the_primary_and_secondary_rules():
    prompt = topic_system_prompt(LABELS)
    assert "主要在問什麼" in prompt
    assert "null" in prompt
    assert "不能跟 primary 相同" in prompt


def test_the_text_to_classify_reaches_the_user_message():
    text = "一句話：要求衛福部說明長照 3.0 的預算\n各段小標：\n- 長照人力缺口"
    assert text in topic_user_prompt(text)


# --- 分類器名稱 ---


def test_the_classifier_name_carries_the_model_and_the_prompt_version():
    """新聞服務靠這串分辨不同版本分出來的結果：換模型或改提示詞都要變。"""
    assert PROMPT_VERSION == "topic-v1"
    assert classifier_name("qwen3.5:9b") == "qwen3.5:9b#topic-v1"
