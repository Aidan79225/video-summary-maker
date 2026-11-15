"""歸屬不可靠而失敗時，記在進度裡的診斷：被丟掉的重點，每一段到底是怎麼回事（純文字）。

失敗訊息只說「丟掉 135 個（有問題 25、查不出來 61…）」，看不出是模型編造、改了幾個字、接了兩輪、
抄的是其他聲音，還是檢查本身太嚴。這份文字補上：每一個窗的計數與原因碼、quote 對不上的原因分類、
不能改用原話的原因，以及幾個例子（模型寫的 quote 和逐字稿真正的那一輪並排）。

只讀、不判斷：沒有任何地方依這裡的結果而定，也不改變哪些點被留下或丟掉。分類用的門檻（見 quotes）
都是暫定值，這份診斷就是要拿來校正它們的。這個模組只該由 BuildDeckUseCase 呼叫，不屬於會影響摘要結果
的 RULE_MODULES（它不決定任何一個點的去留）。
"""
from __future__ import annotations

import difflib
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from ..domain.entities import SpeakerInfo, Turn
from .conversation import (
    BAD_QUOTE,
    OPAQUE_TAG,
    RESCUE_REFUSED,
    UNSUPPORTED,
    DroppedPoint,
    Window,
    citable_ids,
)
from .diarized_pipeline import WindowOutcome
from .quotes import (
    MISS_ABSENT,
    MISS_ACROSS_TURNS,
    MISS_LOCATED,
    MISS_NEAR_VERBATIM,
    MISS_OTHER_VOICE,
    MISS_PARTIAL,
    MISS_REORDERED,
    QUOTE_AMBIGUOUS,
    QUOTE_EMPTY,
    QUOTE_SHORT_FRAGMENTS,
    QUOTE_TOO_SHORT,
    QuoteMiss,
    explain_quote_miss,
    squash_positions,
)
from .speakers import squash

MAX_CHARS = 8000                   # 整份診斷最多幾個字：先省略例子，再不夠才硬切
MAX_WINDOW_LINES = 40              # 最多列幾個窗；其餘只說有幾個沒列
MAX_EXAMPLES = 8
MAX_EXAMPLES_PER_GROUP = 2
QUOTE_CHARS = 60                   # 一個 quote、一句重點最多顯示幾個字
EXCERPT_CHARS = 70                 # 逐字稿那一輪最多顯示幾個字（太長就取最像 quote 的那一段）
UNSEEN_NOTE_SHARE = 0.2            # quote 裡有這麼多比例的字是這一窗逐字稿從沒出現過的，就特別點出來（暫定值）
TRUNCATED_NOTE = "（診斷太長，後面省略）"
UNKNOWN = "unknown"                # 引用的輪不在這個窗裡，沒辦法分類

_HEADER = "診斷：被丟掉的重點（T 是逐字稿的輪編號，S 是可以引用的說話者，X 是不能引用的聲音）"
_EXAMPLES_HEADER = "例子（模型寫的和逐字稿對照；每一種原因先各給一個）："

# quote 對不上的原因：說明文字，以及同樣多時的先後順序
_KIND_TEXT = {
    MISS_OTHER_VOICE: "一字不差，但出自其他聲音或無法判斷的輪（不能引用）",
    MISS_ACROSS_TURNS: "接了相鄰的兩、三輪才湊得齊",
    MISS_REORDERED: "每一片都在同一輪裡，但順序和 quote 不同",
    MISS_NEAR_VERBATIM: "照抄得差不多，只差幾個字（同音字、數字寫法、簡繁體）",
    MISS_PARTIAL: "只有一部分對得上（跳過子句、改寫）",
    MISS_ABSENT: "幾乎不在這一窗的逐字稿裡（編造或整段改寫）",
    QUOTE_EMPTY: "quote 是空的",
    QUOTE_TOO_SHORT: "quote 太短，認不出是哪一輪",
    QUOTE_SHORT_FRAGMENTS: "quote 由太短的片段拼成，認不出是哪一輪",
    QUOTE_AMBIGUOUS: "好幾輪都有這段話，無法確定是哪一輪",
    MISS_LOCATED: "quote 本身找得到，但不能直接當重點顯示（只顯示原話的模式）",
    UNKNOWN: "引用的輪不在這一窗裡，無法分類",
}
_KIND_RANK = {kind: rank for rank, kind in enumerate(_KIND_TEXT)}

