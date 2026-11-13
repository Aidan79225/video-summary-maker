"""多人歸屬的各項檢查，一項一項單獨測：問句（V2）、內容出處（anchor）、改用原話（hybrid）、
整段文字的代名詞、安全模式、計數與分類、每頁上限、穩定的原因碼。

端到端的失敗模式（FINAL 的 error taxonomy）在 test_attribution_taxonomy.py；
這裡是各個檢查的邊界。
"""
from __future__ import annotations

import random
from dataclasses import replace

import pytest

from slidebox.domain.entities import RawPoint, RawSlide, Turn
from slidebox.usecases import conversation as conv
from slidebox.usecases.conversation import (
    ANCHOR_CONFLICT,
    BAD_QUOTE,
    CONTRADICTED,
    CROSS_SPEAKER_TIE,
    DUPLICATE_POINT,
    MAX_BULLETS,
    NUMBER_MISMATCH,
    SLIDE_RULE,
    SLIDE_TOO_SMALL,
    SOURCE_TURN_NOT_CITABLE,
    UNSUPPORTED,
    UNVERIFIABLE,
    Problem,
    parse_quote_only,
    problem_class,
    validate_points,
)
from slidebox.usecases.quotes import MAX_RESCUED_QUOTE_CHARS

from .conversation_support import GOOD, SPEAKERS, TURNS, WHOLE, at, problems_of, run, with_anchors
from .speaker_fixtures import make_speakers, make_turns

CITABLE = frozenset({"S1", "S2"})


def codes(result) -> dict[str, int]:
    return dict(result.dropped_by_code)


# --- 沒有共同用字的假資料：要幾個相符的特徵，就給幾個 ---

SEQ = "天地玄黃宇宙洪荒日月盈昃辰宿列張"          # 16 個互不相同、都不是虛字的字：n 個字 = n-1 個兩字一組


def claim_of(n: int) -> frozenset[str]:
    return conv._grams(SEQ[:n])


def talk(index: int, speaker: str, shared: int) -> Turn:
    """和 claim_of(n) 剛好有 shared 個相同特徵的一輪（逗號把共同的字和不相干的字隔開）。"""
    text = (SEQ[:shared + 1] + "，" if shared else "") + "春夏秋冬"
    return Turn(index, float(index), float(index) + 0.9, speaker, text)


# --- V2：這是不是一句問句——只看帶著重點的句子 ---


def evidence(turn_text: str, claim_text: str) -> tuple[str, bool]:
    return conv._evidence(turn_text, conv._grams(claim_text))


def test_a_reported_question_beside_the_claim_does_not_make_the_claim_a_question():
    flag = evidence("每年檢討一次，有人問預算有沒有公開？預算完全公開。", "每年檢討一次，預算完全公開")[1]

    assert flag is False


def test_a_question_in_the_clause_that_carries_the_claim_is_a_question():
    assert evidence("預算有沒有公開呢？", "預算公開")[1] is True


def test_when_no_clause_shares_anything_with_the_claim_the_whole_turn_decides():
    assert evidence("是不是這樣呢？", "完全不同的內容")[1] is True
    assert evidence("完全沒有疑問的一句話。", "預算公開")[1] is False


def test_a_turn_without_any_question_wording_is_never_a_question():
    assert evidence("預算完全公開，成效每年檢討。", "預算公開成效檢討")[1] is False


def test_the_flag_can_only_be_turned_off_never_on():
    """整段沒有問句字眼就一定不是問句：帶著重點的句子再怎麼挑，也不會多出一個問句。"""
    rng = random.Random(3)
    pool = "預算成效公開檢討期程補助托育名額加倍，。！"
    for _ in range(400):
        turn = "".join(rng.choice(pool) for _ in range(rng.randint(2, 40)))
        claim = "".join(rng.choice(pool) for _ in range(rng.randint(2, 12)))
        if not conv._QUESTION_RE.search(turn):
            assert evidence(turn, claim)[1] is False


@pytest.mark.parametrize("scores, question_at, expected", [
    ([4, 2, 1], 1, True),            # 2 × 2 >= 4：這句也算帶著重點
    ([4, 2, 1], 2, False),           # 1 × 2 < 4：不算
    ([4, 2, 1], 0, True),
    ([0, 0, 0], 2, True),            # 沒有任何一句和重點共用字：照舊看整段
    ([3, 0, 0], 1, False),
])
def test_only_clauses_with_at_least_half_the_best_overlap_carry_the_claim(scores, question_at, expected):
    clauses = [("甲甲", "，"), ("乙乙", "，"), ("丙丙", "。")]
    clauses[question_at] = (clauses[question_at][0] + "嗎", clauses[question_at][1])

    assert conv._carries_question(clauses, scores) is expected


