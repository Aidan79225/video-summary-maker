"""議員名單同步：解析用存下來的真實頁面；認人與換黨用小型資料。"""
from __future__ import annotations

import json
import os
import re
from datetime import date

from django.test import SimpleTestCase, TestCase

from articles.members_sync import (LyMemberSource, MemberRecord, MembersUnavailable,
                                   NtpcMemberSource, TcccMemberSource, link_article,
                                   membership_for, parse_ntpc_caucus, parse_ntpc_list,
                                   parse_ntpc_profile, parse_tccc_list, parse_tccc_profile,
                                   relink_all, sync)
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


class NtpcParseTests(SimpleTestCase):
    def test_the_list_gives_every_councilor_with_their_district(self):
        rows = parse_ntpc_list(_read("ntpc_councilor_all.html"))
        self.assertEqual(len(rows), 64)
        self.assertIn(("483", "蔣根煌", "3"), rows)
        self.assertIn(("482", "陳鴻源", "7"), rows)
        # 原住民議員：去空白、間隔號統一成「．」，跟影音系統的寫法一樣
        self.assertIn(("589", "宋雨蓁Nikar．Falong", "12"), rows)
        self.assertIn(("605", "蘇錦雄Paylang．Caya", "12"), rows)
        self.assertIn(("604", "馬見Lahuy．Ipin", "13"), rows)

    def test_the_profile_gives_party_role_and_term(self):
        self.assertEqual(parse_ntpc_profile(_read("ntpc_councilor_C483.html")),
                         ("中國國民黨", "議長", "第4屆"))
        self.assertEqual(parse_ntpc_profile(_read("ntpc_councilor_C482.html")),
                         ("中國國民黨", "副議長", "第4屆"))
        self.assertEqual(parse_ntpc_profile(_read("ntpc_councilor_C520.html")),
                         ("民主進步黨", "", "第4屆"))
        # 官網寫「無政黨」，統一成跟其他來源一樣的「無黨籍」
        self.assertEqual(parse_ntpc_profile(_read("ntpc_councilor_C589.html")),
                         ("無黨籍", "", "第4屆"))

    def test_only_the_current_post_counts_as_the_role(self):
        """「經歷」裡的「新北市第3屆議長」不算：只看「現任」清單。"""
        page = re.sub(r"(<h4>經歷</h4>\s*<ul>)", r"\1<li>新北市第3屆議長</li>",
                      _read("ntpc_councilor_C520.html"), count=1)
        self.assertIn("新北市第3屆議長", page)
        self.assertEqual(parse_ntpc_profile(page)[1:], ("", "第4屆"))

    def test_caucus_pages_list_their_members(self):
        counts = [len(parse_ntpc_caucus(_read(f"ntpc_party_group_P{n}.html"))) for n in (1, 2, 3)]
        self.assertEqual(counts, [31, 28, 5])
        self.assertEqual(parse_ntpc_caucus(_read("ntpc_party_group_P3.html")),
                         ["565", "614", "577", "589", "604"])