# 例子裡「逐字稿」那一行要顯示哪一輪：quote 其實在的那幾輪，或最接近的那一輪；其餘顯示模型引用的那一輪
_FOUND_KINDS = frozenset({MISS_LOCATED, MISS_OTHER_VOICE, MISS_ACROSS_TURNS, MISS_REORDERED})
_CLOSEST_KINDS = frozenset({MISS_NEAR_VERBATIM, MISS_PARTIAL})

_QUOTED_WORD_RE = re.compile(r"「[^」]*」")
_TURN_NUMBER_RE = re.compile(r"T\d+")


@dataclass(frozen=True)
class _Drop:
    """一個被丟掉的點，加上它在哪個窗、以及（bad_quote 才有）為什麼對不上。"""
    number: int                    # 第幾個窗（從 1 算）
    window: Window
    point: DroppedPoint
    kind: str = ""                 # bad_quote 才有：_KIND_TEXT 的分類
    miss: QuoteMiss | None = None


def drop_diagnostics(outcomes: Sequence[WindowOutcome], speakers: Sequence[SpeakerInfo]) -> str:
    """每個窗的計數與原因碼、合計、quote 對不上的原因、不能改用原話的原因、例子，一段多行文字。

    太長時先省略例子（最後的先省略），再不夠才把整份硬切並註明；每一段的計數與原因統計不會因為例子而少。
    """
    outcomes = tuple(outcomes)
    citable = citable_ids(speakers)
    drops = _collect(outcomes, citable)
    head = [_HEADER, *_window_lines(outcomes), _totals_line(outcomes)]
    middle = [*_quote_breakdown(drops), *_refusal_breakdown(drops)]
    examples = [_example_lines(number, drop, citable) for number, drop in enumerate(_pick_examples(drops), 1)]
    return _fit(head, middle, examples)


# --- 蒐集 ---


def _collect(outcomes: Sequence[WindowOutcome], citable: frozenset[str]) -> list[_Drop]:
    drops: list[_Drop] = []
    for number, outcome in enumerate(outcomes, 1):
        window = outcome.allocation.window
        by_index = {t.index: t for t in window.turns}
        for point in outcome.dropped:
            if point.code != BAD_QUOTE:
                drops.append(_Drop(number, window, point))
                continue
            cited = by_index.get(point.turn) if isinstance(point.turn, int) else None
            if cited is None:
                drops.append(_Drop(number, window, point, UNKNOWN))
                continue
            miss = explain_quote_miss(point.quote, cited, window.turns, citable)
            drops.append(_Drop(number, window, point, miss.kind, miss))
    return drops


# --- 每個窗與合計 ---


def _counts(proposed: int, kept: int) -> str:
    return f"提出 {proposed}、留下 {kept}、丟掉 {proposed - kept}"


def _code_list(pairs: Sequence[tuple[str, int]]) -> str:
    return "、".join(f"{code} {count}" for code, count in pairs)


def _window_line(number: int, total: int, outcome: WindowOutcome) -> str:
    window = outcome.allocation.window
    where = f"第 {number}/{total} 段"
    span = f"T{window.first_turn}–T{window.last_turn}"
    if outcome.skipped:
        return f"  {where}（{span}）：沒有主要發言者可以引用，沒有送給模型"
    asked = f"，問了 {outcome.attempts} 次" if outcome.attempts else ""
    line = f"  {where}（{span}{asked}）：{_counts(outcome.proposed, outcome.kept)}"
    codes = _code_list(outcome.dropped_by_code)
    return f"{line}：{codes}" if codes else line


