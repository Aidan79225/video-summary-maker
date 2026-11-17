# -*- coding: utf-8 -*-
"""照抄得差不多的引文：找到的那一段，起點和終點不能靠猜的。

容許差幾個字之後，找到的那一段（excerpt）要拿去做「是不是在轉述別人」「前面是不是引言」這類看位置的檢查。
如果同樣少的字數也能對上別的起點或終點（引文第一個字或最後一個字寫錯、第二個字漏掉、同一輪裡有兩段一樣接近…），
就不知道模型指的是哪一段；猜錯一個字，那一段就可能少了「網友說」的「網」，或把引言的冒號擠到範圍外，轉述就檢查不出來。
這種引文一律不用（和以前一樣丟掉）。
"""
from __future__ import annotations

import pytest

from slidebox.domain.entities import RawPoint, Turn
from slidebox.usecases import conversation as c
from slidebox.usecases.quotes import locate_quote

from .conversation_support import run, with_anchors
from .speaker_fixtures import make_speakers

CITABLE = frozenset({"S1", "S2"})
TWO = make_speakers("participant", "participant")


def turn(index: int, speaker: str, text: str) -> Turn:
    return Turn(index, float(index), float(index) + 0.9, speaker, text)


# --- 起點與終點只有一種可能才用 ---

STRETCH = "我們今年編了三千萬元的預算來補助偏鄉的托育服務"
IN_THE_MIDDLE = f"好的那我先說一下{STRETCH}這樣大家就清楚了"                    # 前後都還有字


def locate(quote: str, said: str = IN_THE_MIDDLE):
    turns = [turn(0, "S1", "今天討論這件事"), turn(1, "S2", said)]
    return locate_quote(quote, turns[0], turns, CITABLE), turns[1]


@pytest.mark.parametrize("quote", [
    "我們今年編了三千萬元的預算來補助偏鄉的託育服務",             # 換掉中間一個字
    "我們今年編了三千萬元的預算來也補助偏鄉的托育服務",           # 多了中間一個字
    "我們今年編了三千萬的預算來補助偏鄉的托育服務",               # 少了中間一個字
])
def test_a_difference_in_the_middle_is_tolerated_and_the_stretch_is_the_transcripts_own(quote):
    found, holder = locate(quote)

    assert found.turn is holder and found.excerpt == STRETCH and found.edits == 1


@pytest.mark.parametrize("quote", [
    "甲們今年編了三千萬元的預算來補助偏鄉的托育服務",             # 第一個字寫錯：也可能是多了一個字、從第二個字開始
    "我們今年編了三千萬元的預算來補助偏鄉的托育服甲",             # 最後一個字寫錯
    "甲我們今年編了三千萬元的預算來補助偏鄉的托育服務",           # 開頭多一個字：可能是前一個字寫錯
    "我們今年編了三千萬元的預算來補助偏鄉的托育服務甲",           # 結尾多一個字：可能是後一個字寫錯
    "我今年編了三千萬元的預算來補助偏鄉的托育服務",               # 漏了第二個字：也可能是第一個字寫錯、從第二個字開始
    "我們今年編了三千萬元的預算來補助偏鄉的托育務",               # 漏了倒數第二個字
])
def test_a_difference_at_the_edge_that_could_also_shift_the_stretch_is_not_used(quote):
    found, _ = locate(quote)

    assert found.turn is None and found.kind == "not_found"


def test_the_same_edge_difference_is_fine_when_the_transcript_leaves_no_other_choice():
    """前後沒有別的字可以對：多出來的那一個字只可能是多的，起點與終點只有一種可能。"""
    found, holder = locate("甲我們今年編了三千萬元的預算來補助偏鄉的托育服務甲", STRETCH)

    assert found.turn is holder and found.excerpt == STRETCH and found.edits == 2


def test_two_stretches_of_one_turn_that_are_equally_close_to_the_quote_are_not_used():
    said = "預算大約增加兩成左右而且還有很多其他的事情要說明預算大約增加了兩成大左右這樣的說法"
    found, _ = locate("預算大約增加了兩成左右", said)                 # 前一段少一個「了」、後一段多一個「大」

    assert found.turn is None and found.kind == "not_found"


def test_a_turn_that_is_just_as_close_but_has_no_single_start_still_counts_as_a_competing_turn():
    """另一輪也一樣接近、只是前後有字所以起點終點不確定：不能因此被當成沒有競爭者，不然就變成只有前一輪說過。"""
    quote = STRETCH + "啊"                                                   # 多了一個字：只有前後都沒有字的那一輪才確定
    exact = turn(1, "S1", STRETCH)
    surrounded = turn(2, "S2", IN_THE_MIDDLE)
    intro = turn(0, "S1", "今天討論這件事")

    found = locate_quote(quote, intro, [intro, exact], CITABLE)
    assert found.turn is exact and found.excerpt == STRETCH                    # 沒有別輪競爭：用
    assert locate_quote(quote, intro, [intro, exact, surrounded], CITABLE).turn is None


# --- 往下的檢查：切錯一個字，轉述就看不出來 ---


def tolerated_run(point_text, quote, said):
    turns, good = with_anchors(("S1", "今天我們來談預算的編列方式。"), ("S2", said))
    return run(RawPoint(1, point_text, (), quote), turns=turns, speakers=TWO, good=good)


