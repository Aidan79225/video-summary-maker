# 事實查核與查證相符率 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 從立委的發言逐字稿抽出可用官方資料查證的主張，取回法條／議案原文比對，產出附證據的逐則判定與公開公式算出的「查證相符率」。

**Architecture:** GPU 主機新增純邏輯為主的 `src/factcheck/` 套件（抽取 → 落地 → 取證 → 比對），以 `kind = factcheck` 的工作掛進既有 `slidebox_api` 佇列；Pi 上的 Django 新增 `factchecks` app 負責送工作、落地、人工審核「不符」、算分與開 API；Astro 前端新增查證區塊、委員查證相符率與「查證方法」頁。

**Tech Stack:** Python 3.13、FastAPI、Ollama（`qwen3.5:9b`）、LYAPI（`https://ly.govapi.tw/v2`）、Django 5 + django-ninja、Astro 7 + Tailwind 4。

**Spec:** `docs/superpowers/specs/2026-09-19-fact-check-design.md`

## Global Constraints

- LLM 永遠不產生分數、不判斷數字；數字只由 `factcheck/usecases/compare.py` 比對
- 主張的 `quote` 必須是逐字稿的子字串（正規化後），否則丟棄；模型判讀的 `evidence_quote` 必須是證據的子字串，否則改判 `unverifiable`
- 判定值只有 `supported` / `partial` / `contradicted` / `unverifiable`；方法只有 `numeric` / `model` / `none`；主張種類只有 `law_article` / `bill_content` / `bill_status`
- `contradicted` 落地時一律 `review_status = pending_review`，人工核准前不公開、不計分
- 查證相符率：`n = 相符 + 部分相符 + 不符`；`rate = (相符 + 0.5 × 部分相符 + 1) / (n + 2)`；`n < 5` 時 `rate = None`；「無法查證」不計入
- 只查核片段的委員本人（`Article.speaker`）
- LYAPI 的 `q` 一定要用雙引號包住（`q="醫療法"`）
- 法條要取**發言當日有效**的版本；條號是中文數字（`第一百零六條`）
- 每筆證據存 `official_url`（官方頁面）與 `api_url`（LYAPI 紀錄）；法律的官方網址為 `https://law.moj.gov.tw/Law/LawSearchResult.aspx?ty=ONEBAR&kw=<法律名稱 URL 編碼>`
- 程式碼註解、docstring、使用者看得到的字串一律繁體中文，風格比照既有程式碼（註解寫「為什麼」）
- 指令在 Windows 的 Git Bash 執行；根目錄測試 `uv run pytest`；Django 測試在 `services/news` 下 `uv run python manage.py test`；前端在 `web/news` 下 `npx astro check`
- 每個 commit 訊息結尾加上：

```
Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QK5KtdLiAMEyhj5rUoCwRX
```

## File Structure

```
src/factcheck/
  __init__.py
  domain/__init__.py
  domain/entities.py        Speech / ExtractedClaim / Evidence / Judgement / CheckedClaim / 列舉
  domain/errors.py          SourceUnavailable / ModelUnavailable / ModelOutputInvalid / OperationCancelled
  domain/ports.py           ClaimExtractor / EvidenceSource / TextJudge
  usecases/__init__.py
  usecases/numbers.py       中文數字、金額與期間正規化、條號轉換
  usecases/grounding.py     正規化、子字串落地、逐字稿定位時間
  usecases/compare.py       數字比對 → 判定
  usecases/check.py         FactCheckUseCase 編排
  infrastructure/__init__.py
  infrastructure/lyapi.py   LYAPI HTTP client
  infrastructure/law_source.py
  infrastructure/bill_source.py
  infrastructure/ollama.py  抽取器與判讀器
  composition.py
src/slidebox_api/
  jobs.py                   (改) JobKind、Job.kind / Job.params
  runner.py                 (改) KindDispatcher、FactCheckExecutor
  factcheck_payload.py      (新) CheckedClaim → JSON
  app.py                    (改) POST /factchecks、JobView.kind
serve_api.py                (改) 組裝 dispatcher
tests/factcheck/            fixtures/（實測 LYAPI 回應，已存在）＋各模組測試
services/news/factchecks/   Django app：models / scoring / runner / review / admin / api / command / tests
services/news/articles/     (改) gpu_client.submit_factcheck、api.py 掛上查證資料、run_scheduler
web/news/src/               types / lib/factcheck.ts / api.ts / 元件 / 頁面 / 示範資料
scripts/eval_factcheck.py   準確率評估
```

`tests/factcheck/fixtures/` 已經存有實測的 LYAPI 回應（`laws_search_醫療法.json`、`law_versions_02533.json`、`law_contents_02533_現行_subset.json`、`bills_search_無人載具.json`、`bill_201110221870000.json`、`bill_202110223690000.json`），Task 3、4 直接使用。

---

### Task 1: 領域模型與數字正規化

**Files:**
- Create: `src/factcheck/__init__.py`, `src/factcheck/domain/__init__.py`, `src/factcheck/domain/entities.py`, `src/factcheck/domain/errors.py`, `src/factcheck/domain/ports.py`, `src/factcheck/usecases/__init__.py`, `src/factcheck/usecases/numbers.py`
- Test: `tests/factcheck/__init__.py`, `tests/factcheck/test_numbers.py`

**Interfaces:**
- Produces:
  - `factcheck.domain.entities`: `ClaimKind`, `Verdict`, `Method`, `SourceKind`（StrEnum）、`Speech(speaker, date, meeting, transcript)`、`ExtractedClaim(quote, kind, statement, figures=(), law="", article="", bill_keywords="", proposer="")`、`Evidence(source, title, official_url, api_url, excerpt)`、`Judgement(verdict, evidence_quote, reason)`、`CheckedClaim(claim, timestamp, verdict, method, rationale, evidence=())`
  - `factcheck.domain.errors`: `SourceUnavailable`, `ModelUnavailable`, `ModelOutputInvalid`, `OperationCancelled`
  - `factcheck.domain.ports`: `ClaimExtractor.extract(speech) -> list[ExtractedClaim]`、`EvidenceSource.find(claim, on: date) -> list[Evidence]`、`TextJudge.judge(statement, evidence) -> Judgement`、`ProgressCallback`、`CancelCheck`
  - `factcheck.usecases.numbers`: `Unit`、`Quantity(value, unit)`、`parse_chinese_number(text) -> int | None`、`quantities(text) -> list[Quantity]`、`has_approximation(text) -> bool`、`to_chinese(n) -> str`、`article_label(n) -> str`、`article_number(text) -> int | None`

- [ ] **Step 1: 建立空的套件檔**

`src/factcheck/__init__.py`：

```python
"""事實查核：從發言抽出可查證的主張，取回官方資料比對，產出附證據的判定。

分數不在這裡算——那是公開公式的事，放在新聞服務（Pi）上。
"""
```

`src/factcheck/domain/__init__.py`、`src/factcheck/usecases/__init__.py`、`tests/factcheck/__init__.py`：空檔。

- [ ] **Step 2: 寫領域實體、錯誤與 port**

`src/factcheck/domain/entities.py`：

```python
"""事實查核的實體。純資料，不依賴任何框架。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class ClaimKind(StrEnum):
    """主張種類決定要去哪裡取證。"""
    LAW_ARTICLE = "law_article"    # 現行法條內容：罰則、期限、金額
    BILL_CONTENT = "bill_content"  # 議案內容：誰提的版本、編列多少
    BILL_STATUS = "bill_status"    # 議案進度：是否三讀、目前狀態


class Verdict(StrEnum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    CONTRADICTED = "contradicted"
    UNVERIFIABLE = "unverifiable"


class Method(StrEnum):
    NUMERIC = "numeric"   # 程式比對數字
    MODEL = "model"       # 模型判讀文字，引用經程式確認
    NONE = "none"         # 沒有比對（找不到證據、來源掛了）


class SourceKind(StrEnum):
    LAW = "law"
    BILL = "bill"


@dataclass(frozen=True)
class Speech:
    """一段要查核的發言。只查核 speaker 本人：逐字稿沒有講者標記。"""
    speaker: str
    date: date
    meeting: str
    transcript: str


@dataclass(frozen=True)
class ExtractedClaim:
    """模型從逐字稿挑出的主張。

    quote 與 figures 都必須能在原文裡逐字找到，由 use case 檢查——模型
    可能改寫，也可能「幫委員說了他沒說的話」。
    """
    quote: str
    kind: ClaimKind
    statement: str
    figures: tuple[str, ...] = ()
    law: str = ""
    article: str = ""
    bill_keywords: str = ""
    proposer: str = ""


@dataclass(frozen=True)
class Evidence:
    source: SourceKind
    title: str
    official_url: str
    api_url: str
    excerpt: str


@dataclass(frozen=True)
class Judgement:
    """文字判讀的結果。evidence_quote 要由呼叫端確認真的在證據裡。"""
    verdict: Verdict
    evidence_quote: str
    reason: str


@dataclass(frozen=True)
class CheckedClaim:
    claim: ExtractedClaim
    timestamp: float
    verdict: Verdict
    method: Method
    rationale: str
    evidence: tuple[Evidence, ...] = ()
```

`src/factcheck/domain/errors.py`：

```python
"""事實查核的錯誤。分成「這一則查不到」與「整篇做不下去」兩類。"""


class SourceUnavailable(Exception):
    """官方資料這次拿不到。只讓那一則變成無法查證，整篇不失敗。"""


class ModelUnavailable(Exception):
    """模型連不上或回了錯誤。整篇工作失敗，下次排程重試。"""


class ModelOutputInvalid(Exception):
    """模型的回應不是約定的 JSON。"""


class OperationCancelled(Exception):
    """使用者或服務要求停止。"""
```

`src/factcheck/domain/ports.py`：

```python
"""use case 需要的外部能力。實作在 infrastructure，測試用假的。"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date
from typing import Protocol

from .entities import Evidence, ExtractedClaim, Judgement, Speech

ProgressCallback = Callable[[float | None, str], None]
CancelCheck = Callable[[], bool]


class ClaimExtractor(Protocol):
    def extract(self, speech: Speech) -> list[ExtractedClaim]: ...


class EvidenceSource(Protocol):
    def find(self, claim: ExtractedClaim, on: date) -> list[Evidence]: ...


class TextJudge(Protocol):
    def judge(self, statement: str, evidence: Sequence[Evidence]) -> Judgement: ...
```

- [ ] **Step 3: 寫數字正規化的失敗測試**

`tests/factcheck/test_numbers.py`：

```python
"""數字正規化：事實查核裡唯一由程式判對錯的部分，每條規則都要釘住。"""
from __future__ import annotations

import pytest

from factcheck.usecases.numbers import (
    Quantity,
    Unit,
    article_label,
    article_number,
    has_approximation,
    parse_chinese_number,
    quantities,
    to_chinese,
)

MONEY = Unit.MONEY


@pytest.mark.parametrize("text, expected", [
    ("三", 3), ("十五", 15), ("三十", 30), ("二十四", 24), ("一百零六", 106),
    ("一百一十", 110), ("二千一百", 2100), ("三十萬", 300_000),
    ("一萬五千", 15_000), ("二千一百億", 210_000_000_000),
])
def test_chinese_numbers(text, expected):
    assert parse_chinese_number(text) == expected


def test_non_numbers_are_rejected():
    assert parse_chinese_number("") is None
    assert parse_chinese_number("三讀") is None


def test_law_text_money_range():
    """醫療法第 106 條的原文。"""
    assert quantities("處新臺幣三萬元以上五萬元以下罰鍰") == [
        Quantity(30_000, MONEY), Quantity(50_000, MONEY)]


def test_spoken_money_with_shared_unit():
    """「5到25萬」的萬是兩個數字共用的——這是口語最常見的寫法。"""
    assert quantities("從現行的3萬到5萬提升到5到25萬") == [
        Quantity(30_000, MONEY), Quantity(50_000, MONEY),
        Quantity(50_000, MONEY), Quantity(250_000, MONEY)]


def test_years_and_money_together():
    assert quantities("6年2100億") == [
        Quantity(6, Unit.YEAR), Quantity(210_000_000_000, MONEY)]


def test_bill_text_money():
    assert quantities("本條例所需經費上限為新臺幣二千一百億元") == [
        Quantity(210_000_000_000, MONEY)]


def test_prison_terms_are_durations():
    assert quantities("處三年以下有期徒刑") == [Quantity(3, Unit.YEAR)]
    assert quantities("6個月以上5年以下的有期徒刑") == [
        Quantity(6, Unit.MONTH), Quantity(5, Unit.YEAR)]


def test_fiscal_years_are_not_durations():
    """「116 年度」是年度不是期間。收進來的話，證據裡的年度會讓「6 年」
    被判成對不上——一個假的「不符」。"""
    result = quantities("114 至 116 年度累計編列 82.4 億元")
    assert len(result) == 1
    assert result[0].unit == MONEY
    assert result[0].value == pytest.approx(8_240_000_000)


def test_counts_are_ignored():
    assert quantities("契約上總數是1,860 架") == []
    assert quantities("5萬人參加") == []


def test_article_numbers_are_not_quantities():
    assert quantities("違反第二十四條第二項規定者") == []


def test_words_that_look_like_numbers():
    assert quantities("千萬不要") == []


def test_full_width_digits():
    assert quantities("罰鍰３萬元") == [Quantity(30_000, MONEY)]


def test_approximation_words():
    assert has_approximation("大約2千億")
    assert has_approximation("逾3萬件")
    assert not has_approximation("罰鍰3萬到5萬")


@pytest.mark.parametrize("n, text", [
    (3, "三"), (10, "十"), (15, "十五"), (24, "二十四"), (100, "一百"),
    (106, "一百零六"), (110, "一百一十"), (124, "一百二十四"),
])
def test_to_chinese(n, text):
    assert to_chinese(n) == text


def test_article_label_matches_lyapi():
    assert article_label(106) == "第一百零六條"


@pytest.mark.parametrize("text, n", [
    ("第24條", 24), ("24條", 24), ("第二十四條", 24), ("醫療法第 106 條", 106),
])
def test_article_number(text, n):
    assert article_number(text) == n


def test_article_number_missing():
    assert article_number("") is None
    assert article_number("醫療法") is None
```

- [ ] **Step 4: 確認測試失敗**

Run: `uv run pytest tests/factcheck/test_numbers.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'factcheck.usecases.numbers'`）

- [ ] **Step 5: 實作 `src/factcheck/usecases/numbers.py`**

```python
"""數字正規化：把「三萬元」「2100億」「5到25萬」變成可以比對的數值。

純函式。事實查核裡由程式（而不是模型）判定對錯的就是數字，所以每一條
規則都有測試釘住。只認兩類：金額與期間。人數、架數、年度一律不收——
收了反而會讓「證據裡有同類數字卻對不上」變成誤判的「不符」。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_SMALL = {"十": 10, "百": 100, "千": 1000}
_BIG = {"萬": 10_000, "億": 100_000_000}
_CN_CHARS = "".join(_DIGITS) + "".join(_SMALL) + "".join(_BIG)

# 阿拉伯數字與倍數之間可以有空白：LYAPI 與院方文件常寫「82.4 億元」
_NUMBER = rf"[0-9][0-9,]*(?:\.[0-9]+)?(?:\s*[千百]?[萬億]|千)?|[{_CN_CHARS}]+"
_RANGE = re.compile(rf"(?P<a>{_NUMBER})(?:\s*(?:到|至|~|-)\s*(?P<b>{_NUMBER}))?")
_ARABIC = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([千百]?)([萬億]?)")
_ARTICLE = re.compile(rf"第?\s*([0-9]+|[{_CN_CHARS}]+)\s*條")

# 金額沒寫「元」時，後面接這些字就是在數東西，不是錢
_COUNT_WORDS = frozenset("人架件個台臺輛艘名位次家所條項張頁份篇支隻戶款案")
_APPROX_WORDS = ("約", "將近", "超過", "逾", "左右", "上下")
_CN_ORDINAL = "零一二三四五六七八九"


class Unit(StrEnum):
    MONEY = "money"
    YEAR = "year"
    MONTH = "month"
    DAY = "day"


@dataclass(frozen=True)
class Quantity:
    value: float
    unit: Unit

    def __str__(self) -> str:
        if self.unit == Unit.MONEY:
            return f"{self.value:,.0f} 元"
        suffix = {Unit.YEAR: "年", Unit.MONTH: "個月", Unit.DAY: "天"}[self.unit]
        return f"{self.value:g} {suffix}"


def parse_chinese_number(text: str) -> int | None:
    """「二千一百」→ 2100、「一百零六」→ 106、「一萬五千」→ 15000。"""
    if not text or any(ch not in _CN_CHARS for ch in text):
        return None
    result = 0      # 已經乘上萬、億的部分
    section = 0     # 萬以下的累計
    number: int | None = None
    for ch in text:
        if ch in _DIGITS:
            number = _DIGITS[ch]
        elif ch in _SMALL:
            # 「十五」開頭的十前面沒有數字，代表一十
            section += (1 if number is None else number) * _SMALL[ch]
            number = None
        else:
            section += number or 0
            result += (section or 1) * _BIG[ch]
            section = 0
            number = None
    return result + section + (number or 0)


def _value(token: str) -> float | None:
    token = token.replace(",", "")
    if token[0].isdigit():
        match = _ARABIC.fullmatch(token)
        if match is None:
            return None
        value = float(match.group(1))
        if match.group(2):
            value *= _SMALL[match.group(2)]
        if match.group(3):
            value *= _BIG[match.group(3)]
        return value
    # 「千萬不要」「萬一」裡的字也是數字字元，但沒有任何一個數目
    if not any(ch in _DIGITS for ch in token) and not token.startswith("十"):
        return None
    parsed = parse_chinese_number(token)
    return float(parsed) if parsed is not None else None


def _big_multiplier(token: str) -> int:
    return _BIG.get(token[-1], 1)


def _unit(rest: str, token: str, before: str) -> Unit | None:
    rest = rest.lstrip()
    if rest.startswith(("元", "塊")):
        return Unit.MONEY
    if rest.startswith("個月"):
        return Unit.MONTH
    if rest.startswith("天"):
        return Unit.DAY
    if rest.startswith("年") and not rest.startswith("年度"):
        return Unit.YEAR
    if token[-1] in _BIG and (not rest or rest[0] not in _COUNT_WORDS):
        return Unit.MONEY
    if before.endswith(("新臺幣", "新台幣")):
        return Unit.MONEY
    return None


def quantities(text: str) -> list[Quantity]:
    """文字裡所有的金額與期間，依出現順序。"""
    text = unicodedata.normalize("NFKC", text or "")
    found: list[Quantity] = []
    for match in _RANGE.finditer(text):
        start = match.start()
        if start > 0 and text[start - 1] == "第":
            continue            # 第二十四條、第二項
        a, b = match.group("a"), match.group("b")
        unit = _unit(text[match.end():], b or a, text[max(0, start - 3):start])
        if unit is None:
            continue
        values = [_value(a)]
        if b:
            vb = _value(b)
            # 「5到25萬」：萬是兩個數字共用的
            if values[0] is not None and _big_multiplier(a) == 1:
                values[0] *= _big_multiplier(b)
            values.append(vb)
        for value in values:
            if value is None:
                continue
            if unit == Unit.YEAR and value >= 100:
                continue        # 民國年或西元年，不是期間
            found.append(Quantity(value, unit))
    return found


def has_approximation(text: str) -> bool:
    return any(word in (text or "") for word in _APPROX_WORDS)


def to_chinese(n: int) -> str:
    """1～999 轉成法條用的中文數字：106 → 一百零六、110 → 一百一十。"""
    if not 0 < n < 1000:
        raise ValueError(f"條號超出範圍：{n}")
    hundreds, rest = divmod(n, 100)
    tens, ones = divmod(rest, 10)
    tail = _CN_ORDINAL[ones] if ones else ""
    if hundreds:
        head = _CN_ORDINAL[hundreds] + "百"
        if rest == 0:
            return head
        if tens == 0:
            return head + "零" + tail
        return head + _CN_ORDINAL[tens] + "十" + tail
    if tens:
        return ("" if tens == 1 else _CN_ORDINAL[tens]) + "十" + tail
    return tail


def article_label(n: int) -> str:
    """LYAPI 的條號格式。"""
    return f"第{to_chinese(n)}條"


def article_number(text: str) -> int | None:
    """「第24條」「24條」「第二十四條」→ 24。"""
    match = _ARTICLE.search(unicodedata.normalize("NFKC", text or ""))
    if match is None:
        return None
    token = match.group(1)
    return int(token) if token.isdigit() else parse_chinese_number(token)
```

- [ ] **Step 6: 確認測試通過**

Run: `uv run pytest tests/factcheck/test_numbers.py -q`
Expected: 全部 PASS。若某個案例失敗，修正 `numbers.py` 而不是改測試——這些案例全部取自實測的逐字稿與法條原文。

- [ ] **Step 7: Commit**

```bash
git add src/factcheck tests/factcheck/__init__.py tests/factcheck/test_numbers.py
git commit -m "feat(factcheck): 領域模型與數字正規化"
```

---
### Task 2: 落地檢查與數字比對

**Files:**
- Create: `src/factcheck/usecases/grounding.py`, `src/factcheck/usecases/compare.py`
- Test: `tests/factcheck/test_grounding.py`, `tests/factcheck/test_compare.py`

**Interfaces:**
- Consumes: Task 1 的 `Verdict`、`Quantity`、`quantities`
- Produces:
  - `factcheck.usecases.grounding`: `normalize(text) -> str`、`is_grounded(fragment, source) -> bool`、`transcript_lines(transcript) -> list[tuple[float, str]]`、`locate(quote, transcript) -> float | None`
  - `factcheck.usecases.compare`: `NumericOutcome(verdict: Verdict | None, rationale: str)`、`compare_numbers(figures, evidence_texts, approximate=False) -> NumericOutcome`（`verdict is None` 代表數字比不了，要改走文字判讀）

- [ ] **Step 1: 寫落地檢查的失敗測試**

`tests/factcheck/test_grounding.py`：

```python
"""落地檢查：模型說的話必須在原文裡找得到。"""
from __future__ import annotations

from factcheck.usecases.grounding import is_grounded, locate, normalize, transcript_lines

TRANSCRIPT = (
    "00:16 那這幾年有逐漸攀升的一個趨勢\n"
    "00:32 臺灣民眾黨對醫療法24條跟106條提出修正條文草案 首先我們對於滋擾醫院秩序之人 "
    "他的行政罰鍰從現行的3萬到5萬提升到5到25萬\n"
    "00:48 希望能夠提升賀主力\n"
)


def test_normalize_drops_spaces_and_widths():
    assert normalize("３萬 到 ５萬\n") == "3萬到5萬"


def test_a_verbatim_quote_is_grounded():
    assert is_grounded("他的行政罰鍰從現行的3萬到5萬", TRANSCRIPT)


def test_spacing_differences_do_not_matter():
    assert is_grounded("滋擾醫院秩序之人他的行政罰鍰", TRANSCRIPT)


def test_a_rewritten_quote_is_not_grounded():
    """攔的行為：模型把「3萬到5萬」改寫成「三萬至五萬元」。原文不是那樣說的。"""
    assert not is_grounded("行政罰鍰從現行的三萬至五萬元", TRANSCRIPT)


def test_an_empty_fragment_is_never_grounded():
    assert not is_grounded("", TRANSCRIPT)
    assert not is_grounded("   ", TRANSCRIPT)


def test_lines_carry_their_seconds():
    lines = transcript_lines(TRANSCRIPT)
    assert [at for at, _ in lines] == [16.0, 32.0, 48.0]
    assert lines[0][1] == "那這幾年有逐漸攀升的一個趨勢"


def test_hour_timestamps():
    assert transcript_lines("1:02:03 很長的會議")[0][0] == 3723.0


def test_locate_finds_the_line_where_the_quote_starts():
    assert locate("從現行的3萬到5萬", TRANSCRIPT) == 32.0


def test_locate_across_a_line_break():
    """quote 跨兩行時，時間取開頭那一行。"""
    assert locate("提升到5到25萬希望能夠", TRANSCRIPT) == 32.0


def test_timestamps_are_not_part_of_the_spoken_text():
    """攔的 bug：沒去掉時間標記的話，跨行的 quote 中間會夾著「00:48」而永遠找不到。"""
    assert locate("25萬 希望", TRANSCRIPT) == 32.0


def test_locate_returns_none_when_missing():
    assert locate("完全不存在的句子", TRANSCRIPT) is None
    assert locate("", TRANSCRIPT) is None
```

