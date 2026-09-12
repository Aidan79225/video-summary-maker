"""SettingsRepository 的 JSON 檔案實作。"""
from __future__ import annotations

import json
import os

from ..domain.entities import Settings


class JsonSettingsRepository:
    def __init__(self, path: str):
        self._path = path

    def load(self, default: Settings) -> Settings:
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return default
        if not isinstance(data, dict):
            return default

        langs = data.get("subtitle_langs")
        return Settings(
            output_dir=data.get("output_dir", default.output_dir),
            model=data.get("model", default.model),
            ollama_host=data.get("ollama_host", default.ollama_host),
            max_height=data.get("max_height", default.max_height),
            min_slides=data.get("min_slides", default.min_slides),
            max_slides=data.get("max_slides", default.max_slides),
            image_width=data.get("image_width", default.image_width),
            num_ctx=data.get("num_ctx", default.num_ctx),
            char_budget=data.get("char_budget", default.char_budget),
            whisper_model=data.get("whisper_model", default.whisper_model),
            detailed=bool(data.get("detailed", default.detailed)),
            # 空清單或型別不對都視同未設定：偏好語言全空會讓字幕挑軌永遠
            # 失敗，而且症狀會顯示成「這部影片沒有字幕」，是誤導使用者的
            # 錯誤訊息。退回預設值比忠實還原一個會讓 app 不可用的值有用。
            subtitle_langs=(
                tuple(langs) if isinstance(langs, list) and langs
                else default.subtitle_langs
            ),
        )

    def save(self, settings: Settings) -> None:
        data = {
            "output_dir": settings.output_dir,
            "model": settings.model,
            "ollama_host": settings.ollama_host,
            "max_height": settings.max_height,
            "min_slides": settings.min_slides,
            "max_slides": settings.max_slides,
            "image_width": settings.image_width,
            "num_ctx": settings.num_ctx,
            "char_budget": settings.char_budget,
            "subtitle_langs": list(settings.subtitle_langs),
            "whisper_model": settings.whisper_model,
            "detailed": settings.detailed,
        }
        os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
