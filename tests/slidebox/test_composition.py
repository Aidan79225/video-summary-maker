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


def test_the_forwarder_reads_the_settings_object_live():
    """設定是可變物件，使用者換模型後不該需要重開 app。"""
    from slidebox.composition import CurrentSettingsSummarizer

    settings = Settings(output_dir="OUT", model="a")
    fwd = CurrentSettingsSummarizer(settings)
    settings.model = "b"
    assert fwd._settings.model == "b"
