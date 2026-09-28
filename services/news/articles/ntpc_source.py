"""新北市議會「議事影音隨選視訊系統」：查某一天有哪些質詢片段。

沒有 API，只有一個 ASP.NET MVC 的查詢頁（vod.ntp.gov.tw/VodCloudV2）。以下都是
2026-09 對真實回應實測的結果，只用 stdlib，跟 ivod_source 一樣：

- 查詢是 `POST /VodCloudV2/VOD/Search`，9 個欄位**全部都要送**——少一個，伺服器就
  靜默忽略篩選、回「最近半年」的預設清單。日期只收 `yyyy/MM/dd`，其他格式回 500，
  而且那個 session 之後每個請求都 500，只能換一個 cookie 重來。
- 回 302 到 `/VodCloudV2/VOD/Index`。篩選條件存在 `ASP.NET_SessionId` 的 session
  裡，所以要 cookie jar；翻頁 `GET /VodCloudV2/VOD/ToPage?ToPage=N` 靠同一個 session。
- 頁面回顯「開會日期:2026/09/16~2026/09/16」與「總筆數:N」。一定要檢查：「篩選被
  忽略」跟「那天有 332 段」在下游看起來一模一樣。
- 卡片欄位是「標籤：值」（全形冒號）；「議　　程」中間是兩個 U+3000；開會日期是
  民國年（115-09-16）；長度在縮圖角落的 timecode。
- 播放器頁（不需要 session）的串流網址裡有媒體檔的 key：
  `/Book_SD/0408R1150916/0408R1150916020.mp4/`。同一個檔案可能被上架兩次、有兩個
  GUID，所以以檔案 key 去重，文章的 ivod_id 也用它。

一段影片代表什麼、收哪些、講者怎麼認，見
docs/superpowers/specs/2026-09-29-ntpc-source-design.md 的決策 2、3。
"""
from __future__ import annotations

import html
import http.cookiejar
import logging
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from django.conf import settings

from .ivod_source import IvodClip, SourceUnavailable
from .members_sync import CHAIR_ROLES, NTPC_CAUCUSES, MemberRecord
from .models import ArticleSource, Membership
from .names import normalize_name

logger = logging.getLogger(__name__)

_TIMEOUT = 60.0
SOURCE = "ntpc"
# 多位講者（黨團時段、市政總質詢）用頓號串起，同臺中的聯合質詢
SPEAKER_SEPARATOR = "、"
# 兩個新北的網站一律循序、間隔一秒（設計決策 6）
_PACE_SECONDS = 1.0
# 更短的是點名、宣布開會或休息的空檔，不是質詢
MIN_SECONDS = 180

_SEARCH_PATH = "/VodCloudV2/VOD/Search"
_TO_PAGE_PATH = "/VodCloudV2/VOD/ToPage?ToPage={page}"
_PLAYER_PATH = "/VodCloudV2/VodStream/VideoPlayer?assetID={guid}&type=Book_SD"
# 給人看、也送給 GPU 的網址：不需要 session，直接貼就能看
_VIEW_PATH = "/VodCloudV2/VOD/ViewMetaData?assetID={guid}"

_GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_CARD_SPLIT_RE = re.compile(r'<div class="col-md-3 col-lg-2"[^>]*>')
# href 沒有引號：`<a href=ViewDetailMetaData/<guid> class="mov">`
_CARD_GUID_RE = re.compile(r"ViewDetailMetaData/(" + _GUID + ")")
_TIMECODE_RE = re.compile(r'<span class="timecode">\s*([^<]*?)\s*</span>')
_FIELD_RE = re.compile(r'<span onclick="showTip\(this,event\)"[^>]*>([^<]*)</span>')
_ECHO_RE = re.compile(r'<span class="fb24">([^<]*)<span[^>]*>\s*總筆數:\s*(\d+)\s*</span>')
_HMS_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})$")
_ROC_DATE_RE = re.compile(r"^(\d{2,3})-(\d{1,2})-(\d{1,2})$")
_NAME_SPLIT_RE = re.compile(r"[,，、]")
_WHITESPACE_RE = re.compile(r"\s+")
# KEY = 屆2 次2 R|T 民國yyyMMdd，SEQ 三碼：/Book_SD/0408R1150916/0408R1150916020.mp4/
_FILE_KEY_RE = re.compile(r"/Book_SD/([0-9A-Za-z]+)/\1(\d{3})\.mp4/")

