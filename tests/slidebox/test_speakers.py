"""說話者歸屬的純邏輯：區段整理、字→說話者、字→段、說話者清單與品質、逐字稿。

最重要的性質：**一個字只有在 diarizer 的證據支持時才會被歸給某人，否則是 S0**。
測試分三層：手寫的三人對談（有標準答案）、逐一的小情境、以及用固定亂數產生的性質測試。
"""
from __future__ import annotations

import math
import random

import pytest

from slidebox.domain.entities import SpeakerTurn, Turn, Word
from slidebox.domain.errors import AttributionUnreliable
from slidebox.usecases import speakers
from slidebox.usecases.speakers import (
    ALIGN_VERSION,
    assign_speakers,
    build_turns,
    clock,
    diarization_report,
    normalize_turns,
    summarize_speakers,
    transcript_lines,
)

from .speaker_fixtures import SID, TRUE_SPEAKER, Say, build, dialogue, tokenize


def labels_of(fixture, turns=None):
    turns = normalize_turns(fixture.turns) if turns is None else turns
    return [label for _, label in assign_speakers(fixture.words, turns)]


def pairs(fixture):
    return [(w.text, label) for w, label in assign_speakers(fixture.words, normalize_turns(fixture.turns))]


# --- normalize_turns ---


def test_turns_are_sorted_and_renamed_by_first_appearance():
    turns = [SpeakerTurn(5.0, 6.0, "SPEAKER_07"), SpeakerTurn(0.0, 2.0, "SPEAKER_03"),
             SpeakerTurn(3.0, 4.0, "SPEAKER_07"), SpeakerTurn(2.0, 2.5, "SPEAKER_01")]

    out = normalize_turns(turns)

    assert [(t.start, t.end, t.speaker) for t in out] == [
        (0.0, 2.0, "S1"), (2.0, 2.5, "S2"), (3.0, 4.0, "S3"), (5.0, 6.0, "S3")]


def test_invalid_turns_are_dropped_and_negative_starts_clamped():
    turns = [SpeakerTurn(1.0, 1.0, "a"), SpeakerTurn(2.0, 1.0, "a"),
             SpeakerTurn(math.nan, 3.0, "a"), SpeakerTurn(0.0, math.inf, "a"),
             SpeakerTurn(0.0, 1.0, ""), SpeakerTurn(-2.0, 1.5, "b"), SpeakerTurn(-3.0, -1.0, "b")]

    out = normalize_turns(turns)

    assert out == (SpeakerTurn(0.0, 1.5, "S1"),)


def test_overlapping_turns_are_kept_and_normalizing_twice_changes_nothing():
    turns = [SpeakerTurn(0.0, 3.0, "a"), SpeakerTurn(2.0, 4.0, "b")]

    once = normalize_turns(turns)

    assert len(once) == 2 and once[0].end > once[1].start
    assert normalize_turns(once) == once


def test_no_turns_is_an_empty_result():
    assert normalize_turns([]) == ()


def test_turns_with_exactly_the_same_times_are_numbered_the_same_whatever_the_input_order():
    # 起訖時間完全一樣的兩段（不同標籤）：S1／S2 的編號不能跟輸入順序有關
    a = SpeakerTurn(0.0, 2.0, "SPEAKER_01")
    b = SpeakerTurn(0.0, 2.0, "SPEAKER_00")
    later = SpeakerTurn(3.0, 4.0, "SPEAKER_01")

    assert normalize_turns([a, b, later]) == normalize_turns([b, a, later])


# --- assign_speakers：手寫的三人對談 ---


def test_the_dialogue_never_gives_a_word_to_the_wrong_speaker():
    fixture = dialogue()

    wrong = [(w.text, label, truth) for (w, label), truth in
             zip(assign_speakers(fixture.words, normalize_turns(fixture.turns)), fixture.truth)
             if label not in (truth, "S0")]

    assert wrong == []


def test_the_dialogue_keeps_exactly_the_undecidable_words_undecidable():
    undecided = [text for text, label in pairs(dialogue()) if label == "S0"]

    # 同時說話的那幾個字，以及 diarizer 沒蓋到、左右又是不同人的「請」
    assert undecided == ["題，", "所", "以", "其", "實—", "請"]


def test_the_output_is_one_to_one_with_the_input_words():
    fixture = dialogue()

    out = assign_speakers(fixture.words, normalize_turns(fixture.turns))

    assert [w for w, _ in out] == list(fixture.words)


def test_a_word_in_a_diarizer_gap_between_the_same_speaker_belongs_to_that_speaker():
    fixture = dialogue()
    turns = normalize_turns(fixture.turns)
    assigned = assign_speakers(fixture.words, turns)

    # Whisper 第 21 段裡只有 G 在說話，其中「的」完全沒被 diarizer 蓋到
    gap_word = [(w, label) for w, label in assigned if w.segment == 21 and w.text == "的"]

    assert len(gap_word) == 1
    assert gap_word[0][1] == SID["G"]
    mid = (gap_word[0][0].start + gap_word[0][0].end) / 2
    assert [t for t in turns if t.start <= mid < t.end] == []   # 確實沒被任何區段蓋到：是推出來的


def test_a_word_in_a_gap_between_two_different_speakers_is_undecided():
    fixture = dialogue()
    assigned = assign_speakers(fixture.words, normalize_turns(fixture.turns))

    gap_word = [(w, label) for w, label in assigned if w.segment == 23 and w.text == "請"]

    assert len(gap_word) == 1 and gap_word[0][1] == "S0"


def test_the_speaker_change_inside_one_whisper_segment_is_split():
    fixture = dialogue()
    assigned = assign_speakers(fixture.words, normalize_turns(fixture.turns))
    shared = [(w.text, label) for w, label in assigned if w.segment == 15]

    speakers_in_segment = {label for _, label in shared}

    assert speakers_in_segment == {SID["G"], SID["H"]}
    assert "".join(t for t, label in shared if label == SID["G"]) == "我們社區就是這樣。"
    assert "".join(t for t, label in shared if label == SID["H"]) == "好的，謝謝您。"


