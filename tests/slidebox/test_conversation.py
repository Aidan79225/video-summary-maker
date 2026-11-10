"""多人對談的摘要邏輯：切窗、頁數分配、重點檢查、排版。

模型與網路一律不碰：模型的回傳是手寫的 RawSlide。重點的每一道檢查另有「拿掉就會失敗」的
變異測試（test_conversation_mutations.py）。
"""
from __future__ import annotations

import random
import re

import pytest

from slidebox.domain.entities import Point, RawPoint, RawSlide, Turn
from slidebox.usecases import conversation
from slidebox.usecases.conversation import (
    Allocation,
    Window,
    allocate_slides,
    chunk_turns,
    format_window,
    parse_chunk_chars,
    render_placeholders,
    render_slide,
    validate_points,
)

from .conversation_support import (
    GOOD,
    SPEAKERS,
    TURNS,
    TWO,
    WHOLE,
    at,
    problems_of,
    run,
    with_anchors,
)
from .speaker_fixtures import make_speakers, make_turns


# --- 切窗 ---


def test_a_short_conversation_fits_in_one_window():
    windows = chunk_turns(TURNS, SPEAKERS)

    assert len(windows) == 1 and windows[0].turns == TURNS
    assert (windows[0].first_turn, windows[0].last_turn) == (0, len(TURNS) - 1)


def test_every_turn_is_in_exactly_one_window_in_order_whatever_the_budget():
    for budget in (1, 30, 80, 200, 500, 6000):
        windows = chunk_turns(TURNS, SPEAKERS, budget)

        assert [t.index for w in windows for t in w.turns] == [t.index for t in TURNS]
        assert [w.index for w in windows] == list(range(len(windows)))
        assert all(w.turns for w in windows)


def test_a_window_stays_inside_the_budget_unless_one_turn_alone_is_longer():
    for budget in (60, 120, 300, 900):
        for window in chunk_turns(TURNS, SPEAKERS, budget):
            assert len(format_window(window, SPEAKERS)) <= budget or len(window.turns) == 1


def test_a_line_is_never_cut_and_always_starts_with_its_turn_number_and_a_tag():
    pattern = re.compile(r"^\[T(\d+)\] (S\d+|X)：")
    for budget in (40, 100, 250, 6000):
        for window in chunk_turns(TURNS, SPEAKERS, budget):
            lines = format_window(window, SPEAKERS).split("\n")
            assert len(lines) == len(window.turns)
            for turn, line in zip(window.turns, lines):
                match = pattern.match(line)
                assert match and int(match.group(1)) == turn.index
                assert line.endswith(" ".join(turn.text.split()))


def test_formatted_lines_name_participants_and_hide_everyone_else_behind_x():
    lines = format_window(WHOLE, SPEAKERS).split("\n")

    assert lines[0] == "[T0] S1：今天我們來談少子化，先請教您怎麼看目前的補助政策？"
    assert lines[at("我想補充")].startswith("[T13] X：")              # 其他聲音
    assert lines[at("題，所以")].startswith("[T15] X：")               # 無法判斷
    assert "S3" not in "\n".join(lines) and "S0" not in "\n".join(lines)


def test_a_turn_longer_than_the_budget_gets_a_window_of_its_own():
    turns = make_turns(("S1", "短"), ("S2", "很長" * 100), ("S1", "短"))

    windows = chunk_turns(turns, make_speakers("participant", "participant"), 50)

    assert [[t.index for t in w.turns] for w in windows] == [[0], [1], [2]]


def test_text_with_line_breaks_stays_on_one_line():
    turns = make_turns(("S1", "上面一行\n下面一行"))

    assert format_window(chunk_turns(turns, make_speakers("participant"))[0],
                         make_speakers("participant")) == "[T0] S1：上面一行 下面一行"


def test_no_turns_means_no_windows():
    assert chunk_turns([], SPEAKERS) == ()


