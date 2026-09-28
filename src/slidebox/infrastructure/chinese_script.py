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
_TAIWAN_CHARS = frozenset("雇霉庄么虱恒")

# 臺灣用語／地名：轉完被改掉就改回來（位置必須對得上原文）
_TAIWAN_TERMS = (
    "里長", "里民", "鄰里", "里辦公處", "里幹事", "村里長", "萬里", "后里", "大里", "八里",
    "雇主", "雇員", "雇用", "倒霉", "發霉", "老么", "族群", "群組", "病床", "尖峰",
    "核准", "了解", "咨文", "主秘", "秘書", "苧麻", "虱目", "表決", "記名",
    "村里幹事", "村里辦公處",
)

# 漂移段落裡的共用字：預設採用 OpenCC 的結果（它的詞庫知道「屬於」「遊說」「颱風」
# 「準備」「委託」），只有下面這些是 OpenCC 的單字預設或已知錯誤的詞條，逐一例外。
_SURNAMES = frozenset("游范余于郁岳涂")              # 單字預設會變成 遊範餘於鬱嶽塗
_TITLES = (
    "局长", "局長", "委员", "委員", "议员", "議員", "市长", "市長", "部长", "部長", "院长",
    "院長", "主委", "处长", "處長", "先生", "小姐", "女士", "老师", "老師", "署长", "署長",
    "司长", "司長", "科长", "科長", "主任", "次长", "次長", "秘书", "秘書", "董事", "理事",
    "校长", "校長", "所长", "所長", "厅长", "廳長", "课长", "課長", "股长", "股長", "专员",
    "專員", "立委", "议长", "議長", "区长", "區長", "里长", "里長", "教授", "医师", "醫師",
    "律师", "律師", "同学", "同學", "召委", "总统", "總統", "院士", "代表", "副座", "大哥",
    "县长", "縣長", "乡长", "鄉長", "镇长", "鎮長", "村长", "村長", "组长", "組長", "队长", "隊長",
    "主席", "召集人", "执行长", "執行長", "发言人", "發言人", "政委", "经理", "經理",
    "董事长", "董事長", "会长", "會長", "馆长", "館長", "站长", "站長", "厂长", "廠長", "社长",
    "社長", "团长", "團長", "总干事", "總幹事", "阿姨", "伯伯",
)
# 職稱在姓氏前面（立委游毓蘭、委員范雲）也算；「于」例外：前面有職稱的「于」幾乎都是介詞
_SURNAMES_AFTER_TITLE = _SURNAMES - {"于"}
# OpenCC 的「是只→是隻」「这只→這隻」：前面是這些字、後面又不是被數的東西，就是副詞「只」
_ZHI_ADVERB_BEFORE = frozenset("是这這那就也都不还還才只")
# 後面接這些字的「只」是副詞（唯一只有、最多只能、別只看）；量詞「隻」後面接的是名詞
_ZHI_ADVERB_NEXT = frozenset("是有能要会會好针針限剩需看顾顧想靠做讲講说說在对對给給为為差求花拿怕管用")
_COUNTED = frozenset("狗猫貓鸡雞鸟鳥鴨鸭鱼魚手脚腳眼船车車牛羊猪豬犬兔虫蟲熊猴马馬鹿鼠蚊")
_ZHUN_WORD_AFTER = frozenset("确確备備时時则則绳繩点點")  # 不準確、不準備、準則…才是「準」
# 「不准」只有後面接動作（不准停、不准進）才是禁止；句尾、語助詞前、「准不准」「說不准」是「不準」
_ZHUN_PARTICLES = frozenset("了的啦啊耶吗嗎嘛呢喔哦吧欸呀")
_ZHUN_JUDGE_BEFORE = frozenset("说說看抓猜测測")
_TUO_KEEP_BEFORE = frozenset("育婴嬰儿兒盘盤")          # 托育、托嬰、托兒、托盤
_TUO_COMPOUND_AFTER = frozenset("委拜请請信寄嘱囑推")     # 委託、拜託…的「托」照 OpenCC
_QIAN_LOT_BEFORE = frozenset("抽求竹牙号號")              # 抽籤、求籤、竹籤、號碼籤
_QIAN_LOT_AFTER = frozenset("诗詩筒王")
_QIAN_LABEL_BEFORE = frozenset("书書标標")                  # 書籤、標籤——除非後面是簽署的動作
_QIAN_SIGN_AFTER = frozenset("约約名署发發核收呈字")         # 目標簽約、證書簽發、文書簽核
_QIAN_SIGN_AFTER_BOOK = frozenset("了下到")                  # 契約書簽了、協議書簽下去
_QIAN_NOT_LOTTERY = frozenset("其之当當")                    # 其中簽了三份 不是 中籤
# 「里」：後面接 面／頭／邊 是「裡」；前面是這些字、或後面是 長／民／辦… 是行政區的「里」
_LI_INSIDE_AFTER = frozenset("面头頭边邊")
_LI_INSIDE_SIMPLIFIED = frozenset("头边")                    # 正體段落裡的「里头」「里边」
_LI_NEAR_BEFORE = frozenset("这這那哪")                      # 這裡、那裡
_LI_VILLAGE_BEFORE = frozenset("全該该本各每個个們们")
_LI_VILLAGE_AFTER = frozenset("長长民辦办幹干鄰邻")
# 「園區里辦公」「公司里幹了十年」「社區里長大」：後面這兩個字表示「在…裡」，不是里辦、里幹、里長
_LI_LOCATIVE_NEXT = frozenset({"民众", "民眾", "办公", "辦公", "长大", "長大", "长期", "長期",
                               "长久", "長久", "干了", "幹了"})