# --- assign_speakers：邊界規則 ---


def two_voices(h_text, g_text, gap, *, h_end=0.0, g_start=0.0):
    return build([
        Say("H", h_text, gap=0, segment=0, diar_end=h_end),
        Say("G", g_text, gap=gap, segment=1, diar_start=g_start),
    ])


def test_a_change_right_after_a_sentence_end_is_accepted_without_any_pause():
    fixture = two_voices("你覺得呢？", "我覺得很好，但是還不夠", gap=0.0)

    assert labels_of(fixture) == list(fixture.truth)


def test_a_change_after_a_long_enough_pause_is_accepted_without_punctuation():
    fixture = two_voices("你覺得呢", "我覺得很好", gap=0.5)

    assert labels_of(fixture) == list(fixture.truth)


def test_a_boundary_the_diarizer_placed_too_late_does_not_hand_words_to_the_wrong_speaker():
    # H 在 5.8 秒就說完了，但 diarizer 把換人點放在 6.7 秒：「我覺得」被抓成 H
    fixture = two_voices("你覺得呢？", "我覺得很好，但是還不夠", gap=0.2, h_end=0.9, g_start=0.7)

    labels = labels_of(fixture)

    assert [w.text for w in fixture.words][4:7] == ["我", "覺", "得"]
    assert labels[:4] == ["S1"] * 4
    assert labels[4:7] == ["S0"] * 3          # 不確定的字不給任何人
    assert labels[7] == "S0"                  # 換人點（6.7 秒）切過「很」（6.6–6.8 秒）：它也說不準
    assert labels[8:] == ["S2"] * (len(labels) - 8)


def test_a_boundary_the_diarizer_placed_too_early_does_not_hand_words_to_the_wrong_speaker():
    # diarizer 在 H 還沒說完時就換成 G：「覺得呢？」被抓成 G
    fixture = two_voices("你覺得呢？", "我覺得很好，但是還不夠", gap=0.2, h_end=-0.5, g_start=-0.7)

    labels = labels_of(fixture)

    assert labels[0] == "S1"
    assert labels[1:4] == ["S0"] * 3
    assert labels[4:] == ["S2"] * (len(labels) - 4)


def test_without_a_natural_boundary_nearby_only_the_two_words_at_the_change_are_undecided():
    fixture = two_voices("一二三四五六七八九十", "壹貳參肆伍陸柒捌玖拾", gap=0.0, h_end=-1.0, g_start=-1.0)

    labels = labels_of(fixture)

    assert labels.count("S0") == 2
    assert [i for i, label in enumerate(labels) if label == "S0"] == [4, 5]


def test_a_pause_shorter_than_the_threshold_is_not_a_natural_boundary():
    fixture = two_voices("你覺得呢", "我覺得很好", gap=0.25, h_end=0.0, g_start=0.0)

    labels = labels_of(fixture)

    assert labels.count("S0") == 2


def test_a_word_the_diarizer_change_point_cuts_through_is_undecided():
    # B 的一字句「好。」（1.0–1.4 秒），diarizer 把換人點放在 1.25 秒：字的中點落在 A，
    # 但另一位的區段蓋到這個字 0.15 秒，這個字是誰的說不準（審查用模擬量過：沒有這條，
    # 換人點在自然邊界上就完全信任，沒有任何時間上的餘裕）
    words = [Word(0.0, 1.0, "我同意。", 0), Word(1.0, 1.4, "好。", 0), Word(1.5, 2.5, "那我們開始。", 1)]
    turns = normalize_turns([SpeakerTurn(0.0, 1.25, "A"), SpeakerTurn(1.25, 3.0, "B")])

    assert [label for _, label in assign_speakers(words, turns)] == ["S1", "S0", "S2"]


def test_a_change_point_between_two_words_costs_nothing():
    words = [Word(0.0, 1.0, "我同意。", 0), Word(1.0, 2.0, "那我們開始。", 1)]
    turns = normalize_turns([SpeakerTurn(0.0, 1.0, "A"), SpeakerTurn(1.0, 3.0, "B")])

    assert [label for _, label in assign_speakers(words, turns)] == ["S1", "S2"]


def test_a_change_point_a_few_hundredths_inside_a_word_is_within_the_tolerance():
    # 語音辨識與 diarizer 的時間都以幾十毫秒為單位：差一兩格不算換人點切過這個字
    words = [Word(0.0, 1.0, "我同意。", 0), Word(1.0, 2.0, "那我們開始。", 1)]
    inside_a_little = normalize_turns([SpeakerTurn(0.0, 0.97, "A"), SpeakerTurn(0.97, 3.0, "B")])
    inside_a_lot = normalize_turns([SpeakerTurn(0.0, 0.8, "A"), SpeakerTurn(0.8, 3.0, "B")])

    assert [label for _, label in assign_speakers(words, inside_a_little)] == ["S1", "S2"]
    # 換人點在 0.8 秒：「我同意。」的中點（0.5）仍是 A，但 B 蓋到它 0.2 秒
    assert [label for _, label in assign_speakers(words, inside_a_lot)] == ["S0", "S2"]


def test_a_word_reaching_into_the_gap_before_the_next_speaker_is_undecided_too():
    # A 在 1.2 秒結束、B 在 1.3 秒開始，中間 0.1 秒沒有區段；「好。」（0.9–1.4）的中點在 A，
    # 但 B 蓋到它 0.1 秒
    words = [Word(0.0, 0.9, "我同意。", 0), Word(0.9, 1.4, "好。", 0), Word(1.5, 2.5, "那我們開始。", 1)]
    turns = normalize_turns([SpeakerTurn(0.0, 1.2, "A"), SpeakerTurn(1.3, 3.0, "B")])

    assert [label for _, label in assign_speakers(words, turns)] == ["S1", "S0", "S2"]


def test_a_speaker_overlapping_only_the_same_speakers_other_turn_is_not_a_change_point():
    words = [Word(0.0, 1.0, "我同意。", 0), Word(1.0, 2.0, "這個方案很好。", 0)]
    turns = normalize_turns([SpeakerTurn(0.0, 1.5, "A"), SpeakerTurn(0.8, 3.0, "A")])

    assert [label for _, label in assign_speakers(words, turns)] == ["S1", "S1"]


