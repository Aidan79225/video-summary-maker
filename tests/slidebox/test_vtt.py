"""WebVTT 解析：時間軸、多行、內嵌標籤、BOM、NOTE 區塊。"""
from __future__ import annotations

from slidebox.usecases.chapters import parse_vtt

BASIC = """WEBVTT
Kind: captions
Language: zh-TW

00:00:01.000 --> 00:00:03.500
第一句

00:01:02.250 --> 00:01:04.000
第二句
"""


def test_parses_timings_and_text():
    cues = parse_vtt(BASIC)
    assert len(cues) == 2
    assert cues[0].start == 1.0
    assert cues[0].end == 3.5
    assert cues[0].text == "第一句"
    assert cues[1].start == 62.25


def test_skips_header_and_note_blocks():
    text = BASIC.replace("\n\n00:00:01", "\n\nNOTE 這是註解\n\n00:00:01")
    assert len(parse_vtt(text)) == 2


def test_strips_bom():
    assert len(parse_vtt("﻿" + BASIC)) == 2


def test_handles_crlf():
    assert len(parse_vtt(BASIC.replace("\n", "\r\n"))) == 2


def test_removes_inline_tags():
    """自動字幕帶 <c> 與逐字時間戳，必須清掉。"""
    text = (
        "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n"
        "<00:00:01.100><c>你好</c><00:00:01.500><c> 世界</c>\n"
    )
    assert parse_vtt(text)[0].text == "你好 世界"


def test_joins_multiline_cue():
    text = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n第一行\n第二行\n"
    assert parse_vtt(text)[0].text == "第一行 第二行"


def test_ignores_cue_identifier_line():
    """時間軸前面可以有一行 cue 編號，不能被當成內文。"""
    text = "WEBVTT\n\ncue-7\n00:00:01.000 --> 00:00:03.000\n內文\n"
    cues = parse_vtt(text)
    assert len(cues) == 1
    assert cues[0].text == "內文"


def test_unescapes_html_entities():
    text = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nA &amp; B\n"
    assert parse_vtt(text)[0].text == "A & B"


def test_keeps_cue_settings_out_of_text():
    """時間軸後面的排版參數不是內文。"""
    text = (
        "WEBVTT\n\n00:00:01.000 --> 00:00:03.000 align:start position:0%\n乾淨\n"
    )
    assert parse_vtt(text)[0].text == "乾淨"


def test_empty_cue_is_dropped():
    text = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n\n\n00:00:04.000 --> 00:00:05.000\n有內容\n"
    cues = parse_vtt(text)
    assert len(cues) == 1
    assert cues[0].text == "有內容"


def test_empty_input_returns_empty_tuple():
    assert parse_vtt("") == ()
