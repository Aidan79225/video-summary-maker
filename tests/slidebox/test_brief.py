"""質詢卡的純邏輯：數字落地、引用落地、截量。"""
from __future__ import annotations

from slidebox.domain.entities import Ask, Brief, KeyNumber, Slide
from slidebox.usecases.brief import (
    MAX_ASKS,
    MAX_KEY_NUMBERS,
    ground_brief,
    number_in_text,
    parse_chinese_number,
    quote_in_transcript,
    slides_digest,
    validate_brief,
)

TRANSCRIPT = (
    "00:12 從一百一十四年度到一百一十六年度 國防部累計編列八十二點四億元\n"
    "00:20 依契約應完成交機一千八百六十架 實際點收的只有 872 架\n"
    "00:31 認定成立的比率只有百分之十八左右\n"
)


def _number(value="82.4", unit="億元", label="三年累計編列",
            quote="國防部累計編列八十二點四億元", law="", article="") -> KeyNumber:
    return KeyNumber(value=value, unit=unit, label=label, quote=quote, law=law, article=article)


# --- 中文數字 ---


def test_parses_plain_chinese_integers():
    assert parse_chinese_number("二千一百") == 2100
    assert parse_chinese_number("一百零六") == 106
    assert parse_chinese_number("十五") == 15
    assert parse_chinese_number("一萬五千") == 15000
    assert parse_chinese_number("兩百一十四") == 214


def test_parses_decimals_before_a_big_unit():
    """「八十二點四億」是逐字稿最常見的寫法，小數在萬／億之前。"""
    assert abs(parse_chinese_number("八十二點四億") - 8.24e9) < 1
    assert parse_chinese_number("零點八七") == 0.87


def test_words_that_merely_contain_number_characters_are_not_numbers():
    assert parse_chinese_number("千萬") is None
    assert parse_chinese_number("") is None


# --- 數字在句子裡 ---


def test_arabic_value_matches_chinese_spelling_in_the_quote():
    assert number_in_text("82.4", "億元", "累計編列八十二點四億元")
    assert number_in_text("1860", "架", "應完成交機一千八百六十架")
    assert number_in_text("18", "%", "成立的比率只有百分之十八左右")


def test_arabic_value_matches_arabic_spelling_with_commas_and_spaces():
    assert number_in_text("1,860", "架", "應交 1,860 架")
    assert number_in_text("82.4", "億元", "編列 82.4 億元")


def test_a_value_the_quote_never_states_is_rejected():
    """攔的 bug：模型把「相差二十個百分點」腦補成 38% 與 61%。"""
    assert not number_in_text("38", "%", "相差二十個百分點以上")
    assert not number_in_text("", "架", "應交 1,860 架")
    assert not number_in_text("很多", "架", "應交 1,860 架")


# --- 引用在逐字稿裡 ---


def test_quote_matching_ignores_whitespace_punctuation_and_time_marks():
    assert quote_in_transcript("國防部累計編列八十二點四億元，", TRANSCRIPT)
    assert quote_in_transcript("實際點收的只有872架", TRANSCRIPT)


def test_a_rewritten_quote_is_not_in_the_transcript():
    assert not quote_in_transcript("國防部三年編列 82.4 億元", TRANSCRIPT)
    assert not quote_in_transcript("", TRANSCRIPT)


# --- ground_brief ---


def test_grounded_numbers_survive_and_fabricated_ones_are_dropped():
    brief = Brief("一句話", key_numbers=(
        _number(),
        _number(value="872", unit="架", label="實際點收", quote="實際點收的只有 872 架"),
        # 句子在逐字稿裡，但數字不在句子裡
        _number(value="47", unit="%", label="交機比例", quote="實際點收的只有 872 架"),
        # 數字在句子裡，但句子不在逐字稿裡
        _number(value="214", unit="架", label="待驗", quote="另有兩百一十四架待驗"),
    ))
    out = ground_brief(brief, TRANSCRIPT)
    assert [n.label for n in out.key_numbers] == ["三年累計編列", "實際點收"]


def test_numbers_are_capped_at_what_the_card_can_hold():
    many = tuple(_number(label=f"第 {i} 個") for i in range(MAX_KEY_NUMBERS + 3))
    out = ground_brief(Brief("一句話", key_numbers=many), TRANSCRIPT)
    assert len(out.key_numbers) == MAX_KEY_NUMBERS


