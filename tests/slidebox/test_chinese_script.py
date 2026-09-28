"""Whisper 簡體漂移的修正：只轉漂掉的段落，正體段落一字不動。"""
from __future__ import annotations

import os

import pytest

from slidebox.infrastructure.chinese_script import _tables, looks_simplified, to_traditional

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _lines(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return [line.rstrip("\n") for line in f if line.strip()]


@pytest.mark.parametrize("text", [
    # 這幾行是整份丟進 OpenCC s2t／s2tw 時被改壞的真實例子
    "是不是可以請去公所請里長",
    "不是只有永和",
    "就是細紙淡水萬里共聊跟三支",
    "了解 了解",
    "所以他便需要分區",
    "要分布很多點齁",
    "",
])
def test_traditional_or_shared_text_is_left_alone(text):
    assert to_traditional(text) == text


@pytest.mark.parametrize("text,expected", [
    ("我这样是不对的", "我這樣是不對的"),
    ("为什么你知道吗", "為什麼你知道嗎"),
    ("我们就是", "我們就是"),
    ("法治单也会支援", "法治單也會支援"),
])
def test_a_drifted_segment_becomes_traditional(text, expected):
    assert looks_simplified(text)
    assert to_traditional(text) == expected


def test_a_mixed_segment_only_has_its_simplified_characters_fixed():
    """「市長你知道吗」：正體專用字與簡體專用字各一，不算整段漂移；只修「吗」。"""
    assert to_traditional("市長你知道吗") == "市長你知道嗎"


def test_real_traditional_transcript_is_untouched():
    """回歸：新北市議會實際逐字稿前 9 分鐘（正體）轉完必須一字不差。"""
    lines = _lines("ntpc_whisper_traditional.txt")
    changed = [(t, to_traditional(t)) for t in lines if to_traditional(t) != t]
    # 這一段裡偶爾有幾句已經漂成簡體，那些本來就該轉；正體句子不能被碰
    simplified_only, _, _ = _tables()
    wrongly = [(a, b) for a, b in changed if not any(ch in simplified_only for ch in a)]
    assert wrongly == []


def test_real_drifted_transcript_leaves_no_simplified_only_characters():
    simplified_only, _, _ = _tables()
    out = [to_traditional(t) for t in _lines("ntpc_whisper_drifted.txt")]
    residue = [o for o in out if any(ch in simplified_only for ch in o)]
    assert residue == []
