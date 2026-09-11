"""語音辨識 adapter：注入假模型，驗證 adapter 自己的邏輯而不必下載模型。"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from slidebox.domain.entities import Cue
from slidebox.domain.errors import NoSubtitlesAvailable, OperationCancelled
from slidebox.infrastructure.whisper_transcriber import FasterWhisperTranscriber


@dataclass
class Seg:
    """比照 faster_whisper.transcribe.Segment 會被讀到的欄位。"""
    start: float
    end: float
    text: str


@dataclass
class Info:
    language: str
    language_probability: float = 0.99
    duration: float = 0.0


class FakeModel:
    """transcribe 回傳惰性 generator，並記錄實際被取走幾段——真實的
    faster-whisper 也是邊迭代邊辨識，取消的價值就在於不必跑完。"""

    def __init__(self, segments, language="ja", error=None):
        self._segments = segments
        self._language = language
        self._error = error
        self.pulled = 0
        self.calls: list[tuple[str, dict]] = []

    def transcribe(self, path, **kwargs):
        self.calls.append((path, kwargs))
        if self._error is not None:
            raise self._error

        def gen():
            for s in self._segments:
                self.pulled += 1
                yield s

        return gen(), Info(self._language)


def _quiet(frac, status):
    pass


def _make(model, **kw):
    return FasterWhisperTranscriber("large-v3-turbo", model_factory=lambda name: model, **kw)


def test_segments_become_cues_and_the_language_is_returned():
    """攔的 bug：欄位對錯（start/end 顛倒）、文字沒去頭尾空白、語言沒回傳。"""
    model = FakeModel([Seg(0.0, 2.5, " 今日は "), Seg(2.5, 5.0, "よろしく")], language="ja")
    cues, lang = _make(model).transcribe("a.webm", 5.0, _quiet, lambda: False)
    assert cues == (Cue(0.0, 2.5, "今日は"), Cue(2.5, 5.0, "よろしく"))
    assert lang == "ja"


def test_blank_segments_are_dropped():
    """攔的 bug：空白段落變成空字幕，一路流進 compress_cues 與提示詞。"""
    model = FakeModel([Seg(0.0, 1.0, "   "), Seg(1.0, 2.0, "안녕하세요")])
    cues, _ = _make(model).transcribe("a.webm", 2.0, _quiet, lambda: False)
    assert cues == (Cue(1.0, 2.0, "안녕하세요"),)


def test_no_speech_returns_an_empty_tuple():
    """攔的 bug：沒有語音時拋例外或回 None。空 tuple 讓 use case 決定怎麼告知使用者。"""
    cues, _ = _make(FakeModel([])).transcribe("a.webm", 60.0, _quiet, lambda: False)
    assert cues == ()


def test_cancellation_stops_between_segments():
    """攔的 bug：迴圈裡沒檢查取消，辨識一路跑完——10 分鐘影片要等 2 分半。"""
    model = FakeModel([Seg(i, i + 1.0, f"段{i}") for i in range(10)])
    calls = {"n": 0}

    def cancel_after_two():
        calls["n"] += 1
        return calls["n"] > 2

    with pytest.raises(OperationCancelled):
        _make(model).transcribe("a.webm", 10.0, _quiet, cancel_after_two)
    assert model.pulled < 10


def test_the_model_is_loaded_once_and_reused():
    """攔的 bug：每次生成都重新載入模型。實測 large-v3-turbo 載入要 40 秒。"""
    built: list[str] = []

    def factory(name):
        built.append(name)
        return FakeModel([Seg(0.0, 1.0, "hi")], language="en")

    t = FasterWhisperTranscriber("large-v3-turbo", model_factory=factory)
    t.transcribe("a.webm", 1.0, _quiet, lambda: False)
    t.transcribe("b.webm", 1.0, _quiet, lambda: False)
    assert built == ["large-v3-turbo"]


def test_voice_activity_filter_is_requested():
    """攔的 bug：沒開 VAD。Whisper 在靜音或純音樂處會幻覺出「Thanks for watching」
    之類的句子，而那些句子會被寫進投影片。"""
    model = FakeModel([Seg(0.0, 1.0, "hi")])
    _make(model).transcribe("a.webm", 1.0, _quiet, lambda: False)
    assert model.calls[0][1].get("vad_filter") is True


def test_progress_is_reported_for_every_segment():
    """攔的 bug：辨識好幾分鐘卻沒有任何進度，使用者以為當機。"""
    seen: list[tuple[float | None, str]] = []
    model = FakeModel([Seg(0.0, 5.0, "a"), Seg(5.0, 19.0, "b")])
    _make(model).transcribe("a.webm", 60.0, lambda f, s: seen.append((f, s)), lambda: False)
    per_segment = [s for f, s in seen if "0:19" in s]
    assert per_segment, seen
    assert all(f is None for f, _ in seen)


def test_a_missing_library_becomes_a_readable_error():
    """攔的 bug：ImportError 原封不動冒到 UI，使用者不知道要執行 uv sync。"""

    def factory(name):
        raise ImportError("No module named 'faster_whisper'")

    t = FasterWhisperTranscriber("large-v3-turbo", model_factory=factory)
    with pytest.raises(NoSubtitlesAvailable) as exc:
        t.transcribe("a.webm", 1.0, _quiet, lambda: False)
    assert "faster-whisper" in str(exc.value)


def test_a_decode_failure_becomes_a_domain_error():
    """攔的 bug：音訊壞掉時的底層錯誤原封不動冒到 UI。"""
    model = FakeModel([], error=RuntimeError("Invalid data found when processing input"))
    with pytest.raises(NoSubtitlesAvailable):
        _make(model).transcribe("a.webm", 1.0, _quiet, lambda: False)


def test_progress_position_never_exceeds_the_total():
    """攔的 bug：顯示「0:20 / 0:19」。實測 large-v3-turbo 的段落時間戳以 4 秒對齊，
    最後一段的結束時間會超過影片實際長度，看起來像當掉或算錯。"""
    seen: list[str] = []
    model = FakeModel([Seg(16.0, 20.0, "end")])
    _make(model).transcribe("a.webm", 19.0, lambda f, s: seen.append(s), lambda: False)
    assert "0:19 / 0:19" in seen[-1]
