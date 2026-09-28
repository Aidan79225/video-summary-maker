"""議員名單同步：解析用存下來的真實頁面；認人與換黨用小型資料。"""
from __future__ import annotations

import json
import os
from datetime import date

from django.test import SimpleTestCase, TestCase

from articles.members_sync import (LyMemberSource, MemberRecord, MembersUnavailable,
                                   TcccMemberSource, link_article, membership_for,
                                   parse_tccc_list, parse_tccc_profile, relink_all, sync)
from articles.models import Article, Membership, Person

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class FakeFetch:
    def __init__(self, pages, error_for=()):
        self.pages, self.error_for, self.urls = pages, tuple(error_for), []

    def __call__(self, url):
        self.urls.append(url)
        if any(url.endswith(e) for e in self.error_for):
            raise OSError("connection refused")
        for key, body in self.pages.items():
            if key in url:
                return body
        raise AssertionError(f"沒準備這個網址：{url}")


class LyParseTests(SimpleTestCase):
    def test_legislators_become_records_with_party_and_leave_dates(self):
        fetch = FakeFetch({"/legislators": _read("lyapi_legislators_11.json")})
        records = LyMemberSource("https://api.example/v2", term=11, fetch=fetch).fetch()
        by_name = {r.name: r for r in records}
        self.assertEqual(by_name["徐巧芯"].party, "中國國民黨")
        self.assertEqual(by_name["徐巧芯"].term, "第11屆")
        self.assertIsNone(by_name["徐巧芯"].end_date)
        self.assertEqual(by_name["徐巧芯"].source, "ly")
        self.assertTrue(by_name["徐巧芯"].external_id)
        self.assertEqual(by_name["黃國昌"].end_date, date(2026, 2, 1))     # 是否離職 = 是
        self.assertEqual(by_name["徐巧芯"].start_date, date(2024, 2, 1))
        self.assertIn("屆=11", fetch.urls[0].replace("%E5%B1%86", "屆"))

    def test_a_broken_api_is_unavailable(self):
        def fetch(url):
            raise OSError("connection refused")

        with self.assertRaises(MembersUnavailable):
            LyMemberSource("https://api.example/v2", term=11, fetch=fetch).fetch()


class TcccParseTests(SimpleTestCase):
    def test_the_roster_lists_each_councilor_once(self):
        rows = parse_tccc_list(_read("tccc_members_list.html"))
        self.assertEqual(len(rows), 62)      # 官網名單比影音系統多一位
        self.assertIn(("66", "楊啓邦"), rows)

    def test_the_profile_gives_party_district_and_term(self):
        self.assertEqual(parse_tccc_profile(_read("tccc_member_66.html")),
                         ("中國國民黨", "第一選區", "第4屆"))

    def test_the_source_visits_every_profile_and_skips_broken_ones(self):
        pages = {"wb_introduction01.asp": _read("tccc_members_list.html"),
                 "cno=66": _read("tccc_member_66.html")}

        class Fetch(FakeFetch):
            def __call__(self, url):
                try:
                    return super().__call__(url)
                except AssertionError:
                    return "<html>沒有資料</html>"

        fetch = Fetch(pages, error_for=["cno=3"])
        records = TcccMemberSource("https://web.example", fetch=fetch).fetch()
        self.assertEqual(len(records), 61)   # 62 位，cno=3 那頁抓不到就跳過
        yang = next(r for r in records if r.name == "楊啓邦")
        self.assertEqual((yang.party, yang.district, yang.external_id), ("中國國民黨", "第一選區", "66"))


def _rec(name, party, source="ly", external_id="", **kw):
    return MemberRecord(source=source, external_id=external_id or f"{source}-{name}",
                        name=name, party=party, **kw)