def test_the_evidence_is_a_stretch_of_the_turn_of_at_most_eighty_characters():
    rng = random.Random(8)
    pool = "預算成效公開檢討期程補助托育名額加倍"
    for _ in range(300):
        clauses = ["".join(rng.choice(pool) for _ in range(rng.randint(2, 50))) for _ in range(rng.randint(1, 6))]
        turn = "，".join(clauses) + "。"
        claim = "".join(rng.choice(pool) for _ in range(rng.randint(2, 10)))

        text, _ = evidence(turn, claim)

        assert len(text) <= conv.MAX_EVIDENCE_CHARS
        assert text.rstrip("…") in turn


# --- 內容的出處（anchor）：quote 所在的那一輪是不是真的有這個重點 ---


def test_a_claim_needs_two_shared_features_and_a_quarter_of_its_own_features():
    claim = claim_of(13)                                 # 12 個特徵
    enough = talk(0, "S1", 3)                            # 3 / 12 = 25%：剛好夠
    too_few = talk(0, "S1", 2)                           # 2 / 12：不夠

    assert conv._pick_anchor(enough, claim, [enough], CITABLE) is enough
    problem = conv._pick_anchor(too_few, claim, [too_few], CITABLE)
    assert isinstance(problem, Problem) and problem.code == UNSUPPORTED and "T0" in problem.message


def test_one_shared_feature_is_never_enough_however_short_the_claim():
    claim = claim_of(3)                                  # 2 個特徵
    one = talk(0, "S1", 1)
    both = talk(0, "S1", 2)

    assert conv._pick_anchor(one, claim, [one], CITABLE).code == UNSUPPORTED
    assert conv._pick_anchor(both, claim, [both], CITABLE) is both


def test_the_quote_turn_wins_when_it_is_within_the_margin_of_the_best():
    claim = claim_of(16)                                 # 15 個特徵，差距 1.5
    quote_turn, better = talk(0, "S1", 6), talk(1, "S1", 7)

    assert conv._pick_anchor(quote_turn, claim, [quote_turn, better], CITABLE) is quote_turn


def test_a_clearly_better_turn_of_the_same_speaker_is_returned_as_the_source():
    claim = claim_of(16)
    quote_turn, better = talk(0, "S1", 5), talk(1, "S1", 7)

    assert conv._pick_anchor(quote_turn, claim, [quote_turn, better], CITABLE) is better


def test_two_speakers_that_fit_equally_well_cannot_be_told_apart():
    claim = claim_of(16)
    quote_turn, left, right = talk(0, "S1", 3), talk(2, "S1", 8), talk(3, "S2", 8)

    problem = conv._pick_anchor(quote_turn, claim, [quote_turn, left, right], CITABLE)

    assert isinstance(problem, Problem) and problem.code == CROSS_SPEAKER_TIE
    assert "T2、T3" in problem.message and "無法確定是誰說的" in problem.message


def test_a_best_turn_of_a_voice_that_cannot_be_cited_is_not_a_source():
    claim = claim_of(16)
    quote_turn, minor = talk(0, "S1", 4), talk(2, "S3", 9)

    problem = conv._pick_anchor(quote_turn, claim, [quote_turn, minor], CITABLE)

    assert isinstance(problem, Problem) and problem.code == SOURCE_TURN_NOT_CITABLE and "T2" in problem.message


def test_among_equally_good_turns_of_one_speaker_the_nearest_to_the_quote_turn_wins_then_the_earlier():
    claim = claim_of(16)
    quote_turn = talk(3, "S1", 2)
    near, far = talk(4, "S2", 9), talk(8, "S2", 9)
    early, late = talk(1, "S2", 9), talk(5, "S2", 9)

    assert conv._pick_anchor(quote_turn, claim, [quote_turn, far, near], CITABLE) is near
    assert conv._pick_anchor(quote_turn, claim, [quote_turn, late, early], CITABLE) is early        # 一樣近：編號小的


