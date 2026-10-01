"""人物側寫：會期解析、掛會期、指標計算、排程。API 的部分在 test_api.py。"""
from __future__ import annotations

import io
import itertools
from datetime import date, datetime
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from articles.models import Article, ArticleStatus, Membership, Person, ProfileStat, Session
from articles.profiles import (
    MIN_PEERS,
    MIN_SAMPLE,
    SessionKey,
    assign_sessions,
    brief_counts,
    compute_profiles,
    parse_session,
    percentile,
)

TCCC_MEETING = "第4屆第8次定期會 市政總質詢"
NTPC_MEETING = "第4屆第8次定期會 市政總質詢"
LY_MEETING = "第11屆第5會期第23次會議"

SOURCE = {"law": "醫療法", "article": "第一百零六條", "title": "醫療法 第一百零六條",
          "excerpt": "處新臺幣三萬元以上五萬元以下罰鍰",
          "official_url": "https://law.moj.gov.tw/x", "api_url": "https://ly.govapi.tw/v2/x"}

_ids = itertools.count(1)


def _member(name, source="tccc", role="", start=None, end=None, person=None, party=""):
    person = person or Person.objects.create(name=name)
    return Membership.objects.create(person=person, source=source, name=name, role=role,
                                     start_date=start, end_date=end, party=party)


def _article(speaker, source="tccc", meeting=TCCC_MEETING, day="2026-09-01", duration=600,
             status=ArticleStatus.READY, brief=None):
    n = next(_ids)
    return Article.objects.create(
        ivod_id=f"{source}-{n}", slug=f"{day}-{source}-{n}", source=source, title="t",
        speaker=speaker, meeting=meeting, date=date.fromisoformat(day), duration_seconds=duration,
        ivod_url="https://example.invalid/x", status=status, brief=brief)


def _brief(numbers=0, sourced=0, asks=0, deadlines=0, blank_deadline=False):
    """numbers 個數字，其中前 sourced 個有條文來源；asks 項要求，其中前 deadlines 項有期限。"""
    return {
        "one_liner": "一句話",
        "key_numbers": [{"value": str(i + 1), "unit": "億元", "label": f"數字{i}", "quote": "q",
                         "law": "", "article": "", "sources": [SOURCE] if i < sourced else []}
                        for i in range(numbers)],
        "asks": [{"request": f"要求{i}",
                  "deadline": "一個月內" if i < deadlines else ("   " if blank_deadline else ""),
                  "response": ""} for i in range(asks)],
    }


def _stats(indicator):
    return {s.person.name: s for s in
            ProfileStat.objects.filter(indicator=indicator).select_related("person")}


def _snapshot():
    return sorted((s.person_id, s.session_id, s.indicator, s.value, s.n, s.percentile, s.peers)
                  for s in ProfileStat.objects.all())


class ParseSessionTests(SimpleTestCase):
    """會議名稱都是實測的寫法（見設計文件的表）。"""

    def test_legislative_yuan_meetings_map_to_their_session(self):
        for meeting in ("第11屆第5會期財政委員會第3次全體委員會議", "第11屆第5會期第23次會議"):
            self.assertEqual(parse_session("ly", meeting), SessionKey("ly", "11", "第11屆第5會期"),
                             meeting)

    def test_an_extraordinary_session_joins_the_session_it_belongs_to(self):
        self.assertEqual(parse_session("ly", "第11屆第3會期第1次臨時會第2次會議"),
                         SessionKey("ly", "11", "第11屆第3會期"))

    def test_council_sessions_are_taken_as_they_are(self):
        self.assertEqual(parse_session("tccc", "第4屆第8次定期會"),
                         SessionKey("tccc", "4", "第4屆第8次定期會"))
        self.assertEqual(parse_session("tccc", "第4屆第2次臨時會"),
                         SessionKey("tccc", "4", "第4屆第2次臨時會"))
        self.assertEqual(parse_session("ntpc", "第4屆第8次定期會 市政總質詢"),
                         SessionKey("ntpc", "4", "第4屆第8次定期會"))

    def test_full_width_digits_are_the_same_session(self):
        self.assertEqual(parse_session("ly", "第１１屆第５會期第２３次會議"),
                         SessionKey("ly", "11", "第11屆第5會期"))
        self.assertEqual(parse_session("ntpc", "第４屆第８次定期會 市政總質詢"),
                         SessionKey("ntpc", "4", "第4屆第8次定期會"))

    def test_meetings_without_a_session_are_left_out(self):
        self.assertIsNone(parse_session("ly", "立法院朝野黨團協商"))
        self.assertIsNone(parse_session("ly", ""))
        self.assertIsNone(parse_session("tccc", "市政總質詢"))
        # 每個來源只認自己的格式
        self.assertIsNone(parse_session("tccc", "第11屆第5會期第23次會議"))
        self.assertIsNone(parse_session("ly", "第4屆第8次定期會"))


