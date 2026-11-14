"""[[Sn]] 記號換成名字：顯示規則、併掉的編號、名字裡的特殊字元、不能漏到網頁上的殘渣。"""
from __future__ import annotations

from django.test import SimpleTestCase

from articles.speakers import (
    PLACEHOLDER,
    SPEAKER_ID,
    UNDECIDABLE_TEXT,
    UNKNOWN_TEXT,
    display_of,
    diarization_view,
    has_structured_speakers,
    point_views,
    render_placeholders,
    resolve,
    speaker_views,
)
from articles.tests.speaker_support import run_with_deadline


def _speaker(sid, label, **extra):
    return {"id": sid, "label": label, "kind": "participant", "seconds": 60.0, "turns": 4,
            "first_seen": 1.0, "last_seen": 59.0, "samples": [], "flags": [],
            "name": "", "role": "", "basis": "none", "merged_into": "", **extra}


def _roster(**names):
    """S1「發言者 A」、S2「發言者 B」、S3 minor「其他聲音 1」；names 是要設的名字（S1="黃國昌"）。"""
    roster = [_speaker("S1", "發言者 A"), _speaker("S2", "發言者 B"),
              _speaker("S3", "其他聲音 1", kind="minor")]
    for sid, name in names.items():
        next(s for s in roster if s["id"] == sid)["name"] = name
    return roster


class DisplayTests(SimpleTestCase):
    def test_the_neutral_label_is_shown_until_somebody_is_named(self):
        self.assertEqual(display_of(_roster(), "S1"), "發言者 A")
        self.assertEqual(display_of(_roster(S1="黃國昌"), "S1"), "黃國昌")
        self.assertEqual(display_of(_roster(S1="黃國昌"), "S2"), "發言者 B")

    def test_a_blank_name_does_not_hide_the_label(self):
        self.assertEqual(display_of(_roster(S1="   "), "S1"), "發言者 A")

    def test_a_merged_number_follows_the_one_it_was_merged_into(self):
        roster = _roster(S1="黃國昌")
        roster[2]["merged_into"] = "S1"
        self.assertEqual(display_of(roster, "S3"), "黃國昌")
        self.assertEqual(display_of(roster, "S1"), "黃國昌")

    def test_a_merge_into_a_speaker_with_no_name_shows_that_speakers_label(self):
        roster = _roster()
        roster[1]["merged_into"] = "S1"
        self.assertEqual(display_of(roster, "S2"), "發言者 A")

    def test_a_chain_in_old_data_is_followed_to_the_end(self):
        roster = _roster(S1="黃國昌")
        roster[2]["merged_into"] = "S2"
        roster[1]["merged_into"] = "S1"
        self.assertEqual(display_of(roster, "S3"), "黃國昌")

    def test_a_merge_cycle_neither_hangs_nor_picks_a_side(self):
        """A 併進 B、B 併進 A：壞資料。不能無限迴圈；各自顯示自己，不亂猜哪一位才對。"""
        roster = _roster(S1="甲", S2="乙")
        roster[0]["merged_into"] = "S2"
        roster[1]["merged_into"] = "S1"
        self.assertEqual(run_with_deadline(lambda: (display_of(roster, "S1"), display_of(roster, "S2"))), ("甲", "乙"))
        roster[0]["merged_into"] = "S1"
        self.assertEqual(run_with_deadline(display_of, roster, "S1"), "甲")
        # 讀取 API 時走的是另一條路（整份名單一次算）：同樣不能卡住
        roster[0]["merged_into"] = "S2"
        self.assertEqual(run_with_deadline(lambda: [v["display"] for v in speaker_views(roster)]),
                         ["甲", "乙", "其他聲音 1"])

    def test_a_merge_into_a_number_that_does_not_exist_shows_the_speaker_itself(self):
        roster = _roster(S1="甲")
        roster[0]["merged_into"] = "S9"
        self.assertEqual(display_of(roster, "S1"), "甲")

    def test_an_unknown_number_and_the_undecidable_one_have_their_own_words(self):
        self.assertEqual(display_of(_roster(), "S9"), UNKNOWN_TEXT)
        self.assertEqual(UNKNOWN_TEXT, "（未知發言者）")
        self.assertEqual(display_of(_roster(), "S0"), UNDECIDABLE_TEXT)
        self.assertEqual(UNDECIDABLE_TEXT, "（無法判斷）")
        self.assertEqual(display_of([], "S1"), UNKNOWN_TEXT)

    def test_s0_is_undecidable_even_if_bad_data_lists_it(self):
        self.assertEqual(display_of([_speaker("S0", "發言者 Z", name="不該出現")], "S0"), UNDECIDABLE_TEXT)

    def test_garbage_in_the_list_is_ignored_not_a_crash(self):
        roster = ["說話者 1", None, {"no": "id"}, {"id": 5}, _speaker("S1", "發言者 A")]
        self.assertEqual(display_of(roster, "S1"), "發言者 A")
        self.assertEqual(display_of("not a list", "S1"), UNKNOWN_TEXT)
        self.assertEqual(display_of(None, "S1"), UNKNOWN_TEXT)

    def test_a_row_with_neither_name_nor_label_still_shows_something(self):
        self.assertEqual(display_of([{"id": "S1"}], "S1"), "發言者 S1")


