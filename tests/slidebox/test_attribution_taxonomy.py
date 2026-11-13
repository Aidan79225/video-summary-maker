"""已知的失敗模式（FINAL 的 error taxonomy #1–#19）各一個小例子：每一種錯，程式現在怎麼處理。

對談與點都是合成的短例（不是真實逐字稿）；每個測試只講一件事，並寫明是第幾列。
「已知的限制」（#9、#15）沒有任何檢查擋得住，測試只是把現況寫下來：以後補了檢查，改這兩個測試即可。
"""
from __future__ import annotations

import pytest

from slidebox.domain.entities import RawPoint, RawSlide
from slidebox.usecases.conversation import MAX_BULLETS, chunk_turns, validate_points

from .conversation_support import problems_of, run, with_anchors
from .speaker_fixtures import make_speakers, make_turns

TWO = ("participant", "participant")


def judge(lines, *points, kinds=TWO, quote_only=False):
    """lines 後面接兩段毫不相干的話與兩個一定通過的點，讓放進去的 points 那一頁一定留得住。

    回傳（points 之外留下來的點, 整個結果）。
    """
    turns, good = with_anchors(*lines)
    return run(*points, turns=turns, speakers=make_speakers(*kinds), good=good, quote_only=quote_only)


def codes(result) -> dict[str, int]:
    return dict(result.dropped_by_code)


# --- 1：數字的寫法不同（原本會被字面比對誤丟）---


def test_01_the_same_numbers_written_another_way_are_kept():
    lines = [("S1", "請問這次問卷的回收情況？"), ("S2", "我們簽了9100份問卷，三月二十八號開始統計，回收率很高。")]
    point = RawPoint(1, "回收九千一百份問卷，3月28日開始統計", quote="我們簽了9100份問卷，三月二十八號開始統計")

    kept, result = judge(lines, point)

    assert [p.text for p in kept] == ["回收九千一百份問卷，3月28日開始統計"] and not result.problems


# --- 2、3、11、19：重點的內容出自別輪（模型把甲的話抄進 quote、卻把乙說的事寫進重點）---


def test_02_one_speakers_figure_merged_with_another_speakers_rebuttal_is_dropped():
    lines = [("S1", "今天先請兩位發言。"), ("S2", "我們這次回收兩百一十份問卷，樣本來自各縣市。"),
             ("S1", "這樣的樣本太少，不能拿來當政策依據，也不具代表性。")]
    point = RawPoint(1, "兩百一十份問卷的樣本不能拿來當政策依據", quote="我們這次回收兩百一十份問卷，樣本來自各縣市")

    kept, result = judge(lines, point)

    assert kept == [] and codes(result) == {"anchor_conflict": 1} and result.anchor_conflicts == 1
    assert "出自別輪發言（T2）" in problems_of(result) and "與 quote 所在的 T1 不一致" in problems_of(result)


def test_03_a_merge_of_two_adjacent_turns_of_different_speakers_is_dropped():
    lines = [("S1", "目前的預算只編了三個月。"), ("S2", "我同意，但是期程要拉長到半年，才看得出成效。")]
    point = RawPoint(0, "預算只編三個月，但期程要拉長到半年", quote="目前的預算只編了三個月")

    kept, result = judge(lines, point)

    assert kept == [] and codes(result) == {"anchor_conflict": 1}


def test_11_a_point_is_never_moved_to_the_turn_that_echoes_the_quote():
    """甲說的話被乙複述並加了新的內容：重點的內容對著乙的那一輪，quote 卻在甲的那一輪，不能改歸給任何一方。"""
    lines = [("S2", "補助應該分三年編列，每年編一千萬。"),
             ("S1", "所以您的意思是說，補助應該分三年編列，每年編一千萬，而且要先編第一年的預算，是嗎？")]
    point = RawPoint(0, "補助分三年編列，每年編一千萬，先編第一年的預算", quote="補助應該分三年編列，每年編一千萬")

    kept, result = judge(lines, point)

    assert kept == [] and codes(result) == {"anchor_conflict": 1}


