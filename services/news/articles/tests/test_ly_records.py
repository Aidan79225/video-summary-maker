"""立法院院內紀錄的同步：解析用存下來的真實 LYAPI 回應（2026-10-03），翻頁、429、失敗不寫半套、
冪等用假的 LYAPI。指標與證據清單在 test_chamber.py。"""
from __future__ import annotations

import io
import json
from collections import Counter
import os
import urllib.error
from datetime import date, datetime
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from articles import ly_records
from articles.ly_records import (LyRecordsSource, RecordsUnavailable, name_key,
                                 parse_bill, parse_meeting, parse_session_name, parse_vote, save,
                                 session_name, vote_date)
from articles.members_sync import LyMemberSource, sync
from articles.models import (Article, LyBill, LyMeeting, LyMeetingKind, LyVote, Membership, Person,
                             Session)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


def _rows(name, key):
    return _load(name)[key]


def _payload(key, rows, total=None, total_page=1, page=1):
    """LYAPI 清單回應的形狀（total、total_page、page 與資料陣列）。"""
    return {"total": len(rows) if total is None else total, "total_page": total_page, "page": page,
            "limit": 100, key: rows}


def _groups_payload(rows, total=None):
    """/bills?agg=會期 的回應形狀（真實的一份在 lyapi_bills_11_by_session.json）。"""
    counts = Counter(row["會期"] for row in rows)
    return {"total": len(rows) if total is None else total, "total_page": len(rows), "page": 1,
            "limit": 1, "bills": rows[:1],
            "aggs": [{"agg": "會期", "agg_fields": ["會期"],
                      "buckets": [{"會期": k, "count": v} for k, v in counts.items()]}]}


def _bill_routes(rows):
    """委員提案：先問分組（agg），再按「會期」一批一批抓。"""
    routes = {("bills", "agg", 1): _groups_payload(rows)}
    for number in {row["會期"] for row in rows}:
        routes[("bills", str(number), 1)] = _payload("bills", [r for r in rows if r["會期"] == number])
    return routes


class FakeLyApi:
    """照網址的路徑與查詢參數回應，記下每一個網址。

    routes：{(路徑, 選擇, 頁碼): 回應}。選擇是會議種類（/meets）、"agg"（分組查詢）、會期（/bills 的
    分批），其他是 None。回應是 dict、或要丟出去的例外。
    """

    def __init__(self, routes):
        self.routes, self.urls = routes, []

    def __call__(self, url):
        self.urls.append(url)
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        path = parts.path.rsplit("/", 1)[-1]
        kind = query.get("會議種類", [None])[0]
        if "agg" in query:
            kind = "agg"
        elif path == "bills" and "會期" in query:
            kind = query["會期"][0]
        page = int(query.get("page", ["1"])[0])
        answer = self.routes.get((path, kind, page))
        if answer is None:
            answer = self.routes.get((path, kind, "*"), _payload(path, []))
        if isinstance(answer, Exception):
            raise answer
        return json.dumps(answer, ensure_ascii=False)

    def queries(self, path):
        return [parse_qs(urlsplit(u).query) for u in self.urls if urlsplit(u).path.endswith(path)]


def _real_routes():
    """每個端點一頁、內容是存下來的真實回應。"""
    return {
        ("meets", "院會", 1): _payload("meets", _rows("lyapi_meets_plenary_11.json", "meets")),
        ("meets", "委員會", 1): _payload("meets", _rows("lyapi_meets_committee_11.json", "meets")),
        ("meets", "聯席會議", 1): _payload("meets", _rows("lyapi_meets_joint_11.json", "meets")),
        **_bill_routes(_rows("lyapi_bills_11.json", "bills")),
        ("votes", None, 1): _payload("votes", _rows("lyapi_votes_11.json", "votes")),
    }


def _source(routes=None, sleep=None):
    fetch = FakeLyApi(_real_routes() if routes is None else routes)
    sleeps = []
    source = LyRecordsSource("https://api.example/v2", term=11, fetch=fetch,
                             sleep=sleep or sleeps.append)
    return source, fetch, sleeps