class RenderTests(SimpleTestCase):
    def test_every_placeholder_is_replaced(self):
        text = "[[S1]]：詢問補助。 [[S2]]：現金有幫助。 [[S1]]：追問托育。"
        self.assertEqual(render_placeholders(text, _roster(S1="黃國昌")),
                         "黃國昌：詢問補助。 發言者 B：現金有幫助。 黃國昌：追問托育。")

    def test_a_name_is_never_a_regex_template(self):
        """攔的 bug：用 re.sub(pattern, name, text)。名字裡的 \\1、\\g<0>、$1 會被當成反向參照。"""
        for name in ("A.*B", "$1", r"\1", r"\g<0>", r"\g<name>", "(?P<x>a)", "[x]", "a|b", "^$", "\\", "{1}",
                     "%s", "{0}", "\\n", "a\\"):
            with self.subTest(name=name):
                self.assertEqual(render_placeholders("[[S1]]：好。[[S1]]", _roster(S1=name)),
                                 f"{name}：好。{name}")

    def test_a_name_that_looks_like_a_placeholder_is_not_replaced_again(self):
        """換出來的名字不再被掃一次：單次替換，不會連鎖。"""
        roster = _roster(S1="甲")
        roster[1]["name"] = "[[S1]]"
        self.assertEqual(render_placeholders("[[S2]]：好", roster), "S1：好")

    def test_unknown_and_undecidable_numbers_are_worded(self):
        self.assertEqual(render_placeholders("[[S0]]：嗯 [[S9]]：喔", _roster()),
                         "（無法判斷）：嗯 （未知發言者）：喔")
        self.assertEqual(render_placeholders("[[S99999999999999999999]]", _roster()), "（未知發言者）")

    def test_leftovers_of_a_broken_placeholder_never_reach_the_page(self):
        for text, expected in (("[[S1", "S1"), ("S1]]：好", "S1：好"), ("[[Sx]]：好", "Sx：好"), ("[[]]", ""),
                               ("[[ S1 ]]：好", " S1 ：好"), ("[[[S1]]]：好", "[發言者 A]：好")):
            with self.subTest(text=text):
                out = render_placeholders(text, _roster())
                self.assertNotIn("[[", out)
                self.assertNotIn("]]", out)
                self.assertEqual(out, expected)

    def test_text_without_placeholders_comes_back_the_same(self):
        self.assertEqual(render_placeholders("沒有記號的一句話。", _roster()), "沒有記號的一句話。")
        self.assertEqual(render_placeholders("", _roster()), "")
        self.assertEqual(render_placeholders(None, _roster()), "")

    def test_the_pattern_matches_exactly_the_contract_tokens(self):
        self.assertEqual(PLACEHOLDER.findall("[[S1]]x[[S0]]y[[S12]] [[s1]] [[S]] [[S1 ]]"), ["S1", "S0", "S12"])
        for ok in ("S1", "S9", "S10", "S999"):
            self.assertTrue(SPEAKER_ID.fullmatch(ok), ok)
        for bad in ("S0", "S01", "S1000", "s1", "S", "X1", "S1a", " S1", "S1\n"):
            self.assertFalse(SPEAKER_ID.fullmatch(bad), bad)