def test_a_point_whose_content_comes_from_another_turn_is_never_moved_to_that_turns_speaker():
    """kept 的點一定在 quote 所在的那一輪：內容出自別輪就丟掉，不會改歸給別人。"""
    turns, good = with_anchors(("S1", SEQ[:9] + "，春夏秋冬"), ("S2", SEQ[:14] + "，春夏秋冬"))
    point = RawPoint(0, SEQ[:14], quote=SEQ[:9] + "，春夏秋冬")           # quote 在 T0（S1），內容幾乎整句是 T1（S2）的

    kept, result = run(point, turns=turns, speakers=make_speakers("participant", "participant"), good=good)

    assert kept == [] and codes(result) == {ANCHOR_CONFLICT: 1} and result.anchor_conflicts == 1


# --- 改用原話（混合條列）：每一個條件 ---


def span_or_reason(text: str, quote: str, names=frozenset()):
    return conv._quote_to_show(Turn(0, 0.0, 1.0, "S1", text), quote, names)


def test_a_clean_contiguous_quote_is_shown_exactly_as_the_transcript_has_it():
    span = span_or_reason("我們認為，預算應該先編三個月。", "預算應該先編三個月")

    assert span.text == "預算應該先編三個月" and not isinstance(span, str)


@pytest.mark.parametrize("text, quote, fragment", [
    ("預算應該先編三個月，之後再追加。", "預算應該先編三個月 追加", "連續的一段原話"),
    ("預算很多。", "預算很多", "不到 8 個字"),
    ("預算" * 51, "預算" * 51, "太長"),
])
def test_a_quote_that_is_not_contiguous_too_short_or_too_long_is_refused(text, quote, fragment):
    reason = span_or_reason(text, quote)

    assert isinstance(reason, str) and fragment in reason


def test_a_quote_of_exactly_the_maximum_length_is_still_shown():
    text = ("今天討論預算與期程" * 12)[:MAX_RESCUED_QUOTE_CHARS]

    assert not isinstance(span_or_reason(text, text), str)
    assert isinstance(span_or_reason(text + "安", text + "安"), str)


@pytest.mark.parametrize("pronoun", ["他", "她", "它", "牠", "他們", "她們", "對方", "雙方", "此人", "該員", "該名", "該位"])
def test_a_pronoun_inside_the_quote_stops_it_from_being_shown(pronoun):
    reason = span_or_reason(f"我們討論過{pronoun}提出的預算方案。", f"我們討論過{pronoun}提出的預算方案")

    assert isinstance(reason, str) and f"「{pronoun}" in reason


@pytest.mark.parametrize("word", ["其他單位", "其它縣市", "他人的意見"])
def test_words_that_only_happen_to_contain_a_pronoun_are_not_pronouns(word):
    text = f"我們討論過{word}提出的預算方案"

    assert not isinstance(span_or_reason(text, text), str)


@pytest.mark.parametrize("word", ["主持人", "來賓", "講者", "受訪者", "發言者", "記者", "委員", "部長", "議員", "教授",
                                  "說話者", "與談人", "提問者"])
def test_a_role_or_label_word_inside_the_quote_stops_it_from_being_shown(word):
    text = f"這件事{word}也有提到預算方案"

    reason = span_or_reason(text, text)

    assert isinstance(reason, str) and f"「{word}」" in reason


@pytest.mark.parametrize("word", ["記者會", "委員會", "登記者"])
def test_ordinary_words_that_contain_a_role_are_fine(word):
    text = f"這件事在{word}討論過預算方案"

    assert not isinstance(span_or_reason(text, text), str)


def test_a_name_that_the_transcript_uses_for_addressing_people_stops_the_quote_from_being_shown():
    text = "謝謝林先生的提醒，預算會先編"

    reason = span_or_reason(text, text, names=frozenset({"林先生"}))

    assert isinstance(reason, str) and "「林先生」" in reason
    assert not isinstance(span_or_reason(text, text, names=frozenset({"小安"})), str)
    assert not isinstance(span_or_reason(text, text), str)


def test_a_quote_that_starts_by_relaying_the_other_person_is_refused():
    reason = span_or_reason("你剛才說預算不夠用的問題，我們會檢討。", "你剛才說預算不夠用的問題")

    assert isinstance(reason, str) and "轉述對方" in reason


def test_the_first_half_of_a_restating_question_is_refused():
    text = "所以補助應該全部取消，然後改編列在地方預算，是這個意思嗎？"

    reason = span_or_reason(text, "所以補助應該全部取消，然後改編列在地方預算")

    assert isinstance(reason, str) and "複述對方" in reason


