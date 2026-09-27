"""臺中市議會「議員個人質詢隨選視訊系統」（vod.tccc.gov.tw）。

沒有 API：片段清單是舊式 ASP 頁 + iframe，影片在第三方播放器（Ginnet）
的頁面裡以 HLS 提供。這裡只做兩件事——解析片段頁拿 metadata 與播放器網址、
解析播放器頁拿 m3u8。逐字稿系統（yishi.tccc.gov.tw）沒有時間戳，不用；
逐字稿交給語音辨識。
"""
from __future__ import annotations

import html
import re
import urllib.request
from dataclasses import dataclass

from ..domain.errors import NoSubtitlesAvailable
from ..usecases.sources import TcccRef

_BASE = "https://vod.tccc.gov.tw"
_TIMEOUT = 30.0

_PLAYER_RE = re.compile(r'<iframe[^>]+src="(https://rds\.ginnet\.cloud/player/[^"]+)"', re.I)
_SPEAKER_RE = re.compile(r">\s*([^<>\s]+)\s+議員\s*<")
_MEETING_RE = re.compile(r'<font color="#0D57BB">([^<]+)</font>')
_DATE_RE = re.compile(r"會議日期：</td>\s*<td[^>]*>\s*(\d{4}-\d{2}-\d{2})")
_DURATION_RE = re.compile(r"影片長度：</td>\s*<td[^>]*>\s*(\d{1,2}:\d{2})")
# 播放器頁把來源寫在 JS 裡：src: 'https://….m3u8?iMda_seq=…'
_STREAM_RE = re.compile(r"""https://[^'"\s]+\.m3u8(?:\?[^'"\s]*)?""")


def _http_get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _hhmm_seconds(text: str) -> int:
    """頁面的「影片長度」是 HH:MM（「00:50」= 50 分鐘，實測 51 分）。分鐘精度。"""
    try:
        hours, minutes = text.split(":")
        return int(hours) * 3600 + int(minutes) * 60
    except ValueError:
        return 0


@dataclass(frozen=True)
class TcccRecord:
    ano: str
    cno: str
    speaker: str
    meeting: str
    date: str
    duration_seconds: int
    player_url: str

    @property
    def title(self) -> str:
        """與立法院文章同形（日期 姓名－會議），前端才不用分兩套排版。"""
        return f"{self.date} {self.speaker}議員－{self.meeting}"


def parse_clip_page(page: str, ref: TcccRef) -> TcccRecord:
    """從 wb_region02.asp 的「選中片段」區塊取 metadata。缺任何一項都算解析失敗。"""
    player = _PLAYER_RE.search(page)
    speaker = _SPEAKER_RE.search(page)
    meeting = _MEETING_RE.search(page)
    day = _DATE_RE.search(page)
    if not (player and speaker and meeting and day):
        raise NoSubtitlesAvailable("臺中市議會的片段頁解析失敗，頁面格式可能改了")
    duration = _DURATION_RE.search(page)
    return TcccRecord(
        ano=ref.ano,
        cno=ref.cno,
        speaker=html.unescape(speaker.group(1)).strip(),
        meeting=html.unescape(meeting.group(1)).strip(),
        date=day.group(1),
        duration_seconds=_hhmm_seconds(duration.group(1)) if duration else 0,
        player_url=html.unescape(player.group(1)),
    )


def parse_stream_url(page: str) -> str | None:
    match = _STREAM_RE.search(page)
    return match.group(0) if match else None


class TcccClient:
    """一段片段兩個 gateway 都要用（音訊要串流、截圖也要串流），快取最後一筆。"""

    def __init__(self, fetch=_http_get, base: str = _BASE):
        self._fetch = fetch
        self._base = base.rstrip("/")
        self._cached: tuple[TcccRef, TcccRecord] | None = None

    def clip_page_url(self, ref: TcccRef) -> str:
        return f"{self._base}/wb_region02.asp?url=12&cno={ref.cno}&ano={ref.ano}&pageno=1"

    def record(self, ref: TcccRef) -> TcccRecord:
        if self._cached and self._cached[0] == ref:
            return self._cached[1]
        try:
            page = self._fetch(self.clip_page_url(ref))
        except OSError as e:
            raise NoSubtitlesAvailable(f"臺中市議會的片段頁抓不到：{e}") from e
        record = parse_clip_page(page, ref)
        self._cached = (ref, record)
        return record

    def video_url(self, ref: TcccRef) -> str:
        record = self.record(ref)
        try:
            page = self._fetch(record.player_url)
        except OSError as e:
            raise NoSubtitlesAvailable(f"臺中市議會的播放器頁抓不到：{e}") from e
        stream = parse_stream_url(page)
        if not stream:
            raise NoSubtitlesAvailable("臺中市議會的播放器頁裡找不到串流網址")
        return stream
