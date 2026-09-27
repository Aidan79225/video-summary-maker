"""數字正規化：事實查核裡唯一由程式判對錯的部分，每條規則都要釘住。"""
from __future__ import annotations

import pytest

from factcheck.usecases.numbers import (
    Quantity,
    Unit,
    article_label,
    article_number,
    has_approximation,
    parse_chinese_number,
    quantities,
    to_chinese,
)

MONEY = Unit.MONEY


@pytest.mark.parametrize("text, expected", [
    ("三", 3), ("十五", 15), ("三十", 30), ("二十四", 24), ("一百零六", 106),
    ("一百一十", 110), ("二千一百", 2100), ("三十萬", 300_000),
    ("一萬五千", 15_000), ("二千一百億", 210_000_000_000),
])
def test_chinese_numbers(text, expected):
    assert parse_chinese_number(text) == expected


def test_non_numbers_are_rejected():
    assert parse_chinese_number("") is None
    assert parse_chinese_number("三讀") is None


def test_law_text_money_range():
    """醫療法第 106 條的原文。"""
    assert quantities("處新臺幣三萬元以上五萬元以下罰鍰") == [
        Quantity(30_000, MONEY), Quantity(50_000, MONEY)]


def test_spoken_money_with_shared_unit():
    """「5到25萬」的萬是兩個數字共用的——這是口語最常見的寫法。"""
    assert quantities("從現行的3萬到5萬提升到5到25萬") == [
        Quantity(30_000, MONEY), Quantity(50_000, MONEY),
        Quantity(50_000, MONEY), Quantity(250_000, MONEY)]


def test_years_and_money_together():
    assert quantities("6年2100億") == [
        Quantity(6, Unit.YEAR), Quantity(210_000_000_000, MONEY)]


def test_bill_text_money():
    assert quantities("本條例所需經費上限為新臺幣二千一百億元") == [
        Quantity(210_000_000_000, MONEY)]


def test_prison_terms_are_durations():
    assert quantities("處三年以下有期徒刑") == [Quantity(3, Unit.YEAR)]
    assert quantities("6個月以上5年以下的有期徒刑") == [
        Quantity(6, Unit.MONTH), Quantity(5, Unit.YEAR)]


def test_fiscal_years_are_not_durations():
    """「116 年度」是年度不是期間。收進來的話，證據裡的年度會讓「6 年」
    被判成對不上——一個假的「不符」。"""
    result = quantities("114 至 116 年度累計編列 82.4 億元")
    assert len(result) == 1
    assert result[0].unit == MONEY
    assert result[0].value == pytest.approx(8_240_000_000)


def test_counts_are_ignored():
    assert quantities("契約上總數是1,860 架") == []
    assert quantities("5萬人參加") == []


def test_article_numbers_are_not_quantities():
    assert quantities("違反第二十四條第二項規定者") == []


def test_words_that_look_like_numbers():
    assert quantities("千萬不要") == []


def test_full_width_digits():
    assert quantities("罰鍰３萬元") == [Quantity(30_000, MONEY)]


def test_approximation_words():
    assert has_approximation("大約2千億")
    assert has_approximation("逾3萬件")
    assert not has_approximation("罰鍰3萬到5萬")


@pytest.mark.parametrize("n, text", [
    (3, "三"), (10, "十"), (15, "十五"), (24, "二十四"), (100, "一百"),
    (106, "一百零六"), (110, "一百一十"), (124, "一百二十四"),
])
def test_to_chinese(n, text):
    assert to_chinese(n) == text


def test_article_label_matches_lyapi():
    assert article_label(106) == "第一百零六條"


@pytest.mark.parametrize("text, n", [
    ("第24條", 24), ("24條", 24), ("第二十四條", 24), ("醫療法第 106 條", 106),
])
def test_article_number(text, n):
    assert article_number(text) == n


def test_article_number_missing():
    assert article_number("") is None
    assert article_number("醫療法") is None
