"""多人模式的零件（diarized_pipeline）單獨測：存檔指紋、逐窗摘要迴圈、可靠度檢查、彙整、來源註記。

整條流程（含下載、辨識、取消）在 test_build_deck_diarized.py；這裡用手刻的最小輸入，
一次只動一個條件，邊界（剛好 25%、頁數剛好等於上限）看得清楚。
"""
from __future__ import annotations

import dataclasses

import pytest

from slidebox.domain.entities import DiarizationReport, Point, RawPoint, RawSlide, Settings, Turn
from slidebox.domain.errors import AttributionUnreliable, SummarizerOutputInvalid, SummarizerUnavailable
from slidebox.usecases import diarized_pipeline as dp
from slidebox.usecases.conversation import Allocation, CheckedSlide, ValidationResult, Window, chunk_turns
from slidebox.usecases.speakers import (
    WARN_COUNT_MISMATCH,
    WARN_HIGH_UNASSIGNED,
    WARN_MANY_MINOR,
    WARN_SINGLE_FOUND,
)

from .diarized_support import FakeConversation
from .speaker_fixtures import make_speakers, make_turns


def point(turn: int, speaker: str = "S1", text: str | None = None) -> Point:
    return Point(speaker, turn, turn * 10.0, text or f"重點{turn}", f"原文{turn}")


def checked(title: str, *turns: int) -> CheckedSlide:
    return CheckedSlide(title, tuple(point(t) for t in turns))


def window_over(first: int, last: int, index: int = 0, speaker: str = "S1") -> Window:
    return Window(index, tuple(Turn(i, i * 10.0, i * 10.0 + 9.0, speaker, f"第{i}段的話") for i in range(first, last + 1)))


def outcome(first: int, last: int, proposed: int, kept: int, *, pages: int = 1, skipped: bool = False,
            index: int = 0, trimmed: int = 0) -> dp.WindowOutcome:
    slides = tuple(checked(f"第{n}頁", first + n) for n in range(pages)) if kept else ()
    return dp.WindowOutcome(Allocation(window_over(first, last, index), 1, 3), slides, proposed, kept,
                            skipped=skipped, trimmed_pages=trimmed)


def result(slides: int, proposed: int, kept: int, problems: tuple[str, ...] = ()) -> ValidationResult:
    return ValidationResult(tuple(checked(f"第{n}頁", n) for n in range(slides)), proposed, kept, problems)


def report(**overrides) -> DiarizationReport:
    values = dict(engine="e", version="1", model="m", fingerprint="f", status="ok", warnings=(),
                  requested_speakers=None, found_speakers=2, minor_speakers=0, speech_seconds=100.0,
                  unassigned_ratio=0.0, overlap_ratio=0.0)
    values.update(overrides)
    return DiarizationReport(**values)


# --- 存檔指紋 ---


def test_a_fingerprint_is_stable_and_short():
    assert dp.stage_fingerprint("a", 1, None) == dp.stage_fingerprint("a", 1, None)
    assert len(dp.stage_fingerprint("a")) == 16
    assert dp.stage_fingerprint("a", 1) != dp.stage_fingerprint("a", 2)
    assert dp.stage_fingerprint("少子化") != dp.stage_fingerprint("高齡化")


def test_the_diarization_fingerprint_follows_the_engine_and_the_requested_count():
    base = dp.diarization_fingerprint("engine-1", None)
    assert dp.diarization_fingerprint("engine-1", None) == base
    assert dp.diarization_fingerprint("engine-2", None) != base
    assert dp.diarization_fingerprint("engine-1", 2) != base
    assert dp.diarization_fingerprint("engine-1", 2) != dp.diarization_fingerprint("engine-1", 3)