# --- 議程字樣（比對前先去掉所有空白） ---
_GENERAL = "市政總質詢"
_BUSINESS_MARK = "各機關聯合業務報告及質詢-"
# 「…質詢-李翁議員月娥」（姓 + 議員 + 名）或「…質詢-馬見Lahuy．Ipin議員」
_INDIVIDUAL_RE = re.compile(r"-([^-]+?)議員([^-]*)$")
# 「…質詢-國民黨團發言」「…質詢-民進黨團聯合發言」
_CAUCUS_RE = re.compile(
    "-(" + "|".join(map(re.escape, NTPC_CAUCUSES.values())) + ")(?:黨團)?(?:聯合)?發言$")
# 市長施政報告、市長報告116年度總預算編製之經過、市府針對「…」專案報告
_MIXED_RE = re.compile(r"^市長施政報告|市長報告.*總預算|專案報告")
_REPORT_ITEMS = "報告事項"


class Label(StrEnum):
    """卡片上的欄位標籤（去掉空白後）。集中在這裡，上游改字時只要動一個地方。"""
    SESSION = "屆次會期"
    AGENDA = "議程"
    SPEAKERS = "發言議員"
    DATE = "開會日期"
    START = "開始時間"
    END = "結束時間"


class Slot(StrEnum):
    """一段影片是哪一種時段；決定講者怎麼認。"""
    GENERAL = "市政總質詢"          # 一個黨團聯合質詢的一個時間切片
    CAUCUS = "黨團時段"              # 業務質詢：-國民黨團發言、-民進黨團聯合發言
    INDIVIDUAL = "個人時段"          # 業務質詢：-李翁議員月娥
    BUSINESS = "業務質詢"            # 業務質詢但字樣不是上面兩種：不另外過濾
    MIXED = "多黨混合"               # 市長施政報告、總預算報告、專案報告


def _ssl_context() -> ssl.SSLContext:
    """vod.ntp.gov.tw 的憑證鏈跟臺中一樣有張 CA 憑證沒有 Subject Key Identifier，
    Python 3.13 起的 VERIFY_X509_STRICT 會拒絕它。只關掉 strict 旗標，主機名與信任鏈
    照常驗證，不是關掉驗證。"""
    context = ssl.create_default_context()
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


def _compact(text: str) -> str:
    """去掉所有空白（含 U+3000）：「議　　程」→「議程」。"""
    return _WHITESPACE_RE.sub("", text or "")


def _seconds(hms: str) -> int | None:
    match = _HMS_RE.match((hms or "").strip())
    if not match:
        return None
    h, m, s = (int(x) for x in match.groups())
    return h * 3600 + m * 60 + s


def _roc_to_iso(text: str) -> str:
    """民國年「115-09-16」→「2026-09-16」；看不懂就空字串（之後會因日期不符被丟掉）。"""
    match = _ROC_DATE_RE.match((text or "").strip())
    if not match:
        return ""
    try:
        return date(int(match.group(1)) + 1911, int(match.group(2)),
                    int(match.group(3))).isoformat()
    except ValueError:
        return ""


# --- 解析 ---


@dataclass(frozen=True)
class Card:
    """清單上的一張卡片 = 一個 GUID。"""
    guid: str
    session: str
    agenda: str
    # 已正規化，順序照頁面（筆畫排序，不是發言順序）
    speakers: tuple[str, ...]
    date: str
    start: str
    duration_seconds: int


def parse_cards(page: str) -> list[Card]:
    cards: list[Card] = []
    for chunk in _CARD_SPLIT_RE.split(page)[1:]:
        guid = _CARD_GUID_RE.search(chunk)
        if not guid:
            continue
        fields: dict[str, str] = {}
        for raw in _FIELD_RE.findall(chunk):
            label, colon, value = html.unescape(raw).partition("：")
            if colon:
                fields.setdefault(_compact(label), value.strip())
        names = (normalize_name(n) for n in _NAME_SPLIT_RE.split(fields.get(Label.SPEAKERS, "")))
        timecode = _TIMECODE_RE.search(chunk)
        duration = _seconds(timecode.group(1)) if timecode else None
        if duration is None:
            # 縮圖上的 timecode 就是影片長度；沒有的話退回結束減開始
            start, end = _seconds(fields.get(Label.START, "")), _seconds(fields.get(Label.END, ""))
            duration = max(0, end - start) if start is not None and end is not None else 0
        cards.append(Card(
            guid=guid.group(1).lower(),
            session=fields.get(Label.SESSION, ""),
            agenda=fields.get(Label.AGENDA, ""),
            speakers=tuple(dict.fromkeys(n for n in names if n)),
            date=_roc_to_iso(fields.get(Label.DATE, "")),
            start=fields.get(Label.START, ""),
            duration_seconds=duration,
        ))
    return cards