def _http_error(code, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("https://api.example/v2/x", code, "err", headers, None)


class NameKeyTests(SimpleTestCase):
    def test_spellings_of_one_name_across_endpoints_match(self):
        # 會議資料用空白、表決與名冊用 U+2027、影音系統用全形句點
        keys = {name_key(n) for n in ("伍麗華Saidhai Tahovecahe", "伍麗華Saidhai‧Tahovecahe",
                                      "伍麗華Saidhai．Tahovecahe", " 伍麗華 Saidhai·Tahovecahe ")}
        self.assertEqual(keys, {"伍麗華SaidhaiTahovecahe"})
        self.assertEqual(name_key("鄭天財Sra Kacaw"), name_key("鄭天財Sra．Kacaw"))
        self.assertNotEqual(name_key("王立"), name_key("王立任"))


class SessionNameTests(SimpleTestCase):
    def test_the_session_name_is_the_one_articles_use(self):
        from articles.profiles import parse_session

        self.assertEqual(session_name(11, 5), parse_session("ly", "第11屆第5會期第23次會議").name)
        self.assertEqual(parse_session_name("第11屆第5會期"), (11, 5))
        self.assertIsNone(parse_session_name("第4屆第8次定期會"))


class ParseMeetingTests(SimpleTestCase):
    def test_a_plenary_meeting_over_three_days_is_one_meeting(self):
        rows = _rows("lyapi_meets_plenary_11.json", "meets")
        meeting = parse_meeting(rows[0], LyMeetingKind.PLENARY)
        self.assertEqual((meeting.code, meeting.term, meeting.session_number, meeting.name),
                         ("院會-11-5-23", 11, 5, "第11屆第5會期第23次會議"))
        self.assertEqual(meeting.dates, (date(2026, 8, 21), date(2026, 8, 25), date(2026, 8, 27)))
        self.assertEqual(meeting.date, date(2026, 8, 21))
        self.assertEqual(meeting.units, ("院會",))
        # 每天一份同樣的簽到名單：合起來還是 112 人
        self.assertEqual(len(meeting.attendees), 112)
        self.assertEqual(meeting.url, "https://ppg.ly.gov.tw/ppg/sittings/2026081816/details"
                                      "?meetingDate=115/08/21")

    def test_the_minutes_and_the_sign_in_list_are_the_same_people(self):
        """院會-11-5-22 兩份名單都有、是同一批人，族名寫法不同（空白對 U+2027）：用議事錄的那份，
        同一個人只算一次。"""
        meeting = parse_meeting(_rows("lyapi_meets_plenary_11.json", "meets")[1],
                                LyMeetingKind.PLENARY)
        self.assertEqual(len(meeting.attendees), 112)
        self.assertEqual(sum(1 for n in meeting.attendees if n.startswith("伍麗華")), 1)

    def test_plenary_attendance_is_the_minutes_not_the_padded_daily_list(self):
        """院會-11-5-2（2026-10-03 實測，原樣存成 fixture）：每天的「出席委員」都是 114 人，把議事錄寫著
        請假的 7 位、與 2026-04-22 才到職的許忠信都列了進去；議事錄的出席委員是 106 人。"""
        row = _rows("lyapi_meets_plenary_11_5_2.json", "meets")[0]
        self.assertEqual([len(item["出席委員"]) for item in row["會議資料"]], [114, 114])
        meeting = parse_meeting(row, LyMeetingKind.PLENARY)
        self.assertEqual((meeting.code, meeting.dates), ("院會-11-5-2", (date(2026, 3, 6), date(2026, 3, 10))))
        self.assertEqual(len(meeting.attendees), 106)
        keys = {name_key(n) for n in meeting.attendees}
        for absent in ("羅美玲", "林楚茵", "林宜瑾", "邱議瑩", "沈發惠", "李昆澤", "蔡其昌", "許忠信"):
            self.assertNotIn(absent, keys)
        self.assertEqual(keys, {name_key(n) for n in row["議事錄"]["出席委員"]})

    def test_without_minutes_the_daily_list_is_used_minus_anyone_on_leave(self):
        row = _rows("lyapi_meets_plenary_11_5_2.json", "meets")[0]
        # 還沒有議事錄：只能用每天的名單
        self.assertEqual(len(parse_meeting(dict(row, 議事錄=None), LyMeetingKind.PLENARY).attendees), 114)
        # 議事錄只有請假名單（出席委員是空的）：每天的名單扣掉請假的 7 位
        leave_only = dict(row, 議事錄={"請假委員": row["議事錄"]["請假委員"]})
        meeting = parse_meeting(leave_only, LyMeetingKind.PLENARY)
        self.assertEqual(len(meeting.attendees), 107)
        self.assertNotIn("羅美玲", meeting.attendees)
        # 請假名單的寫法跟出席名單不同也認得（族名的間隔號）
        spaced = dict(row, 議事錄={"請假委員": ["伍麗華Saidhai‧Tahovecahe"]})
        self.assertFalse(any(n.startswith("伍麗華") for n in
                             parse_meeting(spaced, LyMeetingKind.PLENARY).attendees))

    def test_committee_attendance_comes_from_the_minutes(self):
        rows = _rows("lyapi_meets_committee_11.json", "meets")
        meeting = parse_meeting(rows[0], LyMeetingKind.COMMITTEE)
        self.assertEqual((meeting.code, meeting.units), ("委員會-11-4-15-4", ("內政委員會",)))
        # 會議資料的出席委員是空的；議事錄有 13 位（列席委員不算）
        self.assertEqual(len(meeting.attendees), 13)
        self.assertIn("黃建賓", meeting.attendees)
        self.assertNotIn("徐富癸", meeting.attendees)      # 列席

    def test_a_meeting_without_minutes_has_unknown_attendance_not_an_empty_one(self):
        rows = _rows("lyapi_meets_committee_11.json", "meets")
        meeting = parse_meeting(rows[4], LyMeetingKind.COMMITTEE)
        self.assertEqual(meeting.code, "委員會-11-4-35-3")
        self.assertIsNone(meeting.attendees)

    def test_a_joint_meeting_keeps_every_committee(self):
        rows = _rows("lyapi_meets_joint_11.json", "meets")
        meeting = parse_meeting(rows[1], LyMeetingKind.COMMITTEE)
        self.assertEqual(meeting.units, ("財政委員會", "司法及法制委員會"))
        self.assertEqual(len(meeting.attendees), 25)
        self.assertEqual(meeting.session_number, 3)

    def test_broken_rows_are_dropped(self):
        self.assertIsNone(parse_meeting({"會議代碼": "院會-11-5-1", "屆": 11}, LyMeetingKind.PLENARY))
        self.assertIsNone(parse_meeting("junk", LyMeetingKind.PLENARY))

    def test_a_broken_official_link_is_left_out(self):
        row = {"會議代碼": "x", "屆": 11, "會期": 5,
               "會議資料": [{"ppg_url": "https://ppg.ly.gov.tw/ppg/sittings//details?meetingDate=59/01/01"}]}
        self.assertEqual(parse_meeting(row, LyMeetingKind.COMMITTEE).url, "")


class ParseBillTests(SimpleTestCase):
    def test_bills_carry_proposers_cosigners_status_and_link(self):
        bills = [parse_bill(row) for row in _rows("lyapi_bills_11.json", "bills")]
        first = bills[0]
        self.assertEqual((first.bill_no, first.term, first.session_number, first.status),
                         ("202110224730000", 11, 5, "三讀"))
        self.assertEqual(first.proposers, ("李坤城",))
        self.assertEqual(len(first.cosigners), 17)
        self.assertEqual(first.proposed_on, date(2026, 7, 10))
        self.assertEqual(first.url, "https://ppg.ly.gov.tw/ppg/bills/202110224730000/details")

    def test_a_bill_belongs_to_the_session_of_its_first_reading(self):
        """「會期」是最新進度的會期：第 1 會期一讀、第 5 會期撤案的案子，「會期」寫 5。"""
        row = _rows("lyapi_bills_11.json", "bills")[4]
        self.assertEqual((row["會期"], row["會議代碼"]), (5, "院會-11-1-10"))
        bill = parse_bill(row)
        self.assertEqual((bill.bill_no, bill.session_number, bill.status), ("202110031760000", 1, "撤案"))
        # 讀不出一讀的院會、或讀出不存在的會期（實測有「院會-11-9-2」）才退回「會期」
        self.assertEqual(parse_bill(dict(row, 會議代碼="")).session_number, 5)
        self.assertEqual(parse_bill(dict(row, 會議代碼="院會-11-9-2")).session_number, 5)

    def test_a_caucus_bill_has_no_cosigners_field(self):
        caucus_bill = parse_bill(_rows("lyapi_bills_11.json", "bills")[2])
        self.assertEqual(caucus_bill.proposers[0], "台灣民眾黨立法院黨團")
        self.assertEqual(caucus_bill.cosigners, ())


class ParseVoteTests(SimpleTestCase):
    def test_votes_carry_each_members_choice(self):
        vote = parse_vote(_rows("lyapi_votes_11.json", "votes")[0])
        self.assertEqual((vote.code, vote.term, vote.session_number, vote.meeting_code),
                         ("1151901_00002_717", 11, 5, "院會-11-5-4"))
        self.assertEqual((len(vote.yes), len(vote.no), len(vote.abstain)), (59, 50, 0))
        self.assertEqual(len(vote.voters), 109)
        # 一長串全形空白縮成一個空白
        self.assertEqual(vote.topic, "討論事項第二案 台灣民眾黨黨團提議逕付二讀")
        self.assertEqual(vote.voted_at, "中華民國年3月20日 上午11時52分30秒")


class VoteDateTests(SimpleTestCase):
    def test_the_year_in_the_text_wins(self):
        self.assertEqual(vote_date("中華民國113年5月28日 下午3時", []), date(2024, 5, 28))
        self.assertEqual(vote_date("113年5月28日下午3時", []), date(2024, 5, 28))

    def test_without_a_year_the_meeting_day_with_that_month_and_day(self):
        days = [date(2026, 3, 17), date(2026, 3, 20)]
        self.assertEqual(vote_date("中華民國年3月20日 上午11時52分30秒", days), date(2026, 3, 20))

    def test_a_vote_after_midnight_takes_the_meetings_year(self):
        # 院會-11-2-18 開到 1 月 21 日凌晨：月日不是會議的任何一天
        self.assertEqual(vote_date("中華民國年1月21日 上午1時10分04秒", [date(2025, 1, 20)]),
                         date(2025, 1, 21))
        self.assertEqual(vote_date("中華民國年1月2日 上午1時", [date(2025, 12, 31)]), date(2026, 1, 2))

    def test_a_year_that_contradicts_the_meeting_is_a_typo(self):
        """1150601_00002_718（2026-10-03 實測，原樣存成 fixture）：院會-11-4-16 開在 2026-01-02 與
        01-06，表決時間卻寫「中華民國114年」（2025）。照原文的年會掉到整整一年前、不在那場會議裡。"""
        row = _rows("lyapi_votes_11_wrong_year.json", "votes")[0]
        vote = parse_vote(row)
        self.assertEqual((vote.code, vote.meeting_code, vote.voted_at),
                         ("1150601_00002_718", "院會-11-4-16", "中華民國114年1月2日 下午12時39分54秒"))
        days = [date(2026, 1, 2), date(2026, 1, 6)]
        self.assertEqual(vote_date(vote.voted_at, days, vote.code), date(2026, 1, 2))
        # 月日也不是會議的任何一天：跟沒有年的一樣，用會議第一天的年份（比第一天早就是跨年）
        self.assertEqual(vote_date("中華民國114年1月3日", days), date(2026, 1, 3))
        self.assertEqual(vote_date("中華民國114年12月31日", [date(2026, 1, 2)]), date(2026, 12, 31))

    def test_a_written_year_inside_the_meeting_is_kept(self):
        """原文的日期落在會議第一天到最後一天的隔天（過了午夜）之間，就照原文。"""
        days = [date(2026, 1, 2), date(2026, 1, 6)]
        self.assertEqual(vote_date("中華民國115年1月6日 下午3時", days), date(2026, 1, 6))
        self.assertEqual(vote_date("中華民國115年1月7日 上午0時30分", days), date(2026, 1, 7))
        self.assertEqual(vote_date("中華民國114年1月21日 上午1時10分", [date(2025, 1, 20)]),
                         date(2025, 1, 21))
        # 會議的日期不知道，就只能信原文
        self.assertEqual(vote_date("中華民國114年1月2日", []), date(2025, 1, 2))

    def test_without_the_meeting_the_gazette_year_of_the_code(self):
        self.assertEqual(vote_date("中華民國年3月20日", [], "1151901_00002_717"), date(2026, 3, 20))

    def test_nonsense_falls_back_to_the_first_meeting_day(self):
        self.assertEqual(vote_date("", [date(2026, 3, 20)]), date(2026, 3, 20))
        self.assertEqual(vote_date("中華民國115年2月30日", [date(2026, 3, 1)]), date(2026, 3, 1))
        self.assertIsNone(vote_date("", []))


class SourceTests(SimpleTestCase):
    def test_every_kind_of_record_is_fetched_with_only_the_fields_needed(self):
        source, fetch, _ = _source()
        fetched = source.fetch()
        self.assertEqual(len(fetched.meetings), 2 + 5 + 2)
        self.assertEqual({m.kind for m in fetched.meetings[2:]}, {LyMeetingKind.COMMITTEE})
        self.assertEqual((len(fetched.bills), len(fetched.votes)), (5, 2))
        meets = fetch.queries("meets")
        self.assertEqual([q["會議種類"] for q in meets], [["院會"], ["委員會"], ["聯席會議"]])
        self.assertTrue(all(q["屆"] == ["11"] for q in meets))
        self.assertIn("議事錄", meets[0]["output_fields"])
        groups, batch = fetch.queries("bills")
        self.assertEqual((groups["agg"], groups["提案來源"]), (["會期"], ["委員提案"]))
        self.assertEqual((batch["會期"], batch["提案來源"]), (["5"], ["委員提案"]))
        self.assertIn("連署人", batch["output_fields"])

    def test_one_request_per_second(self):
        source, fetch, sleeps = _source()
        source.fetch()
        self.assertEqual(len(fetch.urls), 6)
        self.assertEqual(sleeps, [1.0] * 5)

    def test_every_page_is_fetched(self):
        plenary = _rows("lyapi_meets_plenary_11.json", "meets")
        routes = {("meets", "院會", 1): _payload("meets", plenary[:1], total=2, total_page=2),
                  ("meets", "院會", 2): _payload("meets", plenary[1:], total=2, total_page=2, page=2)}
        source, fetch, _ = _source(routes)
        fetched = source.fetch()
        self.assertEqual([m.code for m in fetched.meetings], ["院會-11-5-23", "院會-11-5-22"])
        self.assertEqual([q["page"] for q in fetch.queries("meets")[:2]], [["1"], ["2"]])

    def test_bills_are_fetched_in_batches_by_their_latest_session(self):
        """一次查詢最多翻到第 10,000 筆：委員提案按「會期」分批。只同步第 5 會期時第 1 批不必抓。"""
        bills = _rows("lyapi_bills_11.json", "bills")
        rows = [dict(bills[4], 會期=1), *bills[:4]]
        routes = _real_routes()
        routes.update(_bill_routes(rows))
        source, fetch, _ = _source(routes)
        self.assertEqual(len(source.fetch().bills), 5)
        self.assertEqual([q.get("會期") for q in fetch.queries("bills")], [None, ["1"], ["5"]])
        source, fetch, _ = _source(routes)
        self.assertEqual(len(source.fetch(session=5).bills), 4)
        self.assertEqual([q.get("會期") for q in fetch.queries("bills")], [None, ["5"]])

    def test_when_the_groups_do_not_add_up_the_bills_come_in_one_go(self):
        """有案子沒有「會期」：分組加起來比總數少，總數還在上限內就整批抓。"""
        bills = _rows("lyapi_bills_11.json", "bills")
        routes = _real_routes()
        routes[("bills", "agg", 1)] = _groups_payload(bills, total=6)
        routes[("bills", None, 1)] = _payload("bills", bills)
        source, fetch, _ = _source(routes)
        self.assertEqual(len(source.fetch().bills), 5)
        self.assertEqual([q.get("會期") for q in fetch.queries("bills")], [None, None])
        # LYAPI 哪天不給分組了也一樣
        routes[("bills", "agg", 1)] = _payload("bills", bills[:1], total=5)
        source, fetch, _ = _source(routes)
        self.assertEqual(len(source.fetch().bills), 5)

    def test_more_than_lyapi_can_page_through_fails_up_front(self):
        """翻到第 10,000 筆之後 LYAPI 回 413：與其翻到一半才失敗，第一頁就說清楚。"""
        routes = _real_routes()
        routes[("votes", None, 1)] = _payload("votes", _rows("lyapi_votes_11.json", "votes"),
                                              total=10_001, total_page=21)
        source, _, _ = _source(routes)
        with self.assertRaisesMessage(RecordsUnavailable, "要分批抓"):
            source.fetch()

    def test_the_real_grouping_response_is_understood(self):
        routes = _real_routes()
        routes[("bills", "agg", 1)] = _load("lyapi_bills_11_by_session.json")
        source, _, _ = _source(routes)
        groups, total = source._groups("bills", "議案編號", {"提案來源": "委員提案"}, "會期")
        self.assertEqual((sum(groups.values()), total), (7402, 7402))
        self.assertEqual(groups[5], 1831)

    def test_a_page_that_went_missing_fails_the_whole_fetch(self):
        """翻頁途中資料有變動：說有 3 筆、翻完只拿到 2 筆，寧可整次失敗。"""
        plenary = _rows("lyapi_meets_plenary_11.json", "meets")
        routes = {("meets", "院會", 1): _payload("meets", plenary, total=3)}
        source, _, _ = _source(routes)
        with self.assertRaisesMessage(RecordsUnavailable, "說有 3 筆"):
            source.fetch()

    def test_429_backs_off_using_retry_after(self):
        routes = _real_routes()
        answers = [_http_error(429, retry_after="5")]

        class Flaky(FakeLyApi):
            def __call__(self, url):
                if "/bills" in url and answers:
                    self.urls.append(url)
                    raise answers.pop(0)
                return super().__call__(url)

        sleeps = []
        source = LyRecordsSource("https://api.example/v2", term=11, fetch=Flaky(routes),
                                 sleep=sleeps.append)
        self.assertEqual(len(source.fetch().bills), 5)
        self.assertIn(5.0, sleeps)

    def test_429_that_never_ends_gives_up(self):
        routes = _real_routes()
        routes[("votes", None, 1)] = _http_error(429)
        source, _, sleeps = _source(routes)
        with self.assertRaises(RecordsUnavailable):
            source.fetch()
        self.assertEqual([s for s in sleeps if s != 1.0], [2.0, 4.0, 8.0])

    def test_any_failure_is_unavailable(self):
        for error in (OSError("connection refused"), _http_error(500)):
            routes = _real_routes()
            routes[("meets", "聯席會議", 1)] = error
            source, _, _ = _source(routes)
            with self.assertRaises(RecordsUnavailable):
                source.fetch()

    def test_a_reply_that_is_not_json_is_unavailable(self):
        source = LyRecordsSource("https://api.example/v2", term=11, fetch=lambda url: "<html>",
                                 sleep=lambda s: None)
        with self.assertRaises(RecordsUnavailable):
            source.fetch()

    def test_one_session_asks_for_that_session_and_filters_votes_here(self):
        """/votes 不支援會期篩選：整屆抓回來，在這裡只留那個會期。"""
        source, fetch, _ = _source()
        fetched = source.fetch(session=5)
        self.assertEqual(fetch.queries("meets")[0]["會期"], ["5"])
        # 議案的「會期」是最新進度的會期：抓第 5 批以後的，再照一讀的會期篩
        self.assertEqual([q.get("會期") for q in fetch.queries("bills")], [None, ["5"]])
        self.assertNotIn("會期", fetch.queries("votes")[0])
        self.assertEqual(len(fetched.bills), 4)
        self.assertEqual([v.code for v in fetched.votes], ["1151901_00002_717"])
        # 存下來的委員會會議是第 4 會期的：不收
        self.assertEqual({m.session_number for m in fetched.meetings}, {5})

    def test_impossible_sessions_and_other_terms_are_not_kept(self):
        votes = _rows("lyapi_votes_11.json", "votes")
        odd = dict(votes[0], 表決代碼="x-1", session_period=11)
        older = dict(votes[1], 表決代碼="x-2", 屆=10)
        routes = _real_routes()
        routes[("votes", None, 1)] = _payload("votes", [*votes, odd, older])
        source, _, _ = _source(routes)
        fetched = source.fetch()
        self.assertEqual(len(fetched.votes), 2)
        self.assertEqual(fetched.skipped, ["表決 x-1：會期 11"])

    def test_the_real_http_call_says_who_we_are(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"{}"
        with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
            ly_records._http_get("https://api.example/v2/meets")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), ly_records.USER_AGENT)