@pytest.mark.parametrize("raw, expected", [
    (None, 6000), ("", 6000), ("abc", 6000), ("999", 6000), ("20001", 6000), ("6000.5", 6000),
    ("1000", 1000), (" 8000 ", 8000), ("20000", 20000), (7000, 7000),
])
def test_chunk_size_from_the_environment_falls_back_to_the_default_when_unusable(raw, expected):
    assert parse_chunk_chars(raw) == expected


# --- 頁數分配 ---


def timed(*seconds: float) -> list[Window]:
    return [Window(i, (Turn(i, 0.0, sec, "S1", "文字"),)) for i, sec in enumerate(seconds)]


def test_a_single_window_gets_the_whole_range():
    windows = timed(120.0)

    assert allocate_slides(windows, 5, 12) == (Allocation(windows[0], 5, 12),)


def test_two_equal_windows_split_the_range_evenly():
    out = allocate_slides(timed(60.0, 60.0), 4, 10)

    assert [(a.min_slides, a.max_slides) for a in out] == [(2, 5), (2, 5)]


def test_a_longer_window_gets_more_slides():
    out = allocate_slides(timed(180.0, 60.0), 4, 12)

    assert out[0].max_slides > out[1].max_slides and out[0].min_slides >= out[1].min_slides
    assert sum(a.max_slides for a in out) == 12


def test_a_window_too_small_to_earn_one_slide_is_merged_into_its_neighbour():
    windows = timed(100.0, 1.0)

    out = allocate_slides(windows, 3, 10)

    assert len(out) == 1
    assert out[0].window.turns == windows[0].turns + windows[1].turns
    assert (out[0].min_slides, out[0].max_slides) == (3, 10)


def test_when_min_equals_max_every_window_gets_exactly_its_share():
    out = allocate_slides(timed(30.0, 70.0), 7, 7)

    assert [(a.min_slides, a.max_slides) for a in out] == [(2, 2), (5, 5)]


def test_windows_with_no_speech_time_share_by_turn_count():
    windows = [Window(0, (Turn(0, 0.0, 0.0, "S1", "甲"), Turn(1, 0.0, 0.0, "S1", "乙"), Turn(2, 0.0, 0.0, "S1", "丙"))),
               Window(1, (Turn(3, 0.0, 0.0, "S1", "丁"),))]

    out = allocate_slides(windows, 4, 8)

    assert out[0].max_slides > out[1].max_slides and sum(a.max_slides for a in out) == 8


def test_no_windows_means_no_allocation():
    assert allocate_slides([], 3, 6) == ()


def test_a_minimum_above_the_maximum_is_treated_as_exactly_the_minimum():
    out = allocate_slides(timed(10.0), 5, 3)

    assert (out[0].min_slides, out[0].max_slides) == (5, 5)


@pytest.mark.parametrize("seed", range(80))
def test_allocation_keeps_every_window_and_the_total_inside_the_requested_range(seed):
    rng = random.Random(seed)
    windows = timed(*(rng.choice([0.0, 0.5, 5.0, 40.0, 300.0]) for _ in range(rng.randint(1, 12))))
    low = rng.randint(1, 10)
    high = low + rng.randint(0, 30)

    out = allocate_slides(windows, low, high)

    assert [t.index for a in out for t in a.window.turns] == [t.index for w in windows for t in w.turns]
    assert [a.window.index for a in out] == list(range(len(out)))
    assert all(1 <= a.min_slides <= a.max_slides for a in out)
    assert sum(a.max_slides for a in out) == high
    assert low <= sum(a.min_slides for a in out) <= high


# --- 重點檢查：通過的情況 ---


def test_good_points_keep_the_speaker_of_the_cited_turn_and_quote_it_as_evidence():
    result = validate_points([RawSlide("補助與托育", GOOD)], TURNS, SPEAKERS, WHOLE)

    assert result.problems == () and (result.proposed, result.kept) == (2, 2)
    first, second = result.slides[0].points
    assert (first.speaker, first.turn, first.start) == ("S1", 0, TURNS[0].start)
    assert first.evidence == "先請教您怎麼看目前的補助政策"
    assert (second.speaker, second.turn) == ("S2", 1)
    assert second.evidence == "現金補助有幫助，但不能只靠現金，托育資源更重要"


