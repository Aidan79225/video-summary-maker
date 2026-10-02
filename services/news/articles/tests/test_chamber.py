"""人物側寫的院內紀錄（出席、提案、表決）：指標的公式、在任期間、黨團多數，以及 API 的區塊與
紀錄清單（筆數要等於指標的 n 或值）。同步本身在 test_ly_records.py。"""
from __future__ import annotations

import io
import itertools
from datetime import date, datetime
from urllib.parse import parse_qsl, urlsplit

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from articles import chamber
from articles.models import (Article, ArticleStatus, LyBill, LyMeeting, LyVote, Membership, Person,
                             ProfileStat, Session)
from articles.profiles import MIN_SAMPLE, compute_profiles

NOW = timezone.make_aware(datetime(2026, 10, 4, 3, 40))
S5 = "第11屆第5會期"
KMT, DPP, TPP = "中國國民黨", "民主進步黨", "台灣民眾黨"

_ids = itertools.count(1)


def _legislator(name, caucus=KMT, start=None, end=None, committees=(), person=None, term="第11屆"):
    person = person or Person.objects.create(name=name)
    return Membership.objects.create(person=person, source="ly", name=name, term=term, caucus=caucus,
                                     start_date=start, end_date=end, committees=list(committees))


def _meeting(day, attendees, kind="plenary", units=("院會",), session=5):
    n = next(_ids)
    return LyMeeting.objects.create(
        code=f"{kind}-{n}", kind=kind, term=11, session_number=session, date=date.fromisoformat(day),
        dates=[day], name=f"第11屆第{session}會期第{n}次會議", units=list(units),
        attendees=None if attendees is None else list(attendees),
        url=f"https://ppg.ly.gov.tw/ppg/sittings/{n}/details", synced_at=NOW)


def _bill(proposers, cosigners=(), status="交付審查", day="2026-03-02", session=5):
    n = next(_ids)
    return LyBill.objects.create(
        bill_no=f"B{n}", term=11, session_number=session, name=f"議案{n}", status=status,
        proposers=list(proposers), cosigners=list(cosigners),
        url=f"https://ppg.ly.gov.tw/ppg/bills/B{n}/details",
        proposed_on=date.fromisoformat(day) if day else None, synced_at=NOW)


def _vote(day, yes=(), no=(), abstain=(), session=5, meeting_code=""):
    n = next(_ids)
    return LyVote.objects.create(
        code=f"V{n}", term=11, session_number=session, meeting_code=meeting_code, voted_at="",
        date=date.fromisoformat(day), topic=f"表決{n}", yes=list(yes), no=list(no),
        abstain=list(abstain), voters=[*yes, *no, *abstain], synced_at=NOW)


def _session(name=S5):
    return Session.objects.get_or_create(source="ly", name=name, defaults={"term": "11"})[0]


def _stats(indicator, name=S5):
    return {s.person.name: s for s in ProfileStat.objects.filter(indicator=indicator, session__name=name)
            .select_related("person")}


def _values(indicator, name=S5):
    return {who: (s.value, s.n) for who, s in _stats(indicator, name).items()}