class AssignSessionsTests(TestCase):
    def test_articles_of_every_status_get_a_session_and_the_span_covers_them(self):
        first = _article("甲", "ly", "第11屆第5會期財政委員會第3次全體委員會議", day="2026-03-03",
                         status=ArticleStatus.PENDING)
        _article("甲", "ly", "第11屆第5會期第23次會議", day="2026-05-20")
        extra = _article("甲", "ly", "第11屆第3會期第1次臨時會第2次會議", day="2025-07-15")
        council = _article("乙", "tccc", "第4屆第2次臨時會", day="2026-07-01",
                           status=ArticleStatus.FAILED)
        orphan = _article("甲", "ly", "立法院朝野黨團協商", day="2026-04-01")

        self.assertEqual(assign_sessions(), 4)

        session = Session.objects.get(source="ly", name="第11屆第5會期")
        self.assertEqual((session.term, session.start_date, session.end_date),
                         ("11", date(2026, 3, 3), date(2026, 5, 20)))
        first.refresh_from_db()
        self.assertEqual(first.session, session)
        extra.refresh_from_db()
        self.assertEqual(extra.session.name, "第11屆第3會期")
        council.refresh_from_db()
        self.assertEqual((council.session.source, council.session.name), ("tccc", "第4屆第2次臨時會"))
        orphan.refresh_from_db()
        self.assertIsNone(orphan.session)

    def test_running_again_assigns_nothing_new_but_keeps_the_span_current(self):
        _article("甲", "ly", LY_MEETING, day="2026-05-20")
        assign_sessions()
        self.assertEqual(assign_sessions(), 0)
        _article("甲", "ly", LY_MEETING, day="2026-02-25")
        self.assertEqual(assign_sessions(), 1)
        session = Session.objects.get()
        self.assertEqual((session.start_date, session.end_date),
                         (date(2026, 2, 25), date(2026, 5, 20)))

    def test_assigning_does_not_touch_updated_at(self):
        """updated_at 是判斷「處理中卡太久」的依據，掛會期不能讓卡住的文章看起來剛動過。"""
        article = _article("甲", "ly", LY_MEETING, status=ArticleStatus.PROCESSING)
        before = Article.objects.get(pk=article.pk).updated_at
        assign_sessions()
        self.assertEqual(Article.objects.get(pk=article.pk).updated_at, before)


class BriefCountsTests(SimpleTestCase):
    def test_counts_numbers_deadlines_and_sources(self):
        self.assertEqual(brief_counts(_brief(numbers=3, sourced=1, asks=3, deadlines=2)), (3, 2, 1))

    def test_a_blank_deadline_is_no_deadline(self):
        self.assertEqual(brief_counts(_brief(asks=2, deadlines=0, blank_deadline=True)), (0, 0, 0))

    def test_a_malformed_brief_contributes_nothing(self):
        self.assertEqual(brief_counts(None), (0, 0, 0))
        self.assertEqual(brief_counts({"key_numbers": "junk", "asks": [1, None]}), (0, 0, 0))


class PercentileTests(SimpleTestCase):
    def test_mid_rank_with_ties(self):
        peers = [0, 1, 1, 2]
        # 比 1 低的有 1 人、同值 2 人（含自己）：(1 + 0.5×2) ÷ 4
        self.assertEqual(percentile(1, peers), 50.0)
        self.assertEqual(percentile(0, peers), 12.5)
        self.assertEqual(percentile(2, peers), 87.5)

    def test_everyone_equal_is_the_middle(self):
        self.assertEqual(percentile(3, [3, 3, 3, 3, 3]), 50.0)


