# -*- coding: utf-8 -*-
"""quote 沒有一字不差地落在任何一輪時的兩種有保護的找法：D（合併同一位說話者的輪）與 C（只取最長一段）。

真實影片的診斷：模型的 quote 常有一百多個字，逐字稿又把同一個人切成很多輪，所以「quote 跨了好幾輪」「quote 只有一段
對得上」是大宗。這兩種找法放寬了「quote 要完整落在一輪」，所以每一道保護都要有一個測試：拿掉那一道保護就會失敗。
"""
from __future__ import annotations

import pytest

from slidebox.domain.entities import RawPoint, Turn
from slidebox.usecases.anchoring import JOIN_GAP_CHARS, join_for_quote
from slidebox.usecases.diarized_pipeline import AttributionCounts, summary_line
from slidebox.usecases.quotes import QUOTE_NOT_FOUND, locate_quote

from .conversation_support import problems_of, run, with_anchors

CITABLE = frozenset({"S1", "S2"})                       # 另有不能引用的 X（雜音）


def turn(index: int, speaker: str, text: str) -> Turn:
    return Turn(index, float(index) * 10, float(index) * 10 + 9.0, speaker, text)


FIRST = "我們今年編了三千萬元的預算"
SECOND = "來補助偏鄉的托育服務"
THIRD = "而且明年還會再增加"
OTHER = "完全不相干的另一個話題"


# --- D：合併同一位說話者的輪 ---

def test_a_quote_across_two_turns_of_one_speaker_is_found_in_the_merged_turn():
    turns = [turn(0, "S1", OTHER), turn(1, "S2", FIRST), turn(2, "S2", SECOND)]

    assert locate_quote(FIRST + SECOND, turns[1], turns, CITABLE).kind == QUOTE_NOT_FOUND
    joined = join_for_quote(FIRST + SECOND, turns[1], turns, CITABLE)

    assert joined is not None
    assert (joined.first, joined.last) == (1, 2)
    assert (joined.turn.index, joined.turn.start, joined.turn.end) == (1, 10.0, 29.0)    # 第一輪的編號與開始、最後一輪的結束
    assert joined.turn.speaker == "S2"
    assert [t.index for t in joined.window] == [0, 1]                                  # 被合進去的輪從這一窗拿掉


def test_only_the_shortest_span_that_holds_the_quote_is_merged():
    turns = [turn(0, "S2", FIRST), turn(1, "S2", SECOND), turn(2, "S2", THIRD)]

    joined = join_for_quote(FIRST + SECOND, turns[0], turns, CITABLE)

    assert joined is not None and (joined.first, joined.last) == (0, 1)
    assert [t.index for t in joined.window] == [0, 2]                                  # 第三輪沒用到，原封不動


def test_a_short_noise_between_the_turns_is_skipped():
    turns = [turn(0, "S2", FIRST), turn(1, "X", "嗯" * JOIN_GAP_CHARS), turn(2, "S2", SECOND)]

    joined = join_for_quote(FIRST + SECOND, turns[0], turns, CITABLE)

    assert joined is not None and (joined.first, joined.last) == (0, 2)
    assert [t.index for t in joined.window] == [0]                                     # 中間的雜音一起被合進去


def test_a_noise_longer_than_the_limit_keeps_the_turns_apart():
    turns = [turn(0, "S2", FIRST), turn(1, "X", "嗯" * (JOIN_GAP_CHARS + 1)), turn(2, "S2", SECOND)]

    assert join_for_quote(FIRST + SECOND, turns[0], turns, CITABLE) is None


def test_another_citable_speaker_in_between_keeps_the_turns_apart():
    turns = [turn(0, "S2", FIRST), turn(1, "S1", "對"), turn(2, "S2", SECOND)]

    assert join_for_quote(FIRST + SECOND, turns[0], turns, CITABLE) is None


