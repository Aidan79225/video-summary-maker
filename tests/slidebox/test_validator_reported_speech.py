# -*- coding: utf-8 -*-
"""A speaker REPORTS what a third party said / reported / predicted (他們說, 有人說, 網友說, 根據報導, 部長表示, 專家預測, 聽說); the point is published as the speaker's own claim. Only 你剛才說 (rescue) and an earlier supporting turn of another citable speaker (echo) are recognised."""
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

CASES = [('1a',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', 'B昨天在記者會上說我們明年不會加稅，但是我覺得這個承諾很難兌現。')],
  '昨天在記者會上說我們明年不會加稅',
  1,
  '指出明年不會加稅',
  ()),
 ('1b',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '他們說明年預算要增加兩成，可是我們看到的數字完全不是這樣。')],
  '他們說明年預算要增加兩成',
  1,
  '表示明年預算要增加兩成',
  ()),
 ('1c',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '外面有人說這個政策會讓房價上漲三成，我認為這是誇大的說法。')],
  '有人說這個政策會讓房價上漲三成',
  1,
  '指出政策會讓房價上漲三成',
  ()),
 ('1d',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '網友說這家公司去年虧損了五百億，但實際的數字不是這樣。')],
  '網友說這家公司去年虧損了五百億',
  1,
  '指出公司去年虧損五百億',
  ()),
 ('1e',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '根據報導，這個工廠去年排放了一萬兩千噸廢水，但是我們內部的數字是三千噸。')],
  '根據報導，這個工廠去年排放了一萬兩千噸廢水',
  1,
  '工廠去年排放一萬兩千噸廢水',
  ()),
 ('1f',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '經濟部長表示明年電價不會調漲，不過我今天來是要問清楚。')],
  '經濟部長表示明年電價不會調漲',
  1,
  '表示明年電價不會調漲',
  ()),
 ('1g',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '專家預測明年景氣會衰退，成長率只有百分之一，我覺得太悲觀了。')],
  '專家預測明年景氣會衰退，成長率只有百分之一',
  1,
  '預測明年景氣衰退，成長率百分之一',
  ()),
 ('1h',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '我引用市場上的說法：預算要增加三千億才夠，但這不是我的立場。')],
  '預算要增加三千億才夠',
  1,
  '主張預算要增加三千億才夠',
  ()),
 ('3d',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '聽說這間公司明年會裁員三百人，不過我沒有看到正式的公告。')],
  '聽說這間公司明年會裁員三百人',
  1,
  '公司明年裁員三百人',
  ())]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_reported_third_party_claim_is_not_published_as_the_speakers_own(case):
    _id, lines, quote, turn, text, context = case
    extra, _ = published(lines, quote, turn, text, context)
    assert extra == [], f"{_id}: published {[p.text for p in extra]}"
