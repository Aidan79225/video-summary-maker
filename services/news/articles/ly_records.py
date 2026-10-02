"""立法院的院內紀錄：從 LYAPI 同步會議（出席）、委員提案（主提案、連署、狀態）與記名表決。

人物側寫第四、五步（設計見 docs/superpowers/specs/2026-10-03-profile-records-steps4-5-design.md）的
資料來源。不打模型：全部是 LYAPI 的結構化資料，指標由 chamber 用程式計數。

幾件探勘出來、跟直覺不同的事（2026-10-03 實測 LYAPI v2）：

- 院會的出席名單在「會議資料[].出席委員」；委員會與聯席會議的「會議資料」出席名單**永遠是空的**，
  名單在「議事錄.出席委員」，而議事錄常常晚好幾週才有——還沒有的那一場是「不知道」，不是「沒人出席」。
- 聯席會議是另一個會議種類（「聯席會議」），不在「委員會」裡，要另外抓。
- 清單加 output_fields=連署人 就拿得到每一件的連署人：翻完全部委員提案（八頁）就有，不必逐人逐會期
  查 `連署人=<姓名>`（那要上百個請求）。實測兩種做法的件數相同。
- 議案的「會期」是**最新進度**的會期，不是提案的會期（第 1 會期提、第 5 會期撤案的案子寫 5）：
  一件提案算在一讀那一次院會（「會議代碼」）的會期，見 bill_session。LYAPI 的會期篩選也是看最新
  進度，所以議案一律抓整屆、在這裡篩。
- /votes 不支援用會期篩選：抓整屆（三頁）、在這裡篩。
- 同一個人在不同端點的寫法不一定一樣：會議資料寫「伍麗華Saidhai Tahovecahe」，表決與名冊寫
  「伍麗華Saidhai‧Tahovecahe」。比對一律用 name_key（去空白、去間隔號），顯示仍用原樣。

一秒一個請求、429 退避（同 members_sync）。全部抓完才寫、在同一個 transaction 裡寫：任何一頁失敗
就整次失敗、資料庫不動（同 members_sync 的原則），下次排程再來。以唯一鍵 upsert，重跑是安全的。
"""
from __future__ import annotations

import http.client
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import ArticleSource, LyBill, LyMeeting, LyMeetingKind, LyVote, Membership, Session
from .names import normalize_name

logger = logging.getLogger(__name__)

_TIMEOUT = 60.0
# LYAPI 是公民科技社群的服務：一秒一個請求，跟 members_sync 抓議會官網的節奏一樣
_PACE_SECONDS = 1.0
_RETRY_DELAYS = (2.0, 4.0, 8.0)
_MAX_RETRY_AFTER = 30.0
# 自報身分：對方的流量出問題時找得到是誰
USER_AGENT = "ly-news/0.1 (video-summary-maker; weekly records sync)"
# 每頁筆數。院會的一頁含好幾百個姓名，太大容易逾時；委員提案只要幾個欄位，一頁一千筆
_PAGE_SIZE = {"meets": 100, "bills": 1000, "votes": 500}
# 防上游給了怪的 total_page 而無限翻頁（最大的委員會清單目前是 10 頁）
_MAX_PAGES = 100
# 一屆最多八個會期（四年、每年兩個），臨時會併在所屬會期裡。超出的是 LYAPI 的資料錯誤（2026-10
# 有一件審查報告標成第 11 會期），不能因此建出一個不存在的會期
MAX_SESSION_NUMBER = 8

# LYAPI 的會議種類 → 我們的種類。聯席會議存成委員會：出席率只問「單位裡有沒有他的委員會」。
# 全院委員會、公聽會、黨團協商、考察不算：設計只有院會與委員會。
MEETING_KINDS: tuple[tuple[str, str], ...] = (
    ("院會", LyMeetingKind.PLENARY),
    ("委員會", LyMeetingKind.COMMITTEE),
    ("聯席會議", LyMeetingKind.COMMITTEE),
)

