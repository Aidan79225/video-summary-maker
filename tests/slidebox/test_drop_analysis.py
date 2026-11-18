"""被丟掉的重點「再算一次」（drop_analysis）：只算、不判斷。

要回答的問題（真實影片 ReXUCiDCbl8 的第四次失敗之後）：跨輪的 quote 是同一位說話者被切開、還是兩個人的話拼在一起？
問句被丟掉，是哪一個字眼觸發的？如果換一種做法（把同一位說話者相鄰的輪併起來、或只取 quote 裡逐字吻合的最長一段），
被丟掉的點有幾個會通過其餘的檢查？——這些都用真的檢查重跑，不是另外寫一份規則來猜。
"""
from __future__ import annotations

import random

import pytest

from slidebox.domain.entities import RawPoint, RawSlide
from slidebox.usecases import conversation as c
from slidebox.usecases import drop_analysis as a
from slidebox.usecases.conversation import Window
from slidebox.usecases.speakers import squash

from .drop_support import (
    BODY,
    BOTH_POINT,
    BOTH_TURNS,
    C_POINT,
    C_TEXT,
    C_TURNS,
    CITABLE,
    CROSS_TURNS,
    D_OTHER,
    D_POINT,
    D_QUOTE,
    D_TEXT,
    D_TURNS,
    HEAD,
    NOISE_TURNS,
    Q_NEAR_POINT,
    Q_NEAR_TURNS,
    Q_SHORT_POINT,
    Q_SHORT_TURNS,
    Q_STRICT_POINT,
    Q_STRICT_TURNS,
    SPEAKERS,
    contexts,
    dropped,
)
from .speaker_fixtures import make_turns


# --- 併輪：同一位說話者相鄰的輪（中間可以夾著很短的其他聲音）---


def test_adjacent_turns_of_one_speaker_become_one_turn():
    turns = make_turns(("S1", "我們今年的預算"), ("S1", "大約增加了兩成"), ("S2", "好的謝謝"))

    joined = a.join_adjacent(turns, CITABLE)

    assert [(t.index, t.speaker, t.text) for t in joined.turns] == [
        (0, "S1", "我們今年的預算 大約增加了兩成"), (2, "S2", "好的謝謝")]
    assert (joined.turns[0].start, joined.turns[0].end) == (turns[0].start, turns[1].end)      # 時間涵蓋整段
    assert joined.merged_into == {0: 0, 1: 0, 2: 2}
    assert joined.chains == (a.Chain("S1", (0, 1)),)


def test_a_very_short_other_voice_inside_one_speakers_words_is_left_out():
    """真實影片的例子：同一個人的話中間夾了一個字的「X 結」（diarizer 把一個字判成別的聲音）。"""
    turns = make_turns(("S1", "第三部分候選人結論"), ("S3", "結"), ("S1", "論時間每位候選人各12分鐘"))

    joined = a.join_adjacent(turns, CITABLE)

    assert [(t.index, t.speaker) for t in joined.turns] == [(0, "S1")]
    assert joined.turns[0].text == "第三部分候選人結論 論時間每位候選人各12分鐘"            # 雜音的字不算進去
    assert joined.merged_into == {0: 0, 2: 0}                                                  # 雜音的輪沒有對應
    assert joined.chains == (a.Chain("S1", (0, 2), gaps=(1,), gap_chars=1),)


def test_the_noise_may_be_made_of_several_turns_as_long_as_it_is_short_in_all():
    turns = make_turns(("S1", "第三部分候選人結論"), ("S3", "嗯"), ("S3", "啊"), ("S1", "論時間每位候選人各12分鐘"))

    joined = a.join_adjacent(turns, CITABLE)

    assert joined.chains == (a.Chain("S1", (0, 3), gaps=(1, 2), gap_chars=2),)


def test_the_noise_is_measured_in_characters_without_punctuation_or_spaces():
    exactly = "一二三四五六"[:a.JOIN_GAP_CHARS]
    turns = make_turns(("S1", "第三部分候選人結論"), ("S3", " ".join(exactly) + "。"), ("S1", "論時間每位候選人各12分鐘"))

    assert a.join_adjacent(turns, CITABLE).chains[0].gap_chars == a.JOIN_GAP_CHARS


def test_more_than_the_allowed_noise_is_a_real_interruption():
    turns = make_turns(("S1", "第三部分候選人結論"), ("S3", "一二三四五六七"[:a.JOIN_GAP_CHARS + 1]),
                       ("S1", "論時間每位候選人各12分鐘"))

    joined = a.join_adjacent(turns, CITABLE)

    assert joined.turns == turns and joined.chains == ()
    assert joined.merged_into == {0: 0, 1: 1, 2: 2}


