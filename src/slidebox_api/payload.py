"""把 Deck、議題分類與追問判斷的結果轉成新聞服務要的 JSON 材料。

圖片以 base64 隨 JSON 走：Pi 上沒有這台機器的檔案系統，而另外開一個圖片
端點要處理保存期限、清理與授權——一篇 4～10 頁、每頁 20～40 KB，為了省
這幾百 KB 不值得。
"""
from __future__ import annotations

import base64
import os
from enum import StrEnum

from slidebox.domain.entities import (
    Brief,
    Deck,
    DiarizationReport,
    FollowUpResult,
    Point,
    Slide,
    SpeakerInfo,
    TopicResult,
)

from .jobs import SpeakerMode

_MEDIA_TYPE = "image/webp"

# 多人模式結果的契約版本（/capabilities 的 speaker_contract）。欄位的增減、改名或改意義就要升版；
# 新聞服務只在版本相同時才派多人的工作。
SPEAKER_CONTRACT = 1


class DeckField(StrEnum):
    """回給新聞服務的欄位名。這是 HTTP 契約，改名等於改 API。"""
    VIDEO_ID = "video_id"
    SOURCE_URL = "source_url"
    TITLE = "title"
    SOURCE_NOTE = "source_note"
    TRANSCRIPT = "transcript_text"
    SLIDES = "slides"
    BRIEF = "brief"
    # 以下三個只有多人模式（speaker_mode="diarize"）的結果才有；單人結果連空的都不帶
    SPEAKER_MODE = "speaker_mode"
    SPEAKERS = "speakers"
    DIARIZATION = "diarization"


class SlideField(StrEnum):
    INDEX = "index"
    TITLE = "title"
    BULLETS = "bullets"
    DETAIL = "detail"
    TIMESTAMP = "timestamp"
    IMAGE = "image_base64"
    IMAGE_TYPE = "image_media_type"
    # 只有多人模式才有：這一頁每個重點由誰說、出自哪一段話
    POINTS = "points"


class PointField(StrEnum):
    SPEAKER = "speaker"
    TURN = "turn"
    START = "start"
    TEXT = "text"
    EVIDENCE = "evidence"


class SpeakerField(StrEnum):
    ID = "id"
    LABEL = "label"
    KIND = "kind"
    SECONDS = "seconds"
    TURNS = "turns"
    FIRST_SEEN = "first_seen"
    LAST_SEEN = "last_seen"
    SAMPLES = "samples"
    FLAGS = "flags"


class DiarizationField(StrEnum):
    ENGINE = "engine"
    VERSION = "version"
    MODEL = "model"
    FINGERPRINT = "fingerprint"
    STATUS = "status"
    WARNINGS = "warnings"
    REQUESTED_SPEAKERS = "requested_speakers"
    FOUND_SPEAKERS = "found_speakers"
    MINOR_SPEAKERS = "minor_speakers"
    SPEECH_SECONDS = "speech_seconds"
    UNASSIGNED_RATIO = "unassigned_ratio"
    OVERLAP_RATIO = "overlap_ratio"
    PROPOSED_POINTS = "proposed_points"
    KEPT_POINTS = "kept_points"


def _image_base64(path: str | None) -> str | None:
    """讀圖轉 base64；沒有圖或讀不到就回 None（該頁降級成無圖）。"""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return None


def _slide_payload(slide: Slide, with_points: bool = False) -> dict:
    encoded = _image_base64(slide.image_path)
    payload = {
        SlideField.INDEX: slide.index,
        SlideField.TITLE: slide.title,
        SlideField.BULLETS: list(slide.bullets),
        SlideField.DETAIL: slide.detail,
        SlideField.TIMESTAMP: slide.timestamp,
        SlideField.IMAGE: encoded,
        SlideField.IMAGE_TYPE: _MEDIA_TYPE if encoded else None,
    }
    if with_points:
        payload[SlideField.POINTS] = [_point_payload(p) for p in slide.points]
    return payload


def _point_payload(point: Point) -> dict:
    return {
        PointField.SPEAKER: point.speaker,
        PointField.TURN: point.turn,
        PointField.START: point.start,
        PointField.TEXT: point.text,
        PointField.EVIDENCE: point.evidence,
    }