def parse_echo(page: str) -> tuple[str, int] | None:
    """(篩選條件回顯, 總筆數)；頁面上沒有就 None。"""
    match = _ECHO_RE.search(page)
    return (html.unescape(match.group(1)), int(match.group(2))) if match else None


def parse_file_key(page: str) -> str:
    """播放器頁 → 媒體檔 key（KEY+SEQ，例：0408R1150916020）；找不到就空字串。"""
    match = _FILE_KEY_RE.search(page)
    return match.group(1) + match.group(2) if match else ""


# --- 篩選與講者規則 ---


def classify(agenda: str, include_mixed: bool = True) -> Slot | None:
    """只收質詢（決策 2）。其餘——報告事項、三讀、討論議案、預備會議——回 None。"""
    compact = _compact(agenda)
    if compact == _GENERAL:
        return Slot.GENERAL
    if _BUSINESS_MARK in compact:
        if _INDIVIDUAL_RE.search(compact):
            return Slot.INDIVIDUAL
        if _CAUCUS_RE.search(compact):
            return Slot.CAUCUS
        return Slot.BUSINESS
    if include_mixed and _MIXED_RE.search(compact):
        return Slot.MIXED
    return None


def _is_report_items(agenda: str) -> bool:
    compact = _compact(agenda)
    return compact == _REPORT_ITEMS or compact.endswith("-" + _REPORT_ITEMS)


def day_chair(cards: Iterable[Card]) -> str:
    """當天開場「報告事項」那段只列一個名字時，那就是當天的主席；否則空字串。

    業務質詢由審查會召集人輪流主持，名冊上看不出是誰；但每天開場的報告事項只有
    主席一個人在講話，影音系統也就只列他一個。
    """
    reports = [c for c in cards if _is_report_items(c.agenda)]
    if not reports:
        return ""
    # 開始時間是 HH:MM:SS，字串比較就是時間比較
    opening = min(reports, key=lambda c: c.start or "99:99:99")
    return opening.speakers[0] if len(opening.speakers) == 1 else ""


