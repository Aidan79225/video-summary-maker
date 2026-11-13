# -*- coding: utf-8 -*-
"""Direction / polarity / time / quantifier flips inside ONE turn (下降->上升, 增加->減少, 反對->支持, 之後->之前, 尚未->已經, 去年->明年, 大多數->所有, 不是不->不): number, negation and lexical checks all pass. The negation check is also one-directional and turn-wide (248 of 295 real turns contain some 不/沒/無/非/未/否, so it is vacuous for 84%)."""
from __future__ import annotations

import pytest

from slidebox.domain.entities import RawPoint

from .conversation_support import run, with_anchors
from .speaker_fixtures import make_speakers

TWO = make_speakers("participant", "participant")


def published(lines, quote, turn, text, context=()):
    """The points that survive validation next to two always-good filler points (what the viewer would see)."""
    turns, good = with_anchors(*lines)
    extra, result = run(RawPoint(turn, text, tuple(context), quote), turns=turns, speakers=TWO, good=good)
    return extra, result

CASES = [('3g',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '今年的失業率比去年下降了零點五個百分點，這是好消息。')],
  '今年的失業率比去年下降了零點五個百分點',
  1,
  '失業率比去年上升零點五個百分點',
  ()),
 ('3h',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '明年的預算將會增加兩成，用在托育和長照上面。')],
  '明年的預算將會增加兩成',
  1,
  '明年預算將減少兩成',
  ()),
 ('3i',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '我反對這個課徵碳稅的提案，因為成本太高了。')],
  '我反對這個課徵碳稅的提案',
  1,
  '支持課徵碳稅的提案',
  ()),
 ('3l',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '去年的稅收增加了五百億，主要是營所稅成長。')],
  '去年的稅收增加了五百億',
  1,
  '明年稅收增加五百億',
  ()),
 ('3m',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '法案通過之後，電價就會調漲，這是一定的。')],
  '法案通過之後，電價就會調漲',
  1,
  '法案通過之前電價調漲',
  ()),
 ('3n',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '這個工程尚未完成，預計要等到明年三月才會驗收。')],
  '這個工程尚未完成，預計要等到明年三月才會驗收',
  1,
  '工程已經完成，明年三月驗收',
  ()),
 ('3o',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '我們不得不增加預算，否則整個計畫會停擺，沒有別的辦法。')],
  '我們不得不增加預算，否則整個計畫會停擺',
  1,
  '不得增加預算，否則計畫停擺',
  ()),
 ('3e',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '我們不是不願意加薪，只是目前的預算真的非常有限。')],
  '我們不是不願意加薪',
  1,
  '不願意加薪，預算有限',
  ()),
 ('3k',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '大多數家長都支持延長托育時間，只有少數人持不同意見。')],
  '大多數家長都支持延長托育時間',
  1,
  '所有家長都支持延長托育時間',
  ())]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_opposite_meaning_is_not_published(case):
    _id, lines, quote, turn, text, context = case
    extra, _ = published(lines, quote, turn, text, context)
    assert extra == [], f"{_id}: published {[p.text for p in extra]}"