- [ ] **Step 2: 寫數字比對的失敗測試**

`tests/factcheck/test_compare.py`：

```python
"""數字比對：判定規則。"""
from __future__ import annotations

from factcheck.domain.entities import Verdict
from factcheck.usecases.compare import compare_numbers

ARTICLE_106 = ("違反第二十四條第二項規定者，處新臺幣三萬元以上五萬元以下罰鍰。"
               "對於醫事人員以強暴、脅迫、恐嚇或其他非法之方法，妨害其執行醫療或救護業務者，"
               "處三年以下有期徒刑，得併科新臺幣三十萬元以下罰金。")


def test_every_number_found_is_supported():
    outcome = compare_numbers(["3萬到5萬"], [ARTICLE_106])
    assert outcome.verdict == Verdict.SUPPORTED
    assert "30,000 元" in outcome.rationale


def test_some_numbers_found_is_partial():
    outcome = compare_numbers(["3萬到10萬"], [ARTICLE_106])
    assert outcome.verdict == Verdict.PARTIAL
    assert "100,000 元" in outcome.rationale


def test_no_number_found_is_contradicted():
    outcome = compare_numbers(["1萬到2萬"], [ARTICLE_106])
    assert outcome.verdict == Verdict.CONTRADICTED


def test_durations_compare_with_durations():
    assert compare_numbers(["3年以下"], [ARTICLE_106]).verdict == Verdict.SUPPORTED


def test_no_figures_means_numbers_cannot_decide():
    assert compare_numbers([], [ARTICLE_106]).verdict is None


def test_evidence_without_the_same_kind_of_number_cannot_decide():
    """證據裡沒有金額時，「對不上」不代表說錯，只代表這份證據比不了。"""
    outcome = compare_numbers(["3萬"], ["議案狀態：三讀"])
    assert outcome.verdict is None


def test_units_that_cannot_be_checked_are_reported_not_counted():
    """「6年2100億」對上只寫了金額的條文：金額對得上，年限沒得比。"""
    outcome = compare_numbers(["6年2100億"], ["本條例所需經費上限為新臺幣二千一百億元"])
    assert outcome.verdict == Verdict.SUPPORTED
    assert "6 年" in outcome.rationale


def test_approximate_claims_allow_ten_percent():
    evidence = ["總額為新臺幣二千四百億元"]
    assert compare_numbers(["2300億"], evidence).verdict == Verdict.CONTRADICTED
    assert compare_numbers(["2300億"], evidence, approximate=True).verdict == Verdict.SUPPORTED


def test_duplicated_figures_count_once():
    outcome = compare_numbers(["3萬", "3萬"], [ARTICLE_106])
    assert outcome.verdict == Verdict.SUPPORTED
```

- [ ] **Step 3: 確認兩個測試檔都失敗**

Run: `uv run pytest tests/factcheck/test_grounding.py tests/factcheck/test_compare.py -q`
Expected: FAIL（模組不存在）

- [ ] **Step 4: 實作 `src/factcheck/usecases/grounding.py`**

```python
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
```

- [ ] **Step 5: 實作 `src/factcheck/usecases/compare.py`**

```python
"""數字比對：程式判定，不經過模型。

規則：只比同一類單位。主張的數字全都在證據裡 → 相符；一部分在 → 部分
相符；證據有同類數字但一個都對不上 → 不符；證據沒有同類數字 → 比不了，
交給文字判讀。證據沒有的那一類不算錯，只在理由裡講出來。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..domain.entities import Verdict
from .numbers import Quantity, quantities

APPROXIMATE_TOLERANCE = 0.10
_EPSILON = 1e-9


@dataclass(frozen=True)
class NumericOutcome:
    verdict: Verdict | None
    rationale: str


def _same(claimed: Quantity, found: Quantity, approximate: bool) -> bool:
    if claimed.unit != found.unit:
        return False
    scale = max(abs(found.value), 1.0)
    tolerance = APPROXIMATE_TOLERANCE if approximate else _EPSILON
    return abs(claimed.value - found.value) <= tolerance * scale


def _listed(items: Sequence[Quantity]) -> str:
    return "、".join(str(q) for q in items)


def compare_numbers(figures: Sequence[str], evidence_texts: Sequence[str],
                    approximate: bool = False) -> NumericOutcome:
    claimed = list(dict.fromkeys(q for figure in figures for q in quantities(figure)))
    if not claimed:
        return NumericOutcome(None, "主張沒有可比對的數字")
    found = [q for text in evidence_texts for q in quantities(text)]
    units = {q.unit for q in found}
    checkable = [q for q in claimed if q.unit in units]
    if not checkable:
        return NumericOutcome(None, "證據裡沒有同類的數字")

    hit = [q for q in checkable if any(_same(q, f, approximate) for f in found)]
    missed = [q for q in checkable if q not in hit]
    unchecked = [q for q in claimed if q.unit not in units]

    parts = []
    if hit:
        parts.append(f"主張的 {_listed(hit)} 出現在證據中")
    if missed:
        parts.append(f"證據中找不到 {_listed(missed)}")
    if unchecked:
        parts.append(f"{_listed(unchecked)} 在證據中沒有同類數字可比")
    rationale = "；".join(parts)

    if not missed:
        return NumericOutcome(Verdict.SUPPORTED, rationale)
    if hit:
        return NumericOutcome(Verdict.PARTIAL, rationale)
    return NumericOutcome(Verdict.CONTRADICTED, rationale)
```

- [ ] **Step 6: 確認測試通過**

Run: `uv run pytest tests/factcheck -q`
Expected: 全部 PASS

- [ ] **Step 7: Commit**

```bash
git add src/factcheck/usecases/grounding.py src/factcheck/usecases/compare.py tests/factcheck/test_grounding.py tests/factcheck/test_compare.py
git commit -m "feat(factcheck): 落地檢查與數字比對"
```

---

### Task 3: LYAPI 客戶端與法條取證

**Files:**
- Create: `src/factcheck/infrastructure/__init__.py`（空檔）、`src/factcheck/infrastructure/lyapi.py`、`src/factcheck/infrastructure/law_source.py`
- Test: `tests/factcheck/lyapi_fakes.py`、`tests/factcheck/test_lyapi.py`、`tests/factcheck/test_law_source.py`

**Interfaces:**
- Consumes: Task 1 的 `Evidence`、`ExtractedClaim`、`ClaimKind`、`SourceKind`、`SourceUnavailable`、`article_label`、`article_number`
- Produces:
  - `factcheck.infrastructure.lyapi`: `DEFAULT_BASE`、`LyApi(base=DEFAULT_BASE, fetch=_http_get)`，方法 `url(path, params=None) -> str`、`get(path, params=None) -> dict`、`laws_by_name(name) -> list[dict]`、`law_versions(law_id) -> list[dict]`、`law_contents(version_id) -> list[dict]`、`bills_search(keyword, term) -> list[dict]`、`bill(bill_id) -> dict`；任何連線或格式錯誤都丟 `SourceUnavailable`
  - `factcheck.infrastructure.law_source`: `official_law_url(name) -> str`、`version_on(versions, on) -> dict | None`、`LawSource(api).find(claim, on) -> list[Evidence]`
  - `tests/factcheck/lyapi_fakes.py`: `load(name) -> dict`、`FakeFetch(routes)`（Task 4 也用）

- [ ] **Step 1: 寫測試用的假 fetch**

`tests/factcheck/lyapi_fakes.py`：

```python
"""用實測存下來的 LYAPI 回應當假資料。fixture 是真的回應，欄位名與形狀都對得上。"""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeFetch:
    """依路徑回應。routes 的值可以是 dict，或吃 query（dict[str, str]）回 dict 的函式。"""

    def __init__(self, routes: dict):
        self.routes = routes
        self.urls: list[str] = []

    def __call__(self, url: str) -> dict:
        self.urls.append(url)
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.removeprefix("/v2")
        if path not in self.routes:
            raise OSError(f"沒有這個路徑的假資料：{path}")
        route = self.routes[path]
        if callable(route):
            query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            return route(query)
        return route
```

- [ ] **Step 2: 寫 LYAPI 客戶端的失敗測試**

`tests/factcheck/test_lyapi.py`：

```python
"""LYAPI 客戶端：查詢參數與錯誤轉譯。"""
from __future__ import annotations

import pytest

from factcheck.domain.errors import SourceUnavailable
from factcheck.infrastructure.lyapi import LyApi

from .lyapi_fakes import FakeFetch, load


def test_law_search_wraps_the_name_in_quotes():
    """攔的 bug：q 不加引號是模糊搜尋，實測搜「醫療法」會回傳所得稅法。"""
    fetch = FakeFetch({"/laws": load("laws_search_醫療法.json")})
    rows = LyApi(fetch=fetch).laws_by_name("醫療法")
    assert rows[0]["名稱"] == "醫療法"
    assert "q=%22%E9%86%AB%E7%99%82%E6%B3%95%22" in fetch.urls[0]


def test_law_contents_walks_every_page():
    pages = {
        "1": {"total_page": 2, "lawcontents": [{"條號": "第一條"}]},
        "2": {"total_page": 2, "lawcontents": [{"條號": "第二條"}]},
    }
    fetch = FakeFetch({"/law_contents": lambda q: pages[q["page"]]})
    rows = LyApi(fetch=fetch).law_contents("02533:2026-05-08-修正")
    assert [r["條號"] for r in rows] == ["第一條", "第二條"]


def test_bill_detail_returns_the_data_object():
    fetch = FakeFetch({"/bills/201110221870000": load("bill_201110221870000.json")})
    bill = LyApi(fetch=fetch).bill("201110221870000")
    assert bill["提案單位/提案委員"] == "行政院"


def test_connection_errors_become_source_unavailable():
    def broken(url):
        raise OSError("connection reset")
    with pytest.raises(SourceUnavailable):
        LyApi(fetch=broken).laws_by_name("醫療法")


def test_a_non_object_response_is_source_unavailable():
    with pytest.raises(SourceUnavailable):
        LyApi(fetch=lambda url: ["not", "a", "dict"]).bill("1")


def test_url_encodes_chinese_parameters():
    url = LyApi(base="https://ly.govapi.tw/v2").url("/bills", {"屆": 11})
    assert url == "https://ly.govapi.tw/v2/bills?%E5%B1%86=11"
```

- [ ] **Step 3: 寫法條取證的失敗測試**

`tests/factcheck/test_law_source.py`：

```python
"""法條取證：用實測的醫療法資料。"""
from __future__ import annotations

from datetime import date

from factcheck.domain.entities import ClaimKind, ExtractedClaim, SourceKind
from factcheck.infrastructure.law_source import LawSource, official_law_url, version_on
from factcheck.infrastructure.lyapi import LyApi

from .lyapi_fakes import FakeFetch, load

SPEECH_DAY = date(2026, 8, 25)


def _claim(law="醫療法", article="第24條"):
    return ExtractedClaim(quote="他的行政罰鍰從現行的3萬到5萬", kind=ClaimKind.LAW_ARTICLE,
                          statement="醫療法現行罰鍰 3 萬到 5 萬", figures=("3萬到5萬",),
                          law=law, article=article)


def _source():
    fetch = FakeFetch({
        "/laws": load("laws_search_醫療法.json"),
        "/laws/02533/versions": load("law_versions_02533.json"),
        "/law_contents": load("law_contents_02533_現行_subset.json"),
    })
    return LawSource(LyApi(fetch=fetch)), fetch


def test_the_cited_article_comes_first_then_articles_that_reference_it():
    """委員說第 24 條，罰鍰其實在第 106 條（「違反第二十四條第二項規定者…」）。
    只取第 24 條的話，數字永遠對不上。"""
    evidence = _source()[0].find(_claim(), SPEECH_DAY)
    titles = [e.title for e in evidence]
    assert titles[0].startswith("醫療法 第二十四條")
    assert any("第一百零六條" in t for t in titles)
    assert any("三萬元以上五萬元以下" in e.excerpt for e in evidence)


def test_evidence_names_the_version_and_links_to_official_and_api_sources():
    evidence = _source()[0].find(_claim(), SPEECH_DAY)
    first = evidence[0]
    assert first.source == SourceKind.LAW
    assert "2026-05-08" in first.title
    assert first.official_url == official_law_url("醫療法")
    assert first.official_url.startswith("https://law.moj.gov.tw/")
    assert first.api_url.startswith("https://ly.govapi.tw/v2/law_contents?")


def test_the_version_in_force_on_the_speech_day_is_used():
    versions = load("law_versions_02533.json")["lawversions"]
    assert version_on(versions, date(2026, 8, 25))["版本編號"] == "02533:2026-05-08-修正"
    assert version_on(versions, date(2025, 1, 1))["版本編號"] == "02533:2023-05-30-修正"
    assert version_on(versions, date(1980, 1, 1)) is None


def test_a_law_that_does_not_exist_yields_nothing():
    assert _source()[0].find(_claim(law="不存在的法"), SPEECH_DAY) == []


def test_a_claim_without_an_article_number_yields_nothing():
    """沒有條號時不去整部法律裡找「剛好有這個數字」的條文——那等於拿答案找題目，
    只會讓「相符」變得廉價。"""
    assert _source()[0].find(_claim(article=""), SPEECH_DAY) == []


def test_contents_are_fetched_once_per_version():
    source, fetch = _source()
    source.find(_claim(), SPEECH_DAY)
    source.find(_claim(article="第106條"), SPEECH_DAY)
    assert sum("/law_contents" in u for u in fetch.urls) == 1
```

- [ ] **Step 4: 確認測試失敗**

Run: `uv run pytest tests/factcheck/test_lyapi.py tests/factcheck/test_law_source.py -q`
Expected: FAIL（模組不存在）

- [ ] **Step 5: 實作 `src/factcheck/infrastructure/lyapi.py`**

```python
"""LYAPI（ly.govapi.tw）的 HTTP 客戶端。

LYAPI 是公民科技社群整理的立法院資料，不是立法院官方——所以證據同時留
官方網址與這裡的 API 網址。對外的錯誤契約只有 SourceUnavailable：它讓
那一則主張變成無法查證，而不是讓整篇查核失敗。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping

from ..domain.errors import SourceUnavailable

DEFAULT_BASE = "https://ly.govapi.tw/v2"
_TIMEOUT = 30.0
_PAGE_LIMIT = 100
# 一部法律最多兩三百條；翻頁上限只是防上游給了怪的 total_page 而無限迴圈
_MAX_PAGES = 10


def _http_get(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _total_pages(payload: dict) -> int:
    try:
        return int(payload.get("total_page") or 1)
    except (TypeError, ValueError):
        return 1


class LyApi:
    def __init__(self, base: str = DEFAULT_BASE,
                 fetch: Callable[[str], object] = _http_get):
        self._base = base.rstrip("/")
        self._fetch = fetch

    def url(self, path: str, params: Mapping[str, object] | None = None) -> str:
        query = f"?{urllib.parse.urlencode(params, doseq=True)}" if params else ""
        return f"{self._base}{path}{query}"

    def get(self, path: str, params: Mapping[str, object] | None = None) -> dict:
        url = self.url(path, params)
        try:
            payload = self._fetch(url)
        except (OSError, ValueError) as e:
            raise SourceUnavailable(f"LYAPI 取不到 {path}：{str(e)[:200]}") from e
        if not isinstance(payload, dict):
            raise SourceUnavailable(f"LYAPI 的 {path} 回應格式不符預期")
        return payload

    def laws_by_name(self, name: str) -> list[dict]:
        # 引號是必要的：不加的話是模糊搜尋，搜「醫療法」會先回所得稅法
        payload = self.get("/laws", {"q": f'"{name}"', "limit": 20})
        return list(payload.get("laws") or [])

    def law_versions(self, law_id: str) -> list[dict]:
        payload = self.get(f"/laws/{law_id}/versions", {"limit": 100})
        return list(payload.get("lawversions") or [])

    def law_contents(self, version_id: str) -> list[dict]:
        rows: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            payload = self.get("/law_contents", {"版本編號": version_id,
                                                 "limit": _PAGE_LIMIT, "page": page})
            rows.extend(payload.get("lawcontents") or [])
            if page >= _total_pages(payload):
                break
        return rows

    def bills_search(self, keyword: str, term: int) -> list[dict]:
        payload = self.get("/bills", {"q": f'"{keyword}"', "屆": term, "limit": 30})
        return list(payload.get("bills") or [])

    def bill(self, bill_id: str) -> dict:
        data = self.get(f"/bills/{bill_id}").get("data")
        return data if isinstance(data, dict) else {}
```

- [ ] **Step 6: 實作 `src/factcheck/infrastructure/law_source.py`**

```python
"""法條取證：法律名稱 → 發言當日有效的版本 → 指定條文與提到它的條文。"""
from __future__ import annotations

import urllib.parse
from collections import OrderedDict
from collections.abc import Sequence
from datetime import date

from ..domain.entities import Evidence, ExtractedClaim, SourceKind
from ..usecases.numbers import article_label, article_number
from .lyapi import LyApi

# 指定條文之外，最多再取幾條「內文提到它」的條文。委員常引義務條文，
# 罰則卻在後面另一條（醫療法第 24 條 → 第 106 條）。
MAX_RELATED = 2
EXCERPT_LIMIT = 1200
# 常駐服務會連續跑好幾個月；每部法律一兩百條，快取要有上限
_CACHE_SIZE = 16


def official_law_url(name: str) -> str:
    """全國法規資料庫的名稱查詢頁。實測可用；它沒有穩定的「依名稱直達」網址。"""
    return ("https://law.moj.gov.tw/Law/LawSearchResult.aspx?ty=ONEBAR&kw="
            + urllib.parse.quote(name))


def version_on(versions: Sequence[dict], on: date) -> dict | None:
    """發言當日有效的版本：日期不晚於發言日的最新一版。

    不能直接用現行版：醫療法最近一次修正是 2026-05-08，查錯版本會把
    「修法前說的是對的」判成錯。
    """
    day = on.isoformat()
    eligible = [v for v in versions if str(v.get("日期") or "") and str(v["日期"]) <= day]
    return max(eligible, key=lambda v: str(v["日期"]), default=None)


def _matches(law: dict, name: str) -> bool:
    names = [law.get("名稱") or ""]
    names += list(law.get("其他名稱") or []) + list(law.get("別名") or [])
    return name in names


class LawSource:
    def __init__(self, api: LyApi):
        self._api = api
        self._contents: OrderedDict[str, list[dict]] = OrderedDict()

    def find(self, claim: ExtractedClaim, on: date) -> list[Evidence]:
        number = article_number(claim.article)
        if not claim.law or number is None:
            return []
        law = next((row for row in self._api.laws_by_name(claim.law)
                    if _matches(row, claim.law)), None)
        if law is None:
            return []
        version = version_on(self._api.law_versions(str(law["法律編號"])), on)
        if version is None:
            return []
        rows = self._rows(str(version["版本編號"]))
        label = article_label(number)
        target = [r for r in rows if r.get("條號") == label]
        related = [r for r in rows
                   if r.get("條號") and r.get("條號") != label
                   and label in str(r.get("內容") or "")][:MAX_RELATED]
        return [self._evidence(law, version, row) for row in target + related]

    def _rows(self, version_id: str) -> list[dict]:
        if version_id in self._contents:
            self._contents.move_to_end(version_id)
            return self._contents[version_id]
        rows = self._api.law_contents(version_id)
        self._contents[version_id] = rows
        if len(self._contents) > _CACHE_SIZE:
            self._contents.popitem(last=False)
        return rows

    def _evidence(self, law: dict, version: dict, row: dict) -> Evidence:
        name = str(law.get("名稱") or "")
        article = str(row.get("條號") or "")
        return Evidence(
            source=SourceKind.LAW,
            title=f"{name} {article}（{version.get('日期')} {version.get('動作')}版）",
            official_url=official_law_url(name),
            api_url=self._api.url("/law_contents", {"版本編號": version["版本編號"],
                                                    "條號": article}),
            excerpt=str(row.get("內容") or "")[:EXCERPT_LIMIT],
        )
```

- [ ] **Step 7: 確認測試通過**

Run: `uv run pytest tests/factcheck -q`
Expected: 全部 PASS

- [ ] **Step 8: Commit**

```bash
git add src/factcheck/infrastructure tests/factcheck/lyapi_fakes.py tests/factcheck/test_lyapi.py tests/factcheck/test_law_source.py tests/factcheck/fixtures
git commit -m "feat(factcheck): LYAPI 客戶端與法條取證"
```

---

### Task 4: 議案取證

**Files:**
- Create: `src/factcheck/infrastructure/bill_source.py`
- Test: `tests/factcheck/test_bill_source.py`

**Interfaces:**
- Consumes: Task 3 的 `LyApi`、`FakeFetch`、`load`；Task 1 的 `quantities`、`Evidence`、`ClaimKind`、`SourceKind`
- Produces: `factcheck.infrastructure.bill_source`: `term_on(day) -> int`、`proposer_matches(proposer, field) -> bool`、`BillSource(api).find(claim, on) -> list[Evidence]`

- [ ] **Step 1: 寫失敗測試**

`tests/factcheck/test_bill_source.py`：

```python
"""議案取證：用實測的無人載具條例草案資料。"""
from __future__ import annotations

from datetime import date

from factcheck.domain.entities import ClaimKind, ExtractedClaim, SourceKind
from factcheck.infrastructure.bill_source import BillSource, proposer_matches, term_on
from factcheck.infrastructure.lyapi import LyApi

from .lyapi_fakes import FakeFetch, load

SPEECH_DAY = date(2026, 8, 27)


def _claim(proposer="行政院", kind=ClaimKind.BILL_CONTENT, keywords="無人載具"):
    return ExtractedClaim(quote="行政院提出的案子 6年2100億", kind=kind,
                          statement="行政院版本 6 年編列 2100 億", figures=("6年2100億",),
                          bill_keywords=keywords, proposer=proposer)


def _source(search=None):
    fetch = FakeFetch({
        "/bills": search or load("bills_search_無人載具.json"),
        "/bills/201110221870000": load("bill_201110221870000.json"),
        "/bills/202110223690000": load("bill_202110223690000.json"),
    })
    return BillSource(LyApi(fetch=fetch)), fetch


def test_term_is_derived_from_the_speech_date():
    assert term_on(date(2026, 8, 27)) == 11
    assert term_on(date(2024, 2, 1)) == 11
    assert term_on(date(2024, 1, 31)) == 10


def test_party_names_match_their_caucus():
    assert proposer_matches("國民黨", "本院國民黨黨團")
    assert proposer_matches("國民黨黨團", "本院國民黨黨團")
    assert proposer_matches("行政院", "行政院")
    assert not proposer_matches("行政院", "本院國民黨黨團")
    assert proposer_matches("", "任何人")


def test_the_executive_yuan_version_carries_its_budget_article():
    evidence = _source()[0].find(_claim(), SPEECH_DAY)
    assert len(evidence) == 1
    item = evidence[0]
    assert item.source == SourceKind.BILL
    assert "二千一百億元" in item.excerpt
    assert item.official_url == "https://ppg.ly.gov.tw/ppg/bills/201110221870000/details"
    assert item.api_url == "https://ly.govapi.tw/v2/bills/201110221870000"
    assert item.title.startswith("行政院｜")


def test_a_party_name_finds_the_caucus_bill():
    evidence = _source()[0].find(_claim(proposer="國民黨"), SPEECH_DAY)
    assert "二千四百億元" in evidence[0].excerpt


def test_review_reports_are_skipped():
    """審查報告把好幾個版本併在一起，分不出是誰的主張。"""
    rows = load("bills_search_無人載具.json")["bills"]
    review = next(r for r in rows if r["提案來源"] == "審查報告")
    executive = next(r for r in rows if r["議案編號"] == "201110221870000")
    source, fetch = _source({"bills": [review, executive]})
    evidence = source.find(_claim(proposer=""), SPEECH_DAY)
    assert len(evidence) == 1
    assert not any(review["議案編號"] in u for u in fetch.urls)


def test_bills_proposed_after_the_speech_are_not_evidence():
    """行政院版是 2026-06-26 提案，六月初的發言不可能在講它。"""
    assert _source()[0].find(_claim(), date(2026, 6, 1)) == []


def test_status_claims_quote_the_bill_status():
    evidence = _source()[0].find(_claim(kind=ClaimKind.BILL_STATUS), SPEECH_DAY)
    assert evidence[0].excerpt.startswith("議案狀態：")


def test_keywords_are_tried_in_turn_until_something_is_found():
    """模型常給口語的「無人機」，議案名稱寫的是「無人載具」。"""
    full = load("bills_search_無人載具.json")

    def search(query):
        return full if query["q"] == '"無人載具"' else {"bills": []}

    evidence = _source(search)[0].find(_claim(keywords="無人機 無人載具"), SPEECH_DAY)
    assert evidence


def test_no_keywords_means_no_search():
    source, fetch = _source()
    assert source.find(_claim(keywords=""), SPEECH_DAY) == []
    assert fetch.urls == []
```

