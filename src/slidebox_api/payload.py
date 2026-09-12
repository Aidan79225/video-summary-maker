"""把 Deck 轉成新聞服務要的 JSON 材料。

圖片以 base64 隨 JSON 走：Pi 上沒有這台機器的檔案系統，而另外開一個圖片
端點要處理保存期限、清理與授權——一篇 4～10 頁、每頁 20～40 KB，為了省
這幾百 KB 不值得。
"""
from __future__ import annotations

import base64
import os

from slidebox.domain.entities import Deck, Slide

_MEDIA_TYPE = "image/webp"


def _image_base64(path: str | None) -> str | None:
    """讀圖轉 base64；沒有圖或讀不到就回 None（該頁降級成無圖）。"""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return None


def _slide_payload(slide: Slide) -> dict:
    encoded = _image_base64(slide.image_path)
    return {
        "index": slide.index,
        "title": slide.title,
        "bullets": list(slide.bullets),
        "detail": slide.detail,
        "timestamp": slide.timestamp,
        "image_base64": encoded,
        "image_media_type": _MEDIA_TYPE if encoded else None,
    }


def deck_payload(deck: Deck, video_id: str) -> dict:
    return {
        "video_id": video_id,
        "source_url": deck.source_url,
        "title": deck.video_title,
        "source_note": deck.source_note,
        "transcript_text": deck.transcript_text,
        "slides": [_slide_payload(s) for s in deck.slides],
    }