NOW = timezone.make_aware(datetime(2026, 10, 4, 3, 40))


class SaveTests(TestCase):
    def _fetched(self, session=None):
        source, _, _ = _source()
        return source.fetch(session=session)

    def test_records_and_their_sessions_are_written(self):
        report = save(self._fetched(), now=NOW)
        self.assertEqual((LyMeeting.objects.count(), LyBill.objects.count(), LyVote.objects.count()),
                         (9, 5, 2))
        # 由小到大建，id 才跟會期的先後一致；第 1 會期來自一讀在第 1 會期的那件議案
        self.assertEqual(report.sessions_created, [f"第11屆第{n}會期" for n in range(1, 6)])
        session = Session.objects.get(name="第11屆第5會期")
        # 涵蓋範圍仍以文章為準：沒有文章就留空
        self.assertEqual((session.source, session.term, session.start_date, session.end_date),
                         ("ly", "11", None, None))
        meeting = LyMeeting.objects.get(code="院會-11-5-23")
        self.assertEqual((meeting.kind, meeting.date, meeting.dates[-1], len(meeting.attendees)),
                         ("plenary", date(2026, 8, 21), "2026-08-27", 112))
        self.assertIsNone(LyMeeting.objects.get(code="委員會-11-4-35-3").attendees)
        self.assertEqual(LyBill.objects.get(bill_no="202110201670000").cosigners, [])

    def test_an_existing_session_is_reused(self):
        Article.objects.create(ivod_id="1", slug="a-1", source="ly", title="t", speaker="甲",
                               meeting="第11屆第5會期第23次會議", date=date(2026, 8, 21),
                               ivod_url="https://example.invalid/1")
        from articles.profiles import assign_sessions

        assign_sessions()
        save(self._fetched(), now=NOW)
        self.assertEqual(Session.objects.filter(name="第11屆第5會期").count(), 1)
        # 起訖照舊是文章的範圍，不被紀錄撐開
        self.assertEqual(Session.objects.get(name="第11屆第5會期").end_date, date(2026, 8, 21))

    def test_saving_again_updates_in_place(self):
        save(self._fetched(), now=NOW)
        routes = _real_routes()
        bills = _rows("lyapi_bills_11.json", "bills")
        routes.update(_bill_routes([dict(bills[0], 議案狀態="審查完畢"), *bills[1:]]))
        source, _, _ = _source(routes)
        save(source.fetch(), now=NOW)
        self.assertEqual((LyMeeting.objects.count(), LyBill.objects.count(), LyVote.objects.count()),
                         (9, 5, 2))
        self.assertEqual(LyBill.objects.get(bill_no="202110224730000").status, "審查完畢")

    def test_records_lyapi_no_longer_has_are_removed(self):
        """LYAPI 改了代碼（或刪掉重複）的那一筆不能留著：會被算兩次。"""
        save(self._fetched(), now=NOW)
        routes = _real_routes()
        votes = _rows("lyapi_votes_11.json", "votes")
        routes[("votes", None, 1)] = _payload("votes", [dict(votes[0], 表決代碼="新代碼"), votes[1]])
        source, _, _ = _source(routes)
        report = save(source.fetch(), now=NOW)
        self.assertEqual(sorted(LyVote.objects.values_list("code", flat=True)),
                         ["1141921_00002_591", "新代碼"])
        self.assertEqual(report.removed, Counter({"表決": 1}))
        self.assertIn("表決 1 筆", str(report))

    def test_only_the_synced_session_is_reconciled(self):
        save(self._fetched(), now=NOW)
        source, _, _ = _source()
        report = save(source.fetch(session=5), now=NOW)
        # 第 2～4 會期的會議、表決，第 1 會期的議案都還在
        self.assertEqual((LyMeeting.objects.count(), LyBill.objects.count(), LyVote.objects.count()),
                         (9, 5, 2))
        self.assertFalse(report.removed)

    def test_an_empty_answer_does_not_wipe_the_table(self):
        save(self._fetched(), now=NOW)
        routes = _real_routes()
        routes[("votes", None, 1)] = _payload("votes", [])
        source, _, _ = _source(routes)
        report = save(source.fetch(), now=NOW)
        self.assertEqual(LyVote.objects.count(), 2)
        self.assertEqual(report.kept_because_empty, ["表決"])
        self.assertIn("舊紀錄先留著", str(report))

    def test_members_on_leave_are_not_counted_as_attending(self):
        """院會-11-5-2 每天的名單把請假的人也列進去：照它算，請假的羅美玲會是 100% 出席。"""
        from articles.models import ProfileStat
        from articles.profiles import compute_profiles

        for name, start in (("范雲", date(2024, 2, 1)), ("羅美玲", date(2024, 2, 1)),
                            ("許忠信", date(2026, 4, 22))):
            Membership.objects.create(person=Person.objects.create(name=name), source="ly", name=name,
                                      term="第11屆", start_date=start)
        routes = {("meets", "院會", 1): _payload("meets", _rows("lyapi_meets_plenary_11_5_2.json",
                                                                "meets"))}
        source, _, _ = _source(routes)
        save(source.fetch(), now=NOW)
        compute_profiles()
        stats = {s.person.name: (s.value, s.n) for s in ProfileStat.objects.filter(
            indicator="plenary_attendance").select_related("person")}
        # 許忠信 2026-04-22 才到職：這一場不是他在任期間的會議，不在母體裡
        self.assertEqual(stats, {"范雲": (100.0, 1), "羅美玲": (0.0, 1)})

    def test_vote_dates_come_from_the_meeting(self):
        """院會-11-2-18 那一筆的原文沒有年：靠會議的日期補上。"""
        LyMeeting.objects.create(code="院會-11-2-18", kind="plenary", term=11, session_number=2,
                                 date=date(2025, 1, 20), dates=["2025-01-20"], name="m", synced_at=NOW)
        save(self._fetched(session=2), now=NOW)
        self.assertEqual(LyVote.objects.get(code="1141921_00002_591").date, date(2025, 1, 21))

    def test_a_vote_with_a_mistyped_year_is_dated_by_its_meeting(self):
        """表決時間寫錯年份（114 年，會議在 2026 年）：存成會議那天。存錯的話，這個會期的紀錄期間
        會被撐到前一年，早就離職的人也會跑進母體。"""
        LyMeeting.objects.create(code="院會-11-4-16", kind="plenary", term=11, session_number=4,
                                 date=date(2026, 1, 2), dates=["2026-01-02", "2026-01-06"], name="m",
                                 synced_at=NOW)
        routes = {("votes", None, 1): _payload("votes", _rows("lyapi_votes_11_wrong_year.json", "votes"))}
        source, _, _ = _source(routes)
        save(source.fetch(session=4), now=NOW)
        self.assertEqual(LyVote.objects.get(code="1150601_00002_718").date, date(2026, 1, 2))

    def test_a_vote_without_any_ballot_is_kept_and_reported(self):
        """第 11 屆有一筆表決的投票委員是空的（2026-10-03 實測，原樣存成 fixture）：照樣存，報告點出來；
        指標那邊不算進任何人的分母（test_chamber）。"""
        routes = _real_routes()
        votes = _rows("lyapi_votes_11.json", "votes") + _rows("lyapi_votes_11_without_ballots.json",
                                                              "votes")
        routes[("votes", None, 1)] = _payload("votes", votes)
        source, _, _ = _source(routes)
        report = save(source.fetch(), now=NOW)
        vote = LyVote.objects.get(code="1139101_00002_466")
        self.assertEqual((vote.yes, vote.no, vote.abstain, vote.voters, vote.date),
                         ([], [], [], [], date(2024, 11, 8)))
        self.assertEqual(report.without_ballots, 1)
        self.assertIn("沒有任何人的票的表決 1 次", str(report))
        self.assertNotIn("沒有任何人的票", str(save(self._fetched(), now=NOW)))

    def test_names_that_match_no_term_are_reported_but_caucus_proposers_are_not(self):
        person = Person.objects.create(name="李坤城")
        Membership.objects.create(person=person, source="ly", name="李坤城")
        report = save(self._fetched(), now=NOW)
        self.assertNotIn("李坤城", report.unknown_names)
        self.assertIn("張雅琳", report.unknown_names)
        self.assertNotIn("台灣民眾黨立法院黨團", report.unknown_names)
        self.assertIn("對不到立法院任期的姓名", str(report))


