"""人物側寫的院內紀錄（出席、提案、表決）：指標的公式、在任期間、黨團多數，以及 API 的區塊與
紀錄清單（筆數要等於指標的 n 或值）。同步本身在 test_ly_records.py。"""
from __future__ import annotations

import io
import itertools
import re
import unittest
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qsl, urlsplit

from django.core.management import call_command
from django.db import OperationalError, connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from articles import chamber
from articles.models import (Article, ArticleStatus, BillTopic, BillTopicEvaluation, LyBill, LyMeeting,
                             LyVote, Membership, Person, ProfileStat, Session, Topic, TopicEvaluation)
from articles.profiles import MIN_SAMPLE, compute_profiles

NOW = timezone.make_aware(datetime(2026, 10, 4, 3, 40))
S5 = "第11屆第5會期"
KMT, DPP, TPP = "中國國民黨", "民主進步黨", "台灣民眾黨"

_ids = itertools.count(1)


def _legislator(name, caucus=KMT, start=None, end=None, committees=(), person=None, term="第11屆"):
    person = person or Person.objects.create(name=name)
    return Membership.objects.create(person=person, source="ly", name=name, term=term, caucus=caucus,
                                     start_date=start, end_date=end, committees=list(committees))


def _meeting(day, attendees, kind="plenary", units=("院會",), session=5, later_days=()):
    """later_days：開好幾天的會議的其他天（院會多半是週五、下週二）。"""
    n = next(_ids)
    return LyMeeting.objects.create(
        code=f"{kind}-{n}", kind=kind, term=11, session_number=session, date=date.fromisoformat(day),
        dates=[day, *later_days], name=f"第11屆第{session}會期第{n}次會議", units=list(units),
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

    def test_a_meeting_over_several_days_counts_if_he_served_on_any_of_them(self):
        """院會開週五與下週二：週一到職的遞補委員簽了週二的到，那一場是他在任期間的會議、他有出席；
        週六離職的人週五還在，那一場也算他的。只看第一天的話，遞補的人那一場整個不見了。"""
        _legislator("甲")
        _legislator("乙", start=date(2026, 3, 9))                 # 週一到職
        _legislator("丙", end=date(2026, 3, 7))                   # 週六離職
        _meeting("2026-03-06", ["甲", "丙", "乙"], later_days=["2026-03-10"])
        _meeting("2026-03-13", ["甲"], later_days=["2026-03-17"])
        compute_profiles()
        self.assertEqual(_values("plenary_attendance"),
                         {"甲": (100.0, 2), "乙": (50.0, 2), "丙": (100.0, 1)})

    def test_damaged_meeting_days_fall_back_to_the_first_day(self):
        _legislator("甲")
        meeting = _meeting("2026-03-06", ["甲"])
        LyMeeting.objects.filter(pk=meeting.pk).update(dates=["不是日期"])
        compute_profiles()
        self.assertEqual(_values("plenary_attendance"), {"甲": (100.0, 1)})

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

    def test_a_vote_without_any_ballot_counts_for_nobody(self):
        """一張記名的票都沒有的表決（LYAPI 第 11 屆有一筆）是「不知道」，不是全院都沒投。"""
        _vote("2026-03-13")
        compute_profiles()
        self.assertEqual(_values("vote_participation")["甲"], (100.0, 4))
        self.assertEqual(_values("caucus_agreement")["甲"], (100.0, 3))

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

    def test_an_old_proposal_does_not_pull_a_departed_member_into_the_session(self):
        """休會期間提、第 5 會期才一讀的案子：提案日期不算進會期的期間，早就離職的人不是這個會期的同儕。"""
        _session()
        _legislator("甲")
        _legislator("早走", end=date(2026, 1, 31))
        _meeting("2026-03-01", ["甲"])
        _bill(["早走"], ["甲"], day="2025-12-20")
        compute_profiles()
        self.assertEqual(_values("bills_cosigned"), {"甲": (1.0, 1)})

    def test_a_session_with_only_proposals_uses_their_dates(self):
        _session("第11屆第6會期")
        _legislator("甲")
        _legislator("早走", end=date(2026, 1, 31))
        _bill(["甲"], day="2026-09-20", session=6)
        compute_profiles()
        self.assertEqual(_values("bills_proposed", "第11屆第6會期"), {"甲": (1.0, 1)})

    def test_damaged_records_are_skipped_not_fatal(self):
        _session()
        _legislator("甲")
        _meeting("2026-03-01", ["甲"])
        broken = _meeting("2026-03-02", ["甲"])
        LyMeeting.objects.filter(pk=broken.pk).update(attendees="甲")     # 不是清單
        LyVote.objects.filter(pk=_vote("2026-03-03", yes=["甲"]).pk).update(no="甲", abstain=[1, "甲"])
        compute_profiles()
        self.assertEqual(_values("plenary_attendance"), {"甲": (100.0, 1)})
        self.assertEqual(_values("vote_participation"), {"甲": (100.0, 1)})

    def test_a_failure_in_the_records_does_not_stop_the_recompute(self):
        """院內紀錄算不出來時，那個會期先不給院內紀錄，其他會期（包括市議會的）照算。"""
        _session()
        _legislator("甲")
        _meeting("2026-03-01", ["甲"])
        Membership.objects.create(person=Person.objects.create(name="乙"), source="tccc", name="乙")
        Article.objects.create(ivod_id="tccc-1", slug="t-1", source="tccc", title="t", speaker="乙",
                               meeting="第4屆第8次定期會", date=date(2026, 9, 1),
                               ivod_url="https://example.invalid/1", status=ArticleStatus.READY)
        with mock.patch.object(chamber.SessionRecords, "tally", side_effect=RuntimeError("壞了")), \
                self.assertLogs("articles.chamber", "ERROR"):
            report = compute_profiles()
        self.assertFalse(ProfileStat.objects.filter(indicator__in=chamber.INDICATOR_KEYS).exists())
        self.assertTrue(ProfileStat.objects.filter(person__name="乙", indicator="speeches").exists())
        self.assertIn("計算失敗", str(report))

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
        # 一張票都沒有的表決：不在任何人的清單、也不在任何人的分母裡
        _vote("2026-03-10")
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
        # 追問區塊永遠在最後（清單不靠模型）
        self.assertEqual([b["key"] for b in res["blocks"]], ["chamber", "followup"])
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
                         ["volume", "specificity", "chamber", "followup"])

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

    def test_without_committee_data_the_reason_says_so(self):
        """庚的名冊上沒有第 5 會期的委員會：委員會出席率是「沒有資料」。辛有委員會、只是那個委員會
        沒有會議：那是樣本不足，不給原因。"""
        self.m["辛"] = _legislator("辛", committees=["第11屆第5會期：交通委員會"])
        compute_profiles()
        got = {i["key"]: i for i in self._chamber("庚")["indicators"]}["committee_attendance"]
        self.assertEqual((got["value"], got["n"], got["reason"]), (None, 0, "no_committee_data"))
        other = {i["key"]: i for i in self._chamber("辛")["indicators"]}["committee_attendance"]
        self.assertEqual((other["value"], other["n"], other["reason"]), (None, 0, ""))

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


