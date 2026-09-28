"""把語音辨識漂成簡體的段落轉回正體；本來就是正體的字一個都不動。

實測：Whisper 在長音訊中途會整段漂成簡體（新北市議會 1 小時的質詢從第 35 分鐘起
全簡體），立法院自己用 WhisperX 產的逐字稿也有（IVOD 170000 有 9/14 段）。

不能整份丟進 OpenCC：正體的「里長」「只有」「萬里」「雇主」「虱目魚」都會被改錯。
兩輪 review 找到的問題歸成三類，這裡的做法分別對應：

1. **哪些段落漂了**：看整份逐字稿，不是一段一段各自判斷。每段的證據是「簡體專用
   字數 − 正體專用字數」（保護詞裡的字不算，講者姓名不能左右判斷），再用前後幾段
   平滑：一個「对」不會讓後面的正體段落被整段轉換，一個「市長」也不會讓漂移區段
   提早結束，開頭只有共用字的段落（「然后呢」）看後面來決定。
2. **怎麼換字**：先把整段正規化再轉（s2tw(t2s(text))），取得詞級的正確選擇
   （分钟→分鐘、头发→頭髮），但逐字決定要不要採用：
   - 正體專用字（裡、盡、畫…）一律保留原字；
   - 簡體專用字一律採用轉換結果；
   - 兩邊共用的字（里、后、只、准、游…）只在漂移段落才換，而且有逐字規則。
3. **臺灣用法**：共用字的規則（台／占保留、姓氏在職稱前保留、「不准」「托育」保留、
   「只」後面接副詞用法時不改成「隻」、單獨的「签」是「簽」），加上保護詞（臺灣
   用語＋提示句裡的姓名）轉完改回來，並檢查詞界（「这三千万里面」不是「萬里」）。

字表從 OpenCC 自己的字典推：一個字的候選對應不包含自己、而且 s2tw 真的會改它，
才算那一邊專用；再排除臺灣標準字（雇霉庄么虱）。
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from functools import lru_cache

# s2tw（臺灣用字，字級＋基本詞）。不用 s2twp：詞彙轉換會把「分區」改成「分割槽」。
_CONFIG = "s2tw"

# OpenCC 當成簡體、但在臺灣是正確寫法的字
_TAIWAN_CHARS = frozenset("雇霉庄么虱")

# 臺灣用語／地名：轉完被改掉就改回來（位置必須對得上原文）
_TAIWAN_TERMS = (
    "里長", "里民", "鄰里", "里辦公處", "里幹事", "村里長", "萬里", "后里", "大里",
    "雇主", "雇員", "雇用", "倒霉", "發霉", "老么", "族群", "群組", "病床", "尖峰",
    "核准", "批准", "了解", "咨文", "主秘", "秘書", "苧麻", "虱目", "表決", "記名",
)

# 共用字的逐字規則用到的字集
_KEEP_ALWAYS = frozenset("台占")                     # 臺灣兩種寫法都通行：保留原字
_SURNAMES = frozenset("游范余于郁岳涂")              # 當姓氏時不能改成 遊範餘於鬱嶽塗
_NOT_SURNAME_BEFORE = frozenset("旅导導上下交漫周云雲优優关關由至对對其多剩业業残殘")
_TITLES = (
    "局长", "局長", "委员", "委員", "议员", "議員", "市长", "市長", "部长", "部長", "院长",
    "院長", "主委", "处长", "處長", "先生", "小姐", "女士", "老师", "老師", "署长", "署長",
    "司长", "司長", "科长", "科長", "主任", "次长", "次長", "秘书", "秘書", "董事", "理事",
    "校长", "校長", "所长", "所長", "厅长", "廳長", "课长", "課長", "股长", "股長", "专员",
    "專員", "立委", "议长", "議長", "区长", "區長", "里长", "里長", "教授", "医师", "醫師",
    "律师", "律師", "同学", "同學", "召委", "总统", "總統", "院士", "代表", "副座", "大哥",
)
_ZHUN_KEEP_AFTER = frozenset("不批核获獲照恩允")      # 不准、批准、核准…的「准」是對的
_TUO_KEEP_BEFORE = frozenset("育婴嬰儿兒盘盤")        # 托育、托嬰、托兒、托盤
# 「只」後面接這些是副詞「只」（只有、只能、只是…），不是量詞「隻」
_ZHI_ADVERB_NEXT = frozenset(
    "有能要好會会是剩不可得管因在想知說说需须須限跟和對对怕看做作用拿讓让給给把被為为"
    "從从就許许見见算還还够夠准準差")
_QIAN_LOT_BEFORE = frozenset("抽标標书書求中牙")      # 抽籤、標籤、書籤、求籤、中籤、牙籤
_LI_INSIDE_AFTER = frozenset("面头頭边邊")            # 里面／里頭／里邊 的「里」是「裡」
_LI_INSIDE_BEFORE = frozenset("这那哪這")             # 这里／那里／哪里 的「里」是「裡」
# 保護詞的詞界：這些字接在前面，表示這個位置其實屬於前一個詞
_NOT_A_TERM_AFTER = {
    "里": frozenset("这這那哪家心城屋村夜手眼嘴鄉乡縣县市"),
    "后": frozenset("然以最之往前背落此事午今而"),
}

# 「什么」「怎么」的「么」是簡體的「麼」；「老么」的「么」是正確的正體
_ME_RE = re.compile(r"(?<=[什怎这這那多要])么")
_CJK_RUN = re.compile(r"[一-鿿]{2,}")

# 平滑：往前、往後各看幾段「有證據」的段落（沒證據的跳過）、每段最多算幾分、
# 自己的證據多強就不看鄰居
_NEIGHBOURS = 3
_CLIP = 3
_STRONG = 2


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


def _available():
    try:
        return _tables()
    except (ImportError, OSError):
        # OpenCC 沒裝或字典壞了：什麼都不做。沒轉的逐字稿仍然可用，丟例外會讓整篇失敗。
        return None


def keep_terms_from(text: str | None) -> list[str]:
    """提示句（「臺中市議會 市政總質詢。發言者：楊啓邦、游淑慧」）裡的漢字詞。

    以「、」「：」與非漢字切開，名字就會是獨立的詞。
    """
    return _CJK_RUN.findall(text or "")


# --- 對外 ---

def fix_transcript(texts: Sequence[str], keep_terms: Iterable[str] = ()) -> list[str]:
    """整份逐字稿（依時間順序的段落）轉成正體。"""
    tables = _available()
    if tables is None:
        return list(texts)
    keep = tuple(dict.fromkeys(t for t in (*_TAIWAN_TERMS, *keep_terms) if len(t) >= 2))
    scores = [_score(t, keep, tables) for t in texts]
    drift = _smooth(scores)
    return [_convert(t, d, keep, tables) for t, d in zip(texts, drift)]


def to_traditional(text: str, keep_terms: Iterable[str] = ()) -> str:
    """單一段落（沒有前後文）。"""
    return fix_transcript([text], keep_terms)[0]


def looks_simplified(text: str) -> bool:
    tables = _available()
    return tables is not None and _score(text, (), tables) > 0


# --- 判斷 ---

def _masked(text: str, keep: Sequence[str]) -> list[bool]:
    """保護詞覆蓋到的位置。講者姓名是正體（例如「黃守達」），它們出現在漂移段落裡
    不代表段落是正體，不能拿來算證據。"""
    mask = [False] * len(text)
    for term in keep:
        start = text.find(term)
        while start != -1:
            for i in range(start, start + len(term)):
                mask[i] = True
            start = text.find(term, start + 1)
    return mask


def _score(text: str, keep: Sequence[str], tables) -> int:
    simplified_only, traditional_only, _, _ = tables
    mask = _masked(text, keep)
    simp = sum(1 for ch, m in zip(text, mask) if not m and ch in simplified_only)
    simp += len(_ME_RE.findall(text))
    trad = sum(1 for ch, m in zip(text, mask) if not m and ch in traditional_only)
    return simp - trad


def _smooth(scores: Sequence[int]) -> list[bool]:
    """自己的證據夠強（±2 以上）就照自己；否則加上前後最近幾段「有證據」的段落。

    跳過沒證據的段落（只有共用字的「然后」「拜托一下」）：漂移區段裡常連著好幾段
    都是共用字，固定寬度的視窗會在中間失去前後文。
    """
    clipped = [max(-_CLIP, min(_CLIP, s)) for s in scores]
    evidence = [i for i, s in enumerate(scores) if s != 0]
    out: list[bool] = []
    for i, score in enumerate(scores):
        if score >= _STRONG:
            out.append(True)
            continue
        if score <= -_STRONG:
            out.append(False)
            continue
        before = [j for j in evidence if j < i][-_NEIGHBOURS:]
        after = [j for j in evidence if j > i][:_NEIGHBOURS]
        out.append(clipped[i] + sum(clipped[j] for j in (*before, *after)) > 0)
    return out


# --- 換字 ---

def _convert(text: str, drift: bool, keep: Sequence[str], tables) -> str:
    if not text:
        return text
    simplified_only, traditional_only, to_trad, to_simp = tables
    norm = to_simp(text)
    full = to_trad(norm)
    if not (len(norm) == len(full) == len(text)):
        # 對不齊（極少見）：保守處理，只換簡體專用字
        return "".join(to_trad(ch) if ch in simplified_only else ch for ch in text)

    me = {m.start() for m in _ME_RE.finditer(text)}
    out = []
    for i, (o, c) in enumerate(zip(text, full)):
        prev = text[i - 1] if i > 0 else ""
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if o in traditional_only:
            out.append(o)
        elif i in me:
            out.append("麼")
        elif o in simplified_only:
            if o == "签" and c == "籤" and prev not in _QIAN_LOT_BEFORE:
                c = "簽"          # OpenCC 單字預設是「籤」；說話裡的「签」幾乎都是簽名的簽
            out.append(c)
        elif o == "里" and (nxt in _LI_INSIDE_AFTER or prev in _LI_INSIDE_BEFORE):
            out.append("裡")      # 里面／这里：兩種模式都該換（OpenCC 的「格里」會蓋掉「里面」）
        elif not drift:
            out.append(o)         # 正體段落裡的共用字一律不動
        else:
            out.append(_shared(text, i, o, c, prev, nxt))
    return _protect(text, norm, "".join(out), keep, to_simp, traditional_only)


def _shared(text: str, i: int, o: str, c: str, prev: str, nxt: str) -> str:
    """漂移段落裡的共用字：預設採用 OpenCC 的詞級結果，臺灣用法例外。"""
    if o in _KEEP_ALWAYS:
        return o
    if o in _SURNAMES and _surname_like(text, i, prev):
        return o
    if o == "准" and prev in _ZHUN_KEEP_AFTER:
        return o
    if o == "托" and nxt in _TUO_KEEP_BEFORE:
        return o
    if o == "只" and c == "隻" and nxt in _ZHI_ADVERB_NEXT:
        return o              # OpenCC 詞庫的「是只」「这只」會把副詞「只有／只能」改成「隻」
    return c


def _surname_like(text: str, i: int, prev: str) -> bool:
    """後面 1～3 個字內接職稱（游淑慧議員、余局長），前面又不是「旅游」「其余」之類的詞。"""
    if prev in _NOT_SURNAME_BEFORE:
        return False
    return any(text.startswith(title, j) for j in range(i + 1, i + 4) for title in _TITLES)


def _protect(original: str, norm: str, out: str, keep: Sequence[str], to_simp,
             traditional_only: frozenset[str]) -> str:
    """保護詞：原文在這個位置就是這個詞（不管寫成正體或簡體），轉完不是它就改回來。

    不能只比對「單獨轉這個詞會變成什麼」——OpenCC 可能從別的詞切進來（「一群里民」
    會先吃到「群里」）。但要檢查詞界：「这三千万里面」「然后里面」的「萬里」「后里」
    是別的詞的一部分。
    """
    chars = list(out)
    for term in keep:
        simp_term = to_simp(term)
        start = norm.find(simp_term)
        while start != -1:
            end = start + len(term)
            current = "".join(chars[start:end])
            span = original[start:end]
            # 只收回自己改過的地方：原文本來就是別種正確寫法（瞭解）就不去動它；
            # 原文的正體專用字更不能被保護詞蓋掉（「議會裡长期」不是「里長」）
            if (current != term and current != span
                    and not any(o in traditional_only and o != t for o, t in zip(span, term))
                    and not _crosses_word(original, start, end, term)):
                chars[start:end] = term
            start = norm.find(simp_term, start + 1)
    return "".join(chars)


def _crosses_word(original: str, start: int, end: int, term: str) -> bool:
    before = original[start - 1] if start > 0 else ""
    after = original[end] if end < len(original) else ""
    if before in _NOT_A_TERM_AFTER.get(term[0], ()):
        return True
    return term.endswith("里") and after in _LI_INSIDE_AFTER
