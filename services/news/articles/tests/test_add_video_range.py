"""add_video --start/--end：只處理影片的一段（多人說話者模式）。

假的 GPU 與影片資料沿用 test_add_video 的（patch 掉指令裡的 GpuApiClient 與 fetch_metadata），不碰網路。
範圍的規則本身（時間怎麼寫）在 test_video_range.py，預檢在 test_video_range_preflight.py，GPU 結果的核對在
test_speaker_result_range.py，送出的請求在 test_gpu_client_speakers.py；這裡測的是指令把它們接起來之後的後果：
哪些組合被擋下來、擋下來的時候資料庫有沒有動、已經存在的影片每一種狀態下範圍怎麼處理。
"""
from __future__ import annotations

import time
from datetime import timedelta

from django.utils import timezone

from articles.gpu_client import GpuApiError, JobFailed
from articles.models import Article, ArticleStatus
from articles.tests import test_add_video as base
from articles.tests.speaker_support import READY_CAPABILITIES, RANGE_CAPABILITIES, diarized_result, ranged_result

ZOO, ZOO_URL = base.ZOO, base.ZOO_URL
HOUR = 3600


def ranged_gpu(start=600, end=1500, requested=None, **kwargs):
    """GPU 已更新（認得範圍），結果是契約範例換成「只處理 start～end」的版本（end＝回報的終點）。"""
    kwargs.setdefault("capabilities", dict(RANGE_CAPABILITIES))
    return base.Gpu(result=ranged_result(ZOO, start, end, requested=requested), **kwargs)


def long_video(**kw):
    return base._meta(duration_seconds=HOUR, **kw)


def sent(gpu):
    """GPU 收到的每一個請求的（模式、人數、起點、終點）。"""
    return [(s["speaker_mode"], s["speaker_count"], s["range_start"], s["range_end"]) for s in gpu.submits]


