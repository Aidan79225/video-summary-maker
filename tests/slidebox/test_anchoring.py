# -*- coding: utf-8 -*-
"""quote 沒有一字不差地落在任何一輪時的兩種有保護的找法：D（合併同一位說話者的輪）與 C（只取最長一段）。

真實影片的診斷：模型的 quote 常有一百多個字，逐字稿又把同一個人切成很多輪，所以「quote 跨了好幾輪」「quote 只有一段
對得上」是大宗。這兩種找法放寬了「quote 要完整落在一輪」，所以每一道保護都要有一個測試：拿掉那一道保護就會失敗。
"""
from __future__ import annotations

import pytest

from slidebox.domain.entities import RawPoint, Turn
from slidebox.usecases.anchoring import (
    EDGE_REST_CHARS, JOIN_GAP_CHARS, MIN_REST_CHARS, OTHER_SPEAKER_RUN_CHARS, RUN_MARGIN, RUN_MIN_CHARS, join_for_quote,
    longest_run,
)
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


# --- C：只取最長一段 ---

LONG = "我們今年編了三千萬元的預算來補助偏鄉的托育服務"           # 逐字稿裡有的一段（23 個字）
TAIL = "模型多寫了一段逐字稿裡沒有的話"                            # 15 個字，不在任何一輪


def test_the_longest_run_that_one_turn_holds_is_used_and_the_transcripts_own_text_comes_back():
    turns = [turn(0, "S1", OTHER), turn(1, "S2", f"好的，{LONG}，大家都知道。")]

    run_ = longest_run(LONG + TAIL, turns[1], turns, CITABLE)

    assert run_ is not None and run_.turn is turns[1] and run_.text == LONG


def test_a_run_shorter_than_the_minimum_is_not_used():
    short = LONG[:RUN_MIN_CHARS - 1]
    turns = [turn(0, "S1", OTHER), turn(1, "S2", short)]

    assert longest_run(short + TAIL + TAIL, turns[1], turns, CITABLE) is None


def test_a_run_that_is_not_clearly_longer_than_the_next_best_turn_is_not_used():
    near = LONG[:len(LONG) - RUN_MARGIN + 1]                       # 比最長的一段只短 RUN_MARGIN - 1 個字
    turns = [turn(0, "S2", LONG), turn(1, "S2", near + "，其他的話")]

    assert longest_run(LONG + TAIL, turns[0], turns, CITABLE) is None


def test_a_run_clearly_longer_than_the_next_best_turn_is_used():
    shorter = LONG[:len(LONG) - RUN_MARGIN]
    turns = [turn(0, "S2", LONG), turn(1, "S2", shorter + "，其他的話")]

    run_ = longest_run(LONG + TAIL, turns[0], turns, CITABLE)

    assert run_ is not None and run_.turn is turns[0]


def test_a_run_in_a_turn_nobody_may_be_cited_for_is_not_used():
    turns = [turn(0, "S1", OTHER), turn(1, "X", LONG)]

    assert longest_run(LONG + TAIL, turns[0], turns, CITABLE) is None


def test_the_cited_turn_must_be_the_same_speaker_as_the_run():
    turns = [turn(0, "S1", OTHER), turn(1, "S2", LONG)]

    assert longest_run(LONG + TAIL, turns[0], turns, CITABLE) is None


def test_a_rest_that_matches_another_speakers_turn_means_two_peoples_words_were_stitched():
    stitched = "另一位說的這段話在這裡"[:OTHER_SPEAKER_RUN_CHARS + 1]
    turns = [turn(0, "S1", "對對，" + stitched), turn(1, "S2", LONG)]

    assert longest_run(LONG + stitched + TAIL, turns[1], turns, CITABLE) is None


def test_a_rest_that_also_appears_in_a_non_citable_turn_does_not_block_the_run():
    heard = "旁邊的人也說了這句話好"
    turns = [turn(0, "X", heard), turn(1, "S2", LONG)]

    assert longest_run(LONG + heard + TAIL, turns[1], turns, CITABLE) is not None


def test_a_rest_shorter_than_the_minimum_is_the_near_verbatim_rules_business_not_this_ones():
    turns = [turn(0, "S1", OTHER), turn(1, "S2", f"前面，{LONG}，後面")]
    rest = "。" + "多" * (MIN_REST_CHARS - 1)

    assert longest_run(LONG + rest, turns[1], turns, CITABLE) is None
    assert longest_run(LONG + "多" * MIN_REST_CHARS, turns[1], turns, CITABLE) is not None


def test_a_quote_no_longer_than_the_minimum_run_is_not_cut_down():
    turns = [turn(0, "S2", "我們今年編了三千萬元")]

    assert longest_run("我們今年編了三千萬元", turns[0], turns, CITABLE) is None


# --- 端到端：找到之後，往下的檢查一個都沒少 ---

SAID = "我們今年編了三千萬元的預算來補助偏鄉的托育服務"
PADDING = "模型多寫了一段逐字稿裡沒有的話"


def one_point(quote, lines, cited, text="說明今年預算補助托育"):
    turns, good = with_anchors(*lines)
    extra, result = run(RawPoint(cited, text, quote=quote), turns=turns, good=good)
    return extra, result


def test_a_quote_with_one_matching_run_keeps_the_point_and_counts_it_as_anchored():
    extra, result = one_point(SAID + PADDING, [("S1", OTHER), ("S2", f"好的，{SAID}，大家都知道。")], 1)

    assert len(extra) == 1 and result.anchored == 1


