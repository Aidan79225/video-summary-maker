"""歸屬不可靠而失敗時，記在進度裡的診斷：被丟掉的重點，每一段到底是怎麼回事（純文字）。

失敗訊息只說「丟掉 135 個（有問題 25、查不出來 61…）」，看不出是模型編造、改了幾個字、接了兩輪、
抄的是其他聲音，還是檢查本身太嚴。這份文字補上：每一個窗的計數與原因碼、quote 對不上的原因分類、
不能改用原話（或不能直接顯示）的原因，以及幾個例子（模型寫的、檢查自己說的原因、逐字稿真正的那一輪並排）。

再下來是「再算一次」（見 drop_analysis）：跨輪的 quote 是誰和誰拼的、問句是哪個字眼觸發的、換一種做法（C 只取 quote 裡
逐字吻合的最長一段、D 把同一位說話者相鄰的輪併起來）被丟掉的 quote 有幾個會通過其餘的檢查——全部用真的檢查重跑，
只算、不判斷，是留給人決定要不要放寬規則的資料。每一項各自擋住自己的錯：算不出來的那一項只有自己那一行寫
「沒能算出來」，不拖累其他的。

只讀、不判斷：沒有任何地方依這裡的結果而定，也不改變哪些點被留下或丟掉。分類用的門檻（見 quote_misses、drop_analysis）
都是暫定值，這份診斷就是要拿來校正它們的。這個模組只該由 BuildDeckUseCase 呼叫，不屬於會影響摘要結果
的 RULE_MODULES（它不決定任何一個點的去留；quote_misses、drop_analysis 也一樣）。
"""
from __future__ import annotations

