"""只處理影片的一段區間（add_video --start/--end）：多人模式的整條流程，全部用假零件。

最重要的三件事：發佈出去的每個時間都是原影片的時間；區間外的音訊根本不會進聲音區分與語音辨識；
區間絕不被默默忽略（單人模式、沒有切割器、舊存檔）——那會把整支影片發佈出去。
"""
from __future__ import annotations

import dataclasses
import os
import threading

import pytest

from slidebox.domain.entities import TimeRange
from slidebox.domain.errors import (
    AttributionUnreliable,
    AudioCutFailed,
    DiarizationUnavailable,
    OperationCancelled,
    SpeechRecognitionFailed,
    VideoRangeInvalid,
)
from slidebox.usecases import diarized_pipeline

from .diarized_support import (
    LONG_CLIP,
    TURNS,
    URL,
    FakeAudioCutter,
    FakeDiarizer,
    FakeWordTranscriber,
    FlakyRenderer,
    crowd_parts,
    make,
    run,
    settings,
)
from .fakes import FakeAudioGateway

RANGE = TimeRange(600.0, 1500.0)
AUDIO_DIR = os.path.join("OUT", ".slidebox_audio_tmp")
CUT_FILE = os.path.join(AUDIO_DIR, "range.wav")


def long_video(**overrides):
    """半小時的影片：聲音區分與語音辨識的假零件照舊回傳「切出來的檔案裡的時間」（從 0 起算）。"""
    return make(audio=FakeAudioGateway(clip=LONG_CLIP), **overrides)


def stored(parts):
    return parts.checkpoints.stages("OUT", URL)


def counts(parts):
    return len(parts.diarizer.calls), len(parts.words.calls), len(parts.cutter.calls)


def without_counters(report):
    return dataclasses.replace(report, rescued_points=None, contradicted_points=None,
                               unverifiable_points=None, anchor_conflicts=None)


# --- 聽的是切出來的那一段 ---


def test_the_models_only_hear_the_cut_audio_and_the_audio_is_cut_once():
    """攔的 bug：區間外的聲音照樣進聲音區分（人數與 24 個聲音的上限被它們吃掉），或音訊被切了兩次。"""
    parts = long_video()
    run(parts, time_range=RANGE)
    assert parts.cutter.calls == [(LONG_CLIP.path, 600.0, 1500.0, AUDIO_DIR)]
    assert parts.diarizer.calls == [(CUT_FILE, 900.0, None)]
    assert parts.words.calls == [(CUT_FILE, 900.0)]
    assert parts.audio.events == ["cleanup", "download", "cleanup"]
    assert parts.audio.dest_dirs == [AUDIO_DIR]            # 切出來的檔和下載的音訊同一個資料夾，一起清掉


def test_without_a_range_nothing_is_cut_and_the_whole_audio_is_heard():
    parts = long_video()
    run(parts)
    assert parts.cutter.calls == []
    assert parts.diarizer.calls == [(LONG_CLIP.path, 1800.0, None)]
    assert parts.words.calls == [(LONG_CLIP.path, 1800.0)]


def test_a_job_without_a_range_does_not_need_a_cutter():
    assert run(make(cutter=None)).deck.slides


# --- 時間軸永遠是原影片的 ---


def test_every_published_time_is_a_time_on_the_original_video():
    """攔的 bug：發佈出去的時間是「區間內第幾秒」——點 &t= 連結會跳到影片開頭附近，聽到的是另一個人。"""
    plain = run(long_video()).deck
    cut = run(long_video(), time_range=RANGE).deck

    def moved(value):
        return pytest.approx(value + 600.0, abs=1e-6)

    assert [s.timestamp for s in cut.slides] == [moved(s.timestamp) for s in plain.slides]
    assert [p.start for s in cut.slides for p in s.points] == [moved(p.start) for s in plain.slides for p in s.points]
    assert [(p.speaker, p.turn) for s in cut.slides for p in s.points] == [
        (p.speaker, p.turn) for s in plain.slides for p in s.points]           # 誰說的與引用的段落一個都沒變
    assert [(s.id, s.first_seen, s.last_seen) for s in cut.speakers] == [
        (s.id, moved(s.first_seen), moved(s.last_seen)) for s in plain.speakers]
    assert [[x.start for x in s.samples] for s in cut.speakers] == [
        [moved(x.start) for x in s.samples] for s in plain.speakers]
    assert cut.transcript_text.splitlines()[0].startswith("10:05 [[S1]]：")      # 05 秒 + 10 分鐘
    assert plain.transcript_text.splitlines()[0].startswith("00:05 [[S1]]：")


