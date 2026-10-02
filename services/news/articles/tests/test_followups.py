"""追問率：期限換算、候選與篩選、判斷與落地檢查、狀態、重產清理、標註集、評估、上線條件。

用假的 GPU 客戶端，不碰網路。指標的計算在 test_profiles.py，API 在 test_api.py，admin 在
test_admin.py，GPU 客戶端在 test_gpu_client.py。
"""
from __future__ import annotations

import io
import itertools
import re
from datetime import date, datetime, timedelta
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from articles.followups import (
    EXCERPT_LIMIT,
    FOLLOWUP_MIN_LABELS,
    CandidateIndex,
    FollowUpState,
    add_months,
    ask_at,
    asks_with_deadline,
    bigrams,
    card_for,
    check_followups,
    evaluate,
    excerpt_for,
    grounded,
    ly_session_number,
    parse_deadline,
    parse_result,
    passing_evaluation,
    passing_judge,
    sample_labels,
    state_for,
    sync_followups,
)
from articles.gpu_client import GpuApiError, JobFailed
from articles.ingest import save_result
from articles.models import (Article, ArticleStatus, FollowUp, FollowUpEvaluation, FollowUpLabel,
                             Membership, Person, ProfileStat, Slide)
from articles.topics import EvaluationAborted

CLASSIFIER = "fake-model#followup-v1#abcd1234"
LY_MEETING = "第11屆第5會期外交及國防委員會第3次全體委員會議"
REQUEST = "提出無人機交機時程清冊"
_ids = itertools.count(1)


def _brief(asks=(), one_liner="一句話"):
    return {"one_liner": one_liner, "key_numbers": [],
            "asks": [{"request": r, "deadline": d, "response": resp} for r, d, resp in asks]}


def _member(name="王立", source="ly", person=None):
    person = person or Person.objects.create(name=name)
    return Membership.objects.create(person=person, source=source, name=name)


def _article(speaker="王立", day="2026-03-02", asks=(), one_liner="一句話", titles=(), transcript="",
             source="ly", status=ArticleStatus.READY, brief=True, meeting=LY_MEETING, membership=None):
    n = next(_ids)
    article = Article.objects.create(
        ivod_id=f"{source}-f{n}", slug=f"{day}-{source}-f{n}", source=source, title=f"t{n}",
        speaker=speaker, meeting=meeting, date=date.fromisoformat(day),
        ivod_url="https://example.invalid/x", status=status, transcript_text=transcript,
        brief=_brief(asks, one_liner) if brief else None, membership=membership)
    for index, title in enumerate(titles, start=1):
        Slide.objects.create(article=article, index=index, title=title)
    return article


def _quote_in(text: str) -> str:
    match = re.search(r"「(.+?)」", text)
    return match.group(1) if match else ""


def _says_followed(pair: dict):
    """假判斷器的預設答案：逐字稿片段裡有「…」就是有追問，引用就是括號裡那句。"""
    quote = _quote_in(pair["excerpt"])
    return (bool(quote), quote)


class FakeFollowupGpu:
    """假的 GPU 客戶端，只實作 followup 工作用得到的兩個方法。

    answer(pair) 回 (有沒有追問, 引用)、例外（丟出去）或整個成品 dict（原樣回傳）；classifier
    可以是字串或 pair → 字串（模擬評估途中換了模型）。
    """

    def __init__(self, answer=_says_followed, classifier=CLASSIFIER):
        self.answer = answer
        self.classifier = classifier
        self.pairs: list[dict] = []

    def submit_followup(self, request, response, card, excerpt):
        self.pairs.append({"request": request, "response": response, "card": card, "excerpt": excerpt})
        return f"job-{len(self.pairs)}"

    def wait(self, job_id, timeout, poll_seconds=3.0, on_progress=None):
        pair = self.pairs[int(job_id.removeprefix("job-")) - 1]
        outcome = self.answer(pair)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, dict):
            return outcome
        followed, quote = outcome
        classifier = self.classifier(pair) if callable(self.classifier) else self.classifier
        return {"followed_up": followed, "quote": quote, "classifier": classifier}


def _aware(*args):
    return timezone.make_aware(datetime(*args))


# --- 期限換算 ---


class DeadlineTableTests(SimpleTestCase):
    """表上的每一種寫法：發言日 2026-03-10（立法院第 11 屆第 5 會期）。"""

    SPOKEN = date(2026, 3, 10)

    def _check(self, cases, source="ly", session=5, spoken=None):
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_deadline(text, spoken or self.SPOKEN, source, session),
                                 date.fromisoformat(expected) if expected else None)

    def test_days_weeks_months_and_years(self):
        self._check([
            ("7天內", "2026-03-17"), ("10日內", "2026-03-20"), ("30天之內", "2026-04-09"),
            ("30天以內", "2026-04-09"),
            ("2週內", "2026-03-24"), ("2周內", "2026-03-24"), ("3星期內", "2026-03-31"),
            ("2個禮拜內", "2026-03-24"), ("2個星期內", "2026-03-24"),
            ("1個月內", "2026-04-10"), ("3月內", "2026-06-10"), ("6個月內", "2026-09-10"),
            ("半年內", "2026-09-10"), ("1年內", "2027-03-10"), ("2年內", "2028-03-10"),
        ])

    def test_month_and_year_ends(self):
        self._check([
            ("本月底", "2026-03-31"), ("月底前", "2026-03-31"), ("這個月底", "2026-03-31"),
            ("下個月", "2026-04-30"), ("下個月底前", "2026-04-30"),
            ("年底前", "2026-12-31"), ("今年底", "2026-12-31"), ("今年內", "2026-12-31"),
        ])

    def test_a_calendar_day_or_month_end_without_a_year(self):
        self._check([
            ("6月30日前", "2026-06-30"), ("6月30號前", "2026-06-30"), ("6月底前", "2026-06-30"),
            ("12月31日", "2026-12-31"), ("十二月底前", "2026-12-31"),
            # 已經過了就是明年
            ("3月1日前", "2027-03-01"), ("2月底前", "2027-02-28"),
            # 當天還沒過
            ("3月10日前", "2026-03-10"),
        ])

    def test_this_session_only_for_the_legislative_yuan(self):
        same = ["本會期", "這個會期", "這會期", "會期內", "會期結束前", "本會期結束前"]
        self._check([(text, "2026-05-31") for text in same], session=5)
        self._check([(text, "2026-12-31") for text in same], session=6,
                    spoken=date(2026, 10, 3))
        # 議會不換算；立法院解析不出會期也不換算
        self._check([(text, None) for text in same], source="tccc", session=None)
        self._check([("本會期", None)], session=None)
        # 延會裡講的本會期：算出來比發言還早，換不出來
        self._check([("本會期", None)], session=5, spoken=date(2026, 7, 15))
        self._check([("下會期", None), ("下個會期內", None), ("下一會期結束前", None)])

    def test_an_even_session_belongs_to_the_year_it_opened(self):
        """攔的 bug：雙數會期延到隔年一月時，照「發言那年」算會變成隔年年底，憑空多出將近一年。"""
        # 2026 年九月開議的第 6 會期：當年講的到 12/31，最後一天也算
        self._check([("本會期", "2026-12-31")], session=6, spoken=date(2026, 9, 15))
        self._check([("本會期", "2026-12-31")], session=6, spoken=date(2026, 12, 31))
        # 延會或臨時會拖到 2027 年一、二月：會期是 2026 年開的，已經過了年底，換不出來
        for spoken in (date(2027, 1, 5), date(2027, 2, 20)):
            with self.subTest(spoken=spoken):
                self._check([("本會期", None), ("會期結束前", None)], session=6, spoken=spoken)

    def test_vague_deadlines_are_not_converted(self):
        self._check([(text, None) for text in (
            "儘快", "盡速", "立即", "馬上", "下次", "預算審查前", "下次會議前", "", "   ", "半個月內",
            "0天內", "一兩個月內", "兩三週內", "2月30日前", "13月1日前")])

    def test_a_written_year_is_not_guessed(self):
        self._check([(text, None) for text in (
            "明年3月底前", "2027年底前", "明年底", "明年年底", "115年10月1日前", "後年內")])

    def test_a_year_before_within_is_a_calendar_year_not_a_count(self):
        """攔的 bug：「115年內」被讀成 115 年後（2141 年）、「2026年內」讀成西元 4052 年。"""
        self._check([(text, None) for text in (
            "115年內", "民國115年內", "2026年內", "2026年以內", "２０２６年之內", "民國99年內", "西元2年內")])
        # 年數照算
        self._check([("3年內", "2029-03-10"), ("十年內", "2036-03-10"), ("99年內", "2125-03-10")])

    def test_a_date_out_of_range_is_unconvertible_rather_than_an_error(self):
        """攔的 bug：「99999個月內」在加月份時丟 ValueError、「9999999天內」丟 OverflowError，整輪同步停掉。"""
        self._check([(text, None) for text in ("99999個月內", "9999999天內", "999999週內")])

    def test_a_range_of_numbers_is_not_converted(self):
        """攔的 bug：「1到2個月內」只讀到後面的 2，等於替講者把期限定在最寬的那一頭。"""
        self._check([(text, None) for text in (
            "1到2個月內", "一、兩個月內", "1~2個月內", "1～2個月內", "三到六個月內", "2~3週內", "3到5天內",
            "3-5天內", "3－5天內", "十到十五天內", "1至2年內", "1或2個月內", "兩到三個禮拜內")])
        # 連接詞前面不是數字的照算
        self._check([("在2個月內", "2026-05-10"), ("到2個月內", "2026-05-10")])

    def test_a_day_of_next_month(self):
        self._check([("下個月15日前", "2026-04-15"), ("下個月15號", "2026-04-15"), ("下月5日", "2026-04-05"),
                     ("下個月十日前", "2026-04-10"), ("下個月１日", "2026-04-01")])
        # 那個月沒有這一天就取月底；跨年
        self._check([("下個月31日前", "2026-02-28")], spoken=date(2026, 1, 10))
        self._check([("下個月10號前", "2027-01-10")], spoken=date(2026, 12, 20))
        # 沒有確定的一天：不放寬成月底
        self._check([(text, None) for text in (
            "下個月初", "下個月中", "下個月中旬", "下個月上旬", "下個月下旬", "下月初", "下個月0日", "下個月32日")])
        # 沒寫哪一天的照舊是月底
        self._check([("下個月", "2026-04-30"), ("下月底前", "2026-04-30"), ("下個月內", "2026-04-30")])


