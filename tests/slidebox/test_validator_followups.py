# -*- coding: utf-8 -*-
"""審查之後補的檢查（複述換個說法、短回答回應的是哪一輪、意思相反的說法、雙重否定）的「不該擋」那一面。

審查留下的重現測試（test_validator_*.py）只證明「該擋的有擋」；這裡釘的是反過來：忠實的寫法不能被誤丟
（誤丟的點會算進 MAX_DROPPED_RATIO，丟太多整個工作會失敗）。
"""
from __future__ import annotations

import pytest

from slidebox.domain.entities import RawPoint, Turn
from slidebox.usecases import conversation as conv

from .conversation_support import run, with_anchors
from .speaker_fixtures import make_speakers

TWO = make_speakers("participant", "participant")


def published(lines, quote, turn, text, context=()):
    turns, good = with_anchors(*lines)
    extra, result = run(RawPoint(turn, text, tuple(context), quote), turns=turns, speakers=TWO, good=good)
    return [p.text for p in extra], result


def turn_of(index: int, speaker: str, text: str) -> Turn:
    return Turn(index, index * 10.0, index * 10.0 + 9.0, speaker, text)


# --- 複述 ---


def test_a_hosts_plain_question_about_the_topic_is_not_an_echo():
    """問句語氣詞（嗎）不是「對著對方複述」的字眼：主持人問和來賓講過的同一件事，是他自己的提問。"""
    lines = [("S1", "今天來談這個議題，先請教您的看法。"),
             ("S2", "這個稅制改革會讓中小企業多繳兩成的稅，我們強烈反對。"),
             ("S1", "那中小企業多繳兩成的稅會影響到雇用嗎？")]

    kept, _ = published(lines, "那中小企業多繳兩成的稅會影響到雇用嗎", 2, "詢問中小企業多繳兩成的稅是否影響雇用")

    assert kept == ["詢問中小企業多繳兩成的稅是否影響雇用"]


def test_the_carrier_region_is_the_clause_of_the_claim_and_the_two_before_it():
    claim = conv._grams("報告目前MRR為38萬")
    text = "好目前MRR是38萬 比上個月多了4萬2 然後青蛙診所試用30天快到期了 他們說想續約但是要砍價 我跟你講 目前活躍門市一共有212間"

    region = conv._carrier_region(text, claim)

    assert "好目前MRR是38萬" in region
    assert "他們說" not in region


def test_the_lead_in_before_a_claim_belongs_to_its_carrier_region():
    claim = conv._grams("指出中小企業要多繳兩成的稅")

    assert "您剛才提到" in conv._carrier_region("您剛才提到，中小企業要多繳兩成的稅。", claim)
    assert "您剛才提到" in conv._carrier_region("您剛才提到 嗯 中小企業要多繳兩成的稅", claim)


def test_a_turn_that_shares_nothing_with_the_claim_has_no_carrier_region():
    assert conv._carrier_region("山水花草鳥魚蟲獸石海", conv._grams("指出中小企業要多繳兩成的稅")) == ""


# 局長回答時先複述了一句（你說一個便當只有兩樣青菜），接著講自己的做法；quote 只引自己的做法那一半
REPLY = ("你說一個便當只有兩樣青菜，這個我們有看到照片，是第一天備援業者趕工的狀況，我們已經要求每餐至少三樣青菜，"
         "熱量不得低於六百五十大卡，稽查員到現場抽看，不合格的當場退回。")
RAISED = "局長，備援便當的品質也要談，有家長拍照給我，說一個便當只有兩樣青菜，雞肉也沒看到，孩子根本吃不飽。"
OWN_PART = "我們已經要求每餐至少三樣青菜，熱量不得低於六百五十大卡，稽查員到現場抽看"


def test_a_restating_phrase_outside_the_quoted_part_does_not_make_the_quoted_part_a_restatement():
    anchor, said = turn_of(41, "S1", REPLY), turn_of(40, "S2", RAISED)
    claim = conv._grams("規範備援便當青菜數量及熱量標準並加強稽查")

    assert conv._echo_source(anchor, claim, [said], OWN_PART) is None
    assert conv._echo_source(anchor, claim, [said]) is said              # 沒有 quote：照整輪（原本的行為）


