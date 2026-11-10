"""label_video 指令：找影片、對照表、取名字／角色／併人／清除（全有或全無）、抽查、不碰 GPU。

規則本身（名字的驗證、併人只併一層…）在 test_speaker_labels.py；這裡測指令把它接到資料庫、參數與輸出上。
"""
from __future__ import annotations

import io
import shutil
import tempfile
import urllib.request
from datetime import date
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from articles import gpu_client
from articles.ingest import register_video
from articles.management.commands import label_video
from articles.models import Article, ArticleSource, ArticleStatus
from articles.tests.speaker_support import publish_diarized

VIDEO = "jNQXAC9IVRw"
SLUG = f"2026-10-30-yt-{VIDEO}"
URL = f"https://www.youtube.com/watch?v={VIDEO}"


class LabelTestCase(TestCase):
    def setUp(self):
        media = tempfile.mkdtemp(prefix="news_label_video_")
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        self.enterContext(override_settings(MEDIA_ROOT=media))
        self.article = self.published()

    @staticmethod
    def published(video_id=VIDEO, **kwargs):
        return publish_diarized(video_id, **kwargs)

    def run_cmd(self, *args):
        out, err = io.StringIO(), io.StringIO()
        call_command("label_video", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def fails(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with self.assertRaises(CommandError) as caught:
            call_command("label_video", *args, stdout=out, stderr=err)
        return str(caught.exception), out.getvalue()

    def speakers(self):
        return Article.objects.get(pk=self.article.pk).speakers

    def row(self, speaker_id):
        return next(s for s in self.speakers() if s["id"] == speaker_id)


class FindingTheVideoTests(LabelTestCase):
    def test_slug_id_and_url_all_find_it(self):
        for ref in (SLUG, VIDEO, URL, f"https://youtu.be/{VIDEO}", f" {SLUG} "):
            with self.subTest(ref=ref):
                out, _ = self.run_cmd(ref)
                self.assertTrue(out.splitlines()[0].startswith(f"影片 {VIDEO}"), out)

    def test_an_unknown_video_says_so(self):
        for ref in ("2026-01-01-yt-aaaaaaaaaaa", "aaaaaaaaaaa", "https://example.com/x", "", "   "):
            with self.subTest(ref=ref):
                message, _ = self.fails(ref)
                self.assertIn("找不到這支影片", message)

    def test_a_council_article_is_not_a_video(self):
        Article.objects.create(ivod_id="900001", slug="2026-08-27-900001", source=ArticleSource.LY, title="質詢",
                               speaker="某委員", date=date(2026, 8, 27), ivod_url="https://example.com/1",
                               status=ArticleStatus.READY)
        message, _ = self.fails("2026-08-27-900001")
        self.assertIn("這不是 YouTube 影片", message)

    def test_a_single_speaker_video_has_nobody_to_label_and_the_message_says_how_to_change_that(self):
        article, _ = register_video("aaaaaaaaaaa", title="演講", speaker="頻道", day=date(2026, 10, 30),
                                    speaker_count=1)
        Article.objects.filter(pk=article.pk).update(status=ArticleStatus.READY)
        message, _ = self.fails("aaaaaaaaaaa")
        self.assertIn("沒有說話者的資料", message)
        self.assertIn("add_video", message)
        self.assertIn("--rerun", message)

    def test_a_video_that_is_not_done_yet_is_called_that(self):
        register_video("aaaaaaaaaaa", title="演講", speaker="頻道", day=date(2026, 10, 30))
        message, _ = self.fails("aaaaaaaaaaa")
        self.assertIn("還沒處理完", message)

    def test_a_dash_id_works_without_the_double_dash(self):
        self.published("-abcdefghij")
        out, _ = self.run_cmd("-abcdefghij")
        self.assertTrue(out.splitlines()[0].startswith("影片 -abcdefghij"))


class SheetTests(LabelTestCase):
    def test_no_options_prints_the_sheet_and_changes_nothing(self):
        before = self.speakers()
        out, _ = self.run_cmd(SLUG)
        self.assertEqual(self.speakers(), before)
        self.assertIn("說話者對照表", out)
        self.assertIn(f"https://www.youtube.com/watch?v={VIDEO}&t=5s", out)
        self.assertIn("label_video", out)

    def test_the_sheet_shows_what_each_number_is_called_now(self):
        self.run_cmd(SLUG, "--name", "S1=黃國昌", "--role", "S1=主持人", "--merge", "S3=S1")
        out, _ = self.run_cmd(SLUG, "--sheet")
        self.assertIn("目前顯示：黃國昌（主持人）", out)
        self.assertIn("目前顯示：併入 S1，顯示成「黃國昌」", out)
        self.assertIn("目前顯示：發言者 B", out)

    def test_the_sheet_after_a_change_comes_after_the_confirmation(self):
        out, _ = self.run_cmd(SLUG, "--name", "S1=黃國昌", "--sheet")
        self.assertLess(out.index("已更新"), out.index("說話者對照表"))

    def test_a_change_alone_does_not_print_the_whole_sheet(self):
        out, _ = self.run_cmd(SLUG, "--name", "S1=黃國昌")
        self.assertNotIn("說話者對照表", out)


class ApplyingTests(LabelTestCase):
    def test_a_name_and_a_role_are_stored_and_confirmed_with_a_sample_sentence(self):
        out, _ = self.run_cmd(SLUG, "--name", "S1=黃國昌", "--name", "S2=王小明", "--role", "S1=主持人")
        self.assertEqual((self.row("S1")["name"], self.row("S1")["role"], self.row("S1")["basis"]),
                         ("黃國昌", "主持人", "operator"))
        self.assertEqual((self.row("S2")["name"], self.row("S2")["basis"]), ("王小明", "operator"))
        self.assertEqual(self.row("S3")["basis"], "none")
        self.assertIn("已更新 2 位說話者", out)
        self.assertIn("S1　發言者 A　→　黃國昌（主持人）", out)
        self.assertIn("例：「今天我們來談少子化", out)

    def test_nothing_else_about_the_video_changes(self):
        before = Article.objects.get(pk=self.article.pk)
        slides = [(s.title, s.bullets, s.detail, s.points) for s in before.slides.all()]
        self.run_cmd(SLUG, "--name", "S1=黃國昌", "--merge", "S3=S1")
        after = Article.objects.get(pk=self.article.pk)
        self.assertEqual((after.transcript_text, after.diarization, after.status, after.speaker_count),
                         (before.transcript_text, before.diarization, before.status, before.speaker_count))
        self.assertEqual([(s.title, s.bullets, s.detail, s.points) for s in after.slides.all()], slides)

    def test_the_gpus_own_fields_of_a_speaker_are_not_touched(self):
        before = {s["id"]: dict(s) for s in self.speakers()}
        self.run_cmd(SLUG, "--name", "S1=黃國昌")
        after = self.row("S1")
        for key in ("id", "label", "kind", "seconds", "turns", "first_seen", "last_seen", "samples", "flags"):
            self.assertEqual(after[key], before["S1"][key], key)

    def test_a_merge_keeps_the_points_on_their_original_speaker(self):
        points_before = [s.points for s in self.article.slides.all()]
        self.run_cmd(SLUG, "--merge", "S2=S1")
        self.assertEqual([s.points for s in Article.objects.get(pk=self.article.pk).slides.all()], points_before)
        self.assertEqual(self.row("S2")["merged_into"], "S1")

    def test_a_merge_forgets_the_merged_numbers_own_name(self):
        self.run_cmd(SLUG, "--name", "S3=社區民眾")
        self.run_cmd(SLUG, "--merge", "S3=S1")
        self.assertEqual((self.row("S3")["name"], self.row("S3")["basis"], self.row("S3")["merged_into"]),
                         ("", "none", "S1"))

    def test_clear_takes_everything_back(self):
        self.run_cmd(SLUG, "--name", "S1=黃國昌", "--role", "S1=主持人", "--merge", "S3=S1")
        self.run_cmd(SLUG, "--clear", "S1", "--clear", "S3")
        for sid in ("S1", "S3"):
            self.assertEqual({k: self.row(sid)[k] for k in ("name", "role", "basis", "merged_into")},
                             {"name": "", "role": "", "basis": "none", "merged_into": ""})

    def test_the_same_settings_again_say_nothing_changed(self):
        self.run_cmd(SLUG, "--name", "S1=黃國昌")
        updated = Article.objects.get(pk=self.article.pk).updated_at
        out, _ = self.run_cmd(SLUG, "--name", "S1=黃國昌")
        self.assertIn("沒有任何變動", out)
        self.assertEqual(Article.objects.get(pk=self.article.pk).updated_at, updated)

    def test_the_names_survive_nothing_but_the_operator_decides_them(self):
        """GPU 的結果裡沒有名字：剛存進去的說話者一律是中性標籤。"""
        self.assertEqual({(s["name"], s["role"], s["basis"], s["merged_into"]) for s in self.speakers()},
                         {("", "", "none", "")})

    def test_the_full_width_equals_sign_works(self):
        self.run_cmd(SLUG, "--name", "S1＝黃國昌")
        self.assertEqual(self.row("S1")["name"], "黃國昌")

    def test_a_name_with_control_characters_is_stored_clean(self):
        self.run_cmd(SLUG, "--name", "S1=黃\x1b[2J國昌\u202e")
        self.assertEqual(self.row("S1")["name"], "黃[2J國昌")


class NothingIsAppliedOnErrorTests(LabelTestCase):
    """先全部驗證，再一次套用：一項不對就整批都不套用，說話者清單和之前逐位元組一樣。"""

    def refused(self, *args, fragment=""):
        before = self.speakers()
        updated = Article.objects.get(pk=self.article.pk).updated_at
        message, out = self.fails(SLUG, *args)
        self.assertIn("一項都沒有套用", message)
        self.assertIn(fragment, message)
        self.assertEqual(self.speakers(), before)
        self.assertEqual(Article.objects.get(pk=self.article.pk).updated_at, updated)
        self.assertNotIn("已更新", out)
        return message

    def test_a_good_setting_next_to_a_bad_one_is_not_applied(self):
        self.refused("--name", "S1=黃國昌", "--name", "S2=[[壞的]]", fragment="[[")
        self.refused("--name", "S1=黃國昌", "--merge", "S1=S9", fragment="沒有 S9")
        self.refused("--clear", "S1", "--name", "S1=甲", "--name", "S3=甲", fragment="都會顯示成")

    def test_the_bad_settings_are_all_named(self):
        message = self.refused("--name", "S1=a:b", "--name", "S9=甲", "--role", "S2=" + "字" * 30, "--clear", "S8")
        self.assertEqual(len([line for line in message.splitlines() if line.strip().startswith("--")]), 4)

    def test_a_malformed_pair_is_refused_before_the_video_is_even_looked_up(self):
        message, out = self.fails("2026-01-01-yt-aaaaaaaaaaa", "--name", "沒有等號")
        self.assertIn("S1=內容", message)
        self.assertNotIn("找不到", message)

    def test_every_malformed_pair_is_listed(self):
        message, _ = self.fails(SLUG, "--name", "x", "--role", "y", "--merge", "z")
        for flag in ("--name", "--role", "--merge"):
            self.assertIn(flag, message)

    def test_the_whole_batch_goes_in_with_one_write(self):
        """全有或全無也要經得起寫入時出事：寫入失敗的話一個字都不能留下。"""
        before = self.speakers()
        with mock.patch.object(Article, "save", side_effect=RuntimeError("寫入失敗")):
            with self.assertRaises(RuntimeError):
                self.run_cmd(SLUG, "--name", "S1=黃國昌", "--name", "S2=王小明")
        self.assertEqual(self.speakers(), before)


class FreshDataTests(LabelTestCase):
    def test_the_change_is_computed_from_the_row_as_it_is_now_not_as_it_was_when_looked_up(self):
        """攔的 bug：指令讀了說話者清單，算新清單的時候 add_video --rerun 剛好做完，換了一批說話者（編號全變）；
        用舊的清單算、寫回去，名字就掛到別人身上了。"""
        stale = Article.objects.get(pk=self.article.pk)
        replacement = [dict(s, label=f"新{s['label']}") for s in stale.speakers[:2]]     # 只剩 S1、S2
        Article.objects.filter(pk=stale.pk).update(speakers=replacement)
        with mock.patch.object(label_video.Command, "_find", return_value=stale):
            message, _ = self.fails(SLUG, "--name", "S3=甲")
        self.assertIn("沒有 S3", message)
        self.assertEqual(self.speakers(), replacement)

    def test_a_video_that_was_removed_in_between_is_called_that(self):
        stale = Article.objects.get(pk=self.article.pk)
        Article.objects.filter(pk=stale.pk).delete()
        with mock.patch.object(label_video.Command, "_find", return_value=stale):
            message, _ = self.fails(SLUG, "--name", "S1=甲")
        self.assertIn("下架", message)

    def test_a_video_whose_speakers_were_swept_away_in_between_is_not_labelled(self):
        stale = Article.objects.get(pk=self.article.pk)
        Article.objects.filter(pk=stale.pk).update(speakers=[])
        with mock.patch.object(label_video.Command, "_find", return_value=stale):
            message, _ = self.fails(SLUG, "--name", "S1=甲")
        self.assertIn("什麼都沒有改", message)


class AuditTests(LabelTestCase):
    def test_it_prints_each_point_with_who_the_evidence_and_a_link_three_seconds_early(self):
        out, _ = self.run_cmd(SLUG, "--audit", "6", "--seed", "1")
        self.assertIn("抽查 6 個重點（共 6 個", out)
        self.assertIn("第 1 頁　[S1 發言者 A] 詢問目前補助政策的成效", out)
        self.assertIn("依據：「先請教您怎麼看目前的補助政策」", out)
        # 第 1 個重點在第 5 秒、第 2 頁的第一個在第 69 秒：往前 3 秒
        self.assertIn(f"https://www.youtube.com/watch?v={VIDEO}&t=2s", out)
        self.assertIn(f"https://www.youtube.com/watch?v={VIDEO}&t=66s", out)
        self.assertIn("第 2 頁　[S2 發言者 B] 主張把公共托育的名額加倍", out)

    def test_a_point_in_the_first_three_seconds_links_to_zero_not_a_negative_time(self):
        Article.objects.filter(pk=self.article.pk).update()
        slide = self.article.slides.first()
        slide.points[0]["start"] = 1.5
        slide.save(update_fields=["points"])
        out, _ = self.run_cmd(SLUG, "--audit", "6", "--seed", "1")
        self.assertIn(f"https://www.youtube.com/watch?v={VIDEO}&t=0s", out)
        self.assertNotIn("&t=-", out)

    def test_the_display_name_is_shown_next_to_the_neutral_label(self):
        self.run_cmd(SLUG, "--name", "S1=黃國昌", "--merge", "S3=S1")
        out, _ = self.run_cmd(SLUG, "--audit", "6", "--seed", "1")
        self.assertIn("[S1 發言者 A／黃國昌]", out)
        self.assertIn("[S2 發言者 B]", out)

    def test_the_same_seed_gives_the_same_points_and_other_seeds_can_differ(self):
        first, _ = self.run_cmd(SLUG, "--audit", "2", "--seed", "7")
        again, _ = self.run_cmd(SLUG, "--audit", "2", "--seed", "7")
        self.assertEqual(first, again)
        others = {self.run_cmd(SLUG, "--audit", "2", "--seed", str(seed))[0] for seed in range(30)}
        self.assertGreater(len(others), 1, "30 個種子抽到的全是同一批")

    def test_the_chosen_points_come_out_in_video_order(self):
        for seed in range(10):
            out, _ = self.run_cmd(SLUG, "--audit", "4", "--seed", str(seed))
            pages = [int(line.split("第 ")[1].split(" 頁")[0]) for line in out.splitlines() if "頁　[" in line]
            self.assertEqual(pages, sorted(pages))
            self.assertEqual(len(pages), 4)

    def test_asking_for_more_than_there_are_gives_all_of_them_once(self):
        out, _ = self.run_cmd(SLUG, "--audit", "50", "--seed", "3")
        self.assertIn("抽查 6 個重點（共 6 個", out)
        self.assertEqual(len([line for line in out.splitlines() if "頁　[" in line]), 6)

    def test_without_a_seed_one_is_chosen_and_printed_so_the_batch_can_be_drawn_again(self):
        out, _ = self.run_cmd(SLUG, "--audit", "2")
        seed = out.split("抽樣種子 ")[1].split("，")[0]
        again, _ = self.run_cmd(SLUG, "--audit", "2", "--seed", seed)
        self.assertEqual(out.split("\n", 1)[1].split("\n", 1)[1], again.split("\n", 1)[1].split("\n", 1)[1])

    def test_audit_never_changes_anything(self):
        before = self.speakers()
        self.run_cmd(SLUG, "--audit", "3", "--seed", "1")
        self.assertEqual(self.speakers(), before)

    def test_a_change_and_an_audit_in_one_command_audit_the_new_names(self):
        out, _ = self.run_cmd(SLUG, "--name", "S2=王小明", "--audit", "6", "--seed", "1")
        self.assertLess(out.index("已更新"), out.index("抽查"))
        self.assertIn("[S2 發言者 B／王小明]", out)
        self.assertNotIn("說話者對照表", out)

    def test_bad_numbers_are_refused_before_anything_happens(self):
        for value in ("0", "101", "-1", "abc", "", "1.5", "１", "9" * 30):
            with self.subTest(audit=value):
                message, out = self.fails(SLUG, "--audit", value)
                self.assertIn("--audit 要是 1～100 的整數", message)
                self.assertEqual(out, "")
        for value in ("-1", "x", "", "9" * 30, "2147483648"):
            with self.subTest(seed=value):
                message, _ = self.fails(SLUG, "--audit", "1", "--seed", value)
                self.assertIn("--seed 要是", message)

    def test_a_seed_without_an_audit_is_a_mistake_not_ignored(self):
        message, _ = self.fails(SLUG, "--seed", "5")
        self.assertIn("--seed 只搭配 --audit", message)

    def test_a_video_with_no_points_says_so(self):
        for slide in self.article.slides.all():
            slide.points = []
            slide.save(update_fields=["points"])
        out, _ = self.run_cmd(SLUG, "--audit", "3", "--seed", "1")
        self.assertIn("沒有可以抽查的重點", out)

    def test_odd_stored_points_are_skipped_not_a_crash(self):
        slide = self.article.slides.first()
        slide.points = ["junk", {"text": "沒有說話者"}, {"speaker": 5}, *slide.points]
        slide.save(update_fields=["points"])
        out, _ = self.run_cmd(SLUG, "--audit", "50", "--seed", "1")
        self.assertIn("抽查 6 個重點（共 6 個", out)


class NoGpuTests(LabelTestCase):
    def test_the_command_never_builds_a_gpu_client_or_opens_a_connection(self):
        """label_video 只改資料庫。把可能上網的入口全部炸掉，每一種用法都照樣完成。"""
        boom = AssertionError("label_video 不該連 GPU 或網路")
        with mock.patch.object(gpu_client.GpuApiClient, "__init__", side_effect=boom), \
                mock.patch.object(gpu_client.GpuApiClient, "_call", side_effect=boom), \
                mock.patch.object(urllib.request, "urlopen", side_effect=boom), \
                mock.patch("socket.socket.connect", side_effect=boom):
            self.run_cmd(SLUG)
            self.run_cmd(SLUG, "--name", "S1=黃國昌", "--merge", "S3=S1")
            self.run_cmd(SLUG, "--audit", "3", "--seed", "1")
            self.run_cmd(SLUG, "--clear", "S1")

    def test_the_module_does_not_even_import_the_client(self):
        self.assertFalse(hasattr(label_video, "GpuApiClient"))
        self.assertFalse(hasattr(label_video, "settings"))


class ApiIntegrationTests(LabelTestCase):
    """設定完網站讀到的就變了（API 在讀取時才把 [[Sn]] 換成名字）。"""

    def detail(self):
        from django.test import Client
        return Client().get(f"/api/articles/{SLUG}").json()

    def test_the_names_show_up_in_the_api_without_touching_the_stored_text(self):
        stored = [s.bullets for s in self.article.slides.all()]
        self.assertIn("[[S1]]", stored[0][0])
        before = self.detail()
        self.assertTrue(before["slides"][0]["bullets"][0].startswith("發言者 A："))
        self.run_cmd(SLUG, "--name", "S1=黃國昌", "--merge", "S3=S1")
        after = self.detail()
        self.assertTrue(after["slides"][0]["bullets"][0].startswith("黃國昌："))
        self.assertIn("黃國昌：", after["transcript_text"])
        self.assertEqual([s.bullets for s in Article.objects.get(pk=self.article.pk).slides.all()], stored)
