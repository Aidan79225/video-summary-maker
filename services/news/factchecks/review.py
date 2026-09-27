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