def test_points_are_sorted_by_the_turn_they_rest_on_and_text_is_tidied():
    late = RawPoint(at("預算大約"), "  預算大約要三千億，\n比去年多了兩成。 ")

    extra, result = run(late)

    assert [p.turn for p in result.slides[0].points] == [0, 1, at("預算大約")]
    assert extra[0].text == "預算大約要三千億， 比去年多了兩成"


def test_a_slide_with_fewer_than_two_surviving_points_is_dropped_whole():
    only_one = RawSlide("只有一點", (GOOD[0], RawPoint(at("今天我們"), "他說了什麼")))

    result = validate_points([only_one, RawSlide("兩點", GOOD)], TURNS, SPEAKERS, WHOLE)

    assert [s.title for s in result.slides] == ["兩點"]
    assert (result.proposed, result.kept) == (4, 2)
    assert "第 1 頁只剩 1 個通過檢查的重點" in problems_of(result) and "整頁不用" in problems_of(result)


def test_a_title_with_a_label_or_a_pronoun_is_replaced_by_a_neutral_one():
    for bad in ("主持人與來賓的對談", "他怎麼看補助", "我們的看法", "", "字" * 61, "[[S1]]的看法"):
        _, result = run(title=bad)

        assert result.slides[0].title == "00:05 起的討論", bad      # 第一個重點的時間，不帶任何內容
        assert "中性標題" in problems_of(result)


def test_a_clean_title_is_kept_with_its_spaces_normalised():
    _, result = run(title="  補助 與托育  ")

    assert result.slides[0].title == "補助 與托育" and result.problems == ()


def test_the_same_point_twice_on_a_page_is_kept_once():
    extra, result = run(GOOD[0])

    assert extra == [] and result.kept == 2
    assert "重複" in problems_of(result)


def test_a_slide_list_with_nothing_in_it_is_reported():
    result = validate_points([], TURNS, SPEAKERS, WHOLE)

    assert (result.slides, result.proposed, result.kept) == ((), 0, 0)
    assert result.problems


def test_the_problem_list_is_capped_for_the_retry_hint():
    bad = [RawPoint(0, "他說了很多事情") for _ in range(30)]

    result = validate_points([RawSlide("很多問題", tuple(bad))], TURNS, SPEAKERS, WHOLE)

    assert len(result.problems) == 13 and result.problems[-1].startswith("（另有 ")


def test_odd_values_from_a_sloppy_model_are_dropped_not_raised():
    odd = [RawPoint(True, "詢問目前補助政策的看法"), RawPoint("0", "詢問目前補助政策的看法"),
           RawPoint(None, "詢問目前補助政策的看法"), RawPoint(10 ** 9, "詢問目前補助政策的看法"),
           RawPoint(-3, "詢問目前補助政策的看法"), RawPoint(0, None), RawPoint(0, ""), RawPoint(0, "   "),
           RawPoint(0, "字" * 5000), RawPoint(0, "詢問目前補助政策的看法", context=("x", None, True, 9999))]

    extra, result = run(*odd)

    # 最後一個其實是好的重點，但和 GOOD[0] 一模一樣，被當成重複丟掉；其餘全部因為壞值被丟掉
    assert extra == [] and (result.proposed, result.kept) == (12, 2)


# --- 重點檢查：引用的 turn ---


def test_a_turn_outside_the_window_cannot_be_cited():
    window = Window(0, TURNS[:9])

    extra, result = run(RawPoint(at("預算大約"), "預算大約要三千億"), window=window)

    assert extra == [] and "不在這一段範圍" in problems_of(result)


def test_other_voices_and_undecidable_turns_cannot_be_cited():
    for prefix in ("我想補充", "題，所以"):
        extra, result = run(RawPoint(at(prefix), "補充社區的托育名額真的不夠"))

        assert extra == [], prefix
        assert "不能引用" in problems_of(result)


