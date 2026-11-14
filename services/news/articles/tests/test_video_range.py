"""影片的處理範圍（add_video --start/--end）的純規則：時間怎麼寫、什麼樣的範圍合理、跟影片長度怎麼對。

不碰資料庫與網路。指令、預檢、送出與結果核對各自的測試在 test_add_video_range.py、test_youtube_source.py、
test_gpu_client_speakers.py 與 test_speaker_result.py；這裡只管那份共用的規則。
"""
from __future__ import annotations

from types import SimpleNamespace

from django.test import SimpleTestCase

from articles.video_range import (
    MIN_RANGE_SECONDS,
    RangeInvalid,
    VideoRange,
    clock,
    fit_problem,
    parse_time,
    range_length,
    range_of,
    resolve_range,
)


class ParseTimeTests(SimpleTestCase):
    def test_seconds_minutes_and_hours_are_all_accepted(self):
        for text, seconds in (("90", 90), ("0", 0), ("1:30", 90), ("10:00", 600), ("0:05", 5), ("1:02:03", 3723),
                              ("0:00:10", 10), ("125:30", 7530), ("  45:00 ", 2700), ("7", 7)):
            with self.subTest(text=text):
                self.assertEqual(parse_time(text, "--start"), seconds)

    def test_only_whole_seconds_are_accepted_and_the_reason_says_so(self):
        """範圍存成整數秒、送出去的也是整數：「10.5」被悄悄截成 10 的話，操作者以為的跟處理的差了半秒，
        而且每個時間連結都是整數秒。寧可拒絕，說清楚。"""
        for text in ("10.5", "1:30.5", "0.0", "1e3", "１０", "٣", "1_0", "+5", "-5", "5s", "1m30s", "0x10"):
            with self.subTest(text=text):
                with self.assertRaises(RangeInvalid) as caught:
                    parse_time(text, "--start")
                self.assertIn("--start", str(caught.exception))
                self.assertIn("整數", str(caught.exception))

    def test_things_that_are_not_times_are_refused(self):
        for text in ("", "   ", ":", "1:", ":30", "1::30", "a:b", "1:2:3:4", "nan", "inf", "-", "10:-5"):
            with self.subTest(text=text):
                with self.assertRaises(RangeInvalid):
                    parse_time(text, "--end")

    def test_minutes_and_seconds_after_a_colon_are_below_sixty(self):
        """跟 GPU 的 diarize_check 一樣：第一段不封頂（逐字稿會印 125:30），之後每一段要小於 60。"""
        for text in ("1:60", "1:75", "1:60:00", "1:00:60", "0:99"):
            with self.subTest(text=text):
                with self.assertRaises(RangeInvalid) as caught:
                    parse_time(text, "--end")
                self.assertIn("60", str(caught.exception))

    def test_a_time_beyond_a_day_is_not_a_video_time(self):
        self.assertEqual(parse_time("24:00:00", "--end"), 86400)
        for text in ("24:00:01", "999999", "99999:00", "1000000"):
            with self.subTest(text=text):
                with self.assertRaises(RangeInvalid):
                    parse_time(text, "--end")

    def test_a_huge_number_cannot_become_a_slow_conversion_or_a_huge_message(self):
        with self.assertRaises(RangeInvalid) as caught:
            parse_time("9" * 5000, "--end")
        self.assertLess(len(str(caught.exception)), 200)

    def test_the_message_does_not_echo_control_characters(self):
        with self.assertRaises(RangeInvalid) as caught:
            parse_time("\x1b[2J" + "長" * 100, "--start")
        self.assertNotIn("\x1b", str(caught.exception))
        self.assertLess(len(str(caught.exception)), 200)

    def test_it_names_the_flag_it_was_given(self):
        for flag in ("--start", "--end"):
            with self.subTest(flag=flag):
                with self.assertRaises(RangeInvalid) as caught:
                    parse_time("abc", flag)
                self.assertIn(flag, str(caught.exception))


