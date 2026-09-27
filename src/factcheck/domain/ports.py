"""use case 需要的外部能力。實作在 infrastructure，測試用假的。"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date
from typing import Protocol

from .entities import Evidence, ExtractedClaim, Judgement, Speech

ProgressCallback = Callable[[float | None, str], None]
CancelCheck = Callable[[], bool]


class ClaimExtractor(Protocol):
    def extract(self, speech: Speech) -> list[ExtractedClaim]: ...


class EvidenceSource(Protocol):
    def find(self, claim: ExtractedClaim, on: date) -> list[Evidence]: ...


class TextJudge(Protocol):
    def judge(self, statement: str, evidence: Sequence[Evidence]) -> Judgement: ...
