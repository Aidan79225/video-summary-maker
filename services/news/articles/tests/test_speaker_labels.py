"""label_video 的規則（純函式）：名字與角色的驗證、併人只併一層、顯示不能撞名、全部驗證完才算出新清單。"""
from __future__ import annotations

import copy

from django.test import SimpleTestCase

from articles.speaker_labels import (
    NAME_LENGTH,
    ROLE_LENGTH,
    LabelError,
    Labels,
    apply_labels,
    changed,
    check_name,
    split_pair,
)
from articles.speakers import display_of


def speaker(sid, label, kind="participant", **extra):
    return {"id": sid, "label": label, "kind": kind, "seconds": 60.0, "turns": 4, "first_seen": 1.0,
            "last_seen": 59.0, "samples": [], "flags": [], "name": "", "role": "", "basis": "none",
            "merged_into": "", **extra}


def roster():
    """S1「發言者 A」、S2「發言者 B」兩位主要發言者，S3「其他聲音 1」一個只講了幾句的聲音。"""
    return [speaker("S1", "發言者 A"), speaker("S2", "發言者 B"), speaker("S3", "其他聲音 1", "minor")]


def three():
    """三位主要發言者：併人的連鎖要三個編號才湊得出來（其他聲音不能被主要發言者併進去）。"""
    return [speaker("S1", "發言者 A"), speaker("S2", "發言者 B"), speaker("S3", "發言者 C")]


def labels(names=(), roles=(), merges=(), clears=()):
    return Labels(names=list(names), roles=list(roles), merges=list(merges), clears=list(clears))


def row(result, sid):
    return next(r for r in result if r["id"] == sid)


class SplitTests(SimpleTestCase):
    def test_only_the_first_equals_sign_splits(self):
        self.assertEqual(split_pair("S1=黃=國昌", "--name"), ("S1", "黃=國昌"))

    def test_the_full_width_sign_works_and_the_earlier_one_wins(self):
        self.assertEqual(split_pair("S1＝黃國昌", "--name"), ("S1", "黃國昌"))
        self.assertEqual(split_pair("S1＝a=b", "--name"), ("S1", "a=b"))
        self.assertEqual(split_pair("S1=a＝b", "--name"), ("S1", "a＝b"))

    def test_spaces_around_the_number_are_ignored(self):
        self.assertEqual(split_pair(" S1 = 黃國昌", "--name"), ("S1", " 黃國昌"))

    def test_no_sign_is_an_error_that_says_the_format(self):
        for raw in ("S1", "黃國昌", ""):
            with self.subTest(raw=raw), self.assertRaisesRegex(LabelError, "S1=內容"):
                split_pair(raw, "--name")


class NameTests(SimpleTestCase):
    def rejected(self, value, fragment, limit=NAME_LENGTH):
        with self.assertRaises(LabelError) as caught:
            check_name(value, "--name S1", limit)
        self.assertIn(fragment, str(caught.exception))

    def test_a_plain_name_passes_and_is_cleaned(self):
        self.assertEqual(check_name("黃國昌", "--name S1", NAME_LENGTH), "黃國昌")
        self.assertEqual(check_name("  黃  國昌 ", "--name S1", NAME_LENGTH), "黃 國昌")
        self.assertEqual(check_name("黃\x1b國昌\u202e", "--name S1", NAME_LENGTH), "黃國昌")

    def test_the_length_limit_counts_after_cleaning(self):
        check_name("字" * NAME_LENGTH, "--name S1", NAME_LENGTH)
        self.rejected("字" * (NAME_LENGTH + 1), f"最多 {NAME_LENGTH} 個字")
        check_name("字" * ROLE_LENGTH, "--role S1", ROLE_LENGTH)
        self.rejected("字" * (ROLE_LENGTH + 1), f"最多 {ROLE_LENGTH} 個字", limit=ROLE_LENGTH)
        check_name("字" * NAME_LENGTH + "\u200b\u200b\u200b", "--name S1", NAME_LENGTH)

    def test_a_marker_cannot_be_spelled_out_or_smuggled(self):
        for value in ("[[S1]]", "a[[b", "b]]a", "[\u200b[S1]\u200b]", "[\u202e[S2]]", "[[", "]]"):
            with self.subTest(value=value):
                self.rejected(value, "[[" if "[" in value else "]]")

    def test_a_colon_would_make_who_said_what_ambiguous(self):
        for value in ("黃國昌:委員", "黃國昌：委員", ":", "："):
            with self.subTest(value=value):
                self.rejected(value, "冒號")

    def test_line_breaks_are_refused_not_flattened(self):
        for value in ("甲\n乙", "甲\r乙", "甲 乙", "甲 乙", "甲\x0b乙", "甲\x0c乙", "甲\x85乙"):
            with self.subTest(value=repr(value)):
                self.rejected(value, "換行")

    def test_nothing_left_after_cleaning_is_empty(self):
        for value in ("", "   ", "\x1b\x07", "\u200b", "\t"):
            with self.subTest(value=repr(value)):
                self.rejected(value, "空的")

    def test_a_name_cannot_pass_for_a_speaker_number_or_a_reserved_word(self):
        for value in ("S1", "S12", "（無法判斷）", "（未知發言者）"):
            with self.subTest(value=value):
                self.rejected(value, "不能是")

    def test_names_that_merely_look_a_bit_like_those_are_fine(self):
        for value in ("S", "S0", "S1a", "Sam", "（主持人）"):
            with self.subTest(value=value):
                check_name(value, "--name S1", NAME_LENGTH)


