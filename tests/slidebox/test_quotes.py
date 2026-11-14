"""引文核對（locate_quote）與原話（contiguous_span）：多人歸屬的基礎。

說話者永遠是「真正有這段原文的那一輪」的說話者；模型填的編號只是線索。
"""
from __future__ import annotations

import random

import pytest

from slidebox.domain.entities import Turn
from slidebox.usecases.quotes import (
    ECHO_START_RE,
    MIN_QUOTE_CHARS,
    QUOTE_MIN_FRAGMENT,
    QuoteSpan,
    _holds,
    contiguous_span,
    fragments,
    is_restatement,
    locate_quote,
    squash_positions,
)
from slidebox.usecases.speakers import squash

CITABLE = frozenset({"S1", "S2"})


def turn(index: int, speaker: str, text: str) -> Turn:
    return Turn(index, float(index), float(index) + 0.9, speaker, text)


def window() -> list[Turn]:
    return [
        turn(0, "S0", "各位好，今天討論試辦計畫的預算與期程。"),
        turn(1, "S1", "我認為預算應該先編三個月，等成效出來再追加。"),
        turn(2, "S2", "我不同意，三個月太短了，至少要半年才看得出成效。"),
        turn(3, "S0", "請兩位各自說明理由，先編三個月的預算。"),
        turn(4, "S2", "沒錯"),
        turn(5, "S1", "第一，預算要公開；第二，成效要公開；第三，期程要公開。"),
    ]


# --- fragments ---


def test_a_quote_is_cut_at_punctuation_and_whitespace_and_short_pieces_are_dropped():
    assert fragments("我們先試辦三個月，對，然後評估成效") == ["我們先試辦三個月", "然後評估成效"]
    assert fragments("預算 要公開；成效　要公開。") == ["預算", "要公開", "成效", "要公開"]
    assert QUOTE_MIN_FRAGMENT == 2


def test_fragments_ignore_case_and_width():
    assert fragments("ＡＢＣ公司，OK") == ["abc公司", "ok"]


@pytest.mark.parametrize("quote", [None, 12, ["預算"], b"abc", "", "   ", "，。！", "對", "嗯，對，好"])
def test_a_quote_without_a_piece_of_two_characters_has_no_fragments(quote):
    assert fragments(quote) == []


def test_the_pieces_must_all_be_found_in_order_and_the_answer_is_a_real_boolean():
    assert _holds("預算大約三千億", ["預算", "三千億"]) is True
    assert _holds("預算大約三千億", ["三千億", "預算"]) is False                  # 順序反了
    assert _holds("預算大約三千億", ["預算", "補助"]) is False                    # 少了一片


# --- locate_quote：說話者從有這段原文的那一輪查 ---


def test_the_cited_turn_is_used_when_it_holds_the_quote():
    turns = window()

    found = locate_quote("我認為預算應該先編三個月，等成效出來再追加", turns[1], turns, CITABLE)

    assert found.turn is turns[1] and found.problem is None
    assert found.turn.speaker == "S1"


def test_the_one_other_turn_that_holds_the_quote_is_used_instead_of_the_cited_one():
    """最常見的錯：把問題的編號填在答案的引文上。說話者是答案那一輪的人。"""
    turns = window()

    found = locate_quote("我不同意，三個月太短了，至少要半年才看得出成效", turns[1], turns, CITABLE)

    assert found.turn is turns[2] and found.turn.speaker == "S2" and found.problem is None


def test_a_quote_that_two_other_turns_hold_is_ambiguous():
    turns = window()
    turns.append(turn(6, "S1", "我再說一次：成效出來之後才決定要不要追加預算。"))
    turns.append(turn(7, "S2", "我同意，成效出來之後才決定要不要追加預算。"))

    found = locate_quote("成效出來之後才決定要不要追加預算", turns[1], turns, CITABLE)

    assert found.turn is None and "在好幾輪發言裡都有" in found.problem


