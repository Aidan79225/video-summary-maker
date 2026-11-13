"""多人模式（speaker_mode="diarize"）的整條流程：全部用假零件，不碰模型、網路或磁碟。

最重要的兩件事：說話者永遠是程式從被引用的那一段查出來的；不可靠就失敗、什麼都不發佈，
絕不退回單人的「講者」寫法。
"""
from __future__ import annotations

import dataclasses
import os

import pytest

from slidebox.domain.entities import DiarizerOutput, RawPoint, RawSlide, SpeakerTurn
from slidebox.domain.errors import (
    AttributionUnreliable,
    DiarizationUnavailable,
    NoSubtitlesAvailable,
    SpeechRecognitionFailed,
    SummarizerOutputInvalid,
    SummarizerUnavailable,
)
from slidebox.usecases import diarized_pipeline
from slidebox.usecases.conversation import chunk_turns, format_window
from slidebox.usecases.naming import deck_folder_name
from slidebox.usecases.speakers import transcript_lines

from .diarized_support import (
    CLIP,
    FIXTURE,
    SPEAKERS,
    SPLIT_AT,
    TURNS,
    TWO_WINDOWS,
    URL,
    FakeConversation,
    FakeDiarizer,
    FakeWordTranscriber,
    FlakyRenderer,
    at,
    good_points,
    good_reply,
    make,
    run,
    settings,
)
from .fakes import FakeAudioGateway


def stored(parts):
    return parts.checkpoints.stages("OUT", URL)


def without_counters(report):
    """沿用存檔時沒有重新檢查，report 裡內部用的計數（原話、丟掉的分類）是空的；其餘都要一樣。"""
    return dataclasses.replace(report, rescued_points=None, contradicted_points=None,
                               unverifiable_points=None, anchor_conflicts=None)


def bad(turn: int, text: str = "他認為補助應該加倍") -> RawPoint:
    """開頭是代名詞：一定被檢查丟掉。"""
    return RawPoint(turn, text)


# --- 一條成功的路 ---


def test_a_diarized_job_publishes_points_attributed_by_the_program():
    """攔的 bug：說話者是模型說的，或標在錯的人身上。這裡每個重點的說話者都要等於它引用的那一段的說話者。"""
    parts = make()
    result = run(parts)
    deck = result.deck
    by_index = {t.index: t for t in TURNS}
    assert deck.speaker_mode == "diarize" and deck.brief is None
    assert [len(s.points) for s in deck.slides] == [4, 3]            # 七個重點，每頁最多四個
    for slide in deck.slides:
        for point in slide.points:
            assert point.speaker == by_index[point.turn].speaker
            assert point.start == by_index[point.turn].start
    assert deck.slides[0].bullets[0] == "[[S1]]：詢問目前補助政策的看法"
    assert deck.slides[0].bullets[1] == "[[S2]]：現金補助有幫助，但托育資源更重要"
    assert deck.slides[0].timestamp == 5.0 and deck.slides[0].index == 1


def test_the_deck_carries_the_speaker_list_the_report_and_the_transcript():
    parts = make()
    deck = run(parts).deck
    assert [(s.id, s.label, s.kind) for s in deck.speakers] == [
        ("S1", "發言者 A", "participant"), ("S2", "發言者 B", "participant"), ("S3", "其他聲音 1", "minor")]
    report = deck.diarization
    assert (report.engine, report.version, report.model, report.fingerprint) == (
        "fake-engine", "1.0", "fake/model", parts.diarizer.fingerprint)
    assert (report.status, report.warnings, report.requested_speakers) == ("ok", (), None)
    assert (report.found_speakers, report.minor_speakers) == (2, 1)
    assert (report.proposed_points, report.kept_points) == (7, 7)
    assert (report.rescued_points, report.contradicted_points,
            report.unverifiable_points, report.anchor_conflicts) == (0, 0, 0, 0)
    assert deck.transcript_text == transcript_lines(TURNS)
    assert deck.source_note == diarized_pipeline.SOURCE_NOTE
    assert deck.video_title == CLIP.title


def test_the_overlap_the_diarizer_measured_reaches_the_report():
    quiet = run(make()).deck.diarization
    loud = run(make(diarizer=FakeDiarizer(output=DiarizerOutput(FIXTURE.turns, 6.0)))).deck.diarization
    assert quiet.overlap_ratio == 0.0 and loud.overlap_ratio > 0.0


