# -*- coding: utf-8 -*-
"""引文照抄得差不多（同音字、數字寫法、多一個「也」）：容許少數幾個字不同，往下一律用逐字稿的原文。

真實影片（ReXUCiDCbl8）有 6 個 quote 只差幾個字就被丟掉（診斷的 near_verbatim）：模型寫「我也看到二重書宏道…」，
逐字稿是「我看到二重書宏道…」。容許的字數很少（每 TOLERATED_EDIT_CHARS 個字 1 個、最多 TOLERATED_EDITS_MAX 個），
而且只能有一輪對得上（不論說話者）；找到之後內容檢查與顯示都用逐字稿的原文，不用模型寫的字。
"""
from __future__ import annotations

import random

import pytest

from slidebox.domain.entities import Point, RawPoint, RawSlide, Turn
from slidebox.usecases import conversation as c
from slidebox.usecases import diarized_pipeline as dp
from slidebox.usecases.quotes import (
    MAX_TOLERANT_QUOTE_CHARS,
    TOLERATED_EDIT_CHARS,
    TOLERATED_EDITS_MAX,
    locate_quote,
    tolerated_edits,
)
from slidebox.usecases.speakers import squash

from .conversation_support import run, with_anchors
from .diarized_support import FakeConversation
from .speaker_fixtures import make_speakers, make_turns

CITABLE = frozenset({"S1", "S2"})
TWO = make_speakers("participant", "participant")


def turn(index: int, speaker: str, text: str) -> Turn:
    return Turn(index, float(index), float(index) + 0.9, speaker, text)


def edit_distance(a: str, b: str) -> int:
    """整串 a 變成整串 b 最少要改幾個字（換、增、刪各算一個）。"""
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j - 1] + (x != y), previous[j] + 1, current[j - 1] + 1))
        previous = current
    return previous[-1]


# 真實的例子（逐字稿 T76，診斷裡的第 5 個）：模型的 quote 多了一個「也」
REAL_TURN = ("我看到很多人努力改變了台北縣這個城市脫胎換骨的那一刻我看到二重書宏道從髒亂變美麗變成台北縣最大片的公園"
             "我也看到陶瓷老街改頭換帽看到櫻哥陶博館從一個大骷髏變成是國際享譽的博物館")
REAL_QUOTE = ("我也看到二重書宏道從髒亂變美麗變成台北縣最大片的公園"
              "我也看到陶瓷老街改頭換帽看到櫻哥陶博館從一個大骷髏變成是國際享譽的")
REAL_EXCERPT = ("我看到二重書宏道從髒亂變美麗變成台北縣最大片的公園"
                "我也看到陶瓷老街改頭換帽看到櫻哥陶博館從一個大骷髏變成是國際享譽的")


def test_the_allowance_is_one_character_per_ten_and_never_more_than_three():
    assert (TOLERATED_EDIT_CHARS, TOLERATED_EDITS_MAX) == (10, 3)
    assert [tolerated_edits(n) for n in (0, 8, 9, 10, 19, 20, 29, 30, 59, 60, 200)] == [0, 0, 0, 1, 1, 2, 2, 3, 3, 3, 3]


def test_a_quote_with_one_extra_word_is_found_and_the_transcripts_own_words_come_back():
    turns = [turn(75, "S1", "好請說"), turn(76, "S2", REAL_TURN), turn(77, "S1", "謝謝")]
    assert REAL_EXCERPT in REAL_TURN and REAL_QUOTE not in REAL_TURN                        # 嚴格比對一定對不上

    found = locate_quote(REAL_QUOTE, turns[1], turns, CITABLE)

    assert found.turn is turns[1] and found.problem is None and found.kind is None
    assert (found.edits, found.excerpt) == (1, REAL_EXCERPT)


def test_the_turn_is_found_even_when_the_model_cited_another_one():
    turns = [turn(75, "S1", "好請說"), turn(76, "S2", REAL_TURN), turn(77, "S1", "謝謝")]

    found = locate_quote(REAL_QUOTE, turns[0], turns, CITABLE)

    assert found.turn is turns[1] and found.excerpt == REAL_EXCERPT


