"""人物側寫（第一步：投入量＋具體度）：從既有文章算出每個人每個會期的指標，存進 ProfileStat。

原則照 issue #24：不加總、不排名、只跟同一議會同一會期的人比、樣本不足不給百分位、
每個數字都能點回文章清單——/api/articles 用證據網址的篩選條件查出來的篇數，必須等於
這裡算出來的 n。所以「誰算講者」「什麼算單獨發言」「什麼算有摘要卡」兩邊要用同一套定義。

純程式計算、不打模型。Pi 每晚匯入後跑一次（run_scheduler），整批重算、冪等。
Pi 的資料庫是 SD 卡上的 SQLite：任期一個來源只讀一次、文章一個會期只讀一次，
在記憶體裡對名字，不要每篇查一次。
"""
from __future__ import annotations

import logging
import math
import re
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from fractions import Fraction

from django.db import transaction
from django.db.models import Max, Min
from django.utils import timezone

from .members_sync import CHAIR_ROLES, SPEAKER_SEPARATOR
from .models import Article, ArticleSource, ArticleStatus, Membership, Person, ProfileStat, Session

logger = logging.getLogger(__name__)

# 分母未滿這個數就只給原始計數、不給百分位（跟查證相符率的規則相同）
MIN_SAMPLE = 5
# 同儕少於這個數就誰都不比：四個人裡的「第 75 百分位」沒有意義
MIN_PEERS = 5

# 一次 UPDATE 最多帶幾個 id。SQLite 的綁定參數有上限，舊版是 999。
_ID_CHUNK = 500


# --- 會期 ---

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
# 立法院：「第11屆第5會期財政委員會…」「第11屆第3會期第1次臨時會第2次會議」。臨時會
# 併入它所屬的會期——它是同一批委員在同一段時間裡的延長，拆開來每段都只有幾天。
_LY_SESSION_RE = re.compile(r"第(\d+)屆第(\d+)會期")
# 市議會：「第4屆第8次定期會 市政總質詢」「第4屆第2次臨時會」。臨時會是另一次會議，
# 有自己的議程，原樣當成一個會期。
_COUNCIL_SESSION_RE = re.compile(r"第(\d+)屆第(\d+)次(定期會|臨時會)")


@dataclass(frozen=True)
class SessionKey:
    source: str
    term: str
    name: str


def parse_session(source: str, meeting: str) -> SessionKey | None:
    """從會議名稱解析會期。解析不出來（例如「立法院朝野黨團協商」）回 None，那篇不計入。

    名稱用解析出來的數字重組，不照抄原文：全形數字與「第05會期」這種寫法才會併成同一個會期。
    """
    text = (meeting or "").translate(_FULLWIDTH_DIGITS)
    if source == ArticleSource.LY:
        match = _LY_SESSION_RE.search(text)
        if not match:
            return None
        term, number = int(match.group(1)), int(match.group(2))
        return SessionKey(str(source), str(term), f"第{term}屆第{number}會期")
    match = _COUNCIL_SESSION_RE.search(text)
    if not match:
        return None
    term, number, kind = int(match.group(1)), int(match.group(2)), match.group(3)
    return SessionKey(str(source), str(term), f"第{term}屆第{number}次{kind}")


def _as_date(value) -> date:
    # 剛 create 還沒重讀的 article，date 可能還是字串（同 members_sync.link_article）
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def attach_session(article: Article, save: bool = True) -> Session | None:
    """登記新文章時就掛上會期，並把會期的涵蓋範圍撐開到這一篇的日期。

    涵蓋範圍每晚還會整批重算一次（assign_sessions）；這裡先撐開，是為了讓新會期在重算
    之前就有合理的起訖，而不是空的。
    """
    key = parse_session(article.source, article.meeting)
    if key is None:
        return None
    session, _ = Session.objects.get_or_create(source=key.source, name=key.name,
                                               defaults={"term": key.term})
    day = _as_date(article.date)
    changed = []
    if session.start_date is None or day < session.start_date:
        session.start_date = day
        changed.append("start_date")
    if session.end_date is None or day > session.end_date:
        session.end_date = day
        changed.append("end_date")
    if changed:
        session.save(update_fields=changed)
    article.session = session
    if save:
        article.save(update_fields=["session", "updated_at"])
    return session


