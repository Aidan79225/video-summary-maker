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


def _find_original(
    tracks: Mapping[str, object] | None, original_language: str | None
) -> str | None:
    """在自動字幕裡找「影片原始語言」那一軌，也就是未經翻譯的語音辨識結果。

    YouTube 的自動字幕同時提供原文辨識與約 150 種機器翻譯，翻譯軌的 URL
    帶 tlang= 參數。翻譯是「辨識錯誤 + 翻譯錯誤」兩層損失，而且實測翻譯
    端點會回 HTTP 429 限流。把原文交給模型、讓它邊摘要邊翻譯更好。

    language 可能帶區域或字體標記（實測見過 zh-Hant），所以精確比對之後
    再退讓到基底語言。
    """
    if not original_language or not tracks:
        return None
    base = original_language.split("-")[0]
    for key in (f"{original_language}-orig", original_language, f"{base}-orig", base):
        if key in tracks:
            return key
    return None


def pick_subtitle_track(
    subtitles: Mapping[str, object] | None,
    automatic_captions: Mapping[str, object] | None,
    prefer: Sequence[str],
    original_language: str | None = None,
) -> tuple[str, bool]:
    """從 yt-dlp 的兩份字典挑一軌，回傳 (語言鍵, 是否為自動字幕)。

    優先序三層：

    1. 人工字幕，依偏好序——人工翻譯品質高出太多，寧可語言排序退讓。
    2. 自動字幕中的原始語言軌——原文只有辨識一層損失，機器翻譯是兩層。
    3. 自動字幕，依偏好序——即 YouTube 的機器翻譯，最後手段。

    都沒有則 raise NoSubtitlesAvailable。
    """
    hit = _find_lang(subtitles, prefer)
    if hit is not None:
        return hit, False
    hit = _find_original(automatic_captions, original_language)
    if hit is not None:
        return hit, True
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

# 少於這個長度的重疊視為巧合而非滾動重複。中文相鄰句子單字重疊很常見
# （「…很好」接「好的…」），沒有這道門檻就會把正常內容當成重複刪掉。
_MIN_OVERLAP = 3

# 合併字幕的時間窗。摘要與逐字稿必須用同一個值，否則兩邊的時間戳對不起來。
_WINDOW = 15.0


def _dedupe(cues: Sequence[Cue], is_automatic: bool = True) -> list[tuple[float, str]]:
    """移除滾動字幕的重複內容，回傳 (起始秒數, 新增文字)。

    自動字幕是滾動式的：同一句話會在連續數個 cue 裡逐字重複出現。
    直接丟給 LLM 會浪費一半以上的 context。

    is_automatic 為 False（手動字幕）時完全略過這套滾動去重：手動字幕
    不會滾動，去重只有壞處，只濾掉空白 cue。
    """
    if not is_automatic:
        return [(cue.start, " ".join(cue.text.split())) for cue in cues
                if " ".join(cue.text.split())]
    out: list[tuple[float, str]] = []
    prev = ""
    for cue in cues:
        text = " ".join(cue.text.split())
        # 只有夠長的重複才視為滾動字幕的殘留；短句（對／好／是啊）本身就是內容
        if not text or (len(text) >= _MIN_OVERLAP and text in prev):
            continue
        # 找出 prev 的最長結尾，同時是 text 的開頭
        k = min(len(prev), len(text))
        while k > 0 and prev[-k:] != text[:k]:
            k -= 1
        if k < _MIN_OVERLAP:
            k = 0          # 巧合等級的重疊不修剪
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


def compress_cues(
    cues: Sequence[Cue], char_budget: int, window: float = _WINDOW,
    is_automatic: bool = True,
) -> str:
    """把數千句字幕壓成帶秒數標記的段落文字，長度不超過 char_budget。

    標記用秒數而非 MM:SS：模型被要求輸出的 timestamp 是秒數，讓它直接
    從標記抄一個數字，遠比要求它做換算可靠。

    is_automatic 為 False 時（手動字幕）略過滾動去重——手動字幕不會
    滾動，去重只會誤刪正常的相鄰句重疊或短句。
    """
    pairs = _dedupe(cues, is_automatic)
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


# 詳細模式下一段敘述至少要有的字數。要求是 150～300 字，這個門檻只抓
# 「空字串」與「就是這樣」這種敷衍，不去管稍微短一點的正常輸出。
_MIN_DETAIL = 40


def validate_slides(slides: Sequence[Slide], min_slides: int, max_slides: int,
                    detailed: bool = False) -> list[str]:
    """回傳問題描述清單；空清單表示通過。

    不檢查時間戳——那一律由 clamp_timestamps 先處理掉。這裡抓的是
    「模型沒照指示做」的問題，需要重試才能修正。

    detailed 時多檢查 detail：JSON schema 的 required 只擋得住「少了欄位」，
    擋不住空字串。少了這一關，使用者勾了詳細、等了兩倍時間，拿到的成品
    跟一般模式一模一樣，而完成訊息照樣說「✅ 完成」。
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
        if detailed and len(slide.detail.strip()) < _MIN_DETAIL:
            problems.append(
                f"第 {slide.index} 頁的 detail 太短或空白，需要 150 到 300 字的完整敘述")
    return problems


# --- 完整逐字稿 ---

_MARKER_RE = re.compile(r"^\[(\d+)\] ", re.MULTILINE)


def readable_transcript(text: str) -> str:
    """把 `[秒數] 文字` 的行首標記換成 `mm:ss 文字`。

    秒數標記是給模型抄的（見 compress_cues），人要看的是分秒。超過一小時
    不換成 hh:mm:ss——分鐘持續累加（62:05）不會誤讀，也省掉一種格式。
    """
    def repl(m: re.Match[str]) -> str:
        total = int(m.group(1))
        return f"{total // 60:02d}:{total % 60:02d} "

    return _MARKER_RE.sub(repl, text)


def full_transcript(cues: Sequence[Cue], is_automatic: bool = True) -> str:
    """整份逐字稿，不套用字元預算——這是給人讀的，不進模型的 context。

    只做去重與時間分段，內容一字不刪：使用者要的正是「不看影片也知道
    全部講了什麼」。
    """
    return readable_transcript(_group(_dedupe(cues, is_automatic), _WINDOW))