@pytest.mark.parametrize("said, quote, edits", [
    ("我們今年編了三千萬元的預算來補助偏鄉的托育服務", "我們今年編了三千萬元的預算來補助偏鄉的託育服務", 1),             # 托 → 託
    ("我們今年編了三千萬元的預算來補助偏鄉的托育服務", "我們今年編了三千萬元的預算來補助偏鄉的托育服務啊", 1),             # 多一個字
    ("我們今年編了三千萬元的預算來補助偏鄉的托育服務", "我們今年編了三千萬的預算來補助偏鄉的托育服務", 1),               # 少一個字
    ("平均提前三十分鐘到場報到並且完成簽名手續後入座", "平均提前30分鐘到場報到並且完成簽名手續後入座", 2),              # 三十 → 30
])
def test_homophones_variant_characters_and_number_notation_are_tolerated(said, quote, edits):
    turns = [turn(0, "S1", "今天討論這件事"), turn(1, "S2", said)]

    found = locate_quote(quote, turns[0], turns, CITABLE)

    assert found.turn is turns[1], (quote, found.problem)
    assert found.edits == edits and squash(found.excerpt) == squash(said)[:len(squash(found.excerpt))]


def test_punctuation_and_spacing_inside_the_transcript_stay_in_the_excerpt():
    turns = [turn(0, "S1", "今天討論這件事"), turn(1, "S2", "各位好，我們今年編了三千萬元的預算，來補助偏鄉的托育服務，謝謝。")]

    found = locate_quote("我們今年編了三千萬元的預算來補助偏鄉的託育服務", turns[0], turns, CITABLE)

    assert found.turn is turns[1]
    assert found.excerpt == "我們今年編了三千萬元的預算，來補助偏鄉的托育服務"             # 逐字稿的標點原樣留著


# 容許的字數：每 10 個字 1 個、最多 3 個。一段不重複的長文字，抄一段、換掉其中幾個字
POOL = ("預算成效期程公開試辦三個月半年追加同意討論說明理由補助托育服務偏鄉長照交通水電教育醫療環境安全"
        "經濟社會保險退休勞工稅收財政審議條例修正草案規劃執行監督評估報告資料統計調查研究")
SUBSTITUTES = "甲乙丙丁戊己庚辛壬癸"


def distinct_text(length: int, seed: int = 3) -> str:
    rng = random.Random(seed)
    return "".join(rng.sample(POOL, length))


def substituted(text: str, count: int) -> str:
    """把 text 裡平均分散在中間的 count 個字換成沒出現過的字。"""
    step = len(text) // (count + 1)
    chars = list(text)
    for number in range(count):
        chars[step * (number + 1)] = SUBSTITUTES[number]
    return "".join(chars)


@pytest.mark.parametrize("length, changed, found", [
    (9, 1, False),                                  # 9 個字：不容許
    (10, 1, True), (10, 2, False),                  # 每 10 個字 1 個
    (20, 2, True), (20, 3, False),
    (30, 3, True), (30, 4, False),
    (45, 3, True), (45, 4, False),                  # 最多 3 個
])
def test_only_as_many_characters_may_differ_as_the_length_allows(length, changed, found):
    base = distinct_text(60)
    turns = [turn(0, "S1", "今天討論這件事"), turn(1, "S2", f"開頭一些話{base}之後還有一些話")]
    quote = substituted(base[:length], changed)

    result = locate_quote(quote, turns[0], turns, CITABLE)

    assert (result.turn is turns[1]) is found, (length, changed, result.problem)


def test_the_tolerance_can_be_switched_off_to_ask_why_a_quote_is_not_verbatim():
    turns = [turn(75, "S1", "好請說"), turn(76, "S2", REAL_TURN)]

    strict = locate_quote(REAL_QUOTE, turns[0], turns, CITABLE, tolerant=False)

    assert strict.turn is None and strict.kind == "not_found" and strict.excerpt is None


