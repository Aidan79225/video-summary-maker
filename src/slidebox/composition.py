"""Composition root：組裝所有相依（依賴注入）。"""
from __future__ import annotations

import os

from .domain.entities import Settings, Slide
from .domain.ports import CancelCheck, ProgressCallback
from .infrastructure.ffmpeg_frames import FfmpegFrameExtractor
from .infrastructure.html_renderer import HtmlDeckRenderer
from .infrastructure.ollama_summarizer import OllamaModelCatalog, OllamaSummarizer
from .infrastructure.settings_repository import JsonSettingsRepository
from .infrastructure.whisper_transcriber import FasterWhisperTranscriber
from .infrastructure.ytdlp_audio import YtDlpAudioGateway
from .infrastructure.ytdlp_sections import YtDlpSectionGateway
from .infrastructure.ytdlp_subtitles import YtDlpSubtitleGateway
from .presentation.deck_page import DeckPage
from .presentation.main_window import MainWindow
from .usecases.build_deck import BuildDeckUseCase

# 預設輸出資料夾
DEFAULT_OUTPUT_DIR = r"C:\Users\Aidan\Desktop\影片摘要"


def _project_root() -> str:
    # 此檔位於 <root>/src/slidebox/composition.py，往上三層即專案根目錄
    here = os.path.abspath(__file__)
    return os.path.dirname(os.path.dirname(os.path.dirname(here)))


class CurrentSettingsSummarizer:
    """每次生成都用當下的 model／host 設定，讓使用者換模型後不必重開 app。

    參數必須與 Summarizer port 完全一致：這一層是純轉發，簽章少一個參數
    不會有任何測試抓到（composition root 沒有測試），只會在使用者按下
    生成時炸成 TypeError。
    """

    def __init__(self, settings: Settings, factory=OllamaSummarizer):
        self._settings = settings
        self._factory = factory

    def summarize(
        self,
        compressed: str,
        duration: float,
        min_slides: int,
        max_slides: int,
        hint: str,
        progress: ProgressCallback,
        detailed: bool = False,
        is_cancelled: CancelCheck | None = None,
    ) -> tuple[Slide, ...]:
        s = self._settings
        impl = self._factory(s.ollama_host, s.model, s.num_ctx)
        return impl.summarize(compressed, duration, min_slides, max_slides,
                              hint, progress, detailed, is_cancelled)


def build_main_window() -> MainWindow:
    settings_repo = JsonSettingsRepository(
        os.path.join(_project_root(), "slidebox_settings.json")
    )
    settings = settings_repo.load(Settings(output_dir=DEFAULT_OUTPUT_DIR))

    def save() -> None:
        settings_repo.save(settings)

    usecase = BuildDeckUseCase(
        YtDlpSubtitleGateway(),
        CurrentSettingsSummarizer(settings),
        YtDlpSectionGateway(),
        FfmpegFrameExtractor(),
        HtmlDeckRenderer(),
        # 沒有字幕時的語音辨識備援。模型在第一次需要時才載入（約 40 秒）並
        # 快取在這個實例裡；改 whisper_model 需重開 app（語言模型與 host 則每次生成時即時讀取）。
        audio=YtDlpAudioGateway(),
        transcriber=FasterWhisperTranscriber(settings.whisper_model),
    )
    page = DeckPage(usecase, OllamaModelCatalog(settings.ollama_host), settings, save)
    return MainWindow(page)
