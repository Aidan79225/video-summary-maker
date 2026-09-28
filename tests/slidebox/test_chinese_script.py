"""語音辨識簡體漂移的修正：只轉漂掉的段落，正體段落一字不動。

大部分例子來自 review 時實際跑出來的錯誤，每一個都曾經被改壞過。
"""
from __future__ import annotations

import os

import pytest

from slidebox.infrastructure.chinese_script import (
    TraditionalFixer,
    _tables,
    keep_terms_from,
    looks_simplified,
    to_traditional,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _lines(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return [line.rstrip("\n") for line in f if line.strip()]


def _run(segments, keep=()):
    fixer = TraditionalFixer(keep)
    return [fixer(s) for s in segments]


# --- 正體不能碰 ---

@pytest.mark.parametrize("text", [
    # 整份丟 OpenCC 時被改壞的真實逐字稿
    "是不是可以請去公所請里長", "不是只有永和", "就是細紙淡水萬里共聊跟三支", "了解 了解",
    "所以他便需要分區", "要分布很多點齁",
    # 臺灣標準字被當成簡體（群床峰秘）而整段轉換
    "有一群里民", "全里的族群", "郁慕明一群人", "游淑慧的秘密", "于美人一群人", "余家的秘方",
    "病床的核准", "排泄的病床", "占床率", "台南的病床", "舞台上一群人", "台北的尖峰",
    "主秘也是只有", "不是只有族群", "于美人主秘",
    # OpenCC 當成簡體、臺灣正確的字
    "雇主的責任", "就業服務法規定雇主", "雇員", "牆壁都發霉了", "霉運", "倒霉", "苧麻",
    "我是家裡的老么", "庄跤",
    "",
])
def test_traditional_text_is_left_alone(text):
    assert to_traditional(text) == text


def test_a_traditional_run_stays_untouched_even_with_shared_only_segments():
    segments = ["請里長幫忙", "然后", "拜托一下", "新北市里面"]
    # 前一段是正體，後面只有共用字的段落不知道是哪一種：沿用正體、不動
    assert _run(segments) == segments


# --- 漂移的段落要轉 ---

@pytest.mark.parametrize("text,expected", [
    ("我这样是不对的", "我這樣是不對的"),
    ("为什么你知道吗", "為什麼你知道嗎"),
    ("法治单也会支援", "法治單也會支援"),
    # 段落裡已經是正體的詞要先正規化，才對得到 OpenCC 的詞庫
    ("请里長帮忙", "請里長幫忙"),
    ("请公所的里長来", "請公所的里長來"),
    ("淡水萬里这边", "淡水萬里這邊"),
    # OpenCC 詞庫的「是只→是隻」
    ("不是只有永和的问题", "不是只有永和的問題"),
    ("我们不是只有这样", "我們不是只有這樣"),
    ("就是只有这个", "就是只有這個"),
    ("他就是只要钱", "他就是只要錢"),
    ("我们这里有两只狗", "我們這裡有兩隻狗"),
    # 臺灣用語
    ("总统咨文", "總統咨文"),
    ("一群里民说", "一群里民說"),
    ("外籍移工的雇主说", "外籍移工的雇主說"),
    ("你在干什么", "你在幹什麼"),
    # 保護詞不能從別的詞中間切出來
    ("这里长期以来", "這裡長期以來"),
    ("这里民众很多", "這裡民眾很多"),
])
def test_a_drifted_segment_becomes_traditional(text, expected):
    assert looks_simplified(text)
    assert to_traditional(text) == expected


def test_speaker_names_are_protected():
    keep = keep_terms_from("新北市議會 市政總質詢。發言者：游淑慧、范雲")
    assert to_traditional("游淑慧议员说", keep) == "游淑慧議員說"
    assert to_traditional("范云委员说", keep) == "范雲委員說"


def test_shared_only_segments_inside_a_drifted_run_are_converted():
    out = _run(["我们来讲一下", "然后", "拜托一下", "新北市里面", "土地征收"])
    assert out == ["我們來講一下", "然後", "拜託一下", "新北市裡面", "土地徵收"]


def test_a_protected_term_is_not_cut_out_of_a_neighbouring_word():
    assert _run(["我们来讲一下", "然后里面的人"]) == ["我們來講一下", "然後裡面的人"]


def test_the_run_ends_when_traditional_comes_back():
    out = _run(["我们来讲一下", "謝謝議員", "然后"])
    assert out == ["我們來講一下", "謝謝議員", "然后"]


# --- 正體段落夾幾個簡體字 ---

@pytest.mark.parametrize("text,expected", [
    ("市長你知道吗", "市長你知道嗎"),
    ("議員還有五分钟", "議員還有五分鐘"),
    ("請議員签名", "請議員簽名"),
    ("這個問題很复雜", "這個問題很複雜"),
    ("議員的头发", "議員的頭髮"),
    ("今天是農历初一", "今天是農曆初一"),
    ("預算編在这里", "預算編在這裡"),
])
def test_a_few_simplified_characters_in_traditional_text_are_fixed_in_context(text, expected):
    assert to_traditional(text) == expected


# --- 真實逐字稿回歸 ---

def test_real_traditional_transcript_is_untouched_where_it_was_traditional():
    lines = _lines("ntpc_whisper_traditional.txt")
    out = _run(lines)
    simplified_only, _, _, _ = _tables()
    wrongly = [(a, b) for a, b in zip(lines, out)
               if a != b and not looks_simplified(a) and not any(ch in simplified_only for ch in a)
               and not _preceded_by_drift(lines, a)]
    assert wrongly == []


def _preceded_by_drift(lines, line):
    i = lines.index(line)
    for prev in reversed(lines[:i]):
        simplified_only, traditional_only, _, _ = _tables()
        s = sum(c in simplified_only for c in prev)
        t = sum(c in traditional_only for c in prev)
        if s != t:
            return s > t
    return False


def test_real_drifted_transcript_leaves_no_simplified_text():
    _, _, to_trad, to_simp = _tables()
    out = _run(_lines("ntpc_whisper_drifted.txt"))
    # 比 simplified_only 更嚴：整段再轉一次若還會變，就是還有簡體沒轉到
    residue = [o for o in out if to_trad(to_simp(o)) != o and looks_simplified(o)]
    assert residue == []