class VolumeTests(TestCase):
    def test_a_joint_speech_counts_once_for_each_speaker_and_splits_the_time(self):
        for name in ("甲", "乙", "丙"):
            _member(name)
        _article("甲", duration=600)
        _article("甲、乙", duration=1200)
        _article("甲、乙、丙", duration=900)
        compute_profiles()
        speeches = _stats("speeches")
        self.assertEqual({k: (v.value, v.n) for k, v in speeches.items()},
                         {"甲": (3.0, 3), "乙": (2.0, 2), "丙": (1.0, 1)})
        minutes = _stats("speaking_minutes")
        # 甲：10 + 20/2 + 15/3 = 25；乙：10 + 5 = 15；丙：5
        self.assertEqual({k: (v.value, v.n) for k, v in minutes.items()},
                         {"甲": (25.0, 3), "乙": (15.0, 2), "丙": (5.0, 1)})

    def test_minutes_are_rounded_to_one_decimal(self):
        _member("甲")
        _member("乙")
        _article("甲、乙", duration=100)
        compute_profiles()
        self.assertEqual(_stats("speaking_minutes")["甲"].value, 0.8)

    def test_a_member_who_never_spoke_is_a_zero_not_missing(self):
        _member("甲")
        _member("乙")
        _article("甲", brief=_brief(numbers=2))
        compute_profiles()
        quiet = {s.indicator: (s.value, s.n) for s in ProfileStat.objects.filter(person__name="乙")}
        self.assertEqual(quiet, {"speeches": (0.0, 0), "speaking_minutes": (0.0, 0),
                                 "numbers_per_speech": (None, 0),
                                 "deadline_asks_per_speech": (None, 0),
                                 "sourced_number_share": (None, 0)})

    def test_the_speaker_and_deputy_speaker_are_not_peers(self):
        _member("蔣根煌", "ntpc", role="議長")
        _member("陳鴻源", "ntpc", role="副議長")
        regulars = ["王威元", "林裔綺", "周雅玲", "宋雨蓁Nikar．Falong", "鍾宏仁"]
        for name in regulars:
            _member(name, "ntpc")
        _article("王威元、蔣根煌", "ntpc", NTPC_MEETING)
        compute_profiles()
        self.assertFalse(ProfileStat.objects.filter(person__name__in=["蔣根煌", "陳鴻源"]).exists())
        speeches = _stats("speeches")
        self.assertEqual(set(speeches), set(regulars))
        self.assertEqual({s.peers for s in speeches.values()}, {5})
        # 主持人沒有被當成對不到任期的講者
        self.assertEqual(compute_profiles().unmatched, {})

    def test_only_finished_articles_count(self):
        _member("甲")
        _article("甲", day="2026-09-01")
        _article("甲", day="2026-08-01", status=ArticleStatus.PENDING)
        _article("甲", day="2026-08-02", status=ArticleStatus.FAILED)
        compute_profiles()
        self.assertEqual(_stats("speeches")["甲"].value, 1.0)
        # 涵蓋範圍看所有狀態的文章
        self.assertEqual(Session.objects.get().start_date, date(2026, 8, 1))

    def test_a_party_switch_mid_session_is_still_one_person(self):
        person = Person.objects.create(name="甲")
        _member("甲", "ly", person=person, party="台灣民眾黨",
                start=date(2024, 2, 1), end=date(2026, 3, 15))
        _member("甲", "ly", person=person, party="無黨籍", start=date(2026, 3, 16))
        _article("甲", "ly", LY_MEETING, day="2026-03-01")
        _article("甲", "ly", LY_MEETING, day="2026-04-01")
        compute_profiles()
        rows = ProfileStat.objects.filter(person=person, indicator="speeches")
        self.assertEqual([(r.value, r.n) for r in rows], [(2.0, 2)])

    def test_a_term_must_cover_the_date_of_the_speech(self):
        _member("甲", "ly", start=date(2024, 2, 1), end=date(2026, 3, 1))
        _article("甲", "ly", LY_MEETING, day="2026-04-01")
        report = compute_profiles()
        self.assertEqual(report.unmatched, {("ly", "甲"): 1})
        # 任期跟這個會期的涵蓋範圍不重疊，也不在母體裡
        self.assertFalse(ProfileStat.objects.exists())