def test_the_noise_of_several_turns_counts_together():
    turns = make_turns(("S1", "第三部分候選人結論"), ("S3", "一二三"), ("S3", "四五六七"), ("S1", "論時間每位候選人各12分鐘"))

    assert a.join_adjacent(turns, CITABLE).chains == ()


def test_different_speakers_are_never_joined():
    turns = make_turns(("S1", "我們今年的預算"), ("S2", "大約增加了兩成"), ("S1", "這樣的幅度不低"))

    joined = a.join_adjacent(turns, CITABLE)

    assert joined.turns == turns and joined.chains == ()


def test_a_short_other_voice_between_two_different_speakers_stays():
    turns = make_turns(("S1", "我們今年的預算"), ("S3", "嗯"), ("S2", "大約增加了兩成"))

    joined = a.join_adjacent(turns, CITABLE)

    assert joined.turns == turns and joined.chains == ()


def test_other_voices_are_never_joined_with_each_other():
    turns = make_turns(("S3", "我是聽眾"), ("S3", "我想問一件事"))

    assert a.join_adjacent(turns, CITABLE).turns == turns


def test_a_short_other_voice_at_the_edge_stays_where_it_is():
    turns = make_turns(("S3", "嗯"), ("S1", "我們今年的預算"), ("S1", "大約增加了兩成"), ("S3", "好"))

    joined = a.join_adjacent(turns, CITABLE)

    assert [(t.index, t.speaker) for t in joined.turns] == [(0, "S3"), (1, "S1"), (3, "S3")]
    assert joined.merged_into == {0: 0, 1: 1, 2: 1, 3: 3}


def test_a_run_of_three_turns_of_one_speaker_is_one_chain():
    turns = make_turns(("S1", "甲甲甲甲"), ("S1", "乙乙乙乙"), ("S3", "嗯"), ("S1", "丙丙丙丙"))

    joined = a.join_adjacent(turns, CITABLE)

    assert [(t.index, t.text) for t in joined.turns] == [(0, "甲甲甲甲 乙乙乙乙 丙丙丙丙")]
    assert joined.chains == (a.Chain("S1", (0, 1, 3), gaps=(2,), gap_chars=1),)


def test_turns_that_were_not_joined_are_returned_as_they_were():
    turns = make_turns(("S1", "我們今年的預算"), ("S2", "大約增加了兩成"), ("S1", "這樣的幅度不低"), ("S1", "而且還在成長"))

    joined = a.join_adjacent(turns, CITABLE)

    assert joined.turns[0] is turns[0] and joined.turns[1] is turns[1]


def test_nothing_to_join_in_an_empty_window():
    joined = a.join_adjacent((), CITABLE)

    assert joined.turns == () and joined.chains == () and dict(joined.merged_into) == {}


def test_joining_never_loses_the_words_of_the_speakers():
    """攔的 bug：併的時候掉了字（或重複了字），之後再算的結果就和真的不一樣。隨機的對談，每個可以引用的字都還在。"""
    rng = random.Random(7)
    for _ in range(200):
        lines = tuple((rng.choice(("S1", "S1", "S2", "S3")), "".join(rng.choice("甲乙丙丁戊己") for _ in range(rng.randint(1, 9))))
                      for _ in range(rng.randint(1, 12)))
        turns = make_turns(*lines)

        joined = a.join_adjacent(turns, CITABLE)

        before = "".join(squash(t.text) for t in turns if t.speaker in CITABLE)
        after = "".join(squash(t.text) for t in joined.turns if t.speaker in CITABLE)
        assert before == after
        assert [t.index for t in joined.turns] == sorted({t.index for t in joined.turns})
        assert set(joined.merged_into.values()) <= {t.index for t in joined.turns}
        for chain in joined.chains:
            assert chain.members[0] == joined.merged_into[chain.members[-1]]
            assert all(turns[i].speaker == chain.speaker for i in chain.members)
            assert all(turns[i].speaker not in CITABLE for i in chain.gaps)


# --- 跨輪的 quote 是誰和誰拼的 ---


