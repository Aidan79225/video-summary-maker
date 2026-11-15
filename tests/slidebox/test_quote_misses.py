"""引文找不到的時候，為什麼找不到（診斷用，不參與任何判斷）。

locate_quote 的 kind 說明它在哪一關擋下來（空的、太短、只有很短的片段、不在任何一個可以引用的輪裡、好幾輪都有）；
explain_quote_miss 再把「不在任何一個可以引用的輪裡」分成：在其他聲音的輪裡、橫跨相鄰的輪、順序不同、
幾乎照抄只差幾個字、只有一部分對得上、幾乎不在這一窗的逐字稿裡。

這些分類只給人看：拿它做決定的地方一個也沒有，locate_quote 的結果（turn、problem）不因此改變。
"""
from __future__ import annotations

import pytest

from slidebox.domain.entities import Turn
from slidebox.usecases.quotes import (
    MISS_ABSENT,
    MISS_ACROSS_TURNS,
    MISS_LOCATED,
    MISS_NEAR_VERBATIM,
    MISS_OTHER_VOICE,
    MISS_PARTIAL,
    MISS_REORDERED,
    QUOTE_AMBIGUOUS,
    QUOTE_EMPTY,
    QUOTE_NOT_FOUND,
    QUOTE_SHORT_FRAGMENTS,
    QUOTE_TOO_SHORT,
    QuoteLocation,
    explain_quote_miss,
    locate_quote,
)

CITABLE = frozenset({"S1", "S2"})


def turn(index: int, speaker: str, text: str) -> Turn:
    return Turn(index, float(index), float(index) + 0.9, speaker, text)


def window() -> list[Turn]:
    """S0 是不能引用的聲音（其他聲音／無法判斷），S1、S2 是主要發言者。"""
    return [
        turn(0, "S0", "各位好，今天討論試辦計畫的預算與期程。"),
        turn(1, "S1", "我認為預算應該先編三個月，等成效出來再追加。"),
        turn(2, "S2", "我不同意，三個月太短了，至少要半年才看得出成效。"),
        turn(3, "S0", "請兩位各自說明理由，先編三個月的預算。"),
        turn(4, "S2", "沒錯"),
        turn(5, "S1", "第一，預算要公開；第二，成效要公開；第三，期程要公開。"),
    ]


# --- locate_quote 的失敗原因 ---


def test_a_located_quote_has_no_failure_kind():
    turns = window()

    found = locate_quote("我認為預算應該先編三個月，等成效出來再追加", turns[1], turns, CITABLE)

    assert found.turn is turns[1] and found.kind is None


def test_a_failure_kind_does_not_change_what_locate_quote_decides():
    """kind 只是多一個說明：沒有 kind 的舊寫法（只給 turn 與 problem）照樣能組出一個結果。"""
    assert QuoteLocation(None, "原因") == QuoteLocation(None, "原因", None)
    assert QuoteLocation(None, "原因").kind is None


def test_an_empty_quote_is_reported_as_empty():
    turns = window()

    assert locate_quote("", turns[1], turns, CITABLE).kind == QUOTE_EMPTY
    assert locate_quote("，。 ", turns[1], turns, CITABLE).kind == QUOTE_EMPTY


def test_a_quote_of_one_character_pieces_is_too_short():
    turns = window()

    assert locate_quote("對", turns[4], turns, CITABLE).kind == QUOTE_TOO_SHORT


def test_a_quote_under_eight_characters_is_too_short_even_when_the_turn_holds_it():
    turns = window()

    found = locate_quote("三個月太短", turns[2], turns, CITABLE)

    assert found.turn is None and found.kind == QUOTE_TOO_SHORT


def test_a_quote_made_only_of_two_character_pieces_is_reported_as_short_fragments():
    turns = [turn(0, "S1", "第一，預算要公開；第二，成效要公開；第三，期程要公開。"), turn(1, "S2", "好")]

    found = locate_quote("預算，成效，期程，公開", turns[0], turns, CITABLE)

    assert found.turn is None and found.kind == QUOTE_SHORT_FRAGMENTS