class OptionTests(base.CommandTestCase):
    def test_a_range_is_sent_remembered_and_published(self):
        out, err, gpu, _ = self.run_cmd(ZOO_URL, "--start", "10:00", "--end", "25:00", gpu=ranged_gpu(),
                                        meta=long_video())
        self.assertEqual(sent(gpu), [("diarize", None, 600, 1500)])
        article = self.refresh()
        self.assertEqual((article.status, article.range_start, article.range_end, article.speaker_count),
                         (ArticleStatus.READY, 600, 1500, None))
        self.assertEqual((article.diarization["range_start"], article.diarization["range_end"]), (600.0, 1500.0))
        # 整支影片的長度還是整支影片的，不是這一段的
        self.assertEqual(article.duration_seconds, HOUR)
        self.assertIn("處理範圍：10:00–25:00", out)

    def test_the_published_times_are_the_original_videos(self):
        self.run_cmd(ZOO_URL, "--start", "10:00", "--end", "25:00", gpu=ranged_gpu(), meta=long_video())
        article = self.refresh()
        self.assertTrue(article.transcript_text.startswith("10:05 [[S1]]："))
        self.assertEqual(article.slides.order_by("index").first().points[0]["start"], 605.0)

    def test_the_sheet_links_jump_to_the_original_videos_seconds(self):
        out, *_ = self.run_cmd(ZOO_URL, "--start", "10:00", "--end", "25:00", gpu=ranged_gpu(), meta=long_video())
        self.assertIn(f"https://www.youtube.com/watch?v={ZOO}&t=605s", out)
        self.assertNotIn("&t=5s", out)
        # 對照表也說這些人是從哪一段聽出來的
        self.assertIn("處理範圍：10:00～25:00", out)

    def test_every_time_format_is_accepted(self):
        for start, end in (("600", "1500"), ("10:00", "25:00"), ("0:10:00", "0:25:00")):
            with self.subTest(start=start):
                Article.objects.all().delete()
                _, _, gpu, _ = self.run_cmd(ZOO_URL, "--start", start, "--end", end, gpu=ranged_gpu(),
                                            meta=long_video())
                self.assertEqual(sent(gpu), [("diarize", None, 600, 1500)])

    def test_a_start_alone_runs_to_the_end_of_the_video(self):
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--start", "10:00", gpu=ranged_gpu(end=3600), meta=long_video())
        self.assertEqual(sent(gpu), [("diarize", None, 600, None)])
        article = self.refresh()
        self.assertEqual((article.range_start, article.range_end), (600, None))
        self.assertIn("10:00–影片結尾", out)

    def test_an_end_alone_starts_at_the_beginning(self):
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--end", "25:00", gpu=ranged_gpu(start=0, end=1500), meta=long_video())
        self.assertEqual(sent(gpu), [("diarize", None, 0, 1500)])
        article = self.refresh()
        self.assertEqual((article.range_start, article.range_end), (0, 1500))
        self.assertIn("0:00–25:00", out)

    def test_a_start_of_zero_alone_is_the_whole_video_so_no_range(self):
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--start", "0", gpu=base.Gpu(result=diarized_result(ZOO)),
                                      meta=long_video())
        self.assertEqual(sent(gpu), [("diarize", None, None, None)])
        article = self.refresh()
        self.assertEqual((article.range_start, article.range_end), (None, None))
        self.assertNotIn("處理範圍", out)
        # 但操作者寫了它：說清楚它的意思，不是悄悄當成沒寫
        self.assertIn("--start 0 就是從頭開始", out)
        self.assertIn("整支影片都會處理", out)

    def test_a_fixed_count_goes_with_the_range(self):
        _, _, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "3", "--start", "10:00", "--end", "25:00",
                                    gpu=ranged_gpu(requested=3), meta=long_video())
        self.assertEqual(sent(gpu), [("diarize", 3, 600, 1500)])

    def test_without_the_options_nothing_about_a_range_is_printed_or_sent(self):
        out, err, gpu, _ = self.run_cmd(ZOO_URL, gpu=base.Gpu(result=diarized_result(ZOO)), meta=long_video())
        self.assertEqual(sent(gpu), [("diarize", None, None, None)])
        self.assertNotIn("範圍", out + err)

    def test_a_dry_run_prints_the_range_and_touches_nothing(self):
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--start", "10:00", "--end", "25:00", "--dry-run", meta=long_video())
        self.assertIn("處理範圍：10:00–25:00", out)
        self.assertFalse(Article.objects.exists())
        self.assertEqual((gpu.submits, gpu.capability_calls), ([], 0))


class RefusedBeforeAnythingTests(base.CommandTestCase):
    """擋在最前面的：時間看不懂、組合不合理。資料庫沒動、沒有網路請求、GPU 沒有被問任何事。"""

    def refused(self, *args, fragment):
        message, out, err, gpu, fetch = self.fails(ZOO_URL, *args, gpu=ranged_gpu(), meta=long_video())
        self.assertIn(fragment, message)
        self.assertFalse(Article.objects.exists(), "登記了影片")
        fetch.assert_not_called()
        self.assertEqual((gpu.submits, gpu.capability_calls, gpu.looked_up), ([], 0, []))
        return message

    def test_a_time_that_cannot_be_read_names_the_flag(self):
        self.refused("--start", "abc", fragment="--start")
        self.refused("--end", "xyz", fragment="--end")
        self.refused("--start", "10:00", "--end", "1:61", fragment="--end")

    def test_only_whole_seconds_are_accepted(self):
        message = self.refused("--start", "10.5", fragment="整數")
        self.assertIn("--start", message)

    def test_an_end_that_is_not_after_the_start_is_refused(self):
        self.refused("--start", "25:00", "--end", "10:00", fragment="晚")
        self.refused("--start", "10:00", "--end", "10:00", fragment="晚")

    def test_a_range_shorter_than_five_seconds_is_refused(self):
        self.refused("--start", "10:00", "--end", "10:03", fragment="5 秒")

    def test_a_range_is_for_one_video_only(self):
        other = "f4r2yWk4LV0"
        self.refused(f"https://youtu.be/{other}", "--start", "10:00", fragment="只能搭配一個網址")
        self.refused(f"https://youtu.be/{other}", "--end", "10:00", fragment="只能搭配一個網址")

    def test_a_start_of_zero_with_several_videos_is_still_refused_not_ignored(self):
        """--start 0 是「整支影片」，但操作者寫了它：寫在多支影片的批次裡就是弄錯了，不悄悄接受。"""
        other = "f4r2yWk4LV0"
        self.refused(f"https://youtu.be/{other}", "--start", "0", fragment="只能搭配一個網址")

    def test_the_single_speaker_mode_has_no_range(self):
        message = self.refused("--speakers", "1", "--start", "10:00", "--end", "25:00", fragment="多人")
        self.assertIn("--speakers 1", message)

    def test_a_zero_start_with_the_single_speaker_mode_is_refused_too(self):
        self.refused("--speakers", "1", "--start", "0", fragment="多人")

    def test_the_url_is_still_the_first_line(self):
        _, out, *_ = self.fails(ZOO_URL, "--start", "abc", gpu=ranged_gpu())
        self.assertTrue(out.splitlines()[0].startswith(f"影片 {ZOO}"))


