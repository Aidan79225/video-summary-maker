# -*- coding: utf-8 -*-
"""number_problem compares value + unit only for percent vs absolute: 二十年 -> 二十人, 兩百萬人 -> 兩百萬元, 三成 -> 三倍 pass (the whole number is 'copied whole' and no other unit is compared). Measured cost of a same-value/different-listed-unit rule on 295 real kept points: 0."""
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

CASES = [('3r',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '這個案子前後花了二十年才完成，參與的人員一共有四十人。')],
  '這個案子前後花了二十年才完成',
  1,
  '案子前後花了二十人才完成',
  ()),
 ('3s',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '這次災害造成兩百萬人受災，政府預計撥款一百億元救助。')],
  '這次災害造成兩百萬人受災',
  1,
  '造成兩百萬元受災',
  ()),
 ('3t',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '今年的出口比去年成長了三成，進口則是成長兩成左右。')],
  '今年的出口比去年成長了三成',
  1,
  '今年出口比去年成長三倍',
  ())]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_changed_unit_is_not_published(case):
    _id, lines, quote, turn, text, context = case
    extra, _ = published(lines, quote, turn, text, context)
    assert extra == [], f"{_id}: published {[p.text for p in extra]}"
