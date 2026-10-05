"""工作執行：用假的 use case，不碰 Ollama 也不碰網路。"""
from __future__ import annotations

from slidebox.domain.entities import (
    Deck,
    DeckResult,
    Settings,
    Slide,
    TopicLabel,
    TopicResult,
)
from slidebox.domain.errors import SummarizerOutputInvalid
from slidebox_api.jobs import JobKind, JobStatus, JobStore
from slidebox_api.runner import (
    ByKindExecutor,
    JobWorker,
    SlideboxExecutor,
    TopicExecutor,
    video_id_of,
)

IVOD = "https://ivod.ly.gov.tw/Play/Clip/1M/171180"
YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class FakeUseCase:
    def __init__(self):
        self.seen: list[Settings] = []

    def execute(self, url, settings, progress=None, is_cancelled=None, speech_hint=None):
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


def test_a_taichung_clip_is_keyed_by_its_prefixed_ano():
    assert video_id_of("https://vod.tccc.gov.tw/index.asp?url=12&cno=85&ano=14833") == "tccc-14833"


def test_a_new_taipei_clip_is_keyed_by_its_prefixed_lowercase_guid():
    guid = "ebc80ece-7491-4288-be73-7c59f6b4815c"
    assert video_id_of(
        f"https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewMetaData?assetID={guid.upper()}"
    ) == f"ntpc-{guid}"
    assert video_id_of(
        f"https://vod.ntp.gov.tw/VodCloudV2/VOD/ViewDetailMetaData/{guid}") == f"ntpc-{guid}"


# --- 分類工作與依種類分派 ---

LABELS = (TopicLabel("defense", "國防外交"), TopicLabel("welfare", "衛生福利"))
TEXT = "一句話：要求衛福部說明長照 3.0 的預算"


class FakeClassifier:
    """記下自己被用哪個模型建出來、拿到什麼；回一個固定的答案或錯誤。"""

    def __init__(self, model, calls, result=None, error=None):
        self._model = model
        self._calls = calls
        self._result = result
        self._error = error

    def classify(self, text, labels, progress, is_cancelled=None):
        self._calls.append((self._model, text, tuple(labels)))
        if self._error is not None:
            raise self._error
        return self._result or TopicResult("welfare", None, f"{self._model}#topic-v1")


def _topic_executor(model="qwen3.5:9b", error=None):
    calls: list = []
    executor = TopicExecutor(lambda m: FakeClassifier(m, calls, error=error), model)
    return executor, calls


def _topic_job(store, **overrides):
    fields = {"kind": JobKind.TOPIC, "text": TEXT, "labels": LABELS}
    fields.update(overrides)
    return store.submit(**fields)


def test_a_topic_job_returns_primary_secondary_and_classifier():
    executor, _ = _topic_executor()
    payload = _run(executor, _topic_job(JobStore()))
    assert payload == {"primary": "welfare", "secondary": None,
                       "classifier": "qwen3.5:9b#topic-v1"}


def test_the_classifier_gets_the_jobs_text_and_labels():
    executor, calls = _topic_executor()
    _run(executor, _topic_job(JobStore()))
    assert calls == [("qwen3.5:9b", TEXT, LABELS)]


def test_a_topic_job_can_name_its_own_model():
    executor, calls = _topic_executor()
    _run(executor, _topic_job(JobStore(), model="llama3"))
    assert calls[0][0] == "llama3"


def test_a_deck_jobs_model_does_not_leak_into_later_topic_jobs():
    """攔的 bug：分類讀那個共用的 Settings 的話，有人手動送過一次 model=llama3
    的摘要，之後的分類都會悄悄換成 llama3——classifier 名稱跟著變，新聞服務會
    把這些結果全部當成沒評估過的版本。"""
    deck, _, store = _kit()
    topic, calls = _topic_executor(model="qwen3.5:9b")
    _run(deck, store.submit(IVOD, model="llama3"))
    _run(topic, _topic_job(store))
    assert calls[0][0] == "qwen3.5:9b"


def test_each_kind_goes_to_its_own_executor():
    deck, usecase, store = _kit()
    topic, calls = _topic_executor()
    dispatch = ByKindExecutor({JobKind.DECK: deck, JobKind.TOPIC: topic})
    assert _run(dispatch, store.submit(IVOD))["video_id"] == "171180"
    assert _run(dispatch, _topic_job(store))["primary"] == "welfare"
    assert len(usecase.seen) == 1
    assert len(calls) == 1


def test_a_kind_without_an_executor_fails_that_job_and_the_queue_moves_on():
    deck, _, store = _kit()
    worker = JobWorker(store, ByKindExecutor({JobKind.DECK: deck}))
    stranded = _topic_job(store)
    after = store.submit(IVOD)
    worker.run_once()
    worker.run_once()
    assert stranded.status == JobStatus.FAILED
    assert "topic" in stranded.error
    assert after.status == JobStatus.DONE


def test_the_worker_runs_a_topic_job_through_the_same_queue():
    deck, _, store = _kit()
    topic, _ = _topic_executor()
    worker = JobWorker(store, ByKindExecutor({JobKind.DECK: deck, JobKind.TOPIC: topic}))
    job = _topic_job(store)
    assert worker.run_once() is True
    assert job.status == JobStatus.DONE
    assert job.result["classifier"] == "qwen3.5:9b#topic-v1"


def test_an_answer_outside_the_list_fails_the_job_with_its_reason():
    """不猜：主領域對不回清單，這個工作就是失敗，新聞服務下一輪再送。"""
    _, _, store = _kit()
    topic, _ = _topic_executor(error=SummarizerOutputInvalid("主領域「經濟」不在清單裡"))
    worker = JobWorker(store, ByKindExecutor({JobKind.TOPIC: topic}))
    job = _topic_job(store)
    worker.run_once()
    assert job.status == JobStatus.FAILED
    assert "不在清單裡" in job.error
    assert job.result is None
