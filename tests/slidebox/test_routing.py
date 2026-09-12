"""依來源分派的 adapter：只驗「誰被叫到」。"""
from __future__ import annotations

from slidebox.infrastructure.routing import (
    BySourceSectionGateway,
    BySourceSubtitleGateway,
)

IVOD = "https://ivod.ly.gov.tw/Play/Clip/1M/171180"
YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class Spy:
    def __init__(self, name):
        self.name = name
        self.fetched: list[str] = []
        self.downloaded: list[str] = []
        self.cleaned: list[str] = []

    def fetch(self, url, langs):
        self.fetched.append(url)
        return self.name

    def download_sections(self, url, timestamps, max_height, dest_dir, progress,
                          is_cancelled):
        self.downloaded.append(url)
        return [self.name]

    def cleanup(self, dest_dir):
        self.cleaned.append(dest_dir)


def test_subtitles_go_to_the_gateway_that_understands_the_url():
    yt, ivod = Spy("yt"), Spy("ivod")
    gateway = BySourceSubtitleGateway(yt, ivod)
    assert gateway.fetch(IVOD, ()) == "ivod"
    assert gateway.fetch(YOUTUBE, ()) == "yt"
    assert ivod.fetched == [IVOD]
    assert yt.fetched == [YOUTUBE]


def test_sections_go_to_the_gateway_that_understands_the_url():
    yt, ivod = Spy("yt"), Spy("ivod")
    gateway = BySourceSectionGateway(yt, ivod)
    assert gateway.download_sections(IVOD, [], None, "d", None, None) == ["ivod"]
    assert gateway.download_sections(YOUTUBE, [], None, "d", None, None) == ["yt"]


def test_cleanup_reaches_both_because_it_is_not_told_which_url_it_was_for():
    """攔的 bug：cleanup 的簽章沒有 url，猜錯邊就會把暫存片段留在硬碟上。
    兩個實作的 cleanup 都是「刪掉這個目錄，不存在也不報錯」，呼叫兩次是
    安全的。"""
    yt, ivod = Spy("yt"), Spy("ivod")
    BySourceSectionGateway(yt, ivod).cleanup("某個目錄")
    assert yt.cleaned == ["某個目錄"]
    assert ivod.cleaned == ["某個目錄"]
