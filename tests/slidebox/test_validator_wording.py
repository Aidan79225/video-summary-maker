# -*- coding: utf-8 -*-
"""Role words and first-person variants missing from the lexicons: 記者, 市長, 發言人 mid-text; 本部, 本公司, 小弟; 雙方/前者/後者."""
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

CASES = [('5d',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '明年預算會增加兩成，這是我們內部的規劃。')],
  '明年預算會增加兩成',
  1,
  '記者指出明年預算增加兩成',
  ()),
 ('5e',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '明年預算會增加兩成，這是我們內部的規劃。')],
  '明年預算會增加兩成',
  1,
  '預算會增加兩成，市長已承諾',
  ()),
 ('5f',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '明年預算會增加兩成，這是我們內部的規劃。')],
  '明年預算會增加兩成',
  1,
  '發言人表示明年預算會增加兩成',
  ()),
 ('5g',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '雙方都同意延後協商，下個月再重新討論這個問題。')],
  '雙方都同意延後協商',
  1,
  '雙方同意延後協商',
  ()),
 ('5h',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '前者主張增加預算，後者主張刪減預算，兩種意見差很多。')],
  '前者主張增加預算，後者主張刪減預算',
  1,
  '前者主張增加預算，後者主張刪減預算',
  ()),
 ('5i',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '本部明年會增加預算兩成，用在托育和長照上面。')],
  '本部明年會增加預算兩成',
  1,
  '本部明年會增加預算兩成',
  ()),
 ('5j',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '本公司明年會增加預算兩成，用在研發上面。')],
  '本公司明年會增加預算兩成',
  1,
  '本公司明年增加預算兩成',
  ()),
 ('5p',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '小弟認為明年預算應該增加兩成，用在托育和長照上面。')],
  '小弟認為明年預算應該增加兩成',
  1,
  '小弟認為明年預算應該增加兩成',
  ())]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_role_or_first_person_wording_is_not_published(case):
    _id, lines, quote, turn, text, context = case
    extra, _ = published(lines, quote, turn, text, context)
    assert extra == [], f"{_id}: published {[p.text for p in extra]}"