def test_the_transcript_is_published_even_when_the_job_is_not_detailed():
    """Pi 要靠逐字稿核對每個重點出自哪一段；不是詳細模式也一樣。"""
    deck = run(make(), settings(detailed=False)).deck
    assert deck.transcript_text.startswith("00:05 [[S1]]：今天我們來談少子化")


def test_the_rest_of_the_pipeline_is_shared_with_the_single_mode_path():
    parts = make()
    result = run(parts)
    assert parts.sections.timestamps == [s.timestamp for s in result.deck.slides]
    assert len(parts.frames.calls) == len(result.deck.slides)
    folder = deck_folder_name(CLIP.title, CLIP.video_id)
    html = os.path.join("OUT", folder, "slides.html")
    assert parts.renderer.rendered == [(result.deck, html)] and result.html_path == html
    assert parts.sections.cleaned == [os.path.join("OUT", folder, "_clips")]


def test_nothing_from_the_single_speaker_path_is_used():
    """攔的 bug：多人模式先去抓字幕、或把結果交給單人的摘要器／質詢卡／第一人稱改寫。"""
    parts = make()
    run(parts, settings(detailed=True))
    assert parts.subtitles.calls == []
    assert parts.summarizer.compressed == []
    assert parts.brief_writer.calls == 0
    assert parts.rewriter.calls == []


def test_the_speech_hint_goes_to_the_recognizer_and_the_speaker_count_to_the_diarizer():
    parts = make()
    run(parts, speaker_count=3, speech_hint="少子化 對談")
    assert parts.words.hints == ["少子化 對談"]
    assert parts.diarizer.calls == [(CLIP.path, CLIP.duration, 3)]


def test_the_final_status_reports_the_page_count_and_the_source_note():
    seen = []
    run(make(), progress=lambda frac, status: seen.append((frac, status)))
    frac, status = seen[-1]
    assert frac == 1.0 and status.startswith("完成，共 2 頁") and diarized_pipeline.SOURCE_NOTE in status


def test_the_progress_bar_never_goes_backwards():
    """攔的 bug：語音辨識、說話者區分的進度直接丟給進度條，接著摘要從 5% 開始，條子就倒退。"""
    seen = []
    run(make(TWO_WINDOWS), progress=lambda frac, status: seen.append(frac))
    fractions = [f for f in seen if f is not None]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0 and len(fractions) > 3


def test_without_checkpoints_the_job_still_runs():
    parts = make(checkpoints=None)
    assert run(parts).deck.slides


# --- 開工之前：能不能做 ---


def test_an_unavailable_diarizer_fails_before_anything_is_downloaded():
    """攔的 bug：先花幾分鐘下載音訊，才發現沒有 token。"""
    parts = make(diarizer=FakeDiarizer(usable=False, reason="沒有設定 HUGGINGFACE_TOKEN"))
    with pytest.raises(DiarizationUnavailable, match="HUGGINGFACE_TOKEN"):
        run(parts)
    assert parts.audio.events == [] and parts.diarizer.calls == [] and parts.conversation.calls == []
    assert stored(parts) == [] and parts.renderer.rendered == []


def test_an_unavailable_diarizer_without_a_reason_still_fails_with_a_message():
    with pytest.raises(DiarizationUnavailable, match="說話者區分"):
        run(make(diarizer=FakeDiarizer(usable=False)))


@pytest.mark.parametrize("missing", ["audio", "diarizer", "words", "conversation"])
def test_a_missing_component_fails_loudly_instead_of_degrading_to_single_mode(missing):
    parts = make(**{missing: None})
    with pytest.raises(DiarizationUnavailable):
        run(parts)
    assert parts.subtitles.calls == [] and parts.summarizer.compressed == []
    assert parts.renderer.rendered == [] and stored(parts) == []


@pytest.mark.parametrize("count", [1, 0, -2, True, "2", 2.0])
def test_a_speaker_count_that_is_not_two_or_more_is_refused_before_anything_runs(count):
    parts = make()
    with pytest.raises(ValueError, match="speaker_count"):
        run(parts, speaker_count=count)
    assert parts.diarizer.availability_checks == 0 and parts.audio.events == []


def test_the_availability_check_is_made_once_per_job():
    parts = make()
    run(parts)
    assert parts.diarizer.availability_checks == 1


