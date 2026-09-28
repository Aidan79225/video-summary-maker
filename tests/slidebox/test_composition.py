"""Composition root 的轉發層：簽章必須與 port 一致。"""
from __future__ import annotations

import inspect

from slidebox.domain.entities import Settings
from slidebox.domain.ports import Summarizer


def test_the_forwarding_summarizer_takes_exactly_what_the_port_declares():
    """攔的 bug：port 加了 detailed 參數，轉發層忘了跟上。沒有任何單元測試
    會碰到 composition root，症狀是使用者按下生成才炸 TypeError。"""
    from slidebox.composition import CurrentSettingsSummarizer

    port = inspect.signature(Summarizer.summarize)
    impl = inspect.signature(CurrentSettingsSummarizer.summarize)
    assert list(impl.parameters) == list(port.parameters)


def test_the_forwarder_builds_the_summarizer_with_the_settings_of_the_moment():
    """使用者換模型後不該需要重開 app：每次生成都要用當下的 model／host。

    攔的 bug：在 __init__ 就把 OllamaSummarizer 建好（等於把當時的模型名稱
    凍住），之後改設定完全沒有作用。
    """
    from slidebox.composition import CurrentSettingsSummarizer

    built: list[tuple] = []

    class FakeImpl:
        def __init__(self, host, model, num_ctx):
            built.append((host, model, num_ctx))

        def summarize(self, *args):
            return ()

    settings = Settings(output_dir="OUT", model="a")
    fwd = CurrentSettingsSummarizer(settings, factory=FakeImpl)
    settings.model = "b"
    fwd.summarize("字幕", 60.0, 1, 3, "", lambda f, s: None)
    assert built == [(settings.ollama_host, "b", settings.num_ctx)]


def test_the_pipeline_can_be_built_without_qt():
    """長期目標是讓無介面的服務每天跑一輪產出網頁。

    攔的 bug：composition 在 module 層級 import presentation，於是
    `from slidebox.composition import build_usecase` 會拉進十幾個 PySide6
    模組——無頭機器沒有 libGL/xcb，連 import 都會失敗。在本行程裡測不
    出來（開發機裝得起 PySide6），所以開一個把 PySide6 擋掉的子行程。
    """
    import subprocess
    import sys

    code = (
        "import sys;"
        "sys.path.insert(0, 'src');"
        "sys.modules['PySide6'] = None;"
        "from slidebox.composition import build_usecase;"
        "from slidebox.domain.entities import Settings;"
        "build_usecase(Settings(output_dir='OUT'));"
        "print('ok')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, cwd=_repo_root())
    assert result.returncode == 0, result.stderr[-800:]
    assert "ok" in result.stdout


def _repo_root() -> str:
    import os
    here = os.path.abspath(__file__)
    return os.path.dirname(os.path.dirname(os.path.dirname(here)))


def test_an_ivod_url_reaches_the_ivod_gateways():
    """攔的 bug：接線接反或漏接，IVOD 網址會被送進 yt-dlp，錯誤訊息是
    無關的「Unsupported URL」。"""
    from slidebox.composition import build_usecase
    from slidebox.infrastructure.ivod_api import IvodSubtitleGateway
    from slidebox.infrastructure.ivod_sections import IvodSectionGateway

    usecase = build_usecase(Settings(output_dir="OUT"))
    subtitles = usecase._subtitles
    sections = usecase._sections
    assert isinstance(subtitles._ivod, IvodSubtitleGateway)
    assert isinstance(sections._ivod, IvodSectionGateway)
    # 兩個 IVOD adapter 必須共用同一個 client，否則同一筆 record 會抓兩次
    assert subtitles._ivod._client is sections._ivod._client


def test_the_forwarding_rewriter_takes_exactly_what_the_port_declares():
    from slidebox.composition import CurrentSettingsSlideRewriter
    from slidebox.domain.ports import SlideRewriter

    port = inspect.signature(SlideRewriter.rewrite)
    impl = inspect.signature(CurrentSettingsSlideRewriter.rewrite)
    assert list(impl.parameters) == list(port.parameters)


def test_the_pipeline_has_a_rewriter_wired_in():
    from slidebox.composition import CurrentSettingsSlideRewriter, build_usecase

    usecase = build_usecase(Settings(output_dir="OUT"))
    assert isinstance(usecase._rewriter, CurrentSettingsSlideRewriter)


def test_a_new_taipei_url_reaches_the_new_taipei_gateways():
    """攔的 bug：漏接或接反新北分支時網址會被送進 yt-dlp，錯誤訊息是無關的
    「Unsupported URL」，而且字幕、截圖、音訊三條路要各自接對才會動。"""
    import pytest

    from slidebox.composition import build_usecase
    from slidebox.domain.errors import NoSubtitlesAvailable
    from slidebox.infrastructure.ntpc_api import NtpcClient
    from slidebox.infrastructure.ntpc_audio import NtpcAudioGateway

    url = ("https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData"
           "?assetID=ebc80ece-7491-4288-be73-7c59f6b4815c")
    usecase = build_usecase(Settings(output_dir="OUT"))

    # 字幕：一律說沒有，讓 use case 走語音辨識
    with pytest.raises(NoSubtitlesAvailable, match="新北市議會沒有逐字稿"):
        usecase._subtitles.fetch(url, ("zh-TW",))

    # 音訊：新北的 gateway，接的是 NtpcClient
    audio = usecase._audio._ntpc
    assert isinstance(audio, NtpcAudioGateway)
    assert isinstance(audio._client, NtpcClient)

    # 截圖：找串流走的是同一個 NtpcClient 的播放器頁——換掉它的 fetch 就看得到
    seen: list[str] = []

    def fetch(page_url):
        seen.append(page_url)
        return ("<source src=\"https://vodwms.ntp.gov.tw:443/NTP/x.mp4/playlist.m3u8"
                "?device=PC&amp;kind=Guest\" />")

    audio._client._fetch = fetch
    stream = usecase._sections._ntpc._stream_for(url)
    assert stream == "https://vodwms.ntp.gov.tw:443/NTP/x.mp4/playlist.m3u8?device=PC&kind=Guest"
    assert len(seen) == 1 and "VideoPlayer" in seen[0]