def test_a_verbatim_quote_is_located_as_before_without_any_tolerance_bookkeeping():
    turns = [turn(0, "S1", "今天討論這件事"), turn(1, "S2", REAL_TURN)]

    found = locate_quote(REAL_EXCERPT, turns[0], turns, CITABLE)

    assert found.turn is turns[1] and found.excerpt is None and found.edits == 0


# --- 一定只有一輪對得上 ---


def test_a_quote_that_comes_as_close_to_two_turns_is_not_used():
    turns = [turn(0, "S1", "我認為預算應該先編三個月等成效出來再追加"), turn(1, "S2", "我認為預算應該先編三個月等成果出來再追加")]

    found = locate_quote("我認為預算應該先編三個月等成慮出來再追加", turns[0], turns, CITABLE)         # 兩輪都只差一個字

    assert found.turn is None and found.kind == "not_found"


def test_a_near_match_in_a_voice_that_cannot_be_cited_is_not_used():
    turns = [turn(0, "S0", "我們今年編了三千萬元的預算來補助偏鄉的托育服務"), turn(1, "S1", "好的")]

    found = locate_quote("我們今年編了三千萬元的預算來補助偏鄉的託育服務", turns[1], turns, CITABLE)

    assert found.turn is None


def test_a_cited_voice_does_not_get_the_credit_when_an_uncitable_voice_said_nearly_the_same():
    said = "我們今年編了三千萬元的預算來補助偏鄉的托育服務"
    turns = [turn(0, "S0", said), turn(1, "S1", "我們今年編了三千萬元的預算來補助偏鄉的托育服務啊"), turn(2, "S2", "好")]

    found = locate_quote("我們今年編了三千萬元的預算來補助偏鄉的託育服務", turns[1], turns, CITABLE)

    assert found.turn is None                                       # 其他聲音也只差一個字：分不出是誰說的


def test_a_strict_holder_is_never_replaced_by_a_closer_looking_turn():
    turns = [turn(0, "S1", "我們今年編了三千萬元的預算來補助偏鄉的托育服務"), turn(1, "S2", "我們今年編了三千萬元的預算來補助偏鄉的託育服務")]

    found = locate_quote("我們今年編了三千萬元的預算來補助偏鄉的托育服務", turns[1], turns, CITABLE)

    assert found.turn is turns[0] and found.excerpt is None         # 一字不差的在 T0；容許只在沒有一字不差的時候才用


UNIQUE_LONG = "".join(distinct_text(60, seed=seed) for seed in range(1, 6))                # 300 個字，沒有重複的一段


@pytest.mark.parametrize("length, found", [
    (MAX_TOLERANT_QUOTE_CHARS - 1, True),
    (MAX_TOLERANT_QUOTE_CHARS, True),               # 剛好是上限：還比
    (MAX_TOLERANT_QUOTE_CHARS + 1, False),          # 多一個字：不比
    (MAX_TOLERANT_QUOTE_CHARS + 20, False),
])
def test_a_quote_longer_than_the_comparison_limit_is_not_tolerated(length, found):
    turns = [turn(0, "S1", "今天討論這件事"), turn(1, "S2", UNIQUE_LONG)]
    quote = UNIQUE_LONG[:length]

    assert (locate_quote(substituted(quote, 1), turns[0], turns, CITABLE).turn is turns[1]) is found
    assert locate_quote(quote, turns[0], turns, CITABLE).turn is turns[1]                    # 一字不差還是可以


def test_a_turn_whose_characters_cannot_be_mapped_back_to_its_text_is_not_compared():
    """標音符號分開寫的字（e 加上重音符號）在比對時會合成一個字，位置就對不回逐字稿的原文：寧可不比，也不顯示位置錯的原文。"""
    said = "今年的預算大約增加了兩成左右而且還有咖啡的補助"
    quote = "今年的預算大約增加了兩成佐右"
    plain = [turn(0, "S1", "今天討論這件事"), turn(1, "S2", said)]
    accented = [turn(0, "S1", "今天討論這件事"), turn(1, "S2", said + "cafe\u0301")]

    found = locate_quote(quote, plain[0], plain, CITABLE)
    assert found.turn is plain[1] and (found.excerpt, found.edits) == ("今年的預算大約增加了兩成左右", 1)
    assert locate_quote(quote, accented[0], accented, CITABLE).turn is None