def test_the_words_fingerprint_follows_model_hint_and_alignment_version(monkeypatch):
    base = dp.words_fingerprint("whisper-a", "少子化")
    assert dp.words_fingerprint("whisper-a", "少子化") == base
    assert dp.words_fingerprint("whisper-b", "少子化") != base
    assert dp.words_fingerprint("whisper-a", "高齡化") != base
    assert dp.words_fingerprint("whisper-a", None) == dp.words_fingerprint("whisper-a", "")
    assert dp.words_fingerprint("whisper-a", None) != base
    monkeypatch.setattr(dp, "ALIGN_VERSION", dp.ALIGN_VERSION + 1)
    assert dp.words_fingerprint("whisper-a", "少子化") != base


def test_the_words_and_diarization_fingerprints_never_collide_across_stages():
    assert dp.words_fingerprint("x", None) != dp.diarization_fingerprint("x", None)


SETTINGS = Settings(output_dir="OUT", min_slides=2, max_slides=6, model="qwen-a")
PLAN = [(1, 3, "[T0] S1：甲"), (1, 3, "[T1] S2：乙")]


@pytest.mark.parametrize("change", [
    dict(model="qwen-b"), dict(num_ctx=16384), dict(min_slides=3), dict(max_slides=7),
], ids=["model", "context", "min", "max"])
def test_the_conversation_fingerprint_follows_every_setting_the_model_saw(change):
    base = dp.conversation_fingerprint(SETTINGS, 6000, PLAN)
    assert dp.conversation_fingerprint(dataclasses.replace(SETTINGS, **change), 6000, PLAN) != base


def test_the_conversation_fingerprint_ignores_settings_multi_speaker_mode_does_not_use():
    base = dp.conversation_fingerprint(SETTINGS, 6000, PLAN)
    unrelated = dataclasses.replace(SETTINGS, detailed=True, whisper_model="other", max_height=360, image_width=640)
    assert dp.conversation_fingerprint(unrelated, 6000, PLAN) == base


def test_the_conversation_fingerprint_follows_the_windows_and_the_chunk_size(monkeypatch):
    base = dp.conversation_fingerprint(SETTINGS, 6000, PLAN)
    assert dp.conversation_fingerprint(SETTINGS, 7000, PLAN) != base
    assert dp.conversation_fingerprint(SETTINGS, 6000, PLAN[:1]) != base
    assert dp.conversation_fingerprint(SETTINGS, 6000, [(1, 3, "[T0] S1：甲"), (1, 3, "[T1] S1：乙")]) != base
    assert dp.conversation_fingerprint(SETTINGS, 6000, [(1, 4, "[T0] S1：甲"), (1, 3, "[T1] S2：乙")]) != base
    monkeypatch.setattr(dp, "PROMPT_VERSION", dp.PROMPT_VERSION + 1)
    assert dp.conversation_fingerprint(SETTINGS, 6000, PLAN) != base


# --- 來源註記 ---


def test_the_source_note_is_the_fixed_sentence_when_there_are_no_warnings():
    assert dp.source_note(report()) == dp.SOURCE_NOTE
    assert "標籤可能有誤" in dp.SOURCE_NOTE and "未經人工確認" in dp.SOURCE_NOTE


def test_every_warning_is_spelled_out_in_the_source_note():
    note = dp.source_note(report(
        warnings=(WARN_COUNT_MISMATCH, WARN_HIGH_UNASSIGNED, WARN_MANY_MINOR, WARN_SINGLE_FOUND),
        requested_speakers=3, found_speakers=2, minor_speakers=4, unassigned_ratio=0.31))
    assert note == (dp.SOURCE_NOTE + "；指定 3 人，實際找到 2 位主要發言者；約 31% 的說話時間無法判斷是誰在說；"
                    "有 4 個只出現一下的聲音，未納入重點；只找到 1 位主要發言者")


def test_an_unknown_warning_code_is_shown_as_is_rather_than_dropped():
    assert dp.source_note(report(warnings=("something_new",))).endswith("；something_new")


# --- 頁數超過上限 ---