- [ ] **Step 2: 確認測試失敗**

Run: `uv run pytest tests/factcheck/test_bill_source.py -q`
Expected: FAIL（模組不存在）

- [ ] **Step 3: 實作 `src/factcheck/infrastructure/bill_source.py`**

```python
"""議案取證：關鍵詞 → 同一屆的議案 → 比對提案者 → 取案由與條文對照表。"""
from __future__ import annotations

import re
from datetime import date

from ..domain.entities import ClaimKind, Evidence, ExtractedClaim, SourceKind
from ..usecases.numbers import quantities
from .lyapi import LyApi

# 口語的政黨名稱 → 議案上寫的提案單位
_ALIASES = {
    "國民黨": "國民黨黨團",
    "民進黨": "民主進步黨黨團",
    "民眾黨": "台灣民眾黨黨團",
    "時代力量": "時代力量黨團",
}
# 審查報告把好幾個版本併在一起，分不出是誰提的
_REVIEW_REPORT = "審查報告"
# 對照表裡不是「提案內容」的欄位：說明是理由、現行是修法前的條文
_SKIP_COLUMNS = frozenset({"說明", "現行"})
MAX_CANDIDATES = 3
MAX_ROWS = 4
EXCERPT_LIMIT = 1500


def term_on(day: date) -> int:
    """立法委員的屆別：第 7 屆從 2008-02-01 起，每屆四年。"""
    year = day.year - (1 if (day.month, day.day) < (2, 1) else 0)
    return 7 + (year - 2008) // 4


def proposer_matches(proposer: str, field: str) -> bool:
    if not proposer:
        return True
    return proposer in field or _ALIASES.get(proposer, proposer) in field


def _keywords(text: str) -> list[str]:
    return [k for k in re.split(r"[\s、，,]+", text.strip()) if k]


def _proposed_texts(data: dict) -> list[str]:
    texts: list[str] = []
    for table in data.get("對照表") or []:
        if not isinstance(table, dict):
            continue
        for row in table.get("rows") or []:
            if not isinstance(row, dict):
                continue
            for column, value in row.items():
                if column not in _SKIP_COLUMNS and isinstance(value, str) and value.strip():
                    texts.append(value.strip())
    return texts


class BillSource:
    def __init__(self, api: LyApi):
        self._api = api

    def find(self, claim: ExtractedClaim, on: date) -> list[Evidence]:
        rows = self._search(claim.bill_keywords, term_on(on))
        candidates = [
            r for r in rows
            if r.get("提案來源") != _REVIEW_REPORT
            and proposer_matches(claim.proposer, str(r.get("提案單位/提案委員") or ""))
        ][:MAX_CANDIDATES]
        evidence: list[Evidence] = []
        for row in candidates:
            data = self._api.bill(str(row.get("議案編號")))
            proposed_on = str(data.get("提案日期") or "")
            if proposed_on and proposed_on > on.isoformat():
                continue        # 發言時還不存在的議案，不可能是他在講的那一個
            evidence.append(self._evidence(claim, data, str(row.get("議案編號"))))
        return evidence

    def _search(self, keywords: str, term: int) -> list[dict]:
        for keyword in _keywords(keywords):
            rows = self._api.bills_search(keyword, term)
            if rows:
                return rows
        return []

    def _evidence(self, claim: ExtractedClaim, data: dict, bill_id: str) -> Evidence:
        if claim.kind == ClaimKind.BILL_STATUS:
            excerpt = (f"議案狀態：{data.get('議案狀態') or '不明'}；"
                       f"最新進度日期：{data.get('最新進度日期') or '不明'}")
        else:
            texts = _proposed_texts(data)
            # 先放有數字的條文：數字是最常被引用、也最容易比對的
            chosen = [t for t in texts if quantities(t)][:MAX_ROWS] or texts[:MAX_ROWS]
            reason = str(data.get("案由") or "").strip()
            excerpt = "\n".join(([f"案由：{reason}"] if reason else []) + chosen)
        url = str(data.get("url") or "")
        if not url.startswith(("http://", "https://")):
            url = f"https://ppg.ly.gov.tw/ppg/bills/{bill_id}/details"
        return Evidence(
            source=SourceKind.BILL,
            title=f"{data.get('提案單位/提案委員') or '提案者不明'}｜{data.get('議案名稱') or ''}"[:200],
            official_url=url,
            api_url=self._api.url(f"/bills/{bill_id}"),
            excerpt=excerpt[:EXCERPT_LIMIT],
        )
```

- [ ] **Step 4: 確認測試通過**

Run: `uv run pytest tests/factcheck -q`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add src/factcheck/infrastructure/bill_source.py tests/factcheck/test_bill_source.py
git commit -m "feat(factcheck): 議案取證"
```

---

### Task 5: Ollama 抽取器與判讀器

**Files:**
- Create: `src/factcheck/infrastructure/ollama.py`
- Test: `tests/factcheck/test_ollama.py`

**Interfaces:**
- Consumes: Task 1 的實體與錯誤
- Produces: `factcheck.infrastructure.ollama`: `MAX_CLAIMS`、`EXTRACT_SCHEMA`、`JUDGE_SCHEMA`、`build_extract_messages(speech)`、`parse_claims(payload) -> list[ExtractedClaim]`、`build_judge_messages(statement, evidence)`、`parse_judgement(payload) -> Judgement`、`OllamaClaimExtractor(host, model, num_ctx, timeout=600.0, chat=_chat)`、`OllamaTextJudge(host, model, num_ctx, timeout=300.0, chat=_chat)`；`chat` 的簽名是 `(host: str, body: dict, timeout: float) -> str`（回傳模型輸出的文字）

- [ ] **Step 1: 寫失敗測試**

`tests/factcheck/test_ollama.py`：

```python
"""Ollama 實作：提示詞、schema 與回應解析。HTTP 用假的 chat 函式替換。"""
from __future__ import annotations

import json
from datetime import date

import pytest

from factcheck.domain.entities import ClaimKind, Evidence, SourceKind, Speech, Verdict
from factcheck.domain.errors import ModelOutputInvalid
from factcheck.infrastructure.ollama import (
    EXTRACT_SCHEMA,
    MAX_CLAIMS,
    OllamaClaimExtractor,
    OllamaTextJudge,
    build_judge_messages,
    parse_claims,
    parse_judgement,
)

SPEECH = Speech(speaker="邱慧洳", date=date(2026, 8, 25), meeting="第11屆第5會期第23次會議",
                transcript="00:32 他的行政罰鍰從現行的3萬到5萬提升到5到25萬")


def _claim(**overrides):
    item = {"quote": "他的行政罰鍰從現行的3萬到5萬", "kind": "law_article",
            "statement": "醫療法現行罰鍰為 3 萬到 5 萬元", "figures": ["3萬到5萬"],
            "law": "醫療法", "article": "第24條", "bill_keywords": "", "proposer": ""}
    item.update(overrides)
    return item


def test_claims_are_parsed():
    claims = parse_claims(json.dumps({"claims": [_claim()]}))
    assert len(claims) == 1
    claim = claims[0]
    assert claim.kind == ClaimKind.LAW_ARTICLE
    assert claim.figures == ("3萬到5萬",)
    assert claim.law == "醫療法"


def test_claims_with_an_unknown_kind_are_dropped():
    assert parse_claims(json.dumps({"claims": [_claim(kind="opinion")]})) == []


def test_claims_without_a_quote_are_dropped():
    assert parse_claims(json.dumps({"claims": [_claim(quote="  ")]})) == []


def test_a_missing_statement_falls_back_to_the_quote():
    claim = parse_claims(json.dumps({"claims": [_claim(statement="")]}))[0]
    assert claim.statement == claim.quote


def test_at_most_max_claims_are_kept():
    payload = json.dumps({"claims": [_claim() for _ in range(MAX_CLAIMS + 3)]})
    assert len(parse_claims(payload)) == MAX_CLAIMS


def test_broken_json_is_model_output_invalid():
    with pytest.raises(ModelOutputInvalid):
        parse_claims("not json")
    with pytest.raises(ModelOutputInvalid):
        parse_claims(json.dumps({"items": []}))


@pytest.mark.parametrize("raw, verdict", [
    ("supported", Verdict.SUPPORTED),
    ("contradicted", Verdict.CONTRADICTED),
    ("insufficient", Verdict.UNVERIFIABLE),
    ("maybe", Verdict.UNVERIFIABLE),
])
def test_judgements_map_to_verdicts(raw, verdict):
    judgement = parse_judgement(json.dumps(
        {"verdict": raw, "evidence_quote": "處新臺幣三萬元以上", "reason": "理由"}))
    assert judgement.verdict == verdict
    assert judgement.evidence_quote == "處新臺幣三萬元以上"


def test_the_extractor_sends_a_constrained_deterministic_request():
    seen = {}

    def chat(host, body, timeout):
        seen.update(host=host, body=body)
        return json.dumps({"claims": [_claim()]})

    claims = OllamaClaimExtractor("http://gpu:11434", "qwen3.5:9b", 32768,
                                  chat=chat).extract(SPEECH)
    assert claims[0].law == "醫療法"
    body = seen["body"]
    assert body["model"] == "qwen3.5:9b"
    assert body["format"] == EXTRACT_SCHEMA
    assert body["stream"] is False
    assert body["options"] == {"num_ctx": 32768, "temperature": 0}
    prompt = body["messages"][-1]["content"]
    assert "邱慧洳" in prompt and "3萬到5萬" in prompt


def test_the_judge_numbers_its_evidence():
    evidence = [Evidence(SourceKind.LAW, "醫療法 第一百零六條", "https://a", "https://b",
                         "處新臺幣三萬元以上五萬元以下罰鍰")]
    prompt = build_judge_messages("罰鍰 3 萬到 5 萬", evidence)[-1]["content"]
    assert "[1] 醫療法 第一百零六條" in prompt
    assert "處新臺幣三萬元以上五萬元以下罰鍰" in prompt

    judge = OllamaTextJudge("http://gpu:11434", "qwen3.5:9b", 32768, chat=lambda h, b, t: json.dumps(
        {"verdict": "supported", "evidence_quote": "三萬元以上", "reason": "相同"}))
    assert judge.judge("罰鍰 3 萬到 5 萬", evidence).verdict == Verdict.SUPPORTED
```

- [ ] **Step 2: 確認測試失敗**

Run: `uv run pytest tests/factcheck/test_ollama.py -q`
Expected: FAIL（模組不存在）

- [ ] **Step 3: 實作 `src/factcheck/infrastructure/ollama.py`**

```python
"""主張抽取器與文字判讀器的 Ollama 實作。

上半是純函式（schema、提示詞、回應解析），可離線測試；下半是 HTTP，透過
注入的 chat 函式呼叫，測試時換成假的。模型只做兩件事：挑出主張、判讀文字。
它不給分數，也不判斷數字對不對。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence

from ..domain.entities import ClaimKind, Evidence, ExtractedClaim, Judgement, Speech, Verdict
from ..domain.errors import ModelOutputInvalid, ModelUnavailable

MAX_CLAIMS = 8

Chat = Callable[[str, dict, float], str]

# 傳給 Ollama 的 format：文法層級約束，格式錯誤幾乎不可能發生
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {"claims": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "quote": {"type": "string"},
            "kind": {"type": "string", "enum": [k.value for k in ClaimKind]},
            "statement": {"type": "string"},
            "figures": {"type": "array", "items": {"type": "string"}},
            "law": {"type": "string"},
            "article": {"type": "string"},
            "bill_keywords": {"type": "string"},
            "proposer": {"type": "string"},
        },
        # 全部放進 required：小模型會直接省略選填欄位
        "required": ["quote", "kind", "statement", "figures", "law", "article",
                     "bill_keywords", "proposer"],
    }}},
    "required": ["claims"],
}

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["supported", "contradicted", "insufficient"]},
        "evidence_quote": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "evidence_quote", "reason"],
}

_EXTRACT_SYSTEM = ("你是立法院的事實查核助理。你的工作是從委員的發言逐字稿中，挑出"
                   "「可以用官方法律條文或議案資料驗證」的具體事實陳述。你不判斷對錯。")

_EXTRACT_RULES = f"""規則：
1. 只挑三種可以查證的陳述：
   - law_article：關於現行法律條文的內容，例如罰鍰金額、刑期、期限。必須說得出是哪一部法律的第幾條；委員沒講條號也可以依內容推斷，推斷不出就不要挑。
   - bill_content：關於某個議案或草案的內容，例如某個版本編列多少預算、要把罰則提高到多少。
   - bill_status：關於議案的進度，例如已經三讀、還在委員會審查。
2. 不要挑意見、評價、預測、承諾、口號，也不要挑無法用法律或議案資料驗證的統計數字。
3. quote 必須從逐字稿逐字複製，連錯字、空白都照抄，不要修正、不要改寫、不要加標點。逐字稿由語音辨識產生，錯字很多，照抄即可。
4. figures：quote 裡跟這個主張有關的數字片段，逐字複製（例如「3萬到5萬」「6年2100億」）；沒有數字就給空陣列。同一句話如果同時講了現行規定和提案要改成多少，要拆成兩則主張，各自只放自己的數字。
5. statement：用一句話把主張講清楚，補上主詞（例如「醫療法現行對妨礙醫療業務者的罰鍰為3萬到5萬元」）。
6. law：法律全名（例如「醫療法」），不是 law_article 就給空字串。article：條號（例如「第106條」），不是 law_article 就給空字串。
   bill_keywords：議案名稱裡會出現的關鍵詞，越短越好（例如「無人載具」），可以用空白分隔多個；不是議案就給空字串。
   proposer：提案者（例如「行政院」「國民黨黨團」「民進黨黨團」「台灣民眾黨黨團」或委員姓名）；不知道就給空字串。
7. 最多 {MAX_CLAIMS} 則。沒有可查證的陳述就回傳空陣列。"""

_JUDGE_SYSTEM = "你是事實查核助理。你只根據提供的證據判斷，不使用自己的知識。"

_JUDGE_RULES = """請判斷證據是否支持這個主張：
- supported：證據明確支持主張
- contradicted：證據明確與主張衝突
- insufficient：證據不足以判斷（證據沒提到，或只提到一部分）
evidence_quote：從證據中逐字複製最關鍵的一句作為依據；insufficient 時給空字串。
reason：用一句話說明理由。"""

_VERDICTS = {"supported": Verdict.SUPPORTED, "contradicted": Verdict.CONTRADICTED}


def build_extract_messages(speech: Speech) -> list[dict]:
    user = (f"委員：{speech.speaker}\n日期：{speech.date.isoformat()}\n會議：{speech.meeting}\n\n"
            f"{_EXTRACT_RULES}\n\n逐字稿：\n{speech.transcript}")
    return [{"role": "system", "content": _EXTRACT_SYSTEM}, {"role": "user", "content": user}]


def build_judge_messages(statement: str, evidence: Sequence[Evidence]) -> list[dict]:
    blocks = "\n\n".join(f"[{i}] {e.title}\n{e.excerpt}" for i, e in enumerate(evidence, 1))
    user = f"主張：{statement}\n\n證據：\n{blocks}\n\n{_JUDGE_RULES}"
    return [{"role": "system", "content": _JUDGE_SYSTEM}, {"role": "user", "content": user}]


def _text(value: object, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _load(payload: str) -> dict:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        raise ModelOutputInvalid(f"模型回應不是 JSON：{payload[:120]}") from e
    if not isinstance(data, dict):
        raise ModelOutputInvalid("模型回應不是 JSON 物件")
    return data


def parse_claims(payload: str) -> list[ExtractedClaim]:
    items = _load(payload).get("claims")
    if not isinstance(items, list):
        raise ModelOutputInvalid("模型回應缺少 claims 陣列")
    claims: list[ExtractedClaim] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            kind = ClaimKind(item.get("kind"))
        except ValueError:
            continue
        quote = _text(item.get("quote"), 500)
        if not quote:
            continue
        raw_figures = item.get("figures") if isinstance(item.get("figures"), list) else []
        claims.append(ExtractedClaim(
            quote=quote,
            kind=kind,
            statement=_text(item.get("statement"), 300) or quote,
            figures=tuple(f for f in (_text(x, 60) for x in raw_figures) if f),
            law=_text(item.get("law"), 100),
            article=_text(item.get("article"), 30),
            bill_keywords=_text(item.get("bill_keywords"), 60),
            proposer=_text(item.get("proposer"), 60),
        ))
        if len(claims) >= MAX_CLAIMS:
            break
    return claims


def parse_judgement(payload: str) -> Judgement:
    data = _load(payload)
    return Judgement(
        verdict=_VERDICTS.get(str(data.get("verdict") or ""), Verdict.UNVERIFIABLE),
        evidence_quote=_text(data.get("evidence_quote"), 500),
        reason=_text(data.get("reason"), 300),
    )


# --- HTTP ---


def _chat(host: str, body: dict, timeout: float) -> str:
    request = urllib.request.Request(
        f"{host.rstrip('/')}/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        raise ModelUnavailable(f"Ollama 回應錯誤 {e.code}：{detail}") from e
    except (OSError, ValueError) as e:
        raise ModelUnavailable(f"連不上 Ollama（{host}）：{e}") from e
    if payload.get("error"):
        raise ModelUnavailable(str(payload["error"]))
    return str((payload.get("message") or {}).get("content") or "")


def _body(model: str, num_ctx: int, messages: list[dict], schema: dict) -> dict:
    return {
        "model": model,
        "messages": messages,
        "format": schema,
        "stream": False,
        # num_ctx 必須顯式設定：Ollama 預設 context 很小，長逐字稿會被靜默截斷。
        # temperature 0：同一段發言每次抽出的主張要一樣，否則重跑會換一批。
        "options": {"num_ctx": num_ctx, "temperature": 0},
    }


class OllamaClaimExtractor:
    def __init__(self, host: str, model: str, num_ctx: int, timeout: float = 600.0,
                 chat: Chat = _chat):
        self._host, self._model, self._num_ctx = host, model, num_ctx
        self._timeout = timeout
        self._chat = chat

    def extract(self, speech: Speech) -> list[ExtractedClaim]:
        body = _body(self._model, self._num_ctx, build_extract_messages(speech), EXTRACT_SCHEMA)
        return parse_claims(self._chat(self._host, body, self._timeout))


class OllamaTextJudge:
    def __init__(self, host: str, model: str, num_ctx: int, timeout: float = 300.0,
                 chat: Chat = _chat):
        self._host, self._model, self._num_ctx = host, model, num_ctx
        self._timeout = timeout
        self._chat = chat

    def judge(self, statement: str, evidence: Sequence[Evidence]) -> Judgement:
        body = _body(self._model, self._num_ctx, build_judge_messages(statement, evidence),
                     JUDGE_SCHEMA)
        return parse_judgement(self._chat(self._host, body, self._timeout))
```

- [ ] **Step 4: 確認測試通過**

Run: `uv run pytest tests/factcheck -q`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add src/factcheck/infrastructure/ollama.py tests/factcheck/test_ollama.py
git commit -m "feat(factcheck): Ollama 抽取器與判讀器"
```

---

### Task 6: 查核編排與組裝

**Files:**
- Create: `src/factcheck/usecases/check.py`、`src/factcheck/composition.py`
- Test: `tests/factcheck/test_check.py`

**Interfaces:**
- Consumes: Task 1～5 的全部公開名稱
- Produces:
  - `factcheck.usecases.check`: `FactCheckUseCase(extractor, sources: Mapping[ClaimKind, EvidenceSource], judge)`，`execute(speech, progress, is_cancelled) -> list[CheckedClaim]`
  - `factcheck.composition`: `build_factcheck(ollama_host: str, model: str, num_ctx: int, lyapi_base: str = DEFAULT_BASE) -> FactCheckUseCase`

- [ ] **Step 1: 寫失敗測試**

`tests/factcheck/test_check.py`：

```python
"""查核編排：用假的抽取器、來源與判讀器，不碰網路也不碰模型。"""
from __future__ import annotations

from datetime import date

import pytest

from factcheck.domain.entities import (
    ClaimKind,
    Evidence,
    ExtractedClaim,
    Judgement,
    Method,
    SourceKind,
    Speech,
    Verdict,
)
from factcheck.domain.errors import ModelOutputInvalid, OperationCancelled, SourceUnavailable
from factcheck.usecases.check import FactCheckUseCase

TRANSCRIPT = ("00:16 醫療暴力層出不窮\n"
              "00:32 他的行政罰鍰從現行的3萬到5萬提升到5到25萬\n"
              "01:04 希望能夠健全我們臺灣的醫療環境\n")
SPEECH = Speech("邱慧洳", date(2026, 8, 25), "第11屆第5會期第23次會議", TRANSCRIPT)
ARTICLE_106 = Evidence(SourceKind.LAW, "醫療法 第一百零六條", "https://law", "https://api",
                       "違反第二十四條第二項規定者，處新臺幣三萬元以上五萬元以下罰鍰。")


def _claim(quote="他的行政罰鍰從現行的3萬到5萬", figures=("3萬到5萬",),
           kind=ClaimKind.LAW_ARTICLE, statement="醫療法現行罰鍰為 3 萬到 5 萬元"):
    return ExtractedClaim(quote=quote, kind=kind, statement=statement, figures=figures,
                          law="醫療法", article="第24條")


class FakeExtractor:
    def __init__(self, claims):
        self.claims = claims

    def extract(self, speech):
        return list(self.claims)


class FakeSource:
    def __init__(self, evidence=(ARTICLE_106,), error=None):
        self.evidence = list(evidence)
        self.error = error
        self.calls = []

    def find(self, claim, on):
        self.calls.append((claim, on))
        if self.error is not None:
            raise self.error
        return list(self.evidence)


class FakeJudge:
    def __init__(self, judgement=None, error=None):
        self.judgement = judgement or Judgement(Verdict.SUPPORTED, "處新臺幣三萬元以上五萬元以下罰鍰", "條文相同")
        self.error = error
        self.calls = 0

    def judge(self, statement, evidence):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.judgement


def _run(claims, source=None, judge=None, cancelled=lambda: False):
    usecase = FactCheckUseCase(FakeExtractor(claims),
                               {ClaimKind.LAW_ARTICLE: source or FakeSource()},
                               judge or FakeJudge())
    return usecase.execute(SPEECH, lambda fraction, status: None, cancelled)


def test_numbers_are_decided_by_code_not_the_model():
    judge = FakeJudge()
    [checked] = _run([_claim()], judge=judge)
    assert checked.verdict == Verdict.SUPPORTED
    assert checked.method == Method.NUMERIC
    assert checked.evidence == (ARTICLE_106,)
    assert judge.calls == 0


def test_the_timestamp_comes_from_the_transcript():
    [checked] = _run([_claim()])
    assert checked.timestamp == 32.0


def test_the_source_is_asked_about_the_speech_day():
    source = FakeSource()
    _run([_claim()], source=source)
    assert source.calls[0][1] == date(2026, 8, 25)


def test_a_quote_that_is_not_in_the_transcript_is_dropped():
    """攔的行為：模型改寫或捏造委員的話。那不是他說的，不能拿來查核他。"""
    assert _run([_claim(quote="現行罰鍰是三萬到五萬元")]) == []


def test_figures_that_are_not_in_the_quote_are_ignored():
    """模型給了 quote 裡沒有的數字：不能拿它去比，改走文字判讀。"""
    judge = FakeJudge()
    [checked] = _run([_claim(figures=("10萬",))], judge=judge)
    assert checked.method == Method.MODEL
    assert judge.calls == 1


def test_no_evidence_is_unverifiable():
    [checked] = _run([_claim()], source=FakeSource(evidence=()))
    assert checked.verdict == Verdict.UNVERIFIABLE
    assert checked.method == Method.NONE
    assert "查無" in checked.rationale


def test_an_unavailable_source_only_affects_that_claim():
    [checked] = _run([_claim()], source=FakeSource(error=SourceUnavailable("503")))
    assert checked.verdict == Verdict.UNVERIFIABLE
    assert "暫時無法取得" in checked.rationale


def test_a_kind_without_a_source_is_unverifiable():
    [checked] = _run([_claim(kind=ClaimKind.BILL_STATUS)])
    assert checked.verdict == Verdict.UNVERIFIABLE


def test_the_model_decides_when_there_are_no_numbers():
    [checked] = _run([_claim(figures=())])
    assert checked.verdict == Verdict.SUPPORTED
    assert checked.method == Method.MODEL
    assert "處新臺幣三萬元以上五萬元以下罰鍰" in checked.rationale


def test_a_model_quote_that_is_not_in_the_evidence_voids_the_judgement():
    """攔的行為：模型引用一句證據裡沒有的話來撐它的判斷。"""
    judge = FakeJudge(Judgement(Verdict.CONTRADICTED, "處新臺幣十萬元以上罰鍰", "金額不同"))
    [checked] = _run([_claim(figures=())], judge=judge)
    assert checked.verdict == Verdict.UNVERIFIABLE
    assert "作廢" in checked.rationale


def test_an_unreadable_judgement_is_unverifiable():
    [checked] = _run([_claim(figures=())], judge=FakeJudge(error=ModelOutputInvalid("x")))
    assert checked.verdict == Verdict.UNVERIFIABLE


def test_cancelling_stops_before_the_next_claim():
    with pytest.raises(OperationCancelled):
        _run([_claim()], cancelled=lambda: True)
```

- [ ] **Step 2: 確認測試失敗**

Run: `uv run pytest tests/factcheck/test_check.py -q`
Expected: FAIL（模組不存在）

- [ ] **Step 3: 實作 `src/factcheck/usecases/check.py`**

```python
"""查核一段發言：抽取 → 落地 → 取證 → 比對。