# 網站的紀錄類別清單（web/news/src/lib/records.ts 的 RECORD_KIND_KEYS）。後端只部署 services/news 時
# 沒有這個檔案，那時跳過
WEB_RECORDS_TS = Path(__file__).resolve().parents[4] / "web" / "news" / "src" / "lib" / "records.ts"


class RecordKindContractTests(SimpleTestCase):
    """紀錄清單的類別（網址的 kind）是前後端的契約：後端叫 caucus_votes、網站叫 caucus 的時候，
    一致率的「看這 n 次表決」連到網站認不得的類別，點進去變成院會出席。"""

    def test_the_evidence_urls_use_exactly_the_record_kinds(self):
        kinds = [dict(parse_qsl(urlsplit(chamber.evidence_url(1, 2, i.key)).query))["kind"]
                 for i in chamber.INDICATORS]
        self.assertEqual(kinds, list(chamber.KIND_KEYS))

    @unittest.skipUnless(WEB_RECORDS_TS.exists(), "沒有網站的原始碼")
    def test_the_website_knows_the_same_record_kinds_in_the_same_order(self):
        source = WEB_RECORDS_TS.read_text(encoding="utf-8")
        match = re.search(r"export const RECORD_KIND_KEYS = \[(.*?)\] as const", source, re.S)
        self.assertIsNotNone(match, "records.ts 要有 RECORD_KIND_KEYS（後端 chamber.KIND_KEYS 的對照）")
        self.assertEqual(tuple(re.findall(r"'([a-z_]+)'", match.group(1))), chamber.KIND_KEYS)


