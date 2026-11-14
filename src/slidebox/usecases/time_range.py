"""影片區間（add_video --start/--end）：夾住、回報，以及把區間內量到的時間加回原影片的時間軸。

只靠純函式：切音訊的是 AudioCutter（infrastructure），這裡不碰檔案。

時間軸的規矩（設計文件 #32）：聲音區分與語音辨識吃的是切出來的檔案，量到的時間從檔案開頭算起；
發佈出去的每一個時間（重點、頁、說話者、逐字稿、`&t=` 連結、截圖）都是**原影片的**秒數，
所以在存檔與對齊之前就把起點加回去。
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import replace

from ..domain.entities import MIN_RANGE_SECONDS, DiarizerOutput, SpeakerTurn, TimeRange, Word
from ..domain.errors import VideoRangeInvalid
from .speakers import clock

# 加完起點之後的時間留幾位小數：600 + 1.23 在浮點數裡是 601.2299999999999，
# 不該把這種尾巴存進存檔、發佈出去
_DECIMALS = 3


def normalize_range(requested: TimeRange | None) -> TimeRange | None:
    """沒給、或只是「從頭到尾」的區間就是沒有區間（None）。"""
    if requested is None or (requested.start <= 0 and requested.end is None):
        return None
    return requested


def effective_range(requested: TimeRange | None, duration: float) -> TimeRange | None:
    """影片的實際長度（秒）已知之後，這次真正要切的區間；整支影片就是 None。

    終點超過影片長度就夾到影片結尾、沒給終點就是影片結尾。起點已經在結尾之後、或夾完比
    MIN_RANGE_SECONDS 還短，raise VideoRangeInvalid（訊息給操作者看）。長度不明（0 或不是有限的數字）時
    沒辦法夾，也沒辦法檢查起點，原樣回傳：切出來的音訊太短由切割器判斷。
    """
    wanted = normalize_range(requested)
    if wanted is None:
        return None
    if not (math.isfinite(duration) and duration > 0):
        return wanted
    if wanted.start >= duration:
        raise VideoRangeInvalid(
            f"起點（{clock(wanted.start)}）已經超過影片長度（{clock(duration)}），沒有東西可以處理；請確認 --start")
    end = duration if wanted.end is None else min(wanted.end, duration)
    if wanted.start <= 0 and end >= duration:
        return None
    if end - wanted.start < MIN_RANGE_SECONDS:
        raise VideoRangeInvalid(
            f"區間（{clock(wanted.start)}–{clock(end)}）太短：影片只有 {clock(duration)}，"
            f"夾在影片長度內之後不到 {MIN_RANGE_SECONDS:g} 秒；請確認 --start 與 --end")
    return TimeRange(wanted.start, end)


def reported_range(requested: TimeRange | None, duration: float, words: Sequence[Word]) -> TimeRange | None:
    """寫進成品的實際區間。新做的與接著做的都走這一條，所以兩邊回報的一定一樣。

    長度不明又沒給終點時，終點用最後一個字的結束時間（總比留空好：Pi 要求兩個鍵一起出現）。
    """
    effective = effective_range(requested, duration)
    if effective is None or effective.end is not None:
        return effective
    last = max((w.end for w in words if math.isfinite(w.end)), default=effective.start)
    return TimeRange(effective.start, max(last, effective.start + MIN_RANGE_SECONDS))


def shift_turns(output: DiarizerOutput, offset: float) -> DiarizerOutput:
    """每個說話者區段的時間加上 offset（區間的起點）。overlap_seconds 是長度，不動。"""
    if not offset:
        return output
    return DiarizerOutput(
        tuple(SpeakerTurn(_moved(t.start, offset), _moved(t.end, offset), t.speaker) for t in output.turns),
        output.overlap_seconds)


def shift_words(words: Sequence[Word], offset: float) -> tuple[Word, ...]:
    """每個字的時間加上 offset。不是有限數字的時間維持不是有限數字：對齊會因此不信任那個字。"""
    if not offset:
        return tuple(words)
    return tuple(replace(w, start=_moved(w.start, offset), end=_moved(w.end, offset)) for w in words)


def _moved(seconds: float, offset: float) -> float:
    return round(seconds + offset, _DECIMALS)