class DeadlineNumeralTests(SimpleTestCase):
    SPOKEN = date(2026, 3, 10)

    def test_chinese_numerals(self):
        for text, expected in (("三十天內", "2026-04-09"), ("二十一天內", "2026-03-31"),
                               ("十天內", "2026-03-20"), ("十二個月內", "2027-03-10"),
                               ("一百二十天內", "2026-07-08"), ("一百零五天內", "2026-06-23"),
                               ("兩週之內", "2026-03-24"), ("一個月內", "2026-04-10"),
                               ("三個禮拜內", "2026-03-31"), ("一年內", "2027-03-10"),
                               ("六月底前", "2026-06-30"), ("十二月三十一日前", "2026-12-31")):
            with self.subTest(text=text):
                self.assertEqual(parse_deadline(text, self.SPOKEN, "ly", 5), date.fromisoformat(expected))

    def test_full_width_digits_and_spaces(self):
        for text, expected in (("３０天內", "2026-04-09"), ("１２月３１日前", "2026-12-31"),
                               ("６ 月 底 前", "2026-06-30"), (" 2 週 內 ", "2026-03-24")):
            with self.subTest(text=text):
                self.assertEqual(parse_deadline(text, self.SPOKEN, "ly", 5), date.fromisoformat(expected))


class DeadlineCalendarTests(SimpleTestCase):
    def test_month_ends_are_clamped(self):
        self.assertEqual(parse_deadline("1個月內", date(2026, 1, 31), "ly"), date(2026, 2, 28))
        self.assertEqual(parse_deadline("1個月內", date(2028, 1, 31), "ly"), date(2028, 2, 29))
        self.assertEqual(parse_deadline("一年內", date(2028, 2, 29), "ly"), date(2029, 2, 28))
        self.assertEqual(add_months(date(2026, 8, 31), 1), date(2026, 9, 30))

    def test_crossing_the_year(self):
        self.assertEqual(parse_deadline("3個月內", date(2026, 11, 15), "tccc"), date(2027, 2, 15))
        self.assertEqual(parse_deadline("下個月", date(2026, 12, 20), "tccc"), date(2027, 1, 31))
        self.assertEqual(parse_deadline("兩週內", date(2026, 12, 25), "tccc"), date(2027, 1, 8))
        self.assertEqual(parse_deadline("本月底", date(2026, 12, 20), "tccc"), date(2026, 12, 31))
        self.assertEqual(parse_deadline("1月15日前", date(2026, 12, 20), "tccc"), date(2027, 1, 15))

    def test_the_session_number_comes_from_the_session_or_meeting_name(self):
        self.assertEqual(ly_session_number("第11屆第5會期"), 5)
        self.assertEqual(ly_session_number("第１１屆第０６會期財政委員會"), 6)
        self.assertIsNone(ly_session_number("立法院朝野黨團協商"))


# --- 要求、雙字組、送出的內容、落地檢查 ---


class AskTests(SimpleTestCase):
    def test_only_asks_with_a_deadline_keep_their_position(self):
        brief = _brief([("甲", "", ""), ("乙", " 一個月內 ", "部長允諾"), ("丙", "   ", ""),
                        ("丁", "儘快", "")])
        self.assertEqual([(a.index, a.request, a.deadline, a.response) for a in asks_with_deadline(brief)],
                         [(1, "乙", "一個月內", "部長允諾"), (3, "丁", "儘快", "")])
        self.assertEqual(ask_at(brief, 0).request, "甲")
        self.assertIsNone(ask_at(brief, 9))

    def test_a_malformed_brief_has_no_asks(self):
        for brief in (None, [], {"asks": "x"}, {"asks": [None, 3, {"request": 5, "deadline": "一週內"}]}):
            with self.subTest(brief=brief):
                self.assertEqual(asks_with_deadline(brief), [])


class BigramTests(SimpleTestCase):
    def test_character_pairs_inside_runs_of_chinese(self):
        self.assertEqual(bigrams("交機時程"), {"交機", "機時", "時程"})
        # 標點兩邊不是一個詞
        self.assertEqual(bigrams("交機，時程"), {"交機", "時程"})

    def test_stop_words_and_stop_characters_are_dropped(self):
        self.assertEqual(bigrams("請問部長的清冊"), {"問部", "清冊"})

    def test_latin_words_are_lowercased_and_full_width_is_folded(self):
        self.assertEqual(bigrams("ＴＰＡＳＳ 與 F16 跟 A"), {"tpass", "f16"})