# --- 提案與質詢一致率 ---

SPEECH_MODEL = "speech-model#topic-v1"
BILL_MODEL = "bill-model#topic-v1"
STAMP = f"{BILL_MODEL}｜{SPEECH_MODEL}"
S5_MEETING = "第11屆第5會期財政委員會第3次全體委員會議"


def _speeches(name, *areas, classifier=SPEECH_MODEL, meeting=S5_MEETING, solo=True, brief=True):
    """他在第 5 會期的質詢：每個領域一篇基礎文章（單獨發言、有摘要卡），由 classifier 分好類。"""
    for area in areas:
        n = next(_ids)
        article = Article.objects.create(
            ivod_id=f"a{n}", slug=f"2026-03-10-a{n}", source="ly", title="t",
            speaker=name if solo else f"{name}、路人", meeting=meeting, date=date(2026, 3, 10),
            ivod_url="https://example.invalid/a", status=ArticleStatus.READY,
            brief={"one_liner": "一句話", "key_numbers": [], "asks": []} if brief else None)
        Topic.objects.create(article=article, primary=area, classifier=classifier, labeled_at=NOW)


def _proposals(name, *areas, classifier=BILL_MODEL, session=5):
    """他主提案的議案，每個領域一件，由 classifier 分好類。"""
    bills = []
    for area in areas:
        bill = _bill([name], session=session)
        BillTopic.objects.create(bill=bill, primary=area, classifier=classifier, labeled_at=NOW)
        bills.append(bill)
    return bills


def _bill_gate(classifier=BILL_MODEL, passed=True, day=1):
    return BillTopicEvaluation.objects.create(
        classifier=classifier, labeled=20, correct=18 if passed else 10, accuracy=0.9 if passed else 0.5,
        passed=passed, ran_at=timezone.make_aware(datetime(2026, 10, day, 9, 30)))


def _speech_gate(classifier=SPEECH_MODEL, passed=True, day=1):
    return TopicEvaluation.objects.create(
        source="ly", classifier=classifier, labeled=20, correct=18 if passed else 10,
        accuracy=0.9 if passed else 0.5, passed=passed,
        ran_at=timezone.make_aware(datetime(2026, 10, day, 9, 30)))


class OverlapTests(SimpleTestCase):
    """一致率 ＝ Σ 各領域 min(提案占比, 質詢占比) × 100。"""

    def test_two_worked_examples(self):
        # 提案一半財經、一半衛福；質詢 75% 財經、25% 勞動：只有財經重疊，min(50%, 75%) = 50%
        self.assertEqual(chamber.distribution_overlap(Counter(finance=2, welfare=2),
                                                      Counter(finance=3, labor=1)), 50.0)
        # 提案 75% 教育、25% 財經；質詢各一半：min(75%, 50%) + min(25%, 50%) = 75%
        self.assertEqual(chamber.distribution_overlap(Counter(education=3, finance=1),
                                                      Counter(education=1, finance=1)), 75.0)

    def test_the_same_shares_are_100_and_nothing_shared_is_0(self):
        # 三分之一加三分之二：用分數算，剛好 100，不是 99.99999999999999
        self.assertEqual(chamber.distribution_overlap(Counter(finance=1, welfare=2),
                                                      Counter(finance=2, welfare=4)), 100.0)
        self.assertEqual(chamber.distribution_overlap(Counter(finance=3), Counter(labor=5)), 0.0)

    def test_it_is_symmetric_and_zero_counts_do_not_matter(self):
        a, b = Counter(finance=2, welfare=1, labor=0), Counter(finance=1, defense=4)
        self.assertEqual(chamber.distribution_overlap(a, b), chamber.distribution_overlap(b, a))
        self.assertEqual(chamber.distribution_overlap(a, b), 20.0)

    def test_an_empty_side_has_no_value(self):
        self.assertIsNone(chamber.distribution_overlap(Counter(), Counter(finance=5)))
        self.assertIsNone(chamber.distribution_overlap(Counter(finance=5), Counter(finance=0)))


