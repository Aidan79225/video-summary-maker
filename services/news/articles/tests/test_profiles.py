"""人物側寫：會期解析、掛會期、指標計算、排程。API 的部分在 test_api.py。"""
from __future__ import annotations

import io
import itertools
from datetime import date, datetime
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from articles.models import (Article, ArticleStatus, Membership, Person, ProfileStat, Session, Topic,
                             TopicEvaluation)
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
from articles.topics import TOPICS

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

    def test_an_exact_half_rounds_up(self):
        """round() 是銀行家捨入：15 秒＝0.25 分鐘會變成 0.2、23.25 會變成 23.2。"""
        _member("甲")
        _member("乙")
        _article("甲", duration=15)
        _article("乙", duration=1395)
        compute_profiles()
        minutes = _stats("speaking_minutes")
        self.assertEqual((minutes["甲"].value, minutes["乙"].value), (0.3, 23.3))

    def test_equal_time_from_different_splits_is_a_tie(self):
        """1/3 + 49/3 + 13/3 秒跟單獨 21 秒一樣長（0.35 分鐘）。

        用浮點數加是 20.999999999999996 秒，會捨成 0.3，單獨講 21 秒的人卻是 0.4。
        """
        for name in ("甲", "乙", "丙", "丁", "戊"):
            _member(name)
        for duration in (1, 49, 13):
            _article("甲、乙、丙", duration=duration)
        _article("丁", duration=21)
        compute_profiles()
        minutes = _stats("speaking_minutes")
        self.assertEqual({k: v.value for k, v in minutes.items()},
                         {"甲": 0.4, "乙": 0.4, "丙": 0.4, "丁": 0.4, "戊": 0.0})
        # 比 0.4 低的 1 人、同值 4 人：(1 + 0.5×4) ÷ 5
        self.assertEqual({minutes[k].percentile for k in ("甲", "乙", "丙", "丁")}, {60.0})

    def test_one_person_under_two_spellings_is_one_speaker(self):
        """名冊上同一個人的兩段任期寫法不同、又同時出現在講者欄位：仍然只是一篇有他。"""
        person = Person.objects.create(name="楊啓邦")
        _member("楊啓邦", person=person, end=date(2026, 12, 31))
        _member("楊啟邦", person=person, start=date(2026, 1, 1))
        _member("乙")
        _article("楊啓邦、楊啟邦、乙", duration=1200)
        compute_profiles()
        speeches = _stats("speeches")
        self.assertEqual((speeches["楊啓邦"].value, speeches["楊啓邦"].n), (1.0, 1))
        # 講者是兩個人，各佔一半
        minutes = _stats("speaking_minutes")
        self.assertEqual((minutes["楊啓邦"].value, minutes["乙"].value), (10.0, 10.0))

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


@override_settings(TOPIC_DAILY_LIMIT=200)
class NightlyJobTests(SimpleTestCase):
    """排程：匯入 → 分政策領域 → 重算；任何一步失敗都不能冒出排程（APScheduler 會把工作移除）。"""

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

    STEPS = ["ingest_ivod", "classify_topics", "compute_profiles"]

    def test_topics_are_classified_after_the_ingest_and_before_the_recompute(self):
        self.assertEqual(self._run(), [("ingest_ivod", {"days": 3}),
                                       ("classify_topics", {"limit": 200}),
                                       ("compute_profiles", {})])

    @override_settings(TOPIC_DAILY_LIMIT=7)
    def test_the_classification_limit_comes_from_the_settings(self):
        self.assertEqual(self._run()[1], ("classify_topics", {"limit": 7}))

    def test_a_failing_recompute_does_not_break_the_schedule(self):
        with self.assertLogs(self.LOGGER, "ERROR") as logs:
            calls = self._run(fail={"compute_profiles"})
        self.assertEqual([name for name, _ in calls], self.STEPS)
        self.assertIn("人物側寫重算失敗", logs.output[0])

    def test_a_failing_ingest_still_classifies_and_recomputes(self):
        with self.assertLogs(self.LOGGER, "ERROR"):
            calls = self._run(fail={"ingest_ivod"})
        self.assertEqual([name for name, _ in calls], self.STEPS)

    def test_a_failing_classification_still_recomputes(self):
        with self.assertLogs(self.LOGGER, "ERROR") as logs:
            calls = self._run(fail={"classify_topics"})
        self.assertEqual([name for name, _ in calls], self.STEPS)
        self.assertIn("議題分類失敗", logs.output[0])