class SyncTests(TestCase):
    def test_new_people_get_a_person_and_a_membership(self):
        report = sync([_rec("甲", "民主進步黨", district="臺北市第一選舉區", term="第11屆")])
        self.assertEqual((report.created_persons, report.created_memberships), (1, 1))
        m = Membership.objects.get()
        self.assertEqual((m.person.name, m.party, m.district, m.term), ("甲", "民主進步黨", "臺北市第一選舉區", "第11屆"))

    def test_running_twice_is_idempotent(self):
        sync([_rec("甲", "民主進步黨")])
        report = sync([_rec("甲", "民主進步黨", district="新選區")])
        self.assertEqual((report.created_persons, report.created_memberships, report.updated), (0, 0, 1))
        self.assertEqual(Membership.objects.get().district, "新選區")

    def test_the_same_name_in_another_body_joins_the_same_person(self):
        sync([_rec("甲", "中國國民黨", source="tccc", term="第4屆")])
        sync([_rec("甲", "中國國民黨", source="ly", term="第11屆")])
        self.assertEqual(Person.objects.count(), 1)
        self.assertEqual(Person.objects.get().memberships.count(), 2)

    def test_an_alias_also_matches(self):
        Person.objects.create(name="楊啓邦", aliases=["楊啟邦"])
        sync([_rec("楊啟邦", "中國國民黨", source="tccc")])
        self.assertEqual(Person.objects.count(), 1)

    def test_two_people_with_the_same_name_are_not_merged_but_flagged(self):
        Person.objects.create(name="甲")
        Person.objects.create(name="甲")
        report = sync([_rec("甲", "民主進步黨")])
        self.assertEqual(Person.objects.count(), 3)
        self.assertTrue(Person.objects.order_by("-id").first().needs_review)
        self.assertEqual(report.review, 1)

    def test_a_party_change_closes_the_old_term_and_opens_a_new_one(self):
        sync([_rec("甲", "台灣民眾黨")])
        with self.assertLogs("articles.members_sync", level="WARNING"):
            report = sync([_rec("甲", "無黨籍")], today=date(2026, 9, 28))
        self.assertEqual(report.party_changes, 1)
        old, new = Membership.objects.order_by("id")
        self.assertEqual((old.party, old.end_date, old.needs_review), ("台灣民眾黨", date(2026, 9, 28), True))
        self.assertEqual((new.party, new.start_date, new.needs_review), ("無黨籍", date(2026, 9, 28), True))
        self.assertEqual(old.person_id, new.person_id)

    def test_someone_missing_from_this_run_is_not_retired(self):
        sync([_rec("甲", "民主進步黨"), _rec("乙", "中國國民黨")])
        sync([_rec("甲", "民主進步黨")])
        self.assertIsNone(Membership.objects.get(name="乙").end_date)

    def test_a_leave_date_from_the_source_is_recorded(self):
        sync([_rec("甲", "民主進步黨")])
        sync([_rec("甲", "民主進步黨", end_date=date(2026, 2, 1))])
        self.assertEqual(Membership.objects.get().end_date, date(2026, 2, 1))


def _article(ivod_id, speaker, day, source="ly"):
    return Article.objects.create(
        ivod_id=ivod_id, slug=f"{day}-{ivod_id}", title="t", speaker=speaker, source=source,
        date=date.fromisoformat(day), ivod_url="https://x/")


class LinkTests(TestCase):
    def setUp(self):
        sync([_rec("甲", "民主進步黨"), _rec("乙", "中國國民黨", source="tccc"),
              _rec("丙", "中國國民黨", source="tccc")])

    def test_a_single_speaker_gets_party_and_membership(self):
        a = _article("1", "甲", "2026-08-27")
        link_article(a)
        a.refresh_from_db()
        self.assertEqual(a.party, "民主進步黨")
        self.assertEqual(a.membership.name, "甲")

    def test_a_joint_article_gets_the_distinct_parties_and_no_membership(self):
        sync([_rec("丁", "民主進步黨", source="tccc")])
        a = _article("tccc-1", "乙、丙、丁", "2026-09-24", source="tccc")
        link_article(a)
        a.refresh_from_db()
        self.assertEqual(a.party, "中國國民黨、民主進步黨")
        self.assertIsNone(a.membership)

    def test_the_party_is_the_one_in_force_on_the_article_date(self):
        sync([_rec("甲", "無黨籍")], today=date(2026, 9, 1))
        before = _article("1", "甲", "2026-08-27")
        after = _article("2", "甲", "2026-09-24")
        link_article(before)
        link_article(after)
        before.refresh_from_db()
        after.refresh_from_db()
        self.assertEqual((before.party, after.party), ("民主進步黨", "無黨籍"))

    def test_an_unknown_speaker_or_wrong_body_leaves_it_blank(self):
        a = _article("1", "乙", "2026-08-27")            # 乙是臺中的，這篇是立法院
        link_article(a)
        a.refresh_from_db()
        self.assertEqual((a.party, a.membership), ("", None))

    def test_relink_all_touches_every_article(self):
        _article("1", "甲", "2026-08-27")
        _article("2", "誰", "2026-08-27")
        self.assertEqual(relink_all(), 2)
        self.assertEqual(Article.objects.get(ivod_id="1").party, "民主進步黨")

    def test_membership_for_prefers_the_latest_overlapping_term(self):
        sync([_rec("甲", "無黨籍")], today=date(2026, 9, 1))
        self.assertEqual(membership_for("ly", "甲", date(2026, 9, 1)).party, "無黨籍")
        self.assertEqual(membership_for("ly", "甲", date(2026, 8, 31)).party, "民主進步黨")
