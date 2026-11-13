"""實驗室留下的 10 份真實模型輸出（合成對談，不是真人）原樣重播，確認產品的檢查和實驗室的參考驗證一致。

做法：用 gold.json 建 Turn 與 SpeakerInfo（說話者預設是主要發言者，標成少數聲音的、S0 不能引用），
把 call.json 的 raw_text 交給 parse_conversation_response，逐點跑 _check_point，和 expect.json 裡
實驗室的裁決比。F01／F02 是舊提示詞的輸出（沒有 quote）：把「被引用的那一輪整段話」當 quote 補上，
只比文字本身的檢查（數字、問句、名字、代名詞…）。

產品故意比實驗室更嚴的地方，與只是檢查順序不同的地方，各寫在 STRICTER 與 CODE_ORDER：每一項都有原因。
絕不接受實驗室丟掉的點（反過來才會發生）：這是整個歸屬機制的底線。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from slidebox.domain.entities import RawSlide, SpeakerInfo, Turn
from slidebox.infrastructure.ollama_conversation import conversation_user_prompt, parse_conversation_response
from slidebox.usecases import conversation as conv
from slidebox.usecases.diarized_pipeline import (
    MAX_DROPPED_RATIO,
    dropped_ratio,
    fit_to_range,
    retry_hint,
    summarize_windows,
)
from slidebox.usecases.names import detect_names
from slidebox.usecases.speakers import squash

from .diarized_support import FakeConversation

FIXTURES = Path(__file__).parent / "fixtures" / "attribution"
MAX_SLIDES = 5                      # 實驗室每次都要求「2 到 5 頁」

PROD = ["F01_prod_qa_official_names", "F02_prod_retry_debate"]
P4 = [
    "F03_p4_clean_callin", "F04_p4_debate_stitched_quote", "F05_p4_qa_restatement_and_rescue",
    "F06_p4f_panel_percent_to_minutes", "F07_p4_asr_numbers_and_false_flags",
    "F08_p4f_press_wrong_speaker_reanchor", "F09_p4f_monologue_percent_to_cheng", "F10_gemma4_overlong_slide",
]

# 產品比實驗室的參考更嚴：實驗室留下、產品丟掉（原因）
STRICTER = {
    ("F03_p4_clean_callin", 2, 1): (
        "number_mismatch", "「平均提前三十分鐘」被寫成「提前三分鐘」：實驗室的參考把「三」當成「三十」的一部分、算照抄，"
                          "產品要求整個數字相符（第一次真模型的完整跑一遍，2 次裡有 2 次出現這個錯）"),
    ("F10_gemma4_overlong_slide", 3, 22): (
        "pronoun_in_text", "「對方」在文字中間：產品把代名詞檢查延伸到整段文字，實驗室的參考只看句首"),
}
# 兩邊都丟，但原因碼不同：產品先看文字（名字）、再看出處（anchor），最後才看數字、問句
CODE_ORDER = {
    ("F04_p4_debate_stitched_quote", 3, 1): ("number_mismatch", "anchor_conflict"),
    ("F10_gemma4_overlong_slide", 2, 4): ("number_mismatch", "anchor_conflict"),
    ("F10_gemma4_overlong_slide", 3, 18): ("question_stated_as_fact", "name_in_text"),
}
# 舊提示詞的輸出裡，被引用的那一輪只有一個字（「有。」）：單字的回答不能當 quote，產品一律丟掉
SINGLE_CHARACTER_ANSWERS = {("F01_prod_qa_official_names", 2, 6), ("F01_prod_qa_official_names", 6, 2)}
# 舊提示詞第一次的輸出：兩邊都丟，原因碼不同（實驗室看到數字，產品先看到內容出自別輪：「兩百一十份」在 T3，
# 「問卷在店門口發放、不能當政策依據」是 T4 說的）。鍵是（call 檔, 頁, 點）。
OLD_PROMPT_CODE_ORDER = {("call_first.json", 2, 1): ("number_mismatch", "anchor_conflict")}


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class Replay:
    name: str
    call: dict
    gold: dict
    expect: dict
    turns: tuple[Turn, ...]
    speakers: tuple[SpeakerInfo, ...]
    window: conv.Window
    raw: tuple[RawSlide, ...]
    checks: dict


def replay_of(name: str, call_file: str = "call.json", expect_file: str = "expect.json") -> Replay:
    folder = FIXTURES / name
    call, gold, expect = load(folder / call_file), load(folder / "gold.json"), load(folder / expect_file)
    turns = tuple(Turn(t["index"], float(t["start"]), float(t["end"]), t["speaker"], t["text"]) for t in gold["turns"])
    kinds = {s: (gold["speakers"].get(s) or {}).get("kind", "participant") for s in {t.speaker for t in turns}}
    ids = sorted({t.speaker for t in turns} - {"S0"})
    speakers = tuple(SpeakerInfo(id=s, label=s, kind="participant" if kinds[s] == "participant" else "minor",
                                 seconds=60.0, turns=5, first_seen=0.0, last_seen=60.0) for s in ids)
    window = conv.Window(call["window"]["k"], turns)
    raw = parse_conversation_response(call["raw_text"])
    if name in PROD:                              # 舊提示詞沒有 quote：把被引用的那一輪整段話補上
        by_index = {t.index: t for t in turns}
        raw = tuple(replace(slide, points=tuple(
            replace(p, quote=by_index[p.turn].text) if not p.quote and p.turn in by_index else p
            for p in slide.points)) for slide in raw)
    by_index = {t.index: t for t in turns}
    in_window = [by_index[i] for i in range(window.first_turn, window.last_turn + 1) if i in by_index]
    citable = conv.citable_ids(speakers)
    names = detect_names("\n".join(t.text for t in in_window))
    checks = {(page, number): conv._check_point(point, by_index, in_window, citable, window.first_turn,
                                                window.last_turn, names)
              for page, slide in enumerate(raw, 1) for number, point in enumerate(slide.points, 1)}
    return Replay(name, call, gold, expect, turns, speakers, window, raw, checks)


def lab_code(decision: str) -> str:
    """實驗室的「dropped:…」裁決換成產品的原因碼。"""
    code = decision.split(":", 1)[1]
    if code.startswith("other:文字裡有逐字稿用來稱呼人的名字"):
        return conv.NAME_IN_TEXT
    if code.startswith("other:數字"):
        return conv.NUMBER_MISMATCH
    if code.startswith("unsupported"):
        return conv.UNSUPPORTED
    if code == "clause_pronoun":
        return conv.PRONOUN_IN_TEXT
    return code


def flat(text: str) -> str:
    return " ".join(text.split())


# --- 新提示詞（P4）的輸出：逐點和實驗室的最終裁決一致 ---


@pytest.mark.parametrize("name", P4)
def test_every_point_gets_the_verdict_the_lab_reached_except_where_the_product_is_deliberately_stricter(name):
    replay = replay_of(name)
    judged = 0
    for row in replay.expect["points"]:
        decision = row["hybrid_decision"]
        if decision is None:
            continue                              # 超過頁數上限而被砍掉的頁：沒有被裁決過
        judged += 1
        position = (row["page"], row["number"])
        key = (name, *position)
        check = replay.checks[position]
        if key in STRICTER:
            code, _reason = STRICTER[key]
            assert decision.startswith("kept") and check.point is None and check.problem.code == code, key
        elif decision.startswith("dropped"):
            lab = lab_code(decision)
            expected = CODE_ORDER[key][1] if key in CODE_ORDER else lab
            if key in CODE_ORDER:
                assert CODE_ORDER[key][0] == lab, key
            assert check.point is None and check.problem.code == expected, (key, decision, check)
        else:
            assert check.point is not None, (key, decision, check.problem)
            assert check.rescued == (decision == "rescued_quote"), key
            if decision == "kept_paraphrase":
                anchor = row["reference_v1b_v2_v3_fragment_quote_guard"]["anchor_turn"]
                assert check.point.turn == anchor, key
    assert judged > 0


@pytest.mark.parametrize("name", PROD + P4)
def test_the_product_never_keeps_a_point_the_lab_dropped(name):
    """底線：絕不比實驗室的參考更寬鬆（舊提示詞的輸出用文字檢查的參考比）。"""
    replay = replay_of(name)
    for row in replay.expect["points"]:
        position = (row["page"], row["number"])
        if name in PROD:
            lab_dropped = row["reference_v1b_v2_v3"]["status"] == "dropped"
        else:
            decision = row["hybrid_decision"]
            lab_dropped = decision is not None and decision.startswith("dropped")
        if lab_dropped:
            assert replay.checks[position].point is None, (name, position, row["text"])


@pytest.mark.parametrize("name, call_file, expect_file", [
    ("F01_prod_qa_official_names", "call.json", "expect.json"),
    ("F02_prod_retry_debate", "call.json", "expect.json"),
    ("F02_prod_retry_debate", "call_first.json", "expect_first.json"),
])
def test_the_old_prompts_outputs_get_the_labs_text_verdicts(name, call_file, expect_file):
    replay = replay_of(name, call_file, expect_file)
    for row in replay.expect["points"]:
        position = (row["page"], row["number"])
        reference = row["reference_v1b_v2_v3"]
        check = replay.checks[position]
        lab_kept = reference["status"] in ("kept", "slide_dropped")      # slide_dropped：點本身過了，整頁因為別的點被丟
        if (name, *position) in SINGLE_CHARACTER_ANSWERS:
            assert lab_kept and check.point is None and check.problem.code == conv.BAD_QUOTE
            assert "太短" in check.problem.message
        elif lab_kept:
            assert check.point is not None, (name, position, check.problem)
        else:
            assert check.point is None, (name, position, row["text"])
            lab = lab_code("dropped:" + reference["code"])
            expected = lab
            if (call_file, *position) in OLD_PROMPT_CODE_ORDER:
                before, expected = OLD_PROMPT_CODE_ORDER[(call_file, *position)]
                assert before == lab, (name, position)
            assert check.problem.code == expected, (name, position, check.problem)


# --- 每個留下的點：說話者是那一輪的人、原話逐字在那一輪裡 ---


@pytest.mark.parametrize("name", PROD + P4)
def test_every_kept_point_is_the_speaker_of_its_turn_and_a_rescued_one_is_a_literal_span_of_it(name):
    replay = replay_of(name)
    by_index = {t.index: t for t in replay.turns}
    gold_speaker = {t["index"]: t["speaker"] for t in replay.gold["turns"]}
    participants = conv.citable_ids(replay.speakers)
    kept = rescued = 0
    for check in replay.checks.values():
        if check.point is None:
            continue
        point = check.point
        kept += 1
        assert point.speaker == by_index[point.turn].speaker == gold_speaker[point.turn]
        assert point.speaker in participants
        if check.rescued:
            rescued += 1
            assert point.text[0] == "「" and point.text[-1] == "」"
            assert point.text[1:-1] in flat(by_index[point.turn].text)
            assert len(squash(point.text)) >= conv.MIN_QUOTE_CHARS
    assert kept > 0


# --- 整份：頁數、每頁上限、丟掉的分類 ---


def totals(replay: Replay):
    result = conv.validate_points(replay.raw, replay.turns, replay.speakers, replay.window)
    fitted, cut = fit_to_range(result, MAX_SLIDES)
    return result, fitted, cut


@pytest.mark.parametrize("name", PROD + P4)
def test_the_counts_of_a_whole_window_add_up(name):
    result, fitted, cut = totals(replay_of(name))

    assert result.proposed - result.kept == result.contradicted + result.unverifiable + result.slide_rule
    assert sum(n for _, n in result.dropped_by_code) == result.proposed - result.kept
    assert result.rescued == sum(s.rescued for s in result.slides)
    assert all(len(s.points) <= conv.MAX_BULLETS for s in result.slides)
    assert fitted.proposed - fitted.kept == result.proposed - result.kept
    assert len(fitted.slides) <= MAX_SLIDES and len(result.slides) - len(fitted.slides) == cut


# 每份的整窗數字：(提出, 留下, 其中顯示原話, 有問題, 查不出來, 同頁規則)，砍頁之後、進整份的比例檢查之前。
# 和實驗室比：留下的點少的，只有 F01（兩個單字的回答）、F03（一個數字：三分鐘 ≠ 三十分鐘）與 F10（一個代名詞）；
# F10 提出的 25 個比模型寫的 48 個少 23 個：
# 留下的點超過每頁 4 個的部分不算提出，也不算丟掉；F09 少的 4 個是被砍掉的第 6 頁。
WINDOW_TOTALS = {
    "F01_prod_qa_official_names": (45, 5, 0, 36, 2, 2),
    "F02_prod_retry_debate": (10, 8, 0, 1, 0, 1),
    "F03_p4_clean_callin": (16, 14, 0, 2, 0, 0),
    "F04_p4_debate_stitched_quote": (16, 11, 0, 3, 2, 0),
    "F05_p4_qa_restatement_and_rescue": (16, 13, 3, 1, 2, 0),
    "F06_p4f_panel_percent_to_minutes": (16, 14, 0, 1, 1, 0),
    "F07_p4_asr_numbers_and_false_flags": (16, 13, 2, 2, 1, 0),
    "F08_p4f_press_wrong_speaker_reanchor": (16, 14, 1, 1, 1, 0),
    "F09_p4f_monologue_percent_to_cheng": (20, 17, 1, 3, 0, 0),
    "F10_gemma4_overlong_slide": (25, 9, 0, 14, 2, 0),
}
# 這些窗在管線的比例檢查（被丟掉的不超過 25%）會過關；沒過關的，管線會帶著原因再問一次。
# F01 的舊提示詞、F04 的接合引文、F10 的長頁都在這裡被擋下來，F02 的第一次也是（見下面）。
GATE_ACCEPTS = {name: True for name in WINDOW_TOTALS} | {
    "F01_prod_qa_official_names": False, "F04_p4_debate_stitched_quote": False, "F10_gemma4_overlong_slide": False}


@pytest.mark.parametrize("name", PROD + P4)
def test_the_numbers_of_every_whole_window_are_pinned_and_so_is_the_gates_verdict(name):
    _, fitted, _ = totals(replay_of(name))

    assert (fitted.proposed, fitted.kept, fitted.rescued, fitted.contradicted, fitted.unverifiable,
            fitted.slide_rule) == WINDOW_TOTALS[name]
    assert (dropped_ratio(fitted.proposed, fitted.kept) <= MAX_DROPPED_RATIO) is GATE_ACCEPTS[name]


def test_f09_a_sixth_slide_is_cut_without_counting_its_points_as_proposed_or_dropped():
    result, fitted, cut = totals(replay_of("F09_p4f_monologue_percent_to_cheng"))

    assert (len(result.slides), len(fitted.slides), cut) == (6, 5, 1)
    assert (result.proposed, result.kept, result.rescued) == (24, 21, 1)
    assert (fitted.proposed, fitted.kept, fitted.rescued) == (20, 17, 1)       # 少的 4 點是第 6 頁留下的點
    assert fitted.proposed - fitted.kept == result.proposed - result.kept == 3


def test_f10_a_slide_of_thirty_nine_points_shows_four_and_its_other_points_are_not_counted_as_proposed():
    replay = replay_of("F10_gemma4_overlong_slide")
    result, _, _ = totals(replay)

    assert [len(slide.points) for slide in replay.raw] == [4, 5, 39]
    assert [len(slide.points) for slide in result.slides] == [2, 3, conv.MAX_BULLETS]
    assert sum(len(slide.points) for slide in replay.raw) - result.proposed == 23       # 留下卻超出每頁 4 點的
    assert result.proposed - result.kept == 16 == sum(n for _, n in result.dropped_by_code)


# --- 這幾份各自要釘住的事（manifest.json 的 why）---


def test_f01_the_old_prompts_names_and_titles_are_dropped_by_the_transcript_derived_name_check():
    replay = replay_of("F01_prod_qa_official_names")
    by_code = {}
    for position, check in replay.checks.items():
        if check.problem:
            by_code.setdefault(check.problem.code, []).append(position)

    assert by_code[conv.NAME_IN_TEXT] == [(4, 10), (5, 8), (6, 4), (6, 8)]
    assert "委員" in "".join(detect_names("\n".join(t.text for t in replay.turns)))


def test_f03_a_computed_ratio_the_speaker_never_said_is_dropped_by_the_number_check():
    replay = replay_of("F03_p4_clean_callin")

    assert replay.checks[(3, 4)].problem.code == conv.NUMBER_MISMATCH


def test_f04_a_quote_stitched_across_turns_is_dropped_by_the_fragment_guard():
    replay = replay_of("F04_p4_debate_stitched_quote")

    assert replay.checks[(4, 2)].problem.code == conv.BAD_QUOTE
    assert "把兩輪接在一起" in replay.checks[(4, 2)].problem.message


def test_f05_the_restatement_is_not_rescued_nor_is_a_quote_with_a_role_word_inside():
    replay = replay_of("F05_p4_qa_restatement_and_rescue")

    assert sorted(c.point.turn for c in replay.checks.values() if c.rescued) == [4, 27, 34]
    assert "複述對方" in replay.checks[(3, 3)].problem.message
    assert "部長" in replay.checks[(1, 4)].problem.message


def test_f06_a_percentage_rewritten_as_minutes_is_dropped_by_the_number_check_alone():
    replay = replay_of("F06_p4f_panel_percent_to_minutes")

    assert replay.checks[(3, 1)].problem.code == conv.NUMBER_MISMATCH
    assert "是百分比，不是分鐘" in replay.checks[(3, 1)].problem.message


def test_f08_figures_the_spokesman_said_and_a_reporter_repeated_in_a_question_are_not_moved_to_the_reporter():
    """最典型的歸錯人：quote 在發言人 S1 的 T0，內容卻和記者 S2 在 T2 複述數字的問句最像。
    舊的檢查把這個重點改掛到 T2、留下來（寫成記者說的）；現在 quote 所在的輪與內容出處的輪不一致就丟掉。"""
    replay = replay_of("F08_p4f_press_wrong_speaker_reanchor")
    check = replay.checks[(1, 3)]

    assert replay.raw[0].points[2].turn == 0 and "六千三百二十位" in replay.raw[0].points[2].quote
    assert check.point is None and check.problem.code == conv.ANCHOR_CONFLICT
    assert "（T2）" in check.problem.message and "T0" in check.problem.message
    assert replay.checks[(2, 1)].rescued and replay.checks[(2, 1)].point.turn == 9      # 沒有歧義的才改用原話


def test_f09_percentages_written_as_tenths_are_dropped_and_the_numeral_formats_are_not_confused():
    replay = replay_of("F09_p4f_monologue_percent_to_cheng")
    wrong = {pos: check.problem.message for pos, check in replay.checks.items()
             if check.problem and check.problem.code == conv.NUMBER_MISMATCH}

    assert sorted(wrong) == [(4, 1), (4, 4), (5, 2)]
    assert "「五十五成」不在被引用的那段話裡（百分比的數值對不上" in wrong[(4, 4)]
    assert "「九十一成」不在被引用的那段話裡（百分比的數值對不上" in wrong[(5, 2)]
    assert "「三百」" in wrong[(4, 1)]


def test_f07_the_two_points_the_scorer_flags_are_kept_because_the_speakers_did_say_them():
    replay = replay_of("F07_p4_asr_numbers_and_false_flags")
    by_index = {t.index: t for t in replay.turns}

    for position, turn in (((1, 4), 12), ((3, 2), 41)):
        check = replay.checks[position]
        assert check.point is not None and check.point.turn == turn
        assert check.point.speaker == by_index[turn].speaker


def test_f02_the_two_points_the_scorer_calls_misattributed_are_the_speakers_own_words():
    """計分器看到數字出現在另一位說話者的話裡就標「歸錯人」，但兩點都是 T4、T41 的 S1 自己說的（手動讀過）。
    不要把「說話者必須是唯一提到這個數字的人」寫成規則：複述對方的數字再反駁，本來就會出現在兩個人的話裡。"""
    replay = replay_of("F02_prod_retry_debate")
    by_index = {t.index: t for t in replay.turns}

    for position, turn, figures in (((1, 1), 4, ("兩百", "百分之十二")), ((4, 2), 41, ("十二格", "二十格", "拖吊"))):
        check = replay.checks[position]
        assert check.point is not None and check.point.turn == turn and check.point.speaker == "S1"
        assert all(figure in by_index[turn].text for figure in figures)
        assert figures[0] in by_index[turn - 1].text                                  # 前一輪對方也提到


# --- F02：重試 ---

RETRY_HEADER = "上一次的結果有這些問題，請修正後重新產出（只修正被指出的地方）："


def lab_retry_hint() -> str:
    """實驗室第二次呼叫實際送出的、夾在範圍與逐字稿之間的那一段。"""
    lab_user = load(FIXTURES / "F02_prod_retry_debate" / "call.json")["user_message"]
    return lab_user[lab_user.index(RETRY_HEADER):lab_user.index("\n\n以下是對談：")]


def f02_attempts():
    first = replay_of("F02_prod_retry_debate", "call_first.json", "expect_first.json")
    return first, replay_of("F02_prod_retry_debate")


def test_f02_the_first_attempt_is_rejected_and_the_second_accepted_by_the_pipelines_gate():
    first, second = (totals(replay)[1] for replay in f02_attempts())

    assert (first.proposed, first.kept, second.proposed, second.kept) == (10, 6, 10, 8)
    assert dropped_ratio(first.proposed, first.kept) == 0.4 > MAX_DROPPED_RATIO
    assert dropped_ratio(second.proposed, second.kept) == 0.2 <= MAX_DROPPED_RATIO


def test_f02_the_retry_hint_is_the_labs_format():
    lab_hint = lab_retry_hint()
    lab_lines = [line[2:] for line in lab_hint.splitlines()[1:]]

    assert lab_hint.splitlines()[0] == RETRY_HEADER and len(lab_lines) == 9
    assert retry_hint(lab_lines) == lab_hint


def test_f02_the_hint_the_product_writes_for_the_first_attempt_lists_what_the_checks_rejected():
    first, _ = f02_attempts()
    mine = retry_hint(conv.validate_points(first.raw, first.turns, first.speakers, first.window).problems).splitlines()
    lab = lab_retry_hint().splitlines()

    assert mine[0] == RETRY_HEADER
    assert all(line.startswith("- 第 ") for line in mine[1:])
    # 和實驗室那次的原因有兩行一字不差：問句，與「整頁不用」；其他行的原因是產品的檢查先看到的（內容出自別輪）
    assert "- 第 3 頁第 2 點：被引用的是一句問句，重點要寫成「詢問…」「追問…」，不能寫成陳述（T9）" in mine
    assert "- 第 3 頁第 2 點：被引用的是一句問句，重點要寫成「詢問…」「追問…」，不能寫成陳述（T9）" in lab
    assert "- 第 3 頁只剩 1 個通過檢查的重點，至少要 2 個，整頁不用" in mine
    assert "- 第 3 頁只剩 1 個通過檢查的重點，至少要 2 個，整頁不用" in lab
    assert "- 第 2 頁第 1 點：重點的內容出自別輪發言（T4），與 quote 所在的 T3 不一致；一個重點只寫 quote 所在那一輪說的事" in mine


def test_f02_the_hint_sits_between_the_range_lines_and_the_transcript_and_the_transcript_is_the_labs_byte_for_byte():
    lab_user = load(FIXTURES / "F02_prod_retry_debate" / "call.json")["user_message"]
    lab_transcript = lab_user.split("以下是對談：\n", 1)[1]
    replay = replay_of("F02_prod_retry_debate")
    text = conv.format_window(replay.window, replay.speakers)

    prompt = conversation_user_prompt(text, replay.window.first_turn, replay.window.last_turn, 2, MAX_SLIDES,
                                      lab_retry_hint())

    assert text == lab_transcript.rstrip("\n")
    assert prompt.split("\n\n") == [
        "這個片段的發言編號是 T0 到 T88，turn 只能填 0 到 88 之間的整數。\n請整理成 2 到 5 頁。",
        lab_retry_hint(),
        "以下是逐字稿：\n" + text + "\n"]


def test_f02_through_the_pipeline_the_second_attempt_gets_the_hint_and_is_the_one_used():
    first, second = f02_attempts()
    conversation = FakeConversation([first.raw, second.raw])
    allocation = conv.Allocation(second.window, 2, MAX_SLIDES)

    (outcome,) = summarize_windows(conversation, (allocation,), second.turns, second.speakers,
                                   lambda *_: None, lambda: None, lambda: False)

    first_result = conv.validate_points(first.raw, first.turns, first.speakers, first.window)
    assert [call.hint for call in conversation.calls] == ["", retry_hint(first_result.problems)]
    assert [(call.first_turn, call.last_turn) for call in conversation.calls] == [(0, 88), (0, 88)]
    assert (outcome.proposed, outcome.kept, outcome.rescued) == (10, 8, 0)
    assert len(outcome.slides) == 4
    assert outcome.dropped_by_code == (("negation_mismatch", 1), ("slide_too_small", 1))