class SpecificityTests(TestCase):
    def setUp(self):
        _member("甲")
        _member("乙")

    def test_only_solo_speeches_with_a_brief_count(self):
        _article("甲", brief=_brief(numbers=2, sourced=1, asks=2, deadlines=1))
        _article("甲")  # 沒有摘要卡
        # 聯合質詢的摘要卡分不出是誰講的
        _article("甲、乙", brief=_brief(numbers=5, sourced=5, asks=5, deadlines=5))
        compute_profiles()
        self.assertEqual((_stats("numbers_per_speech")["甲"].value, _stats("numbers_per_speech")["甲"].n),
                         (2.0, 1))
        self.assertEqual(_stats("deadline_asks_per_speech")["甲"].value, 1.0)
        share = _stats("sourced_number_share")["甲"]
        self.assertEqual((share.value, share.n), (50.0, 2))
        self.assertEqual((_stats("numbers_per_speech")["乙"].value, _stats("numbers_per_speech")["乙"].n),
                         (None, 0))

    def test_no_numbers_means_no_sourced_share(self):
        _article("甲", brief=_brief(numbers=0, asks=1, deadlines=1))
        compute_profiles()
        share = _stats("sourced_number_share")["甲"]
        self.assertEqual((share.value, share.n), (None, 0))
        self.assertEqual(_stats("numbers_per_speech")["甲"].value, 0.0)


class PercentileRankingTests(TestCase):
    def _members(self, count):
        names = [f"議員{i}" for i in range(count)]
        for name in names:
            _member(name)
        return names

    def test_percentiles_use_mid_rank_and_ties_share_a_value(self):
        names = self._members(6)
        for name, times in zip(names, [0, 1, 1, 2, 3, 5], strict=True):
            for _ in range(times):
                _article(name)
        compute_profiles()
        got = {k: v.percentile for k, v in _stats("speeches").items()}
        expected = dict(zip(names, [0.5 / 6 * 100, 2 / 6 * 100, 2 / 6 * 100,
                                    3.5 / 6 * 100, 4.5 / 6 * 100, 5.5 / 6 * 100],
                            strict=True))
        for name in names:
            self.assertAlmostEqual(got[name], expected[name], places=9, msg=name)
        self.assertEqual({s.peers for s in _stats("speeches").values()}, {6})

    def test_too_few_peers_means_no_percentile_for_anyone(self):
        names = self._members(MIN_PEERS - 1)
        for name in names:
            _article(name)
        compute_profiles()
        rows = _stats("speeches")
        self.assertEqual({(r.percentile, r.peers) for r in rows.values()}, {(None, MIN_PEERS - 1)})

    def test_a_small_sample_gets_no_percentile_and_is_not_a_peer(self):
        names = self._members(6)
        # 五個人各 MIN_SAMPLE 篇有卡的單獨發言，最後一個人只差一篇
        for i, name in enumerate(names):
            for _ in range(MIN_SAMPLE if i < 5 else MIN_SAMPLE - 1):
                _article(name, brief=_brief(numbers=i))
        compute_profiles()
        rows = _stats("numbers_per_speech")
        short = rows[names[5]]
        self.assertEqual((short.value, short.n, short.percentile), (5.0, MIN_SAMPLE - 1, None))
        self.assertEqual({r.peers for r in rows.values()}, {5})
        self.assertEqual([rows[n].percentile for n in names[:5]], [10.0, 30.0, 50.0, 70.0, 90.0])

    def test_too_few_with_enough_sample_means_no_specificity_percentile(self):
        names = self._members(6)
        for _ in range(MIN_SAMPLE):
            _article(names[0], brief=_brief(numbers=1))
        for name in names[1:]:
            _article(name, brief=_brief(numbers=1))
        compute_profiles()
        rows = _stats("numbers_per_speech")
        self.assertEqual({(r.percentile, r.peers) for r in rows.values()}, {(None, 1)})
        # 投入量的同儕是整個母體，照樣比
        self.assertTrue(all(r.percentile is not None for r in _stats("speeches").values()))