def test_a_restating_phrase_inside_the_quote_still_counts():
    anchor, said = turn_of(41, "S1", REPLY), turn_of(40, "S2", RAISED)
    claim = conv._grams("指出一個便當只有兩樣青菜")

    assert conv._echo_source(anchor, claim, [said], "你說一個便當只有兩樣青菜") is said
    assert conv._echo_source(anchor, claim, [said], REPLY.rstrip("。")) is said          # 整輪當 quote


def test_the_carrier_region_looks_only_inside_the_quote_and_two_clauses_before_it():
    claim = conv._grams("指出一個便當只有兩樣青菜")

    assert "你說" in conv._carrier_region(REPLY, claim, "你說一個便當只有兩樣青菜")
    assert conv._carrier_region(REPLY, claim, "這個我們有看到照片，是第一天備援業者趕工的狀況") == ""
    assert conv._carrier_region(REPLY, claim, "不在這一輪裡的字句") == conv._carrier_region(REPLY, claim)


@pytest.mark.parametrize("phrase", ["您剛才提到", "你剛剛說", "所以你是說", "也就是說", "換句話說", "您的意思是"])
def test_restating_phrases_aimed_at_the_other_person_are_explicit(phrase):
    assert conv._RESTATE_PHRASE_RE.search(phrase + "中小企業要多繳兩成的稅")


@pytest.mark.parametrize("phrase", ["他們說", "她說", "所說", "剛才", "說過", "請問", "是不是"])
def test_talk_about_a_third_person_and_plain_question_words_are_not_explicit(phrase):
    assert conv._RESTATE_PHRASE_RE.search(phrase + "中小企業要多繳兩成的稅") is None


def test_an_explicit_restatement_needs_only_a_fifth_of_the_features_in_the_nearest_speakers_but_not_further_back():
    anchor = turn_of(20, "S1", "也就是說五年來半數計畫延遲")
    claim = conv._grams("指出五年來半數計畫延遲")                          # 10 個特徵，共同的只有 2 個（20%）
    said = turn_of(0, "S2", "這個計畫的失敗率非常高，過去五年有一半都延誤了")
    fillers = [turn_of(i, "S2", f"雨雪雲風雷電星月光影{'甲乙丙丁戊'[i % 5]}") for i in range(1, 6)]

    assert conv._echo_source(anchor, claim, [said, fillers[0]]) is said                    # 隔 1 位別人的發言
    assert conv._echo_source(anchor, claim, [said, *fillers[:3]]) is said                  # 隔 3 位（還在 ECHO_NEAR_TURNS 內）
    assert conv._echo_source(anchor, claim, [said, *fillers]) is None                      # 隔 5 位：要 25%


def test_without_an_explicit_phrase_the_other_speakers_words_must_fit_at_least_as_well_as_the_speakers_own():
    claim = conv._grams("指出五年來半數計畫延遲")
    said = turn_of(2, "S2", "過去五年來半數計畫都延遲了")                                  # 共同的特徵 7 個
    plain = turn_of(3, "S1", "五年來半數計畫延遲，是這樣嗎")                                 # 說話者自己的話共同的有 8 個
    explicit = turn_of(3, "S1", "您剛才提到五年來半數計畫延遲，是這樣嗎")

    assert conv._echo_source(plain, claim, [said]) is None
    assert conv._echo_source(explicit, claim, [said]) is said


# --- 短回答回應的是哪一輪 ---


def test_a_short_answer_to_a_question_the_same_speaker_split_over_two_turns_is_kept():
    lines = [("S1", "我們明年的預算要增加兩成。"), ("S1", "你同意嗎？"), ("S2", "沒錯。")]

    both, _ = published(lines, "沒錯", 2, "同意明年的預算要增加兩成", (0, 1))
    nearest_only, result = published(lines, "沒錯", 2, "同意明年的預算要增加兩成", (1,))

    assert both == ["同意明年的預算要增加兩成"]
    assert nearest_only == [] and "對不上" in "".join(result.problems)


def test_a_short_answer_answers_the_nearest_question_not_an_older_one():
    lines = [("S1", "我們明年的預算要增加兩成，你同意嗎？"), ("S2", "這個我們還要再研究。"),
             ("S1", "那你們願意取消現在的補助嗎？"), ("S2", "沒有。")]

    older, result = published(lines, "沒有。", 3, "否認明年的預算要增加兩成", (0,))
    nearest, _ = published(lines, "沒有。", 3, "否認願意取消現在的補助", (2,))

    assert older == [] and "T2" in "".join(result.problems)
    assert nearest == ["否認願意取消現在的補助"]