class TermRolloverTests(TestCase):
    """攔的 bug：議會的任期沒有日期，只看日期的話每個會期的同儕是「名冊上出現過的所有人」。
    換屆同步之後，舊會期混進新議員、新會期混進卸任的人，已經公開的百分位跟著變。"""

    def _sync(self, names, term, today, roles=None):
        from articles.members_sync import MemberRecord, sync

        roles = roles or {}
        sync([MemberRecord(source="ntpc", external_id=f"C{name}", name=name, party="無黨籍",
                           term=term, role=roles.get(name, "")) for name in names], today=today)

    def test_old_sessions_keep_their_peers_after_the_next_term_is_synced(self):
        old = ["甲", "乙", "丙", "丁", "戊", "己"]
        self._sync(old, "第4屆", date(2026, 9, 1))
        for count, name in enumerate(old, start=1):
            for _ in range(count):
                _article(name, source="ntpc", meeting=NTPC_MEETING, day="2026-09-10")
        compute_profiles()
        before = _snapshot()
        self.assertEqual(_stats("speeches")["甲"].peers, 6)

        # 第5屆：甲、乙連任，五位新人；丙丁戊己卸任
        self._sync(["甲", "乙", "庚", "辛", "壬", "癸", "子"], "第5屆", date(2026, 12, 27))
        _article("甲", source="ntpc", meeting="第5屆第1次定期會 市政總質詢", day="2027-03-01")
        compute_profiles()

        old_session = Session.objects.get(name="第4屆第8次定期會")
        self.assertEqual(sorted(r for r in _snapshot() if r[1] == old_session.id), before)
        new_session = Session.objects.get(name="第5屆第1次定期會")
        peers = {s.person.name for s in ProfileStat.objects.filter(
            session=new_session, indicator="speeches").select_related("person")}
        self.assertEqual(peers, {"甲", "乙", "庚", "辛", "壬", "癸", "子"})

    def test_a_member_who_later_becomes_speaker_keeps_his_earlier_speeches(self):
        """職位看「那一屆」的任期：下一屆當選議長的人，這一屆仍是一般議員。"""
        names = ["甲", "乙", "丙", "丁", "戊", "己", "蔣根煌"]
        self._sync(names, "第4屆", date(2026, 9, 1), roles={"蔣根煌": "議長"})
        for _ in range(4):
            _article("甲", source="ntpc", meeting=NTPC_MEETING, day="2026-09-10")
        compute_profiles()
        self.assertEqual(_stats("speeches")["甲"].value, 4)

        self._sync(names, "第5屆", date(2026, 12, 27), roles={"甲": "議長"})
        compute_profiles()
        stats = _stats("speeches")
        self.assertEqual(stats["甲"].value, 4)
        self.assertNotIn("蔣根煌", stats)


class ShortSessionAfterTermChangeTests(TestCase):
    def test_re_elected_councillors_count_in_a_session_held_before_the_next_sync(self):
        """攔的 bug：新任期從同步那天開始的話，新屆開議到下一次同步之間的臨時會裡，連任的
        人對不上第5屆的任期（從同步日才開始）、第4屆的又被屆別擋掉，於是從母體裡消失。"""
        from articles.members_sync import MemberRecord, sync

        def roster(names, term, today):
            sync([MemberRecord(source="ntpc", external_id=f"C{n}", name=n, party="無黨籍", term=term)
                  for n in names], today=today)

        roster(["甲", "乙", "丙", "丁", "戊", "己"], "第4屆", date(2026, 9, 6))
        new_term = ["甲", "乙", "庚", "辛", "壬", "癸", "子"]
        for name in new_term:
            _article(name, source="ntpc", meeting="第5屆第1次臨時會", day="2026-12-28")
        roster(new_term, "第5屆", date(2027, 1, 3))
        report = compute_profiles()
        stats = _stats("speeches")
        self.assertEqual(set(stats), set(new_term))
        self.assertEqual(stats["甲"].peers, 7)
        self.assertFalse(report.outside)