def test_a_result_within_the_limit_is_left_alone():
    same = result(slides=2, proposed=4, kept=4)
    fitted, cut = dp.fit_to_range(same, max_slides=2)
    assert fitted is same and cut == 0


def test_pages_beyond_the_limit_are_cut_and_their_points_leave_both_counts():
    """砍掉的頁不算「被檢查丟掉」：提出與留下都扣掉，被檢查丟掉的點數不變。"""
    slides = (checked("一", 0, 1), checked("二", 2, 3, 4), checked("三", 5, 6))
    fitted, cut = dp.fit_to_range(ValidationResult(slides, proposed=9, kept=7, problems=("丟了兩個",)), max_slides=2)
    assert [s.title for s in fitted.slides] == ["一", "二"] and cut == 1
    assert (fitted.proposed, fitted.kept) == (7, 5) and fitted.problems == ("丟了兩個",)
    assert fitted.proposed - fitted.kept == 2


def test_the_front_pages_are_the_ones_kept():
    slides = tuple(checked(f"第{n}頁", n) for n in range(5))
    fitted, cut = dp.fit_to_range(ValidationResult(slides, 5, 5, ()), max_slides=3)
    assert [s.title for s in fitted.slides] == ["第0頁", "第1頁", "第2頁"] and cut == 2


# --- 重問的提示與比例 ---


def test_the_retry_hint_lists_each_problem_on_its_own_line():
    hint = dp.retry_hint(["第一個問題", "第二個問題"])
    assert hint.startswith("上一次的結果有這些問題")
    assert hint.endswith("：\n- 第一個問題\n- 第二個問題")


def test_the_retry_hint_still_says_something_when_there_are_no_reasons():
    assert dp.retry_hint([]).endswith("：\n- 沒有任何一頁通過檢查")


@pytest.mark.parametrize("proposed,kept,expected", [(0, 0, 1.0), (4, 4, 0.0), (8, 6, 0.25), (3, 0, 1.0)])
def test_dropped_ratio(proposed, kept, expected):
    assert dp.dropped_ratio(proposed, kept) == expected


# --- 逐窗摘要 ---


def _setup(*lines, budget=10**6, pages=(1, 2)):
    """lines 是（說話者代號, 文字）；回傳（turns, speakers, allocations）。S1／S2 是主要發言者，S3 是其他聲音。"""
    turns = make_turns(*lines)
    speakers = make_speakers("participant", "participant", "minor")
    return turns, speakers, tuple(Allocation(w, *pages) for w in chunk_turns(turns, speakers, budget))


LINES = (("S1", "請問今年的預算編列有沒有增加"), ("S2", "今年的預算大約增加了兩成左右"),
         ("S1", "那明年的預算打算怎麼編列呢"), ("S2", "明年預計再增加一成左右而已"))


def run_windows(conversation, turns, speakers, allocations, *, check=None, cancelled=None):
    seen = []
    outcomes = dp.summarize_windows(
        conversation, allocations, turns, speakers, lambda frac, status: seen.append((frac, status)),
        check or (lambda: None), cancelled or (lambda: False))
    return outcomes, seen


# 每一段話整段照抄當 quote：一定通過 quote 的核對（單段、連續）
QUOTES = {number: text for number, (_, text) in enumerate(LINES)}


def reply_citing(*cites):
    return lambda call: (RawSlide("標題", tuple(
        RawPoint(turn, text, quote=QUOTES.get(turn, "")) for turn, text in cites)),)


GOOD = ((0, "詢問今年的預算編列有沒有增加"), (1, "今年的預算大約增加了兩成左右"))


def test_a_window_is_sent_with_its_text_turn_range_and_page_range():
    turns, speakers, allocations = _setup(*LINES, pages=(2, 3))
    conversation = FakeConversation([reply_citing(*GOOD)])
    outcomes, _ = run_windows(conversation, turns, speakers, allocations)
    (call,) = conversation.calls
    assert (call.first_turn, call.last_turn, call.min_slides, call.max_slides, call.hint) == (0, 3, 2, 3, "")
    assert call.citable_turns == (0, 1, 2, 3)
    assert call.window_text.startswith("[T0] S1：請問今年的預算編列有沒有增加\n[T1] S2：")
    assert [(o.proposed, o.kept, o.skipped) for o in outcomes] == [(2, 2, False)]