def test_the_cited_turn_wins_when_it_is_one_of_several_holders():
    turns = window()
    turns.append(turn(6, "S2", "我同意，成效出來之後才決定要不要追加預算。"))
    turns.append(turn(7, "S1", "成效出來之後才決定要不要追加預算，我這樣想。"))

    found = locate_quote("成效出來之後才決定要不要追加預算", turns[7], turns, CITABLE)

    assert found.turn is turns[7] and found.problem is None


def test_a_made_up_quote_is_not_found_anywhere():
    turns = window()

    found = locate_quote("我認為預算應該先編六個月再追加", turns[1], turns, CITABLE)

    assert found.turn is None and "不是 T1 或任何一輪發言裡連續照抄的原文" in found.problem


def test_two_turns_spliced_together_are_not_one_quote():
    turns = window()

    found = locate_quote("先編三個月，等成效出來再追加，我不同意，三個月太短了", turns[1], turns, CITABLE)

    assert found.turn is None and found.problem


def test_the_pieces_have_to_come_in_the_same_order_as_in_the_turn():
    turns = window()

    found = locate_quote("等成效出來再追加，我認為預算應該先編三個月", turns[1], turns, CITABLE)

    assert found.turn is None


def test_a_skipped_filler_between_two_pieces_is_fine_but_a_changed_word_is_not():
    turns = [turn(0, "S1", "我認為預算應該先編三個月，嗯，那個，等成效出來再追加。"), turn(1, "S2", "好")]

    kept = locate_quote("我認為預算應該先編三個月，等成效出來再追加", turns[0], turns, CITABLE)
    changed = locate_quote("我認為預算應該先編三個月，等成果出來再追加", turns[0], turns, CITABLE)

    assert kept.turn is turns[0] and changed.turn is None


def test_width_case_and_punctuation_are_ignored_when_matching():
    turns = [turn(0, "S1", "ＡＢＣ公司今年編了３０００萬元預算，不夠用。"), turn(1, "S2", "好")]

    found = locate_quote("abc公司今年編了3000萬元預算 不夠用", turns[0], turns, CITABLE)

    assert found.turn is turns[0]


def test_only_citable_speakers_can_hold_a_quote():
    """主持人、旁白等不能引用的聲音，就算說過這段話也不算：說話者歸屬不能落到他們身上。"""
    turns = window()

    found = locate_quote("請兩位各自說明理由，先編三個月的預算", turns[3], turns, CITABLE)

    assert found.turn is None and found.problem
    assert locate_quote("請兩位各自說明理由，先編三個月的預算", turns[3], turns, frozenset({"S0", "S1"})).turn is turns[3]


def test_a_turn_of_a_non_citable_speaker_never_counts_as_a_second_holder():
    turns = [turn(0, "S0", "我們先編三個月的預算再說。"), turn(1, "S1", "我們先編三個月的預算再說，之後再評估。"), turn(2, "S2", "好")]

    found = locate_quote("我們先編三個月的預算再說", turns[0], turns, CITABLE)      # 引用的是主持人那一輪，但他不能被歸屬

    assert found.turn is turns[1] and found.turn.speaker == "S1"


def test_a_quote_that_is_too_short_cannot_identify_a_turn():
    turns = window()

    found = locate_quote("三個月太短", turns[2], turns, CITABLE)

    assert found.turn is None and f"不到 {MIN_QUOTE_CHARS} 個字" in found.problem and "三個月太短" in found.problem


def test_a_short_quote_is_fine_when_it_is_the_whole_cited_turn():
    turns = window()

    found = locate_quote("沒錯", turns[4], turns, CITABLE)

    assert found.turn is turns[4] and found.turn.speaker == "S2"


def test_a_two_character_answer_cannot_be_pinned_on_another_turn():
    turns = window()
    turns.append(turn(6, "S1", "沒錯，就是這樣處理。"))

    found = locate_quote("沒錯", turns[6], turns, CITABLE)               # 引用的是 T6，但「沒錯」不是 T6 的整輪

    assert found.turn is None and "太短" in found.problem


