"""歸屬不可靠而失敗時，記在進度裡的診斷（drop_report.drop_diagnostics）。

要回答的問題：模型提出的重點被丟掉了這麼多，**每一段**丟掉的是什麼原因？quote 對不上，是因為接了兩輪、
來自其他聲音、改了幾個字，還是根本編造的？被丟掉的點，模型寫的 quote 和逐字稿真正的內容差在哪裡？
這份診斷是純文字、只讀：它不改變任何判斷，也不會讓原本的失敗變成別的錯誤。
"""
from __future__ import annotations

import re
from collections import Counter

import pytest

from slidebox.domain.entities import RawPoint, RawSlide
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


def run(points, turns=TURNS, speakers=SPEAKERS, budget=10**6):
    """用真的檢查跑一遍：每個窗每次問模型都得到同一份回應（一頁、這些點）。"""
    reply = (RawSlide("預算", tuple(points)),)
    allocations = tuple(Allocation(w, 1, 3) for w in chunk_turns(turns, speakers, budget))
    return dp.summarize_windows(FakeConversation(default=lambda call: reply), allocations, turns, speakers,
                                lambda frac, status: None, lambda: None, lambda: False)


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
    assert line.startswith("  第 1/1 段（T0–T4，問了 2 次）：提出 6、留下 2、丟掉 4：")
    assert "bad_quote 3" in line and "leading_pronoun_or_title 1" in line
    assert line.index("bad_quote 3") < line.index("leading_pronoun_or_title 1")     # 多的在前


def test_the_asks_are_left_out_when_the_outcome_does_not_know_them():
    outcome = real_outcomes(PRONOUN)[0]
    outcome = dp.WindowOutcome(outcome.allocation, outcome.slides, outcome.proposed, outcome.kept,
                               dropped=outcome.dropped, dropped_by_code=outcome.dropped_by_code)     # attempts 預設 0

    assert "  第 1/1 段（T0–T4）：提出 3、留下 2、丟掉 1：leading_pronoun_or_title 1" in text_of((outcome,))


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

    assert "  unknown 1：" in text and "[第 1 段 T999 bad_quote／unknown]" in text


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


def test_examples_are_numbered_and_the_biggest_group_comes_first():
    text = text_of(real_outcomes(PARTIAL, ABSENT, absent(1), PRONOUN))

    numbered = re.findall(r"^  (\d+)\. \[第 \d+ 段 T\S+ (\S+)\]$", text, re.MULTILINE)
    assert numbered == [("1", "bad_quote／absent"), ("2", "bad_quote／partial"),
                        ("3", "leading_pronoun_or_title"), ("4", "bad_quote／absent")]


def test_every_group_gets_an_example_before_any_group_gets_a_second_one():
    text = text_of(real_outcomes(PARTIAL, PRONOUN, *(absent(n) for n in range(6))))

    order = [label for _, label in re.findall(r"^  (\d+)\. \[第 \d+ 段 T\S+ (\S+)\]$", text, re.MULTILINE)]
    assert order == ["bad_quote／absent", "bad_quote／partial", "leading_pronoun_or_title", "bad_quote／absent"]


def test_there_are_at_most_eight_examples_and_at_most_two_per_group():
    many = [p for point in (PARTIAL, OTHER_VOICE, PRONOUN, FIRST_PERSON) for p in (point,) * 3]
    many += [absent(n) for n in range(3)]

    text = text_of(real_outcomes(*many))

    labels = example_labels(text)
    assert set(labels) == {"bad_quote／partial", "bad_quote／other_voice", "bad_quote／absent",
                           "leading_pronoun_or_title", "first_person"}
    assert sum(labels.values()) == report.MAX_EXAMPLES == 8
    assert max(labels.values()) == 2


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
    assert line.startswith("     逐字稿 T2 S1（相同約 81%）：「…") and line.endswith("…」")
    assert "預算大約要三千億比去年多了兩成" in line and len(line) < 140


def test_a_very_long_quote_is_clipped():
    point = RawPoint(0, "談論今年預算", quote="這是一句非常非常長的話" * 30)

    text = text_of(real_outcomes(point))

    quote_line = next(l for l in text.splitlines() if "模型的 quote：" in l)
    assert quote_line.endswith("…」") and len(quote_line) < 100


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


def test_a_text_that_is_still_too_long_without_any_example_is_cut_and_says_so(monkeypatch):
    monkeypatch.setattr(report, "MAX_CHARS", 200)

    text = text_of(real_outcomes(PARTIAL, OTHER_VOICE, ABSENT, PRONOUN))

    assert len(text) <= 200 and text.endswith(report.TRUNCATED_NOTE)
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


def test_the_same_input_always_gives_the_same_text():
    outcomes = real_outcomes(PARTIAL, OTHER_VOICE, ABSENT, PRONOUN)

    assert text_of(outcomes) == text_of(outcomes)


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


def test_a_turn_without_text_does_not_break_the_excerpt():
    turns = make_turns(("S1", ""), ("S2", "今年的預算大約增加了兩成左右"), ("S1", "請問今年的預算編列有沒有增加"))
    outcome = dp.WindowOutcome(
        Allocation(Window(0, turns), 1, 3), (), 2, 0,
        dropped=(DroppedPoint("bad_quote", "找不到", "這一句話在逐字稿裡沒有", "寫的字", 0),),
        dropped_by_code=(("bad_quote", 1),), attempts=1)

    text = text_of((outcome,))

    assert "[第 1 段 T0 bad_quote／absent]" in text and "     它引用的 T0 S1：「」" in text