def assign_sessions() -> int:
    """替還沒掛會期的文章（任何狀態）掛上會期，並重算每個會期的資料涵蓋範圍。

    回傳這次掛上幾篇。用 UPDATE 而不是逐篇 save：不動 updated_at——那是判斷
    「處理中卡太久」的依據，掛會期不該讓一篇卡住的文章看起來剛動過。
    """
    sessions = {(s.source, s.name): s for s in Session.objects.all()}
    wanted: dict[SessionKey, list[int]] = defaultdict(list)
    for article_id, source, meeting in (Article.objects.filter(session__isnull=True)
                                        .values_list("id", "source", "meeting").iterator()):
        key = parse_session(source, meeting)
        if key is not None:
            wanted[key].append(article_id)

    assigned = 0
    with transaction.atomic():
        for key, ids in wanted.items():
            session = sessions.get((key.source, key.name))
            if session is None:
                # get_or_create 而不是 create：匯入（attach_session）可能剛好同時建了同一個
                session, _ = Session.objects.get_or_create(source=key.source, name=key.name,
                                                           defaults={"term": key.term})
                sessions[(key.source, key.name)] = session
            for start in range(0, len(ids), _ID_CHUNK):
                assigned += (Article.objects.filter(id__in=ids[start:start + _ID_CHUNK])
                             .update(session=session))
        _refresh_coverage(sessions.values())
    return assigned


def _refresh_coverage(sessions: Iterable[Session]) -> None:
    """start_date／end_date = 掛在這個會期的文章（任何狀態）最早與最晚的日期。"""
    spans = {row["session"]: (row["first"], row["last"])
             for row in (Article.objects.filter(session__isnull=False).order_by()
                         .values("session").annotate(first=Min("date"), last=Max("date")))}
    changed = []
    for session in sessions:
        span = spans.get(session.id, (None, None))
        if (session.start_date, session.end_date) != span:
            session.start_date, session.end_date = span
            changed.append(session)
    if changed:
        Session.objects.bulk_update(changed, ["start_date", "end_date"])


# --- 人與文章的對應 ---


def speaker_names(speaker: str) -> list[str]:
    """講者欄位拆成名字（聯合質詢是「甲、乙、丙」），去空白、去重、保留順序。"""
    names = (n.strip() for n in (speaker or "").split(SPEAKER_SEPARATOR))
    return list(dict.fromkeys(n for n in names if n))


def is_solo(speaker: str) -> bool:
    """單獨發言：講者欄位沒有「、」。跟 /api/articles?solo=1 是同一個定義，證據才對得上。"""
    return SPEAKER_SEPARATOR not in (speaker or "")


def _overlaps(first: date | None, last: date | None,
              start: date | None, end: date | None) -> bool:
    """任期 [first, last] 跟會期涵蓋範圍 [start, end] 有沒有重疊。任期的空值是無限早／現任。"""
    if start is None or end is None:
        return False
    return (first is None or first <= end) and (last is None or last >= start)


@dataclass(frozen=True)
class _Term:
    person_id: int
    name: str
    start: date | None
    end: date | None
    chair: bool

    def covers(self, day: date) -> bool:
        return (self.start is None or self.start <= day) and (self.end is None or day <= self.end)


class _Roster:
    """一個來源的所有任期，一次載入，在記憶體裡對名字。"""

    def __init__(self, terms: Sequence[_Term]):
        self._terms = list(terms)
        self._by_name: dict[str, list[_Term]] = defaultdict(list)
        for term in self._terms:
            self._by_name[term.name].append(term)

    @classmethod
    def load(cls, source: str) -> _Roster:
        # 跟 members_sync.membership_for 同一個排序：多筆重疊時取最晚開始的、同日再取最新建的
        rows = (Membership.objects.filter(source=source).order_by("-start_date", "-id")
                .values_list("person_id", "name", "start_date", "end_date", "role"))
        return cls([_Term(pid, name, start, end, role in CHAIR_ROLES)
                    for pid, name, start, end, role in rows])

    def person_for(self, name: str, day: date) -> int | None:
        """同來源、同名、任期涵蓋文章日期——跟 members_sync.link_article 同一套規則。"""
        candidates = [t for t in self._by_name.get(name, ()) if t.covers(day)]
        if not candidates:
            return None
        return max(candidates, key=lambda t: t.start or date.min).person_id

    def population(self, start: date | None, end: date | None) -> set[int]:
        """任期與會期涵蓋範圍有重疊的人，去掉議長、副議長。

        議長、副議長只主持、不質詢，放進母體只是一堆零，會把其他人的百分位墊高。
        只有新北的名冊有這欄；立法院、臺中沒有，就沒有人被排除。
        """
        overlapping = [t for t in self._terms if _overlaps(t.start, t.end, start, end)]
        chairs = {t.person_id for t in overlapping if t.chair}
        return {t.person_id for t in overlapping} - chairs


