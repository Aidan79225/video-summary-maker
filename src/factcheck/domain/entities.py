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