def test_a_tolerated_match_is_always_close_unique_and_inside_the_turn():
    """性質：隨機改幾個字，找到的話一定是原來那一輪，原文在那一輪裡，和 quote 差不超過容許的字數。"""
    rng = random.Random(21)
    for _ in range(150):
        texts = [distinct_text(rng.randint(30, 40), seed=rng.randint(0, 10_000)) for _ in range(5)]
        turns = [turn(i, rng.choice(["S1", "S2"]), t) for i, t in enumerate(texts)]
        index = rng.randrange(len(turns))
        start = rng.randint(0, 6)
        base = texts[index][start:start + rng.randint(12, 24)]
        chars = list(base)
        for position in rng.sample(range(2, len(chars) - 2), rng.randint(0, 2)):
            chars[position] = rng.choice(SUBSTITUTES)
        quote = "".join(chars)

        found = locate_quote(quote, turns[(index + 1) % len(turns)], turns, CITABLE)

        if found.turn is not None:
            assert found.turn.speaker in CITABLE
            if found.excerpt is not None:
                assert found.excerpt in turns[found.turn.index].text
                assert edit_distance(squash(quote), squash(found.excerpt)) <= tolerated_edits(len(squash(quote)))
                assert found.edits == edit_distance(squash(quote), squash(found.excerpt))


# --- 經過整個檢查：往下一律用逐字稿的原文 ---

SAID = "我認為今年的預算應該先編三個月等成效出來再追加"
QUOTE_WITH_TYPO = "我認為今年的預算應該先編三個月等成效出來在追加"               # 再 → 在


def tolerated_run(point_text, quote, said=SAID, **options):
    turns, good = with_anchors(("S1", "今天我們來談預算的編列方式。"), ("S2", said))
    return run(RawPoint(1, point_text, (), quote), turns=turns, speakers=TWO, good=good, **options)


def test_a_point_whose_quote_has_a_typo_is_kept_and_counted_as_tolerated():
    extra, result = tolerated_run("主張預算先編三個月，成效出來再追加", QUOTE_WITH_TYPO)

    assert [(p.speaker, p.turn) for p in extra] == [("S2", 1)]
    assert result.tolerated == 1 and result.rescued == 0
    assert any("逐字稿不同" in problem and "T1" in problem for problem in result.problems)


def test_a_lead_in_is_looked_up_with_the_transcripts_words_and_not_the_typo_in_the_quote():
    """引言（「我引用…的說法：」）是靠 quote 在這一輪裡的位置往前找的；用模型寫錯的字找不到位置，轉述就當成他自己的主張。"""
    said = "我引用市場上的說法：預算要增加三千億才夠而且要盡快執行，但這不是我的立場。"
    point = "指出預算要增加三千億才夠"
    exact, _ = tolerated_run(point, "預算要增加三千億才夠而且要盡快執行", said)
    extra, result = tolerated_run(point, "預算要增加三千億才夠而且要儘快執行", said)           # 盡 → 儘

    assert exact == []
    assert extra == [] and dict(result.dropped_by_code) == {c.REPORTED_SPEECH: 1}


# 局長回答時先複述了一句（你說一個便當只有兩樣青菜），接著講自己的做法；quote 只引自己的做法那一半
RAISED = "局長，備援便當的品質也要談，有家長拍照給我，說一個便當只有兩樣青菜，雞肉也沒看到，孩子根本吃不飽。"
REPLY = ("你說一個便當只有兩樣青菜，這個我們有看到照片，是第一天備援業者趕工的狀況，我們已經要求每餐至少三樣青菜，"
         "熱量不得低於六百五十大卡，稽查員到現場抽看，不合格的當場退回。")
OWN_PART = "我們已經要求每餐至少三樣青菜，熱量不得低於六百五十大卡，稽查員到現場抽看"


