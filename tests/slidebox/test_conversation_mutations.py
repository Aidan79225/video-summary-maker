"""變異測試：重點檢查的每一道安全措施，拿掉就必須有測試失敗。

每個情境（Scenario）是一個「錯誤的重點」，各有兩個測試：

- test_the_safeguard_stops_the_bad_point：措施在，錯誤的重點被擋下（或被改正到真正說話的人）。
- test_without_the_safeguard_the_bad_point_gets_through：把這道措施換成什麼都不做（monkeypatch），
  同一個錯誤的重點被收下、歸給錯的人——證明上面那個測試真的是靠這道措施擋住的，
  不是被別的檢查順便擋掉。這個測試在 CI 裡每次都跑。

九個必要的變異（brief 列的）：label swap、cited turn outside window、minor/S0 cited、
leading pronoun、role/label word、unsupported claim、echo attributed to host、
quote spliced across turns、first person。其餘是這個實作多加的措施。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from slidebox.domain.entities import RawPoint
from slidebox.usecases import conversation
from slidebox.usecases.conversation import Window

from .conversation_support import GOOD, SPEAKERS, TURNS, TWO, at, problems_of, run, with_anchors
from .speaker_fixtures import make_speakers, make_turns


def nothing(*_args, **_kwargs):
    """換上去的「這道檢查什麼都不做」。"""
    return None


def always(*_args, **_kwargs):
    return True


def trust_the_cited_turn(cited, _claim, _nearby, _citable):
    """沒有「找出真正說話的那一段」：模型引用哪一段就算哪一段。"""
    return cited, None


@dataclass(frozen=True)
class Scenario:
    safeguard: str                       # conversation 模組裡的名稱
    off: object                          # 換上去的「什麼都不做」
    points: tuple
    kept_with: tuple                     # 措施在時留下的 (speaker, turn, text)
    kept_without: tuple                  # 措施拿掉時（錯誤地）留下的 (speaker, turn, text)
    reason: str                          # 措施在時，problems 裡必有、拿掉後不會有的字
    turns: tuple = TURNS
    speakers: tuple = SPEAKERS
    good: tuple = GOOD
    window: object = None
    extra: dict = field(default_factory=dict)


def tri(speaker: str, turn: int, text: str):
    return (speaker, turn, text)


# 三人對談裡的話（編號用內容找，不寫死）
BUDGET = at("預算大約")                 # 受訪者：預算大約要三千億…至少要把公共托育的名額加倍…
HOST_THANKS = at("好的，謝謝您")        # 主持人：好的，謝謝您。我們現在接聽一位民眾的電話。
CALLER = at("我想補充")                 # 只出現一次的來電者（其他聲音）

# 1. label swap：兩個重點的引用互換（受訪者的話引到主持人那段、主持人的話引到受訪者那段）
SWAP_A = RawPoint(HOST_THANKS, "主張至少要把公共托育的名額加倍")
SWAP_B = RawPoint(BUDGET, "說明現在接聽一位民眾的電話")

# 2. 引用的 turn 不在這個窗裡
SHORT_WINDOW = Window(0, TURNS[:9])

# 3b. 無法判斷（S0）的段落被引用：自訂一個小對談
S0_TURNS, S0_GOOD = with_anchors(("S1", "請問今年的預算大約是多少？"),
                                 ("S0", "預算大約要三千億，比去年多了兩成。"),
                                 ("S2", "好的。"))

# 7. 回音：主持人複述受訪者剛說的話（沒有問號，只有「您剛才說」的語氣）
ECHO_TURNS, ECHO_GOOD = with_anchors(("S2", "名額不夠，很多家長排不到，排了兩年也沒有消息。"),
                                     ("S1", "您剛才說名額不夠，家長排不到。"))

# 8. 引文接在兩段話的交界：前半是主持人說的、後半是受訪者說的
SPLICE_TURNS, SPLICE_GOOD = with_anchors(("S1", "我們必須立刻增加公共托育的名額。"),
                                         ("S2", "預算已經編好了，下個月就會公布。"))

# 兩位說話者一樣符合（同樣一句話各說了一次），引用的那段只是別的話
TIE_TURNS, TIE_GOOD = with_anchors(("S1", "托育名額不夠，家長排不到。"),
                                   ("S2", "托育名額不夠，家長排不到。"),
                                   ("S1", "謝謝大家，今天的意見我們不會漏掉。"))

SCENARIOS: dict[str, Scenario] = {
    # 兩個重點的引用互換：每個都被改正回真正說這句話的人；沒有這道措施就各自歸給引錯的人
    "label_swap": Scenario(
        "_pick_anchor", trust_the_cited_turn, (SWAP_A, SWAP_B),
        kept_with=(tri("S2", BUDGET, SWAP_A.text), tri("S1", HOST_THANKS, SWAP_B.text)),
        kept_without=(tri("S2", BUDGET, SWAP_B.text), tri("S1", HOST_THANKS, SWAP_A.text)),
        reason="已改引內容符合的"),
    "cited_turn_outside_window": Scenario(
        "_window_problem", nothing, (RawPoint(BUDGET, "預算大約要三千億，比去年多了兩成"),),
        window=SHORT_WINDOW, kept_with=(), kept_without=(), reason="不在這一段範圍"),
    "minor_voice_cited": Scenario(
        "_citable_problem", nothing, (RawPoint(CALLER, "補充社區的托育名額真的不夠"),),
        kept_with=(), kept_without=(tri("S3", CALLER, "補充社區的托育名額真的不夠"),), reason="不能引用"),
    "undecidable_turn_cited": Scenario(
        "_citable_problem", nothing, (RawPoint(1, "預算大約要三千億，比去年多了兩成"),),
        turns=S0_TURNS, speakers=TWO, good=S0_GOOD,
        kept_with=(), kept_without=(tri("S0", 1, "預算大約要三千億，比去年多了兩成"),), reason="不能引用"),
    "leading_pronoun_or_title": Scenario(
        "_leading_problem", nothing,
        (RawPoint(at("名額不夠"), "他認為名額不夠，家長排不到"), RawPoint(BUDGET, "邱委員指出預算大約要三千億")),
        kept_with=(),
        kept_without=(tri("S2", at("名額不夠"), "他認為名額不夠，家長排不到"),
                      tri("S2", BUDGET, "邱委員指出預算大約要三千億")),
        reason="開頭"),
    "role_or_label_word": Scenario(
        "_label_problem", nothing,
        (RawPoint(at("謝謝邱委員"), "主持人詢問預算要怎麼編才夠"), RawPoint(at("名額不夠"), "來賓表示名額不夠，家長排不到")),
        kept_with=(),
        kept_without=(tri("S2", at("名額不夠"), "來賓表示名額不夠，家長排不到"),
                      tri("S1", at("謝謝邱委員"), "主持人詢問預算要怎麼編才夠")),
        reason="文字裡有「"),
    "unsupported_claim": Scenario(
        "_is_supported", always, (RawPoint(at("名額不夠"), "宣布將立法禁止外送平台"),),
        kept_with=(), kept_without=(tri("S2", at("名額不夠"), "宣布將立法禁止外送平台"),), reason="找不到依據"),
    "echo_attributed_to_the_host": Scenario(
        "_echo_source", nothing, (RawPoint(1, "名額不夠，家長排不到"),),
        turns=ECHO_TURNS, speakers=TWO, good=ECHO_GOOD,
        kept_with=(), kept_without=(tri("S1", 1, "名額不夠，家長排不到"),), reason="複述"),
    "quote_spliced_across_turns": Scenario(
        "_quote_problem", nothing,
        (RawPoint(1, "說明「必須立刻增加公共托育的名額，預算已經編好了」"), RawPoint(0, "指出「必須立刻」")),
        turns=SPLICE_TURNS, speakers=TWO, good=SPLICE_GOOD,
        kept_with=(),
        kept_without=(tri("S1", 0, "說明「必須立刻增加公共托育的名額，預算已經編好了」"), tri("S1", 0, "指出「必須立刻」")),
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
        "_number_problem", nothing, (RawPoint(BUDGET, "預算大約要五千億，比去年多了兩成"),),
        kept_with=(), kept_without=(tri("S2", BUDGET, "預算大約要五千億，比去年多了兩成"),), reason="數字「五千億」"),
    "flipped_meaning": Scenario(
        "_negation_problem", nothing, (RawPoint(BUDGET, "預算不需要三千億"),),
        kept_with=(), kept_without=(tri("S2", BUDGET, "預算不需要三千億"),), reason="否定的說法"),
    "question_stated_as_fact": Scenario(
        "_question_problem", nothing, (RawPoint(at("那公共托育"), "公共托育現在有不足"),),
        kept_with=(), kept_without=(tri("S1", at("那公共托育"), "公共托育現在有不足"),), reason="問句"),
    "bare_one_word_answer": Scenario(
        "_answer_problem", nothing, (RawPoint(at("對。"), "同意還要提高保母的薪資"),),
        kept_with=(), kept_without=(tri("S2", at("對。"), "同意還要提高保母的薪資"),), reason="必須在 context"),
    "equal_fit_two_speakers": Scenario(
        "_pick_anchor", trust_the_cited_turn, (RawPoint(2, "托育名額不夠，家長排不到"),),
        turns=TIE_TURNS, speakers=TWO, good=TIE_GOOD,
        kept_with=(), kept_without=(tri("S1", 2, "托育名額不夠，家長排不到"),), reason="無法確定是誰說的"),
    "single_surviving_point": Scenario(
        "MIN_POINTS_PER_SLIDE", 1,
        (RawPoint(0, "詢問目前補助政策的看法"), RawPoint(0, "他說了什麼事")), good=(),
        kept_with=(), kept_without=(tri("S1", 0, "詢問目前補助政策的看法"),), reason="整頁不用"),
}

def outcome(scenario: Scenario):
    extra, result = run(*scenario.points, turns=scenario.turns, speakers=scenario.speakers,
                        window=scenario.window, good=scenario.good)
    return [(p.speaker, p.turn, p.text) for p in extra], problems_of(result)


@pytest.mark.parametrize("name", SCENARIOS)
def test_the_safeguard_stops_the_bad_point(name):
    scenario = SCENARIOS[name]

    kept, problems = outcome(scenario)

    assert kept == list(scenario.kept_with)
    assert scenario.reason in problems


@pytest.mark.parametrize("name", SCENARIOS)
def test_without_the_safeguard_the_bad_point_gets_through(name, monkeypatch):
    scenario = SCENARIOS[name]
    monkeypatch.setattr(conversation, scenario.safeguard, scenario.off)

    kept, problems = outcome(scenario)

    assert kept == list(scenario.kept_without)
    assert scenario.reason not in problems


def test_the_scenarios_cover_the_nine_required_mutations():
    required = {"label_swap", "cited_turn_outside_window", "minor_voice_cited", "leading_pronoun_or_title",
                "role_or_label_word", "unsupported_claim", "echo_attributed_to_the_host",
                "quote_spliced_across_turns", "first_person"}

    assert required <= set(SCENARIOS)
