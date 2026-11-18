# -*- coding: utf-8 -*-
"""A question word only makes a point a "question" when what the point says sits next to it.

Real transcripts (ReXUCiDCbl8, 2026-11-16) have no punctuation: a speaker's whole turn is one clause, so any 為什麼
anywhere in a long speech made every point drawn from that turn a "question stated as fact" (22 of 184 points).
For a long clause without a question mark the question words now only count for the few characters around them
(conversation.QUESTION_REACH); short clauses and clauses with a real question mark behave exactly as before.
"""
from __future__ import annotations

import pytest

from slidebox.domain.entities import RawPoint
from slidebox.usecases import conversation as c

from .conversation_support import run, with_anchors
from .speaker_fixtures import make_speakers

TWO = make_speakers("participant", "participant")

# 真實逐字稿 T83 S2（沒有任何標點與空白）
EX3_TURN = ("我們的高地有了自來水阿伯打開水龍頭流出水的時候他掉眼淚了我也哭了這種等待一件微小的事"
            "但是卻對你日常生活影響這麼重大的事過去為什麼沒有人做但沒")
EX3_POINT = "解決高地自來水問題，改善居民日常生活品質"
FILLER = "山水花草鳥魚蟲獸石海雨雪雲風雷電星月光影"                      # 和下面的重點沒有任何相鄰兩字組相同


def is_question(turn_text: str, point: str) -> bool:
    return c._evidence(turn_text, c._grams(point))[1]


def test_a_rhetorical_question_next_to_nothing_the_point_says_is_not_a_question_point():
    assert is_question(EX3_TURN, EX3_POINT) is False


@pytest.mark.parametrize("turn_text, point", [
    ("我想請問今年的預算編列到底有沒有增加然後全面檢討所有補助項目的做法", "今年的預算編列增加"),            # 請問、有沒有
    ("各位覺得我們這樣調整補助的方向真的對嗎還是需要再重新討論過", "調整補助的方向正確"),                  # 嗎：問的內容在它前面
    ("市長是不是願意在這個月內完成高地自來水的全面檢討然後公開時程給大家看", "完成高地自來水的全面檢討"),    # 是不是：兩邊都有
    ("你們為什麼要把明年的補助調高三成而且完全沒有先跟大家討論過這件事", "把明年的補助調高三成"),            # 為什麼：問的內容在它後面
    ("這個預算有一半是用在老舊社區的整建工程嗎我覺得需要再多看看細項", "預算有一半用在老舊社區的整建工程"),
])
def test_a_point_that_says_what_the_question_asks_is_still_a_question_point(turn_text, point):
    assert len(turn_text) > c.LONG_CLAUSE_CHARS
    assert is_question(turn_text, point) is True


@pytest.mark.parametrize("marker", ["為什麼", "為何", "難道", "何必", "請問", "請教", "想問", "怎麼可能", "您怎麼", "你怎麼", "怎麼看"])
def test_what_follows_a_leading_question_word_is_the_question(marker):
    turn_text = f"{FILLER}{FILLER}{marker}預算增加了兩成{FILLER}{FILLER}"
    assert is_question(turn_text, "預算增加兩成") is True


@pytest.mark.parametrize("marker", ["嗎", "呢", "對吧", "是吧"])
def test_what_comes_before_a_trailing_question_word_is_the_question(marker):
    turn_text = f"{FILLER}{FILLER}預算增加了兩成{marker}{FILLER}{FILLER}"
    assert is_question(turn_text, "預算增加兩成") is True


@pytest.mark.parametrize("marker", ["是不是", "對不對", "好不好", "行不行", "有沒有", "能不能", "可不可以", "是否"])
def test_what_surrounds_a_middle_question_word_is_the_question(marker):
    turn_text = f"{FILLER}{FILLER}預算{marker}增加了兩成{FILLER}{FILLER}"
    assert is_question(turn_text, "預算增加兩成") is True


@pytest.mark.parametrize("marker", ["為什麼", "難道", "請問", "嗎", "有沒有", "是不是"])
def test_a_point_from_far_away_in_the_same_long_clause_is_not_a_question_point(marker):
    turn_text = f"預算增加了兩成{FILLER}{FILLER}{FILLER}{marker}{FILLER}{FILLER}{FILLER}"
    assert is_question(turn_text, "預算增加兩成") is False


def test_the_reach_is_a_few_characters_not_the_whole_clause():
    lead_before, lead_after = c.QUESTION_REACH["lead"]
    inside = f"{FILLER}{FILLER}為什麼{'山' * (lead_after - 4)}預算增加{FILLER}"            # 預算增加整個落在 lead_after 之內
    outside = f"{FILLER}{FILLER}為什麼{'山' * (lead_after + 2)}預算增加{FILLER}"           # 在 lead_after 之外
    assert is_question(inside, "預算增加兩成") is True
    assert is_question(outside, "預算增加兩成") is False


