"""立法院開放資料：查某一天有哪些質詢片段。

只用 stdlib：Pi 上少一個相依就少一個要維護的東西。查詢參數是實測確認過
的（`日期`、`影片種類` 都在 API 的 supported_filter_fields 裡）。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

_PAGE_SIZE = 100
_TIMEOUT = 30.0


class VideoKind(StrEnum):
    CLIP = "Clip"
    FULL = "Full"


class Feature(StrEnum):
    AI_TRANSCRIPT = "ai-transcript"


class Field(StrEnum):
    """立法院 API 的欄位名。集中在這裡，上游改名時只要動一個地方。"""
    ID = "IVOD_ID"
    URL = "IVOD_URL"
    DATE = "日期"
    KIND = "影片種類"
    SPEAKER = "委員名稱"
    DURATION = "影片長度"
    MEETING = "會議資料"
    MEETING_TITLE = "標題"
    FEATURES = "支援功能"
    ROWS = "ivods"
    TOTAL_PAGES = "total_page"


class IvodUnavailable(Exception):
    """立法院的 API 這次拿不到。屬於暫時性問題，下次排程會再試。"""


@dataclass(frozen=True)
class IvodClip:
    ivod_id: str
    date: str
    speaker: str
    meeting: str
    duration_seconds: int
    ivod_url: str
    has_transcript: bool

    @property
    def title(self) -> str:
        """與 slidebox 產生的標題同一個形狀，讓兩邊看起來是同一件事。"""
        head = " ".join(p for p in (self.date, self.speaker) if p)
        return f"{head}－{self.meeting}" if head and self.meeting else head or self.meeting


def _http_get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
        return resp.read().decode("utf-8")


def _duration(value: object) -> int:
    """清單端點給秒數（197），單筆端點給 "00:03:17"。兩種都要吃。"""
    if isinstance(value, (int, float)):
        return int(value)
    parts = str(value or "").split(":")
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        return 0
    total = 0.0
    for number in numbers:
        total = total * 60 + number
    return int(total)


def _total_pages(payload: dict) -> int:
    """上游給了怪值也不要讓整個指令帶著 traceback 死掉——那會連已經登記好
    的積壓都不處理。當成只有一頁即可。"""
    try:
        return int(payload.get(Field.TOTAL_PAGES) or 1)
    except (TypeError, ValueError):
        return 1


def _clip(raw: dict) -> IvodClip | None:
    ivod_id = raw.get(Field.ID)
    url = str(raw.get(Field.URL) or "")
    if ivod_id is None or not url:
        # 沒有播放網址的話，後面送去 GPU 一定被拒，而那個拒絕會被當成
        # 「服務不可用」而擋住當天整批。在這裡就不要登記它。
        return None
    meeting = (raw.get(Field.MEETING) or {}).get(Field.MEETING_TITLE) or ""
    return IvodClip(
        ivod_id=str(ivod_id),
        date=str(raw.get(Field.DATE) or ""),
        speaker=str(raw.get(Field.SPEAKER) or ""),
        meeting=str(meeting),
        duration_seconds=_duration(raw.get(Field.DURATION)),
        ivod_url=url,
        has_transcript=Feature.AI_TRANSCRIPT in (raw.get(Field.FEATURES) or []),
    )


class IvodDailySource:
    def __init__(self, base: str, fetch=_http_get):
        self._base = base.rstrip("/")
        self._fetch = fetch

    def clips_for(self, day: date, only_with_transcript: bool = True) -> list[IvodClip]:
        """某一天的質詢片段，最早的排前面。

        只收 Clip（一位委員的一段發言）。完整會議是 8 小時的錄影，壓成十幾
        頁不是新聞，而且它的影片主機實測連不上、拿不到截圖。
        """
        clips: list[IvodClip] = []
        page = 1
        while True:
            payload = self._page(day, page)
            if not isinstance(payload, dict):
                raise IvodUnavailable("立法院 API 的回應不是物件")
            if Field.ROWS not in payload:
                # 上游改了 schema 跟「今天休會」在下游看起來一模一樣，
                # 都是「發現 0 篇」。寧可吵一次也不要靜悄悄地停更。
                raise IvodUnavailable(f"立法院 API 的回應少了 {Field.ROWS} 欄位")
            rows = payload[Field.ROWS]
            if not isinstance(rows, list) or not rows:
                break
            clips.extend(c for c in (_clip(r) for r in rows if isinstance(r, dict))
                         if c is not None)
            if page >= _total_pages(payload):
                break
            page += 1
        if only_with_transcript:
            clips = [c for c in clips if c.has_transcript]
        # 上游哪天給了非數字 id，排序不該讓整個指令崩潰
        return sorted(clips, key=lambda c: (len(c.ivod_id), c.ivod_id))

    def _page(self, day: date, page: int) -> dict:
        query = urllib.parse.urlencode({
            Field.DATE: day.isoformat(),
            Field.KIND: VideoKind.CLIP,
            "limit": _PAGE_SIZE,
            "page": page,
        })
        try:
            return json.loads(self._fetch(f"{self._base}?{query}"))
        except (OSError, ValueError) as e:
            raise IvodUnavailable(f"立法院 API 取得失敗：{str(e)[:200]}") from e
