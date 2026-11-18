"""診斷的第二部分（drop_report 的「再算一次」）：被丟掉的點還有什麼可以看出來、換個做法會怎樣。

第一部分（test_drop_report）回答「每一段丟了什麼、為什麼對不上」。這裡回答真實影片 ReXUCiDCbl8 第四次失敗之後
還不知道的事：跨輪的 quote 是誰和誰拼的、問句是哪個字眼觸發的、每一段的 quote 對不上的原因、把同一位說話者相鄰的輪
併起來（D）或只取 quote 最長的一段逐字吻合（C）會救回幾個。全部只算、不判斷。
"""
from __future__ import annotations

import re
import time

import pytest

from slidebox.domain.entities import RawPoint
from slidebox.usecases import conversation as c
from slidebox.usecases import diarized_pipeline as dp
from slidebox.usecases import drop_analysis as analysis
from slidebox.usecases import drop_report as report

from .drop_support import (
    BODY,
    BOTH_POINT,
    BOTH_TURNS,
    C_POINT,
    C_TEXT,
    C_TURNS,
    CROSS_TURNS,
    D_POINT,
    D_TURNS,
    HEAD,
    NOISE_POINT,
    NOISE_TURNS,
    Q_LONG,
    Q_NEAR_POINT,
    Q_NEAR_TURNS,
    Q_SHORT_POINT,
    Q_SHORT_TURNS,
    Q_STRICT_POINT,
    Q_STRICT_TURNS,
    SPEAKERS,
    outcomes_for,
)
from .speaker_fixtures import make_turns

SECTION = "再算一次（只是計算，沒有改變任何判斷）："
INVENTED = RawPoint(1, C_TEXT, quote="這一句話完全是模型自己編出來的內容啊")             # 編造：C 與 D 都救不回來


def text_of(outcomes, quote_only=False) -> str:
    return report.drop_diagnostics(outcomes, SPEAKERS, quote_only)


def line_with(text: str, start: str) -> str:
    (line,) = [l for l in text.splitlines() if l.strip().startswith(start)]
    return line


def two_windows():
    """兩個一模一樣的窗各丟一個 bad_quote（C 會通過的那一種）。"""
    return (*outcomes_for(C_TURNS, C_POINT), *outcomes_for(C_TURNS, C_POINT))


# --- 每一段的 quote 對不上的原因 ---


def test_a_window_line_says_why_its_quotes_did_not_match():
    text = text_of(outcomes_for(C_TURNS, C_POINT, INVENTED))

    line = line_with(text, "第 1/1 段")
    assert "bad_quote 2（partial 1、absent 1）" in line


def test_the_kinds_on_a_window_line_are_only_for_the_quotes_of_that_window():
    text = text_of((*outcomes_for(C_TURNS, C_POINT), *outcomes_for(C_TURNS, INVENTED)))

    assert "bad_quote 1（partial 1）" in line_with(text, "第 1/2 段")
    assert "bad_quote 1（absent 1）" in line_with(text, "第 2/2 段")


def test_a_window_line_without_a_dropped_quote_has_nothing_in_brackets():
    text = text_of(outcomes_for(C_TURNS, RawPoint(1, "他認為民主法治很重要", quote=BODY)))

    assert "（" not in line_with(text, "第 1/1 段").split("：", 1)[1]


def test_only_bad_quote_gets_the_kinds_in_brackets_when_the_window_dropped_other_things_too():
    """攔的 bug：括號跟在每一個原因碼後面：「first_person 1（partial 1）」讀起來像是第一人稱的點是 partial。"""
    text = text_of(outcomes_for(C_TURNS, C_POINT, RawPoint(1, "我認為民主法治很重要", quote=BODY)))

    line = line_with(text, "第 1/1 段")
    assert "bad_quote 1（partial 1）、first_person 1" in line
    assert "first_person 1（" not in line and "slide_too_small 0（" not in line


# --- 診斷的開頭與結尾 ---


def test_the_text_ends_with_a_line_that_says_it_is_complete():
    """攔的 bug：貼回來的診斷少了後半（終端機捲動、被切掉），讀的人不知道少了。結尾這一行在，才是完整的。"""
    text = text_of(outcomes_for(C_TURNS, C_POINT))

    assert text.endswith("\n" + report.END_LINE)
    assert text.splitlines()[0].startswith("診斷：被丟掉的重點")