def name_in_session(person: Person, session: Session) -> str:
    """證據連結用的名字：這個人在該來源任期上的寫法（也就是文章講者欄位的寫法）。

    有好幾段任期時，取跟會期重疊、最晚開始的那一段；都不重疊就取該來源最新的一段。
    """
    terms = list(Membership.objects.filter(person=person, source=session.source))
    overlapping = [m for m in terms
                   if _overlaps(m.start_date, m.end_date, session.start_date, session.end_date)]
    chosen = max(overlapping or terms, key=lambda m: (m.start_date or date.min, m.id), default=None)
    return chosen.name if chosen else person.name


# --- 指標 ---


@dataclass
class _Tally:
    """一個人在一個會期的原始計數。"""
    speeches: int = 0
    # 用分數累加：聯合質詢平分出來的 1000/3 秒用浮點數加，加的順序不同尾數就不同，
    # 兩個實際上一樣長的人會在取整時被分出高低
    seconds: Fraction = field(default_factory=Fraction)
    # 具體度的基礎文章：單獨發言、而且有摘要卡
    base: int = 0
    numbers: int = 0
    deadline_asks: int = 0
    sourced_numbers: int = 0

    def add_brief(self, brief: object) -> None:
        numbers, deadline_asks, sourced = brief_counts(brief)
        self.base += 1
        self.numbers += numbers
        self.deadline_asks += deadline_asks
        self.sourced_numbers += sourced


def _items(value: object) -> list[dict]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def brief_counts(brief: object) -> tuple[int, int, int]:
    """摘要卡的 (落地數字數, 帶期限的要求數, 有來源的數字數)。

    摘要卡的形狀由 ingest.clean_brief 釘死，這裡仍然防禦：不成形的卡片照樣算一篇基礎
    文章（/api/articles?has_brief=1 也會列出它），只是貢獻零。
    """
    if not isinstance(brief, dict):
        return 0, 0, 0
    numbers = _items(brief.get("key_numbers"))
    asks = _items(brief.get("asks"))
    with_deadline = sum(1 for a in asks if isinstance(a.get("deadline"), str) and a["deadline"].strip())
    sourced = sum(1 for n in numbers if n.get("sources"))
    return len(numbers), with_deadline, sourced


def _ratio(part: float, whole: int, scale: float = 1.0) -> float | None:
    return part / whole * scale if whole else None


def round_half_up(value: Fraction, places: int = 1) -> float:
    """四捨五入到小數 places 位（值不會是負的）。

    不用 round()：它是銀行家捨入，15 秒＝0.25 分鐘會變成 0.2；而浮點數的 1.05 其實是
    1.0500…04，又會進位成 1.1——同樣是「剛好一半」，結果看浮點數的尾數決定。讀者心裡的
    「取到小數一位」是四捨五入，所以用精確的分數算。
    """
    scale = 10 ** places
    return math.floor(value * scale + Fraction(1, 2)) / scale


VOLUME = "volume"
SPECIFICITY = "specificity"
BLOCK_TITLES = {VOLUME: "投入量", SPECIFICITY: "具體度"}


@dataclass(frozen=True)
class Indicator:
    key: str
    block: str
    label: str
    unit: str
    # n 的單位：頁面寫「n = 12 篇」
    n_unit: str
    # (值, n)
    measure: Callable[[_Tally], tuple[float | None, int]]
    # 套不套最小樣本。投入量沒有「分母」，不套；但母體少於 MIN_PEERS 一樣不給百分位
    min_sample: bool = False

    def sample_ok(self, n: int) -> bool:
        return not self.min_sample or n >= MIN_SAMPLE


SPEECHES = Indicator(
    "speeches", VOLUME, "發言次數", "次", "篇",
    # 聯合質詢每人各算一次
    lambda t: (float(t.speeches), t.speeches))
