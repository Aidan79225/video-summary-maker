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
    # 實測模型會寫「臺灣民眾黨」（臺），LYAPI 的欄位寫「台灣民眾黨黨團」（台）：
    # 兩種寫法都要能對到同一個黨團
    "臺灣民眾黨": "台灣民眾黨黨團",
    "台灣民眾黨": "台灣民眾黨黨團",
    "時代力量": "時代力量黨團",
}
# 審查報告把好幾個版本併在一起，分不出是誰提的
_REVIEW_REPORT = "審查報告"
# 對照表裡不是「提案內容」的欄位：說明是理由、現行是修法前的條文
_SKIP_COLUMNS = frozenset({"說明", "現行"})
# 模型常把這些泛用的法案格式詞也算進 bill_keywords，但議案名稱通常只寫法規或
# 政策本體（例如「醫療法部分條文修正案」查無此名，議案名稱其實是「醫療法」；
# 「無人機產業發展」查無此名，議案名稱其實是「無人機」或「無人載具」）。
# 依序反覆剝除，直到剝不動為止
_BILL_SUFFIXES = ("修正草案", "修正案", "草案", "修正", "部分條文", "條文", "產業發展", "發展")
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
    # LYAPI 的提案單位欄位「臺」「台」兩種寫法都有，模型抽出來的也不一定跟欄位一致，
    # 統一換成「台」再比對
    proposer = proposer.replace("臺", "台")
    field = field.replace("臺", "台")
    if proposer in field or _ALIASES.get(proposer, proposer) in field:
        return True
    # 別名表只列了常見的幾個政黨：其他黨只要欄位是「政黨名稱＋團」（不一定疊字寫成
    # 「黨黨團」）也算同一個黨團，不必每個政黨都手動加進別名表
    return proposer.endswith("黨") and f"{proposer}團" in field


# 不會出現在議案名稱裡、卻常被模型塞進關鍵詞的提案單位
_NOT_KEYWORDS = frozenset(_ALIASES) | frozenset(_ALIASES.values()) | {"行政院"}


def _strip_bill_suffixes(token: str) -> str:
    stripped = True
    while stripped:
        stripped = False
        for suffix in _BILL_SUFFIXES:
            if len(token) > len(suffix) and token.endswith(suffix):
                token = token[: -len(suffix)]
                stripped = True
                break
    return token


def _keywords(claim: ExtractedClaim) -> list[str]:
    # 實測模型會給「無人機 行政院」「醫療暴力 臺灣民眾黨」：用提案者或政黨名稱去搜，
    # 會命中幾百個不相干的議案。提案者另外由 proposer 比對，不該拿來搜尋。
    tokens = [k for k in re.split(r"[\s、，,]+", claim.bill_keywords.strip())
              if k and k != claim.proposer and k not in _NOT_KEYWORDS and "黨" not in k]
    keywords: list[str] = []
    for token in tokens:
        if token not in keywords:
            keywords.append(token)
    # 原詞查不到時，退而求其次用去掉法案格式詞的短詞再查一次
    for token in tokens:
        short = _strip_bill_suffixes(token)
        if len(short) >= 2 and short not in keywords:
            keywords.append(short)
    # 關鍵詞都查不到、或整段被過濾光時，law 欄位（例如「醫療法」）是最後的退路
    if claim.law and claim.law not in keywords:
        keywords.append(claim.law)
    return keywords


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
        rows = self._search(_keywords(claim), term_on(on))
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

    def _search(self, keywords: list[str], term: int) -> list[dict]:
        for keyword in keywords:
            rows = self._api.bills_search(keyword, term)
            if rows:
                return rows
        return []

    def _evidence(self, claim: ExtractedClaim, data: dict, bill_id: str) -> Evidence:
        if claim.kind == ClaimKind.BILL_STATUS:
            # 狀態是 LYAPI 查詢當下的，不是發言當天的：標明白，判讀才不會當成發言時的狀態
            excerpt = (f"查詢時的議案狀態：{data.get('議案狀態') or '不明'}；"
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