def test_a_real_question_mark_makes_the_whole_clause_a_question_however_long():
    long_question = f"預算增加了兩成{FILLER}{FILLER}{FILLER}為什麼會這樣{FILLER}？"
    assert is_question(long_question, "預算增加兩成") is True


@pytest.mark.parametrize("short_clause, point", [
    ("預算增加了還是減少了你覺得呢", "預算增加"),
    ("今年預算增加了嗎", "今年預算增加"),
    ("為什麼預算要增加", "預算增加"),
])
def test_a_short_clause_with_a_question_word_is_a_question_as_before(short_clause, point):
    assert len(short_clause) <= c.LONG_CLAUSE_CHARS
    assert is_question(short_clause, point) is True


def test_a_question_in_another_punctuated_clause_does_not_count_as_before():
    turn_text = "預算增加了兩成，這是一年來努力的成果，各位可能不知道，但是為什麼過去一直都沒有人願意去做這件事"
    assert is_question(turn_text, "預算增加兩成") is False


def test_a_point_that_shares_nothing_with_the_turn_still_looks_at_the_whole_turn():
    assert is_question(f"{FILLER}為什麼{FILLER}", "預算增加兩成") is True                    # 沒有任何子句共用字：照舊看整段


def test_a_clause_of_exactly_the_short_limit_is_still_a_short_clause():
    """攔的 bug：門檻差一個字（剛好 LONG_CLAUSE_CHARS 個字的子句被當成「很長」）。
    重點的內容在「為什麼」往後的範圍之外，兩段話只差一個字：24 個字照舊整段看（是問句），25 個字才只看附近（不是）。"""
    at_limit = "為什麼" + FILLER[:c.LONG_CLAUSE_CHARS - 10] + "預算增加了兩成"
    one_more = "為什麼" + FILLER[:c.LONG_CLAUSE_CHARS - 9] + "預算增加了兩成"

    assert (len(at_limit), len(one_more)) == (c.LONG_CLAUSE_CHARS, c.LONG_CLAUSE_CHARS + 1)
    assert is_question(at_limit, "預算增加兩成") is True
    assert is_question(one_more, "預算增加兩成") is False


def test_one_question_word_next_to_the_point_is_enough_when_another_one_is_far_from_it():
    """攔的 bug：要每一個問句字眼都靠近重點才算（all 而不是 any）：開頭那個「嗎」離重點很遠，結尾的「為什麼」就在重點旁邊。"""
    point = "追究過去為什麼沒有人做這件事"

    assert is_question(EX3_TURN, point) is True                                  # 只有結尾那個字眼：算
    assert is_question("你覺得這樣可以嗎" + EX3_TURN, point) is True               # 前面多一個離得遠的：還是算


# --- 經過整個檢查 ---


def kept_points(point_text, turn_text, quote, extra_lines=()):
    turns, good = with_anchors(("S1", "今天我們來談高地自來水的事情。"), ("S2", turn_text), *extra_lines)
    extra, result = run(RawPoint(1, point_text, (), quote), turns=turns, speakers=TWO, good=good)
    return extra, result


def test_the_real_monologue_point_is_published_now():
    extra, result = kept_points(EX3_POINT, EX3_TURN, "我們的高地有了自來水阿伯打開水龍頭流出水的時候")
    assert [p.text for p in extra] == [EX3_POINT]
    assert dict(result.dropped_by_code).get(c.QUESTION_STATED_AS_FACT, 0) == 0


def test_a_point_that_restates_the_rhetorical_question_is_still_dropped():
    extra, result = kept_points("過去沒有人做高地自來水", EX3_TURN, "對你日常生活影響這麼重大的事過去為什麼沒有人做")
    assert extra == []
    assert dict(result.dropped_by_code).get(c.QUESTION_STATED_AS_FACT) == 1


# --- 別人剛說過的內容：問句字眼照舊嚴格算 ---
# 一個人一邊複述對方剛提的方案、一邊反問（「月費2980喔 比標準版的4800便宜快四成 你不怕…嗎」）：那句問句問的是別的事，
# 但這個重點本來就是對方的主張，不能因為問句字眼離得遠就當成他自己的。（F10 的 p3#12，實驗室判定是歸錯人。）

PROPOSAL = ("S1", "我覺得要出一個便宜的輕量版 月費2980 這樣可以把小店拉進來")
RESTATE_TURN = ("S2", "月費2980喔 比標準版的4800便宜快四成 你不怕大客戶全部跳過來嗎 這個我真的有點擔心 一旦跳過來我們收入反而會掉")
RESTATE_POINT = "指出月費2980比標準版4800便宜近四成"
RESTATE_QUOTE = "月費2980喔 比標準版的4800便宜快四成"
UNRELATED = ("S1", "好 那今天先討論到這裡 大家有什麼想補充的 山水花草鳥魚蟲獸石海雨雪雲風雷電星月光影")