class ExcerptTests(SimpleTestCase):
    def test_a_short_transcript_is_sent_whole(self):
        self.assertEqual(excerpt_for(REQUEST, "短的逐字稿"), "短的逐字稿")

    def test_the_window_with_the_most_request_bigrams_is_picked(self):
        filler = "今天天氣很好大家辛苦了。" * 300
        hot = "部長，無人機交機時程清冊到底在哪裡？"
        transcript = filler + "無人機" + filler + hot + filler
        excerpt = excerpt_for(REQUEST, transcript)
        self.assertEqual(len(excerpt), EXCERPT_LIMIT)
        self.assertIn(hot, excerpt)
        # 置中：前後都有上下文
        self.assertGreater(excerpt.index(hot), 500)

    def test_without_any_shared_bigram_the_excerpt_is_the_beginning(self):
        transcript = "甲乙丙丁" * 1000
        self.assertEqual(excerpt_for(REQUEST, transcript), transcript[:EXCERPT_LIMIT])

    def test_a_latin_word_that_passed_the_prefilter_is_in_the_excerpt(self):
        """攔的 bug：篩選靠「TPASS」通過的一對，片段只比中文雙字組、而且不做 NFKC 與小寫，
        找不到它就給逐字稿開頭——模型讀到的是沒講這件事的那一段，只能判成沒有追問。"""
        filler = "今天天氣很好大家辛苦了。" * 300
        request = "延長TPASS"
        for hot in ("部長，ＴＰＡＳＳ月票的補貼到底延不延？", "部長，Tpass月票的補貼到底延不延？"):
            with self.subTest(hot=hot):
                transcript = filler + hot + filler
                # 篩選認得這個詞：兩邊共有的只有它
                self.assertEqual(bigrams(request) & bigrams(transcript), {"tpass"})
                excerpt = excerpt_for(request, transcript)
                self.assertEqual(len(excerpt), EXCERPT_LIMIT)
                self.assertIn(hot, excerpt)

    def test_positions_stay_on_the_original_text_when_normalizing_changes_lengths(self):
        """NFKC 把「㍿」變成四個字：位置要對回原文，片段才會落在要求詞上、而且是原文的一段。"""
        filler = "㍿" * 2000
        hot = "部長，無人機交機時程清冊到底在哪裡？"
        transcript = filler + hot + filler
        excerpt = excerpt_for(REQUEST, transcript)
        self.assertEqual(len(excerpt), EXCERPT_LIMIT)
        self.assertIn(hot, excerpt)
        self.assertIn(excerpt, transcript)


class CardTests(TestCase):
    def test_the_card_is_the_one_liner_its_own_asks_and_the_slide_titles(self):
        article = _article(one_liner="交機進度落後", asks=[("補足預算", "", "")], titles=("交機進度", " "))
        self.assertEqual(card_for(article), "一句話：交機進度落後\n要求：\n- 補足預算\n各段小標：\n- 交機進度")

    def test_an_oversized_card_drops_titles_first_and_keeps_the_one_liner(self):
        article = _article(one_liner="一句話", asks=[("要求", "", "")],
                           titles=[f"{i:03d}" + "長" * 290 for i in range(20)])
        card = card_for(article)
        self.assertLessEqual(len(card), 4000)
        self.assertTrue(card.startswith("一句話：一句話\n要求：\n- 要求\n各段小標：\n- 000"))
        self.assertNotIn("019", card)


class GroundingTests(SimpleTestCase):
    TRANSCRIPT = "00:00 王立 部長，交機時程清冊，到現在還沒給！\n00:30 部長 會盡快。"

    def test_punctuation_and_spaces_do_not_matter(self):
        self.assertTrue(grounded("交機時程清冊到現在還沒給", self.TRANSCRIPT))
        self.assertTrue(grounded("交機 時程 清冊、到現在", self.TRANSCRIPT))
        self.assertTrue(grounded("ＡＢＣＤＥＦ", "abc ABCDEF"))

    def test_too_short_or_not_in_the_transcript_is_not_grounded(self):
        self.assertFalse(grounded("時程清冊到", self.TRANSCRIPT))          # 5 個字
        self.assertFalse(grounded("交機時程清冊已經交了", self.TRANSCRIPT))  # 編的
        self.assertFalse(grounded("", self.TRANSCRIPT))


class ParseResultTests(SimpleTestCase):
    def test_a_good_result(self):
        result = parse_result({"followed_up": True, "quote": " 引用 ", "classifier": f" {CLASSIFIER} "})
        self.assertEqual((result.followed_up, result.quote, result.classifier), (True, "引用", CLASSIFIER))

    def test_no_follow_up_drops_the_quote(self):
        self.assertEqual(parse_result({"followed_up": False, "quote": "x", "classifier": "m"}).quote, "")
        self.assertEqual(parse_result({"followed_up": True, "quote": None, "classifier": "m"}).quote, "")

    def test_a_malformed_result_fails_rather_than_guessing(self):
        for result in (None, [], {"followed_up": "true", "classifier": "m"},
                       {"followed_up": 1, "classifier": "m"},
                       {"followed_up": True, "quote": 3, "classifier": "m"},
                       {"followed_up": True, "quote": "q"}, {"followed_up": True, "classifier": " "}):
            with self.subTest(result=result), self.assertRaises(JobFailed):
                parse_result(result)


# --- 狀態 ---


class StateBoundaryTests(SimpleTestCase):
    """到期日 2026-04-02，觀察期到 2026-07-01（+90 天）。"""

    DUE = date(2026, 4, 2)
    END = date(2026, 7, 1)
    AFTER_END = _aware(2026, 7, 2, 4, 30)

    def _state(self, today, followed_by=None, classifier=CLASSIFIER, checked_at=AFTER_END,
               judge=CLASSIFIER, due=DUE):
        return state_for(due, followed_by, classifier, checked_at, today, judge)

    def test_the_four_states_by_date(self):
        day = timedelta(days=1)
        self.assertEqual(self._state(self.DUE - day), FollowUpState.PENDING)
        self.assertEqual(self._state(self.DUE), FollowUpState.PENDING)
        self.assertEqual(self._state(self.DUE + day), FollowUpState.WATCHING)
        self.assertEqual(self._state(self.END), FollowUpState.WATCHING)
        self.assertEqual(self._state(self.END + day), FollowUpState.NOT_FOLLOWED)

    def test_followed_is_followed_at_any_time(self):
        for today in (date(2026, 3, 20), self.DUE, self.END, date(2027, 1, 1)):
            with self.subTest(today=today):
                self.assertEqual(self._state(today, followed_by=7), FollowUpState.FOLLOWED)

    def test_not_followed_needs_a_full_check_after_the_window_closed(self):
        after = self.END + timedelta(days=1)
        self.assertEqual(self._state(after, checked_at=None), FollowUpState.WATCHING)
        self.assertEqual(self._state(after, checked_at=_aware(2026, 7, 1, 23, 0)), FollowUpState.WATCHING)
        self.assertEqual(self._state(after, checked_at=_aware(2026, 7, 2, 0, 5)),
                         FollowUpState.NOT_FOLLOWED)

    def test_a_request_without_candidates_needs_no_judge_verdict(self):
        """一個候選都沒有：程式篩出來的結果，classifier 是空的也算數。"""
        self.assertEqual(self._state(self.END + timedelta(days=1), classifier=""),
                         FollowUpState.NOT_FOLLOWED)

    def test_without_a_passing_judge_only_pending_is_given(self):
        for classifier in (CLASSIFIER, ""):
            with self.subTest(classifier=classifier):
                self.assertEqual(self._state(self.DUE, judge=None, classifier=classifier),
                                 FollowUpState.PENDING)
                # 不能說他追了：判斷不算數，還沒到期就照樣是待追蹤
                self.assertEqual(self._state(self.DUE, followed_by=7, judge=None, classifier=classifier),
                                 FollowUpState.PENDING)
                for today in (self.DUE + timedelta(days=1), self.END + timedelta(days=1)):
                    self.assertIsNone(self._state(today, judge=None, classifier=classifier))
                    self.assertIsNone(self._state(today, followed_by=7, judge=None,
                                                  classifier=classifier))

    def test_another_versions_verdicts_wait_for_the_live_judge(self):
        """有通過的判斷器、但這一項是別的版本判的：到期後是觀察中（等上線的判斷器看），不從清單上消失。"""
        old = "old#followup-v0#x"
        self.assertEqual(self._state(self.DUE, followed_by=7, classifier=old), FollowUpState.PENDING)
        for today in (self.DUE + timedelta(days=1), self.END + timedelta(days=1)):
            with self.subTest(today=today):
                # 舊版本說有追問、或觀察期結束後確認過沒有，都不算數
                self.assertEqual(self._state(today, classifier=old), FollowUpState.WATCHING)
                self.assertEqual(self._state(today, followed_by=7, classifier=old), FollowUpState.WATCHING)

    def test_an_unparsed_deadline_has_no_state(self):
        self.assertIsNone(self._state(self.DUE, due=None))


# --- 建立要求 ---