def test_the_question_may_be_three_turns_back_when_what_lies_between_is_no_question():
    lines = [("S1", "你認為補助應該加倍嗎？"), ("S2", "這個要看預算。"), ("S1", "我們先看數字。"), ("S2", "沒錯。")]

    kept, _ = published(lines, "沒錯", 3, "同意補助應該加倍", (0,))

    assert kept == ["同意補助應該加倍"]


def test_a_number_in_a_short_answers_text_must_come_from_the_question_it_answers():
    lines = [("S1", "今年的預算是三千億。"), ("S2", "這個我們還要再研究。"),
             ("S1", "那你們同意取消現在的補助嗎？"), ("S2", "同意。")]

    wrong, result = published(lines, "同意", 3, "同意取消現在的補助，預算三千億", (0, 2))
    right, _ = published(lines, "同意", 3, "同意取消現在的補助", (2,))

    assert wrong == [] and "三千億" in "".join(result.problems)
    assert right == ["同意取消現在的補助"]


def test_an_answer_with_no_one_else_before_it_in_range_has_nothing_to_answer():
    turns = [turn_of(0, "S2", "我認為補助要加倍。"), turn_of(1, "S2", "沒錯。")]

    assert conv._asked_turns(turns[1], {t.index: t for t in turns}, 0) == []


def test_the_asked_turns_never_reach_before_the_start_of_the_window():
    turns = [turn_of(0, "S1", "你同意嗎？"), turn_of(1, "S2", "沒錯。")]
    by_index = {t.index: t for t in turns}

    assert [t.index for t in conv._asked_turns(turns[1], by_index, 0)] == [0]
    assert conv._asked_turns(turns[1], by_index, 1) == []


# --- 意思相反的說法 ---


@pytest.mark.parametrize("text, spoken", [
    ("預算將減少兩成", "預算將會增加兩成"),
    ("失業率比去年上升零點五個百分點", "今年的失業率比去年下降了零點五個百分點"),
    ("支持課徵碳稅的提案", "我反對這個課徵碳稅的提案"),
    ("同意課徵碳稅", "我不同意課徵碳稅"),
    ("反對課徵碳稅", "我不反對課徵碳稅"),
    ("明年稅收增加五百億", "去年的稅收增加了五百億"),
    ("法案通過之前電價調漲", "法案通過之後，電價就會調漲"),
    ("工程已經完成", "這個工程尚未完成"),
    ("所有家長都支持延長托育時間", "大多數家長都支持延長托育時間"),
    ("每日補助三千元", "每個月補助三千元"),
    ("下週開始補助", "預計兩週後開始，也就是這週"),
    ("調降電價", "明年電價預計調漲百分之十"),
])
def test_a_text_that_uses_the_opposite_side_of_what_was_said_is_caught(text, spoken):
    problem = conv._polarity_flip_problem(text, spoken)

    assert problem is not None and problem.code == conv.POLARITY_FLIP
    assert "寫反" in problem.message


@pytest.mark.parametrize("text, spoken", [
    ("不同意延長開放", "我反對延長開放"),                          # 不同意 ≈ 反對
    ("不支持課徵碳稅", "我反對課徵碳稅"),
    ("支持這個提案", "我不反對這個提案"),
    ("不贊成這個提案", "我不認同這個提案"),
    ("主張提高效率", "我們要降低成本，同時提高效率"),            # 原話兩邊都有：看不出來
    ("指出明年預算增加", "今年預算增加，明年預算也增加"),
    ("說明增加預算", "預算增加了兩成"),
    ("說明預算上升兩成", "預算增加了兩成"),                      # 同一邊的不同說法
    ("指出預算沒有增加", "預算不會增加"),                         # 否定的方向詞不好判斷，不算
    ("說明「預算將會增加兩成」", "預算將減少兩成"),               # 「」裡的字是逐字引文，另有一關核對
    ("說明補助沒有取消", "補助還沒有取消"),
    ("說明每個月補助三千元", "每個月補助三千元，期限到年底"),
])
def test_equivalent_or_undecidable_wordings_are_not_flagged(text, spoken):
    assert conv._polarity_flip_problem(text, spoken) is None


def test_the_word_that_is_checked_is_named_in_the_reason():
    problem = conv._polarity_flip_problem("預算將減少兩成", "預算將會增加兩成")

    assert "減少" in problem.message and "增加" in problem.message