def test_a_window_nobody_citable_spoke_in_is_not_sent_to_the_model():
    """整個窗都是其他聲音（來電者、背景人聲）：沒有東西可以引用，不送出去，也不算失敗。"""
    turns, speakers, allocations = _setup(*LINES, ("S3", "我是打電話進來的聽眾想問一件事"),
                                          ("S3", "請問這個政策什麼時候開始實施呢"), budget=100)
    assert len(allocations) >= 2
    conversation = FakeConversation(default=reply_citing(*GOOD))
    outcomes, seen = run_windows(conversation, turns, speakers, allocations)
    skipped = [o for o in outcomes if o.skipped]
    assert skipped and all(o.slides == () and (o.proposed, o.kept) == (0, 0) for o in skipped)
    assert len(conversation.calls) == len(outcomes) - len(skipped)
    assert all(call.last_turn < skipped[0].allocation.window.first_turn for call in conversation.calls)
    assert any("沒有主要發言者可以整理，略過" in status for _, status in seen)


def test_the_cancel_check_runs_before_every_model_call():
    turns, speakers, allocations = _setup(*LINES)
    seen = []
    # 第一次有一個點引用了不存在的段落：丟掉一半，所以要重問一次
    conversation = FakeConversation([reply_citing((0, "詢問今年的預算編列有沒有增加"), (90, "錯誤的內容")), reply_citing(*GOOD)])
    run_windows(conversation, turns, speakers, allocations, check=lambda: seen.append(len(conversation.calls)))
    assert seen == [0, 1]                    # 第一次與重問之前各檢查一次，都在呼叫之前


FOUR_GOOD = ((0, "詢問今年的預算編列有沒有增加"), (1, "今年的預算大約增加了兩成左右"),
             (2, "詢問明年的預算打算怎麼編列"), (3, "明年預計再增加一成左右而已"))
NOWHERE = tuple((90 + n, "根本沒有這一段的內容") for n in range(8))      # 不存在的段落：一定被丟掉


def test_the_checks_accept_the_hand_written_good_points():
    """FOUR_GOOD 真的全部通過檢查；下面的測試靠這個前提，這裡先釘住。"""
    turns, speakers, allocations = _setup(*LINES)
    outcomes, _ = run_windows(FakeConversation([reply_citing(*FOUR_GOOD)]), turns, speakers, allocations)
    assert [(o.proposed, o.kept) for o in outcomes] == [(4, 4)]


def test_when_neither_attempt_passes_the_one_that_kept_more_is_used():
    """兩次都丟掉一半：選留下來的重點多的那一次（整份夠不夠可靠，交給整份的比例檢查）。"""
    turns, speakers, allocations = _setup(*LINES)
    conversation = FakeConversation([reply_citing(*FOUR_GOOD[:2], *NOWHERE[:2]),
                                     reply_citing(*FOUR_GOOD, *NOWHERE[:4])])
    outcomes, _ = run_windows(conversation, turns, speakers, allocations)
    assert len(conversation.calls) == 2 and [(o.proposed, o.kept) for o in outcomes] == [(8, 4)]


def test_when_neither_attempt_passes_fewer_dropped_does_not_beat_more_kept():
    conversation = FakeConversation([reply_citing(*FOUR_GOOD[:3], *NOWHERE[:3]),       # 留 3、丟 3
                                     reply_citing(*FOUR_GOOD[:2], *NOWHERE[:2])])      # 留 2、丟 2
    turns, speakers, allocations = _setup(*LINES)
    outcomes, _ = run_windows(conversation, turns, speakers, allocations)
    assert [(o.proposed, o.kept) for o in outcomes] == [(6, 3)]


