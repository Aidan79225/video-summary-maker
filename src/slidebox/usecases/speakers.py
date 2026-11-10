"""把 diarization 的時間區段安全地套到逐字稿 cue。"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from ..domain.entities import Cue, SpeakerTurn

UNKNOWN = "未辨識"
OVERLAP = "多位說話者"


def attach_speakers(
    cues: Sequence[Cue], turns: Sequence[SpeakerTurn],
) -> tuple[tuple[Cue, ...], tuple[str, ...], int]:
    """依時間重疊標註 cue，回傳 (新 cues、實際 speaker 清單、不確定句數)。

    沒有 word-level timestamp 時，跨越換人或同時說話的整句不能安全切開；
    這時保留原文並明確標成多位，而不是把整句錯分給其中一人。
    """
    ordered = tuple(dict.fromkeys(turn.speaker for turn in turns if turn.speaker))
    result: list[Cue] = []
    uncertain = 0
    for cue in cues:
        start, end = float(cue.start), max(float(cue.end), float(cue.start))
        length = end - start
        overlap_by_speaker: dict[str, float] = {}
        for turn in turns:
            overlap = max(0.0, min(end, turn.end) - max(start, turn.start))
            if overlap:
                overlap_by_speaker[turn.speaker] = overlap_by_speaker.get(turn.speaker, 0.0) + overlap
        ranked = sorted(overlap_by_speaker.items(), key=lambda pair: pair[1], reverse=True)
        label = UNKNOWN
        if ranked:
            best_name, best = ranked[0]
            second = ranked[1][1] if len(ranked) > 1 else 0.0
            # 短 cue 的絕對門檻避免一點點邊界誤差造成錯分；長 cue 則要求
            # 明顯多數，否則把跨人句子照實標示。
            clear = best >= max(0.35, length * 0.55) and best - second >= max(0.2, length * 0.15)
            if clear or (len(ranked) == 1 and best >= max(0.35, length * 0.55)):
                label = best_name
            elif len(ranked) > 1:
                label = OVERLAP
        if label in (UNKNOWN, OVERLAP):
            uncertain += 1
        result.append(replace(cue, speaker=label))
    return tuple(result), ordered, uncertain