class NtpcSourceTests(SimpleTestCase):
    BASE = "https://web.example"

    def _fetch(self, error_for=(), **overrides):
        pages = {"councilor-all?program=37": _read("ntpc_councilor_all.html"),
                 "P=1": _read("ntpc_party_group_P1.html"),
                 "P=2": _read("ntpc_party_group_P2.html"),
                 "P=3": _read("ntpc_party_group_P3.html"),
                 "&C=483": _read("ntpc_councilor_C483.html"),
                 "&C=482": _read("ntpc_councilor_C482.html"),
                 "&C=589": _read("ntpc_councilor_C589.html")}
        pages.update(overrides)
        normal = _read("ntpc_councilor_C520.html")

        class Fetch(FakeFetch):
            # 其他 60 位都給一張一般議員的個人頁
            def __call__(self, url):
                try:
                    return super().__call__(url)
                except AssertionError:
                    return normal

        return Fetch(pages, error_for)

    def test_every_councilor_gets_party_role_caucus_and_district(self):
        sleeps = []
        fetch = self._fetch()
        records = NtpcMemberSource(self.BASE, fetch=fetch, sleep=sleeps.append).fetch()
        self.assertEqual(len(records), 64)
        by_name = {r.name: r for r in records}
        chiang = by_name["蔣根煌"]
        self.assertEqual((chiang.source, chiang.external_id, chiang.party, chiang.role,
                          chiang.caucus, chiang.district, chiang.term),
                         ("ntpc", "483", "中國國民黨", "議長", "國民黨團", "第3選區", "第4屆"))
        self.assertEqual((by_name["陳鴻源"].role, by_name["陳鴻源"].caucus), ("副議長", "國民黨團"))
        song = by_name["宋雨蓁Nikar．Falong"]
        self.assertEqual((song.party, song.caucus, song.district), ("無黨籍", "無黨團結聯盟", "第12選區"))
        # 選區來自總覽頁的區塊：這裡給的個人頁是第 11 選區的周雅玲，結果照樣是第 13
        self.assertEqual(by_name["馬見Lahuy．Ipin"].district, "第13選區")
        self.assertEqual(by_name["彭佳芸"].caucus, "民進黨團")
        self.assertIn(f"{self.BASE}/councilor-detail?program=37&A=3&C=483", fetch.urls)
        self.assertIn(f"{self.BASE}/party-group-detail?program=39&P=3", fetch.urls)
        # 1 + 3 + 64 次請求，循序、每次間隔一秒
        self.assertEqual(len(fetch.urls), 68)
        self.assertEqual(sleeps, [1.0] * 67)

    def test_a_profile_without_the_current_term_gets_the_roster_term(self):
        """洪佳君的「現任」只列社團職務；總覽頁上的人都是本屆，屆次用其他人的補。"""
        no_term = re.sub(r"<li>新北市第4屆議員</li>", "<li>新北市體育總會副理事長</li>",
                         _read("ntpc_councilor_C520.html"))
        self.assertEqual(parse_ntpc_profile(no_term), ("民主進步黨", "", ""))
        fetch = self._fetch(**{"&C=532": no_term})
        records = NtpcMemberSource(self.BASE, fetch=fetch, sleep=lambda s: None).fetch()
        self.assertEqual(next(r for r in records if r.external_id == "532").term, "第4屆")

    def test_an_error_page_fails_the_whole_sync(self):
        """錯誤頁也回 200：寫進去會把議長的職位洗成空的；略過又會讓名冊少了議長。"""
        fetch = self._fetch(**{"&C=483": "<html>系統忙碌中，請稍後再試</html>"})
        with self.assertRaises(MembersUnavailable):
            NtpcMemberSource(self.BASE, fetch=fetch, sleep=lambda s: None).fetch()

    def test_one_unreachable_profile_fails_the_whole_sync(self):
        """攔的 bug：略過抓不到的那一位，剛好是副議長的話，名冊就少了主席，而且表不是空的
        就不會再同步——之後每段總質詢都把副議長當成講者。"""
        with self.assertRaises(MembersUnavailable):
            NtpcMemberSource(self.BASE, fetch=self._fetch(error_for=["C=482"]),
                             sleep=lambda s: None).fetch()

    def test_an_ordinary_councilors_unreachable_profile_also_fails_the_sync(self):
        """議長、副議長以外的人抓不到也一樣：略過會讓那位議員的政黨與黨團從名冊消失。"""
        with self.assertRaises(MembersUnavailable):
            NtpcMemberSource(self.BASE, fetch=self._fetch(error_for=["C=520"]),
                             sleep=lambda s: None).fetch()

    def test_a_transient_profile_error_is_retried(self):
        fetch = self._fetch()
        failures = {"left": 2}
        original = fetch.__call__

        def flaky(url):
            if "C=520" in url and failures["left"]:
                failures["left"] -= 1
                raise OSError("timed out")
            return original(url)

        sleeps = []
        records = NtpcMemberSource(self.BASE, fetch=flaky, sleep=sleeps.append).fetch()
        self.assertEqual(len(records), 64)
        self.assertIn(3.0, sleeps)
        self.assertIn(10.0, sleeps)

    def test_a_roster_without_exactly_one_speaker_and_deputy_is_rejected(self):
        normal = _read("ntpc_councilor_C520.html")
        for page in ("&C=483", "&C=482"):
            with self.assertRaises(MembersUnavailable):
                NtpcMemberSource(self.BASE, fetch=self._fetch(**{page: normal}),
                                 sleep=lambda s: None).fetch()

    def test_a_broken_list_is_unavailable(self):
        for fetch in (self._fetch(error_for=["councilor-all?program=37"]),
                      self._fetch(**{"councilor-all?program=37": "<html>改版了</html>"})):
            with self.assertRaises(MembersUnavailable):
                NtpcMemberSource(self.BASE, fetch=fetch, sleep=lambda s: None).fetch()

    def test_a_broken_caucus_page_is_unavailable(self):
        """同步直接覆寫黨團欄位：少一頁就會把一整個黨團寫成「沒有黨團」。"""
        for fetch in (self._fetch(error_for=["P=2"]), self._fetch(**{"P=2": "<html></html>"})):
            with self.assertRaises(MembersUnavailable):
                NtpcMemberSource(self.BASE, fetch=fetch, sleep=lambda s: None).fetch()


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


