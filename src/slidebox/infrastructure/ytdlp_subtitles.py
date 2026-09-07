"""SubtitleGateway 的 yt-dlp 實作：只抓字幕，不下載影片。"""
from __future__ import annotations

import glob
import os
import tempfile
from collections.abc import Sequence

import yt_dlp

from ..domain.entities import Transcript
from ..domain.errors import NoSubtitlesAvailable
from ..usecases.chapters import parse_vtt, pick_subtitle_track


class YtDlpSubtitleGateway:
    def fetch(self, url: str, langs: Sequence[str]) -> Transcript:
        with tempfile.TemporaryDirectory(prefix="slidebox_subs_") as tmp:
            info = self._probe(url)
            lang, is_auto = pick_subtitle_track(
                info.get("subtitles"), info.get("automatic_captions"), langs
            )
            vtt_text = self._download_track(url, lang, is_auto, tmp)

        cues = parse_vtt(vtt_text)
        if not cues:
            raise NoSubtitlesAvailable("字幕檔下載成功但沒有任何內容")

        return Transcript(
            video_id=info.get("id") or "video",
            title=info.get("title") or "未命名影片",
            duration=float(info.get("duration") or 0.0),
            cues=cues,
            language=lang,
            is_automatic=is_auto,
        )

    @staticmethod
    def _probe(url: str) -> dict:
        opts = {"skip_download": True, "quiet": True, "no_warnings": True,
                "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False) or {}

    @staticmethod
    def _download_track(url: str, lang: str, is_auto: bool, tmp_dir: str) -> str:
        opts = {
            "skip_download": True,
            "writesubtitles": not is_auto,
            "writeautomaticsub": is_auto,
            "subtitleslangs": [lang],
            "subtitlesformat": "vtt",
            "outtmpl": os.path.join(tmp_dir, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        # yt-dlp 會寫成 <id>.<lang>.vtt，語言後綴可能與請求的鍵略有出入
        files = glob.glob(os.path.join(tmp_dir, "*.vtt"))
        if not files:
            raise NoSubtitlesAvailable(f"字幕軌 {lang} 下載失敗")
        with open(files[0], encoding="utf-8") as f:
            return f.read()
