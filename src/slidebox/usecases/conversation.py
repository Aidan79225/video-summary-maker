"""多人對談的摘要：切窗、分配頁數、檢查模型提出的重點、排成投影片（純邏輯，不碰 IO、不碰模型）。

模型只看到「[T12] S1：文字」這樣的行，回傳「照抄的一小段原文（quote）＋那段原文在第幾輪（turn）＋一句
沒有主詞的重點」。**說話者永遠由程式查出來**：程式找出真正有這段原文的那一輪，說話者就是那一輪的
說話者；模型從來不決定誰說了什麼，也看不到姓名、職稱、頻道或說明。validate_points 逐點檢查，
過不了的點整個丟掉：

    寧可少一個重點，也不要把話歸給錯的人。

只有一種例外，而且比丟掉更保守：模型的改寫和那段原文的用字對不上，但它照抄的原文本身核對過了，
就改成顯示那一段原話（「…」，取自逐字稿，不是模型寫的字）。所以畫面上的每個重點，不是通過全部
檢查的改寫，就是逐字稿裡某一位主要發言者的一段原話。

所有門檻（共同用字的比例、往前後找幾段、引文最短幾個字…）都是暫定值，要用實際影片校正。
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache

from ..domain.entities import Point, RawPoint, RawSlide, Slide, SpeakerInfo, Turn
from .names import detect_names
from .numerals import number_problem
from .quotes import (
    ECHO_START_RE,
    MAX_RESCUED_QUOTE_CHARS,
    MIN_QUOTE_CHARS,
    QuoteSpan,
    contiguous_span,
    is_restatement,
    locate_quote,
)
from .speakers import FILLER_CHARS, PARTICIPANT, UNDECIDED, clock, squash

# 提示詞或驗證規則改了就加一，存檔的指紋跟著變，舊的結果不會被拿來冒充新的。
# 2：每個重點先照抄一段原文（quote）、說話者由 quote 所在的那一輪決定；數字改比「數值與單位」；
#    問句只看帶著重點的句子；文字裡不可以有逐字稿用來稱呼人的名字；提示詞整份換掉。
PROMPT_VERSION = 2

# --- 切窗 ---
CHUNK_CHARS_ENV = "SLIDEBOX_CHUNK_CHARS"
DEFAULT_CHUNK_CHARS = 6000
MIN_CHUNK_CHARS = 1000
MAX_CHUNK_CHARS = 20000
# 少數聲音（其他聲音、無法判斷）在給模型的行裡一律用這個固定的標記，不能被引用
OPAQUE_TAG = "X"

# --- 只顯示原話的安全模式 ---
QUOTE_ONLY_ENV = "SLIDEBOX_QUOTE_ONLY"
_TRUE_WORDS = frozenset({"1", "true", "yes"})

# --- 檢查（暫定值）---
MIN_POINT_CHARS = 4
MAX_POINT_CHARS = 120
MAX_EVIDENCE_CHARS = 80
MAX_TITLE_CHARS = 60
MIN_SHARED_GRAMS = 2
MIN_SHARED_RATIO = 0.25
# 引用的那一段對不上時，只在前後這麼多段裡找出處
ANCHOR_RADIUS = 3
# 兩段的相符程度差不到「重點字數 × 這個比例」就算一樣好
TIE_MARGIN = 0.1
MIN_POINTS_PER_SLIDE = 2
MAX_BULLETS = 4
MAX_PROBLEMS = 12

_EPS = 1e-9


# --- 切窗 ---


@dataclass(frozen=True)
class Window:
    """一次交給模型的一段對談：連續的幾個 Turn，不會切開任何一個 Turn。"""
    index: int
    turns: tuple[Turn, ...]

    @property
    def first_turn(self) -> int:
        return self.turns[0].index

    @property
    def last_turn(self) -> int:
        return self.turns[-1].index

    @property
    def seconds(self) -> float:
        return sum(max(0.0, t.end - t.start) for t in self.turns)


def parse_chunk_chars(raw: str | int | None) -> int:
    """環境變數 SLIDEBOX_CHUNK_CHARS 的值；不是整數或不在 1000–20000 就用預設值。"""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_CHUNK_CHARS
    return value if MIN_CHUNK_CHARS <= value <= MAX_CHUNK_CHARS else DEFAULT_CHUNK_CHARS


def parse_quote_only(raw: str | None) -> bool:
    """環境變數 SLIDEBOX_QUOTE_ONLY 的值：1／true／yes（不分大小寫）才開；沒設、空的、其他值都是關。"""
    return str(raw if raw is not None else "").strip().lower() in _TRUE_WORDS


def citable_ids(speakers: Sequence[SpeakerInfo]) -> frozenset[str]:
    """可以被引用的說話者：主要發言者（不含少數聲音與「無法判斷」）。"""
    return frozenset(s.id for s in speakers if s.kind == PARTICIPANT and s.id != UNDECIDED)


def citable_turn_ids(window: Window, speakers: Sequence[SpeakerInfo]) -> tuple[int, ...]:
    """這個窗裡可以被引用的 turn 編號（由小到大）：模型的 turn 欄位只能從這裡面選。"""
    citable = citable_ids(speakers)
    return tuple(t.index for t in window.turns if t.speaker in citable)


def _line(turn: Turn, citable: frozenset[str]) -> str:
    tag = turn.speaker if turn.speaker in citable else OPAQUE_TAG
    return f"[T{turn.index}] {tag}：{' '.join(turn.text.split())}"


def chunk_turns(
    turns: Sequence[Turn], speakers: Sequence[SpeakerInfo], budget_chars: int = DEFAULT_CHUNK_CHARS,
) -> tuple[Window, ...]:
    """在 Turn 的邊界切成一個個窗；每個 Turn 恰好在一個窗裡，行（標記加文字）不會被切開。

    一個窗的字數（標記加文字）不超過 budget_chars，除非單一個 Turn 就超過——Turn 不切。
    """
    citable = citable_ids(speakers)
    budget = max(1, int(budget_chars))
    windows: list[Window] = []
    current: list[Turn] = []
    size = 0
    for turn in turns:
        cost = len(_line(turn, citable)) + 1          # +1：行與行之間的換行
        if current and size + cost > budget:
            windows.append(Window(len(windows), tuple(current)))
            current, size = [], 0
        current.append(turn)
        size += cost
    if current:
        windows.append(Window(len(windows), tuple(current)))
    return tuple(windows)


def format_window(window: Window, speakers: Sequence[SpeakerInfo]) -> str:
    """給模型看的文字：每段一行 `[T12] S1：文字`。其他聲音與無法判斷的段落標成 X（不能引用）。"""
    citable = citable_ids(speakers)
    return "\n".join(_line(t, citable) for t in window.turns)


# --- 頁數分配 ---


@dataclass(frozen=True)
class Allocation:
    """一個窗要寫幾頁：min_slides..max_slides，至少 1 頁。"""
    window: Window
    min_slides: int
    max_slides: int


def allocate_slides(
    windows: Sequence[Window], min_slides: int, max_slides: int,
) -> tuple[Allocation, ...]:
    """依各窗的說話秒數，把整份的頁數範圍分給每個窗。

    - 太小的窗（分到的最大頁數不足 1 頁）併進相鄰較小的那一個。併完的窗可能略超過字數預算。
    - 每個窗至少 1 頁。各窗下限加起來 >= min_slides（至少窗數）、上限加起來 == max_slides，
      所以模型照範圍寫，總頁數不會超出 [min, max]；每個窗 1 <= 下限 <= 上限。
    - 用 Sainte-Laguë 除數法逐個分頁（先每窗 1 頁）：頁數越多每窗拿到的只增不減，
      所以下限一定不會超過上限。直接 ceil(min×占比)／floor(max×占比) 在 min 與 max 很接近時
      會出現下限大於上限。
    """
    if not windows:
        return ()
    low = max(1, int(min_slides))
    high = max(low, int(max_slides))

    groups = [[w] for w in windows]
    while len(groups) > 1:
        weights = _weights(groups)
        total = sum(weights)
        tiny = min(range(len(groups)), key=lambda i: (weights[i], i))
        if weights[tiny] / total * high >= 1 - _EPS:
            break
        neighbours = [j for j in (tiny - 1, tiny + 1) if 0 <= j < len(groups)]
        other = min(neighbours, key=lambda j: (weights[j], j))
        first, second = sorted((tiny, other))
        groups[first:second + 1] = [groups[first] + groups[second]]

    merged = tuple(Window(i, tuple(t for w in g for t in w.turns)) for i, g in enumerate(groups))
    weights = _weights(groups)
    order = _seat_order(weights, high - len(merged))
    mins = [1] * len(merged)
    maxs = [1] * len(merged)
    for k, position in enumerate(order):
        maxs[position] += 1
        if k < low - len(merged):
            mins[position] += 1
    return tuple(Allocation(w, lo, hi) for w, lo, hi in zip(merged, mins, maxs))


def _weights(groups: Sequence[Sequence[Window]]) -> list[float]:
    seconds = [sum(w.seconds for w in g) for g in groups]
    if sum(seconds) > 0:
        return seconds
    sizes = [float(sum(len(w.turns) for w in g)) for g in groups]
    return sizes if sum(sizes) > 0 else [1.0] * len(groups)


def _seat_order(weights: Sequence[float], extra: int) -> list[int]:
    """每窗已有 1 頁；依 Sainte-Laguë 商數（權重 / (2×已有頁數+1)）決定多出來的頁依序給誰。"""
    seats = [1] * len(weights)
    order: list[int] = []
    for _ in range(max(0, extra)):
        best = max(range(len(weights)), key=lambda i: (weights[i] / (2 * seats[i] + 1), -i))
        seats[best] += 1
        order.append(best)
    return order


# --- 檢查模型提出的重點 ---

# 為什麼丟掉的點：穩定的碼（記錄、報告與測試用；原因的文字可以改，碼不要改）
TOO_SHORT = "too_short"
TOO_LONG = "too_long"
MARKER_IN_TEXT = "marker_in_text"
LABEL_WORD = "label_word"
LEADING_PRONOUN_OR_TITLE = "leading_pronoun_or_title"
PRONOUN_IN_TEXT = "pronoun_in_text"
FIRST_PERSON = "first_person"
NAME_IN_TEXT = "name_in_text"
TURN_NOT_INT = "turn_not_int"
TURN_OUT_OF_WINDOW = "turn_out_of_window"
CITES_X_OR_MINOR = "cites_x_or_minor"
BAD_QUOTE = "bad_quote"
SHORT_ANSWER_NO_CONTEXT = "short_answer_no_context"
SHORT_ANSWER_CONTEXT_MISMATCH = "short_answer_context_mismatch"
SHORT_ANSWER_STANCE = "short_answer_stance"
UNSUPPORTED = "unsupported"
ANCHOR_CONFLICT = "anchor_conflict"
CROSS_SPEAKER_TIE = "cross_speaker_tie"
SOURCE_TURN_NOT_CITABLE = "source_turn_not_citable"
NUMBER_MISMATCH = "number_mismatch"
NEGATION_MISMATCH = "negation_mismatch"
QUESTION_STATED_AS_FACT = "question_stated_as_fact"
ECHO = "echo"
BAD_INLINE_QUOTE = "bad_inline_quote"
DUPLICATE_POINT = "duplicate_point"
SLIDE_TOO_SMALL = "slide_too_small"

# 丟掉的點分三類，報告與記錄分開列（FINAL 的 contradicted／unverifiable／slide rule）：
CONTRADICTED = "contradicted"   # 檢查發現文字有問題：數字、否定、問句、稱呼、複述、歸屬衝突…
UNVERIFIABLE = "unverifiable"   # 查不出來：引文不是某一輪的原文，或內容在附近找不到依據
SLIDE_RULE = "slide_rule"       # 點本身沒問題，但在同一頁重複、或那一頁留下的點太少
UNVERIFIABLE_CODES = frozenset({BAD_QUOTE, UNSUPPORTED})
SLIDE_RULE_CODES = frozenset({DUPLICATE_POINT, SLIDE_TOO_SMALL})


def problem_class(code: str) -> str:
    """丟掉的原因碼屬於哪一類：CONTRADICTED／UNVERIFIABLE／SLIDE_RULE。"""
    if code in UNVERIFIABLE_CODES:
        return UNVERIFIABLE
    if code in SLIDE_RULE_CODES:
        return SLIDE_RULE
    return CONTRADICTED


@dataclass(frozen=True)
class Problem:
    """一個點為什麼被丟掉：code 是穩定的碼，message 是繁體中文的原因（可以原樣放進重試提示）。"""
    code: str
    message: str


@dataclass(frozen=True)
class PointCheck:
    """_check_point 的結果：通過就有 point，沒通過就有 problem。

    rescued 表示這個點顯示的是逐字稿裡的原話（見 _rescue_quote），不是模型的改寫；
    note 是「引用的 turn 沒有 quote 的原文，已改引有的那一輪」這類不影響結果的說明。
    """
    point: Point | None = None
    problem: Problem | None = None
    note: str | None = None
    rescued: bool = False


@dataclass(frozen=True)
class CheckedSlide:
    """通過檢查的一頁：標題與留下來的重點（依被引用的 turn 排序）；rescued 是其中顯示原話的點數。"""
    title: str
    points: tuple[Point, ...]
    rescued: int = 0


@dataclass(frozen=True)
class ValidationResult:
    """slides 是留下來的頁；proposed／kept 是模型提出的點數與留下的點數；
    problems 是丟掉或改引的原因（繁體中文），可以原樣當作重試時的提示。

    丟掉的點分三類：contradicted（檢查發現有問題）、unverifiable（查不出來）、slide_rule（同頁重複或整頁
    留不下來），proposed − kept 永遠等於三類的和（超過每頁 MAX_BULLETS 的點在計數前就已經扣掉）。
    anchor_conflicts 是 contradicted 裡「內容出自別輪，與 quote 所在的輪不一致」的點數；
    rescued 是留下的點裡顯示原話的點數；dropped_by_code 是每個原因碼丟了幾個。
    """
    slides: tuple[CheckedSlide, ...]
    proposed: int
    kept: int
    problems: tuple[str, ...]
    rescued: int = 0
    contradicted: int = 0
    unverifiable: int = 0
    slide_rule: int = 0
    anchor_conflicts: int = 0
    dropped_by_code: tuple[tuple[str, int], ...] = ()


# 程式用的標記與角色字眼
_TURN_TAG_RE = re.compile(r"\[\s*T\d+\s*\]", re.IGNORECASE)
_SPEAKER_TAG_RE = re.compile(r"(?<![0-9A-Za-z])S\d+(?![0-9A-Za-z])")
_LABEL_WORDS = (
    "發言者", "說話者", "講者", "主持人", "來賓", "嘉賓", "受訪者", "被訪者", "與談人", "與談者",
    "提問者", "回答者", "回應者", "主講人", "主講者", "其他聲音",
)
# 引號裡是引述原話（另外核對過），不算
_QUOTED_RE = re.compile(r"「[^」]*」|『[^』]*』|“[^”]*”|\"[^\"]*\"")
_QUOTE_BODY_RE = re.compile(r"「([^」]*)」|『([^』]*)』|“([^”]*)”|\"([^\"]*)\"")
# 「我國」「自我」是一般用語，不是視角問題（與單人模式的判斷一致）
_FIRST_PERSON_RE = re.compile(r"我們|我方|本席|咱們|咱|本人|在下|敝|(?<!自)我(?![國們方])")

_SURNAMES = ("陳林黃張李王吳劉蔡楊許鄭謝郭洪邱曾廖賴徐周葉蘇莊呂江何蕭羅高潘朱簡鍾彭游詹胡施沈余趙盧梁顏柯孫魏"
             "翁戴范宋方鄧杜傅侯曹薛丁卓阮馬董溫唐藍蔣石古紀姚連馮歐程湯田康姜汪白鄒尤巫黎塗龔嚴韓袁金童陸夏柳邵")
_TITLES = ("委員|部長|院長|市長|縣長|鄉長|鎮長|區長|里長|局長|處長|署長|廳長|科長|議員|議長|主席|主任|秘書長|"
           "總統|副總統|總理|校長|教授|老師|博士|醫師|醫生|律師|法官|檢察官|董事長|理事長|執行長|總經理|"
           "先生|小姐|女士|立委")
_MAJOR_TITLES = "部長|院長|署長|廳長|總統|副總統|總理|董事長|理事長|執行長|總經理|秘書長|議長|主席"
# 「委員會」「長期」「長大」這類普通詞不是稱呼
_NOT_A_TITLE = "(?![會期久遠大度短途年時])"
_PRONOUN = r"(?:他們|她們|牠們|(?:他|她|牠)(?![人山方鄉])|對方|此人|該員|該名|該位)"
_LEADING_RE = re.compile(
    rf"^(?:{_PRONOUN}"
    rf"|(?:[{_SURNAMES}][一-鿿]{{0,2}})?(?:{_TITLES}){_NOT_A_TITLE}"
    rf"|[一-鿿]{{1,5}}(?:{_MAJOR_TITLES}){_NOT_A_TITLE})")
_PRONOUN_RE = re.compile(_PRONOUN)
_PRONOUN_SAFE_RE = re.compile(r"其他|其它|他人")          # 這些詞只是剛好含有「他」
_LEAD_STRIP = "-•・*· 　\t"

# 內容相符：兩個字一組（bigram），這些是虛字，兩個都是虛字的組合不算
_STOP = frozenset("的了是在和與也有就都要會能不而並及或這那其之於為把被讓對向從到所以我你他她它您們")
_TOKEN_RE = re.compile(r"[0-9a-z]+|[㐀-鿿]+")

# 「不同」「不少」「是否」「未來」「非常」這類普通詞不是否定
_NEGATION_RE = re.compile(r"不(?![僅只但管論過如妨同少斷久時錯然勝禁])|沒|無(?!論)|非(?!常)|未(?!來)|(?<!是)否(?!則)")
_NEGATION_CHARS = "不沒無非未否"

# 很短的回答：整段只有「對」「不是」這類字。內容要靠 context 補
_YES_RE = re.compile(r"(?:對|是|好|行|沒錯|對的|是的|可以|當然|正確|同意|贊成|有)+")
_NO_RE = re.compile(r"(?:不是|不對|沒有|沒|不會|不行|不可以|不能|不好|不要|不用|反對|不同意|不贊成|並沒有|並非)+")
_YES_LEADS = (
    "同意", "認同", "贊成", "支持", "確認", "肯定", "承認", "答應", "接受", "認為", "表示同意", "表示贊成",
    "表示支持", "表示認同", "表示可以", "回答是", "回答對", "回答可以", "回答有", "回應是", "回應可以",
)
_NO_LEADS = (
    "不同意", "不贊成", "不認同", "不認為", "不支持", "不接受", "反對", "否認", "否定", "拒絕", "並非",
    "沒有", "表示不", "表示沒有", "表示反對", "表示否認", "回答不", "回答沒有", "回應不", "回應沒有",
)

# 問句與複述：問句要寫成「詢問…」，複述別人的話不能算在複述的人頭上
_QUESTION_RE = re.compile(r"[？?]|嗎|呢|是不是|對不對|好不好|行不行|有沒有|能不能|可不可以|是否|對吧|是吧")
_ASK_RE = re.compile(
    r"詢問|追問|請教|請問|提問|質疑|質問|反問|想知道|想了解|問(?!題)|是否|能否|可否|有沒有|是不是|對不對|"
    r"如何|怎麼|怎樣|為何|為什麼|什麼|哪|多少|嗎|呢")
_RESTATING_RE = re.compile(
    r"[？?]|嗎|呢|是不是|對不對|對吧|是吧|是否|有沒有|"
    r"剛才|剛剛|方才|您說|你說|妳說|您提到|你提到|他說|她說|他們說|所說|意思是|也就是說|換句話說|"
    r"你是說|您是說|說過|[「『“]")

_CLAUSE_RE = re.compile(r"([^，。！？；：,.!?;:…]+)([，。！？；：,.!?;:…]*)")
_TRAILING_PUNCT = "，。！？；：,.!?;:… \t　"
_TEXT_TRAILING = "。．.；;！!？?，,、 \t　"

# 改用原話時，原話裡不可以有這些（比一般文字多一道：原話是別人說的話，代名詞指到誰就不確定）
_RESCUE_PRONOUN_RE = re.compile(r"他們|她們|它們|牠們|他|她|它|牠|對方|雙方|該員|此人|該名|該位")
_RESCUE_PRONOUN_SAFE_RE = re.compile(r"其他|其它|他人")        # 這些詞只是剛好含有「他」「它」
_RESCUE_ROLE_RE = re.compile(r"發言者|主持人|來賓|講者|受訪者|(?<![登書筆])記者(?!會)|委員(?!會)|部長|議員|教授")


def validate_points(
    raw_slides: Sequence[RawSlide],
    turns: Sequence[Turn],
    speakers: Sequence[SpeakerInfo],
    window: Window,
    quote_only: bool = False,
) -> ValidationResult:
    """逐點檢查模型提出的重點；說話者由程式從 quote 所在的那一輪查出來。

    每個點依序過這些關（任何一關沒過，這個點整個丟掉，原因寫進 problems）：
    1. 文字不能有標記／角色字眼／代名詞或姓名職稱／第一人稱，也不能有逐字稿用來稱呼人的名字（「」裡面的不算）；
    2. 引用的 turn 必須在這個窗裡、說話者是主要發言者（不是其他聲音也不是無法判斷）；
    3. quote（照抄的原文）要依序出現在**同一個**可引用的 turn 裡：引用的那一輪有就用它，只有別的某一輪有就
       改用那一輪（模型把問題的編號填在答案上），都沒有或好幾輪都有就丟掉（bad_quote）；
    4. 很短的回答（對、不是…）要附上它回應的上文（context），重點只能寫成表態；
    5. 內容要有 quote 那一輪（前後 3 輪內）的依據；依據在別輪、或兩位說話者一樣符合就丟掉；
       完全找不到依據時，如果 quote 本身夠格（連續的一段、不含稱呼與代名詞、不是在複述對方求證）就改顯示那段原話；
    6. 數字要和那段話一致（比數值與單位）、否定要一致、問句要寫成詢問、複述別人話的那一輪不算複述者的主張、
       「」裡的引文要逐字出現在同一位說話者的某一輪裡。
    每頁留下的點少於 2 個就整頁丟掉；超過 4 個的點在計數前就扣掉。

    quote_only=True（安全模式）：每個留下的點都顯示 quote 的原話，不看模型寫的文字；quote 夠格才留。
    """
    by_index = {t.index: t for t in turns}
    first, last = window.first_turn, window.last_turn
    in_window = [by_index[i] for i in range(first, last + 1) if i in by_index]
    citable = citable_ids(speakers)
    names = detect_names("\n".join(t.text for t in in_window))

    problems: list[str] = []
    codes: Counter[str] = Counter()
    slides: list[CheckedSlide] = []
    proposed = 0
    for page, raw in enumerate(raw_slides, 1):
        kept: list[tuple[Point, bool]] = []
        seen: set[tuple[str, str]] = set()
        for number, raw_point in enumerate(raw.points, 1):
            proposed += 1
            where = f"第 {page} 頁第 {number} 點"
            check = _check_point(raw_point, by_index, in_window, citable, first, last, names, quote_only)
            if check.point is None:
                assert check.problem is not None                 # 沒有點就一定有原因
                codes[check.problem.code] += 1
                problems.append(f"{where}：{check.problem.message}")
                continue
            if check.note:
                problems.append(f"{where}：{check.note}")
            key = (check.point.speaker, squash(check.point.text))
            if key in seen:
                codes[DUPLICATE_POINT] += 1
                problems.append(f"{where}：與同一頁的另一點重複")
                continue
            seen.add(key)
            kept.append((check.point, check.rescued))
        if len(kept) < MIN_POINTS_PER_SLIDE:
            codes[SLIDE_TOO_SMALL] += len(kept)
            problems.append(f"第 {page} 頁只剩 {len(kept)} 個通過檢查的重點，至少要 {MIN_POINTS_PER_SLIDE} 個，整頁不用")
            continue
        kept.sort(key=lambda item: item[0].turn)
        if len(kept) > MAX_BULLETS:                              # 多出來的點不會顯示，也不算提出過
            proposed -= len(kept) - MAX_BULLETS
            kept = kept[:MAX_BULLETS]
        title_problem = _title_problem(raw.title)
        if title_problem:
            problems.append(f"第 {page} 頁的標題{title_problem}，改用中性標題")
            title = f"{clock(kept[0][0].start)} 起的討論"
        else:
            title = " ".join(raw.title.split())
        slides.append(CheckedSlide(title, tuple(point for point, _ in kept),
                                   rescued=sum(1 for _, rescued in kept if rescued)))
    if not raw_slides:
        problems.append("沒有提出任何一頁")
    classes: Counter[str] = Counter()
    for code, count in codes.items():
        classes[problem_class(code)] += count
    return ValidationResult(
        slides=tuple(slides), proposed=proposed,
        kept=sum(len(s.points) for s in slides), problems=_cap(problems),
        rescued=sum(s.rescued for s in slides),
        contradicted=classes[CONTRADICTED], unverifiable=classes[UNVERIFIABLE], slide_rule=classes[SLIDE_RULE],
        anchor_conflicts=codes[ANCHOR_CONFLICT],
        dropped_by_code=tuple(sorted(codes.items(), key=lambda item: (-item[1], item[0]))))


def _cap(problems: list[str]) -> tuple[str, ...]:
    if len(problems) <= MAX_PROBLEMS:
        return tuple(problems)
    return (*problems[:MAX_PROBLEMS], f"（另有 {len(problems) - MAX_PROBLEMS} 項問題未列出）")


def _check_point(
    raw: RawPoint,
    by_index: Mapping[int, Turn],
    in_window: Sequence[Turn],
    citable: frozenset[str],
    first: int,
    last: int,
    names: frozenset[str] = frozenset(),
    quote_only: bool = False,
) -> PointCheck:
    """逐關檢查一個點（關卡與順序見 validate_points）。

    順序是刻意的：先擋文字本身的問題，再用 quote 決定這個點屬於哪一輪（也就是誰說的），之後才看內容；
    內容檢查都對著「quote 所在的那一輪」，所以不會有「quote 在甲說的話裡、內容卻對著乙說的話」的點通過。
    """
    text = _clean_text(raw.text)
    if not quote_only:                      # 只顯示原話時，模型寫的文字不會顯示，不用檢查
        problem = _text_problem(text) or _name_problem(text, names)
        if problem:
            return PointCheck(problem=problem)

    problem = _window_problem(raw.turn, first, last, by_index)
    if problem:
        return PointCheck(problem=problem)
    cited = by_index[raw.turn]
    problem = _citable_problem(cited, citable)
    if problem:
        return PointCheck(problem=problem)

    located = locate_quote(raw.quote, cited, in_window, citable)
    if located.turn is None:
        return PointCheck(problem=Problem(BAD_QUOTE, located.problem or "quote 核對不過"))
    turn = located.turn                     # 說話者從這一輪查，不從模型填的編號查
    note = None
    if turn.index != cited.index:
        note = f"引用的 T{cited.index} 沒有 quote 的原文，已改引有這段原文的 T{turn.index}"
    if quote_only:
        shown = _quote_to_show(turn, raw.quote, names)
        if isinstance(shown, str):
            return PointCheck(problem=Problem(
                BAD_QUOTE, f"quote 不能直接當重點顯示：{shown}；請抄一個完整、連續、不含稱呼與代名詞的子句"))
        return PointCheck(point=_quoted_point(turn, shown), note=note, rescued=True)

    claim = _grams(text)
    polarity = _answer_polarity(turn.text)
    if polarity:
        anchor = turn
        context = [by_index[i] for i in _context_ids(raw, turn, first, by_index)]
        problem = _answer_problem(polarity, text, claim, context)
        if problem:
            return PointCheck(problem=problem)
        support_text = " ".join(t.text for t in (*context, turn))
        earlier: list[Turn] = []                      # 回答的字是「對」，沒有可複述的內容
    else:
        radius = [by_index[i] for i in range(max(first, turn.index - ANCHOR_RADIUS),
                                             min(last, turn.index + ANCHOR_RADIUS) + 1) if i in by_index]
        picked = _pick_anchor(turn, claim, radius, citable)
        if isinstance(picked, Problem):
            if picked.code == UNSUPPORTED:
                return _rescue_quote(turn, raw.quote, names, note, picked)
            return PointCheck(problem=picked)
        anchor = picked
        problem = _anchor_problem(anchor, turn)
        if problem:
            return PointCheck(problem=problem)
        support_text = anchor.text
        earlier = [t for t in in_window if t.index < anchor.index]

    problem = _number_problem(text, support_text) or _negation_problem(text, support_text)
    if problem:
        return PointCheck(problem=_at(problem, anchor))

    evidence, is_question = _evidence(anchor.text, claim)
    problem = _question_problem(is_question, text)
    if problem:
        return PointCheck(problem=_at(problem, anchor))

    source = _echo_source(anchor, claim, earlier)
    if source is not None:
        return PointCheck(problem=Problem(ECHO, (
            f"T{anchor.index} 是在複述 T{source.index} 另一位說話者的話，不是這位說話者自己的主張")))

    problem = _quote_problem(text, [t for t in in_window if t.speaker == anchor.speaker])
    if problem:
        return PointCheck(problem=problem)

    point = Point(speaker=anchor.speaker, turn=anchor.index, start=anchor.start,
                  text=text, evidence=evidence)
    return PointCheck(point=point, note=note)


def _at(problem: Problem, turn: Turn) -> Problem:
    return Problem(problem.code, f"{problem.message}（T{turn.index}）")


def _anchor_problem(anchor: Turn, quote_turn: Turn) -> Problem | None:
    """內容的依據（anchor）必須就是 quote 所在的那一輪。

    依據在別輪，表示模型把 A 說的話抄進 quote、卻把 B 說的事寫進重點（或反過來）：這個重點歸給誰都可能是錯的，
    丟掉。絕不只靠「內容比較像」把重點改歸給另一位說話者。
    """
    if anchor.index == quote_turn.index:
        return None
    return Problem(ANCHOR_CONFLICT, (
        f"重點的內容出自別輪發言（T{anchor.index}），與 quote 所在的 T{quote_turn.index} 不一致；"
        f"一個重點只寫 quote 所在那一輪說的事"))


def _clean_text(text: object) -> str:
    """模型寫的文字整理成單行；不是文字（None、數字…）就當作空的，不要變成「None」這個詞。"""
    if not isinstance(text, str):
        return ""
    flat = " ".join(text.split()).lstrip(_LEAD_STRIP)
    return flat.rstrip(_TEXT_TRAILING)


# --- 改用原話（混合條列）---


def _rescue_quote(turn: Turn, quote: str, names: frozenset[str], note: str | None,
                  unsupported: Problem) -> PointCheck:
    """內容在附近找不到依據（模型的改寫和原文的用字對不上），但 quote 本身核對過了：改顯示那一段原話。

    這是丟掉之外唯一的出路，所以條件很嚴（見 _quote_to_show）：顯示的是逐字稿裡**連續的一段原話**，
    不是模型寫的字；說話者是那一輪的說話者。條件不符就照原樣丟掉（unsupported）。
    只有「內容找不到依據」這一個原因的點才會走到這裡；數字、否定、問句、複述、稱呼的問題都不會被救回來。
    """
    shown = _quote_to_show(turn, quote, names)
    if isinstance(shown, str):
        return PointCheck(problem=Problem(UNSUPPORTED, f"{unsupported.message}（也不能改用原話：{shown}）"))
    return PointCheck(point=_quoted_point(turn, shown), note=note, rescued=True)


def _quote_to_show(turn: Turn, quote: str, names: frozenset[str]) -> QuoteSpan | str:
    """quote 能不能當作原話直接顯示：能就回傳那一段（逐字稿的原文位置），不能就回傳原因。

    條件：是那一輪裡連續的一段（跳過字句的不行：顯示出來像是連著說了那兩句）；至少 8 個字、
    不超過 MAX_RESCUED_QUOTE_CHARS 個字；沒有代名詞、角色字眼、稱呼說話者的字眼、逐字稿裡用來稱呼人的名字；
    不是「你剛才說…」開頭；不是複述對方、等對方確認的那半句（單獨顯示會變成這個人自己的主張）。
    原話裡的第一人稱不算問題：那是他自己說的話。
    """
    span = contiguous_span(turn.text, quote)
    if span is None:
        return f"quote 不是 T{turn.index} 裡連續的一段原話（中間跳過了字句）"
    if len(squash(span.text)) < MIN_QUOTE_CHARS:
        return f"原話不到 {MIN_QUOTE_CHARS} 個字，太短"
    if len(span.text) > MAX_RESCUED_QUOTE_CHARS:
        return f"原話超過 {MAX_RESCUED_QUOTE_CHARS} 個字，太長"
    word = _wording_hit(span.text, names)
    if word:
        return f"原話裡有「{word}」（代名詞、稱謂或逐字稿裡用來稱呼人的名字）"
    if ECHO_START_RE.search(span.text):
        return "原話一開頭就是在轉述對方（你剛才說…）"
    if is_restatement(span):
        return "原話是複述對方、等對方確認的那半句，單獨顯示會變成這個人自己的主張"
    return span


def _wording_hit(text: str, names: frozenset[str]) -> str | None:
    """原話裡第一個不能顯示的字眼（代名詞、角色字眼、稱呼說話者的字眼、逐字稿裡的名字）；沒有回傳 None。"""
    flat = unicodedata.normalize("NFKC", text)
    for pattern, searched in ((_RESCUE_PRONOUN_RE, _RESCUE_PRONOUN_SAFE_RE.sub("", flat)),
                              (_RESCUE_ROLE_RE, flat)):
        match = pattern.search(searched)
        if match:
            return match.group(0)
    for word in _LABEL_WORDS:
        if word in flat:
            return word
    return next((name for name in sorted(names) if name in text), None)


def _quoted_point(turn: Turn, span: QuoteSpan) -> Point:
    """顯示原話的點：文字是「原話」，證據是那一輪裡最貼近的一句。"""
    evidence, _ = _evidence(turn.text, _grams(span.text))
    return Point(speaker=turn.speaker, turn=turn.index, start=turn.start,
                 text=f"「{span.text}」", evidence=evidence)


# --- 文字本身的檢查 ---


def _text_problem(text: str) -> Problem | None:
    if len(squash(text)) < MIN_POINT_CHARS:
        return Problem(TOO_SHORT, "重點太短或是空的")
    if len(text) > MAX_POINT_CHARS:
        return Problem(TOO_LONG, f"重點超過 {MAX_POINT_CHARS} 個字，請縮短")
    for check in (_marker_problem, _label_problem, _leading_problem, _first_person_problem):
        problem = check(text)
        if problem:
            return problem
    return None


def _title_problem(title: str) -> str | None:
    flat = " ".join(str(title).split())
    if not flat:
        return "是空的"
    if len(flat) > MAX_TITLE_CHARS:
        return f"超過 {MAX_TITLE_CHARS} 個字"
    for check in (_marker_problem, _label_problem, _leading_problem, _first_person_problem):
        if check(flat):
            return "含有稱呼、代名詞或標記"
    return None


def _outside_quotes(text: str) -> str:
    return _QUOTED_RE.sub("", text)


def _marker_problem(text: str) -> Problem | None:
    if "[[" in text or "]]" in text or _TURN_TAG_RE.search(text) or _SPEAKER_TAG_RE.search(text):
        return Problem(MARKER_IN_TEXT, "文字裡不可以有 [[ ]]、[T編號]、S1 這類標記，只寫內容")
    return None


def _label_problem(text: str) -> Problem | None:
    outside = _outside_quotes(text)
    for word in _LABEL_WORDS:
        if word in outside:
            return Problem(LABEL_WORD, f"文字裡有「{word}」；不要稱呼說話者，寫成沒有主詞的短句")
    return None


def _leading_problem(text: str) -> Problem | None:
    """文字以代名詞、姓名或職稱開頭，或整段文字（「」以外）任何地方有代名詞。"""
    head = text.lstrip(_LEAD_STRIP)
    match = _LEADING_RE.match(head)
    if match:
        return Problem(LEADING_PRONOUN_OR_TITLE, (
            f"文字以「{match.group(0)}」開頭；不要用代名詞、姓名或職稱當主詞，寫成沒有主詞的短句"))
    match = _PRONOUN_RE.search(_PRONOUN_SAFE_RE.sub("", _outside_quotes(head)))
    if match:
        return Problem(PRONOUN_IN_TEXT, f"文字裡有代名詞「{match.group(0)}」；不要用代名詞，寫成沒有主詞的短句")
    return None


def _first_person_problem(text: str) -> Problem | None:
    outside = _outside_quotes(text)
    match = _FIRST_PERSON_RE.search(outside)
    if match:
        start, end = max(0, match.start() - 8), min(len(outside), match.end() + 8)
        return Problem(FIRST_PERSON, (
            f"用了第一人稱「{match.group(0)}」（…{outside[start:end]}…）；"
            f"請寫成沒有主詞的短句，要引述原話請放在「」裡"))
    return None


def _name_problem(text: str, names: frozenset[str]) -> Problem | None:
    """文字（「」以外）裡有逐字稿用來稱呼人的名字或稱謂（見 names.detect_names）。"""
    outside = _outside_quotes(text)
    hit = next((name for name in sorted(names) if name in outside), None)
    if hit:
        return Problem(NAME_IN_TEXT, f"文字裡有逐字稿用來稱呼人的名字或稱謂「{hit}」；不要寫進去，寫成沒有主詞的短句")
    return None


# --- 引用的 turn ---


def _window_problem(value: object, first: int, last: int, by_index: Mapping[int, Turn]) -> Problem | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return Problem(TURN_NOT_INT, "turn 必須是整數編號")
    if value < first or value > last or value not in by_index:
        return Problem(TURN_OUT_OF_WINDOW, f"引用的 T{value} 不在這一段範圍（T{first}–T{last}）")
    return None


def _citable_problem(turn: Turn, citable: frozenset[str]) -> Problem | None:
    if turn.speaker not in citable:
        return Problem(CITES_X_OR_MINOR, f"T{turn.index} 是無法判斷或其他聲音（X），不能引用")
    return None


def _context_ids(raw: RawPoint, cited: Turn, first: int, by_index: Mapping[int, Turn]) -> list[int]:
    """很短的回答所附的上文：必須在窗裡、在回答之前、離回答不超過 ANCHOR_RADIUS 段。"""
    seen: list[int] = []
    for value in raw.context:
        if isinstance(value, bool) or not isinstance(value, int) or value in seen:
            continue
        if max(first, cited.index - ANCHOR_RADIUS) <= value < cited.index and value in by_index:
            seen.append(value)
    return sorted(seen)


# --- 內容相符 ---


@lru_cache(maxsize=8192)
def _grams(text: str) -> frozenset[str]:
    """內容的特徵：中文兩個字一組（兩個都是虛字的不算）、英數字一個詞一組，標點隔開不連。"""
    out: set[str] = set()
    for token in _TOKEN_RE.findall(unicodedata.normalize("NFKC", text).lower()):
        if token.isascii():
            if len(token) >= 2 or token.isdigit():
                out.add(token)
            continue
        for i in range(len(token) - 1):
            pair = token[i:i + 2]
            if not (pair[0] in _STOP and pair[1] in _STOP):
                out.add(pair)
    return frozenset(out)


def _is_supported(shared: int, total: int) -> bool:
    """共同的特徵至少 2 個，且至少占重點特徵的 25%。"""
    return total > 0 and shared >= MIN_SHARED_GRAMS and shared >= MIN_SHARED_RATIO * total - _EPS


def _pick_anchor(
    turn: Turn, claim: frozenset[str], nearby: Sequence[Turn], citable: frozenset[str],
) -> Turn | Problem:
    """quote 所在的那一輪（turn）是不是真的有這個重點的依據：是就回傳它，否則回傳原因。

    - 前後幾段裡沒有任何一段夠符合 → unsupported（內容沒有依據；可能改用原話，見 _rescue_quote）。
    - quote 那一輪和最符合的差不多好（差距在 TIE_MARGIN 內）→ 用它。
    - 有別段明顯更符合：兩位以上說話者一樣符合就丟掉；最符合的那段不是可引用的說話者也丟掉；
      否則回傳那一段（呼叫的人會把它當成「內容出自別輪」的衝突）。
    """
    total = len(claim)
    shared = {t.index: len(claim & _grams(t.text)) for t in nearby}
    supported = [t for t in nearby if _is_supported(shared[t.index], total)]
    if not supported:
        return Problem(UNSUPPORTED, f"內容在 T{turn.index} 前後幾段話裡都找不到依據；請只寫那段話說的事")
    best = max(shared[t.index] for t in supported)
    top = [t for t in supported if shared[t.index] >= best - TIE_MARGIN * total - _EPS]
    if any(t.index == turn.index for t in top):
        return turn
    if len({t.speaker for t in top}) > 1:
        ids = "、".join(f"T{t.index}" for t in top)
        return Problem(CROSS_SPEAKER_TIE, f"{ids} 是不同說話者的話，同樣符合這個重點，無法確定是誰說的")
    winner = max(top, key=lambda t: (shared[t.index], -abs(t.index - turn.index), -t.index))
    problem = _citable_problem(winner, citable)
    if problem:
        return Problem(SOURCE_TURN_NOT_CITABLE, f"內容其實出自 T{winner.index}：{problem.message}")
    return winner


def _echo_source(anchor: Turn, claim: frozenset[str], earlier: Sequence[Turn]) -> Turn | None:
    """被引用的那段是不是在複述別人先說過的話（問「您剛才說…對嗎」、引述對方）。

    條件：那段話有問句或引述的語氣，而且更早有另一位說話者的一段話同樣（或更）符合這個重點。
    這種情況重點是被複述的人主張的，不能算在複述的人頭上。
    """
    if not _RESTATING_RE.search(anchor.text):
        return None
    total = len(claim)
    mine = len(claim & _grams(anchor.text))
    for turn in reversed(earlier):
        if turn.speaker == anchor.speaker:
            continue
        theirs = len(claim & _grams(turn.text))
        if _is_supported(theirs, total) and theirs >= mine:
            return turn
    return None


# --- 數字、否定、問句、回答 ---


def _number_problem(text: str, support_text: str) -> Problem | None:
    """重點裡的數字要和那段話的數字同值、同種類（見 numerals.number_problem）。"""
    message = number_problem(text, support_text)
    return Problem(NUMBER_MISMATCH, message) if message else None


def _negation_problem(text: str, support_text: str) -> Problem | None:
    """重點有否定（不、沒、無…），被引用的那段話卻一個否定字都沒有：多半是把意思寫反了。"""
    if _NEGATION_RE.search(_outside_quotes(text)) and not any(ch in support_text for ch in _NEGATION_CHARS):
        return Problem(NEGATION_MISMATCH, "重點有否定的說法，但被引用的那段話沒有，意思可能寫反了")
    return None


def _question_problem(is_question: bool, text: str) -> Problem | None:
    if is_question and not _ASK_RE.search(text):
        return Problem(QUESTION_STATED_AS_FACT, "被引用的是一句問句，重點要寫成「詢問…」「追問…」，不能寫成陳述")
    return None


def _answer_polarity(turn_text: str) -> str | None:
    """整段只有「對」「不是」這類字就回 "yes"／"no"，否則 None（語助詞不算）。"""
    core = "".join(ch for ch in squash(turn_text) if ch not in FILLER_CHARS)
    if not core:
        return None
    if _YES_RE.fullmatch(core):
        return "yes"
    if _NO_RE.fullmatch(core):
        return "no"
    return None


def _answer_problem(polarity: str, text: str, claim: frozenset[str], context: Sequence[Turn]) -> Problem | None:
    if not context:
        return Problem(SHORT_ANSWER_NO_CONTEXT, "被引用的是很短的回答（對、不是…），必須在 context 填上它回應的上文 turn")
    shared = len(claim & _grams(" ".join(t.text for t in context)))
    if not _is_supported(shared, len(claim)):
        return Problem(SHORT_ANSWER_CONTEXT_MISMATCH, "內容和它回應的上文（context）對不上")
    leads = _YES_LEADS if polarity == "yes" else _NO_LEADS
    if not text.startswith(leads):
        want = "「同意…」「認同…」" if polarity == "yes" else "「不同意…」「否認…」"
        return Problem(SHORT_ANSWER_STANCE, (
            f"被引用的是很短的{'肯定' if polarity == 'yes' else '否定'}回答，重點只能寫成表態，例如{want}"))
    return None


# --- 證據與引文 ---


def _evidence(turn_text: str, claim: frozenset[str]) -> tuple[str, bool]:
    """被引用的那段話裡最貼近重點的原文（<= 80 字），以及重點是不是在說一句問句。

    「是不是問句」只看帶著重點的句子（和重點共用字的數量不到最多的那句一半的不算）：整段對談裡的一句
    反問（「這樣公平嗎？」）不能讓同一段話裡其他陳述被當成問句。這個判斷只會把「是問句」改成「不是」，
    不會反過來。
    """
    flat = " ".join(turn_text.split())
    clauses = [(body, punct) for body, punct in _CLAUSE_RE.findall(flat) if body.strip()]
    if not clauses:
        return _clip(flat, MAX_EVIDENCE_CHARS), False
    scores = [len(claim & _grams(body)) for body, _ in clauses]
    hits = [i for i, s in enumerate(scores) if s > 0]
    low, high = (hits[0], hits[-1]) if hits else (0, len(clauses) - 1)
    if len(_join(clauses, low, high).rstrip(_TRAILING_PUNCT)) > MAX_EVIDENCE_CHARS:
        low, high = _best_window(clauses, scores, low, high)
    span = _join(clauses, low, high)
    evidence = _clip(span.strip().rstrip(_TRAILING_PUNCT), MAX_EVIDENCE_CHARS)
    if not _QUESTION_RE.search(span):
        return evidence, False
    return evidence, _carries_question(clauses, scores)


def _carries_question(clauses: Sequence[tuple[str, str]], scores: Sequence[int]) -> bool:
    """帶著重點的句子裡有沒有問句（和重點共用字的數量不到最多那句一半的句子不算）。"""
    best = max(scores)
    if best <= 0:                                       # 沒有任何一句和重點共用字：照舊看整段
        return True
    carriers = [i for i, s in enumerate(scores) if s * 2 >= best]
    return any(_QUESTION_RE.search(clauses[i][0] + clauses[i][1]) for i in carriers)


def _join(clauses: Sequence[tuple[str, str]], low: int, high: int) -> str:
    return "".join(body + punct for body, punct in clauses[low:high + 1])


def _best_window(clauses: Sequence[tuple[str, str]], scores: Sequence[int], low: int, high: int) -> tuple[int, int]:
    """[low, high] 太長時，在裡面挑放得下 80 字、相符最多的連續幾句（同分取最前面、最短的）。"""
    best = (low, low)
    best_key = (-1, 0, 0)
    for a in range(low, high + 1):
        for b in range(a, high + 1):
            if len(_join(clauses, a, b).rstrip(_TRAILING_PUNCT)) > MAX_EVIDENCE_CHARS:
                break
            key = (sum(scores[a:b + 1]), -a, -(b - a))
            if key > best_key:
                best, best_key = (a, b), key
    return best


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _quotes(text: str) -> list[str]:
    return [next(g for g in m.groups() if g is not None) for m in _QUOTE_BODY_RE.finditer(text)]


def _quote_problem(text: str, speaker_turns: Sequence[Turn]) -> Problem | None:
    """「引文」必須逐字（忽略空白與標點）出現在同一位說話者的某「一」段話裡，且至少 8 個字。"""
    if (text.count("「") != text.count("」") or text.count("『") != text.count("』")
            or text.count("“") != text.count("”") or text.count('"') % 2):
        return Problem(BAD_INLINE_QUOTE, "引號沒有成對")
    for body in _quotes(text):
        squashed = squash(body)
        if len(squashed) < MIN_QUOTE_CHARS:
            return Problem(BAD_INLINE_QUOTE, f"引文「{body}」不到 {MIN_QUOTE_CHARS} 個字，無法核對；請改寫成不帶引號的短句")
        if not any(squashed in squash(t.text) for t in speaker_turns):
            return Problem(BAD_INLINE_QUOTE, f"引文「{body}」不是這位說話者在同一段話裡說的原話（可能是把不同段落接在一起）")
    return None


# --- 排成投影片 ---


def render_slide(index: int, title: str, points: Sequence[Point]) -> Slide:
    """一頁投影片：bullets 是前 4 個重點，detail 是連續同一位說話者的重點併成一句，timestamp 是第一點的時間。

    文字裡的 [[Sn]] 是代號，名字或「發言者 A」之類的標籤到顯示時才換上去（見 render_placeholders）。
    """
    points = tuple(points)
    if not points:
        raise ValueError("投影片至少要有一個重點")
    bullets = tuple(f"[[{p.speaker}]]：{p.text}" for p in points[:MAX_BULLETS])
    runs: list[tuple[str, list[str]]] = []
    for point in points:
        if runs and runs[-1][0] == point.speaker:
            runs[-1][1].append(point.text)
        else:
            runs.append((point.speaker, [point.text]))
    detail = " ".join(f"[[{speaker}]]：{'；'.join(texts)}。" for speaker, texts in runs)
    return Slide(index=index, title=title, bullets=bullets, timestamp=points[0].start,
                 image_path=None, detail=detail, points=points)


_PLACEHOLDER_RE = re.compile(r"\[\[(S\d+)\]\]")


def render_placeholders(
    text: str, speakers: Sequence[SpeakerInfo], names: Mapping[str, str] | None = None,
) -> str:
    """把 [[Sn]] 換成給人看的稱呼：有名字用名字，否則用程式給的標籤（發言者 A…）；S0 是「無法判斷」。"""
    labels = {s.id: s.label for s in speakers}

    def replace(match: re.Match[str]) -> str:
        speaker = match.group(1)
        name = (names or {}).get(speaker, "")
        if name and name.strip():
            return name.strip()
        if speaker == UNDECIDED:
            return "無法判斷"
        return labels.get(speaker, "未知說話者")

    return _PLACEHOLDER_RE.sub(replace, text)
