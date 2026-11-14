"""多人模式的取消：每個耗時的步驟之前都要檢查，取消後音訊清掉、做完的存檔留著、什麼都不發佈。

「adapter 自己會丟 OperationCancelled」的那幾步（下載、辨識、摘要）由假零件模擬；
「adapter 照樣回傳」的那幾步（下載完才收到取消、存檔完才收到取消）靠 use case 自己檢查。
"""
from __future__ import annotations

import threading

import pytest

from slidebox.domain.errors import OperationCancelled

from .diarized_support import (
    CLIP,
    TWO_WINDOWS,
    URL,
    CancelDuring,
    FakeConversation,
    FakeDiarizer,
    FakeWordTranscriber,
    FlakyRenderer,
    QuietAudio,
    good_reply,
    make,
    run,
)
from .fakes import FakeAudioGateway

DIARIZATION, WORDS, CONVERSATION = "diarization", "words", "conversation"


def _first_window_then_cancel(flag):
    """第一個窗正常答完，答完的瞬間使用者按下取消。"""
    def reply(call):
        flag.set()
        return good_reply(call)
    return reply


# 名稱 -> (怎麼在那一刻按下取消, 說話者區分幾次, 語音辨識幾次, 對談摘要幾次, 存下來的階段)
POINTS = {
    "while loading saved results": (lambda f: dict(checkpoints=CancelDuring("load_words", f)), 0, 0, 0, []),
    "during the download": (lambda f: dict(audio=FakeAudioGateway(clip=CLIP, on_download=f.set)), 0, 0, 0, []),
    "right after the download": (lambda f: dict(audio=QuietAudio(on_download=f.set)), 0, 0, 0, []),
    "during diarization": (lambda f: dict(diarizer=FakeDiarizer(on_diarize=f.set)), 1, 0, 0, []),
    "right after diarization": (lambda f: dict(checkpoints=CancelDuring("save_diarization", f)),
                                1, 0, 0, [DIARIZATION]),
    "during recognition": (lambda f: dict(words=FakeWordTranscriber(on_transcribe=f.set)),
                           1, 1, 0, [DIARIZATION]),
    "right after recognition": (lambda f: dict(checkpoints=CancelDuring("save_words", f)),
                                1, 1, 0, [DIARIZATION, WORDS]),
    "during a window": (lambda f: dict(conversation=FakeConversation(on_call=lambda call: f.set())),
                        1, 1, 1, [DIARIZATION, WORDS]),
    "between windows": (lambda f: dict(conversation=FakeConversation([_first_window_then_cancel(f)])),
                        1, 1, 1, [DIARIZATION, WORDS]),
    "right after the summary": (lambda f: dict(checkpoints=CancelDuring("save_conversation", f)),
                                1, 1, 2, [CONVERSATION, DIARIZATION, WORDS]),
}


@pytest.mark.parametrize("point", list(POINTS))
def test_cancelling_at_any_step_stops_there_and_leaves_nothing_half_done(point):
    """攔的 bug：某一步沒有在動手之前檢查取消，按下取消後還繼續花幾分鐘的 GPU；
    或取消後留下音訊檔、半個存檔、或被清掉的存檔（下次得整個重來）。"""
    arrange, diarizations, recognitions, summaries, stages = POINTS[point]
    flag = threading.Event()
    parts = make(TWO_WINDOWS, **arrange(flag))
    with pytest.raises(OperationCancelled):
        run(parts, cancelled=flag.is_set)
    assert len(parts.diarizer.calls) == diarizations
    assert len(parts.words.calls) == recognitions
    assert len(parts.conversation.calls) == summaries
    assert parts.checkpoints.stages("OUT", URL) == stages
    assert parts.checkpoints.cleared == []                       # 取消不清存檔：下次接著做
    assert parts.renderer.rendered == [] and parts.sections.timestamps == []
    assert parts.audio.events[-1] == "cleanup"                   # 不管停在哪，音訊都清掉
    assert parts.audio.events.count("download") <= 1


def test_cancelling_while_loading_saved_results_downloads_nothing():
    flag = threading.Event()
    parts = make(checkpoints=CancelDuring("load_words", flag))
    with pytest.raises(OperationCancelled):
        run(parts, cancelled=flag.is_set)
    assert "download" not in parts.audio.events


def test_cancelling_before_anything_starts_downloads_nothing():
    parts = make()
    with pytest.raises(OperationCancelled):
        run(parts, cancelled=lambda: True)
    assert "download" not in parts.audio.events and parts.diarizer.calls == []
    assert parts.checkpoints.stages("OUT", URL) == []


def test_cancelling_while_resuming_does_no_further_work():
    """兩個階段都是沿用上次的：沒有任何耗時的步驟會自己檢查取消，use case 要在往下之前檢查。"""
    parts = make(TWO_WINDOWS, renderer=FlakyRenderer())
    with pytest.raises(RuntimeError):
        run(parts)
    summaries_before = len(parts.conversation.calls)
    flag = threading.Event()

    seen = []

    def cancel_when_resuming(frac, status):
        seen.append(status)
        if "沿用上次的語音辨識" in status:
            flag.set()

    with pytest.raises(OperationCancelled):
        run(parts, progress=cancel_when_resuming, cancelled=flag.is_set)
    assert seen[-1] == "沿用上次的語音辨識"                     # 注意到取消之後，什麼新的步驟都沒有再開始
    assert len(parts.conversation.calls) == summaries_before and parts.renderer.rendered == []
    assert parts.checkpoints.stages("OUT", URL) == [CONVERSATION, DIARIZATION, WORDS]
    assert parts.checkpoints.cleared == []


def test_a_cancelled_job_can_be_resumed_without_redoing_the_finished_steps():
    flag = threading.Event()
    parts = make(TWO_WINDOWS, checkpoints=CancelDuring("save_words", flag))
    with pytest.raises(OperationCancelled):
        run(parts, cancelled=flag.is_set)
    flag.clear()
    assert run(parts, cancelled=flag.is_set).deck.slides
    assert len(parts.diarizer.calls) == 1 and len(parts.words.calls) == 1
    assert parts.audio.events.count("download") == 1


def test_the_cancel_check_reaches_every_adapter():
    """攔的 bug：傳給 adapter 的是死的取消函式，adapter 在長時間的呼叫裡看不到取消。"""
    flag = threading.Event()
    audio = FakeAudioGateway(clip=CLIP, on_download=flag.set)
    with pytest.raises(OperationCancelled):
        run(make(audio=audio), cancelled=flag.is_set)
    assert audio.saw_cancel

    flag.clear()
    diarizer = FakeDiarizer(on_diarize=flag.set)
    with pytest.raises(OperationCancelled):
        run(make(diarizer=diarizer), cancelled=flag.is_set)
    assert diarizer.saw_cancel

    flag.clear()
    words = FakeWordTranscriber(on_transcribe=flag.set)
    with pytest.raises(OperationCancelled):
        run(make(words=words), cancelled=flag.is_set)
    assert words.saw_cancel

    flag.clear()
    conversation = FakeConversation(on_call=lambda call: flag.set())
    with pytest.raises(OperationCancelled):
        run(make(conversation=conversation), cancelled=flag.is_set)
    assert conversation.saw_cancel
