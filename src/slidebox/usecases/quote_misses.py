"""引文找不到的時候，為什麼找不到（純邏輯；只給診斷看，沒有任何判斷依它而定）。

locate_quote（quotes）決定一個引文留不留；它擋下來時，QuoteLocation.kind 說明是在哪一關。其中「不在任何一個
可以引用的輪裡」還可以分得更細，那是這裡做的事：一字不差卻在其他聲音的輪裡、要接起相鄰的輪才湊得齊、片段都在
同一輪但順序不同；其餘看「quote 最少要改幾個字，才會變成某一輪裡連續的一段原文」：改得很少（同音字、數字寫法）、
改了一部分（跳過子句、改寫）、幾乎沒有相同的。

這個模組不屬於 diarized_pipeline.RULE_MODULES：它不決定任何一個點的去留，分類用的門檻又是暫定值、要用真實影片
校正——放在規則模組裡，每校正一次門檻就會讓還沒用完的摘要存檔作廢。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..domain.entities import Turn
from .quotes import QUOTE_AMBIGUOUS, QUOTE_NOT_FOUND, _holds, fragments, locate_quote
from .speakers import squash

# 「不在任何一個可以引用的輪裡」再分得更細（QuoteMiss.kind；另外還有 locate_quote 自己的 QUOTE_*）
MISS_LOCATED = "located"                    # 其實找得到（安全模式會因為別的原因丟掉它）
MISS_OTHER_VOICE = "other_voice"            # 一字不差，但在其他聲音／無法判斷的輪裡（不能引用）
MISS_ACROSS_TURNS = "across_turns"          # 要接起相鄰的兩、三輪才湊得齊
MISS_REORDERED = "reordered"                # 每一片都在同一個可以引用的輪裡，但順序和 quote 不同
MISS_NEAR_VERBATIM = "near_verbatim"        # 照抄得差不多：要改的字不到兩成（同音字、數字寫法、少數簡繁體字）
MISS_PARTIAL = "partial"                    # 只有一部分對得上（跳過子句、改寫）
MISS_ABSENT = "absent"                      # 幾乎不在這一窗的逐字稿裡（編造、整段改寫）
# quote 有幾成的字不用改，就是最接近的那一輪裡連續的一段原文（暫定值，要用實際影片校正）
NEAR_VERBATIM_RATIO = 0.8
PARTIAL_RATIO = 0.4
# 連續幾輪湊起來看（橫跨兩輪、三輪）
MAX_ACROSS_TURNS = 3
# 比對的成本和 quote 長度的平方成正比：quote 太長只拿前面這麼多個字去比；先用相鄰兩字組挑出最可能的幾輪，只對它們精算
MAX_COMPARE_CHARS = 120
MAX_CANDIDATES = 3


@dataclass(frozen=True)
class QuoteMiss:
    """一個 quote 為什麼找不到：kind 是 QUOTE_*（locate_quote 自己給的原因）或 MISS_*；near 是最接近的那幾輪
    （橫跨時是接起來的那幾輪、好幾輪都有時是有這段話的那幾輪；找不到任何一點相像的輪就是空的）；
    ratio 是 quote 在最接近的那一輪裡有幾成的字不用改（只有 near_verbatim 與 partial 才有意義）；
    unseen 是 quote 裡這一窗逐字稿從沒出現過的字（簡體字、編造的詞的線索），unseen_share 是這些字佔 quote 的比例。"""
    kind: str
    near: tuple[Turn, ...] = ()
    ratio: float = 0.0
    unseen: tuple[str, ...] = ()
    unseen_share: float = 0.0


def explain_quote_miss(
    quote: object, cited: Turn, in_window: Sequence[Turn], citable: frozenset[str],
) -> QuoteMiss:
    """locate_quote 沒有留下的 quote 到底是怎麼回事：只給診斷用，不改變任何判斷。

    先問 locate_quote：它自己說得出原因的（空的、太短、只有很短的片段）照它說的，附上被引用的那一輪；好幾輪都有的，
    附上有這段話的那幾輪；找得到的（安全模式不能顯示的那種）回報 located。「不在任何一個可以引用的輪裡」才再分：
    一字不差卻在其他聲音的輪裡、要接起相鄰的輪才湊得齊、片段都在同一輪但順序不同；其餘看最接近的那一輪
    要改幾個字：差幾個字、一部分、幾乎沒有。
    """
    located = locate_quote(quote, cited, in_window, citable, tolerant=False)      # 分析的是「為什麼不是一字不差」
    if located.turn is not None:
        return QuoteMiss(MISS_LOCATED, (located.turn,))
    if located.kind == QUOTE_AMBIGUOUS:
        pieces = fragments(quote)                                  # ambiguous：一定是字串，而且至少有一片
        return QuoteMiss(QUOTE_AMBIGUOUS, tuple(
            t for t in in_window if t.speaker in citable and _holds(squash(t.text), pieces)))
    if located.kind != QUOTE_NOT_FOUND:
        return QuoteMiss(located.kind or "", (cited,))
    pieces = fragments(quote)                                    # not_found：一定是字串，而且至少有一片
    squashed = {t.index: squash(t.text) for t in in_window}
    window_chars = set("".join(squashed.values()))
    whole = squash(quote)
    missing = [char for char in whole if char not in window_chars]
    unseen = tuple(dict.fromkeys(missing))
    share = len(missing) / len(whole)

    def miss(kind: str, near: tuple[Turn, ...] = (), ratio: float = 0.0) -> QuoteMiss:
        return QuoteMiss(kind, near, ratio, unseen, share)

    for t in in_window:
        if t.speaker not in citable and _holds(squashed[t.index], pieces):
            return miss(MISS_OTHER_VOICE, (t,))
    for size in range(2, MAX_ACROSS_TURNS + 1):
        for start in range(len(in_window) - size + 1):
            run = tuple(in_window[start:start + size])
            if _holds("".join(squashed[t.index] for t in run), pieces):
                return miss(MISS_ACROSS_TURNS, run)
    for t in in_window:
        if t.speaker in citable and all(piece in squashed[t.index] for piece in pieces):
            return miss(MISS_REORDERED, (t,))
    best, ratio = _closest(whole, cited, in_window, squashed)
    near = () if best is None else (best,)
    if ratio >= NEAR_VERBATIM_RATIO:
        return miss(MISS_NEAR_VERBATIM, near, ratio)
    if ratio >= PARTIAL_RATIO:
        return miss(MISS_PARTIAL, near, ratio)
    return miss(MISS_ABSENT, near, ratio)


# --- 最接近的那一輪 ---


def _bigrams(text: str) -> set[str]:
    """相鄰兩個字的組合（不到兩個字就是空的）。"""
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _densest(text: str, grams: set[str], width: int) -> tuple[int, int]:
    """text 裡哪一段（寬 width 個字）有最多個位置的相鄰兩字組在 grams 裡：回傳（那一段第一個命中的位置, 命中數）；
    一個都沒有就是（0, 0）。看的是一小段裡的密度，不是整輪有幾種：輪越長、越切題，整輪的種類就越多。"""
    hits = [i for i in range(len(text) - 1) if text[i:i + 2] in grams]
    best_start = best_count = first = 0
    for last, position in enumerate(hits):
        while position - hits[first] >= width:
            first += 1
        if last - first + 1 > best_count:
            best_start, best_count = hits[first], last - first + 1
    return best_start, best_count


def _edits_to_excerpt(quote: str, text: str) -> int:
    """quote 最少要改幾個字（換一個字、多一個字、少一個字各算一個），才會變成 text 裡連續的一段。"""
    previous = [0] * (len(text) + 1)                        # 從 text 的哪裡開始都不用付出
    for row, wanted in enumerate(quote, 1):
        current = [row]
        for column, char in enumerate(text, 1):
            current.append(min(previous[column - 1] + (wanted != char), previous[column] + 1, current[column - 1] + 1))
        previous = current
    return min(previous)                                    # 從 text 的哪裡結束都可以


def _closest(
    whole: str, cited: Turn, in_window: Sequence[Turn], squashed: dict[int, str],
) -> tuple[Turn | None, float]:
    """quote（squash 過）最像哪一輪、有幾成的字不用改。

    先用相鄰兩字組在一小段裡的密度挑出最可能的 MAX_CANDIDATES 輪（一樣密就先看模型引用的那一輪、再看前面的），
    只對這幾輪最密的那一小段（前後各多留一個 quote 那麼長：改了字的地方不一定有相鄰兩字組對得上）算要改幾個字：
    一輪再長，成本也只和 quote 的長度有關。一個都不像就是（None, 0.0）。
    """
    quote = whole[:MAX_COMPARE_CHARS]
    grams = _bigrams(quote)
    width = len(quote) + len(quote) // 4 + 2
    slack = len(quote)
    ranked = []
    for t in in_window:
        start, hits = _densest(squashed[t.index], grams, width)
        if hits:
            ranked.append((-hits, t.index != cited.index, t.index, start, t))
    ranked.sort(key=lambda item: item[:3])
    best: Turn | None = None
    best_ratio = 0.0
    for _, _, _, start, t in ranked[:MAX_CANDIDATES]:
        excerpt = squashed[t.index][max(0, start - slack):start + width + slack]
        ratio = 1 - _edits_to_excerpt(quote, excerpt) / len(quote)
        if ratio > best_ratio:
            best, best_ratio = t, ratio
    return best, best_ratio
