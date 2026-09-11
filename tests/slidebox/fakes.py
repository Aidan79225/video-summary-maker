"""測試用的記憶體假實作（不碰真實磁碟／網路）。"""
from __future__ import annotations

from slidebox.domain.entities import AudioClip, Cue, Slide, Transcript
from slidebox.domain.errors import NoSubtitlesAvailable, SummarizerOutputInvalid


class FakeSubtitleGateway:
    def __init__(self, transcript: Transcript | None = None, fail: bool = False):
        self._transcript = transcript or Transcript(
            video_id="vid1",
            title="測試影片",
            duration=600.0,
            cues=(Cue(0.0, 3.0, "第一句"), Cue(30.0, 33.0, "第二句")),
            language="zh-TW",
            is_automatic=True,
        )
        self._fail = fail
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def fetch(self, url, langs):
        self.calls.append((url, tuple(langs)))
        if self._fail:
            raise NoSubtitlesAvailable("沒有字幕")
        return self._transcript


class FakeSummarizer:
    """依序回傳預設的多批結果，用來模擬「第一次不合格、重試後合格」。"""

    def __init__(self, batches: list[tuple[Slide, ...]]):
        self._batches = list(batches)
        self.hints: list[str] = []
        self.compressed: list[str] = []

    def summarize(self, compressed, duration, min_slides, max_slides, hint, progress):
        self.compressed.append(compressed)
        self.hints.append(hint)
        progress(0.0, "開始")
        progress(0.5, "一半")
        progress(None, "長度未知")
        return self._batches.pop(0) if self._batches else ()


class RaisingThenSucceedingSummarizer:
    """第一次呼叫 raise SummarizerOutputInvalid（模擬完全不是 JSON 的回應），
    第二次才回傳合格結果——用來驗證這種失敗也會拿到重試機會。"""

    def __init__(self, slides: tuple[Slide, ...], error: str = "模型回應不是合法的 JSON"):
        self._slides = slides
        self._error = error
        self.calls = 0

    def summarize(self, compressed, duration, min_slides, max_slides, hint, progress):
        self.calls += 1
        if self.calls == 1:
            raise SummarizerOutputInvalid(self._error)
        return self._slides


class FakeSectionGateway:
    """results 為 None 的位置代表該時間點下載失敗。"""

    def __init__(self, results: list[str | None] | None = None):
        self._results = results
        self.timestamps: list[float] = []
        self.cleaned: list[str] = []

    def download_sections(self, url, timestamps, max_height, dest_dir, progress, is_cancelled):
        self.timestamps = list(timestamps)
        if self._results is not None:
            return list(self._results)
        return [f"{dest_dir}/clip{i}.mp4" for i in range(len(timestamps))]

    def cleanup(self, dest_dir):
        self.cleaned.append(dest_dir)


class FakeFrameExtractor:
    def __init__(self, fail_on: set[str] | None = None):
        self.fail_on = fail_on or set()
        self.calls: list[tuple[str, str, int]] = []

    def extract(self, section_path, dest_path, width):
        self.calls.append((section_path, dest_path, width))
        if section_path in self.fail_on:
            raise RuntimeError("抽幀失敗")


class FakeRenderer:
    def __init__(self):
        self.rendered: list[tuple[object, str]] = []

    def render(self, deck, dest_path):
        self.rendered.append((deck, dest_path))


def make_slides(n: int) -> tuple[Slide, ...]:
    return tuple(
        Slide(index=i, title=f"第 {i} 段", bullets=(f"重點 {i}",), timestamp=float(i * 30))
        for i in range(1, n + 1)
    )


class FakeAudioGateway:
    """回傳固定的 AudioClip；下載時回報一個非 None 的進度，用來確認 use case
    不會把它直接丟給進度條。"""

    def __init__(self, clip: AudioClip | None = None, error: Exception | None = None):
        self._clip = clip or AudioClip(
            path="OUT/_audio/audio.webm", video_id="spk1", title="沒有字幕的影片", duration=120.0
        )
        self._error = error
        self.dest_dirs: list[str] = []
        self.cleaned: list[str] = []
        self.events: list[str] = []

    def download_audio(self, url, dest_dir, progress, is_cancelled):
        self.events.append("download")
        self.dest_dirs.append(dest_dir)
        progress(0.5, "下載音訊…")
        if self._error is not None:
            raise self._error
        return self._clip

    def cleanup(self, dest_dir):
        self.events.append("cleanup")
        self.cleaned.append(dest_dir)


class FakeTranscriber:
    def __init__(self, cues=None, language: str = "ja", error: Exception | None = None):
        self._cues = cues if cues is not None else (
            Cue(0.0, 4.0, "こんにちは"), Cue(30.0, 34.0, "よろしくお願いします"),
        )
        self._language = language
        self._error = error
        self.calls: list[tuple[str, float]] = []

    def transcribe(self, audio_path, duration, progress, is_cancelled):
        self.calls.append((audio_path, duration))
        progress(None, "語音辨識中… 0:30 / 2:00")
        if self._error is not None:
            raise self._error
        return tuple(self._cues), self._language