# 口語的「那」後面接「里民」「里長」，多半是「那，里民也…」；「那裡民宿」「那裡長出」例外
_LI_NA_VILLAGE = frozenset("民长長")
_LI_NA_THERE = frozenset({"民宿", "民生", "民间", "民間", "民风", "民風", "民俗", "民意", "民族",
                          "长出", "長出", "长满", "長滿", "长得", "長得"})
_LI_OFFICES = frozenset({"办公处", "辦公處", "办公室", "辦公室"})   # 里辦公處、里辦公室 是行政區
_NUMERALS = frozenset("零一二三四五六七八九十百千两兩几幾")
# 「三千万里面」「108万里面」「上万里面」：萬前面是數量，「萬里」就不是地名
_AMOUNT = _NUMERALS | frozenset("0123456789０１２３４５６７８９上数數多")
_ZHENG = frozenset("争爭")                                   # 系爭（OpenCC 詞庫把「系争」排成「係爭」）
_JUE = frozenset("决決")                                     # 表決（OpenCC 的「名表→名錶」）
# 保護詞的詞界
_LI_TERM_AFTER = frozenset("这這那哪家心城屋村夜手眼嘴")       # 这里长期 不是 里長
_CITY_BEFORE_LI = ("城市", "都市")                            # 城市里民众 不是 里民
_HOU_TERM_BEFORE = frozenset("在到去住從从的是中台臺 ，。、")   # 只有這些後面的「后里」才是地名

# 「什么」「怎么」的「么」是簡體的「麼」；「老么」的「么」是正確的正體
_ME_RE = re.compile(r"(?<=[什怎这這那多要])么")
_CJK_RUN = re.compile(r"[一-鿿]{2,}")
_CJK_CHAR = re.compile(r"[一-鿿]")

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


@lru_cache(maxsize=1)
def _phrases() -> tuple[frozenset[str], int]:
    """OpenCC 的簡轉繁詞庫（簡體詞）：一個字在詞裡，就信 OpenCC 的詞級結果。"""
    import opencc

    path = os.path.join(os.path.dirname(opencc.__file__), "dictionary", "STPhrases.txt")
    keys = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            key, sep, _ = line.partition("\t")
            if sep:
                keys.add(key)
    return frozenset(keys), max(len(k) for k in keys)


def _in_phrase(norm: str, i: int) -> bool:
    keys, longest = _phrases()
    for size in range(2, min(longest, 6) + 1):
        for start in range(max(0, i - size + 1), min(i, len(norm) - size) + 1):
            if norm[start:start + size] in keys:
                return True
    return False


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
    names = tuple(dict.fromkeys(t for t in keep_terms if len(t) >= 2))
    keep = tuple(dict.fromkeys((*_TAIWAN_TERMS, *names)))
    # 證據只遮講者姓名：姓名是正體，出現在漂移段落裡不代表段落是正體。臺灣用語不遮——
    # 漂掉的段落會寫成「里长」，所以「里長」的「長」是真的正體證據。
    counts = [_counts(t, names, tables) for t in texts]
    drift = _smooth([simp - trad for simp, trad in counts])
    # 只有正體證據、沒有任何簡體字的段落，不管鄰居怎麼說都不是漂移：
    # 「每個里都有」夾在漂移區段前面，不能被鄰居拉去整段轉換
    drift = [d and not (simp == 0 and trad > 0) for d, (simp, trad) in zip(drift, counts)]
    return [_convert(t, d, keep, frozenset(names), tables) for t, d in zip(texts, drift)]


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