def test_the_end_line_is_still_there_when_the_text_has_to_be_cut(monkeypatch):
    monkeypatch.setattr(report, "MAX_CHARS", 300)

    text = text_of(outcomes_for(C_TURNS, C_POINT, INVENTED))

    assert len(text) <= 300 and report.TRUNCATED_NOTE in text and text.endswith(report.END_LINE)


# --- 跨輪的 quote 是誰和誰拼的 ---


def spliced_outcomes():
    """三種跨輪：同一位說話者的相鄰輪、中間夾著一個字的其他聲音、兩位說話者的話。"""
    return (*outcomes_for(D_TURNS, D_POINT), *outcomes_for(NOISE_TURNS, NOISE_POINT), *outcomes_for(CROSS_TURNS, D_POINT))


def test_the_speakers_of_every_spliced_quote_are_counted():
    text = text_of(spliced_outcomes())

    assert ("  跨輪的 quote（across_turns 共 3 個）拼自：同一位說話者的相鄰輪 1、"
            "同一位說話者、中間夾著很短的其他聲音 1（夾著的聲音最多 1 個字）、不同說話者的話 1") in text


def test_the_most_common_kind_of_splice_comes_first():
    text = text_of((*spliced_outcomes(), *outcomes_for(CROSS_TURNS, D_POINT)))

    line = line_with(text, "跨輪的 quote")
    assert line.index("不同說話者的話 2") < line.index("同一位說話者的相鄰輪 1")


def test_no_line_about_splices_when_no_quote_was_spliced():
    assert "跨輪的 quote" not in text_of(outcomes_for(C_TURNS, C_POINT))


def noisy_turns(noise: str):
    """NOISE_TURNS 的樣子，夾在同一位說話者中間的其他聲音換成這一段話。"""
    return make_turns(("S2", "請問您怎麼看今年交通建設的規劃"), ("S1", "優先處理淡水捷運延伸線預計"), ("S3", noise),
                      ("S1", "三年內完成規劃並且開始動工興建"), ("S2", "好的謝謝您的說明"))


def interrupted_outcomes(*lengths: int):
    """同一位說話者的話被一整句其他聲音（每個長度一份）隔開，quote 跨過它：每個長度一個跨輪的 quote。"""
    return tuple(o for n in lengths for o in outcomes_for(noisy_turns("嗯" * n), NOISE_POINT))


def test_the_longest_of_the_short_noises_is_what_the_line_says():
    """攔的 bug：說的是最短的那個：「最多 1 個字」，其實有一個夾了 3 個字，之後要把門檻放在哪裡就看錯了。"""
    text = text_of((*outcomes_for(NOISE_TURNS, NOISE_POINT), *outcomes_for(noisy_turns("嗯嗯啊"), NOISE_POINT)))

    assert "同一位說話者、中間夾著很短的其他聲音 2（夾著的聲音最多 3 個字）" in text


def test_every_length_of_an_interrupting_voice_is_listed_smallest_first():
    text = text_of(interrupted_outcomes(9, 7, 12))

    assert "同一位說話者、中間夾著一整句其他聲音的話 3（夾著的聲音 7、9、12 個字）" in text


def test_the_list_of_interruption_lengths_ends_with_an_ellipsis_only_when_it_was_cut(monkeypatch):
    monkeypatch.setattr(report, "MAX_GAP_LENGTHS", 2)

    assert "（夾著的聲音 7、9 個字）" in text_of(interrupted_outcomes(9, 7))             # 剛好等於上限：沒有省略
    assert "（夾著的聲音 7、9… 個字）" in text_of(interrupted_outcomes(9, 12, 7))         # 多了一個：省略，留下最小的


# --- 問句是哪個字眼觸發的 ---