def reply_run(quote):
    turns, good = with_anchors(("S2", RAISED), ("S1", REPLY))
    return run(RawPoint(1, "規範備援便當青菜數量及熱量標準並加強稽查", (), quote), turns=turns, speakers=TWO, good=good)


def test_the_restating_clause_outside_the_quoted_part_stays_outside_when_the_quote_has_a_typo():
    """quote 的範圍是用 quote 在這一輪裡的位置找的；用模型寫錯的字找不到位置，那句複述就被算進來，自己的做法被當成複述丟掉。"""
    exact, _ = reply_run(OWN_PART)
    extra, result = reply_run(OWN_PART.replace("要求", "要球"))                                  # 求 → 球

    assert [p.speaker for p in exact] == ["S1"]
    assert [p.speaker for p in extra] == ["S1"] and result.tolerated == 1 and dict(result.dropped_by_code) == {}


# 只差一、兩個字，但那幾個字有意思（否定、數字的數值、意思相反的說法）：不算照抄，和以前一樣丟掉（bad_quote）
MEANING_SAID = "這個預算我們不能再增加了因為財政壓力真的很大"


@pytest.mark.parametrize("said, quote, point", [
    (MEANING_SAID, "這個預算我們能再增加了因為財政壓力真的很大", "主張預算可以再增加"),                     # 少了「不」
    ("這個預算我們可以再增加了因為財政壓力真的很大", "這個預算我們不可以再增加了因為財政壓力真的很大", "主張預算不可以再增加"),   # 多了「不」
    ("這個預算我們今年增加了三十億元因為財政壓力很大", "這個預算我們今年增加了二十億元因為財政壓力很大", "指出預算增加二十億元"),   # 三十 → 二十
    ("這個預算我們今年增加了三成因為財政壓力很大真的", "這個預算我們今年減少了三成因為財政壓力很大真的", "指出預算減少三成"),       # 增加 → 減少
    ("我們明年的預算要編三個月的因為財政壓力很大喔", "我們去年的預算要編三個月的因為財政壓力很大喔", "指出去年的預算編三個月"),       # 明年 → 去年
    ("我們每隔3週就會重新檢討一次預算執行的情況", "我們每隔3個月就會重新檢討一次預算執行的情況", "指出每隔3個月檢討預算執行"),     # 3週 → 3個月：數字一樣、單位不同
    ("預算增加了但是沒有增加太多", "預算增加了但是沒有說增加太多", "指出預算增加了"),                                  # 沒有增加 → 沒有說增加：否定管不到了
])
def test_a_difference_in_a_negation_a_number_or_an_opposite_is_not_tolerated(said, quote, point):
    extra, result = tolerated_run(point, quote, said)

    assert extra == [] and result.tolerated == 0
    assert dict(result.dropped_by_code) == {c.BAD_QUOTE: 1}
    assert any("照抄原文" in problem for problem in result.problems)


def test_the_same_number_written_another_way_is_tolerated():
    said = "平均提前三十分鐘到場報到並且完成簽名手續後入座"
    extra, result = tolerated_run("指出要提前三十分鐘到場報到", "平均提前30分鐘到場報到並且完成簽名手續後入座", said)

    assert [p.turn for p in extra] == [1] and result.tolerated == 1


def test_a_yes_no_form_is_not_a_negation_when_the_wording_of_it_changes():
    """「是不是」「是否」都是在問：不是否定。把「不」「否」當成否定的話，兩邊的否定數目對不上，就被當成改了意思丟掉。"""
    said = "我想知道這個預算今年是不是有增加我們還在討論因為財政很緊張"
    quote = "我想知道這個預算今年是否有增加我們還在討論因為財政很緊張"
    extra, result = tolerated_run("詢問今年的預算是不是有增加", quote, said)

    assert [p.text for p in extra] == ["詢問今年的預算是不是有增加"]
    assert result.tolerated == 1


def test_the_safe_mode_shows_the_transcripts_words_and_not_the_models():
    extra, result = tolerated_run("主張預算先編三個月，成效出來再追加", QUOTE_WITH_TYPO, quote_only=True)

    assert [p.text for p in extra] == [f"「{SAID}」"]
    assert result.tolerated == 1