def _counts(text: str, keep: Sequence[str], tables) -> tuple[int, int]:
    simplified_only, traditional_only, _, _ = tables
    mask = _masked(text, keep)
    simp = sum(1 for ch, m in zip(text, mask) if not m and ch in simplified_only)
    simp += len(_ME_RE.findall(text))
    trad = sum(1 for ch, m in zip(text, mask) if not m and ch in traditional_only)
    return simp, trad


def _score(text: str, keep: Sequence[str], tables) -> int:
    simp, trad = _counts(text, keep, tables)
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

def _convert(text: str, drift: bool, keep: Sequence[str], names: frozenset[str], tables) -> str:
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
            out.append(_simplified(text, i, o, c, prev, nxt))
        elif drift:
            out.append(_shared(text, norm, i, o, c, prev, nxt))
        elif o == "里" and (prev == "这" or nxt in _LI_INSIDE_SIMPLIFIED):
            out.append("裡")      # 正體段落裡夾著「这里」「里头」：旁邊的字就是簡體證據
        else:
            out.append(o)         # 正體段落裡的共用字一律不動
    return _protect(text, norm, "".join(out), keep, names, to_simp, traditional_only)


def _simplified(text: str, i: int, o: str, c: str, prev: str, nxt: str) -> str:
    """簡體專用字：採用 OpenCC 的詞級結果；「签」的單字預設「籤」是例外。"""
    if o == "签" and c == "籤":
        return _qian(text, i, prev, nxt)
    return c


def _qian(text: str, i: int, prev: str, nxt: str) -> str:
    """「签」：抽籤、中籤、書籤、標籤是「籤」，其他（簽約、簽名、簽了）是「簽」。"""
    if text[max(0, i - 2):i] in ("上上", "下下"):
        return "籤"
    if prev == "中" and (i < 2 or text[i - 2] not in _QIAN_NOT_LOTTERY):
        return "籤"           # 中籤率；其中簽了三份 不是
    if prev in _QIAN_LOT_BEFORE or nxt in _QIAN_LOT_AFTER:
        return "籤"
    if nxt == "一" and text[i + 2:i + 3] == "签":
        return "簽"           # 簽一簽
    if prev in _QIAN_LABEL_BEFORE:
        signing = nxt in _QIAN_SIGN_AFTER or (prev in "书書" and nxt in _QIAN_SIGN_AFTER_BOOK)
        return "簽" if signing else "籤"
    return "簽"


def _shared(text: str, norm: str, i: int, o: str, c: str, prev: str, nxt: str) -> str:
    """漂移段落裡的共用字：預設採用 OpenCC 的詞級結果，只修它的單字預設與已知錯誤的詞條。"""
    if o == "里":
        return _li(text, i, c, prev, nxt)
    if o in "台占" and c in "臺佔":
        return o              # 臺灣兩種寫法都通行；但「台风」的「颱」要照 OpenCC
    if o in _SURNAMES and not _in_phrase(norm, i) and (
            _title_follows(text, i)
            or (o in _SURNAMES_AFTER_TITLE and _title_precedes(text, i))):
        return o              # 不在任何詞裡、前後有職稱：是姓氏（游局長、范雲委員、立委游毓蘭）
    if o == "只" and c == "隻" and _zhi_is_adverb(prev, nxt):
        return o              # 是只有、唯一只能、最多只有、別只看、第一只是：副詞
    if o == "准" and c == "準" and prev == "不" and _zhun_forbids(text, i, nxt):
        return o              # 不准停車（OpenCC 詞庫把「不准」排成「不準」）
    if o == "系" and c == "係" and nxt in _ZHENG:
        return o              # 系爭條文、系爭土地
    if o == "托" and nxt in _TUO_KEEP_BEFORE and prev not in _TUO_COMPOUND_AFTER:
        return o              # 托育、托嬰（OpenCC 會變成「託嬰」）；委托兒福 仍是委託
    if o == "表" and c == "錶" and nxt in _JUE:
        return o              # 記名表決（OpenCC 的「名表→名錶」）
    return c