@pytest.mark.parametrize(("speakers", "pattern"), [
    (("S1", "S1"), a.SPLICE_SAME),
    (("S2", "S2", "S2"), a.SPLICE_SAME),
    (("S1", "S3", "S1"), a.SPLICE_NOISE),
    (("S1", "S2"), a.SPLICE_CROSS),
    (("S1", "S2", "S1"), a.SPLICE_CROSS),
    (("S1", "S1", "S2"), a.SPLICE_CROSS),
    (("S3", "S1"), a.SPLICE_OTHER),
    (("S1", "S3"), a.SPLICE_OTHER),
    (("S1", "S1", "S3"), a.SPLICE_OTHER),
    (("S3", "S3"), a.SPLICE_OTHER),
])
def test_the_speakers_of_a_spliced_quote_say_what_kind_of_splice_it_is(speakers, pattern):
    run = make_turns(*((speaker, "嗯" if speaker == "S3" else "一些話在這裡說") for speaker in speakers))

    assert a.splice_pattern(run, CITABLE) == pattern


def test_a_long_interruption_between_the_same_speaker_is_not_noise():
    run = make_turns(("S1", "第三部分候選人結論"), ("S3", "一二三四五六七"[:a.JOIN_GAP_CHARS + 1]), ("S1", "論時間每位候選人"))

    assert a.splice_pattern(run, CITABLE) == a.SPLICE_INTERRUPTED


def test_the_noise_limit_for_a_splice_is_the_same_one_joining_uses():
    """攔的 bug：排列說是雜音、併輪卻不併（或反過來），兩個數字就對不起來。"""
    for length in range(1, a.JOIN_GAP_CHARS + 3):
        run = make_turns(("S1", "第三部分候選人結論"), ("S3", "甲" * length), ("S1", "論時間每位候選人"))

        merged = bool(a.join_adjacent(run, CITABLE).chains)

        assert (a.splice_pattern(run, CITABLE) == a.SPLICE_NOISE) == merged


# --- 問句字眼：是哪一個字眼讓重點被當成問句 ---

LONG_NO_PUNCT = ("我們的高地有了自來水阿伯打開水龍頭流出水的時候他掉眼淚了我也哭了這種等待一件微小的事"
                 "但是卻對你日常生活影響這麼重大的事過去為什麼沒有人做但沒有人去做")


def triggers(turn_text: str, claim_text: str, strict: bool = False):
    return a.question_triggers(turn_text, claim_text, strict)


def test_a_real_question_mark_makes_every_question_word_in_that_clause_count():
    found = triggers("請問今年的預算有沒有增加？", "詢問今年的預算有沒有增加")

    assert {(t.word, t.form) for t in found} == {("請問", a.FORM_MARKED), ("有沒有", a.FORM_MARKED), ("？", a.FORM_MARKED)}


def test_a_short_clause_without_a_mark_counts_by_its_question_word_alone():
    found = triggers("請問今年的預算有沒有增加", "詢問今年的預算有沒有增加")

    assert {(t.word, t.form) for t in found} == {("請問", a.FORM_SHORT), ("有沒有", a.FORM_SHORT)}


def test_a_long_clause_counts_only_for_a_word_near_what_the_point_says():
    found = triggers(LONG_NO_PUNCT, "追究過去為什麼沒有人做這件事")

    assert [(t.word, t.form) for t in found] == [("為什麼", a.FORM_NEAR)]


def test_a_long_clause_whose_question_word_is_far_from_the_point_has_no_trigger():
    """第一次真實診斷的例 3：「為什麼」在這一輪的最後面，重點講的是前面的自來水，不是那句問句。"""
    assert triggers(LONG_NO_PUNCT, "解決高地自來水問題，改善居民日常生活品質") == ()


def test_a_strict_check_counts_the_far_word_too_and_says_so():
    found = triggers(LONG_NO_PUNCT, "解決高地自來水問題，改善居民日常生活品質", strict=True)

    assert [(t.word, t.form) for t in found] == [("為什麼", a.FORM_STRICT)]


def test_the_trigger_is_from_the_clause_that_carries_the_point_not_from_another_one():
    turn = "這樣公平嗎？預算今年大約增加了兩成左右，而且明年還會繼續增加"

    assert triggers(turn, "預算今年大約增加了兩成左右") == ()


def test_nothing_in_common_with_the_point_counts_the_whole_stretch():
    found = triggers("請問你覺得天氣如何呢", "完全不相干的另一件事情")

    assert {t.form for t in found} == {a.FORM_UNMATCHED} and {t.word for t in found} == {"請問", "呢"}


def test_no_question_word_no_trigger():
    assert triggers("今年的預算大約增加了兩成左右", "預算大約增加了兩成") == ()
    assert triggers("", "預算大約增加了兩成") == ()


def test_the_same_word_is_reported_once_per_form():
    found = triggers("請問請問今年的預算有沒有增加？", "詢問今年的預算有沒有增加")

    assert len(found) == len(set(found))


