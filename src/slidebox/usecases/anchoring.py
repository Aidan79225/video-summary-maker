"""quote 沒有一字不差地落在任何一輪時，還有一種有保護的找法（純邏輯）。真實影片（ReXUCiDCbl8）的診斷顯示，這類 quote
幾乎都不是模型編的：模型的 quote 常有一百多個字（規格是 8 到 20 個字），而逐字稿把同一個人切成很多輪。

D（join_for_quote）：同一位說話者相鄰的輪、中間最多夾 JOIN_GAP_CHARS 個字的其他聲音（雜音），合起來當一輪找。
    只用來找 quote：逐字稿本身不動；合起來的一輪只含 quote 用得到的那幾輪（最短的一段），用第一輪的編號與開始時間。
    條件：全部的合併處裡只有一處（一種對法）對得上、模型填的輪是同一位說話者。

暫定值，用實際影片校正。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..domain.entities import Turn
from .quotes import MIN_QUOTE_CHARS, QUOTE_MIN_LONGEST, _holds, fragments
from .speakers import squash

JOIN_GAP_CHARS = 4


def _chains(in_window: Sequence[Turn], citable: frozenset[str]) -> list[list[Turn]]:
    """同一位說話者相鄰的輪連成一串（中間夾著的其他聲音合計不超過 JOIN_GAP_CHARS 個字就跳過）；不同的可以引用的說話者
    永遠隔開。只回傳兩輪以上的串。"""
    chains: list[list[Turn]] = []
    run: list[Turn] = []
    pending = 0                                       # run 後面夾著的其他聲音合計幾個字

    def flush() -> None:
        nonlocal run, pending
        if len(run) >= 2:
            chains.append(run)
        run, pending = [], 0

    for turn in in_window:
        if turn.speaker in citable:
            if run and run[-1].speaker == turn.speaker:
                run.append(turn)
                pending = 0
                continue
            flush()
            run = [turn]
        elif run and pending + len(squash(turn.text)) <= JOIN_GAP_CHARS:
            pending += len(squash(turn.text))
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


def join_for_quote(quote: object, cited: Turn, in_window: Sequence[Turn], citable: frozenset[str]) -> Joined | None:
    pieces = fragments(quote)
    total = sum(len(piece) for piece in pieces)
    if not pieces or total < MIN_QUOTE_CHARS or max(len(piece) for piece in pieces) < QUOTE_MIN_LONGEST:
        return None
    found: list[tuple[list[Turn], int, int]] = []
    for members in _chains(in_window, citable):
        if members[0].speaker != cited.speaker:
            continue
        flat = [squash(t.text) for t in members]
        size = len(members)
        for i in range(size - 1):
            for j in range(i + 1, size):
                if (_holds("".join(flat[i:j + 1]), pieces)
                        and not _holds("".join(flat[i + 1:j + 1]), pieces)
                        and not _holds("".join(flat[i:j]), pieces)):
                    found.append((members, i, j))
                    break                             # 再往後的 j 都不是最短的
    if len(found) != 1:
        return None
    members, i, j = found[0]
    first, last = members[i], members[j]
    merged = Turn(first.index, first.start, last.end, first.speaker, " ".join(t.text for t in members[i:j + 1]))
    window: list[Turn] = []
    for turn in in_window:
        if turn.index == first.index:
            window.append(merged)
        elif not first.index < turn.index <= last.index:
            window.append(turn)
    return Joined(merged, tuple(window), first.index, last.index)