class NamingTests(SimpleTestCase):
    def test_a_name_and_a_role_are_set_with_the_operator_as_the_basis(self):
        result = apply_labels(roster(), labels(names=[("S1", "黃國昌")], roles=[("S1", "主持人")]))
        self.assertEqual({k: row(result, "S1")[k] for k in ("name", "role", "basis")},
                         {"name": "黃國昌", "role": "主持人", "basis": "operator"})
        self.assertEqual(row(result, "S2")["basis"], "none")

    def test_a_role_alone_is_enough_and_still_counts_as_the_operators(self):
        result = apply_labels(roster(), labels(roles=[("S2", "來賓")]))
        self.assertEqual((row(result, "S2")["name"], row(result, "S2")["role"], row(result, "S2")["basis"]),
                         ("", "來賓", "operator"))
        self.assertEqual(display_of(result, "S2"), "發言者 B")

    def test_the_input_is_not_modified(self):
        original = roster()
        snapshot = copy.deepcopy(original)
        apply_labels(original, labels(names=[("S1", "甲")], merges=[("S3", "S1")]))
        self.assertEqual(original, snapshot)

    def test_a_minor_voice_can_have_a_name(self):
        result = apply_labels(roster(), labels(names=[("S3", "社區民眾")]))
        self.assertEqual(display_of(result, "S3"), "社區民眾")

    def test_missing_pi_fields_in_old_rows_get_their_defaults(self):
        bare = [{"id": "S1", "label": "發言者 A", "kind": "participant"}]
        result = apply_labels(bare, labels(names=[("S1", "甲")]))
        self.assertEqual((result[0]["role"], result[0]["merged_into"]), ("", ""))

    def test_an_unknown_or_malformed_number_is_refused_and_the_known_ones_are_listed(self):
        with self.assertRaisesRegex(LabelError, "沒有 S9（有：S1、S2、S3）"):
            apply_labels(roster(), labels(names=[("S9", "甲")]))
        for bad in ("S0", "s1", "S01", "1", "", "S1 ", "S1000"):
            with self.subTest(bad=bad), self.assertRaises(LabelError):
                apply_labels(roster(), labels(names=[(bad, "甲")]))
        with self.assertRaisesRegex(LabelError, "S0 是保留"):
            apply_labels(roster(), labels(names=[("S0", "甲")]))

    def test_the_same_number_twice_in_one_flag_is_refused(self):
        for given in (labels(names=[("S1", "甲"), ("S1", "乙")]), labels(roles=[("S1", "甲"), ("S1", "乙")]),
                      labels(merges=[("S3", "S1"), ("S3", "S2")])):
            with self.subTest(given=given), self.assertRaisesRegex(LabelError, "給了不只一次"):
                apply_labels(roster(), given)

    def test_every_problem_is_reported_at_once(self):
        with self.assertRaises(LabelError) as caught:
            apply_labels(roster(), labels(names=[("S1", "[[x]]"), ("S9", "乙")], roles=[("S2", "字" * 30)],
                                          clears=["S8"]))
        self.assertEqual(len(str(caught.exception).splitlines()), 4)

    def test_nothing_comes_back_when_anything_is_wrong(self):
        """攔的 bug：先套用有效的幾項，遇到無效的才停（只套了一半）。"""
        with self.assertRaises(LabelError):
            apply_labels(roster(), labels(names=[("S1", "甲"), ("S2", "[[乙")]))

    def test_the_data_must_be_a_speaker_list(self):
        for junk in ([], None, ["說話者 1"], "x"):
            with self.subTest(junk=junk), self.assertRaises(LabelError):
                apply_labels(junk, labels(names=[("S1", "甲")]))


