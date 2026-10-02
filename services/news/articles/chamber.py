"""人物側寫第四、五步：立委的院內紀錄（出席、提案、表決）。

資料是 ly_records 每週從 LYAPI 同步的結構化紀錄，這裡只用程式計數、不打模型。八個指標（區塊 `chamber`
「院內紀錄」，只有立法院）都以「該會期、他在任期間」為準：任期是 LYAPI 名冊的到職日、離職日
（sync_members 存進 Membership）。不在任的那幾場會議、那幾次表決不算進他的分母——中途遞補、中途
離職的人才不會被算成缺席。

百分位、最小樣本、同儕的規則跟第 1 步一樣（profiles._rank）；同儕是該會期的立委母體：任期跟這個會期
的紀錄期間（會議、表決、提案的最早到最晚）有重疊的人。

每個數字都能點回紀錄清單（/api/people/{id}/records）：清單跟指標用同一個 SessionRecords 算，筆數
必然等於指標的 n（或值）。

不在這裡的：提案與質詢一致率（議案名稱要另一份標註集，見設計文件）。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from django.db.models import Max, Min

from . import profiles, topics
from .ly_records import name_key, parse_session_name
from .members_sync import term_number
from .models import (ArticleSource, LyBill, LyMeeting, LyMeetingKind, LyVote, Membership, ProfileStat,
                     Session)

CHAMBER = "chamber"
TITLE = "院內紀錄"

YES, NO, ABSTAIN = "贊成", "反對", "棄權"
# 議案狀態含這兩個字就算三讀（「三讀」「審查完畢(三讀)」）
PASSED_MARK = "三讀"
# 沒有參加黨團：一致率與跨黨投票沒有值，頁面寫「沒有參加黨團」
NO_CAUCUS = "no_caucus"


# --- 指標 ---


@dataclass
class _Tally:
    """一個人在一個會期的院內紀錄計數。"""
    plenary: int = 0
    plenary_attended: int = 0
    committee: int = 0
    committee_attended: int = 0
    proposed: int = 0
    cosigned: int = 0
    passed: int = 0
    votes: int = 0
    voted: int = 0
    has_caucus: bool = False
    # 他有投票、而且他的黨團那次有多數的表決
    caucus_votes: int = 0
    agreed: int = 0


def _percent(part: int, whole: int) -> float | None:
    return part / whole * 100 if whole else None


def _caucus_agreement(t: _Tally) -> tuple[float | None, int]:
    return (_percent(t.agreed, t.caucus_votes), t.caucus_votes) if t.has_caucus else (None, 0)


def _caucus_defections(t: _Tally) -> tuple[float | None, int]:
    """沒有黨團是 null（不是 0）：API 靠這個分辨「沒有參加黨團」與「從不跨黨」。"""
    if not t.has_caucus:
        return None, 0
    defections = t.caucus_votes - t.agreed
    return float(defections), defections


PLENARY_ATTENDANCE = profiles.Indicator(
    "plenary_attendance", CHAMBER, "院會出席率", "%", "場",
    lambda t: (_percent(t.plenary_attended, t.plenary), t.plenary), min_sample=True)
COMMITTEE_ATTENDANCE = profiles.Indicator(
    "committee_attendance", CHAMBER, "委員會出席率", "%", "場",
    lambda t: (_percent(t.committee_attended, t.committee), t.committee), min_sample=True)
BILLS_PROPOSED = profiles.Indicator(
    "bills_proposed", CHAMBER, "主提案數", "件", "件",
    lambda t: (float(t.proposed), t.proposed), integer=True)
BILLS_COSIGNED = profiles.Indicator(
    "bills_cosigned", CHAMBER, "連署數", "件", "件",
    lambda t: (float(t.cosigned), t.cosigned), integer=True)
BILLS_PASSED = profiles.Indicator(
    "bills_passed", CHAMBER, "三讀數", "件", "件",
    lambda t: (float(t.passed), t.passed), integer=True)
VOTE_PARTICIPATION = profiles.Indicator(
    "vote_participation", CHAMBER, "投票出席率", "%", "次",
    lambda t: (_percent(t.voted, t.votes), t.votes), min_sample=True)
CAUCUS_AGREEMENT = profiles.Indicator(
    "caucus_agreement", CHAMBER, "與所屬黨團一致率", "%", "次", _caucus_agreement, min_sample=True)
CAUCUS_DEFECTIONS = profiles.Indicator(
    "caucus_defections", CHAMBER, "跨黨投票數", "次", "次", _caucus_defections, integer=True)

INDICATORS = (PLENARY_ATTENDANCE, COMMITTEE_ATTENDANCE, BILLS_PROPOSED, BILLS_COSIGNED, BILLS_PASSED,
              VOTE_PARTICIPATION, CAUCUS_AGREEMENT, CAUCUS_DEFECTIONS)
INDICATOR_KEYS = tuple(i.key for i in INDICATORS)
# 沒有黨團時一起說明原因的兩項
CAUCUS_KEYS = (CAUCUS_AGREEMENT.key, CAUCUS_DEFECTIONS.key)


@dataclass(frozen=True)
class Kind:
    """紀錄清單的一類：網址的 kind、頁面的標題、對應的指標，與清單筆數要等於指標的哪個數。"""
    key: str
    label: str
    indicator: str
    # "n"：筆數等於指標的 n（分母）；"value"：等於值（計數）
    counts: str


KINDS = (
    Kind("plenary", "院會", PLENARY_ATTENDANCE.key, "n"),
    Kind("committee", "委員會會議", COMMITTEE_ATTENDANCE.key, "n"),
    Kind("proposed", "主提案", BILLS_PROPOSED.key, "value"),
    Kind("cosigned", "連署", BILLS_COSIGNED.key, "value"),
    Kind("passed", "主提案中三讀的", BILLS_PASSED.key, "value"),
    Kind("votes", "記名表決", VOTE_PARTICIPATION.key, "n"),
    # 一致率的分母：他有投票、黨團也有多數的表決。設計只列了七類，但一致率的 n 跟投票出席率的 n
    # 不一樣，沒有這一類就點不回「n 筆」
    Kind("caucus_votes", "他有投票、黨團有多數的表決", CAUCUS_AGREEMENT.key, "n"),
    Kind("defections", "跨黨投票", CAUCUS_DEFECTIONS.key, "value"),
)
KIND_BY_KEY = {kind.key: kind for kind in KINDS}
KIND_KEYS = tuple(kind.key for kind in KINDS)
KIND_OF_INDICATOR = {kind.indicator: kind.key for kind in KINDS}


def evidence_url(person_id: int, session_id: int, indicator_key: str) -> str:
    """網站的紀錄清單頁（相對路徑）。"""
    return f"/records/{person_id}?session={session_id}&kind={KIND_OF_INDICATOR[indicator_key]}"


# --- 立委的任期 ---


@dataclass(frozen=True)
class _Seat:
    """一段立法院任期。"""
    person_id: int
    key: str
    start: date | None
    end: date | None
    # 第幾屆（「11」）；空字串是不知道
    term: str
    caucus: str
    committees: tuple[str, ...]

    def covers(self, day: date) -> bool:
        return (self.start is None or self.start <= day) and (self.end is None or day <= self.end)

    def overlaps(self, first: date | None, last: date | None) -> bool:
        if first is None or last is None:
            return True
        return (self.start is None or self.start <= last) and (self.end is None or self.end >= first)


def _latest(seats: Iterable[_Seat]) -> _Seat | None:
    """多段重疊時取最晚開始的（同 members_sync.membership_for）。"""
    return max(seats, key=lambda s: s.start or date.min, default=None)


class Legislators:
    """立法院的所有任期，一次載入，在記憶體裡對名字（同 profiles._Roster：SD 卡上的 SQLite 不逐筆查）。"""

    def __init__(self, seats: Iterable[_Seat]):
        self.seats = list(seats)

    @classmethod
    def load(cls) -> Legislators:
        rows = (Membership.objects.filter(source=ArticleSource.LY).order_by("-start_date", "-id")
                .values_list("person_id", "name", "start_date", "end_date", "term", "caucus",
                             "committees"))
        return cls(_Seat(pid, name_key(name), start, end, term_number(term), caucus or "",
                         tuple(c for c in (committees or []) if isinstance(c, str)))
                   for pid, name, start, end, term, caucus, committees in rows)

    def for_term(self, term: str) -> _TermRoster:
        """這一屆的任期（不知道屆別的也算，同 profiles._Roster.population）。"""
        return _TermRoster([s for s in self.seats if not s.term or s.term == term])


class _TermRoster:
    def __init__(self, seats: Sequence[_Seat]):
        self._by_key: dict[str, list[_Seat]] = defaultdict(list)
        self._by_person: dict[int, list[_Seat]] = defaultdict(list)
        for seat in seats:
            self._by_key[seat.key].append(seat)
            self._by_person[seat.person_id].append(seat)

    def person_for(self, name: str, day: date | None) -> int | None:
        """紀錄上的名字 → 那天坐在那個位子上的人。沒有日期（少數議案）就不看任期。"""
        seats = self._by_key.get(name_key(name), ())
        seat = _latest(s for s in seats if day is None or s.covers(day))
        return seat.person_id if seat else None

    def serving(self, person_id: int, day: date | None) -> bool:
        """他那天在不在任。日期不知道就當在任（只有 LYAPI 資料不全時才會發生）。"""
        return day is None or any(s.covers(day) for s in self._by_person.get(person_id, ()))

    def caucus(self, person_id: int, day: date | None) -> str:
        """他那天所屬的黨團（同一會期換過黨團的話，看那一天的那段任期）。"""
        seats = self._by_person.get(person_id, ())
        seat = _latest(s for s in seats if day is None or s.covers(day))
        return seat.caucus if seat else ""

    def has_caucus(self, person_id: int, first: date | None, last: date | None) -> bool:
        return any(s.caucus for s in self._by_person.get(person_id, ()) if s.overlaps(first, last))

    def caucus_name(self, person_id: int, first: date | None, last: date | None) -> str:
        seat = _latest(s for s in self._by_person.get(person_id, ())
                       if s.caucus and s.overlaps(first, last))
        return seat.caucus if seat else ""

    def committees(self, person_id: int, session_name: str) -> frozenset[str]:
        """他在這個會期所屬的委員會（LYAPI 名冊的「第11屆第5會期：財政委員會」）。換黨切成兩段任期
        的人，委員會合起來看（同 profiles._Roster.committee_areas）。"""
        names = set()
        for seat in self._by_person.get(person_id, ()):
            for entry in seat.committees:
                parsed = topics.parse_committee(entry)
                if parsed and parsed[0] == session_name:
                    names.add(parsed[1])
        return frozenset(names)

    def population(self, first: date | None, last: date | None) -> set[int]:
        return {pid for pid, seats in self._by_person.items()
                if any(s.overlaps(first, last) for s in seats)}


# --- 一個會期的紀錄 ---


@dataclass(frozen=True)
class MeetingItem:
    meeting: LyMeeting
    attended: bool


@dataclass(frozen=True)
class VoteItem:
    vote: LyVote
    # 他的票；沒投是 None
    choice: str | None
    # 他那天的黨團；空字串是沒有
    caucus: str
    # 他黨團那次的多數；並列、沒有黨團、或黨團沒有人投是 None
    majority: str | None


def _with_majority(items: Iterable[VoteItem]) -> list[VoteItem]:
    """一致率的分母：他有投票、而且他的黨團那次有多數（並列與沒有黨團的不算）。"""
    return [item for item in items if item.choice and item.majority]


def _against_majority(items: Iterable[VoteItem]) -> list[VoteItem]:
    """跨黨投票：他的票跟黨團多數不同。"""
    return [item for item in items if item.choice != item.majority]


def _majority(counts: Counter) -> str | None:
    """票數最多的選項；並列就沒有多數。"""
    ranked = counts.most_common(2)
    if not ranked or (len(ranked) == 2 and ranked[0][1] == ranked[1][1]):
        return None
    return ranked[0][0]


class SessionRecords:
    """一個會期的院內紀錄，與每個人的清單。指標（tally）與證據清單（evidence）都從這裡來。"""

    def __init__(self, session: Session, legislators: Legislators):
        self.session = session
        numbers = parse_session_name(session.name) if session.source == ArticleSource.LY else None
        self.meetings: list[LyMeeting] = []
        self.bills: list[LyBill] = []
        self.votes: list[LyVote] = []
        if numbers is not None:
            term, number = numbers
            self.meetings = list(LyMeeting.objects.filter(term=term, session_number=number))
            self.bills = list(LyBill.objects.filter(term=term, session_number=number))
            self.votes = list(LyVote.objects.filter(term=term, session_number=number))
        # 屆別從會期名稱拿（跟紀錄的篩選同一個來源），不靠 Session.term 這個另外存的欄位
        self.roster = legislators.for_term(str(numbers[0]) if numbers else session.term)
        days = ([m.date for m in self.meetings] + [v.date for v in self.votes]
                + [b.proposed_on for b in self.bills])
        days = [d for d in days if d]
        # 這個會期的紀錄期間：母體是任期跟它有重疊的人
        self.first, self.last = (min(days), max(days)) if days else (None, None)
        self._index()

    @property
    def empty(self) -> bool:
        return not (self.meetings or self.bills or self.votes)

    def _index(self) -> None:
        """名字一次對成人：每場會議的出席者、每件議案的提案人與連署人、每次表決每個人的票。"""
        self._meeting_index = {m.code: m for m in self.meetings}
        self._attended = {m.code: set(self._people(m.attendees, m.date))
                          for m in self.meetings if m.attendees is not None}
        self._proposed: dict[int, list[LyBill]] = defaultdict(list)
        self._cosigned: dict[int, list[LyBill]] = defaultdict(list)
        for bill in self.bills:
            for pid in self._people(bill.proposers, bill.proposed_on):
                self._proposed[pid].append(bill)
            for pid in self._people(bill.cosigners, bill.proposed_on):
                self._cosigned[pid].append(bill)
        self._choices: dict[str, dict[int, str]] = {}
        self._majorities: dict[str, dict[str, str | None]] = {}
        for vote in self.votes:
            choices: dict[int, str] = {}
            for option, names in ((YES, vote.yes), (NO, vote.no), (ABSTAIN, vote.abstain)):
                for pid in self._people(names, vote.date):
                    choices.setdefault(pid, option)
            by_caucus: dict[str, Counter] = defaultdict(Counter)
            for pid, option in choices.items():
                caucus = self.roster.caucus(pid, vote.date)
                if caucus:
                    by_caucus[caucus][option] += 1
            self._choices[vote.code] = choices
            self._majorities[vote.code] = {c: _majority(n) for c, n in by_caucus.items()}

    def _people(self, names: Iterable[str] | None, day: date | None) -> list[int]:
        found = (self.roster.person_for(name, day) for name in (names or ()))
        return list(dict.fromkeys(pid for pid in found if pid is not None))

    @property
    def population(self) -> set[int]:
        return self.roster.population(self.first, self.last)

    # --- 每個人的清單 ---

    def plenary(self, person_id: int) -> list[MeetingItem]:
        """在任期間、有出席紀錄的院會。"""
        return self._meetings(person_id, LyMeetingKind.PLENARY, lambda m: True)

    def committee(self, person_id: int) -> list[MeetingItem]:
        """在任期間、有出席紀錄、單位裡有他那個會期所屬委員會的會議（聯席會議只要有一個就算）。"""
        mine = self.roster.committees(person_id, self.session.name)
        return self._meetings(person_id, LyMeetingKind.COMMITTEE,
                              lambda m: bool(mine.intersection(m.units or ())))

    def _meetings(self, person_id: int, kind: str, wanted) -> list[MeetingItem]:
        return [MeetingItem(m, person_id in self._attended[m.code]) for m in self.meetings
                if m.kind == kind and m.code in self._attended and wanted(m)
                and self.roster.serving(person_id, m.date)]

    def proposed(self, person_id: int) -> list[LyBill]:
        return list(self._proposed.get(person_id, ()))

    def cosigned(self, person_id: int) -> list[LyBill]:
        return list(self._cosigned.get(person_id, ()))

    def passed(self, person_id: int) -> list[LyBill]:
        return [b for b in self.proposed(person_id) if PASSED_MARK in b.status]

    def all_votes(self, person_id: int) -> list[VoteItem]:
        """在任期間的記名表決，附他的票與他黨團的多數。"""
        items = []
        for vote in self.votes:
            if not self.roster.serving(person_id, vote.date):
                continue
            caucus = self.roster.caucus(person_id, vote.date)
            majority = self._majorities[vote.code].get(caucus) if caucus else None
            items.append(VoteItem(vote, self._choices[vote.code].get(person_id), caucus, majority))
        return items

    def caucus_votes(self, person_id: int) -> list[VoteItem]:
        return _with_majority(self.all_votes(person_id))

    def defections(self, person_id: int) -> list[VoteItem]:
        return _against_majority(self.caucus_votes(person_id))

    def has_caucus(self, person_id: int) -> bool:
        return self.roster.has_caucus(person_id, self.first, self.last)

    def caucus_name(self, person_id: int) -> str:
        return self.roster.caucus_name(person_id, self.first, self.last)

    def tally(self, person_id: int) -> _Tally:
        plenary, committee = self.plenary(person_id), self.committee(person_id)
        votes = self.all_votes(person_id)
        caucus_votes = _with_majority(votes)
        return _Tally(
            plenary=len(plenary), plenary_attended=sum(i.attended for i in plenary),
            committee=len(committee), committee_attended=sum(i.attended for i in committee),
            proposed=len(self.proposed(person_id)), cosigned=len(self.cosigned(person_id)),
            passed=len(self.passed(person_id)),
            votes=len(votes), voted=sum(1 for item in votes if item.choice),
            has_caucus=self.has_caucus(person_id), caucus_votes=len(caucus_votes),
            agreed=len(caucus_votes) - len(_against_majority(caucus_votes)))

    # --- 證據清單 ---

    def evidence(self, person_id: int, kind: str) -> list[dict]:
        """紀錄清單的每一筆（RecordOut 的形狀），新的在前。筆數就是指標的 n 或值。"""
        listings = {
            "plenary": (self.plenary, self._meeting_out),
            "committee": (self.committee, self._meeting_out),
            "proposed": (self.proposed, self._bill_out),
            "cosigned": (self.cosigned, self._bill_out),
            "passed": (self.passed, self._bill_out),
            "votes": (self.all_votes, self._vote_out),
            "caucus_votes": (self.caucus_votes, self._vote_out),
            "defections": (self.defections, self._vote_out),
        }
        if kind not in listings:
            raise ValueError(f"沒有這一類紀錄：{kind}")
        listing, out = listings[kind]
        items = [out(item) for item in listing(person_id)]
        items.sort(key=lambda row: (row["date"] or date.min, row["id"]), reverse=True)
        return items

    @staticmethod
    def _meeting_out(item: MeetingItem) -> dict:
        m = item.meeting
        return {"id": m.code, "date": m.date, "title": m.name, "url": m.url, "meeting_code": m.code,
                "attended": item.attended}

    @staticmethod
    def _bill_out(bill: LyBill) -> dict:
        return {"id": bill.bill_no, "date": bill.proposed_on, "title": bill.name, "url": bill.url,
                "status": bill.status, "proposers": list(bill.proposers or [])}

    def _vote_out(self, item: VoteItem) -> dict:
        v = item.vote
        meeting = self._meeting_index.get(v.meeting_code)
        return {"id": v.code, "date": v.date, "title": v.topic,
                # 表決本身沒有議事網的頁面：給那場院會的會議頁
                "url": meeting.url if meeting else "", "meeting_code": v.meeting_code,
                "vote": item.choice, "caucus_majority": item.majority}

    def summary(self) -> str:
        plenary = [m for m in self.meetings if m.kind == LyMeetingKind.PLENARY]
        committee = [m for m in self.meetings if m.kind == LyMeetingKind.COMMITTEE]
        known = sum(1 for m in committee if m.attendees is not None)
        return (f"{self.session.name} 院內紀錄：院會 {len(plenary)} 場、委員會 {len(committee)} 場"
                f"（有出席紀錄 {known} 場）、委員提案 {len(self.bills)} 件、記名表決 {len(self.votes)} 次；"
                f"母體 {len(self.population)} 人")


def session_rows(session: Session, legislators: Legislators,
                 now: datetime) -> tuple[list[ProfileStat], str]:
    """一個會期的院內紀錄指標列（每人八列）與報告的一行。沒有同步過紀錄的會期什麼都不給——
    API 靠「有沒有這幾列」決定要不要給院內紀錄區塊。"""
    if session.source != ArticleSource.LY:
        return [], ""
    records = SessionRecords(session, legislators)
    if records.empty:
        return [], ""
    tallies = {pid: records.tally(pid) for pid in records.population}
    rows: list[ProfileStat] = []
    for indicator in INDICATORS:
        rows.extend(profiles._rank(indicator, session, tallies, now))
    return rows, records.summary()


def record_spans(sessions: Iterable[Session]) -> dict[int, tuple[date, date]]:
    """還沒有文章的立法院會期 → 院內紀錄的期間（最早、最晚），給 API 排會期的新舊用。

    Session 的起訖是文章的涵蓋範圍（設計：沒有文章就留空），只有紀錄的會期排序時會被當成最舊的，
    剛開議的會期就會排在所有舊會期後面。每種紀錄一個彙總查詢；會期都有文章時一個查詢都不打。
    """
    wanted: dict[tuple[int, int], int] = {}
    for session in sessions:
        numbers = parse_session_name(session.name)
        if session.source == ArticleSource.LY and session.end_date is None and numbers:
            wanted[numbers] = session.id
    if not wanted:
        return {}
    spans: dict[tuple[int, int], tuple[date, date]] = {}
    for model, field in ((LyMeeting, "date"), (LyVote, "date"), (LyBill, "proposed_on")):
        rows = (model.objects.filter(term__in={t for t, _ in wanted},
                                     session_number__in={n for _, n in wanted})
                .exclude(**{f"{field}__isnull": True}).order_by()
                .values("term", "session_number").annotate(first=Min(field), last=Max(field)))
        for row in rows:
            key = (row["term"], row["session_number"])
            if key in wanted:
                first, last = spans.get(key, (row["first"], row["last"]))
                spans[key] = (min(first, row["first"]), max(last, row["last"]))
    return {wanted[key]: span for key, span in spans.items()}