def test_an_attempt_that_passes_beats_one_that_kept_more_but_did_not():
    turns, speakers, allocations = _setup(*LINES)
    conversation = FakeConversation([reply_citing(*FOUR_GOOD, *NOWHERE[:3]), reply_citing(*FOUR_GOOD[:3])])
    outcomes, _ = run_windows(conversation, turns, speakers, allocations)
    assert [(o.proposed, o.kept) for o in outcomes] == [(3, 3)]


@pytest.mark.parametrize("good,dropped,calls", [(4, 0, 1), (4, 1, 1), (3, 1, 1), (4, 2, 2), (3, 2, 2), (2, 1, 2)])
def test_a_window_is_asked_again_only_when_more_than_a_quarter_was_dropped(good, dropped, calls):
    """3 好 1 壞剛好 25%，不重問；2 好 1 壞是 33%，重問。"""
    turns, speakers, allocations = _setup(*LINES)
    conversation = FakeConversation(default=reply_citing(*FOUR_GOOD[:good], *NOWHERE[:dropped]))
    run_windows(conversation, turns, speakers, allocations)
    assert len(conversation.calls) == calls


def test_a_model_that_cannot_be_reached_is_not_retried_here():
    turns, speakers, allocations = _setup(*LINES)
    conversation = FakeConversation([SummarizerUnavailable("連不上 Ollama")])
    with pytest.raises(SummarizerUnavailable, match="連不上 Ollama"):
        run_windows(conversation, turns, speakers, allocations)
    assert len(conversation.calls) == 1


def test_two_malformed_replies_raise_with_the_window_number_and_the_original_reason():
    turns, speakers, allocations = _setup(*LINES)
    conversation = FakeConversation([SummarizerOutputInvalid("第一個原因"), SummarizerOutputInvalid("第二個原因")])
    with pytest.raises(SummarizerOutputInvalid) as raised:
        run_windows(conversation, turns, speakers, allocations)
    assert "第 1/1 段" in str(raised.value) and "第二個原因" in str(raised.value)
    assert "第一個原因" in conversation.calls[1].hint


def test_progress_never_reports_more_than_the_share_of_windows_started():
    turns, speakers, allocations = _setup(*LINES, budget=60)
    assert len(allocations) >= 2
    _, seen = run_windows(FakeConversation(default=reply_citing(*GOOD)), turns, speakers, allocations)
    total = len(allocations)
    fractions = [frac for frac, _ in seen]
    assert fractions == sorted(fractions) and fractions[0] == 0.0 and fractions[-1] < 1.0
    assert all(frac in {n / total for n in range(total)} for frac in fractions)


# --- 整份的可靠度 ---


def test_a_window_with_no_surviving_page_raises_and_says_which_one():
    outcomes = [outcome(0, 9, 4, 4), outcome(10, 18, 2, 0, index=1)]
    with pytest.raises(AttributionUnreliable) as raised:
        dp.check_reliability(outcomes)
    message = str(raised.value)
    assert "第 2/2 段" in message and "T10–T18" in message and "提出 2 個重點、留下 0 個" in message
    assert "寧可不發佈" in message


def test_the_first_window_without_pages_is_the_one_reported():
    outcomes = [outcome(0, 9, 3, 0), outcome(10, 18, 2, 0, index=1)]
    with pytest.raises(AttributionUnreliable, match="第 1/2 段"):
        dp.check_reliability(outcomes)


def test_a_skipped_window_is_not_a_failure():
    dp.check_reliability([outcome(0, 9, 4, 4), outcome(10, 18, 0, 0, skipped=True, index=1)])


def test_a_job_where_every_window_was_skipped_has_nothing_to_publish():
    with pytest.raises(AttributionUnreliable, match="沒有任何一段對談整理出重點"):
        dp.check_reliability([outcome(0, 9, 0, 0, skipped=True)])