class CommandTests(TestCase):
    def test_the_command_syncs_and_prints_the_report(self):
        source, _, _ = _source()
        out = io.StringIO()
        with mock.patch.object(ly_records, "LyRecordsSource", return_value=source) as cls:
            call_command("sync_ly_records", "--term", "11", stdout=out)
        cls.assert_called_once_with(term=11)
        self.assertIn("院會 2 場", out.getvalue())
        self.assertEqual(LyBill.objects.count(), 5)

    def test_one_failed_page_writes_nothing(self):
        routes = _real_routes()
        routes[("votes", None, 1)] = OSError("connection reset")
        source, _, _ = _source(routes)
        with mock.patch.object(ly_records, "LyRecordsSource", return_value=source):
            with self.assertRaisesMessage(CommandError, "資料庫沒有改動"):
                call_command("sync_ly_records", stdout=io.StringIO())
        self.assertEqual((LyMeeting.objects.count(), LyBill.objects.count(), Session.objects.count()),
                         (0, 0, 0))


class LegislatorCaucusTests(TestCase):
    def test_the_caucus_comes_from_the_roster_and_independents_can_join_one(self):
        fetch = lambda url: json.dumps(_load("lyapi_legislators_11_independents.json"))  # noqa: E731
        records = LyMemberSource("https://api.example/v2", term=11, fetch=fetch).fetch()
        by_name = {r.name: r for r in records}
        self.assertEqual((by_name["高金素梅"].party, by_name["高金素梅"].caucus), ("無黨籍", "中國國民黨"))
        sync(records)
        self.assertEqual(Membership.objects.get(name="陳超明").caucus, "中國國民黨")

    def test_no_caucus_is_an_empty_string(self):
        """第 10 屆的名冊把沒有參加黨團寫成「0無」。"""
        row = {"委員姓名": "某甲", "黨籍": "無黨籍", "黨團": "0無", "屆": 10}
        fetch = lambda url: json.dumps({"legislators": [row], "total_page": 1})  # noqa: E731
        record = LyMemberSource("https://api.example/v2", term=10, fetch=fetch).fetch()[0]
        self.assertEqual(record.caucus, "")