def test_first_person_inside_a_shown_quote_is_the_speakers_own_words_and_allowed():
    text = "我們認為預算應該先編三個月"

    assert not isinstance(span_or_reason(text, text), str)


def test_a_rescued_point_is_a_literal_span_of_the_turn_of_the_speaker_who_said_it():
    kept, result = run(RawPoint(at("現金補助"), "金錢支持不是唯一解方", quote="現金補助有幫助，但不能只靠現金"))

    assert [(p.speaker, p.turn, p.text) for p in kept] == [("S2", at("現金補助"), "「現金補助有幫助，但不能只靠現金」")]
    assert result.rescued == 1 and result.slides[0].rescued == 1
    assert "現金補助有幫助，但不能只靠現金" in TURNS[at("現金補助")].text


def test_only_an_unsupported_claim_can_be_rescued_never_a_wrong_number_or_a_wrong_polarity():
    quote = "預算大約要三千億，比去年多了兩成"
    wrong_number = RawPoint(at("預算大約"), "預算大約要五千億，比去年多了兩成", quote=quote)
    inverted = RawPoint(at("預算大約"), "預算不用三千億，比去年少了兩成", quote=quote)
    unsupported = RawPoint(at("預算大約"), "金錢支持不是唯一解方", quote=quote)

    kept, result = run(wrong_number, inverted, unsupported)

    assert [p.text for p in kept] == [f"「{quote}」"]
    assert codes(result) == {NUMBER_MISMATCH: 1, "negation_mismatch": 1} and result.rescued == 1


def test_a_text_problem_is_never_rescued_even_when_the_quote_is_fine():
    quote = "現金補助有幫助，但不能只靠現金"
    pronoun = RawPoint(at("現金補助"), "他認為現金補助有幫助", quote=quote)
    name = RawPoint(at("現金補助"), "邱委員認為現金不夠", quote=quote)

    kept, result = run(pronoun, name)

    assert kept == [] and result.rescued == 0 and set(codes(result)) == {"leading_pronoun_or_title"}


# --- 整段文字的代名詞（不只開頭）---


@pytest.mark.parametrize("text, code", [
    ("嘗試說服對方降級", "pronoun_in_text"),
    ("資金壓在他們身上", "pronoun_in_text"),
    ("認為牠們不會同意", "pronoun_in_text"),
    ("他認為預算不夠", "leading_pronoun_or_title"),
    ("王委員認為預算不夠", "leading_pronoun_or_title"),
])
def test_a_pronoun_anywhere_in_the_text_is_refused(text, code):
    problem = conv._text_problem(text)

    assert problem is not None and problem.code == code


@pytest.mark.parametrize("text", [
    "其他單位也要降級", "其它縣市同樣適用", "尊重他人的意見", "他山之石可以參考", "他方的看法也要聽", "他鄉的經驗可供參考",
    "強調「他們都同意這個做法」",                  # 引號裡是引述的原話，另外核對
])
def test_words_that_only_contain_a_pronoun_and_quoted_words_are_not_refused(text):
    assert conv._text_problem(text) is None


# --- 安全模式（SLIDEBOX_QUOTE_ONLY）---


@pytest.mark.parametrize("raw, expected", [
    ("1", True), ("true", True), ("TRUE", True), ("True", True), ("yes", True), ("YES", True),
    (" yes ", True), ("\t1\n", True),
    (None, False), ("", False), ("   ", False), ("0", False), ("false", False), ("no", False), ("off", False),
    ("on", False), ("2", False), ("enabled", False), ("y", False), ("truee", False),
])
def test_only_one_true_or_yes_turns_the_safe_mode_on(raw, expected):
    assert parse_quote_only(raw) is expected


def test_the_safe_mode_shows_the_verified_words_whatever_the_model_wrote():
    junk = RawPoint(at("名額不夠"), "[T3] S1 他們我們都亂寫", quote="名額不夠，很多家長排不到，排了兩年也沒有消息")

    kept, result = run(junk, quote_only=True)

    assert [(p.speaker, p.turn, p.text) for p in kept] == [
        ("S2", at("名額不夠"), "「名額不夠，很多家長排不到，排了兩年也沒有消息」")]
    assert result.rescued == result.kept == 3 and not result.dropped_by_code


