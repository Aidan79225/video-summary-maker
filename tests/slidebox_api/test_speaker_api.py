"""多人模式的 HTTP 介面：能力查詢、請求規則，以及成功與失敗時 API 交出去的東西。

全部用假的 use case：不碰 Ollama、GPU、Hugging Face 或網路。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from slidebox.domain.entities import DeckResult, Settings
from slidebox.domain.errors import AttributionUnreliable, DiarizationUnavailable
from slidebox_api.app import create_app
from slidebox_api.jobs import JobStatus, JobStore
from slidebox_api.runner import JobWorker, SlideboxExecutor

from .speaker_support import deck_from_result, load_contract

CONTRACT = load_contract()
RESULT = CONTRACT["result"]
YOUTUBE = "https://www.youtube.com/watch?v=abcdefghijk"
IVOD = "https://ivod.ly.gov.tw/Play/Clip/1M/171180"


class CannedUseCase:
    """依腳本回一份成品（或丟例外），並記下每次被怎麼叫。"""

    def __init__(self, deck=None, error=None):
        self._deck = deck if deck is not None else deck_from_result(RESULT)
        self._error = error
        self.calls: list[dict] = []

    def execute(self, url, settings, progress=None, is_cancelled=None, **kwargs):
        self.calls.append(dict(url=url, **kwargs))
        if self._error is not None:
            raise self._error
        return DeckResult(self._deck, "OUT/x.html")


def kit(probe=None, usecase=None, api_key=""):
    store = JobStore()
    usecase = usecase or CannedUseCase()
    worker = JobWorker(store, SlideboxExecutor(usecase, Settings(output_dir="OUT")))
    app = create_app(store=store, worker=worker, api_key=api_key, model="m", ollama_host="http://h",
                     probe_ollama=lambda: True, probe_speaker_diarization=probe)
    return TestClient(app), store, worker, usecase


def submit(body, probe=None, usecase=None):
    """送出一個工作並回傳（回應, store, worker, usecase）；背景執行緒先停掉，由測試決定何時跑。"""
    client, store, worker, usecase = kit(probe=probe, usecase=usecase)
    with client:
        worker.stop()
        response = client.post("/jobs", json=body)
    return response, store, worker, usecase


# --- /capabilities ---


def test_a_ready_gpu_announces_the_diarization_capability_and_the_contract_version():
    client, *_ = kit(probe=lambda: (True, ""))
    with client:
        assert client.get("/capabilities").json() == {
            "speaker_diarization": True, "reason": "", "speaker_contract": 1}


def test_a_gpu_that_cannot_diarize_says_why():
    client, *_ = kit(probe=lambda: (False, "沒有設定 HUGGINGFACE_TOKEN；多人模式需要 Hugging Face token"))
    with client:
        body = client.get("/capabilities").json()
    assert body["speaker_diarization"] is False
    assert "HUGGINGFACE_TOKEN" in body["reason"]
    assert body["speaker_contract"] == 1


def test_a_probe_that_blows_up_is_reported_as_unavailable_without_leaking_its_message():
    """攔的 bug：檢查壞掉就 500（Pi 看不出原因），或把例外訊息（可能含路徑或 token）原樣交出去。"""
    secret = "hf_FAKEtokenForTestsOnly0123456789"

    def broken():
        raise RuntimeError(f"載入失敗 {secret} C:\\Users\\someone\\.cache")

    client, *_ = kit(probe=broken)
    with client:
        response = client.get("/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["speaker_diarization"] is False
    assert "RuntimeError" in body["reason"]
    assert secret not in response.text and "someone" not in response.text


def test_a_gpu_with_no_probe_wired_in_does_not_claim_the_capability():
    client, *_ = kit(probe=None)
    with client:
        body = client.get("/capabilities").json()
    assert body["speaker_diarization"] is False and body["reason"]


# --- 請求規則 ---


def test_a_request_without_speaker_fields_is_a_single_speaker_job():
    response, store, *_ = submit({"url": IVOD})
    assert response.status_code == 202
    job = store.get(response.json()["id"])
    assert (job.speaker_mode, job.speaker_count) == ("single", None)


@pytest.mark.parametrize("fields,expected", [
    ({"speaker_mode": "single"}, ("single", None)),
    ({"speaker_mode": "single", "speaker_count": 1}, ("single", 1)),
    ({"speaker_mode": "diarize"}, ("diarize", None)),
    ({"speaker_mode": "diarize", "speaker_count": None}, ("diarize", None)),
    ({"speaker_mode": "diarize", "speaker_count": 2}, ("diarize", 2)),
    ({"speaker_mode": "diarize", "speaker_count": 20}, ("diarize", 20)),
])
def test_the_allowed_combinations_reach_the_job(fields, expected):
    response, store, *_ = submit({"url": YOUTUBE, **fields})
    assert response.status_code == 202
    job = store.get(response.json()["id"])
    assert (job.speaker_mode, job.speaker_count) == expected


@pytest.mark.parametrize("fields", [
    {"speaker_mode": "single", "speaker_count": 2},     # 單人不能指定多人
    {"speaker_mode": "diarize", "speaker_count": 1},    # 一個人就是單人，不是自動區分
    {"speaker_mode": "diarize", "speaker_count": 0},
    {"speaker_mode": "diarize", "speaker_count": -2},
    {"speaker_mode": "diarize", "speaker_count": 21},
    {"speaker_mode": "diarize", "speaker_count": 2.5},
    {"speaker_mode": "diarize", "speaker_count": "兩個"},
    {"speaker_mode": "multi"},                          # 不認得的模式不會被當成單人
    {"speaker_mode": "Diarize"},
    {"speaker_mode": ""},
    {"speaker_mode": None},
    {"speaker_count": 2},                               # 沒說模式就是單人，單人沒有人數可指定
])
def test_every_other_combination_is_refused_before_any_job_exists(fields):
    response, store, *_ = submit({"url": YOUTUBE, **fields})
    assert response.status_code == 422
    assert store.counts() == (0, 0)


TOPIC = {"kind": "topic", "text": "一句話", "labels": [
    {"key": "a", "label": "甲"}, {"key": "b", "label": "乙"}]}
FOLLOWUP = {"kind": "followup", "request": "提高補助", "card": "摘要卡", "excerpt": "片段"}


@pytest.mark.parametrize("base", [TOPIC, FOLLOWUP], ids=["topic", "followup"])
@pytest.mark.parametrize("fields", [
    {"speaker_mode": "diarize"},
    {"speaker_count": 2},
    {"speaker_count": 1},
    {"speaker_mode": "single", "speaker_count": 1},
])
def test_speaker_fields_on_a_job_that_has_no_speakers_are_refused_not_ignored(base, fields):
    """送的人以為分類或追問判斷會照多人模式做；默默忽略等於讓他一直以為如此。"""
    response, store, *_ = submit({**base, **fields})
    assert response.status_code == 422
    assert store.counts() == (0, 0)


@pytest.mark.parametrize("base", [TOPIC, FOLLOWUP], ids=["topic", "followup"])
def test_the_single_default_stays_acceptable_on_every_kind_of_job(base):
    for fields in ({}, {"speaker_mode": "single"}, {"speaker_mode": "single", "speaker_count": None}):
        response, *_ = submit({**base, **fields})
        assert response.status_code == 202


# --- 一個多人工作從頭到尾 ---


def finish(body, usecase=None, probe=lambda: (True, "")):
    client, store, worker, usecase = kit(probe=probe, usecase=usecase)
    with client:
        worker.stop()
        job_id = client.post("/jobs", json=body).json()["id"]
        assert worker.run_once() is True
        return client.get(f"/jobs/{job_id}").json(), usecase


def test_a_diarize_job_delivers_the_contract_result_and_the_use_case_is_told_the_mode():
    view, usecase = finish(CONTRACT["request"])

    assert view["status"] == "done" and view["error"] == ""
    assert view["result"] == RESULT
    assert usecase.calls == [{"url": YOUTUBE, "speech_hint": "示範頻道 少子化對談",
                              "speaker_mode": "diarize", "speaker_count": None}]


def test_a_fixed_count_is_passed_on_to_the_use_case():
    _, usecase = finish({"url": YOUTUBE, "speaker_mode": "diarize", "speaker_count": 3})
    assert usecase.calls[0]["speaker_mode"] == "diarize" and usecase.calls[0]["speaker_count"] == 3


def test_an_old_style_request_calls_the_use_case_exactly_as_before():
    """議會的 client 不知道多人模式：use case 收到的參數與沒有這個功能時完全相同。"""
    view, usecase = finish({"url": IVOD}, usecase=CannedUseCase(deck=_single_deck()))
    assert usecase.calls == [{"url": IVOD, "speech_hint": None}]
    assert view["status"] == "done" and "speaker_mode" not in view["result"]


def _single_deck():
    from slidebox.domain.entities import Deck, Slide
    return Deck(IVOD, "標題", (Slide(1, "章", ("點",), 0.0),))


def test_a_declared_single_speaker_job_tells_the_use_case_so():
    """操作者明講「只有一個人」（speaker_count=1）：use case 收到的是明確的單人，不是沒說。"""
    _, usecase = finish({"url": YOUTUBE, "speaker_mode": "single", "speaker_count": 1},
                        usecase=CannedUseCase(deck=_single_deck()))
    assert usecase.calls == [{"url": YOUTUBE, "speech_hint": None,
                              "speaker_mode": "single", "speaker_count": 1}]


@pytest.mark.parametrize("error", [
    AttributionUnreliable("說話者歸屬不可靠：模型提出 10 個重點，有 4 個（40%）沒通過檢查"),
    DiarizationUnavailable("沒有設定 HUGGINGFACE_TOKEN；多人模式不會退回單人的寫法，工作中止"),
    RuntimeError("出片失敗"),
], ids=lambda e: type(e).__name__)
def test_when_attribution_fails_the_job_fails_with_the_reason_and_nothing_is_delivered(error):
    view, _ = finish(CONTRACT["request"], usecase=CannedUseCase(error=error))

    assert view["status"] == "failed"
    assert view["result"] is None
    assert type(error).__name__ in view["error"] and str(error) in view["error"]


def test_a_conversation_with_more_than_twelve_voices_fails_the_job_and_delivers_nothing():
    """真的 use case（假零件）從頭到尾：13 個聲音，工作失敗、沒有成品、沒有任何一次模型呼叫，
    而且前兩個階段的存檔還在，錯誤訊息把人數與下一步講清楚。"""
    from tests.slidebox import diarized_support as support

    parts = support.crowd_parts(10)

    view, _ = finish(CONTRACT["request"], usecase=parts.usecase)

    assert view["status"] == JobStatus.FAILED and view["result"] is None
    assert view["error"].startswith("AttributionUnreliable: ")
    for needle in ("2 位主要發言者", "11 個其他聲音", "共 13 個", "上限 12 個", "--speakers N"):
        assert needle in view["error"], needle
    assert parts.conversation.calls == [] and parts.renderer.rendered == []
    assert parts.checkpoints.stages("OUT", support.URL) == ["diarization", "words"]


def test_a_diarized_deck_that_lost_its_sources_is_not_delivered():
    """use case 若因為任何原因交出缺出處的成品，payload 這一層是最後一道關：任務失敗，不是交出去。"""
    from dataclasses import replace
    deck = deck_from_result(RESULT)
    bare = replace(deck, slides=(deck.slides[0], replace(deck.slides[1], points=())))

    view, _ = finish(CONTRACT["request"], usecase=CannedUseCase(deck=bare))

    assert view["status"] == JobStatus.FAILED
    assert view["result"] is None
    assert "第 2 頁" in view["error"]