def test_a_wrong_cited_turn_next_door_is_corrected_to_the_turn_that_really_says_it():
    extra, result = run(RawPoint(at("謝謝邱委員"), "預算大約要三千億，比去年多了兩成"))

    assert [(p.speaker, p.turn) for p in extra] == [("S2", at("預算大約"))]
    assert f"已改引內容符合的 T{at('預算大約')}" in problems_of(result)


def test_a_cited_turn_too_far_from_the_real_one_is_dropped():
    extra, result = run(RawPoint(at("今天我們"), "預算大約要三千億，比去年多了兩成"))

    assert extra == [] and "找不到依據" in problems_of(result)


def test_turns_of_two_speakers_that_fit_equally_well_cannot_be_told_apart():
    turns, good = with_anchors(("S1", "托育名額不夠，家長排不到。"), ("S2", "托育名額不夠，家長排不到。"),
                               ("S1", "謝謝大家今天的參與。"))

    extra, result = run(RawPoint(2, "托育名額不夠，家長排不到"), turns=turns, speakers=TWO, good=good)

    assert extra == [] and "無法確定是誰說的" in problems_of(result)


def test_a_cited_turn_that_ties_with_its_confirmation_still_counts_for_the_speaker_who_said_it():
    # 受訪者先說，主持人複述，受訪者再確認；引用受訪者確認的那段：他確實說了
    turns, good = with_anchors(("S2", "名額不夠，很多家長排不到，排了兩年也沒有消息。"),
                               ("S1", "您說名額不夠，家長排不到。"),
                               ("S2", "對，名額不夠，家長排不到。"))

    extra, _ = run(RawPoint(2, "名額不夠，家長排不到"), turns=turns, speakers=TWO, good=good)

    assert [(p.speaker, p.turn) for p in extra] == [("S2", 2)]


# --- 重點檢查：文字 ---


@pytest.mark.parametrize("text", [
    "[[S1]]：詢問目前補助政策的看法", "詢問目前補助政策的看法 [T3]", "S2 現金補助有幫助", "詢問目前補助政策的看法]]",
])
def test_markers_that_belong_to_the_program_are_rejected(text):
    extra, result = run(RawPoint(0, text))

    assert extra == [] and "標記" in problems_of(result)


@pytest.mark.parametrize("text, word", [
    ("主持人詢問目前補助政策的看法", "主持人"), ("詢問來賓目前補助政策的看法", "來賓"),
    ("講者詢問目前補助政策的看法", "講者"), ("詢問目前補助政策的看法，發言者提出問題", "發言者"),
])
def test_role_and_label_words_are_rejected(text, word):
    extra, result = run(RawPoint(0, text))

    assert extra == [] and f"文字裡有「{word}」" in problems_of(result)


@pytest.mark.parametrize("text, flagged", [
    ("他認為名額不夠，家長排不到", True), ("她表示名額不夠", True), ("他們指出預算要三千億", True),
    ("對方指出名額不夠，家長排不到", True), ("邱委員指出預算大約要三千億", True),
    ("委員指出預算大約要三千億", True), ("經濟部長表示預算大約要三千億", True),
    ("陳老師認為名額不夠", True), ("詢問預算怎麼編，他說三千億", True), ("說明預算；她認為不夠", True),
    ("地區長期缺乏托育資源", False), ("委員會決議增加名額", False), ("城市長期缺乏托育資源", False),
    ("批評其他縣市做得不夠", False), ("照顧他人的家庭", False), ("他山之石可以參考", False),
    ("詢問預算怎麼編", False), ("指出名額不夠，並提到他人的經驗", False),
])
def test_pronouns_names_and_titles_as_the_subject_are_rejected_but_ordinary_words_are_not(text, flagged):
    assert (conversation._leading_problem(text) is not None) is flagged