class DisplayCollisionTests(SimpleTestCase):
    def test_two_speakers_cannot_get_the_same_name(self):
        with self.assertRaisesRegex(LabelError, "都會顯示成「黃國昌」"):
            apply_labels(roster(), labels(names=[("S1", "黃國昌"), ("S2", "黃國昌")]))

    def test_the_comparison_ignores_case(self):
        with self.assertRaises(LabelError):
            apply_labels(roster(), labels(names=[("S1", "Alice"), ("S2", "alice")]))

    def test_a_name_cannot_be_another_speakers_neutral_label(self):
        """把 S1 取名「發言者 B」，網頁上就分不出它和還沒取名的 S2。"""
        with self.assertRaises(LabelError):
            apply_labels(roster(), labels(names=[("S1", "發言者 B")]))

    def test_a_name_that_collides_with_an_existing_name_is_refused(self):
        named = roster()
        named[0]["name"] = "黃國昌"
        with self.assertRaises(LabelError):
            apply_labels(named, labels(names=[("S2", "黃國昌")]))

    def test_renaming_a_speaker_to_its_own_current_name_is_fine(self):
        named = roster()
        named[0]["name"] = "黃國昌"
        apply_labels(named, labels(names=[("S1", "黃國昌")]))

    def test_merged_speakers_share_one_display_without_colliding(self):
        result = apply_labels(roster(), labels(names=[("S1", "黃國昌")], merges=[("S3", "S1")]))
        self.assertEqual({display_of(result, s) for s in ("S1", "S3")}, {"黃國昌"})

    def test_a_cleared_speaker_goes_back_to_its_label_and_may_not_collide_with_a_name(self):
        named = roster()
        named[0]["name"] = "發言者 B"            # 壞資料：撞了 S2 的中性標籤
        with self.assertRaises(LabelError):
            apply_labels(named, labels(roles=[("S3", "x")]))


