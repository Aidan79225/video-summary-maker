"""預檢與處理範圍：範圍要落在影片裡；--max-minutes 比的是要處理的那一段，不是整支影片。

預檢本身（不公開、直播、Shorts、音樂、長度…）的測試在 test_youtube_source.py；這裡只管範圍多出來的規則。
"""
from __future__ import annotations

from datetime import date

from django.test import SimpleTestCase

from articles.tests.test_youtube_source import TALK, _meta
from articles.video_range import VideoRange
from articles.youtube_source import PreflightOptions, VideoRefused, preflight

HOUR = 3600


def options(video_range, **kw):
    return PreflightOptions(video_range=video_range, **kw)


class RangeInsideTheVideoTests(SimpleTestCase):
    def refused(self, meta, opts, *words):
        with self.assertRaises(VideoRefused) as caught:
            preflight(meta, opts)
        for word in words:
            self.assertIn(word, str(caught.exception))
        return caught.exception

    def test_no_range_is_the_default_and_changes_nothing(self):
        self.assertIsNone(PreflightOptions().video_range)
        self.assertEqual(preflight(_meta(duration_seconds=HOUR), PreflightOptions()), [])

    def test_a_range_inside_the_video_passes_without_warnings(self):
        self.assertEqual(preflight(_meta(duration_seconds=HOUR), options(VideoRange(600, 1500))), [])
        self.assertEqual(preflight(_meta(duration_seconds=HOUR), options(VideoRange(600, None))), [])

    def test_a_start_at_or_after_the_end_of_the_video_is_refused_with_no_flag_to_override(self):
        for start in (HOUR, HOUR + 1, 5 * HOUR):
            with self.subTest(start=start):
                error = self.refused(_meta(duration_seconds=HOUR), options(VideoRange(start, None)),
                                     "--start", "1:00:00")
                self.assertIsNone(error.flag)

    def test_a_range_cut_down_to_almost_nothing_by_the_end_of_the_video_is_refused(self):
        """--start 59:58 --end 1:30:00 的影片只有 1:00:00：夾在影片之內只剩 2 秒，GPU 也不會收。"""
        self.refused(_meta(duration_seconds=HOUR), options(VideoRange(HOUR - 2, 5400)), "太短", "5 秒")

    def test_exactly_the_minimum_left_is_fine(self):
        self.assertEqual(preflight(_meta(duration_seconds=HOUR), options(VideoRange(HOUR - 5, None))), [])

    def test_an_end_beyond_the_video_is_a_warning_not_a_refusal(self):
        warnings = preflight(_meta(duration_seconds=HOUR), options(VideoRange(600, 2 * HOUR)))
        self.assertEqual(len(warnings), 1)
        self.assertIn("--end", warnings[0])
        self.assertIn("2:00:00", warnings[0])
        self.assertIn("1:00:00", warnings[0])

    def test_a_range_that_is_the_whole_video_says_so(self):
        warnings = preflight(_meta(duration_seconds=HOUR), options(VideoRange(0, HOUR)))
        self.assertEqual(len(warnings), 1)
        self.assertIn("整支影片", warnings[0])

    def test_an_unknown_length_cannot_be_checked_against_the_range(self):
        """影片頁沒給長度：起點是不是在影片裡沒辦法知道，交給 GPU（下載之後才知道，那時再失敗）。"""
        warnings = preflight(_meta(duration_seconds=0), options(VideoRange(600, 1500)))
        self.assertEqual(len(warnings), 1)
        self.assertIn("沒有給長度", warnings[0])


class MaxMinutesTests(SimpleTestCase):
    """上限比的是要處理的那一段：整支影片五小時、只處理其中一小時，不該要操作者把上限調成五小時。"""

    def test_a_long_video_with_a_short_range_passes_the_default_limit(self):
        meta = _meta(duration_seconds=5 * HOUR)
        with self.assertRaises(VideoRefused):
            preflight(meta, PreflightOptions())
        self.assertEqual(preflight(meta, options(VideoRange(HOUR, 2 * HOUR))), [])

    def test_the_default_limit_counts_the_range_not_the_video(self):
        meta = _meta(duration_seconds=5 * HOUR)
        # 剛好 180 分鐘的一段不算超過
        self.assertEqual(preflight(meta, options(VideoRange(0, 3 * HOUR))), [])
        with self.assertRaises(VideoRefused):
            preflight(meta, options(VideoRange(0, 3 * HOUR + 1)))

    def test_a_range_over_the_limit_names_the_range_and_the_number_to_raise_it_to(self):
        meta = _meta(duration_seconds=6 * HOUR)
        with self.assertRaises(VideoRefused) as caught:
            preflight(meta, options(VideoRange(HOUR, 5 * HOUR)))     # 240 分鐘
        message = str(caught.exception)
        self.assertEqual(caught.exception.flag, "--max-minutes")
        for fragment in ("1:00:00–5:00:00", "240 分鐘", "--max-minutes 240", "360 分鐘"):
            self.assertIn(fragment, message)

    def test_an_open_end_counts_to_the_end_of_the_video(self):
        meta = _meta(duration_seconds=6 * HOUR)
        with self.assertRaises(VideoRefused) as caught:
            preflight(meta, options(VideoRange(HOUR, None)))         # 300 分鐘
        self.assertIn("--max-minutes 300", str(caught.exception))

    def test_an_end_beyond_the_video_counts_only_what_is_there(self):
        meta = _meta(duration_seconds=2 * HOUR)
        self.assertEqual(len(preflight(meta, options(VideoRange(0, 9 * HOUR), max_minutes=120))), 1)

    def test_without_a_range_the_refusal_reads_exactly_as_before(self):
        with self.assertRaises(VideoRefused) as caught:
            preflight(_meta(duration_seconds=95 * 60), PreflightOptions(max_minutes=60))
        self.assertTrue(str(caught.exception).startswith("影片長 95 分鐘，超過上限 60 分鐘。要處理的話，加上 --max-minutes 95"))

    def test_an_unknown_length_with_an_end_is_checked_by_the_range_length(self):
        meta = _meta(duration_seconds=0)
        with self.assertRaises(VideoRefused):
            preflight(meta, options(VideoRange(0, 4 * HOUR)))
        self.assertEqual(len(preflight(meta, options(VideoRange(0, 2 * HOUR)))), 1)

    def test_the_date_and_the_rest_of_the_preflight_are_untouched(self):
        """範圍不影響其他檢查：不公開的影片照樣要旗標。"""
        meta = _meta(duration_seconds=HOUR, unlisted=True)
        with self.assertRaises(VideoRefused) as caught:
            preflight(meta, options(VideoRange(600, 1500)))
        self.assertEqual(caught.exception.flag, "--allow-unlisted")
        self.assertEqual(TALK, meta.video_id)
        self.assertEqual(meta.date, date(2026, 10, 13))