class AttendanceTests(TestCase):
    def setUp(self):
        _session()

    def test_plenary_attendance_only_counts_meetings_while_he_served(self):
        _legislator("甲")
        _legislator("乙", start=date(2026, 4, 1))                 # 中途遞補
        _legislator("丙", end=date(2026, 4, 30))                  # 中途離職
        _meeting("2026-03-01", ["甲", "丙"])
        _meeting("2026-04-10", ["甲", "乙"])
        _meeting("2026-05-01", ["乙"])
        compute_profiles()
        self.assertEqual(_values("plenary_attendance"),
                         {"甲": (2 / 3 * 100, 3), "乙": (100.0, 2), "丙": (50.0, 2)})

    def test_a_meeting_without_attendance_records_counts_for_nobody(self):
        """委員會的議事錄常晚好幾週：還沒有名單的那場是「不知道」，不是全員缺席。"""
        _legislator("甲")
        _meeting("2026-03-01", ["甲"])
        _meeting("2026-03-05", None)
        compute_profiles()
        self.assertEqual(_values("plenary_attendance"), {"甲": (100.0, 1)})

    def test_committee_meetings_are_those_of_his_committees_that_session_and_joint_ones(self):
        _legislator("甲", committees=["第11屆第5會期：財政委員會", "第11屆第4會期：內政委員會"])
        _meeting("2026-03-02", ["甲"], kind="committee", units=["財政委員會"])
        _meeting("2026-03-03", ["乙"], kind="committee", units=["經濟委員會", "財政委員會"])  # 聯席
        _meeting("2026-03-04", ["甲"], kind="committee", units=["內政委員會"])   # 上一個會期的委員會
        _meeting("2026-03-05", ["甲"], kind="committee", units=["經濟委員會"])
        _meeting("2026-03-06", None, kind="committee", units=["財政委員會"])     # 還沒有議事錄
        compute_profiles()
        self.assertEqual(_values("committee_attendance"), {"甲": (50.0, 2)})

    def test_without_committee_data_there_is_no_committee_attendance(self):
        _legislator("甲")
        _meeting("2026-03-02", ["甲"], kind="committee", units=["財政委員會"])
        compute_profiles()
        self.assertEqual(_values("committee_attendance"), {"甲": (None, 0)})

    def test_spellings_differ_between_the_roster_and_the_sign_in_list(self):
        _legislator("伍麗華Saidhai‧Tahovecahe", caucus=DPP)
        _meeting("2026-03-01", ["伍麗華Saidhai Tahovecahe"])
        compute_profiles()
        self.assertEqual(_values("plenary_attendance"), {"伍麗華Saidhai‧Tahovecahe": (100.0, 1)})


class BillTests(TestCase):
    def setUp(self):
        _session()
        _legislator("甲")
        _legislator("乙")

    def test_proposed_cosigned_and_passed(self):
        _bill(["甲"], ["乙"], status="三讀")
        _bill(["甲", "乙"], status="審查完畢(三讀)")
        _bill(["甲"], ["乙"], status="審查完畢")
        _bill(["台灣民眾黨立法院黨團", "乙"])                    # 黨團提案：黨團本身不算給誰
        _bill(["甲"], session=4)                                 # 別的會期
        compute_profiles()
        self.assertEqual(_values("bills_proposed"), {"甲": (3.0, 3), "乙": (2.0, 2)})
        self.assertEqual(_values("bills_cosigned"), {"甲": (0.0, 0), "乙": (2.0, 2)})
        self.assertEqual(_values("bills_passed"), {"甲": (2.0, 2), "乙": (1.0, 1)})

    def test_counts_have_no_minimum_sample(self):
        for name in ("丙", "丁", "戊"):
            _legislator(name)
        _bill(["甲"])
        compute_profiles()
        stats = _stats("bills_proposed")
        # 一件也給百分位：計數沒有分母，只有同儕不足才不比
        self.assertEqual(stats["甲"].percentile, 90.0)
        self.assertEqual(stats["乙"].percentile, 40.0)
        self.assertEqual(ProfileStat.objects.get(indicator="bills_proposed",
                                                 person__name="甲").peers, 5)


