"""說話者對照表：操作者靠它決定每個編號是誰。連結跳到對的那一秒、警告說成白話、資料庫裡的字串印出來之前再清一次。"""
from __future__ import annotations

import copy
from types import SimpleNamespace

from django.test import SimpleTestCase

from articles.speaker_sheet import (
    WARNING_TEXT,
    clock,
    describe,
    next_step_lines,
    rerun_loss_note,
    sheet_lines,
    watch_link,
)
from articles.speaker_result import with_pi_fields
from articles.tests.speaker_support import CONTRACT

VIDEO = "abcdefghijk"


def article(**changes):
    """契約範例存進資料庫之後的樣子（說話者加上 Pi 的欄位）。"""
    fields = dict(ivod_id=f"yt-{VIDEO}", slug=f"2026-10-30-yt-{VIDEO}",
                  speakers=with_pi_fields(copy.deepcopy(CONTRACT["result"]["speakers"])),
                  diarization=copy.deepcopy(CONTRACT["result"]["diarization"]))
    fields.update(changes)
    return SimpleNamespace(**fields)


class ClockAndLinkTests(SimpleTestCase):
    def test_the_clock_runs_past_ninety_nine_minutes_and_never_goes_negative(self):
        self.assertEqual([clock(s) for s in (0, 5.9, 75.4, 3599, 6000, 11999.9, -4)],
                         ["00:00", "00:05", "01:15", "59:59", "100:00", "199:59", "00:00"])

    def test_the_link_jumps_to_the_whole_second_and_can_start_a_little_early(self):
        self.assertEqual(watch_link(VIDEO, 69.9), f"https://www.youtube.com/watch?v={VIDEO}&t=69s")
        self.assertEqual(watch_link(VIDEO, 69.9, lead=3), f"https://www.youtube.com/watch?v={VIDEO}&t=66s")
        self.assertEqual(watch_link(VIDEO, 1, lead=3), f"https://www.youtube.com/watch?v={VIDEO}&t=0s")


class SheetTests(SimpleTestCase):
    def test_each_speaker_has_its_numbers_its_quotes_and_a_link_per_quote(self):
        text = "\n".join(sheet_lines(article()))
        for expected in ("S1　發言者 A　主要發言者　5.0 分鐘、41 段　00:05～11:42",
                         "S3　其他聲音 1　其他聲音（講得少）", "目前顯示：發言者 A",
                         f"「今天我們來談少子化，先請教您怎麼看目前的補助政策？」00:05 https://www.youtube.com/watch?v={VIDEO}&t=5s",
                         f"https://www.youtube.com/watch?v={VIDEO}&t=31s", f"https://www.youtube.com/watch?v={VIDEO}&t=69s"):
            self.assertIn(expected, text)

    def test_a_name_and_a_merge_show_up_in_the_current_display(self):
        stored = article()
        stored.speakers[0].update(name="黃國昌", role="主持人", basis="operator")
        stored.speakers[2].update(merged_into="S1")
        text = "\n".join(sheet_lines(stored))
        self.assertIn("目前顯示：黃國昌（主持人）", text)
        self.assertIn("目前顯示：併入 S1，顯示成「黃國昌」", text)

    def test_the_header_does_not_say_nobody_is_named_once_somebody_is(self):
        """label_video 設完名字之後也會印這張表：標題不能還說「名字還沒有人確認」。"""
        stored = article()
        stored.speakers[0].update(name="黃國昌", basis="operator")
        header = sheet_lines(stored)[0]
        self.assertNotIn("還沒有", header)
        self.assertIn("沒有設定名字的編號", header)

    def test_the_summary_line_says_how_the_run_went(self):
        text = "\n".join(sheet_lines(article()))
        self.assertIn("區分結果：正常（自動判斷人數，找到 2 位主要發言者、1 個其他聲音）", text)
        stored = article()
        stored.diarization.update(requested_speakers=3, status="warn", warnings=["count_mismatch"])
        text = "\n".join(sheet_lines(stored))
        self.assertIn("區分結果：有警告（指定 3 位", text)

    def test_every_known_warning_is_said_in_plain_chinese_and_never_as_a_code(self):
        for code, words in WARNING_TEXT.items():
            with self.subTest(code=code):
                stored = article()
                stored.diarization.update(status="warn", warnings=[code])
                text = "\n".join(sheet_lines(stored))
                self.assertIn(words, text)
                self.assertNotIn(code, text)

    def test_the_four_contract_warnings_all_have_words(self):
        self.assertEqual(sorted(WARNING_TEXT), sorted(["count_mismatch", "high_unassigned", "many_minor", "single_found"]))

    def test_a_warning_this_version_does_not_know_is_still_shown(self):
        stored = article()
        stored.diarization.update(status="warn", warnings=["from_the_future"])
        text = "\n".join(sheet_lines(stored))
        self.assertIn("不認得的警告：from_the_future", text)

    def test_no_speakers_no_sheet(self):
        for speakers in ([], None, ["說話者 1"], "x"):
            with self.subTest(speakers=speakers):
                self.assertEqual(sheet_lines(article(speakers=speakers)), [])

    def test_no_diarization_summary_is_not_a_crash(self):
        for diarization in (None, [], "x", {}):
            with self.subTest(diarization=diarization):
                text = "\n".join(sheet_lines(article(diarization=diarization)))
                self.assertIn("說話者對照表", text)
                self.assertNotIn("區分結果", text)

    def test_what_the_database_holds_is_cleaned_again_before_it_reaches_the_terminal(self):
        stored = article()
        stored.speakers[0].update(label="\x1b[2J甲", name="\x1b]0;標題\x07乙", role="\u202e丙")
        stored.speakers[0]["samples"][0]["text"] = "\x1b[31m紅\x07字"
        stored.diarization["warnings"] = ["\x1b[2J壞"]
        text = "\n".join(sheet_lines(stored))
        for bad in ("\x1b", "\x07", "\u202e"):
            self.assertNotIn(bad, text)

    def test_odd_stored_values_are_tamed_not_a_crash(self):
        stored = article()
        stored.speakers[0].update(seconds="x", turns=None, first_seen=None, last_seen="y", samples="z")
        stored.speakers[1]["samples"] = [None, "字串", {"start": None, "text": None}]
        lines = sheet_lines(stored)
        self.assertTrue(lines)

    def test_a_row_without_a_video_id_has_quotes_but_no_links(self):
        text = "\n".join(sheet_lines(article(ivod_id="ivod-1")))
        self.assertIn("今天我們來談少子化", text)
        self.assertNotIn("youtube.com", text)