class SyncTests(TestCase):
    def test_every_deadline_ask_of_a_base_article_becomes_a_followup(self):
        base = _article(day="2026-03-10", asks=[("甲", "一個月內", ""), ("乙", "", ""), ("丙", "儘快", "")])
        _article("王立、甲", asks=[("聯合", "一週內", "")])
        _article(brief=False)
        _article(status=ArticleStatus.PENDING, asks=[("處理中", "一週內", "")])
        report = sync_followups()
        rows = {(f.article_id, f.ask_index): (f.request, f.deadline_text, f.due_date)
                for f in FollowUp.objects.all()}
        self.assertEqual(rows, {(base.id, 0): ("甲", "一個月內", date(2026, 4, 10)),
                                (base.id, 2): ("丙", "儘快", None)})
        self.assertEqual((report.created, report.unparsed), (2, 1))
        self.assertIn("期限無法換算 1", str(report))
        # 再跑一次什麼都沒變
        again = sync_followups()
        self.assertEqual((again.created, again.updated, again.removed), (0, 0, 0))

    def test_this_session_uses_the_articles_session(self):
        _article(day="2026-03-10", asks=[("甲", "本會期", "")])
        _article(day="2026-10-06", asks=[("乙", "本會期", "")],
                 meeting="第11屆第6會期財政委員會第1次全體委員會議")
        _article(day="2026-03-10", asks=[("丙", "本會期", "")], source="tccc", meeting="第4屆第8次定期會")
        sync_followups()
        self.assertEqual(dict(FollowUp.objects.values_list("request", "due_date")),
                         {"甲": date(2026, 5, 31), "乙": date(2026, 12, 31), "丙": None})

    def test_requests_of_articles_that_stop_being_base_are_removed(self):
        article = _article(asks=[("甲", "一個月內", "")])
        sync_followups()
        Article.objects.filter(pk=article.pk).update(speaker="王立、乙")
        self.assertEqual(sync_followups().removed, 1)
        self.assertFalse(FollowUp.objects.exists())

    def test_a_changed_request_throws_away_its_judgments(self):
        article = _article(asks=[("甲", "一個月內", "")])
        other = _article(day="2026-03-20")
        sync_followups()
        FollowUp.objects.update(checked=[other.id], followed_by=other, quote="q", classifier=CLASSIFIER,
                                checked_at=timezone.now())
        Article.objects.filter(pk=article.pk).update(brief=_brief([("乙", "一個月內", "")]))
        sync_followups()
        fu = FollowUp.objects.get()
        self.assertEqual((fu.request, fu.checked, fu.followed_by, fu.quote, fu.classifier, fu.checked_at),
                         ("乙", [], None, "", "", None))

    def test_a_changed_deadline_keeps_the_judgments_but_asks_for_a_new_check(self):
        article = _article(asks=[("甲", "一個月內", "")])
        other = _article(day="2026-03-20")
        sync_followups()
        FollowUp.objects.update(checked=[other.id], classifier=CLASSIFIER, checked_at=timezone.now())
        Article.objects.filter(pk=article.pk).update(brief=_brief([("甲", "兩個月內", "")]))
        sync_followups()
        fu = FollowUp.objects.get()
        self.assertEqual((fu.due_date, fu.checked, fu.checked_at), (date(2026, 5, 2), [other.id], None))

    def test_a_shorter_deadline_drops_a_follow_up_outside_the_new_window(self):
        """攔的 bug：期限改短、觀察期跟著縮，原本判定的追問落在新的觀察期之外還算數。"""
        article = _article(day="2026-03-02", asks=[("甲", "6個月內", "")])
        inside = _article(day="2026-03-20")
        outside = _article(day="2026-08-01")      # 6 個月內 → 觀察期到 2026-12-01；一個月內 → 2026-07-01
        sync_followups()
        FollowUp.objects.update(checked=[inside.id, outside.id], followed_by=outside, quote="q",
                                classifier=CLASSIFIER, checked_at=timezone.now())
        Article.objects.filter(pk=article.pk).update(brief=_brief([("甲", "一個月內", "")]))
        sync_followups()
        fu = FollowUp.objects.get()
        self.assertEqual((fu.due_date, fu.followed_by, fu.quote, fu.checked, fu.classifier),
                         (date(2026, 4, 2), None, "", [inside.id], CLASSIFIER))
        # 追問那篇還在新的觀察期裡：照樣算
        FollowUp.objects.update(checked=[inside.id], followed_by=inside, quote="q")
        Article.objects.filter(pk=article.pk).update(brief=_brief([("甲", "兩週內", "")]))
        sync_followups()
        self.assertEqual(FollowUp.objects.get().followed_by, inside)


# --- 候選 ---


class CandidateTests(TestCase):
    """發言 2026-03-02、期限一個月內 → 到期 2026-04-02、觀察期到 2026-07-01。"""

    def setUp(self):
        self.member = _member("王立")
        self.source = _article(day="2026-03-02", asks=[(REQUEST, "一個月內", "")], membership=self.member)
        sync_followups()
        self.followup = FollowUp.objects.select_related("article").get()

    def _candidate(self, day, one_liner="無人機交機", **kw):
        kw.setdefault("transcript", "逐字稿")
        kw.setdefault("membership", self.member)
        return _article(day=day, one_liner=one_liner, **kw)

    def _ids(self):
        return [doc.id for doc in CandidateIndex.load().candidates(self.followup)]

    def test_the_window_runs_from_the_day_after_the_speech_to_ninety_days_after_the_deadline(self):
        _ = self._candidate("2026-03-02")                 # 當天：不算
        first = self._candidate("2026-03-03")
        last = self._candidate("2026-07-01")
        _ = self._candidate("2026-07-02")                 # 觀察期過了
        _ = self._candidate("2026-02-27")                 # 之前講的
        self.assertEqual(sorted(self._ids()), sorted([first.id, last.id]))

    def test_only_the_same_persons_base_articles_with_a_transcript(self):
        kept = self._candidate("2026-03-10")
        same_person = _member("王小立", person=self.member.person)
        renamed = self._candidate("2026-03-11", speaker="王小立", membership=same_person)
        unlinked = self._candidate("2026-03-12", membership=None)   # 對不到任期，講者寫法一樣
        for kw in ({"speaker": "甲", "membership": _member("甲")},
                   {"speaker": "王立、甲"},
                   {"source": "tccc", "membership": None},
                   {"brief": False},
                   {"status": ArticleStatus.PENDING},
                   {"transcript": ""}):
            self._candidate("2026-03-13", **kw)
        self._candidate("2026-03-14", one_liner="完全不相干的長照")   # 一個雙字組都沒有重疊
        self.assertEqual(sorted(self._ids()), sorted([kept.id, renamed.id, unlinked.id]))

    def test_a_namesake_linked_to_someone_else_is_not_the_same_person(self):
        """攔的 bug：講者寫法一樣、但任期對到另一個人（同名的另一位），不是他的追問。"""
        namesake = _member("王立")                      # 另一個 Person，名字一樣
        self._candidate("2026-03-10", membership=namesake)
        mine = self._candidate("2026-03-11")
        self.assertEqual(self._ids(), [mine.id])
        # 來源自己對不到任期時，只能看講者寫法：同名的都算
        Article.objects.filter(pk=self.source.pk).update(membership=None)
        self.followup.article.refresh_from_db()
        self.assertEqual(len(self._ids()), 2)

    def test_the_top_three_by_overlap_earlier_first_on_ties(self):
        best = self._candidate("2026-05-01", one_liner="無人機交機時程清冊")
        tie_early = self._candidate("2026-03-20", one_liner="無人機交機")
        tie_late = self._candidate("2026-03-25", one_liner="無人機交機")
        self._candidate("2026-03-05", one_liner="無人機")
        self.assertEqual(self._ids(), [best.id, tie_early.id, tie_late.id])

    def test_an_unparsed_deadline_has_no_candidates(self):
        _article(day="2026-03-02", asks=[(REQUEST, "儘快", "")], membership=self.member)
        self._candidate("2026-03-10")
        sync_followups()
        vague = FollowUp.objects.select_related("article").get(deadline_text="儘快")
        self.assertEqual(CandidateIndex.load().candidates(vague), [])


