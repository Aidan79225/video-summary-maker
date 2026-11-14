# -*- coding: utf-8 -*-
"""Hybrid rescue shows 「quote」 when the paraphrase is unsupported; the quote only has to be a contiguous span. A main clause that follows 如果.../只要... is shown without its condition ('我們就支持這個方案')."""
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

CASES = [('2b',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '如果明年預算增加兩成，我們就支持這個方案，但目前還不是這樣。')],
  '我們就支持這個方案',
  1,
  '樂意贊助該提案',
  ()),
 ('2c',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '只要有新的財源，我們就會同意提高補助，現在沒有所以不同意。')],
  '我們就會同意提高補助',
  1,
  '樂意贊助該提案',
  ()),
 ('2j',
  [('S1', '今天我們來談明年的預算和稅制，先請教您的看法。'), ('S2', '我不同意預算增加百分之二十的方案，因為財源根本不足。')],
  '預算增加百分之二十的方案',
  1,
  '樂意贊助該提案',
  ())]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_rescued_quote_after_a_condition_clause_is_not_shown(case):
    _id, lines, quote, turn, text, context = case
    extra, _ = published(lines, quote, turn, text, context)
    assert extra == [], f"{_id}: shown {[p.text for p in extra]}"