def test_a_clause_with_exactly_half_of_the_best_clauses_share_of_the_point_still_carries_it():
    """攔的 bug：「帶著重點的子句」的門檻差一點（剛好一半就被當成沒有）：真的檢查算它是問句，這裡卻說沒有觸發的字眼。
    重點「預算今」和兩個子句共用的字組：「預算嗎」1 個、「預算今天」2 個，剛好一半。"""
    turn, claim = "預算嗎，預算今天", "預算今"

    assert c._evidence(turn, c._grams(claim))[1] is True
    assert triggers(turn, claim) == (a.QuestionTrigger("嗎", a.FORM_SHORT),)


def test_a_long_clause_lists_only_the_question_words_that_sit_next_to_what_the_point_says():
    """攔的 bug：很長的子句只要有一個字眼在重點附近，就把整句每個字眼都列成觸發：離重點很遠的「嗎」也被算進去，
    讀的人會以為是它害的。"""
    turn, claim = "你覺得這樣可以嗎" + LONG_NO_PUNCT, "追究過去為什麼沒有人做這件事"

    assert c._evidence(turn, c._grams(claim))[1] is True
    assert [(t.word, t.form) for t in triggers(turn, claim)] == [("為什麼", a.FORM_NEAR)]


def _random_case(rng: random.Random):
    pool = ("請問今年的預算有沒有增加", "預算大約增加了兩成左右", "為什麼過去沒有人做這件事", "這個政策是不是有效呢",
            "我們的高地有了自來水阿伯打開水龍頭流出水的時候他掉眼淚了我也哭了這種等待一件微小的事",
            "明年預計再增加一成而已", "對吧", "好不好", "托育服務在偏鄉很缺乏", "那為什麼你不去做")
    parts = [rng.choice(pool) for _ in range(rng.randint(1, 5))]
    seps = ("，", "。", " ", "？", "", "！")
    turn = "".join(part + rng.choice(seps) for part in parts)
    source = rng.choice(parts)
    start = rng.randint(0, max(0, len(source) - 6))
    claim = rng.choice(("詢問", "指出", "")) + source[start:start + rng.randint(5, 14)]
    if rng.random() < 0.1:
        claim = "完全不相干的另一件事情"                        # 沒有任何一句和重點共用字
    return turn, claim, rng.random() < 0.3


def test_the_triggers_agree_with_the_check_on_whether_a_point_is_taken_for_a_question():
    """攔的 bug：這裡自己判斷哪些字眼算數，卻和真的檢查（conversation._evidence）不一致，診斷就會說了一個真的檢查
    沒有用過的原因。隨機的一輪話與重點，兩邊一定同時有或同時沒有問句。"""
    rng = random.Random(11)
    for _ in range(800):
        turn, claim, strict = _random_case(rng)

        real = c._evidence(turn, c._grams(claim), strict_question=strict)[1]

        assert bool(triggers(turn, claim, strict)) == real, (turn, claim, strict)


# --- 最長的一段逐字吻合（C）---


def test_the_longest_verbatim_stretch_of_a_quote_with_an_invented_head_is_found():
    found = a.longest_segment(HEAD + BODY, C_TURNS, CITABLE)

    assert found == a.Segment(C_TURNS[1], BODY)


def test_the_stretch_is_taken_from_the_transcript_with_its_punctuation():
    turns = make_turns(("S2", "請問您怎麼看"), ("S1", "正民主法治的國家，這個時候你要去用人家的私有權。來做這件事情"))

    found = a.longest_segment("他說正民主法治的國家這個時候你要去用人家的私有權", turns, CITABLE)

    assert found is not None and found.text == "正民主法治的國家，這個時候你要去用人家的私有權"


def test_the_stretch_must_be_long_enough_to_recognise_the_turn():
    body = "正民主法治的國家這個時候"[:a.LONGEST_SEGMENT_MIN_CHARS]
    turns = make_turns(("S2", "請問您怎麼看"), ("S1", body + "來做這件事情"))

    assert a.longest_segment("我覺得啊其實" + body, turns, CITABLE) is not None
    assert a.longest_segment("我覺得啊其實" + body[:-1], turns, CITABLE) is None


def test_a_stretch_that_two_turns_have_cannot_say_who_said_it():
    for other in ("S1", "S2", "S3"):               # 另一輪是誰說的都一樣：包括不能引用的聲音
        turns = make_turns(("S2", "請問您怎麼看都更的問題"), ("S1", BODY + "來做這件事情"), (other, "我同意" + BODY))

        assert a.longest_segment(HEAD + BODY, turns, CITABLE) is None