判定權的分配：數字由程式比，文字由模型判讀但引用要經程式確認。分數不在
這裡算——那是公開公式的事，放在 Pi 上。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..domain.entities import (
    CheckedClaim,
    ClaimKind,
    Evidence,
    ExtractedClaim,
    Method,
    Speech,
    Verdict,
)
from ..domain.errors import ModelOutputInvalid, OperationCancelled, SourceUnavailable
from ..domain.ports import CancelCheck, ClaimExtractor, EvidenceSource, ProgressCallback, TextJudge
from .compare import compare_numbers
from .grounding import is_grounded, locate
from .numbers import has_approximation


def _unverifiable(claim: ExtractedClaim, at: float, reason: str,
                  evidence: Sequence[Evidence] = ()) -> CheckedClaim:
    return CheckedClaim(claim, at, Verdict.UNVERIFIABLE, Method.NONE, reason, tuple(evidence))


class FactCheckUseCase:
    def __init__(self, extractor: ClaimExtractor,
                 sources: Mapping[ClaimKind, EvidenceSource], judge: TextJudge):
        self._extractor = extractor
        self._sources = dict(sources)
        self._judge = judge

    def execute(self, speech: Speech, progress: ProgressCallback,
                is_cancelled: CancelCheck) -> list[CheckedClaim]:
        progress(None, "抽取可查證的陳述…")
        grounded: list[tuple[ExtractedClaim, float]] = []
        for claim in self._extractor.extract(speech):
            at = locate(claim.quote, speech.transcript)
            # 逐字稿裡找不到：模型改寫或捏造了這句話，那不是委員說的
            if at is not None:
                grounded.append((claim, at))

        results: list[CheckedClaim] = []
        for i, (claim, at) in enumerate(grounded):
            if is_cancelled():
                raise OperationCancelled()
            progress(i / len(grounded), f"查核第 {i + 1}/{len(grounded)} 則")
            results.append(self._check(claim, at, speech))
        progress(1.0, f"查核完成，共 {len(results)} 則")
        return results

    def _check(self, claim: ExtractedClaim, at: float, speech: Speech) -> CheckedClaim:
        source = self._sources.get(claim.kind)
        if source is None:
            return _unverifiable(claim, at, "沒有這類主張的資料來源")
        try:
            evidence = tuple(source.find(claim, speech.date))
        except SourceUnavailable as e:
            return _unverifiable(claim, at, f"資料來源暫時無法取得：{e}"[:300])
        if not evidence:
            return _unverifiable(claim, at, "查無對應的法條或議案")

        # 只拿 quote 裡真的有的數字去比：模型給的數字可能是它自己補的
        figures = [f for f in claim.figures if is_grounded(f, claim.quote)]
        outcome = compare_numbers(figures, [e.excerpt for e in evidence],
                                  approximate=has_approximation(claim.quote))
        if outcome.verdict is not None:
            return CheckedClaim(claim, at, outcome.verdict, Method.NUMERIC,
                                outcome.rationale, evidence)
        return self._judge_text(claim, at, evidence)

    def _judge_text(self, claim: ExtractedClaim, at: float,
                    evidence: tuple[Evidence, ...]) -> CheckedClaim:
        try:
            judgement = self._judge.judge(claim.statement, evidence)
        except ModelOutputInvalid:
            return _unverifiable(claim, at, "模型的判讀無法解讀", evidence)
        if judgement.verdict == Verdict.UNVERIFIABLE:
            return CheckedClaim(claim, at, Verdict.UNVERIFIABLE, Method.MODEL,
                                judgement.reason or "證據不足以判斷", evidence)
        if not is_grounded(judgement.evidence_quote, "\n".join(e.excerpt for e in evidence)):
            return CheckedClaim(claim, at, Verdict.UNVERIFIABLE, Method.MODEL,
                                "模型引用的句子不在證據中，判讀作廢", evidence)
        return CheckedClaim(claim, at, judgement.verdict, Method.MODEL,
                            f"{judgement.reason}（依據：「{judgement.evidence_quote}」）",
                            evidence)
```

- [ ] **Step 4: 實作 `src/factcheck/composition.py`**

```python
"""組裝：把 LYAPI、法條／議案來源與 Ollama 接到 use case 上。

換模型或換資料來源只動這裡。
"""
from __future__ import annotations

from .domain.entities import ClaimKind
from .infrastructure.bill_source import BillSource
from .infrastructure.law_source import LawSource
from .infrastructure.lyapi import DEFAULT_BASE, LyApi
from .infrastructure.ollama import OllamaClaimExtractor, OllamaTextJudge
from .usecases.check import FactCheckUseCase


def build_factcheck(ollama_host: str, model: str, num_ctx: int,
                    lyapi_base: str = DEFAULT_BASE) -> FactCheckUseCase:
    api = LyApi(lyapi_base)
    bills = BillSource(api)
    return FactCheckUseCase(
        extractor=OllamaClaimExtractor(ollama_host, model, num_ctx),
        sources={
            ClaimKind.LAW_ARTICLE: LawSource(api),
            ClaimKind.BILL_CONTENT: bills,
            ClaimKind.BILL_STATUS: bills,
        },
        judge=OllamaTextJudge(ollama_host, model, num_ctx),
    )
```

- [ ] **Step 5: 確認測試通過，並確認組裝不需要網路**

Run: `uv run pytest tests/factcheck -q && uv run python -c "import sys; sys.path.insert(0, 'src'); from factcheck.composition import build_factcheck; print(type(build_factcheck('http://localhost:11434', 'qwen3.5:9b', 32768)).__name__)"`
Expected: 測試全部 PASS，最後印出 `FactCheckUseCase`

- [ ] **Step 6: Commit**

```bash
git add src/factcheck/usecases/check.py src/factcheck/composition.py tests/factcheck/test_check.py
git commit -m "feat(factcheck): 查核編排與組裝"
```

---

### Task 7: GPU API 的查核工作

**Files:**
- Modify: `src/slidebox_api/jobs.py`（`JobKind`、`Job.kind`、`Job.params`、`JobStore.submit`）
- Modify: `src/slidebox_api/runner.py`（新增 `KindDispatcher`、`FactCheckExecutor`）
- Create: `src/slidebox_api/factcheck_payload.py`
- Modify: `src/slidebox_api/app.py`（`FactCheckRequest`、`POST /factchecks`、`JobView.kind`）
- Modify: `serve_api.py`（`build()` 組裝 dispatcher）
- Modify: `README.md`（摘要 API 的端點表）
- Test: `tests/slidebox_api/test_jobs.py`、`tests/slidebox_api/test_runner.py`、`tests/slidebox_api/test_app.py`（都是追加）、`tests/slidebox_api/test_factcheck_payload.py`（新）

**Interfaces:**
- Consumes: Task 6 的 `FactCheckUseCase`、`build_factcheck`；Task 1 的實體與 `OperationCancelled`
- Produces:
  - `slidebox_api.jobs.JobKind`（`DECK = "deck"`、`FACTCHECK = "factcheck"`）；`JobStore.submit(url, ..., kind=JobKind.DECK, params=None)`
  - `slidebox_api.runner.KindDispatcher(executors: Mapping[JobKind, Executor])`、`FactCheckExecutor(usecase, model)`
  - `slidebox_api.factcheck_payload.factcheck_payload(checked, model) -> dict`，形狀：`{"model": str, "claims": [{"quote", "timestamp", "kind", "statement", "subject": {law?, article?, bill_keywords?, proposer?}, "verdict", "method", "rationale", "evidence": [{"source", "title", "official_url", "api_url", "excerpt"}]}]}`
  - HTTP：`POST /factchecks`，body `{source_url, speaker, date: "YYYY-MM-DD", meeting, transcript_text}` → `202` 的 JobView（多一個 `kind`）；結果一樣用 `GET /jobs/{id}` 取

- [ ] **Step 1: 寫失敗測試**

追加到 `tests/slidebox_api/test_jobs.py` 最後（沿用該檔既有的 import 風格，缺的 import 補在檔頭）：

```python
from slidebox_api.jobs import JobKind


def test_jobs_are_decks_unless_told_otherwise():
    store = JobStore()
    assert store.submit("https://ivod.ly.gov.tw/Play/Clip/1M/1").kind == JobKind.DECK


def test_a_factcheck_job_keeps_its_parameters():
    store = JobStore()
    params = {"speaker": "邱慧洳", "date": "2026-08-25"}
    job = store.submit("https://ivod.ly.gov.tw/Play/Clip/1M/1", kind=JobKind.FACTCHECK,
                       params=params)
    params["speaker"] = "改掉了"
    assert job.kind == JobKind.FACTCHECK
    assert job.params["speaker"] == "邱慧洳"
```

新檔 `tests/slidebox_api/test_factcheck_payload.py`：

```python
"""查核成品轉 JSON。這是給 Django 的 HTTP 契約。"""
from __future__ import annotations

from factcheck.domain.entities import (
    CheckedClaim, ClaimKind, Evidence, ExtractedClaim, Method, SourceKind, Verdict,
)
from slidebox_api.factcheck_payload import factcheck_payload


def test_payload_shape():
    claim = ExtractedClaim(quote="3萬到5萬", kind=ClaimKind.LAW_ARTICLE, statement="罰鍰",
                           figures=("3萬到5萬",), law="醫療法", article="第24條")
    evidence = Evidence(SourceKind.LAW, "醫療法 第一百零六條", "https://law", "https://api", "三萬元")
    payload = factcheck_payload(
        [CheckedClaim(claim, 32.0, Verdict.SUPPORTED, Method.NUMERIC, "相同", (evidence,))],
        "qwen3.5:9b")
    assert payload["model"] == "qwen3.5:9b"
    [item] = payload["claims"]
    assert item["verdict"] == "supported"
    assert item["method"] == "numeric"
    assert item["kind"] == "law_article"
    assert item["timestamp"] == 32.0
    assert item["subject"] == {"law": "醫療法", "article": "第24條"}
    assert item["evidence"] == [{"source": "law", "title": "醫療法 第一百零六條",
                                 "official_url": "https://law", "api_url": "https://api",
                                 "excerpt": "三萬元"}]
```

追加到 `tests/slidebox_api/test_runner.py` 最後：

```python
import pytest
from datetime import date as date_type

from factcheck.domain.entities import (
    CheckedClaim, ClaimKind, ExtractedClaim, Method, Verdict,
)
from factcheck.domain.errors import OperationCancelled as FactCheckCancelled
from slidebox.domain.errors import OperationCancelled
from slidebox_api.jobs import JobKind
from slidebox_api.runner import FactCheckExecutor, KindDispatcher

FACTCHECK_PARAMS = {"speaker": "邱慧洳", "date": "2026-08-25", "meeting": "院會",
                    "transcript_text": "00:32 他的行政罰鍰從現行的3萬到5萬"}


class FakeFactCheck:
    def __init__(self, error=None):
        self.speeches = []
        self.error = error

    def execute(self, speech, progress, is_cancelled):
        self.speeches.append(speech)
        if self.error is not None:
            raise self.error
        claim = ExtractedClaim(quote="3萬到5萬", kind=ClaimKind.LAW_ARTICLE, statement="罰鍰")
        return [CheckedClaim(claim, 32.0, Verdict.SUPPORTED, Method.NUMERIC, "相同")]


def test_the_dispatcher_routes_by_kind():
    store = JobStore()
    seen = []
    dispatcher = KindDispatcher({
        JobKind.DECK: lambda job, p, c: seen.append("deck") or {},
        JobKind.FACTCHECK: lambda job, p, c: seen.append("factcheck") or {},
    })
    dispatcher(store.submit(IVOD, kind=JobKind.FACTCHECK, params={}), lambda f, s: None,
               lambda: False)
    assert seen == ["factcheck"]


def test_an_unknown_kind_fails_the_job():
    store = JobStore()
    with pytest.raises(ValueError):
        KindDispatcher({})(store.submit(IVOD), lambda f, s: None, lambda: False)


def test_the_factcheck_executor_builds_the_speech_from_the_job():
    usecase = FakeFactCheck()
    store = JobStore()
    job = store.submit(IVOD, kind=JobKind.FACTCHECK, params=FACTCHECK_PARAMS)
    payload = FactCheckExecutor(usecase, "qwen3.5:9b")(job, lambda f, s: None, lambda: False)
    speech = usecase.speeches[0]
    assert speech.speaker == "邱慧洳"
    assert speech.date == date_type(2026, 8, 25)
    assert payload["claims"][0]["verdict"] == "supported"


def test_factcheck_cancellation_becomes_the_workers_cancellation():
    """工作執行緒只認得 slidebox 的 OperationCancelled；不轉譯的話取消會被記成失敗。"""
    store = JobStore()
    job = store.submit(IVOD, kind=JobKind.FACTCHECK, params=FACTCHECK_PARAMS)
    with pytest.raises(OperationCancelled):
        FactCheckExecutor(FakeFactCheck(error=FactCheckCancelled()), "m")(
            job, lambda f, s: None, lambda: True)
```

追加到 `tests/slidebox_api/test_app.py` 最後：

```python
FACTCHECK = {"source_url": IVOD, "speaker": "邱慧洳", "date": "2026-08-25",
             "meeting": "第11屆第5會期第23次會議",
             "transcript_text": "00:32 他的行政罰鍰從現行的3萬到5萬"}


def test_a_factcheck_is_queued_as_its_own_kind(kit):
    client, store, *_ = kit
    response = client.post("/factchecks", json=FACTCHECK)
    assert response.status_code == 202
    body = response.json()
    assert body["kind"] == "factcheck"
    job = store.get(body["id"])
    assert job.params["speaker"] == "邱慧洳"
    assert job.params["date"] == "2026-08-25"


def test_summary_jobs_report_their_kind(kit):
    client, *_ = kit
    assert client.post("/jobs", json={"url": IVOD}).json()["kind"] == "deck"


@pytest.mark.parametrize("change", [
    {"source_url": "file:///etc/passwd"},
    {"transcript_text": ""},
    {"date": "昨天"},
    {"speaker": ""},
])
def test_invalid_factchecks_are_rejected(kit, change):
    client, *_ = kit
    assert client.post("/factchecks", json={**FACTCHECK, **change}).status_code == 422


def test_factchecks_require_the_key_when_one_is_set():
    store = JobStore()
    worker = JobWorker(store, FakeExecutor())
    with TestClient(_app(store, worker, api_key="secret")) as client:
        worker.stop()
        assert client.post("/factchecks", json=FACTCHECK).status_code == 401
        assert client.post("/factchecks", json=FACTCHECK,
                           headers={"X-API-Key": "secret"}).status_code == 202
```

- [ ] **Step 2: 確認新測試失敗**

Run: `uv run pytest tests/slidebox_api -q`
Expected: FAIL（`JobKind` 等名稱不存在）

- [ ] **Step 3: 修改 `src/slidebox_api/jobs.py`**

在 `JobStatus` 之後加：

```python
class JobKind(StrEnum):
    """工作種類。兩種共用同一條佇列：都要用 Ollama，而 GPU 只有一張卡。"""
    DECK = "deck"
    FACTCHECK = "factcheck"
```

`Job` 在 `model` 欄位之後加兩個欄位：

```python
    kind: JobKind = JobKind.DECK
    # 各種類自己的輸入（查核用：speaker、date、meeting、transcript_text）
    params: dict = field(default_factory=dict)
```

`JobStore.submit` 改成：

```python
    def submit(self, url: str, detailed: bool = True, min_slides: int | None = None,
               max_slides: int | None = None, model: str | None = None,
               kind: JobKind = JobKind.DECK, params: dict | None = None) -> Job:
        job = Job(id=uuid.uuid4().hex, url=url, detailed=detailed,
                  min_slides=min_slides, max_slides=max_slides, model=model,
                  # 複製一份：呼叫端之後改自己的 dict 不該改到已經排隊的工作
                  kind=kind, params=dict(params or {}))
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._forget_old_unlocked()
        return job
```

- [ ] **Step 4: 建立 `src/slidebox_api/factcheck_payload.py`**

```python
"""把查核結果轉成新聞服務要的 JSON。這是 HTTP 契約，改欄位名等於改 API。"""
from __future__ import annotations

from collections.abc import Sequence

from factcheck.domain.entities import CheckedClaim


def factcheck_payload(checked: Sequence[CheckedClaim], model: str) -> dict:
    return {"model": model, "claims": [_claim(c) for c in checked]}


def _claim(checked: CheckedClaim) -> dict:
    claim = checked.claim
    subject = {"law": claim.law, "article": claim.article,
               "bill_keywords": claim.bill_keywords, "proposer": claim.proposer}
    return {
        "quote": claim.quote,
        "timestamp": checked.timestamp,
        "kind": str(claim.kind),
        "statement": claim.statement,
        "subject": {key: value for key, value in subject.items() if value},
        "verdict": str(checked.verdict),
        "method": str(checked.method),
        "rationale": checked.rationale,
        "evidence": [{
            "source": str(e.source),
            "title": e.title,
            "official_url": e.official_url,
            "api_url": e.api_url,
            "excerpt": e.excerpt,
        } for e in checked.evidence],
    }
```

- [ ] **Step 5: 修改 `src/slidebox_api/runner.py`**

檔頭 import 補上：

```python
from collections.abc import Callable, Mapping
from datetime import date

from factcheck.domain.entities import Speech
from factcheck.domain.errors import OperationCancelled as FactCheckCancelled
from factcheck.usecases.check import FactCheckUseCase

from .factcheck_payload import factcheck_payload
from .jobs import Job, JobKind, JobStore
```

（原本的 `from collections.abc import Callable` 與 `from .jobs import Job, JobStore` 換成上面這兩行。）檔尾加：

```python
class KindDispatcher:
    """依工作種類分派執行函式。佇列只有一條：摘要與查核都要用 Ollama。"""

    def __init__(self, executors: Mapping[JobKind, Executor]):
        self._executors = dict(executors)

    def __call__(self, job: Job, progress: ProgressCallback,
                 is_cancelled: CancelCheck) -> dict:
        execute = self._executors.get(job.kind)
        if execute is None:
            raise ValueError(f"不支援的工作種類：{job.kind}")
        return execute(job, progress, is_cancelled)


class FactCheckExecutor:
    """用 factcheck 查核一段發言。use case 整個行程只建一次（它快取法條）。"""

    def __init__(self, usecase: FactCheckUseCase, model: str):
        self._usecase = usecase
        self._model = model

    def __call__(self, job: Job, progress: ProgressCallback,
                 is_cancelled: CancelCheck) -> dict:
        params = job.params
        speech = Speech(
            speaker=str(params["speaker"]),
            date=date.fromisoformat(str(params["date"])),
            meeting=str(params.get("meeting") or ""),
            transcript=str(params["transcript_text"]),
        )
        try:
            checked = self._usecase.execute(speech, progress, is_cancelled)
        except FactCheckCancelled as e:
            # 工作執行緒只認得 slidebox 的取消；不轉譯的話會被記成失敗
            raise OperationCancelled() from e
        return factcheck_payload(checked, self._model)
```

- [ ] **Step 6: 修改 `src/slidebox_api/app.py`**

import 區：`from datetime import date as date_type`；`from .jobs import Job, JobKind, JobStore`。在 `MAX_SLIDES` 後加 `MAX_TRANSCRIPT = 200_000`。

`JobRequest` 之後加：

```python
class FactCheckRequest(BaseModel):
    # 只用來識別與回溯，不會拿去下載——但一樣限定 http(s)
    source_url: str = Field(min_length=4)
    speaker: str = Field(min_length=1, max_length=100)
    date: date_type
    meeting: str = Field(default="", max_length=300)
    transcript_text: str = Field(min_length=1, max_length=MAX_TRANSCRIPT)
```

`JobView` 在 `url: str` 之後加 `kind: str = JobKind.DECK.value`；`_view` 的建構加上 `kind=job.kind`。

在 `@app.post("/jobs"...)` 那個函式之後加：

```python
    @app.post("/factchecks", status_code=202, dependencies=[Depends(require_key)])
    def submit_factcheck(request: FactCheckRequest) -> JobView:
        if urlparse(request.source_url).scheme not in ALLOWED_SCHEMES:
            raise HTTPException(status_code=422, detail="網址必須是 http 或 https")
        return _view(store.submit(request.source_url, kind=JobKind.FACTCHECK, params={
            "speaker": request.speaker,
            "date": request.date.isoformat(),
            "meeting": request.meeting,
            "transcript_text": request.transcript_text,
        }))
```

- [ ] **Step 7: 修改 `serve_api.py` 的組裝**

import 補：

```python
from factcheck.composition import build_factcheck
from slidebox_api.jobs import JobKind, JobStore
from slidebox_api.runner import FactCheckExecutor, JobWorker, KindDispatcher, SlideboxExecutor
```

`build()` 裡的 `executor = SlideboxExecutor(build_usecase(settings), settings)` 換成：

```python
    executor = KindDispatcher({
        JobKind.DECK: SlideboxExecutor(build_usecase(settings), settings),
        JobKind.FACTCHECK: FactCheckExecutor(
            build_factcheck(settings.ollama_host, settings.model, settings.num_ctx),
            settings.model),
    })
