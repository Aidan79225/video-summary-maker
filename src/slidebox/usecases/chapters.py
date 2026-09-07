"""字幕處理的純邏輯：挑軌、解析、壓縮、驗證。

這個模組不碰網路、不碰磁碟、不碰 LLM，是本專案自動化測試的主要對象。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

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
