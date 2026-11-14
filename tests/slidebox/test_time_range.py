"""影片區間（add_video --start/--end）：領域的值物件，與 use case 用的「夾住、回報、把時間加回去」。

全部是純函式，不碰檔案、模型或網路。
"""
from __future__ import annotations

import math

import pytest

from slidebox.domain.entities import MIN_RANGE_SECONDS, AudioClip, DiarizerOutput, SpeakerTurn, TimeRange, Word
from slidebox.domain.errors import VideoRangeInvalid
from slidebox.usecases.time_range import (
    effective_range,
    normalize_range,
    reported_range,
    shift_turns,
    shift_words,
)

# --- 值物件 ---


def test_a_range_may_leave_either_end_open():
    assert (TimeRange(600.0, 1500.0).start, TimeRange(600.0, 1500.0).end) == (600.0, 1500.0)
    assert TimeRange(600.0).end is None
    assert TimeRange(end=1500.0).start == 0.0


@pytest.mark.parametrize("start,end", [
    (-1.0, 100.0),                      # 負的起點
    (100.0, 100.0), (100.0, 50.0),      # 終點不在起點之後
    (100.0, 100.0 + MIN_RANGE_SECONDS - 0.5),   # 比下限還短
    (math.nan, 100.0), (0.0, math.nan),
    (math.inf, None), (0.0, math.inf), (-math.inf, 10.0),
    (True, 100.0), (0.0, False), ("10", 100.0), (0.0, "100"), (None, 100.0),
])
def test_a_range_that_makes_no_sense_is_refused(start, end):
    """攔的 bug：打錯的區間（終點在起點前面、負數、NaN、字串）一路走到 ffmpeg 才失敗，或被默默當成整支影片。"""
    with pytest.raises(ValueError, match="區間"):
        TimeRange(start, end)


def test_the_shortest_allowed_range_is_exactly_the_minimum():
    assert TimeRange(10.0, 10.0 + MIN_RANGE_SECONDS).end == 10.0 + MIN_RANGE_SECONDS


# --- normalize_range：沒有給、或只是「整支影片」的區間就是沒有區間 ---


@pytest.mark.parametrize("given", [None, TimeRange(), TimeRange(0.0), TimeRange(0.0, None)])
def test_no_range_and_a_range_that_is_the_whole_video_are_the_same_thing(given):
    assert normalize_range(given) is None


@pytest.mark.parametrize("given", [TimeRange(1.0), TimeRange(0.0, 60.0), TimeRange(600.0, 1500.0), TimeRange(end=60.0)])
def test_a_real_range_is_kept(given):
    assert normalize_range(given) is given


# --- effective_range：影片的實際長度知道之後 ---


def test_a_range_inside_the_video_is_used_as_given():
    assert effective_range(TimeRange(600.0, 1500.0), 1800.0) == TimeRange(600.0, 1500.0)


def test_an_open_end_becomes_the_end_of_the_video():
    assert effective_range(TimeRange(600.0), 1800.0) == TimeRange(600.0, 1800.0)


def test_an_end_past_the_video_is_clamped_to_its_length():
    assert effective_range(TimeRange(600.0, 5000.0), 1800.0) == TimeRange(600.0, 1800.0)


@pytest.mark.parametrize("given", [TimeRange(0.0, 1800.0), TimeRange(0.0, 5000.0)])
def test_a_range_that_covers_the_whole_video_is_no_range(given):
    """攔的 bug：整支影片被寫成「只處理影片的 00:00–30:00」，還多切一次音訊。"""
    assert effective_range(given, 1800.0) is None


def test_no_range_stays_no_range():
    assert effective_range(None, 1800.0) is None


@pytest.mark.parametrize("start", [1800.0, 1800.5, 5000.0])
def test_a_start_at_or_after_the_end_of_the_video_is_refused_with_the_length(start):
    with pytest.raises(VideoRangeInvalid, match=r"起點.*超過影片長度.*30:00"):
        effective_range(TimeRange(start), 1800.0)