def test_the_video_clips_and_screenshots_are_taken_at_the_original_times():
    parts = long_video()
    deck = run(parts, time_range=RANGE).deck
    assert parts.sections.timestamps == [s.timestamp for s in deck.slides]
    assert all(t >= 600.0 for t in parts.sections.timestamps)
    assert len(parts.frames.calls) == len(deck.slides)


def test_the_shift_does_not_change_who_said_what():
    """區間只改時間，不改對齊與歸屬：說話者清單、每一輪的人、報告裡的數字都和整支影片時一樣。"""
    plain = run(long_video()).deck
    cut = run(long_video(), time_range=RANGE).deck
    assert [(s.id, s.label, s.kind, s.turns) for s in cut.speakers] == [
        (s.id, s.label, s.kind, s.turns) for s in plain.speakers]
    for name in ("found_speakers", "minor_speakers", "status", "warnings", "proposed_points", "kept_points"):
        assert getattr(cut.diarization, name) == getattr(plain.diarization, name)


# --- 成品說清楚處理了哪一段 ---


def test_the_report_and_the_source_note_say_which_part_was_processed():
    deck = run(long_video(), time_range=RANGE).deck
    assert (deck.diarization.range_start, deck.diarization.range_end) == (600.0, 1500.0)
    assert deck.source_note == diarized_pipeline.SOURCE_NOTE + "；只處理影片的 10:00–25:00"


def test_the_range_sentence_comes_before_the_warnings():
    deck = run(long_video(), time_range=RANGE, speaker_count=3).deck
    assert deck.source_note.startswith(diarized_pipeline.SOURCE_NOTE + "；只處理影片的 10:00–25:00；指定 3 人")


def test_no_range_means_no_range_fields_and_no_sentence():
    deck = run(long_video()).deck
    assert (deck.diarization.range_start, deck.diarization.range_end) == (None, None)
    assert deck.source_note == diarized_pipeline.SOURCE_NOTE


def test_the_progress_log_says_that_the_audio_is_being_cut():
    seen = []
    run(long_video(), time_range=RANGE, progress=lambda frac, status: seen.append(status))
    assert "切出指定區間（10:00–25:00）…" in seen
    plain = []
    run(long_video(), progress=lambda frac, status: plain.append(status))
    assert not any("切出" in status for status in plain)


# --- 夾住 ---


def test_an_open_end_runs_to_the_end_of_the_video():
    parts = long_video()
    deck = run(parts, time_range=TimeRange(600.0)).deck
    assert parts.cutter.calls[0][1:3] == (600.0, 1800.0)
    assert (deck.diarization.range_start, deck.diarization.range_end) == (600.0, 1800.0)
    assert "只處理影片的 10:00–30:00" in deck.source_note


def test_an_open_start_begins_at_the_start_of_the_video():
    parts = long_video()
    deck = run(parts, time_range=TimeRange(end=900.0)).deck
    assert parts.cutter.calls[0][1:3] == (0.0, 900.0)
    assert (deck.diarization.range_start, deck.diarization.range_end) == (0.0, 900.0)
    assert [s.timestamp for s in deck.slides][0] == 5.0                 # 起點是 0：時間不動


def test_an_end_past_the_video_is_clamped_to_its_length():
    parts = long_video()
    deck = run(parts, time_range=TimeRange(600.0, 5000.0)).deck
    assert parts.cutter.calls[0][1:3] == (600.0, 1800.0)
    assert deck.diarization.range_end == 1800.0


@pytest.mark.parametrize("asked", [TimeRange(0.0, 5000.0), TimeRange(0.0, 1800.0), TimeRange(), TimeRange(0.0)])
def test_a_range_that_is_the_whole_video_is_no_range(asked):
    """攔的 bug：整支影片被寫成「只處理影片的 00:00–30:00」，還多切一次音訊、多存一份不同的存檔。"""
    parts = long_video()
    deck = run(parts, time_range=asked).deck
    assert parts.cutter.calls == []
    assert parts.diarizer.calls == [(LONG_CLIP.path, 1800.0, None)]
    assert (deck.diarization.range_start, deck.diarization.range_end) == (None, None)
    assert deck.source_note == diarized_pipeline.SOURCE_NOTE


