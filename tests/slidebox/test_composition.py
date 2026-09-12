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
