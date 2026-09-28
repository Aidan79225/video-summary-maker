"""議員名單同步：從各議會的來源抓「誰、哪個黨、哪個選區」，維護 Person／Membership，
並把文章連到講者「當時」的任期。

每個來源都只給目前的黨籍，沒有異動日期：換黨只能「偵測到就切一段」，日期由人
到 admin 補。認人只用姓名與別名，同名多個一律標 needs_review、不猜。
"""
from __future__ import annotations

import html
import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import date

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Article, ArticleSource, Membership, Person
from .names import normalize_name

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
_RETRY_DELAYS = (2.0, 4.0, 8.0)
SPEAKER_SEPARATOR = "、"


@dataclass(frozen=True)
class MemberRecord:
    source: str
    external_id: str
    name: str
    party: str
    district: str = ""
    term: str = ""
    photo_url: str = ""
    start_date: date | None = None
    end_date: date | None = None
    # 只有新北填：議長／副議長／空、國民黨團／民進黨團／無黨團結聯盟／空
    role: str = ""
    caucus: str = ""


@dataclass
class SyncReport:
    seen: int = 0
    created_persons: int = 0
    created_memberships: int = 0
    updated: int = 0
    party_changes: int = 0
    review: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        text = (f"名單 {self.seen} 筆：新人物 {self.created_persons}、新任期 {self.created_memberships}、"
                f"更新 {self.updated}、換黨 {self.party_changes}")
        if self.review:
            text += f"、待人工確認 {self.review}"
        return text


class MembersUnavailable(Exception):
    """名單來源這次拿不到；屬於暫時性問題，既有資料不動。"""


def _http_get(url: str, sleep: Callable[[float], None] = time.sleep) -> str:
    """含 429 退避；市議會的憑證鏈在 Python 3.13 的嚴格驗證下會被拒絕，同 tccc_source。"""
    context = ssl.create_default_context()
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            with urllib.request.urlopen(url, timeout=_TIMEOUT, context=context) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < len(_RETRY_DELAYS):
                sleep(_RETRY_DELAYS[attempt])
                continue
            raise
    raise AssertionError("unreachable")


def _parse_date(text: object) -> date | None:
    """LYAPI 的日期是 2026/02/01；空字串、None 都當成沒有。"""
    if not isinstance(text, str) or not text.strip():
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return date.fromisoformat(time.strftime("%Y-%m-%d", time.strptime(text.strip(), fmt)))
        except ValueError:
            continue
    return None


# --- 立法院 ---


class LyMemberSource:
    name = "立法院"

    def __init__(self, base: str, term: int, fetch=_http_get):
        self._base = base.rstrip("/")
        self._term = term
        self._fetch = fetch

    def fetch(self) -> list[MemberRecord]:
        records: list[MemberRecord] = []
        page = 1
        while True:
            query = urllib.parse.urlencode({"屆": self._term, "limit": 200, "page": page})
            try:
                payload = json.loads(self._fetch(f"{self._base}/legislators?{query}"))
            except (OSError, ValueError) as e:
                raise MembersUnavailable(f"LYAPI 的立委名單抓不到：{str(e)[:200]}") from e
            rows = payload.get("legislators") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise MembersUnavailable("LYAPI 的立委名單回應少了 legislators 欄位")
            records.extend(r for r in (self._record(row) for row in rows) if r)
            try:
                total_pages = int(payload.get("total_page") or 1)
            except (TypeError, ValueError):
                total_pages = 1
            if page >= total_pages or not rows:
                break
            page += 1
        return records

    def _record(self, row: object) -> MemberRecord | None:
        if not isinstance(row, dict):
            return None
        name = str(row.get("委員姓名") or "").strip()
        if not name:
            return None
        left = str(row.get("是否離職") or "").strip() in ("是", "Y", "true", "True")
        return MemberRecord(
            source=ArticleSource.LY,
            external_id=str(row.get("歷屆立法委員編號") or ""),
            name=name,
            party=str(row.get("黨籍") or "").strip(),
            district=str(row.get("選區名稱") or "").strip(),
            term=f"第{row.get('屆') or self._term}屆",
            photo_url=str(row.get("照片位址") or "").strip(),
            start_date=_parse_date(row.get("到職日")),
            end_date=_parse_date(row.get("離職日期")) if left else None,
        )


# --- 臺中市議會 ---