def test_the_point_is_kept_with_the_transcripts_run_in_quote_only_mode():
    turns, good = with_anchors(("S1", OTHER), ("S2", f"好的，{SAID}，大家都知道。"))
    extra, result = run(RawPoint(1, "說明今年預算補助托育", quote=SAID + PADDING), turns=turns, good=good, quote_only=True)

    assert [p.text for p in extra] == [f"「{SAID}」"] and result.anchored == 1


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


def test_a_stitched_quote_of_two_speakers_is_still_dropped():
    extra, _ = one_point(FIRST + "，" + OTHER, [("S2", FIRST), ("S1", OTHER)], 0)

    assert not extra


@pytest.mark.parametrize("constant", [JOIN_GAP_CHARS, RUN_MIN_CHARS, RUN_MARGIN, OTHER_SPEAKER_RUN_CHARS, MIN_REST_CHARS])
def test_the_provisional_values_are_positive_integers(constant):
    assert isinstance(constant, int) and constant > 0


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


def test_a_run_in_the_cited_turn_that_may_not_be_cited_is_not_used():
    turns = [turn(0, "S1", OTHER), turn(1, "X", LONG)]

    assert longest_run(LONG + TAIL, turns[1], turns, CITABLE) is None


@pytest.mark.parametrize("stitched, usable", [
    ("甲乙丙丁戊己庚辛", False),                           # OTHER_SPEAKER_RUN_CHARS 個字：夠長，算拼接
    ("甲乙丙丁戊己庚", True),                              # 少一個字：還算巧合
])
def test_a_run_of_another_speakers_words_in_the_rest_blocks_at_exactly_the_threshold(stitched, usable):
    assert len(stitched) == OTHER_SPEAKER_RUN_CHARS - (0 if not usable else 1)
    turns = [turn(0, "S1", stitched), turn(1, "S2", LONG)]

    found = longest_run(LONG + stitched + TAIL, turns[1], turns, CITABLE)

    assert (found is not None) is usable


def test_another_speakers_words_before_the_run_block_it_too():
    stitched = "甲乙丙丁戊己庚辛壬"
    turns = [turn(0, "S1", stitched), turn(1, "S2", LONG)]

    assert longest_run(stitched + LONG + TAIL, turns[1], turns, CITABLE) is None


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


# --- C 的頭尾：照抄得差不多、只差頭尾幾個字 ---

BODY = "他呈現出來的是他對於這個花蓮市市政的熟悉他呈現出來的是他認真的把這一場政見發表會認真的把它準備完"


def test_a_quote_with_a_few_extra_characters_at_an_edge_is_used_with_a_wider_span():
    turns = [turn(0, "S1", OTHER), turn(1, "S1", f"因為宇文{BODY}而且在後面還有很多話")]

    found = longest_run("戴宇文" + BODY + "標下來", turns[1], turns, CITABLE)

    assert found is not None and found.edge and found.core == "宇文" + BODY
    assert found.text.startswith("因為宇文") and found.text.endswith("而且在後面還")        # 頭尾各多取 EDGE_REST_CHARS 個字
    assert len(found.text) >= len(found.core) + EDGE_REST_CHARS


def test_the_wider_span_is_clamped_to_the_turn():
    turns = [turn(0, "S1", OTHER), turn(1, "S1", BODY)]

    found = longest_run("甲" + BODY, turns[1], turns, CITABLE)

    assert found is not None and found.edge and found.text == BODY


def test_more_than_the_edge_allowance_is_not_an_edge_run():
    turns = [turn(0, "S1", OTHER), turn(1, "S1", BODY)]

    assert longest_run("甲" * (EDGE_REST_CHARS + 1) + BODY, turns[1], turns, CITABLE) is None
    assert longest_run("甲" * EDGE_REST_CHARS + BODY, turns[1], turns, CITABLE) is not None


def test_a_rest_between_the_edge_allowance_and_the_minimum_is_still_refused():
    turns = [turn(0, "S1", OTHER), turn(1, "S1", BODY)]

    for size in range(EDGE_REST_CHARS + 1, MIN_REST_CHARS):
        assert longest_run(BODY + "甲" * size, turns[1], turns, CITABLE) is None


def one_edge_point(quote, said, text):
    turns, good = with_anchors(("S1", OTHER), ("S1", said))
    return run(RawPoint(1, text, quote=quote), turns=turns, good=good)


def test_an_extra_word_at_the_end_of_a_quote_keeps_the_point_and_counts_it_as_anchored():
    extra, result = one_edge_point("戴宇文" + BODY + "標下來", "因為宇文" + BODY + "而且還有別的話", "讚揚對市政熟悉且認真準備政見")

    assert len(extra) == 1 and result.anchored == 1 and result.tolerated == 0


def test_a_wrong_number_in_the_extra_edge_characters_still_drops_the_point():
    extra, result = one_edge_point(BODY + "共三百萬", "因為" + BODY + "而且還有別的話", "讚揚對市政熟悉且認真準備政見")

    assert not extra and "quote" in problems_of(result)


def test_a_negation_in_the_extra_edge_characters_still_drops_the_point():
    extra, _ = one_edge_point(BODY + "不是", "因為" + BODY + "而且還有別的話", "讚揚對市政熟悉且認真準備政見")

    assert not extra


def test_a_reported_frame_just_before_a_quote_with_a_wrong_first_character_is_still_caught():
    said = "網友說" + BODY + "而且還有別的話"
    extra, _ = one_edge_point("甲" + BODY, said, "讚揚對市政熟悉且認真準備政見")

    assert not extra
