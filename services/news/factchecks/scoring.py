"""查證相符率：公開公式，前端「查證方法」頁寫的就是這一條。

只算已公開的判定；「無法查證」不計入——查不到不代表說錯。拉普拉斯平滑
讓樣本少時往 50% 靠，一則不符不會變成 0%。n < MIN_SAMPLE 不給百分比。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from django.db.models import QuerySet

from articles.models import ArticleStatus

from .models import PUBLIC_REVIEW_STATUSES, Claim, Verdict

MIN_SAMPLE = 5


@dataclass(frozen=True)
class Score:
    supported: int = 0
    partial: int = 0
    contradicted: int = 0
    unverifiable: int = 0

    @property
    def checked(self) -> int:
        return self.supported + self.partial + self.contradicted

    @property
    def rate(self) -> float | None:
        if self.checked < MIN_SAMPLE:
            return None
        return (self.supported + 0.5 * self.partial + 1) / (self.checked + 2)


def score_of(verdicts: Iterable[str]) -> Score:
    counts = Counter(str(v) for v in verdicts)
    return Score(
        supported=counts[Verdict.SUPPORTED.value],
        partial=counts[Verdict.PARTIAL.value],
        contradicted=counts[Verdict.CONTRADICTED.value],
        unverifiable=counts[Verdict.UNVERIFIABLE.value],
    )


def public_claims() -> QuerySet[Claim]:
    """網站上看得到的判定：自動發佈的，加上人工核准的；文章本身也要已發佈。"""
    return Claim.objects.filter(review_status__in=PUBLIC_REVIEW_STATUSES,
                                article__status=ArticleStatus.READY)


def scores_by_speaker() -> dict[str, Score]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for speaker, verdict in public_claims().values_list("article__speaker", "verdict"):
        grouped[speaker].append(verdict)
    return {speaker: score_of(verdicts) for speaker, verdicts in grouped.items()}