@pytest.mark.parametrize("text, flagged", [
    ("我們認為預算大約要三千億", True), ("我覺得名額不夠", True), ("本席認為名額不夠", True),
    ("咱們的預算要三千億", True), ("自我要求預算", False), ("我國出生率下降", False),
    ("說明「我們社區就是這樣」", False),
])
def test_first_person_is_rejected_outside_quotes_only(text, flagged):
    assert (conversation._first_person_problem(text) is not None) is flagged


def test_label_words_and_first_person_inside_a_verified_quote_are_allowed():
    turns, good = with_anchors(("S1", "謝謝主持人邀請我今天來這裡，我想先談補助。"), ("S2", "歡迎你。"))

    extra, result = run(RawPoint(0, "致意「謝謝主持人邀請我今天來這裡」並談補助"),
                        turns=turns, speakers=TWO, good=good)

    assert [p.turn for p in extra] == [0], problems_of(result)


def test_text_too_short_or_too_long_is_rejected():
    extra, result = run(RawPoint(0, "補助"), RawPoint(0, "詢問" + "補助政策" * 40))

    assert extra == []
    assert "太短" in problems_of(result) and "超過 120" in problems_of(result)


# --- 重點檢查：內容要有依據 ---


def test_a_claim_that_the_conversation_never_said_is_rejected():
    extra, result = run(RawPoint(at("名額不夠"), "宣布將立法禁止外送平台"))

    assert extra == [] and "找不到依據" in problems_of(result)


def test_a_wrong_number_is_rejected_and_the_right_one_is_kept():
    right = RawPoint(at("預算大約"), "預算大約要三千億，比去年多了兩成")
    wrong = RawPoint(at("預算大約"), "預算大約要五千億，比去年多了兩成")
    other_unit = RawPoint(at("預算大約"), "預算大約要三千億，比去年多了三成")

    extra, result = run(right, wrong, other_unit)

    assert [p.text for p in extra] == ["預算大約要三千億，比去年多了兩成"]
    assert "數字「五千億」" in problems_of(result) and "數字「三成」" in problems_of(result)


def test_a_claim_that_turns_the_meaning_around_is_rejected():
    extra, result = run(RawPoint(at("預算大約"), "預算不需要三千億"))

    assert extra == [] and "否定的說法" in problems_of(result)


def test_a_question_must_be_written_as_a_question_not_as_a_statement():
    statement = RawPoint(at("那公共托育"), "公共托育現在有不足")
    question = RawPoint(at("那公共托育"), "追問公共托育現在有什麼不足")
    asked = RawPoint(at("所以您認為"), "詢問補助是否應該加倍")
    claimed = RawPoint(at("所以您認為"), "認為補助應該加倍")

    extra, result = run(statement, question, asked, claimed)

    assert sorted(p.text for p in extra) == sorted(["追問公共托育現在有什麼不足", "詢問補助是否應該加倍"])
    assert problems_of(result).count("問句") == 2


def test_a_statement_inside_a_turn_that_also_asks_something_is_not_treated_as_a_question():
    extra, _ = run(RawPoint(at("今天我們"), "談少子化的議題"))

    assert [p.turn for p in extra] == [0]


# --- 重點檢查：很短的回答 ---


def test_a_one_word_answer_needs_the_question_it_answers_as_context():
    answer = at("對。")
    question = answer - 1

    with_context, _ = run(RawPoint(answer, "同意還要提高保母的薪資", context=(question,)))
    no_context, bare = run(RawPoint(answer, "同意還要提高保母的薪資"))
    wrong_context, _ = run(RawPoint(answer, "同意還要提高保母的薪資", context=(0,)))

    assert [(p.speaker, p.turn, p.evidence) for p in with_context] == [("S2", answer, "對")]
    assert no_context == [] and "必須在 context" in problems_of(bare)
    assert wrong_context == []


def test_a_one_word_answer_cannot_be_written_as_the_question_it_answers():
    answer = at("對。")

    extra, result = run(RawPoint(answer, "詢問還要提高保母的薪資", context=(answer - 1,)))

    assert extra == [] and "只能寫成表態" in problems_of(result)