# --- 每晚判斷 ---

FOLLOWED_TRANSCRIPT = "00:00 王立 部長，我再問一次「交機時程清冊到現在還沒給」，什麼時候給？"


class CheckTests(TestCase):
    """王立 2026-03-02 要求「提出無人機交機時程清冊」，一個月內（到期 2026-04-02）。"""

    LOGGER = "articles.followups"
    NOW = _aware(2026, 5, 1, 4, 30)

    def setUp(self):
        self.member = _member("王立")
        self.source = _article(day="2026-03-02", asks=[(REQUEST, "一個月內", "部長允諾")],
                               membership=self.member)

    def _candidate(self, day, one_liner, followed=False, **kw):
        transcript = FOLLOWED_TRANSCRIPT if followed else "00:00 王立 今天問別的事情。"
        return _article(day=day, one_liner=one_liner, transcript=transcript, membership=self.member, **kw)

    def _run(self, gpu=None, limit=10, **kw):
        gpu = gpu or FakeFollowupGpu()
        return gpu, check_followups(gpu, limit=limit, now=kw.pop("now", self.NOW), **kw)

    def test_candidates_are_judged_by_overlap_and_the_first_follow_up_stops(self):
        best = self._candidate("2026-03-20", "無人機交機時程清冊沒下文")                  # 重疊最高，沒追
        second = self._candidate("2026-04-10", "無人機交機進度", followed=True, titles=("交機",))
        self._candidate("2026-04-20", "無人機採購", followed=True)                        # 不必再判
        gpu, report = self._run()
        self.assertEqual([pair["card"].splitlines()[0] for pair in gpu.pairs],
                         ["一句話：無人機交機時程清冊沒下文", "一句話：無人機交機進度"])
        self.assertEqual(gpu.pairs[1], {"request": REQUEST, "response": "部長允諾",
                                        "card": "一句話：無人機交機進度\n各段小標：\n- 交機",
                                        "excerpt": FOLLOWED_TRANSCRIPT})
        fu = FollowUp.objects.get()
        self.assertEqual((fu.followed_by_id, fu.quote, fu.classifier, fu.checked),
                         (second.id, "交機時程清冊到現在還沒給", CLASSIFIER, [best.id, second.id]))
        self.assertEqual(fu.checked_at, self.NOW)
        self.assertEqual((report.judged, report.found, report.failed, report.remaining), (2, 1, 0, 0))
        self.assertIn("判斷完成 2（找到追問 1）", str(report))

    def test_each_pair_is_judged_once_and_only_new_candidates_later(self):
        first = self._candidate("2026-03-20", "無人機交機")
        gpu, _ = self._run()
        self.assertEqual(len(gpu.pairs), 1)
        gpu, _ = self._run()
        self.assertEqual(gpu.pairs, [])
        newer = self._candidate("2026-04-15", "無人機交機時程", followed=True)
        gpu, _ = self._run()
        self.assertEqual(len(gpu.pairs), 1)
        fu = FollowUp.objects.get()
        self.assertEqual((fu.checked, fu.followed_by_id), ([first.id, newer.id], newer.id))
        # 找到之後就不再判
        self._candidate("2026-04-16", "無人機交機時程清冊", followed=True)
        gpu, _ = self._run()
        self.assertEqual(gpu.pairs, [])

    def test_a_quote_that_is_not_in_the_transcript_is_no_follow_up(self):
        self._candidate("2026-03-20", "無人機交機", followed=True)
        gpu = FakeFollowupGpu(answer=lambda pair: (True, "交機時程清冊已經全部交了"))
        with self.assertLogs(self.LOGGER, "INFO"):
            _, report = self._run(gpu)
        fu = FollowUp.objects.get()
        self.assertIsNone(fu.followed_by_id)
        self.assertEqual((report.found, report.ungrounded, len(fu.checked)), (0, 1, 1))
        self.assertIn("引用對不上逐字稿當成沒有追問 1", str(report))

    def test_the_limit_is_kept_and_the_backlog_reported(self):
        for i in range(3):
            self._candidate(f"2026-03-2{i}", "無人機交機")
        _article(day="2026-03-05", asks=[("無人機交機的預算", "兩週內", "")], membership=self.member)
        gpu, report = self._run(limit=2)
        self.assertEqual(len(gpu.pairs), 2)
        self.assertEqual(report.remaining, 2)
        self.assertIn("還有候選沒判斷的要求 2", str(report))
        self.assertFalse(FollowUp.objects.filter(checked_at__isnull=False).exists())

    def test_unparsed_deadlines_are_never_sent(self):
        Article.objects.filter(pk=self.source.pk).update(brief=_brief([(REQUEST, "儘快", "")]))
        self._candidate("2026-03-20", "無人機交機")
        gpu, report = self._run()
        self.assertEqual(gpu.pairs, [])
        self.assertEqual(report.sync.unparsed, 1)

    def test_an_unavailable_gpu_stops_the_whole_round(self):
        for i in range(3):
            self._candidate(f"2026-03-2{i}", "無人機交機")
        gpu = FakeFollowupGpu(answer=lambda pair: GpuApiError("連線被拒"))
        with self.assertLogs(self.LOGGER, "WARNING"):
            _, report = self._run(gpu)
        self.assertEqual(len(gpu.pairs), 1)
        self.assertTrue(report.stopped)
        self.assertEqual((report.failed, report.remaining), (1, 1))
        self.assertIn("GPU 不可用", str(report))
        self.assertEqual(FollowUp.objects.get().checked, [])

    def test_a_run_whose_first_jobs_are_all_rejected_stops_early(self):
        """GPU 端還沒更新時每個都回 422：送三個都被拒就停，不要同一個錯誤重複兩百次。"""
        for i in range(3):
            _article(day=f"2026-03-0{i + 3}", asks=[(f"無人機交機第{i}批", "一個月內", "")],
                     membership=self.member)
        for i in range(3):
            self._candidate(f"2026-03-2{i}", "無人機交機")
        gpu = FakeFollowupGpu(answer=lambda pair: JobFailed("摘要 API 拒絕這個請求（422）"))
        with self.assertLogs(self.LOGGER, "WARNING"):
            _, report = self._run(gpu)
        self.assertEqual((report.failed, report.stopped), (3, True))
        self.assertIn("GPU 端可能還沒更新", str(report))

    def test_failures_after_a_success_do_not_stop_the_run(self):
        good = self._candidate("2026-03-20", "無人機交機時程清冊")
        for i in range(4):
            self._candidate(f"2026-04-0{i + 1}", "無人機交機")
        _article(day="2026-03-03", asks=[("無人機交機的預算", "一個月內", "")], membership=self.member)
        gpu = FakeFollowupGpu(answer=lambda pair: (False, "") if "時程清冊" in pair["card"]
                              else JobFailed("壞"))
        with self.assertLogs(self.LOGGER, "WARNING"):
            _, report = self._run(gpu)
        self.assertFalse(report.stopped)
        self.assertEqual(report.judged, 2)          # 兩項要求都判了 good 那篇
        self.assertGreaterEqual(report.failed, 3)
        self.assertTrue(all(good.id in fu.checked for fu in FollowUp.objects.all()))

    def test_a_pair_that_cannot_be_saved_does_not_stop_the_others(self):
        self._candidate("2026-03-20", "無人機交機時程清冊")
        self._candidate("2026-03-21", "無人機交機")
        save = FollowUp.save
        calls = []

        def flaky(followup, *args, **kwargs):
            calls.append(followup.pk)
            if len(calls) == 1:
                raise OperationalError("database is locked")
            return save(followup, *args, **kwargs)

        with mock.patch.object(FollowUp, "save", flaky), self.assertLogs(self.LOGGER, "WARNING"):
            _, report = self._run()
        self.assertEqual((report.judged, report.failed, report.stopped), (1, 1, False))
        self.assertIn("database is locked", report.errors[0])

    def test_a_result_for_text_that_changed_while_waiting_is_not_saved(self):
        """攔的 bug：等 GPU 的時候後來那篇被重產，舊內容的判斷存到新內容上，之後也不會再判。"""
        candidate = self._candidate("2026-03-20", "無人機交機", followed=True)
        gpu = FakeFollowupGpu()
        original = gpu.wait

        def wait(job_id, timeout, poll_seconds=3.0, on_progress=None):
            Article.objects.filter(pk=candidate.pk).update(brief=_brief(one_liner="無人機交機新的一句話"))
            return original(job_id, timeout)

        gpu.wait = wait
        _, report = self._run(gpu)
        self.assertEqual((report.judged, report.stale, report.remaining), (0, 1, 1))
        fu = FollowUp.objects.get()
        self.assertEqual((fu.checked, fu.followed_by_id), ([], None))
        # 下一輪用新內容重判
        gpu, _ = self._run()
        self.assertEqual(gpu.pairs[0]["card"], "一句話：無人機交機新的一句話")
        self.assertEqual(FollowUp.objects.get().followed_by_id, candidate.id)

    def test_a_new_judge_starts_the_request_over(self):
        """一項要求的判斷永遠出自同一個判斷器：換了就整項重來。"""
        first = self._candidate("2026-03-20", "無人機交機時程清冊")
        self._run(FakeFollowupGpu(classifier="old#followup-v1#x"))
        self.assertEqual(FollowUp.objects.get().checked, [first.id])
        second = self._candidate("2026-04-20", "無人機交機")
        self._run(FakeFollowupGpu(classifier="new#followup-v2#y"))
        fu = FollowUp.objects.get()
        self.assertEqual((fu.classifier, fu.checked, fu.checked_at), ("new#followup-v2#y", [second.id], None))
        # 下一輪補判被清掉的那篇
        gpu, _ = self._run(FakeFollowupGpu(classifier="new#followup-v2#y"))
        self.assertEqual(len(gpu.pairs), 1)
        self.assertEqual(sorted(FollowUp.objects.get().checked), sorted([first.id, second.id]))

    def test_recheck_starts_over_the_requests_of_other_judges(self):
        self._candidate("2026-03-20", "無人機交機", followed=True)
        self._run(FakeFollowupGpu(classifier="old#followup-v1#x"))
        FollowUpEvaluation.objects.create(classifier=CLASSIFIER, labeled=30, correct=30, accuracy=1.0,
                                          passed=True, ran_at=timezone.now())
        gpu, report = self._run(recheck=True)
        self.assertEqual((report.reset, len(gpu.pairs)), (1, 1))
        self.assertEqual(FollowUp.objects.get().classifier, CLASSIFIER)
        self.assertIn("換判斷器清掉重判 1 項", str(report))
        # 判斷器自己判的不重判
        gpu, report = self._run(recheck=True)
        self.assertEqual((report.reset, gpu.pairs), (0, []))

    def test_a_request_is_marked_checked_again_after_its_window_closes(self):
        """未追問要靠觀察期結束之後的確認：期間記過的不算數，結束後的那一輪要再記一次。"""
        self._candidate("2026-03-20", "無人機交機")
        self._run()
        self.assertEqual(FollowUp.objects.get().checked_at, self.NOW)
        later = _aware(2026, 6, 1, 4, 30)
        self._run(now=later)
        self.assertEqual(FollowUp.objects.get().checked_at, self.NOW)    # 觀察期內不必重記
        after = _aware(2026, 7, 2, 4, 30)
        gpu, _ = self._run(now=after)
        self.assertEqual(gpu.pairs, [])
        fu = FollowUp.objects.get()
        self.assertEqual(fu.checked_at, after)
        self.assertEqual(state_for(fu.due_date, fu.followed_by_id, fu.classifier, fu.checked_at,
                                   date(2026, 7, 2), CLASSIFIER), FollowUpState.NOT_FOLLOWED)

    def test_a_request_without_any_candidate_is_complete(self):
        _, report = self._run()
        fu = FollowUp.objects.get()
        self.assertEqual((fu.classifier, fu.checked, fu.checked_at, report.remaining), ("", [], self.NOW, 0))

    def _state_on(self, day):
        fu = FollowUp.objects.get()
        return state_for(fu.due_date, fu.followed_by_id, fu.classifier, fu.checked_at, day, CLASSIFIER)

    def test_not_followed_waits_for_his_unfinished_speeches_in_the_window(self):
        """攔的 bug：觀察期結束那一晚，他觀察期裡的質詢還在排隊產摘要，候選判完了就宣告未追問。"""
        self._candidate("2026-03-20", "無人機交機")
        after = _aware(2026, 7, 2, 4, 30)
        queued = _article(day="2026-07-01", status=ArticleStatus.PENDING, brief=False, membership=self.member)
        # 不算數的：觀察期外、發言當天、別人、聯合質詢、失敗到不再重試
        _article(day="2026-07-02", status=ArticleStatus.PENDING, brief=False)
        _article(day="2026-03-02", status=ArticleStatus.PENDING, brief=False)
        _article(speaker="甲", day="2026-06-30", status=ArticleStatus.PENDING, brief=False)
        _article(speaker="王立、甲", day="2026-06-30", status=ArticleStatus.PROCESSING, brief=False)
        gave_up = _article(day="2026-06-29", status=ArticleStatus.FAILED, brief=False)
        Article.objects.filter(pk=gave_up.pk).update(attempts=5)
        for status, attempts in ((ArticleStatus.PENDING, 0), (ArticleStatus.PROCESSING, 1),
                                 (ArticleStatus.FAILED, 4)):
            with self.subTest(status=status):
                Article.objects.filter(pk=queued.pk).update(status=status, attempts=attempts)
                _, report = self._run(now=after)
                self.assertIsNone(FollowUp.objects.get().checked_at)
                self.assertEqual(report.waiting, 1)
                self.assertIn("觀察期裡還有報導沒做完、先不算未追問的要求 1", str(report))
                self.assertEqual(self._state_on(after.date()), FollowUpState.WATCHING)
        # 失敗到不再重試：永遠不會變成候選，不必再等
        Article.objects.filter(pk=queued.pk).update(status=ArticleStatus.FAILED, attempts=5)
        _, report = self._run(now=after)
        self.assertEqual((FollowUp.objects.get().checked_at, report.waiting), (after, 0))
        self.assertEqual(self._state_on(after.date()), FollowUpState.NOT_FOLLOWED)

    def test_his_other_names_count_and_a_late_import_takes_not_followed_back(self):
        """同一個人換了寫法的名字也算；已經宣告未追問之後才補匯入觀察期裡的質詢，要收回來等它做完。"""
        self._candidate("2026-03-20", "無人機交機")
        after = _aware(2026, 7, 2, 4, 30)
        self._run(now=after)
        self.assertEqual(self._state_on(after.date()), FollowUpState.NOT_FOLLOWED)
        renamed = _member("王小立", person=self.member.person)
        _article(speaker="王小立", day="2026-05-01", status=ArticleStatus.PENDING, brief=False,
                 membership=renamed)
        later = _aware(2026, 7, 3, 4, 30)
        _, report = self._run(now=later)
        self.assertEqual(report.waiting, 1)
        self.assertIsNone(FollowUp.objects.get().checked_at)
        self.assertEqual(self._state_on(later.date()), FollowUpState.WATCHING)


