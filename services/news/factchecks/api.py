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