def test_a_reported_frame_is_not_lost_because_the_quote_left_out_a_character_of_it():
    said = "網友說這家公司去年虧損了五百億，但實際的數字不是這樣。"
    verbatim, _ = tolerated_run("指出這家公司去年虧損了五百億", "網友說這家公司去年虧損了五百億", said)
    slipped, result = tolerated_run("指出這家公司去年虧損了五百億", "網說這家公司去年虧損了五百億", said)      # 少了「友」

    assert verbatim == []                                               # 一字不差的時候當然擋得住
    assert slipped == [] and result.tolerated == 0                      # 少一個字也不能因此放行


def test_a_lead_in_is_not_lost_because_the_quote_starts_with_a_wrong_character():
    said = "我引用市場上的說法：預算要增加三千億才夠而且要盡快執行，但這不是我的立場。"
    verbatim, _ = tolerated_run("指出預算要增加三千億才夠", "預算要增加三千億才夠而且要盡快執行", said)
    slipped, result = tolerated_run("指出預算要增加三千億才夠", "甲預算要增加三千億才夠而且要盡快執行", said)    # 開頭多一個字

    assert verbatim == []
    assert slipped == [] and result.tolerated == 0


# --- 性質：差一個字、而且被接受的 quote，結果和一字不差一樣 ---

TURN_INTRO = ("S1", "今天我們來談預算的編列方式。")
SCENARIOS = {
    # 名字: (這一輪說的話, 重點, 一字不差的 quote, 安全模式（直接顯示 quote）下也有意義)
    "plain": ("我認為今年的預算應該先編三個月等成效出來再追加", "主張預算先編三個月，成效出來再追加",
              "我認為今年的預算應該先編三個月等成效出來再追加", True),
    "lead_in": ("我引用市場上的說法：預算要增加三千億才夠而且要盡快執行，但這不是我的立場。", "指出預算要增加三千億才夠",
                "預算要增加三千億才夠而且要盡快執行", False),
    "reported_inside": ("網友說這家公司去年虧損了五百億，但實際的數字不是這樣。", "指出這家公司去年虧損了五百億",
                        "網友說這家公司去年虧損了五百億", False),
    "echo": ("您剛才提到今年的預算要增加三千億才夠用而且要盡快執行，是這樣嗎？", "指出今年的預算要增加三千億才夠用",
             "您剛才提到今年的預算要增加三千億才夠用而且要盡快執行", False),
    "polarity": ("預算我們不會增加因為財政壓力真的很大", "指出預算會增加", "預算我們不會增加因為財政壓力真的很大", True),
    "double_negation": ("我們不得不增加今年的預算因為財政壓力很大", "指出不得增加今年的預算",
                        "我們不得不增加今年的預算因為財政壓力很大", True),
    "question": ("你覺得今年的預算有沒有增加我們還在討論因為財政很緊張", "今年的預算有增加",
                 "你覺得今年的預算有沒有增加我們還在討論因為財政很緊張", True),
    "number": ("今年的預算增加了三十億元因為財政壓力很大", "指出預算增加二十億元", "今年的預算增加了三十億元因為財政壓力很大", True),
    "unsupported": ("我認為今年的預算應該先編三個月等成效出來再追加", "說明與主題完全無關的火星基地計畫",
                    "我認為今年的預算應該先編三個月等成效出來再追加", True),
}
WRONG = "甲"


def variants(quote: str) -> dict[str, list[str]]:
    return {
        "a character replaced": [quote[:i] + WRONG + quote[i + 1:] for i in range(len(quote))],
        "a character added": [quote[:i] + WRONG + quote[i:] for i in range(1, len(quote))],
        "a character left out": [quote[:i] + quote[i + 1:] for i in range(1, len(quote) - 1)],
    }


def outcome(said, point_text, quote, quote_only):
    turns, good = with_anchors(TURN_INTRO, ("S2", said))
    extra, result = run(RawPoint(1, point_text, (), quote), turns=turns, speakers=TWO, good=good, quote_only=quote_only)
    return [(p.speaker, p.turn, p.text) for p in extra], dict(result.dropped_by_code), result.tolerated


CASES = [(name, quote_only) for name in sorted(SCENARIOS) for quote_only in (False, True)
         if SCENARIOS[name][3] or not quote_only]               # 安全模式下，轉述與複述連一字不差的 quote 也不能直接顯示：沒有可比的


@pytest.mark.parametrize("name, quote_only", CASES, ids=[f"{n}-{'quote-only' if q else 'normal'}" for n, q in CASES])
def test_a_quote_with_one_slip_gets_the_same_verdict_as_the_exact_quote_whenever_it_is_accepted(name, quote_only):
    said, point_text, quote, _ = SCENARIOS[name]
    expected, expected_codes, _ = outcome(said, point_text, quote, quote_only)

    accepted = 0
    for kind, slips in variants(quote).items():
        for slip in slips:
            shown, codes, tolerated = outcome(said, point_text, slip, quote_only)
            if tolerated == 0 and c.BAD_QUOTE in codes and c.BAD_QUOTE not in expected_codes:
                continue                                                # 不接受這種差法（數字、否定、起點終點不確定…）：和以前一樣丟掉
            accepted += 1
            assert (shown, codes) == (expected, expected_codes), (kind, slip)

    assert accepted >= len(quote) // 3, "接受的差法太少：這個測試沒有比對到什麼"
