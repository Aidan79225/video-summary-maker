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