def test_words_covered_by_two_speakers_are_undecided():
    fixture = build([
        Say("H", "我先說一句話", gap=0, segment=0, also=(("G", 0.4, 1.0),)),
    ])

    labels = labels_of(fixture)

    # 「我先說一句話」：後半被 G 的聲音蓋住的三個字（5.4–6.0 秒）不給任何人
    assert labels == ["S1", "S1", "S0", "S0", "S0", "S1"]


def test_a_long_gap_between_the_same_speaker_is_not_filled():
    # 「乙」與「丙」之間隔了 3 秒，中間的「中」沒被任何區段蓋住：太久了，不推測
    words = [Word(0.0, 0.2, "甲"), Word(0.2, 0.4, "乙"), Word(1.0, 1.2, "中"),
             Word(3.4, 3.6, "丙"), Word(3.6, 3.8, "丁")]
    turns = [SpeakerTurn(0.0, 0.4, "x"), SpeakerTurn(3.4, 3.8, "x")]

    out = [label for _, label in assign_speakers(words, turns)]

    assert out == ["x", "x", "S0", "x", "x"]


def test_the_two_second_limit_for_filling_a_gap_is_inclusive():
    # 空隙裡的字一個接一個（沒有停頓、沒有句號），才輪得到「兩秒」這個上限
    def stretch(end):
        width = (end - 0.2) / 10
        gap = [Word(0.2 + k * width, 0.2 + (k + 1) * width, "中") for k in range(10)]
        return [Word(0.0, 0.2, "甲"), *gap, Word(end, end + 0.2, "乙")]

    turns = [SpeakerTurn(0.0, 0.15, "x"), SpeakerTurn(2.25, 3.0, "x")]
    exactly_two = stretch(2.2)      # 甲結束 0.2 → 乙開始 2.2
    just_over = stretch(2.3)        # 間隔 2.1 秒

    assert [label for _, label in assign_speakers(exactly_two, turns)] == ["x"] * 12
    assert [label for _, label in assign_speakers(just_over, turns)] == ["x"] + ["S0"] * 10 + ["x"]


def test_a_short_gap_between_the_same_speaker_is_filled():
    words = [Word(0.0, 0.2, "甲"), Word(0.2, 0.4, "乙"), Word(0.6, 0.8, "中"), Word(1.0, 1.2, "丙")]
    turns = [SpeakerTurn(0.0, 0.5, "x"), SpeakerTurn(0.9, 1.3, "x")]

    assert [label for _, label in assign_speakers(words, turns)] == ["x", "x", "x", "x"]


def test_a_gap_with_a_sentence_end_inside_is_not_filled_even_between_the_same_speaker():
    # A 說到「這個」，diarizer 在 2.0–2.4 秒沒有任何區段，B 在裡面插了一句「對。」，A 接著說。
    # 空隙裡有句子結尾：換人可能就藏在那裡，兩側都是 A 也不推測（審查用模擬量過：空隙補人
    # 是程式自己造成的錯誤歸屬裡幾乎唯一的來源）
    words = [Word(0.0, 1.0, "我覺得", 0), Word(1.0, 2.0, "這個", 0), Word(2.0, 2.4, "對。", 1),
             Word(2.4, 3.4, "問題", 2), Word(3.4, 4.4, "很大。", 2)]
    turns = normalize_turns([SpeakerTurn(0.0, 2.0, "A"), SpeakerTurn(2.4, 5.0, "A")])

    assert [label for _, label in assign_speakers(words, turns)] == ["S1", "S1", "S0", "S1", "S1"]


def test_a_gap_after_a_pause_is_not_filled_even_between_the_same_speaker():
    # 「甲」與「中」之間停了 0.6 秒：換人可能藏在停頓裡
    words = [Word(0.0, 0.4, "甲"), Word(1.0, 1.2, "中"), Word(1.2, 1.6, "乙")]
    turns = [SpeakerTurn(0.0, 0.5, "x"), SpeakerTurn(1.3, 1.7, "x")]

    assert [label for _, label in assign_speakers(words, turns)] == ["x", "S0", "x"]


def test_a_gap_before_a_pause_is_not_filled_even_between_the_same_speaker():
    # 「中」與「乙」之間停了 0.6 秒
    words = [Word(0.0, 0.2, "甲"), Word(0.2, 0.4, "中"), Word(1.0, 1.4, "乙")]
    turns = [SpeakerTurn(0.0, 0.25, "x"), SpeakerTurn(0.95, 1.5, "x")]

    assert [label for _, label in assign_speakers(words, turns)] == ["x", "S0", "x"]


def test_a_gap_in_the_middle_of_a_sentence_is_still_filled_when_nothing_can_hide_there():
    # 句子中間 diarizer 閃了一下沒蓋到「這個」：沒有句號、沒有停頓，仍然算同一位
    words = [Word(0.0, 0.3, "我"), Word(0.3, 0.6, "覺得"), Word(0.6, 0.9, "這個"),
             Word(0.9, 1.2, "問題"), Word(1.2, 1.5, "很大。")]
    turns = [SpeakerTurn(0.0, 0.6, "x"), SpeakerTurn(0.85, 1.6, "x")]

    assert [label for _, label in assign_speakers(words, turns)] == ["x"] * 5


def test_nothing_is_inherited_across_two_different_speakers_or_from_the_edges():
    words = [Word(0.0, 0.2, "空"), Word(0.5, 0.7, "甲"), Word(0.9, 1.1, "中"), Word(1.3, 1.5, "乙"),
             Word(1.7, 1.9, "尾")]
    turns = [SpeakerTurn(0.45, 0.8, "x"), SpeakerTurn(1.25, 1.6, "y")]

    out = [label for _, label in assign_speakers(words, turns)]

    assert out == ["S0", "x", "S0", "y", "S0"]


