"""重點裡的數字核對（V1b）：比數值與單位種類，不比字面。

數字的讀法用一個「反過來」的獨立寫法當標準答案（oracle）：先有整數，再寫成中文數字，讀回來必須相同。
"""
from __future__ import annotations

import random
from fractions import Fraction

import pytest

from slidebox.usecases.numerals import PERCENT, PLAIN, mentions, number_problem, value_of

DIGIT = "零一二三四五六七八九"


def below_10000(n: int, short_ten: bool = False) -> str:
    """0 < n < 10000 的標準中文寫法：一千零二、一千零二十、一百一十、十五（short_ten：開頭的「一十」寫成「十」）。"""
    out, started, pending_zero = "", False, False
    for digit, unit in zip((n // 1000, n // 100 % 10, n // 10 % 10, n % 10), ("千", "百", "十", "")):
        if digit == 0:
            pending_zero = pending_zero or started
            continue
        if pending_zero:
            out += "零"
            pending_zero = False
        out += "十" if (digit == 1 and unit == "十" and not started and short_ten) else DIGIT[digit] + unit
        started = True
    return out


def chinese(n: int, short_ten: bool = False) -> str:
    """0 < n < 10**12 的標準中文寫法（億、萬各自算一節；中間跳過的位數補一個「零」：一億零五千、一萬零三）。"""
    yi, rest = divmod(n, 10 ** 8)
    wan, low = divmod(rest, 10 ** 4)
    out = ""
    if yi:
        out += below_10000(yi, short_ten) + "億"
    if wan:
        out += ("零" if out and wan < 1000 else "") + below_10000(wan, short_ten and not out) + "萬"
    if low:
        skipped = low < 1000 or not wan          # 前面有位數被跳過：這一節不到一千，或整個「萬」節都是 0
        out += ("零" if out and skipped else "") + below_10000(low, short_ten and not out)
    return out


def random_number(rng: random.Random) -> int:
    return rng.choice([rng.randint(1, 99), rng.randint(100, 9999), rng.randint(10_000, 999_999),
                       rng.randint(10 ** 6, 10 ** 10)])


# --- 讀數字：中文、阿拉伯、混著寫，都換成精確的數值 ---


def test_the_oracle_itself_writes_the_numbers_it_should():
    assert [chinese(n) for n in (1, 10, 12, 100, 106, 1002, 1020, 10003, 100000005, 120000, 100005000, 102000000)] == [
        "一", "一十", "一十二", "一百", "一百零六", "一千零二", "一千零二十", "一萬零三", "一億零五", "一十二萬",
        "一億零五千", "一億零二百萬"]
    assert chinese(15, short_ten=True) == "十五" and chinese(110) == "一百一十"


@pytest.mark.parametrize("seed", range(40))
def test_the_numeral_parser_agrees_with_the_oracle(seed):
    rng = random.Random(seed)
    for _ in range(60):
        n = random_number(rng)
        spelled = chinese(n)
        assert value_of(spelled) == n, (n, spelled)
        assert value_of(chinese(n, short_ten=True)) == n, (n, chinese(n, short_ten=True))
        assert value_of(str(n)) == n and value_of(f"{n:,}") == n
        # 「二」開頭、後面接單位字的地方，口語寫「兩」：兩百、兩千、兩萬
        spoken = spelled.replace("二百", "兩百").replace("二千", "兩千").replace("二萬", "兩萬")
        assert value_of(spoken) == n, (n, spoken)


@pytest.mark.parametrize("seed", range(20))
def test_decimals_agree_with_the_oracle(seed):
    rng = random.Random(500 + seed)
    for _ in range(40):
        whole = rng.randint(0, 9999)
        digits = "".join(rng.choice("0123456789") for _ in range(rng.randint(1, 3)))
        expected = whole + Fraction(int(digits), 10 ** len(digits))
        spelled = (chinese(whole) if whole else "零") + "點" + "".join(DIGIT[int(d)] for d in digits)
        assert value_of(spelled) == expected, spelled
        assert value_of(f"{whole}.{digits}") == expected
        assert value_of(spelled + "萬") == expected * 10 ** 4, spelled


@pytest.mark.parametrize("text, expected", [
    ("三百五", 350), ("四萬二", 42000), ("一千二", 1200), ("一百零六", 106), ("一萬零三", 10003),
    ("兩百一十", 210), ("六點三萬", 63000), ("1.2萬", 12000), ("1,234", 1234), ("三點一四", Fraction(314, 100)),
    ("零", 0), ("〇", 0), ("１２３", 123), ("一億二千萬", 120_000_000), ("十五", 15), ("二十", 20),
])
def test_values_that_speakers_actually_say(text, expected):
    assert value_of(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "三點", "三點x", "第", "十二分"])
def test_a_text_that_is_not_a_number_has_no_value(text):
    assert value_of(text) is None


# --- 百分比與「成」 ---


def kinds(text):
    return [(m.kind, m.value, m.unit) for m in mentions(text)]


def test_percentages_are_read_as_percent_values_whatever_the_writing():
    assert kinds("百分之十二") == [(PERCENT, 12, "")]
    assert kinds("12%") == [(PERCENT, 12, "")]
    assert kinds("１２％") == [(PERCENT, 12, "")]
    assert kinds("三成") == [(PERCENT, 30, "")]
    assert kinds("四成二") == [(PERCENT, 42, "")]
    assert kinds("三成五") == [(PERCENT, 35, "")]
    assert kinds("五十五成") == [(PERCENT, 550, "")]          # 55 成 = 550%，不是 55%


def test_a_number_with_a_unit_is_a_plain_number_with_that_unit():
    assert kinds("十二分鐘") == [(PLAIN, 12, "分鐘")]
    assert kinds("兩百人") == [(PLAIN, 200, "人")]
    assert kinds("3,200萬元") == [(PLAIN, 32_000_000, "元")]
    assert kinds("三成五十人") == [(PERCENT, 30, ""), (PLAIN, 50, "人")]      # 「三成」後面接的「五十」是另一個數字


# --- 重點與那段話的數字要同值同種類 ---


@pytest.mark.parametrize("text, support", [
    ("借閱人次6.3萬", "這季借閱人次是六點三萬"),
    ("借閱人次六點三萬", "這季借閱人次是6.3萬"),
    ("九千一百份問卷", "問卷收了9100份"),
    ("三月二十八號上線", "3月28日上線"),
    ("增加四成二", "增加了42%"),
    ("增加42%", "增加了四成二"),
    ("營業額掉了百分之十二", "營業額掉了12%"),
    ("百分之八", "只占8%"),
    ("營業額掉了百分之十二", "營業額要掉十二"),                  # 語音辨識常把「百分之」漏掉：沒有單位的一般數字也算
    ("三百五十人", "大約三百五十個人"),
    ("每個月多編四十二萬", "每個月要多編四十萬，不對，是四十二萬"),
    ("第二個問題", "沒有任何數字"),                               # 沒有單位的中文數字不是數量
    ("一起處理", "沒有任何數字"),
    ("談少子化", "沒有任何數字"),
])
def test_the_same_number_in_another_writing_is_fine(text, support):
    assert number_problem(text, support) is None


@pytest.mark.parametrize("text, support", [
    ("預算大約要五千億", "預算大約要三千億"),
    ("比去年多了三成", "比去年多了兩成"),
    ("八十二人", "八十三人"),
    ("三月上線", "四月上線"),
    ("增加50%", "增加了40%"),
    ("增加五成", "增加了四成"),
    ("約六成", "3.2億是1.2億的兩倍多"),                            # 計算出來的數字，說話的人沒有說
    ("二百五十五元", "一個85元，買三個"),
    ("增加四成二", "增加了四成三"),
    ("三千五百元", "三千元"),
])
def test_a_number_the_turn_does_not_have_is_rejected(text, support):
    assert number_problem(text, support) is not None


@pytest.mark.parametrize("text, support, word", [
    ("可縮短十二分鐘車程", "旅行時間可以減少百分之十二", "分鐘"),         # 百分之十二 → 十二分鐘
    ("缺口達五十五成", "缺口比例是55%", "五十五成"),                     # 55% → 五十五成（= 550%）
    ("九十一成的人同意", "百分之九十一的人同意", "九十一成"),             # 百分之九十一 → 九十一成
    ("不足七成", "完成率是72%", "七成"),                                # 72% → 不足七成
    ("增加了十二人", "增加了12%", "人"),
])
def test_a_percentage_rewritten_as_another_kind_of_number_is_rejected(text, support, word):
    problem = number_problem(text, support)

    assert problem is not None and word in problem


def test_a_unit_rewrite_is_named_when_the_value_is_there_only_as_a_percentage():
    assert "是百分比" in number_problem("可縮短十二分鐘車程", "旅行時間可以減少百分之十二")


def test_a_percentage_is_fine_when_the_turn_has_the_value_as_a_plain_number_too():
    assert number_problem("可縮短十二分鐘車程", "旅行時間可以減少百分之十二，也就是十二分鐘") is None


def test_a_turn_that_states_the_same_number_literally_is_always_fine():
    assert number_problem("簽了3100份", "我們簽了3100份") is None
    assert number_problem("共2,400人", "一共 2400 人") is None


@pytest.mark.parametrize("text, support", [
    # 實際出現過：模型把「三十分鐘」寫成「三分鐘」。「三」只是「三十」的一部分，不是照抄。
    ("預警系統平均可提前三分鐘", "平均提前三十分鐘，但是這三十分鐘是全市平均，低窪區實際只有十二分鐘"),
    ("提前3分鐘", "平均提前30分鐘"),
    ("有12座抽水站", "有112座抽水站"),
    ("需要五人", "需要五十人"),
    ("三百人參加", "三百五十人參加"),
    ("三千元", "三千五百元"),
    ("下陷28公分", "每年下陷2.8公分"),               # 小數點被吃掉：28 不是 2.8
    ("下陷2.8公分", "每年下陷28公分"),
    ("下陷8公分", "每年下陷2.8公分"),                 # 8 只是 2.8 的尾巴
    ("共24人", "一共 2,400 人"),                       # 24 只是 2,400 的開頭
])
def test_a_number_that_is_only_a_piece_of_a_longer_number_in_the_turn_is_rejected(text, support):
    assert number_problem(text, support) is not None


@pytest.mark.parametrize("text, support", [
    ("他們願意投2000萬", "他們願意投2000 萬 估值1.2億"),     # 語音辨識把數字和單位拆開
    ("估值1.2億", "他們願意投 2000萬，估值1.2 億"),
    ("共2,400人", "一共 2,400 人"),
    ("借了三、四本", "借了三、四本"),                         # 頓號隔開的是兩個數字，各自照抄
    ("下陷2.8公分", "平均每年下陷2.8公分，比海平面上升快"),
    ("三百五十人", "有三百五十人，其中三十人是學生"),
    ("提前三十分鐘", "平均提前三十分鐘，但是這三十分鐘是全市平均"),
])
def test_a_whole_number_copied_from_the_turn_is_fine_even_when_speech_recognition_spaced_it(text, support):
    assert number_problem(text, support) is None


@pytest.mark.parametrize("text, support", [
    # 實際出現過：模型在阿拉伯數字和中文單位之間加空白（62 萬、7 個月、2000 萬、1.2 億），逐字稿沒有。
    ("每月燒掉 62 萬，現金流僅剩 7 個月", "burn rate目前一個月62萬 runway只剩7個月"),
    ("願意投資 2000 萬，公司估值為 1.2 億", "他們願意投2000萬 估值1.2億"),
    ("願意投資2000萬，估值1.2億", "他們願意投 2000 萬 估值 1.2 億"),
    ("成長百分之 十二", "成長了12%"),
])
def test_a_space_between_a_digit_and_its_chinese_unit_does_not_change_the_number(text, support):
    assert number_problem(text, support) is None


@pytest.mark.parametrize("text, support", [
    ("每月燒掉 6 萬", "burn rate目前一個月62萬"),
    ("現金流僅剩 7 個月", "runway只剩70個月"),
    ("估值 1.2 億", "估值12億"),
])
def test_a_spaced_number_is_still_checked_against_the_turn(text, support):
    assert number_problem(text, support) is not None


def test_a_digit_and_its_unit_with_a_space_between_are_read_as_one_number():
    assert [m.value for m in mentions("每月 62 萬，7 個月")] == [620000, 7]
    assert [(m.value, m.unit) for m in mentions("共 120 人")] == [(120, "人")]


def test_a_rounded_figure_is_a_known_and_deliberate_false_positive():
    """「約三成」對上「31.5%」會被丟掉：寧可丟掉，不猜差不多算不算同一個數字。"""
    assert number_problem("約三成", "占了31.5%") is not None


def test_every_number_in_the_point_needs_its_own_support():
    assert number_problem("三千億，比去年多了兩成", "三千億，比去年多了兩成") is None
    assert number_problem("三千億，比去年多了兩成", "三千億，比去年多了三成") is not None
    assert number_problem("三千億，比去年多了兩成", "比去年多了兩成") is not None


def test_the_reason_is_readable_and_names_the_number():
    assert "數字「五千億」" in number_problem("預算五千億", "預算三千億")
    assert "百分比" in number_problem("增加50%", "增加了40%")


@pytest.mark.parametrize("text, support", [
    ("先試辦3個月", "先試辦六個月"),               # 阿拉伯數字一定核對
    ("開了十二小時的會", "開了兩小時的會"),          # 帶「十百千萬億」的中文數字一定核對
    ("每月三日繳款", "每月五日繳款"),              # 單獨一個中文數字，後面接 年月日號… 這些單位字的，也核對
    ("每次三天", "每次五天"),
])
def test_a_chinese_numeral_is_checked_when_it_carries_a_magnitude_or_a_unit_character(text, support):
    assert number_problem(text, support) is not None


@pytest.mark.parametrize("text, support", [
    ("先試辦三個月", "先試辦六個月"),
    ("發出兩份通知", "發出五份通知"),
    ("新建兩座橋", "新建四座橋"),
])
def test_known_gap_a_lone_chinese_numeral_before_a_measure_word_is_not_checked(text, support):
    """已知的缺口，不是想要的行為：單獨一個中文數字後面接個月、份、座、張、間，不在核對的範圍。

    實測補上之後，1100 多個重點裡沒有多擋到任何一個真的錯，卻誤丟了「六個半小時 = 六點五小時」，
    所以先維持原樣（見 numerals._UNIT_CHARS_AFTER 的註解）；要補，得先讓 value_of 讀得懂「N個半」。
    補的人改這個測試即可。
    """
    assert number_problem(text, support) is None


def test_a_generic_measure_word_is_never_a_quantity():
    assert number_problem("提出三個問題", "他問了很多問題") is None
    assert number_problem("第一個議程", "討論議程") is None


# --- 性質 ---


@pytest.mark.parametrize("seed", range(30))
def test_a_text_is_always_consistent_with_itself_and_with_any_spelling_of_its_numbers(seed):
    rng = random.Random(900 + seed)
    for _ in range(40):
        n = random_number(rng)
        unit = rng.choice(["元", "人", "分鐘", "份", "個", "年"])
        percent = rng.randint(1, 99)
        spellings = [f"{n}{unit}", f"{chinese(n)}{unit}", f"{n:,}{unit}"]
        text = f"預算{rng.choice(spellings)}，比去年多了百分之{chinese(percent, short_ten=True)}"
        same_in_other_words = f"預算{rng.choice(spellings)}，比去年多了{percent}%"

        assert number_problem(text, text) is None, text
        assert number_problem(text, same_in_other_words) is None, (text, same_in_other_words)
        assert number_problem(same_in_other_words, text) is None, (text, same_in_other_words)


@pytest.mark.parametrize("seed", range(30))
def test_a_different_value_or_a_different_kind_is_never_accepted(seed):
    rng = random.Random(1300 + seed)
    for _ in range(40):
        n = random_number(rng)
        other = n + rng.randint(1, 50)
        unit = rng.choice(["元", "人", "分鐘", "年", "天"])      # 後面接這些，連單獨一個中文數字也核對
        support = f"合計{chinese(n)}{unit}"

        assert number_problem(f"合計{other}{unit}", support) is not None, (n, other)
        assert number_problem(f"合計{chinese(other)}{unit}", support) is not None, (n, other)
        # 同一個數值，只是種類從「帶單位的數量」變成百分比：也不行
        assert number_problem(f"合計{n}%", support) is not None, n
        assert number_problem(f"合計百分之{chinese(n)}", f"合計{n}{unit}") is not None, n


# --- 每個單位字、每種寫法都有人釘住（mutation testing 找出原本沒人看的地方）---

# 這兩份清單故意在測試裡再寫一次：從 numerals 匯入的話，被改掉的清單會連測試的範圍一起縮小，什麼都抓不到
UNIT_CHARS_AFTER = "年月日號歲週周倍元塊點分鐘秒時天度里斤人次戶家件場屆期趟折"       # 「成」是成數，另外處理
ABSOLUTE_UNITS = ["分鐘", "小時", "公里", "公尺", "公頃", "公分", "個月", "人次",
                  "天", "年", "週", "周", "元", "人", "戶", "座", "間", "棟", "輛", "份", "張", "次"]


@pytest.mark.parametrize("unit", list(UNIT_CHARS_AFTER))
def test_a_lone_chinese_numeral_before_each_of_these_characters_is_checked(unit):
    assert number_problem(f"約三{unit}", f"約三{unit}") is None
    assert number_problem(f"約三{unit}", f"約五{unit}") is not None


@pytest.mark.parametrize("unit", ABSOLUTE_UNITS)
def test_every_entity_unit_tells_a_quantity_from_a_percentage(unit):
    """數字後面跟著實體單位，那段話裡同一個數值只以百分比出現，就是單位被改掉；
    反過來，帶單位的數字不能拿來撐「百分之」（語音辨識漏掉百分之的，只會是沒單位的數字）。"""
    changed = number_problem(f"預計十二{unit}", "預計百分之十二")

    assert changed is not None and f"十二{unit}" in changed and "是百分比" in changed
    assert number_problem("預計百分之十二", f"預計十二{unit}") is not None


@pytest.mark.parametrize("magnitude", list("十百千萬億"))
def test_a_chinese_numeral_with_a_magnitude_character_is_checked_even_without_a_unit(magnitude):
    assert number_problem(f"預計三{magnitude}", f"預計三{magnitude}") is None
    assert number_problem(f"預計三{magnitude}", f"預計五{magnitude}") is not None


@pytest.mark.parametrize("magnitude, value", [("十", 50), ("百", 500), ("千", 5000), ("萬", 50_000), ("億", 500_000_000)])
def test_a_magnitude_character_after_the_tenths_digit_starts_another_number(magnitude, value):
    """「三成五」的五是成數的下一位；「三成五千」的五千是另一個數字。"""
    assert kinds(f"三成五{magnitude}") == [(PERCENT, 30, ""), (PLAIN, value, "")]


@pytest.mark.parametrize("text, expected", [
    ("二〇二五", 2025), ("一二三", 123), ("一〇五", 105), ("零五", 5),                  # 一個字一個字唸的數字（年份、編號）
    ("十", 10), ("百", 100), ("千", 1000), ("萬", 10_000), ("億", 100_000_000),         # 單獨一個單位字就是那個單位
    ("三百九", 390), ("三千五百", 3500), ("3萬2千", 32_000), ("2千5", 2500), ("2千10", 2010),
    ("１２．５", Fraction(25, 2)),                                                    # 全形的數字與小數點
    ("點五", Fraction(1, 2)),                                                         # 沒有整數部分的口語寫法
    ("三點五2", Fraction(7, 2)),                                                      # 小數後面的阿拉伯數字不是單位
])
def test_more_ways_numbers_are_written(text, expected):
    assert value_of(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("零三百五", 350), ("零百五", 5), ("1二", 2), ("二1三", 3),
])
def test_combinations_nobody_writes_are_read_the_same_way_every_time(text, expected):
    """開頭的零、阿拉伯數字緊接中文數字：沒有人會這樣寫，不是想要的行為，只是把現在的讀法釘住；
    改 _int_value 讀法的人，改這裡的期望值即可。"""
    assert value_of(text) == expected


def test_a_year_written_digit_by_digit_is_the_same_year_as_the_arabic_one():
    assert kinds("二〇二五年") == [(PLAIN, 2025, "年")]
    assert number_problem("二〇二五年上路", "2025年上路") is None
    assert number_problem("二〇二六年上路", "2025年上路") is not None


def test_the_tenths_digit_is_read_whatever_follows_it():
    assert kinds("四成二，其餘") == [(PERCENT, 42, "")]
    assert kinds("3成5") == [(PERCENT, 35, "")]
    assert kinds("7成5的人") == [(PERCENT, 75, "")]


def test_the_reason_quotes_exactly_the_percentage_the_point_wrote():
    problem = number_problem("增加了四成二，其餘照舊", "增加了四成三，其餘照舊")

    assert problem == "數字「四成二」不在被引用的那段話裡（百分比的數值對不上；數字與單位請照抄）"