def test_a_no_answer_must_be_written_as_a_refusal_not_an_agreement():
    turns, good = with_anchors(("S1", "是不是還要提高保母的薪資？"), ("S2", "不是。"))

    no, _ = run(RawPoint(1, "不同意還要提高保母的薪資", context=(0,)), turns=turns, speakers=TWO, good=good)
    yes, result = run(RawPoint(1, "同意還要提高保母的薪資", context=(0,)), turns=turns, speakers=TWO, good=good)

    assert [(p.speaker, p.turn) for p in no] == [("S2", 1)]
    assert yes == [] and "只能寫成表態" in problems_of(result)


def test_an_answer_whose_content_is_not_in_the_context_is_rejected():
    answer = at("對。")

    extra, result = run(RawPoint(answer, "同意提高托育補助", context=(answer - 1,)))

    assert extra == [] and "對不上" in problems_of(result)


def test_context_must_come_just_before_the_answer():
    answer = at("對。")

    extra, _ = run(RawPoint(answer, "同意還要提高保母的薪資", context=(answer + 1, answer, -1, True)))

    assert extra == []


# --- 重點檢查：複述與引文 ---


def test_a_turn_that_restates_what_someone_else_said_earlier_is_not_that_speakers_claim():
    turns, good = with_anchors(("S2", "名額不夠，很多家長排不到，排了兩年也沒有消息。"),
                               ("S1", "您剛才說名額不夠，家長排不到。"))

    host, result = run(RawPoint(1, "名額不夠，家長排不到"), turns=turns, speakers=TWO, good=good)
    guest, _ = run(RawPoint(0, "名額不夠，家長排不到"), turns=turns, speakers=TWO, good=good)

    assert host == [] and "複述" in problems_of(result)
    assert [(p.speaker, p.turn) for p in guest] == [("S2", 0)]


def test_the_hosts_own_new_content_in_a_restating_turn_is_still_his_own():
    turns, good = with_anchors(("S2", "名額不夠，很多家長排不到。"),
                               ("S1", "您剛才說名額不夠，那政府打算在明年增加多少預算來處理？"))

    extra, _ = run(RawPoint(1, "追問政府打算在明年增加多少預算來處理"), turns=turns, speakers=TWO, good=good)

    assert [(p.speaker, p.turn) for p in extra] == [("S1", 1)]


def test_a_quote_must_be_found_word_for_word_in_one_turn_of_the_same_speaker():
    turns, good = with_anchors(
        ("S1", "我不同意這個做法，因為名額不夠，家長排了兩年還是排不到，這樣不公平。"),
        ("S2", "那政府打算怎麼處理這件事情呢。"),
        ("S1", "我認為應該立刻增加公共托育的名額，而且要把保母的薪資一起調整。"))
    verbatim = RawPoint(0, "指出「名額不夠，家長排了兩年還是排不到」")
    other_speakers_words = RawPoint(0, "指出「這樣不公平，那政府打算怎麼處理這件事情」")
    too_short = RawPoint(0, "指出「名額不夠」")
    unbalanced = RawPoint(0, "指出「名額不夠，家長排了兩年還是排不到")
    spliced_same_speaker = RawPoint(0, "指出「這樣不公平，我認為應該立刻增加公共托育的名額」")

    extra, result = run(verbatim, other_speakers_words, too_short, unbalanced, spliced_same_speaker,
                        turns=turns, speakers=TWO, good=good)

    assert [p.text for p in extra] == ["指出「名額不夠，家長排了兩年還是排不到」"]
    text = problems_of(result)
    assert text.count("引文") >= 3 and "引號沒有成對" in text and "不到 8 個字" in text


# --- 排版 ---


def point(speaker: str, text: str, start: float = 0.0, turn: int = 0) -> Point:
    return Point(speaker=speaker, turn=turn, start=start, text=text, evidence=text)


