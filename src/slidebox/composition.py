"""Composition root：組裝所有相依（依賴注入）。"""
from __future__ import annotations

import os

from .domain.entities import Settings
from .infrastructure.ffmpeg_frames import FfmpegFrameExtractor
from .infrastructure.html_renderer import HtmlDeckRenderer
from .infrastructure.ollama_summarizer import OllamaModelCatalog, OllamaSummarizer
from .infrastructure.settings_repository import JsonSettingsRepository
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


def build_main_window() -> MainWindow:
    settings_repo = JsonSettingsRepository(
        os.path.join(_project_root(), "slidebox_settings.json")
    )
    settings = settings_repo.load(Settings(output_dir=DEFAULT_OUTPUT_DIR))

    def save() -> None:
        settings_repo.save(settings)

    # Summarizer 每次生成時都要用當下的 model／host 設定，所以包一層
    # 轉發器，讓使用者換模型後不必重開 app。
    class _CurrentSummarizer:
        def summarize(self, compressed, duration, min_slides, max_slides, hint, progress):
            impl = OllamaSummarizer(settings.ollama_host, settings.model, settings.num_ctx)
            return impl.summarize(
                compressed, duration, min_slides, max_slides, hint, progress)

    usecase = BuildDeckUseCase(
        YtDlpSubtitleGateway(),
        _CurrentSummarizer(),
        YtDlpSectionGateway(),
        FfmpegFrameExtractor(),
        HtmlDeckRenderer(),
    )
    page = DeckPage(usecase, OllamaModelCatalog(settings.ollama_host), settings, save)
    return MainWindow(page)
