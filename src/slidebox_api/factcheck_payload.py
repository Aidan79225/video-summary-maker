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