# 只要用得到的欄位：院會的完整資料一頁五筆就 500KB（議事網資料、發言紀錄）。
# 「委員會代號:str」要連「委員會代號」一起要，否則是空的
_MEET_FIELDS = ("會議代碼", "屆", "會期", "會議種類", "日期", "會議標題", "委員會代號", "委員會代號:str",
                "會議資料", "議事錄")
_BILL_FIELDS = ("議案編號", "屆", "會期", "會議代碼", "議案名稱", "議案狀態", "提案人", "連署人", "提案日期",
                "url")
_VOTE_FIELDS = ("表決代碼", "屆", "session_period", "會議代碼", "表決時間", "表決議題", "投票委員",
                "贊成", "反對", "棄權")

# 黨團提案的提案人寫的是黨團（「台灣民眾黨立法院黨團」），本來就對不到任何一位委員
_CAUCUS_SUFFIX = "黨團"


class RecordsUnavailable(Exception):
    """LYAPI 這次拿不到完整的紀錄；屬於暫時性問題，既有資料不動。"""


# --- 姓名與會期 ---


def name_key(name: str) -> str:
    """比對用的姓名：normalize_name（去空白、間隔號統一成「．」）之後再拿掉間隔號。

    要拿掉而不只是統一：會議資料把族名的間隔號寫成空白（「伍麗華Saidhai Tahovecahe」），去空白之後
    就沒有間隔號了，名冊與表決卻有。只用來比對，存檔與顯示都用原樣。
    """
    return normalize_name(name).replace("．", "").replace(".", "")


def session_name(term: int, number: int) -> str:
    """紀錄的會期就是第 1 步的會期：跟 profiles.parse_session 同一個寫法（「第11屆第5會期」）。"""
    return f"第{term}屆第{number}會期"


_SESSION_NAME_RE = re.compile(r"^第(\d+)屆第(\d+)會期$")