def test_a_range_left_too_short_by_the_clamp_is_refused():
    """起點離影片結尾只剩 2 秒：夾完比下限還短，不是「照樣處理」。"""
    with pytest.raises(VideoRangeInvalid, match="太短"):
        effective_range(TimeRange(1798.0, 5000.0), 1800.0)


def test_an_unknown_video_length_leaves_the_range_alone():
    """yt-dlp 沒回報長度（0）：沒辦法夾，也沒辦法檢查起點；交給切出來的音訊長度判斷。"""
    assert effective_range(TimeRange(600.0, 1500.0), 0.0) == TimeRange(600.0, 1500.0)
    assert effective_range(TimeRange(600.0), 0.0) == TimeRange(600.0, None)


# --- reported_range：回報給 Pi 的實際區間（新做的與接著做的一律走同一條路） ---


def _words(*ends: float):
    return tuple(Word(end - 0.5, end, "字") for end in ends)


def test_the_reported_range_is_the_clamped_range():
    assert reported_range(TimeRange(600.0, 5000.0), 1800.0, _words(700.0)) == TimeRange(600.0, 1800.0)


def test_no_range_is_reported_when_none_was_asked_for_or_it_covers_the_whole_video():
    assert reported_range(None, 1800.0, _words(700.0)) is None
    assert reported_range(TimeRange(0.0, 5000.0), 1800.0, _words(700.0)) is None


def test_an_unknown_length_and_an_open_end_report_the_last_spoken_word():
    """長度不明又沒給終點：用最後一個字的結束時間，總比留空好；Pi 要求兩個鍵一起出現。"""
    assert reported_range(TimeRange(600.0), 0.0, _words(700.0, 1234.5)) == TimeRange(600.0, 1234.5)


def test_an_unknown_length_with_a_given_end_reports_that_end():
    assert reported_range(TimeRange(600.0, 1500.0), 0.0, _words(700.0)) == TimeRange(600.0, 1500.0)


# --- 把區間內的時間加回原影片的時間軸 ---


def test_turns_are_moved_onto_the_original_timeline():
    output = DiarizerOutput((SpeakerTurn(1.0, 4.5, "A"), SpeakerTurn(4.5, 9.0, "B")), overlap_seconds=2.5)
    moved = shift_turns(output, 600.0)
    assert moved.turns == (SpeakerTurn(601.0, 604.5, "A"), SpeakerTurn(604.5, 609.0, "B"))
    assert moved.overlap_seconds == 2.5                      # 秒數是長度，不是時間點


def test_words_keep_everything_but_their_times():
    words = (Word(1.0, 1.4, "你", segment=3, probability=0.5), Word(1.4, 1.8, "好", segment=3))
    moved = shift_words(words, 600.0)
    assert moved == (Word(601.0, 601.4, "你", segment=3, probability=0.5), Word(601.4, 601.8, "好", segment=3))


def test_shifting_by_nothing_returns_the_same_values():
    output = DiarizerOutput((SpeakerTurn(1.0, 4.5, "A"),), 0.0)
    words = (Word(1.0, 1.4, "你"),)
    assert shift_turns(output, 0.0) is output
    assert shift_words(words, 0.0) == words


def test_shifted_times_do_not_grow_float_noise():
    """攔的 bug：600 + 1.23 在浮點數裡是 601.2299999999999；發佈出去的時間與存檔不該帶這種尾巴。"""
    moved = shift_words((Word(1.23, 1.23 + 0.1, "字"),), 600.0)
    assert moved[0].start == 601.23 and moved[0].end == 601.33


def test_non_finite_times_stay_non_finite_so_the_alignment_still_distrusts_them():
    moved = shift_words((Word(math.nan, math.nan, "字"), Word(1.0, math.inf, "字")), 600.0)
    assert math.isnan(moved[0].start) and math.isnan(moved[0].end)
    assert moved[1].start == 601.0 and math.isinf(moved[1].end)


def test_an_audio_clip_defaults_to_no_offset():
    assert AudioClip(path="a.webm", video_id="v", title="t", duration=1.0).offset == 0.0