def restated_in(turn_line, point, quote, *before):
    turns, good = with_anchors(*before, turn_line)
    extra, result = run(RawPoint(len(before), point, (), quote), turns=turns, speakers=TWO, good=good)
    return extra, result


def restated(*before):
    return restated_in(RESTATE_TURN, RESTATE_POINT, RESTATE_QUOTE, *before)


def test_the_restating_turn_is_a_long_clause_with_a_question_far_from_the_point():
    assert len(RESTATE_TURN[1]) > c.LONG_CLAUSE_CHARS
    assert is_question(RESTATE_TURN[1], RESTATE_POINT) is False                 # 光看這一輪，問句問的是別的事


def test_a_point_the_other_speaker_just_proposed_keeps_the_strict_question_rule():
    extra, result = restated(PROPOSAL)
    assert extra == []
    assert dict(result.dropped_by_code).get(c.QUESTION_STATED_AS_FACT) == 1


def test_the_same_turn_is_published_when_nobody_said_the_content_before():
    extra, result = restated(UNRELATED)
    assert [p.text for p in extra] == [RESTATE_POINT]
    assert dict(result.dropped_by_code).get(c.QUESTION_STATED_AS_FACT, 0) == 0


def between(count):
    """count 段另一位（S1）說的、和重點毫不相干的話，中間夾著這位（S2）的幾句。"""
    lines = []
    for number in range(count):
        lines += [("S2", f"嗯好{number}"), ("S1", UNRELATED[1])]
    return lines


@pytest.mark.parametrize("closer_turns, strict", [
    (0, True),
    (c.ECHO_NEAR_TURNS - 1, True),                  # 對方的話是最近第 ECHO_NEAR_TURNS 段：還算
    (c.ECHO_NEAR_TURNS, False),                     # 再往前一段：太遠，不算
])
def test_only_the_nearest_other_speaker_turns_count_as_just_said(closer_turns, strict):
    extra, result = restated(PROPOSAL, *between(closer_turns))
    assert (extra == []) is strict
    assert dict(result.dropped_by_code).get(c.QUESTION_STATED_AS_FACT, 0) == (1 if strict else 0)


def test_the_speakers_own_earlier_words_do_not_count_as_somebody_else_saying_it():
    extra, result = restated(("S2", PROPOSAL[1]))
    assert [p.text for p in extra] == [RESTATE_POINT]


def test_a_bare_topic_word_in_the_other_speakers_turn_is_not_enough():
    extra, result = restated(("S1", "那個月費的事情 我們下次開會再談好了 山水花草鳥魚蟲獸石海雨雪雲風雷電星月光影"))
    assert [p.text for p in extra] == [RESTATE_POINT]


def test_words_the_other_speaker_shares_with_the_point_are_not_an_echo_only_numbers_are():
    """一般的詞（月費、標準版、便宜）幾乎每個討論都互相重複，不能因此把一般的回答當成複述。"""
    topic_talk = ("S1", "輕量版的月費我覺得要比標準版便宜很多 這樣可以把小店拉進來")
    assert len(c._grams(RESTATE_POINT) & c._grams(topic_talk[1])) >= 4                    # 共用的詞很多…
    extra, result = restated(topic_talk)
    assert [p.text for p in extra] == [RESTATE_POINT]                                    # …但沒有共用的數字


def test_a_different_number_is_not_an_echo():
    extra, result = restated(("S1", "我覺得要出一個便宜的輕量版 月費3980 這樣可以把小店拉進來"))
    assert [p.text for p in extra] == [RESTATE_POINT]


def test_a_number_with_letters_the_other_speaker_just_said_counts_too():
    """攔的 bug：只算「整個字組都是數字」的（2980），「5G」「4K」這種數字夾著字母的就算不出來。"""
    proposal = ("S1", "我覺得要出一個便宜的輕量版 搭配5G方案 這樣可以把小店拉進來")
    turn = ("S2", "搭配5G方案喔 比標準版的便宜很多 你不怕大客戶全部跳過來嗎 這個我真的有點擔心 一旦跳過來我們收入反而會掉")
    point, quote = "指出搭配5G方案比標準版便宜很多", "搭配5G方案喔 比標準版的便宜很多"
    shared = c._grams(proposal[1]) & c._grams(point)

    assert "5g" in shared and not any(gram.isdigit() for gram in shared)           # 共用的數字是「5g」，不是純數字
    assert len(turn[1]) > c.LONG_CLAUSE_CHARS and is_question(turn[1], point) is False   # 光看這一輪：問句問的是別的事

    extra, result = restated_in(turn, point, quote, proposal)
    assert extra == [] and dict(result.dropped_by_code).get(c.QUESTION_STATED_AS_FACT) == 1
    extra, result = restated_in(turn, point, quote, UNRELATED)
    assert [p.text for p in extra] == [point]