def _window_lines(outcomes: Sequence[WindowOutcome]) -> list[str]:
    total = len(outcomes)
    lines = [_window_line(number, total, outcome) for number, outcome in enumerate(outcomes[:MAX_WINDOW_LINES], 1)]
    if total > MAX_WINDOW_LINES:
        lines.append(f"  （另有 {total - MAX_WINDOW_LINES} 段沒有列出）")
    return lines


def _totals_line(outcomes: Sequence[WindowOutcome]) -> str:
    codes: Counter[str] = Counter()
    for outcome in outcomes:
        codes.update(dict(outcome.dropped_by_code))
    pairs = sorted(codes.items(), key=lambda item: (-item[1], item[0]))
    line = "合計：" + _counts(sum(o.proposed for o in outcomes), sum(o.kept for o in outcomes))
    return f"{line}：{_code_list(pairs)}" if pairs else line


# --- 原因的統計 ---


def _quote_breakdown(drops: Sequence[_Drop]) -> list[str]:
    bad = [drop for drop in drops if drop.point.code == BAD_QUOTE]
    if not bad:
        return []
    kinds = Counter(drop.kind for drop in bad)
    ordered = sorted(kinds.items(), key=lambda item: (-item[1], _KIND_RANK.get(item[0], len(_KIND_RANK)), item[0]))
    lines = [f"quote 對不上的原因（{BAD_QUOTE} 共 {len(bad)} 個）："]
    lines.extend(f"  {kind} {count}：{_KIND_TEXT.get(kind, kind)}" for kind, count in ordered)
    unseen = sum(1 for drop in bad if drop.miss is not None and drop.miss.unseen_share >= UNSEEN_NOTE_SHARE)
    if unseen:
        lines.append(f"  其中 {unseen} 個的 quote 有 {UNSEEN_NOTE_SHARE:.0%} 以上的字"
                     f"是這一窗逐字稿從沒出現過的（簡體字或編造的詞）")
    return lines


def _refusal_reason(message: str) -> str:
    """unsupported 的原因結尾是「（也不能改用原話：為什麼不行）」；取出「為什麼不行」，
    把引號裡的字與輪編號換成通用的寫法，同一種原因才會歸在一起。"""
    _, found, rest = message.partition(RESCUE_REFUSED)
    reason = rest.removesuffix("）") if found else message
    return _TURN_NUMBER_RE.sub("T#", _QUOTED_WORD_RE.sub("「…」", reason))


def _refusal_breakdown(drops: Sequence[_Drop]) -> list[str]:
    refused = [drop for drop in drops if drop.point.code == UNSUPPORTED]
    if not refused:
        return []
    reasons = Counter(_refusal_reason(drop.point.message) for drop in refused)
    lines = [f"不能改用原話的原因（{UNSUPPORTED} 共 {len(refused)} 個）："]
    lines.extend(f"  {count} 個：{reason}"
                 for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0])))
    return lines


# --- 例子 ---


def _pick_examples(drops: Sequence[_Drop]) -> list[_Drop]:
    """每一種原因（bad_quote 再依對不上的原因分）先各給一個、再各給第二個，最多 MAX_EXAMPLES 個；
    大的原因在前。同一種原因的兩個例子盡量來自不同的窗。"""
    groups: dict[tuple[str, str], list[_Drop]] = {}
    for drop in drops:
        groups.setdefault((drop.point.code, drop.kind), []).append(drop)
    picks = [_spread(items) for items in sorted(groups.values(), key=len, reverse=True)]    # 穩定排序：一樣大的依出現順序
    chosen: list[_Drop] = []
    for round_ in range(MAX_EXAMPLES_PER_GROUP):
        for items in picks:
            if round_ < len(items) and len(chosen) < MAX_EXAMPLES:
                chosen.append(items[round_])
    return chosen


