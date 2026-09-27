"""落地檢查：模型說的話必須在原文裡找得到。

模型可能「幫委員說了他沒說的話」，也可能引用一句證據裡根本沒有的條文。
兩種都用同一招擋：正規化之後做子字串比對。
"""
from __future__ import annotations

import re
import unicodedata

_SPACE = re.compile(r"\s+")
_LINE = re.compile(r"^\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\s+(.*)$")


def normalize(text: str) -> str:
    """全形轉半形（NFKC）後去掉所有空白。逐字稿的斷句空白不代表任何意思。"""
    return _SPACE.sub("", unicodedata.normalize("NFKC", text or ""))


def is_grounded(fragment: str, source: str) -> bool:
    needle = normalize(fragment)
    return bool(needle) and needle in normalize(source)


def transcript_lines(transcript: str) -> list[tuple[float, str]]:
    """「mm:ss 內容」或「h:mm:ss 內容」逐行拆開；沒有時間的行併進上一行。"""
    lines: list[tuple[float, str]] = []
    for raw in (transcript or "").splitlines():
        match = _LINE.match(raw)
        if match:
            hours, minutes, seconds, text = match.groups()
            at = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
            lines.append((float(at), text))
        elif raw.strip() and lines:
            at, text = lines[-1]
            lines[-1] = (at, text + raw)
        elif raw.strip():
            lines.append((0.0, raw))
    return lines


def locate(quote: str, transcript: str) -> float | None:
    """quote 在逐字稿的哪一秒開始；找不到就是 None（代表它不是原話）。

    比對的對象是去掉時間標記之後的內容，否則跨行的 quote 中間會夾著下一行
    的時間而永遠找不到。
    """
    needle = normalize(quote)
    if not needle:
        return None
    joined = ""
    starts: list[tuple[int, float]] = []
    for at, text in transcript_lines(transcript):
        starts.append((len(joined), at))
        joined += normalize(text)
    index = joined.find(needle)
    if index < 0:
        return None
    return max((at for offset, at in starts if offset <= index), default=0.0)
