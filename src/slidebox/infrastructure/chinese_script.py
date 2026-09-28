"""把語音辨識漂成簡體的段落轉回正體；本來就是正體的段落一字不動。

實測：Whisper 在長音訊中途會整段漂成簡體（新北市議會 1 小時的質詢從第 35 分
鐘起全簡體），立法院自己用 WhisperX 產的逐字稿也有（IVOD 170000 有 9/14 段）。

不能整份丟進 OpenCC——正體的「里長」「只有」「萬里」「雇主」會被改錯。規則：

1. **判斷每段是哪一種**：數「簡體專用字」與「正體專用字」。字表從 OpenCC 自己
   的字典推：一個字的候選對應裡不包含自己、而且 s2tw 真的會改它，才算那一邊專用
   （们→們 算；里→裏／里、只→只／隻 兩邊都用，不算；群→羣 但 s2tw 又改回群，
   這是臺灣的標準字，不算）。另外排除臺灣慣用的 雇、霉、庄、么。
2. **漂移是一段一段連著的**：只有共用字的段落（「然后」「拜托一下」）看不出是哪
   一種，沿用前一段的判斷——在簡體區段裡就轉，在正體區段裡就不動。
3. **轉的時候先正規化**：s2tw(t2s(text))，段落裡已經是正體的詞（里長、萬里）才
   對得到 OpenCC 的詞庫。
4. **正體段落夾了幾個簡體字**：只換那幾個字（以及跟它相連、同一個詞裡被 OpenCC
   改掉的共用字，「这里」→「這裡」），取值來自整段的詞級轉換，「分钟」才會是
   「分鐘」而不是單字預設的「分鍾」。
5. **轉完再保護**：臺灣用語與專有名詞（里長、萬里、后里、雇主、咨文…，加上呼叫
   端給的講者姓名）若被 OpenCC 改掉就改回來；OpenCC 詞庫的「是只→是隻」會讓
   「不是只有」變「不是隻有」，前面不是數量詞的「隻」改回「只」。
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable
from functools import lru_cache

# 轉換用 s2tw（臺灣用字，字級＋基本詞），不用 s2twp：詞彙轉換會把「分區」改成
# 「分割槽」、「進程」改成「程序」，對口語逐字稿是改錯。
_CONFIG = "s2tw"

# OpenCC 當成簡體、但在臺灣是正確寫法的字
_TAIWAN_CHARS = frozenset("雇霉庄么")

# 臺灣用語／地名：OpenCC 轉簡體段落時可能改壞，轉完要改回來
_TAIWAN_TERMS = (
    "里長", "里民", "鄰里", "里辦公處", "里幹事", "村里", "萬里", "后里", "大里",
    "雇主", "雇員", "雇用", "倒霉", "發霉", "老么", "族群", "群組", "病床", "尖峰",
    "核准", "批准", "了解", "咨文", "主秘", "秘書", "台灣", "台北", "台中", "台南",
    "台東", "舞台", "平台", "月台", "櫃台", "后豐", "苧麻",
)

# 「兩隻」「幾隻」的「隻」是量詞，其餘位置的「隻」多半是 OpenCC 把「只」改壞的
_MEASURE_BEFORE_ZHI = frozenset("一二兩两三四五六七八九十百千幾几這这那哪每半多好")

# 「什么」「怎么」之類的「么」是簡體的「麼」；「老么」的「么」是正確的正體
_ME_RE = re.compile(r"(?<=[什怎这這那多要])么")

# 保護詞的第一個字若和前一個字組成別的詞，這個位置就不是保護詞：
# 「这里长期」是「這裡／長期」不是「里長」，「然后里面」是「然後／裡面」不是「后里」
_NOT_A_TERM_AFTER = {
    "里": frozenset("这這那哪家心城屋村夜手眼嘴鄉乡"),
    "后": frozenset("然以最之往前背落此事午今而"),
}

# 從提示句抽出要保護的詞：連續兩個以上的漢字
_CJK_RUN = re.compile(r"[一-鿿]{2,}")


def _load(path: str) -> dict[str, list[str]]:
    table: dict[str, list[str]] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            key, sep, values = line.rstrip("\n").partition("\t")
            if sep and len(key) == 1:
                table[key] = values.split()
    return table


@lru_cache(maxsize=1)
def _tables():
    import opencc  # 延遲 import：只有中文逐字稿才需要

    folder = os.path.join(os.path.dirname(opencc.__file__), "dictionary")
    st = _load(os.path.join(folder, "STCharacters.txt"))
    ts = _load(os.path.join(folder, "TSCharacters.txt"))
    to_trad = opencc.OpenCC(_CONFIG).convert
    to_simp = opencc.OpenCC("t2s").convert
    traditional_only = frozenset(c for c, vs in ts.items() if c not in vs)
    simplified_only = frozenset(
        c for c, vs in st.items()
        if c not in vs and to_trad(c) != c
        and c not in traditional_only and c not in _TAIWAN_CHARS
    )
    return simplified_only, traditional_only, to_trad, to_simp


def keep_terms_from(text: str | None) -> list[str]:
    """提示句（「臺中市議會 市政總質詢。發言者：楊啓邦、游淑慧」）裡的漢字詞。"""
    return _CJK_RUN.findall(text or "")


class TraditionalFixer:
    """逐段修正一份逐字稿。要依序餵段落：它記得目前是不是在簡體漂移的區段裡。

    OpenCC 沒裝或字典壞了就什麼都不做——沒轉的逐字稿仍然可用，丟例外會讓整篇失敗。
    """

    def __init__(self, keep_terms: Iterable[str] = ()):
        try:
            self._tables = _tables()
        except (ImportError, OSError):
            self._tables = None
        self._keep = tuple(dict.fromkeys(t for t in (*_TAIWAN_TERMS, *keep_terms) if len(t) >= 2))
        self._in_simplified = False

    def __call__(self, text: str) -> str:
        if not text or self._tables is None:
            return text
        simplified_only, traditional_only, to_trad, to_simp = self._tables
        # 「什么」「怎么」的么也算簡體證據；「老么」不算
        simp = sum(ch in simplified_only for ch in text) + len(_ME_RE.findall(text))
        trad = sum(ch in traditional_only for ch in text)
        if simp > trad:
            self._in_simplified = True
        elif trad > simp:
            self._in_simplified = False
        # 其餘（都是共用字，或兩種一樣多）：沿用前一段的判斷

        if simp > trad or (simp == 0 and trad == 0 and self._in_simplified):
            return self._protect(text, to_trad(to_simp(text)))
        if simp:
            return self._patch(text, simplified_only)
        return text

    def _patch(self, text: str, simplified_only: frozenset[str]) -> str:
        """正體段落裡的簡體字：只換那幾個字與同一個詞裡被改掉的相鄰共用字。"""
        _, _, to_trad, to_simp = self._tables
        full = self._protect(text, to_trad(to_simp(text)))
        if len(full) != len(text):
            return _ME_RE.sub("麼", "".join(to_trad(ch) if ch in simplified_only else ch for ch in text))
        take = [ch in simplified_only for ch in text]
        for m in _ME_RE.finditer(text):
            take[m.start()] = True
        for i, ch in enumerate(text):
            if not take[i] and full[i] != ch and (
                    (i > 0 and text[i - 1] in simplified_only)
                    or (i + 1 < len(text) and text[i + 1] in simplified_only)):
                take[i] = True
        return "".join(f if t else o for o, f, t in zip(text, full, take))

    def _protect(self, original: str, converted: str) -> str:
        _, _, to_trad, to_simp = self._tables
        norm = to_simp(original)
        out = list(converted)
        aligned = len(norm) == len(original) == len(converted)
        for term in self._keep:
            simp_term = to_simp(term)
            if not aligned:
                bad = to_trad(simp_term)
                if bad != term:
                    out = list("".join(out).replace(bad, term))
                continue
            # 原文在這個位置就是這個詞（不管原本寫成正體或簡體）：轉完不是它就改回來。
            # 不能只比對「單獨轉這個詞會變成什麼」——OpenCC 的詞庫可能從別的詞切進來
            # （「一群里民」會先吃到「群里」而變成「群裡」）。
            start = norm.find(simp_term)
            while start != -1:
                end = start + len(term)
                blocked = start > 0 and original[start - 1] in _NOT_A_TERM_AFTER.get(term[0], ())
                if not blocked and "".join(out[start:end]) != term:
                    out[start:end] = term
                start = norm.find(simp_term, start + 1)
        if aligned:
            for i, ch in enumerate(original):
                if ch == "只" and out[i] == "隻" and (i == 0 or original[i - 1] not in _MEASURE_BEFORE_ZHI):
                    out[i] = "只"
        return "".join(out)


def to_traditional(text: str, keep_terms: Iterable[str] = ()) -> str:
    """單一段落的版本（沒有前後文）：只有共用字的段落不會被轉。"""
    return TraditionalFixer(keep_terms)(text)


def looks_simplified(text: str) -> bool:
    try:
        simplified_only, traditional_only, _, _ = _tables()
    except (ImportError, OSError):
        return False
    simp = sum(ch in simplified_only for ch in text) + len(_ME_RE.findall(text))
    return simp > sum(ch in traditional_only for ch in text)