# --- 重產時的清理 ---


class RegenerationTests(TestCase):
    def setUp(self):
        self.member = _member("王立")
        self.source = _article(day="2026-03-02", asks=[(REQUEST, "一個月內", "")], membership=self.member)
        self.candidate = _article(day="2026-03-20", one_liner="無人機交機", transcript=FOLLOWED_TRANSCRIPT,
                                  membership=self.member)
        check_followups(FakeFollowupGpu(), limit=10)
        self.assertEqual(FollowUp.objects.get().followed_by_id, self.candidate.id)

    def test_regenerating_the_source_drops_its_requests_but_keeps_the_labels(self):
        FollowUpLabel.objects.create(article=self.source, ask_index=0, request=REQUEST,
                                     candidate=self.candidate, followed=True)
        save_result(self.source, {"title": "t", "slides": [],
                                  "brief": _brief([("新的要求", "兩週內", "")])})
        self.assertFalse(FollowUp.objects.exists())
        self.assertTrue(FollowUpLabel.objects.get().followed)
        sync_followups()
        self.assertEqual(FollowUp.objects.get().request, "新的要求")

    def test_regenerating_a_candidate_takes_it_out_of_checked_and_followed_by(self):
        save_result(self.candidate, {"title": "t", "slides": [], "transcript_text": FOLLOWED_TRANSCRIPT,
                                     "brief": _brief(one_liner="無人機交機重寫過")})
        fu = FollowUp.objects.get()
        self.assertEqual((fu.checked, fu.followed_by_id, fu.quote, fu.checked_at), ([], None, "", None))
        # 下一輪用新內容重判
        gpu = FakeFollowupGpu()
        check_followups(gpu, limit=10)
        self.assertEqual(gpu.pairs[0]["card"], "一句話：無人機交機重寫過")
        self.assertEqual(FollowUp.objects.get().followed_by_id, self.candidate.id)