def test_the_nearest_original_wording_is_checked_too_so_a_quote_next_to_the_claim_cannot_cause_a_false_drop():
    """quote 抄了旁邊的子句、重點寫的是另一個子句（同一輪裡有增有減）：最貼近重點的原文有那一邊，不擋。"""
    lines = [("S1", "請說明一下今年的做法。"), ("S2", "我們要降低成本，同時提高效率，這是今年最重要的目標。"),
             ("S1", "了解。")]

    kept, _ = published(lines, "我們要降低成本，同時", 1, "主張提高效率")

    assert kept == ["主張提高效率"]


def test_a_flipped_wording_is_dropped_with_its_own_code():
    lines = [("S1", "今天我們來談明年的預算，先請教您的看法。"), ("S2", "明年的預算將會增加兩成，用在托育和長照上面。")]

    kept, result = published(lines, "明年的預算將會增加兩成", 1, "明年預算將減少兩成")

    assert kept == [] and dict(result.dropped_by_code) == {conv.POLARITY_FLIP: 1}
    assert result.contradicted == 1                                       # 算在「文字有問題」那一類，不是查不出來


# --- 文字漏掉原話的否定 ---


@pytest.mark.parametrize("text, spoken", [
    ("主張增加明年的預算", "我們不會增加明年的預算，因為財源不足"),
    ("指出預算減少兩成", "預算並沒有減少兩成"),
    ("說明明年的預算上升", "明年的預算不會上升，維持原樣"),
    ("說明補助提高到三千元", "這次補助沒有提高，還是兩千元"),
    ("指出明年開始實施", "法案不是明年開始實施，是後年"),
])
def test_a_wording_the_speaker_only_used_with_a_negation_is_written_without_it(text, spoken):
    problem = conv._polarity_flip_problem(text, spoken)

    assert problem is not None and problem.code == conv.POLARITY_FLIP
    assert "不" in problem.message or "沒" in problem.message


@pytest.mark.parametrize("text, spoken", [
    ("說明預算並沒有增加", "我們不會增加預算"),                              # 文字自己也否定
    ("必須增加預算", "我們不得不增加預算"),                                  # 雙重否定等於肯定
    ("並非不增加預算", "我們不是不增加預算"),
    ("指出預算增加，人力沒有增加", "預算增加了兩成，但人力不會增加"),         # 原話兩種都有
    ("詢問會不會增加預算", "你們會不會增加預算"),                            # 「會不會」是在問
    ("說明不僅增加預算還增加人力", "我們不僅增加預算，也增加人力"),             # 「不僅」不是否定
    ("說明增加預算", "不管增加或減少預算，都要公開"),
    ("說明預算增加", "預算沒有人說會減少，增加是確定的"),                       # 否定的是別的字
])
def test_a_faithful_wording_next_to_a_negation_is_not_flagged_as_dropping_it(text, spoken):
    assert conv._polarity_flip_problem(text, spoken) is None


@pytest.mark.parametrize("text, spoken", [
    ("支持課徵碳稅", "我不僅支持課徵碳稅，也支持補貼"),
    ("說明不僅支持課徵碳稅還支持補貼", "我支持課徵碳稅"),
])
def test_not_only_is_not_a_negation_of_a_stance(text, spoken):
    assert conv._polarity_flip_problem(text, spoken) is None


# --- 雙重否定 ---


@pytest.mark.parametrize("text, spoken", [
    ("不得增加預算", "我們不得不增加預算，否則整個計畫會停擺"),
    ("不願意加薪，預算有限", "我們不是不願意加薪"),
    ("不能提高費用", "這一次我們不能不提高費用"),
])
def test_a_double_negation_written_as_a_single_negation_is_caught(text, spoken):
    problem = conv._polarity_flip_problem(text, spoken)

    assert problem is not None and problem.code == conv.POLARITY_FLIP
    assert "雙重否定" in problem.message


@pytest.mark.parametrize("text, spoken", [
    ("願意加薪", "我們不是不願意加薪"),                              # 改寫成肯定，意思沒變
    ("必須增加預算", "我們不得不增加預算"),
    ("並非不願意加薪", "我們不是不願意加薪"),                         # 自己也是雙重否定
    ("不得不增加預算", "我們不得不增加預算"),                         # 照抄
    ("說明沒有不同意見", "我們沒有不同意見"),                         # 「不同」是一個詞，不是雙重否定
    ("說明不管怎樣都要做", "我們沒有不管這件事"),
])
def test_a_faithful_rewording_of_a_double_negation_is_not_flagged(text, spoken):
    assert conv._polarity_flip_problem(text, spoken) is None


