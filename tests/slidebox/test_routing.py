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


def test_cleanup_reaches_the_second_gateway_even_if_the_first_misbehaves():
    """port 契約說 cleanup 不可 raise，但萬一某個實作違約，不該連帶讓
    另一邊的暫存目錄留在硬碟上。"""
    class Exploding:
        def cleanup(self, dest_dir):
            raise RuntimeError("違約了")

    ivod = Spy("ivod")
    BySourceSectionGateway(Exploding(), ivod).cleanup("某個目錄")
    assert ivod.cleaned == ["某個目錄"]


def test_the_routers_take_exactly_what_the_ports_declare():
    """攔的 bug：port 演進時轉發層漏跟。這一層是純轉發，簽章少一個參數
    不會有任何測試抓到，只會在使用者按下生成時炸成 TypeError。"""
    import inspect

    from slidebox.domain.ports import AudioGateway, SubtitleGateway, VideoSectionGateway
    from slidebox.infrastructure.routing import BySourceAudioGateway

    pairs = [
        (SubtitleGateway.fetch, BySourceSubtitleGateway.fetch),
        (VideoSectionGateway.download_sections, BySourceSectionGateway.download_sections),
        (VideoSectionGateway.cleanup, BySourceSectionGateway.cleanup),
        (AudioGateway.download_audio, BySourceAudioGateway.download_audio),
        (AudioGateway.cleanup, BySourceAudioGateway.cleanup),
    ]
    for port, impl in pairs:
        assert list(inspect.signature(impl).parameters) == \
            list(inspect.signature(port).parameters), port.__qualname__


# --- 臺中市議會 ---

TCCC = "https://vod.tccc.gov.tw/index.asp?url=12&cno=85&ano=14833"


class AudioSpy:
    def __init__(self, name):
        self.name = name
        self.downloaded = []
        self.cleaned = []

    def download_audio(self, url, dest_dir, progress, is_cancelled):
        self.downloaded.append(url)
        return self.name

    def cleanup(self, dest_dir):
        self.cleaned.append(dest_dir)


def test_taichung_urls_go_to_the_taichung_branch():
    yt, ivod, tccc = Spy("yt"), Spy("ivod"), Spy("tccc")
    assert BySourceSubtitleGateway(yt, ivod, tccc).fetch(TCCC, ()) == "tccc"
    assert BySourceSectionGateway(yt, ivod, tccc).download_sections(
        TCCC, [], None, "d", None, None) == ["tccc"]
    assert BySourceSubtitleGateway(yt, ivod, tccc).fetch(IVOD, ()) == "ivod"
    assert BySourceSubtitleGateway(yt, ivod, tccc).fetch(YOUTUBE, ()) == "yt"


def test_without_a_taichung_branch_the_default_handles_it():
    """桌面 app 沒接臺中那一套：退回 yt-dlp，錯誤訊息會是它的「Unsupported URL」。"""
    yt, ivod = Spy("yt"), Spy("ivod")
    assert BySourceSubtitleGateway(yt, ivod).fetch(TCCC, ()) == "yt"


def test_audio_routes_taichung_and_falls_back_to_the_default():
    from slidebox.infrastructure.routing import BySourceAudioGateway

    yt, tccc = AudioSpy("yt"), AudioSpy("tccc")
    router = BySourceAudioGateway(yt, tccc)
    assert router.download_audio(TCCC, "d", None, None) == "tccc"
    assert router.download_audio(YOUTUBE, "d", None, None) == "yt"
    router.cleanup("x")
    assert yt.cleaned == ["x"] and tccc.cleaned == ["x"]


def test_section_cleanup_reaches_the_taichung_branch_too():
    yt, ivod, tccc = Spy("yt"), Spy("ivod"), Spy("tccc")
    BySourceSectionGateway(yt, ivod, tccc).cleanup("d")
    assert tccc.cleaned == ["d"]
