"""數字比對：判定規則。"""
from __future__ import annotations

from factcheck.domain.entities import Verdict
from factcheck.usecases.compare import compare_numbers

ARTICLE_106 = ("違反第二十四條第二項規定者，處新臺幣三萬元以上五萬元以下罰鍰。"
               "對於醫事人員以強暴、脅迫、恐嚇或其他非法之方法，妨害其執行醫療或救護業務者，"
               "處三年以下有期徒刑，得併科新臺幣三十萬元以下罰金。")


def test_every_number_found_is_supported():
    outcome = compare_numbers(["3萬到5萬"], [ARTICLE_106])
    assert outcome.verdict == Verdict.SUPPORTED
    assert "30,000 元" in outcome.rationale


def test_some_numbers_found_is_partial():
    outcome = compare_numbers(["3萬到10萬"], [ARTICLE_106])
    assert outcome.verdict == Verdict.PARTIAL
    assert "100,000 元" in outcome.rationale


def test_no_number_found_is_contradicted():
    outcome = compare_numbers(["1萬到2萬"], [ARTICLE_106])
    assert outcome.verdict == Verdict.CONTRADICTED


def test_durations_compare_with_durations():
    assert compare_numbers(["3年以下"], [ARTICLE_106]).verdict == Verdict.SUPPORTED


def test_no_figures_means_numbers_cannot_decide():
    assert compare_numbers([], [ARTICLE_106]).verdict is None


def test_evidence_without_the_same_kind_of_number_cannot_decide():
    """證據裡沒有金額時，「對不上」不代表說錯，只代表這份證據比不了。"""
    outcome = compare_numbers(["3萬"], ["議案狀態：三讀"])
    assert outcome.verdict is None


def test_units_that_cannot_be_checked_are_reported_not_counted():
    """「6年2100億」對上只寫了金額的條文：金額對得上，年限沒得比。"""
    outcome = compare_numbers(["6年2100億"], ["本條例所需經費上限為新臺幣二千一百億元"])
    assert outcome.verdict == Verdict.SUPPORTED
    assert "6 年" in outcome.rationale


def test_approximate_claims_allow_ten_percent():
    evidence = ["總額為新臺幣二千四百億元"]
    assert compare_numbers(["2300億"], evidence).verdict == Verdict.CONTRADICTED
    assert compare_numbers(["2300億"], evidence, approximate=True).verdict == Verdict.SUPPORTED


def test_duplicated_figures_count_once():
    outcome = compare_numbers(["3萬", "3萬"], [ARTICLE_106])
    assert outcome.verdict == Verdict.SUPPORTED