# --- 音訊 ---


def test_the_audio_is_downloaded_once_and_shared_by_both_recognizers():
    parts = make()
    run(parts)
    assert parts.audio.events == ["cleanup", "download", "cleanup"]
    assert parts.diarizer.calls[0][:2] == (CLIP.path, CLIP.duration)
    assert parts.words.calls == [(CLIP.path, CLIP.duration)]
    assert parts.audio.dest_dirs == [os.path.join("OUT", ".slidebox_audio_tmp")]


@pytest.mark.parametrize("where", ["diarize", "words", "empty", "conversation", "attribution", "render"])
def test_the_audio_is_cleaned_up_whatever_fails(where):
    """攔的 bug：失敗時留下幾百 MB 的音訊檔，下一次的 yt-dlp 續傳把兩支影片的位元組拼在一起。"""
    kwargs = {
        "diarize": dict(diarizer=FakeDiarizer(error=DiarizationUnavailable("模型載入失敗"))),
        "words": dict(words=FakeWordTranscriber(error=SpeechRecognitionFailed("辨識失敗"))),
        "empty": dict(words=FakeWordTranscriber(words=())),
        "conversation": dict(conversation=FakeConversation([SummarizerUnavailable("連不上 Ollama")])),
        "attribution": dict(diarizer=FakeDiarizer(output=DiarizerOutput((SpeakerTurn(0.0, 80.0, "A"),), 0.0))),
        "render": dict(renderer=FlakyRenderer()),
    }[where]
    parts = make(**kwargs)
    with pytest.raises(Exception):
        run(parts, speaker_count=2 if where == "attribution" else None)
    assert parts.audio.events[0] == "cleanup" and parts.audio.events[-1] == "cleanup"
    assert parts.audio.events.count("download") == 1


def test_a_failed_audio_download_is_reported_as_a_speech_recognition_failure():
    """攔的 bug：音訊 gateway 的訊息是為「沒有字幕之後」寫的，開頭就是「這部影片沒有字幕」，
    對一支根本沒去找過字幕的多人工作是誤導。"""
    error = NoSubtitlesAvailable("這部影片沒有字幕，音訊也下載失敗：HTTP 403")
    parts = make(audio=FakeAudioGateway(clip=CLIP, error=error))
    with pytest.raises(SpeechRecognitionFailed) as raised:
        run(parts)
    assert "沒有字幕" not in str(raised.value) and "HTTP 403" in str(raised.value)
    assert parts.diarizer.calls == [] and parts.audio.events[-1] == "cleanup"


# --- 說話者區分與語音辨識的失敗 ---


def test_a_diarizer_failure_stops_the_job_and_nothing_is_published():
    parts = make(diarizer=FakeDiarizer(error=DiarizationUnavailable("說話者區分失敗（CUDA out of memory）")))
    with pytest.raises(DiarizationUnavailable, match="CUDA out of memory"):
        run(parts)
    assert parts.words.calls == [] and parts.conversation.calls == []
    assert parts.renderer.rendered == [] and stored(parts) == []


def test_a_recognition_failure_keeps_the_finished_diarization():
    parts = make(words=FakeWordTranscriber(error=SpeechRecognitionFailed("語音辨識失敗：模型載入失敗")))
    with pytest.raises(SpeechRecognitionFailed, match="模型載入失敗"):
        run(parts)
    assert stored(parts) == ["diarization"] and parts.conversation.calls == []


def test_no_speech_at_all_fails_and_the_empty_result_is_not_remembered():
    """攔的 bug：沒偵測到語音的空結果被存下來，下次直接沿用，永遠做不出東西。"""
    parts = make(words=FakeWordTranscriber(words=()))
    with pytest.raises(SpeechRecognitionFailed, match="沒有偵測到語音"):
        run(parts)
    assert stored(parts) == ["diarization"]
    assert parts.renderer.rendered == []


# --- 存檔與接著做 ---


def test_a_successful_job_clears_its_checkpoints():
    parts = make()
    run(parts)
    assert parts.checkpoints.save_log == ["diarization", "words", "conversation"]
    assert parts.checkpoints.cleared == [("OUT", URL)] and stored(parts) == []


