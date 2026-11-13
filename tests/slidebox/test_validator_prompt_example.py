# -*- coding: utf-8 -*-
"""Prompt/validator contradiction on one-word answers (rule 9 and the worked example vs locate_quote)."""
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

def test_the_worked_example_in_the_system_prompt_is_not_rejected_by_the_quote_guard():
    """The system prompt (rule 9 + the example point {"quote": "對", "turn": 5, ...}) teaches the model to quote a one-word answer;
    locate_quote rejects every quote shorter than 2 characters (BAD_QUOTE), so that point can never be published."""
    import json
    import re
    from slidebox.domain.entities import RawPoint as RP
    from slidebox.infrastructure.ollama_conversation import conversation_system_prompt
    from slidebox.usecases.conversation import BAD_QUOTE, _check_point, citable_ids
    from slidebox.usecases.names import detect_names
    from .speaker_fixtures import make_turns

    prompt = conversation_system_prompt()
    example = prompt.split("逐字稿：\n", 1)[1]
    transcript, output = example.split("輸出：\n", 1)
    lines = re.findall(r"\[T(\d+)\] (\w+)：(.*)", transcript)
    turns = make_turns(*[(("S9" if sp == "X" else sp), text) for _, sp, text in lines])
    speakers = make_speakers("participant", "participant", "participant")
    by_index = {t.index: t for t in turns}
    citable = citable_ids(speakers)
    names = detect_names("\n".join(t.text for t in turns))
    codes = {}
    for slide in json.loads(output.strip())["slides"]:
        for p in slide["points"]:
            res = _check_point(RP(p["turn"], p["text"], tuple(p["context"]), p["quote"]), by_index, list(turns), frozenset(citable),
                               0, len(turns) - 1, names)
            codes[(p["turn"], p["quote"])] = res.problem.code if res.problem else "kept"
    assert BAD_QUOTE not in codes.values(), codes