class ComputeTests(TestCase):
    def test_names_without_a_term_are_reported_with_their_article_count(self):
        _member("甲")
        _article("路人甲")
        _article("路人甲")
        _article("甲、路人甲", duration=1200)
        report = compute_profiles()
        self.assertEqual(report.unmatched, {("tccc", "路人甲"): 3})
        self.assertIn("路人甲：3 篇", str(report))
        # 對不到的人仍然佔一份時長：分不出誰講了多久，平分不因名冊缺人而改變
        self.assertEqual(_stats("speaking_minutes")["甲"].value, 10.0)

    def test_the_report_lists_each_session_with_its_population(self):
        for name in ("甲", "乙", "丙"):
            _member(name)
        _article("甲")
        report = compute_profiles()
        self.assertEqual([(s.session.name, s.population, s.ready_articles) for s in report.sessions],
                         [("第4屆第8次定期會", 3, 1)])
        self.assertIn("母體 3 人", str(report))

    def test_recomputing_gives_the_same_rows(self):
        for name in ("甲", "乙", "丙", "丁", "戊", "己"):
            _member(name)
        _article("甲、乙", brief=_brief(numbers=1))
        for _ in range(6):
            _article("丙", brief=_brief(numbers=2, sourced=1, asks=1, deadlines=1))
        compute_profiles()
        first = _snapshot()
        compute_profiles()
        self.assertEqual(_snapshot(), first)
        self.assertEqual(len(first), 6 * 5)

    def test_every_row_of_a_run_has_the_same_timestamp(self):
        _member("甲")
        _article("甲")
        when = timezone.make_aware(datetime(2026, 10, 1, 6, 12))
        compute_profiles(now=when)
        self.assertEqual({s.computed_at for s in ProfileStat.objects.all()}, {when})

    def test_a_session_with_nothing_finished_any_more_loses_its_rows(self):
        _member("甲")
        article = _article("甲")
        compute_profiles()
        self.assertTrue(ProfileStat.objects.exists())
        Article.objects.filter(pk=article.pk).update(status=ArticleStatus.PENDING)
        compute_profiles()
        self.assertFalse(ProfileStat.objects.exists())

    def test_the_query_count_does_not_grow_with_the_number_of_articles(self):
        """Pi 跑在 SD 卡上的 SQLite：任期一個來源讀一次、文章一個會期讀一次。"""
        for name in ("甲", "乙", "丙"):
            _member(name)

        def queries(articles):
            Article.objects.all().delete()
            for i in range(articles):
                _article(("甲", "乙", "甲、丙")[i % 3], brief=_brief(numbers=1))
            with CaptureQueriesContext(connection) as ctx:
                compute_profiles()
            return len(ctx.captured_queries)

        queries(1)  # 第一次會建會期，多幾個查詢；之後才是穩定狀態
        self.assertEqual(queries(5), queries(40))


class CommandTests(TestCase):
    def test_the_command_prints_the_report(self):
        _member("甲")
        _article("甲")
        _article("路人甲")
        out = io.StringIO()
        call_command("compute_profiles", stdout=out)
        text = out.getvalue()
        self.assertIn("第4屆第8次定期會", text)
        self.assertIn("路人甲：1 篇", text)
        self.assertEqual(ProfileStat.objects.filter(indicator="speeches").count(), 1)


class NightlyJobTests(SimpleTestCase):
    """排程：匯入之後接著重算；任何一步失敗都不能冒出排程（APScheduler 會把工作移除）。"""

    LOGGER = "articles.management.commands.run_scheduler"

    def _run(self, fail=()):
        from articles.management.commands import run_scheduler

        calls = []

        def fake(name, **kwargs):
            calls.append((name, kwargs))
            if name in fail:
                raise RuntimeError(f"{name} 壞了")

        with mock.patch.object(run_scheduler, "call_command", side_effect=fake):
            run_scheduler.nightly(3)
        return calls

    def test_profiles_are_recomputed_right_after_the_ingest(self):
        self.assertEqual(self._run(), [("ingest_ivod", {"days": 3}), ("compute_profiles", {})])

    def test_a_failing_recompute_does_not_break_the_schedule(self):
        with self.assertLogs(self.LOGGER, "ERROR") as logs:
            calls = self._run(fail={"compute_profiles"})
        self.assertEqual([name for name, _ in calls], ["ingest_ivod", "compute_profiles"])
        self.assertIn("人物側寫重算失敗", logs.output[0])

    def test_a_failing_ingest_still_recomputes(self):
        with self.assertLogs(self.LOGGER, "ERROR"):
            calls = self._run(fail={"ingest_ivod"})
        self.assertEqual([name for name, _ in calls], ["ingest_ivod", "compute_profiles"])
