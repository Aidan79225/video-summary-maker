"""check_speaker_result 與處理範圍：結果回報的區間要跟我們要求的對得上，而且結果裡每一個時間都落在那一段裡。

為什麼這麼嚴：範圍的整個意思是「只處理這一段，時間都是原影片的」。GPU 舊版會悄悄忽略範圍、處理整支影片；
新版如果漏了把起點加回去，每一個時間連結與截圖都會跳到別的地方、逐字稿的時間全錯。這兩種都不能發佈。
全是純函式測試，不碰資料庫與網路。結果的範例是契約檔，用 speaker_support.ranged_result 換成「只處理一段」的版本。
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

from django.test import SimpleTestCase

from articles.gpu_client import JobFailed
from articles.speaker_result import check_speaker_result, clean_diarization
from articles.tests.speaker_support import CONTRACT, CONTRACT_VIDEO, diarized_result, ranged_result

VIDEO = f"yt-{CONTRACT_VIDEO}"


def video(start=600, end=3480, count=None, duration=3600):
    """要求只處理 start～end（end 是 None＝到影片結尾）的影片；範例的影片 id 是 abcdefghijk。"""
    return SimpleNamespace(speaker_count=count, ivod_id=VIDEO, range_start=start, range_end=end,
                           duration_seconds=duration)


def whole_video(count=None):
    return SimpleNamespace(speaker_count=count, ivod_id=VIDEO, range_start=None, range_end=None,
                           duration_seconds=3600)


def fails(test, article, payload, fragment):
    with test.assertRaises(JobFailed) as caught:
        check_speaker_result(article, payload)
    test.assertIn(fragment, str(caught.exception))
    return str(caught.exception)


class AcceptanceTests(SimpleTestCase):
    def test_a_result_for_the_range_we_asked_for_is_accepted_and_keeps_the_range(self):
        checked = check_speaker_result(video(600, 1500), ranged_result(start=600, end=1500))
        self.assertEqual((checked.diarization["range_start"], checked.diarization["range_end"]), (600.0, 1500.0))
        self.assertEqual(checked.diarization["status"], "ok")

    def test_every_time_in_the_result_is_the_original_videos(self):
        """範例的第一句在 5 秒，加上起點 600 就是原影片的 10:05：存進去的就是這個，不是 0:05。"""
        checked = check_speaker_result(video(600, 1500), ranged_result(start=600, end=1500))
        self.assertTrue(checked.transcript_text.startswith("10:05 [[S1]]："))
        self.assertEqual(checked.slides[0].points[0]["start"], 605.0)
        self.assertEqual(checked.speakers[0]["first_seen"], 605.0)

    def test_an_open_end_accepts_the_end_of_the_video_the_gpu_reports(self):
        checked = check_speaker_result(video(600, None), ranged_result(start=600, end=3600))
        self.assertEqual(checked.diarization["range_end"], 3600.0)

    def test_an_end_beyond_the_video_is_cut_by_the_gpu_and_accepted(self):
        """--end 比影片還長：GPU 夾到影片結尾，回報的終點比要求的小。"""
        checked = check_speaker_result(video(600, 7200, duration=3000), ranged_result(start=600, end=3000))
        self.assertEqual(checked.diarization["range_end"], 3000.0)

    def test_a_start_inside_the_slack_is_accepted(self):
        payload = ranged_result(start=600, end=1500)
        payload["diarization"]["range_start"] = 600.5
        check_speaker_result(video(600, 1500), payload)

    def test_the_range_from_the_very_beginning_is_a_real_range(self):
        checked = check_speaker_result(video(0, 1500), ranged_result(start=0, end=1500))
        self.assertEqual((checked.diarization["range_start"], checked.diarization["range_end"]), (0.0, 1500.0))

    def test_a_video_without_a_range_is_checked_exactly_as_before(self):
        checked = check_speaker_result(whole_video(), diarized_result(CONTRACT_VIDEO))
        self.assertNotIn("range_start", checked.diarization)
        self.assertNotIn("range_end", checked.diarization)
        self.assertEqual(checked.diarization, CONTRACT["result"]["diarization"])

    def test_the_gpu_may_drop_a_range_that_is_the_whole_video(self):
        """GPU 把「從 0 到影片結尾以後」當成整支影片、不回報範圍：要求的是 0～end 的話，沒有區間是正常的，
        只要結果裡的時間都沒有超過 end。"""
        checked = check_speaker_result(video(0, 1500), diarized_result(CONTRACT_VIDEO))
        self.assertNotIn("range_start", checked.diarization)


class RangeEchoTests(SimpleTestCase):
    def test_a_range_we_did_not_ask_for_is_refused(self):
        """沒有指定範圍的影片，結果卻說只處理了一段：接回了另一個設定的工作、或 GPU 搞錯了——兩邊都不信。"""
        fails(self, whole_video(), ranged_result(start=600, end=1500), "沒有指定")

    def test_a_missing_range_is_refused_as_a_gpu_that_is_not_updated(self):
        """舊版 GPU 不認得 range_start／range_end，會悄悄處理整支影片：結果裡沒有區間就是這個徵兆。"""
        for article in (video(600, 3480), video(600, None)):
            with self.subTest(range=(article.range_start, article.range_end)):
                message = fails(self, article, diarized_result(CONTRACT_VIDEO), "還沒更新")
                self.assertIn("範圍", message)
                self.assertIn("整支影片", message)

    def test_a_start_that_is_not_the_one_we_asked_for_is_refused(self):
        payload = ranged_result(start=500, end=1500)
        fails(self, video(600, 1500), payload, "起點")

    def test_a_start_beyond_the_slack_is_refused(self):
        payload = ranged_result(start=600, end=1500)
        payload["diarization"]["range_start"] = 598.5
        fails(self, video(600, 1500), payload, "起點")

    def test_an_end_beyond_what_we_asked_for_is_refused(self):
        payload = ranged_result(start=600, end=1500)
        payload["diarization"]["range_end"] = 1502
        fails(self, video(600, 1500), payload, "終點")

    def test_an_end_beyond_the_video_when_we_gave_none_is_refused(self):
        fails(self, video(600, None, duration=3600), ranged_result(start=600, end=5000), "影片")

    def test_an_end_beyond_the_video_is_not_checked_when_the_length_is_unknown(self):
        check_speaker_result(video(600, None, duration=0), ranged_result(start=600, end=5000))

    def test_a_range_with_only_one_end_is_refused(self):
        for drop in ("range_start", "range_end"):
            with self.subTest(dropped=drop):
                payload = ranged_result(start=600, end=1500)
                del payload["diarization"][drop]
                fails(self, video(600, 1500), payload, "一起")

    def test_numbers_that_cannot_be_a_range_are_refused(self):
        for start, end in ((600, 600), (600, 100), (-1, 1500), (600, float("nan")), (float("inf"), 1500),
                           (600, float("inf")), (True, 1500), (600, "1500"), ("600", 1500), (600, None)):
            with self.subTest(start=start, end=end):
                payload = ranged_result(start=600, end=1500)
                payload["diarization"]["range_start"], payload["diarization"]["range_end"] = start, end
                with self.assertRaises(JobFailed):
                    check_speaker_result(video(600, 1500), payload)

    def test_a_stored_range_that_makes_no_sense_fails_the_job_instead_of_guessing(self):
        fails(self, video(600, 600), ranged_result(start=600, end=1500), "範圍")

    def test_an_end_alone_means_from_the_beginning(self):
        article = video(None, 1500)
        article.range_start = None
        check_speaker_result(article, ranged_result(start=0, end=1500))


class InsideTheRangeTests(SimpleTestCase):
    """結果裡每一個時間都要落在處理的那一段裡。GPU 漏了把起點加回去的話，每個時間都落在區間之前。"""

    def test_a_gpu_that_forgot_to_add_the_start_back_is_caught(self):
        """區間說是 10:00～25:00，說話者與逐字稿卻從 0:05 開始：那是剪出來的檔案的時間，不是原影片的。"""
        payload = diarized_result(CONTRACT_VIDEO)
        payload["diarization"].update(range_start=600, range_end=1500)
        message = fails(self, video(600, 1500), payload, "範圍")
        self.assertIn("起點", message)

    def test_a_speaker_seen_before_the_range_is_refused(self):
        payload = ranged_result(start=600, end=1500)
        payload["speakers"][1]["first_seen"] = 300.0
        fails(self, video(600, 1500), payload, "發言者 B")

    def test_a_speaker_seen_after_the_range_is_refused(self):
        payload = ranged_result(start=600, end=1500)
        payload["speakers"][0]["last_seen"] = 2000.0
        fails(self, video(600, 1500), payload, "發言者 A")

    def test_a_transcript_line_outside_the_range_is_refused(self):
        payload = ranged_result(start=600, end=1500)
        payload["transcript_text"] = payload["transcript_text"].replace("10:05", "00:05", 1)
        fails(self, video(600, 1500), payload, "逐字稿")

    def test_a_transcript_line_after_the_range_is_refused(self):
        payload = ranged_result(start=600, end=1500)
        payload["transcript_text"] += "\n30:00 [[S1]]：這句話在範圍之外"
        fails(self, video(600, 1500), payload, "逐字稿")

    def test_a_slide_whose_screenshot_time_is_outside_the_range_is_refused(self):
        payload = ranged_result(start=600, end=1500)
        payload["slides"][0]["timestamp"] = 5.0
        fails(self, video(600, 1500), payload, "截圖")

    def test_a_slide_without_a_timestamp_is_fine(self):
        payload = ranged_result(start=600, end=1500)
        del payload["slides"][0]["timestamp"]
        check_speaker_result(video(600, 1500), payload)

    def test_a_second_of_slack_at_each_edge(self):
        payload = ranged_result(start=600, end=1500)
        payload["speakers"][0]["first_seen"] = 599.5
        payload["speakers"][0]["last_seen"] = 1500.9
        check_speaker_result(video(600, 1500), payload)
        payload["speakers"][0]["first_seen"] = 598.9
        fails(self, video(600, 1500), payload, "發言者 A")

    def test_a_whole_video_range_with_no_echo_still_has_to_stay_before_its_end(self):
        payload = diarized_result(CONTRACT_VIDEO)
        payload["transcript_text"] += "\n30:00 [[S1]]：這句話在 --end 之後"
        fails(self, video(0, 1500), payload, "逐字稿")

    def test_a_sample_outside_the_range_is_dropped_not_fatal(self):
        """代表的話只是給操作者聽音檔取名用的：壞的丟掉，不為它讓整支影片失敗（跟其他壞的 sample 一樣）。"""
        payload = ranged_result(start=600, end=1500)
        # 放在最前面：一份清單最多留 3 句，壞的先被丟掉，好的才補得上來
        payload["speakers"][0]["samples"].insert(0, {"start": 5.0, "text": "這句話的時間在範圍之外"})
        checked = check_speaker_result(video(600, 1500), payload)
        self.assertEqual([s["start"] for s in checked.speakers[0]["samples"]], [605.0, 631.0, 669.0])

    def test_without_a_range_nothing_new_is_checked(self):
        """沒有指定範圍的影片：時間可以是任何落在一天之內的數字，跟以前一樣。"""
        payload = diarized_result(CONTRACT_VIDEO)
        payload["speakers"][0]["last_seen"] = 20000.0
        check_speaker_result(whole_video(), payload)


class CleanDiarizationRangeTests(SimpleTestCase):
    def good(self):
        return copy.deepcopy(CONTRACT["result"]["diarization"])

    def test_no_range_keys_in_means_none_out(self):
        cleaned = clean_diarization(self.good())
        self.assertNotIn("range_start", cleaned)
        self.assertNotIn("range_end", cleaned)

    def test_both_keys_come_out_as_numbers(self):
        raw = self.good() | {"range_start": 600, "range_end": 1500}
        cleaned = clean_diarization(raw)
        self.assertEqual((cleaned["range_start"], cleaned["range_end"]), (600.0, 1500.0))

    def test_both_null_is_no_range(self):
        cleaned = clean_diarization(self.good() | {"range_start": None, "range_end": None})
        self.assertNotIn("range_start", cleaned)

    def test_one_key_alone_is_refused(self):
        for raw in ({"range_start": 600}, {"range_end": 1500}, {"range_start": 600, "range_end": None},
                    {"range_start": None, "range_end": 1500}):
            with self.subTest(raw=raw):
                with self.assertRaises(JobFailed):
                    clean_diarization(self.good() | raw)
