"""Composition root：組裝所有相依（依賴注入）。"""
from __future__ import annotations

import os

from .domain.entities import Settings
from .infrastructure.filesystem_renamer import FilesystemGateway
from .infrastructure.settings_repository import JsonSettingsRepository
from .infrastructure.tags import MutagenTagGateway
from .infrastructure.ytdlp_downloader import YtDlpDownloader
from .presentation.main_window import MainWindow
from .presentation.pages.download_page import DownloadPage
from .presentation.pages.rename_page import RenamePage
from .usecases.download_video import DownloadVideoUseCase
from .usecases.rename_songs import ApplyRenamePlanUseCase, BuildRenamePlanUseCase

# 預設下載／重新編號的目標資料夾
DEFAULT_MUSIC_DIR = r"C:\Users\Aidan\Desktop\音樂 - test"


def _project_root() -> str:
    # 此檔位於 <root>/src/musicbox/composition.py，往上三層即專案根目錄
    here = os.path.abspath(__file__)
    return os.path.dirname(os.path.dirname(os.path.dirname(here)))


def build_main_window() -> MainWindow:
    # infrastructure
    downloader = YtDlpDownloader()
    gateway = FilesystemGateway()
    settings_repo = JsonSettingsRepository(os.path.join(_project_root(), "settings.json"))

    # 設定：載入（首次執行用預設值）
    default_settings = Settings(output_dir=DEFAULT_MUSIC_DIR, rename_folder=DEFAULT_MUSIC_DIR)
    settings = settings_repo.load(default_settings)

    def save() -> None:
        settings_repo.save(settings)

    # usecases
    download_uc = DownloadVideoUseCase(downloader)
    build_uc = BuildRenamePlanUseCase(gateway)
    apply_uc = ApplyRenamePlanUseCase(gateway, MutagenTagGateway())

    # presentation
    download_page = DownloadPage(download_uc, settings, save)
    rename_page = RenamePage(build_uc, apply_uc, settings, save)
    return MainWindow(download_page, rename_page)