def _li(text: str, i: int, c: str, prev: str, nxt: str) -> str:
    if (prev == "那" and nxt in _LI_NA_VILLAGE and not _li_locative(text, i)
            and text[i + 1:i + 3] not in _LI_NA_THERE):
        return "里"           # 那 里民也跟我反映、那 里長說：這個「那」是語氣詞
    if prev in _LI_NEAR_BEFORE:
        return "裡"           # 这里长期、那里民众：「這裡」「那裡」優先
    if prev in _LI_TERM_AFTER or text[max(0, i - 2):i] in _CITY_BEFORE_LI:
        return "裡"           # 家里、村里住、在城市里长大：「在…裡」
    if nxt in _LI_INSIDE_AFTER:
        return "裡"           # 里面、里头；真正的地名（萬里、八里…）由保護詞改回
    if prev in _LI_VILLAGE_BEFORE:
        return "里"           # 全里、每個里、該里民眾：行政區
    if _li_locative(text, i):
        return "裡"           # 公園里民众、園區里辦公、社區里長大
    if nxt in _LI_VILLAGE_AFTER:
        return "里"           # 里長、里民、里辦公處
    return c


def _li_locative(text: str, i: int) -> bool:
    return text[i + 1:i + 3] in _LI_LOCATIVE_NEXT and text[i + 1:i + 4] not in _LI_OFFICES


def _title_follows(text: str, i: int) -> bool:
    for j in range(i + 1, i + 4):
        for title in _TITLES:
            if text.startswith(title, j) and not (
                    title in ("委员", "委員") and text[j + 2:j + 3] in ("会", "會")):
                return True   # 「于委员会」的委員會不是職稱
    return False


def _title_precedes(text: str, i: int) -> bool:
    return any(text.endswith(title, 0, i) for title in _TITLES)


def _zhi_is_adverb(prev: str, nxt: str) -> bool:
    if prev in _COUNTED or nxt in _COUNTED:
        return False          # 豬隻、犬隻、一隻狗
    return prev in _ZHI_ADVERB_BEFORE or nxt in _ZHI_ADVERB_NEXT


def _zhun_forbids(text: str, i: int, nxt: str) -> bool:
    """「不准」是禁止（准）還是不準確（準）：後面接動作才是禁止。"""
    before = text[i - 2] if i >= 2 else ""
    if before == "准" or before in _ZHUN_JUDGE_BEFORE:
        return False          # 准不准、說不準
    if not nxt or not _CJK_CHAR.match(nxt) or nxt in _ZHUN_PARTICLES or nxt in _ZHUN_WORD_AFTER:
        return False          # 預報不準、不準啦、不準的問題、不準確
    return True


def _protect(original: str, norm: str, out: str, keep: Sequence[str], names: frozenset[str],
             to_simp, traditional_only: frozenset[str]) -> str:
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
                    and not _crosses_word(original, start, end, term)
                    and not (term in names and _name_inside_word(original, norm, start, end))):
                chars[start:end] = term
            start = norm.find(simp_term, start + 1)
    return "".join(chars)


def _crosses_word(original: str, start: int, end: int, term: str) -> bool:
    """這個位置的保護詞其實是別的詞的一部分，就不要改回來。"""
    before = original[start - 1] if start > 0 else ""
    after = original[end] if end < len(original) else ""
    if term[0] == "里":
        if before in _LI_TERM_AFTER or original[max(0, start - 2):start] in _CITY_BEFORE_LI:
            return True
        if _li_locative(original, start):
            return True       # 社區里長大、公園里民眾 不是 里長、里民
    if term[0] == "后" and before and before not in _HOU_TERM_BEFORE:
        return True           # 然后里面、会后里长 的「后」屬於前一個詞
    if term.endswith("里") and after in _LI_INSIDE_AFTER:
        return before in _AMOUNT  # 三千万里面、108万里面、上万里面 不是 萬里
    if term.endswith("准") and after in _ZHUN_WORD_AFTER:
        return True           # 考核准则 是 準則
    return False


def _name_inside_word(original: str, norm: str, start: int, end: int) -> bool:
    """提示句裡的姓名其實是別的詞的一部分（規范雲端、剩余天數、關于美人魚）：前一個字跟
    姓名的第一個字在 OpenCC 詞庫裡是一個詞，而且後面沒有職稱。"""
    if start == 0 or any(original.startswith(title, end) for title in _TITLES):
        return False
    return _joins(norm, start)


def _joins(norm: str, k: int) -> bool:
    """有沒有一個詞同時蓋到 k-1 與 k 兩個位置。"""
    keys, longest = _phrases()
    for size in range(2, min(longest, 6) + 1):
        for begin in range(max(0, k - size + 1), k):
            if begin + size <= len(norm) and norm[begin:begin + size] in keys:
                return True
    return False