def test_nothing_to_check_is_not_reliable_either():
    with pytest.raises(AttributionUnreliable, match="沒有任何一段對談整理出重點"):
        dp.check_reliability([])


def test_exactly_a_quarter_dropped_is_reliable():
    dp.check_reliability([outcome(0, 9, 100, 75)])
    dp.check_reliability([outcome(0, 9, 4, 3)])


def test_just_over_a_quarter_dropped_is_not():
    with pytest.raises(AttributionUnreliable) as raised:
        dp.check_reliability([outcome(0, 9, 100, 74)])
    message = str(raised.value)
    assert "100 個重點" in message and "26 個沒有通過" in message and "26%" in message and "上限 25%" in message


def test_the_ratio_is_over_the_whole_job_not_per_window():
    """一個窗丟掉四成但整份只丟 10%：整份可信，照常發佈（窗內的比例由重問處理）。"""
    dp.check_reliability([outcome(0, 9, 10, 6), outcome(10, 18, 90, 90, index=1)])


def test_totals_add_up_the_windows_and_skip_nothing():
    outcomes = [outcome(0, 9, 5, 4), outcome(10, 18, 3, 3, index=1), outcome(19, 25, 0, 0, skipped=True, index=2)]
    assert dp.totals(outcomes) == (8, 7)
    assert dp.totals([]) == (0, 0)


# --- 彙整成投影片 ---


def test_pages_are_ordered_by_the_conversation_and_renumbered_from_one():
    """模型回傳的順序不算數：排序依據是每頁第一個被引用的那一段。"""
    first = dp.WindowOutcome(Allocation(window_over(0, 9), 1, 3),
                             (checked("後面的", 5, 6), checked("前面的", 1, 2)), 4, 4)
    second = dp.WindowOutcome(Allocation(window_over(10, 19, 1), 1, 3), (checked("最後的", 12),), 1, 1)
    slides = dp.assemble([first, second])
    assert [(s.index, s.title) for s in slides] == [(1, "前面的"), (2, "後面的"), (3, "最後的")]
    assert [s.timestamp for s in slides] == [10.0, 50.0, 120.0]
    assert all(s.points and s.image_path is None for s in slides)


def test_a_window_later_in_the_list_can_still_come_first_if_it_cites_earlier_turns():
    late_window = dp.WindowOutcome(Allocation(window_over(10, 19, 1), 1, 3), (checked("後", 12),), 1, 1)
    early_window = dp.WindowOutcome(Allocation(window_over(0, 9), 1, 3), (checked("前", 1),), 1, 1)
    assert [s.title for s in dp.assemble([late_window, early_window])] == ["前", "後"]


def test_pages_are_ordered_by_where_they_start_not_where_they_end():
    wide_then_narrow = dp.WindowOutcome(
        Allocation(window_over(0, 19), 1, 3), (checked("跨很長的一頁", 1, 9), checked("短的一頁", 4, 5)), 4, 4)
    assert [s.title for s in dp.assemble([wide_then_narrow])] == ["跨很長的一頁", "短的一頁"]


def test_pages_that_start_at_the_same_turn_keep_the_order_the_model_gave():
    both = dp.WindowOutcome(Allocation(window_over(0, 9), 1, 3), (checked("乙", 3, 4), checked("甲", 3, 5)), 4, 4)
    assert [s.title for s in dp.assemble([both])] == ["乙", "甲"]


def test_the_pages_carry_the_placeholders_not_names():
    slides = dp.assemble([dp.WindowOutcome(Allocation(window_over(0, 9), 1, 3),
                                           (CheckedSlide("標題", (point(1, "S2"), point(2, "S1"))),), 2, 2)])
    assert slides[0].bullets == ("[[S2]]：重點1", "[[S1]]：重點2")


def test_no_windows_no_pages():
    assert dp.assemble([]) == ()
    assert dp.assemble([outcome(0, 9, 3, 0)]) == ()