class AlignmentTests(TestCase):
    """第 5 會期，議案與質詢的分類器名字不同（各自評估通過）。"""

    def setUp(self):
        self.s5 = _session()
        for name in ("甲", "乙", "丙", "丁", "戊", "己", "庚"):
            _legislator(name)

    def _both_gates(self):
        _bill_gate()
        _speech_gate()

    def test_the_value_is_the_overlap_and_n_the_classified_proposals(self):
        self._both_gates()
        # 質詢：財經 3、勞動 1、教育 1（60%、20%、20%）；提案：財經 2、衛福 2（各 50%）
        _speeches("甲", "finance", "finance", "finance", "labor", "education")
        _proposals("甲", "finance", "welfare", "finance", "welfare")
        _bill(["甲"])                                                  # 還沒分類
        _proposals("甲", "labor", classifier="old-model#topic-v0")    # 不是上線的版本
        _proposals("甲", "labor", session=4)                          # 別的會期
        _proposals("乙", "labor")                                     # 別人的
        compute_profiles()
        stat = _stats("proposal_alignment")["甲"]
        self.assertEqual((stat.value, stat.n, stat.classifier), (50.0, 4, STAMP))
        # 4 件不到最小樣本：照給值，不給百分位
        self.assertIsNone(stat.percentile)

    def test_only_solo_speeches_with_a_brief_classified_by_the_live_version_count(self):
        self._both_gates()
        _speeches("甲", "finance", "finance", "finance", "finance", "finance")
        _speeches("甲", "labor", solo=False)
        _speeches("甲", "labor", brief=False)
        _speeches("甲", "labor", classifier="old-model#topic-v0")
        _proposals("甲", "finance", "labor")
        compute_profiles()
        self.assertEqual(_values("proposal_alignment")["甲"], (50.0, 2))

    def test_too_few_speeches_gives_no_value(self):
        self._both_gates()
        _speeches("甲", "finance", "finance", "finance", "finance")
        _proposals("甲", *["finance"] * 6)
        compute_profiles()
        self.assertEqual(_values("proposal_alignment")["甲"], (None, 6))
        # 沒有質詢、也沒有提案的人照樣有一列（API 用它確認算過）
        self.assertEqual(_values("proposal_alignment")["乙"], (None, 0))

    def test_peers_have_enough_on_both_sides_and_get_mid_ranks(self):
        """乙到己：質詢 5 篇財經，提案 5 件裡 i 件財經 → 20%～100%。甲質詢不夠、庚提案不夠：不是同儕。"""
        self._both_gates()
        for i, name in enumerate(("乙", "丙", "丁", "戊", "己"), start=1):
            _speeches(name, *["finance"] * 5)
            _proposals(name, *["finance"] * i, *["welfare"] * (5 - i))
        _speeches("甲", *["finance"] * 4)
        _proposals("甲", *["finance"] * 5)
        _speeches("庚", *["finance"] * 5)
        _proposals("庚", *["finance"] * 4)
        compute_profiles()
        stats = _stats("proposal_alignment")
        self.assertEqual([stats[n].value for n in ("乙", "丙", "丁", "戊", "己")],
                         [20.0, 40.0, 60.0, 80.0, 100.0])
        self.assertEqual([stats[n].percentile for n in ("乙", "丙", "丁", "戊", "己")],
                         [10.0, 30.0, 50.0, 70.0, 90.0])
        self.assertEqual({s.peers for s in stats.values()}, {5})
        self.assertEqual((stats["甲"].value, stats["甲"].percentile), (None, None))
        self.assertEqual((stats["庚"].value, stats["庚"].n, stats["庚"].percentile), (100.0, 4, None))

    def test_fewer_than_five_peers_means_nobody_is_ranked(self):
        self._both_gates()
        for name in ("乙", "丙", "丁", "戊"):
            _speeches(name, *["finance"] * 5)
            _proposals(name, *["finance"] * 5)
        compute_profiles()
        self.assertEqual({(s.peers, s.percentile) for s in _stats("proposal_alignment").values()},
                         {(4, None)})

    def test_both_gates_must_pass(self):
        _speeches("甲", *["finance"] * 5)
        _proposals("甲", *["finance"] * 5)

        def rows():
            compute_profiles()
            return ProfileStat.objects.filter(indicator="proposal_alignment").count()

        self.assertEqual(rows(), 0)
        bill = _bill_gate()
        self.assertEqual(rows(), 0)                                    # 只有議案分類通過
        bill.delete()
        _speech_gate()
        self.assertEqual(rows(), 0)                                    # 只有議題分類通過
        self.assertTrue(ProfileStat.objects.filter(indicator="topic_focus").exists())
        _bill_gate()
        self.assertEqual(rows(), 7)
        # 議案分類器自己在更多標註上重評沒通過：下架，一致率跟著不算
        _bill_gate(passed=False, day=2)
        self.assertEqual(rows(), 0)
        _bill_gate(day=3)
        _speech_gate(passed=False, day=2)
        self.assertEqual(rows(), 0)

    def test_the_report_says_which_classifiers_were_used(self):
        self.assertIn("提案與質詢一致率：議案分類與立法院的議題分類沒有通過的評估，不計算",
                      str(compute_profiles()))
        _speech_gate()
        self.assertIn("提案與質詢一致率：議案分類沒有通過的評估，不計算", str(compute_profiles()))
        _bill_gate()
        self.assertIn(f"提案與質詢一致率：用議案分類器 {BILL_MODEL}、質詢分類器 {SPEECH_MODEL}（評估都通過）",
                      str(compute_profiles()))

    def test_stale_rows_are_detected(self):
        _proposals("甲", "finance")
        compute_profiles()
        self.assertFalse(chamber.alignment_stats_stale())
        # 兩道門檻剛通過、還沒重算：有院內紀錄卻沒有一致率列
        self._both_gates()
        self.assertTrue(chamber.alignment_stats_stale())
        compute_profiles()
        self.assertFalse(chamber.alignment_stats_stale())
        _bill_gate("new-model#topic-v2", day=2)
        self.assertTrue(chamber.alignment_stats_stale())
        compute_profiles()
        self.assertFalse(chamber.alignment_stats_stale())
        self.assertEqual(set(ProfileStat.objects.filter(indicator="proposal_alignment")
                             .values_list("classifier", flat=True)), {f"new-model#topic-v2｜{SPEECH_MODEL}"})
        BillTopicEvaluation.objects.all().delete()
        self.assertTrue(chamber.alignment_stats_stale())
        compute_profiles()
        self.assertFalse(chamber.alignment_stats_stale())

    def test_a_failure_in_the_alignment_keeps_the_other_eight_cards(self):
        """一致率是唯一靠模型、多讀 BillTopic 的卡：它出錯時只少這一張，出席與提案的卡照給。"""
        self._both_gates()
        _speeches("甲", *["finance"] * 5)
        _proposals("甲", *["finance"] * 5)
        locked = OperationalError("database is locked")
        with mock.patch.object(chamber, "_alignment_rows", side_effect=locked), \
                self.assertLogs("articles.chamber", "ERROR"):
            report = compute_profiles()
        self.assertEqual(_values("bills_proposed")["甲"], (5.0, 5))
        self.assertFalse(ProfileStat.objects.filter(indicator="proposal_alignment").exists())
        self.assertIn("提案與質詢一致率計算失敗", str(report))
        self.assertNotIn("院內紀錄：計算失敗", str(report))
        # 有院內紀錄、卻沒有一致率列：eval_bill_topics 看得出要重算
        self.assertTrue(chamber.alignment_stats_stale())
        compute_profiles()
        self.assertEqual(_values("proposal_alignment")["甲"], (100.0, 5))

    def test_the_query_count_does_not_grow_with_the_number_of_bills(self):
        """議案的領域一個會期查一次，不逐件查（SD 卡上的 SQLite）。"""
        self._both_gates()
        _speeches("甲", *["finance"] * 5)

        def queries(count):
            LyBill.objects.all().delete()
            _proposals("甲", *["finance"] * count)
            with CaptureQueriesContext(connection) as ctx:
                compute_profiles()
            return len(ctx.captured_queries)

        queries(1)
        self.assertEqual(queries(5), queries(40))