def _spread(items: Sequence[_Drop]) -> list[_Drop]:
    first = items[0]
    others = [drop for drop in items[1:] if drop.number != first.number]
    return [first, *(others or list(items[1:]))[:MAX_EXAMPLES_PER_GROUP - 1]]


def _flat(text: str) -> str:
    return " ".join(text.split())


def _clip(text: str, limit: int = QUOTE_CHARS) -> str:
    flat = _flat(text)
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"


def _match_start(flat: str, quote: str) -> int:
    """quote 和這一輪最長的相同片段，在這一輪原文（flat）裡的位置；一個字都對不上就是 0。"""
    squashed, positions = squash_positions(flat)
    wanted = squash(quote)
    match = difflib.SequenceMatcher(None, squashed, wanted, autojunk=False).find_longest_match(
        0, len(squashed), 0, len(wanted))
    return positions[match.a] if match.size and match.a < len(positions) else 0


def _excerpt(turn_text: str, quote: str) -> str:
    """這一輪的原文；太長就取最像 quote 的那一段，頭尾被切掉的地方用 … 標出來。"""
    flat = _flat(turn_text)
    if len(flat) <= EXCERPT_CHARS:
        return flat
    start = max(0, _match_start(flat, quote) - EXCERPT_CHARS // 4)
    end = min(len(flat), start + EXCERPT_CHARS)
    start = max(0, end - EXCERPT_CHARS)
    return ("…" if start > 0 else "") + flat[start:end] + ("…" if end < len(flat) else "")


def _turn_line(label: str, turn: Turn, citable: frozenset[str], quote: str, ratio: float | None = None) -> str:
    who = turn.speaker if turn.speaker in citable else OPAQUE_TAG
    share = f"（相同約 {ratio:.0%}）" if ratio is not None else ""
    return f"     {label} T{turn.index} {who}{share}：「{_excerpt(turn.text, quote)}」"


def _transcript_lines(drop: _Drop, citable: frozenset[str]) -> list[str]:
    quote = drop.point.quote
    miss = drop.miss
    if miss is not None and miss.kind in _FOUND_KINDS:
        return [_turn_line("逐字稿", turn, citable, quote) for turn in miss.near]
    if miss is not None and miss.kind in _CLOSEST_KINDS and miss.near:
        return [_turn_line("逐字稿", miss.near[0], citable, quote, miss.ratio)]
    turn = drop.point.turn
    cited = next((t for t in drop.window.turns if t.index == turn), None) if isinstance(turn, int) else None
    return [_turn_line("它引用的", cited, citable, quote)] if cited is not None else []


def _example_lines(number: int, drop: _Drop, citable: frozenset[str]) -> list[str]:
    point = drop.point
    turn = f"T{point.turn}" if point.turn is not None else "T?"
    label = f"{point.code}／{drop.kind}" if drop.kind else point.code
    lines = [f"  {number}. [第 {drop.number} 段 {turn} {label}]"]
    if point.text:
        lines.append(f"     模型寫的重點：「{_clip(point.text)}」")
    lines.append("     模型的 quote：" + (f"「{_clip(point.quote)}」" if point.quote.strip() else "（空的）"))
    lines.extend(_transcript_lines(drop, citable))
    if drop.miss is not None and drop.miss.unseen_share >= UNSEEN_NOTE_SHARE:
        lines.append("     逐字稿從沒出現過的字：" + " ".join(drop.miss.unseen[:12]))
    return lines


# --- 長度 ---


def _fit(head: Sequence[str], middle: Sequence[str], examples: Sequence[Sequence[str]]) -> str:
    shown = list(examples)
    while True:
        lines = [*head, *middle]
        if shown:
            lines.append(_EXAMPLES_HEADER)
            lines.extend(line for block in shown for line in block)
        text = "\n".join(lines)
        if len(text) <= MAX_CHARS or not shown:
            break
        shown.pop()
    if len(text) > MAX_CHARS:
        text = text[:max(0, MAX_CHARS - len(TRUNCATED_NOTE) - 1)].rstrip() + "\n" + TRUNCATED_NOTE
    return text
