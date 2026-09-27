"""工作執行：用假的 use case，不碰 Ollama 也不碰網路。"""
from __future__ import annotations

from datetime import date as date_type

import pytest

from factcheck.domain.entities import (
    CheckedClaim, ClaimKind, ExtractedClaim, Method, Verdict,
)
from factcheck.domain.errors import OperationCancelled as FactCheckCancelled
from slidebox.domain.entities import Deck, DeckResult, Settings, Slide
from slidebox.domain.errors import OperationCancelled
from slidebox_api.jobs import JobKind, JobStore
from slidebox_api.runner import FactCheckExecutor, KindDispatcher, SlideboxExecutor, video_id_of

IVOD = "https://ivod.ly.gov.tw/Play/Clip/1M/171180"
YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
FACTCHECK_PARAMS = {"speaker": "邱慧洳", "date": "2026-08-25", "meeting": "院會",
                    "transcript_text": "00:32 他的行政罰鍰從現行的3萬到5萬"}


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


class FakeFactCheck:
    def __init__(self, error=None):
        self.speeches = []
        self.error = error

    def execute(self, speech, progress, is_cancelled):
        self.speeches.append(speech)
        if self.error is not None:
            raise self.error
        claim = ExtractedClaim(quote="3萬到5萬", kind=ClaimKind.LAW_ARTICLE, statement="罰鍰")
        return [CheckedClaim(claim, 32.0, Verdict.SUPPORTED, Method.NUMERIC, "相同")]


def test_the_dispatcher_routes_by_kind():
    store = JobStore()
    seen = []
    dispatcher = KindDispatcher({
        JobKind.DECK: lambda job, p, c: seen.append("deck") or {},
        JobKind.FACTCHECK: lambda job, p, c: seen.append("factcheck") or {},
    })
    dispatcher(store.submit(IVOD, kind=JobKind.FACTCHECK, params={}), lambda f, s: None,
               lambda: False)
    assert seen == ["factcheck"]


def test_an_unknown_kind_fails_the_job():
    store = JobStore()
    with pytest.raises(ValueError):
        KindDispatcher({})(store.submit(IVOD), lambda f, s: None, lambda: False)


def test_the_factcheck_executor_builds_the_speech_from_the_job():
    usecase = FakeFactCheck()
    store = JobStore()
    job = store.submit(IVOD, kind=JobKind.FACTCHECK, params=FACTCHECK_PARAMS)
    payload = FactCheckExecutor(usecase, "qwen3.5:9b")(job, lambda f, s: None, lambda: False)
    speech = usecase.speeches[0]
    assert speech.speaker == "邱慧洳"
    assert speech.date == date_type(2026, 8, 25)
    assert payload["claims"][0]["verdict"] == "supported"


def test_factcheck_cancellation_becomes_the_workers_cancellation():
    """工作執行緒只認得 slidebox 的 OperationCancelled；不轉譯的話取消會被記成失敗。"""
    store = JobStore()
    job = store.submit(IVOD, kind=JobKind.FACTCHECK, params=FACTCHECK_PARAMS)
    with pytest.raises(OperationCancelled):
        FactCheckExecutor(FakeFactCheck(error=FactCheckCancelled()), "m")(
            job, lambda f, s: None, lambda: True)
