"""議員名單同步：從各議會的來源抓「誰、哪個黨、哪個選區」，維護 Person／Membership，
並把文章連到講者「當時」的任期。

兩個來源都只給目前的黨籍，沒有異動日期：換黨只能「偵測到就切一段」，日期由人
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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date

from django.db import transaction
from django.utils import timezone

from .models import Article, ArticleSource, Membership, Person

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