# --- 改用原話：語音辨識只用空白分段、不加標點 ---

# 這些是真的語音辨識逐字稿（lab 的真實模型輸出）：quote 前面那一段話裡有「大概」「不可能」「不要」「是不是」，
# 但它們管的是前一段（空白前），不是 quote。
SPACE_SEPARATED = [
    ("還有一個風險我要講一下 我們資料庫的備份現在一天只有一次 我想改成每小時一次 大概要2個人天 因為",
     "上個月有一個客戶不小心刪掉整個門市的資料 我們只能還原到前一天晚上"),
    ("欸不是啦 有遵守啦 只是 座位只買了一百八十個 三百六十個人 你算一下 不可能同時進來啊 所以",
     "我們用一個系統訂位 搶不到位置的人 就只好留在家裡"),
    ("家人呢 在家工作最常被打斷的是不是家人 ", "我媽以為我在家就是沒事做 一天可以打來四次問我吃飯了沒"),
    ("這個不要省 ", "這種事情出一次就完蛋了"),
]


def rescued(before, quote, after=""):
    """文字和原文完全對不上（內容找不到依據）：看那段原話能不能被拿來顯示。"""
    lines = [("S1", "今天我們來談這件事，先請教您的看法。"), ("S2", before + quote + after)]
    kept, _ = published(lines, quote, 1, "樂意贊助該提案")
    return kept


@pytest.mark.parametrize("before, quote", SPACE_SEPARATED)
def test_a_negation_or_hedge_in_an_earlier_space_separated_chunk_does_not_block_the_rescue(before, quote):
    assert rescued(before, quote) == [f"「{quote}」"]


@pytest.mark.parametrize("before", [
    "我不同意 ", "我不同意啦 ", "我不確定 ", "我覺得可能 ", "他們不認為 ", "大概 ", "如果 ", "我反對 ",
    "我不覺得 ", "我不相信 ", "我不知道 ", "我懷疑 ",
])
def test_a_chunk_that_ends_with_a_word_governing_what_follows_still_blocks_the_rescue(before):
    """「我不同意 預算增加兩成」：空白前那一段是在表態、管著後面那句，單獨顯示後面那句就缺了這個態度。"""
    assert rescued(before, "預算明年會增加百分之二十") == []


@pytest.mark.parametrize("before", ["不要省", "我不同意這個預算", "可能", "大概會"])
def test_a_negation_or_hedge_in_the_same_chunk_as_the_quote_still_blocks_the_rescue(before):
    """同一段（沒有空白隔開）裡被截掉的前半有否定、語氣詞：照舊擋（原本就是這樣）。"""
    assert rescued(before, "這種事情出一次就完蛋了") == []


def test_a_condition_earlier_in_the_same_sentence_still_blocks_the_rescue_across_spaces():
    assert rescued("如果明年預算增加兩成 ", "我們就支持這個方案") == []


# --- 轉述 ---

SHUTTLE = ("公車也有接駁的做法，社區小巴我們可以先試辦，六個社區，每個社區兩輛車，十分鐘一班，這個成本很低，一年大概兩千萬。"
           "小巴的司機我們也找好了，都是退休的老司機，他們說在家裡待著也很無聊，願意回來開。")


def test_a_reporting_frame_in_a_part_the_point_does_not_use_does_not_make_it_a_report():
    """quote 是一整段話，最後有一句「他們說…」；重點寫的是前面自己的做法，沒有用到轉述的那一句。"""
    lines = [("S1", "請說明一下接駁的做法。"), ("S2", SHUTTLE)]
    text = "提出試辦社區小巴接駁服務，六個社區每處兩輛車十分鐘一班"

    kept, result = published(lines, SHUTTLE, 1, text)

    assert kept == [text], result.problems


def test_a_point_that_uses_what_they_said_is_still_a_report():
    lines = [("S1", "最近的進度如何？"), ("S2", "青蛙診所試用30天快到期了 他們說想續約但是要砍價")]

    kept, result = published(lines, "青蛙診所試用30天快到期了 他們說想續約但是要砍價", 1,
                             "指出青蛙診所試用期將滿，客戶要求續約但希望降價")

    assert kept == [] and dict(result.dropped_by_code) == {conv.REPORTED_SPEECH: 1}