def test_every_question_that_was_taken_for_a_statement_says_which_word_did_it():
    text = text_of((*outcomes_for(Q_SHORT_TURNS, Q_SHORT_POINT), *outcomes_for(Q_NEAR_TURNS, Q_NEAR_POINT)))

    assert ("  問句被丟（question_stated_as_fact 共 2 個）：觸發的字眼 呢 1、為什麼 1；"
            "條件 很長又沒有標點的話，字眼附近有重點的內容 1、短子句裡有字眼 1") in text


def test_a_real_question_mark_is_called_a_question_mark():
    turns = make_turns(("S2", "請問您怎麼看今年的預算"), ("S1", "那明年的預算打算怎麼編列？"), ("S2", "好的謝謝您的說明"))

    text = text_of(outcomes_for(turns, RawPoint(1, "明年的預算打算要編列", quote="那明年的預算打算怎麼編列")))

    assert "觸發的字眼 問號 1；條件 子句結尾有問號 1" in text


def test_the_triggers_say_the_number_was_just_said_when_that_is_what_made_the_check_strict():
    text = text_of(outcomes_for(Q_STRICT_TURNS, Q_STRICT_POINT))

    assert "觸發的字眼 嗎 1；條件 重點的數字剛被別人說過（不看距離） 1" in line_with(text, "問句被丟")


def test_the_trigger_words_are_listed_most_common_first():
    outcomes = (*outcomes_for(Q_NEAR_TURNS, Q_NEAR_POINT), *outcomes_for(Q_SHORT_TURNS, Q_SHORT_POINT, Q_SHORT_POINT, Q_SHORT_POINT))

    line = line_with(text_of(outcomes), "問句被丟")

    assert "（question_stated_as_fact 共 4 個）：觸發的字眼 呢 3、為什麼 1；" in line       # 先出現的是為什麼，但呢比較多


def test_the_conditions_that_are_more_likely_a_mistake_come_first_when_equally_common():
    """要找的是檢查有沒有誤判：數字剛被說過、很長又沒有標點的話這兩種最可疑，真的問號最不可疑。"""
    turns = make_turns(("S2", "請問您怎麼看今年的預算"), ("S1", "那明年的預算打算怎麼編列？"), ("S2", "好的謝謝您的說明"))
    outcomes = (*outcomes_for(turns, RawPoint(1, "明年的預算打算要編列", quote="那明年的預算打算怎麼編列")),
                *outcomes_for(Q_SHORT_TURNS, Q_SHORT_POINT), *outcomes_for(Q_NEAR_TURNS, Q_NEAR_POINT),
                *outcomes_for(Q_STRICT_TURNS, Q_STRICT_POINT))

    line = line_with(text_of(outcomes), "問句被丟")

    conditions = ["重點的數字剛被別人說過（不看距離）", "很長又沒有標點的話，字眼附近有重點的內容", "短子句裡有字眼", "子句結尾有問號"]
    positions = [line.index(word) for word in conditions]
    assert positions == sorted(positions)


def test_no_line_about_questions_when_none_was_taken_for_a_statement():
    assert "問句被丟" not in text_of(outcomes_for(C_TURNS, C_POINT))


def test_a_question_whose_quote_cannot_be_found_is_counted_as_having_no_trigger_found():
    """真的檢查是對著 quote 所在的那一輪做的：找不到 quote 就沒有那一輪可以對照，診斷要老實說沒找到，
    不能硬猜一輪、也不能讓整行壞掉。"""
    outcome = outcomes_for(Q_SHORT_TURNS, Q_SHORT_POINT)[0]
    stray = c.DroppedPoint(c.QUESTION_STATED_AS_FACT, "問句", "這一句話完全是模型自己編出來的內容啊", Q_SHORT_POINT.text, 1)
    outcome = dp.WindowOutcome(outcome.allocation, outcome.slides, 3, 2, dropped=(stray,),
                               dropped_by_code=((c.QUESTION_STATED_AS_FACT, 1),), attempts=1)

    text = text_of((outcome,))

    assert ("  問句被丟（question_stated_as_fact 共 1 個）：1 個沒找到觸發的字眼（這裡的算法和檢查對不上）"
            in text.splitlines())