def test_each_finished_stage_is_saved_right_away():
    parts = make(conversation=FakeConversation([SummarizerUnavailable("連不上 Ollama")]))
    with pytest.raises(SummarizerUnavailable):
        run(parts)
    assert stored(parts) == ["diarization", "words"] and parts.checkpoints.cleared == []


def test_a_rerun_after_a_recognition_failure_does_not_diarize_again():
    parts = make(words=FakeWordTranscriber(error=SpeechRecognitionFailed("辨識失敗")))
    with pytest.raises(SpeechRecognitionFailed):
        run(parts)
    parts.words.error = None
    assert run(parts).deck.slides
    assert len(parts.diarizer.calls) == 1 and len(parts.words.calls) == 2
    assert parts.audio.events.count("download") == 2


def test_a_rerun_after_the_last_step_failed_reuses_everything_and_downloads_no_audio():
    parts = make(TWO_WINDOWS, renderer=FlakyRenderer())
    with pytest.raises(RuntimeError):
        run(parts)
    assert stored(parts) == ["conversation", "diarization", "words"]
    result = run(parts)
    assert parts.audio.events.count("download") == 1
    assert len(parts.diarizer.calls) == 1 and len(parts.words.calls) == 1
    assert len(parts.conversation.calls) == 2                       # 第一次的兩個窗，第二次一個都沒再問
    fresh = run(make(TWO_WINDOWS))
    assert result.deck.slides == fresh.deck.slides
    assert result.deck.diarization == without_counters(fresh.deck.diarization)
    assert result.deck.speakers == fresh.deck.speakers


def test_a_resumed_job_reports_the_same_counts_as_a_fresh_one():
    """攔的 bug：沿用存下來的摘要時，report 的 proposed_points 變成 0，或 kept 變成別的數字。"""
    one_in_five_bad = (RawSlide("補助與托育", (*good_points(0, 3), bad(5))),)     # 20%，不用重問
    parts = make(renderer=FlakyRenderer(), conversation=FakeConversation([one_in_five_bad]))
    with pytest.raises(RuntimeError):
        run(parts)
    fresh = run(make(conversation=FakeConversation([one_in_five_bad]))).deck.diarization
    resumed = run(parts).deck.diarization
    assert (fresh.proposed_points, fresh.kept_points) == (5, 4)
    assert (fresh.rescued_points, fresh.contradicted_points,
            fresh.unverifiable_points, fresh.anchor_conflicts) == (0, 1, 0, 0)
    assert resumed == without_counters(fresh) and len(parts.conversation.calls) == 1
    assert (resumed.rescued_points, resumed.contradicted_points) == (None, None)      # 沒有重新檢查，不亂填


def mixed_reply(call):
    """10 個重點：7 個通過、1 個用字對不上改顯示原話、1 個 quote 是編出來的、1 個 quote 在甲、內容卻是乙說的。"""
    good = good_points()
    conflict = RawPoint(at("不是，是要"), "名額不夠，家長排了兩年也沒有消息", quote="不是，是要把名額加倍")
    invented = RawPoint(at("謝謝邱委員"), "詢問預算的編列方式", quote="這是一句逐字稿裡根本沒有的話啊啊啊")
    quoted = RawPoint(at("預算大約"), "完全不同的主題在這裡亂寫一通", quote="至少要把公共托育的名額加倍")
    return (RawSlide("補助", (*good[0:3], conflict)), RawSlide("托育", (*good[3:6], invented)),
            RawSlide("預算", (good[6], quoted)))


def test_the_report_and_the_progress_log_say_what_was_quoted_and_what_was_dropped():
    """攔的 bug：顯示原話的點與三類丟掉的點只在內部算，沒有進報告、也沒有記進進度紀錄——
    事後看不出這份摘要的歸屬有多可靠。"""
    seen = []
    deck = run(make(conversation=FakeConversation([mixed_reply])), progress=lambda frac, status: seen.append(status)).deck
    report = deck.diarization

    assert (report.proposed_points, report.kept_points) == (10, 8)
    assert (report.rescued_points, report.contradicted_points,
            report.unverifiable_points, report.anchor_conflicts) == (1, 1, 1, 1)
    assert ("對談重點：模型提出 10 個，留下 8 個（其中 1 個直接引用原話）；"
            "丟掉 2 個（有問題 1、查不出來 1、同頁規則 0；內容出自別輪 1）") in seen
    assert [p.text for s in deck.slides for p in s.points].count("「至少要把公共托育的名額加倍」") == 1