class ResolveRangeTests(SimpleTestCase):
    def test_no_flags_means_no_range(self):
        self.assertIsNone(resolve_range(None, None))

    def test_both_ends(self):
        self.assertEqual(resolve_range("10:00", "58:00"), VideoRange(600, 3480))

    def test_a_start_alone_runs_to_the_end_of_the_video(self):
        self.assertEqual(resolve_range("10:00", None), VideoRange(600, None))

    def test_an_end_alone_starts_at_the_beginning(self):
        self.assertEqual(resolve_range(None, "58:00"), VideoRange(0, 3480))

    def test_zero_start_alone_is_the_whole_video_so_it_is_no_range(self):
        """從頭到尾就是整支影片：不留一個「範圍」，之後的比對與顯示都不會把它當成處理過一段。"""
        self.assertIsNone(resolve_range("0", None))
        self.assertIsNone(resolve_range("0:00", None))

    def test_zero_start_with_an_end_is_a_real_range(self):
        self.assertEqual(resolve_range("0", "30:00"), VideoRange(0, 1800))

    def test_the_end_has_to_come_after_the_start(self):
        for start, end in (("10:00", "10:00"), ("10:00", "5:00"), ("1:00", "0")):
            with self.subTest(start=start, end=end):
                with self.assertRaises(RangeInvalid) as caught:
                    resolve_range(start, end)
                self.assertIn("--end", str(caught.exception))
                self.assertIn("--start", str(caught.exception))

    def test_a_range_shorter_than_the_minimum_is_refused(self):
        """跟 GPU 的 TimeRange 一樣的下限：太短的一段，聲音區分沒有東西可以認。"""
        self.assertEqual(MIN_RANGE_SECONDS, 5)
        for start, end in (("0", "4"), ("10:00", "10:04"), ("10:00", "10:01")):
            with self.subTest(start=start, end=end):
                with self.assertRaises(RangeInvalid) as caught:
                    resolve_range(start, end)
                self.assertIn("5 秒", str(caught.exception))
        self.assertEqual(resolve_range("10:00", "10:05"), VideoRange(600, 605))

    def test_a_bad_time_names_which_flag(self):
        for start, end, flag in (("abc", None, "--start"), (None, "xyz", "--end"), ("10:00", "1.5", "--end")):
            with self.subTest(flag=flag):
                with self.assertRaises(RangeInvalid) as caught:
                    resolve_range(start, end)
                self.assertIn(flag, str(caught.exception))


class ClockTests(SimpleTestCase):
    def test_it_prints_the_way_the_transcript_and_the_links_do(self):
        for seconds, text in ((0, "0:00"), (5, "0:05"), (600, "10:00"), (3480, "58:00"), (3600, "1:00:00"),
                              (3723, "1:02:03"), (86400, "24:00:00")):
            with self.subTest(seconds=seconds):
                self.assertEqual(clock(seconds), text)


class DescribeTests(SimpleTestCase):
    def test_both_ends(self):
        self.assertEqual(VideoRange(600, 3480).describe(), "10:00–58:00")

    def test_an_open_end_says_so(self):
        self.assertEqual(VideoRange(600, None).describe(), "10:00–影片結尾")

    def test_it_starts_at_the_beginning(self):
        self.assertEqual(VideoRange(0, 1800).describe(), "0:00–30:00")