def test_two_noises_that_together_pass_the_limit_keep_the_turns_apart():
    half = "嗯" * (JOIN_GAP_CHARS // 2 + 1)
    turns = [turn(0, "S2", FIRST), turn(1, "X", half), turn(2, "X", half), turn(3, "S2", SECOND)]

    assert join_for_quote(FIRST + SECOND, turns[0], turns, CITABLE) is None


def test_two_places_that_could_hold_the_quote_are_not_guessed_between():
    turns = [turn(0, "S2", FIRST), turn(1, "S2", SECOND), turn(2, "S1", OTHER),
             turn(3, "S2", FIRST), turn(4, "S2", SECOND)]

    assert join_for_quote(FIRST + SECOND, turns[0], turns, CITABLE) is None


def test_the_cited_turn_must_belong_to_the_speaker_who_said_it():
    turns = [turn(0, "S1", OTHER), turn(1, "S2", FIRST), turn(2, "S2", SECOND)]

    assert join_for_quote(FIRST + SECOND, turns[0], turns, CITABLE) is None


def test_a_quote_that_is_still_not_all_there_after_merging_is_not_found():
    turns = [turn(0, "S2", FIRST), turn(1, "S2", SECOND)]

    assert join_for_quote(FIRST + SECOND + "還有一段根本沒說過的話", turns[0], turns, CITABLE) is None


def test_a_quote_too_short_to_tell_apart_is_not_merged():
    turns = [turn(0, "S2", "好的"), turn(1, "S2", "知道了")]

    assert join_for_quote("好的知道了", turns[0], turns, CITABLE) is None


# --- 端到端：找到之後，往下的檢查一個都沒少 ---

SAID = "我們今年編了三千萬元的預算來補助偏鄉的托育服務"
PADDING = "模型多寫了一段逐字稿裡沒有的話"


def one_point(quote, lines, cited, text="說明今年預算補助托育"):
    turns, good = with_anchors(*lines)
    extra, result = run(RawPoint(cited, text, quote=quote), turns=turns, good=good)
    return extra, result


def test_a_quote_across_two_turns_keeps_the_point_and_counts_it_as_anchored():
    extra, result = one_point(FIRST + SECOND, [("S1", OTHER), ("S2", FIRST), ("S2", SECOND)], 1)

    assert len(extra) == 1 and result.anchored == 1


def test_the_merged_turn_is_still_checked_for_the_speaker_being_cited():
    extra, result = one_point(FIRST + SECOND, [("S1", OTHER), ("S2", FIRST), ("S2", SECOND)], 0)

    assert not extra and result.anchored == 0


def test_the_content_checks_run_against_the_merged_text():
    first, second = "我們今年編了三千萬元的預算", "來補助偏鄉的托育服務"
    extra, result = one_point(first + second, [("S1", OTHER), ("S2", first), ("S2", second)], 1,
                              text="說明今年編了五千萬元的預算補助托育")             # 數字和逐字稿對不上

    assert not extra and "數字" in problems_of(result)


def test_a_question_in_the_merged_turn_is_still_a_question():
    q1, q2 = "請問今年一共編了多少預算來補助偏鄉", "的托育服務呢"
    extra, result = one_point(q1 + q2, [("S1", OTHER), ("S2", q1), ("S2", q2)], 1, text="說明今年編了預算補助托育")

    assert not extra and "問" in problems_of(result)


# --- 補上變異測試找到的邊界 ---

def test_noises_between_turns_add_up_across_the_whole_gap():
    turns = [turn(0, "S2", FIRST), turn(1, "X", "嗯嗯"), turn(2, "X", "啊啊"), turn(3, "X", "喔喔"), turn(4, "S2", SECOND)]

    assert join_for_quote(FIRST + SECOND, turns[0], turns, CITABLE) is None


def test_a_gap_is_counted_from_the_last_turn_of_the_speaker_not_from_the_start_of_the_chain():
    middle = "而且明年還會再增加預算"
    turns = [turn(0, "S2", FIRST), turn(1, "X", "嗯嗯嗯"), turn(2, "S2", middle), turn(3, "X", "嗯嗯嗯"), turn(4, "S2", SECOND)]

    joined = join_for_quote(FIRST + middle + SECOND, turns[0], turns, CITABLE)

    assert joined is not None and (joined.first, joined.last) == (0, 4)


def test_a_turn_before_the_span_that_the_quote_does_not_need_is_left_out():
    turns = [turn(0, "S2", THIRD), turn(1, "S2", FIRST), turn(2, "S2", SECOND)]

    joined = join_for_quote(FIRST + SECOND, turns[1], turns, CITABLE)

    assert joined is not None and (joined.first, joined.last) == (1, 2)
    assert [t.index for t in joined.window] == [0, 1]


def test_a_quote_that_one_turn_already_holds_is_never_merged_with_its_neighbour():
    turns = [turn(0, "S2", FIRST + SECOND), turn(1, "S2", THIRD)]

    assert join_for_quote(FIRST + SECOND, turns[0], turns, CITABLE) is None


def test_citing_the_second_of_the_merged_turns_is_fine():
    extra, result = one_point(FIRST + SECOND, [("S1", OTHER), ("S2", FIRST), ("S2", SECOND)], 2)

    assert len(extra) == 1 and result.anchored == 1


def test_a_quote_only_point_found_by_merging_counts_as_anchored():
    turns, good = with_anchors(("S1", OTHER), ("S2", FIRST), ("S2", SECOND))
    _, result = run(RawPoint(1, "說明今年預算補助托育", quote=FIRST + SECOND), turns=turns, good=good, quote_only=True)

    assert result.anchored == 1


def test_the_summary_line_says_how_many_points_were_found_by_anchoring_only_when_there_are_some():
    base = dict(proposed=10, kept=9, rescued=0, contradicted=1, unverifiable=0, slide_rule=0, anchor_conflicts=0)

    assert "合併那幾輪" not in summary_line(AttributionCounts(**base))
    assert "其中 3 個的 quote 太長或跨了同一位說話者的幾輪" in summary_line(AttributionCounts(**base, anchored=3))