class CapabilityTests(base.CommandTestCase):
    """GPU 要明說自己認得範圍（/capabilities 的 video_range 剛好是 true）才送：舊版 GPU 會悄悄忽略 range_start／
    range_end、處理整支影片，那樣發佈出去的是整支影片的結果，而操作者以為只處理了一段。"""

    def refused(self, capabilities, *args):
        gpu = base.Gpu(capabilities=capabilities)
        message, out, err, gpu, fetch = self.fails(ZOO_URL, "--start", "10:00", "--end", "25:00", *args, gpu=gpu,
                                                   meta=long_video())
        self.assertFalse(Article.objects.exists(), "登記了影片")
        self.assertEqual(gpu.submits, [])
        self.assertIn("沒有登記、也沒有送出", message)
        self.assertIn("video_range", message)
        self.assertIn("更新", message)
        return message

    def test_a_gpu_without_the_flag_is_refused_before_anything_is_registered(self):
        self.refused(dict(READY_CAPABILITIES))

    def test_the_flag_has_to_be_exactly_true(self):
        for value in (False, None, "true", "yes", 1, 0, [], {"x": 1}):
            with self.subTest(value=value):
                self.refused(READY_CAPABILITIES | {"video_range": value})

    def test_a_gpu_that_cannot_diarize_says_that_first(self):
        """能力問不到、或根本不能區分說話者：照舊說原因，不是說「不認得範圍」。"""
        gpu = base.Gpu(capabilities={"speaker_diarization": False, "reason": "模型還沒下載", "video_range": True,
                                     "speaker_contract": 1})
        message, *_ = self.fails(ZOO_URL, "--start", "10:00", "--end", "25:00", gpu=gpu, meta=long_video())
        self.assertIn("模型還沒下載", message)
        # 範圍不能搭配 --speakers 1：不建議那條退路（沒帶範圍的指令照舊會建議）
        self.assertNotIn("--speakers 1", message)
        Article.objects.all().delete()
        plain, *_ = self.fails(ZOO_URL, gpu=gpu, meta=long_video())
        self.assertIn("模型還沒下載", plain)
        self.assertIn("--speakers 1", plain)

    def test_a_gpu_that_knows_ranges_is_asked_once(self):
        _, _, gpu, _ = self.run_cmd(ZOO_URL, "--start", "10:00", "--end", "25:00", gpu=ranged_gpu(), meta=long_video())
        self.assertEqual(gpu.capability_calls, 1)

    def test_without_a_range_an_old_gpu_is_fine(self):
        """沒有指定範圍的指令一個字都不變：舊版 GPU（沒有 video_range）照樣可以加影片。"""
        _, _, gpu, _ = self.run_cmd(ZOO_URL, gpu=base.Gpu(result=diarized_result(ZOO),
                                                          capabilities=dict(READY_CAPABILITIES)))
        self.assertEqual(self.refresh().status, ArticleStatus.READY)
        self.assertEqual(sent(gpu), [("diarize", None, None, None)])