def test_19_a_merge_across_a_one_word_interjection_by_the_same_speaker_is_dropped_too():
    """同一個人隔著別人一句「嗯」說的兩段話併成一點：歸屬沒錯，但程式分不出來這種和真的併錯人，一律丟（成本約 1%）。"""
    lines = [("S2", "第一個問題是預算，今年只編了三千萬。"), ("S1", "嗯，了解。"),
             ("S2", "第二個問題是期程，要拉長到兩年才做得完。")]
    point = RawPoint(0, "預算只編三千萬，期程要拉長到兩年", quote="第一個問題是預算，今年只編了三千萬")

    kept, result = judge(lines, point)

    assert kept == [] and codes(result) == {"anchor_conflict": 1}


# --- 4、5：文字裡的名字、代名詞、第一人稱、角色字眼 ---


def test_04_names_nicknames_and_titles_copied_from_the_transcript_are_dropped_wherever_they_stand():
    lines = [("S1", "小安，你覺得這次的調整會不會影響營運？"),
             ("S2", "會有影響，營運虧損已經連續兩季，卞教授也提醒過這件事。")]
    nickname = RawPoint(1, "小安要先評估營運影響", quote="會有影響，營運虧損已經連續兩季")
    title = RawPoint(1, "營運虧損已經連續兩季，卞教授也提醒過", quote="會有影響，營運虧損已經連續兩季")

    kept, result = judge(lines, nickname, title)

    assert kept == [] and codes(result) == {"name_in_text": 2}
    assert "「小安」" in problems_of(result) and "「卞教授」" in problems_of(result)


def test_04_a_name_inside_a_verified_quotation_is_allowed():
    lines = [("S1", "小安，你覺得這次的調整會不會影響營運？"),
             ("S2", "會有影響，營運虧損已經連續兩季，卞教授也提醒過這件事。")]
    point = RawPoint(1, "營運虧損連續兩季，並提到「卞教授也提醒過這件事」", quote="營運虧損已經連續兩季，卞教授也提醒過這件事")

    kept, result = judge(lines, point)

    assert [p.speaker for p in kept] == ["S2"] and not result.dropped_by_code


@pytest.mark.parametrize("text, code", [
    ("嘗試說服對方降級", "pronoun_in_text"),
    ("資金壓在他們身上", "pronoun_in_text"),
    ("我們要先降級再評估", "first_person"),
    ("主持人追問資金來源", "label_word"),
    ("S2 主張先降級", "marker_in_text"),
])
def test_05_pronouns_first_person_and_role_words_anywhere_in_the_text_are_dropped(text, code):
    lines = [("S1", "先降級再評估，資金來源要說清楚。"), ("S2", "資金壓在雙方身上，嘗試說服降級並不容易。")]

    kept, result = judge(lines, RawPoint(0, text, quote="先降級再評估，資金來源要說清楚"))

    assert kept == [] and codes(result) == {code: 1}


# --- 6、7、8：quote 本身有問題 ---


def test_06_a_quote_stitched_across_two_turns_is_dropped():
    lines = [("S1", "貨車可以夜間配送。"), ("S2", "對，晚上十點到早上六點配送。")]
    point = RawPoint(0, "貨車可以夜間配送，時段是晚上十點到早上六點", quote="貨車可以夜間配送。對，晚上十點到早上六點配送")

    kept, result = judge(lines, point)

    assert kept == [] and codes(result) == {"bad_quote": 1}
    assert "把兩輪接在一起" in problems_of(result)


def test_07_a_quote_that_skips_a_clause_is_fine_for_provenance_but_never_shown_as_words():
    lines = [("S1", "剩下十座到今天還沒有發包，我們前面講過了，其中有四座是去年就應該要發包的。"), ("S2", "好的。")]
    skipping = "剩下十座到今天還沒有發包 其中有四座是去年就應該要發包的"
    faithful = RawPoint(0, "剩下十座還沒有發包，其中四座去年就應該發包", quote=skipping)
    unrelated = RawPoint(0, "標案執行進度嚴重落後", quote=skipping)

    kept, result = judge(lines, faithful, unrelated)

    assert [p.text for p in kept] == ["剩下十座還沒有發包，其中四座去年就應該發包"]          # 通過檢查的改寫：照寫
    assert codes(result) == {"unsupported": 1} and result.rescued == 0
    assert "也不能改用原話" in problems_of(result) and "連續的一段原話" in problems_of(result)