def test_without_the_safe_mode_the_same_text_is_dropped():
    junk = RawPoint(at("名額不夠"), "他們我們都亂寫", quote="名額不夠，很多家長排不到，排了兩年也沒有消息")

    kept, result = run(junk)

    assert kept == [] and codes(result) == {"leading_pronoun_or_title": 1}


def test_the_safe_mode_still_finds_the_turn_that_really_holds_the_quote():
    wrong_turn = RawPoint(at("現金補助"), "隨便", quote="名額不夠，很多家長排不到，排了兩年也沒有消息")

    kept, result = run(wrong_turn, quote_only=True)

    assert [(p.speaker, p.turn) for p in kept] == [("S2", at("名額不夠"))]
    assert f"已改引有這段原文的 T{at('名額不夠')}" in problems_of(result)


@pytest.mark.parametrize("quote, reason", [
    ("名額不夠，排了兩年也沒有消息", "連續的一段原話"),            # 跳過一個子句
    ("謝謝邱委員，那預算要怎麼編才夠", "委員"),                  # 稱呼
    ("您剛才說名額不夠，家長排不到，對嗎", "轉述對方"),            # 「您剛才說…」開頭
])
def test_the_safe_mode_drops_a_quote_that_cannot_stand_alone_as_words(quote, reason):
    kept, result = run(RawPoint(0, "隨便", quote=quote), quote_only=True)

    assert kept == [] and codes(result) == {BAD_QUOTE: 1}
    assert "quote 不能直接當重點顯示：" in problems_of(result) and reason in problems_of(result)
    assert "請抄一個完整、連續、不含稱呼與代名詞的子句" in problems_of(result)


def test_the_safe_mode_counts_a_quote_that_matches_no_turn_like_any_other():
    kept, result = run(RawPoint(0, "隨便", quote="根本沒有人說過這句話的引文"), quote_only=True)

    assert kept == [] and codes(result) == {BAD_QUOTE: 1} and result.unverifiable == 1


def test_the_safe_mode_does_not_show_the_same_words_twice_on_a_page():
    once = RawPoint(at("名額不夠"), "一", quote="名額不夠，很多家長排不到，排了兩年也沒有消息")

    kept, result = run(once, replace(once, text="二"), quote_only=True)

    assert len(kept) == 1 and codes(result) == {DUPLICATE_POINT: 1}


def test_in_the_safe_mode_every_kept_point_is_a_literal_span_of_one_turn_of_its_own_speaker():
    """性質：隨機的引文（真的片段、拼接、改字），留下來的每個點都是它那一輪的逐字原話，說話者是那一輪的人。"""
    rng = random.Random(41)
    participants = [t for t in TURNS if t.speaker in CITABLE and len(t.text) > 12]
    checked = 0
    for _ in range(400):
        base = rng.choice(participants)
        start = rng.randrange(0, len(base.text) - 8)
        quote = base.text[start:start + rng.randint(8, 30)]
        roll = rng.random()
        if roll < 0.25:
            quote += rng.choice(participants).text[:rng.randint(2, 12)]            # 接上別輪的字
        elif roll < 0.5:
            cut = rng.randrange(len(quote))
            quote = quote[:cut] + "好" + quote[cut + 1:]                        # 改一個字
        kept, result = run(RawPoint(base.index, "隨便寫", quote=quote), quote_only=True)
        for point in kept:
            if point.text in {f"「{p.quote}」" for p in GOOD}:
                continue
            checked += 1
            words = point.text[1:-1]
            holder = TURNS[point.turn]
            assert point.text[0] == "「" and point.text[-1] == "」"
            assert words in " ".join(holder.text.split())
            assert point.speaker == holder.speaker and holder.speaker in CITABLE
    assert checked > 50


# --- 計數與分類 ---


def test_every_dropped_point_is_counted_in_exactly_one_class_and_one_code():
    good, other = GOOD
    wrong_number = RawPoint(at("預算大約"), "預算大約要五千億，比去年多了兩成", quote="預算大約要三千億，比去年多了兩成")
    slide_one = RawSlide("補助與托育", (
        good, other, other,                                                    # 重複一次
        RawPoint(0, "詢問補助", quote="這句話根本沒有人說過的引文"),                # 引文找不到
        RawPoint(1, "補助有幫助", quote="另一句沒人說過的引文內容"),
        wrong_number))
    slide_two = RawSlide("單薄的一頁", (
        RawPoint(at("名額不夠"), "名額不夠，家長排不到", quote="名額不夠，很多家長排不到，排了兩年也沒有消息"),
        RawPoint(0, "詢問補助", quote="第三句沒人說過的引文內容")))

    result = validate_points([slide_one, slide_two], TURNS, SPEAKERS, WHOLE)

    assert (result.proposed, result.kept) == (8, 2)
    assert (result.contradicted, result.unverifiable, result.slide_rule) == (1, 3, 2)
    assert result.proposed - result.kept == result.contradicted + result.unverifiable + result.slide_rule
    assert result.dropped_by_code == ((BAD_QUOTE, 3), (DUPLICATE_POINT, 1), (NUMBER_MISMATCH, 1), (SLIDE_TOO_SMALL, 1))
    assert sum(n for _, n in result.dropped_by_code) == result.proposed - result.kept


