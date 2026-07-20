"""SettingsRepository 的 JSON 檔案實作。"""
from __future__ import annotations

import json
import os

from ..domain.entities import DownloadFormat, RenameMode, Settings


class JsonSettingsRepository:
    def __init__(self, path: str):
        self._path = path

    def load(self, default: Settings) -> Settings:
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return default

        return Settings(
            output_dir=data.get("output_dir", default.output_dir),
            rename_folder=data.get("rename_folder", default.rename_folder),
            fmt=self._parse_fmt(data.get("fmt"), default.fmt),
            max_height=data.get("max_height", default.max_height),
            separator=data.get("separator", default.separator),
            mode=self._parse_mode(data.get("mode"), default.mode),
            padding=data.get("padding", default.padding),
        )

    def save(self, settings: Settings) -> None:
        data = {
            "output_dir": settings.output_dir,
            "rename_folder": settings.rename_folder,
            "fmt": settings.fmt.value,
            "max_height": settings.max_height,
            "separator": settings.separator,
            "mode": settings.mode.value,
            "padding": settings.padding,
        }
        os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _parse_fmt(value, default: DownloadFormat) -> DownloadFormat:
        try:
            return DownloadFormat(value)
        except ValueError:
            return default

    @staticmethod
    def _parse_mode(value, default: RenameMode) -> RenameMode:
        try:
            return RenameMode(value)
        except ValueError:
            return default
