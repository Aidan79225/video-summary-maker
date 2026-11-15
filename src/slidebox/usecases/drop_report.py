"""歸屬不可靠而失敗時，記在進度裡的診斷：被丟掉的重點，每一段到底是怎麼回事（純文字）。

失敗訊息只說「丟掉 135 個（有問題 25、查不出來 61…）」，看不出是模型編造、改了幾個字、接了兩輪、
抄的是其他聲音，還是檢查本身太嚴。這份文字補上：每一個窗的計數與原因碼、quote 對不上的原因分類、
不能改用原話（或不能直接顯示）的原因，以及幾個例子（模型寫的、檢查自己說的原因、逐字稿真正的那一輪並排）。

只讀、不判斷：沒有任何地方依這裡的結果而定，也不改變哪些點被留下或丟掉。分類用的門檻（見 quote_misses）
都是暫定值，這份診斷就是要拿來校正它們的。這個模組只該由 BuildDeckUseCase 呼叫，不屬於會影響摘要結果
的 RULE_MODULES（它不決定任何一個點的去留；quote_misses 也一樣）。
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
    QUOTE_ONLY_ADVICE,
    QUOTE_ONLY_REFUSED,
    RESCUE_REFUSED,
    UNSUPPORTED,
    DroppedPoint,
    Window,
    citable_ids,
)
from .diarized_pipeline import WindowOutcome
from .quote_misses import (
    MISS_ABSENT,
    MISS_ACROSS_TURNS,
    MISS_LOCATED,
    MISS_NEAR_VERBATIM,
    MISS_OTHER_VOICE,
    MISS_PARTIAL,
    MISS_REORDERED,
    QuoteMiss,
    explain_quote_miss,
)
from .quotes import (
    QUOTE_AMBIGUOUS,
    QUOTE_EMPTY,
    QUOTE_SHORT_FRAGMENTS,
    QUOTE_TOO_SHORT,
    locate_quote,
    squash_positions,
)
from .speakers import squash

MAX_CHARS = 8000                   # 整份診斷最多幾個字：先省略例子，再不夠才硬切
MAX_WINDOW_LINES = 40              # 最多列幾個窗；其餘只說有幾個沒列
MAX_EXAMPLES = 8
MAX_EXAMPLES_PER_GROUP = 2
MAX_SHOWN_TURNS = 3                # 好幾輪都有同一段話時，最多顯示幾輪
MAX_MENTIONED_TURNS = 2            # 檢查的原因裡提到、又還沒顯示過的輪，最多顯示幾輪
QUOTE_CHARS = 60                   # 一個 quote、一句重點最多顯示幾個字
REASON_CHARS = 120                 # 檢查自己說的原因最多顯示幾個字
EXCERPT_CHARS = 70                 # 逐字稿那一輪最多顯示幾個字（太長就取最像 quote 的那一段）
UNSEEN_NOTE_SHARE = 0.2            # quote 裡有這麼多比例的字是這一窗逐字稿從沒出現過的，就特別點出來（暫定值）
TRUNCATED_NOTE = "（診斷太長，後面省略）"
UNKNOWN = "unknown"                # 引用的輪不在這個窗裡，沒辦法分類

_HEADER = "診斷：被丟掉的重點（T 是逐字稿的輪編號，S 是可以引用的說話者，X 是不能引用的聲音）"
_EXAMPLES_HEADER = f"例子（模型寫的和逐字稿對照；多的原因先給，每種原因先各給一個，最多 {MAX_EXAMPLES} 個）："

# quote 對不上的原因：說明文字，以及同樣多時的先後順序
_KIND_TEXT = {
    MISS_OTHER_VOICE: "一字不差，但出自其他聲音或無法判斷的輪（不能引用）",
    MISS_ACROSS_TURNS: "接了相鄰的兩、三輪才湊得齊",
    MISS_REORDERED: "每一片都在同一輪裡，但順序和 quote 不同",
    MISS_NEAR_VERBATIM: "照抄得差不多，只差幾個字（同音字、數字寫法、少數簡繁體字）",
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
_FOUND_KINDS = frozenset({MISS_LOCATED, MISS_OTHER_VOICE, MISS_ACROSS_TURNS, MISS_REORDERED, QUOTE_AMBIGUOUS})
_CLOSEST_KINDS = frozenset({MISS_NEAR_VERBATIM, MISS_PARTIAL})

_QUOTED_WORD_RE = re.compile(r"「[^」]*」")
_TURN_NUMBER_RE = re.compile(r"T\d+")


@dataclass(frozen=True)
class _Drop:
    """一個被丟掉的點，加上它在哪個窗；bad_quote 才有「為什麼對不上」（kind、miss），其餘的有 quote 實際所在的那一輪。"""
    number: int                    # 第幾個窗（從 1 算）
    window: Window
    point: DroppedPoint
    kind: str = ""                 # bad_quote 才有：_KIND_TEXT 的分類
    miss: QuoteMiss | None = None
    where: Turn | None = None      # 不是 bad_quote 才有：quote 真正所在的那一輪（檢查是對著它做的，不一定是模型填的）


def drop_diagnostics(outcomes: Sequence[WindowOutcome], speakers: Sequence[SpeakerInfo]) -> str:
    """每個窗的計數與原因碼、合計、quote 對不上的原因、不能改用原話的原因、例子，一段多行文字。

    太長時先省略例子（最後的先省略），再不夠才把整份硬切並註明；每一段的計數與原因統計不會因為例子而少。
    """
    outcomes = tuple(outcomes)
    citable = citable_ids(speakers)
    drops = _collect(outcomes, citable)
    groups = _groups(drops)
    head = [_HEADER, *_window_lines(outcomes), _totals_line(outcomes)]
    middle = [*_quote_breakdown(drops), *_refusal_breakdown(drops)]
    examples = [(_label(drop), _example_lines(number, drop, citable))
                for number, drop in enumerate(_pick_examples(groups), 1)]
    return _fit(head, middle, examples, [(_label(items[0]), len(items)) for items in groups])


# --- 蒐集 ---


def _collect(outcomes: Sequence[WindowOutcome], citable: frozenset[str]) -> list[_Drop]:
    drops: list[_Drop] = []
    for number, outcome in enumerate(outcomes, 1):
        window = outcome.allocation.window
        by_index = {t.index: t for t in window.turns}
        for point in outcome.dropped:
            cited = by_index.get(point.turn) if isinstance(point.turn, int) else None
            if point.code != BAD_QUOTE:
                where = locate_quote(point.quote, cited, window.turns, citable).turn if cited is not None else None
                drops.append(_Drop(number, window, point, where=where))
                continue
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
    if outcome.attempts > 1 and outcome.adopted:
        asked += f"，採用第 {outcome.adopted} 次"          # 下面的數字與例子都是採用的那一次的
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


def _normalized(reason: str) -> str:
    """引號裡的字與輪編號換成通用的寫法，同一種原因才會歸在一起。"""
    return _TURN_NUMBER_RE.sub("T#", _QUOTED_WORD_RE.sub("「…」", reason))


def _refusal_reason(message: str) -> str:
    """unsupported 的原因結尾是「（也不能改用原話：為什麼不行）」；取出「為什麼不行」。"""
    _, found, rest = message.partition(RESCUE_REFUSED)
    return rest.removesuffix("）") if found else message


def _quote_only_reason(message: str) -> str:
    """安全模式 quote 不能直接顯示的原因是「quote 不能直接當重點顯示：為什麼不行；請抄…」；取出「為什麼不行」。"""
    return message.removeprefix(QUOTE_ONLY_REFUSED).removesuffix(QUOTE_ONLY_ADVICE)


def _reason_section(name: str, label: str, reasons: Sequence[str]) -> list[str]:
    if not reasons:
        return []
    counted = Counter(_normalized(reason) for reason in reasons)
    lines = [f"{name}（{label} 共 {len(reasons)} 個）："]
    lines.extend(f"  {count} 個：{reason}" for reason, count in sorted(counted.items(), key=lambda item: (-item[1], item[0])))
    return lines


def _refusal_breakdown(drops: Sequence[_Drop]) -> list[str]:
    refused = [_refusal_reason(drop.point.message) for drop in drops if drop.point.code == UNSUPPORTED]
    unshown = [_quote_only_reason(drop.point.message)
               for drop in drops if drop.point.code == BAD_QUOTE and drop.kind == MISS_LOCATED]
    return [*_reason_section("不能改用原話的原因", UNSUPPORTED, refused),
            *_reason_section("quote 不能直接當重點顯示的原因", f"{BAD_QUOTE}／{MISS_LOCATED}", unshown)]


# --- 例子 ---


def _label(drop: _Drop) -> str:
    return f"{drop.point.code}／{drop.kind}" if drop.kind else drop.point.code


def _groups(drops: Sequence[_Drop]) -> list[list[_Drop]]:
    """每一種原因（bad_quote 再依對不上的原因分）一組；大的在前（一樣大的依出現順序）。"""
    groups: dict[tuple[str, str], list[_Drop]] = {}
    for drop in drops:
        groups.setdefault((drop.point.code, drop.kind), []).append(drop)
    return sorted(groups.values(), key=len, reverse=True)           # 穩定排序：一樣大的依出現順序


def _pick_examples(groups: Sequence[Sequence[_Drop]]) -> list[_Drop]:
    """每一組先各給一個、再各給第二個，最多 MAX_EXAMPLES 個；大的在前。同一組的兩個例子盡量來自不同的窗。"""
    picks = [_spread(items) for items in groups]
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


def _turn_line(label: str, turn: Turn, citable: frozenset[str], quote: str, note: str = "") -> str:
    who = turn.speaker if turn.speaker in citable else OPAQUE_TAG
    return f"     {label} T{turn.index} {who}{note}：「{_excerpt(turn.text, quote)}」"


def _mentioned_turns(message: str, drop: _Drop, shown: set[int]) -> list[Turn]:
    """檢查自己說的原因裡提到、這個窗裡有、又還沒顯示過的輪（例如「內容出自別輪發言（T1）」的 T1）。"""
    by_index = {t.index: t for t in drop.window.turns}
    found = dict.fromkeys(int(number) for number in re.findall(r"T(\d+)", message))
    return [by_index[index] for index in found if index in by_index and index not in shown][:MAX_MENTIONED_TURNS]


def _transcript_lines(drop: _Drop, citable: frozenset[str]) -> list[str]:
    quote = drop.point.quote
    miss = drop.miss
    if miss is not None and miss.kind in _FOUND_KINDS:
        lines = [_turn_line("逐字稿", turn, citable, quote) for turn in miss.near[:MAX_SHOWN_TURNS]]
        if len(miss.near) > MAX_SHOWN_TURNS:
            lines.append(f"     （另外還有 {len(miss.near) - MAX_SHOWN_TURNS} 輪也有這段話）")
        return lines
    if miss is not None and miss.kind in _CLOSEST_KINDS and miss.near:
        return [_turn_line("逐字稿", miss.near[0], citable, quote, f"（相同約 {miss.ratio:.0%}）")]
    turn = drop.point.turn
    cited = next((t for t in drop.window.turns if t.index == turn), None) if isinstance(turn, int) else None
    shown = {t.index for t in (cited, drop.where) if t is not None}
    lines = []
    if drop.where is not None and (cited is None or drop.where.index != cited.index):
        note = f"（模型填的是 T{turn}）" if turn is not None else ""            # 模型把問題的編號填在答案的 quote 上
        lines.append(_turn_line("quote 所在的", drop.where, citable, quote, note))
    elif cited is not None:
        lines.append(_turn_line("它引用的", cited, citable, quote))
    lines.extend(_turn_line("原因裡提到的", t, citable, quote) for t in _mentioned_turns(drop.point.message, drop, shown))
    return lines


def _reason_line(drop: _Drop) -> list[str]:
    """檢查自己說的原因。bad_quote 的原因已經分類、統計過了，不重複（安全模式找得到卻不能顯示的那種例外：
    它的原因只在這裡）。"""
    if drop.point.code != BAD_QUOTE:
        return [f"     原因：{_clip(drop.point.message, REASON_CHARS)}"]
    if drop.kind == MISS_LOCATED:
        return [f"     原因：{_clip(_quote_only_reason(drop.point.message), REASON_CHARS)}"]
    return []


def _example_lines(number: int, drop: _Drop, citable: frozenset[str]) -> list[str]:
    point = drop.point
    turn = f"T{point.turn}" if point.turn is not None else "T?"
    lines = [f"  {number}. [第 {drop.number} 段 {turn} {_label(drop)}]"]
    lines.append("     模型寫的重點：" + (f"「{_clip(point.text)}」" if point.text.strip() else "（空的）"))
    lines.append("     模型的 quote：" + (f"「{_clip(point.quote)}」" if point.quote.strip() else "（空的）"))
    lines.extend(_reason_line(drop))
    lines.extend(_transcript_lines(drop, citable))
    if drop.miss is not None and drop.miss.unseen_share >= UNSEEN_NOTE_SHARE:
        lines.append("     逐字稿從沒出現過的字：" + " ".join(drop.miss.unseen[:12]))
    return lines


# --- 長度 ---


def _fit(
    head: Sequence[str], middle: Sequence[str], examples: Sequence[tuple[str, Sequence[str]]],
    groups: Sequence[tuple[str, int]],
) -> str:
    """examples 是（原因的標籤, 那個例子的幾行）；groups 是所有的原因（標籤, 個數）。例子太多放不下就從最後一個
    開始省略，省略之後沒有例子的原因要點名。"""
    shown = list(examples)
    while True:
        lines = [*head, *middle]
        if shown:
            lines.append(_EXAMPLES_HEADER)
            lines.extend(line for _, block in shown for line in block)
            named = {label for label, _ in shown}
            left_out = [(label, count) for label, count in groups if label not in named]
            if left_out:
                lines.append("  沒有附例子的原因：" + "、".join(f"{label} {count}" for label, count in left_out))
        text = "\n".join(lines)
        if len(text) <= MAX_CHARS or not shown:
            break
        shown.pop()
    if len(text) > MAX_CHARS:
        text = text[:max(0, MAX_CHARS - len(TRUNCATED_NOTE) - 1)].rstrip() + "\n" + TRUNCATED_NOTE
    return text
