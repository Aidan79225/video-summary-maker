"""查核一段發言：抽取 → 落地 → 取證 → 比對。

判定權的分配：數字由程式比，文字由模型判讀但引用要經程式確認。分數不在
這裡算——那是公開公式的事，放在 Pi 上。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..domain.entities import (
    CheckedClaim,
    ClaimKind,
    Evidence,
    ExtractedClaim,
    Method,
    Speech,
    Verdict,
)
from ..domain.errors import ModelOutputInvalid, OperationCancelled, SourceUnavailable
from ..domain.ports import CancelCheck, ClaimExtractor, EvidenceSource, ProgressCallback, TextJudge
from .compare import compare_numbers
from .grounding import is_grounded, locate
from .numbers import has_approximation


def _unverifiable(claim: ExtractedClaim, at: float, reason: str,
                  evidence: Sequence[Evidence] = ()) -> CheckedClaim:
    return CheckedClaim(claim, at, Verdict.UNVERIFIABLE, Method.NONE, reason, tuple(evidence))


class FactCheckUseCase:
    def __init__(self, extractor: ClaimExtractor,
                 sources: Mapping[ClaimKind, EvidenceSource], judge: TextJudge):
        self._extractor = extractor
        self._sources = dict(sources)
        self._judge = judge

    def execute(self, speech: Speech, progress: ProgressCallback,
                is_cancelled: CancelCheck) -> list[CheckedClaim]:
        progress(None, "抽取可查證的陳述…")
        grounded: list[tuple[ExtractedClaim, float]] = []
        for claim in self._extractor.extract(speech):
            at = locate(claim.quote, speech.transcript)
            # 逐字稿裡找不到：模型改寫或捏造了這句話，那不是委員說的
            if at is not None:
                grounded.append((claim, at))

        results: list[CheckedClaim] = []
        for i, (claim, at) in enumerate(grounded):
            if is_cancelled():
                raise OperationCancelled()
            progress(i / len(grounded), f"查核第 {i + 1}/{len(grounded)} 則")
            results.append(self._check(claim, at, speech))
        progress(1.0, f"查核完成，共 {len(results)} 則")
        return results

    def _check(self, claim: ExtractedClaim, at: float, speech: Speech) -> CheckedClaim:
        source = self._sources.get(claim.kind)
        if source is None:
            return _unverifiable(claim, at, "沒有這類主張的資料來源")
        try:
            evidence = tuple(source.find(claim, speech.date))
        except SourceUnavailable as e:
            return _unverifiable(claim, at, f"資料來源暫時無法取得：{e}"[:300])
        if not evidence:
            return _unverifiable(claim, at, "查無對應的法條或議案")

        # 只拿 quote 裡真的有的數字去比：模型給的數字可能是它自己補的
        figures = [f for f in claim.figures if is_grounded(f, claim.quote)]
        outcome = compare_numbers(figures, [e.excerpt for e in evidence],
                                  approximate=has_approximation(claim.quote))
        if outcome.verdict is not None:
            return CheckedClaim(claim, at, outcome.verdict, Method.NUMERIC,
                                outcome.rationale, evidence)
        return self._judge_text(claim, at, evidence)

    def _judge_text(self, claim: ExtractedClaim, at: float,
                    evidence: tuple[Evidence, ...]) -> CheckedClaim:
        try:
            judgement = self._judge.judge(claim.statement, evidence)
        except ModelOutputInvalid:
            return _unverifiable(claim, at, "模型的判讀無法解讀", evidence)
        if judgement.verdict == Verdict.UNVERIFIABLE:
            return CheckedClaim(claim, at, Verdict.UNVERIFIABLE, Method.MODEL,
                                judgement.reason or "證據不足以判斷", evidence)
        if not is_grounded(judgement.evidence_quote, "\n".join(e.excerpt for e in evidence)):
            return CheckedClaim(claim, at, Verdict.UNVERIFIABLE, Method.MODEL,
                                "模型引用的句子不在證據中，判讀作廢", evidence)
        return CheckedClaim(claim, at, judgement.verdict, Method.MODEL,
                            f"{judgement.reason}（依據：「{judgement.evidence_quote}」）",
                            evidence)