def test_no_turns_at_all_means_everything_is_undecided():
    words = [Word(0.0, 0.2, "甲"), Word(0.2, 0.4, "乙")]

    assert [label for _, label in assign_speakers(words, [])] == ["S0", "S0"]


def test_no_words_is_an_empty_result():
    assert assign_speakers([], [SpeakerTurn(0.0, 1.0, "x")]) == ()


def test_a_word_with_a_broken_time_is_undecided_and_cannot_break_the_others():
    words = [Word(0.0, 0.2, "甲"), Word(math.nan, math.nan, "壞"), Word(0.4, 0.6, "乙")]
    turns = [SpeakerTurn(0.0, 1.0, "x")]

    out = assign_speakers(words, turns)

    assert [label for _, label in out] == ["x", "S0", "x"]
    assert [w.text for w, _ in out] == ["甲", "壞", "乙"]
    assert all(math.isfinite(w.start) and math.isfinite(w.end) for w, _ in out)


def test_word_start_times_are_made_monotone_without_touching_the_text():
    words = [Word(1.0, 1.2, "甲"), Word(0.5, 0.9, "乙"), Word(1.3, 1.2, "丙")]

    out = assign_speakers(words, [SpeakerTurn(0.0, 3.0, "x")])

    starts = [w.start for w, _ in out]
    assert starts == sorted(starts)
    assert all(w.end >= w.start for w, _ in out)
    assert [w.text for w, _ in out] == ["甲", "乙", "丙"]


def test_overlapping_turns_of_the_same_speaker_are_not_an_overlap():
    words = [Word(1.0, 1.2, "甲")]
    turns = [SpeakerTurn(0.0, 2.0, "x"), SpeakerTurn(0.5, 1.5, "x")]

    assert assign_speakers(words, turns)[0][1] == "x"


def test_a_turn_labelled_s0_by_a_caller_is_never_treated_as_a_speaker():
    words = [Word(1.0, 1.2, "甲")]

    assert assign_speakers(words, [SpeakerTurn(0.0, 2.0, "S0")])[0][1] == "S0"


# --- assign_speakers 的安全性質（固定亂數）---


def random_fixture(rng: random.Random):
    pool = "我們你他的是不有在這那就和了很多少子化補助預算名額托育"
    script = []
    for k in range(rng.randint(3, 14)):
        text = f"第{k}段" + "".join(rng.choice(pool) for _ in range(rng.randint(1, 9)))
        text += rng.choice(["", "", "，", "。", "？"])
        jitter = lambda: rng.uniform(-0.6, 0.6)  # noqa: E731
        script.append(Say(
            rng.choice("HGV"), text, gap=rng.choice([0.0, 0.1, 0.3, 0.5, 1.0, 2.5]),
            segment=None if rng.random() < 0.7 else (k - 1 if k else 0),
            diar_start=jitter(), diar_end=jitter(), diar=rng.random() > 0.1,
            also=(("G", 0.2, 0.9),) if rng.random() < 0.1 else ()))
    return build(script)


def covering(turns, word):
    mid = (word.start + word.end) / 2
    return {t.speaker for t in turns if t.start <= mid < t.end}


@pytest.mark.parametrize("seed", range(60))
def test_a_word_is_never_given_to_a_speaker_the_diarizer_does_not_support(seed):
    fixture = random_fixture(random.Random(seed))
    turns = normalize_turns(fixture.turns)
    out = assign_speakers(fixture.words, turns)

    for i, (word, label) in enumerate(out):
        if label == "S0":
            continue
        # 別的說話者的區段不能蓋到這個字超過容許的時間（換人點切過這個字就說不準）
        for turn in turns:
            if turn.speaker != label:
                overlap = min(turn.end, word.end) - max(turn.start, word.start)
                assert overlap <= speakers.STRADDLE_TOLERANCE_SECONDS + 1e-6, (word.text, label, turn)
        cover = covering(turns, word)
        if cover:
            assert label in cover, f"字 {word.text!r}（{word.start}）被歸給 {label}，區段只支持 {cover}"
            continue
        # 沒被任何區段蓋住：只有左右最近的有人字都是同一位、且間隔不超過 2 秒才算他
        left = next((j for j in range(i - 1, -1, -1) if covering(turns, out[j][0])), None)
        right = next((j for j in range(i + 1, len(out)) if covering(turns, out[j][0])), None)
        assert left is not None and right is not None
        assert covering(turns, out[left][0]) == {label} == covering(turns, out[right][0])
        assert out[right][0].start - out[left][0].end <= speakers.INHERIT_MAX_GAP_SECONDS + 1e-6
        # 而且空隙裡（含兩端）沒有句子結尾或停頓：換人可能藏在那裡就不推測
        in_order = [w for w, _ in out]
        assert not any(speakers._is_natural_boundary(in_order, k) for k in range(left, right))


@pytest.mark.parametrize("seed", range(60))
def test_a_speaker_change_only_happens_at_a_sentence_end_or_a_pause(seed):
    fixture = random_fixture(random.Random(1000 + seed))
    out = assign_speakers(fixture.words, normalize_turns(fixture.turns))

    for (a, la), (b, lb) in zip(out, out[1:]):
        if la != lb and "S0" not in (la, lb):
            assert speakers._ends_sentence(a.text) or b.start - a.end >= speakers.PAUSE_SECONDS - 1e-6


# --- build_turns ---


def turns_of(fixture):
    return build_turns(assign_speakers(fixture.words, normalize_turns(fixture.turns)))


