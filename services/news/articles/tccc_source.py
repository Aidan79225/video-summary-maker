"""臺中市議會「議員個人質詢隨選視訊系統」：查某一天有哪些片段。

沒有 API，只有依議員分類的 ASP 頁。所以「某一天有誰發言」要逐一翻 61 位
議員的第一頁（10 筆、新到舊）——每天 61 次 HTTP，順序、逐一，對方是舊系統。
只用 stdlib，跟 ivod_source 一樣。

頁面規則（2026-09 實測）：
- `wb_region01.asp?url=11`：全部議員，`index.asp?url=12&cno=<n>` 連結文字是姓名。
- `wb_region02.asp?url=12&cno=<n>[&ano=<m>]`：該議員的片段清單，每列一個
  `index.asp?…&ano=<m>` 連結 + 日期欄；「選中」的那一筆（不帶 ano 時是最新一筆）
  沒有連結，ano 只出現在分頁連結裡，會議名稱與日期在上方區塊。
- 上方區塊的「影片長度」是 HH:MM（「00:50」= 50 分鐘）。
"""
from __future__ import annotations

import html
import logging
import re
import urllib.request
from dataclasses import dataclass
from datetime import date

from .ivod_source import IvodClip, SourceUnavailable

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
SOURCE = "tccc"

_COUNCILOR_RE = re.compile(
    r'href="index\.asp\?url=12&cno=(\d+)"[^>]*>(?:<font[^>]*>)?\s*([^<]+?)\s*<')
_ROW_RE = re.compile(
    r'href="index\.asp\?url=12&cno=\d+&ano=(\d+)&pageno=\d+"[^>]*>([^<]+)</a>'
    r'.*?<td valign="top" nowrap="nowrap">(\d{4}-\d{2}-\d{2})</td>',
    re.S | re.I)
_SELECTED_ANO_RE = re.compile(r"&ano=(\d+)&PageNo=\d+")
_SELECTED_MEETING_RE = re.compile(r'<font color="#0D57BB">([^<]+)</font>')
_SELECTED_DATE_RE = re.compile(r"會議日期：</td>\s*<td[^>]*>\s*(\d{4}-\d{2}-\d{2})")
_SELECTED_DURATION_RE = re.compile(r"影片長度：</td>\s*<td[^>]*>\s*(\d{1,2}):(\d{2})")


def _http_get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


@dataclass(frozen=True)
class ClipRow:
    ano: str
    meeting: str
    date: str


def parse_councilors(page: str) -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for cno, name in _COUNCILOR_RE.findall(page):
        seen.setdefault(cno, html.unescape(name).strip())
    return [(cno, name) for cno, name in seen.items() if name]


def parse_clip_rows(page: str) -> list[ClipRow]:
    """清單裡每一列，新到舊；被選中的那一筆沒有連結，改從分頁連結拿 ano、
    從上方區塊拿會議名稱與日期。"""
    rows = [ClipRow(ano, html.unescape(meeting).strip(), day)
            for ano, meeting, day in _ROW_RE.findall(page)]
    selected = _SELECTED_ANO_RE.search(page)
    meeting = _SELECTED_MEETING_RE.search(page)
    day = _SELECTED_DATE_RE.search(page)
    if selected and meeting and day and all(r.ano != selected.group(1) for r in rows):
        rows.append(ClipRow(selected.group(1), html.unescape(meeting.group(1)).strip(),
                            day.group(1)))
    # 被選中的那筆插回去之後順序就亂了；ano 是流水號，新片段一定比較大
    return sorted(rows, key=lambda r: (r.date, int(r.ano)), reverse=True)


def parse_selected_duration(page: str) -> int:
    """「影片長度：00:50」是 HH:MM（實測 51 分鐘），分鐘精度。"""
    match = _SELECTED_DURATION_RE.search(page)
    if not match:
        return 0
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60


class TcccDailySource:
    name = "臺中市議會"

    def __init__(self, base: str, fetch=_http_get):
        self._base = base.rstrip("/")
        self._fetch = fetch

    def clips_for(self, day: date, only_with_transcript: bool = True) -> list[IvodClip]:
        """某一天的片段。only_with_transcript 只為了跟立法院同介面：臺中一律走
        語音辨識，沒有「有沒有逐字稿」這回事。"""
        try:
            councilors = parse_councilors(self._fetch(f"{self._base}/wb_region01.asp?url=11"))
        except OSError as e:
            raise SourceUnavailable(f"臺中市議會的議員清單抓不到：{e}") from e
        if not councilors:
            raise SourceUnavailable("臺中市議會的議員清單解析不出任何議員，頁面格式可能改了")
        wanted = day.isoformat()
        clips: list[IvodClip] = []
        for cno, name in councilors:
            for row in self._rows_for(cno, name):
                if row.date != wanted:
                    continue
                clips.append(IvodClip(
                    ivod_id=f"{SOURCE}-{row.ano}",
                    date=row.date,
                    speaker=name,
                    meeting=row.meeting,
                    duration_seconds=self._duration(cno, row.ano),
                    ivod_url=f"{self._base}/index.asp?url=12&cno={cno}&ano={row.ano}",
                    has_transcript=True,
                    source=SOURCE,
                ))
        return sorted(clips, key=lambda c: int(c.ivod_id.split("-")[1]))

    def _rows_for(self, cno: str, name: str) -> list[ClipRow]:
        try:
            page = self._fetch(f"{self._base}/wb_region02.asp?url=12&cno={cno}")
        except OSError as e:
            # 一位議員的頁面掛了不該讓整天沒有任何臺中的內容
            logger.warning("臺中市議會 %s（cno=%s）的片段頁抓不到：%s", name, cno, e)
            return []
        rows = parse_clip_rows(page)
        if rows and not _SELECTED_ANO_RE.search(page):
            # 沒有分頁就拿不到選中那一筆的 ano：改選另一筆，最新的就會變成有連結的列
            try:
                page = self._fetch(
                    f"{self._base}/wb_region02.asp?url=12&cno={cno}&ano={rows[0].ano}&pageno=1")
                rows = parse_clip_rows(page)
            except OSError:
                pass
        return rows

    def _duration(self, cno: str, ano: str) -> int:
        try:
            page = self._fetch(f"{self._base}/wb_region02.asp?url=12&cno={cno}&ano={ano}&pageno=1")
        except OSError as e:
            logger.warning("臺中市議會片段 %s 的長度抓不到：%s", ano, e)
            return 0
        return parse_selected_duration(page)