class StructuredTests(SimpleTestCase):
    def test_only_a_list_of_speakers_with_ids_counts(self):
        self.assertTrue(has_structured_speakers(_roster()))
        for not_structured in ([], None, "x", ["說話者 1"], [{"name": "甲"}], [_speaker("S1", "A"), "說話者 2"], {}):
            with self.subTest(value=not_structured):
                self.assertFalse(has_structured_speakers(not_structured))

    def test_resolve_returns_the_final_speaker(self):
        roster = _roster()
        roster[2]["merged_into"] = "S1"
        self.assertEqual(resolve(roster, "S3")["id"], "S1")
        self.assertIsNone(resolve(roster, "S9"))


class ViewTests(SimpleTestCase):
    def test_a_merged_speaker_shows_the_identity_it_was_merged_into(self):
        roster = _roster(S1="黃國昌")
        roster[0].update(role="主持人", basis="operator")
        roster[2]["merged_into"] = "S1"
        views = {v["id"]: v for v in speaker_views(roster)}
        self.assertEqual((views["S3"]["label"], views["S3"]["kind"], views["S3"]["display"]),
                         ("其他聲音 1", "minor", "黃國昌"))
        self.assertEqual((views["S3"]["name"], views["S3"]["role"], views["S3"]["basis"]),
                         ("黃國昌", "主持人", "operator"))
        self.assertEqual(views["S2"]["basis"], "none")

    def test_the_view_carries_exactly_the_public_fields(self):
        view = speaker_views(_roster())[0]
        self.assertEqual(sorted(view), sorted(
            ["id", "label", "name", "role", "display", "kind", "seconds", "turns", "basis", "flags"]))

    def test_malformed_rows_are_tamed_not_trusted(self):
        weird = [{"id": "S1", "label": 5, "kind": "boss", "seconds": "x", "turns": None, "basis": "hacked",
                  "flags": "abc", "name": ["x"]}]
        self.assertEqual(speaker_views(weird), [{
            "id": "S1", "label": "", "name": "", "role": "", "display": "發言者 S1", "kind": "participant",
            "seconds": 0.0, "turns": 0, "basis": "none", "flags": []}])
        self.assertEqual(speaker_views(["說話者 1"]), [])

    def test_points_carry_the_display_but_not_the_evidence(self):
        points = [{"speaker": "S1", "turn": 0, "start": 5.0, "text": "詢問補助", "evidence": "原話"},
                  {"speaker": "S9", "turn": 1, "start": 6.0, "text": "x", "evidence": "y"}, "junk", {"text": "x"}]
        self.assertEqual(point_views(points, _roster(S1="黃國昌")), [
            {"speaker": "S1", "display": "黃國昌", "start": 5.0, "text": "詢問補助"},
            {"speaker": "S9", "display": "（未知發言者）", "start": 6.0, "text": "x"}])
        self.assertEqual(point_views("junk", _roster()), [])

    def test_the_diarization_view_is_the_public_summary(self):
        stored = {"status": "warn", "warnings": ["count_mismatch", 5], "requested_speakers": 3,
                  "found_speakers": 2, "engine": "pyannote.audio", "fingerprint": "0123456789abcdef"}
        self.assertEqual(diarization_view(stored), {
            "status": "warn", "warnings": ["count_mismatch"], "requested_speakers": 3, "found_speakers": 2})
        for nothing in (None, {}, [], "x", {"status": "weird"}):
            self.assertIsNone(diarization_view(nothing))


class BracketedNameTests(SimpleTestCase):
    """資料庫被直接改過（admin、手動 SQL）而名字長得像記號：顯示的字、API 的欄位都不能帶 [[ ]]。"""

    def test_a_name_never_shows_double_brackets(self):
        roster = _roster(S1="[[S2]]")
        self.assertEqual(display_of(roster, "S1"), "S2")
        roster[0]["name"] = "a[[b]]c"
        self.assertEqual(display_of(roster, "S1"), "abc")

    def test_a_name_of_only_brackets_falls_back_to_the_label(self):
        self.assertEqual(display_of(_roster(S1="[[]]"), "S1"), "發言者 A")

    def test_the_api_view_has_no_brackets_in_any_text_field(self):
        roster = _roster(S1="[[S2]]")
        roster[0].update(role="]]主持[[", label="[[發言者 A]]")
        view = speaker_views(roster)[0]
        for key in ("label", "name", "role", "display"):
            self.assertNotIn("[[", view[key])
            self.assertNotIn("]]", view[key])
        self.assertEqual((view["name"], view["role"], view["label"]), ("S2", "主持", "發言者 A"))
        points = point_views([{"speaker": "S1", "start": 1.0, "text": "x"}], roster)
        self.assertEqual(points[0]["display"], "S2")
