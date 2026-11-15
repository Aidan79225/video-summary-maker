"""引文找不到的時候，為什麼找不到（診斷用，不參與任何判斷）。

locate_quote 的 kind 說明它在哪一關擋下來（空的、太短、只有很短的片段、不在任何一個可以引用的輪裡、好幾輪都有）；
explain_quote_miss 再把「不在任何一個可以引用的輪裡」分成：在其他聲音的輪裡、橫跨相鄰的輪、順序不同、
幾乎照抄只差幾個字、只有一部分對得上、幾乎不在這一窗的逐字稿裡。

這些分類只給人看：拿它做決定的地方一個也沒有，locate_quote 的結果（turn、problem）不因此改變。
"""
from __future__ import annotations

import pytest

from slidebox.domain.entities import Turn
from slidebox.usecases.quote_misses import (
    MISS_ABSENT,
    MISS_ACROSS_TURNS,
    MISS_LOCATED,
    MISS_NEAR_VERBATIM,
    MISS_OTHER_VOICE,
    MISS_PARTIAL,
    MISS_REORDERED,
    explain_quote_miss,
)
from slidebox.usecases.quotes import (
    QUOTE_AMBIGUOUS,
    QUOTE_EMPTY,
    QUOTE_NOT_FOUND,
    QUOTE_SHORT_FRAGMENTS,
    QUOTE_TOO_SHORT,
    locate_quote,
)
from slidebox.usecases.speakers import squash

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


def test_the_thresholds_are_inclusive_eighty_percent_is_near_verbatim_and_forty_percent_is_partial():
    """門檻是「以上」：八成的字不用改算照抄得差不多、四成算有一部分對得上（README 就是這樣寫的）。
    quote 裡這一窗從沒出現過的字怎麼比都得改，所以要改的字數是確定的：兩個、六個。"""
    turns = [turn(0, "S1", "預算要先公開成效也要公開"), turn(1, "S2", "好")]

    eighty = explain_quote_miss("預算要先公開成效雨雪", turns[0], turns, CITABLE)          # 十個字裡有兩個從沒出現過
    forty = explain_quote_miss("預算要先雨雪雷電霜露", turns[0], turns, CITABLE)            # 十個字裡有六個從沒出現過

    assert (eighty.kind, eighty.ratio) == (MISS_NEAR_VERBATIM, 0.8)
    assert (forty.kind, forty.ratio) == (MISS_PARTIAL, 0.4)


def test_a_quote_written_in_simplified_characters_shows_the_characters_the_transcript_never_used():
    """攔的 bug：模型照抄時順手改成簡體字，程式只說「對不上」，看不出是字體不同。"""
    turns = window()

    miss = explain_quote_miss("我认为预算应该先编三个月，等成效出来再追加", turns[1], turns, CITABLE)

    assert miss.kind == MISS_PARTIAL and miss.near == (turns[1],)       # 二十個字裡有八個字不同：要改八個字
    assert miss.ratio == pytest.approx(0.6)
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


def test_a_quote_that_is_actually_found_is_reported_as_located():
    """安全模式（只顯示原話）會因為別的原因丟掉一個找得到的 quote；診斷要看得出那不是找不到。"""
    turns = window()

    miss = explain_quote_miss("我認為預算應該先編三個月，等成效出來再追加", turns[1], turns, CITABLE)

    assert miss.kind == MISS_LOCATED and miss.near == (turns[1],)


def test_a_found_quote_is_located_in_the_turn_that_has_it_not_the_one_the_model_cited():
    """模型把問題的編號填在答案的 quote 上：找得到的那一輪才是檢查實際對著做的那一輪。"""
    turns = window()

    miss = explain_quote_miss("我認為預算應該先編三個月，等成效出來再追加", turns[2], turns, CITABLE)

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


# --- 相似度是「要改幾個字」，不是「相鄰兩字組有幾成出現在整輪裡」 ---