```

- [ ] **Step 8: 更新根目錄 `README.md` 的摘要 API 端點表**

在 `| POST /jobs | ... |` 那一列之後加一列：

```markdown
| `POST /factchecks` | `{source_url, speaker, date, meeting, transcript_text}` → `202 {id}`；事實查核，結果一樣用 `GET /jobs/{id}` 取 |
```

- [ ] **Step 9: 確認全部測試通過，服務能啟動**

Run: `uv run pytest -q`
Expected: 全部 PASS（原本 366 個加上新的）

Run: `uv run --group api python -c "import serve_api; print([r.path for r in serve_api.app.routes if 'fact' in r.path])"`
Expected: 印出 `['/factchecks']`

- [ ] **Step 10: Commit**

```bash
git add src/slidebox_api serve_api.py README.md tests/slidebox_api
git commit -m "feat(api): 事實查核工作掛進 GPU 佇列"
```

---

### Task 8: Django 的查核資料模型與查證相符率

**Files:**
- Create: `services/news/factchecks/__init__.py`（空檔）、`services/news/factchecks/apps.py`、`services/news/factchecks/models.py`、`services/news/factchecks/scoring.py`、`services/news/factchecks/migrations/0001_initial.py`（用 makemigrations 產生）
- Modify: `services/news/newsroom/settings.py`（`INSTALLED_APPS` 加 `"factchecks"`）
- Test: `services/news/factchecks/tests/__init__.py`（空檔）、`services/news/factchecks/tests/test_scoring.py`

**Interfaces:**
- Consumes: `articles.models.Article`、`ArticleStatus`
- Produces:
  - `factchecks.models`: `RunStatus`、`Verdict`、`Method`、`ClaimKind`、`ReviewStatus`（`AUTO="auto"`、`PENDING="pending_review"`、`APPROVED="approved"`、`REJECTED="rejected"`）、`PUBLIC_REVIEW_STATUSES`、`HUMAN_REVIEW_STATUSES`、`FactCheckRun`（`article` 一對一，`related_name="factcheck_run"`）、`Claim`（`article` FK，`related_name="claims"`）、`Evidence`（`claim` FK，`related_name="evidence"`）
  - `factchecks.scoring`: `MIN_SAMPLE = 5`、`Score(supported, partial, contradicted, unverifiable)`（屬性 `checked`、`rate`）、`score_of(verdicts) -> Score`、`public_claims() -> QuerySet[Claim]`、`scores_by_speaker() -> dict[str, Score]`

- [ ] **Step 1: 建立 app 與模型**

`services/news/factchecks/apps.py`：

```python
from django.apps import AppConfig


class FactchecksConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "factchecks"
    verbose_name = "事實查核"
```

`services/news/factchecks/models.py`：

```python
"""查核結果：一篇文章有多則主張，每則主張附上它的證據。

「不符」落地時一律待審：把具名的人標成講錯是這個功能唯一會傷人的地方，
而語音辨識的錯字最容易被誤判成不符。人工核准之前不公開、不計分。
"""
from __future__ import annotations

from django.db import models

from articles.models import Article


class RunStatus(models.TextChoices):
    PENDING = "pending", "等待查核"
    PROCESSING = "processing", "查核中"
    DONE = "done", "已完成"
    FAILED = "failed", "失敗"


class Verdict(models.TextChoices):
    SUPPORTED = "supported", "相符"
    PARTIAL = "partial", "部分相符"
    CONTRADICTED = "contradicted", "不符"
    UNVERIFIABLE = "unverifiable", "無法查證"


class Method(models.TextChoices):
    NUMERIC = "numeric", "數字比對"
    MODEL = "model", "模型判讀"
    NONE = "none", "未比對"


class ClaimKind(models.TextChoices):
    LAW_ARTICLE = "law_article", "法條內容"
    BILL_CONTENT = "bill_content", "議案內容"
    BILL_STATUS = "bill_status", "議案進度"


class ReviewStatus(models.TextChoices):
    AUTO = "auto", "自動發佈"
    PENDING = "pending_review", "待審核"
    APPROVED = "approved", "已核准"
    REJECTED = "rejected", "已駁回"


PUBLIC_REVIEW_STATUSES = (ReviewStatus.AUTO, ReviewStatus.APPROVED)
HUMAN_REVIEW_STATUSES = (ReviewStatus.APPROVED, ReviewStatus.REJECTED)


