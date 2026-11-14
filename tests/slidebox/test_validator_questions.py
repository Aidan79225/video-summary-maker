# -*- coding: utf-8 -*-
"""Questions without a particle / question mark (請問, 想請教, 您怎麼看, 難道, 怎麼可能) are not in _QUESTION_RE (although _ASK_RE knows 請問 / 怎麼 on the text side) so the question is published as the asker's claim."""
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

CASES = [('4a',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '請問您怎麼看預算增加兩成這件事，我想聽聽您的意見。')],
  '請問您怎麼看預算增加兩成這件事',
  1,
  '預算增加兩成',
  ()),
 ('4b',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '我想請教為什麼今年要把補助調高三成這個決定')],
  '我想請教為什麼今年要把補助調高三成',
  1,
  '今年要把補助調高三成',
  ()),
 ('4d',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '預算增加兩成對這件事您怎麼看，請表達您的立場。')],
  '預算增加兩成對這件事您怎麼看',
  1,
  '預算增加兩成對這件事',
  ()),
 ('4e',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '怎麼可能增加兩成的預算就解決問題，大家都知道不可能。')],
  '怎麼可能增加兩成的預算就解決問題',
  1,
  '增加兩成的預算就解決問題',
  ()),
 ('2e',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '難道要增加預算才能解決問題，這根本說不通，我完全不接受。')],
  '要增加預算才能解決問題',
  1,
  '要增加預算才能解決問題',
  ())]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_unmarked_or_rhetorical_question_is_not_published_as_a_claim(case):
    _id, lines, quote, turn, text, context = case
    extra, _ = published(lines, quote, turn, text, context)
    assert extra == [], f"{_id}: published {[p.text for p in extra]}"