@dataclass(frozen=True)
class NtpcRoster:
    """講者規則要用到的名冊：誰是議長／副議長、誰屬於哪個黨團。姓名都已正規化。"""

    chairs: frozenset[str] = frozenset()
    caucus_of: Mapping[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.chairs and not self.caucus_of

    @classmethod
    def from_rows(cls, rows: Iterable[tuple[str, str, str]]) -> NtpcRoster:
        """(姓名, 職位, 黨團) 的列 → 名冊。"""
        chairs: set[str] = set()
        caucus_of: dict[str, str] = {}
        for name, role, caucus in rows:
            name = normalize_name(name)
            if role in CHAIR_ROLES:
                chairs.add(name)
            if caucus:
                caucus_of[name] = caucus
        return cls(frozenset(chairs), caucus_of)

    @classmethod
    def from_records(cls, records: Iterable[MemberRecord]) -> NtpcRoster:
        return cls.from_rows((r.name, r.role, r.caucus) for r in records)

    @classmethod
    def from_db(cls) -> NtpcRoster:
        """目前在任的新北任期。名冊只有現在的狀態、沒有歷史，所以回補舊會期時用的也是
        現在的名冊：第 4 屆的議長、副議長沒換過人（4-6～4-8 三個會期的講者全部對得上
        現在的名冊）；換屆（115-12-25）之後重同步即可。"""
        current = Membership.objects.filter(source=ArticleSource.NTPC, end_date__isnull=True)
        return cls.from_rows(current.values_list("name", "role", "caucus"))


def speakers_for(card: Card, slot: Slot, roster: NtpcRoster, chair: str = "") -> list[str]:
    """決策 3：這段影片要掛誰的名字。

    影音系統的「發言議員」= 這段實際發言的議員 + 主席。議長、副議長從不質詢；業務
    質詢的主持人是審查會召集人，不屬於該時段黨團時也會被列進來。

    名冊是空的（還沒同步成功）時只能做不需要名冊的事：個人時段照字樣認人，其他時段
    拿掉開場報告事項認出來的當天主席——就是「移除已知主席」。
    """
    compact = _compact(card.agenda)
    if slot is Slot.INDIVIDUAL:
        # 個人時段就是那一位的：「李翁」+「月娥」、「馬見Lahuy．Ipin」+「」
        match = _INDIVIDUAL_RE.search(compact)
        return [normalize_name(match.group(1) + match.group(2))]
    names = [n for n in card.speakers if n not in roster.chairs]
    if chair and (slot is Slot.MIXED or not roster.caucus_of):
        names = [n for n in names if n != chair]
    if slot is Slot.CAUCUS and roster.caucus_of:
        caucus = _CAUCUS_RE.search(compact).group(1)
        names = [n for n in names if roster.caucus_of.get(n) == caucus]
    return names


# --- HTTP ---


class NtpcHttp:
    """帶 cookie jar 的小客戶端。查詢條件存在伺服器的 session 裡，查詢與翻頁一定要
    用同一個 cookie；session 壞掉（格式錯的查詢之後一直 500）就 reset 換一個。

    urllib 遇到 POST 的 302 會改用 GET 跟過去，正好是這個網站要的。
    """

    def __init__(self, base: str, timeout: float = _TIMEOUT):
        self._base = base.rstrip("/")
        self._timeout = timeout
        self.reset()

    def reset(self) -> None:
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=_ssl_context()),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def post_form(self, path: str, fields: Mapping[str, str]) -> str:
        body = urllib.parse.urlencode(fields).encode("ascii")
        with self._opener.open(self._base + path, data=body, timeout=self._timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def get(self, path: str) -> str:
        with self._opener.open(self._base + path, timeout=self._timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")


# --- 來源 ---


class NtpcDailySource:
    name = "新北市議會"

    def __init__(self, base: str | None = None, roster: NtpcRoster | None = None,
                 http: NtpcHttp | None = None, sleep: Callable[[float], None] = time.sleep,
                 include_mixed: bool = True):
        # 預設值在呼叫時才讀 settings：寫在參數預設值裡會在 import 當下就定死
        self._base = (base or settings.NTPC_VOD_BASE).rstrip("/")
        self._roster = roster if roster is not None else NtpcRoster()
        self._http = http or NtpcHttp(self._base)
        self._sleep = sleep
        self._include_mixed = include_mixed
        self._requests = 0
        self._warned_roster = False

    def clips_for(self, day: date, only_with_transcript: bool = True) -> list[IvodClip]:
        """某一天的質詢片段，一個媒體檔一篇。only_with_transcript 只為了跟立法院同
        介面：新北沒有逐字稿，一律走語音辨識。"""
        cards = self._cards_for(day)
        chair = day_chair(cards)
        if not self._roster.caucus_of and not self._warned_roster:
            self._warned_roster = True
            logger.warning("新北市議會的名冊沒有黨團資料（還沒同步成功？）：只套用個人時段"
                           "與移除已知主席，黨團時段不過濾")
        kept: list[tuple[Card, list[str]]] = []
        for card in cards:
            slot = classify(card.agenda, self._include_mixed)
            if slot is None or card.duration_seconds < MIN_SECONDS:
                continue
            names = speakers_for(card, slot, self._roster, chair)
            if not names:
                # 例如名冊跟影音系統的寫法對不上；不登記，之後重跑再看
                logger.info("新北市議會片段 %s（%s）過濾後沒有講者，先不登記",
                            card.guid, card.agenda)
                continue
            kept.append((card, names))

        clips: dict[str, IvodClip] = {}
        unresolved = 0
        for card, names in kept:
            key = self._file_key(card.guid)
            if not key:
                unresolved += 1
                continue
            if key in clips:
                # 同一個檔案上架兩次（兩個 GUID）：留第一個看到的
                continue
            clips[key] = IvodClip(
                ivod_id=f"{SOURCE}-{key}",
                date=card.date,
                speaker=SPEAKER_SEPARATOR.join(names),
                meeting=" ".join(p for p in (card.session, card.agenda) if p),
                duration_seconds=card.duration_seconds,
                ivod_url=self._base + _VIEW_PATH.format(guid=card.guid),
                has_transcript=True,
                source=SOURCE,
            )
        if kept and unresolved == len(kept):
            # 一段都對不到檔案，多半是播放器頁改版；安靜地回空清單會讓人以為那天沒開會
            raise SourceUnavailable(f"新北市議會 {day} 的 {len(kept)} 段都找不到媒體檔")
        return sorted(clips.values(), key=lambda c: c.ivod_id)

    def _cards_for(self, day: date) -> list[Card]:
        wanted = day.strftime("%Y/%m/%d")
        page = self._search(wanted)
        total = self._check_echo(page, wanted)
        found: dict[str, Card] = {}
        for card in parse_cards(page):
            found.setdefault(card.guid, card)
        page_no = 1
        # 每頁 12 筆；只有一頁時沒有分頁列。翻到湊滿總筆數、或某一頁沒有新卡片為止
        while len(found) < total:
            page_no += 1
            try:
                page = self._request(self._http.get, _TO_PAGE_PATH.format(page=page_no))
            except OSError as e:
                raise SourceUnavailable(f"新北市議會的影音清單第 {page_no} 頁抓不到：{str(e)[:200]}") from e
            self._check_echo(page, wanted)
            new = [c for c in parse_cards(page) if c.guid not in found]
            if not new:
                break
            for card in new:
                found[card.guid] = card
        if len(found) < total:
            logger.warning("新北市議會 %s：總筆數 %d，只拿到 %d 筆", wanted, total, len(found))
        iso = day.isoformat()
        cards = [c for c in found.values() if c.date == iso]
        if len(cards) < len(found):
            logger.warning("新北市議會 %s：%d 張卡片的開會日期不是這天，略過",
                           wanted, len(found) - len(cards))
        return cards

    def _search(self, wanted: str) -> str:
        # 9 個欄位一個都不能少：少一個伺服器就靜默忽略篩選
        fields = {"pageindex": "1", "MJ": "", "MP": "", "MType": "",
                  "sMDate": wanted, "eMDate": wanted, "Keyword": "", "cEPName": "",
                  "Sort": "MDate"}
        for attempt in range(2):
            try:
                return self._request(self._http.post_form, _SEARCH_PATH, fields)
            # HTTPError 是 OSError 的子類別，要排在前面才能只針對 500 重試
            except urllib.error.HTTPError as e:
                if e.code == 500 and attempt == 0:
                    # 這個 session 之後會一直 500；換一個 cookie 重來一次
                    logger.warning("新北市議會的影音查詢回 500，換一個 session 重試")
                    self._http.reset()
                    continue
                raise SourceUnavailable(f"新北市議會的影音查詢失敗：{str(e)[:200]}") from e
            except OSError as e:
                raise SourceUnavailable(f"新北市議會的影音查詢失敗：{str(e)[:200]}") from e
        raise AssertionError("unreachable：迴圈每次都會 return、continue 或 raise")

    def _check_echo(self, page: str, wanted: str) -> int:
        """確認頁面回顯的是這一天的查詢，回傳總筆數。"""
        echo = parse_echo(page)
        if echo is None or f"{wanted}~{wanted}" not in echo[0]:
            shown = echo[0] if echo else "沒有回顯"
            raise SourceUnavailable(f"新北市議會的查詢條件沒有生效（{shown}），頁面格式可能改了")
        return echo[1]

    def _file_key(self, guid: str) -> str:
        try:
            page = self._request(self._http.get, _PLAYER_PATH.format(guid=guid))
        except OSError as e:
            logger.warning("新北市議會片段 %s 的播放器頁抓不到：%s", guid, e)
            return ""
        key = parse_file_key(page)
        if not key:
            logger.warning("新北市議會片段 %s 的播放器頁找不到媒體檔 key", guid)
        return key

    def _request(self, call: Callable[..., str], *args) -> str:
        if self._requests:
            # 第一次不等；之後每次請求前留一秒，多天回補才不會一口氣打爆對方
            self._sleep(_PACE_SECONDS)
        self._requests += 1
        return call(*args)