def test_the_dialogue_becomes_the_expected_turns():
    out = [(t.speaker, t.text) for t in turns_of(dialogue())]

    assert out == [
        ("S1", "今天我們來談少子化，先請教您怎麼看目前的補助政策？"),
        ("S2", "現金補助有幫助，但不能只靠現金，托育資源更重要。"),
        ("S1", "那公共托育現在有什麼不足？"),
        # H 的「嗯」只是語助詞：整段拿掉，G 前後兩半接起來
        ("S2", "名額不夠，很多家長排不到，排了兩年也沒有消息。而且費用從去年開始漲了三成很多家長只好放棄。"),
        ("S1", "所以您認為補助應該加倍嗎？"),
        ("S2", "不是，是要把名額加倍。"),
        ("S1", "是不是還要提高保母的薪資？"),
        ("S2", "對。"),
        ("S1", "好，那我再確認一次。您剛才說名額不夠，家長排不到，對嗎？"),
        ("S2", "對，名額不夠，家長排不到。"),
        ("S1", "謝謝邱委員，那預算要怎麼編才夠？"),
        ("S2", "預算大約要三千億，比去年多了兩成。至少要把公共托育的名額加倍。我們社區就是這樣。"),
        ("S1", "好的，謝謝您。我們現在接聽一位民眾的電話。"),
        ("S3", "我想補充，我們社區的托育名額真的不夠。"),
        ("S1", "謝謝您的來電。最後一個問"),
        ("S0", "題，所以其實—"),
        ("S2", "我想說的是，公共托育一定要先做。還有一件事，保母的薪資也要調。這樣可以嗎。"),
        ("S0", "請"),
        ("S1", "回答我們最後一題。"),
    ]


def test_answers_like_dui_and_bushi_stay_as_their_own_turns():
    out = turns_of(dialogue())

    assert ("S2", "對。") in [(t.speaker, t.text) for t in out]
    assert any(t.speaker == "S2" and t.text.startswith("不是，") for t in out)


def test_a_pure_filler_between_two_halves_is_removed_and_the_halves_join():
    words = [(Word(0.0, 0.2, "我", 0), "S2"), (Word(0.2, 0.4, "們", 0), "S2"),
             (Word(0.7, 0.9, "嗯", 1), "S1"), (Word(1.2, 1.4, "走", 2), "S2")]

    out = build_turns(words)

    assert [(t.speaker, t.text) for t in out] == [("S2", "我們走")]


def test_answer_tokens_are_never_treated_as_fillers():
    for token in ["對。", "不是。", "沒有。", "好。", "可以。"]:
        words = [(Word(0.0, 0.4, "請問嗎？", 0), "S1"), (Word(0.5, 0.9, token, 1), "S2"),
                 (Word(1.0, 1.4, "謝謝。", 2), "S1")]

        out = build_turns(words)

        assert [t.speaker for t in out] == ["S1", "S2", "S1"], token
        assert out[1].text == token


def test_the_same_speaker_repeating_a_segment_is_kept_once_but_another_speaker_saying_it_is_kept():
    same = [(Word(0.0, 0.4, "謝謝。", 0), "S1"), (Word(0.5, 0.9, "謝謝。", 1), "S1"),
            (Word(1.0, 1.4, "謝謝。", 2), "S1")]
    different = [(Word(0.0, 0.4, "對。", 0), "S1"), (Word(0.5, 0.9, "對。", 1), "S2")]

    assert [(t.speaker, t.text) for t in build_turns(same)] == [("S1", "謝謝。")]
    assert [(t.speaker, t.text) for t in build_turns(different)] == [("S1", "對。"), ("S2", "對。")]


def test_the_same_text_by_the_same_speaker_with_someone_in_between_is_not_a_repeat():
    words = [(Word(0.0, 0.4, "對。", 0), "S1"), (Word(0.5, 0.9, "好。", 1), "S2"),
             (Word(1.0, 1.4, "對。", 2), "S1")]

    assert [(t.speaker, t.text) for t in build_turns(words)] == [("S1", "對。"), ("S2", "好。"), ("S1", "對。")]


def test_the_same_words_in_one_segment_are_not_a_repeat():
    words = [(Word(0.0, 0.2, "對", 0), "S1"), (Word(0.2, 0.4, "對", 0), "S1")]

    assert [t.text for t in build_turns(words)] == ["對對"]


def test_a_long_monologue_is_cut_at_sentence_ends_below_the_limit():
    sentences = ["這是第一件很重要的事情，我們要先說清楚。", "這是第二件事情，需要大家一起想辦法。",
                 "這是第三件事情，希望能夠在年底以前完成。", "最後一件事情，就是要把預算編足。"]
    script = [Say("H", s, gap=0.0, segment=k) for k, s in enumerate(sentences)]
    fixture = build(script)

    out = build_turns(assign_speakers(fixture.words, normalize_turns(fixture.turns)), max_turn_seconds=7.0)

    assert len(out) >= 2
    assert all(t.end - t.start <= 7.0 + 1e-6 for t in out)
    assert all(t.text.endswith("。") for t in out)
    assert "".join(t.text for t in out) == "".join(sentences)


def test_a_long_run_without_punctuation_is_cut_at_a_comma_then_a_pause_then_anywhere():
    comma = [(Word(i * 0.5, i * 0.5 + 0.5, ("，" if i == 5 else "字"), 0), "S1") for i in range(12)]
    out = build_turns(comma, max_turn_seconds=4.0)
    assert out[0].text.endswith("，") and all(t.end - t.start <= 4.0 + 1e-6 for t in out)

    pause = [(Word(i * 0.5 + (0.4 if i >= 6 else 0.0), i * 0.5 + 0.5 + (0.4 if i >= 6 else 0.0), "字", 0), "S1")
             for i in range(12)]
    out = build_turns(pause, max_turn_seconds=4.0)
    assert len(out[0].text) == 6 and all(t.end - t.start <= 4.0 + 1e-6 for t in out)

    plain = [(Word(i * 0.5, i * 0.5 + 0.5, "字", 0), "S1") for i in range(12)]
    out = build_turns(plain, max_turn_seconds=4.0)
    assert len(out) == 2 and sum(len(t.text) for t in out) == 12
    assert all(t.end - t.start <= 4.0 + 1e-6 for t in out)


def test_one_word_longer_than_the_limit_still_makes_progress():
    words = [(Word(0.0, 100.0, "長", 0), "S1"), (Word(100.0, 100.2, "字", 0), "S1")]

    assert [t.text for t in build_turns(words, max_turn_seconds=1.0)] == ["長", "字"]


def test_a_long_silence_inside_one_speakers_words_starts_a_new_turn():
    words = [(Word(0.0, 0.2, "前", 0), "S1"), (Word(10.0, 10.2, "後", 0), "S1")]

    out = build_turns(words)

    assert [(t.text, t.start) for t in out] == [("前", 0.0), ("後", 10.0)]