def test_empty_asks_are_dropped_and_the_rest_are_capped():
    asks = (Ask("  "), Ask("提出清冊", "一個月內", "部長允諾")) + tuple(
        Ask(f"要求 {i}") for i in range(MAX_ASKS + 2))
    out = ground_brief(Brief("一句話", asks=asks), TRANSCRIPT)
    assert out.asks[0] == Ask("提出清冊", "一個月內", "部長允諾")
    assert len(out.asks) == MAX_ASKS


def test_fields_are_stripped():
    out = ground_brief(Brief("  一句話  ", key_numbers=(
        _number(value=" 82.4 ", unit=" 億元 ", label=" 三年 "),)), TRANSCRIPT)
    assert out.one_liner == "一句話"
    assert out.key_numbers[0].value == "82.4"
    assert out.key_numbers[0].unit == "億元"


def test_a_number_without_a_label_is_useless_on_the_card():
    out = ground_brief(Brief("一句話", key_numbers=(_number(label=" "),)), TRANSCRIPT)
    assert out.key_numbers == ()


# --- validate_brief ---


def test_an_empty_one_liner_fails_validation():
    assert validate_brief(Brief("")) != []
    assert validate_brief(Brief("就這樣")) != []
    assert validate_brief(Brief("國防部三年編 82.4 億買無人機，交到部隊的不到一半")) == []


# --- slides_digest ---


def test_digest_carries_title_bullets_and_detail_in_order():
    slides = (
        Slide(1, "第一段", ("重點一", "重點二"), 0.0, detail="完整敘述一"),
        Slide(2, "第二段", ("重點三",), 30.0),
    )
    digest = slides_digest(slides)
    assert digest.index("第一段") < digest.index("重點一") < digest.index("完整敘述一")
    assert digest.index("完整敘述一") < digest.index("第二段") < digest.index("重點三")


# --- 法條參照 ---


def test_article_numbers_are_read_from_every_common_spelling():
    from slidebox.usecases.brief import article_number, to_chinese
    assert article_number("第24條") == 24
    assert article_number("24條") == 24
    assert article_number("第一百零六條") == 106
    assert article_number("") is None
    assert article_number("第0條") is None
    assert to_chinese(106) == "一百零六"
    assert to_chinese(110) == "一百一十"
    assert to_chinese(24) == "二十四"
    assert to_chinese(10) == "十"


def test_a_law_reference_survives_only_when_both_name_and_article_are_in_the_transcript():
    from slidebox.usecases.brief import article_in_text
    transcript = "00:32 臺灣民眾黨對醫療法24條跟106條提出修正條文草案 他的行政罰鍰從現行的3萬到5萬"
    assert article_in_text(24, transcript)
    assert article_in_text(106, transcript)
    assert not article_in_text(50, transcript)
    number = _number(value="3", unit="萬", label="現行罰鍰下限",
                     quote="他的行政罰鍰從現行的3萬到5萬", law="醫療法", article="第106條")
    out = ground_brief(Brief("一句話", key_numbers=(number,)), transcript)
    assert out.key_numbers[0].law == "醫療法"
    assert out.key_numbers[0].article == "第106條"


def test_a_law_reference_the_transcript_never_mentions_is_cleared_not_kept():
    """附錯法條比沒附更糟：名稱或條號有一邊對不上，兩個一起清掉，數字本身留著。"""
    transcript = "00:32 他的行政罰鍰從現行的3萬到5萬"
    number = _number(value="3", unit="萬", label="現行罰鍰下限",
                     quote="他的行政罰鍰從現行的3萬到5萬", law="醫療法", article="第106條")
    out = ground_brief(Brief("一句話", key_numbers=(number,)), transcript)
    assert out.key_numbers[0].value == "3"
    assert out.key_numbers[0].law == ""
    assert out.key_numbers[0].article == ""
    # 名稱在、條號不在
    number = _number(value="3", unit="萬", label="現行罰鍰下限",
                     quote="他的行政罰鍰從現行的3萬到5萬", law="醫療法", article="第106條")
    out = ground_brief(Brief("一句話", key_numbers=(number,)), "00:30 醫療法 " + transcript)
    assert out.key_numbers[0].law == ""


def test_article_numbers_are_normalised_to_arabic():
    transcript = "00:32 醫療法第一百零六條 罰鍰從現行的3萬到5萬"
    number = _number(value="3", unit="萬", label="現行罰鍰下限",
                     quote="罰鍰從現行的3萬到5萬", law="醫療法", article="第一百零六條")
    out = ground_brief(Brief("一句話", key_numbers=(number,)), transcript)
    assert out.key_numbers[0].article == "第106條"