def test_a_stretch_mostly_from_another_voice_is_not_given_to_a_speaker():
    turns = make_turns(("S2", "請問您怎麼看都更的問題"), ("S3", BODY + "來做這件事情"), ("S1", BODY[:14] + "所以才這樣"))

    assert a.longest_segment(HEAD + BODY, turns, CITABLE) is None


def test_two_turns_with_equally_long_stretches_are_a_tie():
    first, second = "正民主法治的國家這個時候", "你要去用人家的私有權來做"
    turns = make_turns(("S1", first + "後面有別的話"), ("S2", "前面也有話" + second), ("S1", "好的謝謝"))

    assert len(first) == len(second) == 12
    assert a.longest_segment(first + second, turns, CITABLE) is None


def test_the_longer_of_two_stretches_wins():
    first, second = "正民主法治的國家這個時候你要去", "用人家的私有權來做這件事"
    turns = make_turns(("S1", first + "後面有別的話"), ("S2", "前面也有話" + second), ("S1", "好的謝謝"))

    found = a.longest_segment(first + second, turns, CITABLE)

    assert found == a.Segment(turns[0], first)


@pytest.mark.parametrize("quote", ["", "   ", "，。", None, 42])
def test_nothing_usable_in_a_quote_gives_no_stretch(quote):
    assert a.longest_segment(quote, C_TURNS, CITABLE) is None


# --- 用真的檢查重跑 ---


def test_a_point_whose_quote_has_an_invented_head_would_pass_on_its_longest_stretch():
    (drop,) = dropped(C_TURNS, C_POINT)
    assert drop.code == c.BAD_QUOTE                                     # 真的檢查丟掉它

    (ctx,) = contexts(C_TURNS)

    assert a.try_longest_segment(ctx, drop) == a.Rerun(True, turn=1)


def test_the_other_checks_still_apply_to_the_longest_stretch():
    (drop,) = dropped(C_TURNS, RawPoint(1, "指出三成的國家需要尊重私有權", quote=HEAD + BODY))
    (ctx,) = contexts(C_TURNS)

    assert a.try_longest_segment(ctx, drop) == a.Rerun(False, c.NUMBER_MISMATCH)


def test_no_usable_stretch_means_nothing_to_rerun():
    (drop,) = dropped(C_TURNS, RawPoint(1, C_TEXT, quote="這一句話完全是模型自己編出來的內容啊"))
    (ctx,) = contexts(C_TURNS)

    assert a.try_longest_segment(ctx, drop) is None


def test_the_rerun_follows_the_mode_the_run_was_in():
    turns = make_turns(("S2", "請問您怎麼看今年的預算"), ("S1", "他們今年的預算大約增加了兩成左右來做這件事情"), ("S2", "好的謝謝"))
    point = RawPoint(1, "預算今年大約增加了兩成左右", quote="我覺得啊其實他們今年的預算大約增加了兩成左右")
    (drop,) = dropped(turns, point)
    assert drop.code == c.BAD_QUOTE

    (normal,) = contexts(turns)
    (quote_only,) = contexts(turns, quote_only=True)

    assert a.try_longest_segment(normal, drop) == a.Rerun(True, turn=1)
    assert a.try_longest_segment(quote_only, drop) == a.Rerun(False, c.BAD_QUOTE)           # 原話裡有「他們」，不能直接顯示