class ReportTests(TestCase):
    def test_a_speaker_outside_the_sessions_peers_is_reported_not_silently_dropped(self):
        person = Person.objects.create(name="甲")
        Membership.objects.create(person=person, source="tccc", name="甲", term="第3屆")
        _member("乙", source="tccc")
        _article("甲、乙", source="tccc")
        report = compute_profiles()
        self.assertEqual(report.outside[("tccc", "甲")], 1)
        self.assertIn("不在該會期母體", str(report))

    def test_finished_articles_without_a_session_are_counted_in_the_report(self):
        _member("甲", source="ly")
        _article("甲", source="ly", meeting="")
        _article("甲", source="ly", meeting="立法院朝野黨團協商")
        _article("甲", source="ly", meeting="", status=ArticleStatus.PENDING)
        report = compute_profiles()
        self.assertEqual(report.unsessioned["ly"], 2)
        self.assertIn("會議名稱裡沒有會期的文章 2 篇", str(report))


class BackfillMeetingsTests(TestCase):
    def test_empty_meeting_names_are_filled_from_lyapi_and_then_get_a_session(self):
        _member("甲", source="ly")
        empty = _article("甲", source="ly", meeting="")
        kept = _article("甲", source="ly", meeting=LY_MEETING)
        record = {"會議名稱": "第11屆第5會期第2次全院委員會（事由：總統咨…）"}
        out = io.StringIO()
        with mock.patch("articles.management.commands.backfill_meetings.IvodDailySource") as cls:
            cls.return_value.record.return_value = record
            call_command("backfill_meetings", stdout=out)
            cls.return_value.record.assert_called_once_with(empty.ivod_id)
        empty.refresh_from_db()
        self.assertEqual(empty.meeting, "第11屆第5會期第2次全院委員會")
        self.assertIn("補上 1", out.getvalue())
        compute_profiles()
        empty.refresh_from_db()
        kept.refresh_from_db()
        self.assertEqual(empty.session_id, kept.session_id)
        self.assertEqual(_stats("speeches")["甲"].value, 2)


# --- 議題分布 ---

CLASSIFIER = "fake-model#topic-v1"


def _pass(source, classifier=CLASSIFIER, passed=True, when=None):
    """一筆評估。通過了，那個來源的議題分布才會算，而且只算這個分類器分的。"""
    return TopicEvaluation.objects.create(
        source=source, classifier=classifier, labeled=20, correct=18 if passed else 10,
        accuracy=0.9 if passed else 0.5, passed=passed, ran_at=when or timezone.now())


def _classified(speaker, primary, source="tccc", classifier=CLASSIFIER, brief="default", **kw):
    """一篇基礎文章（單獨發言、有摘要卡）＋它的 Topic。"""
    if brief == "default":
        brief = _brief(numbers=1)
    meeting = kw.pop("meeting", LY_MEETING if source == "ly" else TCCC_MEETING)
    article = _article(speaker, source=source, meeting=meeting, brief=brief, **kw)
    Topic.objects.create(article=article, primary=primary, classifier=classifier,
                         labeled_at=timezone.now())
    return article


def _topic_rows(name):
    return {s.indicator.removeprefix("topic:"): s for s in
            ProfileStat.objects.filter(person__name=name, indicator__startswith="topic:")}


class TopicGateTests(TestCase):
    def test_without_a_passing_evaluation_nothing_about_topics_is_computed(self):
        _member("甲")
        _classified("甲", "finance")
        _pass("tccc", passed=False)
        report = compute_profiles()
        self.assertFalse(ProfileStat.objects.filter(indicator__startswith="topic").exists())
        self.assertFalse(ProfileStat.objects.filter(indicator="committee_alignment").exists())
        # 第一步的指標照常
        self.assertEqual(_stats("speeches")["甲"].value, 1)
        self.assertIn("議題分布 臺中市議會：沒有通過的評估，不計算", str(report))

    def test_a_passing_evaluation_only_switches_on_its_own_source(self):
        _member("甲")
        _member("乙", source="ly")
        _classified("甲", "finance")
        _classified("乙", "finance", source="ly")
        _pass("ly")
        report = compute_profiles()
        self.assertEqual(_topic_rows("甲"), {})
        self.assertEqual(_topic_rows("乙")["finance"].value, 1)
        self.assertIn(f"議題分布 立法院：用分類器 {CLASSIFIER}（評估通過）", str(report))

    def test_only_topics_from_the_latest_passing_classifier_count(self):
        _member("甲")
        _pass("tccc", "old#topic-v1", when=timezone.make_aware(datetime(2026, 9, 1)))
        _pass("tccc", "new#topic-v1", when=timezone.make_aware(datetime(2026, 9, 20)))
        _pass("tccc", "newest#topic-v1", passed=False,
              when=timezone.make_aware(datetime(2026, 9, 30)))
        _classified("甲", "finance", classifier="old#topic-v1")
        _classified("甲", "labor", classifier="new#topic-v1")
        _classified("甲", "welfare", classifier="newest#topic-v1")
        compute_profiles()
        rows = _topic_rows("甲")
        self.assertEqual({k: v.value for k, v in rows.items() if v.value}, {"labor": 1.0})
        self.assertEqual(rows["labor"].n, 1)