def _speaker_payload(speaker: SpeakerInfo) -> dict:
    # 秒數是一堆小數加起來的，湊成一位小數再交出去，免得 JSON 裡出現 301.49999999999994
    return {
        SpeakerField.ID: speaker.id,
        SpeakerField.LABEL: speaker.label,
        SpeakerField.KIND: speaker.kind,
        SpeakerField.SECONDS: round(speaker.seconds, 1),
        SpeakerField.TURNS: speaker.turns,
        SpeakerField.FIRST_SEEN: speaker.first_seen,
        SpeakerField.LAST_SEEN: speaker.last_seen,
        SpeakerField.SAMPLES: [{"start": s.start, "text": s.text} for s in speaker.samples],
        SpeakerField.FLAGS: list(speaker.flags),
    }


def _diarization_payload(report: DiarizationReport) -> dict:
    return {
        DiarizationField.ENGINE: report.engine,
        DiarizationField.VERSION: report.version,
        DiarizationField.MODEL: report.model,
        DiarizationField.FINGERPRINT: report.fingerprint,
        DiarizationField.STATUS: report.status,
        DiarizationField.WARNINGS: list(report.warnings),
        DiarizationField.REQUESTED_SPEAKERS: report.requested_speakers,
        DiarizationField.FOUND_SPEAKERS: report.found_speakers,
        DiarizationField.MINOR_SPEAKERS: report.minor_speakers,
        DiarizationField.SPEECH_SECONDS: round(report.speech_seconds, 1),
        DiarizationField.UNASSIGNED_RATIO: round(report.unassigned_ratio, 3),
        DiarizationField.OVERLAP_RATIO: round(report.overlap_ratio, 3),
        DiarizationField.PROPOSED_POINTS: report.proposed_points,
        DiarizationField.KEPT_POINTS: report.kept_points,
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


_SPEAKER_MODES = frozenset(mode.value for mode in SpeakerMode)


def deck_payload(deck: Deck, video_id: str) -> dict:
    """單人結果的形狀與 origin/master 逐鍵相同；多人結果（speaker_mode="diarize"）才多出
    speaker_mode、speakers、diarization 與每頁的 points（契約 v1，見 speaker_contract_v1.json）。

    多人結果缺任何一塊（說話者清單、說話者區分報告、某一頁的出處）就 raise ValueError，工作失敗：
    少了出處的多人結果會讓新聞服務把一段沒有人負責的話發佈出去。
    """
    if deck.speaker_mode not in _SPEAKER_MODES:
        raise ValueError(f"不認得的 speaker_mode：{deck.speaker_mode!r}")
    diarized = deck.speaker_mode == SpeakerMode.DIARIZE
    if diarized:
        _require_attributed(deck)
    payload = {
        DeckField.VIDEO_ID: video_id,
        DeckField.SOURCE_URL: deck.source_url,
        DeckField.TITLE: deck.video_title,
        DeckField.SOURCE_NOTE: deck.source_note,
        DeckField.TRANSCRIPT: deck.transcript_text,
        DeckField.SLIDES: [_slide_payload(s, with_points=diarized) for s in deck.slides],
        DeckField.BRIEF: brief_payload(deck.brief),
    }
    if diarized:
        payload[DeckField.SPEAKER_MODE] = SpeakerMode.DIARIZE.value
        payload[DeckField.SPEAKERS] = [_speaker_payload(s) for s in deck.speakers]
        payload[DeckField.DIARIZATION] = _diarization_payload(deck.diarization)
    return payload


def _require_attributed(deck: Deck) -> None:
    if deck.diarization is None or not deck.speakers:
        raise ValueError("多人模式的成品缺少說話者清單或說話者區分報告，不能交出去")
    if deck.brief is not None:
        raise ValueError("多人模式不寫質詢卡（brief 一律是 null），這份成品不符合契約")
    unattributed = [s.index for s in deck.slides if not s.points]
    if unattributed:
        raise ValueError(f"多人模式的第 {'、'.join(map(str, unattributed))} 頁沒有任何重點的出處，不能交出去")


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