def test_bullets_are_the_first_four_points_and_detail_joins_runs_of_the_same_speaker():
    points = (point("S1", "甲", 5.0), point("S1", "乙", 6.0), point("S2", "丙", 7.0),
              point("S1", "丁", 8.0), point("S2", "戊", 9.0))

    slide = render_slide(3, "標題", points)

    assert slide.bullets == ("[[S1]]：甲", "[[S1]]：乙", "[[S2]]：丙", "[[S1]]：丁")
    assert slide.detail == "[[S1]]：甲；乙。 [[S2]]：丙。 [[S1]]：丁。 [[S2]]：戊。"
    assert (slide.index, slide.title, slide.timestamp, slide.points, slide.image_path) == (3, "標題", 5.0, points, None)


def test_a_slide_needs_at_least_one_point():
    with pytest.raises(ValueError):
        render_slide(1, "標題", ())


def test_placeholders_become_names_when_given_and_labels_otherwise():
    text = "[[S1]]：問。 [[S2]]：答。 [[S3]]：插話。 [[S0]]：聽不清。 [[S9]]：誰"

    assert render_placeholders(text, SPEAKERS) == (
        "發言者 A：問。 發言者 B：答。 其他聲音 1：插話。 無法判斷：聽不清。 未知說話者：誰")
    assert render_placeholders(text, SPEAKERS, {"S1": "  王小明 ", "S2": "", "S3": "   "}) == (
        "王小明：問。 發言者 B：答。 其他聲音 1：插話。 無法判斷：聽不清。 未知說話者：誰")


def test_text_without_placeholders_is_left_alone():
    assert render_placeholders("沒有任何代號[[]] [S1]", SPEAKERS) == "沒有任何代號[[]] [S1]"


# --- 契約範例 ---

CONTRACT_TURNS = (
    (5.0, "S1", "今天我們來談少子化，先請教您怎麼看目前的補助政策？"),
    (14.0, "S2", "現金補助有幫助，但不能只靠現金，托育資源更重要。"),
    (31.0, "S1", "那公共托育現在有什麼不足？"),
    (38.0, "S2", "名額不夠，很多家長排不到，排了兩年也沒有消息。"),
    (62.0, "S3", "我想補充，我們社區就是這樣。"),
    (69.0, "S1", "謝謝，那預算要怎麼編才夠？"),
    (76.0, "S2", "至少要把公共托育的名額加倍，還要提高保母的薪資。"),
)


