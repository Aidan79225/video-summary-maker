"""Ollama 串流途中的取消：用假的 HTTP 回應，不碰網路。"""
from __future__ import annotations

import json

import pytest

from slidebox.domain.errors import OperationCancelled
from slidebox.infrastructure import ollama_summarizer as mod


class FakeResponse:
    """模擬 urlopen 回傳的 NDJSON 串流。"""

    def __init__(self, lines):
        self._lines = lines
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def __iter__(self):
        for line in self._lines:
            yield line.encode("utf-8")


def _chunks(n: int) -> list[str]:
    out = [json.dumps({"message": {"content": "字"}}) for _ in range(n)]
    out.append(json.dumps({"message": {"content": ""}, "done": True}))
    return out


def test_cancel_stops_the_stream_instead_of_waiting_for_the_whole_answer(monkeypatch):
    """攔的 bug：取消只在每次嘗試「之前」檢查，串流中完全不理。詳細模式的
    輸出量近十倍，按下取消後會停在「取消中…」好幾分鐘，整個佇列卡住。"""
    response = FakeResponse(_chunks(50))
    monkeypatch.setattr(mod, "_post_json", lambda *a, **k: response)
    seen = []

    def progress(frac, status):
        seen.append(status)

    summarizer = mod.OllamaSummarizer("http://x", "m", 1024)
    with pytest.raises(OperationCancelled):
        summarizer.summarize("字幕", 60.0, 1, 3, "", progress,
                             is_cancelled=lambda: len(seen) >= 3)
    assert len(seen) < 10          # 沒有把 50 個 chunk 全部收完
    assert response.closed         # 連線有關掉，不是留著慢慢流


def test_without_cancellation_the_whole_answer_is_read(monkeypatch):
    payload = json.dumps({"slides": [
        {"title": "章", "bullets": ["點"], "timestamp": 0}]})
    lines = [json.dumps({"message": {"content": c}}) for c in payload]
    lines.append(json.dumps({"message": {"content": ""}, "done": True}))
    monkeypatch.setattr(mod, "_post_json", lambda *a, **k: FakeResponse(lines))

    slides = mod.OllamaSummarizer("http://x", "m", 1024).summarize(
        "字幕", 60.0, 1, 3, "", lambda f, s: None, is_cancelled=lambda: False)
    assert slides[0].title == "章"
