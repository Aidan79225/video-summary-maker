"""重點裡的數字核對（純邏輯）：比「數值與單位種類」，不比字面。

為什麼不比字面：模型常把 6.3萬 寫成 六點三萬、把 九千一百份 寫成 9100份，字面比對會把這些忠實的
改寫全部丟掉（實測：模型的 45 個數字被丟掉的點裡，36 個只是寫法不同）。反過來，只比「數字本身」
又會放過「單位被改掉」的錯：百分之十二 → 十二分鐘、55% → 五十五成（= 550%）、百分之九十一 → 九十一成，
這三種都實際出現過。所以這裡兩件事一起做：

1. 阿拉伯數字與中文數字（零一二兩三…十百千萬億、「點」小數、口語的「四萬二」「三百五」）都換成精確的
   分數（Fraction）比較數值。
2. 百分比一律換成「百分數」比較：百分之N、N% 是 N；N成 是 10×N；N成M（口語的「四成二」）是 10×N+M。
   所以 四成二 = 42%，而 五十五成 ≠ 55%。重點裡的一個「數字＋實體單位」（分鐘、小時、元、人…）
   若那段話裡同一個數值只出現在百分比，就是單位被改掉，丟掉。

規則（number_problem）：重點裡每一個需要依據的數字，都要在被引用的那段話裡找得到「同值、同種類」的數字。
找不到才回傳原因（繁體中文，可以原樣放進重試提示）；全部找得到回傳 None。

已知的、故意不處理的小誤判：重點寫「約三成」而那段話說 31.5%（四捨五入）會被丟掉；
寧可丟掉，不猜「差不多算不算同一個數字」。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from fractions import Fraction

_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}
_BIG_UNITS = {"萬": 10 ** 4, "億": 10 ** 8}
_NUMERAL_CHARS = "零〇一二兩三四五六七八九十百千萬億"
_MAGNITUDE_CHARS = frozenset("十百千萬億")

# 一個數字運算式：阿拉伯數字（可有千分位逗號與小數），或一串中文數字（可帶「點」小數），
# 後面可以再接中文數字或阿拉伯數字（1.2萬、三百二十、1100萬、二百一十）。
_EXPRESSION_RE = re.compile(
    rf"(?:\d+(?:,\d{{3}})*(?:\.\d+)?|[{_NUMERAL_CHARS}]+(?:點[零〇一二兩三四五六七八九]+)?)"
    rf"(?:[{_NUMERAL_CHARS}]|\d+(?:\.\d+)?)*")

# 光是一串中文數字（二、三十五）不一定是數量（「第二」「一起」），只有帶「十百千萬億」，
# 或後面跟著這些單位字的才需要依據。沿用既有的單位字，再加上「折」（七折）。
# 已知的缺口：單獨一個中文數字後面接「個月、小時、份、張、座、間」不在這裡（三個月 對 六個月 擋不下來）。
# 實測把它們補進來，在 1100 多個重點裡一個真的錯都沒多擋，卻誤丟了一個等價寫法（六個半小時 = 六點五小時），
# 還讓一份文件過不了「丟棄比例」的門檻；要補，得先會讀「N個半」。
_UNIT_CHARS_AFTER = frozenset("年月日號歲週周成倍元塊點分鐘秒時天度里斤人次戶家件場屆期趟折")

# 「實體單位」：數字後面跟著這些，表示數量（不是百分比）。長的在前，只看開頭符合的第一個。
_ABSOLUTE_UNITS = (
    "分鐘", "小時", "公里", "公尺", "公頃", "公分", "個月", "人次",
    "天", "年", "週", "周", "元", "人", "戶", "座", "間", "棟", "輛", "份", "張", "次",
)

# 「百分之」換成這個不會出現在文字裡的控制字元：否則 百分之十二 的「百」會被讀成數字 100。
_PERCENT_MARK = "\x01"

PERCENT = "percent"        # 百分比（數值是百分數）
PLAIN = "plain"            # 一般的數字（數值本身，可能帶實體單位）


@dataclass(frozen=True)
class NumberMention:
    """文字裡的一個數字：原樣的寫法、數值、種類（PERCENT／PLAIN）、後面緊接的實體單位、位置。"""
    raw: str
    value: Fraction
    kind: str
    unit: str
    start: int
    end: int


def _int_value(run: str) -> Fraction | None:
    """一串數字（中文、阿拉伯、混著寫）的數值；讀不懂回傳 None。

    口語的省略也讀：三百五 = 350、四萬二 = 42000、一千二 = 1200；但 一百零六 = 106（中間有零就不省略）。
    """
    total = Fraction(0)             # 萬、億以上已經結算的部分
    section = Fraction(0)           # 目前這一節（萬以下）已經累計的部分
    current: Fraction | None = None  # 還沒遇到單位字的最後一個數
    last_unit = 0
    previous_was_chinese_digit = False
    zero_after_unit = False
    seen = False
    for token in re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?|.", run):
        if token[0].isdigit():
            current = Fraction(token.replace(",", ""))
            previous_was_chinese_digit = False
        elif token in _DIGITS:
            digit = _DIGITS[token]
            if digit == 0 and last_unit:
                zero_after_unit = True
            current = current * 10 + digit if previous_was_chinese_digit and current is not None else Fraction(digit)
            previous_was_chinese_digit = True
        elif token in _SMALL_UNITS:
            section += (current if current is not None else 1) * _SMALL_UNITS[token]
            current, previous_was_chinese_digit, last_unit = None, False, _SMALL_UNITS[token]
        elif token in _BIG_UNITS:
            section += current if current is not None else 0
            total += (section if section else 1) * _BIG_UNITS[token]
            section, current, previous_was_chinese_digit, last_unit = Fraction(0), None, False, _BIG_UNITS[token]
        else:
            return None
        seen = True
    if not seen:
        return None
    if (current is not None and last_unit >= 100 and current < 10 and not zero_after_unit
            and total + section > 0):
        current = current * Fraction(last_unit, 10)     # 三百五、四萬二、一千二
    return total + section + (current or 0)


def value_of(expression: str) -> Fraction | None:
    """一個數字運算式的數值（含「點」小數：六點三萬 = 63000）；讀不懂回傳 None。"""
    expression = unicodedata.normalize("NFKC", expression)
    if "點" not in expression:
        return _int_value(expression)
    head, _, tail = expression.partition("點")
    whole = _int_value(head) if head else Fraction(0)
    decimals = ""
    for char in tail:
        if char not in _DIGITS:
            break
        decimals += str(_DIGITS[char])
    if whole is None or not decimals:
        return None
    value = whole + Fraction(int(decimals), 10 ** len(decimals))
    for char in tail[len(decimals):]:
        value *= _BIG_UNITS.get(char) or _SMALL_UNITS.get(char) or 1
    return value


# 阿拉伯數字和後面的中文字之間的空白（「62 萬」「7 個月」「1.2 億」）：模型和語音辨識都常這樣寫，
# 讀數字時當作沒有。
_SPACE_BETWEEN_DIGIT_AND_CJK = re.compile(r"(?<=\d)\s+(?=[一-鿿])")


def _prepare(text: str) -> str:
    flat = _SPACE_BETWEEN_DIGIT_AND_CJK.sub("", unicodedata.normalize("NFKC", text))
    return re.sub(r"百分之\s*", _PERCENT_MARK, flat)


def _single_digit(char: str) -> int | None:
    if char.isdigit():
        return int(char)
    return _DIGITS.get(char)


def _starts_a_longer_numeral(flat: str, position: int) -> bool:
    """position 的那個數字字是不是一個更長的數字的開頭（三成五十 不是「三成五」後面接「十」）。"""
    following = flat[position + 1:position + 2]
    return bool(following) and (following in _MAGNITUDE_CHARS or following.isdigit())


def mentions(text: str) -> list[NumberMention]:
    """文字裡所有的數字（已讀成數值、分好百分比與一般數字）。"""
    return _mentions(_prepare(text))


def _mentions(flat: str) -> list[NumberMention]:
    found: list[NumberMention] = []
    skip_until = -1
    for match in _EXPRESSION_RE.finditer(flat):
        if match.start() < skip_until:
            continue                                    # 三成五 的「五」已經併進前面那個數字
        raw = match.group()
        value = value_of(raw)
        if value is None:
            continue
        after = flat[match.end():match.end() + 2]
        if flat[max(0, match.start() - 1):match.start()] == _PERCENT_MARK or after.startswith("%"):
            found.append(NumberMention(raw, value, PERCENT, "", match.start(), match.end()))
            continue
        if after.startswith("成"):
            percent = value * 10
            end = match.end() + 1
            digit = _single_digit(flat[end:end + 1]) if flat[end:end + 1] else None
            if digit is not None and not _starts_a_longer_numeral(flat, end):
                percent += digit                        # 四成二 = 42%
                end += 1
                skip_until = end
            found.append(NumberMention(raw + "成", percent, PERCENT, "", match.start(), end))
            continue
        unit = next((u for u in _ABSOLUTE_UNITS if after.startswith(u)), "")
        found.append(NumberMention(raw, value, PLAIN, unit, match.start(), match.end()))
    return found


def _needs_support(raw: str, following: str) -> bool:
    """這個一般數字需不需要在那段話裡找依據：阿拉伯數字一定要；中文數字要帶 十百千萬億 或後面接單位字。"""
    if re.search(r"\d", raw):
        return True
    if any(char in _MAGNITUDE_CHARS for char in raw):
        return True
    return bool(following) and following[0] in _UNIT_CHARS_AFTER


def _is_number_char(char: str) -> bool:
    return bool(char) and (char.isdigit() or char in _NUMERAL_CHARS)


def _compact(text: str) -> str:
    """NFKC、去掉空白，其他都留著（小數點與千分位逗號要留：2.8 不是 28）。"""
    return "".join(unicodedata.normalize("NFKC", text).split())


def _copied_whole(raw: str, support_text: str) -> bool:
    """raw 一字不差地出現在 support_text 裡，而且那一處就是一個完整的數字。

    只是更長的數字的一部分不算照抄：三 在 三十分鐘 裡、3 在 30 裡、8 在 2.8 裡、24 在 2,400 裡，
    都是不同的數字（模型把「三十分鐘」寫成「三分鐘」就是這樣漏掉的）。空白是例外：語音辨識常把
    「2000 萬」拆開，照抄的人寫成「2000萬」不該被丟掉，所以比對前先去掉空白。
    """
    needle, haystack = _compact(raw), _compact(support_text)
    if not needle:
        return False
    start = haystack.find(needle)
    while start != -1:
        end = start + len(needle)
        before = haystack[start - 1] if start else ""
        after = haystack[end] if end < len(haystack) else ""
        continues_before = _is_number_char(before) or (
            before in ".," and start >= 2 and haystack[start - 2].isdigit() and needle[0].isdigit())
        continues_after = _is_number_char(after) or (
            after in ".," and end + 1 < len(haystack) and haystack[end + 1].isdigit() and needle[-1].isdigit())
        if not continues_before and not continues_after:
            return True
        start = haystack.find(needle, start + 1)
    return False


def number_problem(text: str, support_text: str) -> str | None:
    """重點 text 裡的數字有沒有哪一個在 support_text（被引用的那段話）裡找不到「同值同種類」的對應。

    - 百分比：數值（百分數）要和那段話裡某個百分比相同；或和那段話裡「沒有實體單位的一般數字」相同
      （語音辨識常把「百分之」漏掉）。
    - 一般數字：整個數字一字不差地照抄（忽略空白；只是更長的數字的一部分不算）就算有；否則數值要和那段話裡
      某個一般數字或百分數相同——但如果它後面緊接實體單位（分鐘、元、人…），而那段話裡這個數值只以百分比
      出現，就是單位被改了。
    有問題回傳原因（繁體中文），沒有回傳 None。
    """
    support = _mentions(_prepare(support_text))
    support_percent = {m.value for m in support if m.kind == PERCENT}
    support_plain = {m.value for m in support if m.kind == PLAIN}
    support_bare = {m.value for m in support if m.kind == PLAIN and not m.unit}
    flat = _prepare(text)
    for mention in _mentions(flat):
        if mention.kind == PERCENT:
            if mention.value in support_percent or mention.value in support_bare:
                continue
            shown = flat[mention.start:mention.end].replace(_PERCENT_MARK, "")
            return f"數字「{shown}」不在被引用的那段話裡（百分比的數值對不上；數字與單位請照抄）"
        if not _needs_support(mention.raw, flat[mention.end:mention.end + 1]):
            continue
        if mention.unit and mention.value in support_percent and mention.value not in support_plain:
            return (f"數字「{mention.raw}{mention.unit}」在被引用的那段話裡是百分比，不是{mention.unit}"
                    f"（數字與單位請照抄）")
        if _copied_whole(mention.raw, support_text):
            continue
        if mention.value in support_plain or mention.value in support_percent:
            continue
        return f"數字「{mention.raw}」不在被引用的那段話裡（數字與單位請照抄）"
    return None
