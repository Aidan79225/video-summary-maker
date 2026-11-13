# -*- coding: utf-8 -*-
"""A host's echo of a guest's claim that carries an echo marker (您剛才提到 / 你是說 / 也就是說) but REWORDS it a little: _echo_source only fires when the guest's turn matches the text at least as well as the host's own turn, so the echo is published as the host's own claim (verbatim echoes ARE caught: 32/32; reworded: 0/64)."""
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

CASES = [('echo0-v1-您剛才提',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '這個稅制改革會讓中小企業多繳兩成的稅，我們強烈反對。'), ('S1', '您剛才提到中小企業要多繳兩成的稅。')],
  '您剛才提到中小企業要多繳兩成的稅',
  2,
  '指出中小企業要多繳兩成的稅',
  ()),
 ('echo0-v1-所以你是',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '這個稅制改革會讓中小企業多繳兩成的稅，我們強烈反對。'), ('S1', '所以你是說中小企業要多繳兩成的稅。')],
  '所以你是說中小企業要多繳兩成的稅',
  2,
  '指出中小企業要多繳兩成的稅',
  ()),
 ('echo0-v2-也就是說',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '這個稅制改革會讓中小企業多繳兩成的稅，我們強烈反對。'), ('S1', '也就是說中小企業的稅負增加兩成。')],
  '也就是說中小企業的稅負增加兩成',
  2,
  '指出中小企業的稅負增加兩成',
  ()),
 ('echo1-v1-您剛才提',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '明年的電價預計調漲百分之十，家庭用戶也會受到影響。'), ('S1', '您剛才提到明年電價會調漲百分之十。')],
  '您剛才提到明年電價會調漲百分之十',
  2,
  '指出明年電價會調漲百分之十',
  ()),
 ('echo1-v1-所以你是',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '明年的電價預計調漲百分之十，家庭用戶也會受到影響。'), ('S1', '所以你是說明年電價會調漲百分之十。')],
  '所以你是說明年電價會調漲百分之十',
  2,
  '指出明年電價會調漲百分之十',
  ()),
 ('echo1-v2-也就是說',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '明年的電價預計調漲百分之十，家庭用戶也會受到影響。'), ('S1', '也就是說明年電費漲百分之十。')],
  '也就是說明年電費漲百分之十',
  2,
  '指出明年電費漲百分之十',
  ()),
 ('echo2-v1-您剛才提',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '這條路線預計要花三百億，工期大概五年才能完成。'), ('S1', '您剛才提到這條路線要花三百億。')],
  '您剛才提到這條路線要花三百億',
  2,
  '指出這條路線要花三百億',
  ()),
 ('echo2-v1-所以你是',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '這條路線預計要花三百億，工期大概五年才能完成。'), ('S1', '所以你是說這條路線要花三百億。')],
  '所以你是說這條路線要花三百億',
  2,
  '指出這條路線要花三百億',
  ()),
 ('echo2-v2-也就是說',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '這條路線預計要花三百億，工期大概五年才能完成。'), ('S1', '也就是說總預算是三百億。')],
  '也就是說總預算是三百億',
  2,
  '指出總預算是三百億',
  ()),
 ('echo3-v1-您剛才提',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '托育名額不夠，很多家長排了兩年還是沒有消息。'), ('S1', '您剛才提到家長排了兩年都沒有消息。')],
  '您剛才提到家長排了兩年都沒有消息',
  2,
  '指出家長排了兩年都沒有消息',
  ()),
 ('echo3-v1-所以你是',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '托育名額不夠，很多家長排了兩年還是沒有消息。'), ('S1', '所以你是說家長排了兩年都沒有消息。')],
  '所以你是說家長排了兩年都沒有消息',
  2,
  '指出家長排了兩年都沒有消息',
  ()),
 ('echo3-v2-也就是說',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '托育名額不夠，很多家長排了兩年還是沒有消息。'), ('S1', '也就是說家長要等兩年以上。')],
  '也就是說家長要等兩年以上',
  2,
  '指出家長要等兩年以上',
  ()),
 ('echo4-v1-您剛才提',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '公司今年的營收成長了百分之三十，主要是海外市場帶動。'), ('S1', '您剛才提到公司今年營收成長百分之三十。')],
  '您剛才提到公司今年營收成長百分之三十',
  2,
  '指出公司今年營收成長百分之三十',
  ()),
 ('echo4-v1-所以你是',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '公司今年的營收成長了百分之三十，主要是海外市場帶動。'), ('S1', '所以你是說公司今年營收成長百分之三十。')],
  '所以你是說公司今年營收成長百分之三十',
  2,
  '指出公司今年營收成長百分之三十',
  ()),
 ('echo4-v2-也就是說',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '公司今年的營收成長了百分之三十，主要是海外市場帶動。'), ('S1', '也就是說營收今年漲了三成。')],
  '也就是說營收今年漲了三成',
  2,
  '指出營收今年漲了三成',
  ()),
 ('echo5-v1-您剛才提',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '這個計畫的失敗率非常高，過去五年有一半都延誤了。'), ('S1', '您剛才提到過去五年一半都延誤。')],
  '您剛才提到過去五年一半都延誤',
  2,
  '指出過去五年一半都延誤',
  ()),
 ('echo5-v1-所以你是',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '這個計畫的失敗率非常高，過去五年有一半都延誤了。'), ('S1', '所以你是說過去五年一半都延誤。')],
  '所以你是說過去五年一半都延誤',
  2,
  '指出過去五年一半都延誤',
  ()),
 ('echo5-v2-也就是說',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '這個計畫的失敗率非常高，過去五年有一半都延誤了。'), ('S1', '也就是說五年來半數計畫延遲。')],
  '也就是說五年來半數計畫延遲',
  2,
  '指出五年來半數計畫延遲',
  ()),
 ('echo6-v1-您剛才提',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '我們絕對不會同意放寬建築高度限制，這是底線。'), ('S1', '您剛才提到不會同意放寬建築高度的限制。')],
  '您剛才提到不會同意放寬建築高度的限制',
  2,
  '指出不會同意放寬建築高度的限制',
  ()),
 ('echo6-v1-所以你是',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '我們絕對不會同意放寬建築高度限制，這是底線。'), ('S1', '所以你是說不會同意放寬建築高度的限制。')],
  '所以你是說不會同意放寬建築高度的限制',
  2,
  '指出不會同意放寬建築高度的限制',
  ()),
 ('echo6-v2-也就是說',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '我們絕對不會同意放寬建築高度限制，這是底線。'), ('S1', '也就是說拒絕放寬高度管制。')],
  '也就是說拒絕放寬高度管制',
  2,
  '指出拒絕放寬高度管制',
  ()),
 ('echo7-v1-您剛才提',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '學校營養午餐的價格明年要調高五元，家長要多負擔。'), ('S1', '您剛才提到午餐價格明年會調高五元。')],
  '您剛才提到午餐價格明年會調高五元',
  2,
  '指出午餐價格明年會調高五元',
  ()),
 ('echo7-v1-所以你是',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '學校營養午餐的價格明年要調高五元，家長要多負擔。'), ('S1', '所以你是說午餐價格明年會調高五元。')],
  '所以你是說午餐價格明年會調高五元',
  2,
  '指出午餐價格明年會調高五元',
  ()),
 ('echo7-v2-也就是說',
  [('S1', '今天來談這個議題，先請教您的看法。'), ('S2', '學校營養午餐的價格明年要調高五元，家長要多負擔。'), ('S1', '也就是說午餐明年漲五元。')],
  '也就是說午餐明年漲五元',
  2,
  '指出午餐明年漲五元',
  ())]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_reworded_host_echo_is_not_published(case):
    _id, lines, quote, turn, text, context = case
    extra, _ = published(lines, quote, turn, text, context)
    assert extra == [], f"{_id}: published {[p.text for p in extra]}"


def test_verbatim_echo_at_the_start_of_the_next_window_is_not_published():
    """Windows do not overlap and _echo_source only looks at EARLIER TURNS OF THE SAME WINDOW: a verbatim 您剛才提到... echo that opens
    window k+1 is compared with nothing, although the same echo inside one window is dropped (ECHO)."""
    from slidebox.usecases.conversation import Window

    lines = [("S1", "今天來談這個議題，先請教您的看法。"),
             ("S2", "這個稅制改革會讓中小企業多繳兩成的稅，我們強烈反對。"),
             ("S1", "您剛才提到中小企業多繳兩成的稅。")]
    quote, text = "您剛才提到中小企業多繳兩成的稅", "指出中小企業多繳兩成的稅"
    turns, good = with_anchors(*lines)
    whole, _ = run(RawPoint(2, text, (), quote), turns=turns, speakers=TWO, good=good)
    assert whole == [], "control: inside one window the verbatim echo is dropped"
    split, _ = run(RawPoint(2, text, (), quote), turns=turns, speakers=TWO, good=good, window=Window(1, turns[2:]))
    assert split == [], "echo at the window boundary was published as the host's own claim"