class VoteTests(TestCase):
    """國民黨團甲乙丙、民進黨團丁戊、沒有黨團的庚。"""

    def setUp(self):
        _session()
        for name in ("甲", "乙"):
            _legislator(name)
        _legislator("丙", end=date(2026, 4, 30))
        for name in ("丁", "戊"):
            _legislator(name, caucus=DPP)
        _legislator("庚", caucus="")
        # 1：國民黨團多數贊成，丙跨黨；民進黨團多數反對
        _vote("2026-03-10", yes=["甲", "乙", "庚"], no=["丙", "丁", "戊"])
        # 2：國民黨團一比一並列，沒有多數，不算進一致率
        _vote("2026-03-11", yes=["甲"], no=["乙", "丁", "戊"])
        # 3：丙沒投；民進黨團贊成一、棄權一，並列
        _vote("2026-03-12", yes=["甲", "乙", "丁"], abstain=["戊"])
        # 4：丙已經離職，不在他的分母裡
        _vote("2026-05-02", yes=["甲", "乙", "丁", "戊"])
        compute_profiles()

    def test_vote_participation_only_counts_votes_while_he_served(self):
        self.assertEqual(_values("vote_participation"),
                         {"甲": (100.0, 4), "乙": (100.0, 4), "丙": (1 / 3 * 100, 3),
                          "丁": (100.0, 4), "戊": (100.0, 4), "庚": (25.0, 4)})

    def test_agreement_with_the_caucus_majority_leaves_ties_out(self):
        self.assertEqual(_values("caucus_agreement"),
                         {"甲": (100.0, 3), "乙": (100.0, 3), "丙": (0.0, 1),
                          "丁": (100.0, 3), "戊": (100.0, 3), "庚": (None, 0)})
        self.assertEqual(_values("caucus_defections"),
                         {"甲": (0.0, 0), "乙": (0.0, 0), "丙": (1.0, 1),
                          "丁": (0.0, 0), "戊": (0.0, 0), "庚": (None, 0)})

    def test_the_caucus_is_the_one_he_belonged_to_that_day(self):
        """己 4 月起從國民黨團轉到民眾黨團：5 月的表決算民眾黨團的多數。"""
        person = Person.objects.create(name="己")
        _legislator("己", end=date(2026, 3, 31), person=person)
        _legislator("己", caucus=TPP, start=date(2026, 4, 1), person=person)
        _legislator("辛", caucus=TPP)
        _vote("2026-05-03", yes=["甲", "乙"], no=["己", "辛"])
        compute_profiles()
        self.assertEqual(_values("caucus_defections")["己"], (0.0, 0))
        self.assertEqual(_values("caucus_agreement")["己"], (100.0, 1))

    def test_too_small_a_sample_gets_no_percentile(self):
        stats = _stats("caucus_agreement")
        self.assertLess(stats["甲"].n, MIN_SAMPLE)
        self.assertIsNone(stats["甲"].percentile)


class PopulationTests(TestCase):
    def test_peers_are_those_serving_during_the_sessions_records(self):
        _session()
        _legislator("甲")
        _legislator("前屆", term="第10屆", end=date(2024, 1, 31))
        _legislator("早走", end=date(2025, 12, 31))
        _legislator("後到", start=date(2026, 9, 1))
        _meeting("2026-03-01", ["甲"])
        _vote("2026-05-01", yes=["甲"])
        compute_profiles()
        self.assertEqual(set(_stats("plenary_attendance")), {"甲"})

    def test_a_session_without_records_has_no_chamber_rows(self):
        _legislator("甲")
        _session()
        Membership.objects.create(person=Person.objects.create(name="乙"), source="tccc", name="乙")
        Session.objects.create(source="tccc", name="第4屆第8次定期會", term="4")
        compute_profiles()
        self.assertFalse(ProfileStat.objects.filter(indicator__in=chamber.INDICATOR_KEYS).exists())

    def test_a_session_known_only_from_records_gets_rows_and_a_report_line(self):
        _session("第11屆第4會期")
        _legislator("甲")
        _meeting("2025-10-01", ["甲"], session=4)
        out = io.StringIO()
        call_command("compute_profiles", stdout=out)
        self.assertEqual(_values("plenary_attendance", "第11屆第4會期"), {"甲": (100.0, 1)})
        self.assertIn("第11屆第4會期 院內紀錄：院會 1 場", out.getvalue())

    def test_recomputing_gives_the_same_rows(self):
        _session()
        for name in ("甲", "乙", "丙", "丁", "戊"):
            _legislator(name)
        for day in ("2026-03-01", "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"):
            _meeting(day, ["甲", "乙", "丙"])
            _vote(day, yes=["甲", "乙"], no=["丙"])
        compute_profiles(now=NOW)
        first = sorted(ProfileStat.objects.values_list("person_id", "indicator", "value", "n",
                                                       "percentile", "peers"))
        compute_profiles(now=NOW)
        again = sorted(ProfileStat.objects.values_list("person_id", "indicator", "value", "n",
                                                       "percentile", "peers"))
        self.assertEqual(first, again)
        self.assertEqual(_stats("plenary_attendance")["甲"].percentile, 70.0)

    def test_the_query_count_does_not_grow_with_the_number_of_records(self):
        """紀錄跟任期都一次讀完、在記憶體裡對名字（SD 卡上的 SQLite 不能逐筆查）。"""
        _session()
        for name in ("甲", "乙", "丙"):
            _legislator(name)

        def queries(count):
            LyMeeting.objects.all().delete()
            LyVote.objects.all().delete()
            LyBill.objects.all().delete()
            for i in range(count):
                day = f"2026-03-{i % 28 + 1:02d}"
                _meeting(day, ["甲", "乙"])
                _vote(day, yes=["甲"], no=["乙", "丙"])
                _bill(["甲"], ["乙"], day=day)
            with CaptureQueriesContext(connection) as ctx:
                compute_profiles()
            return len(ctx.captured_queries)

        queries(1)
        self.assertEqual(queries(5), queries(40))