def parse_session_name(name: str) -> tuple[int, int] | None:
    """「第11屆第5會期」→ (11, 5)。議會的會期或寫法不對就是 None。"""
    match = _SESSION_NAME_RE.match(name or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


# --- 解析 ---


@dataclass(frozen=True)
class MeetingRecord:
    code: str
    kind: str
    term: int
    session_number: int
    # 每一天，由早到晚
    dates: tuple[date, ...]
    name: str
    units: tuple[str, ...]
    # None：LYAPI 還沒有這場的出席紀錄
    attendees: tuple[str, ...] | None
    url: str

    @property
    def date(self) -> date | None:
        return self.dates[0] if self.dates else None


@dataclass(frozen=True)
class BillRecord:
    bill_no: str
    term: int
    session_number: int
    name: str
    status: str
    proposers: tuple[str, ...]
    cosigners: tuple[str, ...]
    url: str
    proposed_on: date | None


@dataclass(frozen=True)
class VoteRecord:
    code: str
    term: int
    session_number: int
    meeting_code: str
    voted_at: str
    topic: str
    yes: tuple[str, ...]
    no: tuple[str, ...]
    abstain: tuple[str, ...]
    voters: tuple[str, ...]


def _int(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _names(values: Iterable[object]) -> tuple[str, ...]:
    """姓名清單：去空白、去掉空的、同一個人（name_key 相同）只留第一個寫法。

    院會的出席名單是每一天各一份、再加上議事錄的一份，合起來同一個人會出現好幾次、寫法也不一定一樣。
    """
    seen: dict[str, str] = {}
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        seen.setdefault(name_key(value), value.strip())
    return tuple(seen.values())


def _iso_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


_WHITESPACE_RE = re.compile(r"\s+")


def _collapse(value: object) -> str:
    """表決議題裡夾著一長串全形空白（「討論事項第二案　　　　　台灣民眾黨黨團提議逕付二讀」）。"""
    return _WHITESPACE_RE.sub(" ", str(value or "")).strip()


_PPG_MEETING_RE = re.compile(r"^https://ppg\.ly\.gov\.tw/ppg/sittings/\d+/")
_PPG_RE = re.compile(r"^https://ppg\.ly\.gov\.tw/")


def _official_url(value: object, pattern: re.Pattern = _PPG_RE) -> str:
    """議事網的連結才收。只要子欄位時 LYAPI 會拼出「sittings//details?meetingDate=59/01/01」這種壞連結。"""
    text = value.strip() if isinstance(value, str) else ""
    return text if pattern.match(text) and len(text) <= 500 else ""


def parse_meeting(row: object, kind: str) -> MeetingRecord | None:
    """/meets 的一筆。代碼、屆、會期缺一個就不收。

    出席名單取「會議資料」每一天的出席委員與「議事錄」的出席委員的聯集：院會兩邊都有（同一份），
    委員會只有議事錄有。兩邊都空就是 None——還沒有紀錄，不是沒人出席。
    """
    if not isinstance(row, dict):
        return None
    code = str(row.get("會議代碼") or "").strip()
    term, number = _int(row.get("屆")), _int(row.get("會期"))
    if not code or term is None or number is None:
        return None
    data = [item for item in _list(row.get("會議資料")) if isinstance(item, dict)]
    days = sorted({d for d in map(_iso_date, _list(row.get("日期"))) if d}
                  or {d for d in (_iso_date(item.get("日期")) for item in data) if d})
    minutes = row.get("議事錄") if isinstance(row.get("議事錄"), dict) else {}
    attendees = _names([name for item in data for name in _list(item.get("出席委員"))]
                       + _list(minutes.get("出席委員")))
    units = (_names(_list(row.get("委員會代號:str")))
             or _names(item.get("會議單位") for item in data))
    url = next((u for u in (_official_url(item.get("ppg_url"), _PPG_MEETING_RE) for item in data)
                if u), "") or _official_url(minutes.get("ppg_url"), _PPG_MEETING_RE)
    return MeetingRecord(code=code, kind=kind, term=term, session_number=number, dates=tuple(days),
                         name=str(row.get("會議標題") or "").strip()[:300] or code, units=units,
                         attendees=attendees or None, url=url)


_FIRST_READING_RE = re.compile(r"^院會-(\d+)-(\d+)-")


def bill_session(row: dict) -> int | None:
    """一件提案算在哪個會期：一讀（交付審查）的那一次院會的會期。

    不用「會期」欄位：那是**最新進度**的會期（2026-10 實測，第 11 屆 7,402 件裡有 176 件不同）——
    第 1 會期提的案到第 5 會期撤案，「會期」就變成 5。「會議代碼」才是一讀的院會（「院會-11-1-10」）。
    讀不出來、或讀出不存在的會期（2024-11 有兩件寫「院會-11-9-2」，「會期」是 2）才退回「會期」。
    """
    match = _FIRST_READING_RE.match(str(row.get("會議代碼") or "").strip())
    first_reading = int(match.group(2)) if match else None
    if first_reading is not None and 1 <= first_reading <= MAX_SESSION_NUMBER:
        return first_reading
    return _int(row.get("會期"))


def parse_bill(row: object) -> BillRecord | None:
    """/bills 的一筆。沒有連署人欄位的（黨團提案）就是沒有連署人。"""
    if not isinstance(row, dict):
        return None
    bill_no = str(row.get("議案編號") or "").strip()
    term, number = _int(row.get("屆")), bill_session(row)
    if not bill_no or term is None or number is None:
        return None
    return BillRecord(bill_no=bill_no, term=term, session_number=number,
                      name=str(row.get("議案名稱") or "").strip(),
                      status=str(row.get("議案狀態") or "").strip()[:64],
                      proposers=_names(_list(row.get("提案人"))),
                      cosigners=_names(_list(row.get("連署人"))),
                      url=_official_url(row.get("url")), proposed_on=_iso_date(row.get("提案日期")))


def parse_vote(row: object) -> VoteRecord | None:
    """/votes 的一筆。會期在 session_period（這個端點沒有「會期」欄位）。"""
    if not isinstance(row, dict):
        return None
    code = str(row.get("表決代碼") or "").strip()
    term, number = _int(row.get("屆")), _int(row.get("session_period"))
    if not code or term is None or number is None:
        return None
    return VoteRecord(code=code, term=term, session_number=number,
                      meeting_code=str(row.get("會議代碼") or "").strip(),
                      voted_at=_collapse(row.get("表決時間"))[:100], topic=_collapse(row.get("表決議題")),
                      yes=_names(_list(row.get("贊成"))), no=_names(_list(row.get("反對"))),
                      abstain=_names(_list(row.get("棄權"))), voters=_names(_list(row.get("投票委員"))))


_MONTH_DAY_RE = re.compile(r"(\d+)\s*月\s*(\d+)\s*日")
_YEAR_RE = re.compile(r"(\d+)\s*年")
_ROC_OFFSET = 1911


def vote_date(voted_at: str, meeting_days: Sequence[date], code: str = "") -> date | None:
    """表決的日期。原文多半是「中華民國115年3月20日 上午11時52分30秒」，但第 11 屆有九筆沒有年
    （「中華民國年1月21日」）。

    順序：原文有年就用它；沒有年就找會議裡同月同日的那一天；再沒有（過了午夜的表決，月日已經是
    會議的隔天）就用會議第一天的年份，比第一天早就是跨年了、加一年；會議也沒有就用表決代碼開頭的
    公報年份（「1151901_…」是民國 115 年）。月日讀不出來就用會議第一天。
    """
    days = sorted(meeting_days)
    match = _MONTH_DAY_RE.search(voted_at or "")
    if match is None:
        return days[0] if days else None
    month, day = int(match.group(1)), int(match.group(2))
    years = [int(y) for y in _YEAR_RE.findall(voted_at[:match.start()]) if int(y) > 0]
    try:
        if years:
            # 民國年；萬一哪天改寫西元年也認得
            year = years[-1] if years[-1] > _ROC_OFFSET else years[-1] + _ROC_OFFSET
            return date(year, month, day)
        for meeting_day in days:
            if (meeting_day.month, meeting_day.day) == (month, day):
                return meeting_day
        if days:
            guess = date(days[0].year, month, day)
            return guess if guess >= days[0] else date(days[0].year + 1, month, day)
        gazette = _int(code[:3])
        return date(gazette + _ROC_OFFSET, month, day) if gazette else None
    except ValueError:
        # 「2月30日」這種打錯的日期
        return days[0] if days else None


# --- 抓取 ---


def _http_get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as resp:
            return resp.read().decode("utf-8")
    except http.client.HTTPException as e:
        # IncompleteRead 不是 OSError，呼叫端的「抓不到」接不到它
        raise OSError(f"回應不完整：{e!r}") from e


def _retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
    # Retry-After 是伺服器主動告知的等待秒數，比我們猜的退避更準（同 law_source）
    headers = getattr(error, "headers", None)
    retry_after = headers.get("Retry-After") if headers is not None else None
    if retry_after:
        try:
            return min(float(retry_after), _MAX_RETRY_AFTER)
        except ValueError:
            pass
    return _RETRY_DELAYS[attempt]


@dataclass
class FetchedRecords:
    term: int
    # 只同步一個會期時是那個會期；None 是整屆
    session: int | None
    meetings: list[MeetingRecord] = field(default_factory=list)
    bills: list[BillRecord] = field(default_factory=list)
    votes: list[VoteRecord] = field(default_factory=list)
    # 解析不出來、或會期不合理而沒收的：「表決 1151901_…：會期 11」
    skipped: list[str] = field(default_factory=list)


class LyRecordsSource:
    """LYAPI 的 /meets、/bills、/votes。抓完整屆（或一個會期）才回傳，中途任何一頁失敗就丟
    RecordsUnavailable，呼叫端什麼都不寫。"""

    def __init__(self, base: str | None = None, term: int | None = None,
                 fetch: Callable[[str], str] = _http_get,
                 sleep: Callable[[float], None] = time.sleep):
        # 預設值在呼叫時才讀 settings：寫在參數預設值裡會在 import 當下就定死
        self._base = (base or settings.LYAPI_BASE).rstrip("/")
        self.term = int(term if term is not None else settings.LY_TERM)
        self._fetch = fetch
        self._sleep = sleep
        self._requests = 0

    def fetch(self, session: int | None = None) -> FetchedRecords:
        fetched = FetchedRecords(term=self.term, session=session)
        by_session = {"會期": session} if session is not None else {}
        for label, kind in MEETING_KINDS:
            rows = self._rows("meets", "會議代碼", {"會議種類": label, **by_session}, _MEET_FIELDS)
            self._keep(fetched, fetched.meetings, rows, lambda row, k=kind: parse_meeting(row, k),
                       f"會議（{label}）")
        # 議案也抓整屆、在 _keep 裡篩：LYAPI 的「會期」篩選看的是最新進度的會期，不是一讀的（見
        # bill_session）。/votes 則根本不支援用會期篩選
        rows = self._rows("bills", "議案編號", {"提案來源": "委員提案"}, _BILL_FIELDS)
        self._keep(fetched, fetched.bills, rows, parse_bill, "議案")
        rows = self._rows("votes", "表決代碼", {}, _VOTE_FIELDS)
        self._keep(fetched, fetched.votes, rows, parse_vote, "表決")
        return fetched

    def _keep(self, fetched: FetchedRecords, out: list, rows: list[dict],
              parse: Callable[[object], object], label: str) -> None:
        """解析、只留這一屆（與指定會期）的、會期不合理的記下來不收。

        屆與會期在這裡再篩一次：LYAPI 不認得的篩選條件會被安靜地忽略、回整個資料庫。
        """
        for row in rows:
            record = parse(row)
            if record is None:
                fetched.skipped.append(f"{label}：缺代碼、屆或會期")
                continue
            if record.term != self.term:
                continue
            if fetched.session is not None and record.session_number != fetched.session:
                continue
            if not 1 <= record.session_number <= MAX_SESSION_NUMBER:
                fetched.skipped.append(f"{label} {_record_id(record)}：會期 {record.session_number}")
                continue
            out.append(record)

    def _rows(self, path: str, id_field: str, filters: dict, fields: Sequence[str]) -> list[dict]:
        """翻完一個清單的每一頁。

        翻完之後檢查筆數：議案依「最新進度日期」排序，翻頁途中有一件動了，另一件就可能被擠到已經
        翻過的那一頁、整次漏掉。少一件就少算一個人一件提案，所以寧可整次失敗、下次再來。
        """
        rows: list[dict] = []
        expected = None
        page = 1
        while True:
            query = {"屆": self.term, **filters, "limit": _PAGE_SIZE[path], "page": page,
                     "output_fields": list(fields)}
            payload = self._get(path, query)
            batch = payload.get(path)
            if not isinstance(batch, list):
                raise RecordsUnavailable(f"LYAPI 的 /{path} 回應少了 {path} 欄位")
            rows.extend(batch)
            if expected is None:
                expected = _int(payload.get("total"))
            pages = _int(payload.get("total_page")) or 1
            if page >= pages or not batch:
                break
            page += 1
            if page > _MAX_PAGES:
                raise RecordsUnavailable(f"LYAPI 的 /{path} 超過 {_MAX_PAGES} 頁，回應可能不對")
        unique = {str(row.get(id_field)) for row in rows if isinstance(row, dict)}
        if expected is not None and len(unique) < expected:
            raise RecordsUnavailable(f"LYAPI 的 /{path}（{filters}）說有 {expected} 筆，翻完只拿到 "
                                     f"{len(unique)} 筆；可能是翻頁途中資料有變動，下次再同步")
        return rows

    def _get(self, path: str, query: dict) -> dict:
        url = f"{self._base}/{path}?{urllib.parse.urlencode(query, doseq=True)}"
        for attempt in range(len(_RETRY_DELAYS) + 1):
            if self._requests:
                self._sleep(_PACE_SECONDS)
            self._requests += 1
            try:
                payload = json.loads(self._fetch(url))
            # HTTPError 是 OSError 的子類別，要排在前面才能只針對 429 重試
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < len(_RETRY_DELAYS):
                    self._sleep(_retry_delay(e, attempt))
                    continue
                raise RecordsUnavailable(f"LYAPI 的 /{path} 抓不到：{str(e)[:200]}") from e
            except (OSError, ValueError) as e:
                raise RecordsUnavailable(f"LYAPI 的 /{path} 抓不到：{str(e)[:200]}") from e
            if not isinstance(payload, dict):
                raise RecordsUnavailable(f"LYAPI 的 /{path} 回應格式不符預期")
            return payload
        raise AssertionError("unreachable：迴圈每次都會 return、continue 或 raise")


def _record_id(record: MeetingRecord | BillRecord | VoteRecord) -> str:
    return getattr(record, "code", None) or getattr(record, "bill_no", "")


# --- 寫入 ---


@dataclass
class RecordsReport:
    term: int
    session: int | None
    plenary: int = 0
    committee: int = 0
    # 還沒有出席紀錄的會議（委員會的議事錄常常晚好幾週）
    without_attendance: int = 0
    bills: int = 0
    votes: int = 0
    sessions_created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    # 紀錄裡對不到任何一段立法院任期的姓名 → 次數（不計入任何人的指標）
    unknown_names: Counter = field(default_factory=Counter)

    def __str__(self) -> str:
        scope = f"第{self.term}屆" + (f"第{self.session}會期" if self.session else "（全部會期）")
        lines = [f"立法院院內紀錄 {scope}：院會 {self.plenary} 場、委員會（含聯席會議）{self.committee} 場、"
                 f"委員提案 {self.bills} 件、記名表決 {self.votes} 次；還沒有出席紀錄的會議 "
                 f"{self.without_attendance} 場（不計入出席率）"]
        if self.sessions_created:
            lines.append(f"新建會期：{'、'.join(self.sessions_created)}")
        if self.skipped:
            # 經費稽核委員會的會議每週都會在這裡（LYAPI 標成會期 0）：列幾筆當例子就好
            lines.append(f"沒收的紀錄 {len(self.skipped)} 筆（LYAPI 的資料不完整或會期不合理），例如：")
            lines.extend(f"  {text}" for text in self.skipped[:5])
        if self.unknown_names:
            lines.append(f"對不到立法院任期的姓名 {len(self.unknown_names)} 個（不計入任何人的指標；"
                         "先跑 sync_members，還對不到就是寫法不同，請到 admin 核對任期的名字）：")
            lines.extend(f"  {name}：{count} 次"
                         for name, count in sorted(self.unknown_names.items(),
                                                   key=lambda item: (-item[1], item[0]))[:30])
        return "\n".join(lines)


def save(fetched: FetchedRecords, now: datetime | None = None) -> RecordsReport:
    """全部在同一個 transaction 裡寫：會期、會議、議案、表決。以唯一鍵 upsert。"""
    now = now or timezone.now()
    report = RecordsReport(term=fetched.term, session=fetched.session, skipped=list(fetched.skipped))
    with transaction.atomic():
        report.sessions_created = _ensure_sessions(fetched)
        _save_meetings(fetched.meetings, now)
        _save_bills(fetched.bills, now)
        _save_votes(fetched.votes, _meeting_days(fetched), now)
    report.plenary = sum(1 for m in fetched.meetings if m.kind == LyMeetingKind.PLENARY)
    report.committee = len(fetched.meetings) - report.plenary
    report.without_attendance = sum(1 for m in fetched.meetings if m.attendees is None)
    report.bills, report.votes = len(fetched.bills), len(fetched.votes)
    report.unknown_names = _unknown_names(fetched)
    return report


def _ensure_sessions(fetched: FetchedRecords) -> list[str]:
    """紀錄的會期沒有對應的 Session 就建立。涵蓋範圍（起訖）仍以文章為準：沒有文章的會期留空，
    每晚的 assign_sessions 會照文章重算，這裡不碰。由小到大建，id 才跟會期的先後一致。"""
    numbers = sorted({r.session_number for group in (fetched.meetings, fetched.bills, fetched.votes)
                      for r in group})
    created = []
    for number in numbers:
        name = session_name(fetched.term, number)
        _, was_created = Session.objects.get_or_create(source=ArticleSource.LY, name=name,
                                                       defaults={"term": str(fetched.term)})
        if was_created:
            created.append(name)
    return created


_BATCH = 500


def _save_meetings(meetings: list[MeetingRecord], now: datetime) -> None:
    LyMeeting.objects.bulk_create(
        [LyMeeting(code=m.code, kind=m.kind, term=m.term, session_number=m.session_number,
                   date=m.date, dates=[d.isoformat() for d in m.dates], name=m.name,
                   units=list(m.units),
                   attendees=list(m.attendees) if m.attendees is not None else None,
                   url=m.url, synced_at=now) for m in meetings],
        update_conflicts=True, unique_fields=["code"], batch_size=_BATCH,
        update_fields=["kind", "term", "session_number", "date", "dates", "name", "units",
                       "attendees", "url", "synced_at"])


def _save_bills(bills: list[BillRecord], now: datetime) -> None:
    LyBill.objects.bulk_create(
        [LyBill(bill_no=b.bill_no, term=b.term, session_number=b.session_number, name=b.name,
                status=b.status, proposers=list(b.proposers), cosigners=list(b.cosigners),
                url=b.url, proposed_on=b.proposed_on, synced_at=now) for b in bills],
        update_conflicts=True, unique_fields=["bill_no"], batch_size=_BATCH,
        update_fields=["term", "session_number", "name", "status", "proposers", "cosigners", "url",
                       "proposed_on", "synced_at"])


def _meeting_days(fetched: FetchedRecords) -> dict[str, list[date]]:
    """會議代碼 → 每一天。這次沒抓到的會議（例如只同步一個會期時）從資料庫補。"""
    days = {m.code: list(m.dates) for m in fetched.meetings}
    missing = {v.meeting_code for v in fetched.votes} - days.keys()
    for code, stored in LyMeeting.objects.filter(code__in=missing).values_list("code", "dates"):
        days[code] = [d for d in map(_iso_date, stored or []) if d]
    return days


def _save_votes(votes: list[VoteRecord], meeting_days: dict[str, list[date]], now: datetime) -> None:
    LyVote.objects.bulk_create(
        [LyVote(code=v.code, term=v.term, session_number=v.session_number,
                meeting_code=v.meeting_code, voted_at=v.voted_at,
                date=vote_date(v.voted_at, meeting_days.get(v.meeting_code, []), v.code),
                topic=v.topic, yes=list(v.yes), no=list(v.no), abstain=list(v.abstain),
                voters=list(v.voters), synced_at=now) for v in votes],
        update_conflicts=True, unique_fields=["code"], batch_size=_BATCH,
        update_fields=["term", "session_number", "meeting_code", "voted_at", "date", "topic", "yes",
                       "no", "abstain", "voters", "synced_at"])


def _unknown_names(fetched: FetchedRecords) -> Counter:
    """紀錄裡出現、卻對不到任何一段立法院任期的姓名。黨團提案的提案人（「…黨團」）不算。"""
    known = {name_key(name) for name in
             Membership.objects.filter(source=ArticleSource.LY).values_list("name", flat=True)}
    unknown: Counter = Counter()
    groups: list[Iterable[str]] = [m.attendees or () for m in fetched.meetings]
    groups += [(*b.proposers, *b.cosigners) for b in fetched.bills]
    groups += [(*v.yes, *v.no, *v.abstain) for v in fetched.votes]
    for names in groups:
        for name in names:
            if name_key(name) not in known and not name.endswith(_CAUCUS_SUFFIX):
                unknown[name] += 1
    return unknown


def sync_ly_records(term: int | None = None, session: int | None = None,
                    source: LyRecordsSource | None = None) -> RecordsReport:
    """抓、再寫。抓不到（RecordsUnavailable）就冒出去，什麼都沒寫。"""
    source = source or LyRecordsSource(term=term)
    return save(source.fetch(session=session))