def test_the_log_line_is_written_even_when_nothing_was_dropped():
    seen = []
    run(make(), progress=lambda frac, status: seen.append(status))
    assert "對談重點：模型提出 7 個，留下 7 個" in seen


def test_the_safe_mode_publishes_every_point_as_a_verified_quote():
    """安全模式：成品裡的每個重點都是逐字稿裡那一輪的原話，說話者是那一輪的人，報告裡也說得出來。"""
    deck = run(make(quote_only=True)).deck
    by_index = {t.index: t for t in TURNS}
    points = [p for slide in deck.slides for p in slide.points]

    assert len(points) == 7
    for p in points:
        assert p.text.startswith("「") and p.text.endswith("」")
        assert p.text[1:-1] in " ".join(by_index[p.turn].text.split())
        assert p.speaker == by_index[p.turn].speaker
    assert (deck.diarization.rescued_points, deck.diarization.kept_points) == (7, 7)


def test_the_safe_mode_and_the_normal_mode_keep_the_same_speakers_and_turns():
    normal = [(p.speaker, p.turn) for s in run(make()).deck.slides for p in s.points]
    quoted = [(p.speaker, p.turn) for s in run(make(quote_only=True)).deck.slides for p in s.points]
    assert normal == quoted


CHANGES = [
    # 名稱, 換掉什麼, (說話者區分重做, 語音辨識重做, 摘要重做)
    ("diarizer version", lambda p: setattr(p.diarizer, "version", "2.0"), (True, False, False)),
    ("speaker count", lambda p: ("speaker_count", 2), (True, False, False)),
    ("whisper model", lambda p: ("cfg", settings(whisper_model="whisper-b")), (False, True, False)),
    ("speech hint", lambda p: ("speech_hint", "新的提示"), (False, True, False)),
    ("chunk size", lambda p: setattr(p.usecase, "_chunk_chars", TWO_WINDOWS), (False, False, True)),
    ("chunk size, same windows", lambda p: setattr(p.usecase, "_chunk_chars", 7000), (False, False, True)),
    ("language model", lambda p: ("cfg", settings(model="qwen-b")), (False, False, True)),
    ("min slides", lambda p: ("cfg", settings(min_slides=3)), (False, False, True)),
    ("max slides", lambda p: ("cfg", settings(max_slides=5)), (False, False, True)),
    ("context size", lambda p: ("cfg", settings(num_ctx=16384)), (False, False, True)),
    ("detailed flag", lambda p: ("cfg", settings(detailed=True)), (False, False, False)),
    ("prompt version", lambda p: setattr(diarized_pipeline, "PROMPT_VERSION", 99), (False, False, True)),
    ("safe mode", lambda p: setattr(p.usecase, "_quote_only", True), (False, False, True)),
    ("align version", lambda p: setattr(diarized_pipeline, "ALIGN_VERSION", 99), (False, True, False)),
]


@pytest.mark.parametrize("name,change,expected", CHANGES, ids=[c[0] for c in CHANGES])
def test_a_stage_is_redone_only_when_its_own_settings_changed(monkeypatch, name, change, expected):
    """攔的 bug：換了模型、人數或提示卻沿用舊的結果（冒充），或什麼都沒換卻整個重做（浪費）。"""
    monkeypatch.setattr(diarized_pipeline, "PROMPT_VERSION", diarized_pipeline.PROMPT_VERSION)
    monkeypatch.setattr(diarized_pipeline, "ALIGN_VERSION", diarized_pipeline.ALIGN_VERSION)
    parts = make(renderer=FlakyRenderer())
    with pytest.raises(RuntimeError):
        run(parts)
    before = (len(parts.diarizer.calls), len(parts.words.calls), len(parts.conversation.calls))
    kwargs = {}
    outcome = change(parts)
    if isinstance(outcome, tuple):
        kwargs[outcome[0]] = outcome[1]
    run(parts, **kwargs)
    after = (len(parts.diarizer.calls), len(parts.words.calls), len(parts.conversation.calls))
    assert tuple(a > b for a, b in zip(after, before)) == expected


