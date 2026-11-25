# -*- coding: utf-8 -*-
"""第七次診斷（yt-NMyVwZuo26A）看到的三個小問題：quote 中間夾著一個別的聲音的字、「代表會主席」被當成人名、
「1128」與「11月28日」是同一個日期；以及診斷補列「名字、數字被丟」的觸發字眼。"""
from __future__ import annotations

import pytest

from slidebox.domain.entities import Turn
from slidebox.usecases.anchoring import JOIN_GAP_CHARS, join_for_quote
from slidebox.usecases.conversation import DroppedPoint, NAME_IN_TEXT, NUMBER_MISMATCH
from slidebox.usecases.drop_report import _Drop, _quoted_word_lines
from slidebox.usecases.names import detect_names
from slidebox.usecases.numerals import number_problem

CITABLE = frozenset({"S1", "S2"})


def turn(index, speaker, text):
    return Turn(index, float(index) * 10, float(index) * 10 + 9.0, speaker, text)


# --- D：quote 本身就包含被切到別的聲音的那個字 ---

BEFORE, AFTER = "國家機器對於蘇家的包庇", "人嘆為觀止"


def test_a_quote_that_includes_the_one_character_another_voice_took_is_found():
    turns = [turn(0, "S1", "前面的話"), turn(1, "S1", BEFORE), turn(2, "X", "令"), turn(3, "S1", AFTER)]

    joined = join_for_quote(BEFORE + "令" + AFTER, turns[1], turns, CITABLE)

    assert joined is not None and (joined.first, joined.last) == (1, 3)
    assert joined.turn.text == f"{BEFORE} 令 {AFTER}"                    # 夾著的字照順序放回去
    assert [t.index for t in joined.window] == [0, 1]


def test_a_quote_that_leaves_that_character_out_is_still_found_as_before():
    turns = [turn(0, "S1", BEFORE), turn(1, "X", "令"), turn(2, "S1", AFTER)]

    joined = join_for_quote(BEFORE + AFTER, turns[0], turns, CITABLE)

    assert joined is not None and joined.turn.text == f"{BEFORE} {AFTER}"


def test_the_same_span_found_both_ways_is_one_place_not_two():
    turns = [turn(0, "S1", BEFORE), turn(1, "X", "令"), turn(2, "S1", AFTER)]

    assert join_for_quote(BEFORE, turns[0], turns, CITABLE) is None          # 一輪就夠，不合併


def test_a_noise_longer_than_the_limit_is_not_put_back_either():
    turns = [turn(0, "S1", BEFORE), turn(1, "X", "令" * (JOIN_GAP_CHARS + 1)), turn(2, "S1", AFTER)]

    assert join_for_quote(BEFORE + "令" * (JOIN_GAP_CHARS + 1) + AFTER, turns[0], turns, CITABLE) is None


def test_a_character_of_another_citable_speaker_is_never_put_back():
    turns = [turn(0, "S1", BEFORE), turn(1, "S2", "令"), turn(2, "S1", AFTER)]

    assert join_for_quote(BEFORE + "令" + AFTER, turns[0], turns, CITABLE) is None


# --- 名字：機關的名字不是「會＋職稱」 ---

@pytest.mark.parametrize("text", ["由代表會主席林志勇出面", "市議會議員質詢", "委員會主席說明"])
def test_an_organisation_name_before_a_title_is_not_a_name(text):
    assert detect_names(text) == frozenset()


@pytest.mark.parametrize("text, name", [("謝謝李主席的說明", "李主席"), ("請教邱教授", "邱教授")])
def test_a_real_title_after_a_family_name_is_still_a_name(text, name):
    assert name in detect_names(text)


# --- 數字：日期 ---

SAID = "那個目的就是1128這個禮拜六出門投票"


@pytest.mark.parametrize("text", ["唯一目的是在11月28日出門投票", "唯一目的是在11月28號出門投票", "唯一目的是在11月28出門投票"])
def test_a_date_written_out_is_supported_by_the_same_date_said_as_digits(text):
    assert number_problem(text, SAID) is None


@pytest.mark.parametrize("text", ["唯一目的是在11月29日出門投票", "唯一目的是在12月28日出門投票", "唯一目的是在13月28日出門投票"])
def test_a_different_date_is_still_a_problem(text):
    assert number_problem(text, SAID) is not None


def test_a_zero_padded_date_is_supported():
    assert number_problem("活動在5月5日舉行", "活動是0505舉行") is None


def test_a_date_does_not_excuse_the_other_numbers_in_the_sentence():
    assert number_problem("11月28日有3000人參加", SAID) is not None


def test_a_date_only_counts_when_the_digits_stand_alone_in_the_transcript():
    assert number_problem("活動在11月28日舉行", "活動編號是211289號") is not None


# --- 診斷：名字與數字被丟的觸發字眼 ---

def drop(code, message):
    return _Drop(1, None, DroppedPoint(code, message, "quote", "text", 1))      # type: ignore[arg-type]


def test_the_diagnostic_lists_the_quoted_words_of_dropped_names_and_numbers():
    drops = [drop(NAME_IN_TEXT, "文字裡有逐字稿用來稱呼人的名字或稱謂「小編」；不要寫進去"),
             drop(NAME_IN_TEXT, "文字裡有逐字稿用來稱呼人的名字或稱謂「會主席」；不要寫進去"),
             drop(NAME_IN_TEXT, "文字裡有逐字稿用來稱呼人的名字或稱謂「小編」；不要寫進去"),
             drop(NUMBER_MISMATCH, "數字「11」不在被引用的那段話裡")]

    assert _quoted_word_lines(drops, NAME_IN_TEXT, "名字或稱謂被丟", "字眼") == [
        "  名字或稱謂被丟（name_in_text 共 3 個）：字眼 小編 2、會主席 1"]
    assert _quoted_word_lines(drops, NUMBER_MISMATCH, "數字被丟", "數字") == [
        "  數字被丟（number_mismatch 共 1 個）：數字 11 1"]


def test_the_diagnostic_says_so_when_a_reason_has_no_quoted_word():
    assert _quoted_word_lines([drop(NAME_IN_TEXT, "沒有框起來的字")], NAME_IN_TEXT, "名字或稱謂被丟", "字眼") == [
        "  名字或稱謂被丟（name_in_text 共 1 個）：1 個原因裡沒有框起來的字眼"]


def test_nothing_is_listed_when_nothing_of_that_kind_was_dropped():
    assert _quoted_word_lines([], NAME_IN_TEXT, "名字或稱謂被丟", "字眼") == []
