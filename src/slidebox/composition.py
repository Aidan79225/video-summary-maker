"""Composition root：組裝所有相依（依賴注入）。"""
from __future__ import annotations

import os
from collections.abc import Sequence
from typing import TYPE_CHECKING

from .domain.entities import Brief, RawSlide, Settings, Slide
from .domain.ports import CancelCheck, ProgressCallback
from .infrastructure.audio_cutter import FfmpegAudioCutter
from .infrastructure.checkpoint_store import JsonCheckpointStore
from .infrastructure.ffmpeg_frames import FfmpegFrameExtractor
from .infrastructure.hls_audio import NoSubtitlesGateway
from .infrastructure.html_renderer import HtmlDeckRenderer
from .infrastructure.ivod_api import IvodClient, IvodSubtitleGateway
from .infrastructure.ivod_sections import HlsSectionGateway, IvodSectionGateway
from .infrastructure.ollama_conversation import OllamaConversationSummarizer
from .infrastructure.ollama_summarizer import (
    OllamaSlideRewriter,
    OllamaBriefWriter,
    OllamaModelCatalog,
    OllamaSummarizer,
)
from .infrastructure.routing import (
    BySourceAudioGateway,
    BySourceSectionGateway,
    BySourceSubtitleGateway,
)
from .infrastructure.ntpc_api import NtpcClient
from .infrastructure.ntpc_audio import NtpcAudioGateway
from .infrastructure.pyannote_diarizer import PyannoteSpeakerDiarizer
from .infrastructure.settings_repository import JsonSettingsRepository
from .infrastructure.tccc_api import TcccClient
from .infrastructure.tccc_audio import TcccAudioGateway
from .infrastructure.whisper_transcriber import FasterWhisperTranscriber
from .infrastructure.ytdlp_audio import YtDlpAudioGateway
from .infrastructure.ytdlp_sections import YtDlpSectionGateway
from .infrastructure.ytdlp_subtitles import YtDlpSubtitleGateway
from .usecases.build_deck import BuildDeckUseCase
from .usecases.conversation import DEFAULT_CHUNK_CHARS
from .usecases.sources import ntpc_clip, tccc_clip

if TYPE_CHECKING:
    from .presentation.main_window import MainWindow

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


class CurrentSettingsBriefWriter:
    """同 CurrentSettingsSummarizer：每次都用當下的 model／host。"""

    def __init__(self, settings: Settings, factory=OllamaBriefWriter):
        self._settings = settings
        self._factory = factory

    def write(
        self,
        slides,
        progress: ProgressCallback,
        is_cancelled: CancelCheck | None = None,
        hint: str = "",
    ) -> Brief:
        s = self._settings
        return self._factory(s.ollama_host, s.model, s.num_ctx).write(
            slides, progress, is_cancelled, hint)


class CurrentSettingsSlideRewriter:
    """同 CurrentSettingsSummarizer：每次都用當下的 model／host。"""

    def __init__(self, settings: Settings, factory=OllamaSlideRewriter):
        self._settings = settings
        self._factory = factory

    def rewrite(
        self,
        slide: Slide,
        problem: str,
        progress: ProgressCallback,
        is_cancelled: CancelCheck | None = None,
    ) -> Slide:
        s = self._settings
        return self._factory(s.ollama_host, s.model, s.num_ctx).rewrite(
            slide, problem, progress, is_cancelled)


class CurrentSettingsConversationSummarizer:
    """同 CurrentSettingsSummarizer：每次都用當下的 model／host。

    參數必須與 ConversationSummarizer port 完全一致，理由同上：多人模式的窗一個一個送進來，
    簽章少一個參數只會在第一個窗炸成 TypeError，而整份語音辨識與說話者區分已經做完了。
    """

    def __init__(self, settings: Settings, factory=OllamaConversationSummarizer):
        self._settings = settings
        self._factory = factory

    @property
    def fingerprint(self) -> str:
        """轉給真正的摘要器問：它的程式版本就是這一層的程式版本（ConversationSummarizer.fingerprint）。"""
        s = self._settings
        return self._factory(s.ollama_host, s.model, s.num_ctx).fingerprint

    def summarize_window(
        self,
        window_text: str,
        first_turn: int,
        last_turn: int,
        citable_turns: Sequence[int],
        min_slides: int,
        max_slides: int,
        hint: str,
        progress: ProgressCallback,
        is_cancelled: CancelCheck | None = None,
    ) -> tuple[RawSlide, ...]:
        s = self._settings
        impl = self._factory(s.ollama_host, s.model, s.num_ctx)
        return impl.summarize_window(window_text, first_turn, last_turn, citable_turns, min_slides,
                                     max_slides, hint, progress, is_cancelled)


def build_diarizer() -> PyannoteSpeakerDiarizer:
    """建立延遲載入的說話者區分器；沒有 token 時仍可跑議會的單人工作。"""
    return PyannoteSpeakerDiarizer()