def test_08_a_quote_with_changed_or_invented_words_is_dropped():
    lines = [("S1", "沿線大概要拆掉十二戶房子，補償會依法辦理。"), ("S2", "好的。")]
    point = RawPoint(0, "沿線預計拆除十七戶房子", quote="沿線我們大概要拆掉十七戶房子")

    kept, result = judge(lines, point)

    assert kept == [] and codes(result) == {"bad_quote": 1}


# --- 9：已知的限制（沒有檢查擋得住）---


def test_09_known_limit_a_period_or_rate_changed_inside_a_paraphrase_is_not_caught():
    """每個月寫成每日：數字、否定、名字、出處都對，沒有任何一關看得出來；靠提示要求「用 quote 的字」降低機率。"""
    lines = [("S1", "每個月補助三千元，期限到年底。"), ("S2", "好的。")]

    kept, result = judge(lines, RawPoint(0, "每日補助三千元，期限到年底", quote="每個月補助三千元，期限到年底"))

    assert [p.text for p in kept] == ["每日補助三千元，期限到年底"] and not result.dropped_by_code


# --- 10：複述對方的話再反駁 ---


def test_10_a_claim_the_speaker_only_repeated_in_order_to_refute_it_is_not_his_claim():
    lines = [("S2", "我們認為這個政策會害到所有的孩子，影響非常大。"),
             ("S1", "你剛才說這個政策會害到所有的孩子，但事實並非如此，我們有數據。")]
    point = RawPoint(1, "這個政策會害到所有的孩子", quote="你剛才說這個政策會害到所有的孩子")

    kept, result = judge(lines, point)

    assert kept == [] and codes(result) == {"echo": 1}
    assert "T1 是在複述 T0" in problems_of(result)


# --- 12：檢查本身的誤判（V2、混合條列）---


def test_12_a_reported_question_before_the_statement_does_not_turn_the_statement_into_a_question():
    lines = [("S1", "請說明目前的做法。"), ("S2", "每年檢討一次，有人問預算有沒有公開？預算完全公開。")]
    point = RawPoint(1, "每年檢討一次，預算完全公開", quote="每年檢討一次，有人問預算有沒有公開？預算完全公開")

    kept, result = judge(lines, point)

    assert [p.text for p in kept] == ["每年檢討一次，預算完全公開"] and not result.dropped_by_code


def test_12_a_faithful_paraphrase_with_other_words_is_shown_as_the_speakers_own_words_instead_of_dropped():
    lines = [("S1", "請問商圈現在的狀況？"), ("S2", "那邊的人流量一年比一年少，店面撐不下去。")]
    point = RawPoint(1, "商圈客流逐年下滑，店家難以維持", quote="那邊的人流量一年比一年少，店面撐不下去")

    kept, result = judge(lines, point)

    assert [(p.speaker, p.text) for p in kept] == [("S2", "「那邊的人流量一年比一年少，店面撐不下去」")]
    assert result.rescued == 1 and not result.dropped_by_code


# --- 13：只有前半句的複述 ---


def test_13_the_first_half_of_a_restating_question_is_not_shown_as_the_speakers_claim():
    lines = [("S1", "以標案數量計算，所以十個標案發出去就算達成，土方有沒有清根本不重要，是這個意思嗎？"), ("S2", "不是這樣。")]
    point = RawPoint(0, "招標件數決定考核結果", quote="以標案數量計算，所以十個標案發出去就算達成，土方有沒有清根本不重要")

    kept, result = judge(lines, point)

    assert kept == [] and codes(result) == {"unsupported": 1}
    assert "複述對方" in problems_of(result)


# --- 14：只有兩個點的一頁，丟掉一個就整頁沒了 ---