class FactCheckRun(models.Model):
    """一篇文章的查核進度。沿用匯入流程的規則：可重跑、嘗試次數有上限、
    斷線後先接回 GPU 上既有的工作。"""
    article = models.OneToOneField(Article, related_name="factcheck_run",
                                   on_delete=models.CASCADE)
    status = models.CharField(max_length=16, choices=RunStatus.choices,
                              default=RunStatus.PENDING, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    gpu_job_id = models.CharField(max_length=64, blank=True)
    error = models.TextField(blank=True)
    model = models.CharField(max_length=100, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.article} 查核（{self.get_status_display()}）"


class Claim(models.Model):
    article = models.ForeignKey(Article, related_name="claims", on_delete=models.CASCADE)
    index = models.PositiveIntegerField()
    quote = models.TextField()
    timestamp = models.FloatField(default=0.0)
    kind = models.CharField(max_length=20, choices=ClaimKind.choices)
    statement = models.TextField()
    subject = models.JSONField(default=dict, blank=True)
    verdict = models.CharField(max_length=16, choices=Verdict.choices, db_index=True)
    method = models.CharField(max_length=16, choices=Method.choices)
    rationale = models.TextField(blank=True)
    review_status = models.CharField(max_length=16, choices=ReviewStatus.choices,
                                     default=ReviewStatus.AUTO, db_index=True)
    reviewer_note = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["index"]
        unique_together = [("article", "index")]

    def __str__(self) -> str:
        return f"{self.article.ivod_id}#{self.index} {self.get_verdict_display()}"

    @property
    def is_public(self) -> bool:
        return self.review_status in PUBLIC_REVIEW_STATUSES


class Evidence(models.Model):
    claim = models.ForeignKey(Claim, related_name="evidence", on_delete=models.CASCADE)
    position = models.PositiveIntegerField()
    source = models.CharField(max_length=16)
    title = models.CharField(max_length=300)
    official_url = models.URLField(max_length=1000, blank=True)
    api_url = models.URLField(max_length=1000, blank=True)
    excerpt = models.TextField(blank=True)

    class Meta:
        ordering = ["position"]

    def __str__(self) -> str:
        return self.title
```

`services/news/newsroom/settings.py` 的 `INSTALLED_APPS`，在 `"articles",` 之後加 `"factchecks",`。

- [ ] **Step 2: 產生 migration**

Run: `cd services/news && uv run python manage.py makemigrations factchecks`
Expected: 建立 `factchecks/migrations/0001_initial.py`，含 `FactCheckRun`、`Claim`、`Evidence` 三個模型

- [ ] **Step 3: 寫查證相符率的失敗測試**

`services/news/factchecks/tests/test_scoring.py`：

```python
"""查證相符率：公開公式。"""
from __future__ import annotations

from datetime import date

from django.test import SimpleTestCase, TestCase

from articles.models import Article, ArticleStatus
from factchecks.models import Claim, ReviewStatus, Verdict
from factchecks.scoring import MIN_SAMPLE, Score, score_of, scores_by_speaker


class FormulaTests(SimpleTestCase):
    def test_no_claims_has_no_rate(self):
        self.assertIsNone(Score().rate)
        self.assertEqual(Score().checked, 0)

    def test_fewer_than_the_minimum_sample_has_no_rate(self):
        """一兩則就給百分比，一則不符就是 0%——那不是查證，是運氣。"""
        self.assertIsNone(Score(supported=MIN_SAMPLE - 1).rate)

    def test_laplace_smoothing(self):
        self.assertAlmostEqual(Score(supported=5).rate, 6 / 7)
        self.assertAlmostEqual(Score(supported=13, partial=1, contradicted=1).rate,
                               14.5 / 17)

    def test_unverifiable_claims_are_not_counted(self):
        score = Score(supported=5, unverifiable=20)
        self.assertEqual(score.checked, 5)
        self.assertAlmostEqual(score.rate, 6 / 7)

    def test_counting_verdicts(self):
        score = score_of(["supported", "supported", "partial", "contradicted", "unverifiable"])
        self.assertEqual((score.supported, score.partial, score.contradicted, score.unverifiable),
                         (2, 1, 1, 1))


class SpeakerScoreTests(TestCase):
    def _article(self, ivod_id, speaker, status=ArticleStatus.READY):
        return Article.objects.create(
            ivod_id=ivod_id, slug=f"2026-08-25-{ivod_id}", title="t", speaker=speaker,
            date=date(2026, 8, 25), ivod_url=f"https://ivod.ly.gov.tw/Play/Clip/1M/{ivod_id}",
            status=status)

    def _claim(self, article, index, verdict, review=ReviewStatus.AUTO):
        return Claim.objects.create(article=article, index=index, quote="q", kind="law_article",
                                    statement="s", verdict=verdict, method="numeric",
                                    review_status=review)

    def test_only_public_claims_of_published_articles_count(self):
        a = self._article("1", "邱慧洳")
        self._claim(a, 1, Verdict.SUPPORTED)
        self._claim(a, 2, Verdict.CONTRADICTED, ReviewStatus.PENDING)
        self._claim(a, 3, Verdict.CONTRADICTED, ReviewStatus.APPROVED)
        self._claim(a, 4, Verdict.SUPPORTED, ReviewStatus.REJECTED)
        hidden = self._article("2", "邱慧洳", status=ArticleStatus.FAILED)
        self._claim(hidden, 1, Verdict.SUPPORTED)
        score = scores_by_speaker()["邱慧洳"]
        self.assertEqual((score.supported, score.contradicted), (1, 1))

    def test_speakers_are_scored_separately(self):
        self._claim(self._article("1", "甲"), 1, Verdict.SUPPORTED)
        self._claim(self._article("2", "乙"), 1, Verdict.PARTIAL)
        scores = scores_by_speaker()
        self.assertEqual(scores["甲"].supported, 1)
        self.assertEqual(scores["乙"].partial, 1)
```

- [ ] **Step 4: 確認測試失敗**

Run: `cd services/news && uv run python manage.py test factchecks`
Expected: FAIL（`No module named 'factchecks.scoring'`）

- [ ] **Step 5: 實作 `services/news/factchecks/scoring.py`**

```python
"""查證相符率：公開公式，前端「查證方法」頁寫的就是這一條。

只算已公開的判定；「無法查證」不計入——查不到不代表說錯。拉普拉斯平滑
讓樣本少時往 50% 靠，一則不符不會變成 0%。n < MIN_SAMPLE 不給百分比。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from django.db.models import QuerySet

from articles.models import ArticleStatus

from .models import PUBLIC_REVIEW_STATUSES, Claim, Verdict

MIN_SAMPLE = 5


@dataclass(frozen=True)
class Score:
    supported: int = 0
    partial: int = 0
    contradicted: int = 0
    unverifiable: int = 0

    @property
    def checked(self) -> int:
        return self.supported + self.partial + self.contradicted

    @property
    def rate(self) -> float | None:
        if self.checked < MIN_SAMPLE:
            return None
        return (self.supported + 0.5 * self.partial + 1) / (self.checked + 2)


def score_of(verdicts: Iterable[str]) -> Score:
    counts = Counter(str(v) for v in verdicts)
    return Score(
        supported=counts[Verdict.SUPPORTED.value],
        partial=counts[Verdict.PARTIAL.value],
        contradicted=counts[Verdict.CONTRADICTED.value],
        unverifiable=counts[Verdict.UNVERIFIABLE.value],
    )


def public_claims() -> QuerySet[Claim]:
    """網站上看得到的判定：自動發佈的，加上人工核准的；文章本身也要已發佈。"""
    return Claim.objects.filter(review_status__in=PUBLIC_REVIEW_STATUSES,
                                article__status=ArticleStatus.READY)


def scores_by_speaker() -> dict[str, Score]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for speaker, verdict in public_claims().values_list("article__speaker", "verdict"):
        grouped[speaker].append(verdict)
    return {speaker: score_of(verdicts) for speaker, verdicts in grouped.items()}
```

- [ ] **Step 6: 確認測試通過，既有測試不受影響**

Run: `cd services/news && uv run python manage.py test`
Expected: 全部 PASS（既有 72 個加上新的）

- [ ] **Step 7: Commit**

```bash
git add services/news/factchecks services/news/newsroom/settings.py
git commit -m "feat(news): 查核資料模型與查證相符率"
```

---

### Task 9: Django 的查核流程與指令

**Files:**
- Modify: `services/news/articles/gpu_client.py`（新增 `submit_factcheck`）
- Create: `services/news/factchecks/runner.py`、`services/news/factchecks/management/__init__.py`（空檔）、`services/news/factchecks/management/commands/__init__.py`（空檔）、`services/news/factchecks/management/commands/factcheck_articles.py`
- Modify: `services/news/newsroom/settings.py`（`FACTCHECK_DAILY_LIMIT`）、`services/news/articles/management/commands/run_scheduler.py`、`services/news/README.md`
- Test: `services/news/articles/tests/test_gpu_client.py`（追加）、`services/news/factchecks/tests/test_runner.py`

**Interfaces:**
- Consumes: Task 8 的模型；`articles.gpu_client` 的 `GpuApiClient`、`GpuApiError`、`JobFailed`、`JobField`；`articles.ingest.RESUMABLE_STATUSES`；Task 7 的 HTTP 契約（`POST /factchecks` 與 payload 形狀）
- Produces:
  - `GpuApiClient.submit_factcheck(source_url, speaker, day: date, meeting, transcript_text) -> str`
  - `factchecks.runner`: `MAX_ATTEMPTS = 3`、`FactCheckReport`、`SavedClaims(total, pending)`、`candidates(limit, timeout) -> list[Article]`、`has_human_review(article) -> bool`、`save_claims(article, payload) -> SavedClaims`、`check_articles(client, limit, timeout) -> FactCheckReport`、`check_article(article, client, timeout, force=False) -> FactCheckReport`
  - 指令 `python manage.py factcheck_articles [--limit N] [--article IVOD_ID] [--force]`

- [ ] **Step 1: 寫 GPU 客戶端的失敗測試**

追加到 `services/news/articles/tests/test_gpu_client.py` 的 `GpuClientTests` 類別裡：

```python
    def test_a_factcheck_is_submitted_with_the_speech(self):
        from datetime import date
        client, transport, _ = _client([{"id": "fc-1", "status": "queued"}])
        job_id = client.submit_factcheck("https://ivod.ly.gov.tw/Play/Clip/1M/171140", "邱慧洳",
                                         date(2026, 8, 25), "院會", "00:32 逐字稿")
        self.assertEqual(job_id, "fc-1")
        call = transport.calls[0]
        self.assertTrue(call["url"].endswith("/factchecks"))
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["body"], {
            "source_url": "https://ivod.ly.gov.tw/Play/Clip/1M/171140", "speaker": "邱慧洳",
            "date": "2026-08-25", "meeting": "院會", "transcript_text": "00:32 逐字稿"})
```

（先看一下 `_client` 回傳的 tuple 順序，若不是 `(client, transport, clock)` 就照既有測試的用法調整解包。）

- [ ] **Step 2: 寫查核流程的失敗測試**

`services/news/factchecks/tests/test_runner.py`：

```python
"""查核流程：用假的 GPU 客戶端，不碰網路。"""
from __future__ import annotations

from datetime import date

from django.test import TestCase

from articles.gpu_client import GpuApiError, JobFailed
from articles.models import Article, ArticleStatus
from factchecks.models import Claim, FactCheckRun, ReviewStatus, RunStatus
from factchecks.runner import MAX_ATTEMPTS, candidates, check_article, check_articles, save_claims

EVIDENCE = {"source": "law", "title": "醫療法 第一百零六條（2026-05-08 修正版）",
            "official_url": "https://law.moj.gov.tw/Law/LawSearchResult.aspx?ty=ONEBAR&kw=x",
            "api_url": "https://ly.govapi.tw/v2/law_contents?x=1",
            "excerpt": "違反第二十四條第二項規定者，處新臺幣三萬元以上五萬元以下罰鍰。"}


def _claim(verdict="supported", **overrides):
    item = {"quote": "他的行政罰鍰從現行的3萬到5萬", "timestamp": 32.0, "kind": "law_article",
            "statement": "醫療法現行罰鍰為 3 萬到 5 萬元", "subject": {"law": "醫療法"},
            "verdict": verdict, "method": "numeric", "rationale": "相同",
            "evidence": [EVIDENCE]}
    item.update(overrides)
    return item


def _payload(*claims):
    return {"model": "qwen3.5:9b", "claims": list(claims) or [_claim()]}


class FakeClient:
    def __init__(self, result=None, error=None, known_jobs=None):
        self.result = result if result is not None else _payload()
        self.error = error
        self.known_jobs = known_jobs or {}
        self.submitted = []

    def job(self, job_id):
        return self.known_jobs.get(job_id)

    def submit_factcheck(self, source_url, speaker, day, meeting, transcript_text):
        self.submitted.append((source_url, speaker, day, transcript_text))
        if isinstance(self.error, GpuApiError):
            raise self.error
        return "fc-1"

    def wait(self, job_id, timeout, poll_seconds=3.0, on_progress=None):
        if self.error is not None:
            raise self.error
        return self.result


def _article(ivod_id="171140", transcript="00:32 他的行政罰鍰從現行的3萬到5萬",
             status=ArticleStatus.READY):
    return Article.objects.create(
        ivod_id=ivod_id, slug=f"2026-08-25-{ivod_id}", title="t", speaker="邱慧洳",
        meeting="院會", date=date(2026, 8, 25),
        ivod_url=f"https://ivod.ly.gov.tw/Play/Clip/1M/{ivod_id}",
        transcript_text=transcript, status=status)


class SaveTests(TestCase):
    def test_claims_and_evidence_are_saved(self):
        article = _article()
        FactCheckRun.objects.create(article=article)
        saved = save_claims(article, _payload())
        self.assertEqual((saved.total, saved.pending), (1, 0))
        claim = article.claims.get()
        self.assertEqual(claim.review_status, ReviewStatus.AUTO)
        self.assertEqual(claim.evidence.get().excerpt, EVIDENCE["excerpt"])
        run = FactCheckRun.objects.get(article=article)
        self.assertEqual(run.status, RunStatus.DONE)
        self.assertEqual(run.model, "qwen3.5:9b")
        self.assertIsNotNone(run.checked_at)

    def test_contradictions_wait_for_a_human(self):
        article = _article()
        saved = save_claims(article, _payload(_claim("contradicted")))
        self.assertEqual(saved.pending, 1)
        self.assertEqual(article.claims.get().review_status, ReviewStatus.PENDING)

    def test_unknown_verdicts_and_kinds_are_dropped(self):
        article = _article()
        saved = save_claims(article, _payload(_claim("lying"), _claim(kind="opinion"), _claim()))
        self.assertEqual(saved.total, 1)
        self.assertEqual(article.claims.get().index, 1)

    def test_unsafe_urls_are_blanked(self):
        """證據網址會變成網站上可點的連結；javascript: 就是一個點擊型 XSS。"""
        article = _article()
        evidence = {**EVIDENCE, "official_url": "javascript:alert(1)"}
        save_claims(article, _payload(_claim(evidence=[evidence])))
        self.assertEqual(article.claims.get().evidence.get().official_url, "")

    def test_saving_again_replaces_the_previous_claims(self):
        article = _article()
        save_claims(article, _payload(_claim(), _claim()))
        save_claims(article, _payload(_claim()))
        self.assertEqual(article.claims.count(), 1)


class CheckTests(TestCase):
    def test_a_ready_article_is_checked(self):
        article = _article()
        client = FakeClient()
        report = check_articles(client, limit=10, timeout=60)
        self.assertEqual((report.checked, report.claims), (1, 1))
        self.assertEqual(client.submitted[0][1], "邱慧洳")
        self.assertEqual(article.claims.count(), 1)

    def test_articles_that_are_not_ready_or_have_no_transcript_are_skipped(self):
        _article("1", status=ArticleStatus.PENDING)
        _article("2", transcript="")
        self.assertEqual(candidates(limit=10, timeout=60), [])

    def test_a_done_article_is_not_checked_again(self):
        article = _article()
        check_articles(FakeClient(), limit=10, timeout=60)
        self.assertNotIn(article, candidates(limit=10, timeout=60))

    def test_a_service_outage_stops_the_round(self):
        _article("1")
        _article("2")
        report = check_articles(FakeClient(error=GpuApiError("down")), limit=10, timeout=60)
        self.assertEqual(report.failed, 1)
        self.assertEqual(FactCheckRun.objects.filter(status=RunStatus.FAILED).count(), 1)

    def test_a_single_failure_does_not_stop_the_round(self):
        _article("1")
        _article("2")
        report = check_articles(FakeClient(error=JobFailed("bad")), limit=10, timeout=60)
        self.assertEqual(report.failed, 2)

    def test_attempts_are_capped(self):
        article = _article()
        FactCheckRun.objects.create(article=article, status=RunStatus.FAILED,
                                    attempts=MAX_ATTEMPTS)
        self.assertEqual(candidates(limit=10, timeout=60), [])

    def test_an_existing_job_is_resumed_instead_of_resubmitted(self):
        """等待途中斷線時，GPU 那邊的工作可能已經跑完；重送等於把成果丟掉。"""
        article = _article()
        FactCheckRun.objects.create(article=article, status=RunStatus.FAILED,
                                    gpu_job_id="fc-old")
        client = FakeClient(known_jobs={"fc-old": {"id": "fc-old", "status": "done"}})
        check_articles(client, limit=10, timeout=60)
        self.assertEqual(client.submitted, [])
        self.assertEqual(article.claims.count(), 1)

    def test_human_reviews_are_never_washed_away(self):
        """攔的 bug：重跑整批換掉主張，審核過的「不符」與駁回紀錄會一起消失。"""
        article = _article()
        save_claims(article, _payload(_claim("contradicted")))
        Claim.objects.update(review_status=ReviewStatus.APPROVED)
        report = check_article(article, FakeClient(), timeout=60)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(article.claims.get().review_status, ReviewStatus.APPROVED)

    def test_force_rechecks_even_after_review(self):
        article = _article()
        save_claims(article, _payload(_claim("contradicted")))
        Claim.objects.update(review_status=ReviewStatus.APPROVED)
        report = check_article(article, FakeClient(), timeout=60, force=True)
        self.assertEqual(report.checked, 1)
        self.assertEqual(article.claims.get().review_status, ReviewStatus.AUTO)
```

- [ ] **Step 3: 確認測試失敗**

Run: `cd services/news && uv run python manage.py test factchecks articles.tests.test_gpu_client`
Expected: FAIL（`submit_factcheck` 與 `factchecks.runner` 不存在）

- [ ] **Step 4: 在 `services/news/articles/gpu_client.py` 加上 `submit_factcheck`**

檔頭加 `from datetime import date`。把 `submit` 結尾取 id 的三行抽成方法，並新增 `submit_factcheck`：

```python
    def submit(self, url: str, detailed: bool = True, min_slides: int | None = None,
               max_slides: int | None = None) -> str:
        body: dict = {"url": url, "detailed": detailed}
        if min_slides:
            body["min_slides"] = min_slides
        if max_slides:
            body["max_slides"] = max_slides
        return self._job_id(self._call("/jobs", "POST", body=body, timeout=_SUBMIT_TIMEOUT))

    def submit_factcheck(self, source_url: str, speaker: str, day: date, meeting: str,
                         transcript_text: str) -> str:
        """送一段發言去查核。結果一樣用 wait() 取。"""
        body = {"source_url": source_url, "speaker": speaker, "date": day.isoformat(),
                "meeting": meeting, "transcript_text": transcript_text}
        return self._job_id(self._call("/factchecks", "POST", body=body,
                                       timeout=_SUBMIT_TIMEOUT))

    @staticmethod
    def _job_id(job: dict) -> str:
        job_id = job.get(JobField.ID)
        if not job_id:
            raise GpuApiError("摘要 API 沒有回傳工作 id")
        return str(job_id)
```

- [ ] **Step 5: 實作 `services/news/factchecks/runner.py`**

```python
"""查核的編排：挑文章 → 送 GPU → 落地 → 決定哪些要人工審核。

沿用匯入流程的規則：以文章為單位、可重跑、嘗試次數有上限、斷線後先接回
既有的工作。多一條規則：重跑不能洗掉人工審核。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from articles.gpu_client import GpuApiClient, GpuApiError, JobField, JobFailed
from articles.ingest import RESUMABLE_STATUSES
from articles.models import Article, ArticleStatus

from .models import (
    HUMAN_REVIEW_STATUSES,
    Claim,
    ClaimKind,
    Evidence,
    FactCheckRun,
    Method,
    ReviewStatus,
    RunStatus,
    Verdict,
)

logger = logging.getLogger(__name__)

# 比匯入少：查核失敗通常是模型或 LYAPI 的問題，多試幾天也不會變好
MAX_ATTEMPTS = 3
STALE_FACTOR = 2


class ClaimField(StrEnum):
    """GPU 回傳的欄位名。協定的一部分，Pi 上沒有 factcheck，各留一份。"""
    QUOTE = "quote"
    TIMESTAMP = "timestamp"
    KIND = "kind"
    STATEMENT = "statement"
    SUBJECT = "subject"
    VERDICT = "verdict"
    METHOD = "method"
    RATIONALE = "rationale"
    EVIDENCE = "evidence"


class EvidenceField(StrEnum):
    SOURCE = "source"
    TITLE = "title"
    OFFICIAL_URL = "official_url"
    API_URL = "api_url"
    EXCERPT = "excerpt"


@dataclass
class FactCheckReport:
    checked: int = 0
    claims: int = 0
    pending_review: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (f"查核 {self.checked} 篇、主張 {self.claims} 則（待審核 {self.pending_review}）、"
                f"失敗 {self.failed}、略過 {self.skipped}")


@dataclass(frozen=True)
class SavedClaims:
    total: int
    pending: int


def _claimable(timeout: float) -> Q:
    """可以被接手的查核：還沒做、做壞了、或卡在查核中太久。"""
    stale_before = timezone.now() - timedelta(seconds=STALE_FACTOR * timeout)
    return (Q(status__in=[RunStatus.PENDING, RunStatus.FAILED])
            | Q(status=RunStatus.PROCESSING, updated_at__lt=stale_before))


def candidates(limit: int, timeout: float) -> list[Article]:
    runs = FactCheckRun.objects.filter(_claimable(timeout), attempts__lt=MAX_ATTEMPTS)
    return list(Article.objects
                .filter(status=ArticleStatus.READY)
                .exclude(transcript_text="")
                .filter(Q(factcheck_run__isnull=True)
                        | Q(factcheck_run__in=runs))
                .order_by("-date", "ivod_id")[:limit])


def has_human_review(article: Article) -> bool:
    return article.claims.filter(review_status__in=HUMAN_REVIEW_STATUSES).exists()


def check_articles(client: GpuApiClient, limit: int, timeout: float) -> FactCheckReport:
    report = FactCheckReport()
    for article in candidates(limit, timeout):
        if has_human_review(article):
            report.skipped += 1
            continue
        if not _check_one(article, client, timeout, report):
            break
    return report


def check_article(article: Article, client: GpuApiClient, timeout: float,
                  force: bool = False) -> FactCheckReport:
    """手動查核一篇。已經有人工審核的，除非 force 否則不動。"""
    report = FactCheckReport()
    if has_human_review(article) and not force:
        report.skipped += 1
        return report
    run, _ = FactCheckRun.objects.get_or_create(article=article)
    run.status, run.attempts = RunStatus.PENDING, 0
    run.save(update_fields=["status", "attempts", "updated_at"])
    _check_one(article, client, timeout, report)
    return report


def _check_one(article: Article, client: GpuApiClient, timeout: float,
               report: FactCheckReport) -> bool:
    """查核一篇；回傳這一輪還要不要繼續（服務掛了就不要）。"""
    run = _claim(article, timeout)
    if run is None:
        report.skipped += 1
        return True
    try:
        payload = client.wait(_resume_or_submit(article, run, client), timeout=timeout,
                              on_progress=lambda job: logger.info(
                                  "查核 %s：%s", article.ivod_id,
                                  job.get(JobField.PROGRESS) or job.get(JobField.STATUS)))
        saved = save_claims(article, payload)
    except (GpuApiError, JobFailed) as e:
        _fail(run, e, report)
        # 服務層級的問題對後面每一篇都一樣，繼續送只是把整批燒成失敗
        return not isinstance(e, GpuApiError)
    except Exception as e:  # noqa: BLE001 一篇的怪資料不該讓整批停擺
        logger.exception("查核 %s 時發生預期外的錯誤", article.ivod_id)
        _fail(run, e, report)
        return True
    report.checked += 1
    report.claims += saved.total
    report.pending_review += saved.pending
    return True


def _claim(article: Article, timeout: float) -> FactCheckRun | None:
    """用一次條件式 UPDATE 宣告所有權：排程與手動指令同時跑時，才不會把同一
    篇送去 GPU 兩次。"""
    run, _ = FactCheckRun.objects.get_or_create(article=article)
    claimed = (FactCheckRun.objects
               .filter(pk=run.pk)
               .filter(_claimable(timeout))
               .filter(attempts__lt=MAX_ATTEMPTS)
               .update(status=RunStatus.PROCESSING, error="",
                       attempts=F("attempts") + 1, updated_at=timezone.now()))
    if not claimed:
        return None
    run.refresh_from_db()
    return run


def _resume_or_submit(article: Article, run: FactCheckRun, client: GpuApiClient) -> str:
    if run.gpu_job_id:
        job = client.job(run.gpu_job_id)
        if job is not None and job.get(JobField.STATUS) in RESUMABLE_STATUSES:
            logger.info("查核 %s 接回既有工作 %s", article.ivod_id, run.gpu_job_id)
            return run.gpu_job_id
    job_id = client.submit_factcheck(article.ivod_url, article.speaker, article.date,
                                     article.meeting, article.transcript_text)
    run.gpu_job_id = job_id
    run.save(update_fields=["gpu_job_id", "updated_at"])
    logger.info("查核 %s 已送出，工作 %s", article.ivod_id, job_id)
    return job_id


def _fail(run: FactCheckRun, error: Exception, report: FactCheckReport) -> None:
    run.status = RunStatus.FAILED
    run.error = f"{type(error).__name__}: {error}"[:2000]
    if isinstance(error, JobFailed):
        # 工作本身跑完了、結論是失敗；留著 id 只會讓下一輪重讀同一個失敗
        run.gpu_job_id = ""
    run.save(update_fields=["status", "error", "gpu_job_id", "updated_at"])
    report.failed += 1
    report.errors.append(f"{run.article.ivod_id}: {error}")
    logger.warning("查核 %s 失敗：%s", run.article.ivod_id, error)


def _safe_url(value: object) -> str:
    """只收 http(s)：這個網址會變成網站上可點的連結。"""
    url = str(value or "").strip()
    return url[:1000] if url.startswith(("http://", "https://")) else ""


def _float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@transaction.atomic
def save_claims(article: Article, payload: dict) -> SavedClaims:
    """整批換掉，不做增量合併。呼叫端要先確定這篇沒有人工審核。"""
    article.claims.all().delete()
    total = pending = 0
    for raw in payload.get("claims") or []:
        if not isinstance(raw, dict):
            continue
        verdict = str(raw.get(ClaimField.VERDICT) or "")
        kind = str(raw.get(ClaimField.KIND) or "")
        quote = str(raw.get(ClaimField.QUOTE) or "").strip()
        if verdict not in Verdict.values or kind not in ClaimKind.values or not quote:
            continue
        method = str(raw.get(ClaimField.METHOD) or "")
        subject = raw.get(ClaimField.SUBJECT)
        review = (ReviewStatus.PENDING if verdict == Verdict.CONTRADICTED
                  else ReviewStatus.AUTO)
        total += 1
        claim = Claim.objects.create(
            article=article, index=total, quote=quote[:1000],
            timestamp=_float(raw.get(ClaimField.TIMESTAMP)), kind=kind,
            statement=str(raw.get(ClaimField.STATEMENT) or quote)[:1000],
            subject={k: str(v) for k, v in subject.items()} if isinstance(subject, dict) else {},
            verdict=verdict,
            method=method if method in Method.values else Method.NONE,
            rationale=str(raw.get(ClaimField.RATIONALE) or "")[:2000],
            review_status=review,
        )
        pending += review == ReviewStatus.PENDING
        for position, item in enumerate(raw.get(ClaimField.EVIDENCE) or [], start=1):
            if not isinstance(item, dict):
                continue
            Evidence.objects.create(
                claim=claim, position=position,
                source=str(item.get(EvidenceField.SOURCE) or "")[:16],
                title=str(item.get(EvidenceField.TITLE) or "")[:300],
                official_url=_safe_url(item.get(EvidenceField.OFFICIAL_URL)),
                api_url=_safe_url(item.get(EvidenceField.API_URL)),
                excerpt=str(item.get(EvidenceField.EXCERPT) or "")[:4000],
            )

    run, _ = FactCheckRun.objects.get_or_create(article=article)
    run.status = RunStatus.DONE
    run.error = ""
    run.gpu_job_id = ""
    run.model = str(payload.get("model") or "")[:100]
    run.checked_at = timezone.now()
    run.save()
    return SavedClaims(total=total, pending=pending)
```

- [ ] **Step 6: 指令、設定與排程**

`services/news/newsroom/settings.py`，在 `INGEST_HOUR` 那一段之後加：

```python
# 每次執行最多查核幾篇。一篇約 1～3 分鐘（抽取一次、每則主張可能再判讀一次）。
FACTCHECK_DAILY_LIMIT = int(os.environ.get("FACTCHECK_DAILY_LIMIT", "20"))
```

`services/news/factchecks/management/commands/factcheck_articles.py`：

```python
"""事實查核：把已完成摘要的文章送去 GPU 主機查核。

    python manage.py factcheck_articles                 # 查核還沒查過的文章
    python manage.py factcheck_articles --article 171140
    python manage.py factcheck_articles --article 171140 --force   # 連人工審核過的也重跑
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from articles.gpu_client import GpuApiClient
from articles.models import Article
from factchecks.runner import check_article, check_articles


class Command(BaseCommand):
    help = "把已完成摘要的文章送去 GPU 主機做事實查核"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--limit", type=int, default=settings.FACTCHECK_DAILY_LIMIT)
        parser.add_argument("--article", help="只查核這一篇（IVOD id）")
        parser.add_argument("--force", action="store_true",
                            help="連已經有人工審核的也重跑（審核紀錄會被換掉）")

    def handle(self, *args, **options) -> None:
        client = GpuApiClient(settings.GPU_API_BASE, settings.GPU_API_KEY)
        timeout = settings.GPU_JOB_TIMEOUT_SECONDS
        if options["article"]:
            article = Article.objects.filter(ivod_id=options["article"]).first()
            if article is None:
                raise CommandError(f"沒有這篇文章：{options['article']}")
            report = check_article(article, client, timeout, force=options["force"])
        else:
            report = check_articles(client, limit=options["limit"], timeout=timeout)
        self.stdout.write(str(report))
        for error in report.errors[:10]:
            self.stderr.write(f"  ! {error}")
```

`services/news/articles/management/commands/run_scheduler.py`，把 `job()` 改成匯入之後接著查核（各自包 try，匯入失敗不影響查核積壓）：

```python
        def job(days: int = options["backfill_days"] or 1) -> None:
            # 包起來：例外若冒出排程，APScheduler 會把這個工作移除，之後
            # 就再也不會跑——而使用者不會發現，只會覺得「新聞停更了」。
            try:
                call_command("ingest_ivod", days=days)
            except Exception:  # noqa: BLE001
                logger.exception("每日匯入失敗，排程繼續")
            # 查核接在匯入之後：它需要摘要已經完成的文章。分開包，匯入那邊
            # 掛了也還能把先前的積壓查完。
            try:
                call_command("factcheck_articles")
            except Exception:  # noqa: BLE001
                logger.exception("每日查核失敗，排程繼續")
```

- [ ] **Step 7: 更新 `services/news/README.md`**

在「## API」之前加一節：

````markdown
## 事實查核

摘要完成的文章會再送去 GPU 主機做事實查核：從委員的發言挑出可以用法條或議案驗證的陳述，取回官方資料比對。設計見 `docs/superpowers/specs/2026-09-19-fact-check-design.md`。

```bash
uv run python manage.py factcheck_articles                 # 查核還沒查過的文章（排程會在匯入後自動跑）
uv run python manage.py factcheck_articles --article 171140
uv run python manage.py factcheck_articles --article 171140 --force   # 連人工審核過的也重跑
```

**判定為「不符」的主張不會自動公開**，要到 `/admin/factchecks/claim/?review_status__exact=pending_review` 審核：核准後公開並計入查證相符率，駁回則不公開、不計分（例如語音辨識錯字造成的誤判）。重跑不會洗掉審核紀錄，除非加 `--force`。

| 變數 | 預設 | 說明 |
|---|---|---|
| `FACTCHECK_DAILY_LIMIT` | `20` | 每次執行最多查核幾篇 |
````

並把「## 架構」的樹狀圖加上：

```
factchecks/
├─ models.py        FactCheckRun / Claim / Evidence
├─ scoring.py       查證相符率（公開公式）
├─ runner.py        use case：送工作 → 落地 → 不符進審核
├─ review.py        審核動作
├─ admin.py         審核介面
├─ api.py           查證資料的 ninja 端點
└─ management/commands/factcheck_articles.py
```

- [ ] **Step 8: 確認測試通過**

Run: `cd services/news && uv run python manage.py test`
Expected: 全部 PASS

- [ ] **Step 9: Commit**

```bash
git add services/news
git commit -m "feat(news): 查核流程、指令與排程"
```

---

### Task 10: 審核介面與查證 API

**Files:**
- Create: `services/news/factchecks/review.py`、`services/news/factchecks/admin.py`、`services/news/factchecks/api.py`
- Modify: `services/news/articles/api.py`（文章詳情帶查證、委員清單帶相符率、掛上 router）
- Test: `services/news/factchecks/tests/test_review.py`、`services/news/factchecks/tests/test_api.py`

**Interfaces:**
- Consumes: Task 8 的模型與 `scoring`；Task 9 的 `save_claims`
- Produces:
  - `factchecks.review.review(claims: QuerySet[Claim], decision: ReviewStatus) -> int`
  - HTTP（前端 Task 11 依此實作）：
    - `GET /api/articles/{slug}` 多兩個欄位：`factcheck_checked: bool`、`claims: ClaimOut[]`（只含公開的）
    - `GET /api/speakers` 的每一項多 `factcheck: ScoreOut`
    - `GET /api/speakers/{name}/claims` → `{speaker, score: ScoreOut, items: (ClaimOut & {article_slug, article_title, date})[]}`
    - `ClaimOut = {index, quote, timestamp, kind, statement, verdict, method, rationale, reviewed: bool, evidence: EvidenceOut[]}`
    - `EvidenceOut = {source, title, official_url, api_url, excerpt}`
    - `ScoreOut = {rate: float | null, checked, supported, partial, contradicted, unverifiable, min_sample}`

- [ ] **Step 1: 寫審核的失敗測試**

`services/news/factchecks/tests/test_review.py`：

```python
"""審核：核准才公開，駁回就不公開、不計分。"""
from __future__ import annotations

from datetime import date

from django.test import TestCase

from articles.models import Article, ArticleStatus
from factchecks.models import Claim, ReviewStatus
from factchecks.review import review
from factchecks.scoring import public_claims


class ReviewTests(TestCase):
    def setUp(self):
        article = Article.objects.create(
            ivod_id="1", slug="s-1", title="t", speaker="甲", date=date(2026, 8, 25),
            ivod_url="https://ivod.ly.gov.tw/Play/Clip/1M/1", status=ArticleStatus.READY)
        self.claim = Claim.objects.create(
            article=article, index=1, quote="q", kind="law_article", statement="s",
            verdict="contradicted", method="numeric", review_status=ReviewStatus.PENDING)

    def test_approving_publishes(self):
        self.assertEqual(review(Claim.objects.all(), ReviewStatus.APPROVED), 1)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.review_status, ReviewStatus.APPROVED)
        self.assertIsNotNone(self.claim.reviewed_at)
        self.assertIn(self.claim, public_claims())

    def test_rejecting_hides(self):
        review(Claim.objects.all(), ReviewStatus.REJECTED)
        self.assertNotIn(self.claim, public_claims())

    def test_only_human_decisions_are_allowed(self):
        with self.assertRaises(ValueError):
            review(Claim.objects.all(), ReviewStatus.AUTO)
```

- [ ] **Step 2: 寫 API 的失敗測試**

`services/news/factchecks/tests/test_api.py`：

```python
"""查證 API：只給公開的判定，分數跟著公式走。"""
from __future__ import annotations

import urllib.parse
from datetime import date

from django.test import TestCase

from articles.models import Article, ArticleStatus
from factchecks.models import Claim, Evidence, FactCheckRun, ReviewStatus, RunStatus


def _article(ivod_id="171140", speaker="邱慧洳"):
    return Article.objects.create(
        ivod_id=ivod_id, slug=f"2026-08-25-{ivod_id}", title=f"2026-08-25 {speaker}－院會",
        speaker=speaker, date=date(2026, 8, 25),
        ivod_url=f"https://ivod.ly.gov.tw/Play/Clip/1M/{ivod_id}", status=ArticleStatus.READY)


def _claim(article, index, verdict="supported", review=ReviewStatus.AUTO):
    claim = Claim.objects.create(
        article=article, index=index, quote=f"原話 {index}", timestamp=32.0,
        kind="law_article", statement=f"主張 {index}", verdict=verdict, method="numeric",
        rationale="相同", review_status=review)
    Evidence.objects.create(claim=claim, position=1, source="law", title="醫療法 第一百零六條",
                            official_url="https://law.moj.gov.tw/x",
                            api_url="https://ly.govapi.tw/v2/x", excerpt="三萬元以上")
    return claim


class ArticleClaimsTests(TestCase):
    def test_the_detail_page_carries_public_claims_with_evidence(self):
        article = _article()
        FactCheckRun.objects.create(article=article, status=RunStatus.DONE)
        _claim(article, 1)
        _claim(article, 2, "contradicted", ReviewStatus.PENDING)
        _claim(article, 3, "contradicted", ReviewStatus.APPROVED)
        _claim(article, 4, "supported", ReviewStatus.REJECTED)
        body = self.client.get(f"/api/articles/{article.slug}").json()
        self.assertTrue(body["factcheck_checked"])
        self.assertEqual([c["index"] for c in body["claims"]], [1, 3])
        self.assertTrue(body["claims"][1]["reviewed"])
        self.assertEqual(body["claims"][0]["evidence"][0]["excerpt"], "三萬元以上")

    def test_an_unchecked_article_says_so(self):
        body = self.client.get(f"/api/articles/{_article().slug}").json()
        self.assertFalse(body["factcheck_checked"])
        self.assertEqual(body["claims"], [])


class SpeakerScoreTests(TestCase):
    def test_speakers_carry_their_score(self):
        article = _article()
        for i in range(1, 6):
            _claim(article, i)
        items = self.client.get("/api/speakers").json()["items"]
        score = items[0]["factcheck"]
        self.assertEqual(score["checked"], 5)
        self.assertAlmostEqual(score["rate"], 6 / 7)
        self.assertEqual(score["min_sample"], 5)

    def test_a_small_sample_has_no_rate(self):
        _claim(_article(), 1)
        score = self.client.get("/api/speakers").json()["items"][0]["factcheck"]
        self.assertIsNone(score["rate"])
        self.assertEqual(score["checked"], 1)

    def test_a_speakers_claims_list_links_back_to_articles(self):
        article = _article()
        _claim(article, 1)
        _claim(article, 2, "unverifiable")
        _claim(_article("2", speaker="別人"), 1)
        url = f"/api/speakers/{urllib.parse.quote('邱慧洳')}/claims"
        body = self.client.get(url).json()
        self.assertEqual(body["speaker"], "邱慧洳")
        self.assertEqual(len(body["items"]), 2)
        self.assertEqual(body["items"][0]["article_slug"], article.slug)
        self.assertEqual(body["score"]["unverifiable"], 1)

    def test_an_unknown_speaker_is_an_empty_record(self):
        body = self.client.get(f"/api/speakers/{urllib.parse.quote('沒有人')}/claims").json()
        self.assertEqual(body["items"], [])
        self.assertEqual(body["score"]["checked"], 0)
```

- [ ] **Step 3: 確認測試失敗**

Run: `cd services/news && uv run python manage.py test factchecks`
Expected: FAIL（`factchecks.review` 不存在、API 沒有新欄位）

- [ ] **Step 4: 實作 `services/news/factchecks/review.py`**

```python
"""人工審核。只有這裡能把主張改成已核准或已駁回。"""
from __future__ import annotations

from django.db.models import QuerySet
from django.utils import timezone

from .models import HUMAN_REVIEW_STATUSES, Claim, ReviewStatus


def review(claims: QuerySet[Claim], decision: ReviewStatus) -> int:
    """核准：公開並計入查證相符率。駁回：不公開、不計分（例如語音辨識錯字造成的誤判）。

    也可以駁回自動發佈的「相符」——審核者發現它其實判錯了的時候。
    """
    if decision not in HUMAN_REVIEW_STATUSES:
        raise ValueError(f"審核結果只能是核准或駁回：{decision}")
    return claims.update(review_status=decision, reviewed_at=timezone.now())
```

- [ ] **Step 5: 實作 `services/news/factchecks/admin.py`**

```python
from __future__ import annotations

from django.contrib import admin

from .models import Claim, Evidence, FactCheckRun, ReviewStatus
from .review import review


class EvidenceInline(admin.TabularInline):
    model = Evidence
    extra = 0


@admin.register(Claim)
class ClaimAdmin(admin.ModelAdmin):
    list_display = ("article_date", "speaker", "verdict", "review_status", "statement")
    list_filter = ("review_status", "verdict", "kind")
    search_fields = ("statement", "quote", "article__speaker")
    inlines = [EvidenceInline]
    actions = ["approve", "reject"]

    @admin.display(description="日期", ordering="article__date")
    def article_date(self, obj: Claim):
        return obj.article.date

    @admin.display(description="委員", ordering="article__speaker")
    def speaker(self, obj: Claim) -> str:
        return obj.article.speaker

    @admin.action(description="核准（公開並計入查證相符率）")
    def approve(self, request, queryset) -> None:
        self.message_user(request, f"已核准 {review(queryset, ReviewStatus.APPROVED)} 則")

    @admin.action(description="駁回（不公開、不計分）")
    def reject(self, request, queryset) -> None:
        self.message_user(request, f"已駁回 {review(queryset, ReviewStatus.REJECTED)} 則")


@admin.register(FactCheckRun)
class FactCheckRunAdmin(admin.ModelAdmin):
    list_display = ("article", "status", "attempts", "model", "checked_at")
    list_filter = ("status",)
    search_fields = ("article__ivod_id", "article__speaker")
```

- [ ] **Step 6: 實作 `services/news/factchecks/api.py`**

```python
"""查證資料的 ninja 端點與 schema。文章詳情與委員清單也用這裡的轉換函式。"""
from __future__ import annotations

from datetime import date as date_type

from ninja import Router, Schema

from articles.models import Article

from .models import Claim, FactCheckRun, PUBLIC_REVIEW_STATUSES, ReviewStatus, RunStatus
from .scoring import MIN_SAMPLE, Score, public_claims, score_of

router = Router(tags=["factcheck"])


class EvidenceOut(Schema):
    source: str
    title: str
    official_url: str
    api_url: str
    excerpt: str


class ClaimOut(Schema):
    index: int
    quote: str
    timestamp: float
    kind: str
    statement: str
    verdict: str
    method: str
    rationale: str
    reviewed: bool
    evidence: list[EvidenceOut]


class ScoreOut(Schema):
    rate: float | None
    checked: int
    supported: int
    partial: int
    contradicted: int
    unverifiable: int
    min_sample: int


class SpeakerClaimOut(ClaimOut):
    article_slug: str
    article_title: str
    date: date_type


class SpeakerClaimsOut(Schema):
    speaker: str
    score: ScoreOut
    items: list[SpeakerClaimOut]


def claim_out(claim: Claim) -> dict:
    return {
        "index": claim.index,
        "quote": claim.quote,
        "timestamp": claim.timestamp,
        "kind": claim.kind,
        "statement": claim.statement,
        "verdict": claim.verdict,
        "method": claim.method,
        "rationale": claim.rationale,
        # 前端用它標示「不符・已人工確認」
        "reviewed": claim.review_status == ReviewStatus.APPROVED,
        "evidence": [{
            "source": e.source, "title": e.title, "official_url": e.official_url,
            "api_url": e.api_url, "excerpt": e.excerpt,
        } for e in claim.evidence.all()],
    }


def score_out(score: Score) -> dict:
    return {
        "rate": score.rate,
        "checked": score.checked,
        "supported": score.supported,
        "partial": score.partial,
        "contradicted": score.contradicted,
        "unverifiable": score.unverifiable,
        "min_sample": MIN_SAMPLE,
    }


def article_claims(article: Article) -> list[dict]:
    claims = (article.claims.filter(review_status__in=PUBLIC_REVIEW_STATUSES)
              .prefetch_related("evidence"))
    return [claim_out(c) for c in claims]


def is_checked(article: Article) -> bool:
    return FactCheckRun.objects.filter(article=article, status=RunStatus.DONE).exists()


@router.get("/speakers/{name}/claims", response=SpeakerClaimsOut)
def speaker_claims(request, name: str) -> dict:
    claims = list(public_claims()
                  .filter(article__speaker=name)
                  .select_related("article")
                  .prefetch_related("evidence")
                  .order_by("-article__date", "article__ivod_id", "index"))
    items = [{**claim_out(c), "article_slug": c.article.slug,
              "article_title": c.article.title, "date": c.article.date} for c in claims]
    return {"speaker": name, "score": score_out(score_of(c.verdict for c in claims)),
            "items": items}
```

- [ ] **Step 7: 修改 `services/news/articles/api.py`**

import 區加：

```python
from factchecks.api import ClaimOut, ScoreOut, article_claims, is_checked, score_out
from factchecks.api import router as factcheck_router
from factchecks.scoring import Score, scores_by_speaker
```

`ArticleDetailOut` 加兩個欄位：

```python
class ArticleDetailOut(ArticleCardOut):
    source_note: str
    transcript_text: str
    slides: list[SlideOut]
    factcheck_checked: bool
    claims: list[ClaimOut]
```

`SpeakerOut` 加 `factcheck: ScoreOut`。

緊接在 `api = NinjaAPI(...)` 那一行之後加：

```python
# 查證的端點（/speakers/{name}/claims）在 factchecks app 裡
api.add_router("", factcheck_router)
```

`article_detail` 在 `data.update({...})` 的字典裡加：

```python
        "factcheck_checked": is_checked(article),
        "claims": article_claims(article),
```

`speakers` 改成：

```python
@api.get("/speakers", response=SpeakerListOut)
def speakers(request) -> dict:
    rows = (Article.objects.filter(status=ArticleStatus.READY)
            .values("speaker")
            .annotate(count=Count("id"), latest_date=Max("date"))
            .order_by("-latest_date", "-count"))
    scores = scores_by_speaker()
    return {"items": [
        {"name": r["speaker"], "count": r["count"], "latest_date": r["latest_date"],
         "factcheck": score_out(scores.get(r["speaker"], Score()))}
        for r in rows if r["speaker"]
    ]}
```

- [ ] **Step 8: 確認測試通過**

Run: `cd services/news && uv run python manage.py test`
Expected: 全部 PASS。若 `/api/speakers/{name}/claims` 回 404，是 router 前綴的組法不同——改成 `api.add_router("/", factcheck_router)` 並把 route 寫成 `"speakers/{name}/claims"`，再跑一次。

- [ ] **Step 9: Commit**

```bash
git add services/news/factchecks services/news/articles/api.py
git commit -m "feat(news): 查證審核介面與 API"
```

---

### Task 11: 前端資料層與示範資料

**Files:**
- Modify: `web/news/src/lib/types.ts`、`web/news/src/lib/api.ts`、`web/news/src/fixtures/sample.json`
- Create: `web/news/src/lib/factcheck.ts`

**Interfaces:**
- Consumes: Task 10 的 HTTP 形狀
- Produces:
  - `types.ts`：`Verdict`、`ClaimMethod`、`Evidence`、`Claim`、`FactCheckScore`、`SpeakerClaim`、`SpeakerClaims`；`ArticleDetail` 多 `factcheck_checked: boolean` 與 `claims: Claim[]`；`Speaker` 多 `factcheck?: FactCheckScore`
  - `lib/factcheck.ts`：`MIN_SAMPLE`、`VERDICTS`、`VERDICT_LABEL`、`METHOD_LABEL`、`KIND_LABEL`、`isVerdict`、`emptyScore`、`scoreOf`、`formatRate`、`normalizeClaims`、`normalizeScore`
  - `api.ts`：`getSpeakerClaims(name): Promise<Result<SpeakerClaims>>`；`getArticle` 回傳的資料一定有 `claims` 陣列與 `factcheck_checked`

前端沒有單元測試框架，驗證靠 `npx astro check`（型別）與 Task 12 最後的實際瀏覽。

- [ ] **Step 1: 型別**

`web/news/src/lib/types.ts`：在 `ArticleDetail` 之前加：

```ts
export type Verdict = 'supported' | 'partial' | 'contradicted' | 'unverifiable';

export type ClaimMethod = 'numeric' | 'model' | 'none';

export type Evidence = {
  source: string;
  title: string;
  official_url: string;
  api_url: string;
  excerpt: string;
};

export type Claim = {
  index: number;
  quote: string;
  timestamp: number;
  kind: string;
  statement: string;
  verdict: Verdict;
  method: ClaimMethod;
  rationale: string;
  /** 「不符」經人工核准後才會出現；true 代表有人看過 */
  reviewed: boolean;
  evidence: Evidence[];
};

export type FactCheckScore = {
  /** 可查證陳述不足 min_sample 則時為 null */
  rate: number | null;
  checked: number;
  supported: number;
  partial: number;
  contradicted: number;
  unverifiable: number;
  min_sample: number;
};

export type SpeakerClaim = Claim & {
  article_slug: string;
  article_title: string;
  date: string;
};

export type SpeakerClaims = {
  speaker: string;
  score: FactCheckScore;
  items: SpeakerClaim[];
};
```

`ArticleDetail` 改成：

```ts
export type ArticleDetail = ArticleCard & {
  source_note: string;
  transcript_text: string;
  slides: Slide[];
  factcheck_checked: boolean;
  claims: Claim[];
};
```

`Speaker` 加一個欄位 `factcheck?: FactCheckScore;`。

- [ ] **Step 2: `web/news/src/lib/factcheck.ts`**

```ts
import type { Claim, ClaimMethod, Evidence, FactCheckScore, Verdict } from './types';

/** 與後端 factchecks/scoring.py 的 MIN_SAMPLE 相同 */
export const MIN_SAMPLE = 5;

export const VERDICTS: Verdict[] = ['supported', 'partial', 'contradicted', 'unverifiable'];

export const VERDICT_LABEL: Record<Verdict, string> = {
  supported: '相符',
  partial: '部分相符',
  contradicted: '不符',
  unverifiable: '無法查證',
};

export const METHOD_LABEL: Record<ClaimMethod, string> = {
  numeric: '數字比對',
  model: '模型判讀',
  none: '未比對',
};

export const KIND_LABEL: Record<string, string> = {
  law_article: '法條內容',
  bill_content: '議案內容',
  bill_status: '議案進度',
};

const METHODS: ClaimMethod[] = ['numeric', 'model', 'none'];

export function isVerdict(v: unknown): v is Verdict {
  return typeof v === 'string' && (VERDICTS as string[]).includes(v);
}

export function emptyScore(): FactCheckScore {
  return {
    rate: null, checked: 0, supported: 0, partial: 0, contradicted: 0, unverifiable: 0,
    min_sample: MIN_SAMPLE,
  };
}

/**
 * 與後端 factchecks/scoring.py 同一條公式。只給示範資料模式用——
 * 正式站一律顯示後端算好的值，不在前端重算。
 */
export function scoreOf(verdicts: readonly string[]): FactCheckScore {
  const s = emptyScore();
  for (const v of verdicts) if (isVerdict(v)) s[v] += 1;
  s.checked = s.supported + s.partial + s.contradicted;
  s.rate = s.checked < MIN_SAMPLE ? null : (s.supported + 0.5 * s.partial + 1) / (s.checked + 2);
  return s;
}

export function formatRate(rate: number | null): string {
  return rate === null ? '—' : `${Math.round(rate * 100)}%`;
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

function num(v: unknown): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

function normalizeEvidence(raw: unknown): Evidence[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((e): e is Record<string, unknown> => !!e && typeof e === 'object')
    .map((e) => ({
      source: str(e.source),
      title: str(e.title),
      official_url: str(e.official_url),
      api_url: str(e.api_url),
      excerpt: str(e.excerpt),
    }));
}

/**
 * 後端少給或給錯的欄位不讓整頁爆掉。判定不認得的整則丟掉——
 * 顯示一個不認得的判定比不顯示更糟。
 */
export function normalizeClaims<T extends Claim = Claim>(raw: unknown): T[] {
  if (!Array.isArray(raw)) return [];
  const out: T[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const o = item as Record<string, unknown>;
    if (!isVerdict(o.verdict)) continue;
    const method = METHODS.includes(o.method as ClaimMethod) ? (o.method as ClaimMethod) : 'none';
    out.push({
      ...o,
      index: num(o.index),
      quote: str(o.quote),
      timestamp: num(o.timestamp),
      kind: str(o.kind),
      statement: str(o.statement),
      verdict: o.verdict,
      method,
      rationale: str(o.rationale),
      reviewed: o.reviewed === true,
      evidence: normalizeEvidence(o.evidence),
    } as unknown as T);
  }
  return out;
}

export function normalizeScore(raw: unknown): FactCheckScore {
  if (!raw || typeof raw !== 'object') return emptyScore();
  const o = raw as Record<string, unknown>;
  return {
    rate: typeof o.rate === 'number' && Number.isFinite(o.rate) ? o.rate : null,
    checked: num(o.checked),
    supported: num(o.supported),
    partial: num(o.partial),
    contradicted: num(o.contradicted),
    unverifiable: num(o.unverifiable),
    min_sample: num(o.min_sample) || MIN_SAMPLE,
  };
}
```

- [ ] **Step 3: 修改 `web/news/src/lib/api.ts`**

import 區：型別清單加 `SpeakerClaim, SpeakerClaims`，並加一行：

```ts
import { normalizeClaims, normalizeScore, scoreOf } from './factcheck';
```

`toCard` 也要去掉 `claims` 與 `factcheck_checked`：

```ts
function toCard(a: ArticleDetail) {
  const { source_note, transcript_text, slides, claims, factcheck_checked, ...card } = a;
  void source_note;
  void transcript_text;
  void slides;
  void claims;
  void factcheck_checked;
  return card;
}
```

`getArticle` 的假資料分支改成：

```ts
    return {
      ok: true,
      data: {
        ...found,
        // 示範資料裡有 claims 欄位就視為查核過
        factcheck_checked: Array.isArray(found.claims),
        claims: normalizeClaims(found.claims),
      },
    };
```

正式分支回傳的 `data` 加兩行：

```ts
      factcheck_checked: d.factcheck_checked === true,
      claims: normalizeClaims(d.claims),
```

`getSpeakers` 的假資料分支改成（多算每位委員的相符率）：

```ts
  if (isFixtureMode()) {
    const map = new Map<string, { name: string; count: number; latest_date: string; verdicts: string[] }>();
    for (const a of fixtureArticles()) {
      const verdicts = normalizeClaims(a.claims).map((c) => c.verdict);
      const cur = map.get(a.speaker);
      if (cur) {
        cur.count += 1;
        cur.verdicts.push(...verdicts);
        if (a.date > cur.latest_date) cur.latest_date = a.date;
      } else {
        map.set(a.speaker, { name: a.speaker, count: 1, latest_date: a.date, verdicts });
      }
    }
    const items = [...map.values()]
      .sort((a, b) => b.count - a.count)
      .map(({ verdicts, ...s }) => ({ ...s, factcheck: scoreOf(verdicts) }));
    return { ok: true, data: { items } };
  }
```

正式分支的回傳改成：

```ts
  const items = Array.isArray(res.data?.items) ? res.data.items : [];
  return {
    ok: true,
    data: { items: items.map((s) => ({ ...s, factcheck: normalizeScore(s.factcheck) })) },
  };
```

檔尾（`statusFor` 之前）加：

```ts
export async function getSpeakerClaims(name: string): Promise<Result<SpeakerClaims>> {
  if (isFixtureMode()) {
    const items: SpeakerClaim[] = [];
    for (const a of fixtureArticles().filter((x) => x.speaker === name)) {
      for (const c of normalizeClaims(a.claims)) {
        items.push({ ...c, article_slug: a.slug, article_title: a.title, date: a.date });
      }
    }
    return {
      ok: true,
      data: { speaker: name, score: scoreOf(items.map((c) => c.verdict)), items },
    };
  }

  const res = await getJson<SpeakerClaims>(`/api/speakers/${encodeURIComponent(name)}/claims`);
  if (!res.ok) return res;
  return {
    ok: true,
    data: {
      speaker: name,
      score: normalizeScore(res.data?.score),
      items: normalizeClaims<SpeakerClaim>(res.data?.items),
    },
  };
}
```

- [ ] **Step 4: 示範資料加上查證結果**

示範資料的人名、數字全部虛構，網址一律指向 `example.invalid`（`_note` 已經這樣約定）。每則 `quote` 都是該篇逐字稿裡委員自己說的話、逐字複製。用這段一次性的腳本寫進去（不必提交腳本本身）：

```bash
cd web/news && python - <<'PY'
import json
path = "src/fixtures/sample.json"
data = json.load(open(path, encoding="utf-8"))

def ev(source, title, excerpt, slug):
    return {"source": source, "title": f"（虛構）{title}",
            "official_url": f"https://example.invalid/{slug}",
            "api_url": f"https://example.invalid/api/{slug}", "excerpt": excerpt}

def claim(index, quote, ts, kind, statement, verdict, method, rationale, evidence, reviewed=False):
    return {"index": index, "quote": quote, "timestamp": ts, "kind": kind,
            "statement": statement, "verdict": verdict, "method": method,
            "rationale": rationale, "reviewed": reviewed, "evidence": evidence}

claims = {
    "2026-08-27-900001": [
        claim(1, "一百一十四到一百一十六年度，無人機相關的採購，累計編列八十二點四億", 14,
              "bill_content", "國防部 114 至 116 年度無人機採購累計編列 82.4 億元",
              "supported", "numeric", "主張的 8,240,000,000 元出現在證據中",
              [ev("bill", "行政院｜示範用無人機採購預算案",
                  "案由：一百十四年度至一百十六年度無人機採購計畫。\n第三條　本計畫所需經費共計新臺幣八十二億四千萬元。",
                  "bill-900001-a")]),
        claim(2, "那個測試場域，一年只有兩梯次", 113, "law_article",
              "示範採購作業要點規定抗干擾測試每年辦理兩梯次", "supported", "model",
              "證據明確寫每年辦理兩梯次（依據：「抗干擾測試每年辦理二梯次」）",
              [ev("law", "示範採購作業要點 第十二條（2025-03-01 修正版）",
                  "抗干擾測試每年辦理二梯次，分別於三月及十月受理。", "law-900001-b")]),
        claim(3, "廠商錯過一梯，就要等半年", 113, "law_article",
              "抗干擾測試錯過一梯次須等候六個月", "partial", "numeric",
              "主張的 6 個月 在證據中對不上；兩梯次相隔 7 個月",
              [ev("law", "示範採購作業要點 第十二條（2025-03-01 修正版）",
                  "抗干擾測試每年辦理二梯次，分別於三月及十月受理；未通過者得於七個月後重新申請。",
                  "law-900001-b")]),
        claim(4, "你們的預算執行率報上來是很好看的，因為錢簽出去就算執行", 66, "bill_content",
              "預算執行率以契約簽訂金額計算", "supported", "model",
              "證據寫明執行率以簽約金額計（依據：「執行率以契約簽訂金額計算」）",
              [ev("bill", "國防部｜示範用預算執行報告",
                  "本年度執行率以契約簽訂金額計算，交貨進度另案列管。", "bill-900001-c")]),
        claim(5, "同一個機種可以差二十個百分點以上", 188, "bill_content",
              "同一機種以金額與件數計算的國產化率相差逾 20 個百分點", "supported", "model",
              "證據列出的兩種算法相差 21 個百分點（依據：「以金額計為百分之六十二，以件數計為百分之四十一」）",
              [ev("bill", "國防部｜示範用國產化率說明",
                  "該機種國產化率以金額計為百分之六十二，以件數計為百分之四十一。", "bill-900001-d")]),
        claim(6, "你們在記者會講一個數字，在委員會講另外一個數字", 169, "bill_content",
              "國防部對外公布的國產化率與送委員會的數字不同", "unverifiable", "none",
              "查無對應的法條或議案", []),
    ],
    "2026-08-26-900002": [
        claim(1, "醫院做了價值十塊錢的服務，實際領到八塊七", 32, "bill_content",
              "最近四季健保平均點值約 0.87", "supported", "model",
              "季報的平均點值與主張相同（依據：「最近四季平均點值為零點八七」）",
              [ev("bill", "衛福部｜示範用健保點值季報",
                  "最近四季平均點值為零點八七。", "bill-900002-a")]),
        claim(2, "三成一。次長，這不是因為病人變多，是因為床開不出來", 142, "bill_content",
              "今年上半年急診暫留逾 24 小時人次較去年同期增加 31%", "supported", "model",
              "證據的增幅與主張相同（依據：「較去年同期增加百分之三十一」）",
              [ev("bill", "衛福部｜示範用急診壅塞報告",
                  "上半年急診暫留逾二十四小時人次較去年同期增加百分之三十一。", "bill-900002-b")]),
        claim(3, "你們每年公布招募了多少護理師，可是一年內離職的有多少", 183, "bill_content",
              "衛福部只公布護理師招募人數、未公布一年內離職人數", "unverifiable", "none",
              "查無對應的法條或議案", []),
    ],
    "2026-08-25-900003": [
        claim(1, "農地變更做光電，三年前一年四百一十二件", 18, "bill_content",
              "三年前農地變更做光電一年 412 件", "supported", "model",
              "證據記載的件數相同（依據：「一百一十二年計四百一十二件」）",
              [ev("bill", "經濟部｜示範用農地光電統計",
                  "農地變更作光電設施者，一百一十二年計四百一十二件。", "bill-900003-a")]),
        claim(2, "有七十四件的案場，位置在既有灌溉渠道的上游", 70, "bill_content",
              "有 74 件光電案場位於既有灌溉渠道上游", "contradicted", "model",
              "證據記載為六十一件（依據：「位於灌溉渠道上游之案場計六十一件」）",
              [ev("bill", "農業部｜示範用灌區案場清查",
                  "經套疊圖資，位於灌溉渠道上游之案場計六十一件。", "bill-900003-b")],
              reviewed=True),
    ],
    "2026-08-25-900004": [],
}

for article in data["articles"]:
    article["claims"] = claims[article["slug"]]
    for c in article["claims"]:
        assert c["quote"] in article["transcript_text"], c["quote"]

json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("ok")
PY
```

Expected: 印出 `ok`（`assert` 確認每則 quote 都真的在該篇逐字稿裡）。

- [ ] **Step 5: 型別檢查**

Run: `cd web/news && npx astro check`
Expected: `0 errors`。看完整輸出，不要只看最後幾行。

- [ ] **Step 6: Commit**

```bash
git add web/news/src/lib web/news/src/fixtures/sample.json
git commit -m "feat(web): 查證資料的型別、API 與示範資料"
```

---

### Task 12: 前端畫面——查證區塊、委員相符率、查證方法頁

**Files:**
- Modify: `web/news/src/styles/global.css`（語意色 token）
- Create: `web/news/src/components/VerdictBadge.astro`、`ClaimItem.astro`、`FactCheckSection.astro`、`TrustScore.astro`、`web/news/src/pages/method.astro`
- Modify: `web/news/src/pages/article/[slug].astro`、`web/news/src/pages/speaker/[name].astro`、`web/news/src/components/SiteFooter.astro`、`web/news/README.md`

**Interfaces:**
- Consumes: Task 11 的型別與 `lib/factcheck.ts`、`getSpeakerClaims`；既有的 `safeExternalUrl`、`mmss`、`formatDateWithWeekday`、`displayTitle`
- Produces: 元件 `VerdictBadge {verdict, reviewed?, size?}`、`ClaimItem {claim, showArticle?}`、`FactCheckSection {claims, checked}`、`TrustScore {score}`；頁面 `/method`（`#score` 錨點是公式那一節）

判定顏色是語意色，與強調色（琥珀）分開：相符綠、部分相符橙、不符紅、無法查證灰。深淺兩種主題都要定義。

- [ ] **Step 1: 語意色 token**

`web/news/src/styles/global.css`：`@theme` 區塊在 `--color-danger` 之後加：

```css
  --color-danger-soft: var(--c-danger-soft);
  --color-ok: var(--c-ok);
  --color-ok-soft: var(--c-ok-soft);
  --color-partial: var(--c-partial);
  --color-partial-soft: var(--c-partial-soft);
```

`:root`（深色）在 `--c-danger` 之後加：

```css
  --c-danger-soft: rgba(239, 125, 106, 0.14);
  --c-ok: #6cc38f;
  --c-ok-soft: rgba(108, 195, 143, 0.14);
  --c-partial: #f0a35e;
  --c-partial-soft: rgba(240, 163, 94, 0.14);
```

`@media (prefers-color-scheme: light)` 的 `:root` 在 `--c-danger` 之後加：

```css
    --c-danger-soft: rgba(178, 64, 43, 0.08);
    --c-ok: #2f7d4f;
    --c-ok-soft: rgba(47, 125, 79, 0.1);
    --c-partial: #b45309;
    --c-partial-soft: rgba(180, 83, 9, 0.1);
```

- [ ] **Step 2: `web/news/src/components/VerdictBadge.astro`**

```astro
---
import type { Verdict } from '../lib/types';
import { VERDICT_LABEL } from '../lib/factcheck';

interface Props {
  verdict: Verdict;
  reviewed?: boolean;
  size?: 'sm' | 'md';
}
const { verdict, reviewed = false, size = 'md' } = Astro.props;

// 類別字串要完整寫出來，Tailwind 才掃描得到
const tone: Record<Verdict, string> = {
  supported: 'bg-ok-soft text-ok',
  partial: 'bg-partial-soft text-partial',
  contradicted: 'bg-danger-soft text-danger',
  unverifiable: 'bg-surface-2 text-muted',
};
const pad = size === 'sm' ? 'px-2 py-0.5 text-[0.7rem]' : 'px-2.5 py-1 text-[0.78rem]';
---

<span class={`inline-flex items-center gap-1.5 rounded-full font-medium ${tone[verdict]} ${pad}`}>
  <span class="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true"></span>
  {VERDICT_LABEL[verdict]}
  {reviewed && verdict === 'contradicted' && <span class="font-normal opacity-80">・已人工確認</span>}
</span>
```

- [ ] **Step 3: `web/news/src/components/ClaimItem.astro`**

```astro
---
import type { Claim, SpeakerClaim } from '../lib/types';
import { KIND_LABEL, METHOD_LABEL } from '../lib/factcheck';
import { safeExternalUrl } from '../lib/api';
import { displayTitle, formatDateWithWeekday, mmss } from '../lib/format';
import VerdictBadge from './VerdictBadge.astro';

interface Props {
  claim: Claim | SpeakerClaim;
  /** 委員頁用：顯示這一則出自哪一篇 */
  showArticle?: boolean;
}
const { claim, showArticle = false } = Astro.props;
const source = showArticle && 'article_slug' in claim ? claim : null;
---

<li class="rounded-[var(--radius-card)] border border-line bg-surface p-4 sm:p-5">
  <div class="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[0.75rem] text-muted">
    <VerdictBadge verdict={claim.verdict} reviewed={claim.reviewed} />
    <span>{KIND_LABEL[claim.kind] ?? '其他'}</span>
    <span aria-hidden="true" class="text-line">·</span>
    <span>{METHOD_LABEL[claim.method]}</span>
    {
      source ? (
        <a
          href={`/article/${encodeURIComponent(source.article_slug)}#factcheck`}
          class="min-w-0 truncate transition-colors hover:text-accent sm:ml-auto"
        >
          <time datetime={source.date} class="tabular">{formatDateWithWeekday(source.date)}</time>
          ・{displayTitle(source.article_title)}
        </a>
      ) : (
        <span class="tabular sm:ml-auto">影片 {mmss(claim.timestamp)}</span>
      )
    }
  </div>

  <blockquote class="prose-cn mt-3 border-l-2 border-line pl-3 text-[0.95rem]">
    「{claim.quote}」
  </blockquote>
  <p class="mt-2 text-[0.9rem] text-ink-2">{claim.statement}</p>
  {claim.rationale && (
    <p class="mt-2 text-[0.82rem] leading-relaxed text-muted">判定理由：{claim.rationale}</p>
  )}

  {
    claim.evidence.length > 0 && (
      <div class="mt-4 space-y-2.5">
        {claim.evidence.map((e, i) => {
          const official = safeExternalUrl(e.official_url);
          const api = safeExternalUrl(e.api_url);
          return (
            <details open={i === 0} class="rounded-lg border border-line-soft bg-bg-soft/60 px-3 py-2.5">
              <summary class="flex items-start gap-2 text-[0.82rem] text-ink-2">
                <span class="mt-0.5 shrink-0 rounded bg-surface-2 px-1.5 text-[0.68rem] text-muted">
                  {e.source === 'law' ? '法條' : '議案'}
                </span>
                <span class="min-w-0 flex-1">{e.title}</span>
                <svg viewBox="0 0 24 24" class="chev mt-1 h-3 w-3 shrink-0 text-muted transition-transform" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                  <path d="M6 9l6 6 6-6" stroke-linecap="round" stroke-linejoin="round" />
                </svg>
              </summary>
              <p class="mt-2 text-[0.82rem] leading-relaxed whitespace-pre-line text-muted">{e.excerpt}</p>
              <p class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[0.75rem]">
                {official && (
                  <a href={official} target="_blank" rel="noopener noreferrer" class="text-accent underline underline-offset-4">
                    官方來源
                  </a>
                )}
                {api && (
                  <a href={api} target="_blank" rel="noopener noreferrer" class="text-muted underline underline-offset-4 hover:text-accent">
                    原始資料（LYAPI）
                  </a>
                )}
              </p>
            </details>
          );
        })}
      </div>
    )
  }
</li>
```

- [ ] **Step 4: `web/news/src/components/FactCheckSection.astro`**

```astro
---
import type { Claim } from '../lib/types';
import { VERDICTS, VERDICT_LABEL } from '../lib/factcheck';
import ClaimItem from './ClaimItem.astro';

interface Props {
  claims: Claim[];
  /** 後端已經跑完查核。false 時不能說「沒有可查證的陳述」——根本還沒查。 */
  checked: boolean;
}
const { claims, checked } = Astro.props;
const counts = VERDICTS.map((v) => ({ v, n: claims.filter((c) => c.verdict === v).length }))
  .filter((x) => x.n > 0);
---

<section id="factcheck" class="mt-14 scroll-mt-24 border-t border-line-soft pt-10" aria-labelledby="factcheck-h">
  <div class="flex flex-wrap items-baseline gap-x-4 gap-y-2">
    <h2 id="factcheck-h" class="headline text-2xl font-semibold">查證</h2>
    {counts.length > 0 && (
      <p class="tabular flex flex-wrap gap-x-3 text-[0.8rem] text-muted">
        {counts.map(({ v, n }) => <span>{VERDICT_LABEL[v]} {n}</span>)}
      </p>
    )}
  </div>
  <p class="mt-3 max-w-2xl text-[0.82rem] leading-relaxed text-muted">
    自動查核：從委員的發言挑出可以用官方法條或議案資料驗證的陳述，取回原文比對。數字由程式比對；文字由模型判讀，而且必須逐字引用證據。「不符」經人工確認後才會顯示。
    <a href="/method" class="text-accent underline underline-offset-4">查證方法</a>
  </p>
  {
    !checked ? (
      <p class="mt-6 rounded-lg border border-dashed border-line px-4 py-6 text-center text-sm text-muted">
        這段發言還沒有完成查核。
      </p>
    ) : claims.length === 0 ? (
      <p class="mt-6 rounded-lg border border-dashed border-line px-4 py-6 text-center text-sm text-muted">
        這段發言沒有找到可以用官方資料查證的陳述。
      </p>
    ) : (
      <ol class="mt-6 space-y-4">
        {claims.map((c) => <ClaimItem claim={c} />)}
      </ol>
    )
  }
</section>
```

- [ ] **Step 5: `web/news/src/components/TrustScore.astro`**

```astro
---
import type { FactCheckScore } from '../lib/types';
import { formatRate } from '../lib/factcheck';

interface Props {
  score: FactCheckScore;
}
const { score } = Astro.props;
const segments = [
  { n: score.supported, cls: 'bg-ok', label: '相符' },
  { n: score.partial, cls: 'bg-partial', label: '部分相符' },
  { n: score.contradicted, cls: 'bg-danger', label: '不符' },
].filter((s) => s.n > 0);
const detail =
  score.rate !== null
    ? `${score.checked} 則可查證陳述中 ${score.supported} 則相符` +
      (score.partial ? `、${score.partial} 則部分相符` : '') +
      (score.contradicted ? `、${score.contradicted} 則不符` : '')
    : `可查證陳述 ${score.checked}／${score.min_sample} 則，滿 ${score.min_sample} 則才計算`;
---

<div class="w-full rounded-[var(--radius-card)] border border-line bg-surface p-5 sm:w-80">
  <p class="text-[0.7rem] tracking-[0.2em] text-muted uppercase">查證相符率</p>
  {
    score.rate !== null ? (
      <p class="headline tabular mt-1 text-5xl font-semibold">{formatRate(score.rate)}</p>
    ) : (
      <p class="headline mt-2 text-2xl font-semibold text-muted">樣本不足</p>
    )
  }
  <p class="tabular mt-2 text-[0.8rem] leading-relaxed text-muted">{detail}</p>
  {
    score.checked > 0 && (
      <div
        class="mt-4 flex h-2 overflow-hidden rounded-full bg-surface-2"
        role="img"
        aria-label={segments.map((s) => `${s.label} ${s.n} 則`).join('、')}
      >
        {segments.map((s) => (
          <span class={s.cls} style={`width: ${(s.n / score.checked) * 100}%`} />
        ))}
      </div>
    )
  }
  <p class="mt-3 text-[0.75rem] leading-relaxed text-muted">
    {score.unverifiable > 0 && <>另有 {score.unverifiable} 則無法查證，不計入。</>}
    <a href="/method#score" class="text-accent underline underline-offset-4">怎麼算的</a>
  </p>
</div>
```

- [ ] **Step 6: 文章頁插入查證區塊**

`web/news/src/pages/article/[slug].astro`：import 區加 `import FactCheckSection from '../../components/FactCheckSection.astro';`。在 `<!-- 完整逐字稿 -->` 那一行之前插入：

```astro
        <!-- 查證 -->
        <FactCheckSection claims={article.claims} checked={article.factcheck_checked} />
```

- [ ] **Step 7: 委員頁加上相符率與查證紀錄**

`web/news/src/pages/speaker/[name].astro`：

import 區加：

```astro
import ClaimItem from '../../components/ClaimItem.astro';
import TrustScore from '../../components/TrustScore.astro';
import { getArticles, getSpeakerClaims, getSpeakers, statusFor } from '../../lib/api';
```

（取代原本的 `import { getArticles, getSpeakers, statusFor } from '../../lib/api';`）

資料抓取改成：

```ts
const [listRes, speakersRes, claimsRes] = await Promise.all([
  getArticles({ speaker: name, page, page_size: PAGE_SIZE }),
  getSpeakers(),
  getSpeakerClaims(name),
]);
```

並在 `const retryHref = ...` 之後加：

```ts
const record = claimsRes.ok ? claimsRes.data : null;
```

報頭 `<section>` 裡的 `<div class="mx-auto max-w-6xl px-4 py-10 sm:px-6 sm:py-12">` 改成左右排（窄螢幕自動換行）：

```astro
    <div class="mx-auto flex max-w-6xl flex-wrap items-end justify-between gap-6 px-4 py-10 sm:px-6 sm:py-12">
      <div>
        <!-- 原本的 Legislator 標籤、h1、篇數那三段原封不動放在這裡 -->
      </div>
      {record && <TrustScore score={record.score} />}
    </div>
```

在 `{list && <Pagination ... />}` 之後、「其他委員」那一段之前插入：

```astro
    {
      record && record.items.length > 0 && (
        <section class="mt-16 border-t border-line-soft pt-8" aria-labelledby="record-h">
          <h2 id="record-h" class="headline text-xl font-semibold">查證紀錄</h2>
          <p class="mt-2 text-[0.8rem] text-muted">
            每一則都附上比對的官方資料。「不符」經人工確認後才會列出。
          </p>
          <ol class="mt-5 space-y-4">
            {record.items.map((c) => <ClaimItem claim={c} showArticle={true} />)}
          </ol>
        </section>
      )
    }
```

- [ ] **Step 8: 「查證方法」頁 `web/news/src/pages/method.astro`**

```astro
---
import Base from '../layouts/Base.astro';
import { MIN_SAMPLE, VERDICT_LABEL } from '../lib/factcheck';
import VerdictBadge from '../components/VerdictBadge.astro';

const verdicts = [
  { v: 'supported', text: '主張的每個數字都出現在證據裡；或模型判讀為支持，而且它引用的句子確實在證據中。' },
  { v: 'partial', text: '主張有好幾個數字，只有一部分在證據裡找得到。' },
  { v: 'contradicted', text: '證據裡有同一類的數字，但一個都對不上；或模型判讀為衝突且引用成立。一律經人工確認才公開。' },
  { v: 'unverifiable', text: '找不到對應的法條或議案、資料來源暫時取不到、證據不足以判斷，或模型的引用不在證據中。' },
] as const;
---

<Base title="查證方法" description="立院質詢日報的事實查核怎麼做、查證相符率怎麼算。" back={true}>
  <article class="prose-cn mx-auto max-w-3xl px-4 py-10 sm:px-6 sm:py-14">
    <p class="text-[0.7rem] tracking-[0.3em] text-accent uppercase">Methodology</p>
    <h1 class="headline mt-3 text-3xl font-semibold text-ink sm:text-4xl">查證方法</h1>
    <p class="mt-5">
      我們查的是「委員說的，和官方資料對不對得上」，不是「委員有沒有說謊」。說錯、口誤、語音辨識的錯字，在資料上看起來都一樣；我們無從判斷意圖。
    </p>

    <h2 class="headline mt-12 text-2xl font-semibold text-ink">查什麼</h2>
    <ul class="mt-4 list-disc space-y-2 pl-5">
      <li><strong class="text-ink">現行法條的內容</strong>：罰鍰金額、刑期、期限。以發言當天有效的版本為準——法律會修，修法前說的話要對照修法前的條文。</li>
      <li><strong class="text-ink">議案的內容</strong>：某個版本編列多少、要把罰則改成多少、是誰提的。</li>
      <li><strong class="text-ink">議案的進度</strong>：是否已經三讀、目前在哪個階段。</li>
    </ul>
    <p class="mt-4">
      只查核該段影片的委員本人。立法院的逐字稿沒有標記講者，委員會質詢中官員的回答分不出是誰說的，所以不收。意見、評價、承諾與預測也不查。
    </p>

    <h2 class="headline mt-12 text-2xl font-semibold text-ink">資料從哪裡來</h2>
    <p class="mt-4">
      法條與議案取自 <a href="https://ly.govapi.tw/" class="text-accent underline underline-offset-4" rel="noopener noreferrer" target="_blank">LYAPI</a>——公民科技社群整理的立法院資料服務，不是立法院官方。所以每一筆證據都附上兩個連結：官方來源（全國法規資料庫、立法院議事暨公報資訊網）與 LYAPI 的原始資料，任何人都能自己核對。
    </p>

    <h2 class="headline mt-12 text-2xl font-semibold text-ink">怎麼判定</h2>
    <p class="mt-4">
      語言模型只做兩件事：從逐字稿挑出可查證的陳述，以及在沒有數字可比的時候判讀文字。它不給分數，也不判斷數字對不對。
    </p>
    <ul class="mt-4 list-disc space-y-2 pl-5">
      <li>模型挑出的每一句話都必須在逐字稿裡逐字找得到，找不到就丟掉——它不能替委員說他沒說過的話。</li>
      <li>數字由程式比對：「三萬元」「3萬」「5到25萬」先換算成數值，只比同一類（金額對金額、期間對期間）。</li>
      <li>文字由模型判讀，但它必須逐字引用證據中的一句話作為依據；引用不在證據裡，判讀就作廢。</li>
    </ul>
    <dl class="mt-6 space-y-4">
      {verdicts.map(({ v, text }) => (
        <div class="flex flex-col gap-1.5 sm:flex-row sm:gap-4">
          <dt class="shrink-0 sm:w-28"><VerdictBadge verdict={v} /></dt>
          <dd class="text-[0.95rem]">{text}</dd>
        </div>
      ))}
    </dl>

    <h2 class="headline mt-12 text-2xl font-semibold text-ink">人工確認</h2>
    <p class="mt-4">
      「{VERDICT_LABEL.contradicted}」不會自動公開。把具名的人標成講錯，是這個功能唯一可能傷人的地方，而語音辨識的錯字（例如「非紅供應鏈」被聽成「飛鴻公園」）最容易被誤判成不符。每一則「不符」都經人看過、核准後才會出現在網站上並計入分數；誤判的會被駁回。
    </p>

    <h2 id="score" class="headline mt-12 scroll-mt-24 text-2xl font-semibold text-ink">查證相符率怎麼算</h2>
    <p class="mt-4">只算已公開的判定，「無法查證」不計入——查不到不代表說錯。</p>
    <div class="tabular mt-5 overflow-x-auto rounded-lg border border-line bg-surface px-4 py-4 text-[0.95rem] text-ink">
      <p>n ＝ 相符 ＋ 部分相符 ＋ 不符</p>
      <p class="mt-2">查證相符率 ＝（相符 ＋ 0.5 × 部分相符 ＋ 1）÷（n ＋ 2）</p>
    </div>
    <p class="mt-4">
      分子分母各加的 1 與 2 讓樣本少時往 50% 靠：只查到一則而且不符，不會變成 0%。可查證的陳述未滿 {MIN_SAMPLE} 則時不顯示百分比，只顯示「樣本不足」與各類的數量。
    </p>

    <h2 class="headline mt-12 text-2xl font-semibold text-ink">已知限制</h2>
    <ul class="mt-4 list-disc space-y-2 pl-5">
      <li>逐字稿由立法院的 AI 自動產生，錯字多，台語發言更差。錯字可能讓陳述找不到對應資料，而被判為無法查證。</li>
      <li>委員沒講是哪一條法律、哪一個條號時，通常查不到；我們不會拿答案去整部法律裡找「剛好有這個數字」的條文。</li>
      <li>政府統計資料、新聞與事實查核中心的報告目前都還沒有納入。</li>
      <li>查證相符率反映的是「可查證的那一小部分」陳述，不是一個人整體的誠信。</li>
    </ul>
  </article>
</Base>
```

- [ ] **Step 9: 頁尾連結與 README**

`web/news/src/components/SiteFooter.astro`：在「立法院隨選視訊（IVOD）」那個 `<p>` 之後加：

```astro
        <p class="mt-2">
          <a href="/method" class="underline decoration-line underline-offset-4 transition-colors hover:text-accent">
            查證方法
          </a>
        </p>
```

`web/news/README.md` 的「## 頁面」表格加兩列：

```markdown
| `/speaker/[name]` | 委員頁：查證相符率（未滿 5 則可查證陳述顯示「樣本不足」）與逐則查證紀錄。 |
| `/method` | 查證方法：資料來源、四種判定、人工確認、查證相符率公式與已知限制。 |
```

（若表格裡已經有 `/speaker/[name]` 那一列，改寫那一列的說明即可，不要重複。）文章頁那一列的說明末尾補上「逐字稿之前有『查證』區塊，列出每則可查證的陳述、判定與證據原文」。

- [ ] **Step 10: 型別檢查與建置**

Run: `cd web/news && npx astro check && npm run build`
Expected: `0 errors`，建置成功。看完整輸出。

- [ ] **Step 11: 實際看畫面（示範資料）**

Run（背景執行）：`cd web/news && USE_FIXTURE=1 HOST=127.0.0.1 PORT=4322 node ./dist/server/entry.mjs`

用瀏覽器（或 Playwright 截圖）確認：
- `http://127.0.0.1:4322/article/2026-08-27-900001`：逐字稿之前有「查證」區塊，6 則，第一則證據預設展開，徽章顏色依判定不同
- `http://127.0.0.1:4322/speaker/%E7%AF%84%E4%BE%8B%E4%B8%80`（範例一）：右上顯示 `79%`、「5 則可查證陳述中 4 則相符、1 則部分相符」，下方有查證紀錄
- 範例二的委員頁顯示「樣本不足」
- 範例三的「不符」徽章帶「・已人工確認」
- `/method` 正常顯示；深色與淺色主題下徽章都讀得清楚；390px 寬不會橫向捲動

看完停掉這個伺服器。

- [ ] **Step 12: Commit**

```bash
git add web/news
git commit -m "feat(web): 查證區塊、委員查證相符率與查證方法頁"
```

---

### Task 13: 準確率評估腳本與標註樣本

**Files:**
- Create: `scripts/eval_factcheck.py`、`scripts/factcheck_labels.jsonl`

**Interfaces:**
- Consumes: Task 6 的 `build_factcheck`、Task 1 的 `Speech`、`Verdict`
- Produces: `uv run python scripts/eval_factcheck.py scripts/factcheck_labels.jsonl` 印出每篇抽出的主張、每則標註的命中與否、混淆矩陣、「相符」精確率與「不符」召回率

標註檔每一行：`{"ivod_id", "speaker", "date", "meeting", "transcript", "expected": [{"quote_contains", "verdict"}]}`。`quote_contains` 是這則主張 quote 裡一定會出現的一小段，用來把模型抽出的主張對回標註。

- [ ] **Step 1: 寫 `scripts/eval_factcheck.py`**

```python
#!/usr/bin/env python3
"""事實查核的準確率評估：拿人工標註的樣本跑整條 pipeline，印出混淆矩陣。

    uv run python scripts/eval_factcheck.py scripts/factcheck_labels.jsonl

需要 Ollama 與網路（會真的查 LYAPI）。重點看兩個數字：
- 「相符」的精確率：這一類自動發佈又計分，判錯會灌高分數
- 「不符」的召回率：漏掉的會被當成相符或無法查證
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from factcheck.composition import build_factcheck  # noqa: E402
from factcheck.domain.entities import Speech, Verdict  # noqa: E402

MISSED = "（沒有抽出）"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("labels")
    parser.add_argument("--ollama", default="http://localhost:11434")
    parser.add_argument("--model", default="qwen3.5:9b")
    parser.add_argument("--num-ctx", type=int, default=32768)
    args = parser.parse_args()

    usecase = build_factcheck(args.ollama, args.model, args.num_ctx)
    confusion: Counter[tuple[str, str]] = Counter()
    with open(args.labels, encoding="utf-8") as f:
        items = [json.loads(line) for line in f if line.strip()]

    for item in items:
        speech = Speech(item["speaker"], date.fromisoformat(item["date"]),
                        item.get("meeting", ""), item["transcript"])
        checked = usecase.execute(speech, lambda fraction, status: None, lambda: False)
        print(f"\n== {item.get('ivod_id', '')} {item['speaker']} {item['date']}：抽出 {len(checked)} 則")
        for c in checked:
            print(f"   [{c.verdict} / {c.method}] 「{c.claim.quote[:40]}」→ {c.rationale[:90]}")
        for expected in item["expected"]:
            hit = next((c for c in checked if expected["quote_contains"] in c.claim.quote), None)
            predicted = str(hit.verdict) if hit else MISSED
            mark = "✓" if predicted == expected["verdict"] else "✗"
            print(f"   {mark} 標註「{expected['quote_contains']}」應為 {expected['verdict']}，得到 {predicted}")
            confusion[(expected["verdict"], predicted)] += 1

    columns = [v.value for v in Verdict] + [MISSED]
    print("\n混淆矩陣（列＝標註，欄＝得到）")
    print("".ljust(14) + "".join(c.ljust(14) for c in columns))
    for row in [v.value for v in Verdict]:
        print(row.ljust(14) + "".join(str(confusion[(row, c)]).ljust(14) for c in columns))

    total = sum(confusion.values())
    correct = sum(n for (e, p), n in confusion.items() if e == p)
    predicted_supported = sum(n for (e, p), n in confusion.items() if p == Verdict.SUPPORTED)
    true_supported = confusion[(Verdict.SUPPORTED.value, Verdict.SUPPORTED.value)]
    actual_contradicted = sum(n for (e, p), n in confusion.items() if e == Verdict.CONTRADICTED)
    caught = confusion[(Verdict.CONTRADICTED.value, Verdict.CONTRADICTED.value)]
    print(f"\n標註 {total} 則，正確 {correct} 則")
    if predicted_supported:
        print(f"「相符」精確率：{true_supported}/{predicted_supported}")
    if actual_contradicted:
        print(f"「不符」召回率：{caught}/{actual_contradicted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: 從資料庫匯出標註樣本**

這兩篇的預期判定都已經人工對過官方原文（醫療法第 106 條；行政院與國民黨黨團的無人載具條例草案）：

```bash
cd services/news && PYTHONIOENCODING=utf-8 uv run python manage.py shell -c "
import json
from articles.models import Article
labels = {
    '171140': [{'quote_contains': '現行的3萬到5萬', 'verdict': 'supported'},
               {'quote_contains': '3年以下有期徒刑', 'verdict': 'supported'}],
    '171179': [{'quote_contains': '6年2100億', 'verdict': 'supported'},
               {'quote_contains': '6年2400億', 'verdict': 'supported'}],
}
with open('../../scripts/factcheck_labels.jsonl', 'w', encoding='utf-8') as f:
    for ivod_id, expected in labels.items():
        a = Article.objects.get(ivod_id=ivod_id)
        for e in expected:
            assert e['quote_contains'] in a.transcript_text, e
        f.write(json.dumps({'ivod_id': ivod_id, 'speaker': a.speaker, 'date': a.date.isoformat(),
                            'meeting': a.meeting, 'transcript': a.transcript_text,
                            'expected': expected}, ensure_ascii=False) + '\n')
print('ok')
"
```

Expected: 印出 `ok`，產生兩行的 `scripts/factcheck_labels.jsonl`

- [ ] **Step 3: 確認腳本能載入**

Run: `uv run python scripts/eval_factcheck.py --help`
Expected: 印出用法說明（不需要 Ollama）

- [ ] **Step 4: Commit**

```bash
git add scripts/eval_factcheck.py scripts/factcheck_labels.jsonl
git commit -m "feat(factcheck): 準確率評估腳本與第一批標註"
```

實際跑評估（需要 Ollama、會花幾分鐘）留給整合驗證，不在這個 task 裡。

---

## 整合驗證（全部 task 完成後）

1. `uv run pytest -q` 與 `cd services/news && uv run python manage.py test` 全部通過；`cd web/news && npx astro check` 0 errors
2. 重啟 GPU 主機上的 `serve_api.py`（新程式碼），`cd services/news && uv run python manage.py migrate && uv run python manage.py factcheck_articles --limit 10`，對本機 10 篇真實文章跑查核
3. `uv run python scripts/eval_factcheck.py scripts/factcheck_labels.jsonl` 看準確率
4. 前端接真實後端，確認文章頁與委員頁顯示真實查核結果；到 admin 審核「不符」
