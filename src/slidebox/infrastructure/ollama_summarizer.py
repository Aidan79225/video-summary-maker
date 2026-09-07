"""Summarizer 與 ModelCatalog 的 Ollama 實作。

模組刻意分成兩半：本檔上半是純函式（schema 與回應解析），完全可離線
測試；下半是 HTTP 呼叫，無自動化測試。
"""
from __future__ import annotations

import json

from ..domain.entities import Slide
from ..domain.errors import SummarizerOutputInvalid

# 傳給 Ollama 的 format：文法層級約束，格式錯誤幾乎不可能發生
SLIDES_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "slides": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                    "timestamp": {"type": "number"},
                },
                "required": ["title", "bullets", "timestamp"],
            },
        }
    },
    "required": ["slides"],
}


def _to_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _to_bullets(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if not isinstance(value, list):
        return ()
    out = []
    for item in value:
        if item is None:
            continue
        text = item if isinstance(item, str) else str(item)
        if text.strip():
            out.append(text)
    return tuple(out)


def parse_summary_response(payload: str) -> tuple[Slide, ...]:
    """把模型回應的 JSON 字串轉成 Slide 序列。

    只有「連 slides 陣列都拿不到」才 raise；個別欄位缺失一律降級處理，
    是否要重試交給 validate_slides 決定——這樣重試的判準只有一處。
    """
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as e:
        raise SummarizerOutputInvalid(f"模型回應不是合法的 JSON：{e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("slides"), list):
        raise SummarizerOutputInvalid("模型回應裡沒有 slides 陣列")

    slides: list[Slide] = []
    for i, raw in enumerate(data["slides"], start=1):
        if not isinstance(raw, dict):
            continue
        title = raw.get("title")
        slides.append(Slide(
            index=i,
            title=title if isinstance(title, str) else "",
            bullets=_to_bullets(raw.get("bullets")),
            timestamp=_to_float(raw.get("timestamp")),
        ))
    return tuple(slides)