class MergeTests(SimpleTestCase):
    def test_a_merge_points_one_number_at_another_and_forgets_its_own_identity(self):
        named = roster()
        named[2].update(name="路人", role="x", basis="operator")
        result = apply_labels(named, labels(names=[("S1", "黃國昌")], merges=[("S3", "S1")]))
        merged = row(result, "S3")
        self.assertEqual((merged["merged_into"], merged["name"], merged["role"], merged["basis"]),
                         ("S1", "", "", "none"))
        self.assertEqual(display_of(result, "S3"), "黃國昌")

    def test_a_participant_can_be_merged_into_another_participant(self):
        result = apply_labels(roster(), labels(merges=[("S2", "S1")]))
        self.assertEqual(display_of(result, "S2"), "發言者 A")

    def test_a_merge_needs_two_different_known_numbers(self):
        for pair, fragment in ((("S1", "S1"), "不能併給自己"), (("S9", "S1"), "沒有 S9"), (("S1", "S9"), "沒有 S9")):
            with self.subTest(pair=pair), self.assertRaisesRegex(LabelError, fragment):
                apply_labels(roster(), labels(merges=[pair]))

    def test_a_participant_cannot_be_merged_into_a_minor_voice(self):
        with self.assertRaisesRegex(LabelError, "方向反過來：--merge S3=S1"):
            apply_labels(roster(), labels(merges=[("S1", "S3")]))

    def test_there_are_no_chains_whatever_the_order(self):
        """S3→S1 之後 S2→S3 是連鎖；先 S2→S3 再 S3→S1 也是。兩個順序都要擋。"""
        for given in (labels(merges=[("S3", "S1"), ("S2", "S3")]), labels(merges=[("S2", "S3"), ("S3", "S1")])):
            with self.subTest(given=given.merges), self.assertRaisesRegex(LabelError, "只能併一層"):
                apply_labels(three(), given)

    def test_a_chain_with_an_earlier_merge_is_refused_too(self):
        merged = three()
        merged[2]["merged_into"] = "S1"
        with self.assertRaisesRegex(LabelError, "只能併一層"):
            apply_labels(merged, labels(merges=[("S2", "S3")]))

    def test_a_cycle_is_refused(self):
        with self.assertRaises(LabelError):
            apply_labels(roster(), labels(merges=[("S1", "S2"), ("S2", "S1")]))

    def test_a_merge_can_be_pointed_somewhere_else(self):
        merged = roster()
        merged[2]["merged_into"] = "S1"
        result = apply_labels(merged, labels(merges=[("S3", "S2")]))
        self.assertEqual(row(result, "S3")["merged_into"], "S2")

    def test_a_merged_number_cannot_be_named_in_the_same_command(self):
        for given in (labels(names=[("S3", "路人")], merges=[("S3", "S1")]),
                      labels(roles=[("S3", "路人")], merges=[("S3", "S1")])):
            with self.subTest(given=given), self.assertRaisesRegex(LabelError, "同時被併進 S1"):
                apply_labels(roster(), given)

    def test_a_number_that_is_already_merged_cannot_be_named_either(self):
        merged = roster()
        merged[2]["merged_into"] = "S1"
        for given in (labels(names=[("S3", "路人")]), labels(roles=[("S3", "路人")])):
            with self.subTest(given=given), self.assertRaisesRegex(LabelError, "已經併進 S1"):
                apply_labels(merged, given)

    def test_clearing_it_first_lets_it_be_named(self):
        merged = roster()
        merged[2]["merged_into"] = "S1"
        result = apply_labels(merged, labels(names=[("S3", "路人")], clears=["S3"]))
        self.assertEqual((row(result, "S3")["merged_into"], display_of(result, "S3")), ("", "路人"))


class ClearTests(SimpleTestCase):
    def test_clear_resets_name_role_basis_and_merge(self):
        full = roster()
        full[0].update(name="黃國昌", role="主持人", basis="operator")
        full[2].update(merged_into="S1")
        result = apply_labels(full, labels(clears=["S1", "S3"]))
        for sid in ("S1", "S3"):
            self.assertEqual({k: row(result, sid)[k] for k in ("name", "role", "basis", "merged_into")},
                             {"name": "", "role": "", "basis": "none", "merged_into": ""})

    def test_clear_runs_before_the_new_settings(self):
        full = roster()
        full[0].update(name="舊名", role="舊角色", basis="operator")
        result = apply_labels(full, labels(names=[("S1", "新名")], clears=["S1"]))
        self.assertEqual((row(result, "S1")["name"], row(result, "S1")["role"], row(result, "S1")["basis"]),
                         ("新名", "", "operator"))

    def test_clearing_an_unknown_number_is_refused(self):
        with self.assertRaises(LabelError):
            apply_labels(roster(), labels(clears=["S9"]))

    def test_clearing_the_same_number_twice_is_harmless(self):
        apply_labels(roster(), labels(clears=["S1", "S1"]))


class ChangedTests(SimpleTestCase):
    def test_only_the_speakers_whose_identity_changed_are_listed_in_order(self):
        before = roster()
        after = apply_labels(before, labels(names=[("S2", "乙"), ("S1", "甲")]))
        self.assertEqual(changed(before, after), ["S1", "S2"])

    def test_nothing_changed_is_an_empty_list(self):
        before = roster()
        self.assertEqual(changed(before, apply_labels(before, labels(clears=["S1"]))), [])