def test_undecided_stretches_become_s0_turns_and_are_kept_in_order():
    words = [(Word(0.0, 0.2, "甲", 0), "S1"), (Word(0.2, 0.4, "乙", 0), "S0"),
             (Word(0.4, 0.6, "丙", 0), "S1")]

    assert [(t.index, t.speaker, t.text) for t in build_turns(words)] == [
        (0, "S1", "甲"), (1, "S0", "乙"), (2, "S1", "丙")]


def test_no_words_means_no_turns():
    assert build_turns([]) == ()


@pytest.mark.parametrize("seed", range(60))
def test_turns_keep_every_word_exactly_once_in_order(seed):
    rng = random.Random(5000 + seed)
    fixture = random_fixture(rng)
    assigned = assign_speakers(fixture.words, normalize_turns(fixture.turns))

    turns = build_turns(assigned, max_turn_seconds=rng.choice([3.0, 8.0, 45.0]))

    # 文字：每段接起來（去掉空白）等於每個字接起來（沒有語助詞、沒有重複段落可拿掉）
    squash = lambda s: "".join(s.split())  # noqa: E731
    assert squash("".join(t.text for t in turns)) == squash("".join(w.text for w, _ in assigned))
    # 編號從 0 連續，時間不倒退，一段只有一位說話者
    assert [t.index for t in turns] == list(range(len(turns)))
    assert all(t.start <= t.end for t in turns)
    assert all(a.end <= b.start + 1e-6 for a, b in zip(turns, turns[1:]))
    # 沒有字落在兩段裡：每段的字數加起來等於字數
    assert sum(len(squash(t.text)) for t in turns) == sum(len(squash(w.text)) for w, _ in assigned)
    # 每一段的說話者，等於它那個時間範圍裡字的標籤
    for turn in turns:
        inside = {label for w, label in assigned if turn.start <= w.start and w.end <= turn.end + 1e-9}
        assert turn.speaker in inside


# --- summarize_speakers ---


def make_turn(index, speaker, start, seconds, text="某句話"):
    return Turn(index, start, start + seconds, speaker, text)


def test_the_dialogue_summary_names_participants_and_minor_voices_neutrally():
    summary = summarize_speakers(turns_of(dialogue()))

    assert [(s.id, s.label, s.kind) for s in summary] == [
        ("S1", "發言者 A", "participant"), ("S2", "發言者 B", "participant"), ("S3", "其他聲音 1", "minor")]
    assert all(s.id != "S0" for s in summary)
    assert summary[2].turns == 1 and summary[2].first_seen == 57.0


def test_labels_follow_first_appearance_and_minor_voices_are_numbered_separately():
    turns = [make_turn(0, "S1", 0, 30), make_turn(1, "S1", 40, 30), make_turn(2, "S1", 80, 30),
             make_turn(3, "S3", 115, 5),
             make_turn(4, "S2", 130, 30), make_turn(5, "S2", 170, 30), make_turn(6, "S2", 210, 30),
             make_turn(7, "S4", 250, 4)]

    out = {s.id: s.label for s in summarize_speakers(turns)}

    assert out == {"S1": "發言者 A", "S3": "其他聲音 1", "S2": "發言者 B", "S4": "其他聲音 2"}


def test_a_speaker_with_less_than_the_scaled_threshold_or_fewer_than_three_turns_is_minor():
    turns = [make_turn(i, "S1", i * 20, 15) for i in range(10)]              # 150 秒
    turns += [make_turn(10 + i, "S2", 300 + i * 20, 12) for i in range(10)]  # 120 秒
    turns += [make_turn(20, "S3", 600, 40), make_turn(21, "S3", 650, 40)]     # 80 秒但只有 2 段
    turns += [make_turn(22 + i, "S4", 700 + i * 5, 1) for i in range(5)]     # 5 秒（< 27 秒）
    turns += [make_turn(27 + i, "S5", 800 + i * 20, 15) for i in range(3)]    # 45 秒、3 段（>= 27 秒）

    kinds = {s.id: s.kind for s in summarize_speakers(turns)}

    assert kinds == {"S1": "participant", "S2": "participant", "S3": "minor", "S4": "minor",
                     "S5": "participant"}


def test_a_speaker_with_two_long_turns_is_a_participant_when_they_hold_a_large_share_of_the_talk():
    # 訪談：來賓只回答了兩次，但每次都講了很久（180 秒裡佔 80 秒）。不能因為只有兩段，
    # 就整個人變成「其他聲音」、所有重點都被丟掉（審查用模擬對談量過：這是靜默的內容流失）
    turns = [make_turn(i, "S1", i * 40, 20) for i in range(5)]                 # 主持人 100 秒、5 段
    turns += [make_turn(5, "S2", 20, 40), make_turn(6, "S2", 100, 40)]          # 來賓 80 秒、2 段

    kinds = {s.id: s.kind for s in summarize_speakers(turns)}

    assert kinds == {"S1": "participant", "S2": "participant"}


def test_fewer_than_three_turns_only_excuses_a_share_of_a_quarter_or_more():
    def kind_of_second_speaker(seconds_each):
        turns = [make_turn(i, "S1", i * 100, 30) for i in range(5)]               # 150 秒、5 段
        turns += [make_turn(5, "S2", 20, seconds_each), make_turn(6, "S2", 120, seconds_each)]
        return {s.id: s.kind for s in summarize_speakers(turns)}["S2"]

    assert kind_of_second_speaker(25) == "participant"      # 50 / 200 秒 = 25%
    assert kind_of_second_speaker(24) == "minor"            # 48 / 198 秒 < 25%


def test_the_threshold_scales_down_for_short_media():
    # 全部說話 60 秒：門檻是 6 秒，不是 60 秒
    turns = [make_turn(i, "S1", i * 5, 3) for i in range(10)]
    turns += [make_turn(10 + i, "S2", 100 + i * 5, 3) for i in range(5)]

    kinds = {s.id: s.kind for s in summarize_speakers(turns)}

    assert kinds == {"S1": "participant", "S2": "participant"}