def test_the_cut_points_of_a_long_slide_are_neither_proposed_nor_kept_and_the_earliest_turns_stay():
    words = ["預算編列", "期程安排", "成效評估", "人力配置", "場地使用", "宣傳方式"]
    turns = make_turns(*[("S1" if i % 2 == 0 else "S2", f"關於{w}的部分，我們有完整的規劃與說明。") for i, w in enumerate(words)])
    speakers = make_speakers("participant", "participant")
    window = conv.chunk_turns(turns, speakers)[0]
    points = [RawPoint(i, f"說明{w}的規劃", quote=turns[i].text.rstrip("。")) for i, w in enumerate(words)]
    bad = RawPoint(2, "說明成效評估", quote="這句話根本沒有人說過的引文")
    slide = RawSlide("六件事", tuple([bad, *reversed(points)]))                # 故意倒著給：留下的是編號最前面的四個

    result = validate_points([slide], turns, speakers, window)

    assert [p.turn for p in result.slides[0].points] == [0, 1, 2, 3]
    assert (result.proposed, result.kept) == (5, MAX_BULLETS)           # 7 個提出，扣掉多出來的 2 個
    assert result.proposed - result.kept == result.unverifiable == 1
    assert result.dropped_by_code == ((BAD_QUOTE, 1),)


def test_the_counts_only_cover_rescued_points_that_are_still_on_a_slide():
    kept, result = run(RawPoint(at("現金補助"), "金錢支持不是唯一解方", quote="現金補助有幫助，但不能只靠現金"))

    assert result.rescued == sum(s.rescued for s in result.slides) == 1


def test_the_result_does_not_depend_on_a_warm_cache_or_on_running_twice():
    point = RawPoint(at("現金補助"), "金錢支持不是唯一解方", quote="現金補助有幫助，但不能只靠現金")

    first = run(point)[1]
    conv._grams.cache_clear()
    second = run(point)[1]

    assert first == second


# --- 穩定的原因碼 ---

CODE_NAMES = [
    "TOO_SHORT", "TOO_LONG", "MARKER_IN_TEXT", "LABEL_WORD", "LEADING_PRONOUN_OR_TITLE", "PRONOUN_IN_TEXT",
    "FIRST_PERSON", "NAME_IN_TEXT", "TURN_NOT_INT", "TURN_OUT_OF_WINDOW", "CITES_X_OR_MINOR", "BAD_QUOTE",
    "SHORT_ANSWER_NO_CONTEXT", "SHORT_ANSWER_CONTEXT_MISMATCH", "SHORT_ANSWER_STANCE", "UNSUPPORTED",
    "ANCHOR_CONFLICT", "CROSS_SPEAKER_TIE", "SOURCE_TURN_NOT_CITABLE", "NUMBER_MISMATCH", "NEGATION_MISMATCH",
    "QUESTION_STATED_AS_FACT", "ECHO", "BAD_INLINE_QUOTE", "DUPLICATE_POINT", "SLIDE_TOO_SMALL",
]


def test_the_code_of_a_dropped_point_is_its_lower_case_name_and_never_changes():
    values = [getattr(conv, name) for name in CODE_NAMES]

    assert values == [name.lower() for name in CODE_NAMES] and len(set(values)) == len(values) == 26


def test_every_code_belongs_to_exactly_one_of_the_three_classes():
    values = [getattr(conv, name) for name in CODE_NAMES]

    assert {problem_class(v) for v in values} == {CONTRADICTED, UNVERIFIABLE, SLIDE_RULE}
    assert [v for v in values if problem_class(v) == UNVERIFIABLE] == [BAD_QUOTE, UNSUPPORTED]
    assert [v for v in values if problem_class(v) == SLIDE_RULE] == [DUPLICATE_POINT, SLIDE_TOO_SMALL]
    assert sum(1 for v in values if problem_class(v) == CONTRADICTED) == 22


