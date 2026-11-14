"""多人模式的語音辨識：每個字都帶時間。注入假模型，不下載模型、不碰網路。

單人路徑的 transcribe() 與它的呼叫一個字都不能動（test_whisper_transcriber.py 的嚴格假模型
釘住它）；這裡驗證新的 transcribe_words()。
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

import pytest

from slidebox.domain.entities import Word
from slidebox.domain.errors import OperationCancelled, SpeechRecognitionFailed
from slidebox.infrastructure import whisper_transcriber as mod
from slidebox.infrastructure.whisper_transcriber import FasterWhisperTranscriber


@pytest.fixture(autouse=True)
def _restore_path(monkeypatch):
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))


@dataclass
class W:
    """比照 faster_whisper.transcribe.Word 會被讀到的欄位。"""
    start: float
    end: float
    word: str
    probability: float = 0.9


@dataclass
class Seg:
    start: float
    end: float
    text: str
    words: list | None = field(default=None)


@dataclass
class Info:
    language: str
    language_probability: float = 0.99


class FakeModel:
    def __init__(self, segments, language="zh", error=None):
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


def _make(model):
    return FasterWhisperTranscriber("large-v3-turbo", model_factory=lambda name: model)


def _run(model, hint=None, cancelled=lambda: False, progress=_quiet, duration=60.0):
    return _make(model).transcribe_words("a.webm", duration, progress, cancelled, hint)


# --- 呼叫模型的方式 ---


def test_the_model_is_asked_for_word_timestamps_with_the_agreed_settings():
    """攔的 bug：少了 word_timestamps（沒有字的時間就沒辦法對說話者）、少了 vad_filter
    （靜音處的幻覺句子）、提示沒帶、或把暫定的 hallucination_silence_threshold 弄丟。"""
    model = FakeModel([Seg(0.0, 1.0, "你好", [W(0.0, 0.5, "你"), W(0.5, 1.0, "好")])])
    _run(model, hint="示範頻道 少子化對談")
    assert model.calls == [("a.webm", {
        "vad_filter": True,
        "initial_prompt": "示範頻道 少子化對談",
        "word_timestamps": True,
        "hallucination_silence_threshold": 2.0,
    })]


@pytest.mark.parametrize("hint", [None, ""])
def test_no_hint_means_no_initial_prompt(hint):
    model = FakeModel([Seg(0.0, 1.0, "你", [W(0.0, 1.0, "你")])])
    _run(model, hint=hint)
    assert model.calls[0][1]["initial_prompt"] is None


def test_the_single_speaker_call_is_exactly_what_it_was():
    """攔的 bug：為了多人模式改了 transcribe() 的呼叫，單人（議會）路徑跟著變。
    它只能有 vad_filter 與 initial_prompt，沒有 word_timestamps。"""
    model = FakeModel([Seg(0.0, 1.0, "今日は")], language="ja")
    _make(model).transcribe("a.webm", 1.0, _quiet, lambda: False, "提示")
    assert model.calls == [("a.webm", {"vad_filter": True, "initial_prompt": "提示"})]


# --- 字 ---


def test_every_word_keeps_its_time_text_segment_and_probability():
    model = FakeModel([
        Seg(0.0, 2.0, "你好嗎", [W(0.0, 0.7, "你", 0.99), W(0.7, 1.4, "好", 0.8), W(1.4, 2.0, "嗎？", 0.5)]),
        Seg(2.5, 3.5, "是的", [W(2.5, 3.0, "是", 0.9), W(3.0, 3.5, "的。", 0.95)]),
    ])
    words, language = _run(model)
    assert language == "zh"
    assert words == (
        Word(0.0, 0.7, "你", 0, 0.99), Word(0.7, 1.4, "好", 0, 0.8), Word(1.4, 2.0, "嗎？", 0, 0.5),
        Word(2.5, 3.0, "是", 1, 0.9), Word(3.0, 3.5, "的。", 1, 0.95),
    )


def test_english_words_keep_their_leading_space():
    """英文的字帶著前導空白：串起來才還原得出原句。"""
    model = FakeModel([Seg(0.0, 1.0, " Hello world", [W(0.0, 0.5, " Hello"), W(0.5, 1.0, " world")])],
                      language="en")
    words, _ = _run(model)
    assert "".join(w.text for w in words) == " Hello world"


def test_blank_words_and_blank_segments_are_dropped():
    model = FakeModel([
        Seg(0.0, 1.0, "  ", [W(0.0, 1.0, "  ")]),
        Seg(1.0, 2.0, "好", [W(1.0, 1.5, " "), W(1.5, 2.0, "好")]),
    ])
    words, _ = _run(model)
    assert [w.text for w in words] == ["好"]


def test_no_speech_returns_an_empty_tuple():
    words, language = _run(FakeModel([], language="zh"))
    assert words == () and language == "zh"


def test_non_finite_word_times_are_replaced_by_the_segment_bounds():
    """攔的 bug：NaN 進了存檔（JSON 的 NaN 不是標準格式）或讓對齊拿到沒有意義的時間。"""
    model = FakeModel([Seg(4.0, 6.0, "好吧", [W(math.nan, 5.0, "好"), W(5.0, math.inf, "吧")])])
    words, _ = _run(model)
    assert [(w.start, w.end) for w in words] == [(4.0, 5.0), (5.0, 6.0)]


def test_an_end_before_its_start_is_lifted_to_the_start():
    model = FakeModel([Seg(0.0, 2.0, "好", [W(1.5, 1.0, "好")])])
    (word,), _ = _run(model)
    assert word.start == 1.5 and word.end == 1.5


# --- 沒有字時間的段落 ---


def test_a_segment_without_word_times_is_spread_over_its_span_with_zero_confidence():
    """攔的 bug：整段變成一個跨好幾秒的字，對齊時整段歸給中點那一位；或整段被默默丟掉。
    攤開成逐字、時間不遞減、涵蓋整段、可信度 0，換人只會發生在句尾。"""
    model = FakeModel([Seg(10.0, 14.0, "你好嗎？我很好。", None)])
    words, _ = _run(model)
    assert "".join(w.text for w in words) == "你好嗎？我很好。"
    assert [w.text for w in words] == ["你", "好", "嗎？", "我", "很", "好。"]
    assert words[0].start == 10.0 and words[-1].end == pytest.approx(14.0)
    assert all(a.end <= b.start + 1e-9 for a, b in zip(words, words[1:]))
    assert all(w.probability == 0.0 and w.segment == 0 for w in words)


def test_a_spread_segment_keeps_english_spacing():
    model = FakeModel([Seg(0.0, 3.0, " Hello big world.", [])], language="en")
    words, _ = _run(model)
    assert "".join(w.text for w in words) == " Hello big world."
    assert len(words) == 3


def test_a_segment_without_usable_bounds_and_words_is_dropped():
    """時間不是有限數字、又沒有字的時間：無處可放，丟掉（不編造一個位置）。"""
    model = FakeModel([Seg(math.nan, 3.0, "好", None), Seg(2.0, math.inf, "不", None)])
    words, _ = _run(model)
    assert words == ()


def test_a_segment_whose_end_is_before_its_start_is_placed_at_its_start():
    model = FakeModel([Seg(5.0, 4.0, "不", None)])
    (word,), _ = _run(model)
    assert (word.start, word.end, word.probability) == (5.0, 5.0, 0.0)


# --- 簡轉繁 ---


def test_a_drifted_simplified_segment_is_converted_word_by_word_keeping_every_time():
    """攔的 bug：只轉了文字，沒投影回每個字（字數、時間對不上）。字的數量與時間完全不變。"""
    model = FakeModel([Seg(0.0, 2.0, "这个预算", [W(0.0, 0.5, "这"), W(0.5, 1.0, "个"),
                                                  W(1.0, 1.5, "预"), W(1.5, 2.0, "算")])])
    words, _ = _run(model)
    assert "".join(w.text for w in words) == "這個預算"
    assert [(w.start, w.end) for w in words] == [(0.0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.0)]


def test_words_longer_than_one_character_are_sliced_back_correctly():
    model = FakeModel([Seg(0.0, 2.0, "预算很多", [W(0.0, 1.0, "预算"), W(1.0, 2.0, "很多")])])
    words, _ = _run(model)
    assert [w.text for w in words] == ["預算", "很多"]


def test_conversion_sees_the_whole_transcript_not_one_segment_at_a_time(monkeypatch):
    """簡轉繁要看整份才判斷得出哪一段漂了（與單人路徑相同）：一次呼叫、每段一個字串。"""
    seen = []

    def fake(texts, keep_terms=()):
        seen.append((list(texts), list(keep_terms)))
        return list(texts)

    monkeypatch.setattr(mod, "fix_transcript", fake)
    model = FakeModel([Seg(0.0, 1.0, "一二", [W(0, 0.5, "一"), W(0.5, 1, "二")]),
                       Seg(1.0, 2.0, "三四", [W(1, 1.5, "三"), W(1.5, 2, "四")])])
    _run(model, hint="臺中市議會 發言者：楊啓邦")
    assert [t for t, _ in seen] == [["一二", "三四"]]
    assert "楊啓邦" in seen[0][1]


def test_when_the_converted_length_differs_each_word_is_converted_alone(monkeypatch):
    """轉換結果和原文長度不同（目前的轉換器不會，但不能因此錯位）：退回逐字轉換，字與時間不動。"""
    monkeypatch.setattr(mod, "fix_transcript", lambda texts, keep_terms=(): ["多出來的字" + t for t in texts])
    model = FakeModel([Seg(0.0, 2.0, "这个", [W(0.0, 1.0, "这"), W(1.0, 2.0, "个")])])
    words, _ = _run(model)
    assert [w.text for w in words] == ["這", "個"]
    assert [(w.start, w.end) for w in words] == [(0.0, 1.0), (1.0, 2.0)]


def test_other_languages_are_not_converted(monkeypatch):
    """日文的漢字轉了就是錯字：只有中文做簡轉繁。"""
    called = []
    monkeypatch.setattr(mod, "fix_transcript", lambda texts, keep_terms=(): called.append(1) or list(texts))
    model = FakeModel([Seg(0.0, 1.0, "国会", [W(0.0, 0.5, "国"), W(0.5, 1.0, "会")])], language="ja")
    words, _ = _run(model)
    assert called == [] and "".join(w.text for w in words) == "国会"


# --- 取消、進度、錯誤 ---


def test_cancellation_stops_between_segments():
    model = FakeModel([Seg(i, i + 1.0, "好", [W(i, i + 1.0, "好")]) for i in range(10)])
    calls = {"n": 0}

    def cancel_after_two():
        calls["n"] += 1
        return calls["n"] > 2

    with pytest.raises(OperationCancelled):
        _run(model, cancelled=cancel_after_two)
    assert model.pulled < 10


def test_cancel_before_the_model_is_even_used_stops_right_away():
    model = FakeModel([Seg(0.0, 1.0, "好", [W(0.0, 1.0, "好")])])
    with pytest.raises(OperationCancelled):
        _run(model, cancelled=lambda: True)
    assert model.calls == []


def test_progress_is_reported_for_every_segment():
    seen = []
    model = FakeModel([Seg(0.0, 5.0, "a", [W(0.0, 5.0, "a")]), Seg(5.0, 12.0, "b", [W(5.0, 12.0, "b")]),
                       Seg(12.0, 19.0, "c", [W(12.0, 19.0, "c")])])
    _run(model, progress=lambda f, s: seen.append((f, s)), duration=60.0)
    per_segment = [s for _, s in seen if s.startswith("語音辨識中")]
    assert len(per_segment) == 3 and "0:19 / 1:00" in per_segment[-1]
    assert all(f is None for f, _ in seen)


def test_a_missing_library_is_a_readable_error_without_the_misleading_no_subtitles_prefix():
    """多人模式沒有去找過字幕：錯誤不能說「這部影片沒有字幕」。"""

    def factory(name):
        raise ImportError("No module named 'faster_whisper'")

    t = FasterWhisperTranscriber("large-v3-turbo", model_factory=factory)
    with pytest.raises(SpeechRecognitionFailed) as error:
        t.transcribe_words("a.webm", 1.0, _quiet, lambda: False)
    assert "faster-whisper" in str(error.value)
    assert "沒有字幕" not in str(error.value)


def test_a_model_that_cannot_be_loaded_is_a_readable_error():
    def factory(name):
        raise OSError("無法下載模型")

    t = FasterWhisperTranscriber("large-v3-turbo", model_factory=factory)
    with pytest.raises(SpeechRecognitionFailed, match="large-v3-turbo"):
        t.transcribe_words("a.webm", 1.0, _quiet, lambda: False)


def test_a_decode_failure_is_a_domain_error():
    model = FakeModel([], error=RuntimeError("Invalid data found when processing input"))
    with pytest.raises(SpeechRecognitionFailed, match="Invalid data"):
        _run(model)


def test_a_failure_in_the_middle_of_the_stream_is_a_domain_error():
    class Broken(FakeModel):
        def transcribe(self, path, **kwargs):
            def gen():
                yield Seg(0.0, 1.0, "好", [W(0.0, 1.0, "好")])
                raise RuntimeError("CUDA out of memory")

            return gen(), Info("zh")

    with pytest.raises(SpeechRecognitionFailed, match="CUDA out of memory"):
        _run(Broken([]))


def test_the_model_is_loaded_once_and_shared_with_the_single_speaker_path():
    """同一個實例的兩個方法共用一個模型：載入要 40 秒，也佔 VRAM。"""
    built = []

    def factory(name):
        built.append(name)
        return FakeModel([Seg(0.0, 1.0, "好", [W(0.0, 1.0, "好")])], language="zh")

    t = FasterWhisperTranscriber("large-v3-turbo", model_factory=factory)
    t.transcribe("a.webm", 1.0, _quiet, lambda: False)
    t.transcribe_words("a.webm", 1.0, _quiet, lambda: False)
    t.transcribe_words("b.webm", 1.0, _quiet, lambda: False)
    assert built == ["large-v3-turbo"]


# --- 補強：每一道防線都有一個測試抓得到它被拿掉（對照 mutation 的結果加的） ---


def test_a_long_multiline_engine_error_is_shortened_and_keeps_its_cause():
    """攔的 bug：底層錯誤動輒數百字、含換行，原樣放進狀態列；或例外鏈斷了，除錯時看不到真正的原因。"""
    original = RuntimeError("很長的錯誤\n" + "x" * 400)
    with pytest.raises(SpeechRecognitionFailed) as error:
        _run(FakeModel([], error=original))
    message = str(error.value)
    assert "\n" not in message and len(message) <= 200 and message.endswith("…")
    assert error.value.__cause__ is original


def test_a_word_without_a_probability_is_trusted_but_an_unreadable_one_is_not():
    """沒有這個欄位（極少見）不代表不可信；NaN 是壞資料——可信度 0，對齊時不當作確定。"""

    class Bare:
        def __init__(self, start, end, word):
            self.start, self.end, self.word = start, end, word

    model = FakeModel([Seg(0.0, 3.0, "好好好", [Bare(0.0, 1.0, "好"), W(1.0, 2.0, "好", math.nan),
                                              W(2.0, 3.0, "好", 0.4)])])
    words, _ = _run(model)
    assert [w.probability for w in words] == [1.0, 0.0, 0.4]


def test_progress_never_runs_past_the_length_of_the_audio():
    """攔的 bug：最後一段的結束時間比音訊長度大一點（Whisper 常見），進度顯示「0:12 / 0:10」。"""
    seen = []
    model = FakeModel([Seg(0.0, 12.5, "好", [W(0.0, 12.5, "好")])])
    _run(model, duration=10.0, progress=lambda f, s: seen.append(s))
    assert [s for s in seen if s.startswith("語音辨識中")] == ["語音辨識中… 0:10 / 0:10"]


def test_a_non_finite_time_in_the_progress_text_never_fails_the_transcription():
    """攔的 bug：引擎給了 NaN／無限大的段落結束時間（或影片長度），進度文字算不出來——
    進度只是參考，卻害整個辨識失敗（int(NaN) 丟 ValueError）。壞的那一段照樣處理，只是進度顯示 0:00。"""
    seen = []
    model = FakeModel([Seg(0.0, math.nan, "好", [W(0.0, 1.0, "好")]),
                       Seg(1.0, math.inf, "吧", [W(1.0, 2.0, "吧")])])
    words, _ = _run(model, duration=math.inf, progress=lambda f, s: seen.append(s))
    assert [w.text for w in words] == ["好", "吧"]
    per_segment = [s for s in seen if s.startswith("語音辨識中")]
    assert per_segment == ["語音辨識中… 0:00", "語音辨識中… 0:00"]      # 影片長度不明，不顯示「/ 總長」


def test_a_word_whose_times_cannot_be_recovered_is_dropped():
    """攔的 bug：字的時間和整段的時間都是 NaN，字照樣進了結果——之後的對齊與存檔都會壞。"""
    model = FakeModel([Seg(math.nan, math.nan, "好", [W(math.nan, math.nan, "好")]),
                       Seg(1.0, 2.0, "吧", [W(1.0, 2.0, "吧")])])
    words, _ = _run(model)
    assert [w.text for w in words] == ["吧"]


def test_a_spread_segment_gives_each_piece_time_in_proportion_to_its_length():
    """攔的 bug：沒有字時間的段落平均分，長字和短字拿到一樣多的時間——換人點會偏掉。"""
    words, _ = _run(FakeModel([Seg(0.0, 5.0, " abcd e", None)], language="en"))
    assert [w.text for w in words] == [" abcd", " e"]
    assert [(w.start, w.end) for w in words] == [
        (0.0, pytest.approx(4.0)), (pytest.approx(4.0), pytest.approx(5.0))]


def test_a_spread_segment_glues_quotes_to_the_word_they_belong_to():
    """攔的 bug：開引號黏在前一個字上（「說「」），換人的邊界落在引號中間；
    句子最後的開引號被默默弄丟，串起來就不是原文。"""
    words, _ = _run(FakeModel([Seg(0.0, 6.0, "他說「好」。", None)]))
    assert [w.text for w in words] == ["他", "說", "「好」。"]
    words, _ = _run(FakeModel([Seg(0.0, 3.0, "他說「", None)]))
    assert [w.text for w in words] == ["他", "說「"]


def test_the_per_word_fallback_keeps_the_names_from_the_hint(monkeypatch):
    """攔的 bug：退回逐字轉換時忘了保護詞——講者姓名裡的字被轉成別的字。"""
    seen = []
    monkeypatch.setattr(mod, "fix_transcript", lambda texts, keep_terms=(): ["多出來的字" + t for t in texts])
    monkeypatch.setattr(mod, "to_traditional",
                        lambda text, keep_terms=(): seen.append((text, list(keep_terms))) or text)
    model = FakeModel([Seg(0.0, 2.0, "这个", [W(0.0, 1.0, "这"), W(1.0, 2.0, "个")])])
    _run(model, hint="臺中市議會 發言者：楊啓邦")
    assert len(seen) == 2 and all("楊啓邦" in terms for _, terms in seen)


# --- 這一版辨識程式的指紋 ---


def test_the_options_the_model_is_called_with_are_the_ones_the_fingerprint_covers():
    """攔的 bug：辨識參數改了（或多了一個），存檔的字的指紋卻沒變——舊參數辨識出來的字被當成新參數的。
    參數放在 WORD_OPTIONS 一個地方：呼叫模型用它，指紋也含它，兩邊不會各改各的。"""
    model = FakeModel([Seg(0.0, 1.0, "你", [W(0.0, 1.0, "你")])])
    _run(model, hint="提示")
    sent = dict(model.calls[0][1])
    assert sent.pop("initial_prompt") == "提示"
    assert sent == mod.WORD_OPTIONS


def test_the_fingerprint_is_short_stable_and_does_not_depend_on_the_model_name():
    base = _make(FakeModel([])).fingerprint
    assert len(base) == 16 and all(c in "0123456789abcdef" for c in base)
    assert FasterWhisperTranscriber("another-model").fingerprint == base     # 模型名稱在存檔指紋的別的欄位


def test_changing_or_adding_a_recognition_option_changes_the_fingerprint(monkeypatch):
    base = _make(FakeModel([])).fingerprint
    for name, value in mod.WORD_OPTIONS.items():
        changed = dict(mod.WORD_OPTIONS, **{name: not value if isinstance(value, bool) else value + 1})
        monkeypatch.setattr(mod, "WORD_OPTIONS", changed)
        assert _make(FakeModel([])).fingerprint != base, name
    monkeypatch.setattr(mod, "WORD_OPTIONS", dict(mod.WORD_OPTIONS, beam_size=1))
    assert _make(FakeModel([])).fingerprint != base


def test_editing_the_recognizer_or_the_script_fixer_changes_the_fingerprint(monkeypatch):
    from slidebox.usecases import code_fingerprint as cf

    base = _make(FakeModel([])).fingerprint
    real = cf.read_module_source
    for module in ("slidebox.infrastructure.whisper_transcriber", "slidebox.infrastructure.chinese_script"):
        monkeypatch.setattr(cf, "read_module_source",
                            lambda name, edited=module: real(name) + "\n# 改過\n" if name == edited else real(name))
        assert _make(FakeModel([])).fingerprint != base, module


def test_an_unreadable_source_still_gives_the_recognizer_a_fingerprint_that_matches_nothing(monkeypatch):
    from slidebox.usecases import code_fingerprint as cf

    base = _make(FakeModel([])).fingerprint

    def gone(name):
        raise OSError("source is gone")

    monkeypatch.setattr(cf, "read_module_source", gone)
    unreadable = _make(FakeModel([])).fingerprint
    assert len(unreadable) == 16 and unreadable != base