def test_an_unsupported_point_is_shown_with_the_transcripts_words_when_it_can_be_rescued():
    extra, result = tolerated_run("說明與主題完全無關的火星基地計畫", QUOTE_WITH_TYPO)

    assert [p.text for p in extra] == [f"「{SAID}」"]
    assert result.rescued == 1 and result.tolerated == 1


def test_only_the_points_that_end_up_published_are_counted_as_tolerated():
    # 同一位說話者的兩個點文字一樣：第二個被當成重複丟掉，不算
    turns, good = with_anchors(("S1", "今天我們來談預算的編列方式。"), ("S2", SAID))
    first = RawPoint(1, "主張預算先編三個月，成效出來再追加", (), QUOTE_WITH_TYPO)
    again = RawPoint(1, "主張預算先編三個月，成效出來再追加", (), QUOTE_WITH_TYPO)
    extra, result = run(first, again, turns=turns, speakers=TWO, good=good)

    assert len(extra) == 1 and result.tolerated == 1


def test_a_page_that_is_not_kept_does_not_count_its_tolerated_points():
    turns, good = with_anchors(("S1", "今天我們來談預算的編列方式。"), ("S2", SAID))
    point = RawPoint(1, "主張預算先編三個月，成效出來再追加", (), QUOTE_WITH_TYPO)
    result = c.validate_points([RawSlide("預算", (point,))], turns, TWO, c.chunk_turns(turns, TWO)[0])

    assert result.kept == 0 and result.tolerated == 0                   # 一頁至少要 2 個點


# --- 逐窗的結果與整份摘要 ---

LINES = (
    ("S1", "今天我們來談預算的編列方式"),
    ("S2", SAID),
    ("S1", "那你覺得我們應該怎麼處理才合適"),
    ("S2", "我認為公共托育的名額一定要先增加"),
)
VERBATIM = (RawPoint(0, "詢問今天要談預算的編列方式", quote="今天我們來談預算的編列方式"),
            RawPoint(3, "主張公共托育的名額要先增加", quote="公共托育的名額一定要先增加"))
TOLERATED = RawPoint(1, "主張預算先編三個月，成效出來再追加", (), QUOTE_WITH_TYPO)


def outcomes_for(*points):
    turns = make_turns(*LINES)
    reply = (RawSlide("預算", tuple(points)),)
    allocations = tuple(c.Allocation(w, 1, 3) for w in c.chunk_turns(turns, TWO, 10**6))
    return dp.summarize_windows(FakeConversation((), default=lambda call: reply), allocations, turns, TWO,
                                lambda frac, status: None, lambda: None, lambda: False)


def test_a_window_outcome_says_how_many_of_its_kept_points_needed_the_tolerance():
    (outcome,) = outcomes_for(*VERBATIM, TOLERATED)

    assert (outcome.proposed, outcome.kept, outcome.tolerated) == (3, 3, 1)
    (clean,) = outcomes_for(*VERBATIM)
    assert clean.tolerated == 0


def test_the_counts_of_a_whole_summary_and_its_log_line_mention_the_tolerance():
    outcomes = outcomes_for(*VERBATIM, TOLERATED)

    counts = dp.attribution_counts(outcomes)

    assert counts.tolerated == 1
    line = dp.summary_line(counts)
    assert "其中 1 個的 quote 和逐字稿差了幾個字" in line
    assert "差了幾個字" not in dp.summary_line(dp.attribution_counts(outcomes_for(*VERBATIM)))


def test_cutting_pages_takes_their_tolerated_points_off_the_count():
    def page(title, tolerated):
        point = Point(speaker="S2", turn=1, start=10.0, text=title, evidence=title)
        return c.CheckedSlide(title, (point, point), tolerated=tolerated)

    result = c.ValidationResult(slides=(page("第一頁標題文字", 1), page("第二頁標題文字", 1)), proposed=4, kept=4,
                                problems=(), tolerated=2)

    fitted, cut = dp.fit_to_range(result, 1)

    assert cut == 1 and fitted.tolerated == 1
