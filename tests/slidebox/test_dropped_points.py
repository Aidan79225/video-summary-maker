"""被檢查丟掉的點，留下紀錄（診斷用）：原因碼、原因、模型寫的 quote 與文字、它引用的 turn。

只記、不影響任何判斷：留下哪些點、丟掉幾個、原因碼的計數都和沒有這份紀錄時一樣。
這份紀錄是為了在歸屬不可靠而失敗時，讓人看得到每個被丟掉的點到底是怎麼回事
（模型引用的話對不上逐字稿的哪裡），不用再猜。
"""
from __future__ import annotations

from slidebox.domain.entities import RawPoint, RawSlide
from slidebox.usecases.conversation import (
    BAD_QUOTE,
    DUPLICATE_POINT,
    LEADING_PRONOUN_OR_TITLE,
    SLIDE_TOO_SMALL,
    TURN_NOT_INT,
    DroppedPoint,
    chunk_turns,
    validate_points,
)

from .conversation_support import GOOD, SPEAKERS, TURNS, at, problems_of, run

INVENTED = "完全不存在的一句話在逐字稿裡"


def test_a_dropped_point_is_recorded_with_what_the_model_wrote():
    cash = at("現金補助")
    made_up = RawPoint(cash, "現金補助有幫助", quote=INVENTED)
    pronoun = RawPoint(cash, "他認為補助應該加倍", quote="現金補助有幫助，但不能只靠現金")

    _, result = run(made_up, pronoun)

    assert [(d.code, d.turn, d.quote, d.text) for d in result.dropped] == [
        (BAD_QUOTE, cash, INVENTED, "現金補助有幫助"),
        (LEADING_PRONOUN_OR_TITLE, cash, "現金補助有幫助，但不能只靠現金", "他認為補助應該加倍"),
    ]
    assert all(isinstance(d, DroppedPoint) and d.message for d in result.dropped)


def test_the_recorded_reason_is_the_one_the_retry_hint_would_say():
    """診斷看到的原因和第二次問模型時附上的原因是同一句，不會一個說東、一個說西。"""
    _, result = run(RawPoint(at("現金補助"), "現金補助有幫助", quote=INVENTED))

    (dropped,) = result.dropped
    assert dropped.message in problems_of(result)


def test_points_that_pass_are_not_recorded():
    _, result = run()

    assert result.dropped == () and result.kept == 2


def test_dropped_points_come_in_the_order_the_model_wrote_them():
    cash = at("現金補助")
    points = [RawPoint(cash, f"現金補助有幫助{n}", quote=f"{INVENTED}{n}") for n in range(3)]

    _, result = run(*points)

    assert [d.text for d in result.dropped] == ["現金補助有幫助0", "現金補助有幫助1", "現金補助有幫助2"]


def test_a_duplicate_on_the_same_page_is_recorded_as_a_duplicate():
    _, result = run(GOOD[0])

    (dropped,) = result.dropped
    assert dropped.code == DUPLICATE_POINT and dropped.text == GOOD[0].text and dropped.quote == GOOD[0].quote


def test_a_turn_that_is_not_a_number_is_recorded_as_none():
    _, result = run(RawPoint("第三段", "現金補助有幫助", quote="現金補助有幫助，但不能只靠現金"))

    (dropped,) = result.dropped
    assert dropped.code == TURN_NOT_INT and dropped.turn is None


def test_a_flag_is_not_a_turn_number_either():
    _, result = run(RawPoint(True, "現金補助有幫助", quote="現金補助有幫助，但不能只靠現金"))

    assert result.dropped[0].turn is None


def test_a_quote_or_text_that_is_not_a_string_is_recorded_as_empty():
    """模型的回應解析出來的不一定是字串（null、數字）。run 會幫沒有 quote 的點補上整輪的話，所以直接呼叫。"""
    window = chunk_turns(TURNS, SPEAKERS)[0]
    slide = RawSlide("補助", (*GOOD, RawPoint(at("現金補助"), None, quote=None)))

    result = validate_points([slide], TURNS, SPEAKERS, window)

    (dropped,) = result.dropped
    assert dropped.quote == "" and dropped.text == ""


def test_the_record_is_exactly_the_points_dropped_one_by_one():
    """整頁因為太小被丟掉的點（它們自己沒問題）不在紀錄裡；其餘每個被丟掉的點都在，數量對得上原因碼的計數。"""
    window = chunk_turns(TURNS, SPEAKERS)[0]
    one_good = RawSlide("補助", (GOOD[0], RawPoint(at("現金補助"), "現金補助有幫助", quote=INVENTED)))
    two_dropped = RawSlide("托育", (RawPoint(at("現金補助"), "現金補助有幫助", quote=INVENTED),
                                   RawPoint(at("現金補助"), "他認為補助應該加倍")))

    result = validate_points([one_good, two_dropped], TURNS, SPEAKERS, window)

    counts = dict(result.dropped_by_code)
    assert counts[SLIDE_TOO_SMALL] == 1                              # 第一頁只剩 1 個點，整頁不用
    assert len(result.dropped) == sum(n for code, n in counts.items() if code != SLIDE_TOO_SMALL) == 3
    assert result.proposed - result.kept == sum(counts.values())      # 計數本身沒有因為多了紀錄而改變


def test_recording_does_not_change_which_points_are_kept():
    cash = at("現金補助")
    pronoun = RawPoint(cash, "他認為補助應該加倍", quote="現金補助有幫助，但不能只靠現金")

    extra, result = run(pronoun)

    assert extra == [] and result.kept == 2 and result.proposed == 3
    assert result.contradicted == 1 and result.unverifiable == 0