def test_a_19_second_single_speaker_video_still_has_one_participant():
    turns = [make_turn(0, "S1", 0, 10), make_turn(1, "S1", 12, 9)]

    out = summarize_speakers(turns)

    assert [(s.kind, s.label) for s in out] == [("participant", "發言者 A")]


def test_the_most_talkative_speaker_is_always_a_participant_even_with_two_turns():
    turns = [make_turn(0, "S1", 0, 200), make_turn(1, "S1", 300, 200), make_turn(2, "S2", 205, 3)]

    kinds = {s.id: s.kind for s in summarize_speakers(turns)}

    assert kinds == {"S1": "participant", "S2": "minor"}


def test_undecided_turns_are_never_listed_and_no_speech_means_no_speakers():
    assert summarize_speakers([make_turn(0, "S0", 0, 30)]) == ()
    assert summarize_speakers([]) == ()
    out = summarize_speakers([make_turn(0, "S0", 0, 30), make_turn(1, "S1", 30, 30)])
    assert [s.id for s in out] == ["S1"]


def test_samples_are_the_three_longest_turns_in_time_order_and_at_most_80_characters():
    turns = [make_turn(0, "S1", 0, 5, "短"), make_turn(1, "S1", 10, 30, "第二長" + "字" * 100),
             make_turn(2, "S1", 50, 20, "第三長"), make_turn(3, "S1", 80, 40, "最長"),
             make_turn(4, "S1", 130, 10, "第四")]

    sample = summarize_speakers(turns)[0].samples

    assert [s.start for s in sample] == [10.0, 50.0, 80.0]
    assert sample[0].text.endswith("…") and len(sample[0].text) == 80
    assert [s.text for s in sample[1:]] == ["第三長", "最長"]


def test_seconds_and_times_are_rounded_to_a_tenth():
    turns = [make_turn(0, "S1", 1.04, 2.26), make_turn(1, "S1", 10.0, 1.0)]

    info = summarize_speakers(turns)[0]

    assert (info.seconds, info.first_seen, info.last_seen) == (3.3, 1.0, 11.0)


def test_labels_go_past_z():
    assert speakers._letter(0) == "A" and speakers._letter(25) == "Z"
    assert speakers._letter(26) == "AA" and speakers._letter(27) == "AB"


# --- diarization_report ---


def report(turns, summary, **kw):
    base = dict(engine="pyannote.audio", version="4.0.7", model="m", fingerprint="0" * 16,
                requested_speakers=None)
    base.update(kw)
    return diarization_report(turns, summary, **base)


def balanced(n_people=2, seconds=100.0, minors=0, undecided=0.0):
    turns = []
    for i in range(n_people):
        turns += [Turn(len(turns) + k, 0.0, seconds / 10, f"S{i + 1}", "話") for k in range(10)]
    for m in range(minors):
        turns += [Turn(len(turns) + k, 0.0, 0.2, f"S{n_people + m + 1}", "話") for k in range(2)]
    if undecided:
        turns.append(Turn(len(turns), 0.0, undecided, "S0", "？"))
    return turns


def test_a_clean_two_person_conversation_is_ok():
    turns = balanced()

    out = report(turns, summarize_speakers(turns))

    assert (out.status, out.warnings, out.found_speakers, out.minor_speakers) == ("ok", (), 2, 0)
    assert out.engine == "pyannote.audio" and out.fingerprint == "0" * 16
    assert (out.proposed_points, out.kept_points) == (0, 0)


def test_a_fixed_count_that_disagrees_with_what_was_found_is_a_warning():
    turns = balanced(3)

    out = report(turns, summarize_speakers(turns), requested_speakers=2)

    assert out.status == "warn" and out.warnings == ("count_mismatch",) and out.requested_speakers == 2


def test_more_than_a_quarter_undecided_is_a_warning_but_exactly_a_quarter_is_not():
    turns = balanced(2, 100.0, undecided=0.0)
    total = sum(t.end - t.start for t in turns)
    high = balanced(2, 100.0, undecided=total / 3 + 1)          # > 25%
    edge = balanced(2, 100.0, undecided=total / 3)               # 剛好 25%

    assert report(high, summarize_speakers(high)).warnings == ("high_unassigned",)
    assert report(edge, summarize_speakers(edge)).warnings == ()


def test_four_minor_voices_are_a_warning_and_three_are_not():
    four = balanced(2, 100.0, minors=4)
    three = balanced(2, 100.0, minors=3)

    assert report(four, summarize_speakers(four)).warnings == ("many_minor",)
    assert report(three, summarize_speakers(three)).warnings == ()
    assert report(four, summarize_speakers(four)).minor_speakers == 4


def test_automatic_mode_finding_one_participant_is_a_warning():
    turns = balanced(1)

    out = report(turns, summarize_speakers(turns))

    assert out.status == "warn" and out.warnings == ("single_found",) and out.found_speakers == 1


def test_warnings_come_out_in_a_fixed_order():
    turns = balanced(3, 100.0, minors=4, undecided=500.0)

    out = report(turns, summarize_speakers(turns), requested_speakers=2)

    assert out.warnings == ("count_mismatch", "high_unassigned", "many_minor")


def test_asking_for_two_or_more_but_finding_one_fails_instead_of_publishing():
    turns = balanced(1)

    with pytest.raises(AttributionUnreliable, match="1"):
        report(turns, summarize_speakers(turns), requested_speakers=2)


def test_more_than_eight_participants_fails():
    turns = balanced(9)

    with pytest.raises(AttributionUnreliable, match="9"):
        report(turns, summarize_speakers(turns))
    eight = balanced(8)
    assert report(eight, summarize_speakers(eight)).found_speakers == 8


