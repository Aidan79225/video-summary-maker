"""歸屬不可靠而失敗時，記在進度裡的診斷（drop_report.drop_diagnostics）。

要回答的問題：模型提出的重點被丟掉了這麼多，**每一段**丟掉的是什麼原因？quote 對不上，是因為接了兩輪、
來自其他聲音、改了幾個字，還是根本編造的？被丟掉的點，模型寫的 quote 和逐字稿真正的內容差在哪裡？
這份診斷是純文字、只讀：它不改變任何判斷，也不會讓原本的失敗變成別的錯誤。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from slidebox.domain.entities import RawPoint, RawSlide
from slidebox.usecases import conversation
from slidebox.usecases import diarized_pipeline as dp
from slidebox.usecases import drop_report as report
from slidebox.usecases.conversation import RESCUE_REFUSED, Allocation, DroppedPoint, Window, chunk_turns

from .diarized_support import FakeConversation
from .speaker_fixtures import make_speakers, make_turns

SPEAKERS = make_speakers("participant", "participant", "minor")          # S1、S2 主要發言者；S3 其他聲音

LINES = (
    ("S1", "請問今年的預算編列有沒有增加"),
    ("S2", "今年的預算大約增加了兩成左右"),
    ("S1", "那明年的預算打算怎麼編列呢"),
    ("S2", "明年預計再增加一成左右而已"),
    ("S3", "我是打電話進來的聽眾想問一件事"),
)
TURNS = make_turns(*LINES)


def whole(turn: int, text: str) -> RawPoint:
    """文字寫對的點：quote 是那一輪的整段話。"""
    return RawPoint(turn, text, quote=LINES[turn][1])


GOOD = (whole(0, "詢問今年的預算編列有沒有增加"), whole(1, "今年的預算大約增加了兩成左右"))


def run(points, turns=TURNS, speakers=SPEAKERS, budget=10**6, quote_only=False, replies=()):
    """用真的檢查跑一遍：每個窗每次問模型都得到同一份回應（一頁、這些點）；replies 是依序回答前幾次呼叫的回應。"""
    reply = (RawSlide("預算", tuple(points)),)
    allocations = tuple(Allocation(w, 1, 3) for w in chunk_turns(turns, speakers, budget))
    return dp.summarize_windows(FakeConversation(replies, default=lambda call: reply), allocations, turns, speakers,
                                lambda frac, status: None, lambda: None, lambda: False, quote_only)


def real_outcomes(*dropped_points: RawPoint, **kwargs):
    """兩個一定通過的點，加上 dropped_points（它們都該被丟掉）。"""
    return run((*GOOD, *dropped_points), **kwargs)


def text_of(outcomes, speakers=SPEAKERS) -> str:
    return report.drop_diagnostics(outcomes, speakers)


def example_labels(text: str) -> Counter:
    """每個例子標頭裡的「原因碼（／分類）」各有幾個。"""
    return Counter(re.findall(r"\[第 \d+ 段 T\S+ ([^\]\s]+)\]", text))


# 各種原因的點（都用上面這份小對談）
PARTIAL = RawPoint(0, "詢問預算", quote="請問今年的預算編列有沒有增加，然後全面檢討所有補助項目")
OTHER_VOICE = RawPoint(0, "詢問聽眾的事", quote="我是打電話進來的聽眾想問一件事")
ABSENT = RawPoint(0, "談論氣候變遷政策", quote="今年的氣候變遷政策完全沒有進展")
PRONOUN = whole(1, "他認為預算增加了兩成")
FIRST_PERSON = whole(1, "我認為預算增加了兩成")


def absent(n: int) -> RawPoint:
    return RawPoint(0, "談論氣候變遷政策", quote=f"完全沒有出現在這個對談裡的第{n}句話啊啊啊")


# --- 整體的樣子 ---


def test_the_text_says_what_it_is_and_what_x_means():
    text = text_of(real_outcomes(PARTIAL))

    assert text.startswith("診斷：被丟掉的重點")
    assert "X" in text.splitlines()[0] and "不能引用" in text.splitlines()[0]


def test_each_window_gets_a_line_with_its_turn_range_asks_counts_and_codes():
    outcomes = real_outcomes(PARTIAL, OTHER_VOICE, ABSENT, PRONOUN)

    lines = text_of(outcomes).splitlines()

    (line,) = [l for l in lines if l.startswith("  第 1/1 段")]
    assert line.startswith("  第 1/1 段（T0–T4，問了 2 次，採用第 1 次）：提出 6、留下 2、丟掉 4：")
    assert "bad_quote 3" in line and "leading_pronoun_or_title 1" in line
    assert line.index("bad_quote 3") < line.index("leading_pronoun_or_title 1")     # 多的在前


def test_the_asks_are_left_out_when_the_outcome_does_not_know_them():
    outcome = real_outcomes(PRONOUN)[0]
    outcome = dp.WindowOutcome(outcome.allocation, outcome.slides, outcome.proposed, outcome.kept,
                               dropped=outcome.dropped, dropped_by_code=outcome.dropped_by_code)     # attempts 預設 0

    assert "  第 1/1 段（T0–T4）：提出 3、留下 2、丟掉 1：leading_pronoun_or_title 1" in text_of((outcome,))


def test_a_window_asked_twice_says_which_of_the_answers_its_numbers_are_from():
    """攔的 bug：第一次的結果比重問的好、用的是第一次，卻只寫「問了 2 次：提出…」，讀的人會當成是重問的結果，
    看不出「模型有沒有聽進重試的提示」。"""
    first = (RawSlide("預算", (*GOOD, PRONOUN, FIRST_PERSON, PARTIAL)),)             # 5 個丟 3 個：要重問
    second = (RawSlide("預算", GOOD),)                                                # 全部通過

    (outcome,) = run((), replies=(first, second))

    assert (outcome.attempts, outcome.adopted) == (2, 2)
    assert "  第 1/1 段（T0–T4，問了 2 次，採用第 2 次）：提出 2、留下 2、丟掉 0\n" in text_of((outcome,)) + "\n"


def test_the_totals_add_up_all_the_windows_and_all_the_codes():
    outcomes = (*real_outcomes(PARTIAL, ABSENT), *real_outcomes(PRONOUN))

    text = text_of(outcomes)

    (total,) = [l for l in text.splitlines() if l.startswith("合計：")]
    assert total == "合計：提出 7、留下 4、丟掉 3：bad_quote 2、leading_pronoun_or_title 1"
    assert "  第 1/2 段" in text and "  第 2/2 段" in text


def test_a_window_that_was_never_sent_to_the_model_says_so():
    turns = make_turns(*LINES[:2], ("S3", "我是打電話進來的聽眾想問一件事"), ("S3", "請問這個政策什麼時候開始實施呢"))
    outcomes = real_outcomes(turns=turns, budget=60)
    assert [o.skipped for o in outcomes] == [False, True]

    text = text_of(outcomes)

    assert "  第 2/2 段（T2–T3）：沒有主要發言者可以引用，沒有送給模型" in text


def test_a_window_without_any_drop_still_has_its_line():
    text = text_of(real_outcomes())

    assert "  第 1/1 段（T0–T4，問了 1 次）：提出 2、留下 2、丟掉 0\n" in text
    assert "合計：提出 2、留下 2、丟掉 0" in text
    assert "例子" not in text and "quote 對不上的原因" not in text           # 沒有東西可以解釋


def test_nothing_proposed_at_all_still_produces_a_text():
    assert text_of(()).startswith("診斷：被丟掉的重點")
    assert "合計：提出 0、留下 0、丟掉 0" in text_of(())


# --- bad_quote 為什麼對不上 ---


def test_the_reasons_quotes_did_not_match_are_counted_with_a_plain_description():
    text = text_of(real_outcomes(PARTIAL, OTHER_VOICE, ABSENT))

    assert "quote 對不上的原因（bad_quote 共 3 個）：" in text
    assert "  partial 1：只有一部分對得上" in text
    assert "  other_voice 1：一字不差，但出自其他聲音或無法判斷的輪（不能引用）" in text
    assert "  absent 1：幾乎不在這一窗的逐字稿裡" in text


AMBIGUOUS_TURNS = make_turns(("S1", LINES[0][1]), ("S2", LINES[1][1]), ("S1", "預算要先公開，成效也要公開。"),
                             ("S2", "我同意預算要先公開，成效也要公開。"), ("S1", "好的，我們繼續下一題吧"))

# （原因, 例子, 這份對談, 說明文字）：每一種 quote 對不上的原因都用真的檢查產生一次，說明文字寫死在這裡
DESCRIBED_REASONS = [
    pytest.param("other_voice", OTHER_VOICE, TURNS, "一字不差，但出自其他聲音或無法判斷的輪（不能引用）", id="other_voice"),
    pytest.param("across_turns", RawPoint(0, "詢問預算", quote="請問今年的預算編列有沒有增加今年的預算大約增加了兩成"), TURNS,
                 "接了相鄰的兩、三輪才湊得齊", id="across_turns"),
    pytest.param("reordered", RawPoint(0, "詢問預算", quote="有沒有增加，請問今年的預算編列"), TURNS,
                 "每一片都在同一輪裡，但順序和 quote 不同", id="reordered"),
    pytest.param("near_verbatim", RawPoint(0, "詢問預算", quote="請問今年的預算編列有沒有增減"), TURNS,
                 "照抄得差不多，只差幾個字（同音字、數字寫法、少數簡繁體字）", id="near_verbatim"),
    pytest.param("partial", PARTIAL, TURNS, "只有一部分對得上（跳過子句、改寫）", id="partial"),
    pytest.param("absent", ABSENT, TURNS, "幾乎不在這一窗的逐字稿裡（編造或整段改寫）", id="absent"),
    pytest.param("empty", RawPoint(0, "談論今年預算", quote=""), TURNS, "quote 是空的", id="empty"),
    pytest.param("quote_too_short", RawPoint(1, "談論今年預算增加的幅度", quote="預算大約"), TURNS,
                 "quote 太短，認不出是哪一輪", id="quote_too_short"),
    pytest.param("short_fragments", RawPoint(0, "詢問預算", quote="今年，預算，編列，增加"), TURNS,       # 片段依序都在 T0 裡
                 "quote 由太短的片段拼成，認不出是哪一輪", id="short_fragments"),
    pytest.param("ambiguous", RawPoint(4, "談論公開", quote="預算要先公開，成效也要公開"), AMBIGUOUS_TURNS,
                 "好幾輪都有這段話，無法確定是哪一輪", id="ambiguous"),
]


@pytest.mark.parametrize(("kind", "point", "turns", "description"), DESCRIBED_REASONS)
def test_every_reason_a_quote_did_not_match_is_described_in_plain_words(kind, point, turns, description):
    """攔的 bug：說明文字被改掉、清空，或某種原因被歸到別的名字底下，統計裡就只剩一個讀不懂的英文代號。
    每一種原因都用真的檢查產生一次，才知道檢查真的會產生它。"""
    text = text_of(real_outcomes(point, turns=turns))

    assert f"quote 對不上的原因（bad_quote 共 1 個）：\n  {kind} 1：{description}\n" in text + "\n"
    assert f"bad_quote／{kind}]" in text


def test_the_most_common_reason_comes_first():
    text = text_of(real_outcomes(PARTIAL, ABSENT, absent(1)))

    lines = text.splitlines()
    assert lines.index("  absent 2：幾乎不在這一窗的逐字稿裡（編造或整段改寫）") < next(
        i for i, line in enumerate(lines) if line.startswith("  partial 1"))


def test_quotes_with_characters_the_transcript_never_used_are_counted_separately():
    """攔的 bug：模型把照抄的字改成簡體，只看到「對不上」，看不出是字體不同。"""
    simplified = RawPoint(0, "詢問預算", quote="请问今年的预算编列有没有增加")
    reshuffled = RawPoint(0, "詢問預算", quote="有沒有增加請問今年的預算編列")         # 每個字都出現過，只是順序不對

    text = text_of(real_outcomes(simplified, reshuffled))

    assert "quote 對不上的原因（bad_quote 共 2 個）：" in text
    assert "其中 1 個的 quote 有 20% 以上的字是這一窗逐字稿從沒出現過的（簡體字或編造的詞）" in text


def test_the_breakdown_is_left_out_when_no_quote_was_dropped():
    text = text_of(real_outcomes(PRONOUN))

    assert "quote 對不上的原因" not in text


def test_a_cited_turn_outside_the_window_cannot_be_explained_and_is_counted_as_unknown():
    """BAD_QUOTE 一定有被引用的那一輪；萬一紀錄裡的編號不在這個窗裡，也不能讓整份診斷壞掉。"""
    outcome = real_outcomes(PARTIAL)[0]
    stray = DroppedPoint("bad_quote", "quote 核對不過", "任何一句話在這裡", "文字", 999)
    outcome = dp.WindowOutcome(outcome.allocation, outcome.slides, 3, 2, dropped=(stray,),
                               dropped_by_code=(("bad_quote", 1),), attempts=1)

    text = text_of((outcome,))

    assert "  unknown 1：引用的輪不在這一窗裡，無法分類" in text and "[第 1 段 T999 bad_quote／unknown]" in text


# --- unsupported：為什麼不能改用原話 ---


def refusal(reason: str) -> str:
    return f"內容在 T3 前後幾段話裡都找不到依據；請只寫那段話說的事（{RESCUE_REFUSED}{reason}）"


def unsupported(reason: str, n: int = 0) -> DroppedPoint:
    return DroppedPoint("unsupported", refusal(reason), f"原話{n}原話{n}原話", f"重點{n}", 1)


def test_the_reasons_a_quote_could_not_be_shown_instead_are_grouped_ignoring_numbers_and_quoted_words():
    outcome = real_outcomes(PARTIAL)[0]
    drops = (unsupported("原話裡有「他們」（代名詞、稱謂或逐字稿裡用來稱呼人的名字）", 1),
             unsupported("原話裡有「你們」（代名詞、稱謂或逐字稿裡用來稱呼人的名字）", 2),
             unsupported("quote 不是 T3 裡連續的一段原話（中間跳過了字句）", 3))
    outcome = dp.WindowOutcome(outcome.allocation, outcome.slides, 5, 2, dropped=drops,
                               dropped_by_code=(("unsupported", 3),), attempts=1)

    text = text_of((outcome,))

    assert "不能改用原話的原因（unsupported 共 3 個）：" in text
    assert "  2 個：原話裡有「…」（代名詞、稱謂或逐字稿裡用來稱呼人的名字）" in text
    assert "  1 個：quote 不是 T# 裡連續的一段原話（中間跳過了字句）" in text
    assert text.index("  2 個：") < text.index("  1 個：")


def test_the_refusal_reason_is_read_from_a_real_unsupported_drop():
    """格式用真的檢查產生：說明文字改了而這裡的剖析沒跟著改，就會在這裡被抓到。"""
    turns = make_turns(("S1", "他們今年的預算大約增加了兩成左右"), ("S2", "明年預計再增加一成左右而已"),
                       ("S1", "請問今年的預算編列有沒有增加"))
    unrelated = RawPoint(0, "完全不同的主題在這裡亂寫一通", quote="他們今年的預算大約增加了兩成左右")
    second = RawPoint(2, "詢問預算編列", quote="請問今年的預算編列有沒有增加")
    third = RawPoint(1, "明年預計再增加一成", quote="明年預計再增加一成左右而已")
    outcomes = run((second, third, unrelated), turns=turns)
    assert any(d.code == "unsupported" and RESCUE_REFUSED in d.message for o in outcomes for d in o.dropped)

    text = text_of(outcomes)

    assert "不能改用原話的原因（unsupported 共 1 個）：" in text
    assert "  1 個：原話裡有「…」（代名詞、稱謂或逐字稿裡用來稱呼人的名字）" in text


def test_an_unsupported_drop_without_the_refusal_marker_is_still_listed():
    outcome = real_outcomes(PARTIAL)[0]
    plain = DroppedPoint("unsupported", "內容在 T3 前後幾段話裡都找不到依據", "一句話在這裡出現", "重點", 1)
    outcome = dp.WindowOutcome(outcome.allocation, outcome.slides, 3, 2, dropped=(plain,),
                               dropped_by_code=(("unsupported", 1),), attempts=1)

    assert "  1 個：內容在 T# 前後幾段話裡都找不到依據" in text_of((outcome,))


# --- 例子 ---


def test_a_bad_quote_example_shows_the_models_quote_next_to_the_closest_turn():
    text = text_of(real_outcomes(PARTIAL))

    assert "[第 1 段 T0 bad_quote／partial]" in text
    assert "     模型寫的重點：「詢問預算」" in text
    assert "     模型的 quote：「請問今年的預算編列有沒有增加，然後全面檢討所有補助項目」" in text
    assert "     逐字稿 T0 S1（相同約 54%）：「請問今年的預算編列有沒有增加」" in text


def test_a_quote_that_is_nowhere_in_the_window_shows_the_turn_the_model_pointed_at():
    text = text_of(real_outcomes(ABSENT))

    assert "[第 1 段 T0 bad_quote／absent]" in text
    assert "     它引用的 T0 S1：「請問今年的預算編列有沒有增加」" in text


def test_a_quote_copied_from_a_voice_nobody_may_cite_shows_that_turn_as_x():
    text = text_of(real_outcomes(OTHER_VOICE))

    assert "[第 1 段 T0 bad_quote／other_voice]" in text
    assert "     逐字稿 T4 X：「我是打電話進來的聽眾想問一件事」" in text


def test_a_quote_spliced_from_two_turns_shows_both():
    spliced = RawPoint(0, "詢問預算", quote="請問今年的預算編列有沒有增加今年的預算大約增加了兩成")

    text = text_of(real_outcomes(spliced))

    assert "bad_quote／across_turns]" in text
    assert "     逐字稿 T0 S1：" in text and "     逐字稿 T1 S2：" in text


def test_a_quote_with_its_pieces_in_the_wrong_order_shows_the_turn_that_has_them():
    """模型引用 T1，quote 其實是 T0 裡兩段話顛倒過來：要看的是有這兩段話的 T0，不是沒有這些話的 T1。"""
    swapped = RawPoint(1, "詢問預算", quote="有沒有增加，請問今年的預算編列")

    text = text_of(real_outcomes(swapped))

    assert "[第 1 段 T1 bad_quote／reordered]" in text
    assert "     逐字稿 T0 S1：「請問今年的預算編列有沒有增加」" in text
    assert "它引用的" not in text


def test_the_characters_the_transcript_never_used_are_listed_in_the_example():
    simplified = RawPoint(0, "詢問預算", quote="请问今年的预算编列有没有增加")

    text = text_of(real_outcomes(simplified))

    assert "     逐字稿從沒出現過的字：请 问 预 编 没" in text


def test_an_example_of_another_code_shows_what_the_model_wrote_and_the_turn_it_cited():
    text = text_of(real_outcomes(PRONOUN))

    assert "[第 1 段 T1 leading_pronoun_or_title]" in text
    assert "     模型寫的重點：「他認為預算增加了兩成」" in text
    assert "     模型的 quote：「今年的預算大約增加了兩成左右」" in text
    assert "     它引用的 T1 S2：「今年的預算大約增加了兩成左右」" in text


# --- 例子要讓人看得出「檢查是對著哪一輪做的、為什麼丟」---


def test_an_example_shows_where_the_quote_really_is_when_the_model_cited_another_turn():
    """攔的 bug：模型把問題的編號（T0）填在答案的 quote（T1）上是最常見的錯。檢查是對著 quote 所在的 T1 做的，
    例子卻只顯示它引用的 T0——那一輪連數字都沒有，讀的人會以為檢查壞了。"""
    text = text_of(real_outcomes(RawPoint(0, "預算增加了五成", quote=LINES[1][1])))

    assert "[第 1 段 T0 number_mismatch]" in text
    assert "     quote 所在的 T1 S2（模型填的是 T0）：「今年的預算大約增加了兩成左右」" in text
    assert "它引用的" not in text


def test_an_example_shows_the_checks_own_reason():
    """攔的 bug：只有 unsupported 看得到原因；數字對不上是哪一個、否定寫反了、歸屬衝突是哪一輪，一個字都沒說，
    「被檢查丟掉的是檢查太嚴還是模型亂寫」就沒辦法判斷。"""
    outcomes = real_outcomes(RawPoint(0, "預算增加了五成", quote=LINES[1][1]))
    (dropped,) = [d for o in outcomes for d in o.dropped]

    text = text_of(outcomes)

    assert f"     原因：{dropped.message}" in text
    assert "數字「五成」不在被引用的那段話裡" in text


def test_an_anchor_conflict_shows_the_turn_the_content_came_from_too():
    text = text_of(real_outcomes(RawPoint(2, "預算大約增加了兩成左右", quote=LINES[3][1])))

    assert "[第 1 段 T2 anchor_conflict]" in text
    assert "     原因：重點的內容出自別輪發言（T1），與 quote 所在的 T3 不一致" in text
    assert "     quote 所在的 T3 S2（模型填的是 T2）：「明年預計再增加一成左右而已」" in text
    assert "     原因裡提到的 T1 S2：「今年的預算大約增加了兩成左右」" in text


def test_a_turn_the_reason_mentions_is_not_shown_twice():
    """數字對不上的原因結尾是（T1）：那就是 quote 所在的那一輪，已經顯示過了。"""
    text = text_of(real_outcomes(RawPoint(0, "預算增加了五成", quote=LINES[1][1])))

    assert "原因裡提到的" not in text


def test_a_bad_quote_example_leaves_the_reason_to_the_breakdown():
    """quote 對不上的原因已經分類、統計裡也說明了；例子不再把同一段話（還含模型的 quote）重複一遍。"""
    text = text_of(real_outcomes(ABSENT))

    assert "     原因：" not in text


def test_an_ambiguous_quote_shows_every_turn_that_has_it():
    """攔的 bug：好幾輪都有這段話，例子卻只顯示模型引用的那一輪（它根本沒有這段話），
    讀起來像是模型編造；要看的是「哪幾輪」都有。"""
    text = text_of(real_outcomes(RawPoint(4, "談論公開", quote="預算要先公開，成效也要公開"), turns=AMBIGUOUS_TURNS))

    assert "[第 1 段 T4 bad_quote／ambiguous]" in text
    assert "     逐字稿 T2 S1：「預算要先公開，成效也要公開。」" in text
    assert "     逐字稿 T3 S2：「我同意預算要先公開，成效也要公開。」" in text
    assert "它引用的" not in text


def test_an_ambiguous_quote_held_by_many_turns_shows_three_and_counts_the_rest():
    shared = "預算要先公開，成效也要公開。"
    turns = make_turns(("S1", LINES[0][1]), ("S2", LINES[1][1]), *(("S1" if i % 2 else "S2", shared) for i in range(5)),
                       ("S1", "好的，我們繼續下一題吧"))

    text = text_of(real_outcomes(RawPoint(7, "談論公開", quote="預算要先公開，成效也要公開"), turns=turns))

    shown = re.findall(r"^     逐字稿 (T\d+ S\d)：", text, re.MULTILINE)
    assert shown == ["T2 S2", "T3 S1", "T4 S2"]                      # 五輪都有這段話，只顯示前三輪
    assert "     （另外還有 2 輪也有這段話）" in text


def test_a_too_short_quote_is_not_confused_with_a_too_short_text():
    """攔的 bug：quote 太短與重點的文字太短都叫 too_short；窗的計數行（文字）和原因統計（quote）會被讀成同一件事。"""
    short_text = RawPoint(1, "談預", quote=LINES[1][1])
    short_quote = RawPoint(1, "談論今年預算增加的幅度", quote="預算大約")                # 找得到，但只有四個字

    text = text_of(real_outcomes(short_text, short_quote))

    assert "bad_quote 1、too_short 1" in text                          # 計數行的 too_short 是文字太短
    assert "  quote_too_short 1：quote 太短，認不出是哪一輪" in text
    assert "[第 1 段 T1 bad_quote／quote_too_short]" in text


def test_no_reason_a_quote_did_not_match_shares_its_name_with_a_code_a_point_is_dropped_for():
    codes = {value for name, value in vars(conversation).items() if name.isupper() and isinstance(value, str)}

    assert not set(report._KIND_TEXT) & codes


def test_examples_are_numbered_and_the_biggest_group_comes_first():
    text = text_of(real_outcomes(PARTIAL, ABSENT, absent(1), PRONOUN))

    numbered = re.findall(r"^  (\d+)\. \[第 \d+ 段 T\S+ (\S+)\]$", text, re.MULTILINE)
    assert numbered == [("1", "bad_quote／absent"), ("2", "bad_quote／partial"),
                        ("3", "leading_pronoun_or_title"), ("4", "bad_quote／absent")]


def test_every_group_gets_an_example_before_any_group_gets_a_second_one():
    text = text_of(real_outcomes(PARTIAL, PRONOUN, *(absent(n) for n in range(6))))

    order = [label for _, label in re.findall(r"^  (\d+)\. \[第 \d+ 段 T\S+ (\S+)\]$", text, re.MULTILINE)]
    assert order == ["bad_quote／absent", "bad_quote／partial", "leading_pronoun_or_title", "bad_quote／absent"]


def test_there_are_at_most_the_limit_of_examples_and_at_most_two_per_group():
    many = [p for point in (PARTIAL, OTHER_VOICE, PRONOUN, FIRST_PERSON) for p in (point,) * 3]
    many += [absent(n) for n in range(3)]

    labels = example_labels(text_of(real_outcomes(*many)))

    assert set(labels) == {"bad_quote／partial", "bad_quote／other_voice", "bad_quote／absent",
                           "leading_pronoun_or_title", "first_person"}
    assert max(labels.values()) == 2                                  # 五種原因，每種最多兩個：沒有超過上限


def test_the_examples_stop_at_the_limit_even_when_there_are_more_reasons_than_that():
    """每種原因兩個、共 7 種：14 個要給，只給上限那麼多（多的原因先給，每種原因先各給一個）。"""
    labels = example_labels(text_of((many_codes_outcome(7), many_codes_outcome(7))))

    assert sum(labels.values()) == report.MAX_EXAMPLES
    assert max(labels.values()) == 2 and len(labels) == 7


def test_the_second_example_of_a_group_comes_from_another_window_when_there_is_one():
    """一個原因出現在好幾個窗，兩個例子就看兩個不同的窗，不是同一個窗的兩個點。"""
    turns = make_turns(*LINES[:4], *LINES[:4])
    outcomes = run((*GOOD, PRONOUN, PRONOUN), turns=turns, budget=95)         # 同一份回應，兩個窗各自引用 T0、T1
    assert len(outcomes) == 2 and outcomes[0].dropped and outcomes[1].dropped

    text = text_of(outcomes)

    assert re.findall(r"\[第 (\d+) 段 T\S+ leading_pronoun_or_title\]", text) == ["1", "2"]


def test_a_long_quote_and_a_long_turn_are_clipped_around_the_part_that_matches():
    filler = "前面還有很多很多很多很多很多很多很多很多很多很多話在說別的事情" * 3
    long_turn = filler + "預算大約要三千億比去年多了兩成" + "後面也有很多很多很多很多很多很多很多話" * 3
    turns = make_turns(*LINES[:2], ("S1", long_turn), ("S2", "好的謝謝你的說明"))
    point = RawPoint(2, "談論今年預算", quote="預算大約要三千五百億比去年多了兩成")

    text = text_of(real_outcomes(point, turns=turns))

    line = next(l for l in text.splitlines() if l.strip().startswith("逐字稿 T2"))
    assert line.startswith("     逐字稿 T2 S1（相同約 88%）：「…") and line.endswith("…」")       # 17 個字要改兩個字
    assert "預算大約要三千億比去年多了兩成" in line and len(line) < 140


def test_a_very_long_quote_is_clipped():
    point = RawPoint(0, "談論今年預算", quote="這是一句非常非常長的話" * 30)

    text = text_of(real_outcomes(point))

    quote_line = next(l for l in text.splitlines() if "模型的 quote：" in l)
    assert quote_line.endswith("…」") and len(quote_line) < 100


# --- 安全模式（只顯示原話）---

QUOTE_ONLY_TURNS = make_turns(("S1", LINES[0][1]), ("S2", LINES[1][1]), ("S1", "他們今年的預算大約增加了兩成左右"),
                              ("S2", LINES[3][1]), ("S3", LINES[4][1]))
PRONOUN_QUOTE = RawPoint(2, "談論預算", quote="他們今年的預算大約增加了兩成左右")


def quote_only_outcomes(*points):
    return real_outcomes(*points, turns=QUOTE_ONLY_TURNS, quote_only=True)


def test_in_quote_only_mode_the_reasons_a_found_quote_cannot_be_shown_are_listed():
    """攔的 bug：安全模式下 quote 找得到卻不能直接顯示，是最常見的丟法；診斷只說「找得到」，
    看不出是哪一條顯示規則（代名詞、太長、太短、複述…）擋下來的。"""
    outcomes = quote_only_outcomes(PRONOUN_QUOTE)
    assert [d.code for o in outcomes for d in o.dropped] == ["bad_quote"]            # 找得到，只是不能顯示

    text = text_of(outcomes)

    assert "  located 1：quote 本身找得到，但不能直接當重點顯示（只顯示原話的模式）" in text
    assert "quote 不能直接當重點顯示的原因（bad_quote／located 共 1 個）：" in text
    assert "  1 個：原話裡有「…」（代名詞、稱謂或逐字稿裡用來稱呼人的名字）" in text


def test_a_found_quote_that_could_not_be_shown_says_why_in_its_example():
    text = text_of(quote_only_outcomes(PRONOUN_QUOTE))

    assert "[第 1 段 T2 bad_quote／located]" in text
    assert "     原因：原話裡有「他們」（代名詞、稱謂或逐字稿裡用來稱呼人的名字）" in text
    assert "     逐字稿 T2 S1：「他們今年的預算大約增加了兩成左右」" in text
    assert "請抄一個完整" not in text                      # 給模型看的建議，診斷裡不用重複


def test_the_reasons_a_quote_could_not_be_shown_are_left_out_when_nothing_was_refused_for_that():
    text = text_of(real_outcomes(PARTIAL, PRONOUN))

    assert "不能直接當重點顯示的原因" not in text


def test_a_refusal_without_the_known_wording_is_still_listed():
    outcome = real_outcomes(PARTIAL)[0]
    plain = DroppedPoint("bad_quote", "另一種說法（T3）", LINES[1][1], "重點", 1)       # quote 找得到，所以分在 located
    outcome = dp.WindowOutcome(outcome.allocation, outcome.slides, 3, 2, dropped=(plain,),
                               dropped_by_code=(("bad_quote", 1),), attempts=1)

    assert "  1 個：另一種說法（T#）" in text_of((outcome,))


# --- 例子的數量與標頭 ---


def many_codes_outcome(count: int):
    """count 種原因、各丟一個點（原因碼是手寫的）。"""
    outcome = real_outcomes(PARTIAL)[0]
    drops = tuple(DroppedPoint(f"code_{n:02d}", f"原因{n}", f"一句話在這裡出現{n}", f"寫的字{n}", 1) for n in range(count))
    return dp.WindowOutcome(outcome.allocation, outcome.slides, 2 + count, 2, dropped=drops,
                            dropped_by_code=tuple((d.code, 1) for d in drops), attempts=1)


def test_the_header_of_the_examples_says_how_they_were_chosen():
    text = text_of((many_codes_outcome(3),))

    assert f"例子（模型寫的和逐字稿對照；多的原因先給，每種原因先各給一個，最多 {report.MAX_EXAMPLES} 個）：" in text


def test_the_reasons_that_got_no_example_are_named():
    """攔的 bug：原因超過例子的上限，標頭卻說「每一種原因先各給一個」，少見的原因沒有例子，讀的人不知道它存在。"""
    limit = report.MAX_EXAMPLES
    text = text_of((many_codes_outcome(limit + 2),))

    assert text.count("[第 ") == limit
    assert f"  沒有附例子的原因：code_{limit:02d} 1、code_{limit + 1:02d} 1" in text


def test_no_reason_is_named_as_left_out_when_every_one_has_an_example():
    assert "沒有附例子" not in text_of((many_codes_outcome(report.MAX_EXAMPLES),))


def test_a_reason_that_lost_its_example_to_the_length_limit_is_named_too(monkeypatch):
    outcomes = (many_codes_outcome(3),)
    full = text_of(outcomes)
    monkeypatch.setattr(report, "MAX_CHARS", len(full) - 1)

    cut = text_of(outcomes)

    assert cut.count("[第 ") == 2
    assert "  沒有附例子的原因：code_02 1" in cut


# --- 長度 ---


def test_when_the_text_is_too_long_the_examples_are_what_gets_dropped_first(monkeypatch):
    drops = [absent(n) for n in range(3)] + [PARTIAL, OTHER_VOICE, PRONOUN]
    outcomes = real_outcomes(*drops)
    full = text_of(outcomes)
    shown = full.count("[第 ")
    assert shown >= 4

    monkeypatch.setattr(report, "MAX_CHARS", len(full) - 1)
    cut = text_of(outcomes)

    assert len(cut) <= len(full) - 1
    assert cut.count("[第 ") == shown - 1
    assert cut.split("例子")[0] == full.split("例子")[0]          # 每一段的計數、原因的統計，一個字都沒少
    numbered = re.compile(r"^  \d+\. \[第 \d+ 段 T\S+ \S+\]$", re.MULTILINE)
    assert numbered.findall(cut) == numbered.findall(full)[:-1]      # 省略的是排在最後的（最小的原因的）那一個


def test_a_text_that_is_still_too_long_without_any_example_is_cut_and_says_so(monkeypatch):
    monkeypatch.setattr(report, "MAX_CHARS", 200)

    text = text_of(real_outcomes(PARTIAL, OTHER_VOICE, ABSENT, PRONOUN))

    assert len(text) <= 200 and text.splitlines()[-2:] == [report.TRUNCATED_NOTE, report.END_LINE]
    assert text.startswith("診斷：被丟掉的重點")


def test_only_the_first_windows_are_listed_when_there_are_very_many():
    outcome = real_outcomes(PARTIAL)[0]
    many = tuple(outcome for _ in range(report.MAX_WINDOW_LINES + 5))

    text = text_of(many)

    assert text.count("  第 ") == report.MAX_WINDOW_LINES
    assert "（另有 5 段沒有列出）" in text
    assert len(text) <= report.MAX_CHARS


def test_the_real_limit_holds_for_a_huge_run():
    outcome = real_outcomes(PARTIAL, OTHER_VOICE, ABSENT, PRONOUN, FIRST_PERSON, *(absent(n) for n in range(30)))[0]

    text = text_of(tuple(outcome for _ in range(300)))

    assert len(text) <= report.MAX_CHARS


def test_the_text_does_not_depend_on_the_hash_seed_of_the_process():
    """攔的 bug：用 set 排「逐字稿從沒出現過的字」之類的清單，同一份輸入在不同的行程裡排出不同的順序，
    同一支影片重跑兩次的診斷就對不起來。同一個行程裡跑兩次看不出來，要換雜湊種子各跑一個行程。"""
    root = Path(__file__).resolve().parents[2]
    script = (
        "import sys; sys.stdout.reconfigure(encoding='utf-8')\n"
        "from tests.slidebox import test_drop_report as t\n"
        "from slidebox.domain.entities import RawPoint\n"
        "simplified = RawPoint(0, '詢問預算', quote='请问今年的预算编列有没有增加')\n"
        "print(t.text_of(t.real_outcomes(t.PARTIAL, t.OTHER_VOICE, t.ABSENT, t.PRONOUN, simplified)))\n")

    def printed(seed: str) -> str:
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root)]),
               "PYTHONDONTWRITEBYTECODE": "1"}
        done = subprocess.run([sys.executable, "-c", script], cwd=root, env=env, capture_output=True,
                              encoding="utf-8", check=True)
        return done.stdout

    texts = {seed: printed(seed) for seed in ("0", "1", "2")}

    assert "逐字稿從沒出現過的字：请 问 预 编 没" in texts["0"]
    assert len(set(texts.values())) == 1


# --- 診斷不能讓失敗變成別的錯誤 ---


def test_a_drop_the_checks_never_explained_still_gets_an_example():
    """攔的 bug：紀錄裡的點引用的輪不在窗裡（或不是整數），找不到輪就整個例子壞掉。"""
    outcome = real_outcomes(PARTIAL)[0]
    stray = DroppedPoint("turn_out_of_window", "引用的 T999 不在這一段範圍（T0–T4）", "某一句話在這裡出現了", "寫的字", 999)
    broken = DroppedPoint("turn_not_int", "turn 必須是整數編號", "另一句話在這裡出現了", "寫的字", None)
    outcome = dp.WindowOutcome(outcome.allocation, outcome.slides, 4, 2, dropped=(stray, broken),
                               dropped_by_code=(("turn_not_int", 1), ("turn_out_of_window", 1)), attempts=1)

    text = text_of((outcome,))

    assert "[第 1 段 T999 turn_out_of_window]" in text and "[第 1 段 T? turn_not_int]" in text
    assert "     模型寫的重點：「寫的字」" in text and "     它引用的" not in text


@pytest.mark.parametrize("quote", ["", "對", "三個月", "，。"])
def test_unusable_quotes_still_make_a_readable_example(quote):
    text = text_of(real_outcomes(RawPoint(0, "談論今年預算", quote=quote)))

    assert "[第 1 段 T0 bad_quote／" in text and "     模型的 quote：" in text


def test_an_empty_quote_is_shown_as_empty_rather_than_as_nothing():
    text = text_of(real_outcomes(RawPoint(0, "談論今年預算", quote="")))

    assert "     模型的 quote：（空的）" in text and "bad_quote／empty]" in text


def test_an_empty_text_is_shown_as_empty_rather_than_as_nothing():
    """攔的 bug：文字是空的，整行「模型寫的重點」就不見了，例子只剩一個看起來很正常的 quote，
    看不出這個點是因為文字太短才被丟的。"""
    text = text_of(real_outcomes(RawPoint(1, "", quote=LINES[1][1])))

    assert "[第 1 段 T1 too_short]" in text
    assert "     模型寫的重點：（空的）" in text


def test_a_turn_without_text_does_not_break_the_excerpt():
    turns = make_turns(("S1", ""), ("S2", "今年的預算大約增加了兩成左右"), ("S1", "請問今年的預算編列有沒有增加"))
    outcome = dp.WindowOutcome(
        Allocation(Window(0, turns), 1, 3), (), 2, 0,
        dropped=(DroppedPoint("bad_quote", "找不到", "這一句話在逐字稿裡沒有", "寫的字", 0),),
        dropped_by_code=(("bad_quote", 1),), attempts=1)

    text = text_of((outcome,))

    assert "[第 1 段 T0 bad_quote／absent]" in text and "     它引用的 T0 S1：「」" in text