@pytest.mark.parametrize("quote", ["對", "嗯", "好", "是"])
def test_a_single_character_answer_is_never_a_quote(quote):
    turns = [turn(0, "S1", quote), turn(1, "S2", "好的")]

    found = locate_quote(quote, turns[0], turns, CITABLE)

    assert found.turn is None
    assert f"quote「{quote}」太短，無法核對出處" in found.problem and "單字的回答（對、嗯）不能當 quote" in found.problem


@pytest.mark.parametrize("quote", ["", "   ", "，。", None, 3])
def test_an_empty_or_missing_quote_is_asked_for(quote):
    turns = window()

    found = locate_quote(quote, turns[1], turns, CITABLE)

    assert found.turn is None and found.problem.startswith("quote 是空的")


def test_a_quote_made_only_of_very_short_pieces_is_not_enough_to_tell_which_turn():
    turns = [turn(0, "S1", "我們的，這個那，問題在，哪裡呢，還是吧。"), turn(1, "S2", "好的")]

    found = locate_quote("我們的，這個那，問題在，哪裡呢", turns[0], turns, CITABLE)

    assert found.turn is None and "是由太短的片段拼起來的" in found.problem


def test_a_long_quote_is_shortened_in_the_message_but_not_in_the_decision():
    turns = window()
    long_quote = "我認為預算應該先編三個月，等成效出來再追加，還有一大段根本沒有說過的話"

    found = locate_quote(long_quote, turns[1], turns, CITABLE)

    assert found.turn is None and "…" in found.problem and long_quote not in found.problem


def test_the_returned_turn_always_holds_every_piece_and_belongs_to_a_citable_speaker():
    """性質：隨機的引文，不管找到誰，找到的那一輪都真的有每一個片段、說話者可以引用。"""
    rng = random.Random(7)
    pool = "預算成效期程公開試辦三個月半年追加同意不同意討論說明理由"
    turns = [turn(i, rng.choice(["S0", "S1", "S2"]), "".join(rng.choice(pool + "，。 ") for _ in range(rng.randint(5, 40))))
             for i in range(12)]
    for _ in range(600):
        quote = "".join(rng.choice(pool + "，。 ") for _ in range(rng.randint(1, 18)))
        cited = rng.choice(turns)

        found = locate_quote(quote, cited, turns, CITABLE)

        if found.turn is not None:
            assert found.turn.speaker in CITABLE
            squashed = squash(found.turn.text)
            position = 0
            for piece in fragments(quote):
                position = squashed.find(piece, position)
                assert position >= 0, (quote, found.turn.text)
                position += len(piece)
        else:
            assert found.problem


def test_a_quote_that_is_a_substring_of_a_turn_is_always_found_when_it_is_long_enough():
    rng = random.Random(11)
    base = "".join(rng.choice("預算成效期程公開試辦三個月半年追加同意討論說明理由") for _ in range(60))
    turns = [turn(0, "S1", base), turn(1, "S2", "完全不相干的一輪發言，沒有重複的字樣")]
    for _ in range(300):
        start = rng.randint(0, 40)
        quote = base[start:start + rng.randint(MIN_QUOTE_CHARS + 4, 20)]

        found = locate_quote(quote, turns[0], turns, CITABLE)

        assert found.turn is turns[0], (quote, found.problem)


# --- 原話：引文在那一輪裡「連續」的位置 ---


def test_the_squashed_text_keeps_the_original_position_of_every_character():
    assert squash_positions("A，b 3") == ("ab3", [0, 2, 4])
    assert squash_positions("ＡＢ１") == ("ab1", [0, 1, 2])


def test_a_contiguous_quote_is_shown_with_the_original_words_of_the_transcript():
    span = contiguous_span("我們認為，預算 應該先編三個月。", "預算應該先編三個月")

    assert span is not None and span.text == "預算 應該先編三個月"
    assert (span.start, span.end) == (5, 15)