class WeeklyJobTests(SimpleTestCase):
    """每週日：名單 → 院內紀錄 → 重算；任何一步失敗都不能冒出排程（APScheduler 會把工作移除）。"""

    LOGGER = "articles.management.commands.run_scheduler"

    def _run(self, fail=()):
        from articles.management.commands import run_scheduler

        calls = []

        def fake(name, **kwargs):
            calls.append(name)
            if name in fail:
                raise RuntimeError(f"{name} 壞了")

        with mock.patch.object(run_scheduler, "call_command", side_effect=fake):
            run_scheduler.weekly()
        return calls

    STEPS = ["sync_members", "sync_ly_records", "compute_profiles"]

    def test_records_follow_the_roster_and_the_profiles_are_recomputed(self):
        self.assertEqual(self._run(), self.STEPS)

    def test_a_failing_step_does_not_stop_the_rest(self):
        for step in self.STEPS:
            with self.assertLogs(self.LOGGER, "ERROR"):
                self.assertEqual(self._run(fail={step}), self.STEPS)

    def test_the_sunday_job_is_the_weekly_one(self):
        from articles.management.commands import run_scheduler

        scheduler = mock.MagicMock()
        with mock.patch("apscheduler.schedulers.blocking.BlockingScheduler",
                        return_value=scheduler), \
                mock.patch.object(run_scheduler, "sources_missing_members", return_value=[]), \
                mock.patch.object(run_scheduler.signal, "signal"):
            call_command("run_scheduler", "--backfill-days", "0", stdout=io.StringIO())
        jobs = {call.kwargs["id"]: call.args[0] for call in scheduler.add_job.call_args_list}
        self.assertIs(jobs["sync_members"], run_scheduler.weekly)