class PreflightInTheCommandTests(base.CommandTestCase):
    def test_a_start_beyond_the_video_is_refused_before_registering(self):
        message, out, err, gpu, _ = self.fails(ZOO_URL, "--start", "2:00:00", gpu=ranged_gpu(), meta=long_video())
        self.assertIn("預檢沒過，沒有登記", message)
        self.assertIn("--start", message)
        self.assertFalse(Article.objects.exists())
        self.assertEqual((gpu.submits, gpu.capability_calls), ([], 0))

    def test_a_long_video_with_a_short_range_does_not_need_a_higher_limit(self):
        meta = base._meta(duration_seconds=5 * HOUR)
        message, *_ = self.fails(ZOO_URL, gpu=ranged_gpu(), meta=meta)
        self.assertIn("--max-minutes", message)
        Article.objects.all().delete()
        _, _, gpu, _ = self.run_cmd(ZOO_URL, "--start", "1:00:00", "--end", "2:00:00",
                                    gpu=ranged_gpu(start=3600, end=7200), meta=meta)
        self.assertEqual(sent(gpu), [("diarize", None, 3600, 7200)])

    def test_the_limit_applies_to_the_range_and_says_which(self):
        meta = base._meta(duration_seconds=6 * HOUR)
        message, *_ = self.fails(ZOO_URL, "--start", "1:00:00", "--end", "5:00:00", gpu=ranged_gpu(), meta=meta)
        self.assertIn("1:00:00–5:00:00", message)
        self.assertIn("--max-minutes 240", message)

    def test_an_end_beyond_the_video_is_a_warning(self):
        out, err, gpu, _ = self.run_cmd(ZOO_URL, "--start", "10:00", "--end", "2:00:00",
                                        gpu=ranged_gpu(start=600, end=3600), meta=long_video())
        self.assertIn("--end", err)
        self.assertIn("超過影片的長度", err)
        self.assertEqual(sent(gpu), [("diarize", None, 600, 7200)])

    def test_the_slide_count_follows_the_range_not_the_whole_video(self):
        _, _, gpu, _ = self.run_cmd(ZOO_URL, "--start", "10:00", "--end", "25:00", gpu=ranged_gpu(),
                                    meta=long_video())
        # 900 秒：最少 clamp(⌈900/360⌉, 3, 8)=3、最多 clamp(⌈900/120⌉, 6, 50)=8（整支影片 3600 秒的話是 8～30）
        self.assertEqual((gpu.submits[0]["min_slides"], gpu.submits[0]["max_slides"]), (3, 8))

    def test_the_operators_slide_numbers_still_win(self):
        _, _, gpu, _ = self.run_cmd(ZOO_URL, "--start", "10:00", "--end", "25:00", "--min-slides", "4",
                                    "--max-slides", "9", gpu=ranged_gpu(), meta=long_video())
        self.assertEqual((gpu.submits[0]["min_slides"], gpu.submits[0]["max_slides"]), (4, 9))