def test_whitespace_runs_in_the_turn_are_folded_before_the_position_is_taken():
    span = contiguous_span("我們認為\n\n預算   應該先編三個月。", "預算應該先編三個月")

    assert span is not None and span.text == "預算 應該先編三個月" and span.flat == "我們認為 預算 應該先編三個月。"


def test_a_quote_that_skips_words_is_not_contiguous():
    assert contiguous_span("我們認為預算應該先編三個月。", "我們認為三個月") is None


def test_full_width_characters_are_matched_and_shown_as_the_transcript_has_them():
    span = contiguous_span("ＡＢＣ公司編了３個月的預算。", "abc公司編了3個月")

    assert span is not None and span.text == "ＡＢＣ公司編了３個月"


@pytest.mark.parametrize("quote", ["", "，。", None, 12])
def test_an_empty_quote_has_no_span(quote):
    assert contiguous_span("我們認為預算應該先編三個月。", quote) is None


def test_a_turn_whose_positions_cannot_be_mapped_back_has_no_span():
    """組合字（e＋重音符號）逐字轉換與整段轉換的結果不同：位置對不上，寧可當作不連續。"""
    assert contiguous_span("café 開會討論預算與期程", "開會討論預算") is None


def test_the_span_is_always_literally_inside_the_turn_and_squashes_to_the_quote():
    """性質：不管引文是什麼，回傳的位置一定落在那一輪的原文裡，而且就是引文的那些字。"""
    rng = random.Random(23)
    pool = "預算成效期程公開試辦三個月半年追加ＡＢＣ12，。 \n"
    for _ in range(500):
        text = "".join(rng.choice(pool) for _ in range(rng.randint(4, 50)))
        flat = " ".join(text.split())
        if rng.random() < 0.5 and flat:
            i = rng.randrange(len(flat))
            j = rng.randint(i + 1, len(flat))
            quote = flat[i:j]
        else:
            quote = "".join(rng.choice(pool) for _ in range(rng.randint(1, 8)))

        span = contiguous_span(text, quote)

        if span is None:
            continue
        assert span.flat == flat
        assert span.text in flat and flat[span.start:span.end] == span.text
        assert squash(span.text) == squash(quote)
        assert span.text == span.text.strip()


# --- 複述（is_restatement）與「你剛才說」（ECHO_START_RE） ---


def span_of(text: str, quote: str) -> QuoteSpan:
    span = contiguous_span(text, quote)
    assert span is not None, (text, quote)
    return span


def test_the_first_half_of_a_question_that_asks_for_confirmation_is_a_restatement():
    text = "以標案數量計算，所以十個標案發出去就算達成，土方有沒有清根本不重要，是這個意思嗎？"

    assert is_restatement(span_of(text, "以標案數量計算，所以十個標案發出去就算達成，土方有沒有清根本不重要"))


@pytest.mark.parametrize("tail", ["是這個意思嗎", "是這樣嗎", "對不對", "對吧", "不是嗎", "是不是這樣"])
def test_every_confirmation_phrase_after_the_quote_makes_it_a_restatement(tail):
    text = f"你們認為補助應該全部取消，{tail}？"

    assert is_restatement(span_of(text, "你們認為補助應該全部取消"))


def test_a_claim_followed_by_an_open_question_is_the_speakers_own_claim():
    text = "這個計畫還要再花兩百六十八億，做得到嗎？"

    assert not is_restatement(span_of(text, "這個計畫還要再花兩百六十八億"))


def test_a_quote_that_runs_to_the_end_of_the_sentence_is_never_a_restatement():
    assert not is_restatement(span_of("所以你的意思是補助應該全部取消？", "補助應該全部取消"))
    assert not is_restatement(span_of("我們認為補助應該全部取消。", "我們認為補助應該全部取消"))


def test_a_restating_lead_in_and_a_question_mark_make_a_restatement_even_without_a_confirmation_phrase():
    text = "所以你的意思是補助應該全部取消，然後再重新編列？"

    assert is_restatement(span_of(text, "補助應該全部取消"))