def test_twenty_four_voices_in_all_are_published_and_a_twenty_fifth_fails():
    """攔的 bug：只數主要發言者，8 位主要＋40 個其他聲音照樣過關，到新聞服務才因為
    speakers 超過 24 個被擋下來——GPU 白白做完整支影片，還把不可靠的區分當成可以發佈。"""
    twenty_four = balanced(2, 100.0, minors=22)
    assert len(summarize_speakers(twenty_four)) == 24
    out = report(twenty_four, summarize_speakers(twenty_four))
    assert (out.found_speakers, out.minor_speakers) == (2, 22)

    twenty_five = balanced(2, 100.0, minors=23)
    assert len(summarize_speakers(twenty_five)) == 25
    with pytest.raises(AttributionUnreliable):
        report(twenty_five, summarize_speakers(twenty_five))


def test_a_crowd_of_seven_main_speakers_and_seven_other_voices_is_published_with_warnings():
    """第一次真的跑的那支影片（指定 14 人）：7 位主要發言者、7 個其他聲音，共 14 個。以前超過 12 個就整支
    失敗；其他聲音不會被引用、事後也併得回去，所以照常發佈，只是網站上會掛警告。"""
    turns = balanced(7, 100.0, minors=7)
    assert len(summarize_speakers(turns)) == 14

    out = report(turns, summarize_speakers(turns), requested_speakers=14)

    assert (out.found_speakers, out.minor_speakers) == (7, 7)
    assert out.status == "warn" and out.warnings == ("count_mismatch", "many_minor")


def test_the_limit_counts_participants_and_other_voices_together():
    eight_and_sixteen = balanced(8, 100.0, minors=16)
    assert report(eight_and_sixteen, summarize_speakers(eight_and_sixteen)).found_speakers == 8
    eight_and_seventeen = balanced(8, 100.0, minors=17)
    with pytest.raises(AttributionUnreliable, match="共 25 個"):
        report(eight_and_seventeen, summarize_speakers(eight_and_seventeen))


def test_the_limit_is_the_one_the_news_service_checks_before_it_saves():
    assert speakers.MAX_PUBLISHED_SPEAKERS == 24


def test_the_limit_on_main_speakers_did_not_move_with_the_limit_on_all_voices():
    """上限放寬的只有「其他聲音」那一邊：主要發言者（可以被引用的人）仍然是 8 位，再多就是聲音區分不可靠。
    總數離上限還很遠（9 位主要＋0 個其他聲音）也一樣失敗。"""
    assert speakers.MAX_PARTICIPANTS == 8
    nine = balanced(9, 100.0, minors=0)
    assert len(summarize_speakers(nine)) == 9 < speakers.MAX_PUBLISHED_SPEAKERS
    with pytest.raises(AttributionUnreliable, match="找到 9 位主要發言者，超過上限 8 位"):
        report(nine, summarize_speakers(nine))


def test_the_message_for_too_many_voices_has_every_count_and_the_way_out():
    turns = balanced(3, 100.0, minors=22)

    with pytest.raises(AttributionUnreliable) as raised:
        report(turns, summarize_speakers(turns))

    message = str(raised.value)
    for needle in ("3 位主要發言者", "22 個其他聲音", "共 25 個", "上限 24 個", "--speakers N"):
        assert needle in message, needle


def test_when_a_count_was_already_given_the_message_does_not_tell_the_operator_to_give_one():
    turns = balanced(3, 100.0, minors=22)

    with pytest.raises(AttributionUnreliable) as raised:
        report(turns, summarize_speakers(turns), requested_speakers=4)

    message = str(raised.value)
    assert "已指定 4 人" in message and "共 25 個" in message and "--speakers N" not in message


def test_too_many_participants_is_still_reported_as_that_and_not_as_too_many_voices():
    """舊的失敗不變：同樣的輸入同樣的訊息。"""
    turns = balanced(9, 100.0, minors=5)

    with pytest.raises(AttributionUnreliable, match="找到 9 位主要發言者，超過上限 8 位"):
        report(turns, summarize_speakers(turns))


def test_no_participant_at_all_fails():
    with pytest.raises(AttributionUnreliable):
        report([Turn(0, 0.0, 5.0, "S0", "？")], summarize_speakers([Turn(0, 0.0, 5.0, "S0", "？")]))
    with pytest.raises(AttributionUnreliable):
        report([], ())


def test_ratios_and_seconds_are_rounded_and_overlap_is_a_share_of_speech():
    turns = balanced(2, 100.0, undecided=3.0)
    speech = sum(t.end - t.start for t in turns)

    out = report(turns, summarize_speakers(turns), overlap_seconds=speech / 10)

    assert out.speech_seconds == round(speech, 1)
    assert out.unassigned_ratio == round(3.0 / speech, 3)
    assert out.overlap_ratio == 0.1
    assert report(turns, summarize_speakers(turns), overlap_seconds=speech * 5).overlap_ratio == 1.0


# --- transcript_lines / clock ---


def test_transcript_lines_use_minutes_that_do_not_stop_at_99():
    turns = [Turn(0, 5.0, 9.0, "S1", "你好"), Turn(1, 125 * 60 + 30.9, 130 * 60.0, "S2", "再見"),
             Turn(2, 7500.0, 7501.0, "S0", "？")]

    assert transcript_lines(turns).split("\n") == [
        "00:05 [[S1]]：你好", "125:30 [[S2]]：再見", "125:00 [[S0]]：？"]
    assert clock(3725.4) == "62:05" and clock(-3) == "00:00"


def test_a_newline_inside_a_turn_cannot_break_the_one_line_per_turn_rule():
    assert transcript_lines([Turn(0, 0.0, 1.0, "S1", "上\n下")]) == "00:00 [[S1]]：上 下"
    assert transcript_lines([]) == ""


def test_line_k_is_turn_k_in_the_dialogue():
    turns = turns_of(dialogue())

    lines = transcript_lines(turns).split("\n")

    assert len(lines) == len(turns)
    for turn, line in zip(turns, lines):
        assert line.startswith(f"{clock(turn.start)} [[{turn.speaker}]]：")


def test_the_align_version_is_part_of_the_fingerprint_contract():
    assert ALIGN_VERSION == 1
    assert TRUE_SPEAKER == {"S1": "H", "S2": "G", "S3": "V"}
    assert tokenize("對。") == ["對。"]