def test_a_word_or_condition_that_counts_twice_in_one_point_is_counted_once():
    """同一個點的同一輪裡，「嗎」一次在有問號的子句、一次在沒有問號的子句：字眼「嗎」算一個點，條件各算一個點。"""
    turns = make_turns(("S2", "請問您怎麼看這件事"), ("S1", "你覺得這樣可以嗎？你覺得這樣可以嗎"), ("S2", "好的謝謝您的說明"))
    point = RawPoint(1, "覺得這樣可以", quote="你覺得這樣可以嗎？你覺得這樣可以嗎")

    text = text_of(outcomes_for(turns, point))

    assert "觸發的字眼 嗎 1、問號 1；條件 短子句裡有字眼 1、子句結尾有問號 1" in line_with(text, "問句被丟")


def test_an_example_of_a_question_names_the_word_and_how_it_counted():
    text = text_of(outcomes_for(Q_SHORT_TURNS, Q_SHORT_POINT))

    assert "     觸發的字眼：呢（短子句裡有字眼）" in text


def test_an_example_lists_every_word_of_the_clause_grouped_by_how_it_counted():
    turns = make_turns(("S2", "請問您怎麼看今年的預算"), ("S1", "請問今年的預算有沒有增加？"), ("S2", "好的謝謝您的說明"))

    text = text_of(outcomes_for(turns, RawPoint(1, "今年的預算增加", quote="請問今年的預算有沒有增加")))

    assert "     觸發的字眼：請問、有沒有、問號（子句結尾有問號）" in text


def test_an_example_of_something_other_than_a_question_has_no_trigger_line():
    assert "觸發的字眼：" not in text_of(outcomes_for(C_TURNS, C_POINT))


# --- 換個做法會怎樣（C、D）---


def whatif_header(n: int, limit: int | None = None) -> str:
    cut = f"，只重跑前 {limit} 個" if limit is not None else ""
    return (f"  假設的做法（把 quote 對不上的 {n} 個用真的檢查重跑，只算每個點自己的檢查：不含頁數與重複的規則，"
            f"原本留下的點沒有重算{cut}）：")


def test_the_two_alternatives_say_how_many_would_pass():
    text = text_of((*outcomes_for(C_TURNS, C_POINT), *outcomes_for(D_TURNS, D_POINT), *outcomes_for(CROSS_TURNS, D_POINT)))

    lines = text.splitlines()
    assert whatif_header(3) in lines
    assert (f"    C 只取 quote 裡最長的一段逐字吻合（至少 {analysis.LONGEST_SEGMENT_MIN_CHARS} 個字、只有一輪有這麼長的一段、"
            f"那一輪是可以引用的說話者）：能算 1 個，會通過 1 個（原本是 partial 1）") in lines
    assert (f"    D 把同一位說話者相鄰的輪併成一輪（中間夾著不超過 {analysis.JOIN_GAP_CHARS} 個字的其他聲音不算）：能算 1 個，"
            f"會通過 1 個（原本是 across_turns 1）") in lines
    assert "    兩種都會通過 0 個；至少一種會通過 2 個" in lines


def test_a_point_that_would_still_be_dropped_says_for_what():
    text = text_of(outcomes_for(C_TURNS, RawPoint(1, "指出三成的國家需要尊重私有權", quote=HEAD + BODY), INVENTED))

    c_line = line_with(text, "C 只取")
    assert "能算 1 個，會通過 0 個；沒通過 1 個（number_mismatch 1）" in c_line


def test_both_alternatives_can_pass_the_same_point():
    text = text_of(outcomes_for(BOTH_TURNS, BOTH_POINT))

    assert "    兩種都會通過 1 個；至少一種會通過 1 個" in text.splitlines()


def test_the_what_if_is_left_out_when_no_quote_was_dropped():
    text = text_of(outcomes_for(C_TURNS, RawPoint(1, "他認為民主法治很重要", quote=BODY)))

    assert "假設的做法" not in text


def test_a_point_whose_cited_turn_is_not_in_the_window_is_not_counted():
    outcome = outcomes_for(C_TURNS, C_POINT)[0]
    stray = c.DroppedPoint("bad_quote", "quote 核對不過", C_POINT.quote, C_TEXT, 999)
    outcome = dp.WindowOutcome(outcome.allocation, outcome.slides, 3, 2, dropped=(stray,),
                               dropped_by_code=(("bad_quote", 1),), attempts=1)

    text = text_of((outcome,))

    assert "假設的做法" not in text and "unknown 1" in text


