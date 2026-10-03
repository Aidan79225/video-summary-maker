"""把 Deck、議題分類與追問判斷的結果轉成新聞服務要的 JSON 材料。

圖片以 base64 隨 JSON 走：Pi 上沒有這台機器的檔案系統，而另外開一個圖片
端點要處理保存期限、清理與授權——一篇 4～10 頁、每頁 20～40 KB，為了省
這幾百 KB 不值得。
"""
from __future__ import annotations

import base64
import os
from enum import StrEnum

from slidebox.domain.entities import Brief, Deck, FollowUpResult, Slide, TopicResult

_MEDIA_TYPE = "image/webp"


class DeckField(StrEnum):
    """回給新聞服務的欄位名。這是 HTTP 契約，改名等於改 API。"""
    VIDEO_ID = "video_id"
    SOURCE_URL = "source_url"
    TITLE = "title"
    SOURCE_NOTE = "source_note"
    TRANSCRIPT = "transcript_text"
    SLIDES = "slides"
    BRIEF = "brief"


class SlideField(StrEnum):
    INDEX = "index"
    TITLE = "title"
    BULLETS = "bullets"
    DETAIL = "detail"
    TIMESTAMP = "timestamp"
    IMAGE = "image_base64"
    IMAGE_TYPE = "image_media_type"


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
        SlideField.INDEX: slide.index,
        SlideField.TITLE: slide.title,
        SlideField.BULLETS: list(slide.bullets),
        SlideField.DETAIL: slide.detail,
        SlideField.TIMESTAMP: slide.timestamp,
        SlideField.IMAGE: encoded,
        SlideField.IMAGE_TYPE: _MEDIA_TYPE if encoded else None,
    }


class BriefField(StrEnum):
    ONE_LINER = "one_liner"
    KEY_NUMBERS = "key_numbers"
    ASKS = "asks"


def brief_payload(brief: Brief | None) -> dict | None:
    """沒有卡片就是 None，不是空物件：Pi 那邊要能分辨「沒產出」與「產出但空」。"""
    if brief is None:
        return None
    return {
        BriefField.ONE_LINER: brief.one_liner,
        BriefField.KEY_NUMBERS: [
            {"value": n.value, "unit": n.unit, "label": n.label, "quote": n.quote,
             "law": n.law, "article": n.article}
            for n in brief.key_numbers
        ],
        BriefField.ASKS: [
            {"request": a.request, "deadline": a.deadline, "response": a.response}
            for a in brief.asks
        ],
    }


def deck_payload(deck: Deck, video_id: str) -> dict:
    return {
        DeckField.VIDEO_ID: video_id,
        DeckField.SOURCE_URL: deck.source_url,
        DeckField.TITLE: deck.video_title,
        DeckField.SOURCE_NOTE: deck.source_note,
        DeckField.TRANSCRIPT: deck.transcript_text,
        DeckField.SLIDES: [_slide_payload(s) for s in deck.slides],
        DeckField.BRIEF: brief_payload(deck.brief),
    }


class TopicField(StrEnum):
    """分類工作回給新聞服務的欄位名。同 DeckField：改名等於改 API。"""
    PRIMARY = "primary"
    SECONDARY = "secondary"
    CLASSIFIER = "classifier"


def topic_payload(result: TopicResult) -> dict:
    """領域一律給代碼，不給名稱：新聞服務用代碼存資料庫，名稱只是給模型看的。

    沒有次領域是 None（JSON 的 null），不是空字串：空字串會被當成一個叫
    「」的領域。
    """
    return {
        TopicField.PRIMARY: result.primary,
        TopicField.SECONDARY: result.secondary,
        TopicField.CLASSIFIER: result.classifier,
    }


class FollowUpField(StrEnum):
    """追問工作回給新聞服務的欄位名。同 DeckField：改名等於改 API。"""
    FOLLOWED_UP = "followed_up"
    QUOTE = "quote"
    CLASSIFIER = "classifier"


def followup_payload(result: FollowUpResult) -> dict:
    """判斷原樣交出去，不在這裡做落地檢查：引用是不是真的在逐字稿裡，要看整份
    逐字稿，那份在新聞服務手上。"""
    return {
        FollowUpField.FOLLOWED_UP: result.followed_up,
        FollowUpField.QUOTE: result.quote,
        FollowUpField.CLASSIFIER: result.classifier,
    }