class AlignmentApiTests(TestCase):
    """第 5 會期：甲質詢 5 篇（財經 4、勞動 1）、提案 4 件分過類（財經 2、衛福 2）＋1 件還沒分類；
    乙質詢只有 3 篇。兩道門檻都過。"""

    def setUp(self):
        self.s5 = _session()
        self.m = {name: _legislator(name) for name in ("甲", "乙")}
        _speeches("甲", "finance", "finance", "finance", "finance", "labor")
        self.classified = _proposals("甲", "finance", "welfare", "finance", "welfare")
        self.unclassified = _bill(["甲"], ["乙"])
        _speeches("乙", "finance", "finance", "finance")
        _proposals("乙", "finance")
        _bill_gate()
        _speech_gate()
        compute_profiles()

    def _chamber(self, name):
        res = self.client.get(f"/api/people/{self.m[name].person_id}/profile",
                              {"session": self.s5.id}).json()
        return {b["key"]: b for b in res["blocks"]}.get("chamber")

    def _card(self, name):
        return next((i for i in self._chamber(name)["indicators"] if i["key"] == "proposal_alignment"), None)

    def _proposed(self, name, kind="proposed"):
        return self.client.get(f"/api/people/{self.m[name].person_id}/records",
                               {"session": self.s5.id, "kind": kind}).json()

    def test_the_card_sits_with_the_proposals(self):
        keys = [i["key"] for i in self._chamber("甲")["indicators"]]
        self.assertEqual(keys, ["plenary_attendance", "committee_attendance", "bills_proposed",
                                "bills_cosigned", "bills_passed", "proposal_alignment",
                                "vote_participation", "caucus_agreement", "caucus_defections"])
        card = self._card("甲")
        # 提案財經 50%、衛福 50%；質詢財經 80%、勞動 20%：重疊 50%
        self.assertEqual((card["label"], card["value"], card["unit"], card["n"], card["n_unit"]),
                         ("提案與質詢一致率", 50.0, "%", 4, "件"))
        self.assertEqual((card["speech_n"], card["reason"], card["sample_ok"]), (5, "", False))
        self.assertEqual(card["evidence_url"],
                         f"/records/{self.m['甲'].person_id}?session={self.s5.id}&kind=proposed")
        # 其他卡片沒有質詢篇數
        self.assertEqual({i["speech_n"] for i in self._chamber("甲")["indicators"]
                          if i["key"] != "proposal_alignment"}, {None})

    def test_too_few_speeches_says_why(self):
        card = self._card("乙")
        self.assertEqual((card["value"], card["n"], card["speech_n"], card["reason"]),
                         (None, 1, 3, "few_speeches"))

    def test_the_proposal_list_marks_each_classified_bill_and_the_marks_are_n(self):
        """證據一致性：主提案清單上標了領域的筆數等於一致率的 n；沒分類的那件照列、不標。"""
        body = _records(self.client, self._card("甲")["evidence_url"]).json()
        self.assertEqual(body["count"], 5)
        marked = [i for i in body["items"] if i["topic"] is not None]
        self.assertEqual(len(marked), self._card("甲")["n"])
        topics = {i["id"]: i["topic"] for i in body["items"]}
        self.assertIsNone(topics[self.unclassified.bill_no])
        self.assertEqual(topics[self.classified[1].bill_no], {"key": "welfare", "label": "衛生福利"})
        self.assertEqual(body["bill_classifier"]["name"], BILL_MODEL)
        self.assertEqual(body["bill_classifier"]["accuracy"], 0.9)

    def test_only_the_proposal_list_is_marked(self):
        body = self._proposed("乙", kind="cosigned")
        self.assertEqual(body["count"], 1)
        self.assertEqual(({i["topic"] for i in body["items"]}, body["bill_classifier"]), ({None}, None))

    def test_marks_need_only_the_bill_gate(self):
        TopicEvaluation.objects.all().delete()
        self.assertIsNone(self._card("甲"))
        body = self._proposed("甲")
        self.assertEqual(sum(1 for i in body["items"] if i["topic"]), 4)
        BillTopicEvaluation.objects.all().delete()
        body = self._proposed("甲")
        self.assertEqual(({i["topic"] for i in body["items"]}, body["bill_classifier"]), ({None}, None))

    def test_no_card_without_both_gates_even_before_the_recompute(self):
        BillTopicEvaluation.objects.all().delete()
        self.assertIsNone(self._card("甲"))
        _bill_gate()
        self.assertIsNotNone(self._card("甲"))
        TopicEvaluation.objects.all().delete()
        self.assertIsNone(self._card("甲"))

    def test_rows_from_other_versions_are_not_shown(self):
        """攔的錯：上線的分類器換了、重算還沒跑（或失敗），舊版本算的一致率不能掛上新版本的名字。"""
        _bill_gate("new-model#topic-v2", day=2)
        self.assertIsNone(self._card("甲"))
        compute_profiles()
        # 新版本還沒分任何議案：算過、是空的
        card = self._card("甲")
        self.assertEqual((card["value"], card["n"]), (None, 0))
        self.assertEqual(self._proposed("甲")["bill_classifier"]["name"], "new-model#topic-v2")
        self.assertEqual(sum(1 for i in self._proposed("甲")["items"] if i["topic"]), 0)
        # 質詢那一邊換了版本也一樣
        _speech_gate("new-speech#topic-v2", day=2)
        self.assertIsNone(self._card("甲"))

    def test_a_stamp_with_only_one_matching_classifier_is_not_enough(self):
        ProfileStat.objects.filter(indicator="proposal_alignment").update(
            classifier=f"{BILL_MODEL}｜other#topic-v9")
        self.assertIsNone(self._card("甲"))
        ProfileStat.objects.filter(indicator="proposal_alignment").update(classifier=BILL_MODEL)
        self.assertIsNone(self._card("甲"))
        ProfileStat.objects.filter(indicator="proposal_alignment").update(classifier=STAMP)
        self.assertIsNotNone(self._card("甲"))

    def test_no_card_when_the_session_has_no_alignment_row(self):
        """兩道門檻都過，但這個會期沒有一致率列（例如那次一致率算失敗）：不給卡，其他院內紀錄照給。"""
        ProfileStat.objects.filter(indicator="proposal_alignment").delete()
        self.assertIsNone(self._card("甲"))
        self.assertIn("bills_proposed", [i["key"] for i in self._chamber("甲")["indicators"]])

    def test_a_session_with_records_but_no_articles_says_too_few_speeches(self):
        """剛開議、只有院內紀錄的會期：沒有議題分布（也就沒有聚焦度那一列），質詢是 0 篇、不是沒算。"""
        s6 = _session("第11屆第6會期")
        _proposals("甲", "finance", "welfare", session=6)
        compute_profiles()
        res = self.client.get(f"/api/people/{self.m['甲'].person_id}/profile", {"session": s6.id}).json()
        self.assertEqual([b["key"] for b in res["blocks"]], ["chamber", "followup"])
        card = next(i for i in res["blocks"][0]["indicators"] if i["key"] == "proposal_alignment")
        self.assertEqual((card["value"], card["n"], card["speech_n"], card["reason"]),
                         (None, 2, 0, "few_speeches"))
