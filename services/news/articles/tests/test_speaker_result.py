"""check_speaker_result 與三個白名單清理：契約範例要過，每一條規則都有一個「拿掉這條規則就會失敗」的反例。

全是純函式測試，不碰資料庫、不碰網路。結果的範例是 GPU 與 Pi 共用的契約檔（fixtures/speaker_contract_v1.json）。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

from django.test import SimpleTestCase

from articles.gpu_client import JobFailed
from articles.speaker_result import (
    _number,
    check_single_result,
    check_speaker_result,
    clean_diarization,
    clean_points,
    clean_speakers,
    requested_speaker_count,
    with_pi_fields,
)

CONTRACT = json.loads((Path(__file__).parent / "fixtures" / "speaker_contract_v1.json").read_text("utf-8"))
NOT_UPDATED = "還沒更新"


def good():
    return copy.deepcopy(CONTRACT["result"])


def video(count=None, ivod_id="yt-abcdefghijk"):
    """契約範例的影片（video_id abcdefghijk）。"""
    return SimpleNamespace(speaker_count=count, ivod_id=ivod_id)


def slide_of(payload, index=0):
    return payload["slides"][index]


class AcceptanceTests(SimpleTestCase):
    def test_the_contract_example_is_accepted_and_comes_back_clean(self):
        checked = check_speaker_result(video(), good())
        self.assertEqual(checked.speakers, CONTRACT["result"]["speakers"])
        self.assertEqual(checked.diarization["status"], "ok")
        self.assertEqual((checked.diarization["engine"], checked.diarization["found_speakers"],
                          checked.diarization["requested_speakers"]), ("pyannote.audio", 2, None))
        self.assertEqual([s.points for s in checked.slides], [s["points"] for s in CONTRACT["result"]["slides"]])
        self.assertEqual([s.bullets for s in checked.slides], [s["bullets"] for s in CONTRACT["result"]["slides"]])
        self.assertEqual([s.detail for s in checked.slides], [s["detail"] for s in CONTRACT["result"]["slides"]])
        self.assertEqual(checked.transcript_text, CONTRACT["result"]["transcript_text"])

    def test_the_diarization_summary_is_kept_in_full(self):
        stored = check_speaker_result(video(), good()).diarization
        self.assertEqual(stored, CONTRACT["result"]["diarization"])

    def test_a_requested_count_is_accepted_when_it_is_the_one_we_asked_for(self):
        payload = good()
        payload["diarization"]["requested_speakers"] = 3
        self.assertEqual(check_speaker_result(video(3), payload).diarization["requested_speakers"], 3)

    def test_the_gpu_brief_is_not_part_of_the_result(self):
        payload = good()
        payload["brief"] = {"one_liner": "GPU 偷偷產的摘要卡", "key_numbers": [], "asks": []}
        checked = check_speaker_result(video(), payload)
        self.assertFalse(hasattr(checked, "brief"))
        self.assertNotIn("GPU 偷偷產的摘要卡", repr(checked))

    def test_unknown_keys_are_dropped(self):
        payload = good()
        payload["speakers"][0]["secret"] = "x"
        payload["diarization"]["extra"] = "y"
        slide_of(payload)["points"][0]["unknown"] = "z"
        checked = check_speaker_result(video(), payload)
        self.assertNotIn("secret", checked.speakers[0])
        self.assertNotIn("extra", checked.diarization)
        self.assertNotIn("unknown", checked.slides[0].points[0])

    def test_the_pi_adds_its_own_fields_and_no_name_ever_comes_from_the_gpu(self):
        payload = good()
        payload["speakers"][0].update(name="黃國昌", role="主持人", basis="operator", merged_into="S2")
        stored = with_pi_fields(check_speaker_result(video(), payload).speakers)
        for speaker in stored:
            self.assertEqual((speaker["name"], speaker["role"], speaker["basis"], speaker["merged_into"]),
                             ("", "", "none", ""))

    def test_how_many_speakers_were_asked_for(self):
        self.assertEqual([requested_speaker_count(video(c)) for c in (None, 1, 2, 3, 20, 0, True)],
                         [None, None, 2, 3, 20, None, None])


class EchoTests(SimpleTestCase):
    """結果必須自己說「我是多人結果」，而且跟我們要的一樣。"""

    def fails(self, payload, fragment, count=None):
        with self.assertRaises(JobFailed) as caught:
            check_speaker_result(video(count), payload)
        self.assertIn(fragment, str(caught.exception))
        return str(caught.exception)

    def test_a_result_that_does_not_say_it_is_diarized_names_the_gpu_as_the_likely_cause(self):
        payload = good()
        del payload["speaker_mode"]
        message = self.fails(payload, NOT_UPDATED)
        self.assertIn("speaker_mode", message)

    def test_a_single_speaker_result_is_not_a_multi_speaker_result(self):
        for mode in ("single", "multi", "DIARIZE", "", None, 5, ["diarize"]):
            with self.subTest(mode=mode):
                payload = good()
                payload["speaker_mode"] = mode
                self.fails(payload, NOT_UPDATED)

    def test_the_cue_level_result_of_the_base_branch_is_refused_with_the_cause(self):
        """base 分支 GPU：說話者是一串文字、沒有 diarization、沒有 speaker_mode。"""
        payload = {"video_id": "x", "title": "t", "source_note": "n", "transcript_text": "00:00 [說話者 1] 嗨",
                   "brief": None, "speakers": ["說話者 1", "說話者 2"],
                   "slides": [{"index": 1, "title": "a", "bullets": ["b"], "detail": "c", "timestamp": 1.0}]}
        self.fails(payload, NOT_UPDATED)

    def test_a_string_list_is_the_old_format_even_when_the_mode_claims_otherwise(self):
        payload = good()
        payload["speakers"] = ["說話者 1", "說話者 2"]
        message = self.fails(payload, NOT_UPDATED)
        self.assertIn("一串文字", message)

    def test_an_old_gpu_result_without_speakers_at_all_is_refused(self):
        payload = good()
        del payload["diarization"]
        del payload["speakers"]
        self.fails(payload, NOT_UPDATED)

    def test_a_result_that_is_not_an_object_is_refused(self):
        for junk in (None, [], "diarize", 5, [good()]):
            with self.subTest(junk=junk), self.assertRaises(JobFailed):
                check_speaker_result(video(), junk)

    def test_the_requested_count_must_be_the_one_we_asked_for(self):
        for asked, echoed in ((None, 3), (None, 2), (2, None), (3, 2), (2, 3), (20, 2)):
            with self.subTest(asked=asked, echoed=echoed):
                payload = good()
                payload["diarization"]["requested_speakers"] = echoed
                self.fails(payload, "requested_speakers 對不上", count=asked)

    def test_an_echo_of_one_is_never_a_valid_multi_request(self):
        payload = good()
        payload["diarization"]["requested_speakers"] = 1
        with self.assertRaises(JobFailed):
            check_speaker_result(video(None), payload)

    def test_a_result_is_not_checked_against_a_stale_count(self):
        payload = good()
        payload["diarization"]["requested_speakers"] = 4
        self.fails(payload, "requested_speakers 對不上", count=2)
        # 同一份結果，我們要的是 4 人就過
        self.assertEqual(check_speaker_result(video(4), payload).diarization["requested_speakers"], 4)

    def test_a_result_for_another_video_is_refused(self):
        """拿到別支影片的說話者清單，會把別人的話安到這支影片的人頭上。"""
        for other in ("zzzzzzzzzzz", "ABCDEFGHIJK", "", 5, ["abcdefghijk"]):
            with self.subTest(video_id=other):
                payload = good()
                payload["video_id"] = other
                self.fails(payload, "另一支影片")

    def test_a_result_for_this_video_passes_whatever_the_other_ids_look_like(self):
        payload = good()
        payload["video_id"] = "-abcdefghij"
        check_speaker_result(video(ivod_id="yt--abcdefghij"), payload)

    def test_a_row_whose_id_is_not_a_video_id_cannot_match_any_result(self):
        with self.assertRaises(JobFailed):
            check_speaker_result(video(ivod_id="ivod-1234"), good())


class DiarizationTests(SimpleTestCase):
    def fails(self, mutate, fragment):
        payload = good()
        mutate(payload["diarization"])
        with self.assertRaises(JobFailed) as caught:
            check_speaker_result(video(), payload)
        self.assertIn(fragment, str(caught.exception))

    def test_it_must_be_an_object(self):
        for junk in ("ok", [], 5, True):
            with self.subTest(junk=junk):
                payload = good()
                payload["diarization"] = junk
                with self.assertRaises(JobFailed):
                    check_speaker_result(video(), payload)

    def test_the_status_is_one_of_the_two_known_ones(self):
        for status in ("bad", "OK", "", None, 1, True, ["ok"], "failed"):
            with self.subTest(status=status):
                self.fails(lambda d, s=status: d.update(status=s), "diarization.status")
        self.fails(lambda d: d.pop("status"), "diarization.status")

    def test_at_least_one_speaker_must_have_been_found(self):
        for found in (0, -1, None, "2", 2.5, True, 10**9, 10**400):
            with self.subTest(found=found):
                self.fails(lambda d, f=found: d.update(found_speakers=f), "diarization.found_speakers")
        self.fails(lambda d: d.pop("found_speakers"), "diarization.found_speakers")

    def test_the_numbers_are_finite_and_in_range(self):
        for key, bad in (("speech_seconds", float("nan")), ("speech_seconds", float("inf")),
                         ("speech_seconds", -1), ("speech_seconds", 10**9), ("speech_seconds", "711.9"),
                         ("unassigned_ratio", 1.5), ("unassigned_ratio", float("nan")), ("unassigned_ratio", -0.1),
                         ("overlap_ratio", 2), ("overlap_ratio", None), ("overlap_ratio", True),
                         ("proposed_points", -1), ("proposed_points", 1.5), ("proposed_points", 10**9),
                         ("kept_points", "5"), ("kept_points", None), ("minor_speakers", -1),
                         ("minor_speakers", 10**9)):
            with self.subTest(key=key, bad=bad):
                self.fails(lambda d, k=key, b=bad: d.update({k: b}), f"diarization.{key}")

    def test_the_requested_count_is_checked_by_the_cleaner_itself(self):
        """echo 的比對會擋掉不是我們要的人數；清理這一步自己也要只認「2～20 的整數或 null」。"""
        base = CONTRACT["result"]["diarization"]
        for bad in (0, 1, 21, 10**9, 10**400, -2, True, "3", 2.5, [3]):
            with self.subTest(bad=bad), self.assertRaises(JobFailed):
                clean_diarization({**base, "requested_speakers": bad})
        for value in (None, 2, 7, 20):
            self.assertEqual(clean_diarization({**base, "requested_speakers": value})["requested_speakers"], value)

    def test_known_warnings_pass_and_make_the_status_a_warning(self):
        payload = good()
        payload["diarization"]["warnings"] = ["count_mismatch", "many_minor", "count_mismatch"]
        stored = check_speaker_result(video(), payload).diarization
        self.assertEqual((stored["status"], stored["warnings"]), ("warn", ["count_mismatch", "many_minor"]))

    def test_a_warning_the_pi_cannot_read_is_not_lost_it_turns_the_status_into_a_warning(self):
        payload = good()
        payload["diarization"]["warnings"] = ["brand_new_warning", 5, None, {"x": 1}]
        stored = check_speaker_result(video(), payload).diarization
        self.assertEqual((stored["status"], stored["warnings"]), ("warn", []))

    def test_descriptive_strings_are_cleaned_and_cut_not_trusted(self):
        payload = good()
        payload["diarization"].update(engine="e\x1b[2J\u202e" * 100, version=5, model=None, fingerprint="not-hex")
        stored = check_speaker_result(video(), payload).diarization
        self.assertLessEqual(len(stored["engine"]), 120)
        self.assertNotIn("\x1b", stored["engine"])
        self.assertNotIn("\u202e", stored["engine"])
        self.assertEqual((stored["version"], stored["model"], stored["fingerprint"]), ("", "", ""))

    def test_the_cleaner_alone_returns_exactly_the_contract_keys(self):
        self.assertEqual(sorted(clean_diarization(CONTRACT["result"]["diarization"])), sorted(
            ["engine", "version", "model", "fingerprint", "status", "warnings", "requested_speakers",
             "found_speakers", "minor_speakers", "speech_seconds", "unassigned_ratio", "overlap_ratio",
             "proposed_points", "kept_points"]))


class NumberHelperTests(SimpleTestCase):
    def test_a_number_has_to_be_finite_whatever_range_it_is_checked_against(self):
        """NaN 剛好過不了範圍檢查，但範圍放寬到無限大時無限大就過得了：「有限」這件事要自己成立。"""
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad), self.assertRaisesRegex(JobFailed, "有限"):
                _number(bad, "x", low=float("-inf"), high=float("inf"))
        self.assertEqual(_number(3, "x"), 3.0)


class SpeakersTests(SimpleTestCase):
    def fails(self, mutate, fragment):
        payload = good()
        mutate(payload["speakers"])
        with self.assertRaises(JobFailed) as caught:
            check_speaker_result(video(), payload)
        self.assertIn(fragment, str(caught.exception))
        return str(caught.exception)

    def field(self, key, value, fragment=None):
        self.fails(lambda speakers: speakers[0].update({key: value}), fragment or f"speakers[0].{key}")

    def test_the_list_has_between_one_and_twelve_speakers(self):
        self.fails(lambda s: s.clear(), "1～12")
        template = good()["speakers"][0]
        many = [{**template, "id": f"S{n}", "label": f"發言者 {n}"} for n in range(1, 14)]
        self.fails(lambda s: s.__init__(many), "1～12")
        twelve = [{**template, "id": f"S{n}", "label": f"發言者 {n}"} for n in range(1, 13)]
        payload = good()
        payload["speakers"] = twelve
        self.assertEqual(len(check_speaker_result(video(), payload).speakers), 12)

    def test_it_must_be_a_list_of_objects(self):
        for junk in (None, "S1", {"S1": {}}, 5):
            with self.subTest(junk=junk):
                payload = good()
                payload["speakers"] = junk
                with self.assertRaises(JobFailed):
                    check_speaker_result(video(), payload)
        self.fails(lambda s: s.append("S4"), "speakers[3]")

    def test_the_ids_are_s1_s2_in_the_pattern_and_never_s0(self):
        for bad in ("S0", "s1", "S01", "S1000", "", None, 5, "S1\n", "S-1", "SS1", "X1", ["S1"]):
            with self.subTest(bad=bad):
                self.field("id", bad)
        self.fails(lambda s: s[1].update(id="S1"), "重複")

    def test_the_kind_is_participant_or_minor(self):
        for bad in ("host", "Participant", "", None, 1, ["minor"]):
            with self.subTest(bad=bad):
                self.field("kind", bad)

    def test_at_least_one_speaker_is_a_participant(self):
        payload = good()
        for speaker in payload["speakers"]:
            speaker["kind"] = "minor"
        with self.assertRaises(JobFailed) as caught:
            check_speaker_result(video(), payload)
        self.assertIn("participant", str(caught.exception))

    def test_the_label_is_short_plain_and_unique(self):
        for bad in ("", "   ", None, 5, "長" * 41, "[[S1]]", "發言者 ]] A", "\x00", "\u202e"):
            with self.subTest(bad=bad):
                self.field("label", bad)
        self.fails(lambda s: s[1].update(label=s[0]["label"]), "重複")

    def test_the_label_is_cleaned_not_copied(self):
        payload = good()
        payload["speakers"][0]["label"] = "發言者\x1b[0m\u202e A"
        self.assertEqual(check_speaker_result(video(), payload).speakers[0]["label"], "發言者[0m A")

    def test_a_label_cannot_forge_a_placeholder_with_invisible_characters(self):
        """零寬字元夾在 [[ 之間：先檢查再清理的話檢查看不到，清理之後卻變成真的 [[S1]]。"""
        for forged in ("[\u200b[S1]]", "[[\u200bS1]]", "[\x00[S1]]", "[\u202e[S1]]",
                       "[\u200b[S1]\u200b]", "[\x00[S1]\x00]", "[\u202e[S1]\u202e]", "[\u200b[\u200bS1]\u200b]"):
            with self.subTest(forged=forged):
                self.field("label", forged)

    def test_seconds_turns_and_times_are_finite_and_sane(self):
        for key in ("seconds", "first_seen", "last_seen"):
            for bad in (float("nan"), float("inf"), float("-inf"), -1, -0.001, 10**9, 10**400, "301.5", None, True,
                        [1]):
                with self.subTest(key=key, bad=bad):
                    self.field(key, bad)
        for bad in (-1, 1.5, 10**9, "41", None, True, float("nan")):
            with self.subTest(key="turns", bad=bad):
                self.field("turns", bad)

    def test_the_ints_are_accepted_where_a_float_is_expected(self):
        payload = good()
        payload["speakers"][0].update(seconds=301, first_seen=5, last_seen=702)
        self.assertEqual(check_speaker_result(video(), payload).speakers[0]["seconds"], 301.0)

    def test_samples_are_a_nicety_bad_ones_are_dropped_not_fatal(self):
        payload = good()
        payload["speakers"][0]["samples"] = [
            {"start": 1.0, "text": "好的一句"}, {"start": float("nan"), "text": "壞時間"}, {"start": 2.0, "text": 5},
            "junk", {"start": 3.0, "text": "含有 [[S2]] 記號"}, {"start": 4.0, "text": "x" * 200},
            {"start": 5.0, "text": "第四個"}, {"start": 6.0, "text": "第五個"}]
        samples = check_speaker_result(video(), payload).speakers[0]["samples"]
        self.assertEqual([s["start"] for s in samples], [1.0, 4.0, 5.0])
        self.assertEqual(len(samples[1]["text"]), 80)
        payload["speakers"][0]["samples"] = "not a list"
        self.assertEqual(check_speaker_result(video(), payload).speakers[0]["samples"], [])

    def test_flags_are_short_lowercase_words(self):
        payload = good()
        payload["speakers"][0]["flags"] = ["short_turns", "short_turns", "BAD", "x" * 40, 5, None, "ok_1"] + \
            [f"f{n}" for n in range(20)]
        flags = check_speaker_result(video(), payload).speakers[0]["flags"]
        self.assertEqual(flags[:2], ["short_turns", "ok_1"])
        self.assertEqual(len(flags), 8)

    def test_the_cleaner_alone_drops_unknown_keys_and_keeps_the_order(self):
        cleaned = clean_speakers(CONTRACT["result"]["speakers"])
        self.assertEqual([s["id"] for s in cleaned], ["S1", "S2", "S3"])
        self.assertEqual(sorted(cleaned[0]), sorted(
            ["id", "label", "kind", "seconds", "turns", "first_seen", "last_seen", "samples", "flags"]))


class PointsTests(SimpleTestCase):
    def fails(self, mutate, fragment, count=None):
        payload = good()
        mutate(payload)
        with self.assertRaises(JobFailed) as caught:
            check_speaker_result(video(count), payload)
        self.assertIn(fragment, str(caught.exception))
        return str(caught.exception)

    def point(self, key, value, fragment=None, position=1):
        self.fails(lambda p: slide_of(p)["points"][position].update({key: value}),
                   fragment or f"slides[0].points[{position}].{key}")

    def test_every_slide_needs_a_point(self):
        for junk in ([], None, "x", {}, 5):
            with self.subTest(junk=junk):
                self.fails(lambda p, j=junk: slide_of(p, 1).update(points=j), "slides[1].points")
        self.fails(lambda p: slide_of(p, 1).pop("points"), "slides[1].points")

    def test_a_slide_with_too_many_points_is_refused(self):
        def flood(payload):
            slide = slide_of(payload)
            slide["points"] = [dict(slide["points"][0], turn=n) for n in range(101)]

        self.fails(flood, "超過上限")

    def test_the_slides_are_a_non_empty_bounded_list_of_objects(self):
        for junk in ([], None, "x", 5, {"a": 1}):
            with self.subTest(junk=junk):
                self.fails(lambda p, j=junk: p.update(slides=j), "slides 要有")
        self.fails(lambda p: p["slides"].append("junk"), "slides[2]")
        self.fails(lambda p: p.update(slides=[p["slides"][0]] * 201), "slides 要有")

    def test_a_point_is_an_object(self):
        self.fails(lambda p: slide_of(p)["points"].append("junk"), "slides[0].points[4]")

    def test_the_speaker_must_be_a_listed_participant(self):
        """重點只能出自主要發言者：其他聲音（minor）、S0、沒列出來的編號、型別不對的都不行。"""
        for bad in ("S3", "S0", "S9", "s1", "", None, 1, ["S1"], {"id": "S1"}, "S1 "):
            with self.subTest(bad=bad):
                self.point("speaker", bad)
        self.fails(lambda p: slide_of(p)["points"][0].pop("speaker"), "speaker")

    def test_a_minor_voice_is_named_in_the_message(self):
        message = self.fails(lambda p: slide_of(p)["points"][0].update(speaker="S3"), "S3")
        self.assertIn("主要發言者", message)

    def test_the_turn_is_a_non_negative_integer(self):
        for bad in (-1, 1.5, "3", True, None, 10**9, 10**400, float("nan"), [1]):
            with self.subTest(bad=bad):
                self.point("turn", bad)

    def test_the_start_time_is_finite_and_non_negative(self):
        for bad in (-1, -0.5, float("nan"), float("inf"), float("-inf"), 10**9, 10**400, "5", None, True, [5]):
            with self.subTest(bad=bad):
                self.point("start", bad)

    def test_the_text_is_one_to_one_hundred_twenty_characters(self):
        for bad in ("", "   ", "x" * 121, None, 5, ["x"], "\x1b\x00", "\u202e\u202e"):
            with self.subTest(bad=bad):
                self.point("text", bad)
        payload = good()
        slide_of(payload)["points"][0]["text"] = "字" * 120
        slide_of(payload)["bullets"][0] = "[[S1]]：" + "字" * 120
        self.assertEqual(len(check_speaker_result(video(), payload).slides[0].points[0]["text"]), 120)

    def test_the_text_is_cleaned_before_it_is_measured(self):
        payload = good()
        slide_of(payload)["points"][0]["text"] = "字" * 119 + "\x1b\x1b\x1b\u200b\u202e"
        slide_of(payload)["bullets"][0] = "[[S1]]：" + "字" * 119
        self.assertEqual(len(check_speaker_result(video(), payload).slides[0].points[0]["text"]), 119)

    def test_the_text_never_contains_a_placeholder(self):
        for forged in ("[[S2]]：我同意", "他說 [[S2]]", "[[", "半個 [[S", "[\u200b[S2]]", "[\u202e[S2]]", "[\x1b[S2]]",
                       "[\x00[S2]]"):
            with self.subTest(forged=forged):
                self.point("text", forged)

    def test_the_text_may_contain_an_ordinary_square_bracket(self):
        payload = good()
        slide_of(payload)["points"][0]["text"] = "提到[附件]三"
        slide_of(payload)["bullets"][0] = "[[S1]]：提到[附件]三"
        self.assertEqual(check_speaker_result(video(), payload).slides[0].points[0]["text"], "提到[附件]三")

    def test_the_evidence_is_text_up_to_one_hundred_twenty_characters(self):
        for bad in ("x" * 121, None, 5, ["x"]):
            with self.subTest(bad=bad):
                self.point("evidence", bad)
        payload = good()
        slide_of(payload)["points"][0]["evidence"] = "字" * 120
        self.assertEqual(len(check_speaker_result(video(), payload).slides[0].points[0]["evidence"]), 120)

    def test_the_evidence_is_cleaned_of_control_characters(self):
        payload = good()
        slide_of(payload)["points"][0]["evidence"] = "原話\x1b[2J\u202e\u200b結尾"
        self.assertEqual(check_speaker_result(video(), payload).slides[0].points[0]["evidence"], "原話[2J結尾")

    def test_one_turn_cannot_be_two_people(self):
        """同一個發言段落被標成兩個不同的人：不可能兩個都對。連不同頁之間也要一致。"""
        def clash(payload):
            slide_of(payload, 1)["points"][0].update(turn=0, speaker="S2")
            slide_of(payload, 1)["bullets"][0] = "[[S2]]：詢問預算要怎麼編才夠"
            slide_of(payload, 1)["detail"] = ("[[S2]]：詢問預算要怎麼編才夠。 [[S2]]：主張把公共托育的名額加倍，"
                                              "並提高保母的薪資。")
            slide_of(payload, 1)["points"][1].update(speaker="S2")

        self.fails(clash, "發言段落 0")

    def test_one_turn_can_carry_several_points_by_the_same_speaker(self):
        payload = good()
        slide_of(payload)["points"].append(dict(slide_of(payload)["points"][3], text="再補充一點"))
        checked = check_speaker_result(video(), payload)
        self.assertEqual(len(checked.slides[0].points), 5)

    def test_the_cleaner_alone_checks_the_speaker_against_the_participants_it_is_given(self):
        points = CONTRACT["result"]["slides"][0]["points"]
        self.assertEqual(len(clean_points(points, {"S1", "S2"})), 4)
        with self.assertRaises(JobFailed):
            clean_points(points, {"S1"})


class TextTests(SimpleTestCase):
    def fails(self, mutate, fragment):
        payload = good()
        mutate(payload)
        with self.assertRaises(JobFailed) as caught:
            check_speaker_result(video(), payload)
        self.assertIn(fragment, str(caught.exception))

    def test_a_bullet_must_start_with_the_speakers_marker(self):
        for bad in ("沒有記號的重點", "S1：沒有括號", "[[S1]] 沒有冒號", "[[S1]]：", "[[S01]]：x", "[[X1]]：x",
                    "前面有字 [[S1]]：x"):
            with self.subTest(bad=bad):
                self.fails(lambda p, b=bad: slide_of(p)["bullets"].__setitem__(0, b), "bullets[0]")

    def test_a_bullet_must_name_the_speaker_of_the_point_it_summarises(self):
        """條列寫 S2、重點是 S1 說的：不知道哪一邊才對，兩邊都不信。"""
        self.fails(lambda p: slide_of(p)["bullets"].__setitem__(0, "[[S2]]：詢問目前補助政策的成效"), "bullets[0]")
        self.fails(lambda p: slide_of(p)["bullets"].__setitem__(1, "[[S1]]：現金補助有幫助"), "bullets[1]")

    def test_a_swapped_pair_of_bullets_is_caught(self):
        def swap(payload):
            bullets = slide_of(payload)["bullets"]
            bullets[0], bullets[1] = bullets[1], bullets[0]

        self.fails(swap, "對不上")

    def test_there_cannot_be_more_bullets_than_points(self):
        self.fails(lambda p: slide_of(p, 1)["bullets"].append("[[S1]]：多出來的"), "比重點")

    def test_a_bullet_has_one_marker_only(self):
        self.fails(lambda p: slide_of(p)["bullets"].__setitem__(0, "[[S1]]：他說 [[S2]]：好"), "bullets[0]")
        self.fails(lambda p: slide_of(p)["bullets"].__setitem__(0, "[[S1]]：後面 ]] 多一個"), "bullets[0]")

    def test_bullets_are_a_short_list_of_texts(self):
        for junk in (None, "x", {}, 5):
            with self.subTest(junk=junk):
                self.fails(lambda p, j=junk: slide_of(p).update(bullets=j), "bullets")
        self.fails(lambda p: slide_of(p)["bullets"].__setitem__(0, 5), "bullets[0]")
        self.fails(lambda p: slide_of(p)["bullets"].__setitem__(0, "[[S1]]：" + "字" * 300), "bullets[0]")

        def too_many(payload):
            slide = slide_of(payload)
            slide["points"] = [dict(slide["points"][0], turn=n) for n in range(9)]
            slide["bullets"] = ["[[S1]]：" + slide["points"][0]["text"]] * 9
            slide["detail"] = "[[S1]]：好。"

        self.fails(too_many, "bullets")

    def test_the_detail_markers_follow_the_points_in_order(self):
        self.fails(lambda p: slide_of(p).update(
            detail="[[S2]]：甲。 [[S1]]：乙。 [[S2]]：丙。 [[S1]]：丁。"), "對不上")
        # 少了一段
        self.fails(lambda p: slide_of(p).update(detail="[[S1]]：甲。 [[S2]]：乙。 [[S1]]：丙。"), "對不上")
        # 連續同一個人要併成一段：不能把同一個人切成兩段
        self.fails(lambda p: slide_of(p).update(
            detail="[[S1]]：甲。 [[S1]]：乙。 [[S2]]：丙。 [[S1]]：丁。 [[S2]]：戊。"), "對不上")

    def test_the_detail_cannot_name_a_speaker_the_slide_has_no_point_from(self):
        self.fails(lambda p: slide_of(p).update(detail="[[S1]]：甲。 [[S3]]：乙。 [[S1]]：丙。 [[S2]]：丁。"), "對不上")

    def test_the_detail_has_no_stray_brackets_and_is_text(self):
        self.fails(lambda p: slide_of(p).update(detail=slide_of(p)["detail"] + " [[S"), "格式不對")
        self.fails(lambda p: slide_of(p).update(detail=slide_of(p)["detail"] + " S1]]"), "格式不對")
        for junk in (None, 5, ["x"], "", "   ", "字" * 8001):
            with self.subTest(junk=junk):
                self.fails(lambda p, j=junk: slide_of(p).update(detail=j), "detail")

    def test_a_slide_title_cannot_carry_a_marker(self):
        self.fails(lambda p: slide_of(p).update(title="[[S1]] 的質詢"), "title")
        self.fails(lambda p: slide_of(p).update(title="後面 ]] 多一個"), "title")

    def test_the_slide_time_is_a_real_number_when_given(self):
        for bad in (float("nan"), float("inf"), -1, 10**9, "5", True):
            with self.subTest(bad=bad):
                self.fails(lambda p, b=bad: slide_of(p).update(timestamp=b), "timestamp")
        payload = good()
        del slide_of(payload)["timestamp"]
        self.assertEqual(len(check_speaker_result(video(), payload).slides), 2)

    def test_the_transcript_is_lines_of_time_marker_and_text(self):
        for bad in (None, 5, ["x"], "", "\n\n  \n"):
            with self.subTest(bad=bad):
                self.fails(lambda p, b=bad: p.update(transcript_text=b), "transcript_text")
        for line in ("沒有時間 [[S1]]：嗨", "00:05 沒有記號：嗨", "00:05 [[S1]] 沒有冒號", "5 [[S1]]：嗨", "00:5 [[S1]]：嗨",
                     "00:75 [[S1]]：秒數太大", "00:05[[S1]]：沒空格"):
            with self.subTest(line=line):
                self.fails(lambda p, l=line: p.update(transcript_text=CONTRACT["result"]["transcript_text"] + "\n" + l),
                           "逐字稿第 8 行")

    def test_the_transcript_may_run_past_ninety_nine_minutes(self):
        payload = good()
        payload["transcript_text"] += "\n125:30 [[S2]]：兩小時之後"
        self.assertTrue(check_speaker_result(video(), payload).transcript_text.endswith("兩小時之後"))

    def test_the_transcript_can_only_name_listed_speakers_or_the_undecidable_one(self):
        payload = good()
        payload["transcript_text"] += "\n02:00 [[S0]]：聽不清楚的一段"
        self.assertIn("[[S0]]", check_speaker_result(video(), payload).transcript_text)
        self.fails(lambda p: p.update(transcript_text=p["transcript_text"] + "\n02:00 [[S9]]：沒列出來的人"), "S9")
        # 其他聲音（minor）列在清單裡，逐字稿可以引用
        payload = good()
        self.assertIn("[[S3]]", check_speaker_result(video(), payload).transcript_text)

    def test_the_transcript_text_cannot_hide_another_marker(self):
        self.fails(lambda p: p.update(transcript_text=p["transcript_text"] + "\n02:00 [[S1]]：他說 [[S2]]：好"), "多出了")
        self.fails(lambda p: p.update(transcript_text=p["transcript_text"] + "\n02:00 [[S1]]：半個 [[S"), "多出了")

    def test_the_transcript_is_cleaned_line_by_line(self):
        payload = good()
        payload["transcript_text"] = "00:05 [[S1]]：嗨\x1b[2J\u202e\r\n\n00:09 [[S2]]：好\x00"
        self.assertEqual(check_speaker_result(video(), payload).transcript_text,
                         "00:05 [[S1]]：嗨[2J\n00:09 [[S2]]：好")


class MessagesTests(SimpleTestCase):
    def test_hostile_values_never_reach_the_message_raw(self):
        """訊息會印在操作者的終端機、存進資料庫：GPU 給的字串不能把控制序列或雙向覆寫帶進去。"""
        hostile = "\x1b[2J\x1b]0;pwned\x07\u202eevil"
        for mutate in (lambda p: p["speakers"][0].update(kind=hostile),
                       lambda p: p["speakers"][0].update(id=hostile),
                       lambda p: p["diarization"].update(status=hostile),
                       lambda p: slide_of(p)["points"][0].update(speaker=hostile),
                       lambda p: slide_of(p)["points"][0].update(turn=hostile),
                       lambda p: p.update(speaker_mode=hostile),
                       lambda p: p.update(slides=hostile)):
            payload = good()
            mutate(payload)
            with self.assertRaises(JobFailed) as caught:
                check_speaker_result(video(), payload)
            message = str(caught.exception)
            self.assertNotIn("\x1b", message)
            self.assertNotIn("\x07", message)
            self.assertNotIn("\u202e", message)
            self.assertLess(len(message), 600)

    def test_an_enormous_integer_is_a_failure_not_a_crash(self):
        payload = good()
        slide_of(payload)["points"][0]["turn"] = 10 ** 5000
        with self.assertRaises(JobFailed):
            check_speaker_result(video(), payload)


class SingleResultTests(SimpleTestCase):
    def test_a_plain_single_speaker_result_passes(self):
        check_single_result({"video_id": "x", "title": "t", "transcript_text": "00:00 大家好", "brief": None,
                             "slides": [{"index": 1, "title": "a", "bullets": ["重點"], "detail": "敘述",
                                         "timestamp": 1.0}]})

    def test_the_base_branch_empty_speakers_list_is_ignored(self):
        check_single_result({"transcript_text": "00:00 大家好", "speakers": [], "slides": []})

    def test_a_multi_speaker_result_for_a_single_speaker_video_is_refused(self):
        with self.assertRaises(JobFailed) as caught:
            check_single_result(good())
        self.assertIn("單人模式", str(caught.exception))

    def test_markers_without_the_mode_echo_are_refused_too(self):
        for where in ("transcript_text", "detail", "bullets"):
            with self.subTest(where=where):
                payload = {"transcript_text": "00:00 大家好", "slides": [
                    {"index": 1, "title": "a", "bullets": ["重點"], "detail": "敘述", "timestamp": 1.0}]}
                if where == "transcript_text":
                    payload[where] = "00:00 [[S1]]：大家好"
                elif where == "detail":
                    payload["slides"][0][where] = "[[S1]]：敘述。"
                else:
                    payload["slides"][0][where] = ["[[S1]]：重點"]
                with self.assertRaises(JobFailed):
                    check_single_result(payload)

    def test_an_explicit_single_echo_is_fine(self):
        check_single_result({"speaker_mode": "single", "transcript_text": "x", "slides": []})