def test_an_unknown_video_length_cuts_to_the_end_and_reports_the_last_spoken_word():
    unknown = dataclasses.replace(LONG_CLIP, duration=0.0)
    parts = make(audio=FakeAudioGateway(clip=unknown), cutter=FakeAudioCutter(rest=900.0))
    deck = run(parts, time_range=TimeRange(600.0)).deck
    assert parts.cutter.calls[0][1:3] == (600.0, None)
    assert deck.diarization.range_start == 600.0
    assert deck.diarization.range_end > 600.0
    assert deck.source_note.startswith(diarized_pipeline.SOURCE_NOTE + "；只處理影片的 10:00–")


# --- 區間不能用：開工之前、或在花任何模型的時間之前失敗 ---


@pytest.mark.parametrize("asked,message", [
    (TimeRange(1800.0), "起點.*超過影片長度.*30:00"),
    (TimeRange(2000.0, 3000.0), "起點.*超過影片長度.*30:00"),
    (TimeRange(1798.0, 5000.0), "太短"),
])
def test_a_range_the_video_cannot_satisfy_fails_before_the_models_run(asked, message):
    parts = long_video()
    with pytest.raises(VideoRangeInvalid, match=message):
        run(parts, time_range=asked)
    assert counts(parts) == (0, 0, 0)
    assert parts.audio.events == ["cleanup", "download", "cleanup"]
    assert stored(parts) == [] and parts.renderer.rendered == [] and parts.conversation.calls == []


def test_a_failed_cut_stops_the_job_and_cleans_up():
    parts = long_video(cutter=FakeAudioCutter(error=AudioCutFailed("切出指定區間的音訊失敗：Invalid data")))
    with pytest.raises(AudioCutFailed, match="Invalid data"):
        run(parts, time_range=RANGE)
    assert parts.diarizer.calls == [] and parts.words.calls == []
    assert parts.audio.events[-1] == "cleanup" and stored(parts) == [] and parts.renderer.rendered == []


def test_a_range_is_refused_in_single_mode_instead_of_being_ignored():
    """攔的 bug：單人路徑用整支影片的字幕，默默忽略區間，整支影片被發佈出去，而操作者以為只有那一段。"""
    parts = long_video()
    with pytest.raises(ValueError, match="區間.*多人模式"):
        parts.usecase.execute(URL, settings(), None, None, speaker_mode="single", time_range=RANGE)
    assert parts.subtitles.calls == [] and parts.audio.events == [] and parts.summarizer.compressed == []


def test_a_range_without_a_cutter_fails_before_anything_is_downloaded():
    """攔的 bug：這個組裝沒有切割器，卻照樣處理整支影片。"""
    parts = long_video(cutter=None)
    with pytest.raises(DiarizationUnavailable, match="區間"):
        run(parts, time_range=RANGE)
    assert parts.audio.events == [] and parts.diarizer.availability_checks == 0 and parts.diarizer.calls == []


@pytest.mark.parametrize("asked", [(600.0, 1500.0), 600.0, "10:00", [600, 1500]])
def test_a_range_that_is_not_a_time_range_is_refused(asked):
    """呼叫端傳錯型別（例如把 (start, end) 的 tuple 直接丟進來）不能被當成沒有區間。"""
    parts = long_video()
    with pytest.raises(ValueError, match="TimeRange"):
        run(parts, time_range=asked)
    assert parts.audio.events == []


def test_cancelling_after_the_cut_stops_before_the_models_and_cleans_up():
    flag = threading.Event()
    parts = long_video(cutter=FakeAudioCutter(on_cut=flag.set))
    with pytest.raises(OperationCancelled):
        run(parts, time_range=RANGE, cancelled=flag.is_set)
    assert parts.diarizer.calls == [] and parts.audio.events[-1] == "cleanup"


# --- 存檔與接著做 ---


