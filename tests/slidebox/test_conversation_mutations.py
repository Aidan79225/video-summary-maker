"""變異測試：重點檢查的每一道安全措施，拿掉就必須有測試失敗。

每個情境（Scenario）是一個「錯誤的重點」，各有兩個測試：

- test_the_safeguard_stops_the_bad_point：措施在，錯誤的重點被擋下（或被改正到真正說話的人，或只剩原話）。
- test_without_the_safeguard_the_outcome_changes：把這道措施換成什麼都不做（monkeypatch），結果就變了——
  通常是同一個錯誤的重點被收下、歸給錯的人（證明上面那個測試真的是靠這道措施擋住的，不是被別的檢查順便擋掉）。
  有幾道措施是「雙重保險」（例如引用的 turn 不能引用，quote 的核對本來也找不到它）：拿掉其中一道，
  另一道還擋得住，所以這類情境看的是「擋下的原因換了」（reason_without），不是「錯誤的重點通過了」。
  真正逐道拿掉、用整個測試套件檢查的是 scratchpad 外的突變測試（見 README 的歸屬一節與 PR 說明）。

這個測試在 CI 裡每次都跑。九個必要的變異（brief 列的）：label swap、cited turn outside window、minor/S0 cited、
leading pronoun、role/label word、unsupported claim、echo attributed to host、quote spliced across turns、
first person。其餘是這個實作多加的措施。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pytest

from slidebox.domain.entities import RawPoint, RawSlide
from slidebox.usecases import conversation
from slidebox.usecases.conversation import Window, validate_points
from slidebox.usecases.quotes import QuoteLocation, QuoteSpan

from .conversation_support import GOOD, SPEAKERS, TURNS, TWO, at, problems_of, run, with_anchors

NEVER = re.compile(r"(?!x)x")                     # 永遠不會符合的正規式


def nothing(*_args, **_kwargs):
    """換上去的「這道檢查什麼都不做」。"""
    return None


def always(*_args, **_kwargs):
    return True


def never(*_args, **_kwargs):
    return False


def trust_the_cited_turn(turn, _claim, _nearby, _citable):
    """沒有「確認內容出自這一輪」：quote 在哪一輪就當內容也在那一輪。"""
    return turn


def trust_the_cited_turn_for_the_quote(_quote, cited, _in_window, _citable):
    """沒有「找出真正有 quote 的那一輪」：模型引用哪一輪就算哪一輪。"""
    return QuoteLocation(cited)


def the_quote_as_written(_turn_text, quote):
    """不要求原話是那一輪裡連續的一段：模型抄的是什麼就顯示什麼。"""
    return QuoteSpan(str(quote), 0, len(str(quote)))


@dataclass(frozen=True)
class Scenario:
    safeguard: str                       # conversation 模組裡的名稱
    off: object                          # 換上去的「什麼都不做」
    points: tuple
    kept_with: tuple                     # 措施在時留下的 (speaker, turn, text)
    kept_without: tuple                  # 措施拿掉時留下的 (speaker, turn, text)
    reason: str = ""                     # 措施在時，problems 裡必有、拿掉後不會有的字（空的不檢查）
    reason_without: str = ""             # 措施拿掉後，problems 裡改成有的字（雙重保險的情境用）
    turns: tuple = TURNS
    speakers: tuple = SPEAKERS
    good: tuple = GOOD
    window: object = None
    extra: dict = field(default_factory=dict)


def tri(speaker: str, turn: int, text: str):
    return (speaker, turn, text)


def quoted(text: str) -> str:
    return f"「{text}」"


# 三人對談裡的話（編號用內容找，不寫死）
BUDGET = at("預算大約")                 # 受訪者：預算大約要三千億…至少要把公共托育的名額加倍…
HOST_THANKS = at("好的，謝謝您")        # 主持人：好的，謝謝您。我們現在接聽一位民眾的電話。
CALLER = at("我想補充")                 # 只出現一次的來電者（其他聲音）
BUDGET_QUOTE = "至少要把公共托育的名額加倍"       # 受訪者那段裡的原文
HOST_QUOTE = "我們現在接聽一位民眾的電話"          # 主持人那段裡的原文

# 1. label swap：兩個重點的 turn 編號互換（受訪者的話引到主持人那段、主持人的話引到受訪者那段），quote 照抄是對的
SWAP_A = RawPoint(HOST_THANKS, "主張至少要把公共托育的名額加倍", quote=BUDGET_QUOTE)
SWAP_B = RawPoint(BUDGET, "說明現在接聽一位民眾的電話", quote=HOST_QUOTE)

# 2. 引用的 turn 不在這個窗裡
SHORT_WINDOW = Window(0, TURNS[:9])

# 3b. 無法判斷（S0）的段落被引用：自訂一個小對談
S0_TURNS, S0_GOOD = with_anchors(("S1", "請問今年的預算大約是多少？"),
                                 ("S0", "預算大約要三千億，比去年多了兩成。"),
                                 ("S2", "好的。"))

# 7. 回音：主持人複述受訪者剛說的話（沒有問號，只有「您剛才說」的語氣）
ECHO_TURNS, ECHO_GOOD = with_anchors(("S2", "名額不夠，很多家長排不到，排了兩年也沒有消息。"),
                                     ("S1", "您剛才說名額不夠，家長排不到。"))

# 8. 引文接在兩段話的交界：同一位主持人在不同輪說的兩半
SPLICE_TURNS, SPLICE_GOOD = with_anchors(("S1", "我們必須立刻增加公共托育的名額，這件事不能再拖。"),
                                         ("S2", "預算已經編好了，下個月就會公布。"),
                                         ("S1", "預算要先通過審查，才能撥給各縣市。"))

# 兩位說話者一樣符合（同樣一句話各說了一次），quote 所在的那段只是別的話
TIE_TURNS, TIE_GOOD = with_anchors(("S1", "托育名額不夠，家長排不到。"),
                                   ("S2", "托育名額不夠，家長排不到。"),
                                   ("S1", "謝謝大家，今天的意見我們不會漏掉。"))

# 很短的回答
ANSWER_TURNS, ANSWER_GOOD = with_anchors(("S1", "是不是還要提高保母的薪資？"), ("S2", "沒錯。"))

# 內容在附近找不到依據，quote 本身也不夠格改顯示原話的各種情況
UNSAID = "宣布將立法禁止外送平台"
PRONOUN_TURNS, PRONOUN_GOOD = with_anchors(("S2", "他們說名額不夠，很多家長排不到，排了兩年也沒有消息。"),
                                           ("S1", "謝謝您的說明。"))
SKIP_TURNS, SKIP_GOOD = with_anchors(("S2", "名額不夠，很多家長排不到，排了兩年也沒有消息。"), ("S1", "謝謝您的說明。"))
SHORT_TURNS, SHORT_GOOD = with_anchors(("S2", "名額不夠。"), ("S1", "謝謝您的說明。"))
LONG_SENTENCE = "名額不夠，" + "很多家長排不到，" * 15 + "排了兩年也沒有消息。"
LONG_TURNS, LONG_GOOD = with_anchors(("S2", LONG_SENTENCE), ("S1", "謝謝您的說明。"))
ECHO_START_TURNS, ECHO_START_GOOD = with_anchors(("S1", "您剛才說名額不夠，家長排不到，這件事我們會處理。"),
                                                 ("S2", "謝謝您的說明。"))
RESTATE_TURNS, RESTATE_GOOD = with_anchors(("S1", "所以名額加倍就算達成目標，是這個意思嗎？"), ("S2", "謝謝您的說明。"))

# 一段話裡有一句問句，但要寫的那句是陳述
CARRIER_TURNS, CARRIER_GOOD = with_anchors(("S2", "名額不夠，不是嗎？很多家長排不到。"), ("S1", "謝謝您的說明。"))

# 逐字稿用來稱呼人的名字
NAME_TURNS, NAME_GOOD = with_anchors(("S1", "阿翔，請問名額不夠的問題要怎麼解決？"),
                                     ("S2", "名額不夠，很多家長排不到，排了兩年也沒有消息。"))

SCENARIOS: dict[str, Scenario] = {
    # 兩個重點的 turn 編號互換：quote 在哪一輪，就改歸那一輪的人；沒有這道措施，內容和引用的輪對不上，兩個都被丟掉
    "label_swap": Scenario(
        "locate_quote", trust_the_cited_turn_for_the_quote, (SWAP_A, SWAP_B),
        kept_with=(tri("S2", BUDGET, SWAP_A.text), tri("S1", HOST_THANKS, SWAP_B.text)),
        kept_without=(), reason="已改引有這段原文的", reason_without="內容出自別輪發言"),
    # quote 不是任何一輪的原文（改了一個字）：整個丟掉；沒有這道措施，只看內容，就收下了
    "fabricated_quote": Scenario(
        "locate_quote", trust_the_cited_turn_for_the_quote,
        (RawPoint(BUDGET, "預算大約要三千億，比去年多了兩成", quote="預算大約要三千億，比去年多了三成"),),
        kept_with=(), kept_without=(tri("S2", BUDGET, "預算大約要三千億，比去年多了兩成"),),
        reason="連續照抄的原文"),
    "cited_turn_outside_window": Scenario(
        "_window_problem", nothing, (RawPoint(BUDGET, "預算大約要三千億，比去年多了兩成"),),
        window=SHORT_WINDOW, kept_with=(), kept_without=(), reason="不在這一段範圍",
        reason_without="連續照抄的原文"),
    "minor_voice_cited": Scenario(
        "_citable_problem", nothing, (RawPoint(CALLER, "補充社區的托育名額真的不夠"),),
        kept_with=(), kept_without=(), reason="不能引用", reason_without="連續照抄的原文"),
    "undecidable_turn_cited": Scenario(
        "_citable_problem", nothing, (RawPoint(1, "預算大約要三千億，比去年多了兩成"),),
        turns=S0_TURNS, speakers=TWO, good=S0_GOOD,
        kept_with=(), kept_without=(), reason="不能引用", reason_without="連續照抄的原文"),
    "leading_pronoun_or_title": Scenario(
        "_leading_problem", nothing,
        (RawPoint(at("名額不夠"), "他認為名額不夠，家長排不到"), RawPoint(BUDGET, "陳教授指出預算大約要三千億")),
        kept_with=(),
        kept_without=(tri("S2", at("名額不夠"), "他認為名額不夠，家長排不到"),
                      tri("S2", BUDGET, "陳教授指出預算大約要三千億")),
        reason="開頭"),
    "pronoun_in_the_middle_of_the_text": Scenario(
        "_PRONOUN_RE", NEVER, (RawPoint(at("謝謝邱委員"), "詢問預算要怎麼編才夠，他們需要答覆"),),
        kept_with=(), kept_without=(tri("S1", at("謝謝邱委員"), "詢問預算要怎麼編才夠，他們需要答覆"),),
        reason="代名詞"),
    "role_or_label_word": Scenario(
        "_label_problem", nothing,
        (RawPoint(at("謝謝邱委員"), "主持人詢問預算要怎麼編才夠"), RawPoint(at("名額不夠"), "來賓表示名額不夠，家長排不到")),
        kept_with=(),
        kept_without=(tri("S2", at("名額不夠"), "來賓表示名額不夠，家長排不到"),
                      tri("S1", at("謝謝邱委員"), "主持人詢問預算要怎麼編才夠")),
        reason="文字裡有「"),
    "name_the_transcript_uses_for_a_person": Scenario(
        "_name_problem", nothing, (RawPoint(1, "名額不夠，阿翔的家長排不到"),),
        turns=NAME_TURNS, speakers=TWO, good=NAME_GOOD,
        kept_with=(), kept_without=(tri("S2", 1, "名額不夠，阿翔的家長排不到"),), reason="逐字稿用來稱呼人的名字"),
    # 內容在附近找不到依據：模型寫的字不會顯示，頂多改顯示那一輪裡照抄的原話；沒有這道措施，模型寫的字就直接顯示
    "unsupported_claim": Scenario(
        "_is_supported", always, (RawPoint(at("名額不夠"), UNSAID),),
        kept_with=(tri("S2", at("名額不夠"), quoted(
            "名額不夠，很多家長排不到，排了兩年也沒有消息。而且費用從去年開始漲了三成很多家長只好放棄")),),
        kept_without=(tri("S2", at("名額不夠"), UNSAID),)),
    "echo_attributed_to_the_host": Scenario(
        "_echo_source", nothing, (RawPoint(1, "名額不夠，家長排不到"),),
        turns=ECHO_TURNS, speakers=TWO, good=ECHO_GOOD,
        kept_with=(), kept_without=(tri("S1", 1, "名額不夠，家長排不到"),), reason="複述"),
    "quote_spliced_across_turns": Scenario(
        "_quote_problem", nothing,
        (RawPoint(0, "說明「必須立刻增加公共托育的名額，預算要先通過審查」", quote="必須立刻增加公共托育的名額"),
         RawPoint(0, "指出「必須立刻」", quote="必須立刻增加公共托育的名額")),
        turns=SPLICE_TURNS, speakers=TWO, good=SPLICE_GOOD,
        kept_with=(),
        kept_without=(tri("S1", 0, "說明「必須立刻增加公共托育的名額，預算要先通過審查」"), tri("S1", 0, "指出「必須立刻」")),
        reason="引文"),
    "first_person": Scenario(
        "_first_person_problem", nothing, (RawPoint(BUDGET, "我們認為預算大約要三千億"),),
        kept_with=(), kept_without=(tri("S2", BUDGET, "我們認為預算大約要三千億"),), reason="第一人稱"),
    # --- 這個實作多加的措施 ---
    # （標記裡不放數字，否則數字檢查會順便擋掉它，看不出標記檢查有沒有在作用）
    "program_markers": Scenario(
        "_marker_problem", nothing, (RawPoint(0, "[[S]]詢問目前補助政策的看法"),),
        kept_with=(), kept_without=(tri("S1", 0, "[[S]]詢問目前補助政策的看法"),), reason="標記"),
    "wrong_number": Scenario(
        "number_problem", nothing, (RawPoint(BUDGET, "預算大約要五千億，比去年多了兩成"),),
        kept_with=(), kept_without=(tri("S2", BUDGET, "預算大約要五千億，比去年多了兩成"),), reason="數字「五千億」"),
    "wrong_unit": Scenario(
        "number_problem", nothing, (RawPoint(BUDGET, "預算大約要三千億，比去年多了三成"),),
        kept_with=(), kept_without=(tri("S2", BUDGET, "預算大約要三千億，比去年多了三成"),), reason="數字「三成」"),
    "flipped_meaning": Scenario(
        "_negation_problem", nothing, (RawPoint(BUDGET, "預算不需要三千億"),),
        kept_with=(), kept_without=(tri("S2", BUDGET, "預算不需要三千億"),), reason="否定的說法"),
    "question_stated_as_fact": Scenario(
        "_question_problem", nothing, (RawPoint(at("那公共托育"), "公共托育現在有不足"),),
        kept_with=(), kept_without=(tri("S1", at("那公共托育"), "公共托育現在有不足"),), reason="問句"),
    # 一段話裡別的地方有一句問句，不能讓要寫的那句陳述被當成問句
    "statement_next_to_a_question": Scenario(
        "_carries_question", always, (RawPoint(0, "名額不夠，家長排不到"),),
        turns=CARRIER_TURNS, speakers=TWO, good=CARRIER_GOOD,
        kept_with=(tri("S2", 0, "名額不夠，家長排不到"),), kept_without=(), reason_without="問句"),
    "bare_one_word_answer": Scenario(
        "_answer_problem", nothing, (RawPoint(1, "同意還要提高保母的薪資"),),
        turns=ANSWER_TURNS, speakers=TWO, good=ANSWER_GOOD,
        kept_with=(), kept_without=(tri("S2", 1, "同意還要提高保母的薪資"),), reason="必須在 context"),
    "equal_fit_two_speakers": Scenario(
        "_pick_anchor", trust_the_cited_turn, (RawPoint(2, "托育名額不夠，家長排不到", quote="謝謝大家，今天的意見"),),
        turns=TIE_TURNS, speakers=TWO, good=TIE_GOOD,
        kept_with=(), kept_without=(tri("S1", 2, "托育名額不夠，家長排不到"),), reason="無法確定是誰說的"),
    # quote 是甲那一輪的原文，內容卻是旁邊乙那一輪說的：不能把乙說的事歸到引了甲的話的人
    "anchor_agreement": Scenario(
        "_anchor_problem", nothing, (RawPoint(HOST_THANKS, "主張至少要把公共托育的名額加倍", quote=HOST_QUOTE),),
        kept_with=(), kept_without=(tri("S2", BUDGET, "主張至少要把公共托育的名額加倍"),),
        reason="內容出自別輪發言"),
    "single_surviving_point": Scenario(
        "MIN_POINTS_PER_SLIDE", 1,
        (RawPoint(0, "詢問目前補助政策的看法"), RawPoint(0, "他說了什麼事")), good=(),
        kept_with=(), kept_without=(tri("S1", 0, "詢問目前補助政策的看法"),), reason="整頁不用"),
    # --- 改顯示原話的條件：每一條拿掉，不夠格的原話就被顯示出來 ---
    "rescue_needs_no_pronoun_in_the_quote": Scenario(
        "_wording_hit", nothing, (RawPoint(0, UNSAID),),
        turns=PRONOUN_TURNS, speakers=TWO, good=PRONOUN_GOOD,
        kept_with=(), reason="也不能改用原話",
        kept_without=(tri("S2", 0, quoted("他們說名額不夠，很多家長排不到，排了兩年也沒有消息")),)),
    "rescue_needs_a_contiguous_quote": Scenario(
        "contiguous_span", the_quote_as_written, (RawPoint(0, UNSAID, quote="名額不夠，排了兩年也沒有消息"),),
        turns=SKIP_TURNS, speakers=TWO, good=SKIP_GOOD,
        kept_with=(), reason="連續的一段原話",
        kept_without=(tri("S2", 0, quoted("名額不夠，排了兩年也沒有消息")),)),
    "rescue_needs_a_long_enough_quote": Scenario(
        "MIN_QUOTE_CHARS", 1, (RawPoint(0, UNSAID),),
        turns=SHORT_TURNS, speakers=TWO, good=SHORT_GOOD,
        kept_with=(), reason="太短", kept_without=(tri("S2", 0, quoted("名額不夠")),)),
    "rescue_needs_a_short_enough_quote": Scenario(
        "MAX_RESCUED_QUOTE_CHARS", 10 ** 6, (RawPoint(0, UNSAID),),
        turns=LONG_TURNS, speakers=TWO, good=LONG_GOOD,
        kept_with=(), reason="太長", kept_without=(tri("S2", 0, quoted(LONG_SENTENCE[:-1])),)),
    "rescue_refuses_a_quote_that_relays_the_other_side": Scenario(
        "ECHO_START_RE", NEVER, (RawPoint(0, UNSAID),),
        turns=ECHO_START_TURNS, speakers=TWO, good=ECHO_START_GOOD,
        kept_with=(), reason="轉述對方",
        kept_without=(tri("S1", 0, quoted("您剛才說名額不夠，家長排不到，這件事我們會處理")),)),
    "rescue_refuses_the_half_sentence_that_waits_for_confirmation": Scenario(
        "is_restatement", never, (RawPoint(0, UNSAID, quote="所以名額加倍就算達成目標"),),
        turns=RESTATE_TURNS, speakers=TWO, good=RESTATE_GOOD,
        kept_with=(), reason="複述對方", kept_without=(tri("S1", 0, quoted("所以名額加倍就算達成目標")),)),
    # --- 安全模式：每個點都只顯示照抄的原話，所以原話要先過同一道門檻 ---
    "quote_only_refuses_a_pronoun_quote": Scenario(
        "_wording_hit", nothing, (RawPoint(0, "文字不會顯示，所以不看", quote="他們說名額不夠，很多家長排不到"),),
        turns=PRONOUN_TURNS, speakers=TWO, good=PRONOUN_GOOD, extra={"quote_only": True},
        kept_with=(), reason="quote 不能直接當重點顯示",
        kept_without=(tri("S2", 0, quoted("他們說名額不夠，很多家長排不到")),)),
}


def outcome(scenario: Scenario):
    extra, result = run(*scenario.points, turns=scenario.turns, speakers=scenario.speakers,
                        window=scenario.window, good=scenario.good, **scenario.extra)
    return [(p.speaker, p.turn, p.text) for p in extra], problems_of(result)


@pytest.mark.parametrize("name", SCENARIOS)
def test_the_safeguard_stops_the_bad_point(name):
    scenario = SCENARIOS[name]

    kept, problems = outcome(scenario)

    assert kept == list(scenario.kept_with)
    if scenario.reason:
        assert scenario.reason in problems


@pytest.mark.parametrize("name", SCENARIOS)
def test_without_the_safeguard_the_outcome_changes(name, monkeypatch):
    scenario = SCENARIOS[name]
    monkeypatch.setattr(conversation, scenario.safeguard, scenario.off)

    kept, problems = outcome(scenario)

    assert kept == list(scenario.kept_without)
    assert kept != list(scenario.kept_with) or scenario.reason_without
    if scenario.reason:
        assert scenario.reason not in problems
    if scenario.reason_without:
        assert scenario.reason_without in problems


def test_the_scenarios_cover_the_nine_required_mutations():
    required = {"label_swap", "cited_turn_outside_window", "minor_voice_cited", "leading_pronoun_or_title",
                "role_or_label_word", "unsupported_claim", "echo_attributed_to_the_host",
                "quote_spliced_across_turns", "first_person"}

    assert required <= set(SCENARIOS)


# --- 一頁最多四個點：多的在計數前就扣掉 ---

SIX_QUOTES = (
    ("今天我們來談少子化，先請教您怎麼看目前的補助政策", 0), ("現金補助有幫助，但不能只靠現金，托育資源更重要", 1),
    ("那公共托育現在有什麼不足", 2), ("名額不夠，很多家長排不到，排了兩年也沒有消息", 3),
    ("所以您認為補助應該加倍嗎", 4), ("預算大約要三千億，比去年多了兩成", at("預算大約")),
)


def six_good_points():
    return RawSlide("六個重點", tuple(RawPoint(turn, f"說明第{n}段話的內容", quote=quote)
                                    for n, (quote, turn) in enumerate(SIX_QUOTES, 1)))


def test_points_beyond_the_fourth_on_a_page_are_not_shown_and_not_counted():
    from slidebox.usecases.conversation import chunk_turns

    result = validate_points([six_good_points()], TURNS, SPEAKERS, chunk_turns(TURNS, SPEAKERS)[0], quote_only=True)

    assert [p.turn for p in result.slides[0].points] == [0, 1, 2, 3]
    assert (result.proposed, result.kept) == (4, 4)


def test_without_the_cap_the_extra_points_would_be_shown_and_counted(monkeypatch):
    from slidebox.usecases.conversation import chunk_turns

    monkeypatch.setattr(conversation, "MAX_BULLETS", 99)

    result = validate_points([six_good_points()], TURNS, SPEAKERS, chunk_turns(TURNS, SPEAKERS)[0], quote_only=True)

    assert (result.proposed, result.kept) == (6, 6)