def build_usecase(
    settings: Settings,
    diarizer: PyannoteSpeakerDiarizer | None = None,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    quote_only: bool = False,
) -> BuildDeckUseCase:
    """組好一條完整的 pipeline。

    刻意與 UI 分開：這個函式不碰 PySide6，所以無介面的服務（例如每天跑一輪
    產出網頁的排程）可以直接 import 它來用。

    字幕、片段、音訊三個 gateway 依網址分派：立法院 IVOD 走開放 API（逐字稿是
    立法院自己用 WhisperX 產好的）與 ffmpeg 切 HLS，臺中、新北兩個市議會走
    語音辨識與 ffmpeg 切 HLS，其餘走 yt-dlp。
    同一個來源的 adapter 共用同一個 client，讓同一筆 record 只抓一次。
    """
    ivod_client = IvodClient()
    # 臺中市議會沒有逐字稿：字幕 gateway 直接說沒有，音訊用 ffmpeg 抓 HLS 後
    # 交給 Whisper；截圖與 IVOD 同一套 ffmpeg 切 m3u8，只差「怎麼找到串流」。
    tccc_client = TcccClient()
    # 新北市議會同理；差別是音訊走 Wowza 的純音訊串流（wowzaaudioonly），
    # 截圖才用一般的影像串流。
    ntpc_client = NtpcClient()
    # 單人與多人共用同一個 Whisper 實例：模型約 40 秒才載入完，載兩份是浪費顯示卡記憶體
    transcriber = FasterWhisperTranscriber(settings.whisper_model)
    return BuildDeckUseCase(
        BySourceSubtitleGateway(
            YtDlpSubtitleGateway(), IvodSubtitleGateway(ivod_client),
            tccc=NoSubtitlesGateway("臺中市議會沒有逐字稿，改用語音辨識"),
            ntpc=NoSubtitlesGateway("新北市議會沒有逐字稿，改用語音辨識")),
        CurrentSettingsSummarizer(settings),
        BySourceSectionGateway(
            YtDlpSectionGateway(), IvodSectionGateway(ivod_client),
            tccc=HlsSectionGateway(lambda url: tccc_client.video_url(tccc_clip(url))),
            ntpc=HlsSectionGateway(lambda url: ntpc_client.video_url(ntpc_clip(url)))),
        FfmpegFrameExtractor(),
        HtmlDeckRenderer(),
        # 沒有字幕時的語音辨識備援。模型在第一次需要時才載入（約 40 秒）並
        # 快取在這個實例裡；改 whisper_model 需重開 app（語言模型與 host 則每次生成時即時讀取）。
        audio=BySourceAudioGateway(YtDlpAudioGateway(), tccc=TcccAudioGateway(tccc_client),
                                   ntpc=NtpcAudioGateway(ntpc_client)),
        transcriber=transcriber,
        diarizer=diarizer or build_diarizer(),
        # 詳細模式下多寫一張摘要卡（一句話、關鍵數字、要求與回應）
        brief_writer=CurrentSettingsBriefWriter(settings),
        # 摘要有第一人稱時只把那幾頁送回去改，不整份重產
        rewriter=CurrentSettingsSlideRewriter(settings),
        # 停了可以接著做：逐字稿、摘要、摘要卡存在輸出資料夾的 .slidebox_cache/
        checkpoints=JsonCheckpointStore(),
        # 多人模式（speaker_mode="diarize"）：每個字帶時間的語音辨識、對談重點的摘要器、窗的大小、
        # 要不要把每個重點都換成核對過的原話（安全模式）。單人與議會的工作完全不經過這四個。
        word_transcriber=transcriber,
        # 只處理影片的一段區間（time_range）時，先把下載好的音訊切成那一段，再交給說話者區分與逐字辨識
        audio_cutter=FfmpegAudioCutter(),
        conversation=CurrentSettingsConversationSummarizer(settings),
        chunk_chars=chunk_chars,
        quote_only=quote_only,
    )


def build_main_window() -> "MainWindow":
    # presentation 的 import 刻意放在函式內：module 層級 import 會讓
    # `from slidebox.composition import build_usecase` 一併拉進 PySide6，
    # 無頭機器（沒有 libGL/xcb）連 import 都會失敗——那正是抽出
    # build_usecase 要服務的場景。
    from .presentation.deck_page import DeckPage
    from .presentation.main_window import MainWindow

    settings_repo = JsonSettingsRepository(
        os.path.join(_project_root(), "slidebox_settings.json")
    )
    settings = settings_repo.load(Settings(output_dir=DEFAULT_OUTPUT_DIR))

    def save() -> None:
        settings_repo.save(settings)

    page = DeckPage(build_usecase(settings),
                    OllamaModelCatalog(settings.ollama_host), settings, save)
    return MainWindow(page)
