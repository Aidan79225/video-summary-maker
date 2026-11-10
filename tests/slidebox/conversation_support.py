"""摘要檢查測試共用的小工具：三人對談的 Turn、一定通過的兩個重點、把重點放進一頁檢查的 run。"""
from __future__ import annotations

from slidebox.domain.entities import RawPoint, RawSlide
from slidebox.usecases.conversation import chunk_turns, validate_points

from .speaker_fixtures import dialogue_turns, make_speakers, make_turns

TURNS, SPEAKERS = dialogue_turns()
WHOLE = chunk_turns(TURNS, SPEAKERS)[0]
TWO = make_speakers("participant", "participant")


def at(prefix: str) -> int:
    """對談裡以 prefix 開頭的那一段的編號。"""
    return next(t.index for t in TURNS if t.text.startswith(prefix))


GOOD = (
    RawPoint(at("今天我們"), "詢問目前補助政策的看法"),
    RawPoint(at("現金補助"), "現金補助有幫助，但托育資源更重要"),
)


def run(*points, turns=TURNS, speakers=SPEAKERS, window=None, title="補助與托育", good=GOOD):
    """把 points 和兩個一定通過的重點（good）放在同一頁；回傳（多出來的點留下哪些, 整個結果）。"""
    window = window or chunk_turns(turns, speakers)[0]
    result = validate_points([RawSlide(title, (*good, *points))], turns, speakers, window)
    skip = {p.text for p in good}
    extra = [p for s in result.slides for p in s.points if p.text not in skip]
    return extra, result


def with_anchors(*lines):
    """自訂的小對談後面接兩段毫不相干的話，讓 run 放的那一頁一定留得住；回傳（turns, good）。"""
    n = len(lines)
    turns = make_turns(*lines, ("S1", "山水花草鳥魚蟲獸石海。"), ("S2", "雨雪雲風雷電星月光影。"))
    return turns, (RawPoint(n, "說明山水花草鳥魚蟲獸"), RawPoint(n + 1, "說明雨雪雲風雷電星月"))


def problems_of(result) -> str:
    return "\n".join(result.problems)