def test_a_quote_no_citable_turn_holds_is_reported_as_not_found():
    turns = window()

    found = locate_quote("我認為預算應該先編六個月再追加", turns[1], turns, CITABLE)

    assert found.turn is None and found.kind == QUOTE_NOT_FOUND


def test_a_quote_two_turns_hold_is_reported_as_ambiguous():
    turns = [turn(0, "S1", "預算要先公開，成效也要公開。"), turn(1, "S2", "我同意預算要先公開，成效也要公開。"),
             turn(2, "S1", "好")]

    found = locate_quote("預算要先公開，成效也要公開", turns[2], turns, CITABLE)

    assert found.turn is None and found.kind == QUOTE_AMBIGUOUS


# --- explain_quote_miss：把「不在任何一個可以引用的輪裡」分得更細 ---


def test_a_quote_copied_from_a_voice_nobody_may_cite_is_other_voice():
    turns = window()

    miss = explain_quote_miss("請兩位各自說明理由，先編三個月的預算", turns[3], turns, CITABLE)

    assert miss.kind == MISS_OTHER_VOICE and miss.near == (turns[3],)


def test_a_quote_that_needs_two_neighbouring_turns_is_across_turns():
    turns = window()

    miss = explain_quote_miss("等成效出來再追加。我不同意，三個月太短了", turns[1], turns, CITABLE)

    assert miss.kind == MISS_ACROSS_TURNS and miss.near == (turns[1], turns[2])


def test_a_quote_that_spans_three_turns_is_across_turns_too():
    turns = [turn(0, "S1", "預算要先編三個月再說"), turn(1, "S2", "我不同意這樣的做法"), turn(2, "S1", "那就改成半年好了"),
             turn(3, "S2", "好")]

    miss = explain_quote_miss("預算要先編三個月再說，我不同意這樣的做法，那就改成半年好了", turns[0], turns, CITABLE)

    assert miss.kind == MISS_ACROSS_TURNS and miss.near == (turns[0], turns[1], turns[2])


def test_pieces_that_are_all_in_one_turn_but_in_another_order_are_reordered():
    turns = window()

    miss = explain_quote_miss("等成效出來再追加，我認為預算應該先編三個月", turns[1], turns, CITABLE)

    assert miss.kind == MISS_REORDERED and miss.near == (turns[1],)


def test_a_quote_that_differs_from_a_turn_by_a_word_is_near_verbatim():
    """「成效」寫成「成果」：照抄得差不多，只差幾個字（同音字、數字寫法、模型順手改了辨識的錯字）。"""
    turns = window()

    miss = explain_quote_miss("我認為預算應該先編三個月，等成果出來再追加", turns[1], turns, CITABLE)

    assert miss.kind == MISS_NEAR_VERBATIM and miss.near == (turns[1],)
    assert 0.8 <= miss.ratio < 1.0
    assert miss.unseen == ("果",)                      # 「果」在這一窗的逐字稿裡從沒出現過


def test_a_quote_that_is_half_copied_and_half_made_up_is_partial():
    turns = window()

    miss = explain_quote_miss("我認為預算應該先編三個月，然後全面檢討所有補助項目", turns[1], turns, CITABLE)

    assert miss.kind == MISS_PARTIAL and miss.near == (turns[1],)
    assert 0.4 <= miss.ratio < 0.8


def test_a_quote_that_is_nowhere_in_the_window_is_absent_and_has_no_nearest_turn():
    turns = window()

    miss = explain_quote_miss("今年的氣候變遷政策完全沒有進展", turns[1], turns, CITABLE)

    assert miss.kind == MISS_ABSENT and miss.near == () and miss.ratio == 0.0


def test_a_quote_written_in_simplified_characters_shows_the_characters_the_transcript_never_used():
    """攔的 bug：模型照抄時順手改成簡體字，程式只說「對不上」，看不出是字體不同。"""
    turns = window()

    miss = explain_quote_miss("我认为预算应该先编三个月，等成效出来再追加", turns[1], turns, CITABLE)

    assert miss.kind == MISS_ABSENT and miss.near == (turns[1],)
    assert {"认", "预", "应", "编", "个", "来"} <= set(miss.unseen)
    assert "認" not in miss.unseen and "預" not in miss.unseen
    assert miss.unseen_share > 0.3


