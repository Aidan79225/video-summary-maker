"""來源判別：純字串處理，不碰網路。"""
from __future__ import annotations

import pytest

from slidebox.usecases.sources import ivod_id


@pytest.mark.parametrize("url,expected", [
    ("https://ivod.ly.gov.tw/Play/Clip/1M/171180", "171180"),
    ("https://ivod.ly.gov.tw/Play/Full/1M/17704", "17704"),
    ("https://ivod.ly.gov.tw/Play/Clip/300K/154164", "154164"),
    ("http://ivod.ly.gov.tw/Play/Clip/1M/171180/", "171180"),
    ("https://ivod.ly.gov.tw/Play/Clip/1M/171180?x=1", "171180"),
    ("  https://ivod.ly.gov.tw/Play/Clip/1M/171180  ", "171180"),
])
def test_recognises_the_ivod_forms(url, expected):
    assert ivod_id(url) == expected


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://ivod.ly.gov.tw/",
    "https://ivod.ly.gov.tw/Play/Clip/1M/",
    "",
])
def test_everything_else_is_not_ivod(url):
    assert ivod_id(url) is None


def test_a_youtube_video_whose_id_looks_like_a_path_is_not_mistaken_for_ivod():
    """攔的 bug：只比對「網址裡有數字」會把一般影片誤判成 IVOD，
    接著整條 pipeline 會去打立法院 API 找一個不存在的 id。"""
    assert ivod_id("https://www.youtube.com/watch?v=12345678901") is None


def test_a_lookalike_host_is_not_accepted():
    """攔的 bug：用 in 比對主機名，evil-ivod.ly.gov.tw.attacker.com 會被當成
    自己人，程式就會把使用者貼的網址拿去對別人的伺服器組 API 請求。"""
    assert ivod_id("https://ivod.ly.gov.tw.attacker.com/Play/Clip/1M/1") is None