def test_an_unknown_code_counts_as_a_problem_with_the_text_not_as_a_missing_source():
    assert problem_class("something_new") == CONTRADICTED


def answer_dialogue():
    """帶一句只有「沒錯」的回答，讓很短的回答的三個碼都走得到。"""
    return with_anchors(("S1", "你認為補助應該加倍嗎？"), ("S2", "沒錯"))


@pytest.mark.parametrize("code, build", [
    ("too_short", lambda: RawPoint(1, "短", quote="現金補助有幫助，但不能只靠現金")),
    ("too_long", lambda: RawPoint(1, "預算" * 61, quote="現金補助有幫助，但不能只靠現金")),
    ("marker_in_text", lambda: RawPoint(1, "S1 主張補助應該加倍", quote="現金補助有幫助，但不能只靠現金")),
    ("label_word", lambda: RawPoint(1, "主持人詢問補助政策", quote="現金補助有幫助，但不能只靠現金")),
    ("leading_pronoun_or_title", lambda: RawPoint(1, "他認為補助應該加倍", quote="現金補助有幫助，但不能只靠現金")),
    ("pronoun_in_text", lambda: RawPoint(1, "詢問對方補助政策的看法", quote="現金補助有幫助，但不能只靠現金")),
    ("first_person", lambda: RawPoint(1, "我們認為補助應該加倍", quote="現金補助有幫助，但不能只靠現金")),
    ("name_in_text", lambda: RawPoint(1, "預算要怎麼編，邱委員有問過", quote="現金補助有幫助，但不能只靠現金")),
    ("turn_not_int", lambda: RawPoint("1", "現金補助有幫助", quote="現金補助有幫助，但不能只靠現金")),
    ("turn_out_of_window", lambda: RawPoint(999, "現金補助有幫助", quote="現金補助有幫助，但不能只靠現金")),
    ("cites_x_or_minor", lambda: RawPoint(13, "托育名額真的不夠", quote="我想補充，我們社區的托育名額真的不夠")),
    ("bad_quote", lambda: RawPoint(1, "現金補助有幫助", quote="這句話根本沒有人說過的引文")),
    ("number_mismatch", lambda: RawPoint(11, "預算大約要五千億，比去年多了兩成", quote="預算大約要三千億，比去年多了兩成")),
    ("negation_mismatch", lambda: RawPoint(11, "預算要三千億，並非比去年多了兩成", quote="預算大約要三千億，比去年多了兩成")),
    ("question_stated_as_fact", lambda: RawPoint(0, "目前的補助政策成效很好", quote="先請教您怎麼看目前的補助政策")),
    ("echo", lambda: RawPoint(8, "名額不夠，家長排不到", quote="您剛才說名額不夠，家長排不到，對嗎")),
    ("bad_inline_quote", lambda: RawPoint(1, "強調「現金補助有幫助，但不能只靠現金", quote="現金補助有幫助，但不能只靠現金")),
])
def test_each_drop_reason_comes_with_its_code(code, build):
    kept, result = run(build())

    assert kept == [] and codes(result) == {code: 1}, (code, result.problems)


@pytest.mark.parametrize("code, point_of", [
    ("short_answer_no_context", lambda turns: RawPoint(1, "同意補助應該加倍", quote="沒錯")),
    ("short_answer_context_mismatch", lambda turns: RawPoint(1, "同意天地玄黃宇宙", quote="沒錯", context=(0,))),
    ("short_answer_stance", lambda turns: RawPoint(1, "補助應該加倍", quote="沒錯", context=(0,))),
])
def test_each_short_answer_reason_comes_with_its_code(code, point_of):
    turns, good = answer_dialogue()

    _, result = run(point_of(turns), turns=turns, speakers=make_speakers("participant", "participant"), good=good)

    assert codes(result) == {code: 1}, result.problems


def test_a_short_answer_that_states_a_stance_over_the_right_context_is_kept():
    turns, good = answer_dialogue()

    kept, result = run(RawPoint(1, "同意補助應該加倍", quote="沒錯", context=(0,)), turns=turns,
                       speakers=make_speakers("participant", "participant"), good=good)

    assert [p.text for p in kept] == ["同意補助應該加倍"] and not result.dropped_by_code