SPEAKING_MINUTES = Indicator(
    "speaking_minutes", VOLUME, "發言總時長", "分鐘", "篇",
    # 聯合質詢的時長平分給每位講者：分不出誰講了多久，平分是唯一不用猜的公式。
    # 先取到小數一位再比百分位，讀者看到一樣的數字就是同值。
    lambda t: (round_half_up(t.seconds / 60), t.speeches))
NUMBERS_PER_SPEECH = Indicator(
    "numbers_per_speech", SPECIFICITY, "每篇落地數字數", "個／篇", "篇",
    lambda t: (_ratio(t.numbers, t.base), t.base), min_sample=True)
DEADLINE_ASKS_PER_SPEECH = Indicator(
    "deadline_asks_per_speech", SPECIFICITY, "每篇帶期限的要求數", "項／篇", "篇",
    lambda t: (_ratio(t.deadline_asks, t.base), t.base), min_sample=True)
SOURCED_NUMBER_SHARE = Indicator(
    "sourced_number_share", SPECIFICITY, "有來源的數字占比", "%", "個數字",
    # sources 目前只放法條原文（#9）：實際上是「數字裡引用法條、而且找得到條文原文的比例」
    lambda t: (_ratio(t.sourced_numbers, t.numbers, 100.0), t.numbers), min_sample=True)

INDICATORS = (SPEECHES, SPEAKING_MINUTES, NUMBERS_PER_SPEECH, DEADLINE_ASKS_PER_SPEECH,
              SOURCED_NUMBER_SHARE)
INDICATOR_BY_KEY = {i.key: i for i in INDICATORS}


def blocks_for(source: str) -> list[tuple[str, str, list[Indicator]]]:
    """頁面上的區塊與指標順序：(區塊 key, 標題, 指標)。

    市議會時長在前（總質詢一次幾十分鐘、業務質詢十幾分鐘，時長比次數有意義），
    立法院次數在前。
    """
    volume = [SPEECHES, SPEAKING_MINUTES] if source == ArticleSource.LY else [SPEAKING_MINUTES, SPEECHES]
    specificity = [NUMBERS_PER_SPEECH, DEADLINE_ASKS_PER_SPEECH, SOURCED_NUMBER_SHARE]
    return [(VOLUME, BLOCK_TITLES[VOLUME], volume),
            (SPECIFICITY, BLOCK_TITLES[SPECIFICITY], specificity)]


def percentile(value: float, peer_values: Sequence[float]) -> float:
    """（同儕中比他低的人數 ＋ 0.5 × 同值的人數，含他自己）÷ 同儕人數 × 100。

    mid-rank：同值的人拿到同一個百分位，沒有人因為排序的先後被分出高低。
    peer_values 要先排好序。
    """
    lower = bisect_left(peer_values, value)
    equal = bisect_right(peer_values, value) - lower
    # 先乘 100 再除：0.5 ÷ 5 × 100 會變成 10.000000000000002
    return (lower + 0.5 * equal) * 100 / len(peer_values)


# --- 計算 ---


@dataclass
class SessionSummary:
    session: Session
    population: int
    ready_articles: int


@dataclass
class ProfileReport:
    assigned: int = 0
    sessions: list[SessionSummary] = field(default_factory=list)
    # (來源, 講者名字) → 篇數。通常是名冊的寫法跟影音系統不同，要人看
    unmatched: Counter = field(default_factory=Counter)

    def __str__(self) -> str:
        lines = [f"這次掛上會期 {self.assigned} 篇"]
        for s in self.sessions:
            span = (f"{s.session.start_date}～{s.session.end_date}"
                    if s.session.start_date else "無資料")
            lines.append(f"{s.session}（資料涵蓋 {span}）：母體 {s.population} 人、"
                         f"已完成文章 {s.ready_articles} 篇")
        if self.unmatched:
            lines.append(f"對不到任期的講者 {len(self.unmatched)} 位（不計入指標；"
                         "通常是名冊的寫法不同，請到 admin 確認）：")
            for (source, name), count in sorted(self.unmatched.items(),
                                                key=lambda item: (-item[1], item[0])):
                lines.append(f"  {ArticleSource(source).label} {name}：{count} 篇")
        return "\n".join(lines)


