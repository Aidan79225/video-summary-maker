"""查核編排：用假的抽取器、來源與判讀器，不碰網路也不碰模型。"""
from __future__ import annotations

from datetime import date

import pytest

from factcheck.domain.entities import (
    ClaimKind,
    Evidence,
    ExtractedClaim,
    Judgement,
    Method,
    SourceKind,
    Speech,
    Verdict,
)
from factcheck.domain.errors import (
    ModelOutputInvalid,
    ModelUnavailable,
    OperationCancelled,
    SourceUnavailable,
)
from factcheck.usecases.check import FactCheckUseCase

TRANSCRIPT = ("00:16 醫療暴力層出不窮\n"
              "00:32 他的行政罰鍰從現行的3萬到5萬提升到5到25萬\n"
              "01:04 希望能夠健全我們臺灣的醫療環境\n")
SPEECH = Speech("邱慧洳", date(2026, 8, 25), "第11屆第5會期第23次會議", TRANSCRIPT)
ARTICLE_106 = Evidence(SourceKind.LAW, "醫療法 第一百零六條", "https://law", "https://api",
                       "違反第二十四條第二項規定者，處新臺幣三萬元以上五萬元以下罰鍰。")


def _claim(quote="他的行政罰鍰從現行的3萬到5萬", figures=("3萬到5萬",),
           kind=ClaimKind.LAW_ARTICLE, statement="醫療法現行罰鍰為 3 萬到 5 萬元"):
    return ExtractedClaim(quote=quote, kind=kind, statement=statement, figures=figures,
                          law="醫療法", article="第24條")


class FakeExtractor:
    def __init__(self, claims):
        self.claims = claims

    def extract(self, speech):
        return list(self.claims)


class FakeSource:
    def __init__(self, evidence=(ARTICLE_106,), error=None):
        self.evidence = list(evidence)
        self.error = error
        self.calls = []

    def find(self, claim, on):
        self.calls.append((claim, on))
        if self.error is not None:
            raise self.error
        return list(self.evidence)


class FakeJudge:
    def __init__(self, judgement=None, error=None):
        self.judgement = judgement or Judgement(Verdict.SUPPORTED, "處新臺幣三萬元以上五萬元以下罰鍰", "條文相同")
        self.error = error
        self.calls = 0
        self.statements = []

    def judge(self, statement, evidence):
        self.calls += 1
        self.statements.append(statement)
        if self.error is not None:
            raise self.error
        return self.judgement


def _run(claims, source=None, judge=None, cancelled=lambda: False):
    usecase = FactCheckUseCase(FakeExtractor(claims),
                               {ClaimKind.LAW_ARTICLE: source or FakeSource()},
                               judge or FakeJudge())
    return usecase.execute(SPEECH, lambda fraction, status: None, cancelled)


def test_numbers_are_decided_by_code_not_the_model():
    judge = FakeJudge()
    [checked] = _run([_claim()], judge=judge)
    assert checked.verdict == Verdict.SUPPORTED
    assert checked.method == Method.NUMERIC
    assert checked.evidence == (ARTICLE_106,)
    assert judge.calls == 0


def test_the_timestamp_comes_from_the_transcript():
    [checked] = _run([_claim()])
    assert checked.timestamp == 32.0


def test_the_source_is_asked_about_the_speech_day():
    source = FakeSource()
    _run([_claim()], source=source)
    assert source.calls[0][1] == date(2026, 8, 25)


def test_a_quote_that_is_not_in_the_transcript_is_dropped():
    """攔的行為：模型改寫或捏造委員的話。那不是他說的，不能拿來查核他。"""
    assert _run([_claim(quote="現行罰鍰是三萬到五萬元")]) == []


def test_figures_that_are_not_in_the_quote_are_ignored():
    """模型給了 quote 裡沒有的數字：不能拿它去比，改走文字判讀。"""
    judge = FakeJudge()
    [checked] = _run([_claim(figures=("10萬",))], judge=judge)
    assert checked.method == Method.MODEL
    assert judge.calls == 1


def test_no_evidence_is_unverifiable():
    [checked] = _run([_claim()], source=FakeSource(evidence=()))
    assert checked.verdict == Verdict.UNVERIFIABLE
    assert checked.method == Method.NONE
    assert "查無" in checked.rationale


def test_an_unavailable_source_only_affects_that_claim():
    [checked] = _run([_claim()], source=FakeSource(error=SourceUnavailable("503")))
    assert checked.verdict == Verdict.UNVERIFIABLE
    assert "暫時無法取得" in checked.rationale


def test_a_kind_without_a_source_is_unverifiable():
    [checked] = _run([_claim(kind=ClaimKind.BILL_STATUS)])
    assert checked.verdict == Verdict.UNVERIFIABLE


def test_the_model_decides_when_there_are_no_numbers():
    [checked] = _run([_claim(figures=())])
    assert checked.verdict == Verdict.SUPPORTED
    assert checked.method == Method.MODEL
    assert "處新臺幣三萬元以上五萬元以下罰鍰" in checked.rationale


def test_a_model_quote_that_is_not_in_the_evidence_voids_the_judgement():
    """攔的行為：模型引用一句證據裡沒有的話來撐它的判斷。"""
    judge = FakeJudge(Judgement(Verdict.CONTRADICTED, "處新臺幣十萬元以上罰鍰", "金額不同"))
    [checked] = _run([_claim(figures=())], judge=judge)
    assert checked.verdict == Verdict.UNVERIFIABLE
    assert "作廢" in checked.rationale


def test_an_unreadable_judgement_is_unverifiable():
    [checked] = _run([_claim(figures=())], judge=FakeJudge(error=ModelOutputInvalid("x")))
    assert checked.verdict == Verdict.UNVERIFIABLE


def test_the_judge_sees_the_speech_date():
    """議案狀態是查詢當下的；判讀要知道發言日期，才分得出「當時」與「現在」。"""
    judge = FakeJudge()
    _run([_claim(figures=())], judge=judge)
    assert judge.statements == ["醫療法現行罰鍰為 3 萬到 5 萬元（發言日期：2026-08-25）"]


def test_an_unavailable_model_fails_the_whole_speech():
    """模型連不上是整篇做不下去，不能把每一則都記成無法查證。"""
    with pytest.raises(ModelUnavailable):
        _run([_claim(figures=())], judge=FakeJudge(error=ModelUnavailable("連不上 Ollama")))


def test_cancelling_stops_before_the_next_claim():
    with pytest.raises(OperationCancelled):
        _run([_claim()], cancelled=lambda: True)