_TCCC_LIST_RE = re.compile(r'href="main\.asp\?uno=14&cno=(\d+)"[^>]*>\s*([^<\s][^<]*?)\s*<')
_TCCC_PARTY_RE = re.compile(r"黨[藉籍]\s*(?:&nbsp;|\s)*([^\s<&]{2,12})")
_TCCC_DISTRICT_RE = re.compile(r"(第[一二三四五六七八九十]+選區|山地原住民|平地原住民)")
_TCCC_TERM_RE = re.compile(r"直轄市第([一二三四五六七八九十]+)屆議員")
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def parse_tccc_list(page: str) -> list[tuple[str, str]]:
    """議員一覽表：(官網編號, 姓名)，同一人出現兩次（照片與姓名各一個連結）要去重。"""
    seen: dict[str, str] = {}
    for cno, name in _TCCC_LIST_RE.findall(page):
        name = html.unescape(name).strip()
        if name and cno not in seen:
            seen[cno] = name
    return list(seen.items())


def parse_tccc_profile(page: str) -> tuple[str, str, str]:
    """個人頁：(政黨, 選區, 屆次)。官網把「黨籍」打成「黨藉」，兩種都認。"""
    text = re.sub(r"<[^>]+>", " ", page)
    party = _TCCC_PARTY_RE.search(text)
    district = _TCCC_DISTRICT_RE.search(text)
    term = _TCCC_TERM_RE.search(text)
    term_text = ""
    if term:
        number = _CN_NUM.get(term.group(1))
        term_text = f"第{number}屆" if number else f"第{term.group(1)}屆"
    return (html.unescape(party.group(1)).strip() if party else "",
            district.group(1) if district else "", term_text)


class TcccMemberSource:
    name = "臺中市議會"

    def __init__(self, base: str, fetch=_http_get):
        self._base = base.rstrip("/")
        self._fetch = fetch

    def fetch(self) -> list[MemberRecord]:
        try:
            listing = parse_tccc_list(self._fetch(f"{self._base}/wb_introduction01.asp"))
        except OSError as e:
            raise MembersUnavailable(f"臺中市議會的議員名單抓不到：{e}") from e
        if not listing:
            raise MembersUnavailable("臺中市議會的議員名單解析不出任何人，頁面格式可能改了")
        records: list[MemberRecord] = []
        for cno, name in listing:
            try:
                page = self._fetch(f"{self._base}/wb_introduction02.asp?uno=&cno={cno}")
            except OSError as e:
                logger.warning("臺中市議會 %s（cno=%s）的個人頁抓不到：%s", name, cno, e)
                continue
            party, district, term = parse_tccc_profile(page)
            records.append(MemberRecord(source=ArticleSource.TCCC, external_id=cno, name=name,
                                        party=party, district=district, term=term))
        return records


# --- 新北市議會 ---

ROLE_SPEAKER = "議長"
ROLE_DEPUTY_SPEAKER = "副議長"
# 只主持、從不質詢的人：影音系統把他們列進每一段的「發言議員」，要拿掉
CHAIR_ROLES = frozenset({ROLE_SPEAKER, ROLE_DEPUTY_SPEAKER})
# 黨團頁 party-group-detail?program=39&P=<n>。值刻意取影音系統議程字樣的寫法
# （「…質詢-國民黨團發言」「…質詢-民進黨團聯合發言」），講者規則才能直接比對。
NTPC_CAUCUSES = {"1": "國民黨團", "2": "民進黨團", "3": "無黨團結聯盟"}
# 官網寫「無政黨」，立法院與臺中寫「無黨籍」；統一成後者，政黨篩選才不會分成兩個
_NTPC_PARTY_ALIASES = {"無政黨": "無黨籍"}
_NTPC_PACE_SECONDS = 1.0

_NTPC_AREA_RE = re.compile(r'<div class="review-meeting all-list" id="area(\d+)"')
# href 的引號前有一個空白（`C=590 "`），名字前後也有一堆空白
_NTPC_MEMBER_RE = re.compile(
    r'<a href="councilor-detail\?program=37&(?:amp;)?A=\d+&(?:amp;)?C=(\d+)\s*"[^>]*>'
    r'.*?<p>\s*([^<]*?)\s*</p>', re.S)
_NTPC_PARTY_RE = re.compile(r"<li>\s*政黨：\s*([^<]*?)\s*</li>")
_NTPC_CURRENT_RE = re.compile(r"<h4>\s*現任\s*</h4>\s*<ul>(.*?)</ul>", re.S)
_NTPC_POST_RE = re.compile(r"第(\d+)屆(副議長|議長|議員)")
_NTPC_GROUP_MEMBER_RE = re.compile(r'href="councilor-detail\?[^"]*?C=(\d+)')