import difflib
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ..domain.entities import SpeakerInfo, Turn
from . import drop_analysis as analysis
from .conversation import (
    BAD_QUOTE,
    OPAQUE_TAG,
    QUESTION_STATED_AS_FACT,
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

MAX_CHARS = 12000                  # 整份診斷最多幾個字：先省略例子，再不夠才硬切
MAX_WINDOW_LINES = 40              # 最多列幾個窗；其餘只說有幾個沒列
MAX_EXAMPLES = 12
MAX_RERUNS = 150                   # 「假設的做法」最多重跑幾個點（每個都要跑一次完整的檢查）
MAX_GAP_LENGTHS = 10               # 同一位說話者被一整句其他聲音隔開時，最多列幾個「夾著幾個字」
MAX_EXAMPLES_PER_GROUP = 2
MAX_SHOWN_TURNS = 3                # 好幾輪都有同一段話時，最多顯示幾輪
MAX_MENTIONED_TURNS = 2            # 檢查的原因裡提到、又還沒顯示過的輪，最多顯示幾輪
QUOTE_CHARS = 60                   # 一個 quote、一句重點最多顯示幾個字
REASON_CHARS = 120                 # 檢查自己說的原因最多顯示幾個字
EXCERPT_CHARS = 70                 # 逐字稿那一輪最多顯示幾個字（太長就取最像 quote 的那一段）
UNSEEN_NOTE_SHARE = 0.2            # quote 裡有這麼多比例的字是這一窗逐字稿從沒出現過的，就特別點出來（暫定值）
TRUNCATED_NOTE = "（診斷太長，後面省略）"
END_LINE = "（診斷到此結束；貼回來的文字沒有這一行，就是被截掉了）"      # 一定是最後一行：沒有它就知道貼的時候少了後面
FAILED = "沒能算出來"              # 「再算一次」的某一項自己出了錯：只有那一項這樣寫
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

# 「再算一次」：各項的名字（出錯時記在 failures 裡）、標題、說明文字，以及同樣多時的先後順序
_WINDOWS, _SPLICE, _QUESTION, _C, _D = "windows", "splice", "question", "C", "D"
_SECTION = "再算一次（只是計算，沒有改變任何判斷）："
_SPLICE_TEXT = {
    analysis.SPLICE_SAME: "同一位說話者的相鄰輪",
    analysis.SPLICE_NOISE: "同一位說話者、中間夾著很短的其他聲音",
    analysis.SPLICE_INTERRUPTED: "同一位說話者、中間夾著一整句其他聲音的話",
    analysis.SPLICE_CROSS: "不同說話者的話",
    analysis.SPLICE_OTHER: "頭或尾是其他聲音",
}
_SPLICE_RANK = {pattern: rank for rank, pattern in enumerate(_SPLICE_TEXT)}
# 問句字眼是用什麼條件算數的；同樣多時越可能是檢查誤判的越前面（真的問號最不可疑）
_FORM_TEXT = {
    analysis.FORM_UNMATCHED: "這一輪沒有任何一句和重點共用字，整段有字眼就算",
    analysis.FORM_STRICT: "重點的數字剛被別人說過（不看距離）",
    analysis.FORM_NEAR: "很長又沒有標點的話，字眼附近有重點的內容",
    analysis.FORM_SHORT: "短子句裡有字眼",
    analysis.FORM_MARKED: "子句結尾有問號",
}
_FORM_RANK = {form: rank for rank, form in enumerate(_FORM_TEXT)}
_QUESTION_MARKS = frozenset("？?")

# 例子裡「逐字稿」那一行要顯示哪一輪：quote 其實在的那幾輪，或最接近的那一輪；其餘顯示模型引用的那一輪
_FOUND_KINDS = frozenset({MISS_LOCATED, MISS_OTHER_VOICE, MISS_ACROSS_TURNS, MISS_REORDERED, QUOTE_AMBIGUOUS})
_CLOSEST_KINDS = frozenset({MISS_NEAR_VERBATIM, MISS_PARTIAL})

_QUOTED_WORD_RE = re.compile(r"「[^」]*」")
_TURN_NUMBER_RE = re.compile(r"T\d+")


@dataclass(frozen=True)
class _WhatIf:
    """一個 quote 對不上的點換一種做法重跑的結果（見 drop_analysis）；None 是這個做法對這個點沒有東西可以算。"""
    c: analysis.Rerun | None = None
    d: analysis.Rerun | None = None
    chain: analysis.Chain | None = None          # D 通過時，併了哪幾輪


@dataclass(frozen=True)
class _Drop:
    """一個被丟掉的點，加上它在哪個窗；bad_quote 才有「為什麼對不上」（kind、miss），其餘的有 quote 實際所在的那一輪。

    再下面幾個是「再算一次」的結果（沒有就是預設值）：across_turns 有拼自哪幾輪、question_stated_as_fact 有觸發的字眼、
    quote 對不上的有換一種做法重跑的結果。
    """
    number: int                    # 第幾個窗（從 1 算）
    window: Window
    point: DroppedPoint
    kind: str = ""                 # bad_quote 才有：_KIND_TEXT 的分類
    miss: QuoteMiss | None = None
    where: Turn | None = None      # 不是 bad_quote 才有：quote 真正所在的那一輪（檢查是對著它做的，不一定是模型填的）
    splice: str = ""               # across_turns 才有：拼自哪幾輪（analysis.SPLICE_*）
    gap: int = 0                   # 其中頭尾之間夾著的其他聲音合計幾個字
    triggers: tuple[analysis.QuestionTrigger, ...] | None = None      # question_stated_as_fact 才有；None 是沒算出來
    whatif: _WhatIf | None = None  # 有重跑的才有


def drop_diagnostics(
    outcomes: Sequence[WindowOutcome], speakers: Sequence[SpeakerInfo], quote_only: bool = False,
) -> str:
    """每個窗的計數與原因碼、合計、quote 對不上的原因、不能改用原話的原因、再算一次、例子，一段多行文字。

    quote_only 是這一次跑的模式（安全模式只顯示原話）：「假設的做法」要照同一個模式重跑，才和真的檢查一樣。
    太長時先省略例子（最後的先省略），再不夠才把整份硬切並註明；每一段的計數與原因統計不會因為例子而少。
    最後一行一定是 END_LINE：有它才知道文字是完整的。
    """
    outcomes = tuple(outcomes)
    citable = citable_ids(speakers)
    failures: dict[str, str] = {}
    drops = _collect(outcomes, citable, quote_only, failures)
    groups = _groups(drops)
    head = [_HEADER, *_window_lines(outcomes, drops), _totals_line(outcomes)]
    middle = [*_quote_breakdown(drops), *_refusal_breakdown(drops), *_analysis_section(drops, failures)]
    examples = [(_label(drop), _example_lines(number, drop, citable))
                for number, drop in enumerate(_pick_examples(groups), 1)]
    return _fit(head, middle, examples, [(_label(items[0]), len(items)) for items in groups])


# --- 蒐集 ---


def _collect(
    outcomes: Sequence[WindowOutcome], citable: frozenset[str], quote_only: bool, failures: dict[str, str],
) -> list[_Drop]:
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
    return _analysed(drops, outcomes, citable, quote_only, failures)


# --- 再算一次：每個被丟掉的點多算幾樣（見 drop_analysis）---


def _guarded(failures: dict[str, str], name: str, compute: Callable[[], Any], default: Any = None) -> Any:
    """「再算一次」的一項：它自己出了錯，不能拖累診斷的其他部分（更不能蓋掉原本的失敗）。出錯就記下是哪一種錯，
    之後這一項不再算（每個點都會再錯一次，只是白白花時間）。"""
    if name in failures:
        return default
    try:
        return compute()
    except Exception as exc:  # noqa: BLE001 診斷壞了，只有這一項寫「沒能算出來」
        failures[name] = type(exc).__name__
        return default


def _failure(failures: dict[str, str], *names: str) -> str | None:
    return next((failures[name] for name in names if name in failures), None)


def _rerunnable(drop: _Drop) -> bool:
    """quote 對不上、而且引用的輪在這個窗裡的點才有東西可以重跑（找得到只是不能顯示的，換個做法也一樣）。"""
    return drop.point.code == BAD_QUOTE and drop.kind not in (UNKNOWN, MISS_LOCATED)


def _analysed(
    drops: Sequence[_Drop], outcomes: Sequence[WindowOutcome], citable: frozenset[str], quote_only: bool,
    failures: dict[str, str],
) -> list[_Drop]:
    needed = any(_rerunnable(drop) or drop.point.code == QUESTION_STATED_AS_FACT for drop in drops)
    contexts = (_guarded(failures, _WINDOWS, lambda: analysis.window_contexts(
        [outcome.allocation.window for outcome in outcomes], citable, quote_only)) if needed else None)
    budget = MAX_RERUNS
    out: list[_Drop] = []
    for drop in drops:
        ctx = contexts[drop.number - 1] if contexts else None
        extra: dict[str, Any] = {}
        if drop.kind == MISS_ACROSS_TURNS and drop.miss is not None and drop.miss.near:
            near = drop.miss.near
            found = _guarded(failures, _SPLICE, lambda: (analysis.splice_pattern(near, citable), analysis.gap_chars(near)))
            if found is not None:
                extra["splice"], extra["gap"] = found
        if drop.point.code == QUESTION_STATED_AS_FACT and ctx is not None and drop.where is not None:
            extra["triggers"] = _guarded(failures, _QUESTION, lambda: analysis.question_cause(ctx, drop.point, drop.where))
        if _rerunnable(drop) and budget > 0:
            budget -= 1
            extra["whatif"] = _whatif(drop, ctx, failures)
        out.append(replace(drop, **extra) if extra else drop)
    return out


def _whatif(drop: _Drop, ctx: analysis.WindowContext | None, failures: dict[str, str]) -> _WhatIf:
    if ctx is None:                      # 窗的資料沒準備好：每一項都算不出來，各行會寫出原因
        return _WhatIf()
    c_run = _guarded(failures, _C, lambda: analysis.try_longest_segment(ctx, drop.point))
    d_run = _guarded(failures, _D, lambda: analysis.try_joined_turns(ctx, drop.point))
    chain = None
    if d_run is not None and d_run.kept and d_run.turn is not None:
        chain = _guarded(failures, _D, lambda: analysis.chain_for(ctx.joined, d_run.turn))
    return _WhatIf(c_run, d_run, chain)


# --- 每個窗與合計 ---


def _counts(proposed: int, kept: int) -> str:
    return f"提出 {proposed}、留下 {kept}、丟掉 {proposed - kept}"


def _code_list(pairs: Sequence[tuple[str, int]], kinds: Sequence[tuple[str, int]] = ()) -> str:
    """原因碼與個數；bad_quote 後面可以接括號，說明 quote 為什麼對不上（kinds）。"""
    def one(code: str, count: int) -> str:
        return f"{code} {count}（{_code_list(kinds)}）" if code == BAD_QUOTE and kinds else f"{code} {count}"
    return "、".join(one(code, count) for code, count in pairs)


def _kind_counts(drops: Sequence[_Drop]) -> list[tuple[str, int]]:
    """bad_quote 對不上的原因與個數：多的在前，一樣多時依 _KIND_TEXT 的順序。"""
    kinds = Counter(drop.kind for drop in drops if drop.point.code == BAD_QUOTE)
    return sorted(kinds.items(), key=lambda item: (-item[1], _KIND_RANK.get(item[0], len(_KIND_RANK)), item[0]))


def _window_line(number: int, total: int, outcome: WindowOutcome, kinds: Sequence[tuple[str, int]] = ()) -> str:
    window = outcome.allocation.window
    where = f"第 {number}/{total} 段"
    span = f"T{window.first_turn}–T{window.last_turn}"
    if outcome.skipped:
        return f"  {where}（{span}）：沒有主要發言者可以引用，沒有送給模型"
    asked = f"，問了 {outcome.attempts} 次" if outcome.attempts else ""
    if outcome.attempts > 1 and outcome.adopted:
        asked += f"，採用第 {outcome.adopted} 次"          # 下面的數字與例子都是採用的那一次的
    line = f"  {where}（{span}{asked}）：{_counts(outcome.proposed, outcome.kept)}"
    codes = _code_list(outcome.dropped_by_code, kinds)
    return f"{line}：{codes}" if codes else line


def _window_lines(outcomes: Sequence[WindowOutcome], drops: Sequence[_Drop]) -> list[str]:
    total = len(outcomes)
    by_window: dict[int, list[_Drop]] = defaultdict(list)
    for drop in drops:
        by_window[drop.number].append(drop)
    lines = [_window_line(number, total, outcome, _kind_counts(by_window[number]))
             for number, outcome in enumerate(outcomes[:MAX_WINDOW_LINES], 1)]
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
    lines = [f"quote 對不上的原因（{BAD_QUOTE} 共 {len(bad)} 個）："]
    lines.extend(f"  {kind} {count}：{_KIND_TEXT.get(kind, kind)}" for kind, count in _kind_counts(bad))
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


# --- 再算一次的各行 ---


def _counted(pairs: Sequence[tuple[str, int]]) -> str:
    return "、".join(f"{name} {count}" for name, count in pairs)


def _analysis_section(drops: Sequence[_Drop], failures: dict[str, str]) -> list[str]:
    lines = [*_splice_lines(drops, failures), *_question_lines(drops, failures), *_whatif_lines(drops, failures)]
    return [_SECTION, *lines] if lines else []


def _gap_note(pattern: str, gaps: Sequence[int]) -> str:
    """夾在中間的其他聲音有幾個字：很短的只要最大的一個；整句的把每一個列出來（看門檻放在哪裡才合適）。"""
    if pattern == analysis.SPLICE_NOISE:
        return f"（夾著的聲音最多 {max(gaps)} 個字）"
    if pattern == analysis.SPLICE_INTERRUPTED:
        listed = "、".join(str(gap) for gap in sorted(gaps)[:MAX_GAP_LENGTHS])
        return f"（夾著的聲音 {listed}{'…' if len(gaps) > MAX_GAP_LENGTHS else ''} 個字）"
    return ""


def _splice_lines(drops: Sequence[_Drop], failures: dict[str, str]) -> list[str]:
    """跨輪的 quote 是同一位說話者被切開，還是不同的人的話拼在一起。"""
    across = [drop for drop in drops if drop.kind == MISS_ACROSS_TURNS]
    if not across:
        return []
    head = f"  跨輪的 quote（{MISS_ACROSS_TURNS} 共 {len(across)} 個）"
    failed = _failure(failures, _SPLICE)
    if failed:
        return [f"{head}：{FAILED}（{failed}）"]
    counts = Counter(drop.splice for drop in across if drop.splice)
    ordered = sorted(counts.items(), key=lambda item: (-item[1], _SPLICE_RANK.get(item[0], len(_SPLICE_RANK))))
    parts = [f"{_SPLICE_TEXT.get(pattern, pattern)} {count}{_gap_note(pattern, [d.gap for d in across if d.splice == pattern])}"
             for pattern, count in ordered]
    return [f"{head}拼自：{'、'.join(parts)}"] if parts else []


def _trigger_word(word: str) -> str:
    return "問號" if word in _QUESTION_MARKS else word


def _question_lines(drops: Sequence[_Drop], failures: dict[str, str]) -> list[str]:
    """問句被丟（question_stated_as_fact）：是哪些字眼、用什麼條件讓重點被當成問句。"""
    asked = [drop for drop in drops if drop.point.code == QUESTION_STATED_AS_FACT]
    if not asked:
        return []
    head = f"  問句被丟（{QUESTION_STATED_AS_FACT} 共 {len(asked)} 個）："
    failed = _failure(failures, _QUESTION, _WINDOWS)
    if failed:
        return [f"{head}{FAILED}（{failed}）"]
    words: Counter[str] = Counter()
    forms: Counter[str] = Counter()
    unexplained = 0
    for drop in asked:
        found = drop.triggers or ()
        if not found:
            unexplained += 1
            continue
        words.update(list(dict.fromkeys(_trigger_word(trigger.word) for trigger in found)))      # 同一個點的同一個字眼只算一次
        forms.update(list(dict.fromkeys(trigger.form for trigger in found)))
    parts = []
    if words:
        by_count = sorted(words.items(), key=lambda item: -item[1])                    # 穩定排序：一樣多時先出現的先列
        by_form = sorted(forms.items(), key=lambda item: (-item[1], _FORM_RANK.get(item[0], len(_FORM_RANK))))
        parts.append("觸發的字眼 " + _counted(by_count))
        parts.append("條件 " + _counted([(_FORM_TEXT.get(form, form), count) for form, count in by_form]))
    if unexplained:
        parts.append(f"{unexplained} 個沒找到觸發的字眼（這裡的算法和檢查對不上）")
    return [head + "；".join(parts)]


def _c_label() -> str:
    return (f"C 只取 quote 裡最長的一段逐字吻合（至少 {analysis.LONGEST_SEGMENT_MIN_CHARS} 個字、只有一輪有這麼長的一段、"
            f"那一輪是可以引用的說話者）")


def _d_label() -> str:
    return f"D 把同一位說話者相鄰的輪併成一輪（中間夾著不超過 {analysis.JOIN_GAP_CHARS} 個字的其他聲音不算）"


def _variant_line(label: str, runs: Sequence[tuple[_Drop, analysis.Rerun | None]], failed: str | None) -> str:
    """一種做法：有幾個能算（沒有東西可以算的不列入）、幾個會通過（原本是哪一種）、沒通過的卡在哪個原因碼。"""
    if failed:
        return f"    {label}：{FAILED}（{failed}）"
    usable = [(drop, run) for drop, run in runs if run is not None]
    text = f"能算 {len(usable)} 個"
    if usable:
        passed = [drop for drop, run in usable if run.kept]
        text += f"，會通過 {len(passed)} 個"
        if passed:
            text += f"（原本是 {_counted(_kind_counts(passed))}）"
        stuck = Counter(run.code for _, run in usable if not run.kept)
        if stuck:
            text += f"；沒通過 {sum(stuck.values())} 個（{_counted(sorted(stuck.items(), key=lambda item: (-item[1], item[0])))}）"
    return f"    {label}：{text}"


def _whatif_lines(drops: Sequence[_Drop], failures: dict[str, str]) -> list[str]:
    """被丟掉的 quote 對不上的點，換一種做法（C、D）用真的檢查重跑：幾個會通過、哪些卡在別的檢查。"""
    eligible = [drop for drop in drops if _rerunnable(drop)]
    if not eligible:
        return []
    ran = [drop for drop in eligible if drop.whatif is not None]
    cut = f"，只重跑前 {len(ran)} 個" if len(ran) < len(eligible) else ""
    lines = [f"  假設的做法（把 quote 對不上的 {len(eligible)} 個用真的檢查重跑，只算每個點自己的檢查："
             f"不含頁數與重複的規則，原本留下的點沒有重算{cut}）："]
    c_failed, d_failed = _failure(failures, _C, _WINDOWS), _failure(failures, _D, _WINDOWS)
    c_runs = [(drop, drop.whatif.c) for drop in ran]
    d_runs = [(drop, drop.whatif.d) for drop in ran]
    lines.append(_variant_line(_c_label(), c_runs, c_failed))
    lines.append(_variant_line(_d_label(), d_runs, d_failed))
    if not (c_failed or d_failed):
        by_c = {n for n, (_, run) in enumerate(c_runs) if run is not None and run.kept}
        by_d = {n for n, (_, run) in enumerate(d_runs) if run is not None and run.kept}
        lines.append(f"    兩種都會通過 {len(by_c & by_d)} 個；至少一種會通過 {len(by_c | by_d)} 個")
    lines.append("    （這裡的 C、D 沒有保護；正式的檢查已經用了有保護的版本，所以會通過的是「保護擋下來的」那些，不是新增的）")
    return lines


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


def _trigger_example(drop: _Drop) -> list[str]:
    """問句被丟：這個例子是哪些字眼、用什麼條件讓重點被當成問句（同一種條件的字眼放在一起）。"""
    if not drop.triggers:
        return []
    by_form: dict[str, list[str]] = {}
    for trigger in drop.triggers:
        by_form.setdefault(trigger.form, []).append(_trigger_word(trigger.word))
    parts = [f"{'、'.join(words)}（{_FORM_TEXT.get(form, form)}）" for form, words in by_form.items()]
    return ["     觸發的字眼：" + "；".join(parts)]


def _chain_text(chain: analysis.Chain | None) -> str:
    """D 通過時併了哪幾輪（說話者），以及中間略過的其他聲音。"""
    if chain is None:
        return ""
    members = "＋".join(f"T{index} {chain.speaker}" for index in chain.members)
    skipped = "、".join(f"T{index} {OPAQUE_TAG}" for index in chain.gaps)
    return f"（併 {members}" + (f"，略過 {skipped}" if skipped else "") + "）"


def _whatif_example(drop: _Drop) -> list[str]:
    """這個例子換一種做法重跑會怎樣；兩種做法都沒有東西可以算（或沒有重跑）就沒有這一行。"""
    whatif = drop.whatif
    if whatif is None:
        return []
    parts = []
    if whatif.c is not None:
        parts.append("C 會通過" if whatif.c.kept else f"C 仍被丟掉（{whatif.c.code}）")
    if whatif.d is not None:
        parts.append(f"D 會通過{_chain_text(whatif.chain)}" if whatif.d.kept else f"D 仍被丟掉（{whatif.d.code}）")
    return [f"     假設重跑：{'；'.join(parts)}"] if parts else []


def _example_lines(number: int, drop: _Drop, citable: frozenset[str]) -> list[str]:
    point = drop.point
    turn = f"T{point.turn}" if point.turn is not None else "T?"
    lines = [f"  {number}. [第 {drop.number} 段 {turn} {_label(drop)}]"]
    lines.append("     模型寫的重點：" + (f"「{_clip(point.text)}」" if point.text.strip() else "（空的）"))
    lines.append("     模型的 quote：" + (f"「{_clip(point.quote)}」" if point.quote.strip() else "（空的）"))
    lines.extend(_reason_line(drop))
    lines.extend(_trigger_example(drop))
    lines.extend(_transcript_lines(drop, citable))
    if drop.miss is not None and drop.miss.unseen_share >= UNSEEN_NOTE_SHARE:
        lines.append("     逐字稿從沒出現過的字：" + " ".join(drop.miss.unseen[:12]))
    lines.extend(_whatif_example(drop))
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
        text = "\n".join([*lines, END_LINE])
        if len(text) <= MAX_CHARS or not shown:
            break
        shown.pop()
    if len(text) > MAX_CHARS:
        room = MAX_CHARS - len(TRUNCATED_NOTE) - len(END_LINE) - 2          # 兩個換行；結尾那一行不能被切掉
        text = "\n".join(["\n".join(lines)[:max(0, room)].rstrip(), TRUNCATED_NOTE, END_LINE])
    return text