class DescribeTests(SimpleTestCase):
    def test_role_goes_in_parentheses_after_the_name(self):
        stored = article()
        stored.speakers[1].update(name="王小明", role="來賓")
        self.assertEqual(describe(stored.speakers, stored.speakers[1]), "王小明（來賓）")
        self.assertEqual(describe(stored.speakers, stored.speakers[0]), "發言者 A")


class RerunNoteTests(SimpleTestCase):
    def test_a_video_nobody_labelled_has_nothing_to_lose(self):
        self.assertIsNone(rerun_loss_note(article()))
        self.assertIsNone(rerun_loss_note(article(speakers=[])))
        self.assertIsNone(rerun_loss_note(article(speakers=None)))

    def test_names_and_roles_are_listed_with_their_numbers(self):
        stored = article()
        stored.speakers[0].update(name="黃國昌", role="主持人")
        stored.speakers[1].update(name="王小明")
        self.assertEqual(rerun_loss_note(stored),
                         "重跑後說話者編號可能改變，之前設定的名字不會保留：S1=黃國昌（主持人）、S2=王小明")

    def test_merges_are_listed_too(self):
        stored = article()
        stored.speakers[2].update(merged_into="S1")
        note = rerun_loss_note(stored)
        self.assertIn("S3→S1", note)
        stored.speakers[0].update(name="黃國昌")
        self.assertIn("S1=黃國昌", rerun_loss_note(stored))
        self.assertIn("S3→S1", rerun_loss_note(stored))

    def test_a_role_without_a_name_is_still_a_loss(self):
        stored = article()
        stored.speakers[0].update(role="主持人")
        self.assertIn("S1=（沒有名字）（主持人）", rerun_loss_note(stored))

    def test_the_names_are_cleaned_before_they_are_printed(self):
        stored = article()
        stored.speakers[0].update(name="\x1b[2J黃國昌")
        self.assertNotIn("\x1b", rerun_loss_note(stored))


class NextStepTests(SimpleTestCase):
    def test_the_hint_names_the_video_and_does_not_guess_a_name(self):
        text = "\n".join(next_step_lines(article()))
        self.assertIn(f"label_video 2026-10-30-yt-{VIDEO} --name S1=名字 --name S2=名字", text)
        self.assertIn("程式不會猜", text)
        self.assertIn("--merge S3=S1", text)
        self.assertIn("--audit 5", text)
