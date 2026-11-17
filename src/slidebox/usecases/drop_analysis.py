"""被丟掉的重點「再算一次」：只算、不判斷（純邏輯；只給診斷看，沒有任何判斷依它而定）。

drop_report 把每個窗的計數與原因列出來；這裡回答「如果…會怎樣」：

- 跨輪的 quote（across_turns）是誰和誰拼的：同一位說話者的相鄰輪、中間夾著很短的其他聲音（diarizer 把一個字判成別的
  聲音）、還是兩位說話者的話接在一起；
- 問句被丟掉（question_stated_as_fact）是哪一個字眼觸發的、離重點多近；
- 換一種做法，被丟掉的 bad_quote 有幾個會通過其餘的檢查：C 只取 quote 裡逐字吻合的最長一段來定位、D 把同一位說話者相鄰的輪併成
  一輪。這些都是**用真的檢查（conversation._check_point）重跑**，不是另外寫一份規則來猜。

這個模組不屬於 diarized_pipeline.RULE_MODULES：它不決定任何一個點的去留，門檻又是暫定值、要用真實影片校正——放在規則
模組裡，每校正一次門檻就會讓還沒用完的摘要存檔作廢。它只該由 drop_report 呼叫。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from ..domain.entities import RawPoint, Turn
from . import conversation as c
from .names import detect_names
from .quotes import squash_positions
from .speakers import squash

# 同一位說話者的兩輪之間，夾著的其他聲音加起來不超過這麼多個字（忽略標點空白），就當作 diarizer 的雜音（暫定值）
JOIN_GAP_CHARS = 6
# 只取 quote 裡逐字吻合的最長一段來定位時，那一段至少幾個字（暫定值）
LONGEST_SEGMENT_MIN_CHARS = 12

# 跨輪的 quote 是誰和誰拼的（splice_pattern）
SPLICE_SAME = "same_speaker"                    # 同一位說話者的相鄰輪（S1／S1）
SPLICE_NOISE = "same_speaker_noise"             # 同一位說話者，中間夾著很短的其他聲音（S1／X／S1）
SPLICE_INTERRUPTED = "same_speaker_interrupted"  # 同一位說話者，中間夾著一整句其他聲音的話
SPLICE_CROSS = "different_speakers"             # 不同說話者的話拼在一起（S1／S2）：不能歸給任何一個人
SPLICE_OTHER = "other"                          # 其他（頭尾是其他聲音）


@dataclass(frozen=True)
class Chain:
    """併起來的一處：speaker 是同一位說話者、members 是併起來的輪編號、gaps 是夾在中間沒有算進去的其他聲音的輪編號、
    gap_chars 是它們合計的字數。"""
    speaker: str
    members: tuple[int, ...]
    gaps: tuple[int, ...] = ()
    gap_chars: int = 0


@dataclass(frozen=True)
class Joined:
    """join_adjacent 的結果：turns 是併過的輪（沒有併的原樣）、merged_into 是原來的輪編號對到併過之後的輪編號
    （被省略的雜音輪沒有）、chains 是併了兩輪以上的每一處。"""
    turns: tuple[Turn, ...]
    merged_into: Mapping[int, int] = field(default_factory=dict)
    chains: tuple[Chain, ...] = ()


def _chars(turns: Sequence[Turn]) -> int:
    return sum(len(squash(t.text)) for t in turns)


def join_adjacent(turns: Sequence[Turn], citable: frozenset[str]) -> Joined:
    """同一位說話者相鄰的輪併成一輪；中間夾著的其他聲音加起來不超過 JOIN_GAP_CHARS 個字就當作雜音，不算進去。

    併起來的輪用第一輪的編號、第一輪的開始與最後一輪的結束，文字用一個空格接起來。不同說話者、其他聲音（不能引用）
    永遠不會併；夾著的其他聲音太長（一整句話）就是真的有人插話，不併。
    """
    out: list[Turn] = []
    merged_into: dict[int, int] = {}
    chains: list[Chain] = []
    run: list[Turn] = []                    # 正在連起來的同一位說話者的輪
    gaps: list[Turn] = []                   # run 裡已經確定夾在中間的雜音
    pending: list[Turn] = []                # run 後面夾著的其他聲音：還不知道後面接不接回同一位說話者

    def emit(turn: Turn) -> None:
        out.append(turn)
        merged_into[turn.index] = turn.index

    def flush() -> None:
        nonlocal run, gaps, pending
        if len(run) >= 2:
            first, last = run[0], run[-1]
            out.append(Turn(first.index, first.start, last.end, first.speaker, " ".join(t.text for t in run)))
            for member in run:
                merged_into[member.index] = first.index
            chains.append(Chain(first.speaker, tuple(t.index for t in run), tuple(t.index for t in gaps), _chars(gaps)))
        elif run:
            emit(run[0])
        for turn in pending:                # 沒有接回同一位說話者的其他聲音：原樣留著
            emit(turn)
        run, gaps, pending = [], [], []

    for turn in turns:
        if turn.speaker in citable:
            if run and run[-1].speaker == turn.speaker:
                gaps.extend(pending)
                pending = []
                run.append(turn)
                continue
            flush()
            run = [turn]
        elif run and _chars([*pending, turn]) <= JOIN_GAP_CHARS:
            pending.append(turn)
        else:
            flush()
            emit(turn)
    flush()
    return Joined(tuple(out), merged_into, tuple(chains))


def chain_for(joined: Joined, turn: int) -> Chain | None:
    """這一輪（原來的輪編號，或併過之後的輪編號）被併進哪一處；沒有併進任何一處就是 None。"""
    first = joined.merged_into.get(turn)
    return next((chain for chain in joined.chains if chain.members[0] == first), None) if first is not None else None


def gap_chars(run: Sequence[Turn]) -> int:
    """一段連續的輪，頭尾之間夾著的輪合計幾個字（忽略標點空白）。"""
    return _chars(run[1:-1])


def splice_pattern(run: Sequence[Turn], citable: frozenset[str]) -> str:
    """跨輪的 quote 是拼自哪幾輪（SPLICE_*）：只看說話者的排列。"""
    first, last = run[0], run[-1]
    if first.speaker not in citable or last.speaker not in citable:
        return SPLICE_OTHER
    if len({t.speaker for t in run if t.speaker in citable}) > 1:
        return SPLICE_CROSS
    inner = run[1:-1]
    if all(t.speaker in citable for t in inner):
        return SPLICE_SAME
    if all(t.speaker not in citable for t in inner) and _chars(inner) <= JOIN_GAP_CHARS:
        return SPLICE_NOISE
    return SPLICE_INTERRUPTED


# --- 問句字眼：是哪一個字眼讓重點被當成問句 ---

# 問句字眼是用什麼條件算數的（QuestionTrigger.form；對應 conversation._is_question_clause 的幾個分支）
FORM_MARKED = "marked"          # 那個子句結尾有真的問號
FORM_SHORT = "short"            # 短的子句（沒有問號）：有問句字眼就算
FORM_NEAR = "near"              # 很長又沒有標點的子句：字眼附近有重點的內容才算（QUESTION_REACH）
FORM_STRICT = "strict"          # 同上，但重點的數字剛被別人說過，所以不看距離（strict_question）
FORM_UNMATCHED = "unmatched"    # 這一輪沒有任何一句和重點共用字：整段只要有問句字眼就算


@dataclass(frozen=True)
class QuestionTrigger:
    """一個讓重點被當成問句的字眼（問號也算一個）和它是用什麼條件算數的。"""
    word: str
    form: str


def question_triggers(turn_text: str, claim_text: str, strict: bool = False) -> tuple[QuestionTrigger, ...]:
    """被引用的那一輪裡，是哪些字眼讓這個重點被當成問句；沒有就是空的（真的檢查也不會把它當成問句）。

    照 conversation._evidence 的樣子走一遍（同一份標點切法、同一批帶著重點的子句、同一個判斷），只是多記下是哪個字眼
    和哪一種條件；tests 用隨機的輸入和真的檢查比對，兩邊一定同時有或同時沒有。strict 是真的就不看字眼離重點多遠。
    """
    claim = c._grams(claim_text)
    flat = " ".join(turn_text.split())
    clauses = [(body, punct) for body, punct in c._CLAUSE_RE.findall(flat) if body.strip()]
    if not clauses:
        return ()
    scores = [len(claim & c._grams(body)) for body, _ in clauses]
    hits = [i for i, s in enumerate(scores) if s > 0]
    low, high = (hits[0], hits[-1]) if hits else (0, len(clauses) - 1)
    if len(c._join(clauses, low, high).rstrip(c._TRAILING_PUNCT)) > c.MAX_EVIDENCE_CHARS:
        low, high = c._best_window(clauses, scores, low, high)
    span = c._join(clauses, low, high)
    if not c._QUESTION_RE.search(span):
        return ()
    best = max(scores)
    if best <= 0:
        return _unique(QuestionTrigger(m.group(0), FORM_UNMATCHED) for m in c._QUESTION_RE.finditer(span))
    gate = None if strict else claim
    found: list[QuestionTrigger] = []
    for i, score in enumerate(scores):
        if score * 2 < best or not c._is_question_clause(*clauses[i], gate):
            continue
        body, punct = clauses[i]
        marked = any(mark in punct for mark in c._QUESTION_MARKS)
        if marked or len(body.strip()) <= c.LONG_CLAUSE_CHARS or gate is None:
            form = FORM_MARKED if marked else FORM_SHORT if len(body.strip()) <= c.LONG_CLAUSE_CHARS else FORM_STRICT
            found.extend(QuestionTrigger(m.group(0), form) for m in c._QUESTION_RE.finditer(body + punct))
        else:
            found.extend(QuestionTrigger(m.group(0), FORM_NEAR)
                         for m in c._QUESTION_RE.finditer(body) if c._asks_about(body, m, claim))
    return _unique(found)


def _unique(items) -> tuple:
    return tuple(dict.fromkeys(items))


def question_cause(ctx: WindowContext, point: c.DroppedPoint, anchor: Turn) -> tuple[QuestionTrigger, ...]:
    """被丟掉的點（question_stated_as_fact）：真的檢查對著 anchor 這一輪做的問句判斷，是哪些字眼、什麼條件讓它算問句。

    和 conversation._check_point 一樣：重點的數字若是這一輪之前最近幾輪的別人剛說過的（連前一窗的尾巴也看），
    就照最嚴格的算、不看字眼離重點多遠（strict）。
    """
    earlier = [t for t in (*ctx.lead_in, *ctx.turns) if t.index < anchor.index]
    strict = c._repeats_number_just_said(anchor, c._grams(point.text), earlier)
    return question_triggers(anchor.text, point.text, strict)


# --- 最長的一段逐字吻合（C）---


@dataclass(frozen=True)
class Segment:
    """quote 裡逐字吻合的最長一段：turn 是有這一段的那一輪、text 是逐字稿裡的原文（連標點）。"""
    turn: Turn
    text: str


def longest_segment(quote: object, in_window: Sequence[Turn], citable: frozenset[str]) -> Segment | None:
    """quote 裡逐字吻合（忽略標點空白）的最長一段，在這一窗的哪一輪、原文是什麼。

    要同時是：至少 LONGEST_SEGMENT_MIN_CHARS 個字、只有一輪（不論是誰）有這麼長的一段（別輪一樣長就分不出是誰說的）、
    那一輪是可以引用的說話者。不是就回傳 None。
    """
    whole = squash(quote) if isinstance(quote, str) else ""
    if not whole:
        return None
    found: list[tuple[int, int, Turn, str, list[int]]] = []          # （長度, 這一輪裡的起點, 輪, 這一輪的原文, 每個字的位置）
    for turn in in_window:
        flat = " ".join(turn.text.split())
        squashed, positions = squash_positions(flat)
        if squashed != squash(flat):                                  # 位置對不上（組合字之類少見的狀況）：不比
            continue
        match = SequenceMatcher(None, squashed, whole, autojunk=False).find_longest_match(0, len(squashed), 0, len(whole))
        if match.size:
            found.append((match.size, match.a, turn, flat, positions))
    if not found:
        return None
    found.sort(key=lambda item: -item[0])
    size, start, turn, flat, positions = found[0]
    if size < LONGEST_SEGMENT_MIN_CHARS or turn.speaker not in citable:
        return None
    if len(found) > 1 and found[1][0] == size:
        return None
    return Segment(turn, flat[positions[start]:positions[start + size - 1] + 1])


# --- 用真的檢查重跑 ---


@dataclass(frozen=True)
class WindowContext:
    """重跑一個點需要的東西（和 conversation.validate_points 替每個窗準備的一樣）：這一窗的輪與範圍、窗開頭之前的
    幾輪（認複述用）、稱呼名單、是不是安全模式；joined 是這一窗同一位說話者相鄰的輪併過的樣子。"""
    turns: tuple[Turn, ...]
    first: int
    last: int
    lead_in: tuple[Turn, ...]
    citable: frozenset[str]
    names: frozenset[str]
    joined: Joined
    quote_only: bool = False


@dataclass(frozen=True)
class Rerun:
    """重跑的結果：kept 是通過了（其餘的檢查都過了；不含頁數與重複的規則），turn 是這個點最後歸給哪一輪（併過的輪用
    第一輪的編號）；沒通過就是 code（丟掉的原因碼）。"""
    kept: bool
    code: str = ""
    turn: int | None = None


def window_contexts(windows: Sequence[c.Window], citable: frozenset[str], quote_only: bool = False) -> list[WindowContext]:
    """每個窗一份 WindowContext（照 windows 的順序）。窗不重疊，窗頭的複述要看前一窗的尾巴，所以 lead_in 取自所有的窗。"""
    by_index = {t.index: t for window in windows for t in window.turns}
    out = []
    for window in windows:
        turns = tuple(window.turns)
        first, last = (turns[0].index, turns[-1].index) if turns else (0, -1)
        lead_in = tuple(by_index[i] for i in range(first - c.ANCHOR_RADIUS, first) if i in by_index)
        names = detect_names("\n".join(t.text for t in turns))
        out.append(WindowContext(turns, first, last, lead_in, citable, names, join_adjacent(turns, citable), quote_only))
    return out


def rerun_with_quote(ctx: WindowContext, point: c.DroppedPoint, quote: str) -> Rerun | None:
    """這個被丟掉的點，換成 quote 之後，用真的檢查重跑一次；它引用的輪不在這一窗裡就沒有東西可以重跑（None）。"""
    return _rerun(ctx, ctx.turns, point.turn, point.text, quote)


def try_longest_segment(ctx: WindowContext, point: c.DroppedPoint) -> Rerun | None:
    """C：只取 quote 裡逐字吻合的最長一段來定位，重跑；沒有夠格的一段（見 longest_segment）就是 None。"""
    segment = longest_segment(point.quote, ctx.turns, ctx.citable)
    return None if segment is None else rerun_with_quote(ctx, point, segment.text)


def try_joined_turns(ctx: WindowContext, point: c.DroppedPoint) -> Rerun | None:
    """D：把同一位說話者相鄰的輪併成一輪（見 join_adjacent）再重跑；這一窗沒有可以併的輪、或引用的輪不在窗裡就是 None。"""
    if not ctx.joined.chains or not isinstance(point.turn, int):
        return None
    mapped = ctx.joined.merged_into.get(point.turn)
    return None if mapped is None else _rerun(ctx, ctx.joined.turns, mapped, point.text, point.quote)


def _rerun(ctx: WindowContext, turns: Sequence[Turn], turn: object, text: str, quote: str) -> Rerun | None:
    by_index = {t.index: t for t in turns}
    if not isinstance(turn, int) or turn not in by_index:
        return None
    check = c._check_point(RawPoint(turn, text, quote=quote), by_index, list(turns), ctx.citable, ctx.first, ctx.last,
                           ctx.names, ctx.quote_only, ctx.lead_in)
    if check.point is not None:
        return Rerun(True, turn=check.point.turn)
    assert check.problem is not None                       # 沒有點就一定有原因
    return Rerun(False, check.problem.code)
