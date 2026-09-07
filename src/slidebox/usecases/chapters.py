"""字幕處理的純邏輯：挑軌、解析、壓縮、驗證。

這個模組不碰網路、不碰磁碟、不碰 LLM，是本專案自動化測試的主要對象。
"""
from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence

from ..domain.entities import Cue
from ..domain.errors import NoSubtitlesAvailable


def _find_lang(tracks: Mapping[str, object] | None, prefer: Sequence[str]) -> str | None:
    """在一份語言字典裡依偏好序找一個鍵：先精確比對，再比語言前綴。"""
    tracks = tracks or {}
    for lang in prefer:
        if lang in tracks:
            return lang
    # YouTube 常給 en-US、zh-Hant-TW 這類鍵，精確比對會落空
    for lang in prefer:
        base = lang.split("-")[0]
        for key in tracks:
            if key.split("-")[0] == base:
                return key
    return None


def pick_subtitle_track(
    subtitles: Mapping[str, object] | None,
    automatic_captions: Mapping[str, object] | None,
    prefer: Sequence[str],
) -> tuple[str, bool]:
    """從 yt-dlp 的兩份字典挑一軌，回傳 (語言鍵, 是否為自動字幕)。

    手動字幕整體優先於自動字幕——人工字幕品質高出太多，寧可語言排序
    退讓。都沒有則 raise NoSubtitlesAvailable。
    """
    hit = _find_lang(subtitles, prefer)
    if hit is not None:
        return hit, False
    hit = _find_lang(automatic_captions, prefer)
    if hit is not None:
        return hit, True
    raise NoSubtitlesAvailable("這部影片沒有可用的字幕")


# --- WebVTT 解析 ---

_TIME_RE = re.compile(r"(\d{2,}):(\d{2}):(\d{2})[.,](\d{3})")
_TAG_RE = re.compile(r"<[^>]*>")
# 區塊開頭若是這些關鍵字，整塊不是字幕內容
_HEADER_PREFIXES = ("WEBVTT", "NOTE", "STYLE", "REGION")


def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_vtt(text: str) -> tuple[Cue, ...]:
    """解析 WebVTT 成 Cue 序列。無法解析的區塊靜默略過。"""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    for block in text.split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines or lines[0].startswith(_HEADER_PREFIXES):
            continue
        timing_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if timing_idx is None:
            continue
        times = _TIME_RE.findall(lines[timing_idx])
        if len(times) < 2:
            continue
        body = " ".join(lines[timing_idx + 1:])
        body = _TAG_RE.sub("", body)          # <c>、<00:00:01.100> 等
        body = html.unescape(body)
        body = re.sub(r"\s+", " ", body).strip()
        if not body:
            continue
        cues.append(Cue(
            start=_to_seconds(*times[0]),
            end=_to_seconds(*times[1]),
            text=body,
        ))
    return tuple(cues)