def test_a_quote_that_was_found_but_cannot_be_shown_has_nothing_to_rerun():
    """安全模式裡 quote 找得到、只是不能直接顯示（located）：換一種做法定位也一樣，不能算進「能算 n 個」。"""
    turns = make_turns(("S2", "請問您怎麼看今年的預算"), ("S1", "他們今年的預算大約增加了兩成左右來做這件事情"), ("S2", "好的謝謝"))
    point = RawPoint(1, "預算今年大約增加了兩成左右", quote="他們今年的預算大約增加了兩成左右")

    text = text_of(outcomes_for(turns, point, quote_only=True), quote_only=True)

    assert "bad_quote 1（located 1）" in line_with(text, "第 1/1 段")
    assert "假設的做法" not in text and "假設重跑" not in text


def test_the_alternatives_are_run_in_the_mode_the_run_was_in():
    turns = make_turns(("S2", "請問您怎麼看今年的預算"), ("S1", "他們今年的預算大約增加了兩成左右來做這件事情"), ("S2", "好的謝謝"))
    point = RawPoint(1, "預算今年大約增加了兩成左右", quote="我覺得啊其實他們今年的預算大約增加了兩成左右")

    normal = line_with(text_of(outcomes_for(turns, point)), "C 只取")
    safe = line_with(text_of(outcomes_for(turns, point, quote_only=True), quote_only=True), "C 只取")

    assert "會通過 1 個" in normal and "會通過 0 個；沒通過 1 個（bad_quote 1）" in safe       # 原話裡有「他們」，安全模式不能直接顯示


def test_only_the_first_points_are_rerun_when_there_are_very_many(monkeypatch):
    monkeypatch.setattr(report, "MAX_RERUNS", 1)

    text = text_of(two_windows())

    assert whatif_header(2, limit=1) in text.splitlines()
    assert "能算 1 個，會通過 1 個" in line_with(text, "C 只取")


# --- 例子裡的假設 ---


def test_an_example_says_whether_the_alternatives_would_pass_it():
    text = text_of(outcomes_for(D_TURNS, D_POINT))

    assert "     假設重跑：D 會通過（併 T1 S1＋T2 S1）" in text


def test_an_example_the_alternatives_cannot_pass_says_for_what():
    text = text_of(outcomes_for(C_TURNS, RawPoint(1, "指出三成的國家需要尊重私有權", quote=HEAD + BODY)))

    assert "     假設重跑：C 仍被丟掉（number_mismatch）" in text


def test_an_example_neither_alternative_can_work_on_has_no_line_about_it():
    text = text_of(outcomes_for(CROSS_TURNS, D_POINT))

    assert "假設重跑" not in text


def test_an_example_that_both_pass_names_both():
    text = text_of(outcomes_for(BOTH_TURNS, BOTH_POINT))

    assert "     假設重跑：C 會通過；D 會通過（併 T1 S1＋T2 S1）" in text


def test_an_example_whose_joined_turns_stepped_over_a_noise_says_so():
    """C 在這裡不行：重點寫了「三年內」，只取前半（T1）的那一段，數字就不在被引用的那段話裡；D 併起來才對。"""
    text = text_of(outcomes_for(NOISE_TURNS, NOISE_POINT))

    assert "     假設重跑：C 仍被丟掉（number_mismatch）；D 會通過（併 T1 S1＋T3 S1，略過 T2 X）" in text


# --- 診斷自己出錯不能蓋掉其他的 ---


def test_when_one_alternative_breaks_the_rest_of_the_diagnostic_is_still_there(monkeypatch):
    def boom(ctx, point):
        raise RuntimeError("壞掉了")

    monkeypatch.setattr(analysis, "try_longest_segment", boom)

    text = text_of(outcomes_for(D_TURNS, D_POINT))

    assert "    C 只取 quote 裡最長的一段逐字吻合" in text and "沒能算出來（RuntimeError）" in line_with(text, "C 只取")
    assert "會通過 1 個" in line_with(text, "D 把同一位")
    assert "quote 對不上的原因" in text and text.endswith(report.END_LINE)