# --- 標註集 ---


class SampleTests(TestCase):
    def setUp(self):
        self.member = _member("王立")
        # 12 項要求，每項有 3 篇候選（重疊不同）；另外一項沒有候選、一項期限換不出來
        for i in range(12):
            _article(day="2026-03-02", asks=[(f"無人機交機時程清冊第{i}批", "一個月內", "")],
                     membership=self.member)
        for day, one_liner in (("2026-03-10", "無人機交機時程清冊"), ("2026-03-11", "無人機交機"),
                               ("2026-03-12", "無人機")):
            _article(day=day, one_liner=one_liner, transcript="逐字稿", membership=self.member)
        _article(day="2026-03-02", asks=[("長照", "一個月內", "")], membership=self.member)
        _article(day="2026-03-02", asks=[("無人機交機", "儘快", "")], membership=self.member)

    def _drawn(self):
        return {(label.article_id, label.ask_index, label.candidate_id)
                for label in FollowUpLabel.objects.all()}

    def test_half_take_the_top_candidate_and_half_a_random_one(self):
        report = sample_labels(pairs=10, seed=0)
        self.assertEqual((report.added, report.total, report.available), (10, 10, 12))
        labels = list(FollowUpLabel.objects.order_by("id"))
        top = Article.objects.get(brief__one_liner="無人機交機時程清冊")
        # 至少一半是最高的那篇（隨機的那一半也可能抽到它）
        self.assertGreaterEqual(sum(label.candidate_id == top.id for label in labels), 5)
        index = CandidateIndex.load()
        for label in labels:
            fu = FollowUp.objects.select_related("article").get(article=label.article,
                                                                ask_index=label.ask_index)
            self.assertIn(label.candidate_id, [doc.id for doc in index.candidates(fu)])
            self.assertEqual((label.request, label.followed), (fu.request, None))
        # 隨機的那一半不全是最高的那篇（種子 0 的結果，固定可重現）
        self.assertNotEqual({label.candidate_id for label in labels}, {top.id})
        self.assertEqual(len({(label.article_id, label.ask_index) for label in labels}), 10)

    def test_the_admin_order_does_not_give_away_which_half_a_pair_came_from(self):
        """攔的 bug：照抽的順序建，admin 前一半全是重疊最高的那篇（多半有追問），看位置就猜得到答案。"""
        sample_labels(pairs=10, seed=0)
        labels = list(FollowUpLabel.objects.order_by("id"))
        self.assertEqual([(label.article_id, label.ask_index) for label in labels],
                         sorted((label.article_id, label.ask_index) for label in labels))
        top = Article.objects.get(brief__one_liner="無人機交機時程清冊").id
        self.assertNotEqual([label.candidate_id == top for label in labels],
                            sorted((label.candidate_id == top for label in labels), reverse=True))

    def test_the_same_seed_draws_the_same_pairs(self):
        sample_labels(pairs=6, seed=0)
        first = self._drawn()
        FollowUpLabel.objects.all().delete()
        sample_labels(pairs=6, seed=0)
        self.assertEqual(self._drawn(), first)
        FollowUpLabel.objects.all().delete()
        sample_labels(pairs=6, seed=1)
        self.assertNotEqual(self._drawn(), first)

    def test_running_again_only_tops_up_and_never_redraws(self):
        sample_labels(pairs=4)
        first = self._drawn()
        FollowUpLabel.objects.filter(pk=FollowUpLabel.objects.first().pk).update(followed=True)
        report = sample_labels(pairs=7)
        self.assertEqual((report.added, report.total), (3, 7))
        self.assertTrue(first <= self._drawn())
        self.assertEqual(FollowUpLabel.objects.filter(followed=True).count(), 1)
        self.assertEqual(sample_labels(pairs=7).added, 0)
        # 要求抽完了就停在能抽的數量
        self.assertEqual(sample_labels(pairs=100).total, 12)


# --- 評估 ---


