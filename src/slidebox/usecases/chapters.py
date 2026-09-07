"""字幕處理的純邏輯：挑軌、解析、壓縮、驗證。

這個模組不碰網路、不碰磁碟、不碰 LLM，是本專案自動化測試的主要對象。
"""
from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace

from ..domain.entities import Cue, Slide
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


# --- 字幕壓縮 ---


def _dedupe(cues: Sequence[Cue]) -> list[tuple[float, str]]:
    """移除滾動字幕的重複內容，回傳 (起始秒數, 新增文字)。

    自動字幕是滾動式的：同一句話會在連續數個 cue 裡逐字重複出現。
    直接丟給 LLM 會浪費一半以上的 context。
    """
    out: list[tuple[float, str]] = []
    prev = ""
    for cue in cues:
        text = " ".join(cue.text.split())
        if not text or text in prev:
            continue
        # 找出 prev 的最長結尾，同時是 text 的開頭
        k = min(len(prev), len(text))
        while k > 0 and prev[-k:] != text[:k]:
            k -= 1
        new = text[k:].strip()
        prev = text
        if new:
            out.append((cue.start, new))
    return out


def _group(pairs: Sequence[tuple[float, str]], window: float, keep: float = 1.0) -> str:
    """把 (秒數, 文字) 依時間窗合併成 `[秒數] 文字` 的行。

    keep < 1.0 時，每一段的本文只保留開頭該比例的字元——這樣每個時間窗
    都仍有代表，影片後半段不會整塊消失。
    """
    if not pairs:
        return ""
    lines: list[str] = []
    bucket_start = pairs[0][0]
    parts: list[str] = []

    def flush() -> None:
        if not parts:
            return
        body = " ".join(parts)
        if keep < 1.0:
            body = body[:max(1, int(len(body) * keep))]
        lines.append(f"[{int(bucket_start)}] {body}")

    for start, text in pairs:
        if parts and start - bucket_start >= window:
            flush()
            bucket_start = start
            parts = []
        parts.append(text)
    flush()
    return "\n".join(lines)


def compress_cues(cues: Sequence[Cue], char_budget: int, window: float = 15.0) -> str:
    """把數千句字幕壓成帶秒數標記的段落文字，長度不超過 char_budget。

    標記用秒數而非 MM:SS：模型被要求輸出的 timestamp 是秒數，讓它直接
    從標記抄一個數字，遠比要求它做換算可靠。
    """
    pairs = _dedupe(cues)
    text = _group(pairs, window)
    keep = 1.0
    # 超出預算：等比例截短每一段（而非丟棄整段），保留全片涵蓋。
    # 乘 0.98 留一點餘裕，讓迴圈快速收斂。
    while len(text) > char_budget and keep > 0.02:
        keep *= char_budget / len(text) * 0.98
        text = _group(pairs, window, keep)
    return text


# --- 投影片驗證 ---


def clamp_timestamps(slides: Sequence[Slide], duration: float) -> tuple[Slide, ...]:
    """把越界的時間戳夾回 [0, duration)。

    時間戳越界是小模型的常見小毛病，夾回去就好，不值得為此重跑整次摘要。
    duration <= 0（yt-dlp 給不出長度）時只夾負值。
    """
    out: list[Slide] = []
    for slide in slides:
        ts = slide.timestamp
        if ts < 0:
            ts = 0.0
        elif duration > 0 and ts >= duration:
            ts = max(0.0, duration - 1.0)
        out.append(slide if ts == slide.timestamp else replace(slide, timestamp=ts))
    return tuple(out)


def validate_slides(slides: Sequence[Slide], min_slides: int, max_slides: int) -> list[str]:
    """回傳問題描述清單；空清單表示通過。

    不檢查時間戳——那一律由 clamp_timestamps 先處理掉。這裡抓的是
    「模型沒照指示做」的問題，需要重試才能修正。
    """
    problems: list[str] = []
    if len(slides) < min_slides:
        problems.append(f"只產出 {len(slides)} 頁，少於下限 {min_slides} 頁")
    if len(slides) > max_slides:
        problems.append(f"產出 {len(slides)} 頁，超過上限 {max_slides} 頁")
    for slide in slides:
        if not slide.title.strip():
            problems.append(f"第 {slide.index} 頁的標題是空白")
        if not slide.bullets:
            problems.append(f"第 {slide.index} 頁沒有任何重點條列")
    return problems