def parse_ntpc_list(page: str) -> list[tuple[str, str, str]]:
    """議員總覽：(官網編號 C, 正規化後的姓名, 選區編號)。

    選區只能從這頁的 `id="area<N>"` 區塊拿：個人頁上的選區只是把網址的 A 參數
    回顯出來（A 亂填，頁面就跟著顯示錯的選區），不可信。
    """
    marks = list(_NTPC_AREA_RE.finditer(page))
    seen: dict[str, tuple[str, str]] = {}
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(page)
        for cid, name in _NTPC_MEMBER_RE.findall(page[mark.end():end]):
            name = normalize_name(html.unescape(name))
            if name and cid not in seen:
                seen[cid] = (name, mark.group(1))
    return [(cid, name, area) for cid, (name, area) in seen.items()]


def parse_ntpc_profile(page: str) -> tuple[str, str, str]:
    """個人頁：(政黨, 職位, 屆次)。職位是議長／副議長／空。

    職位與屆次只看「現任」清單：「經歷」裡也有「新北市第2、3屆議長」這種字樣。
    """
    party_match = _NTPC_PARTY_RE.search(page)
    party = html.unescape(party_match.group(1)).strip() if party_match else ""
    party = _NTPC_PARTY_ALIASES.get(party, party)
    current = _NTPC_CURRENT_RE.search(page)
    role, term = "", ""
    for number, post in _NTPC_POST_RE.findall(current.group(1) if current else ""):
        term = term or f"第{number}屆"
        if post in CHAIR_ROLES and not role:
            role = post
    return party, role, term


def parse_ntpc_caucus(page: str) -> list[str]:
    """黨團頁上的成員（官網編號 C），依頁面順序、去重。"""
    return list(dict.fromkeys(_NTPC_GROUP_MEMBER_RE.findall(page)))


class NtpcMemberSource:
    """新北市議會官網（www.ntp.gov.tw）：一次拿到全部 64 位，再逐一看個人頁。

    共 1 + 3 + 64 次請求，循序、間隔一秒——對方是市議會的網站，一週才跑一次，
    慢一分鐘沒有關係。
    """

    name = "新北市議會"

    def __init__(self, base: str | None = None, fetch=_http_get,
                 sleep: Callable[[float], None] = time.sleep):
        # 預設值在呼叫時才讀 settings：寫在參數預設值裡會在 import 當下就定死
        self._base = (base or settings.NTPC_WEB_BASE).rstrip("/")
        self._fetch = fetch
        self._sleep = sleep
        self._requests = 0

    def fetch(self) -> list[MemberRecord]:
        try:
            listing = parse_ntpc_list(self._get("councilor-all?program=37"))
        except OSError as e:
            raise MembersUnavailable(f"新北市議會的議員名單抓不到：{e}") from e
        if not listing:
            raise MembersUnavailable("新北市議會的議員名單解析不出任何人，頁面格式可能改了")
        caucus_of = self._caucuses()
        records: list[MemberRecord] = []
        for cid, name, area in listing:
            try:
                page = self._get(f"councilor-detail?program=37&A={area}&C={cid}")
            except OSError as e:
                logger.warning("新北市議會 %s（C=%s）的個人頁抓不到：%s", name, cid, e)
                continue
            party, role, term = parse_ntpc_profile(page)
            if not party and not term:
                # 錯誤頁也可能回 200。當成抓不到：寫進去會把議長的職位洗成空的
                logger.warning("新北市議會 %s（C=%s）的個人頁解析不出政黨與屆次，略過", name, cid)
                continue
            records.append(MemberRecord(
                source=ArticleSource.NTPC, external_id=cid, name=name, party=party,
                district=f"第{area}選區", term=term, role=role,
                caucus=caucus_of.get(cid, "")))
        # 有人的「現任」只列社團職務、沒寫「新北市第4屆議員」（2026-09 的洪佳君）。
        # 總覽頁列的都是本屆議員，屆次就用其他人頁面上的那一屆補。
        terms = Counter(r.term for r in records if r.term)
        if terms:
            current = terms.most_common(1)[0][0]
            records = [r if r.term else replace(r, term=current) for r in records]
        return records

    def _caucuses(self) -> dict[str, str]:
        """官網編號 → 黨團。任何一頁拿不到就整批放棄：同步是直接覆寫黨團欄位的，
        少一頁會把一整個黨團的人寫成「沒有黨團」，講者規則就會把他們濾掉。"""
        caucus_of: dict[str, str] = {}
        for number, caucus in NTPC_CAUCUSES.items():
            try:
                members = parse_ntpc_caucus(self._get(f"party-group-detail?program=39&P={number}"))
            except OSError as e:
                raise MembersUnavailable(f"新北市議會的{caucus}名單抓不到：{e}") from e
            if not members:
                raise MembersUnavailable(f"新北市議會的{caucus}名單解析不出任何人，頁面格式可能改了")
            for cid in members:
                caucus_of.setdefault(cid, caucus)
        return caucus_of

    def _get(self, path: str) -> str:
        if self._requests:
            self._sleep(_NTPC_PACE_SECONDS)
        self._requests += 1
        return self._fetch(f"{self._base}/{path}")