class ExistingVideoTests(base.CommandTestCase):
    """已經登記的影片：範圍跟說話者人數一樣，是「這支影片這次怎麼送去 GPU 的」。要送新工作時以這次的指令為準
    （省略＝整支影片，並且說清楚之前是什麼）；接回 GPU 上還在跑的工作時不能悄悄換掉。"""

    def row(self, state=ArticleStatus.FAILED, start=None, end=None, **fields):
        defaults = dict(status=state, speaker_count=None, duration_seconds=HOUR, range_start=start, range_end=end)
        if state == ArticleStatus.FAILED:
            defaults["error"] = "x"
        if state == ArticleStatus.PROCESSING:
            defaults["attempts"] = 1
        return self.article(**(defaults | fields))

    @staticmethod
    def snapshot(article):
        return (article.status, article.speaker_count, article.range_start, article.range_end, article.gpu_job_id,
                article.attempts, article.error, article.updated_at, article.speakers, article.diarization)

    # --- 要送新工作：以這次的指令為準 ---

    def test_a_failed_video_is_tried_again_with_the_range_asked_for(self):
        self.row()
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--start", "10:00", "--end", "25:00", gpu=ranged_gpu())
        self.assertEqual(sent(gpu), [("diarize", None, 600, 1500)])
        article = self.refresh()
        self.assertEqual((article.status, article.range_start, article.range_end), (ArticleStatus.READY, 600, 1500))
        self.assertIn("處理範圍：10:00–25:00（之前是：整支影片）", out)

    def test_leaving_the_options_out_means_the_whole_video_and_says_what_it_was(self):
        self.row(start=600, end=1500)
        out, _, gpu, _ = self.run_cmd(ZOO_URL, gpu=base.Gpu(result=diarized_result(ZOO)))
        self.assertEqual(sent(gpu), [("diarize", None, None, None)])
        article = self.refresh()
        self.assertEqual((article.range_start, article.range_end), (None, None))
        self.assertIn("處理範圍：整支影片（之前是：10:00–25:00）", out)

    def test_the_same_range_again_is_stated_without_a_before(self):
        self.row(start=600, end=1500)
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--start", "10:00", "--end", "25:00", gpu=ranged_gpu())
        self.assertIn("處理範圍：10:00–25:00", out)
        self.assertNotIn("之前是：10:00", out)

    def test_a_different_range_says_what_it_was(self):
        self.row(start=600, end=1500)
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--start", "20:00", "--end", "35:00", gpu=ranged_gpu(1200, 2100))
        self.assertIn("處理範圍：20:00–35:00（之前是：10:00–25:00）", out)
        self.assertEqual(sent(gpu), [("diarize", None, 1200, 2100)])

    def test_a_single_speaker_video_asked_for_a_range_becomes_multi_speaker_and_says_so(self):
        self.row(speaker_count=1)
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--start", "10:00", "--end", "25:00", gpu=ranged_gpu())
        self.assertIn("說話者模式：自動區分", out)
        self.assertEqual(sent(gpu), [("diarize", None, 600, 1500)])

    def test_the_range_has_to_fit_the_length_the_video_was_registered_with(self):
        self.row(duration_seconds=600)
        message, *_, gpu, _ = self.fails(ZOO_URL, "--start", "20:00", gpu=ranged_gpu())
        self.assertIn("--start", message)
        article = self.refresh()
        self.assertEqual((article.range_start, article.range_end, article.status), (None, None, ArticleStatus.FAILED))
        self.assertEqual((gpu.submits, gpu.capability_calls), ([], 0))

    def test_an_old_gpu_is_refused_before_the_row_is_touched(self):
        self.row()
        before = self.snapshot(self.refresh())
        message, *_, gpu, _ = self.fails(ZOO_URL, "--start", "10:00", "--end", "25:00",
                                         gpu=base.Gpu(capabilities=dict(READY_CAPABILITIES)))
        self.assertIn("video_range", message)
        self.assertEqual(self.snapshot(self.refresh()), before)
        self.assertEqual(gpu.submits, [])

    def test_the_stored_slide_count_follows_the_range(self):
        self.row()
        _, _, gpu, _ = self.run_cmd(ZOO_URL, "--start", "10:00", "--end", "25:00", gpu=ranged_gpu())
        self.assertEqual((gpu.submits[0]["min_slides"], gpu.submits[0]["max_slides"]), (3, 8))

    # --- 已發佈 ---

    def test_a_published_video_without_rerun_states_its_range_and_leaves_everything_alone(self):
        self.row(ArticleStatus.READY, start=600, end=1500)
        out, err, gpu, _ = self.run_cmd(ZOO_URL)
        self.assertIn("已發佈", out)
        self.assertIn("處理範圍：10:00–25:00", out)
        self.assertEqual((err, gpu.submits, gpu.capability_calls), ("", [], 0))

    def test_a_published_whole_video_says_nothing_about_a_range(self):
        self.row(ArticleStatus.READY)
        out, err, *_ = self.run_cmd(ZOO_URL)
        self.assertNotIn("範圍", out + err)

    def test_options_that_differ_need_a_rerun_and_the_command_says_it_did_nothing(self):
        self.row(ArticleStatus.READY, start=600, end=1500)
        before = self.snapshot(self.refresh())
        out, err, gpu, _ = self.run_cmd(ZOO_URL, "--start", "20:00", "--end", "35:00")
        self.assertIn("--start", err)
        self.assertIn("--rerun", err)
        self.assertIn("10:00–25:00", err)
        self.assertEqual(self.snapshot(self.refresh()), before)
        self.assertEqual((gpu.submits, gpu.capability_calls), ([], 0))

    def test_the_same_options_on_a_published_video_are_not_a_warning(self):
        self.row(ArticleStatus.READY, start=600, end=1500)
        _, err, *_ = self.run_cmd(ZOO_URL, "--start", "10:00", "--end", "25:00")
        self.assertEqual(err, "")

    def test_a_published_whole_video_asked_for_a_range_warns_the_same_way(self):
        self.row(ArticleStatus.READY)
        _, err, *_ = self.run_cmd(ZOO_URL, "--start", "10:00", "--end", "25:00")
        self.assertIn("--rerun", err)
        self.assertIn("整支影片", err)

    def test_a_rerun_changes_the_range_and_the_page_keeps_the_new_one(self):
        self.row(ArticleStatus.READY, start=600, end=1500)
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--rerun", "--start", "20:00", "--end", "35:00",
                                      gpu=ranged_gpu(1200, 2100))
        self.assertEqual(sent(gpu), [("diarize", None, 1200, 2100)])
        article = self.refresh()
        self.assertEqual((article.status, article.range_start, article.range_end), (ArticleStatus.READY, 1200, 2100))
        self.assertIn("處理範圍：20:00–35:00（之前是：10:00–25:00）", out)

    def test_a_rerun_without_the_options_goes_back_to_the_whole_video_and_says_so(self):
        self.row(ArticleStatus.READY, start=600, end=1500)
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--rerun", gpu=base.Gpu(result=diarized_result(ZOO)))
        self.assertIn("處理範圍：整支影片（之前是：10:00–25:00）", out)
        self.assertEqual(sent(gpu), [("diarize", None, None, None)])
        article = self.refresh()
        self.assertEqual((article.range_start, article.range_end), (None, None))
        self.assertNotIn("range_start", article.diarization)

    def test_a_rerun_that_never_got_a_job_puts_the_published_range_back(self):
        """--rerun --start 在送出時就失敗：資料列已經被標成新的範圍，網站上卻還是舊的那一段。"""
        self.row(ArticleStatus.READY, start=600, end=1500)
        _, _, err, _, _ = self.fails(ZOO_URL, "--rerun", "--start", "20:00", "--end", "35:00",
                                     gpu=base.Gpu(error=GpuApiError("連線被拒"), capabilities=dict(RANGE_CAPABILITIES)))
        after = self.refresh()
        self.assertEqual((after.status, after.range_start, after.range_end, after.gpu_job_id),
                         (ArticleStatus.READY, 600, 1500, ""))
        self.assertIn("還原", err)
        self.assertIn("10:00–25:00", err)

    def test_a_rerun_whose_job_failed_puts_the_published_range_back_too(self):
        self.row(ArticleStatus.READY, start=600, end=1500)
        with self.assertLogs("articles.ingest", level="WARNING"):
            self.fails(ZOO_URL, "--rerun", "--start", "20:00", "--end", "35:00",
                       gpu=base.Gpu(error=JobFailed("boom"), capabilities=dict(RANGE_CAPABILITIES)))
        after = self.refresh()
        self.assertEqual((after.range_start, after.range_end, after.gpu_job_id), (600, 1500, ""))

    def test_a_rerun_that_failed_also_puts_a_published_whole_video_back(self):
        self.row(ArticleStatus.READY)
        with self.assertLogs("articles.ingest", level="WARNING"):
            self.fails(ZOO_URL, "--rerun", "--start", "20:00", "--end", "35:00",
                       gpu=base.Gpu(error=JobFailed("boom"), capabilities=dict(RANGE_CAPABILITIES)))
        after = self.refresh()
        self.assertEqual((after.range_start, after.range_end), (None, None))

    def test_a_rerun_that_left_a_job_keeps_the_range_that_job_was_sent_with(self):
        """等太久、連線斷了：GPU 上的工作是用新的範圍送出的，下次接回去要靠資料列記得它。"""

        class TimesOut(base.Gpu):
            def wait(self, job_id, timeout, poll_seconds=3.0, on_progress=None):
                self.waits.append((job_id, timeout))
                raise GpuApiError("等太久")

        self.row(ArticleStatus.READY, start=600, end=1500)
        with self.assertLogs("articles.ingest", level="WARNING"):
            self.fails(ZOO_URL, "--rerun", "--start", "20:00", "--end", "35:00",
                       gpu=TimesOut(capabilities=dict(RANGE_CAPABILITIES)))
        after = self.refresh()
        self.assertEqual((after.range_start, after.range_end, after.gpu_job_id), (1200, 2100, "job-1"))
        # 下一次不加選項：GPU 上那個有範圍的工作不能被悄悄接回去當成整支影片
        message, *_ = self.fails(ZOO_URL, "--rerun", gpu=base.Gpu(
            jobs={"job-1": {"status": "running", "started_at": time.time()}}))
        self.assertIn("--start 20:00 --end 35:00", message)

    def test_a_rerun_needs_the_gpu_to_know_ranges_before_the_row_is_touched(self):
        self.row(ArticleStatus.READY, start=600, end=1500)
        before = self.snapshot(self.refresh())
        message, *_ = self.fails(ZOO_URL, "--rerun", "--start", "20:00", "--end", "35:00",
                                 gpu=base.Gpu(capabilities=dict(READY_CAPABILITIES)))
        self.assertIn("video_range", message)
        self.assertEqual(self.snapshot(self.refresh()), before)

    # --- 接回 GPU 上還在跑的工作：範圍要一樣 ---

    RESUMING = ("processing", "failed", "pending", "ready")

    def lingering(self, state, start, end, **fields):
        """GPU 上還留著工作 J（範圍 start～end）的資料列，回傳要加的參數（已發佈的要 --rerun 才會碰到它）。"""
        Article.objects.all().delete()
        if state == "processing":
            article = self.row(ArticleStatus.PROCESSING, start=start, end=end, gpu_job_id="J", **fields)
            Article.objects.filter(pk=article.pk).update(updated_at=timezone.now() - timedelta(minutes=5))
            return ()
        if state == "failed":
            self.row(ArticleStatus.FAILED, start=start, end=end, gpu_job_id="J", **fields)
            return ()
        if state == "pending":
            self.row(ArticleStatus.PENDING, start=start, end=end, gpu_job_id="J", **fields)
            return ()
        self.row(ArticleStatus.READY, start=start, end=end, gpu_job_id="J", **fields)
        return ("--rerun",)

    def running(self):
        return {"J": {"status": "running", "started_at": time.time() - 60}}

    def test_a_job_with_another_range_is_not_resumed(self):
        """四種會接回舊工作的狀態 × 四種不一樣的範圍：拒絕、說明怎麼辦、資料庫與 GPU 都沒動。"""
        cases = (((600, 1500), ()), ((600, 1500), ("--start", "20:00", "--end", "35:00")),
                 ((600, 1500), ("--start", "10:00")), ((600, None), ("--start", "10:00", "--end", "25:00")),
                 ((None, None), ("--start", "10:00", "--end", "25:00")))
        for state in self.RESUMING:
            for (start, end), options in cases:
                with self.subTest(state=state, stored=(start, end), asked=options):
                    extra = self.lingering(state, start, end)
                    before = self.snapshot(self.refresh())
                    gpu = ranged_gpu(jobs=self.running())
                    message, _, _, gpu, _ = self.fails(ZOO_URL, *extra, *options, gpu=gpu)
                    self.assertIn("GPU 工作 J", message)
                    self.assertIn("--rerun", message)
                    self.assertEqual(self.snapshot(self.refresh()), before)
                    self.assertEqual((gpu.submits, gpu.waits, gpu.capability_calls), ([], [], 0))

    def test_the_refusal_says_which_flags_would_resume_the_job(self):
        extra = self.lingering("processing", 600, 1500)
        message, *_ = self.fails(ZOO_URL, *extra, gpu=ranged_gpu(jobs=self.running()))
        self.assertIn("10:00–25:00", message)
        self.assertIn("整支影片", message)
        self.assertIn("--start 10:00 --end 25:00", message)
        self.lingering("processing", None, None)
        message, *_ = self.fails(ZOO_URL, "--start", "10:00", "--end", "25:00", gpu=ranged_gpu(jobs=self.running()))
        self.assertIn("不要加 --start", message)

    def test_a_job_with_the_same_range_is_resumed_and_the_range_is_printed_before_the_wait(self):
        for state in self.RESUMING:
            with self.subTest(state=state):
                extra = self.lingering(state, 600, 1500)
                gpu = ranged_gpu(jobs={"J": {"status": "done"}})
                out, _, gpu, _ = self.run_cmd(ZOO_URL, *extra, "--start", "10:00", "--end", "25:00", gpu=gpu)
                self.assertEqual((gpu.submits, gpu.capability_calls), ([], 0))
                article = self.refresh()
                self.assertEqual((article.status, article.range_start, article.range_end),
                                 (ArticleStatus.READY, 600, 1500))
                self.assertIn("接回去", out)
                self.assertIn("處理範圍：10:00–25:00", out)
                self.assertLess(out.index("處理範圍：10:00–25:00"), out.index("送去 GPU 主機摘要"))

    def test_a_whole_video_job_is_resumed_by_a_command_without_options(self):
        extra = self.lingering("processing", None, None)
        gpu = base.Gpu(result=diarized_result(ZOO), jobs={"J": {"status": "done"}})
        out, _, gpu, _ = self.run_cmd(ZOO_URL, *extra, gpu=gpu)
        self.assertEqual((gpu.submits, self.refresh().status), ([], ArticleStatus.READY))
        self.assertNotIn("範圍", out)

    def test_a_job_the_gpu_forgot_is_sent_again_with_the_range_asked_for(self):
        extra = self.lingering("processing", 600, 1500)
        out, _, gpu, _ = self.run_cmd(ZOO_URL, *extra, "--start", "20:00", "--end", "35:00",
                                      gpu=ranged_gpu(1200, 2100, jobs={}))
        self.assertEqual(sent(gpu), [("diarize", None, 1200, 2100)])
        self.assertEqual((self.refresh().range_start, self.refresh().range_end), (1200, 2100))

    def test_a_lingering_job_does_not_probe_the_gpu_and_does_not_check_the_length_again(self):
        """接回去不需要問 GPU 能不能區分（工作已經在那裡了）；範圍也已經是送出時檢查過的。"""
        extra = self.lingering("processing", 600, 1500, duration_seconds=0)
        gpu = ranged_gpu(jobs={"J": {"status": "done"}})
        self.run_cmd(ZOO_URL, *extra, "--start", "10:00", "--end", "25:00", gpu=gpu)
        self.assertEqual(gpu.capability_calls, 0)

    # --- --dry-run ---

    def test_a_dry_run_states_the_range_and_what_it_was_without_asking_the_gpu(self):
        self.row(start=600, end=1500)
        before = self.snapshot(self.refresh())
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--dry-run", "--start", "20:00", "--end", "35:00")
        self.assertIn("處理範圍：20:00–35:00（之前是：10:00–25:00）", out)
        self.assertEqual(self.snapshot(self.refresh()), before)
        self.assertEqual((gpu.looked_up, gpu.submits, gpu.capability_calls), ([], [], 0))

    def test_a_dry_run_of_a_rerun_states_it_too(self):
        self.row(ArticleStatus.READY, start=600, end=1500)
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--rerun", "--dry-run")
        self.assertIn("處理範圍：整支影片（之前是：10:00–25:00）", out)
        self.assertEqual((gpu.submits, gpu.capability_calls), ([], 0))

    def test_a_dry_run_with_no_range_before_or_after_prints_no_range_line(self):
        self.row()
        out, *_ = self.run_cmd(ZOO_URL, "--dry-run")
        self.assertNotIn("處理範圍", out)

    def test_a_dry_run_with_the_same_range_prints_it_without_a_before(self):
        self.row(start=600, end=1500)
        out, *_ = self.run_cmd(ZOO_URL, "--dry-run", "--start", "10:00", "--end", "25:00")
        self.assertIn("處理範圍：10:00–25:00", out)
        self.assertNotIn("之前是：10:00", out)