def test_a_different_diarization_with_the_same_settings_never_reuses_the_old_summary():
    """攔的 bug：說話者區分換了（升版、結果不同），模型看到的對談已經不一樣，卻沿用舊的摘要——
    舊重點裡寫的是誰說的，已經對不上。這個指紋靠「餵給模型的每個窗」把關。"""
    parts = make(renderer=FlakyRenderer())
    with pytest.raises(RuntimeError):
        run(parts)
    caller_is_the_guest = tuple(
        SpeakerTurn(t.start, t.end, "SPEAKER_01") if t.speaker == "SPEAKER_02" else t for t in FIXTURE.turns)
    parts.diarizer.version = "2.0"
    parts.diarizer.output = DiarizerOutput(caller_is_the_guest, 0.0)
    result = run(parts)
    first, second = parts.conversation.calls
    assert first.window_text != second.window_text
    assert (result.deck.diarization.found_speakers, result.deck.diarization.minor_speakers) == (2, 0)


# --- 窗與模型 ---


def test_the_conversation_is_summarized_one_window_at_a_time_with_the_allocated_page_range():
    parts = make(TWO_WINDOWS)
    run(parts, settings(min_slides=2, max_slides=6))
    first, second = parts.conversation.calls
    assert (first.first_turn, first.last_turn) == (0, SPLIT_AT - 1)
    assert (second.first_turn, second.last_turn) == (SPLIT_AT, TURNS[-1].index)
    windows = chunk_turns(TURNS, SPEAKERS, TWO_WINDOWS)
    assert [c.window_text for c in parts.conversation.calls] == [format_window(w, SPEAKERS) for w in windows]
    assert first.max_slides + second.max_slides == 6 and first.min_slides + second.min_slides == 2
    assert first.hint == "" and second.hint == ""


def test_windows_are_put_back_in_conversation_order_and_renumbered():
    """攔的 bug：頁的順序跟著模型回傳的順序走，或各窗各自從 1 編號。"""
    late = (RawSlide("後面", good_points(SPLIT_AT)),)
    early_then_later = lambda call: (  # noqa: E731
        RawSlide("後半", good_points(2, 3)), RawSlide("前半", good_points(0, 1)))
    parts = make(TWO_WINDOWS, conversation=FakeConversation([early_then_later, late]))
    slides = run(parts).deck.slides
    assert [(s.index, s.title) for s in slides] == [(1, "前半"), (2, "後半"), (3, "後面")]
    assert [s.timestamp for s in slides] == sorted(s.timestamp for s in slides)


def test_a_quarter_dropped_in_a_window_does_not_cost_a_second_attempt():
    """門檻是「超過 25%」才重問：4 個重點丟掉 1 個，剛好 25%，這個窗只問一次。"""
    reply = (RawSlide("補助與托育", (*good_points(0, 2), bad(3))),)
    parts = make(TWO_WINDOWS, conversation=FakeConversation([reply, good_reply]))
    run(parts)
    assert len(parts.conversation.calls) == 2                    # 第一個窗一次、第二個窗一次


def test_a_third_dropped_in_a_window_is_asked_again():
    reply = (RawSlide("補助與托育", (*good_points(0, 3), bad(4), bad(5))),)
    parts = make(TWO_WINDOWS, conversation=FakeConversation([reply, good_reply, good_reply]))
    run(parts)
    assert len(parts.conversation.calls) == 3


def test_a_window_with_too_many_dropped_points_is_asked_again_with_the_reasons():
    first = (RawSlide("補助與托育", (*good_points(0, 1), bad(at("現金補助")), bad(at("那公共托育")))),)
    parts = make(TWO_WINDOWS, conversation=FakeConversation([first, good_reply, good_reply]))
    deck = run(parts).deck
    calls = parts.conversation.calls
    assert len(calls) == 3 and calls[0].hint == "" and calls[2].hint == ""
    assert calls[1].hint.startswith("上一次的結果有這些問題") and "他" in calls[1].hint
    assert (deck.diarization.proposed_points, deck.diarization.kept_points) == (7, 7)


def test_the_better_of_the_two_attempts_is_kept():
    worse_second = (RawSlide("補助與托育", (*good_points(0, 1), bad(2), bad(3), bad(4))),)
    better_first = (RawSlide("補助與托育", (*good_points(0, 2), bad(3), bad(4))),)
    parts = make(TWO_WINDOWS, conversation=FakeConversation([better_first, worse_second, good_reply]))
    deck = run(parts).deck
    first_window = [p.turn for p in deck.slides[0].points]
    assert first_window == [0, 1, 2]                                  # 第一次留下 3 個，第二次只留下 2 個
    assert (deck.diarization.proposed_points, deck.diarization.kept_points) == (8, 6)


