"""字幕壓縮：滾動字幕去重、秒數標記、超出預算時等比例截短。"""
from __future__ import annotations

import re

from slidebox.domain.entities import Cue
from slidebox.usecases.chapters import compress_cues


def test_marks_use_raw_seconds_not_mmss():
    """標記用秒數，讓模型直接抄數字，不必做 MM:SS 換算。"""
    cues = (Cue(75.0, 78.0, "哈囉"),)
    assert compress_cues(cues, 1000).startswith("[75] ")


def test_merges_cues_within_the_same_window():
    cues = (Cue(0.0, 2.0, "甲"), Cue(3.0, 5.0, "乙"), Cue(6.0, 8.0, "丙"))
    out = compress_cues(cues, 1000, window=15.0)
    assert out == "[0] 甲 乙 丙"


def test_starts_a_new_line_past_the_window():
    cues = (Cue(0.0, 2.0, "甲"), Cue(20.0, 22.0, "乙"))
    assert compress_cues(cues, 1000, window=15.0).split("\n") == ["[0] 甲", "[20] 乙"]


def test_drops_cue_fully_contained_in_previous():
    """滾動字幕：前一句已經包含這一句，整句丟掉。"""
    cues = (Cue(0.0, 2.0, "我們今天要講的是"), Cue(1.0, 3.0, "我們今天要"))
    assert compress_cues(cues, 1000) == "[0] 我們今天要講的是"


def test_emits_only_the_new_suffix_when_previous_is_a_prefix():
    cues = (Cue(0.0, 2.0, "我們今天"), Cue(1.0, 3.0, "我們今天要講 Python"))
    assert compress_cues(cues, 1000) == "[0] 我們今天 要講 Python"


def test_trims_overlapping_tail_and_head():
    """前句尾與後句頭重疊的部分只保留一次。"""
    cues = (Cue(0.0, 2.0, "abcdef"), Cue(1.0, 3.0, "defghi"))
    assert compress_cues(cues, 1000) == "[0] abcdef ghi"


def test_result_fits_the_char_budget():
    # 每段內容必須各不相同，否則會被去重邏輯吃掉，測試就變成假通過
    cues = tuple(
        Cue(float(i * 20), float(i * 20 + 5), f"第{i}段內容" * 20) for i in range(40)
    )
    out = compress_cues(cues, 500)
    assert len(out) <= 500


def test_truncation_keeps_whole_video_coverage():
    """超出預算時截短每一段，而不是丟掉後面的段落——最後一段必須還在。"""
    cues = tuple(
        Cue(float(i * 20), float(i * 20 + 5), f"第{i}段內容" * 20) for i in range(40)
    )
    out = compress_cues(cues, 500)
    marks = [int(m) for m in re.findall(r"^\[(\d+)\]", out, flags=re.M)]
    assert marks[0] == 0
    assert marks[-1] == 780        # 第 40 段的起點 39*20
    assert len(marks) == 40        # 每一段都仍有代表


def test_empty_input_returns_empty_string():
    assert compress_cues((), 1000) == ""


def test_single_character_overlap_between_sentences_is_not_trimmed():
    """相鄰中文句子的單字重疊是正常巧合，不是滾動字幕殘留，不該被修剪。"""
    cues = (Cue(0.0, 2.0, "今天天氣很好"), Cue(3.0, 5.0, "好的我們開始吧"))
    assert compress_cues(cues, 10000) == "[0] 今天天氣很好 好的我們開始吧"


def test_short_standalone_cue_survives():
    """對／好／是啊這類短句本身就是內容，不能被當成重複整句丟掉。"""
    cues = (Cue(0.0, 2.0, "我覺得這樣做是對的"), Cue(3.0, 5.0, "對"), Cue(6.0, 8.0, "我們繼續"))
    out = compress_cues(cues, 10000)
    assert out == "[0] 我覺得這樣做是對的 對 我們繼續"


def test_manual_subtitles_skip_rolling_dedupe():
    """is_automatic=False：手動字幕不滾動，滾動去重只會誤刪內容。"""
    cues = (Cue(0.0, 2.0, "我們今天要講的是"), Cue(1.0, 3.0, "我們今天要"))
    out = compress_cues(cues, 1000, is_automatic=False)
    assert out == "[0] 我們今天要講的是 我們今天要"
