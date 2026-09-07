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
            # JSON 只有 list，還原成 tuple 才能與預設值比較相等
            subtitle_langs=tuple(langs) if langs else default.subtitle_langs,
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
        }
        os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
