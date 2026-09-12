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


def test_the_pipeline_can_be_built_without_touching_the_ui():
    """長期目標是讓無介面的服務每天跑一輪。build_usecase 一旦需要 Qt 或
    設定檔，那條路就走不通了。"""
    from slidebox.composition import build_usecase

    usecase = build_usecase(Settings(output_dir="OUT"))
    assert usecase is not None


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
