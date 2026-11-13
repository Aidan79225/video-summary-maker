# -*- coding: utf-8 -*-
"""Short answers: `context` may be ANY turn up to 3 before the answer, and the text only has to be lexically supported by the joined context, so a denial/agreement is attached to a claim or question the answer did not respond to."""
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

CASES = [('4h',
  [('S1', '我認為明年預算要增加兩成才夠用。'), ('S2', '這個我們還要再研究，目前沒有定論。'), ('S1', '那你們願意取消現在的補助嗎？'), ('S2', '沒有。')],
  '沒有。',
  3,
  '不同意明年預算要增加兩成',
  (0,)),
 ('4k',
  [('S1', '你們明年會調漲電價嗎？'), ('S2', '會。'), ('S1', '你們明年會取消補助嗎？'), ('S2', '不會。')],
  '不會。',
  3,
  '否認明年會調漲電價',
  (0,))]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_short_answer_attached_to_the_wrong_turn_is_not_published(case):
    _id, lines, quote, turn, text, context = case
    extra, _ = published(lines, quote, turn, text, context)
    assert extra == [], f"{_id}: published {[p.text for p in extra]}"