def test_an_attempt_that_passes_beats_one_that_kept_more_but_failed():
    """攔的 bug：「過關就停」停在第二次，最後卻選了第一次那份被丟掉四成重點的結果，整份因此被判不可靠。"""
    four_good_three_bad = (RawSlide("補助與托育", (*good_points(0, 3), bad(4), bad(5), bad(6))),)
    three_good = (RawSlide("補助與托育", good_points(0, 2)),)
    parts = make(TWO_WINDOWS, conversation=FakeConversation([four_good_three_bad, three_good, good_reply]))
    deck = run(parts).deck
    assert [p.turn for p in deck.slides[0].points] == [0, 1, 2]
    assert (deck.diarization.proposed_points, deck.diarization.kept_points) == (6, 6)


def test_when_neither_attempt_passes_the_one_that_kept_more_is_used():
    fewer = (RawSlide("補助與托育", (*good_points(0, 1), bad(2), bad(3), bad(4))),)
    more = (RawSlide("補助與托育", (*good_points(0, 2), bad(3), bad(4))),)
    parts = make(TWO_WINDOWS, conversation=FakeConversation([fewer, more, good_reply]))
    assert [p.turn for p in run(parts).deck.slides[0].points] == [0, 1, 2]


def test_when_both_attempts_keep_the_same_number_the_one_with_fewer_dropped_wins():
    more_dropped = (RawSlide("第一次", (*good_points(0, 2), bad(3), bad(4), bad(5))),)
    fewer_dropped = (RawSlide("第二次", (*good_points(0, 2), bad(3), bad(4))),)
    parts = make(TWO_WINDOWS, conversation=FakeConversation([more_dropped, fewer_dropped, good_reply]))
    assert run(parts).deck.slides[0].title == "第二次"


def test_when_both_attempts_are_equally_good_the_first_one_is_used():
    first = (RawSlide("第一次", (*good_points(0, 2), bad(3), bad(4))),)
    second = (RawSlide("第二次", (*good_points(0, 2), bad(3), bad(4))),)
    parts = make(TWO_WINDOWS, conversation=FakeConversation([first, second, good_reply]))
    assert run(parts).deck.slides[0].title == "第一次"


def test_a_malformed_reply_is_retried_with_the_error_as_the_hint():
    parts = make(conversation=FakeConversation([SummarizerOutputInvalid("模型回應不是合法的 JSON"), good_reply]))
    run(parts)
    assert len(parts.conversation.calls) == 2
    assert "模型回應不是合法的 JSON" in parts.conversation.calls[1].hint


def test_two_malformed_replies_fail_the_job_and_name_the_window():
    parts = make(TWO_WINDOWS, conversation=FakeConversation([good_reply, SummarizerOutputInvalid("不是 JSON"),
                                                            SummarizerOutputInvalid("還是不是 JSON")]))
    with pytest.raises(SummarizerOutputInvalid, match="第 2/2 段.*重試後仍不符合要求"):
        run(parts)
    assert parts.renderer.rendered == [] and "conversation" not in stored(parts)


def test_a_malformed_second_reply_keeps_the_first_attempts_result():
    first = (RawSlide("補助與托育", (*good_points(0, 2), bad(3), bad(4))),)
    parts = make(TWO_WINDOWS, conversation=FakeConversation([first, SummarizerOutputInvalid("壞掉"), good_reply]))
    deck = run(parts).deck
    assert [p.turn for p in deck.slides[0].points] == [0, 1, 2]


def test_an_unreachable_model_is_not_retried_and_nothing_is_published():
    parts = make(conversation=FakeConversation([SummarizerUnavailable("連不上 Ollama")]))
    with pytest.raises(SummarizerUnavailable, match="連不上 Ollama"):
        run(parts)
    assert len(parts.conversation.calls) == 1
    assert parts.renderer.rendered == [] and parts.audio.events[-1] == "cleanup"


