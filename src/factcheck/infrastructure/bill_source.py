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
MAX_ROWS = 7
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
