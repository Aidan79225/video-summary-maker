"""把 Whisper 在長音訊中途漂成簡體的段落轉回正體，已經是正體的段落一字不動。

實測（新北市議會 1 小時的市政總質詢）：前 35 分鐘是正體，之後整段變成簡體
（「法治单也会支援」）。不能整份丟進 OpenCC——s2t 會把正體裡本來就對的字改錯：
「里長」變「裡長」、「只有」變「隻有」、地名「萬里」變「萬裡」，1 小時的逐字稿
有 47 行被改壞。

所以逐段判斷：只有「簡體專用字」比「正體專用字」多的段落才轉。兩種字表都從
OpenCC 自己的字典推出來——一個字的候選對應裡不包含它自己，就是那一邊專用的
（们→們 是簡體專用；里→裏／里、只→只／隻 兩邊都用，不算）。兩邊共用字組成
的段落（「不是只有永和」）兩種寫法本來就一樣，不需要轉。
"""
from __future__ import annotations

import os
from functools import lru_cache

# 轉換用 s2tw（臺灣用字，字級），不用 s2twp：詞彙轉換會把「分區」改成「分割槽」、
# 「進程」改成「程序」，對口語逐字稿是改錯。
_CONFIG = "s2tw"


def _load(path: str) -> set[str]:
    """字典檔每行「字<TAB>候選 候選…」；候選裡沒有自己的，就是這一邊專用字。"""
    only: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            key, sep, values = line.rstrip("\n").partition("\t")
            if sep and len(key) == 1 and key not in values.split():
                only.add(key)
    return only


@lru_cache(maxsize=1)
def _tables():
    import opencc  # 延遲 import：只有中文的語音辨識才需要

    folder = os.path.join(os.path.dirname(opencc.__file__), "dictionary")
    simplified_only = _load(os.path.join(folder, "STCharacters.txt"))
    traditional_only = _load(os.path.join(folder, "TSCharacters.txt"))
    return simplified_only, traditional_only, opencc.OpenCC(_CONFIG).convert


def looks_simplified(text: str) -> bool:
    simplified_only, traditional_only, _ = _tables()
    simp = sum(ch in simplified_only for ch in text)
    trad = sum(ch in traditional_only for ch in text)
    return simp > trad


def to_traditional(text: str) -> str:
    """段落看起來是簡體才轉成正體；其餘原樣回傳。OpenCC 沒裝就原樣回傳。"""
    if not text:
        return text
    try:
        simplified_only, _, convert = _tables()
    except ImportError:
        return text
    if looks_simplified(text):
        # 整段漂了：用 OpenCC 的詞級轉換，一對多的字（后／後、发／發髮）才轉得對
        return convert(text)
    if any(ch in simplified_only for ch in text):
        # 正體段落夾了幾個簡體字（「市長你知道吗」）：只轉那幾個字。簡體專用字
        # 在正體裡一定是錯字，逐字轉不會誤傷；整段轉反而會碰到共用字。
        return "".join(convert(ch) if ch in simplified_only else ch for ch in text)
    return text