def test_pages_beyond_the_window_limit_are_dropped_without_counting_as_failed_points():
    """攔的 bug：模型多寫幾頁，每頁都去抓影片片段、截圖；或這些多餘的頁被當成「被檢查丟掉」而讓整份失敗。"""
    four_pages = lambda call: tuple(  # noqa: E731
        RawSlide(f"第 {i} 頁", good_points(0, 1)) for i in range(1, 5))
    seen = []
    parts = make(conversation=FakeConversation(default=four_pages))
    deck = run(parts, settings(min_slides=1, max_slides=2), progress=lambda f, s: seen.append(s)).deck
    assert [s.title for s in deck.slides] == ["第 1 頁", "第 2 頁"]
    assert (deck.diarization.proposed_points, deck.diarization.kept_points) == (4, 4)
    assert any("超出頁數上限" in status for status in seen)
    assert len(parts.sections.timestamps) == 2


# --- 歸屬不可靠：失敗，什麼都不發佈 ---


def test_a_window_without_a_single_surviving_page_fails_the_job_with_counts():
    nothing = (RawSlide("亂寫", (RawPoint(999, "不存在的段落"), bad(at("現金補助")))),)
    parts = make(TWO_WINDOWS, conversation=FakeConversation([good_reply, nothing, nothing]))
    with pytest.raises(AttributionUnreliable) as raised:
        run(parts)
    message = str(raised.value)
    assert "第 2/2 段" in message and f"T{SPLIT_AT}–T{TURNS[-1].index}" in message
    assert "提出 2 個重點、留下 0 個" in message
    assert parts.renderer.rendered == [] and parts.sections.timestamps == []
    assert "conversation" not in stored(parts) and parts.checkpoints.cleared == []


def test_too_many_dropped_points_overall_fail_the_job_with_counts():
    mostly_bad = (RawSlide("補助與托育", (*good_points(0, 1), bad(2), bad(3), bad(4))),)
    parts = make(TWO_WINDOWS, conversation=FakeConversation([mostly_bad, mostly_bad, good_reply]))
    with pytest.raises(AttributionUnreliable) as raised:
        run(parts)
    message = str(raised.value)
    assert "8 個重點" in message and "3 個沒有通過" in message and "38%" in message and "25%" in message
    assert parts.renderer.rendered == [] and "conversation" not in stored(parts)


def test_exactly_a_quarter_dropped_is_still_published():
    """門檻是「超過 25%」：8 個重點丟掉 2 個，剛好 25%，照常出片。"""
    window_one = (RawSlide("補助與托育", (*good_points(0, 2), bad(3), bad(4))),)
    parts = make(TWO_WINDOWS, conversation=FakeConversation([window_one, window_one, good_reply]))
    deck = run(parts).deck
    assert (deck.diarization.proposed_points, deck.diarization.kept_points) == (8, 6)


def test_a_little_over_a_quarter_dropped_fails_the_job():
    """7 個重點丟掉 2 個（29%）：超過上限，不發佈。"""
    window_one = (RawSlide("補助與托育", (*good_points(0, 1), bad(2), bad(3))),)
    parts = make(TWO_WINDOWS, conversation=FakeConversation([window_one, window_one, good_reply]))
    with pytest.raises(AttributionUnreliable, match="7 個重點.*2 個沒有通過.*29%"):
        run(parts)
    assert parts.renderer.rendered == [] and "conversation" not in stored(parts)


def test_an_unreliable_diarization_fails_before_any_model_call():
    """要求 2 人卻只找到 1 位主要發言者：可能把不同的人合併了，不能往下寫。"""
    one_voice = DiarizerOutput((SpeakerTurn(0.0, 80.0, "SPEAKER_00"),), 0.0)
    parts = make(diarizer=FakeDiarizer(output=one_voice))
    with pytest.raises(AttributionUnreliable, match="指定 2 人.*只找到 1 位"):
        run(parts, speaker_count=2)
    assert parts.conversation.calls == [] and parts.renderer.rendered == []
    assert parts.audio.events[-1] == "cleanup"


def test_a_diarizer_that_finds_a_different_count_than_asked_publishes_with_a_warning():
    parts = make()
    deck = run(parts, speaker_count=3).deck
    assert deck.diarization.status == "warn" and deck.diarization.warnings == ("count_mismatch",)
    assert deck.diarization.requested_speakers == 3
    assert deck.source_note.startswith(diarized_pipeline.SOURCE_NOTE)
    assert "指定 3 人，實際找到 2 位主要發言者" in deck.source_note