def test_the_canonical_wire_example_comes_out_exactly_as_the_contract_says():
    turns = tuple(Turn(i, start, start + 7.0, speaker, text) for i, (start, speaker, text) in enumerate(CONTRACT_TURNS))
    speakers = make_speakers("participant", "participant", "minor")
    window = chunk_turns(turns, speakers)[0]
    raw = [
        RawSlide("補助政策與托育名額", (
            RawPoint(0, "詢問目前補助政策的成效"),
            RawPoint(1, "現金補助有幫助，但托育資源更重要"),
            RawPoint(2, "追問公共托育現在有什麼不足"),
            RawPoint(3, "公共托育名額不夠，家長排了兩年沒有消息"))),
        RawSlide("預算怎麼編", (
            RawPoint(5, "詢問預算要怎麼編才夠"),
            RawPoint(6, "主張把公共托育的名額加倍，並提高保母的薪資"))),
    ]

    result = validate_points(raw, turns, speakers, window)
    slides = [render_slide(i, s.title, s.points) for i, s in enumerate(result.slides, 1)]

    assert result.problems == () and (result.proposed, result.kept) == (6, 6)
    assert format_window(window, speakers).split("\n")[4] == "[T4] X：我想補充，我們社區就是這樣。"
    assert [(p.speaker, p.turn, p.start, p.text, p.evidence) for p in slides[0].points] == [
        ("S1", 0, 5.0, "詢問目前補助政策的成效", "先請教您怎麼看目前的補助政策"),
        ("S2", 1, 14.0, "現金補助有幫助，但托育資源更重要", "現金補助有幫助，但不能只靠現金，托育資源更重要"),
        ("S1", 2, 31.0, "追問公共托育現在有什麼不足", "那公共托育現在有什麼不足"),
        ("S2", 3, 38.0, "公共托育名額不夠，家長排了兩年沒有消息", "名額不夠，很多家長排不到，排了兩年也沒有消息"),
    ]
    assert slides[0].bullets == (
        "[[S1]]：詢問目前補助政策的成效", "[[S2]]：現金補助有幫助，但托育資源更重要",
        "[[S1]]：追問公共托育現在有什麼不足", "[[S2]]：公共托育名額不夠，家長排了兩年沒有消息")
    assert slides[0].detail == (
        "[[S1]]：詢問目前補助政策的成效。 [[S2]]：現金補助有幫助，但托育資源更重要。 "
        "[[S1]]：追問公共托育現在有什麼不足。 [[S2]]：公共托育名額不夠，家長排了兩年沒有消息。")
    assert slides[0].timestamp == 5.0
    assert [(p.speaker, p.turn, p.start, p.evidence) for p in slides[1].points] == [
        ("S1", 5, 69.0, "那預算要怎麼編才夠"),
        ("S2", 6, 76.0, "至少要把公共托育的名額加倍，還要提高保母的薪資"),
    ]
    assert slides[1].bullets == ("[[S1]]：詢問預算要怎麼編才夠", "[[S2]]：主張把公共托育的名額加倍，並提高保母的薪資")
    assert slides[1].detail == "[[S1]]：詢問預算要怎麼編才夠。 [[S2]]：主張把公共托育的名額加倍，並提高保母的薪資。"
    assert slides[1].timestamp == 69.0


# --- 性質：說話者永遠來自被引用的那一段，引錯會被改正或丟掉 ---

# 沒有虛字、否定字、數字、人稱、稱呼的字，隨機湊出來的「話」彼此不會有共同的字組
_POOL = list("山水火土金木花草鳥魚蟲獸石海河湖雨雪雲風雷電星月光影聲色味香書畫琴棋詩酒茶米麥稻鐵銅銀玉珠寶鏡燈橋塔牆屋")


def synthetic(rng: random.Random):
    count = rng.randint(4, 14)
    kinds = ["participant", "participant", "participant", "minor"]
    speakers = make_speakers(*kinds)
    lines = []
    seen: set[str] = set()
    for _ in range(count):
        while True:
            body = "".join(rng.sample(_POOL, rng.randint(9, 14)))
            pairs = {body[i:i + 2] for i in range(len(body) - 1)}
            if not pairs & seen:
                seen |= pairs
                break
        lines.append((rng.choice(["S1", "S2", "S3", "S4"]), body + "。"))
    return make_turns(*lines), speakers


@pytest.mark.parametrize("seed", range(60))
def test_a_kept_point_rests_on_the_turn_that_contains_its_words_and_takes_that_turns_speaker(seed):
    rng = random.Random(seed)
    turns, speakers = synthetic(rng)
    window = chunk_turns(turns, speakers)[0]
    citable = {s.id for s in speakers if s.kind == "participant"}

    for source in turns:
        claim = "說明" + source.text[1:9]
        for offset in (-5, -3, -2, -1, 0, 1, 2, 3, 5):
            cited = source.index + offset
            if not 0 <= cited < len(turns):
                continue
            slide = RawSlide("標題", (RawPoint(cited, claim), RawPoint(cited, claim + "的內容")))

            kept = [p for s in validate_points([slide], turns, speakers, window).slides for p in s.points]

            if (source.speaker not in citable or turns[cited].speaker not in citable
                    or abs(offset) > conversation.ANCHOR_RADIUS):
                assert kept == []          # 出處不能引用、引用了不能引用的段落、或離出處太遠
            else:
                assert [(p.turn, p.speaker) for p in kept] == [(source.index, source.speaker)] * 2