def test_a_dropped_point_rerun_with_its_own_quote_is_dropped_for_the_same_reason():
    """攔的 bug：重跑和真的檢查差了一點什麼（窗的範圍、稱呼名單、前一窗的尾巴…），「會通過」就不可信了。
    每一種被丟掉的點，用自己的 quote 重跑一次，原因要一模一樣。"""
    lines = (("S1", "請問今年的預算編列有沒有增加"), ("S2", "今年的預算大約增加了兩成左右"), ("S1", "那明年的預算打算怎麼編列呢"),
             ("S2", "明年預計再增加一成左右而已"), ("S3", "我是打電話進來的聽眾想問一件事"))
    turns = make_turns(*lines)
    points = (
        RawPoint(0, "詢問預算", quote="請問今年的預算編列有沒有增加，然後全面檢討所有補助項目"),
        RawPoint(0, "詢問聽眾的事", quote=lines[4][1]),
        RawPoint(0, "談論氣候變遷政策", quote="今年的氣候變遷政策完全沒有進展"),
        RawPoint(1, "他認為預算增加了兩成", quote=lines[1][1]),
        RawPoint(1, "我認為預算增加了兩成", quote=lines[1][1]),
        RawPoint(1, "預算增加了五成", quote=lines[1][1]),
        RawPoint(2, "預算大約增加了兩成左右", quote=lines[3][1]),
        RawPoint(2, "明年的預算打算要編列", quote=lines[2][1]),
        RawPoint(1, "談預", quote=lines[1][1]),
        RawPoint(4, "談論來電的聽眾想問一件事", quote=lines[4][1]),
        RawPoint(99, "談論今年的預算增加", quote=lines[1][1]),
        RawPoint(1, "今年預算大約增加了兩成左右", quote=lines[1][1]),            # 這個會通過
    )
    drops = [d for d in dropped(turns, *points) if d.code not in (c.DUPLICATE_POINT, c.SLIDE_TOO_SMALL)]
    assert {d.code for d in drops} >= {c.BAD_QUOTE, c.LEADING_PRONOUN_OR_TITLE, c.FIRST_PERSON, c.NUMBER_MISMATCH,
                                       c.ANCHOR_CONFLICT, c.QUESTION_STATED_AS_FACT, c.TOO_SHORT, c.CITES_X_OR_MINOR,
                                       c.TURN_OUT_OF_WINDOW}
    (ctx,) = contexts(turns)

    for drop in drops:
        again = a.rerun_with_quote(ctx, drop, drop.quote)
        if drop.code == c.TURN_OUT_OF_WINDOW:
            assert again is None                                        # 引用的輪不在這個窗裡：沒有東西可以重跑
        else:
            assert again == a.Rerun(False, drop.code), drop


def test_the_names_of_the_window_are_known_when_rerunning():
    """攔的 bug：重跑少了這一窗的稱呼名單：點裡有名字（真的檢查丟掉：name_in_text），重跑卻說會通過。"""
    turns = make_turns(("S1", "小安，你覺得這次的調整會不會影響營運？"),
                       ("S2", "會有影響，營運虧損已經連續兩季，卞教授也提醒過這件事。"), ("S1", "好的謝謝"))
    point = RawPoint(1, "小安要先評估營運影響", quote="會有影響，營運虧損已經連續兩季")
    (drop,) = dropped(turns, point)
    assert drop.code == c.NAME_IN_TEXT
    (ctx,) = contexts(turns)

    assert a.rerun_with_quote(ctx, drop, drop.quote) == a.Rerun(False, c.NAME_IN_TEXT)


def test_the_end_of_the_previous_window_still_counts_when_rerunning_the_first_turns_of_the_next_one():
    """窗不重疊，窗頭的複述要看前一窗的尾巴；重跑也要，不然窗頭的點會被說成「會通過」。"""
    lines = [("S1", "今天來談這個議題，先請教您的看法。"),
             ("S2", "這個稅制改革會讓中小企業多繳兩成的稅，我們強烈反對。"),
             ("S1", "您剛才提到中小企業多繳兩成的稅。")]
    turns = make_turns(*lines)
    windows = [Window(0, turns[:2]), Window(1, turns[2:])]
    point = RawPoint(2, "指出中小企業多繳兩成的稅", quote="您剛才提到中小企業多繳兩成的稅")
    (drop,) = dropped(turns, point, window=windows[1])
    assert drop.code == c.ECHO

    first, second = contexts(turns, windows)

    assert second.lead_in == turns[:2] and first.lead_in == ()
    assert a.rerun_with_quote(second, drop, drop.quote) == a.Rerun(False, c.ECHO)


# --- 併輪（D）---


def test_a_quote_split_over_two_turns_of_one_speaker_would_pass_when_the_turns_are_joined():
    (drop,) = dropped(D_TURNS, D_POINT)
    assert drop.code == c.BAD_QUOTE
    (ctx,) = contexts(D_TURNS)

    assert a.try_joined_turns(ctx, drop) == a.Rerun(True, turn=1)


def test_the_point_may_cite_the_second_of_the_joined_turns():
    (drop,) = dropped(D_TURNS, RawPoint(2, D_TEXT, quote=D_QUOTE))
    (ctx,) = contexts(D_TURNS)

    assert a.try_joined_turns(ctx, drop) == a.Rerun(True, turn=1)


def test_joining_agrees_with_running_the_whole_check_on_the_joined_transcript():
    """攔的 bug：重跑單一個點，和把併過的逐字稿整個交給 validate_points 的結果不一樣。"""
    (drop,) = dropped(D_TURNS, D_POINT, D_OTHER)
    (ctx,) = contexts(D_TURNS)
    joined = a.join_adjacent(D_TURNS, CITABLE)

    result = c.validate_points([RawSlide("交通", (D_POINT, D_OTHER))], joined.turns, SPEAKERS, Window(0, joined.turns))

    assert a.try_joined_turns(ctx, drop) == a.Rerun(True, turn=1)
    assert result.kept == 2 and sorted(p.turn for s in result.slides for p in s.points) == [1, 4]