class RangeOfTests(SimpleTestCase):
    """文章記著的範圍。範圍存在的話 range_start 一定是整數（0 也是）；沒有範圍就兩個都是 None。"""

    @staticmethod
    def _article(start, end):
        return SimpleNamespace(range_start=start, range_end=end)

    def test_no_range(self):
        self.assertIsNone(range_of(self._article(None, None)))

    def test_a_stored_range(self):
        self.assertEqual(range_of(self._article(600, 3480)), VideoRange(600, 3480))
        self.assertEqual(range_of(self._article(600, None)), VideoRange(600, None))
        self.assertEqual(range_of(self._article(0, 1800)), VideoRange(0, 1800))

    def test_an_article_without_the_fields_has_no_range(self):
        """議會的文章與還沒有這兩個欄位的舊物件：沒有範圍，不是錯誤。"""
        self.assertIsNone(range_of(SimpleNamespace()))

    def test_an_end_without_a_start_is_read_as_starting_at_zero(self):
        self.assertEqual(range_of(self._article(None, 1800)), VideoRange(0, 1800))

    def test_a_stored_range_that_makes_no_sense_is_an_error_not_a_guess(self):
        for start, end in ((600, 600), (600, 100), (0, 3)):
            with self.subTest(start=start, end=end):
                with self.assertRaises(RangeInvalid):
                    range_of(self._article(start, end))


class RangeLengthTests(SimpleTestCase):
    """處理的那一段有多長（秒）；0＝不知道。頁數與 --max-minutes 都用它：一小時影片裡的十分鐘不該算成一小時。"""

    def test_without_a_range_it_is_the_whole_video(self):
        self.assertEqual(range_length(3600, None), 3600)
        self.assertEqual(range_length(0, None), 0)

    def test_a_range_inside_the_video(self):
        self.assertEqual(range_length(3600, VideoRange(600, 1200)), 600)

    def test_an_open_end_runs_to_the_end_of_the_video(self):
        self.assertEqual(range_length(3600, VideoRange(600, None)), 3000)

    def test_an_end_beyond_the_video_is_cut_at_the_end(self):
        self.assertEqual(range_length(3600, VideoRange(600, 9000)), 3000)

    def test_a_start_beyond_the_video_leaves_nothing(self):
        self.assertEqual(range_length(3600, VideoRange(4000, None)), 0)
        self.assertEqual(range_length(3600, VideoRange(4000, 5000)), 0)

    def test_an_unknown_video_length_is_known_only_when_the_end_is_given(self):
        self.assertEqual(range_length(0, VideoRange(600, 1200)), 600)
        self.assertEqual(range_length(0, VideoRange(600, None)), 0)


class FitProblemTests(SimpleTestCase):
    """範圍放得進這支影片嗎：預檢（新影片）與指令（已登記的影片）共用同一句話。"""

    def test_no_range_always_fits(self):
        self.assertIsNone(fit_problem(3600, None))
        self.assertIsNone(fit_problem(0, None))

    def test_a_range_inside_the_video_fits(self):
        self.assertIsNone(fit_problem(3600, VideoRange(600, 1500)))
        self.assertIsNone(fit_problem(3600, VideoRange(600, None)))
        self.assertIsNone(fit_problem(3600, VideoRange(0, 1500)))

    def test_an_end_beyond_the_video_still_fits_the_gpu_cuts_it(self):
        self.assertIsNone(fit_problem(3600, VideoRange(600, 9000)))

    def test_an_unknown_length_cannot_be_checked(self):
        self.assertIsNone(fit_problem(0, VideoRange(600, 1500)))
        self.assertIsNone(fit_problem(0, VideoRange(5000, None)))

    def test_a_start_at_or_after_the_end_of_the_video_has_nothing_to_process(self):
        for start in (3600, 3601, 9000):
            with self.subTest(start=start):
                problem = fit_problem(3600, VideoRange(start, None))
                self.assertIn("--start", problem)
                self.assertIn("1:00:00", problem)
                self.assertIn(clock(start), problem)

    def test_what_is_left_inside_the_video_has_to_reach_the_minimum(self):
        problem = fit_problem(3600, VideoRange(3598, 5400))
        self.assertIn("2 秒", problem)
        self.assertIn(f"{MIN_RANGE_SECONDS} 秒", problem)
        self.assertIn("59:58–1:30:00", problem)
        self.assertIsNone(fit_problem(3600, VideoRange(3600 - MIN_RANGE_SECONDS, None)))
        self.assertIn("4 秒", fit_problem(3600, VideoRange(3596, None)))
