# -*- coding: utf-8 -*-
"""Slide titles are only checked for markers / label words / a leading pronoun-or-title / first person: a transcript name, a number nobody said and a free-standing claim are published (unattributed) as the slide title."""
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

LINES = [("S1", "今天談明年預算，志豪，你先說明一下。"), ("S2", "我認為明年預算要增加兩成才夠用，這個數字有根據。"),
         ("S1", "那金額大概是多少？"), ("S2", "兩成大約是三千億，這是我們的估算。")]


@pytest.mark.parametrize("title", ["預算案：志豪承諾增加五千億", "預算增加百分之九十九", "志豪說明預算案"])
def test_slide_title_with_a_transcript_name_or_a_number_nobody_said_is_replaced(title):
    from slidebox.usecases.conversation import validate_points, chunk_turns
    from .speaker_fixtures import make_turns
    turns = make_turns(*LINES)
    window = chunk_turns(turns, TWO)[0]
    points = (RawPoint(1, "主張明年預算要增加兩成", (), "明年預算要增加兩成才夠用"),
              RawPoint(3, "說明兩成大約是三千億", (), "兩成大約是三千億"))
    from slidebox.domain.entities import RawSlide
    result = validate_points([RawSlide(title, points)], turns, TWO, window)
    assert len(result.slides) == 1
    assert result.slides[0].title != title, "the model's title was published verbatim"