class TopicDistributionTests(TestCase):
    def setUp(self):
        _member("甲")
        _member("乙")
        _pass("tccc")

    def test_only_solo_briefed_articles_classified_by_the_passing_version_count(self):
        _classified("甲", "finance")
        _classified("甲", "finance")
        _classified("甲", "welfare")
        _classified("甲", "defense", classifier="other#topic-v2")    # 版本不符
        _classified("甲、乙", "labor")                               # 聯合質詢
        _classified("甲", "labor", brief=None)                       # 沒有摘要卡
        _classified("甲", "labor", status=ArticleStatus.PENDING)    # 還沒完成
        _article("甲", brief=_brief(numbers=1))                     # 還沒分類
        compute_profiles()
        rows = _topic_rows("甲")
        self.assertEqual(set(rows), {area.key for area in TOPICS})
        self.assertEqual({k: (v.value, v.n) for k, v in rows.items() if v.value},
                         {"finance": (2.0, 3), "welfare": (1.0, 3)})
        self.assertEqual({(v.n, v.percentile, v.peers) for v in rows.values()}, {(3, None, 0)})
        # 沒有發言的人也有 12 列，都是 0
        quiet = _topic_rows("乙")
        self.assertEqual({(v.value, v.n) for v in quiet.values()}, {(0.0, 0)})
        self.assertEqual(len(quiet), 12)

    def test_focus_is_the_largest_share_and_breadth_counts_areas_at_ten_percent_or_more(self):
        # 甲：10 篇，6 財經、3 衛福、1 勞動——勞動剛好 10%，算一個
        for primary, count in (("finance", 6), ("welfare", 3), ("labor", 1)):
            for _ in range(count):
                _classified("甲", primary)
        # 乙：11 篇，9 財經、1 衛福、1 勞動——1/11 不到 10%
        for primary, count in (("finance", 9), ("welfare", 1), ("labor", 1)):
            for _ in range(count):
                _classified("乙", primary)
        compute_profiles()
        focus, breadth = _stats("topic_focus"), _stats("topic_breadth")
        self.assertEqual((focus["甲"].value, focus["甲"].n), (60.0, 10))
        self.assertEqual((breadth["甲"].value, breadth["甲"].n), (3.0, 10))
        self.assertAlmostEqual(focus["乙"].value, 9 / 11 * 100)
        self.assertEqual(breadth["乙"].value, 1.0)

    def test_no_classified_articles_means_no_value(self):
        _article("甲", brief=_brief(numbers=1))
        compute_profiles()
        focus = _stats("topic_focus")["甲"]
        self.assertEqual((focus.value, focus.n), (None, 0))
        self.assertIsNone(_stats("topic_breadth")["甲"].value)

    def test_councils_have_no_committee_alignment(self):
        _classified("甲", "finance")
        compute_profiles()
        self.assertFalse(ProfileStat.objects.filter(indicator="committee_alignment").exists())


class TopicPercentileTests(TestCase):
    def test_small_samples_are_not_peers_and_the_rest_get_mid_rank(self):
        names = [f"議員{i}" for i in range(6)]
        for name in names:
            _member(name)
        _pass("tccc")
        areas = ["finance", "welfare", "labor", "defense", "transport"]
        # 議員 i 的五篇裡有 i+1 篇是財經：聚焦度 20、40、60、80、100
        for i, name in enumerate(names[:5]):
            for j in range(MIN_SAMPLE):
                _classified(name, "finance" if j <= i else areas[j])
        # 最後一位只差一篇
        for _ in range(MIN_SAMPLE - 1):
            _classified(names[5], "finance")
        compute_profiles()
        focus = _stats("topic_focus")
        self.assertEqual([focus[n].value for n in names[:5]], [20.0, 40.0, 60.0, 80.0, 100.0])
        self.assertEqual([focus[n].percentile for n in names[:5]], [10.0, 30.0, 50.0, 70.0, 90.0])
        self.assertEqual((focus[names[5]].n, focus[names[5]].percentile), (MIN_SAMPLE - 1, None))
        self.assertEqual({r.peers for r in focus.values()}, {5})
        # 廣度：議員 0 五個領域各一篇（每個都是 20%）＝ 5；議員 4 全是財經＝ 1
        breadth = _stats("topic_breadth")
        self.assertEqual([breadth[n].value for n in names[:5]], [5.0, 4.0, 3.0, 2.0, 1.0])

    def test_too_few_peers_means_no_percentile(self):
        _pass("tccc")
        for i in range(MIN_PEERS - 1):
            _member(f"議員{i}")
            for _ in range(MIN_SAMPLE):
                _classified(f"議員{i}", "finance")
        compute_profiles()
        self.assertEqual({(r.percentile, r.peers) for r in _stats("topic_focus").values()},
                         {(None, MIN_PEERS - 1)})


