"""工作執行：用假的 use case，不碰 Ollama 也不碰網路。"""
from __future__ import annotations

from slidebox.domain.entities import Deck, DeckResult, Settings, Slide
from slidebox_api.jobs import JobStore
from slidebox_api.runner import SlideboxExecutor, video_id_of

IVOD = "https://ivod.ly.gov.tw/Play/Clip/1M/171180"
YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class FakeUseCase:
    def __init__(self):
        self.seen: list[Settings] = []

    def execute(self, url, settings, progress=None, is_cancelled=None):
        from dataclasses import replace
        self.seen.append(replace(settings))
        deck = Deck(url, "標題", (Slide(1, "章", ("點",), 0.0),))
        return DeckResult(deck=deck, html_path="OUT/x.html")


def _kit():
    settings = Settings(output_dir="OUT", min_slides=8, max_slides=15,
                        model="qwen3.5:9b", detailed=True)
    usecase = FakeUseCase()
    return SlideboxExecutor(usecase, settings), usecase, JobStore()


def _run(executor, job):
    return executor(job, lambda frac, status: None, lambda: False)


def test_a_job_without_overrides_uses_the_configured_defaults():
    executor, usecase, store = _kit()
    _run(executor, store.submit(IVOD))
    assert usecase.seen[0].min_slides == 8
    assert usecase.seen[0].model == "qwen3.5:9b"


def test_a_job_can_override_the_defaults():
    executor, usecase, store = _kit()
    _run(executor, store.submit(IVOD, min_slides=3, max_slides=5, model="llama3"))
    assert (usecase.seen[0].min_slides, usecase.seen[0].max_slides) == (3, 5)
    assert usecase.seen[0].model == "llama3"


def test_one_jobs_overrides_do_not_leak_into_the_next():
    """攔的 bug：設定是共用的可變物件，只寫不還原。有人手動送過一次
    min_slides=3，之後每天自動產生的每一篇都會變成 3 頁——而送那一次的人
    早就離開了，沒有人會把它跟那次手動請求連在一起。"""
    executor, usecase, store = _kit()
    _run(executor, store.submit(IVOD, min_slides=3, max_slides=5, model="llama3"))
    _run(executor, store.submit(IVOD))
    assert usecase.seen[1].min_slides == 8
    assert usecase.seen[1].max_slides == 15
    assert usecase.seen[1].model == "qwen3.5:9b"


def test_the_payload_is_keyed_by_the_ivod_id():
    executor, _, store = _kit()
    assert _run(executor, store.submit(IVOD))["video_id"] == "171180"


def test_a_youtube_url_still_gets_a_stable_key():
    assert video_id_of(YOUTUBE) == video_id_of(YOUTUBE + "&t=30")
