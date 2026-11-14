"""模型「照抄」的引文（quote）怎麼核對，以及怎麼把核對過的原話原樣拿來顯示（純邏輯）。

整個多人歸屬的基礎在這裡：模型每提出一個重點，都要先從逐字稿照抄一小段原文放進 quote，再填
那段原文所在那一輪的編號。程式不信模型填的編號，也不靠「內容大概像哪一輪」猜，而是找出**真正有
這段原文的那一輪**，說話者就是那一輪的說話者。找不到、或是兩個以上的輪都有，就丟掉（寧可少一點，
不要歸錯人）。

核對（locate_quote）把引文照標點與空白切成幾個片段，每個片段（忽略空白、標點、全半形、大小寫）都要
依序出現在「同一輪」裡：模型常把中間的語助詞或贅詞跳過，這樣不算錯；但接了兩輪的話、改了字、
倒過來的順序都不行。

改用原話（literal_span／rescue 的條件）：模型的改寫對不上那一輪時，如果引文本身是那一輪裡「連續」的
一段，就顯示那一段原話（取自逐字稿，不是模型寫的字）。這裡放的是不依賴詞彙表的部分：連續的位置、
複述（對方說的話被當成自己的主張）的判斷、「你剛才說…」這種開頭。
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from ..domain.entities import Turn
from .speakers import squash

# 引文的長度與片段規則（暫定值，用實際影片校正）
MIN_QUOTE_CHARS = 8                 # 引文（忽略標點空白）至少幾個字；整輪都不到這麼長就要整輪照抄
QUOTE_MIN_FRAGMENT = 2              # 比這短的片段（單字的語助詞）不算
QUOTE_MIN_LONGEST = 5               # 至少要有一個這麼長的片段，才足以認出是哪一輪
MAX_RESCUED_QUOTE_CHARS = 100       # 改用原話時，顯示的原話最長幾個字（連標點；要小於 MAX_POINT_CHARS）

_FRAGMENT_SPLIT_RE = re.compile(r"[\s，,。.；;：:！!？?、…—\-]+")


def fragments(quote: object) -> list[str]:
    """引文照空白與標點切開，每一片只留文字本身（squash），丟掉比 QUOTE_MIN_FRAGMENT 短的。"""
    if not isinstance(quote, str):
        return []
    pieces = (squash(piece) for piece in _FRAGMENT_SPLIT_RE.split(quote))
    return [piece for piece in pieces if len(piece) >= QUOTE_MIN_FRAGMENT]


def _holds(squashed_turn: str, pieces: Sequence[str]) -> bool:
    """每個片段都依序出現在這一輪裡（後一片在前一片之後開始）。"""
    position = 0
    for piece in pieces:
        found = squashed_turn.find(piece, position)
        if found < 0:
            return False
        position = found + len(piece)
    return True


@dataclass(frozen=True)
class QuoteLocation:
    """locate_quote 的結果：turn 是真正有這段引文的那一輪；找不到時 turn 是 None、problem 說明原因。"""
    turn: Turn | None
    problem: str | None = None


def _shown(quote: object, limit: int = 24) -> str:
    flat = " ".join(str(quote).split())
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"


def locate_quote(
    quote: object, cited: Turn, in_window: Sequence[Turn], citable: frozenset[str],
) -> QuoteLocation:
    """找出真正有這段引文的那一輪（只看可以引用的說話者）。

    - 引文的片段要全部依序出現在同一輪裡。
    - 引文太短（片段合計不到 MIN_QUOTE_CHARS，除非剛好是整輪）、或只由很短的片段拼成（最長的片段
      不到 QUOTE_MIN_LONGEST），都不足以認出是哪一輪。
    - 模型引用的那一輪（cited）有就用它；它沒有、剛好只有一個別輪有，就用那一輪
      （模型把問題的編號填在答案的引文上是最常見的錯）；沒有、或有好幾輪，找不到。
    說話者從這裡回傳的 turn 查，**不是**從模型填的編號查。
    """
    pieces = fragments(quote)
    if not pieces:
        if isinstance(quote, str) and squash(quote):          # 有字，但每一段都不到 2 個字（單字的「對」「嗯」…）
            return QuoteLocation(None, (
                f"quote「{_shown(quote)}」太短，無法核對出處；請抄一整個子句（約 8 到 20 個字），"
                f"單字的回答（對、嗯）不能當 quote"))
        return QuoteLocation(None, "quote 是空的；每個重點都要先從逐字稿照抄一小段原文放進 quote")
    holders = [t for t in in_window if t.speaker in citable and _holds(squash(t.text), pieces)]
    if not holders:
        return QuoteLocation(None, (
            f"quote「{_shown(quote)}」不是 T{cited.index} 或任何一輪發言裡連續照抄的原文"
            f"（可能把兩輪接在一起、改了字或跳過了子句）；請只從單一輪發言照抄一小段"))
    total = sum(len(piece) for piece in pieces)
    if total < MIN_QUOTE_CHARS and "".join(pieces) != squash(cited.text):
        return QuoteLocation(None, (
            f"quote「{_shown(quote)}」不到 {MIN_QUOTE_CHARS} 個字，太短無法核對出處；"
            f"請抄一整個子句（約 8 到 20 個字）"))
    if total >= MIN_QUOTE_CHARS and max(len(piece) for piece in pieces) < QUOTE_MIN_LONGEST:
        return QuoteLocation(None, (
            f"quote「{_shown(quote)}」是由太短的片段拼起來的，無法確認出處；請抄一段連續的原文"))
    if any(t.index == cited.index for t in holders):
        return QuoteLocation(cited)
    if len(holders) == 1:
        return QuoteLocation(holders[0])
    return QuoteLocation(None, (
        f"quote「{_shown(quote)}」在好幾輪發言裡都有，無法確定是哪一輪；請抄一段更長、只出現在一輪的原文"))


# --- 原話：引文在那一輪裡的連續位置 ---


def squash_positions(text: str) -> tuple[str, list[int]]:
    """squash(text)，以及每個留下來的字在 text 裡的位置（顯示原話時要回到逐字稿的原文）。"""
    kept: list[str] = []
    positions: list[int] = []
    for position, char in enumerate(text):
        for piece in unicodedata.normalize("NFKC", char).lower():
            if piece.isalnum():
                kept.append(piece)
                positions.append(position)
    return "".join(kept), positions


@dataclass(frozen=True)
class QuoteSpan:
    """引文在那一輪（空白已整理成單一空格）裡連續的一段：text 是逐字稿的原文，[start, end) 是位置。"""
    flat: str
    start: int
    end: int

    @property
    def text(self) -> str:
        return self.flat[self.start:self.end]


def contiguous_span(turn_text: str, quote: object) -> QuoteSpan | None:
    """引文（忽略標點空白）是不是那一輪裡「連續」的一段；是就回傳那一段的位置，否則 None。

    不連續（跳過了子句、自我修正的前半）的引文仍然可以拿來核對出處，但不能拿來當原話顯示：
    顯示出來會像是講者連著說了那兩句。位置對不上 squash（組合字之類少見的狀況）也當作不連續。
    """
    flat = " ".join(turn_text.split())
    squashed_quote = squash(quote) if isinstance(quote, str) else ""
    if not squashed_quote:
        return None
    squashed_turn, positions = squash_positions(flat)
    if squashed_turn != squash(flat):
        return None
    found = squashed_turn.find(squashed_quote)
    if found < 0:
        return None
    return QuoteSpan(flat, positions[found], positions[found + len(squashed_quote) - 1] + 1)


# --- 複述與「你剛才說」 ---

# 引文一開頭就是在轉述對方：「你剛才說…」「所以你是說…」——那是對方的主張，不是這個人的
ECHO_START_RE = re.compile(
    r"^\s*(?:你|您|妳)(?:剛剛|剛才|剛|先前|方才)?(?:說|講|提到|提過|問)"
    r"|^\s*(?:所以|也就是)?你(?:的意思)?是(?:說|不是)?")
# 一個句子在引文之後只剩「是這個意思嗎」這類確認用語：引文是被拿來求證的複述，不是主張
_CONFIRM_TAIL_RE = re.compile(
    r"^(?:是這個意思|這個意思|是這樣嗎|是不是這樣|是不是這個意思|對不對|是嗎|對嗎|對吧|不是嗎)")
# 引文前面接著「所以／也就是說／你的意思是」，而且整句以問號結尾：同樣是複述後求證
_RESTATE_BEFORE_RE = re.compile(r"(?:所以|也就是說|也就是|換句話說|意思是|你的意思是|你是說|你們是說)$")
_SENTENCE_END_CHARS = "。！？!?"


def is_restatement(span: QuoteSpan) -> bool:
    """引文是不是「複述對方、等對方確認」的那半句（單獨拿出來會變成這個人自己的主張，立場被反過來）。

    例：「以標案數量計算，所以十個標案發出去就算達成，土方有沒有清根本不重要，是這個意思嗎？」——
    單獨顯示前半句，讀起來像質詢的人自己這麼主張。故意抓得很窄：引文之後只剩確認用語，
    或引文前面接著 所以／也就是說／你的意思是 而且整句以問號結尾。一個陳述句後面接著開放的問題
    （「還要再花兩百六十八億，做得到嗎？」）不算：那個陳述是他自己的主張。
    """
    flat = span.flat
    left = max((flat.rfind(char, 0, span.start) for char in _SENTENCE_END_CHARS), default=-1)
    rights = [flat.find(char, span.end) for char in _SENTENCE_END_CHARS]
    rights = [position for position in rights if position >= 0]
    right = min(rights) if rights else len(flat)
    tail = squash(flat[span.end:right])
    if not tail:
        return False                                    # 引文一直到這個句子結束
    ends_with_question = right < len(flat) and flat[right] in "？?"
    return bool(_CONFIRM_TAIL_RE.match(tail)
                or (ends_with_question and _RESTATE_BEFORE_RE.search(squash(flat[left + 1:span.start]))))