def test_a_restating_lead_in_in_a_statement_is_not_a_restatement():
    text = "所以你的意思是補助應該全部取消，然後再重新編列。"

    assert not is_restatement(span_of(text, "補助應該全部取消"))


def test_only_the_sentence_around_the_quote_counts():
    text = "補助應該全部取消。他的看法是補助應該保留，是這個意思嗎？"

    assert not is_restatement(span_of(text, "補助應該全部取消"))
    assert is_restatement(span_of(text, "他的看法是補助應該保留"))


@pytest.mark.parametrize("text", [
    "你剛才說預算不夠", "您提到的三個月", "妳先前問過這件事", "你說得對", "你問的是預算",
    "你是說要延後嗎", "所以你是說要延後", "也就是你的意思是不要做", "你的意思是不要做", "  你剛剛提過了",
])
def test_a_quote_that_starts_by_relaying_the_other_person_is_an_echo(text):
    assert ECHO_START_RE.search(text)


@pytest.mark.parametrize("text", [
    "我們認為預算不夠", "你好，各位", "預算你剛才說了", "他剛才說預算不夠",
])
def test_a_quote_that_does_not_start_that_way_is_not_an_echo(text):
    assert not ECHO_START_RE.search(text)


def test_a_restating_lead_in_at_the_start_of_the_sentence_counts():
    assert is_restatement(span_of("所以補助應該全部取消，這樣對嗎？", "補助應該全部取消")) is True
    assert is_restatement(span_of("預算有限。所以補助應該全部取消，這樣對嗎？", "補助應該全部取消")) is True


def test_a_quote_that_ends_its_sentence_is_not_a_restatement_even_when_a_question_follows():
    """引文自己是一個完整的句子（後面是句號）：後面的那個問句是另一句，不能回頭把它變成複述。"""
    assert is_restatement(span_of("預算應該全部取消。是這個意思嗎？", "預算應該全部取消")) is False


# --- 每個門檻剛好在邊界上的例子（mutation testing 補的）---


def test_eight_characters_of_pieces_that_are_all_shorter_than_five_cannot_tell_which_turn():
    """合計剛好 8 個字，但最長的片段只有 4 個字：是拼起來的，認不出出處；有一個 5 個字的片段就可以。"""
    spliced = [turn(0, "S1", "天地玄黃，宇宙洪荒，日月盈昃。"), turn(1, "S2", "好的")]
    anchored = [turn(0, "S1", "天地玄黃宇，宙洪荒，日月盈昃。"), turn(1, "S2", "好的")]

    refused = locate_quote("天地玄黃，宇宙洪荒", spliced[0], spliced, CITABLE)
    accepted = locate_quote("天地玄黃宇，宙洪荒", anchored[0], anchored, CITABLE)

    assert refused.turn is None and "是由太短的片段拼起來的" in refused.problem
    assert accepted.turn is anchored[0]


def test_seven_characters_are_too_few_and_eight_are_enough():
    turns = [turn(0, "S1", "天地玄黃宇宙洪荒日月盈昃。"), turn(1, "S2", "好的")]

    seven = locate_quote("天地玄黃宇宙洪", turns[0], turns, CITABLE)
    eight = locate_quote("天地玄黃宇宙洪荒", turns[0], turns, CITABLE)

    assert seven.turn is None and "不到 8 個字" in seven.problem
    assert eight.turn is turns[0]


def test_the_message_shows_at_most_twenty_four_characters_of_a_quote():
    turns = window()
    ten = "甲乙丙丁戊己庚辛壬癸"

    def shown(quote):
        message = locate_quote(quote, turns[1], turns, CITABLE).problem
        return message.split("quote「", 1)[1].split("」", 1)[0]

    assert shown(ten * 2 + "甲乙丙丁") == ten * 2 + "甲乙丙丁"                 # 24 個字：整段照列
    assert shown(ten * 2 + "甲乙丙丁戊") == ten * 2 + "甲乙丙" + "…"            # 25 個字：前 23 個字加「…」