LONG_TURN = ("我覺得這個預算的編列方式有一個很大的問題，就是今年的預算雖然增加了兩成，但是增加的部分大多是人事費用，"
             "真正用在托育補助和公共托育名額的預算其實沒有增加多少，所以家長排隊排了兩年還是排不到。")


def long_window() -> list[Turn]:
    return [turn(0, "S1", "請問這個預算的編列方式有沒有問題"), turn(1, "S2", LONG_TURN), turn(2, "S1", "好的，謝謝")]


def test_two_wrong_characters_in_a_long_turn_is_near_verbatim():
    turns = long_window()

    miss = explain_quote_miss("今年的預算雖然增加了二成，但是增加的部分大多是人是費用", turns[0], turns, CITABLE)

    assert miss.kind == MISS_NEAR_VERBATIM and miss.near == (turns[1],)
    assert miss.ratio == pytest.approx(1 - 2 / 26)                # 26 個字裡改兩個字（兩→二、事→是）


def test_a_reworded_quote_is_not_near_verbatim_just_because_the_turn_is_long_and_on_topic():
    """攔的 bug：用「quote 的相鄰兩字組有幾成出現在整輪裡」當相似度，輪越長、越切題，改寫過的 quote
    分數就越高，和只錯兩個字的一樣；「照抄得差不多」的統計被改寫灌水，會誤導人去放寬驗證。"""
    turns = long_window()

    miss = explain_quote_miss("預算增加的部分大多用在人事費用，托育補助和公共托育名額沒有增加", turns[0], turns, CITABLE)

    assert miss.kind == MISS_PARTIAL and miss.near == (turns[1],)
    assert miss.ratio < 0.8


def test_a_quote_glued_from_two_distant_clauses_is_partial_not_near_verbatim():
    """兩個子句中間隔了一大段，quote 把它們直接接起來：每個相鄰兩字組都找得到，但沒有哪一段是連續照抄的。"""
    turns = [turn(0, "S1", "現金補助有幫助，但是不能只靠現金，因為家長最在意的是托育名額夠不夠，現在不少地方托育資源不夠，排隊要排很久。"),
             turn(1, "S2", "好")]

    miss = explain_quote_miss("現金補助有幫助托育資源不夠", turns[0], turns, CITABLE)

    assert miss.kind == MISS_PARTIAL and miss.near == (turns[0],)


def test_a_long_turn_that_reuses_every_word_of_the_quote_does_not_hide_the_turn_it_was_copied_from():
    """攔的 bug：用整輪的相鄰兩字組比，一個把 quote 每個詞都用過一遍、卻沒有任何一段連得起來的長輪，
    會贏過真正被照抄（只錯一個字）的那一輪，診斷就指著錯的那一輪說「照抄得差不多」。"""
    quote = "今年的預算大約增加了二成左右"
    source = turn(1, "S2", "今年的預算大約增加了兩成左右")
    scattered = turn(2, "S1", "，".join(quote[i:i + 2] for i in range(len(quote) - 1)))        # 每個詞都有，沒有一段連得起來
    turns = [turn(0, "S1", "好的，我們先從預算談起"), source, scattered, turn(3, "S1", "好")]

    miss = explain_quote_miss(quote, scattered, turns, CITABLE)

    assert miss.kind == MISS_NEAR_VERBATIM and miss.near == (source,)


def test_the_nearest_turn_is_chosen_by_how_few_characters_change_not_by_how_many_pairs_repeat():
    """攔的 bug：只信相鄰兩字組的命中數挑最近的一輪，一個一直重複 quote 第一個詞的輪（命中數最多）會蓋過
    真正照抄了大半的那一輪。命中數只用來縮小範圍，最後比的是要改幾個字。"""
    quote = "甲乙丙丁戊己庚辛壬癸"
    repeats = turn(0, "S1", "甲乙" * 8)                        # 「甲乙」到處都是（命中最多），但沒有一段連得起來
    copied = turn(1, "S2", "甲乙丙丁戊己庚雨雪雷")                # 前七個字照抄、後三個字不一樣
    turns = [repeats, copied, turn(2, "S1", "好")]

    miss = explain_quote_miss(quote, repeats, turns, CITABLE)

    assert miss.kind == MISS_PARTIAL and miss.near == (copied,)
    assert miss.ratio == pytest.approx(0.7)