def test_a_rerun_with_the_same_range_reuses_everything_and_neither_downloads_nor_cuts():
    parts = long_video(renderer=FlakyRenderer())
    with pytest.raises(RuntimeError):
        run(parts, time_range=RANGE)
    assert stored(parts) == ["conversation", "diarization", "words"]

    result = run(parts, time_range=RANGE)

    assert counts(parts) == (1, 1, 1) and parts.audio.events.count("download") == 1
    fresh = run(long_video(), time_range=RANGE)
    assert result.deck.slides == fresh.deck.slides
    assert result.deck.speakers == fresh.deck.speakers
    assert result.deck.diarization == without_counters(fresh.deck.diarization)
    assert (result.deck.diarization.range_start, result.deck.diarization.range_end) == (600.0, 1500.0)
    assert result.deck.source_note == fresh.deck.source_note


def test_another_range_never_reuses_the_saved_stages():
    """攔的 bug：換了區間卻沿用上一段的聲音區分與字——時間與說話者都是另一段的。"""
    parts = long_video(renderer=FlakyRenderer())
    with pytest.raises(RuntimeError):
        run(parts, time_range=RANGE)

    other = TimeRange(100.0, 700.0)
    result = run(parts, time_range=other)

    assert counts(parts) == (2, 2, 2)
    assert result.deck.slides[0].timestamp == pytest.approx(105.0)
    assert (result.deck.diarization.range_start, result.deck.diarization.range_end) == (100.0, 700.0)


@pytest.mark.parametrize("first,second", [(RANGE, None), (None, RANGE)])
def test_adding_or_dropping_the_range_redoes_the_listening_stages(first, second):
    parts = long_video(renderer=FlakyRenderer())
    with pytest.raises(RuntimeError):
        run(parts, time_range=first)
    run(parts, time_range=second)
    assert (len(parts.diarizer.calls), len(parts.words.calls)) == (2, 2)


def test_a_rerun_after_a_recognition_failure_keeps_the_shifted_diarization_and_shifts_the_new_words():
    """辨識失敗時，聲音區分（已經加回起點）存著；接著做時只重新切、重新辨識，字也要加回起點，兩邊的時間才對得上。"""
    parts = long_video(words=FakeWordTranscriber(error=SpeechRecognitionFailed("辨識失敗")))
    with pytest.raises(SpeechRecognitionFailed):
        run(parts, time_range=RANGE)
    parts.words.error = None

    result = run(parts, time_range=RANGE)

    assert (len(parts.diarizer.calls), len(parts.words.calls), len(parts.cutter.calls)) == (1, 2, 2)
    fresh = run(long_video(), time_range=RANGE)
    assert result.deck.slides == fresh.deck.slides and result.deck.speakers == fresh.deck.speakers
    assert result.deck.slides[0].timestamp == pytest.approx(605.0)


def test_the_saved_stages_hold_times_on_the_original_timeline():
    """存檔裡存的是加回起點之後的時間（與指紋裡的區間一起，才是一份完整的「這一段的聲音區分」）。"""
    parts = long_video(renderer=FlakyRenderer())
    with pytest.raises(RuntimeError):
        run(parts, time_range=RANGE)
    fingerprint = diarized_pipeline.diarization_fingerprint(parts.diarizer.fingerprint, None, RANGE)
    saved = parts.checkpoints.load_diarization("OUT", URL, fingerprint)
    assert saved is not None and saved.turns[0].start == pytest.approx(TURNS[0].start + 600.0)
    assert min(t.start for t in saved.turns) >= 600.0


def test_the_availability_check_and_the_speaker_count_are_unchanged_by_a_range():
    parts = long_video()
    run(parts, time_range=RANGE, speaker_count=3, speech_hint="少子化")
    assert parts.diarizer.availability_checks == 1
    assert parts.diarizer.calls == [(CUT_FILE, 900.0, 3)]
    assert parts.words.hints == ["少子化"]


def test_a_diarizer_that_finds_too_many_voices_in_the_range_is_judged_like_any_other_job():
    """區間不放寬任何門檻：切出來的那一段裡聲音太多，一樣失敗（這是區間存在的理由——前後的採訪不在裡面）。"""
    parts = crowd_parts(22, audio=FakeAudioGateway(clip=LONG_CLIP))
    with pytest.raises(AttributionUnreliable, match="上限 24 個"):
        run(parts, time_range=RANGE)
    assert parts.conversation.calls == [] and stored(parts) == ["diarization", "words"]


def test_the_diarizer_fake_used_here_is_the_shared_one():
    """防呆：這個檔案的測試依賴假零件「照舊回傳從 0 起算的時間」；那個假設變了，這裡要跟著改。"""
    assert FakeDiarizer().output.turns[0].start < 30.0
