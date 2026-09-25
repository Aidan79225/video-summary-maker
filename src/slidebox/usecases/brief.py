"""質詢卡的純邏輯：給模型看的摘要文字、以及模型回應的落地檢查。

不碰網路、不碰 LLM。模型只負責挑數字、寫句子；「這個數字是不是講者真的
說過」由這裡判定——模型畫不出讀者看得懂的圖，但它很會順手補一個逐字稿
裡沒有的數字，而數字比文字更有說服力，錯了傷害更大。
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import replace

from ..domain.entities import Ask, Brief, KeyNumber, Slide

# 卡片放得下的量。超過的不是錯，只是取前面幾個：模型被要求依重要性排序。
MAX_KEY_NUMBERS = 4
MAX_ASKS = 4

_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_SMALL = {"十": 10, "百": 100, "千": 1000}
_BIG = {"萬": 10_000, "億": 100_000_000}
_CN_CHARS = "".join(_DIGITS) + "".join(_SMALL) + "".join(_BIG)
_CN_RUN = re.compile(rf"[{_CN_CHARS}]+(?:點[{''.join(_DIGITS)}]+[萬億]*)?")
_UNIT_SCALE = {"萬": 10_000, "億": 100_000_000, "千": 1000, "百": 100}
# 比對時要忽略的字元：空白、逗號、時間標記。逐字稿是「mm:ss 文字」的格式，
# 模型引用時通常不會抄時間。
_NOISE = re.compile(r"[\s,，、。！？!?「」『』（）()\[\]:：]+")
_TIME_MARK = re.compile(r"^\d{1,3}:\d{2}\s", re.MULTILINE)


def slides_digest(slides: Sequence[Slide]) -> str:
    """把分段摘要壓成一份給模型讀的文字。

    detail 已經是「沒看影片也能懂」的完整敘述，所以質詢卡不必再讀整份
    字幕；標題與條列留著是給模型抓重點順序用的。
    """
    parts: list[str] = []
    for slide in slides:
        lines = [f"## {slide.title}".rstrip()]
        lines.extend(f"- {b}" for b in slide.bullets)
        if slide.detail.strip():
            lines.append(slide.detail.strip())
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _squash(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = _TIME_MARK.sub("", text)
    return _NOISE.sub("", text)


def parse_chinese_number(text: str) -> float | None:
    """「二千一百」→ 2100、「八十二點四億」→ 8.24e9、「十五」→ 15。

    小數點之後只能接數字，再接萬／億；「千萬不要」這種沒有數目的字串
    回 None。
    """
    if not text:
        return None
    head, _, tail = text.partition("點")
    value = _parse_integer(head)
    if value is None:
        return None
    if not _:
        return value
    digits = "".join(str(_DIGITS[ch]) for ch in tail if ch in _DIGITS)
    rest = tail[len([ch for ch in tail if ch in _DIGITS]):]
    if not digits or any(ch not in _BIG for ch in rest):
        return None
    value += float("0." + digits)
    for ch in rest:
        value *= _BIG[ch]
    return value


def _parse_integer(text: str) -> float | None:
    if not text or any(ch not in _CN_CHARS for ch in text):
        return None
    if not any(ch in _DIGITS for ch in text) and not text.startswith("十"):
        return None
    result = 0.0        # 已經乘上萬、億的部分
    section = 0.0       # 萬以下的累計
    number: float | None = None
    for ch in text:
        if ch in _DIGITS:
            number = float(_DIGITS[ch])
        elif ch in _SMALL:
            # 「十五」開頭的十前面沒有數字，代表一十
            section += (1.0 if number is None else number) * _SMALL[ch]
            number = None
        else:
            section += number or 0.0
            result += (section or 1.0) * _BIG[ch]
            section = 0.0
            number = None
    return result + section + (number or 0.0)


def _numbers_in(text: str) -> set[float]:
    """文字裡所有能讀出來的數值，阿拉伯與中文數字都算，含萬／億進位。"""
    found: set[float] = set()
    for m in re.finditer(r"(\d+(?:\.\d+)?)([萬億千百]?)", text):
        value = float(m.group(1))
        found.add(value)
        if m.group(2):
            found.add(value * _UNIT_SCALE[m.group(2)])
    for m in _CN_RUN.finditer(text):
        value = parse_chinese_number(m.group(0))
        if value is not None:
            found.add(value)
    return found


def number_in_text(value: str, unit: str, text: str) -> bool:
    """模型給的數字是不是出現在這句話裡。

    「82.4 億元」對得上「八十二點四億元」，也對得上「82.4億」；「1860 架」
    對得上「一千八百六十架」。比對的是數值而不是字面，因為逐字稿是語音
    辨識的產物，同一個數字有好幾種寫法。
    """
    raw = unicodedata.normalize("NFKC", value or "").replace(",", "").strip()
    if not raw:
        return False
    try:
        wanted = float(raw)
    except ValueError:
        return False
    text = _squash(text)
    if raw in text:
        return True
    candidates = {wanted}
    scale = next((_UNIT_SCALE[ch] for ch in unit or "" if ch in _UNIT_SCALE), None)
    if scale:
        candidates.add(wanted * scale)
    # 相對誤差：八十二點四億乘出來會帶浮點尾數
    return any(abs(v - c) <= 1e-9 * max(1.0, abs(c))
               for v in _numbers_in(text) for c in candidates)


def quote_in_transcript(quote: str, transcript: str) -> bool:
    """引用的句子必須真的在逐字稿裡（忽略空白與標點）。"""
    needle = _squash(quote)
    return bool(needle) and needle in _squash(transcript)


def ground_brief(brief: Brief, transcript: str) -> Brief:
    """丟掉沒有根據的關鍵數字、清掉空白的要求、截到卡片放得下的量。

    只有 key_numbers 做逐字稿比對：一句話與要求是摘要，跟分段的 detail
    一樣本來就是模型的轉述。數字不一樣——它會被讀者直接引用。
    """
    numbers = tuple(
        replace(n, value=n.value.strip(), unit=n.unit.strip(),
                label=n.label.strip(), quote=n.quote.strip())
        for n in brief.key_numbers
        if n.label.strip()
        and quote_in_transcript(n.quote, transcript)
        and number_in_text(n.value, n.unit, n.quote)
    )[:MAX_KEY_NUMBERS]
    asks = tuple(
        Ask(request=a.request.strip(), deadline=a.deadline.strip(),
            response=a.response.strip())
        for a in brief.asks if a.request.strip()
    )[:MAX_ASKS]
    return Brief(one_liner=brief.one_liner.strip(), key_numbers=numbers, asks=asks)


def validate_brief(brief: Brief) -> list[str]:
    """回傳問題描述；空清單表示可用。只擋「等於沒寫」的輸出。"""
    problems: list[str] = []
    if len(brief.one_liner.strip()) < 10:
        problems.append("one_liner 太短或空白，需要一句完整的話")
    return problems
