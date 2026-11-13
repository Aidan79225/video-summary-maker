"""多人模式 use case 測試共用的假零件：說話者區分器、逐字辨識器、對談摘要器、會存多人階段的存檔。

全部在記憶體裡，不碰模型、網路或磁碟。對談用 speaker_fixtures.DIALOGUE（三位說話者、手寫的時間），
所以對齊與分段的結果和 G1 的測試一致：use case 算出來的 Turn 就是 conversation_support.TURNS。
"""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

from slidebox.domain.entities import AudioClip, DiarizerOutput, RawPoint, RawSlide, Settings
from slidebox.domain.errors import OperationCancelled
from slidebox.usecases.build_deck import BuildDeckUseCase
from slidebox.usecases.conversation import MAX_BULLETS

from .conversation_support import SPEAKERS, TURNS, at
from .fakes import (
    FakeAudioGateway,
    FakeBriefWriter,
    FakeCheckpoints,
    FakeFrameExtractor,
    FakeRenderer,
    FakeSectionGateway,
    FakeSlideRewriter,
    FakeSubtitleGateway,
    FakeSummarizer,
)
from .speaker_fixtures import dialogue

FIXTURE = dialogue()
URL = "https://www.youtube.com/watch?v=abcdefghijk"
CLIP = AudioClip(path="OUT/.slidebox_audio_tmp/audio.webm", video_id="abcdefghijk",
                 title="少子化對談", duration=120.0)
# 切成兩個窗（T0–T9、T10–T18）的字數預算；test_diarized_support 確認它真的切成這樣
TWO_WINDOWS = 300
SPLIT_AT = 10


def settings(**overrides) -> Settings:
    values = dict(output_dir="OUT", min_slides=2, max_slides=6, whisper_model="whisper-a", model="qwen-a")
    values.update(overrides)
    return Settings(**values)


# 每一筆是（那一段話的開頭, 通過檢查的重點, 照抄的原文 quote）。重點的文字刻意不用「我們」「他」之類的字。
POOL = (
    ("今天我們", "詢問目前補助政策的看法", "先請教您怎麼看目前的補助政策"),
    ("現金補助", "現金補助有幫助，但托育資源更重要", "現金補助有幫助，但不能只靠現金"),
    ("那公共托育", "詢問公共托育現在有什麼不足", "那公共托育現在有什麼不足"),
    ("名額不夠，很多", "名額不夠，家長排了兩年沒有消息", "名額不夠，很多家長排不到，排了兩年也沒有消息"),
    ("謝謝邱委員", "詢問預算要怎麼編才夠", "那預算要怎麼編才夠"),
    ("預算大約", "預算大約要三千億，比去年多了兩成", "預算大約要三千億，比去年多了兩成"),
    ("我想說的是", "主張公共托育一定要先做", "公共托育一定要先做"),
)


def good_points(first: int = 0, last: int = 10**6) -> tuple[RawPoint, ...]:
    return tuple(RawPoint(at(prefix), text, quote=quote)
                 for prefix, text, quote in POOL if first <= at(prefix) <= last)


