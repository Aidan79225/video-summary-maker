"""立法院開放資料：查某一天有哪些質詢片段。

只用 stdlib：Pi 上少一個相依就少一個要維護的東西。查詢參數是實測確認過
的（`日期`、`影片種類` 都在 API 的 supported_filter_fields 裡）。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

_PAGE_SIZE = 100
_TIMEOUT = 30.0
# 打太快會被 LYAPI 429：--days 45 在 4 秒內連打 45 次，整批清單全部失敗。
# 兩道防線：連續請求之間留一點間隔；真的被擋就退避重試，秒數的長度就是重試次數。
_PACE_SECONDS = 0.5
_RETRY_DELAYS = (2.0, 4.0, 8.0)
_MAX_RETRY_AFTER = 10.0


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
    # 全院委員會、公聽會的片段沒有「會議資料」，只有這個頂層欄位
    MEETING_NAME = "會議名稱"
    FEATURES = "支援功能"
    ROWS = "ivods"
    TOTAL_PAGES = "total_page"


class IvodUnavailable(Exception):
    """立法院的 API 這次拿不到。屬於暫時性問題，下次排程會再試。"""


# 每個來源都丟同一個例外，discover 才能一視同仁地「記錯誤、跳過、繼續」。
SourceUnavailable = IvodUnavailable


@dataclass(frozen=True)
class IvodClip:
    ivod_id: str
    date: str
    speaker: str
    meeting: str
    duration_seconds: int
    ivod_url: str
    has_transcript: bool
    # 對應 Article.source；立法院是預設值，讓既有的呼叫端不用改
    source: str = "ly"

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
    meeting = meeting_title(raw)
    return IvodClip(
        ivod_id=str(ivod_id),
        date=str(raw.get(Field.DATE) or ""),
        speaker=str(raw.get(Field.SPEAKER) or ""),
        meeting=str(meeting),
        duration_seconds=_duration(raw.get(Field.DURATION)),
        ivod_url=url,
        has_transcript=Feature.AI_TRANSCRIPT in (raw.get(Field.FEATURES) or []),
    )


# 「第11屆第5會期第2次全院委員會（事由：總統咨，…）」：事由動輒幾百字，只留會議本身
_REASON_RE = re.compile(r"\s*[（(]事由[：:].*$", re.S)
# 後備的會議名稱最多留這麼長：標題是「日期 講者－會議」，欄位上限 300
_FALLBACK_MEETING_MAX = 200


def meeting_title(raw: dict) -> str:
    """片段的會議名稱。

    平常在「會議資料.標題」。全院委員會、公聽會的片段沒有會議資料（實測約 6%），只有頂層的
    「會議名稱」——沒有這個後備，這些發言的會議名稱是空的，解析不出會期，就從人物側寫裡
    整個消失。
    """
    data = raw.get(Field.MEETING)
    title = data.get(Field.MEETING_TITLE) if isinstance(data, dict) else None
    if title:
        return str(title)
    name = str(raw.get(Field.MEETING_NAME) or "")
    return _REASON_RE.sub("", name).strip()[:_FALLBACK_MEETING_MAX]


def _retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
    # Retry-After 是伺服器主動告知的等待秒數，比我們猜的退避更準
    headers = getattr(error, "headers", None)
    retry_after = headers.get("Retry-After") if headers is not None else None
    if retry_after:
        try:
            return min(float(retry_after), _MAX_RETRY_AFTER)
        except ValueError:
            pass
    return _RETRY_DELAYS[attempt]


class IvodDailySource:
    name = "立法院"

    def __init__(self, base: str, fetch=_http_get,
                 sleep: Callable[[float], None] = time.sleep):
        self._base = base.rstrip("/")
        self._fetch = fetch
        self._sleep = sleep
        self._requests = 0

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

    def record(self, ivod_id: str) -> dict:
        """單一片段的原始資料（回補會議名稱用）。"""
        payload = self._get_json(f"{self._base}/{urllib.parse.quote(str(ivod_id))}")
        record = payload.get("data", payload) if isinstance(payload, dict) else None
        if not isinstance(record, dict):
            raise IvodUnavailable("立法院 API 的片段資料不是物件")
        return record

    def _page(self, day: date, page: int) -> dict:
        query = urllib.parse.urlencode({
            Field.DATE: day.isoformat(),
            Field.KIND: VideoKind.CLIP,
            "limit": _PAGE_SIZE,
            "page": page,
        })
        return self._get_json(f"{self._base}?{query}")

    def _get_json(self, url: str) -> dict:
        for attempt in range(len(_RETRY_DELAYS) + 1):
            if self._requests:
                # 第一次不等；之後每次請求前留間隔，多天回補才不會一口氣打爆對方
                self._sleep(_PACE_SECONDS)
            self._requests += 1
            try:
                return json.loads(self._fetch(url))
            # HTTPError 是 OSError 的子類別，要排在前面才能只針對 429 重試
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < len(_RETRY_DELAYS):
                    self._sleep(_retry_delay(e, attempt))
                    continue
                raise IvodUnavailable(f"立法院 API 取得失敗：{str(e)[:200]}") from e
            except (OSError, ValueError) as e:
                raise IvodUnavailable(f"立法院 API 取得失敗：{str(e)[:200]}") from e
        raise AssertionError("unreachable：迴圈每次都會 return 或 raise")