# --- 同步 ---


def _find_person(name: str) -> tuple[Person | None, bool]:
    """(人, 是否含糊)。恰一個就回它；沒有回 None；多個回 None 並標含糊。"""
    matches = list(Person.objects.filter(name=name))
    # 別名存在 JSON 裡，SQLite 不支援 contains 查詢；人物最多幾百個，在 Python 裡比對就好
    matches += [p for p in Person.objects.exclude(aliases=[])
                if name in (p.aliases or []) and p not in matches]
    if len(matches) == 1:
        return matches[0], False
    return None, len(matches) > 1


@transaction.atomic
def sync(records: Iterable[MemberRecord], today: date | None = None) -> SyncReport:
    today = today or timezone.localdate()
    report = SyncReport()
    now = timezone.now()
    for record in records:
        report.seen += 1
        membership = _existing(record)
        if membership is None:
            person, ambiguous = _find_person(record.name)
            if person is None:
                person = Person.objects.create(name=record.name, needs_review=ambiguous)
                report.created_persons += 1
                if ambiguous:
                    report.review += 1
                    logger.warning("同名多人：%s（%s），已另建一個人物，請到 admin 確認",
                                   record.name, record.source)
            membership = Membership(person=person, source=record.source,
                                    external_id=record.external_id, name=record.name,
                                    party=record.party, start_date=record.start_date)
            report.created_memberships += 1
        elif membership.party != record.party and record.party:
            # 換黨：舊的結束在今天、新開一筆。真正的日期來源不給，標起來讓人補。
            membership.end_date = today
            membership.needs_review = True
            membership.synced_at = now
            membership.save()
            logger.warning("%s 的黨籍由「%s」變成「%s」，已自動切段，請到 admin 補日期",
                           record.name, membership.party, record.party)
            membership = Membership(person=membership.person, source=record.source,
                                    external_id=record.external_id, name=record.name,
                                    party=record.party, start_date=today, needs_review=True)
            report.party_changes += 1
            report.review += 1
        else:
            report.updated += 1
        membership.district = record.district or membership.district
        membership.term = record.term or membership.term
        membership.photo_url = record.photo_url or membership.photo_url
        # 直接覆寫、不「空的就保留舊值」：卸任的議長要變回空的，否則下一屆的新聞
        # 會把他當主持人拿掉。來源抓不到的人根本不會出現在 records 裡，不會被寫空。
        membership.role = record.role
        membership.caucus = record.caucus
        if record.end_date:
            membership.end_date = record.end_date
        membership.synced_at = now
        membership.save()
    return report


def _existing(record: MemberRecord) -> Membership | None:
    """先用來源給的編號找，沒有編號（或改過）再用「同來源、同名、現任」。"""
    queryset = Membership.objects.filter(source=record.source)
    if record.external_id:
        found = queryset.filter(external_id=record.external_id).order_by("-id").first()
        if found:
            return found
    return queryset.filter(name=record.name, end_date__isnull=True).order_by("-id").first()


# --- 文章連結 ---


def membership_for(source: str, name: str, on: date) -> Membership | None:
    candidates = [m for m in Membership.objects.filter(source=source, name=name)
                  .select_related("person") if m.covers(on)]
    if not candidates:
        return None
    # 多筆重疊時取最晚開始的：那是最新的一段
    candidates.sort(key=lambda m: (m.start_date or date.min), reverse=True)
    return candidates[0]


def link_article(article: Article, save: bool = True) -> None:
    """依講者與日期填 party／membership。聯合質詢多人：黨去重、保留順序；
    membership 只在單一講者時填。"""
    names = [n.strip() for n in article.speaker.split(SPEAKER_SEPARATOR) if n.strip()]
    found = [membership_for(article.source, n, article.date) for n in names]
    parties: list[str] = []
    for m in found:
        if m and m.party and m.party not in parties:
            parties.append(m.party)
    article.party = SPEAKER_SEPARATOR.join(parties)
    article.membership = found[0] if len(found) == 1 else None
    if save:
        article.save(update_fields=["party", "membership", "updated_at"])


def relink_all() -> int:
    count = 0
    for article in Article.objects.all().iterator():
        link_article(article)
        count += 1
    return count