def test_a_frame_at_the_start_of_the_quote_covers_everything_after_it():
    lines = [("S1", "市場上怎麼說？"), ("S2", "有人說明年房價會下跌三成，而且還會跌更久。")]

    kept, result = published(lines, "有人說明年房價會下跌三成，而且還會跌更久", 1, "預測房價還會跌更久")

    assert kept == [] and dict(result.dropped_by_code) == {conv.REPORTED_SPEECH: 1}


# --- 問句的字眼出現在陳述句裡 ---


@pytest.mark.parametrize("quote, text", [
    ("除了時間，錢也是問題，這也是為什麼我主張停車費調高以後，要把錢回饋成北岸的月票補貼",
     "主張停車費調高後將錢回饋為北岸月票補貼"),
    ("我們每年都在虧損，這就是為什麼我們要提高補助", "主張提高補助以改善每年虧損"),
    ("預算一直不夠，這也是為何明年一定要增加", "主張明年增加預算以補足不足"),
])
def test_a_declarative_that_is_why_is_not_a_question(quote, text):
    lines = [("S1", "請說明一下您的主張。"), ("S2", quote + "。")]

    kept, result = published(lines, quote, 1, text)

    assert kept == [text], result.problems


def test_a_real_why_question_still_has_to_be_written_as_asking():
    lines = [("S1", "我先請問一下。"), ("S2", "為什麼明年一定要增加預算？")]

    statement, _ = published(lines, "為什麼明年一定要增加預算", 1, "明年一定要增加預算")
    asking, _ = published(lines, "為什麼明年一定要增加預算", 1, "詢問為什麼明年一定要增加預算")

    assert statement == [] and asking == ["詢問為什麼明年一定要增加預算"]


def test_stating_the_topic_of_an_interpellation_is_not_stating_a_fact():
    quote = "本席要請教的是石機溪流域韌性特別預算，第三期，總額六百二十億"
    lines = [("S1", "請議員開始質詢。"), ("S2", quote + "。")]

    kept, result = published(lines, quote, 1, "提出質詢主題為石機溪流域韌性特別預算第三期，總額六百二十億")

    assert kept == ["提出質詢主題為石機溪流域韌性特別預算第三期，總額六百二十億"], result.problems


# --- 一般用語裡剛好含有否定、第一人稱的字 ---


@pytest.mark.parametrize("text, spoken", [
    ("詢問是否同意延長開放", "你們同意延長開放嗎"),
    ("詢問是不是支持課徵碳稅", "您支持課徵碳稅嗎"),
    ("詢問有沒有反對課徵碳稅", "你們反對課徵碳稅嗎"),
    ("詢問會不會同意延長開放", "你們同意延長開放嗎"),
    ("詢問能否同意延長開放", "你們同意延長開放嗎"),
    ("詢問有無支持課徵碳稅", "您支持課徵碳稅嗎"),
])
def test_a_yes_no_question_form_is_not_a_negation_when_deciding_if_a_stance_was_flipped(text, spoken):
    """「是否同意」「是不是支持」是問句的寫法，不是「不同意」「不支持」。"""
    assert conv._polarity_flip_problem(text, spoken) is None


def test_a_yes_no_question_form_does_not_make_a_question_look_like_a_double_negation():
    assert conv._polarity_flip_problem("詢問是否不願意加薪", "你是不是不願意加薪") is None


@pytest.mark.parametrize("text", ["詢問是不是同意延長開放", "詢問有沒有同意延長開放", "詢問能不能延長開放", "詢問會不會增加"])
def test_a_yes_no_question_form_in_the_text_is_not_a_negation_that_the_quote_must_have(text):
    assert conv._negation_problem(text, "你們同意延長開放嗎，會增加嗎") is None


def test_a_real_negation_next_to_a_question_form_is_still_checked():
    assert conv._negation_problem("詢問是否不願意延長開放", "你們願意延長開放嗎") is not None


@pytest.mark.parametrize("text", [
    "說明成本會大幅增加", "指出日本會派代表參加", "預測基本會維持不變", "資本公司明年擴大投資", "原本部分補助會取消",
])
def test_a_compound_word_that_happens_to_contain_an_institutions_first_person_is_not_one(text):
    assert conv._first_person_problem(text) is None


@pytest.mark.parametrize("text", ["本會將於下週召開記者會", "表示本部已經完成審查", "本公司明年會增加投資", "說明本院的做法"])
def test_an_institution_speaking_about_itself_is_still_first_person(text):
    assert conv._first_person_problem(text) is not None
