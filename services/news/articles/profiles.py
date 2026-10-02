"""人物側寫（投入量、具體度、議題分布）：從既有文章算出每個人每個會期的指標，存進 ProfileStat。

原則照 issue #24：不加總、不排名、只跟同一議會同一會期的人比、樣本不足不給百分位、
每個數字都能點回文章清單——/api/articles 用證據網址的篩選條件查出來的篇數，必須等於
這裡算出來的 n。所以「誰算講者」「什麼算單獨發言」「什麼算有摘要卡」兩邊要用同一套定義。

純程式計算、不打模型。議題分布讀的是每晚分類好的 Topic（topics.classify_topics），而且只認
該來源通過評估的那個分類器（topics.passing_classifiers）——沒有通過的來源完全沒有議題指標。
Pi 每晚匯入、分類之後跑一次（run_scheduler），整批重算、冪等。
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
from django.db.models import Count, Max, Min
from django.utils import timezone

from . import topics
from .members_sync import CHAIR_ROLES, SPEAKER_SEPARATOR, term_number
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
    # 第幾屆（「11」「4」）；空字串是不知道
    term: str = ""
    # 所屬委員會（只有立法院），LYAPI 的原樣字串
    committees: tuple[str, ...] = ()

    def covers(self, day: date) -> bool:
        return (self.start is None or self.start <= day) and (self.end is None or day <= self.end)


class _Roster:
    """一個來源的所有任期，一次載入，在記憶體裡對名字。"""

    def __init__(self, terms: Sequence[_Term]):
        self._terms = list(terms)
        self._by_name: dict[str, list[_Term]] = defaultdict(list)
        # 一個人的所有委員會：同一屆裡換黨會切成兩段任期，委員會要合起來看
        self._committees: dict[int, list[str]] = defaultdict(list)
        for term in self._terms:
            self._by_name[term.name].append(term)
            self._committees[term.person_id].extend(term.committees)
        self._areas: dict[tuple[int, str], frozenset[str]] = {}

    @classmethod
    def load(cls, source: str) -> _Roster:
        # 跟 members_sync.membership_for 同一個排序：多筆重疊時取最晚開始的、同日再取最新建的
        rows = (Membership.objects.filter(source=source).order_by("-start_date", "-id")
                .values_list("person_id", "name", "start_date", "end_date", "role", "term",
                             "committees"))
        return cls([_Term(pid, name, start, end, role in CHAIR_ROLES, term_number(term),
                          tuple(c for c in (committees or []) if isinstance(c, str)))
                    for pid, name, start, end, role, term, committees in rows])

    def person_for(self, name: str, day: date) -> int | None:
        """同來源、同名、任期涵蓋文章日期——跟 members_sync.link_article 同一套規則。"""
        candidates = [t for t in self._by_name.get(name, ()) if t.covers(day)]
        if not candidates:
            return None
        return max(candidates, key=lambda t: t.start or date.min).person_id

    def committee_areas(self, person_id: int, session_name: str) -> frozenset[str]:
        """他在這個會期所屬委員會的職掌領域。一個人一個會期只解析一次（每篇都會問）。"""
        key = (person_id, session_name)
        if key not in self._areas:
            self._areas[key] = topics.committee_areas(self._committees.get(person_id, ()),
                                                      session_name)
        return self._areas[key]

    def population(self, start: date | None, end: date | None,
                   term: str = "") -> tuple[set[int], set[int]]:
        """(母體, 被排除的議長副議長)。母體 = 這一屆、任期與會期涵蓋範圍有重疊的人。

        要看「這一屆」：議會的任期沒有起訖日期（空的就是無限早到現任），只看日期的話，
        換屆之後舊會期的同儕會混進新議員、新會期會混進已經卸任的人。任期或會期不知道
        是哪一屆時才只看日期。

        議長、副議長只主持、不質詢，放進母體只是一堆零，會把其他人的百分位墊高。只看
        這一屆的任期上的職位：下一屆當選議長的人，這一屆仍是一般議員。只有新北的名冊有
        這欄；立法院、臺中沒有，就沒有人被排除。
        """
        serving = [t for t in self._terms if _overlaps(t.start, t.end, start, end)
                   and (not term or not t.term or t.term == term)]
        chairs = {t.person_id for t in serving if t.chair}
        return {t.person_id for t in serving} - chairs, chairs


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
    # 議題分布的基礎文章：具體度的基礎文章裡，有「通過版本」Topic 的。領域代碼 → 篇數
    topic_counts: Counter = field(default_factory=Counter)
    topic_base: int = 0
    # 委員會職掌（立法院）：有委員會資料的基礎文章數、其中主領域在職掌內的篇數
    committee_base: int = 0
    in_committee: int = 0

    def add_brief(self, brief: object) -> None:
        numbers, deadline_asks, sourced = brief_counts(brief)
        self.base += 1
        self.numbers += numbers
        self.deadline_asks += deadline_asks
        self.sourced_numbers += sourced

    def add_topic(self, primary: str, committee_areas: frozenset[str]) -> None:
        """committee_areas 是空的：他在這個會期沒有委員會資料（或不是立法院），不進職掌的分母。"""
        self.topic_base += 1
        self.topic_counts[primary] += 1
        if committee_areas:
            self.committee_base += 1
            self.in_committee += primary in committee_areas


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
TOPIC_BLOCK = "topics"
BLOCK_TITLES = {VOLUME: "投入量", SPECIFICITY: "具體度", TOPIC_BLOCK: "議題分布"}


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
    # 值是整數（次數、個數）：API 給 12 而不是 12.0
    integer: bool = False

    def sample_ok(self, n: int) -> bool:
        return not self.min_sample or n >= MIN_SAMPLE


SPEECHES = Indicator(
    "speeches", VOLUME, "發言次數", "次", "篇",
    # 聯合質詢每人各算一次
    lambda t: (float(t.speeches), t.speeches), integer=True)
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


def _focus(t: _Tally) -> tuple[float | None, int]:
    """最大占比 × 100。分母是 0 時沒有值。"""
    if not t.topic_base:
        return None, 0
    return max(t.topic_counts.values()) / t.topic_base * 100, t.topic_base


def _breadth(t: _Tally) -> tuple[float | None, int]:
    """占比 ≥ 10% 的領域數。用整數比較：剛好 10% 的不會因為浮點數的尾數被算掉。"""
    if not t.topic_base:
        return None, 0
    wide = sum(1 for count in t.topic_counts.values()
               if count * topics.BREADTH_SHARE_DENOMINATOR >= t.topic_base)
    return float(wide), t.topic_base


TOPIC_FOCUS = Indicator(
    "topic_focus", TOPIC_BLOCK, "聚焦度", "%", "篇", _focus, min_sample=True)
TOPIC_BREADTH = Indicator(
    "topic_breadth", TOPIC_BLOCK, "廣度", "個", "篇", _breadth, min_sample=True, integer=True)
COMMITTEE_ALIGNMENT = Indicator(
    "committee_alignment", TOPIC_BLOCK, "委員會職掌內的比例", "%", "篇",
    # 分母只算有委員會資料的：會期對不上、沒有資料的不知道職掌是什麼，不能算成「不在職掌內」
    lambda t: (_ratio(t.in_committee, t.committee_base, 100.0), t.committee_base),
    min_sample=True)

TOPIC_INDICATORS = (TOPIC_FOCUS, TOPIC_BREADTH, COMMITTEE_ALIGNMENT)
INDICATOR_BY_KEY = {i.key: i for i in (*INDICATORS, *TOPIC_INDICATORS)}

# 分布存成 ProfileStat 的「topic:<代碼>」列：value＝篇數、n＝基礎文章數、不給百分位
TOPIC_STAT_PREFIX = "topic:"


def topic_stat_key(code: str) -> str:
    return f"{TOPIC_STAT_PREFIX}{code}"


def topic_indicators_for(source: str) -> list[Indicator]:
    """議題分布區塊的指標。委員會職掌只有立法院：市議員沒有對應的委員會資料。"""
    indicators = [TOPIC_FOCUS, TOPIC_BREADTH]
    if source == ArticleSource.LY:
        indicators.append(COMMITTEE_ALIGNMENT)
    return indicators


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
    # (來源, 講者名字) → 篇數：對得到人、但他不在那個會期的母體裡（例如任期的屆別對不上）
    outside: Counter = field(default_factory=Counter)
    # 來源 → 已完成、但掛不上會期的文章數（會議名稱裡沒有會期）。這些不計入任何指標
    unsessioned: Counter = field(default_factory=Counter)
    # 來源 → 議題分布用的分類器（通過評估的那個）。不在裡面的來源沒有議題指標
    topic_classifiers: dict[str, str] = field(default_factory=dict)
    # 院內紀錄（立法院）：每個同步過紀錄的會期一行（chamber.SessionRecords.summary）
    chamber: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        lines = [f"這次掛上會期 {self.assigned} 篇"]
        for s in self.sessions:
            span = (f"{s.session.start_date}～{s.session.end_date}"
                    if s.session.start_date else "無資料")
            lines.append(f"{s.session}（資料涵蓋 {span}）：母體 {s.population} 人、"
                         f"已完成文章 {s.ready_articles} 篇")
        for counter, heading in (
                (self.unmatched, "對不到任期的講者 {n} 位（不計入指標；通常是名冊的寫法不同，"
                                 "請到 admin 核對任期的名字與起訖）："),
                (self.outside, "對得到人、但不在該會期母體裡的講者 {n} 位（不計入指標；"
                               "通常是任期的屆別或起訖不對）：")):
            if counter:
                lines.append(heading.format(n=len(counter)))
                for (source, name), count in sorted(counter.items(),
                                                    key=lambda item: (-item[1], item[0])):
                    lines.append(f"  {ArticleSource(source).label} {name}：{count} 篇")
        for source, count in sorted(self.unsessioned.items()):
            lines.append(f"{ArticleSource(source).label}：已完成但會議名稱裡沒有會期的文章 "
                         f"{count} 篇（不計入指標）")
        for source in ArticleSource:
            classifier = self.topic_classifiers.get(source.value)
            lines.append(f"議題分布 {source.label}：" + (
                f"用分類器 {classifier}（評估通過）" if classifier
                else "沒有通過的評估，不計算"))
        lines.extend(self.chamber)
        return "\n".join(lines)


def compute_profiles(now: datetime | None = None) -> ProfileReport:
    """先掛會期、更新涵蓋範圍，再逐會期整批重算。冪等：重跑結果相同（computed_at 除外）。"""
    report = ProfileReport(assigned=assign_sessions(),
                           topic_classifiers=topics.passing_classifiers())
    now = now or timezone.now()
    rosters: dict[str, _Roster] = {}
    legislators = None
    for session in Session.objects.order_by("source", "start_date", "id"):
        if session.source not in rosters:
            rosters[session.source] = _Roster.load(session.source)
        rows, summary = _compute_session(session, rosters[session.source], now, report,
                                         report.topic_classifiers.get(session.source))
        if session.source == ArticleSource.LY:
            # 院內紀錄（出席、提案、表決）跟文章的指標在同一次 _replace 裡寫，讀者不會看到算一半的
            from . import chamber  # chamber 用到這裡的 Indicator 與 _rank，在模組層級 import 會循環
            legislators = legislators or chamber.Legislators.load()
            chamber_rows, line = chamber.session_rows(session, legislators, now)
            rows.extend(chamber_rows)
            if line:
                report.chamber.append(line)
        _replace(session, rows)
        report.sessions.append(summary)
    report.unsessioned.update({
        row["source"]: row["n"] for row in (
            Article.objects.filter(status=ArticleStatus.READY, session__isnull=True)
            .order_by().values("source").annotate(n=Count("id")))})
    logger.info("人物側寫重算完成：%d 個會期", len(report.sessions))
    return report


def _compute_session(session: Session, roster: _Roster, now: datetime, report: ProfileReport,
                     classifier: str | None = None) -> tuple[list[ProfileStat], SessionSummary]:
    """classifier：這個來源通過評估的分類器；None 就不算議題分布。"""
    # 只算已完成的文章：只有它們有頁面可以點回去。Topic 一起讀（LEFT JOIN），不必每篇再查
    articles = list(Article.objects.filter(session=session, status=ArticleStatus.READY)
                    .order_by().values_list("speaker", "date", "duration_seconds", "brief",
                                            "topic__primary", "topic__classifier"))
    population, chairs = roster.population(session.start_date, session.end_date, session.term)
    summary = SessionSummary(session, len(population) if articles else 0, len(articles))
    if not articles:
        # 會期裡的文章都還沒做完：全員零分不是事實，是還沒有資料
        return [], summary

    # 母體裡沒有任何發言的人也要有一列：投入量算 0——這正是要比較的
    tallies = {person_id: _Tally() for person_id in population}
    committees = session.source == ArticleSource.LY
    for speaker, day, duration, brief, primary, topic_classifier in articles:
        people, strangers = _speakers_of(speaker, day, roster)
        for name in strangers:
            report.unmatched[(session.source, name)] += 1
        heads = len(people) + len(strangers)
        if not heads:
            continue
        # 該篇講者人數：對不到任期的人與議長也是講者，照樣佔一份
        share = Fraction(duration, heads)
        # 聯合質詢的摘要卡是整段的，分不出是誰講的，不算進任何一個人的具體度
        counts_brief = is_solo(speaker) and brief is not None
        # 議題分布只認通過版本分出來的：換了模型或提示詞，新分的在重新評估通過之前都不算
        counts_topic = (counts_brief and classifier is not None
                        and topic_classifier == classifier and primary in topics.TOPIC_BY_KEY)
        for person_id, name in people.items():
            tally = tallies.get(person_id)
            if tally is None:
                # 議長、副議長本來就不在母體；其他不在母體的人要報出來，不能靜悄悄地少算
                if person_id not in chairs:
                    report.outside[(session.source, name)] += 1
                continue
            tally.speeches += 1
            tally.seconds += share
            if counts_brief:
                tally.add_brief(brief)
            if counts_topic:
                tally.add_topic(primary, roster.committee_areas(person_id, session.name)
                                if committees else frozenset())

    rows = []
    for indicator in INDICATORS:
        rows.extend(_rank(indicator, session, tallies, now))
    if classifier is not None:
        topic_rows = _distribution(session, tallies, now)
        for indicator in topic_indicators_for(session.source):
            topic_rows.extend(_rank(indicator, session, tallies, now))
        for row in topic_rows:
            row.classifier = classifier
        rows.extend(topic_rows)
    return rows, summary


def _distribution(session: Session, tallies: dict[int, _Tally], now: datetime) -> list[ProfileStat]:
    """每個人 12 列「topic:<代碼>」：篇數 0 的也存，API 才分得出「算過、是 0」與「沒算過」。"""
    return [ProfileStat(person_id=person_id, session=session, indicator=topic_stat_key(area.key),
                        value=float(tally.topic_counts[area.key]), n=tally.topic_base,
                        percentile=None, peers=0, computed_at=now)
            for person_id, tally in tallies.items() for area in topics.TOPICS]


def _speakers_of(speaker: str, day: date, roster: _Roster) -> tuple[dict[int, str], list[str]]:
    """一篇的講者：({對到的人: 名字}，以人去重、保留順序, 對不到任期的名字)。

    以人去重：同一個人的兩段任期寫法不同、又同時出現在講者欄位時，仍然只是「一篇有他」
    ——發言次數是「講者包含他的文章數」，證據清單點進去也只有一篇。
    """
    people: dict[int, str] = {}
    strangers: list[str] = []
    for name in speaker_names(speaker):
        person_id = roster.person_for(name, day)
        if person_id is None:
            strangers.append(name)
        else:
            people.setdefault(person_id, name)
    return people, strangers


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


def topic_stats_stale() -> bool:
    """側寫裡的議題列跟現在上線的分類器對不上（重算失敗過、或上線的分類器剛換）。

    有上線分類器的來源：它的議題列裡有別的分類器算的、或一列都沒有（但有會期）；沒有上線分類器
    的來源：還留著議題列。任何一種都要重算，網站上的數字才跟證據篩選一致。
    """
    live = topics.passing_classifiers()
    topic_rows = ProfileStat.objects.exclude(classifier="")
    for source in ArticleSource.values:
        rows = topic_rows.filter(session__source=source)
        classifier = live.get(source)
        if classifier is None:
            if rows.exists():
                return True
        elif rows.exclude(classifier=classifier).exists() or (
                not rows.exists() and Session.objects.filter(source=source).exists()):
            return True
    return False