def test_an_alternative_that_broke_is_not_tried_again_for_every_other_point(monkeypatch):
    """攔的 bug：每個點都再錯一次：一百多個點、每次都是一整輪完整的檢查加上例外，診斷只是白白變慢。"""
    calls = []

    def boom(ctx, point):
        calls.append(point)
        raise RuntimeError("壞掉了")

    monkeypatch.setattr(analysis, "try_longest_segment", boom)

    text = text_of(two_windows())

    assert len(calls) == 1
    assert "沒能算出來（RuntimeError）" in line_with(text, "C 只取")


def test_when_the_windows_cannot_be_prepared_every_alternative_says_so(monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("壞掉了")

    monkeypatch.setattr(analysis, "window_contexts", boom)

    text = text_of(spliced_outcomes())

    assert "沒能算出來（ValueError）" in line_with(text, "C 只取") and "沒能算出來（ValueError）" in line_with(text, "D 把同一位")
    assert "跨輪的 quote（across_turns 共 3 個）" in text                # 不需要窗的部分照常


def test_when_the_splice_pattern_breaks_the_line_says_so_and_the_rest_is_there(monkeypatch):
    def boom(run, citable):
        raise KeyError("壞掉了")

    monkeypatch.setattr(analysis, "splice_pattern", boom)

    text = text_of(spliced_outcomes())

    assert "跨輪的 quote（across_turns 共 3 個）：沒能算出來（KeyError）" in text
    assert whatif_header(3) in text.splitlines()


def test_when_the_question_triggers_break_the_line_says_so(monkeypatch):
    def boom(*args, **kwargs):
        raise IndexError("壞掉了")

    monkeypatch.setattr(analysis, "question_triggers", boom)

    text = text_of(outcomes_for(Q_SHORT_TURNS, Q_SHORT_POINT))

    assert "問句被丟（question_stated_as_fact 共 1 個）：沒能算出來（IndexError）" in text
    assert "觸發的字眼" not in text                                         # 例子裡也沒有：沒算出來就不寫


# --- 不依賴雜湊種子、不拖慢 ---


def test_the_new_lines_are_stable_text():
    outcomes = (*spliced_outcomes(), *outcomes_for(Q_SHORT_TURNS, Q_SHORT_POINT), *outcomes_for(C_TURNS, C_POINT))

    assert text_of(outcomes) == text_of(outcomes)
    assert SECTION in text_of(outcomes).splitlines()


def test_a_big_run_is_still_fast_and_within_the_limit():
    outcome = outcomes_for(D_TURNS, D_POINT, *(RawPoint(1, C_TEXT, quote=f"這是模型編造的第{n}句話完全不在逐字稿裡啊啊") for n in range(30)))[0]
    many = tuple(outcome for _ in range(120))

    start = time.perf_counter()
    text = text_of(many)

    assert time.perf_counter() - start < 20 and len(text) <= report.MAX_CHARS and text.endswith(report.END_LINE)


def test_the_section_title_is_there_whenever_any_line_in_it_is():
    for outcomes in (spliced_outcomes(), outcomes_for(Q_SHORT_TURNS, Q_SHORT_POINT), outcomes_for(C_TURNS, C_POINT)):
        lines = text_of(outcomes).splitlines()
        assert lines.count(SECTION) == 1
        assert lines.index(SECTION) < next(i for i, l in enumerate(lines) if l.startswith("例子"))


def test_the_title_is_left_out_with_the_lines():
    text = text_of(outcomes_for(C_TURNS, RawPoint(1, "他認為民主法治很重要", quote=BODY)))

    assert SECTION not in text


@pytest.mark.parametrize("quote", ["", "對", "三個月", "，。"])
def test_unusable_quotes_do_not_break_the_new_lines(quote):
    text = text_of(outcomes_for(C_TURNS, RawPoint(1, C_TEXT, quote=quote)))

    assert "[第 1 段 T1 bad_quote／" in text and text.endswith(report.END_LINE)


def test_the_long_turn_of_the_near_example_is_really_unpunctuated():
    assert not re.search(r"[，。？！；：,.!?;:]", Q_LONG)
