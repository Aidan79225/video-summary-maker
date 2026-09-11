"""領域實體：不依賴任何框架或 IO，純資料與規則。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Cue:
    """字幕的一句。"""
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Transcript:
    video_id: str
    title: str
    duration: float
    cues: tuple[Cue, ...]
    language: str
    # 是否為 YouTube 的滾動式自動字幕：決定是否做滾動去重、以及是否提示品質。
    # 語音辨識的結果不是滾動字幕，設 False——套用滾動去重只會誤刪內容。
    is_automatic: bool = False


@dataclass(frozen=True)
class AudioClip:
    """下載好的音訊檔與其影片資訊。

    語音路徑是在字幕 gateway 失敗之後才走的，拿不到它原本提供的 metadata，
    所以由音訊 gateway 一併帶回。
    """
    path: str
    video_id: str
    title: str
    duration: float


@dataclass(frozen=True)
class Slide:
    """一頁投影片。image_path 由抽幀階段後填，摘要階段一律為 None。"""
    index: int
    title: str
    bullets: tuple[str, ...]
    timestamp: float
    image_path: str | None = None


@dataclass(frozen=True)
class Deck:
    source_url: str
    video_title: str
    slides: tuple[Slide, ...]

    @property
    def missing_images(self) -> int:
        return sum(1 for s in self.slides if s.image_path is None)


@dataclass(frozen=True)
class DeckResult:
    """use case 的回傳：成品與它的落點。UI 需要路徑才能提供「開啟」。"""
    deck: Deck
    html_path: str


@dataclass
class Settings:
    """使用者設定，持久化於 slidebox_settings.json。"""
    output_dir: str
    model: str = "qwen3.5:9b"
    ollama_host: str = "http://localhost:11434"
    max_height: int | None = 1080
    min_slides: int = 8
    max_slides: int = 15
    image_width: int = 1280
    num_ctx: int = 32768
    char_budget: int = 20000
    # 簡中排在繁中之後、英文之前：輸出是繁體，但人工簡中已經是中文，比英文好。
    subtitle_langs: tuple[str, ...] = (
        "zh-TW", "zh-Hant", "zh-HK", "zh", "zh-Hans", "zh-CN", "en",
    )
    # 沒有字幕時的語音辨識模型。large-v3-turbo 在 CPU 上比 medium 還快、品質
    # 最佳；實測日韓 3.9～4.9 倍即時。改 small 約快兩成，但日文明顯較差。
    whisper_model: str = "large-v3-turbo"