def compute_profiles(now: datetime | None = None) -> ProfileReport:
    """先掛會期、更新涵蓋範圍，再逐會期整批重算。冪等：重跑結果相同（computed_at 除外）。"""
    report = ProfileReport(assigned=assign_sessions())
    now = now or timezone.now()
    rosters: dict[str, _Roster] = {}
    for session in Session.objects.order_by("source", "start_date", "id"):
        if session.source not in rosters:
            rosters[session.source] = _Roster.load(session.source)
        rows, summary = _compute_session(session, rosters[session.source], now, report.unmatched)
        _replace(session, rows)
        report.sessions.append(summary)
    logger.info("人物側寫重算完成：%d 個會期", len(report.sessions))
    return report


def _compute_session(session: Session, roster: _Roster, now: datetime,
                     unmatched: Counter) -> tuple[list[ProfileStat], SessionSummary]:
    # 只算已完成的文章：只有它們有頁面可以點回去
    articles = list(Article.objects.filter(session=session, status=ArticleStatus.READY)
                    .order_by().values_list("speaker", "date", "duration_seconds", "brief"))
    population = roster.population(session.start_date, session.end_date)
    summary = SessionSummary(session, len(population) if articles else 0, len(articles))
    if not articles:
        # 會期裡的文章都還沒做完：全員零分不是事實，是還沒有資料
        return [], summary

    # 母體裡沒有任何發言的人也要有一列：投入量算 0——這正是要比較的
    tallies = {person_id: _Tally() for person_id in population}
    for speaker, day, duration, brief in articles:
        people, strangers = _speakers_of(speaker, day, roster)
        for name in strangers:
            unmatched[(session.source, name)] += 1
        heads = len(people) + len(strangers)
        if not heads:
            continue
        # 該篇講者人數：對不到任期的人與議長也是講者，照樣佔一份
        share = Fraction(duration, heads)
        # 聯合質詢的摘要卡是整段的，分不出是誰講的，不算進任何一個人的具體度
        counts_brief = is_solo(speaker) and brief is not None
        for person_id in people:
            tally = tallies.get(person_id)
            if tally is None:
                # 議長、副議長：不在母體
                continue
            tally.speeches += 1
            tally.seconds += share
            if counts_brief:
                tally.add_brief(brief)

    rows = []
    for indicator in INDICATORS:
        rows.extend(_rank(indicator, session, tallies, now))
    return rows, summary


def _speakers_of(speaker: str, day: date, roster: _Roster) -> tuple[list[int], list[str]]:
    """一篇的講者：(對到的人，去重、保留順序, 對不到任期的名字)。

    以人去重：同一個人的兩段任期寫法不同、又同時出現在講者欄位時，仍然只是「一篇有他」
    ——發言次數是「講者包含他的文章數」，證據清單點進去也只有一篇。
    """
    people: dict[int, None] = {}
    strangers: list[str] = []
    for name in speaker_names(speaker):
        person_id = roster.person_for(name, day)
        if person_id is None:
            strangers.append(name)
        else:
            people[person_id] = None
    return list(people), strangers


def _rank(indicator: Indicator, session: Session, tallies: dict[int, _Tally],
          now: datetime) -> list[ProfileStat]:
    """一項指標：每人的值、n，與在同儕（母體中這一項樣本足夠的人）裡的百分位。"""
    measured = {person_id: indicator.measure(tally) for person_id, tally in tallies.items()}
    eligible = {person_id for person_id, (value, n) in measured.items()
                if value is not None and indicator.sample_ok(n)}
    peer_values = sorted(measured[person_id][0] for person_id in eligible)
    peers = len(peer_values)
    rows = []
    for person_id, (value, n) in measured.items():
        ranked = person_id in eligible and peers >= MIN_PEERS
        rows.append(ProfileStat(
            person_id=person_id, session=session, indicator=indicator.key, value=value, n=n,
            percentile=percentile(value, peer_values) if ranked else None,
            peers=peers, computed_at=now))
    return rows


def _replace(session: Session, rows: list[ProfileStat]) -> None:
    """同一個 transaction 裡刪掉該會期的舊列、寫入新列：讀者不會看到算到一半的會期。"""
    with transaction.atomic():
        ProfileStat.objects.filter(session=session).delete()
        ProfileStat.objects.bulk_create(rows)