class CommitteeAlignmentTests(TestCase):
    """立委的委員會職掌：主領域落在他「那個會期」所屬委員會職掌的篇數 ÷ 有委員會資料的基礎文章數。"""

    def setUp(self):
        _pass("ly")

    def _legislator(self, name, committees, **kw):
        member = _member(name, source="ly", **kw)
        member.committees = committees
        member.save()
        return member

    def test_share_of_speeches_inside_the_portfolio(self):
        # 經濟委員會：財經、農業、環境；程序委員會沒有職掌
        self._legislator("甲", ["第11屆第5會期：經濟委員會", "第11屆第5會期：程序委員會",
                               "第11屆第4會期：內政委員會"])
        for primary in ("finance", "finance", "agriculture", "defense", "interior"):
            _classified("甲", primary, source="ly")
        compute_profiles()
        row = _stats("committee_alignment")["甲"]
        # 內政是他上一個會期的委員會，這個會期不算
        self.assertEqual((row.value, row.n), (60.0, 5))

    def test_without_committee_data_for_that_session_nothing_enters_the_denominator(self):
        self._legislator("乙", ["第11屆第4會期：財政委員會"])                # 會期對不上
        self._legislator("丙", ["第11屆第5會期：程序委員會"])                # 沒有職掌
        self._legislator("丁", [])                                           # 沒有資料
        for name in ("乙", "丙", "丁"):
            _classified(name, "finance", source="ly")
        compute_profiles()
        rows = _stats("committee_alignment")
        self.assertEqual({name: (r.value, r.n) for name, r in rows.items()},
                         {"乙": (None, 0), "丙": (None, 0), "丁": (None, 0)})
        # 其他議題指標照算
        self.assertEqual(_stats("topic_focus")["乙"].n, 1)

    def test_an_extraordinary_session_uses_the_committees_of_its_session(self):
        self._legislator("甲", ["第11屆第5會期：財政委員會"])
        _classified("甲", "finance", source="ly", meeting="第11屆第5會期第1次臨時會第2次會議")
        compute_profiles()
        self.assertEqual(_stats("committee_alignment")["甲"].value, 100.0)

    def test_a_party_switch_mid_session_still_sees_the_committees(self):
        person = Person.objects.create(name="甲")
        self._legislator("甲", [], person=person, party="台灣民眾黨",
                         start=date(2024, 2, 1), end=date(2026, 3, 15))
        self._legislator("甲", ["第11屆第5會期：財政委員會"], person=person, party="無黨籍",
                         start=date(2026, 3, 16))
        _classified("甲", "finance", source="ly", day="2026-03-01")
        _classified("甲", "labor", source="ly", day="2026-04-01")
        compute_profiles()
        row = ProfileStat.objects.get(person=person, indicator="committee_alignment")
        self.assertEqual((row.value, row.n), (50.0, 2))

    def test_the_query_count_does_not_grow_with_the_number_of_articles(self):
        """議題分布也一樣：Topic 跟文章一起讀、委員會跟任期一起讀，不逐篇查。"""
        for name in ("甲", "乙", "丙"):
            self._legislator(name, ["第11屆第5會期：財政委員會"])

        def queries(articles):
            Article.objects.all().delete()
            for i in range(articles):
                _classified(("甲", "乙", "丙")[i % 3], ("finance", "labor")[i % 2], source="ly")
            with CaptureQueriesContext(connection) as ctx:
                compute_profiles()
            return len(ctx.captured_queries)

        queries(1)
        self.assertEqual(queries(5), queries(40))