class NtpcSyncTests(TestCase):
    """新北多了職位與黨團兩欄；其他來源留空。"""

    def test_role_and_caucus_are_stored(self):
        sync([_rec("蔣根煌", "中國國民黨", source="ntpc", external_id="483", role="議長",
                   caucus="國民黨團"),
              _rec("甲", "民主進步黨")])
        chiang = Membership.objects.get(source="ntpc")
        self.assertEqual((chiang.role, chiang.caucus), ("議長", "國民黨團"))
        self.assertEqual((Membership.objects.get(source="ly").role,
                          Membership.objects.get(source="ly").caucus), ("", ""))

    def test_a_former_speaker_loses_the_role(self):
        """卸任的議長要變回空的，否則之後他的質詢會被當成主持人拿掉。"""
        sync([_rec("甲", "中國國民黨", source="ntpc", external_id="1", role="議長")])
        sync([_rec("甲", "中國國民黨", source="ntpc", external_id="1", role="")])
        self.assertEqual(Membership.objects.get().role, "")

    def test_a_party_change_carries_role_and_caucus_into_the_new_term(self):
        sync([_rec("甲", "台灣民眾黨", source="ntpc", external_id="1", caucus="無黨團結聯盟")])
        with self.assertLogs("articles.members_sync", level="WARNING"):
            sync([_rec("甲", "無黨籍", source="ntpc", external_id="1", role="副議長",
                       caucus="國民黨團")], today=date(2026, 9, 28))
        old, new = Membership.objects.order_by("id")
        self.assertEqual((old.party, old.role, old.caucus), ("台灣民眾黨", "", "無黨團結聯盟"))
        self.assertEqual((new.party, new.role, new.caucus), ("無黨籍", "副議長", "國民黨團"))

    def test_articles_link_by_the_normalised_name(self):
        """名冊與講者都正規化過（宋雨蓁 Nikar‧Falong → 宋雨蓁Nikar．Falong），才對得上。"""
        sync([_rec("宋雨蓁Nikar．Falong", "無黨籍", source="ntpc", external_id="589"),
              _rec("李翁月娥", "無黨團結聯盟", source="ntpc", external_id="577")])
        a = _article("ntpc-0408R1150908020", "宋雨蓁Nikar．Falong、李翁月娥", "2026-09-08",
                     source="ntpc")
        link_article(a)
        a.refresh_from_db()
        self.assertEqual(a.party, "無黨籍、無黨團結聯盟")


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



class NtpcCaucusClearingTests(TestCase):
    def test_leaving_a_caucus_clears_it(self):
        """攔的 mutation：caucus 若寫成「新值 or 舊值」，退出黨團的人會一直留在原黨團，
        黨團時段的過濾就會錯。"""
        from articles.models import Membership
        sync([_rec("甲", "民主進步黨", source="ntpc", caucus="民進黨團")])
        sync([_rec("甲", "民主進步黨", source="ntpc", caucus="")])
        self.assertEqual(Membership.objects.get(name="甲").caucus, "")


class TermChangeTests(TestCase):
    """攔的 bug：換屆時直接改寫 term 與 role，議會的任期又沒有日期，舊會期的同儕就會混進
    新議員，新任議長以前當一般議員的發言也會被當成主持人拿掉。"""

    def test_a_re_elected_member_gets_a_new_term_and_the_old_one_is_kept(self):
        sync([_rec("甲", "中國國民黨", source="ntpc", term="第4屆")], today=date(2026, 9, 1))
        report = sync([_rec("甲", "中國國民黨", source="ntpc", term="第5屆", role="議長")],
                      today=date(2026, 12, 27))
        self.assertEqual(report.term_changes, 1)
        old, new = Membership.objects.order_by("id")
        self.assertEqual((old.term, old.role, old.end_date), ("第4屆", "", date(2026, 12, 26)))
        self.assertEqual((new.term, new.role, new.start_date), ("第5屆", "議長", date(2026, 12, 27)))
        self.assertEqual(old.person_id, new.person_id)
        # 下一次同步找到的是新的一段，不會再切
        self.assertEqual(sync([_rec("甲", "中國國民黨", source="ntpc", term="第5屆", role="議長")],
                              today=date(2027, 1, 3)).term_changes, 0)

    def test_an_unknown_term_is_not_a_change(self):
        sync([_rec("甲", "中國國民黨", source="tccc", term="")])
        self.assertEqual(sync([_rec("甲", "中國國民黨", source="tccc", term="第4屆")]).term_changes, 0)
        self.assertEqual(Membership.objects.get().term, "第4屆")

    def test_term_numbers(self):
        from articles.members_sync import term_number

        self.assertEqual([term_number(t) for t in ("第11屆", "11", "第０４屆", "", None)],
                         ["11", "11", "4", "", ""])