def test_14_a_slide_of_two_points_loses_both_when_one_is_dropped():
    turns = make_turns(("S1", "預算要先編列三個月的額度，再看成效。"), ("S2", "期程要拉長到半年，才看得出成效。"))
    speakers = make_speakers(*TWO)
    window = chunk_turns(turns, speakers)[0]
    slide = RawSlide("預算與期程", (RawPoint(0, "預算先編三個月的額度", quote="預算要先編列三個月的額度，再看成效"),
                                  RawPoint(1, "期程拉長到一年", quote="期程要拉長到一年才看得出成效")))

    result = validate_points([slide], turns, speakers, window)

    assert result.slides == () and result.kept == 0 and result.proposed == 2
    assert codes(result) == {"bad_quote": 1, "slide_too_small": 1}
    assert (result.unverifiable, result.slide_rule, result.contradicted) == (1, 1, 0)


# --- 15：已知的限制 ---


def test_15_known_limit_a_host_restating_a_figure_without_a_confirming_tail_is_shown_as_his_words():
    """主持人把對方的數字用自己的話再說一次、沒有「是嗎」這類確認語：程式分不出來。顯示的仍然逐字是他那一輪的原話。"""
    lines = [("S2", "這個方案的電價是每度三塊四。"), ("S1", "電價是每度三塊四，那我們算一下總共要多少錢。")]
    point = RawPoint(1, "費率訂為每度三點四元", quote="電價是每度三塊四，那我們算一下總共要多少錢")

    kept, result = judge(lines, point)

    assert [(p.speaker, p.text) for p in kept] == [("S1", "「電價是每度三塊四，那我們算一下總共要多少錢」")]


# --- 16：一頁太多點 ---


def test_16_a_slide_shows_at_most_four_points_and_the_cut_ones_are_not_counted_as_proposed():
    words = ["預算編列", "期程安排", "成效評估", "人力配置", "場地使用", "宣傳方式"]
    turns = make_turns(*[("S1" if i % 2 == 0 else "S2", f"關於{w}的部分，我們有完整的規劃與說明。") for i, w in enumerate(words)])
    speakers = make_speakers(*TWO)
    window = chunk_turns(turns, speakers)[0]
    slide = RawSlide("六件事", tuple(RawPoint(i, f"說明{w}的規劃", quote=turns[i].text.rstrip("。")) for i, w in enumerate(words)))

    result = validate_points([slide], turns, speakers, window)

    assert result.kept == MAX_BULLETS == 4 and result.proposed == 4 and not result.dropped_by_code
    assert [p.turn for p in result.slides[0].points] == [0, 1, 2, 3]


# --- 17：百分比與單位被改寫 ---


@pytest.mark.parametrize("said, written", [
    ("根據模擬的結果，旅行時間可以減少百分之十二，通勤的人最有感。", "旅行時間可縮短十二分鐘"),
    ("這次調查顯示缺口比例是55%，主要集中在偏鄉地區。", "缺口比例達五十五成"),
    ("民調顯示百分之九十一的人同意這個做法，反對的很少。", "九十一成的人同意這個做法"),
    ("目前的完成率是72%，進度略有落後，需要再追趕。", "完成率不足七成，進度略有落後"),
])
def test_17_a_percentage_rewritten_as_minutes_or_tenths_is_dropped(said, written):
    kept, result = judge([("S1", "請說明數字。"), ("S2", said)], RawPoint(1, written, quote=said.rstrip("。")))

    assert kept == [] and codes(result) == {"number_mismatch": 1}


# --- 18：說話的人沒說過的算出來的數字 ---


@pytest.mark.parametrize("said, written", [
    ("每個攤位的租金是85元，一天要收三個攤位。", "三個攤位一共收二百五十五元"),
    ("今年的預算是3.2億，去年的預算是1.2億。", "預算大約是去年的六成"),
])
def test_18_a_figure_the_speaker_never_said_is_dropped(said, written):
    kept, result = judge([("S1", "請說明數字。"), ("S2", said)], RawPoint(1, written, quote=said.rstrip("。")))

    assert kept == [] and codes(result) == {"number_mismatch": 1}