def _records(client, evidence_url):
    """照網站的做法跟著證據網址走：/records/<id>?session=&kind= 換成 API 的紀錄清單。"""
    parts = urlsplit(evidence_url)
    assert parts.path.startswith("/records/"), evidence_url
    person_id = parts.path.removeprefix("/records/")
    return client.get(f"/api/people/{person_id}/records", dict(parse_qsl(parts.query)))


class ChamberApiTests(TestCase):
    """第 5 會期：國民黨團甲乙丙、民進黨團丁戊、沒有黨團的庚；丙 4 月底離職、己 4 月遞補。"""

    def setUp(self):
        self.s5 = _session()
        committees = ["第11屆第5會期：財政委員會"]
        self.m = {name: _legislator(name, committees=committees) for name in ("甲", "乙")}
        self.m["丙"] = _legislator("丙", end=date(2026, 4, 30), committees=committees)
        for name in ("丁", "戊"):
            self.m[name] = _legislator(name, caucus=DPP, committees=["第11屆第5會期：經濟委員會"])
        self.m["己"] = _legislator("己", caucus=DPP, start=date(2026, 5, 1), committees=committees)
        self.m["庚"] = _legislator("庚", caucus="")
        everyone = list(self.m)
        for i, day in enumerate(("2026-03-03", "2026-03-10", "2026-04-07", "2026-05-05",
                                 "2026-05-12", "2026-05-19")):
            plenary = _meeting(day, [n for n in everyone if n != ("乙" if i % 2 else "丁")])
            _vote(day, yes=["甲", "乙", "庚"], no=["丙", "丁", "戊", "己"],
                  meeting_code=plenary.code)
            _vote(day, yes=["甲", "丁"], no=["乙", "戊"], meeting_code=plenary.code)
            _meeting(day, ["甲", "丙"], kind="committee", units=["財政委員會"])
            _meeting(day, ["丁"], kind="committee", units=["經濟委員會", "財政委員會"])
        _meeting("2026-05-20", None, kind="committee", units=["財政委員會"])
        _bill(["甲"], ["乙", "丙"], status="三讀", day="2026-03-04")
        _bill(["甲", "丁"], ["戊"], status="審查完畢", day="2026-05-06")
        _bill(["台灣民眾黨立法院黨團"], day="2026-05-07")
        compute_profiles()

    def _profile(self, name, **params):
        return self.client.get(f"/api/people/{self.m[name].person_id}/profile", params).json()

    def _chamber(self, name, **params):
        blocks = {b["key"]: b for b in self._profile(name, **params)["blocks"]}
        return blocks.get("chamber")

    def test_the_block_has_the_documented_shape(self):
        res = self._profile("甲")
        # 這個會期還沒有任何文章：只給院內紀錄，投入量不是 0、是沒有資料
        self.assertEqual([b["key"] for b in res["blocks"]], ["chamber"])
        block = self._chamber("甲")
        self.assertEqual(block["title"], "院內紀錄")
        self.assertEqual([i["key"] for i in block["indicators"]],
                         ["plenary_attendance", "committee_attendance", "bills_proposed",
                          "bills_cosigned", "bills_passed", "vote_participation",
                          "caucus_agreement", "caucus_defections"])
        got = {i["key"]: i for i in block["indicators"]}
        self.assertEqual((got["plenary_attendance"]["value"], got["plenary_attendance"]["n"],
                          got["plenary_attendance"]["unit"], got["plenary_attendance"]["n_unit"]),
                         (100.0, 6, "%", "場"))
        self.assertEqual((got["bills_proposed"]["value"], got["bills_proposed"]["unit"]), (2, "件"))
        self.assertIsInstance(got["bills_proposed"]["value"], int)
        self.assertEqual(got["bills_passed"]["value"], 1)
        self.assertEqual(got["plenary_attendance"]["evidence_url"],
                         f"/records/{self.m['甲'].person_id}?session={self.s5.id}&kind=plenary")
        self.assertEqual(got["caucus_agreement"]["evidence_url"],
                         f"/records/{self.m['甲'].person_id}?session={self.s5.id}&kind=caucus_votes")
        self.assertEqual({i["reason"] for i in block["indicators"]}, {""})

    def test_articles_and_records_sit_side_by_side(self):
        Article.objects.create(ivod_id="1", slug="a-1", source="ly", title="t", speaker="甲",
                               meeting="第11屆第5會期第3次會議", date=date(2026, 3, 10),
                               ivod_url="https://example.invalid/1", status=ArticleStatus.READY)
        compute_profiles()
        self.assertEqual([b["key"] for b in self._profile("甲")["blocks"]],
                         ["volume", "specificity", "chamber"])

    def test_a_session_known_only_from_records_sorts_by_its_records(self):
        """只有院內紀錄的會期沒有起訖（起訖只看文章）：用紀錄的期間排新舊，剛開議的才不會排在最後。"""
        Article.objects.create(ivod_id="1", slug="a-1", source="ly", title="t", speaker="甲",
                               meeting="第11屆第4會期第8次會議", date=date(2025, 10, 1),
                               ivod_url="https://example.invalid/1", status=ArticleStatus.READY)
        _session("第11屆第6會期")
        _bill(["甲"], day="2026-09-20", session=6)
        compute_profiles()
        res = self._profile("甲")
        self.assertEqual([s["name"] for s in res["sessions"]],
                         ["第11屆第6會期", S5, "第11屆第4會期"])
        # 預設仍是他有發言的最近一個會期；從沒發言的人是最新的會期
        self.assertEqual(res["session"]["name"], "第11屆第4會期")
        self.assertEqual(self._profile("乙")["session"]["name"], "第11屆第6會期")

    def test_without_a_caucus_the_reason_says_so(self):
        got = {i["key"]: i for i in self._chamber("庚")["indicators"]}
        for key in ("caucus_agreement", "caucus_defections"):
            self.assertEqual((got[key]["value"], got[key]["n"], got[key]["reason"]),
                             (None, 0, "no_caucus"))
        self.assertEqual(got["vote_participation"]["reason"], "")

    def test_every_count_can_be_clicked_back_to_exactly_that_many_records(self):
        """證據一致性：每個指標的紀錄清單筆數等於它的 n（分母）或值（計數）。"""
        checked = 0
        for name in self.m:
            for indicator in self._chamber(name)["indicators"]:
                res = _records(self.client, indicator["evidence_url"])
                self.assertEqual(res.status_code, 200, (name, indicator["key"]))
                body = res.json()
                kind = chamber.KIND_BY_KEY[body["kind"]]
                self.assertEqual(kind.indicator, indicator["key"])
                expected = indicator["n"] if kind.counts == "n" else (indicator["value"] or 0)
                self.assertEqual(body["count"], expected, (name, indicator["key"]))
                self.assertEqual(len(body["items"]), body["count"])
                checked += 1
        self.assertEqual(checked, 7 * 8)

    def test_meeting_records_say_whether_he_attended(self):
        body = _records(self.client, self._chamber("乙")["indicators"][0]["evidence_url"]).json()
        self.assertEqual((body["person"]["name"], body["session"]["name"], body["label"]),
                         ("乙", S5, "院會"))
        self.assertEqual([i["attended"] for i in body["items"]], [False, True] * 3)
        item = body["items"][0]
        self.assertEqual(item["date"], "2026-05-19")
        self.assertTrue(item["url"].startswith("https://ppg.ly.gov.tw/ppg/sittings/"))
        self.assertEqual(item["id"], item["meeting_code"])

    def test_vote_records_carry_his_vote_and_his_caucus_majority(self):
        person = self.m["丙"].person_id
        res = self.client.get(f"/api/people/{person}/records",
                              {"session": self.s5.id, "kind": "defections"}).json()
        self.assertEqual(res["caucus"], KMT)
        # 丙 4 月底離職：在任期間 3 天 × 2 次表決；第 1 種他投反對、黨團贊成
        self.assertEqual(res["count"], 3)
        self.assertEqual({(i["vote"], i["caucus_majority"]) for i in res["items"]}, {("反對", "贊成")})
        self.assertTrue(all(i["url"].startswith("https://ppg.ly.gov.tw/") for i in res["items"]))
        votes = self.client.get(f"/api/people/{person}/records",
                                {"session": self.s5.id, "kind": "votes"}).json()
        self.assertEqual(votes["count"], 6)
        # 第 2 種：他沒投、國民黨團一比一並列
        self.assertIn((None, None), {(i["vote"], i["caucus_majority"]) for i in votes["items"]})

    def test_bill_records_link_to_the_official_page(self):
        res = self.client.get(f"/api/people/{self.m['甲'].person_id}/records",
                              {"session": self.s5.id, "kind": "proposed"}).json()
        self.assertEqual([i["status"] for i in res["items"]], ["審查完畢", "三讀"])
        self.assertEqual(res["items"][0]["proposers"], ["甲", "丁"])
        self.assertTrue(res["items"][0]["url"].startswith("https://ppg.ly.gov.tw/ppg/bills/"))

    def test_missing_people_sessions_and_kinds(self):
        person = self.m["甲"].person_id
        url = f"/api/people/{person}/records"
        self.assertEqual(self.client.get("/api/people/999999/records",
                                         {"session": self.s5.id, "kind": "votes"}).status_code, 404)
        self.assertEqual(self.client.get(url, {"session": 999999, "kind": "votes"}).status_code, 404)
        council = Session.objects.create(source="tccc", name="第4屆第8次定期會", term="4")
        self.assertEqual(self.client.get(url, {"session": council.id, "kind": "votes"}).status_code,
                         404)
        # 有紀錄的會期，但他不在那個會期（還沒同步紀錄的第 4 會期）
        s4 = _session("第11屆第4會期")
        self.assertEqual(self.client.get(url, {"session": s4.id, "kind": "votes"}).status_code, 404)
        self.assertEqual(self.client.get(url, {"session": self.s5.id, "kind": "speeches"}).status_code,
                         422)
        self.assertEqual(self.client.get(url, {"kind": "votes"}).status_code, 422)