class EvaluateTests(TestCase):
    LOGGER = "articles.followups"
    WHEN = _aware(2026, 10, 3, 9, 30)

    def setUp(self):
        self.member = _member("王立")
        self.count = itertools.count()

    def _labelled(self, count, human=True, model=True):
        """count 對：人工標 human；model=True 的那篇逐字稿裡有引用（假判斷器會說有追問）。"""
        made = []
        for _ in range(count):
            i = next(self.count)
            request = f"提出第{i}批無人機交機清冊"
            source = _article(day="2026-03-02", asks=[(request, "一個月內", "部長允諾")],
                              membership=self.member)
            candidate = _article(day="2026-03-20", one_liner="無人機交機",
                                 transcript=FOLLOWED_TRANSCRIPT if model else "沒有提到", membership=self.member)
            made.append(FollowUpLabel.objects.create(article=source, ask_index=0, request=request,
                                                     candidate=candidate, followed=human))
        return made

    def test_accuracy_is_matches_over_labels_and_26_of_30_passes(self):
        self._labelled(20)
        self._labelled(6, human=False, model=False)
        wrong = self._labelled(4, human=False, model=True)
        report = evaluate(FakeFollowupGpu(), now=self.WHEN)
        ev = FollowUpEvaluation.objects.get()
        self.assertEqual((ev.classifier, ev.labeled, ev.correct, ev.passed, ev.ran_at),
                         (CLASSIFIER, 30, 26, True, self.WHEN))
        self.assertAlmostEqual(ev.accuracy, 26 / 30)
        self.assertEqual(ev.mistakes[0], {
            "label": wrong[0].pk, "article": wrong[0].article.slug, "ask_index": 0,
            "candidate": wrong[0].candidate.slug, "speaker": "王立", "request": wrong[0].request,
            "human": False, "model": True, "quote": "交機時程清冊到現在還沒給"})
        text = str(report)
        self.assertIn("準確率 86.7%：通過", text)
        self.assertIn(f"判錯：{wrong[0].article.slug} 第 1 項 → {wrong[0].candidate.slug}"
                      "（王立）：人工「沒有追問」、模型「有追問」", text)

    def test_below_85_percent_does_not_pass(self):
        self._labelled(25)
        self._labelled(5, human=True, model=False)
        report = evaluate(FakeFollowupGpu())
        self.assertEqual((report.evaluation.correct, report.evaluation.passed), (25, False))
        self.assertIn("未通過（準確率未達 85%）", str(report))

    def test_too_few_pairs_do_not_pass_even_when_all_are_right(self):
        self._labelled(FOLLOWUP_MIN_LABELS - 1)
        report = evaluate(FakeFollowupGpu())
        self.assertEqual((report.evaluation.accuracy, report.evaluation.passed), (1.0, False))
        self.assertIn("標註不足 30 對", str(report))

    def test_the_grounding_check_is_part_of_the_judge(self):
        """模型說有追問、引用卻是編的：算它說「沒有追問」。"""
        labels = self._labelled(2, human=True, model=True)
        gpu = FakeFollowupGpu(answer=lambda pair: (True, "交機時程清冊已經全部交了"))
        report = evaluate(gpu)
        self.assertEqual(report.evaluation.correct, 0)
        self.assertEqual(report.evaluation.mistakes[0]["model"], False)
        self.assertTrue(report.evaluation.mistakes[0]["ungrounded"])
        self.assertIn("沒有追問（說有追問，但引用對不上逐字稿）", str(report))
        self.assertEqual(len(labels), 2)

    def test_a_failed_judgment_counts_as_wrong(self):
        self._labelled(2)
        broken = self._labelled(1, human=False, model=False)[0]
        gpu = FakeFollowupGpu(answer=lambda pair: JobFailed("JSON 解析失敗") if "沒有提到" in pair["excerpt"]
                              else _says_followed(pair))
        with self.assertLogs(self.LOGGER, "WARNING"):
            report = evaluate(gpu)
        ev = report.evaluation
        self.assertEqual((ev.labeled, ev.correct), (3, 2))
        self.assertEqual((ev.mistakes[0]["label"], ev.mistakes[0]["model"], ev.mistakes[0]["error"]),
                         (broken.pk, None, "JSON 解析失敗"))
        self.assertIn("模型「判斷失敗：JSON 解析失敗」", str(report))

    def test_unlabelled_and_stale_pairs_are_not_sent(self):
        kept = self._labelled(2)
        unlabelled = self._labelled(1)[0]
        FollowUpLabel.objects.filter(pk=unlabelled.pk).update(followed=None)
        changed = self._labelled(1)[0]
        Article.objects.filter(pk=changed.article_id).update(brief=_brief([("別的要求", "一週內", "")]))
        joint = self._labelled(1)[0]
        Article.objects.filter(pk=joint.candidate_id).update(speaker="王立、甲")
        gpu = FakeFollowupGpu()
        report = evaluate(gpu)
        self.assertEqual([pair["request"] for pair in gpu.pairs], [label.request for label in kept])
        self.assertEqual((report.skipped, report.evaluation.labeled), (2, 2))
        self.assertIn("略過 2 對", str(report))

    def test_a_judge_change_midway_aborts_and_saves_nothing(self):
        self._labelled(10)
        gpu = FakeFollowupGpu(classifier=lambda pair: "old#followup-v1#x" if len(gpu.pairs) < 5
                              else "new#followup-v1#x")
        with self.assertRaises(EvaluationAborted) as ctx:
            evaluate(gpu)
        self.assertIn("中途換了模型", str(ctx.exception))
        self.assertEqual(len(gpu.pairs), 5)
        self.assertFalse(FollowUpEvaluation.objects.exists())

    def test_an_unavailable_gpu_saves_nothing(self):
        self._labelled(3)
        with self.assertRaises(GpuApiError):
            evaluate(FakeFollowupGpu(answer=lambda pair: GpuApiError("連線被拒")))
        self.assertFalse(FollowUpEvaluation.objects.exists())

    def test_nothing_labelled_or_nothing_judged_aborts(self):
        with self.assertRaises(EvaluationAborted):
            evaluate(FakeFollowupGpu())
        self._labelled(3)
        with self.assertLogs(self.LOGGER, "WARNING"), self.assertRaises(EvaluationAborted) as ctx:
            evaluate(FakeFollowupGpu(answer=lambda pair: JobFailed("摘要 API 拒絕這個請求（422）")))
        self.assertIn("422", str(ctx.exception))
        self.assertFalse(FollowUpEvaluation.objects.exists())


class GateTests(TestCase):
    """規則同 topics.passing_evaluations：每個判斷器只看自己最新的一次評估。"""

    def _ev(self, classifier, passed, day):
        return FollowUpEvaluation.objects.create(
            classifier=classifier, labeled=30, correct=28 if passed else 20,
            accuracy=28 / 30 if passed else 20 / 30, passed=passed, ran_at=_aware(2026, 10, day))

    def test_no_passing_evaluation_means_no_judge(self):
        self._ev("a#followup-v1#x", False, 1)
        self.assertIsNone(passing_evaluation())
        self.assertIsNone(passing_judge())

    def test_the_latest_passing_judge_wins_and_a_new_failure_does_not_revoke_it(self):
        self._ev("a#followup-v1#x", True, 1)
        newer = self._ev("b#followup-v1#x", True, 2)
        self._ev("c#followup-v1#x", False, 3)
        self.assertEqual(passing_evaluation(), newer)

    def test_its_own_newer_failure_takes_it_offline_and_falls_back(self):
        self._ev("old#followup-v1#x", True, 1)
        self._ev("a#followup-v1#x", True, 2)
        self._ev("a#followup-v1#x", False, 3)
        self.assertEqual(passing_judge(), "old#followup-v1#x")
        self._ev("old#followup-v1#x", False, 4)
        self.assertIsNone(passing_judge())
        self._ev("a#followup-v1#x", True, 5)
        self.assertEqual(passing_judge(), "a#followup-v1#x")


# --- 指令 ---


class CommandTests(TestCase):
    def setUp(self):
        self.member = _member("王立")

    def _gpu(self, module, gpu):
        return mock.patch(f"articles.management.commands.{module}.GpuApiClient", return_value=gpu)

    def test_check_followups_prints_the_report_and_keeps_the_limit(self):
        _article(day="2026-03-02", asks=[(REQUEST, "一個月內", "")], membership=self.member)
        for i in range(3):
            _article(day=f"2026-03-2{i}", one_liner="無人機交機", transcript="x", membership=self.member)
        gpu = FakeFollowupGpu()
        out = io.StringIO()
        with self._gpu("check_followups", gpu):
            call_command("check_followups", limit=2, stdout=out)
        self.assertEqual(len(gpu.pairs), 2)
        self.assertIn("要求新增 1", out.getvalue())
        self.assertIn("判斷完成 2", out.getvalue())

    def test_recheck_needs_a_passing_judge(self):
        with self._gpu("check_followups", FakeFollowupGpu()), self.assertRaises(CommandError):
            call_command("check_followups", "--recheck", stdout=io.StringIO())

    def test_sample_followup_labels(self):
        _article(day="2026-03-02", asks=[(REQUEST, "一個月內", "")], membership=self.member)
        _article(day="2026-03-20", one_liner="無人機交機", transcript="x", membership=self.member)
        out = io.StringIO()
        call_command("sample_followup_labels", "--pairs", "5", "--seed", "3", stdout=out)
        self.assertEqual(FollowUpLabel.objects.count(), 1)
        self.assertIn("新抽 1 對", out.getvalue())
        with self.assertRaises(CommandError):
            call_command("sample_followup_labels", "--pairs", "0", stdout=io.StringIO())

    def test_eval_followups_prints_the_result_and_recomputes_profiles_when_the_gate_changes(self):
        for i in range(FOLLOWUP_MIN_LABELS):
            request = f"提出第{i}批無人機交機清冊"
            source = _article(day="2026-03-02", asks=[(request, "一個月內", "")], membership=self.member)
            candidate = _article(day="2026-03-20", one_liner="無人機交機", transcript=FOLLOWED_TRANSCRIPT,
                                 membership=self.member)
            FollowUpLabel.objects.create(article=source, ask_index=0, request=request,
                                         candidate=candidate, followed=True)
        out = io.StringIO()
        with self._gpu("eval_followups", FakeFollowupGpu()):
            call_command("eval_followups", stdout=out)
        self.assertIn("準確率 100.0%：通過", out.getvalue())
        self.assertIn("已經重算人物側寫", out.getvalue())
        self.assertEqual(set(ProfileStat.objects.filter(indicator="followup_rate")
                             .values_list("classifier", flat=True)), {CLASSIFIER})
        # 同一個判斷器再評一次：上線的版本沒變，不必重算
        out = io.StringIO()
        with self._gpu("eval_followups", FakeFollowupGpu()):
            call_command("eval_followups", stdout=out)
        self.assertNotIn("重算", out.getvalue())

    def test_eval_followups_turns_an_abort_into_a_command_error(self):
        with self._gpu("eval_followups", FakeFollowupGpu()), self.assertRaises(CommandError) as ctx:
            call_command("eval_followups", stdout=io.StringIO())
        self.assertIn("什麼都沒存", str(ctx.exception))
