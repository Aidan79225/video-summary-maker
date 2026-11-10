"""add_video 的多人模式：預設、GPU 能力檢查（登記之前）、--speakers、已存在影片的每一種狀態、做完印的對照表。

假的 GPU 與影片資料沿用 test_add_video 的（patch 掉指令裡的 GpuApiClient 與 fetch_metadata），不碰網路；
多人結果是契約檔的範例（GPU 與 Pi 共用）。單人流程與其他情況在 test_add_video.py。
"""
from __future__ import annotations

import time
from datetime import timedelta

from django.utils import timezone

from articles.gpu_client import NOT_UPDATED, GpuApiError
from articles.models import Article, ArticleStatus
from articles.tests import test_add_video as base
from articles.tests.speaker_support import diarized_result

ZOO, ZOO_URL = base.ZOO, base.ZOO_URL
NOT_READY = {"speaker_diarization": False, "reason": "模型還沒下載"}


def multi_gpu(requested=None, **kwargs):
    """GPU 已更新、結果是契約範例的多人結果（requested＝我們要求的人數）。"""
    return base.Gpu(result=diarized_result(ZOO, requested=requested), **kwargs)


class PerVideoGpu(base.Gpu):
    """批次用：每支影片拿到自己的 video_id 的多人結果。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.videos: dict[str, str] = {}

    def submit(self, url, *args, **kwargs):
        job_id = super().submit(url, *args, **kwargs)
        self.videos[job_id] = url.rsplit("=", 1)[-1]
        return job_id

    def wait(self, job_id, timeout, poll_seconds=3.0, on_progress=None):
        self.waits.append((job_id, timeout))
        return diarized_result(self.videos[job_id])


class ProbeTests(base.CommandTestCase):
    """多人模式送出之前先問 GPU 能不能區分說話者；不能就在登記之前停下來，資料庫完全沒動。"""

    def refused(self, gpu, *args, fragment):
        message, out, err, gpu, fetch = self.fails(ZOO_URL, *args, gpu=gpu)
        self.assertIn(fragment, message)
        self.assertFalse(Article.objects.exists(), "登記了影片")
        self.assertEqual(gpu.submits, [])
        # 第一行與預檢照常印出；擋在登記之前，不是預檢之前
        self.assertTrue(out.splitlines()[0].startswith(f"影片 {ZOO}"))
        return message

    def test_a_gpu_that_can_diarize_lets_the_default_through(self):
        _, _, gpu, _ = self.run_cmd(ZOO_URL, gpu=multi_gpu())
        self.assertEqual(gpu.capability_calls, 1)
        self.assertEqual(self.refresh().status, ArticleStatus.READY)

    def test_a_gpu_without_the_endpoint_is_called_out_as_not_updated(self):
        error = GpuApiError(f"{NOT_UPDATED}（沒有 /capabilities 這個端點）；請先更新 GPU 主機再加影片")
        message = self.refused(base.Gpu(capabilities_error=error), fragment="GPU 服務還沒更新到支援多人區分的版本")
        self.assertIn("沒有登記", message)

    def test_a_gpu_that_cannot_be_reached_is_not_guessed_about(self):
        self.refused(base.Gpu(capabilities_error=GpuApiError("連線被拒")), fragment="連線被拒")

    def test_a_gpu_that_says_no_gives_its_reason(self):
        self.refused(base.Gpu(capabilities=NOT_READY), fragment="模型還沒下載")
        self.refused(base.Gpu(capabilities={"speaker_diarization": False}), fragment="GPU 沒有說明原因")

    def test_the_answer_has_to_be_exactly_true(self):
        """真值不算：字串 "true"、1、"yes" 不是 True；缺這個鍵、None 也不是。"""
        for answer in ("true", "yes", 1, 0, None, [], {"x": 1}):
            with self.subTest(answer=answer):
                capabilities = {"speaker_diarization": answer, "speaker_contract": 1}
                self.refused(base.Gpu(capabilities=capabilities), fragment="不能區分說話者")
        self.refused(base.Gpu(capabilities={"speaker_contract": 1}), fragment="不能區分說話者")

    def test_the_contract_version_has_to_be_exactly_the_one_this_side_speaks(self):
        """布林 true 等於 1、1.0 等於 1，這兩個都不能放行；2、"1"、缺這個鍵也不行。"""
        for version in (2, 0, "1", True, 1.0, None, [1]):
            with self.subTest(version=version):
                capabilities = {"speaker_diarization": True, "reason": "", "speaker_contract": version}
                self.refused(base.Gpu(capabilities=capabilities), fragment="介面版本")
        self.refused(base.Gpu(capabilities={"speaker_diarization": True}), fragment="沒有回報")

    def test_a_reply_that_is_not_an_object_is_refused(self):
        for reply in (["speaker_diarization"], "ok", 1, None, True):
            with self.subTest(reply=reply):
                gpu = base.Gpu()
                gpu._capabilities = reply
                self.refused(gpu, fragment="不是預期的格式")

    def test_the_reason_the_gpu_gives_is_cleaned_before_it_is_printed(self):
        capabilities = {"speaker_diarization": False, "reason": "\x1b[2J\x1b]0;壞\x07原因\n第二行" + "長" * 500}
        message = self.refused(base.Gpu(capabilities=capabilities), fragment="原因")
        self.assertNotIn("\x1b", message)
        self.assertNotIn("\x07", message)
        self.assertLess(len(message), 700)

    def test_a_single_speaker_video_does_not_need_the_gpu_to_diarize(self):
        """--speakers 1 跟第一部分一樣，舊版 GPU 也做得到；問都不問。"""
        _, _, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "1", gpu=base.Gpu(capabilities=NOT_READY))
        self.assertEqual(gpu.capability_calls, 0)
        self.assertEqual(self.refresh().status, ArticleStatus.READY)

    def test_a_fixed_count_needs_it_too(self):
        self.refused(base.Gpu(capabilities=NOT_READY), "--speakers", "3", fragment="模型還沒下載")

    def test_the_check_comes_before_the_registration_not_after(self):
        """攔的 bug：先登記、再發現 GPU 不能用。被擋下來的影片不該留下一列 pending。"""
        self.fails(ZOO_URL, gpu=base.Gpu(capabilities=NOT_READY))
        self.assertEqual(Article.objects.count(), 0)

    def test_the_preflight_still_comes_first(self):
        """預檢沒過就是預檢的原因，不是 GPU 的（也不為一支進不了的影片去問 GPU）。"""
        message, _, _, gpu, _ = self.fails(ZOO_URL, gpu=base.Gpu(capabilities=NOT_READY),
                                           meta=base._meta(is_live=True))
        self.assertIn("預檢沒過", message)
        self.assertEqual(gpu.capability_calls, 0)

    def test_a_whole_batch_asks_once_and_a_good_answer_covers_every_video(self):
        gpu = PerVideoGpu()
        self.run_cmd(ZOO_URL, f"https://youtu.be/{'a' * 11}", gpu=gpu)
        self.assertEqual(gpu.capability_calls, 1)
        self.assertEqual(Article.objects.filter(status=ArticleStatus.READY).count(), 2)


class OptionTests(base.CommandTestCase):
    def test_the_default_asks_for_automatic_multi_speaker(self):
        _, _, gpu, _ = self.run_cmd(ZOO_URL, gpu=multi_gpu())
        self.assertEqual([(s["speaker_mode"], s["speaker_count"]) for s in gpu.submits], [("diarize", None)])
        self.assertIsNone(self.refresh().speaker_count)

    def test_a_fixed_count_is_sent_and_remembered(self):
        _, _, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "3", gpu=multi_gpu(requested=3))
        self.assertEqual([(s["speaker_mode"], s["speaker_count"]) for s in gpu.submits], [("diarize", 3)])
        self.assertEqual(self.refresh().speaker_count, 3)

    def test_the_result_is_stored_with_the_speakers_the_diarization_and_the_points(self):
        self.run_cmd(ZOO_URL, gpu=multi_gpu())
        article = self.refresh()
        self.assertEqual([s["id"] for s in article.speakers], ["S1", "S2", "S3"])
        self.assertEqual(article.diarization["found_speakers"], 2)
        self.assertEqual([len(s.points) for s in article.slides.all()], [4, 2])
        self.assertIsNone(article.brief)

    def test_a_gpu_result_the_pi_cannot_trust_fails_the_video_and_publishes_nothing(self):
        """結果是單人的格式（舊 GPU）：整支影片失敗，不退回「講者」的寫法。"""
        message, *_ = self.fails(ZOO_URL, gpu=base.Gpu())    # base.Gpu 預設結果是單人格式
        self.assertIn("處理失敗", message)
        self.assertIn("沒更新", message.replace("還沒更新", "沒更新"))
        article = self.refresh()
        self.assertEqual(article.status, ArticleStatus.FAILED)
        self.assertEqual((article.speakers, article.slides.count(), article.transcript_text), ([], 0, ""))

    def test_the_mode_is_printed_in_the_data_that_will_be_registered(self):
        out, *_ = self.run_cmd(ZOO_URL, "--dry-run")
        self.assertIn("說話者：自動區分", out)
        out, *_ = self.run_cmd(ZOO_URL, "--speakers", "4", "--dry-run")
        self.assertIn("說話者：固定 4 位", out)
        out, *_ = self.run_cmd(ZOO_URL, "--speakers", "1", "--dry-run")
        self.assertIn("說話者：只有一個人", out)


class ConversationWarningTests(base.CommandTestCase):
    """--speakers 1 但標題或頻道看起來是訪談、對談：提醒一聲，照樣執行。"""

    def warns(self, **meta):
        out, err, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "1", meta=base._meta(**meta))
        self.assertEqual(self.refresh().status, ArticleStatus.READY, "警告不該擋下執行")
        return err

    def test_every_word_in_the_list_warns(self):
        for word in ("專訪", "訪談", "對談", "座談", "圓桌", "來賓", "podcast"):
            with self.subTest(word=word):
                Article.objects.all().delete()
                err = self.warns(title=f"少子化{word}：第 3 集")
                self.assertIn("--speakers 1", err)
                self.assertIn(f"「{word}」", err)

    def test_the_match_ignores_case(self):
        for title in ("PODCAST 第一集", "My Podcast", "PodCast"):
            with self.subTest(title=title):
                Article.objects.all().delete()
                self.assertIn("--speakers 1", self.warns(title=title))

    def test_the_channel_name_counts_too(self):
        self.assertIn("「Podcast」", self.warns(title="第 3 集", channel="某某 Podcast"))

    def test_an_ordinary_title_does_not_warn(self):
        self.assertNotIn("--speakers 1", self.warns(title="演講：少子化的對策", channel="王教授"))

    def test_the_default_and_a_fixed_count_do_not_warn_about_single_speaker(self):
        for extra in ([], ["--speakers", "2"]):
            with self.subTest(extra=extra):
                Article.objects.all().delete()
                gpu = multi_gpu(requested=int(extra[1]) if extra else None)
                _, err, *_ = self.run_cmd(ZOO_URL, *extra, meta=base._meta(title="專訪王教授"), gpu=gpu)
                self.assertNotIn("--speakers 1", err)

    def test_a_dry_run_shows_the_warning_too(self):
        _, err, *_ = self.run_cmd(ZOO_URL, "--speakers", "1", "--dry-run", meta=base._meta(title="圓桌論壇"))
        self.assertIn("「圓桌」", err)

    def test_the_title_a_user_gave_counts_as_the_title(self):
        _, err, *_ = self.run_cmd(ZOO_URL, "--speakers", "1", "--title", "我取的訪談標題")
        self.assertIn("「訪談」", err)

    def test_a_rerun_of_a_published_video_warns_too(self):
        self.article(status=ArticleStatus.READY, title="政治對談")
        _, err, *_ = self.run_cmd(ZOO_URL, "--speakers", "1", "--rerun")
        self.assertIn("「對談」", err)


class ExistingVideoTests(base.CommandTestCase):
    """Part 1 的既有影片狀態表在多人模式下照樣成立：接回工作時不悄悄換模式；要送新工作時才換、而且先問 GPU。"""

    def published_multi(self, **fields):
        """已發佈的多人影片，說話者有操作者取的名字與一組併法。"""
        gpu = multi_gpu()
        self.run_cmd(ZOO_URL, gpu=gpu)
        article = self.refresh()
        article.speakers[0].update(name="黃國昌", role="主持人", basis="operator")
        article.speakers[2].update(merged_into="S1")
        article.save(update_fields=["speakers"])
        for name, value in fields.items():
            Article.objects.filter(pk=article.pk).update(**{name: value})
        return self.refresh()

    def processing(self, count, job="J", minutes_ago=5):
        article = self.article(status=ArticleStatus.PROCESSING, gpu_job_id=job, attempts=1, speaker_count=count)
        Article.objects.filter(pk=article.pk).update(updated_at=timezone.now() - timedelta(minutes=minutes_ago))
        return article

    # --- 已發佈 ---

    def test_a_published_video_without_rerun_is_left_alone_and_the_speakers_option_says_it_did_nothing(self):
        self.article(status=ArticleStatus.READY, speaker_count=None)
        out, err, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "3")
        self.assertIn("已發佈", out)
        self.assertIn("--rerun", err)
        self.assertEqual((gpu.submits, gpu.capability_calls, self.refresh().speaker_count), ([], 0, None))

    def test_a_rerun_prints_the_names_that_will_be_lost_before_anything_is_sent(self):
        self.published_multi()
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--rerun", gpu=multi_gpu())
        note = "重跑後說話者編號可能改變，之前設定的名字不會保留：S1=黃國昌（主持人）"
        self.assertIn(note, out)
        self.assertLess(out.index(note), out.index("送去 GPU 主機摘要"))
        self.assertIn("S3→S1", out)

    def test_a_rerun_really_drops_the_names_and_the_merges(self):
        self.published_multi()
        self.run_cmd(ZOO_URL, "--rerun", gpu=multi_gpu())
        article = self.refresh()
        self.assertEqual({(s["name"], s["role"], s["basis"], s["merged_into"]) for s in article.speakers},
                         {("", "", "none", "")})

    def test_a_rerun_of_a_video_nobody_named_prints_no_note(self):
        self.run_cmd(ZOO_URL, gpu=multi_gpu())
        out, *_ = self.run_cmd(ZOO_URL, "--rerun", gpu=multi_gpu())
        self.assertNotIn("不會保留", out)

    def test_a_dry_run_rerun_prints_the_note_and_touches_nothing(self):
        before = self.published_multi()
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--rerun", "--dry-run")
        self.assertIn("不會保留：S1=黃國昌（主持人）", out)
        self.assertEqual((gpu.submits, gpu.looked_up, gpu.capability_calls), ([], [], 0))
        self.assertEqual(self.refresh().speakers, before.speakers)

    def test_a_failed_rerun_keeps_the_published_content_and_its_names(self):
        before = self.published_multi()
        message, *_ = self.fails(ZOO_URL, "--rerun", gpu=base.Gpu())     # 單人格式的結果：被拒絕
        self.assertIn("影片仍然留在網站上", message)
        after = self.refresh()
        self.assertEqual((after.status, after.speakers), (ArticleStatus.READY, before.speakers))
        self.assertEqual(after.slides.count(), before.slides.count())

    def test_a_rerun_with_a_gpu_that_cannot_diarize_stops_before_anything_changes(self):
        before = self.published_multi()
        message, _, _, gpu, _ = self.fails(ZOO_URL, "--rerun", gpu=base.Gpu(capabilities=NOT_READY))
        self.assertIn("模型還沒下載", message)
        after = self.refresh()
        self.assertEqual((after.speakers, after.speaker_count, after.status, after.updated_at),
                         (before.speakers, before.speaker_count, before.status, before.updated_at))
        self.assertEqual(gpu.submits, [])

    def test_a_rerun_can_change_the_mode_and_says_so(self):
        self.published_multi()
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--rerun", "--speakers", "1")
        self.assertIn("說話者模式：只有一個人", out)
        self.assertIn("之前是：自動區分", out)
        article = self.refresh()
        self.assertEqual((article.speaker_count, article.speakers, article.diarization), (1, [], None))
        self.assertEqual((gpu.submits[0]["speaker_mode"], gpu.capability_calls), (None, 0))
        self.assertEqual([s.points for s in article.slides.all()], [[], []])

    def test_a_rerun_without_the_option_means_automatic_not_whatever_it_was_before(self):
        """一支以前聲明單人的影片，--rerun 不加 --speakers 就是預設的多人：命令列說了算，並且印出來。"""
        self.article(status=ArticleStatus.READY, speaker_count=1)
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--rerun", gpu=multi_gpu())
        self.assertIn("說話者模式：自動區分", out)
        self.assertIn("之前是：只有一個人", out)
        self.assertEqual([(s["speaker_mode"], s["speaker_count"]) for s in gpu.submits], [("diarize", None)])
        self.assertIsNone(self.refresh().speaker_count)

    def test_the_same_mode_prints_no_change_line(self):
        self.run_cmd(ZOO_URL, "--speakers", "2", gpu=multi_gpu(requested=2))
        out, *_ = self.run_cmd(ZOO_URL, "--rerun", "--speakers", "2", gpu=multi_gpu(requested=2))
        self.assertNotIn("說話者模式：", out)

    # --- 接回舊的工作 ---

    def test_a_resumed_job_keeps_the_mode_it_was_sent_with_when_the_option_is_left_out(self):
        self.processing(count=3)
        gpu = multi_gpu(requested=3, jobs={"J": {"status": "done"}})
        out, _, gpu, _ = self.run_cmd(ZOO_URL, gpu=gpu)
        article = self.refresh()
        self.assertEqual((article.status, article.speaker_count), (ArticleStatus.READY, 3))
        self.assertEqual((gpu.submits, gpu.capability_calls), ([], 0))
        self.assertIn("接回去", out)

    def test_a_resumed_job_is_not_switched_to_another_mode_by_the_option(self):
        """攔的 bug：把固定 3 人的工作用 --speakers 1 接回去，結果被記成單人（或反過來）。"""
        for sent, asked in ((3, "1"), (3, "2"), (None, "3"), (None, "1"), (1, "3")):
            with self.subTest(sent=sent, asked=asked):
                Article.objects.all().delete()
                self.processing(count=sent)
                gpu = multi_gpu(jobs={"J": {"status": "running", "started_at": time.time() - 60}})
                message, *_ = self.fails(ZOO_URL, "--speakers", asked, gpu=gpu)
                self.assertIn(f"不能用 --speakers {asked} 接回去", message)
                self.assertIn("--rerun", message)
                article = self.refresh()
                self.assertEqual((article.speaker_count, article.status, article.gpu_job_id, article.attempts),
                                 (sent, ArticleStatus.PROCESSING, "J", 1))
                self.assertEqual((gpu.submits, gpu.waits), ([], []))

    def test_a_resumed_job_with_the_same_option_goes_on(self):
        self.processing(count=3)
        gpu = multi_gpu(requested=3, jobs={"J": {"status": "done"}})
        self.run_cmd(ZOO_URL, "--speakers", "3", gpu=gpu)
        self.assertEqual(self.refresh().status, ArticleStatus.READY)

    def test_a_job_the_gpu_forgot_is_sent_again_in_the_mode_asked_for(self):
        self.processing(count=None)
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--speakers", "2", gpu=multi_gpu(requested=2, jobs={}))
        self.assertEqual([(s["speaker_mode"], s["speaker_count"]) for s in gpu.submits], [("diarize", 2)])
        self.assertEqual(self.refresh().speaker_count, 2)
        self.assertIn("不認得", out)

    def test_a_job_the_gpu_forgot_needs_the_gpu_to_diarize_before_it_is_sent_again(self):
        self.processing(count=None)
        message, _, _, gpu, _ = self.fails(ZOO_URL, gpu=base.Gpu(capabilities=NOT_READY, jobs={}))
        self.assertIn("模型還沒下載", message)
        article = self.refresh()
        self.assertEqual((article.status, article.attempts, article.gpu_job_id), (ArticleStatus.PROCESSING, 1, "J"))
        self.assertEqual(gpu.submits, [])

    def test_a_rerun_that_would_resume_a_job_cannot_change_its_mode_either(self):
        """已發佈的影片上次 --rerun 中斷、GPU 工作還在：這次 --rerun --speakers 不同的值，不能悄悄換掉。"""
        self.published_multi(gpu_job_id="J", speaker_count=3)
        gpu = multi_gpu(requested=3, jobs={"J": {"status": "running", "started_at": time.time() - 60}})
        message, *_ = self.fails(ZOO_URL, "--rerun", "--speakers", "1", gpu=gpu)
        self.assertIn("不能用 --speakers 1 接回去", message)
        article = self.refresh()
        self.assertEqual((article.speaker_count, article.gpu_job_id, article.status), (3, "J", ArticleStatus.READY))

    def test_a_rerun_with_a_job_the_gpu_forgot_may_change_the_mode(self):
        self.published_multi(gpu_job_id="J", speaker_count=3)
        out, _, gpu, _ = self.run_cmd(ZOO_URL, "--rerun", "--speakers", "1", gpu=base.Gpu(jobs={}))
        self.assertEqual(self.refresh().speaker_count, 1)
        self.assertEqual(len(gpu.submits), 1)

    # --- 失敗過的 ---

    def test_a_failed_video_with_a_live_job_cannot_be_switched_either(self):
        self.article(status=ArticleStatus.FAILED, error="x", gpu_job_id="J", speaker_count=None)
        gpu = multi_gpu(jobs={"J": {"status": "running", "started_at": time.time() - 60}})
        message, *_ = self.fails(ZOO_URL, "--speakers", "4", gpu=gpu)
        self.assertIn("不能用 --speakers 4 接回去", message)
        self.assertEqual(self.refresh().status, ArticleStatus.FAILED)

    def test_a_failed_video_is_tried_again_with_the_command_line_asked_for(self):
        self.article(status=ArticleStatus.FAILED, error="x", attempts=3, speaker_count=3)
        out, _, gpu, _ = self.run_cmd(ZOO_URL, gpu=multi_gpu())
        self.assertIn("說話者模式：自動區分", out)
        self.assertEqual([(s["speaker_mode"], s["speaker_count"]) for s in gpu.submits], [("diarize", None)])
        article = self.refresh()
        self.assertEqual((article.status, article.attempts, article.speaker_count), (ArticleStatus.READY, 1, None))


class SheetTests(base.CommandTestCase):
    """做完之後印說話者對照表、下一步的提示，以及 GPU 的警告（白話）。"""

    def test_the_sheet_lists_each_speaker_with_quotes_and_links_that_jump_to_the_second(self):
        out, *_ = self.run_cmd(ZOO_URL, gpu=multi_gpu())
        self.assertIn("說話者對照表", out)
        for text in ("S1　發言者 A　主要發言者", "S2　發言者 B　主要發言者", "S3　其他聲音 1　其他聲音（講得少）",
                     "目前顯示：發言者 A", "今天我們來談少子化", f"https://www.youtube.com/watch?v={ZOO}&t=5s",
                     f"https://www.youtube.com/watch?v={ZOO}&t=31s"):
            self.assertIn(text, out)

    def test_the_next_step_hint_names_the_command_and_the_video(self):
        out, *_ = self.run_cmd(ZOO_URL, gpu=multi_gpu())
        slug = f"2005-04-24-yt-{ZOO}"
        self.assertIn(f"label_video {slug} --name S1=名字 --name S2=名字", out)
        self.assertIn(f"label_video {slug} --audit 5", out)
        self.assertIn("程式不會猜", out)

    def test_nothing_in_the_output_is_a_raw_placeholder(self):
        out, err, *_ = self.run_cmd(ZOO_URL, gpu=multi_gpu())
        self.assertNotIn("[[", out + err)

    def test_the_gpus_warnings_are_said_in_plain_chinese(self):
        result = diarized_result(ZOO, requested=3)
        result["diarization"].update(status="warn", warnings=["count_mismatch", "many_minor"])
        out, *_ = self.run_cmd(ZOO_URL, "--speakers", "3", gpu=base.Gpu(result=result))
        self.assertIn("區分結果：有警告（指定 3 位", out)
        self.assertIn("分出來的人數跟你指定的不一樣", out)
        self.assertIn("只講了幾句話的聲音", out)
        self.assertNotIn("count_mismatch", out)

    def test_a_clean_result_says_so(self):
        out, *_ = self.run_cmd(ZOO_URL, gpu=multi_gpu())
        self.assertIn("區分結果：正常（自動判斷人數，找到 2 位主要發言者、1 個其他聲音）", out)

    def test_a_single_speaker_run_prints_no_sheet_and_no_hint(self):
        out, *_ = self.run_cmd(ZOO_URL, "--speakers", "1")
        self.assertNotIn("對照表", out)
        self.assertNotIn("label_video", out)

    def test_a_failed_run_prints_no_sheet(self):
        _, out, *_ = self.fails(ZOO_URL, gpu=base.Gpu())
        self.assertNotIn("對照表", out)