def test_a_quote_made_of_characters_the_window_uses_has_nothing_unseen():
    turns = window()

    miss = explain_quote_miss("等成效出來再追加，我認為預算應該先編三個月", turns[1], turns, CITABLE)

    assert miss.unseen == () and miss.unseen_share == 0.0


@pytest.mark.parametrize("quote, kind", [
    ("", QUOTE_EMPTY),
    ("對", QUOTE_TOO_SHORT),
    ("三個月太短", QUOTE_TOO_SHORT),
])
def test_the_reasons_locate_quote_gives_itself_are_passed_through_with_the_cited_turn(quote, kind):
    turns = window()

    miss = explain_quote_miss(quote, turns[2], turns, CITABLE)

    assert miss.kind == kind and miss.near == (turns[2],)


def test_a_quote_two_turns_hold_is_ambiguous_here_too():
    turns = [turn(0, "S1", "預算要先公開，成效也要公開。"), turn(1, "S2", "我同意預算要先公開，成效也要公開。"),
             turn(2, "S1", "好")]

    miss = explain_quote_miss("預算要先公開，成效也要公開", turns[2], turns, CITABLE)

    assert miss.kind == QUOTE_AMBIGUOUS and miss.near == (turns[2],)


def test_a_quote_that_is_actually_found_is_reported_as_located():
    """安全模式（只顯示原話）會因為別的原因丟掉一個找得到的 quote；診斷要看得出那不是找不到。"""
    turns = window()

    miss = explain_quote_miss("我認為預算應該先編三個月，等成效出來再追加", turns[1], turns, CITABLE)

    assert miss.kind == MISS_LOCATED and miss.near == (turns[1],)


def test_the_other_voice_check_comes_before_the_looser_ones():
    """一段話逐字出現在其他聲音的輪裡，就是 other_voice，不是 near_verbatim 或 partial。"""
    turns = window()

    miss = explain_quote_miss("請兩位各自說明理由，先編三個月的預算", turns[1], turns, CITABLE)

    assert miss.kind == MISS_OTHER_VOICE


def test_explaining_never_changes_what_locate_quote_says():
    turns = window()
    before = locate_quote("我認為預算應該先編六個月再追加", turns[1], turns, CITABLE)

    explain_quote_miss("我認為預算應該先編六個月再追加", turns[1], turns, CITABLE)

    assert locate_quote("我認為預算應該先編六個月再追加", turns[1], turns, CITABLE) == before


def test_a_window_without_turns_has_nothing_near():
    miss = explain_quote_miss("我認為預算應該先編三個月，等成效出來再追加", turn(0, "S1", "好"), [], CITABLE)

    assert miss.kind == MISS_ABSENT and miss.near == ()


def test_when_two_turns_are_equally_close_the_one_the_model_cited_is_the_nearest():
    """一模一樣的兩輪（甲說完乙又說一遍）對 quote 的相似度一樣：模型引用的是哪一輪，就說是哪一輪。"""
    turns = [turn(0, "S1", "好的，我們開始"), turn(1, "S1", "預算要先公開，成效也要公開"),
             turn(2, "S2", "預算要先公開，成效也要公開"), turn(3, "S1", "好")]
    quote = "預算要先公開然後全面檢討"

    cited_first = explain_quote_miss(quote, turns[1], turns, CITABLE)
    cited_second = explain_quote_miss(quote, turns[2], turns, CITABLE)

    assert cited_first.kind == cited_second.kind == MISS_PARTIAL
    assert cited_first.ratio == cited_second.ratio
    assert cited_first.near == (turns[1],) and cited_second.near == (turns[2],)


def test_a_character_the_transcript_never_used_is_listed_once_however_often_the_quote_repeats_it():
    turns = [turn(0, "S1", "好的，我們開始"), turn(1, "S1", "預算要先公開，成效也要公開"), turn(2, "S2", "好")]

    miss = explain_quote_miss("预算要先公开，成效也要公开", turns[1], turns, CITABLE)

    assert miss.unseen == ("预", "开")
    assert miss.unseen_share == 3 / 12                  # 三個字（开 出現兩次）佔 quote 的十二個字
