"""姓名正規化：同一個人在不同頁面上的寫法要對得起來。

新北市議會的官網名冊寫「宋雨蓁 Nikar‧Falong」（中間一個空白、U+2027），
影音系統寫「宋雨蓁Nikar．Falong」（沒有空白、U+FF0E）。原住民族名的間隔號
各頁面各用各的，漢名跟族名之間有沒有空白也不一定。兩邊都過一次這個函式之後
64 位議員全部對得上（2026-09 實測）。
"""
from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")
# 間隔號的各種寫法：U+2027 連字點、U+00B7 中間點、U+30FB 片假名中點 → 全形句點 U+FF0E。
# 選 U+FF0E 是因為影音系統（講者清單、議程字樣）用的是它，文章上的講者姓名也就是它。
_DOT_TABLE = str.maketrans({"‧": "．", "·": "．", "・": "．"})


def normalize_name(name: str) -> str:
    """去掉所有空白（含全形空白），間隔號統一成「．」。"""
    return _WHITESPACE_RE.sub("", name or "").translate(_DOT_TABLE)
