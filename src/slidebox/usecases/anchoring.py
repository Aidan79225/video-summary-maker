"""quote 沒有一字不差地落在任何一輪時，還有兩種有保護的找法（純邏輯）。真實影片（ReXUCiDCbl8）的診斷顯示，這類 quote
幾乎都不是模型編的：模型的 quote 常有一百多個字（規格是 8 到 20 個字），而逐字稿把同一個人切成很多輪。

D（join_for_quote）：同一位說話者相鄰的輪、中間最多夾 JOIN_GAP_CHARS 個字的其他聲音（雜音），合起來當一輪找。
    只用來找 quote：逐字稿本身不動；合起來的一輪只含 quote 用得到的那幾輪（最短的一段），用第一輪的編號與開始時間。
    條件：全部的合併處裡只有一處（一種對法）對得上、模型填的輪是同一位說話者。

C（longest_run）：只取 quote 裡逐字吻合的最長一段來定位。這放寬了「quote 要完整」的規則，所以有五道保護：
    1. 那一段至少 RUN_MIN_CHARS 個字，而且比別輪（不論是誰）最長的一段多 RUN_MARGIN 個字以上（同長或差不多長就分不出是誰）；
    2. 那一輪是可以引用的說話者；
    3. 模型填的輪和找到的輪是同一位說話者（它以為是誰說的，和程式找到的不能打架）；
    4. quote 沒對上的部分，不能有 OTHER_SPEAKER_RUN_CHARS 個字以上出現在別位說話者的輪裡（不同人的話拼起來的跡象）；
    5. 沒對上的部分要不是至少 MIN_REST_CHARS 個字，就是頭尾合計不超過 EDGE_REST_CHARS 個字（照抄得差不多、只差頭尾幾個字）。
       後者的起點或終點可能差那幾個字（quotes._near_verbatim 因此不用），所以：找到的那一段往頭尾各多取 EDGE_REST_CHARS 個字（逐字稿裡的），
       轉述字眼與前面的引言一個都不會因為差一兩個字而漏掉（頭尾各多取 EDGE_REST_CHARS 個字，比差的字數多）；而且 quote 和這一段的數字、否定、意思相反的用法要一樣（和照抄得差不多相同）。
    找到之後，往下的檢查（出處、數字、否定、問句、轉述…）一個都沒放寬，而且一律用逐字稿的那一段，不用模型寫的字。

暫定值，用實際影片校正。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from ..domain.entities import Turn
from .quotes import MIN_QUOTE_CHARS, QUOTE_MIN_LONGEST, _holds, fragments, squash_positions
from .speakers import squash

JOIN_GAP_CHARS = 4
RUN_MIN_CHARS = 12
RUN_MARGIN = 4
OTHER_SPEAKER_RUN_CHARS = 8
MIN_REST_CHARS = 10
EDGE_REST_CHARS = 6


def _chains(in_window: Sequence[Turn], citable: frozenset[str]) -> list[tuple[list[Turn], list[list[Turn]]]]:
    """同一位說話者相鄰的輪連成一串（中間夾著的其他聲音合計不超過 JOIN_GAP_CHARS 個字就跳過）；不同的可以引用的說話者
    永遠隔開。只回傳兩輪以上的串；每一串附上「每一輪前面夾著的其他聲音」（第一輪前面沒有）。"""
    chains: list[tuple[list[Turn], list[list[Turn]]]] = []
    run: list[Turn] = []
    gaps: list[list[Turn]] = []
    pending: list[Turn] = []                          # run 後面夾著的其他聲音
    pending_chars = 0

    def flush() -> None:
        nonlocal run, gaps, pending, pending_chars
        if len(run) >= 2:
            chains.append((run, gaps))
        run, gaps, pending, pending_chars = [], [], [], 0

    for turn in in_window:
        if turn.speaker in citable:
            if run and run[-1].speaker == turn.speaker:
                run.append(turn)
                gaps.append(pending)
                pending, pending_chars = [], 0
                continue
            flush()
            run, gaps = [turn], [[]]
        elif run and pending_chars + len(squash(turn.text)) <= JOIN_GAP_CHARS:
            pending.append(turn)
            pending_chars += len(squash(turn.text))
        else:
            flush()
    flush()
    return chains


@dataclass(frozen=True)
class Joined:
    """turn 是合起來的一輪（第一輪的編號與開始時間）；window 是把那一段換成它之後的這一窗；
    first／last 是合起來的第一輪與最後一輪的編號。"""
    turn: Turn
    window: tuple[Turn, ...]
    first: int
    last: int


def _span(members: list[Turn], gaps: list[list[Turn]], i: int, j: int, with_gaps: bool) -> list[Turn]:
    """members[i..j] 照順序排；with_gaps 時，中間夾著的其他聲音也照順序放進去（quote 可能就包含那一兩個字）。"""
    out = [members[i]]
    for k in range(i + 1, j + 1):
        if with_gaps:
            out.extend(gaps[k])
        out.append(members[k])
    return out


def join_for_quote(quote: object, cited: Turn, in_window: Sequence[Turn], citable: frozenset[str]) -> Joined | None:
    pieces = fragments(quote)
    total = sum(len(piece) for piece in pieces)
    if not pieces or total < MIN_QUOTE_CHARS or max(len(piece) for piece in pieces) < QUOTE_MIN_LONGEST:
        return None
    found: dict[tuple[int, int, int], tuple[list[Turn], list[Turn]]] = {}
    for chain_id, (members, gaps) in enumerate(_chains(in_window, citable)):
        if members[0].speaker != cited.speaker:
            continue
        size = len(members)
        for with_gaps in (False, True):                  # 先不含夾著的聲音；quote 若包含那一兩個字，再含著找
            for i in range(size - 1):
                for j in range(i + 1, size):
                    def holds(a: int, b: int) -> bool:
                        return _holds("".join(squash(t.text) for t in _span(members, gaps, a, b, with_gaps)), pieces)
                    if holds(i, j) and not holds(i + 1, j) and not holds(i, j - 1):
                        found.setdefault((chain_id, i, j), (members, _span(members, gaps, i, j, with_gaps)))
                        break                             # 再往後的 j 都不是最短的
    if len(found) != 1:
        return None
    (_, i, j), (members, parts) = next(iter(found.items()))
    first, last = members[i], members[j]
    merged = Turn(first.index, first.start, last.end, first.speaker, " ".join(t.text for t in parts))
    window: list[Turn] = []
    for turn in in_window:
        if turn.index == first.index:
            window.append(merged)
        elif not first.index < turn.index <= last.index:
            window.append(turn)
    return Joined(merged, tuple(window), first.index, last.index)


@dataclass(frozen=True)
class Run:
    """turn 是有那一段的輪、text 是逐字稿裡的原文（連標點）；edge 是只差頭尾幾個字的那一種（text 已經往頭尾各多取了 EDGE_REST_CHARS 個字，core 是沒多取的那一段）。"""
    turn: Turn
    text: str
    edge: bool = False
    core: str = ""


def _longest(a: str, b: str) -> tuple[int, int, int]:
    match = SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return match.size, match.a, match.b


def longest_run(quote: object, cited: Turn, in_window: Sequence[Turn], citable: frozenset[str]) -> Run | None:
    whole = squash(quote) if isinstance(quote, str) else ""
    if len(whole) <= RUN_MIN_CHARS:
        return None
    runs: list[tuple[int, int, int, Turn]] = []       # （長度, 這一輪裡的起點, quote 裡的起點, 輪）
    for turn in in_window:
        size, a, b = _longest(squash(turn.text), whole)
        if size:
            runs.append((size, a, b, turn))
    runs.sort(key=lambda item: -item[0])
    if not runs:
        return None
    size, start, at, turn = runs[0]
    if size < RUN_MIN_CHARS or turn.speaker not in citable or turn.speaker != cited.speaker:
        return None
    rest_chars = len(whole) - size
    edge = rest_chars <= EDGE_REST_CHARS
    if not edge and rest_chars < MIN_REST_CHARS:          # 中間的 7–9 個字：既不像照抄，也不夠長到分得出是不是拼接
        return None
    if len(runs) > 1 and runs[1][0] > size - RUN_MARGIN:
        return None
    rest = [part for part in (whole[:at], whole[at + size:]) if part]
    for other in in_window:
        if other.speaker in citable and other.speaker != turn.speaker:
            flat = squash(other.text)
            if any(_longest(flat, part)[0] >= OTHER_SPEAKER_RUN_CHARS for part in rest):
                return None
    flat_text = " ".join(turn.text.split())
    squashed, positions = squash_positions(flat_text)
    if squashed != squash(flat_text):                 # 位置對不上（組合字之類少見的狀況）：不用
        return None
    core = flat_text[positions[start]:positions[start + size - 1] + 1]
    if edge:        # 起點或終點可能差那幾個字（少寫一個字，缺的就多一個）：往頭尾各多取 EDGE_REST_CHARS 個，轉述字眼才不會漏掉
        first = max(0, start - EDGE_REST_CHARS)
        last = min(len(squashed) - 1, start + size - 1 + EDGE_REST_CHARS)
        return Run(turn, flat_text[positions[first]:positions[last] + 1], edge=True, core=core)
    return Run(turn, flat_text[positions[start]:positions[start + size - 1] + 1])