def good_reply(call) -> tuple[RawSlide, ...]:
    """這個窗裡所有通過檢查的重點。每頁最多 MAX_BULLETS 個（超過的點檢查會在計數前扣掉），
    點數盡量平均分在幾頁裡：7 個分成 4＋3，5 個分成 3＋2。"""
    points = good_points(call.first_turn, call.last_turn)
    pages = max(1, -(-len(points) // MAX_BULLETS))
    base, extra = divmod(len(points), pages)
    slides, start = [], 0
    for page in range(pages):
        size = base + (1 if page < extra else 0)
        slides.append(RawSlide("補助與托育", points[start:start + size]))
        start += size
    return tuple(slides)


class FakeDiarizer:
    """比照 SpeechDiarizer：便宜的 available()，diarize() 回傳腳本裡的結果。"""

    engine = "fake-engine"
    model = "fake/model"

    def __init__(self, output: DiarizerOutput | None = None, usable: bool = True, reason: str = "",
                 version: str = "1.0", error: Exception | None = None, on_diarize=None):
        self.output = output if output is not None else DiarizerOutput(FIXTURE.turns, 0.0)
        self._usable, self._reason, self.version = usable, reason, version
        self.error = error
        self._on_diarize = on_diarize
        self.calls: list[tuple[str, float, int | None]] = []
        self.availability_checks = 0
        self.saw_cancel = False

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(f"{self.engine}|{self.version}|{self.model}".encode()).hexdigest()[:16]

    def available(self):
        self.availability_checks += 1
        return self._usable, self._reason

    def diarize(self, audio_path, duration, speaker_count, progress, is_cancelled):
        self.calls.append((audio_path, duration, speaker_count))
        progress(0.5, "區分說話者…")
        if self._on_diarize is not None:
            self._on_diarize()
        self.saw_cancel = is_cancelled()
        if self.saw_cancel:
            raise OperationCancelled()
        if self.error is not None:
            raise self.error
        return self.output


class FakeWordTranscriber:
    """比照 WordTranscriber：回傳手寫對談的字（每個字都有時間）。"""

    def __init__(self, words=None, language: str = "zh", error: Exception | None = None, on_transcribe=None):
        self._words = tuple(FIXTURE.words if words is None else words)
        self._language = language
        self.error = error
        self._on_transcribe = on_transcribe
        self.calls: list[tuple[str, float]] = []
        self.hints: list[str | None] = []
        self.saw_cancel = False

    def transcribe_words(self, audio_path, duration, progress, is_cancelled, speech_hint=None):
        self.calls.append((audio_path, duration))
        self.hints.append(speech_hint)
        progress(None, "語音辨識中… 0:30 / 2:00")
        if self._on_transcribe is not None:
            self._on_transcribe()
        self.saw_cancel = is_cancelled()
        if self.saw_cancel:
            raise OperationCancelled()
        if self.error is not None:
            raise self.error
        return self._words, self._language


class FakeConversation:
    """比照 ConversationSummarizer。replies 依序回答每一次呼叫：一個 tuple[RawSlide,...]、一個例外，
    或一個 callable(call)；用完之後（或沒給）就用 default（預設是這個窗裡全部通過檢查的重點）。"""

    def __init__(self, replies=(), default=good_reply, on_call=None):
        self._replies = list(replies)
        self._default = default
        self._on_call = on_call
        self.calls: list[SimpleNamespace] = []
        self.saw_cancel = False

    def summarize_window(self, window_text, first_turn, last_turn, citable_turns, min_slides, max_slides,
                         hint, progress, is_cancelled=None):
        call = SimpleNamespace(window_text=window_text, first_turn=first_turn, last_turn=last_turn,
                               citable_turns=tuple(citable_turns), min_slides=min_slides,
                               max_slides=max_slides, hint=hint)
        self.calls.append(call)
        progress(None, "生成中")
        if self._on_call is not None:
            self._on_call(call)
        self.saw_cancel = bool(is_cancelled and is_cancelled())
        if self.saw_cancel:
            raise OperationCancelled()
        reply = self._replies.pop(0) if self._replies else self._default
        if callable(reply):
            reply = reply(call)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeMultiCheckpoints(FakeCheckpoints):
    """FakeCheckpoints 再加多人模式的三個階段，記下每一次存檔（stage 名稱）。"""

    def __init__(self):
        super().__init__()
        self.save_log: list[str] = []

    def load_diarization(self, output_dir, url, fingerprint):
        return self._load(output_dir, url, "diarization", fingerprint)

    def save_diarization(self, output_dir, url, fingerprint, output):
        self.save_log.append("diarization")
        self._save(output_dir, url, "diarization", fingerprint, output)

    def load_words(self, output_dir, url, fingerprint):
        return self._load(output_dir, url, "words", fingerprint)

    def save_words(self, output_dir, url, fingerprint, words):
        self.save_log.append("words")
        self._save(output_dir, url, "words", fingerprint, words)

    def load_conversation(self, output_dir, url, fingerprint):
        return self._load(output_dir, url, "conversation", fingerprint)

    def save_conversation(self, output_dir, url, fingerprint, slides, proposed):
        self.save_log.append("conversation")
        self._save(output_dir, url, "conversation", fingerprint, (tuple(slides), proposed))


class QuietAudio:
    """下載完才收到取消，卻照樣回傳音訊：真的 adapter 在下載的最後一刻收到取消時就是這樣。
    取消要由 use case 自己在下一步之前檢查出來，而不是指望 adapter 丟例外。"""

    def __init__(self, clip: AudioClip = CLIP, on_download=None):
        self._clip, self._on_download = clip, on_download
        self.events: list[str] = []
        self.dest_dirs: list[str] = []

    def download_audio(self, url, dest_dir, progress, is_cancelled):
        self.events.append("download")
        self.dest_dirs.append(dest_dir)
        if self._on_download is not None:
            self._on_download()
        return self._clip

    def cleanup(self, dest_dir):
        self.events.append("cleanup")


class CancelDuring(FakeMultiCheckpoints):
    """某個存檔動作（"load_words"、"save_diarization"…）發生的瞬間，使用者按下取消。"""

    def __init__(self, action: str, flag):
        super().__init__()
        self._action, self._flag = action, flag

    def _hit(self, action: str) -> None:
        if action == self._action:
            self._flag.set()

    def load_diarization(self, *args):
        self._hit("load_diarization")
        return super().load_diarization(*args)

    def load_words(self, *args):
        self._hit("load_words")
        return super().load_words(*args)

    def load_conversation(self, *args):
        self._hit("load_conversation")
        return super().load_conversation(*args)

    def save_diarization(self, *args):
        super().save_diarization(*args)
        self._hit("save_diarization")

    def save_words(self, *args):
        super().save_words(*args)
        self._hit("save_words")

    def save_conversation(self, *args):
        super().save_conversation(*args)
        self._hit("save_conversation")


class FlakyRenderer(FakeRenderer):
    """前幾次出片就壞掉：前面的階段都做完了、存檔還在，用來測「接著做」。"""

    def __init__(self, failures: int = 1):
        super().__init__()
        self.failures = failures

    def render(self, deck, dest_path):
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("出片失敗")
        super().render(deck, dest_path)


def make(chunk_chars: int = 6000, quote_only: bool = False, **overrides):
    """組一個多人模式的 use case 和它所有的假零件。overrides 可以換掉任何一個零件；
    quote_only 是安全模式（每個重點都顯示核對過的原話）。"""
    parts = SimpleNamespace(
        audio=FakeAudioGateway(clip=CLIP),
        diarizer=FakeDiarizer(),
        words=FakeWordTranscriber(),
        conversation=FakeConversation(),
        checkpoints=FakeMultiCheckpoints(),
        sections=FakeSectionGateway(),
        frames=FakeFrameExtractor(),
        renderer=FakeRenderer(),
        subtitles=FakeSubtitleGateway(),
        summarizer=FakeSummarizer([]),
        brief_writer=FakeBriefWriter(),
        rewriter=FakeSlideRewriter(),
    )
    for name, value in overrides.items():
        if not hasattr(parts, name):
            raise AttributeError(name)
        setattr(parts, name, value)
    parts.usecase = BuildDeckUseCase(
        parts.subtitles, parts.summarizer, parts.sections, parts.frames, parts.renderer,
        audio=parts.audio, transcriber=None, diarizer=parts.diarizer, brief_writer=parts.brief_writer,
        rewriter=parts.rewriter, checkpoints=parts.checkpoints, word_transcriber=parts.words,
        conversation=parts.conversation, chunk_chars=chunk_chars, quote_only=quote_only)
    return parts


def run(parts, cfg=None, speaker_count=None, speech_hint=None, progress=None, cancelled=None):
    return parts.usecase.execute(
        URL, cfg or settings(), progress, cancelled, speech_hint=speech_hint,
        speaker_mode="diarize", speaker_count=speaker_count)


__all__ = ["CLIP", "FIXTURE", "POOL", "SPEAKERS", "SPLIT_AT", "TURNS", "TWO_WINDOWS", "URL", "CancelDuring",
           "FakeConversation", "FakeDiarizer", "FakeMultiCheckpoints", "FakeWordTranscriber", "FlakyRenderer",
           "QuietAudio", "at", "good_points", "good_reply", "make", "run", "settings"]