def test_a_one_word_noise_between_the_halves_is_stepped_over():
    (drop,) = dropped(NOISE_TURNS, D_POINT)
    assert drop.code == c.BAD_QUOTE
    (ctx,) = contexts(NOISE_TURNS)

    assert a.try_joined_turns(ctx, drop) == a.Rerun(True, turn=1)


def test_two_speakers_words_are_not_joined_so_a_cross_speaker_splice_stays_dropped():
    (drop,) = dropped(CROSS_TURNS, D_POINT)
    (ctx,) = contexts(CROSS_TURNS)

    assert a.try_joined_turns(ctx, drop) is None                          # 這一窗沒有可以併的輪


def test_joined_turns_still_go_through_every_other_check():
    (drop,) = dropped(D_TURNS, RawPoint(1, "優先處理淡水捷運延伸線並在五年內完成規劃", quote=D_QUOTE))
    (ctx,) = contexts(D_TURNS)

    assert a.try_joined_turns(ctx, drop) == a.Rerun(False, c.NUMBER_MISMATCH)


def test_a_window_with_something_to_join_still_says_dropped_when_the_quote_is_not_there_at_all():
    (drop,) = dropped(D_TURNS, RawPoint(1, D_TEXT, quote="這一句話完全是模型自己編出來的內容啊"))
    (ctx,) = contexts(D_TURNS)

    assert a.try_joined_turns(ctx, drop) == a.Rerun(False, c.BAD_QUOTE)


def test_a_point_that_cites_a_turn_outside_the_window_cannot_be_rerun():
    (ctx,) = contexts(D_TURNS)
    stray = c.DroppedPoint(c.BAD_QUOTE, "找不到", D_QUOTE, D_TEXT, 999)
    unknown = c.DroppedPoint(c.BAD_QUOTE, "找不到", D_QUOTE, D_TEXT, None)

    for drop in (stray, unknown):
        assert a.try_joined_turns(ctx, drop) is None and a.try_longest_segment(ctx, drop) is None
        assert a.rerun_with_quote(ctx, drop, drop.quote) is None


# --- 通過了是歸給哪一輪、併了哪幾輪 ---


def test_a_rerun_that_passes_says_which_turn_the_point_was_given_to():
    """C：模型引用的是 T2，quote 的那一段其實在 T1：重跑之後歸給 T1，和真的檢查一樣。"""
    (drop,) = dropped(C_TURNS, RawPoint(2, C_TEXT, quote=HEAD + BODY))
    (ctx,) = contexts(C_TURNS)

    assert a.try_longest_segment(ctx, drop) == a.Rerun(True, turn=1)


def test_a_failed_rerun_has_no_turn():
    (drop,) = dropped(C_TURNS, RawPoint(1, "指出三成的國家需要尊重私有權", quote=HEAD + BODY))
    (ctx,) = contexts(C_TURNS)

    assert a.try_longest_segment(ctx, drop).turn is None


def test_a_joined_rerun_is_given_to_the_first_of_the_joined_turns():
    (drop,) = dropped(BOTH_TURNS, BOTH_POINT)
    (ctx,) = contexts(BOTH_TURNS)

    assert a.try_joined_turns(ctx, drop) == a.Rerun(True, turn=1)
    assert a.chain_for(ctx.joined, 1) == a.Chain("S1", (1, 2))


def test_the_chain_a_turn_was_joined_into_can_be_found_from_any_of_its_members():
    joined = a.join_adjacent(D_TURNS, CITABLE)

    assert a.chain_for(joined, 1) == a.chain_for(joined, 2) == a.Chain("S1", (1, 2))


def test_a_turn_that_was_not_joined_into_anything_has_no_chain():
    joined = a.join_adjacent(D_TURNS, CITABLE)

    assert a.chain_for(joined, 4) is None and a.chain_for(joined, 999) is None


def test_the_chain_remembers_the_noise_it_stepped_over():
    joined = a.join_adjacent(NOISE_TURNS, CITABLE)

    assert a.chain_for(joined, 1) == a.Chain("S1", (1, 3), (2,), 1)


# --- 夾在中間的其他聲音有幾個字 ---