def test_a_quote_the_model_cut_short_halfway_is_still_compared_with_the_part_that_matches():
    turns = long_window()

    miss = explain_quote_miss("今年的預算雖然增加了兩成，但是增加的部分大多是人事費用，然後完全沒有提到托育", turns[1], turns, CITABLE)

    assert miss.kind == MISS_PARTIAL and miss.near == (turns[1],)
    assert 0.4 <= miss.ratio < 0.8


def test_a_huge_quote_against_huge_turns_still_finishes_quickly():
    """比對的成本不能跟著 quote 與逐字稿的長度相乘：模型偶爾會把整段文字當成 quote。"""
    filler = "這是一句用來湊字數的話而且每一輪都不一樣第%d句"
    turns = [turn(i, "S1" if i % 2 else "S2", (filler % i) * 400) for i in range(4)]

    miss = explain_quote_miss("預算" + "這是一句用來湊字數的話而且每一輪都不一樣" * 300, turns[0], turns, CITABLE)

    assert miss.kind in {MISS_PARTIAL, MISS_NEAR_VERBATIM, MISS_ABSENT}


def test_only_the_beginning_of_a_very_long_quote_is_compared():
    """比對的成本和 quote 長度的平方成正比，所以最多只比前面 MAX_COMPARE_CHARS（120）個字：前面照抄得很準、
    後面整段編造的長 quote，看到的是「前面抄得很準」；這份診斷不是在證明整段 quote 都對。"""
    clauses = ["今年的預算雖然增加了兩成", "但是增加的部分大多是人事費用", "真正用在托育補助的預算其實沒有增加多少",
               "所以家長排隊排了兩年還是排不到", "公共托育的名額每年都只增加幾十個", "很多縣市根本沒有辦法照著中央的規定去做",
               "我們希望明年的預算可以先把名額補足", "再來談其他的補助項目", "這樣才不會讓家長一直等下去"]
    text = "，".join(clauses) + "。"
    beginning = squash(text)[:120]
    assert len(squash(text)) > 130
    turns = [turn(0, "S1", text), turn(1, "S2", "好")]
    quote = beginning[:60] + "錯" + beginning[61:] + "雨" * 200            # 前 120 個字只錯一個字，後面 200 個字是編的

    miss = explain_quote_miss(quote, turns[0], turns, CITABLE)

    assert miss.kind == MISS_NEAR_VERBATIM and miss.near == (turns[0],)
    assert miss.ratio == pytest.approx(1 - 1 / 120)
    assert miss.unseen_share > 0.6                                       # 沒出現過的字是拿整段 quote 算的


def test_the_turns_that_hold_an_ambiguous_quote_are_the_ones_reported_as_near():
    """好幾輪都有這段話：要看的是「哪幾輪」，不是模型引用的那一輪（它根本沒有這段話）。"""
    turns = [turn(0, "S1", "預算要先公開，成效也要公開。"), turn(1, "S2", "我同意預算要先公開，成效也要公開。"),
             turn(2, "S1", "好")]

    miss = explain_quote_miss("預算要先公開，成效也要公開", turns[2], turns, CITABLE)

    assert miss.kind == QUOTE_AMBIGUOUS and miss.near == (turns[0], turns[1])


def test_the_ambiguous_turns_are_only_the_ones_a_citable_voice_said():
    turns = [turn(0, "S0", "預算要先公開，成效也要公開。"), turn(1, "S1", "我同意預算要先公開，成效也要公開。"),
             turn(2, "S2", "預算要先公開，成效也要公開，這是基本要求。"), turn(3, "S1", "好")]

    miss = explain_quote_miss("預算要先公開，成效也要公開", turns[3], turns, CITABLE)

    assert miss.kind == QUOTE_AMBIGUOUS and miss.near == (turns[1], turns[2])
