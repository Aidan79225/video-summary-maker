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