def test_the_noise_between_the_ends_of_a_run_is_counted_in_characters_without_marks_and_spaces():
    run = make_turns(("S1", "優先處理淡水捷運延伸線預計"), ("S3", "嗯 ，"), ("S1", "三年內完成規劃"))

    assert a.gap_chars(run) == 1


def test_a_run_with_nothing_in_the_middle_has_no_noise():
    assert a.gap_chars(make_turns(("S1", "優先處理淡水捷運延伸線"), ("S1", "預計三年內完成規劃"))) == 0
    assert a.gap_chars(make_turns(("S1", "優先處理淡水捷運延伸線"))) == 0


# --- 問句：是哪一個字眼、用什麼條件算數的（用被丟掉的那一輪、算進複述的數字）---


def cause_of(turns, point):
    (drop,) = dropped(turns, point)
    assert drop.code == c.QUESTION_STATED_AS_FACT, drop                  # 真的檢查把它當成問句
    (ctx,) = contexts(turns)
    return a.question_cause(ctx, drop, turns[point.turn])


def test_the_question_cause_of_a_short_clause_is_its_question_word():
    assert cause_of(Q_SHORT_TURNS, Q_SHORT_POINT) == (a.QuestionTrigger("呢", a.FORM_SHORT),)


def test_the_question_cause_of_a_long_clause_is_the_word_near_what_the_point_says():
    assert cause_of(Q_NEAR_TURNS, Q_NEAR_POINT) == (a.QuestionTrigger("為什麼", a.FORM_NEAR),)


def test_a_number_the_other_speaker_just_said_makes_the_cause_strict():
    """真實影片的另一種：「月費2980喔…你不怕…嗎」——數字是對方剛說的，字眼離重點再遠也算。"""
    assert cause_of(Q_STRICT_TURNS, Q_STRICT_POINT) == (a.QuestionTrigger("嗎", a.FORM_STRICT),)


def test_the_same_number_said_long_ago_does_not_make_the_cause_strict():
    far = make_turns(("S2", "我們的月費是2980元"), *[("S1", "好的我知道了") if n % 2 else ("S2", "請繼續說明") for n in range(9)],
                     ("S1", Q_STRICT_TURNS[1].text), ("S2", "好的謝謝您的說明"))
    point = c.DroppedPoint(c.QUESTION_STATED_AS_FACT, "", Q_STRICT_POINT.quote, Q_STRICT_POINT.text, 10)
    (ctx,) = contexts(far)

    assert a.rerun_with_quote(ctx, point, point.quote) == a.Rerun(True, turn=10)     # 數字太久以前說的：「嗎」離重點太遠，真的檢查不算問句
    assert a.question_cause(ctx, point, far[10]) == ()


def test_the_question_cause_looks_back_into_the_turns_before_this_window():
    """窗不重疊：窗頭的那一輪，對方剛說的數字在前一窗的尾巴；真的檢查看得到，這裡也要。"""
    turns = Q_STRICT_TURNS
    first, second = Window(0, tuple(turns[:1])), Window(1, tuple(turns[1:]))
    (drop,) = dropped(turns, Q_STRICT_POINT, window=second)
    assert drop.code == c.QUESTION_STATED_AS_FACT
    _, ctx = a.window_contexts([first, second], CITABLE)

    assert a.question_cause(ctx, drop, turns[1]) == (a.QuestionTrigger("嗎", a.FORM_STRICT),)


def test_the_question_cause_looks_back_as_far_as_the_check_does():
    """攔的 bug：往前看的輪數比真的檢查少一輪：對方的數字剛好在最遠的那一輪，真的檢查說「剛說過、不看距離」，這裡卻
    說離重點太遠、沒有觸發的字眼。"""
    turns = make_turns(("S2", "我們的月費是2980元"), *[("S1", "好的我知道了")] * (c.ANCHOR_RADIUS - 1),
                       ("S1", Q_STRICT_TURNS[1].text), ("S2", "好的謝謝您的說明"))
    first, second = Window(0, tuple(turns[:c.ANCHOR_RADIUS])), Window(1, tuple(turns[c.ANCHOR_RADIUS:]))
    point = RawPoint(c.ANCHOR_RADIUS, Q_STRICT_POINT.text, quote=Q_STRICT_POINT.quote)
    (drop,) = dropped(turns, point, window=second)
    assert drop.code == c.QUESTION_STATED_AS_FACT
    _, ctx = a.window_contexts([first, second], CITABLE)

    assert ctx.lead_in == turns[:c.ANCHOR_RADIUS]
    assert a.question_cause(ctx, drop, turns[c.ANCHOR_RADIUS]) == (a.QuestionTrigger("嗎", a.FORM_STRICT),)
