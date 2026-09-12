"""完整逐字稿：給人讀的版本，不受摘要用的字元預算影響。"""
from __future__ import annotations

from slidebox.domain.entities import Cue
from slidebox.usecases.chapters import compress_cues, full_transcript


def _cues(n: int) -> tuple[Cue, ...]:
    return tuple(
        Cue(start=float(i * 20), end=float(i * 20 + 19), text=f"第{i}段內容" * 20)
        for i in range(n)
    )


def test_nothing_is_truncated_even_when_far_over_the_summary_budget():
    """攔的 bug：沿用 compress_cues 產生附錄，長影片的每一段都會被截半句。
    附錄的價值就在於完整。"""
    cues = _cues(30)
    full = full_transcript(cues)
    squeezed = compress_cues(cues, char_budget=500)

    assert len(squeezed) <= 500
    for i in range(30):
        assert f"第{i}段內容" * 20 in full


def test_timestamps_are_human_readable():
    cues = (Cue(start=125.0, end=130.0, text="開始講重點"),)
    assert full_transcript(cues).startswith("02:05 ")


def test_rolling_duplicates_are_still_removed_for_automatic_captions():
    """自動字幕滾動重複若不去掉，附錄會是同一句話重複十幾次，讀不下去。"""
    cues = (
        Cue(start=0.0, end=2.0, text="今天我們來談"),
        Cue(start=2.0, end=4.0, text="今天我們來談談壓縮"),
    )
    assert full_transcript(cues, is_automatic=True) == "00:00 今天我們來談 談壓縮"


def test_manual_captions_keep_every_line():
    cues = (
        Cue(start=0.0, end=2.0, text="好的我們開始吧"),
        Cue(start=2.0, end=4.0, text="對"),
    )
    assert full_transcript(cues, is_automatic=False) == "00:00 好的我們開始吧 對"
